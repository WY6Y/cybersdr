#!/usr/bin/env python3
"""One-shot analysis of WSPR spots for airborne/balloon candidates."""
import math
import sqlite3
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else "/data/cybersdr.db"


def grid_ll(g):
    g = (g or "").upper().strip()
    if len(g) < 4:
        return None
    if not (g[0].isalpha() and g[1].isdigit() and g[2].isalpha() and g[3].isdigit()):
        return None
    lon = (ord(g[0]) - 65) * 20 + int(g[1]) * 2 - 180 + 1
    lat = (ord(g[2]) - 65) * 10 + int(g[3]) - 90 + 0.5
    return lat, lon


def haversine(a, b):
    R = 6371.0
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
print("total", conn.execute("select count(*) from spots").fetchone()[0])

calls = conn.execute(
    """
    SELECT call, COUNT(*) as n, COUNT(DISTINCT substr(grid,1,4)) as grids,
           MIN(timestamp) as first_ts, MAX(timestamp) as last_ts
    FROM spots GROUP BY call HAVING grids>=3 AND n>=3
    ORDER BY grids DESC LIMIT 120
    """
).fetchall()

candidates = []
for c in calls:
    call = c["call"]
    rows = conn.execute(
        "SELECT timestamp,grid,power,band,snr FROM spots WHERE call=? ORDER BY timestamp",
        (call,),
    ).fetchall()
    pts = []
    last_g = None
    for r in rows:
        g = r["grid"][:4]
        if g != last_g:
            ll = grid_ll(g)
            if ll:
                pts.append((parse_ts(r["timestamp"]), g, ll, r["power"], r["band"]))
            last_g = g
    max_kmh = 0
    max_hop = None
    total_km = 0
    for i in range(1, len(pts)):
        dt_h = (pts[i][0] - pts[i - 1][0]).total_seconds() / 3600
        if dt_h <= 0:
            continue
        d = haversine(pts[i - 1][2], pts[i][2])
        total_km += d
        kmh = d / dt_h
        if kmh > max_kmh:
            max_kmh = kmh
            max_hop = (pts[i - 1][1], pts[i][1], round(d), round(dt_h, 1), round(kmh, 1))
    powers = sorted({r["power"] for r in rows})
    span_h = (parse_ts(c["last_ts"]) - parse_ts(c["first_ts"])).total_seconds() / 3600
    avg_kmh = total_km / span_h if span_h > 0 else 0
    # span across grids (bounding)
    lats = [p[2][0] for p in pts]
    lons = [p[2][1] for p in pts]
    span_km = haversine((min(lats), min(lons)), (max(lats), max(lons))) if len(pts) >= 2 else 0
    candidates.append(
        {
            "call": call,
            "n": c["n"],
            "grids": c["grids"],
            "powers": powers,
            "max_kmh": round(max_kmh, 1),
            "avg_kmh": round(avg_kmh, 1),
            "total_km": round(total_km),
            "span_km": round(span_km),
            "span_h": round(span_h, 1),
            "max_hop": max_hop,
            "first": c["first_ts"][:10],
            "last": c["last_ts"][:10],
            "track": [(p[0].isoformat(), p[1], p[3]) for p in pts],
        }
    )

# Airborne-like: sustained geographic span + hop speed in balloon/aircraft range
# Pico balloons ~40-180 km/h jetstream; aircraft faster; exclude tiny local hops.
air = [
    x
    for x in candidates
    if x["grids"] >= 3
    and x["span_km"] >= 400
    and x["max_kmh"] >= 20
    and x["max_kmh"] < 900  # exclude bad/teleport decode glitches
]
air.sort(key=lambda x: (-x["grids"], -x["span_km"], -x["max_kmh"]))
print(f"\n=== Airborne-like candidates ({len(air)}) ===")
for x in air[:40]:
    print(
        f"{x['call']:12} grids={x['grids']:2} n={x['n']:3} P={x['powers']} "
        f"max={x['max_kmh']:6.1f}km/h avg={x['avg_kmh']:5.1f} span_km={x['span_km']:5} "
        f"{x['first']}→{x['last']} hop={x['max_hop']}"
    )

print("\n=== Known suspects detail ===")
for call in ("DG2GG", "KS4VA"):
    for x in candidates:
        if x["call"] == call:
            print(call, x)
            break
    else:
        # still print spots
        rows = conn.execute(
            "SELECT timestamp,grid,power,band FROM spots WHERE call=? ORDER BY timestamp",
            (call,),
        ).fetchall()
        print(call, "raw", [dict(r) for r in rows])

print("\n=== Telemetry-like callsigns (0*/Q*/1*) ===")
tels = conn.execute(
    """
    SELECT call, COUNT(*) n, COUNT(DISTINCT substr(grid,1,4)) grids,
           MIN(timestamp) a, MAX(timestamp) b,
           GROUP_CONCAT(DISTINCT power) powers
    FROM spots WHERE call GLOB '0*' OR call GLOB 'Q*' OR call GLOB '1*'
    GROUP BY call ORDER BY n DESC LIMIT 40
    """
).fetchall()
for r in tels:
    print(dict(r))

print("\n=== Constant low-power (10/13/17/20) multi-grid ===")
for pwr in (10, 13, 17, 20):
    rows = conn.execute(
        """
        SELECT call, COUNT(*) n, COUNT(DISTINCT substr(grid,1,4)) grids
        FROM spots
        WHERE call IN (
          SELECT call FROM spots GROUP BY call
          HAVING COUNT(DISTINCT power)=1 AND MIN(power)=?
        )
        GROUP BY call HAVING grids>=3
        ORDER BY grids DESC LIMIT 15
        """,
        (pwr,),
    ).fetchall()
    print(f"P={pwr}:", [dict(r) for r in rows])
