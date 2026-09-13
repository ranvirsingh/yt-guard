# yt-guard

Blocks YouTube videos on a Samsung TV by keyword, channel, or video ID.

It connects to the TV's YouTube app over the Lounge API (the link phones use to cast),
reads the video that is playing, and when it matches a rule presses Back, then closes
the YouTube app if the video keeps playing. Pairing happens automatically over the LAN
via DIAL, so no phone or pairing code is needed.

A small web UI edits the rules and can pause blocking for a set number of minutes.

## Run locally

```bash
TV_IP=192.168.1.50 uv run yt_guard.py   # guard + UI on http://localhost:8080
```

Environment: `TV_IP` (required), `UI_PORT`, `RULES_FILE`, `AUTH_FILE`,
`KEYWORDS` (seed list used only when no rules file exists), `LOG_LEVEL`.

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
- Matching uses title, channel name and @handle from YouTube oEmbed; videos without a
  keyword in those fields need a channel or video rule.
- The UI has no authentication; anyone on the LAN can change rules or pause blocking.
