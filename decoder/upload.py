"""
upload.py — Upload decoded WSPR spots to WSPRnet.org.
"""
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

WSPRNET_URL = "http://wsprnet.org/post/"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "CyberSDR/1.0"})


def upload_to_wsprnet(spots: list, my_call: str, my_grid: str) -> None:
    """
    POST each spot to WSPRnet.  Never raises — failures are logged and skipped.

    WSPRnet expects a GET (despite being called 'post') with query params.
    """
    for spot in spots:
        try:
            params = {
                "function": "wspr",
                "rcall": my_call,
                "rgrid": my_grid,
                "rqrg": f"{spot['freq']:.6f}",
                "date": _fmt_date(spot["timestamp"]),
                "time": _fmt_time(spot["timestamp"]),
                "sig": str(int(spot["snr"])),
                "dt": f"{spot['drift']:.1f}",
                "tqrg": f"{spot['freq']:.6f}",
                "tcall": spot["call"],
                "tgrid": spot["grid"],
                "dbm": str(spot["power"]),
                "version": "CyberSDR-1.0",
                "mode": "2",
            }
            resp = _SESSION.get(WSPRNET_URL, params=params, timeout=10)
            if resp.status_code == 200:
                logger.info("[Upload] %s → WSPRnet OK", spot["call"])
            else:
                logger.warning(
                    "[Upload] WSPRnet HTTP %s for %s", resp.status_code, spot["call"]
                )
        except requests.RequestException as exc:
            logger.warning("[Upload] Network error uploading %s: %s", spot.get("call", "?"), exc)
        except Exception as exc:
            logger.error("[Upload] Unexpected error for %s: %s", spot.get("call", "?"), exc)


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_date(iso_ts: str) -> str:
    """Return YYMMDD from an ISO-8601 timestamp string."""
    return _parse(iso_ts).strftime("%y%m%d")


def _fmt_time(iso_ts: str) -> str:
    """Return HHMM from an ISO-8601 timestamp string."""
    return _parse(iso_ts).strftime("%H%M")


def _parse(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
