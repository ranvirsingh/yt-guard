"""Leave YouTube videos on the TV that match a blocked keyword, channel, or video.

First press Back; if the same video is still playing, close the YouTube app.
Rules are edited in a small web UI (port 8080) and saved to RULES_FILE.

Connects to the TV's YouTube app over the Lounge API (the same link phones use to cast).

  TV_IP=<tv-ip> uv run yt_guard.py            # run the guard + UI
  uv run yt_guard.py pair <tv-ip>             # optional: pair and save auth
"""
import asyncio
import collections
import html
import ipaddress
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from aiohttp import web
from pyytlounge import EventListener, State, YtLoungeApi
from pyytlounge.dial import get_screen_id_from_dial
from pyytlounge.models import DpadCommand

HERE = Path(__file__).parent
AUTH = Path(os.environ.get("AUTH_FILE", HERE / "auth.json"))
RULES_FILE = Path(os.environ.get("RULES_FILE", HERE / "rules.json"))
SEED_KEYWORDS = os.environ.get("KEYWORDS", "minecraft,roblox,fortnite")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "YT Guard")
TV_IP = os.environ.get("TV_IP", "")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))
DISCOVERY_CIDR = os.environ.get("DISCOVERY_CIDR", "")
DISCOVERY_INTERVAL = max(0.0, float(os.environ.get("DISCOVERY_INTERVAL", "60")))
ACTION_GAP = 3.0  # seconds between commands; a burst of commands can trigger the TV's disconnect prompt
KINDS = ("keywords", "channels", "videos")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("yt-guard")


# ---------------------------------------------------------------- rules

def parse_video_id(text: str) -> str | None:
    """Accept a bare 11-char id or any watch / youtu.be / shorts / live / embed URL."""
    text = text.strip()
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) else None


def normalize_channel(text: str) -> str:
    """Channel name, @handle, or channel URL -> lowercase name/handle without '@'."""
    text = text.strip()
    m = re.search(r"youtube\.com/@([^/?#\s]+)", text)
    return (m.group(1) if m else text).lstrip("@").strip().lower()


class Rules:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            data = json.loads(path.read_text())
        else:
            data = {"keywords": [k.strip().lower() for k in SEED_KEYWORDS.split(",") if k.strip()]}
        self.data = {k: list(dict.fromkeys(data.get(k, []))) for k in KINDS}
        self.data["paused_until"] = float(data.get("paused_until", 0))  # epoch seconds; saved so a restart keeps the pause
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def clean(self, kind: str, value: str) -> str | None:
        if kind == "videos":
            return parse_video_id(value)
        if kind == "channels":
            return normalize_channel(value) or None
        return value.strip().lower() or None

    def add(self, kind: str, value: str) -> bool:
        v = self.clean(kind, value)
        if kind not in KINDS or not v:
            return False
        if v not in self.data[kind]:
            self.data[kind].append(v)
            self.save()
            log.info("rule added %s: %s", kind, v)
        return True

    def remove(self, kind: str, value: str):
        if kind in KINDS and value in self.data[kind]:
            self.data[kind].remove(value)
            self.save()
            log.info("rule removed %s: %s", kind, value)

    def paused(self) -> bool:
        return time.time() < self.data["paused_until"]

    def pause(self, minutes: float):
        self.data["paused_until"] = time.time() + minutes * 60 if minutes > 0 else 0
        self.save()
        log.info("blocking %s", f"paused for {minutes:g} min" if minutes > 0 else "resumed")

    def match(self, video_id: str, info: dict) -> str | None:
        """Return why the video is blocked, or None."""
        if video_id in self.data["videos"]:
            return "video"
        name, handle = info["channel"].lower(), info["handle"].lower()
        for c in self.data["channels"]:
            if c in (name, handle):
                return f"channel: {c}"
        text = f"{info['title']} {info['channel']}".lower()
        for k in self.data["keywords"]:
            if k in text:
                return f"keyword: {k}"
        return None


# ---------------------------------------------------------------- TV

async def video_info(session: aiohttp.ClientSession, video_id: str) -> dict:
    url = f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={video_id}"
    info = {"title": "", "channel": "", "handle": ""}
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                d = await r.json()
                info["title"] = d.get("title", "")
                info["channel"] = d.get("author_name", "")
                info["handle"] = normalize_channel(d.get("author_url", "")) if "/@" in d.get("author_url", "") else ""
            else:
                log.warning("oembed %s -> HTTP %s", video_id, r.status)
    except Exception as e:
        log.warning("oembed %s failed: %s", video_id, e)
    return info


async def close_youtube(session: aiohttp.ClientSession, tv_ip: str, video_id: str, why: str):
    """Stop the YouTube app through the TV's DIAL app launcher (independent of the Lounge link)."""
    url = f"http://{tv_ip}:8080/ws/app/YouTube/run"
    try:
        async with session.delete(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            log.info("close app %s (%s) -> HTTP %s", video_id, why, r.status)
    except Exception as e:
        log.warning("close app failed: %s", e)


class Guard(EventListener):
    def __init__(self, session: aiohttp.ClientSession, rules: Rules, tv_ip: str = TV_IP):
        super().__init__()
        self.api: YtLoungeApi | None = None
        self.session = session
        self.rules = rules
        self.tv_ip = tv_ip
        self.video_id: str | None = None
        self.info: dict[str, dict] = {}  # video_id -> title/channel/handle (rules re-evaluated every time)
        self.recent: collections.deque = collections.deque(maxlen=30)
        self.last_action = 0.0
        self.backed: str | None = None  # last video we pressed Back on
        self.connected_since: float | None = None

    async def check(self, video_id: str) -> str | None:
        if video_id not in self.info:
            self.info[video_id] = await video_info(self.session, video_id)
        info = self.info[video_id]
        why = self.rules.match(video_id, info)
        paused = self.rules.paused()
        if not self.recent or self.recent[0]["id"] != video_id:
            self.recent.appendleft({"t": time.time(), "id": video_id, **info, "why": why, "paused": paused})
            verdict = ("PAUSED-ALLOW" if paused else "BLOCK") if why else "allow"
            log.info("%s %s | %s | %s | %s", verdict, video_id, info["title"], info["channel"], why or "")
        else:
            self.recent[0].update(why=why, paused=paused)
        return None if paused else why

    async def enforce(self, state: State):
        if self.api and self.video_id and state in (State.Playing, State.Starting):
            why = await self.check(self.video_id)
            if why and time.monotonic() - self.last_action > ACTION_GAP:
                self.last_action = time.monotonic()
                if self.backed != self.video_id:
                    self.backed = self.video_id
                    ok = await self.api.send_dpad_command(DpadCommand.BACK)
                    log.info("back %s (%s) sent=%s", self.video_id, why, ok)
                else:
                    await close_youtube(self.session, self.tv_ip, self.video_id, why)

    async def now_playing_changed(self, event):
        log.debug("now_playing %s %s", event.video_id, event.state.name)
        if event.video_id:
            self.video_id = event.video_id
        await self.enforce(event.state)

    async def playback_state_changed(self, event):
        log.debug("playback_state %s", event.state.name)
        await self.enforce(event.state)


def is_samsung_dial_receiver(description: str) -> bool:
    """Return whether a UPnP device description identifies a Samsung DIAL TV."""
    try:
        root = ET.fromstring(description)
    except ET.ParseError:
        return False

    values = {}
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in ("manufacturer", "deviceType") and element.text:
            values[name] = element.text.strip().casefold()
    return (
        values.get("manufacturer") == "samsung electronics"
        and values.get("deviceType") == "urn:dial-multiscreen-org:device:dialreceiver:1"
    )


async def discover_tv_ip(
    session: aiohttp.ClientSession,
    current_ip: str,
    cidr: str | None = None,
) -> str | None:
    """Find the single Samsung DIAL receiver on a bounded IPv4 network."""
    network_text = (DISCOVERY_CIDR if cidr is None else cidr) or f"{current_ip}/24"
    try:
        network = ipaddress.ip_network(network_text, strict=False)
    except ValueError:
        log.warning("cannot discover TV from invalid network %r", network_text)
        return None
    if network.version != 4 or network.num_addresses > 256:
        log.warning("refusing TV discovery outside a single IPv4 /24: %s", network)
        return None

    timeout = aiohttp.ClientTimeout(total=0.75, connect=0.35)
    limit = asyncio.Semaphore(64)

    async def probe(ip: ipaddress.IPv4Address) -> str | None:
        url = f"http://{ip}:7678/nservice/"
        try:
            async with limit:
                async with session.get(url, timeout=timeout) as response:
                    if response.status != 200:
                        return None
                    description = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        if not is_samsung_dial_receiver(description):
            return None
        try:
            screen = await asyncio.wait_for(get_screen_id_from_dial(url), timeout=2)
        except Exception:
            return None
        return str(ip) if screen and screen.screen_id else None

    found = sorted({ip for ip in await asyncio.gather(*(probe(ip) for ip in network.hosts())) if ip})
    if len(found) == 1:
        return found[0]
    if found:
        log.warning("TV discovery ambiguous; Samsung DIAL receivers: %s", ", ".join(found))
    else:
        log.info("TV discovery found no Samsung DIAL receiver on %s", network)
    return None


async def pair_api(api: YtLoungeApi, tv_ip: str) -> bool:
    """Pair over the LAN using the screen id the TV advertises via DIAL. No code needed."""
    screen = await get_screen_id_from_dial(f"http://{tv_ip}:7678/nservice/")
    if not screen or not await api.pair_with_screen_id(screen.screen_id, screen.screen_name):
        return False
    AUTH.write_text(json.dumps(api.auth.serialize()))
    return True


async def pair(tv_ip: str):
    async with YtLoungeApi(DEVICE_NAME) as api:
        if not await pair_api(api, tv_ip):
            sys.exit(f"Pairing failed. Is the TV at {tv_ip} on with YouTube open?")
    print(f"Paired. Saved {AUTH}")


async def pair_with_discovery(
    api: YtLoungeApi,
    session: aiohttp.ClientSession,
    tv_ip: str,
    last_discovery: float,
) -> tuple[bool, str, float]:
    """Pair at the known address, discovering a DHCP replacement when needed."""
    paired = False
    try:
        paired = await pair_api(api, tv_ip)
    except Exception as e:
        log.info("TV at %s unavailable: %s", tv_ip, e)

    now = time.monotonic()
    if not paired and now - last_discovery >= DISCOVERY_INTERVAL:
        last_discovery = now
        discovered = await discover_tv_ip(session, tv_ip)
        if discovered:
            if discovered != tv_ip:
                log.info("TV address changed: %s -> %s", tv_ip, discovered)
                tv_ip = discovered
            paired = await pair_api(api, tv_ip)
    return paired, tv_ip, last_discovery


async def guard_loop(api: YtLoungeApi, guard: Guard):
    delay = 5
    last_discovery = -DISCOVERY_INTERVAL
    while True:
        try:
            if not api.connected():
                guard.connected_since = None
                if not api.linked() or not await api.connect():
                    # Token expired, someone unlinked us, or DHCP moved the TV.
                    log.info("re-pairing with TV at %s", guard.tv_ip)
                    paired, guard.tv_ip, last_discovery = await pair_with_discovery(
                        api, guard.session, guard.tv_ip, last_discovery
                    )
                    if not paired or not await api.connect():
                        raise ConnectionError("TV not reachable (off, YouTube closed, or discovery pending)")
                log.info("connected to %s", api.screen_name)
                guard.connected_since = time.time()
                await api.get_now_playing()
                delay = 5
            await api.subscribe()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("waiting %ss: %s", delay, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)  # stay quick to reattach when YouTube is reopened


# ---------------------------------------------------------------- UI

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>YT Guard</title>
<style>
body{font:15px system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px;color:#222}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px}
.muted{color:#777;font-size:13px} form{display:inline}
.row{display:flex;gap:6px;margin:6px 0} .row input{flex:1;padding:7px;font:inherit}
button{padding:6px 10px;font:inherit;cursor:pointer}
.chip{display:inline-flex;align-items:center;gap:4px;background:#eee;border-radius:14px;padding:2px 4px 2px 10px;margin:3px}
.chip button{border:0;background:none;padding:0 6px;color:#900}
table{width:100%;border-collapse:collapse;font-size:14px} td{border-top:1px solid #eee;padding:6px 4px;vertical-align:top}
.b{color:#b00;font-weight:600} .a{color:#070}
.pause{margin:14px 0;padding:10px;border-radius:8px;background:#f4f4f4} .pause.on{background:#fff3cd}
.pause input{width:70px;padding:6px;font:inherit}
</style>
<h1>YT Guard</h1>
<div class=muted>TV {tv} &middot; {status} &middot; blocked videos get Back, then YouTube is closed</div>
{pause}
{sections}
<h2>Recently played on the TV</h2>
<table>{recent}</table>
<script>
document.querySelectorAll('[data-t]').forEach(e=>e.textContent=new Date(e.dataset.t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
</script>
"""

HELP = {
    "keywords": ("Keywords", "matches anywhere in the title or channel name, e.g. minecraft"),
    "channels": ("Channels", "exact channel name, @handle, or channel URL"),
    "videos": ("Videos &amp; Shorts", "video ID or any link: watch, youtu.be, shorts"),
}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def post_button(kind: str, value: str, label: str, action: str = "add") -> str:
    return (f"<form method=post action=/{action}><input type=hidden name=kind value={kind}>"
            f"<input type=hidden name=value value=\"{esc(value)}\"><button>{label}</button></form>")


def render(guard: Guard) -> str:
    sections = []
    for kind in KINDS:
        title, hint = HELP[kind]
        chips = "".join(
            f"<span class=chip>{esc(v)}{post_button(kind, v, '&times;', 'remove')}</span>"
            for v in guard.rules.data[kind]) or "<span class=muted>none</span>"
        sections.append(
            f"<h2>{title}</h2><form method=post action=/add class=row><input type=hidden name=kind value={kind}>"
            f"<input name=value placeholder=\"{hint}\" required><button>Add</button></form><div>{chips}</div>")
    rows = []
    for r in guard.recent:
        label = "<span class=a>allowed (paused)</span>" if r.get("paused") else "<span class=b>blocked</span>"
        why = f"{label} <span class=muted>{esc(r['why'])}</span>" if r["why"] else "<span class=a>allowed</span>"
        actions = ""
        if not r["why"]:
            actions = post_button("videos", r["id"], "Block video") + " " + post_button(
                "channels", r["handle"] or r["channel"], "Block channel")
        rows.append(
            f"<tr><td class=muted data-t={r['t']:.0f}></td>"
            f"<td><a href=\"https://youtu.be/{esc(r['id'])}\" target=_blank rel=noreferrer>{esc(r['title'] or r['id'])}</a>"
            f"<div class=muted>{esc(r['channel'])}</div></td><td>{why}</td><td>{actions}</td></tr>")
    status = "connected" if guard.connected_since else "not connected (TV off or YouTube closed)"
    quick = "".join(f"<form method=post action=/pause><input type=hidden name=minutes value={m}><button>{m} min</button></form> "
                    for m in (15, 30, 60, 120))
    custom = ("<form method=post action=/pause><input name=minutes type=number min=1 max=1440 placeholder=min required> "
              "<button>Pause</button></form>")
    if guard.rules.paused():
        pause = (f"<div class='pause on'><b>Blocking paused</b> until <span data-t={guard.rules.data['paused_until']:.0f}></span> "
                 "<form method=post action=/pause><input type=hidden name=minutes value=0><button>Resume now</button></form>"
                 f"<div class=muted style='margin-top:6px'>Change: {quick}{custom}</div></div>")
    else:
        pause = f"<div class=pause><b>Pause blocking</b> for {quick}{custom}</div>"
    return (PAGE.replace("{tv}", esc(guard.tv_ip)).replace("{status}", status).replace("{pause}", pause)
            .replace("{sections}", "".join(sections))
            .replace("{recent}", "".join(rows) or "<tr><td class=muted>nothing yet</td></tr>"))


def make_app(guard: Guard) -> web.Application:
    async def index(_):
        return web.Response(text=render(guard), content_type="text/html")

    async def add(request):
        form = await request.post()
        guard.rules.add(form.get("kind", ""), form.get("value", ""))
        raise web.HTTPSeeOther("/")

    async def remove(request):
        form = await request.post()
        guard.rules.remove(form.get("kind", ""), form.get("value", ""))
        raise web.HTTPSeeOther("/")

    async def pause(request):
        form = await request.post()
        try:
            minutes = max(0.0, min(float(form.get("minutes", "0")), 1440.0))
        except ValueError:
            minutes = 0.0
        guard.rules.pause(minutes)
        raise web.HTTPSeeOther("/")

    async def healthz(_):
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes([web.get("/", index), web.post("/add", add), web.post("/remove", remove), web.post("/pause", pause), web.get("/healthz", healthz)])
    return app


async def run():
    if not TV_IP:
        sys.exit("Set TV_IP to the TV's address on your network, e.g. TV_IP=192.168.1.50")
    rules = Rules(RULES_FILE)
    log.info("rules: %s", json.dumps(rules.data))
    async with aiohttp.ClientSession() as session:
        guard = Guard(session, rules, TV_IP)
        runner = web.AppRunner(make_app(guard), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", UI_PORT).start()
        log.info("UI on http://0.0.0.0:%s", UI_PORT)
        async with YtLoungeApi(DEVICE_NAME, guard) as api:
            guard.api = api
            # No saved auth (e.g. a fresh container) is fine: the loop pairs over the LAN.
            if AUTH.exists():
                api.load_auth_state(json.loads(AUTH.read_text()))
            await guard_loop(api, guard)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "pair":
        asyncio.run(pair(sys.argv[2]))
    else:
        asyncio.run(run())
