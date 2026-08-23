"""
db.py — SQLite helpers for CyberSDR spot storage.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "/data/cybersdr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    call        TEXT NOT NULL,
    freq        REAL NOT NULL,
    band        TEXT NOT NULL,
    snr         REAL NOT NULL,
    drift       REAL NOT NULL,
    grid        TEXT NOT NULL,
    power       INTEGER NOT NULL,
    distance_km REAL,
    bearing     REAL,
    country     TEXT
);
CREATE INDEX IF NOT EXISTS idx_spots_timestamp ON spots(timestamp);
CREATE INDEX IF NOT EXISTS idx_spots_band      ON spots(band);
CREATE INDEX IF NOT EXISTS idx_spots_call      ON spots(call);

CREATE TABLE IF NOT EXISTS geocode_cache (
    grid    TEXT PRIMARY KEY,
    country TEXT
);

CREATE TABLE IF NOT EXISTS space_weather (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    sfi      REAL,
    sn       REAL,
    aindex   REAL,
    kindex   REAL
);
CREATE INDEX IF NOT EXISTS idx_sw_ts ON space_weather(ts);

CREATE TABLE IF NOT EXISTS balloon_flags (
    call       TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'auto',
    note       TEXT,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they do not exist yet."""
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: add `country` to spots tables created before this column existed.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(spots)")}
        if "country" not in cols:
            conn.execute("ALTER TABLE spots ADD COLUMN country TEXT")
        # Composite index speeds per-band historical heatmap windows (P83/P84).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_spots_band_ts ON spots(band, timestamp)"
        )


def insert_spot(spot: dict):
    """Persist one decoded spot row."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO spots
                (timestamp, call, freq, band, snr, drift, grid, power, distance_km, bearing, country)
            VALUES
                (:timestamp, :call, :freq, :band, :snr, :drift, :grid, :power,
                 :distance_km, :bearing, :country)
            """,
            {**spot, "country": spot.get("country")},
        )


def get_cached_country(grid: str):
    """
    Look up a previously geocoded grid square.
    Returns (True, country) if cached — country is "" for open ocean / no match —
    or (False, None) if this grid has never been looked up.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT country FROM geocode_cache WHERE grid = ?", (grid,)
        ).fetchone()
    return (True, row["country"]) if row else (False, None)


def cache_country(grid: str, country: str) -> None:
    """Remember the geocoded country for a grid square ("" = looked up, no match)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (grid, country) VALUES (?, ?)",
            (grid, country),
        )


def get_ungeocoded_grids() -> list:
    """Distinct grid squares among spots that haven't been geocoded yet."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT grid FROM spots WHERE country IS NULL"
        ).fetchall()
    return [r["grid"] for r in rows]


def backfill_country(grid: str, country: str) -> None:
    """Fill in `country` for every spot at this grid square still awaiting one."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE spots SET country = ? WHERE grid = ? AND country IS NULL",
            (country, grid),
        )


def get_recent_spots(n: int = 200) -> list:
    """Return the most recent *n* spots, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM spots ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def _utc_today() -> str:
    """Calendar day for spot stats — timestamps are UTC ISO strings."""
    return datetime.now(timezone.utc).date().isoformat()


def get_band_summary() -> list:
    """Return per-band counts and best SNR for the current UTC day."""
    # Spots store UTC timestamps; do NOT use local date.today() — after UTC
    # midnight (19:00 CDT) local "today" still lags and empties BAND ACTIVITY.
    today = _utc_today()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                band,
                COUNT(*)        AS count,
                MAX(snr)        AS best_snr,
                MAX(distance_km) AS farthest_km
            FROM spots
            WHERE date(timestamp) = ?
            GROUP BY band
            ORDER BY band
            """,
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_stats(my_grid: str) -> dict:
    """Return summary statistics for the current UTC day."""
    today = _utc_today()
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM spots WHERE date(timestamp) = ?", (today,)
        ).fetchone()[0]

        unique_calls = conn.execute(
            "SELECT COUNT(DISTINCT call) FROM spots WHERE date(timestamp) = ?",
            (today,),
        ).fetchone()[0]

        farthest = conn.execute(
            """
            SELECT call, grid, distance_km, band
            FROM spots
            WHERE date(timestamp) = ? AND distance_km IS NOT NULL
            ORDER BY distance_km DESC
            LIMIT 1
            """,
            (today,),
        ).fetchone()

        best_snr = conn.execute(
            """
            SELECT call, grid, snr, band
            FROM spots
            WHERE date(timestamp) = ?
            ORDER BY snr DESC
            LIMIT 1
            """,
            (today,),
        ).fetchone()

    return {
        "total_spots": total,
        "unique_calls": unique_calls,
        "farthest_dx": dict(farthest) if farthest else None,
        "best_snr": dict(best_snr) if best_snr else None,
    }


def insert_space_weather(data: dict) -> None:
    """Persist one space weather reading."""
    def sf(key):
        try:
            return float(data.get(key, None))
        except (TypeError, ValueError):
            return None

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO space_weather (ts, sfi, sn, aindex, kindex) VALUES (?,?,?,?,?)",
            (data.get("fetched_at"), sf("sfi"), sf("sn"), sf("aindex"), sf("kindex")),
        )


def get_space_weather_history(days: int = 7) -> list:
    """Return space weather readings for the last *days* days, oldest first."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, sfi, sn, aindex, kindex FROM space_weather "
            "WHERE ts > ? ORDER BY ts ASC",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_wspr_hourly_counts(hours: int = 48) -> list:
    """Return WSPR decode counts per UTC hour for the last *hours* hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT strftime('%Y-%m-%dT%H:00:00', timestamp) AS hour,
                   COUNT(*)                                  AS count
            FROM spots
            WHERE timestamp > ?
            GROUP BY hour
            ORDER BY hour ASC
            """,
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


HEATMAP_BANDS = ("40m", "30m", "20m", "17m", "15m", "12m", "10m")


def _parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to aware UTC datetime, or None."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_heatmap_meta() -> dict:
    """Earliest/latest spot times and per-band totals for the heatmap time machine."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(timestamp) AS earliest, MAX(timestamp) AS latest, COUNT(*) AS total "
            "FROM spots"
        ).fetchone()
        band_rows = conn.execute(
            "SELECT band, COUNT(*) AS count FROM spots GROUP BY band ORDER BY band"
        ).fetchall()
    return {
        "earliest": row["earliest"],
        "latest": row["latest"],
        "total": row["total"] or 0,
        "bands": {r["band"]: r["count"] for r in band_rows},
    }


def get_heatmap_daily_counts(days: int = 30, band: Optional[str] = None) -> list:
    """Daily spot counts for scrubber density marks (oldest first)."""
    days = max(1, min(int(days), 120))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = (
        "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS count "
        "FROM spots WHERE timestamp >= ?"
    )
    params: list = [since]
    if band and band in HEATMAP_BANDS:
        sql += " AND band = ?"
        params.append(band)
    sql += " GROUP BY day ORDER BY day ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_space_weather_for_window(start_iso: str, end_iso: str) -> dict:
    """Average / min / max solar indices inside a heatmap window."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS readings,
                AVG(sfi) AS sfi_avg, MIN(sfi) AS sfi_min, MAX(sfi) AS sfi_max,
                AVG(kindex) AS k_avg, MIN(kindex) AS k_min, MAX(kindex) AS k_max,
                AVG(aindex) AS a_avg,
                AVG(sn) AS sn_avg
            FROM space_weather
            WHERE ts >= ? AND ts <= ?
            """,
            (start_iso, end_iso),
        ).fetchone()
    if not row or not row["readings"]:
        return {"readings": 0}

    def _round(v, n=1):
        return None if v is None else round(float(v), n)

    return {
        "readings": int(row["readings"]),
        "sfi_avg": _round(row["sfi_avg"], 1),
        "sfi_min": _round(row["sfi_min"], 1),
        "sfi_max": _round(row["sfi_max"], 1),
        "k_avg": _round(row["k_avg"], 2),
        "k_min": _round(row["k_min"], 2),
        "k_max": _round(row["k_max"], 2),
        "a_avg": _round(row["a_avg"], 1),
        "sn_avg": _round(row["sn_avg"], 1),
    }


def get_spots_for_heatmap(
    hours: int = 48,
    limit: int = 6000,
    band: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    """
    Return spots + window summary for path-density / DX heatmap.

    Window is [end - hours, end]. end defaults to now (UTC).
    Optional band filter (e.g. "20m") for per-band heatmaps.
    """
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(100, min(int(limit), 12000))
    end_dt = _parse_iso_utc(end) or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=hours)
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()

    band_filter = band if band in HEATMAP_BANDS else None
    where = [
        "timestamp >= ?",
        "timestamp <= ?",
        "grid IS NOT NULL",
        "length(grid) >= 4",
    ]
    params: list = [start_iso, end_iso]
    if band_filter:
        where.append("band = ?")
        params.append(band_filter)

    where_sql = " AND ".join(where)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT grid, band, snr, distance_km, timestamp
            FROM spots
            WHERE {where_sql}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        summary_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS spot_count,
                COUNT(DISTINCT substr(grid, 1, 4)) AS unique_grids,
                COUNT(DISTINCT call) AS unique_calls,
                AVG(snr) AS avg_snr,
                MAX(distance_km) AS max_distance_km
            FROM spots
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        band_rows = conn.execute(
            f"""
            SELECT band, COUNT(*) AS count
            FROM spots
            WHERE {where_sql}
            GROUP BY band
            """,
            params,
        ).fetchall()

    spots = [dict(r) for r in rows]
    summary = {
        "spot_count": int(summary_row["spot_count"] or 0),
        "unique_grids": int(summary_row["unique_grids"] or 0),
        "unique_calls": int(summary_row["unique_calls"] or 0),
        "avg_snr": (
            None
            if summary_row["avg_snr"] is None
            else round(float(summary_row["avg_snr"]), 1)
        ),
        "max_distance_km": (
            None
            if summary_row["max_distance_km"] is None
            else round(float(summary_row["max_distance_km"]), 0)
        ),
        "bands": {r["band"]: r["count"] for r in band_rows},
        "returned": len(spots),
        "truncated": bool(summary_row["spot_count"] and summary_row["spot_count"] > len(spots)),
    }
    space_weather = get_space_weather_for_window(start_iso, end_iso)
    return {
        "hours": hours,
        "band": band_filter or "all",
        "start": start_iso,
        "end": end_iso,
        "count": len(spots),
        "spots": spots,
        "summary": summary,
        "space_weather": space_weather,
    }


def get_space_weather_daily(days: int = 30) -> list:
    """Daily-averaged SFI/K/A/SN for a sparkline trend (oldest first)."""
    days = max(1, min(int(days), 120))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT substr(ts, 1, 10) AS day,
                   AVG(sfi) AS sfi_avg, AVG(kindex) AS k_avg, AVG(sn) AS sn_avg
            FROM space_weather
            WHERE ts >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (since,),
        ).fetchall()

    def _round(v, n=1):
        return None if v is None else round(float(v), n)

    return [
        {"day": r["day"], "sfi": _round(r["sfi_avg"]), "k": _round(r["k_avg"], 2), "sn": _round(r["sn_avg"])}
        for r in rows
    ]


def get_ionosphere_snapshot(days: int = 30) -> dict:
    """
    Monthly-postcard snapshot (P95): spot/band totals, space-weather averages
    and daily trend, best DX contact, and busiest day, all for one rolling
    window. Feeds the public "state of the ionosphere" postcard page.
    """
    days = max(1, min(int(days), 120))
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    end_dt = datetime.now(timezone.utc)
    since_iso = since_dt.isoformat()
    end_iso = end_dt.isoformat()

    with get_conn() as conn:
        totals_row = conn.execute(
            """
            SELECT
                COUNT(*) AS spot_count,
                COUNT(DISTINCT substr(grid, 1, 4)) AS unique_grids,
                COUNT(DISTINCT call) AS unique_calls,
                AVG(snr) AS avg_snr,
                MAX(distance_km) AS max_distance_km
            FROM spots
            WHERE timestamp >= ? AND timestamp <= ?
            """,
            (since_iso, end_iso),
        ).fetchone()
        band_rows = conn.execute(
            """
            SELECT band, COUNT(*) AS count
            FROM spots WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY band ORDER BY count DESC
            """,
            (since_iso, end_iso),
        ).fetchall()
        best_dx_row = conn.execute(
            """
            SELECT call, band, grid, distance_km, snr, timestamp
            FROM spots
            WHERE timestamp >= ? AND timestamp <= ? AND distance_km IS NOT NULL
            ORDER BY distance_km DESC LIMIT 1
            """,
            (since_iso, end_iso),
        ).fetchone()

    daily_counts = get_heatmap_daily_counts(days=days)
    busiest_day = max(daily_counts, key=lambda r: r["count"], default=None)

    return {
        "days": days,
        "start": since_iso,
        "end": end_iso,
        "spot_count": int(totals_row["spot_count"] or 0),
        "unique_grids": int(totals_row["unique_grids"] or 0),
        "unique_calls": int(totals_row["unique_calls"] or 0),
        "avg_snr": None if totals_row["avg_snr"] is None else round(float(totals_row["avg_snr"]), 1),
        "max_distance_km": (
            None if totals_row["max_distance_km"] is None else round(float(totals_row["max_distance_km"]), 0)
        ),
        "bands": {r["band"]: r["count"] for r in band_rows},
        "most_active_band": band_rows[0]["band"] if band_rows else None,
        "best_dx": dict(best_dx_row) if best_dx_row else None,
        "busiest_day": busiest_day,
        "daily_counts": daily_counts,
        "space_weather": get_space_weather_for_window(since_iso, end_iso),
        "space_weather_daily": get_space_weather_daily(days=days),
    }


def get_spots_for_export(hours: Optional[int] = None, limit: int = 100000) -> list:
    """Return spots for CSV export, newest first. hours=None → all (capped by limit)."""
    with get_conn() as conn:
        if hours is not None:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                """
                SELECT timestamp, call, freq, band, snr, drift, grid, power,
                       distance_km, bearing, country
                FROM spots
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT timestamp, call, freq, band, snr, drift, grid, power,
                       distance_km, bearing, country
                FROM spots
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_band_conditions(my_grid: str, hours: int = 2) -> list:
    """
    Return per-band openness stats for the last *hours* hours.

    Each dict in the returned list contains:
        band, spot_count, avg_snr, max_distance_km, unique_calls,
        score (0-100), condition (DARK/WEAK/FAIR/OPEN/STRONG), color (hex)
    """
    BANDS = ["40m", "30m", "20m", "17m", "15m", "12m", "10m"]
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    results = []
    with get_conn() as conn:
        for band in BANDS:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)             AS spot_count,
                    AVG(snr)             AS avg_snr,
                    MAX(distance_km)     AS max_distance_km,
                    COUNT(DISTINCT call) AS unique_calls
                FROM spots
                WHERE band = ? AND timestamp >= ?
                """,
                (band, since),
            ).fetchone()

            spot_count   = row["spot_count"] or 0
            avg_snr      = row["avg_snr"]
            max_dist     = row["max_distance_km"]
            unique_calls = row["unique_calls"] or 0

            if spot_count == 0:
                score = 0
            else:
                score = min(100, spot_count * 4 + max(0, (avg_snr or -30) + 30) * 1.5)

            if score == 0:
                condition = "DARK"
                color = "#333344"
            elif score <= 20:
                condition = "WEAK"
                color = "#ff6600"
            elif score <= 50:
                condition = "FAIR"
                color = "#ffaa00"
            elif score <= 80:
                condition = "OPEN"
                color = "#00f5ff"
            else:
                condition = "STRONG"
                color = "#00ff88"

            results.append({
                "band":            band,
                "spot_count":      spot_count,
                "avg_snr":         round(avg_snr, 1) if avg_snr is not None else None,
                "max_distance_km": round(max_dist) if max_dist is not None else None,
                "unique_calls":    unique_calls,
                "score":           round(score, 1),
                "condition":       condition,
                "color":           color,
            })

    return results


HANGOVER_BANDS = ("40m", "30m", "20m", "17m", "15m", "12m", "10m")


def _find_storm_events(min_k: float, since_iso: str) -> list:
    """Contiguous runs of K-index readings >= min_k, oldest first.

    Each event: {peak_k, onset_ts, end_ts} — onset_ts is the first reading
    at/above threshold, end_ts is the last one before K drops back below it
    (readings are ~3h apart, from decoder/spaceweather.py's NOAA poll).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, kindex FROM space_weather WHERE ts >= ? AND kindex IS NOT NULL ORDER BY ts ASC",
            (since_iso,),
        ).fetchall()

    events = []
    current = None
    for r in rows:
        k = r["kindex"]
        if k >= min_k:
            if current is None:
                current = {"peak_k": k, "onset_ts": r["ts"], "end_ts": r["ts"]}
            else:
                current["end_ts"] = r["ts"]
                current["peak_k"] = max(current["peak_k"], k)
        else:
            if current is not None:
                events.append(current)
                current = None
    if current is not None:
        events.append(current)
    return events


def _hourly_band_rate(conn, band: str, start_iso: str, end_iso: str) -> float:
    """Average spots/hour for one band in [start_iso, end_iso)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM spots WHERE band = ? AND timestamp >= ? AND timestamp < ?",
        (band, start_iso, end_iso),
    ).fetchone()
    n = row["n"] or 0
    hours = max(1e-6, (_parse_iso_utc(end_iso) - _parse_iso_utc(start_iso)).total_seconds() / 3600)
    return n / hours


def get_storm_hangover(min_k: float = 5.0, lookback_days: int = 60, recovery_frac: float = 0.8, max_hours: int = 96) -> dict:
    """
    Storm hangover detector (P93): after each K>=min_k geomagnetic storm,
    measure how long each band's WSPR spot rate stays depressed relative
    to its pre-storm baseline, and when (if) it recovers.

    Returns the most recent storm event plus, per band:
        baseline_rate   — spots/hr in the 48h immediately before storm onset
        current_rate    — spots/hr in the last 3h
        pct_of_baseline — current_rate / baseline_rate (None if no baseline)
        recovered       — True once a 3h rate has reached recovery_frac of baseline
        hours_to_recover — hours from storm end to first recovery (None if not yet)
        curve           — hourly [{hour, rate, pct}] samples since storm end, capped at max_hours
    """
    since_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    events = _find_storm_events(min_k, since_iso)
    if not events:
        return {"event": None, "bands": []}

    event = events[-1]
    onset = _parse_iso_utc(event["onset_ts"])
    end = _parse_iso_utc(event["end_ts"])
    baseline_start = onset - timedelta(hours=48)
    now = datetime.now(timezone.utc)
    window_end = min(now, end + timedelta(hours=max_hours))

    bands_out = []
    with get_conn() as conn:
        for band in HANGOVER_BANDS:
            baseline_rate = _hourly_band_rate(conn, band, baseline_start.isoformat(), onset.isoformat())
            current_start = max(end, now - timedelta(hours=3))
            current_rate = _hourly_band_rate(conn, band, current_start.isoformat(), now.isoformat())

            curve = []
            recovered = False
            hours_to_recover = None
            cursor = end
            hour_offset = 0
            while cursor < window_end:
                nxt = min(cursor + timedelta(hours=3), window_end)
                rate = _hourly_band_rate(conn, band, cursor.isoformat(), nxt.isoformat())
                pct = (rate / baseline_rate) if baseline_rate > 0 else None
                curve.append({"hour": hour_offset, "rate": round(rate, 2), "pct": round(pct, 3) if pct is not None else None})
                if not recovered and pct is not None and pct >= recovery_frac:
                    recovered = True
                    hours_to_recover = hour_offset
                cursor = nxt
                hour_offset += 3

            pct_now = (current_rate / baseline_rate) if baseline_rate > 0 else None
            bands_out.append({
                "band": band,
                "baseline_rate": round(baseline_rate, 2),
                "current_rate": round(current_rate, 2),
                "pct_of_baseline": round(pct_now, 3) if pct_now is not None else None,
                "recovered": recovered,
                "hours_to_recover": hours_to_recover,
                "curve": curve,
            })

    return {
        "event": {
            "peak_k": event["peak_k"],
            "onset_ts": event["onset_ts"],
            "end_ts": event["end_ts"],
            "hours_ago": round((now - end).total_seconds() / 3600, 1),
        },
        "recovery_frac": recovery_frac,
        "bands": bands_out,
    }


# ── Personal openness model (P92) ───────────────────────────────────────────────

OPENNESS_BANDS = ("40m", "30m", "20m", "17m", "15m", "12m", "10m")
_SFI_K_MATCH_WINDOW_HOURS = 4  # a spot without a space_weather reading within this window is unconditioned


def _local_hour(ts_iso: str, tz) -> Optional[int]:
    try:
        dt = _parse_iso_utc(ts_iso)
    except (ValueError, TypeError):
        return None
    return dt.astimezone(tz).hour


def get_openness_model(days: int = 60) -> dict:
    """
    Personal band-openness model (P92): "when does band X usually open for
    this station" learned from this station's own WSPR decode history,
    conditioned on current SFI/K.

    Two layers, both derived straight from `spots`/`space_weather` (no
    external MUF/propagation model):

    1. Hourly baseline per band — avg spots/hr for each of the 24 local
       (America/Chicago) hours, over the trailing `days` window, divided by
       that band's `BAND_WEIGHTS` slot share (decoder/wspr.py's smart
       rotation gives 20m 3x the listening time of 15m/12m/10m, so raw spot
       counts alone aren't comparable across bands). The resulting
       weight-normalized rate is then scaled 0-100 against the 90th
       percentile rate across ALL bands/hours in the window, so bands ARE
       comparable to each other: a consistently-strong band like 20m shows
       mostly green, a sharply time-gated band like 40m shows green only at
       its actual peak hours. (Approximate — doesn't correct for night-park
       skipping of 12m/10m, only the static weight table.)

    2. Current-conditions multiplier per band — spots are matched to the
       nearest space_weather reading within _SFI_K_MATCH_WINDOW_HOURS, then
       split into SFI/K tertiles (low/med/high) computed from this same
       window's space_weather history. rate(tertile) / rate(overall) gives
       a multiplier, clipped to [0.3, 3.0] against noise from small
       samples. Today's live SFI/K picks which tertile multiplier applies.
    """
    from zoneinfo import ZoneInfo
    from decoder.wspr import _parse_weights

    band_weights = _parse_weights(os.getenv("BAND_WEIGHTS", ""))
    tz = ZoneInfo("America/Chicago")
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_conn() as conn:
        spot_rows = conn.execute(
            "SELECT timestamp, band, snr, distance_km FROM spots "
            "WHERE timestamp >= ? AND band IN (%s)"
            % ",".join("?" * len(OPENNESS_BANDS)),
            (since_iso, *OPENNESS_BANDS),
        ).fetchall()
        sw_rows = conn.execute(
            "SELECT ts, sfi, kindex FROM space_weather WHERE ts >= ? AND sfi IS NOT NULL AND kindex IS NOT NULL ORDER BY ts ASC",
            (since_iso,),
        ).fetchall()
        latest_sw = conn.execute(
            "SELECT ts, sfi, kindex FROM space_weather WHERE sfi IS NOT NULL AND kindex IS NOT NULL ORDER BY ts DESC LIMIT 1"
        ).fetchone()

    if not spot_rows:
        return {"days": days, "generated_at": datetime.now(timezone.utc).isoformat(), "current": None, "bands": {}}

    # Sample-day count for the window (used to normalize spots -> avg/hr per local hour)
    dates_seen = {_parse_iso_utc(r["timestamp"]).date() for r in spot_rows}
    sample_days = max(1, len(dates_seen))

    # Build a sorted list of (epoch_seconds, sfi, kindex) for nearest-match lookups
    import bisect
    sw_epochs = [_parse_iso_utc(r["ts"]).timestamp() for r in sw_rows]

    def _nearest_sw(ts_iso: str):
        if not sw_epochs:
            return None
        t = _parse_iso_utc(ts_iso).timestamp()
        i = bisect.bisect_left(sw_epochs, t)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(sw_epochs)]
        if not candidates:
            return None
        best = min(candidates, key=lambda j: abs(sw_epochs[j] - t))
        if abs(sw_epochs[best] - t) > _SFI_K_MATCH_WINDOW_HOURS * 3600:
            return None
        return sw_rows[best]

    def _tertiles(values: list) -> tuple:
        s = sorted(values)
        n = len(s)
        if n < 3:
            return (s[0], s[-1]) if s else (0, 0)
        return (s[n // 3], s[2 * n // 3])

    sfi_values = [r["sfi"] for r in sw_rows]
    k_values = [r["kindex"] for r in sw_rows]
    sfi_lo, sfi_hi = _tertiles(sfi_values)
    k_lo, k_hi = _tertiles(k_values)

    def _tier(val: float, lo: float, hi: float, low_label: str, high_label: str, mid_label: str) -> str:
        if val <= lo:
            return low_label
        if val >= hi:
            return high_label
        return mid_label

    current = None
    if latest_sw:
        current = {
            "sfi": latest_sw["sfi"],
            "kindex": latest_sw["kindex"],
            "sfi_tier": _tier(latest_sw["sfi"], sfi_lo, sfi_hi, "low", "high", "mid"),
            "k_tier": _tier(latest_sw["kindex"], k_lo, k_hi, "quiet", "active", "mid"),
            "as_of": latest_sw["ts"],
        }

    # Bucket spots per band into (local_hour) and (sfi_tier, k_tier)
    per_band = {b: {"hour_counts": [0] * 24, "sfi_tier_counts": {"low": 0, "mid": 0, "high": 0},
                     "k_tier_counts": {"quiet": 0, "mid": 0, "active": 0}, "total": 0} for b in OPENNESS_BANDS}

    for r in spot_rows:
        band = r["band"]
        if band not in per_band:
            continue
        h = _local_hour(r["timestamp"], tz)
        if h is not None:
            per_band[band]["hour_counts"][h] += 1
        per_band[band]["total"] += 1
        sw = _nearest_sw(r["timestamp"])
        if sw:
            per_band[band]["sfi_tier_counts"][_tier(sw["sfi"], sfi_lo, sfi_hi, "low", "high", "mid")] += 1
            per_band[band]["k_tier_counts"][_tier(sw["kindex"], k_lo, k_hi, "quiet", "active", "mid")] += 1

    # Weight-normalize every band/hour cell so bands are comparable to each
    # other, not just to their own peak hour (see docstring). Reference is
    # the 90th percentile across the whole grid, not the max, so one freak
    # hour doesn't compress everything else toward the dim end.
    norm_rates = {}
    all_norm_rates = []
    for band, data in per_band.items():
        weight = band_weights.get(band, 1) or 1
        rates = [(data["hour_counts"][h] / sample_days) / weight for h in range(24)]
        norm_rates[band] = rates
        all_norm_rates.extend(rates)
    sorted_rates = sorted(all_norm_rates)
    if sorted_rates:
        idx = min(len(sorted_rates) - 1, max(0, round(0.9 * (len(sorted_rates) - 1))))
        global_ref = sorted_rates[idx] or (sorted_rates[-1] or 1)
    else:
        global_ref = 1

    bands_out = {}
    for band, data in per_band.items():
        hour_counts = data["hour_counts"]
        hours = []
        for h in range(24):
            avg_per_hr = hour_counts[h] / sample_days
            score = round(min(100.0, 100 * norm_rates[band][h] / global_ref), 1)
            hours.append({"hour": h, "avg_spots_per_hr": round(avg_per_hr, 2), "score": score})

        total = data["total"] or 1
        sfi_mult = 1.0
        k_mult = 1.0
        if current:
            sfi_share = data["sfi_tier_counts"][current["sfi_tier"]] / total
            sfi_mult = min(3.0, max(0.3, sfi_share * 3)) if sfi_share > 0 else 0.3
            k_share = data["k_tier_counts"][current["k_tier"]] / total
            k_mult = min(3.0, max(0.3, k_share * 3)) if k_share > 0 else 0.3
        combined_mult = round((sfi_mult + k_mult) / 2, 2)

        adj_hours = []
        for h_entry in hours:
            adj_score = round(min(100.0, h_entry["score"] * combined_mult), 1)
            adj_hours.append({**h_entry, "adj_score": adj_score})

        best = sorted(adj_hours, key=lambda x: x["adj_score"], reverse=True)[:4]
        best_hours = sorted([b["hour"] for b in best if b["adj_score"] > 0])

        bands_out[band] = {
            "hours": adj_hours,
            "best_hours": best_hours,
            "current_multiplier": combined_mult,
            "sample_days": sample_days,
            "sample_spots": data["total"],
        }

    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": "America/Chicago",
        "current_local_hour": datetime.now(tz).hour,
        "current": current,
        "bands": bands_out,
    }


# ── Balloon / airborne watch (P97) ─────────────────────────────────────────────


def get_spots_for_call(call: str, days: Optional[int] = None, limit: int = 5000) -> list:
    """All spots for one callsign (newest first optional — we return oldest→newest)."""
    with get_conn() as conn:
        if days is not None:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            rows = conn.execute(
                """
                SELECT * FROM spots
                WHERE call = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (call, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM spots
                WHERE call = ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (call, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def get_telemetry_spots(days: int = 45, limit: int = 20000) -> list:
    """
    All 0*/Q*/1* 6-char-callsign spots in the window, flat (not grouped by
    call — a U4B/Traquito telemetry callsign changes with every altitude
    update, so grouping by literal call fragments a flight's history).
    Used to build P101 channel tracks (grouped by id13 instead).
    """
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM spots
            WHERE timestamp >= ?
              AND length(call) = 6
              AND (call GLOB '0*' OR call GLOB 'Q*' OR call GLOB '1*')
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_spots_grouped_by_call(
    days: int = 45,
    min_spots: int = 2,
    limit_calls: int = 800,
) -> dict:
    """
    Spot lists keyed by call for balloon classification.

    Prefetches multi-spot / multi-grid / telemetry-looking calls to keep
    the classifier cheap on ~60k-spot databases.
    """
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        # Candidate calls: enough spots, or telemetry prefix, or seed-like multi-grid
        cand_rows = conn.execute(
            """
            SELECT call,
                   COUNT(*) AS n,
                   COUNT(DISTINCT substr(grid,1,4)) AS grids
            FROM spots
            WHERE timestamp >= ?
            GROUP BY call
            HAVING n >= ?
                OR call GLOB '0*'
                OR call GLOB 'Q*'
                OR call GLOB '1*'
                OR grids >= 3
            ORDER BY grids DESC, n DESC
            LIMIT ?
            """,
            (since, min_spots, limit_calls),
        ).fetchall()
        calls = [r["call"] for r in cand_rows]
        if not calls:
            return {}

        # Pull spots for those calls in one query
        placeholders = ",".join("?" * len(calls))
        spot_rows = conn.execute(
            f"""
            SELECT * FROM spots
            WHERE timestamp >= ? AND call IN ({placeholders})
            ORDER BY call ASC, timestamp ASC
            """,
            [since, *calls],
        ).fetchall()

    grouped: dict = {c: [] for c in calls}
    for r in spot_rows:
        d = dict(r)
        grouped.setdefault(d["call"], []).append(d)
    return grouped


def get_balloon_flags() -> dict:
    """Return {CALL: {status, note, updated_at}} for manual overrides."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT call, status, note, updated_at FROM balloon_flags"
        ).fetchall()
    return {
        r["call"].upper(): {
            "status": r["status"],
            "note": r["note"] or "",
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


def set_balloon_flag(call: str, status: str, note: str = "") -> dict:
    """Upsert a balloon watch flag. status: auto|watch|confirmed|dismissed."""
    call_u = call.strip().upper()
    status = (status or "auto").strip().lower()
    if status not in ("auto", "watch", "confirmed", "dismissed"):
        raise ValueError(f"invalid balloon flag status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if status == "auto" and not note:
            conn.execute("DELETE FROM balloon_flags WHERE call = ?", (call_u,))
        else:
            conn.execute(
                """
                INSERT INTO balloon_flags (call, status, note, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(call) DO UPDATE SET
                    status=excluded.status,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                (call_u, status, note or "", now),
            )
    return {"call": call_u, "status": status, "note": note or "", "updated_at": now}


def get_recent_balloon_spot_calls(hours: int = 48) -> set:
    """
    Quick set of callsigns that currently classify as balloon/telem suspects.

    Used by the live feed highlighter; recomputed on demand (cheap enough).
    """
    # Lazy import to avoid circular import at module load
    from decoder.balloons import classify_all, clean_call

    grouped = get_spots_grouped_by_call(days=max(7, hours // 24 + 7), min_spots=2)
    flags = get_balloon_flags()
    suspects = classify_all(grouped, flags=flags, include_telemetry=True, min_score=48)
    return {clean_call(s["call"]) for s in suspects}
