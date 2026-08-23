#!/usr/bin/env python3
"""
publish_heatmap.py — build the public-safe HF Path Heatmap JSON for wy6y.net.

Reads CyberSDR's own LOCAL loopback API (never anything remote/public) and
writes a compact static export that lets the public page mirror:
  - WSPR-tab map heatmap (live band/window chips + ~30-day scrubber)
  - PROP-tab panels under the map (solar KPIs, HF forecast, storm hangover,
    SFI/K charts, personal band-openness heatmap)

What's shipped:
  - Aggregated Maidenhead grid4 counts (not raw spots / callsigns)
  - Per-window summary (spot counts, SNR, max DX) + space-weather averages
  - PROP: current space weather + band forecast, 7d SFI history, 48h WSPR
    hourly counts, K history, storm hangover table, openness model
  - Station grid (public; already on QRZ)

What's deliberately dropped vs the private API: individual spot rows, SNR
per spot, callsigns, hangover recovery curves (table rates only). Path heat
is rebuilt client-side from grid counts the same way CyberSDR's own map does.

Never touches the staged file if the built content is byte-identical to what
is already there (ignoring generated_at) — the systemd wrapper uses that to
skip the Bluehost rsync push when nothing changed.

Usage: publish_heatmap.py [--api http://127.0.0.1:5020] [--out PATH]
                         [--history-days N] [--grid EM15fo]
Prints CHANGED or UNCHANGED on stdout; exits non-zero only on a hard failure.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Optional

import requests

DEFAULT_API = "http://127.0.0.1:5020"
DEFAULT_OUT = Path.home() / "wy6y-net-site" / "data" / "heatmap.json"
DEFAULT_GRID = "EM15fo"
HOURS_OPTIONS = (6, 12, 24, 48, 168)
HISTORY_HOURS = 24  # scrubber snapshots are 24h windows (split by band client-side)


def fetch_json(api_base: str, path: str, params: Optional[dict] = None):
    resp = requests.get(f"{api_base}{path}", params=params or {}, timeout=45)
    resp.raise_for_status()
    return resp.json()


def fetch(api_base: str, path: str, params: Optional[dict] = None) -> dict:
    data = fetch_json(api_base, path, params)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return data


def fetch_list(api_base: str, path: str, params: Optional[dict] = None) -> list:
    data = fetch_json(api_base, path, params)
    if not isinstance(data, list):
        raise ValueError(f"{path} did not return a JSON array")
    return data


def aggregate_spots(spots: list) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Return (grids_all, grids_by_band) from /api/heatmap spots."""
    grids_all: collections.Counter = collections.Counter()
    by_band: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for s in spots or []:
        g = (s.get("grid") or "")[:4].upper()
        if len(g) < 4:
            continue
        band = (s.get("band") or "").lower()
        grids_all[g] += 1
        if band:
            by_band[band][g] += 1
    return (
        dict(grids_all),
        {b: dict(c) for b, c in sorted(by_band.items())},
    )


def public_summary(summary: Optional[dict]) -> dict:
    """Keep counts only; drop nothing sensitive (already count-level)."""
    if not summary:
        return {}
    out = {
        "spot_count": summary.get("spot_count"),
        "unique_grids": summary.get("unique_grids"),
        "unique_stations": summary.get("unique_calls"),  # rename: no call list shipped
        "avg_snr": summary.get("avg_snr"),
        "max_distance_km": summary.get("max_distance_km"),
        "bands": summary.get("bands") or {},
        "truncated": bool(summary.get("truncated")),
    }
    return out


def window_from_payload(data: dict, hours: int) -> dict[str, Any]:
    grids_all, grids_by_band = aggregate_spots(data.get("spots") or [])
    return {
        "hours": hours,
        "start": data.get("start"),
        "end": data.get("end"),
        "summary": public_summary(data.get("summary")),
        "space_weather": data.get("space_weather") or {},
        "grids": grids_all,
        "grids_by_band": grids_by_band,
    }


def build_live_windows(api_base: str) -> dict[str, dict]:
    """One all-band fetch per hours option; band filters derived from grids_by_band."""
    windows: dict[str, dict] = {}
    for hours in HOURS_OPTIONS:
        data = fetch(api_base, "/api/heatmap", {"hours": hours})
        windows[str(hours)] = window_from_payload(data, hours)
    return windows


def build_history(api_base: str, days: int) -> list[dict]:
    """Daily 24h snapshots for the scrubber (oldest first)."""
    days = max(1, min(int(days), 60))
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    history: list[dict] = []
    for i in range(days - 1, -1, -1):
        end = now - dt.timedelta(days=i)
        end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = fetch(
            api_base,
            "/api/heatmap",
            {"hours": HISTORY_HOURS, "end": end_iso},
        )
        row = window_from_payload(data, HISTORY_HOURS)
        row["day"] = end.strftime("%Y-%m-%d")
        history.append(row)
    return history


def public_hangover(raw: dict) -> dict:
    """Keep table fields; drop bulky per-band recovery curves."""
    bands = []
    for b in raw.get("bands") or []:
        bands.append({
            "band": b.get("band"),
            "baseline_rate": b.get("baseline_rate"),
            "current_rate": b.get("current_rate"),
            "hours_to_recover": b.get("hours_to_recover"),
            "pct_of_baseline": b.get("pct_of_baseline"),
            "recovered": b.get("recovered"),
        })
    return {
        "event": raw.get("event"),
        "recovery_frac": raw.get("recovery_frac"),
        "bands": bands,
    }


def build_prop(api_base: str) -> dict:
    """PROP-tab payload: solar KPIs, forecast, hangover, charts, openness."""
    return {
        "spaceweather": fetch(api_base, "/api/spaceweather"),
        "sfi_history": fetch_list(api_base, "/api/spaceweather/history"),
        "k_history": fetch_list(api_base, "/api/spaceweather/khistory"),
        "wspr_hourly": fetch_list(api_base, "/api/wspr/hourly"),
        "storm_hangover": public_hangover(fetch(api_base, "/api/storm_hangover")),
        "openness_model": fetch(api_base, "/api/openness_model", {"days": 60}),
    }


def build_payload(api_base: str, grid: str, history_days: int, generated_at: str) -> dict:
    meta = fetch(api_base, "/api/heatmap/meta", {"days": history_days})
    live = build_live_windows(api_base)
    history = build_history(api_base, history_days)
    prop = build_prop(api_base)

    # Public meta: coverage + daily density, no private paths
    public_meta = {
        "earliest": meta.get("earliest"),
        "latest": meta.get("latest"),
        "total": meta.get("total"),
        "bands": meta.get("bands") or {},
        "daily": meta.get("daily") or [],
        "days": meta.get("days") or history_days,
    }

    return {
        "generated_at": generated_at,
        "station_grid": grid,
        "station_label": "WY6Y",
        "hours_options": list(HOURS_OPTIONS),
        "history_hours": HISTORY_HOURS,
        "meta": public_meta,
        "live": live,
        "history": history,
        "prop": prop,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--history-days", type=int, default=30)
    ap.add_argument("--grid", default=DEFAULT_GRID)
    ap.add_argument("--generated-at", default=None, help="override for testing")
    args = ap.parse_args()

    generated_at = args.generated_at or dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        payload = build_payload(args.api, args.grid, args.history_days, generated_at)
    except Exception as exc:  # noqa: BLE001 — hard-fail guard, never touch staged file
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    new_body = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    def for_compare(obj: Any) -> Any:
        """Ignore wall-clock window edges so a quiet 15-min poll can skip the push."""
        if isinstance(obj, dict):
            return {
                k: for_compare(v)
                for k, v in obj.items()
                if k not in ("generated_at", "start", "end", "fetched_at", "as_of", "hours_ago")
            }
        if isinstance(obj, list):
            return [for_compare(v) for v in obj]
        return obj

    old = None
    if args.out.exists():
        try:
            existing = json.loads(args.out.read_text())
            old = for_compare(existing)
        except (json.JSONDecodeError, OSError):
            old = None

    if old == for_compare(payload):
        print("UNCHANGED")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(new_body)
    print("CHANGED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
