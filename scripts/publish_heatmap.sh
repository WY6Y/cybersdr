#!/usr/bin/env bash
# systemd ExecStart wrapper: build the public HF Path Heatmap JSON, deploy
# only if it actually changed (skips the Bluehost push on quiet polls).
set -euo pipefail

cd "$(dirname "$0")"

RESULT="$(python3 publish_heatmap.py --history-days 30)"
echo "$RESULT"

if [ "$RESULT" = "CHANGED" ]; then
  ~/wy6y-net-site/deploy-heatmap.sh
fi
