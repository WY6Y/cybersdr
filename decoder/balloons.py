"""
balloons.py — Classify airborne / pico-balloon WSPR candidates from spot history.

What we already store is enough for motion-based detection:
  call, grid, power, timestamp, band, snr

Balloon encodings we can partially interpret from stored fields:
  - Zachtek altitude-in-power: altitude_m ≈ power_dbm * 300
  - WB8ELK coarse altitude: power steps map to 0..18 km (index * 1000 m)
  - Telemetry Type-1 lookalikes: callsigns starting with 0 / Q / 1
    (grids/power in those packets are often encoded sensor data, not QTH)

U4B/Traquito "Basic Telemetry" (P101): a single WSPR spot (call + grid4 + power)
fully encodes 6-char grid, altitude, temp, voltage, speed and GPS-valid — no
pairing across transmissions or bands is needed. The callsign's 6 chars encode
grid56 + altitude; the grid4 + power fields encode temp/voltage/speed/gps as a
second packed integer. Reference: github.com/traquito/WsprEncoded
(WsprMessageTelemetryBasic.h) — decode_traquito_basic() below mirrors that
encoder's math exactly. WB8ELK fine telemetry is still not decoded.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from statistics import median
from typing import Optional

from decoder.grid import distance_km, grid_to_latlon

# Standard WSPR power vocabulary (dBm)
WSPR_POWERS = (0, 3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40, 43, 47, 50, 53, 57, 60)

# Punchlist seeds + strong historical movers we trust from EL29 history
SEED_BALLOONS = {
    "DG2GG": "Punchlist seed — multi-grid P13 track across NA",
    "KS4VA": "Punchlist seed — multi-grid P13 track",
    "AC3AU": "Pacific→Canada→Greenland P17 track (Jul 28–Aug 1)",
    "VE3VRO": "Long NA eastward drift then EU (JO21), constant P13",
}

# Calls that look like airborne movers but are more likely mobile/rover/portable
# or otherwise noisy — keep them out of auto-suspect unless manually confirmed.
DEFAULT_DISMISS = set()

_TELEM_RE = re.compile(r"^(0|Q|1)[A-Z0-9]{2,5}$", re.IGNORECASE)
_REAL_CALL_RE = re.compile(
    r"^<?([A-Z0-9]{1,2}\d[A-Z0-9]{1,4}(?:/[A-Z0-9]{1,4})?)>?$",
    re.IGNORECASE,
)

# U4B/Traquito Basic Telemetry callsign shape: [0/1/Q][base36][0-9][A-Z][A-Z][A-Z]
_TRAQUITO_CALL_RE = re.compile(r"^[01Q][0-9A-Z][0-9][A-Z]{3}$")


def _decode_base36(c: str) -> int:
    if "A" <= c <= "Z":
        return 10 + (ord(c) - ord("A"))
    return ord(c) - ord("0")


def decode_traquito_basic(call: str, grid4: str, power_dbm) -> Optional[dict]:
    """
    Decode one U4B/Traquito "Basic Telemetry" WSPR frame.

    A single spot (call + grid4 + power) is self-sufficient — the callsign's
    6 characters encode a 2-char grid56 extension + altitude; grid4 + power
    together encode temperature/voltage/speed/GPS-valid as a second packed
    integer. No pairing with another transmission, band, or time slot is
    required. Mirrors WsprMessageTelemetryBasic.h's Decode() exactly
    (github.com/traquito/WsprEncoded).

    Returns None if call/grid/power don't fit the Basic Telemetry shape.
    """
    c = clean_call(call)
    g = (grid4 or "").upper().strip()

    if not _TRAQUITO_CALL_RE.match(c):
        return None
    try:
        power = int(power_dbm)
    except (TypeError, ValueError):
        return None
    if power not in WSPR_POWERS:
        return None
    if not (len(g) == 4 and "A" <= g[0] <= "R" and "A" <= g[1] <= "R" and g[2].isdigit() and g[3].isdigit()):
        return None

    id13 = c[0] + c[2]
    id2_val = _decode_base36(c[1])
    id4_val = ord(c[3]) - ord("A")
    id5_val = ord(c[4]) - ord("A")
    id6_val = ord(c[5]) - ord("A")

    val = 0
    val = val * 36 + id2_val
    val = val * 26 + id4_val
    val = val * 26 + id5_val
    val = val * 26 + id6_val

    alt_frac_m = val % 1068
    val //= 1068
    grid6_val = val % 24
    val //= 24
    grid5_val = val % 24
    val //= 24

    grid5 = chr(ord("A") + grid5_val)
    grid6 = chr(ord("A") + grid6_val)
    altitude_m = alt_frac_m * 20

    g1 = ord(g[0]) - ord("A")
    g2 = ord(g[1]) - ord("A")
    g3 = int(g[2])
    g4 = int(g[3])
    power_idx = WSPR_POWERS.index(power)

    val2 = 0
    val2 = val2 * 18 + g1
    val2 = val2 * 18 + g2
    val2 = val2 * 10 + g3
    val2 = val2 * 10 + g4
    val2 = val2 * 19 + power_idx

    telemetry_id = val2 % 2
    val2 //= 2
    gps_bit = val2 % 2
    val2 //= 2
    speed_num = val2 % 42
    val2 //= 42
    voltage_num = val2 % 40
    val2 //= 40
    temp_num = val2 % 90
    val2 //= 90

    out = {
        "id13": id13,
        "grid56": grid5 + grid6,
        "grid6": g + grid5 + grid6,
        "altitude_m": altitude_m,
    }
    if telemetry_id == 1:
        out.update(
            {
                "telemetry_type": "basic",
                "temperature_c": -50 + temp_num,
                "voltage_v": round(3.0 + ((voltage_num + 20) % 40) * 0.05, 2),
                "speed_knots": speed_num * 2,
                "gps_valid": bool(gps_bit),
            }
        )
    else:
        # Extended Telemetry frame — Basic decoder can't interpret the payload
        out["telemetry_type"] = "extended"
    return out


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _valid_grid4(grid: str) -> Optional[str]:
    g = (grid or "").upper().strip()
    if len(g) < 4:
        return None
    g4 = g[:4]
    if not (
        "A" <= g4[0] <= "R"
        and "A" <= g4[1] <= "R"
        and g4[2].isdigit()
        and g4[3].isdigit()
    ):
        return None
    return g4


def is_telemetry_call(call: str) -> bool:
    """True for invalid-prefix Type1 lookalikes used by balloon telem schemes."""
    c = (call or "").strip().upper()
    if c.startswith("<") and c.endswith(">"):
        c = c[1:-1]
    if not c or c == "...":
        return False
    # Real amateur prefixes never start with 0 or Q; 1* is almost always telem
    # (1A/1S are rare DX entities and not used as balloon telem in practice here).
    if c[0] in ("0", "Q"):
        return True
    if c[0] == "1" and _TELEM_RE.match(c):
        return True
    return False


def clean_call(call: str) -> str:
    c = (call or "").strip()
    if c.startswith("<") and c.endswith(">"):
        c = c[1:-1]
    return c.upper()


def zachtek_altitude_m(power_dbm: int) -> Optional[int]:
    """Zachtek WSPR-TX Pico: reported dBm ≈ altitude_m / 300 (0–18 km)."""
    try:
        p = int(power_dbm)
    except (TypeError, ValueError):
        return None
    if p not in WSPR_POWERS:
        return None
    return p * 300


def wb8elk_altitude_m(power_dbm: int) -> Optional[int]:
    """
    WB8ELK coarse altitude: power field index → kilometres * 1000.
    Index 0→0 km … index 12→12 km (power 40), etc.
    """
    try:
        p = int(power_dbm)
    except (TypeError, ValueError):
        return None
    if p not in WSPR_POWERS:
        return None
    return WSPR_POWERS.index(p) * 1000


def _track_points(spots: list[dict]) -> list[dict]:
    """Collapse consecutive identical 4-char grids into track waypoints."""
    pts = []
    last = None
    for s in spots:
        g4 = _valid_grid4(s.get("grid") or "")
        if not g4 or g4 == last:
            continue
        try:
            lat, lon = grid_to_latlon(g4)
        except ValueError:
            continue
        ts = _parse_ts(s.get("timestamp") or "")
        if not ts:
            continue
        pts.append(
            {
                "timestamp": s["timestamp"],
                "grid": g4,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "power": s.get("power"),
                "band": s.get("band"),
                "snr": s.get("snr"),
            }
        )
        last = g4
    return pts


def _hop_stats(pts: list[dict]) -> dict:
    hops = []
    for i in range(1, len(pts)):
        t0 = _parse_ts(pts[i - 1]["timestamp"])
        t1 = _parse_ts(pts[i]["timestamp"])
        if not t0 or not t1:
            continue
        dt_h = (t1 - t0).total_seconds() / 3600.0
        if dt_h <= 0:
            continue
        try:
            d = distance_km(pts[i - 1]["grid"], pts[i]["grid"])
        except ValueError:
            continue
        kmh = d / dt_h
        hops.append(
            {
                "from": pts[i - 1]["grid"],
                "to": pts[i]["grid"],
                "km": round(d, 1),
                "hours": round(dt_h, 2),
                "kmh": round(kmh, 1),
            }
        )

    if not hops:
        return {
            "hops": [],
            "max_kmh": 0.0,
            "median_kmh": 0.0,
            "total_km": 0.0,
            "teleport_fraction": 0.0,
        }

    speeds = [h["kmh"] for h in hops]
    teleports = sum(1 for s in speeds if s > 400)
    return {
        "hops": hops,
        "max_kmh": round(max(speeds), 1),
        "median_kmh": round(float(median(speeds)), 1),
        "total_km": round(sum(h["km"] for h in hops), 1),
        "teleport_fraction": round(teleports / len(hops), 3),
    }


def _span_km(pts: list[dict]) -> float:
    if len(pts) < 2:
        return 0.0
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    # Corner-to-corner of bounding box — coarse geographic extent
    try:
        # Use grid distance via synthetic corners by picking extreme points
        south = min(pts, key=lambda p: p["lat"])
        north = max(pts, key=lambda p: p["lat"])
        west = min(pts, key=lambda p: p["lon"])
        east = max(pts, key=lambda p: p["lon"])
        d_ns = distance_km(south["grid"], north["grid"])
        d_ew = distance_km(west["grid"], east["grid"])
        return round(max(d_ns, d_ew), 1)
    except ValueError:
        return 0.0


def classify_call(
    call: str,
    spots: list[dict],
    flag: Optional[dict] = None,
    force: bool = False,
) -> Optional[dict]:
    """
    Score one callsign's spot history for balloon / airborne likelihood.

    Returns None if there is nothing interesting to show (unless force=True).
    """
    raw_call = call
    call_u = clean_call(call)
    flag = flag or {}
    status = (flag.get("status") or "auto").lower()
    if status == "dismissed" and not force:
        return None

    telem = is_telemetry_call(raw_call) or is_telemetry_call(call_u)
    spots_sorted = sorted(spots, key=lambda s: s.get("timestamp") or "")
    if not spots_sorted:
        return None

    powers = sorted({int(s["power"]) for s in spots_sorted if s.get("power") is not None})
    bands = sorted({s["band"] for s in spots_sorted if s.get("band")})
    first = spots_sorted[0]["timestamp"]
    last = spots_sorted[-1]["timestamp"]
    pts = _track_points(spots_sorted)
    hop = _hop_stats(pts)
    span = _span_km(pts)
    grids = [p["grid"] for p in pts]
    n_grids = len(grids)

    reasons: list[str] = []
    score = 0
    kind = "mover"  # mover | balloon | telemetry

    traquito_frames = []
    if telem:
        kind = "telemetry"
        score = 88
        reasons.append("invalid-prefix callsign (0/Q/1) — balloon telemetry encoding")

        for s in spots_sorted:
            decoded = decode_traquito_basic(s.get("call") or raw_call, s.get("grid") or "", s.get("power"))
            if decoded:
                decoded["timestamp"] = s.get("timestamp")
                decoded["band"] = s.get("band")
                traquito_frames.append(decoded)

        if traquito_frames:
            latest = traquito_frames[-1]
            if latest["telemetry_type"] == "basic":
                score = 92
                reasons = [
                    f"U4B/Traquito Basic Telemetry decoded — alt {latest['altitude_m']}m, "
                    f"{latest['temperature_c']}°C, {latest['voltage_v']}V, "
                    f"{latest['speed_knots']}kn, GPS {'lock' if latest['gps_valid'] else 'no-lock'} "
                    f"(channel {latest['id13']})"
                ]
            else:
                reasons.append(f"Extended Telemetry frame on channel {latest['id13']} — payload not decodable by Basic decoder")

        # Do not treat telem grids as a flight path
        pts = []
        hop = {
            "hops": [],
            "max_kmh": 0.0,
            "median_kmh": 0.0,
            "total_km": 0.0,
            "teleport_fraction": 0.0,
        }
        span = 0.0
        grids = []
        n_grids = len({(s.get("grid") or "")[:4] for s in spots_sorted})
    else:
        if call_u in SEED_BALLOONS:
            score = max(score, 86)
            reasons.append(SEED_BALLOONS[call_u])
            kind = "balloon"

        if n_grids >= 3 and span >= 400:
            score += min(28, n_grids * 3)
            score += min(22, int(span / 250))
            reasons.append(f"{n_grids} distinct grids spanning ~{int(span)} km")

        med = hop["median_kmh"]
        mx = hop["max_kmh"]
        if 15 <= med <= 180 and hop["hops"]:
            score += 24
            reasons.append(f"median hop {med} km/h (pico-balloon / jetstream range)")
            kind = "balloon" if score >= 55 else kind
        elif 25 <= mx <= 220 and hop["hops"]:
            score += 12
            reasons.append(f"peak hop {mx} km/h")

        if hop["teleport_fraction"] >= 0.35 and n_grids >= 3:
            score -= 28
            reasons.append("many teleport-speed hops — likely bad/telem grids")
        if hop["median_kmh"] > 250 and n_grids <= 5:
            score -= 35
            reasons.append(f"median hop {hop['median_kmh']} km/h too fast for balloon (decode glitch?)")

        if len(powers) == 1 and powers[0] in WSPR_POWERS:
            score += 12
            reasons.append(f"constant reported power {powers[0]} dBm")
            if powers[0] in (10, 13, 17, 20, 23, 27, 30, 33, 37, 40):
                score += 6
        elif len(powers) >= 3 and all(p in WSPR_POWERS for p in powers) and span >= 500:
            score += 16
            reasons.append("power steps through WSPR vocabulary (possible altitude encoding)")

        # Prefer coherent long tracks
        if span >= 2000 and n_grids >= 5 and hop["teleport_fraction"] < 0.25:
            score += 10
            kind = "balloon"

    if status == "confirmed":
        score = max(score, 95)
        kind = "balloon" if not telem else "telemetry"
        reasons.insert(0, "manually confirmed")
    elif status == "watch":
        score = max(score, 60)
        reasons.insert(0, "on watch list")

    # Minimum bar for auto suspects
    if not force:
        if status == "auto" and score < 48:
            return None
        if call_u in DEFAULT_DISMISS and status == "auto":
            return None

    # Altitude hints from latest / power set (interpretation only)
    alt_hints = []
    for p in powers[-5:]:
        alt_hints.append(
            {
                "power_dbm": p,
                "zachtek_m": zachtek_altitude_m(p),
                "wb8elk_m": wb8elk_altitude_m(p),
            }
        )

    t0 = _parse_ts(first)
    t1 = _parse_ts(last)
    span_h = round((t1 - t0).total_seconds() / 3600.0, 1) if t0 and t1 else None

    return {
        "call": raw_call if raw_call.startswith("<") else call_u,
        "call_clean": call_u,
        "kind": kind,
        "score": int(max(0, min(99, score))),
        "status": status,
        "note": flag.get("note") or SEED_BALLOONS.get(call_u, ""),
        "spot_count": len(spots_sorted),
        "grid_count": n_grids,
        "grids": grids,
        "powers": powers,
        "bands": bands,
        "first_seen": first,
        "last_seen": last,
        "span_hours": span_h,
        "span_km": span,
        "max_kmh": hop["max_kmh"],
        "median_kmh": hop["median_kmh"],
        "total_track_km": hop["total_km"],
        "teleport_fraction": hop["teleport_fraction"],
        "reasons": reasons,
        "altitude_hints": alt_hints,
        "track": pts,
        "hops": hop["hops"][-20:],
        "traquito_channel": traquito_frames[-1]["id13"] if traquito_frames else None,
        "traquito_latest": traquito_frames[-1] if traquito_frames else None,
        "traquito_frames": traquito_frames[-50:],
        "last_spot": {
            "timestamp": spots_sorted[-1].get("timestamp"),
            "grid": spots_sorted[-1].get("grid"),
            "band": spots_sorted[-1].get("band"),
            "snr": spots_sorted[-1].get("snr"),
            "power": spots_sorted[-1].get("power"),
            "distance_km": spots_sorted[-1].get("distance_km"),
            "country": spots_sorted[-1].get("country"),
        },
    }


def classify_all(
    spots_by_call: dict[str, list[dict]],
    flags: Optional[dict[str, dict]] = None,
    include_telemetry: bool = True,
    min_score: int = 48,
) -> list[dict]:
    """Classify every call in spots_by_call; return suspects sorted by score."""
    flags = flags or {}
    out = []
    for call, spots in spots_by_call.items():
        flag = flags.get(clean_call(call)) or flags.get(call)
        row = classify_call(call, spots, flag=flag)
        if not row:
            continue
        if row["kind"] == "telemetry" and not include_telemetry:
            continue
        if row["score"] < min_score and row["status"] == "auto":
            continue
        out.append(row)
    out.sort(key=lambda r: (-r["score"], r["last_seen"] or ""))
    return out


def group_traquito_channels(spots: list[dict]) -> dict[str, list[dict]]:
    """
    Decode every U4B/Traquito telemetry spot and bucket by id13 channel.

    A telemetry callsign changes with every altitude/grid update, so grouping
    by literal `call` (like classify_all does) fragments one flight's history
    into many one-frame entries. id13 (chars 1 and 3 of the callsign, e.g.
    'Q6') is the flight's fixed channel identifier and is stable across a
    flight's whole transmission history — that's the real grouping key.
    """
    channels: dict[str, list[dict]] = {}
    for s in spots:
        decoded = decode_traquito_basic(s.get("call") or "", s.get("grid") or "", s.get("power"))
        if not decoded:
            continue
        decoded["timestamp"] = s.get("timestamp")
        decoded["band"] = s.get("band")
        decoded["call"] = clean_call(s.get("call") or "")
        decoded["snr"] = s.get("snr")
        channels.setdefault(decoded["id13"], []).append(decoded)
    for id13 in channels:
        channels[id13].sort(key=lambda f: f.get("timestamp") or "")
    return channels


def summarize_channel(id13: str, frames: list[dict]) -> Optional[dict]:
    """Summarize one id13 channel's decoded frame history into a track."""
    if not frames:
        return None

    basic = [f for f in frames if f.get("telemetry_type") == "basic"]
    extended_count = len(frames) - len(basic)

    track = []
    for f in basic:
        grid6 = f.get("grid6") or ""
        if len(grid6) < 6:
            continue
        try:
            lat, lon = grid_to_latlon(grid6)
        except ValueError:
            continue
        track.append(
            {
                "timestamp": f["timestamp"],
                "grid6": grid6,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "altitude_m": f["altitude_m"],
                "temperature_c": f["temperature_c"],
                "voltage_v": f["voltage_v"],
                "speed_knots": f["speed_knots"],
                "gps_valid": f["gps_valid"],
                "band": f.get("band"),
                "call": f.get("call"),
            }
        )

    hop = _hop_stats([{**p, "grid": p["grid6"]} for p in track])
    span = _span_km([{**p, "grid": p["grid6"]} for p in track])
    bands = sorted({f.get("band") for f in frames if f.get("band")})
    alts = [p["altitude_m"] for p in track]

    # id13 is only 2 chars (30 possible values: 0/1/Q x 0-9) — with no
    # frequency-lane/time-slot data to disambiguate the real 600-channel
    # U4B registry, unrelated trackers regularly land on the same id13.
    #
    # A speed-only sanity check isn't enough: two unrelated trackers landing
    # on the same id13 with widely-spaced timestamps can produce a low
    # *implied* speed (e.g. Arctic -> Antarctica over 5 days) while still
    # being geographically impossible for one balloon — pico-balloons ride
    # jet-stream circulation, which is zonal (east-west); they don't cross
    # from one pole to the other. Check latitude coherence explicitly, and
    # require at least a couple of hops before claiming any confidence at
    # all — a single frame is unconfirmed, not validated.
    MIN_COHERENT_FRAMES = 3  # >= 2 hops
    MAX_LAT_SPAN_DEG = 120  # a real flight's whole track
    MAX_HOP_LAT_DELTA_DEG = 70  # any single hop

    lats = [p["lat"] for p in track]
    lat_span = (max(lats) - min(lats)) if lats else 0.0
    max_hop_lat_delta = max(
        (abs(track[i]["lat"] - track[i - 1]["lat"]) for i in range(1, len(track))),
        default=0.0,
    )

    if len(track) < MIN_COHERENT_FRAMES:
        coherent = False
        coherence_note = (
            f"only {len(track)} decoded frame(s) on this channel — not enough "
            "data yet to confirm this is a single flight"
        )
    else:
        bad_speed = hop["hops"] and (hop["median_kmh"] > 250 or hop["teleport_fraction"] >= 0.35)
        bad_geo = lat_span > MAX_LAT_SPAN_DEG or max_hop_lat_delta > MAX_HOP_LAT_DELTA_DEG
        coherent = not (bad_speed or bad_geo)
        coherence_note = (
            None
            if coherent
            else "id13 alone can't disambiguate the full U4B channel — "
            "these frames are likely multiple unrelated trackers sharing this 2-char channel, not one flight"
        )

    return {
        "id13": id13,
        "coherent": coherent,
        "coherence_note": coherence_note,
        "frame_count": len(frames),
        "basic_count": len(basic),
        "extended_count": extended_count,
        "bands": bands,
        "first_seen": frames[0]["timestamp"],
        "last_seen": frames[-1]["timestamp"],
        "span_km": span,
        "max_kmh": hop["max_kmh"],
        "median_kmh": hop["median_kmh"],
        "altitude_min_m": min(alts) if alts else None,
        "altitude_max_m": max(alts) if alts else None,
        "latest": track[-1] if track else None,
        "track": track,
        "hops": hop["hops"][-20:],
    }


def classify_channels(
    spots: list[dict],
    min_frames: int = 1,
) -> list[dict]:
    """Group telemetry spots by id13 channel and summarize each into a track."""
    channels = group_traquito_channels(spots)
    out = []
    for id13, frames in channels.items():
        if len(frames) < min_frames:
            continue
        summary = summarize_channel(id13, frames)
        if summary:
            out.append(summary)
    out.sort(key=lambda c: c["last_seen"] or "", reverse=True)
    return out


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))
