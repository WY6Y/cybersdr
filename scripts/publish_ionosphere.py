#!/usr/bin/env python3
"""
publish_ionosphere.py — build the public-safe ionosphere postcard JSON (P95).

Reads CyberSDR's own LOCAL loopback API (never anything remote/public) and
writes a public-safe snapshot for wy6y.net's "state of the ionosphere from
EL29" postcard page. Same shape as CyberSDR's own /api/ionosphere_snapshot —
nothing here is sensitive (WSPR spot data is inherently public; wsprnet.org
already publishes the same class of information).

On-demand for now (P95 decision 2026-08-18): run by hand, or wire to a
systemd timer later once the page's look is confirmed. Not on a timer yet.

Never touches the staged file if the built content is byte-identical to what
is already there — the deploy wrapper can use that to skip the Bluehost
rsync push when nothing changed.

Usage: publish_ionosphere.py [--api http://127.0.0.1:5020] [--out PATH] [--days N]
Prints CHANGED or UNCHANGED on stdout; exits non-zero only on a hard failure
(unreachable API, malformed response) — the staged file is left untouched.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import requests

DEFAULT_API = "http://127.0.0.1:5020"
DEFAULT_OUT = Path.home() / "wy6y-net-site" / "data" / "ionosphere.json"


def fetch_snapshot(api_base: str, days: int) -> dict:
    resp = requests.get(f"{api_base}/api/ionosphere_snapshot", params={"days": days}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or "spot_count" not in data:
        raise ValueError("/api/ionosphere_snapshot response missing expected fields")
    return data


def build_payload(api_base: str, days: int, generated_at: str) -> dict:
    snap = fetch_snapshot(api_base, days)
    best_dx = snap.get("best_dx")
    if best_dx:
        # Public page shows the grid/band/distance, not the specific callsign —
        # matches the balloon-watch precedent of not treating a WSPR spot's own
        # transmitting call as the interesting/public part of the story.
        best_dx = {k: v for k, v in best_dx.items() if k != "call"}

    return {
        "generated_at": generated_at,
        "days": snap.get("days"),
        "start": snap.get("start"),
        "end": snap.get("end"),
        "spot_count": snap.get("spot_count"),
        "unique_grids": snap.get("unique_grids"),
        "unique_calls": snap.get("unique_calls"),
        "avg_snr": snap.get("avg_snr"),
        "max_distance_km": snap.get("max_distance_km"),
        "bands": snap.get("bands"),
        "most_active_band": snap.get("most_active_band"),
        "best_dx": best_dx,
        "busiest_day": snap.get("busiest_day"),
        "daily_counts": snap.get("daily_counts"),
        "space_weather": snap.get("space_weather"),
        "space_weather_daily": snap.get("space_weather_daily"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--generated-at", default=None, help="override for testing")
    args = ap.parse_args()

    generated_at = args.generated_at or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        payload = build_payload(args.api, args.days, generated_at)
    except Exception as exc:  # noqa: BLE001 - hard-fail guard, never touch staged file
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    new_body = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    old = None
    if args.out.exists():
        try:
            existing = json.loads(args.out.read_text())
            old = {k: v for k, v in existing.items() if k != "generated_at"}
        except (json.JSONDecodeError, OSError):
            old = None

    new_compare = {k: v for k, v in payload.items() if k != "generated_at"}
    if old == new_compare:
        print("UNCHANGED")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(new_body)
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
