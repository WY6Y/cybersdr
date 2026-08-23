#!/usr/bin/env python3
"""
publish_balloons.py — build the public-safe balloon watch JSON for wy6y.net.

Reads CyberSDR's own LOCAL loopback API (never anything remote/public) and
writes a public-safe JSON that mirrors CyberSDR's own BALLOON tab shape
closely enough for the public page to render the same list + map + track
experience: two views, "suspects" (named movers/balloons, from /api/balloons
+ per-call detail for track/hops) and "channels" (U4B/Traquito telemetry
grouped by id13, from /api/balloons/channels, coherent only).

What's deliberately dropped vs the private API: raw spot rows (SNR-level
receive detail per spot), balloon_flags workflow bookkeeping beyond the
status label itself, and rotating telemetry pseudo-callsigns as an identity
(those are relabeled "Channel <id13>"). Everything else — reasons, altitude
hints, full grid track, hop speeds — is the same classifier output CyberSDR's
own UI shows, since none of it is sensitive (WSPR spot data is inherently
public; wsprnet.org already publishes the same class of information).

Never touches the staged file if the built content is byte-identical to what
is already there — the systemd wrapper (publish_balloons.sh) uses that to
skip the Bluehost rsync push entirely when nothing changed.

Usage: publish_balloons.py [--api http://127.0.0.1:5020] [--out PATH] [--days N]
Prints CHANGED or UNCHANGED on stdout; exits non-zero only on a hard failure
(unreachable API, malformed response) — the staged file is left untouched so
a CyberSDR restart/rebuild window never publishes an empty page.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import requests

DEFAULT_API = "http://127.0.0.1:5020"
DEFAULT_OUT = Path.home() / "wy6y-net-site" / "data" / "balloons.json"
MIN_SCORE = 48  # same floor CyberSDR's own BALLOON tab defaults to

# Detail fields carried through verbatim from /api/balloons/<call> — same
# shape the private app's own FLIGHT DETAIL panel renders from.
SUSPECT_DETAIL_FIELDS = (
    "kind", "score", "bands", "powers", "grids",
    "first_seen", "last_seen", "span_km", "max_kmh", "median_kmh",
    "total_track_km", "teleport_fraction", "reasons", "altitude_hints",
    "track", "hops",
)

CHANNEL_FIELDS = (
    "id13", "coherent", "coherence_note", "frame_count", "basic_count",
    "extended_count", "bands", "first_seen", "last_seen", "span_km",
    "max_kmh", "median_kmh", "altitude_min_m", "altitude_max_m",
    "latest", "track", "hops",
)


def fetch(api_base: str, path: str, params: dict) -> dict:
    resp = requests.get(f"{api_base}{path}", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return data


def build_payload(api_base: str, days: int, generated_at: str) -> dict:
    listing = fetch(api_base, "/api/balloons", {"days": days, "min_score": 0})
    if "balloons" not in listing:
        raise ValueError("/api/balloons response missing 'balloons' key")

    channels_resp = fetch(api_base, "/api/balloons/channels", {"days": days})
    if "channels" not in channels_resp:
        raise ValueError("/api/balloons/channels response missing 'channels' key")

    suspects = []
    for b in listing["balloons"]:
        if b.get("kind") == "telemetry":
            continue  # handled via the channel-grouped source below
        status = (b.get("status") or "auto").lower()
        score = int(b.get("score") or 0)
        if status not in ("confirmed", "watch") and score < MIN_SCORE:
            continue

        call = b.get("call_clean") or b.get("call") or ""
        try:
            detail = fetch(api_base, f"/api/balloons/{call}", {"days": days})
        except Exception as exc:  # noqa: BLE001 - skip this one, don't fail the whole run
            print(f"WARN: detail fetch failed for {call}: {exc}", file=sys.stderr)
            continue

        row = {"label": call, "status": "suspected" if status == "auto" else status}
        row.update({k: detail.get(k) for k in SUSPECT_DETAIL_FIELDS})
        suspects.append(row)

    channels = []
    for c in channels_resp["channels"]:
        if not c.get("coherent"):
            continue
        row = {"label": f"Channel {c['id13']}"}
        row.update({k: c.get(k) for k in CHANNEL_FIELDS})
        channels.append(row)

    status_rank = {"confirmed": 0, "watch": 1, "suspected": 2}
    suspects.sort(key=lambda r: (status_rank.get(r["status"], 3), -(r["score"] or 0)))
    channels.sort(key=lambda r: r["last_seen"] or "", reverse=True)

    return {"generated_at": generated_at, "suspects": suspects, "channels": channels}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--days", type=int, default=14)
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
            old = (existing.get("suspects"), existing.get("channels"))
        except (json.JSONDecodeError, OSError):
            old = None

    if old == (payload["suspects"], payload["channels"]):
        print("UNCHANGED")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(new_body)
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
