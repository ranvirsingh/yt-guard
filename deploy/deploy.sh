#!/usr/bin/env bash
# Deploy or update yt-guard. Re-run after editing yt_guard.py, k8s.yaml or deploy/.env.
# Settings come from deploy/.env (not committed); see deploy/env.example.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/.env ] || { echo "Missing deploy/.env - copy deploy/env.example and fill it in" >&2; exit 1; }
set -a; . deploy/.env; set +a
: "${TV_IP:?set TV_IP in deploy/.env}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

kubectl apply -f deploy/k8s.yaml
kubectl -n yt-guard create configmap yt-guard-env \
  --from-literal=TV_IP="$TV_IP" \
  --from-literal=KEYWORDS="${KEYWORDS:-minecraft,roblox,fortnite}" \
  --from-literal=DEVICE_NAME="${DEVICE_NAME:-YT Guard}" \
  --from-literal=LOG_LEVEL="${LOG_LEVEL:-INFO}" \
  --from-literal=DISCOVERY_CIDR="${DISCOVERY_CIDR:-}" \
  --from-literal=DISCOVERY_INTERVAL="${DISCOVERY_INTERVAL:-60}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n yt-guard create configmap yt-guard-script --from-file=yt_guard.py --from-file=ui.html \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n yt-guard rollout restart deploy/yt-guard
kubectl -n yt-guard rollout status deploy/yt-guard --timeout=180s
