# yt-guard

Blocks YouTube videos on a Samsung TV by keyword, channel, or video ID.

It connects to the TV's YouTube app over the Lounge API (the link phones use to cast),
reads the video that is playing, and when it matches a rule presses Back, then closes
the YouTube app if the video keeps playing. Pairing happens automatically over the LAN
via DIAL, so no phone or pairing code is needed. If DHCP changes the TV's address,
yt-guard scans the configured IPv4 `/24`, verifies the single Samsung DIAL receiver,
and reconnects automatically.

A small web UI edits the rules and can pause blocking for a set number of minutes.

## Run locally

```bash
TV_IP=192.168.1.50 uv run yt_guard.py   # guard + UI on http://localhost:8080
```

Environment: `TV_IP` (required initial address and default subnet hint), `UI_PORT`,
`RULES_FILE`, `AUTH_FILE`, `KEYWORDS` (seed list used only when no rules file exists),
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

Uses the stock `python:3.13.13-alpine` image with the script mounted from a ConfigMap,
so no registry is needed. Rules persist on a 1Mi `local-path` volume; auth lives in
`/tmp` and is re-created by pairing on start.

## Limits

- Covers one TV and only the regular YouTube app, not YouTube Kids or other devices.
- Automatic discovery requires the TV and YouTube DIAL service to be reachable. If the
  TV is off, discovery retries after YouTube is opened. Multiple Samsung DIAL TVs are
  treated as ambiguous; set `DISCOVERY_CIDR` to a network containing only the target.
- Matching uses title, channel name and @handle from YouTube oEmbed; videos without a
  keyword in those fields need a channel or video rule.
- The UI has no authentication; anyone on the LAN can change rules or pause blocking.
