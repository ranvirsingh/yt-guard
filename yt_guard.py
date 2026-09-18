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
import math
import os
import re
import signal
import sys
import time
from time import monotonic
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from aiohttp import web
from pyytlounge import EventListener, State, YtLoungeApi
from pyytlounge.dial import get_screen_id_from_dial
from pyytlounge.exceptions import NotSupportedException
from pyytlounge.models import DpadCommand

HERE = Path(__file__).parent
AUTH = Path(os.environ.get("AUTH_FILE", HERE / "auth.json"))
RULES_FILE = Path(os.environ.get("RULES_FILE", HERE / "rules.json"))
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", RULES_FILE.with_name("recent.json")))
SEED_KEYWORDS = os.environ.get("KEYWORDS", "minecraft,roblox,fortnite")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "YT Guard")
TV_IP = os.environ.get("TV_IP", "")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))
DISCOVERY_CIDR = os.environ.get("DISCOVERY_CIDR", "")
DISCOVERY_INTERVAL = max(0.0, float(os.environ.get("DISCOVERY_INTERVAL", "60")))
ACTION_GAP = 3.0  # seconds between commands; a burst of commands can trigger the TV's disconnect prompt
POLL_INTERVAL = 3.0
PLAYBACK_TIMEOUT = 30.0
PAIR_REQUEST_TIMEOUT = 8.0
LIVENESS_TIMEOUT = 60.0
DEFAULT_KIDS_GRACE = 30
MAX_KIDS_GRACE = 600
KINDS = ("keywords", "channels", "videos")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("yt-guard")
# The SDK logs pairing tokens and request URLs, including in exception traces.
# Keep our connection/recovery messages without forwarding its raw logs.
SDK_LOG = logging.getLogger("yt-guard.lounge")
SDK_LOG.disabled = True


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
        self.data["include_kids"] = data.get("include_kids") is True
        grace = data.get("kids_grace_seconds", DEFAULT_KIDS_GRACE)
        self.data["kids_grace_seconds"] = grace if type(grace) is int and 0 <= grace <= MAX_KIDS_GRACE else DEFAULT_KIDS_GRACE
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

    def include_kids(self, enabled: bool, grace_seconds: int | None = None):
        if grace_seconds is not None:
            if type(grace_seconds) is not int or not 0 <= grace_seconds <= MAX_KIDS_GRACE:
                raise ValueError(f"Grace time must be whole seconds from 0 to {MAX_KIDS_GRACE}.")
            self.data["kids_grace_seconds"] = grace_seconds
        self.data["include_kids"] = enabled
        self.save()
        log.info("YouTube Kids video rules %s, grace %ss", "included" if enabled else "excluded", self.data["kids_grace_seconds"])

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


async def youtube_visible(session: aiohttp.ClientSession, tv_ip: str) -> bool | None:
    """Read Samsung app visibility; None means the TV cannot confirm either way."""
    seen = False
    for app_id in ("111299001912", "3201611010983"):
        try:
            async with session.get(f"http://{tv_ip}:8001/api/v2/applications/{app_id}",
                                   timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status == 404:
                    continue
                if response.status != 200:
                    return None
                app = await response.json(content_type=None)
            if not isinstance(app, dict) or app.get("name", "").casefold() not in ("youtube", "youtube kids"):
                return None
            if app.get("visible") is True:
                return True
            if app.get("visible") is not False:
                return None
            seen = True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            return None
    return False if seen else None


async def close_youtube(session: aiohttp.ClientSession, tv_ip: str, video_id: str, why: str) -> bool:
    """Stop YouTube independently of Lounge, falling back to Samsung's native app API."""
    url = f"http://{tv_ip}:8080/ws/app/YouTube/run"
    try:
        async with session.delete(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
            log.info("close app %s (%s) -> HTTP %s", video_id, why, r.status)
            if 200 <= r.status < 300:
                if await youtube_visible(session, tv_ip) is False:
                    return True
    except Exception as e:
        log.info("DIAL close unavailable: %s", type(e).__name__)

    # Kids profiles can run inside YouTube, or in the separate Kids application.
    # Check the installed app's name before using an ID, since IDs vary by model.
    app_ids = ["111299001912", "3201611010983"]
    for app_id in app_ids:
        url = f"http://{tv_ip}:8001/api/v2/applications/{app_id}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status != 200:
                    continue
                app = await r.json(content_type=None)
            if not isinstance(app, dict) or app.get("name", "").casefold() not in ("youtube", "youtube kids"):
                continue
            if app.get("running") is False and app.get("visible") is False:
                continue
            if app.get("visible") is not True:
                continue
            async with session.delete(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                log.info("Samsung close %s (%s) -> HTTP %s", app["name"], why, r.status)
                if not 200 <= r.status < 300:
                    continue
            for _ in range(3):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            state = await r.json(content_type=None)
                            if state.get("visible") is False:
                                return True
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError):
                    # App shutdown can briefly interrupt Samsung's status endpoint.
                    pass
                await asyncio.sleep(0.25)
            log.warning("Samsung acknowledged close but %s is still visible", app["name"])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError) as e:
            log.info("Samsung app close unavailable: %s", type(e).__name__)
    return False


class SnapshotLoungeApi(YtLoungeApi):
    """Read Kids IDs from an already-received reply without bypassing its blacklist.

    Keep the SDK's immediate rejection: never subscribe, request playback, or send
    a Lounge terminate command for Kids. Those can eject the Kids profile.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kids_video_id: str | None = None
        self._reading_handshake = False

    async def connect(self) -> bool:
        self.kids_video_id = None
        self._reading_handshake = True
        try:
            return await super().connect()
        finally:
            self._reading_handshake = False

    async def _parse_event_chunks(self, lines):
        if not self._reading_handshake:
            async for events in super()._parse_event_chunks(lines):
                yield events
            return

        # connect() already read the complete HTTP response before invoking this
        # parser. Buffer these finite chunks only; subscriptions remain streaming.
        batches = [events async for events in super()._parse_event_chunks(lines)]
        kids = False
        for events in batches:
            for _, (event_type, *args) in events:
                if event_type == "loungeStatus" and args:
                    for device in json.loads(args[0]["devices"]):
                        if device.get("type") == "LOUNGE_SCREEN":
                            info = json.loads(device.get("deviceInfo", "null")) or {}
                            kids = info.get("clientName") == "TVHTML5_FOR_KIDS"
                            self.kids_video_id = None
                            break
                elif kids and event_type in ("playlistModified", "nowPlaying") and args:
                    video_id = args[0].get("videoId", "")
                    self.kids_video_id = (video_id if isinstance(video_id, str)
                                          and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else None)
                elif event_type == "loungeScreenDisconnected":
                    self.kids_video_id = None
        for events in batches:
            yield events  # unchanged: loungeStatus still raises NotSupportedException


class Guard(EventListener):
    def __init__(self, session: aiohttp.ClientSession, rules: Rules, tv_ip: str = TV_IP,
                 history_path: Path | None = None):
        super().__init__()
        self.api: YtLoungeApi | None = None
        self.session = session
        self.rules = rules
        self.tv_ip = tv_ip
        self.video_id: str | None = None
        self.info: dict[str, dict] = {}  # video_id -> title/channel/handle (rules re-evaluated every time)
        self.recent: collections.deque = collections.deque(maxlen=30)
        self.history_path = history_path or rules.path.with_name("recent.json")
        try:
            if self.history_path.exists():
                self.recent.extend(json.loads(self.history_path.read_text())[:30])
        except (OSError, ValueError, TypeError) as e:
            log.warning("could not load recent videos: %s", e)
        self.last_action = 0.0
        self.last_close_discovery = -DISCOVERY_INTERVAL
        self.backed: str | None = None  # last video we pressed Back on
        self.connected_since: float | None = None
        self.last_event: float | None = None
        self.last_error: str | None = None
        self.last_progress = time.monotonic()
        self.recovery_count = 0
        self.kids_detected = False
        self.kids_since: float | None = None
        self.kids_pending_video: str | None = None
        self.kids_why: str | None = None
        self.kids_close_ok: bool | None = None
        self.kids_hidden = False
        self.state = State.Stopped

    def save_recent(self):
        try:
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.recent)))
            tmp.replace(self.history_path)
        except OSError as e:
            log.warning("could not save recent videos: %s", e)

    def connection_lost(self):
        self.connected_since = None
        self.last_event = None
        self.video_id = None
        self.state = State.Stopped
        self.backed = None

    def kids_grace_remaining(self) -> int:
        return math.ceil(self.kids_grace_remaining_seconds())

    def kids_grace_remaining_seconds(self) -> float:
        if self.kids_since is None or not self.kids_detected or not self.rules.data["include_kids"] or self.rules.paused():
            return 0.0
        return max(0.0, self.rules.data["kids_grace_seconds"] - (monotonic() - self.kids_since))

    def kids_app_hidden(self):
        self.kids_hidden = True
        self.connection_lost()
        self.kids_detected = False
        self.kids_since = None
        self.kids_pending_video = None
        self.kids_why = None
        self.kids_close_ok = None
        self.last_error = "YouTube is closed or in the background. Open it on the TV to reconnect."

    async def observe_kids(self, video_id: str | None):
        if await youtube_visible(self.session, self.tv_ip) is False:
            self.kids_app_hidden()
            return
        self.kids_hidden = False
        self.kids_detected = True
        self.kids_close_ok = None
        self.video_id = video_id
        self.kids_why = None
        if self.video_id:
            self.last_event = time.time()
            self.kids_why = await self.check(self.video_id, app="kids")
        if self.kids_why:
            if self.kids_since is None or self.kids_pending_video != self.video_id:
                self.kids_since = monotonic()
                self.kids_pending_video = self.video_id
            remaining = self.kids_grace_remaining()
            if remaining:
                self.last_error = f"Matched {self.kids_why}. Switch video or profile within {remaining} seconds."
            else:
                self.kids_close_ok = await self.close_app(self.video_id, self.kids_why)
                if self.kids_close_ok:
                    self.kids_since = None
                    self.kids_pending_video = None
                    self.kids_hidden = True  # wait for local visibility before connecting again
                self.last_error = ("YouTube Kids was closed for a matched video after its grace period."
                                   if self.kids_close_ok else "Could not close YouTube Kids. Retrying automatically.")
        else:
            self.kids_since = None
            self.kids_pending_video = None
            self.last_error = ("YouTube Kids is open. Blocking is paused." if self.rules.paused()
                               else "YouTube Kids is open and excluded from blocking." if not self.rules.data["include_kids"]
                               else "Kids video data unavailable. Waiting for a fresh video ID; the app stays open." if not self.video_id
                               else "Kids video does not match a rule; the app stays open.")

    async def check(self, video_id: str, app: str = "youtube") -> str | None:
        if video_id not in self.info or not (self.info[video_id]["title"] or self.info[video_id]["channel"]):
            self.info[video_id] = await video_info(self.session, video_id)
        info = self.info[video_id]
        why = self.rules.match(video_id, info)
        paused = self.rules.paused()
        excluded = app == "kids" and not self.rules.data["include_kids"]
        if not self.recent or self.recent[0]["id"] != video_id or self.recent[0].get("app", "youtube") != app:
            self.recent.appendleft({"t": time.time(), "id": video_id, **info, "why": why,
                                   "paused": paused, "excluded": excluded, "app": app})
            self.save_recent()
            verdict = ("ALLOW-EXCLUDED" if excluded else "PAUSED-ALLOW" if paused else "BLOCK") if why else "allow"
            log.info("%s %s | %s | %s | %s", verdict, video_id, info["title"], info["channel"], why or "")
        else:
            if (self.recent[0]["why"] != why or self.recent[0].get("paused") != paused
                    or self.recent[0].get("excluded", False) != excluded
                    or self.recent[0].get("title") != info["title"]):
                self.recent[0].update(**info, why=why, paused=paused, excluded=excluded)
                self.save_recent()
        return None if paused or excluded else why

    async def enforce(self, state: State):
        # nowPlaying can arrive inside connect() before the handshake has an AID.
        if self.api and self.connected_since and self.video_id and state in (State.Playing, State.Starting):
            why = await self.check(self.video_id)
            if why and time.monotonic() - self.last_action > ACTION_GAP:
                self.last_action = time.monotonic()
                if self.backed != self.video_id:
                    self.backed = self.video_id
                    ok = await asyncio.wait_for(self.api.send_dpad_command(DpadCommand.BACK), 8)
                    log.info("back %s (%s) sent=%s", self.video_id, why, ok)
                else:
                    await self.close_app(self.video_id, why)

    async def close_app(self, video_id: str, why: str) -> bool:
        if await close_youtube(self.session, self.tv_ip, video_id, why):
            return True
        now = time.monotonic()
        if now - self.last_close_discovery >= DISCOVERY_INTERVAL:
            self.last_close_discovery = now
            self.last_progress = now
            found = await discover_tv_ip(self.session, self.tv_ip,
                                         known_screen=bool(self.api and self.api.auth.screen_id))
            self.last_progress = time.monotonic()
            if found and found != self.tv_ip:
                log.info("TV address changed during app blocking: %s -> %s", self.tv_ip, found)
                self.tv_ip = found
                return await close_youtube(self.session, found, video_id, why)
        return False

    async def now_playing_changed(self, event):
        log.debug("now_playing %s %s", event.video_id, event.state.name)
        self.last_event = time.time()
        if event.video_id != self.video_id:
            self.backed = None
        self.video_id = event.video_id
        self.state = event.state
        if self.video_id and event.state not in (State.Playing, State.Starting):
            await self.check(self.video_id)
        await self.enforce(event.state)

    async def playback_state_changed(self, event):
        log.debug("playback_state %s", event.state.name)
        self.last_event = time.time()
        self.state = event.state
        await self.enforce(event.state)

    async def disconnected(self, event):
        self.connection_lost()


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
    *,
    known_screen: bool = False,
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
        # A previously paired TV can be rediscovered while Kids shuts its app
        # pairing endpoint. Still require exactly one Samsung DIAL receiver.
        if known_screen:
            return str(ip)
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


def save_auth(api: YtLoungeApi):
    AUTH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH.with_suffix(".tmp")
    tmp.write_text(json.dumps(api.auth.serialize()))
    tmp.chmod(0o600)
    tmp.replace(AUTH)


async def pair_api(api: YtLoungeApi, tv_ip: str) -> bool:
    """Refresh a known TV's pairing, or learn its screen id over the LAN."""
    if api.auth.screen_id:
        # Cloud token refresh alone cannot prove that the LAN address is still right.
        # Keep DHCP discovery working, including when cached pairing survives a restart.
        try:
            async with api.session.get(f"http://{tv_ip}:7678/nservice/",
                                       timeout=aiohttp.ClientTimeout(total=2)) as response:
                if response.status != 200 or not is_samsung_dial_receiver(await response.text()):
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        try:
            if await asyncio.wait_for(
                api.pair_with_screen_id(api.auth.screen_id), PAIR_REQUEST_TIMEOUT
            ):
                save_auth(api)
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.info("cached TV pairing could not be refreshed; trying DIAL")
    screen = await asyncio.wait_for(get_screen_id_from_dial(f"http://{tv_ip}:7678/nservice/"), 5)
    if not screen or not await asyncio.wait_for(
        api.pair_with_screen_id(screen.screen_id, screen.screen_name), PAIR_REQUEST_TIMEOUT
    ):
        return False
    save_auth(api)
    return True


async def pair(tv_ip: str):
    async with YtLoungeApi(DEVICE_NAME, logger=SDK_LOG) as api:
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
        log.info("TV at %s unavailable: %s", tv_ip, type(e).__name__)

    now = time.monotonic()
    if not paired and now - last_discovery >= DISCOVERY_INTERVAL:
        last_discovery = now
        discovered = await discover_tv_ip(
            session, tv_ip, known_screen=bool(getattr(getattr(api, "auth", None), "screen_id", None))
        )
        if discovered:
            if discovered != tv_ip:
                log.info("TV address changed: %s -> %s", tv_ip, discovered)
                tv_ip = discovered
            paired = await pair_api(api, tv_ip)
    return paired, tv_ip, last_discovery


async def guard_loop(api: YtLoungeApi, guard: Guard):
    delay = 5
    last_discovery = -DISCOVERY_INTERVAL
    failures = 0
    while True:
        guard.last_progress = time.monotonic()
        subscription = None
        try:
            if failures >= 3:
                # Recovery itself can fail. Keep it inside the retry boundary.
                guard.last_progress = time.monotonic()
                await asyncio.wait_for(api.close(), 5)
                await asyncio.wait_for(api.__aenter__(), 5)
                api._lounge_token_expired()
                failures = 0
                log.info("rebuilt YouTube transport after repeated connection failures")
            if guard.kids_hidden:
                if await youtube_visible(guard.session, guard.tv_ip) is False:
                    guard.kids_app_hidden()
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                guard.kids_hidden = False
            if not api.connected():
                guard.connected_since = None
                if not api.linked() or not await asyncio.wait_for(api.connect(), 10):
                    # Token expired, someone unlinked us, or DHCP moved the TV.
                    log.info("re-pairing with TV at %s", guard.tv_ip)
                    guard.last_progress = time.monotonic()
                    paired, guard.tv_ip, last_discovery = await asyncio.wait_for(
                        pair_with_discovery(api, guard.session, guard.tv_ip, last_discovery), 35
                    )
                    guard.last_progress = time.monotonic()
                    if not paired or not await asyncio.wait_for(api.connect(), 10):
                        raise ConnectionError("TV not reachable (off, YouTube closed, or discovery pending)")
                log.info("connected to %s", api.screen_name)
                guard.connected_since = time.time()
                guard.kids_detected = False
                guard.kids_since = None
                guard.kids_pending_video = None
                guard.kids_why = None
                guard.kids_close_ok = None
                guard.last_error = None
                if not await asyncio.wait_for(api.get_now_playing(), 8):
                    raise ConnectionError("YouTube playback request failed")
                delay = 5
            subscription = asyncio.create_task(api.subscribe())
            while True:
                guard.last_progress = time.monotonic()
                done, _ = await asyncio.wait({subscription}, timeout=POLL_INTERVAL)
                if done:
                    await subscription  # propagate handshake / stream errors
                    raise ConnectionError("YouTube event stream ended; reconnecting")
                if not await asyncio.wait_for(api.get_now_playing(), 8):
                    raise ConnectionError("YouTube playback request failed")
                last_playback = guard.last_event or guard.connected_since or time.time()
                if time.time() - last_playback > PLAYBACK_TIMEOUT:
                    raise ConnectionError("YouTube playback feed stalled; reconnecting")
                if guard.last_event:
                    failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # pyytlounge 3.4.0 can set SID/gsession before a Kids handshake raises,
            # leaving connected() true but AID unset. Never reuse a failed session.
            api._connection_lost()
            guard.connection_lost()
            guard.recovery_count += 1
            failures += 1
            if isinstance(e, NotSupportedException):
                failures = 0  # an excluded app is not a broken HTTP transport
                try:
                    await guard.observe_kids(getattr(api, "kids_video_id", None))
                except Exception as observation_error:
                    # Metadata, discovery or app-control faults must not end the loop.
                    guard.connection_lost()
                    guard.kids_detected = False
                    guard.kids_since = None
                    guard.kids_pending_video = None
                    guard.kids_why = None
                    guard.kids_close_ok = None
                    guard.last_error = "Kids video check failed. Retrying automatically."
                    log.warning("Kids video check failed: %s", type(observation_error).__name__)
            else:
                guard.kids_detected = False
                guard.kids_since = None
                guard.kids_pending_video = None
                guard.kids_why = None
                guard.kids_close_ok = None
                guard.last_error = "Waiting for the TV. Open YouTube and the guard will reconnect."
            remaining = guard.kids_grace_remaining()
            retry_delay = min(delay, POLL_INTERVAL, remaining) if remaining else delay
            log.info("waiting %ss: %s", retry_delay, type(e).__name__)
        finally:
            if subscription:
                subscription.cancel()
                await asyncio.gather(subscription, return_exceptions=True)
        if guard.last_error:
            remaining = guard.kids_grace_remaining()
            await asyncio.sleep(min(delay, POLL_INTERVAL, remaining) if remaining else delay)
            delay = min(delay * 2, 10)  # stay quick to reattach when YouTube is reopened


# ---------------------------------------------------------------- UI

PAGE = (HERE / "ui.html").read_text()

HELP = {
    "keywords": ("Keywords", "Matches a word in the video title or creator name.", "e.g. minecraft"),
    "channels": ("Channels", "Matches a creator’s exact name or @handle.", "@handle or channel link"),
    "videos": ("Videos &amp; Shorts", "Blocks one video, Short, or live stream.", "Paste a YouTube link"),
}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def post_button(kind: str, value: str, label: str, action: str = "add", accessible_label: str | None = None) -> str:
    return (f"<form method=post action=/{action}><input type=hidden name=kind value={kind}>"
            f"<input type=hidden name=value value=\"{esc(value)}\"><button aria-label=\"{esc(accessible_label or label)}\">{label}</button></form>")


def render_recent(guard: Guard) -> str:
    rows = []
    for r in guard.recent:
        if r.get("excluded"):
            label, css = "Allowed · Kids excluded", "allowed"
        elif r.get("why") and r.get("paused"):
            label, css = "Allowed while paused", "paused"
        elif r.get("why"):
            label, css = "Blocked", "blocked"
        else:
            label, css = "Allowed", "allowed"
        actions = ""
        if not r.get("why"):
            actions = post_button("videos", r["id"], "Block video")
            channel = r.get("handle") or r.get("channel")
            if channel:
                actions += post_button("channels", channel, "Block channel")
        reason = f'<div class="reason">{esc(r["why"])}</div>' if r.get("why") else ""
        rows.append(
            f'<li class="activity-item"><div class="activity-video"><div class="video-line">'
            f'<span class="video-symbol" aria-hidden="true">▶</span><div>'
            f'<a class="video-title" href="https://youtu.be/{esc(r["id"])}" target="_blank" rel="noreferrer">'
            f'{esc(r.get("title") or r["id"])}</a><div class="video-channel">{esc(r.get("channel") or "Channel unavailable")}{" · YouTube Kids" if r.get("app") == "kids" else ""}</div>'
            f'<div class="activity-actions">{actions}</div></div></div></div>'
            f'<div class="activity-verdict"><span class="verdict {css}">{label}</span>{reason}</div>'
            f'<time class="activity-time" data-t="{r["t"]:.3f}"><span class="date">Loading local date</span>'
            f'<span class="clock">Loading local time</span></time></li>')
    return "".join(rows) or ('<li class="empty-activity"><strong>The next video starts the story.</strong>'
                             '<p>Open YouTube on your TV. Videos will appear here as they’re detected.</p></li>')


def render_now(guard: Guard) -> str:
    icon = ('<div class="now-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none">'
            '<rect x="2" y="4" width="20" height="14" rx="3" stroke="currentColor" stroke-width="1.4"/>'
            '<path d="m10 8 5 3-5 3V8Z" fill="currentColor"/><path d="M9 21h6" stroke="currentColor" stroke-width="1.4"/></svg></div>')
    if (guard.connected_since or guard.kids_detected) and guard.video_id:
        info = guard.info.get(guard.video_id, {})
        state = {State.Playing: "Playing", State.Paused: "Paused on TV", State.Starting: "Starting",
                 State.Cued: "Ready to play", State.Advertisement: "Advertisement"}.get(guard.state, "Stopped")
        if guard.kids_detected:
            remaining = guard.kids_grace_remaining()
            state = ("YouTube Kids closed · matched video" if guard.kids_close_ok
                     else f"YouTube Kids · matched video closes in {remaining} seconds" if guard.kids_why and remaining
                     else "YouTube Kids · video snapshot; playback state unavailable")
        content = (f'<p class="now-title"><a href="https://youtu.be/{esc(guard.video_id)}" target="_blank" rel="noreferrer">'
                   f'{esc(info.get("title") or guard.video_id)}</a></p><p class="now-channel">{esc(info.get("channel") or "Loading channel…")}</p>'
                   f'<div class="now-state">{state}</div>')
    elif guard.kids_detected:
        title = "YouTube Kids was closed" if guard.kids_close_ok else "YouTube Kids is open"
        if guard.rules.paused():
            detail = "Kids is allowed while blocking is paused."
        elif guard.rules.data["include_kids"]:
            remaining = guard.kids_grace_remaining()
            detail = (f"Switch to a regular profile within {remaining} seconds to keep YouTube open." if remaining
                      else "Current video data is unavailable. The guard waits for a fresh video ID.")
        else:
            detail = "Kids is excluded from blocking."
        content = f'<p class="now-empty">{title}</p><p class="now-channel">{detail}</p>'
    else:
        title = "Ready for the next video" if guard.connected_since else "Waiting for YouTube"
        detail = "Nothing is playing right now." if guard.connected_since else "Your TV connects automatically when YouTube opens."
        content = f'<p class="now-empty">{title}</p><p class="now-channel">{detail}</p>'
    return f'{icon}<div>{content}</div>'


def render_pause(guard: Guard) -> str:
    quick = "".join(f'<form method="post" action="/pause"><input type="hidden" name="minutes" value="{m}">'
                    f'<button aria-label="Pause blocking for {m} minutes">{m} min</button></form>' for m in (15, 30, 60))
    custom = ('<form method="post" action="/pause" class="custom-pause"><label class="visually-hidden" for="pause-minutes">'
              'Custom pause in minutes</label><input id="pause-minutes" name="minutes" type="number" min="1" max="1440" '
              'placeholder="Min" required><button>Pause</button></form>')
    if guard.rules.paused():
        copy = (f'<strong>Blocking is paused</strong><p>Resumes <time data-t="{guard.rules.data["paused_until"]:.3f}">'
                'at your local time</time>.</p>')
        controls = ('<form method="post" action="/pause"><input type="hidden" name="minutes" value="0">'
                    '<button class="primary">Resume protection</button></form>')
    else:
        copy = '<strong>Take a little break</strong><p>Pause blocking, then let it resume automatically.</p>'
        controls = quick + custom
    return f'<div class="pause-copy">{copy}</div><div class="pause-controls">{controls}</div>'


def status_snapshot(guard: Guard) -> dict:
    connected = bool(guard.connected_since and guard.last_event
                     and time.time() - guard.last_event < PLAYBACK_TIMEOUT)
    paused = guard.rules.paused()
    countdown = None
    if (guard.kids_detected and guard.rules.data["include_kids"] and not paused
            and guard.kids_why and not guard.kids_close_ok and guard.kids_since is not None):
        countdown = {"video_id": guard.video_id,
                     "title": guard.info.get(guard.video_id, {}).get("title") or guard.video_id,
                     "reason": guard.kids_why,
                     "total_seconds": guard.rules.data["kids_grace_seconds"],
                     "remaining_seconds": guard.kids_grace_remaining_seconds(),
                     "retrying": guard.kids_close_ok is False}
    if paused:
        mode, title = "paused", "Blocking paused."
        detail = "Videos can play freely until your pause ends."
    elif guard.kids_detected and guard.rules.data["include_kids"]:
        remaining = guard.kids_grace_remaining()
        mode = "waiting"
        if guard.kids_why and remaining:
            title = "Switch video or profile before Kids closes."
            detail = f"Matched {guard.kids_why}. {remaining} seconds left to switch and keep YouTube open."
        elif guard.kids_why:
            title = "YouTube Kids blocked." if guard.kids_close_ok else "Stopping YouTube Kids."
            detail = guard.last_error or "The Kids video matched a rule."
        elif guard.video_id and any(guard.info.get(guard.video_id, {}).get(k) for k in ("title", "channel")):
            mode, title = "active", "Kids video allowed."
            detail = "This video does not match a rule. Kids uses video snapshots; playback state is unavailable."
        elif guard.video_id:
            title = "Kids video details unavailable."
            detail = "Video ID detected. Only specific video rules can be checked until the title and channel are available."
        else:
            title = "Kids video data unavailable."
            detail = "Waiting for a fresh video ID. YouTube stays open; video rules cannot be checked yet."
    elif guard.kids_detected:
        mode, title = "waiting", "YouTube Kids excluded."
        detail = "Regular YouTube reconnects automatically when you return to it."
    elif connected:
        mode, title = "active", "Protection active."
        detail = "Matched videos get Back. If they keep playing, the guard closes YouTube."
    else:
        mode, title = "waiting", "Waiting for YouTube."
        detail = guard.last_error or "Open regular YouTube on your TV to start protection."
    return {"connected": connected, "paused": paused, "mode": mode, "status_title": title,
            "status_detail": detail, "connection_label": "YouTube Kids" if guard.kids_detected else "YouTube connected" if connected else "Not connected",
            "tv_ip": guard.tv_ip, "rule_count": sum(len(guard.rules.data[k]) for k in KINDS),
            "recent_count": len(guard.recent), "last_event": guard.last_event,
            "recovery_count": guard.recovery_count,
            "include_kids": guard.rules.data["include_kids"], "kids_detected": guard.kids_detected,
            "kids_grace_seconds": guard.rules.data["kids_grace_seconds"], "kids_grace_remaining": guard.kids_grace_remaining(),
            "kids_close_ok": guard.kids_close_ok,
            "kids_match": guard.kids_why,
            "kids_countdown": countdown,
            "connected_since": guard.connected_since, "video_id": guard.video_id,
            "state": guard.state.name, "recent": list(guard.recent),
            "recent_html": render_recent(guard), "now_html": render_now(guard), "pause_html": render_pause(guard)}


def render(guard: Guard) -> str:
    sections = []
    for kind in KINDS:
        title, hint, placeholder = HELP[kind]
        chips = "".join(
            f'<span class="chip">{esc(v)}{post_button(kind, v, "×", "remove", f"Remove {v}")}</span>'
            for v in guard.rules.data[kind]) or '<span class="empty-rule">No rules yet. Add your first one above.</span>'
        sections.append(
            f'<section class="glass rule-card" aria-labelledby="heading-{kind}"><div class="rule-heading">'
            f'<h3 id="heading-{kind}">{title}</h3><span class="count">{len(guard.rules.data[kind])}</span></div>'
            f'<p class="rule-hint" id="hint-{kind}">{hint}</p><form method="post" action="/add" class="add-rule">'
            f'<input type="hidden" name="kind" value="{kind}"><label class="visually-hidden" for="rule-{kind}">Add {kind}</label>'
            f'<input id="rule-{kind}" name="value" placeholder="{esc(placeholder)}" aria-describedby="hint-{kind}" required>'
            f'<button aria-label="Add {kind} rule">Add</button></form><div class="chips">{chips}</div></section>')
    snapshot = status_snapshot(guard)
    countdown = snapshot["kids_countdown"]
    remaining = math.ceil(countdown["remaining_seconds"]) if countdown else 0
    replacements = {"tv": esc(guard.tv_ip), "sections": "".join(sections), "recent": snapshot["recent_html"],
                    "pause": snapshot["pause_html"], "now_playing": snapshot["now_html"],
                    "connection_class": "connected" if snapshot["connected"] else "",
                    "kids_checked": "checked" if snapshot["include_kids"] else "",
                    "kids_grace_seconds": str(snapshot["kids_grace_seconds"]),
                    "countdown_hidden": "" if countdown else "hidden",
                    "countdown_title": esc(countdown["title"] if countdown else ""),
                    "countdown_reason": esc(countdown["reason"] if countdown else ""),
                    "countdown_remaining": str(remaining),
                    "countdown_exact": str(countdown["remaining_seconds"] if countdown else 0),
                    "countdown_total": str(max(1, countdown["total_seconds"]) if countdown else 30),
                    "countdown_retrying": "true" if countdown and countdown["retrying"] else "false",
                    "kids_setting_state": (f'Kids video rules are on · {snapshot["kids_grace_seconds"]} sec grace.'
                                           if snapshot["include_kids"] else 'Kids is excluded. Regular YouTube stays protected.'),
                    "pause_class": "paused" if snapshot["paused"] else ""}
    for key in ("mode", "status_title", "status_detail", "connection_label", "rule_count", "recent_count"):
        replacements[key] = esc(snapshot[key])
    # A single substitution pass prevents rule/title text resembling a placeholder from being interpreted.
    return re.sub(r"\{([a-z_]+)\}", lambda m: replacements.get(m.group(1), m.group(0)), PAGE)


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
        if minutes > 0:
            guard.kids_since = None
        raise web.HTTPSeeOther("/")

    async def kids_settings(request):
        form = await request.post()
        included = form.get("include_kids") == "1"
        changed = included != guard.rules.data["include_kids"]
        try:
            grace = int(form.get("kids_grace_seconds", str(guard.rules.data["kids_grace_seconds"])))
            guard.rules.include_kids(included, grace)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text=f"Grace time must be whole seconds from 0 to {MAX_KIDS_GRACE}.")
        if changed:
            guard.kids_since = None
            guard.kids_pending_video = None
            guard.kids_why = None
            guard.kids_close_ok = None
        raise web.HTTPSeeOther("/")

    async def healthz(_):
        return web.Response(text="ok")

    async def livez(_):
        if time.monotonic() - guard.last_progress > LIVENESS_TIMEOUT:
            return web.Response(text="guard stalled", status=503)
        return web.Response(text="ok")

    async def status(_):
        return web.json_response(status_snapshot(guard), headers={"Cache-Control": "no-store"})

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/status", status), web.post("/add", add), web.post("/remove", remove), web.post("/pause", pause), web.post("/settings/kids", kids_settings), web.get("/healthz", healthz), web.get("/livez", livez)])
    return app


async def run():
    if not TV_IP:
        sys.exit("Set TV_IP to the TV's address on your network, e.g. TV_IP=192.168.1.50")
    rules = Rules(RULES_FILE)
    log.info("rules: %s", json.dumps(rules.data))
    async with aiohttp.ClientSession() as session:
        guard = Guard(session, rules, TV_IP, history_path=HISTORY_FILE)
        runner = web.AppRunner(make_app(guard), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", UI_PORT).start()
        log.info("UI on http://0.0.0.0:%s", UI_PORT)
        async with SnapshotLoungeApi(DEVICE_NAME, guard, logger=SDK_LOG) as api:
            guard.api = api
            # No saved auth (e.g. a fresh container) is fine: the loop pairs over the LAN.
            if AUTH.exists():
                try:
                    api.load_auth_state(json.loads(AUTH.read_text()))
                except (OSError, ValueError, KeyError, TypeError):
                    log.warning("ignoring invalid cached auth; pairing again")
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            try:
                await guard_loop(api, guard)
            finally:
                loop.remove_signal_handler(signal.SIGTERM)
                await runner.cleanup()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "pair":
        asyncio.run(pair(sys.argv[2]))
    else:
        try:
            asyncio.run(run())
        except asyncio.CancelledError:
            pass  # graceful SIGTERM during a rollout
