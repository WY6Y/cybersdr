#!/usr/bin/env bash
# systemd ExecStart wrapper: build the public balloon JSON, deploy only if
# it actually changed (skips the Bluehost push, and the SSH connection that
# comes with it, on the common case where nothing new decoded since last run).
set -euo pipefail

cd "$(dirname "$0")"

RESULT="$(python3 publish_balloons.py --days 14)"
echo "$RESULT"

if [ "$RESULT" = "CHANGED" ]; then
  ~/wy6y-net-site/deploy-balloons.sh
fi
