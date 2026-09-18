# yt-guard

Blocks YouTube videos on a Samsung TV by keyword, channel, or video ID.

It connects to the TV's YouTube app over the Lounge API (the link phones use to cast),
reads the video that is playing, and when it matches a rule presses Back, then closes
the YouTube app if the video keeps playing. Pairing happens automatically over the LAN
via DIAL, so no phone or pairing code is needed. If DHCP changes the TV's address,
yt-guard scans the configured IPv4 `/24`, verifies the single Samsung DIAL receiver,
and reconnects automatically.

A small web UI edits the rules and can pause blocking for a set number of minutes.
The glass-style dashboard refreshes playback and connection status every five seconds.
Recent videos show both a date and time in the viewing browser's local format and timezone.
The last 30 videos persist across restarts alongside the rules.
The **Include YouTube Kids** checkbox explicitly controls Kids app blocking and starts
unchecked. Kids videos use the same keyword, channel and video rules as regular
YouTube. The current video ID is read from the initial Lounge reply while keeping
the library's immediate Kids rejection in place. Kids never gets playback commands,
an event subscription or a Lounge terminate command: live probing showed that
fully connecting and disconnecting can display “device disconnected” and exit the
Kids profile, even when playback initially continues normally.

A matched Kids video closes the entire Kids app or Kids profile inside YouTube
after a grace period, defaulting to **30 seconds** from detecting that video. Set **Grace time**
in the UI to 0–600 seconds (0 closes immediately). Switching to a regular profile
or to an allowed video before the grace period ends cancels the close. A different
matched video starts a new grace period. Missing or invalid current-video data
clears pending closes and leaves the app open; the UI reports that video rules
cannot be checked. Pausing blocking allows Kids again. Samsung's native app-control API is used when the
DIAL app launcher is unavailable. Failed closes are retried and shown in the UI.
The dashboard shows a sticky warning banner with the matched video, seconds left,
and a shrinking progress bar during the grace period. The timer ticks locally
between status updates and disappears when the match clears, blocking is paused,
Kids is excluded, or the dashboard loses contact with the guard. This banner is
in the web dashboard; the current Samsung remote/DIAL integration cannot draw a
custom overlay over the native YouTube app on the TV.

## Run locally

```bash
TV_IP=192.168.1.50 uv run yt_guard.py   # guard + UI on http://localhost:8080
```

Environment: `TV_IP` (required initial address and default subnet hint), `UI_PORT`,
`RULES_FILE`, `HISTORY_FILE` (defaults to `recent.json` beside the rules), `AUTH_FILE`,
`KEYWORDS` (seed list used only when no rules file exists),
and `LOG_LEVEL`. `DISCOVERY_CIDR` optionally overrides the discovery network and must
contain at most 256 addresses; `DISCOVERY_INTERVAL` controls retry throttling in seconds
(default: `60`).

## Deploy to Kubernetes

Runs on any cluster node on the same LAN as the TV (built for a single-node Talos box).

```bash
cp deploy/env.example deploy/.env   # set TV_IP and KUBECONFIG
bash deploy/deploy.sh
```

UI: `http://<node-ip>:30102` (LAN only, no login). Needs a `local-path` StorageClass.

Uses the stock `python:3.13.13-alpine` image with the script and UI mounted from a ConfigMap,
so no registry is needed. Rules, recent videos and private pairing state persist on a
1Mi `local-path` volume. The pairing file has mode `0600`; a known screen's token can
be refreshed even while Kids makes local DIAL pairing unavailable.

## Limits

- Video rules cover regular YouTube and optionally Kids on one TV. Kids filtering
  depends on the initial reply including `playlistModified.videoId` or `nowPlaying.videoId`.
  This was verified against the playing video on a Samsung Tizen TV. The first
  playlist item and autoplay's next video are never treated as the current video.
  Kids playback state is unavailable; snapshots do not prove whether it is playing
  or paused. The narrower parser sends the same handshake as the library and keeps
  its Kids blacklist; its effect on extended Kids playback still needs a live check.
  Samsung app visibility is checked before acting on a Kids snapshot, since Lounge
  can keep returning the last Kids video after YouTube has closed. While the app is
  hidden, only local visibility is polled; reopening YouTube resumes the connection.
- Playback is requested every three seconds while the event stream is open. Failed
  sessions are cleared before retrying, including partial handshakes from YouTube Kids,
  so reopening regular YouTube recovers automatically. Requests have deadlines,
  playback feeds silent for 30 seconds reconnect, and repeated transport failures
  rebuild the HTTP connection pool. A Kubernetes liveness probe restarts the process
  if its guard loop stops progressing; a TV being off does not fail that probe.
- Kids app closing depends on the TV's app-control API being reachable and supporting
  the installed app. The UI reports failed attempts rather than claiming protection.
- Automatic discovery requires the TV's DIAL receiver to be reachable. Initial
  pairing also needs regular YouTube open; saved pairing lets a known TV be
  rediscovered while Kids disables the app pairing endpoint. App-close failures
  also trigger discovery, since cloud connections can survive a LAN address change.
  Multiple Samsung DIAL TVs are treated as ambiguous; set `DISCOVERY_CIDR` to a
  network containing only the target.
- Matching uses title, channel name and @handle from YouTube oEmbed; videos without a
  keyword in those fields need a channel or video rule.
- The UI has no authentication; anyone on the LAN can change rules or pause blocking.
