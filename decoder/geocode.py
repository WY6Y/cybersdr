"""
geocode.py — reverse-geocode WSPR spots' grid squares to a country/region name.

Runs as its own background poller thread rather than inline in the WSPR
decode path, so a slow or rate-limited lookup can never delay the next
capture (which must start on the next even UTC minute). Uses OpenStreetMap
Nominatim (free, no API key); results are cached per grid square in SQLite
so a repeat spotter or common DX path is only looked up once, keeping
request volume well under Nominatim's usage policy (max 1 req/s, descriptive
User-Agent required).
"""
import logging
import os
import threading
import time

import requests

import db
from decoder.grid import grid_to_latlon

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_contact = os.getenv("NOMINATIM_CONTACT", "")
USER_AGENT = f"CyberSDR-WY6Y/1.0 (WSPR dashboard{'; contact: ' + _contact if _contact else ''})"
MIN_REQUEST_INTERVAL_S = 1.1  # stay under Nominatim's 1 req/s policy
POLL_INTERVAL_S = 15


def _lookup(grid: str) -> str:
    """One live Nominatim call for a grid square. Returns a country name, or "" for no match (open ocean)."""
    lat, lon = grid_to_latlon(grid)
    resp = requests.get(
        NOMINATIM_URL,
        params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 3, "accept-language": "en"},
        headers={"User-Agent": USER_AGENT},
        timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("address", {}).get("country") or data.get("name") or ""


class GeocodePoller:
    """Daemon thread that backfills `spots.country` for newly-decoded grids."""

    def __init__(self):
        self._stop = threading.Event()
        self._last_request = 0.0

    def start(self) -> None:
        threading.Thread(target=self.run, name="geocode-poller", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info("[GeocodePoller] Started")
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.warning("[GeocodePoller] Poll error", exc_info=True)
            self._stop.wait(POLL_INTERVAL_S)

    def _poll_once(self) -> None:
        for grid in db.get_ungeocoded_grids():
            cached, country = db.get_cached_country(grid)
            if not cached:
                wait = MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
                try:
                    country = _lookup(grid)
                except Exception as exc:
                    logger.warning("[GeocodePoller] Lookup failed for grid %s: %s", grid, exc)
                    continue  # leave uncached — retry next poll
                finally:
                    self._last_request = time.monotonic()
                db.cache_country(grid, country)
            db.backfill_country(grid, country)
