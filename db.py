"""
db.py — SQLite helpers for CyberSDR spot storage.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

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
    bearing     REAL
);
CREATE INDEX IF NOT EXISTS idx_spots_timestamp ON spots(timestamp);
CREATE INDEX IF NOT EXISTS idx_spots_band      ON spots(band);
CREATE INDEX IF NOT EXISTS idx_spots_call      ON spots(call);
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


def insert_spot(spot: dict):
    """Persist one decoded spot row."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO spots
                (timestamp, call, freq, band, snr, drift, grid, power, distance_km, bearing)
            VALUES
                (:timestamp, :call, :freq, :band, :snr, :drift, :grid, :power,
                 :distance_km, :bearing)
            """,
            spot,
        )


def get_recent_spots(n: int = 200) -> list:
    """Return the most recent *n* spots, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM spots ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_band_summary() -> list:
    """Return per-band counts and best SNR for today."""
    today = date.today().isoformat()
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
    today = date.today().isoformat()
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


def get_band_conditions(my_grid: str, hours: int = 2) -> list:
    """
    Return per-band openness stats for the last *hours* hours.

    Each dict in the returned list contains:
        band, spot_count, avg_snr, max_distance_km, unique_calls,
        score (0-100), condition (DARK/WEAK/FAIR/OPEN/STRONG), color (hex)
    """
    BANDS = ["40m", "30m", "20m", "17m", "15m", "10m"]
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
