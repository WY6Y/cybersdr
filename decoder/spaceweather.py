"""
spaceweather.py — Space weather poller for CyberSDR.

Data sources (all free, no API key):
  SFI      — NOAA SWPC 10cm-flux summary  (updated every 3 h)
  Kp / Ap  — NOAA SWPC planetary K-index  (updated every 3 h)
  SN       — SILSO monthly sunspot number  (updated monthly)

HF band forecast is derived from SFI + Kp using standard
propagation rules rather than a third-party service.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import db

logger = logging.getLogger(__name__)

# ── Data endpoints ────────────────────────────────────────────────────────────
SFI_URL    = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
KINDEX_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SN_URL     = "https://www.sidc.be/SILSO/INFO/snmtotcsv.php"

POLL_INDICES_S = 3 * 3600   # 3 hours — SFI and Kp update every 3 h
POLL_KHISTORY_S = 30 * 60   # 30 min — keep history current for the chart


def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _band_forecast(sfi: float, kp: float) -> dict:
    """
    Derive HF band conditions from SFI and Kp.
    Returns dict keyed by band-group with day/night condition strings.
    """
    def rate(score: float) -> str:
        if score >= 3.5: return "Excellent"
        if score >= 2.5: return "Good"
        if score >= 1.5: return "Fair"
        return "Poor"

    # Geomagnetic penalty: 0=quiet → 3=major storm
    kpen = 0 if kp < 2 else (0.5 if kp < 3 else (1.5 if kp < 4 else min(3.0, kp - 2.0)))

    # 80m–40m: generally reliable; better at night; K disturbance hurts
    lf_day   = max(0, 3.0 - kpen * 0.7)
    lf_night = max(0, 4.0 - kpen)

    # 30m–20m: moderate-SFI daytime bands
    mid_bonus = min(1.5, max(0, (sfi - 90) / 50))
    mid_day   = max(0, 2.0 + mid_bonus - kpen)
    mid_night = max(0, 1.0 + mid_bonus - kpen)

    # 17m–15m: higher-SFI dependent
    hi_bonus = min(1.5, max(0, (sfi - 120) / 30))
    hi_day   = max(0, 1.0 + hi_bonus - kpen)
    hi_night = max(0, hi_bonus - kpen)

    # 12m–10m: high-SFI only; closes at night
    vhi_bonus = min(2.0, max(0, (sfi - 150) / 25))
    vhi_day   = max(0, vhi_bonus - kpen * 0.5)
    vhi_night = max(0, vhi_bonus - 1.5 - kpen * 0.5)

    return {
        "80m-40m_day":   rate(lf_day),   "80m-40m_night":  rate(lf_night),
        "30m-20m_day":   rate(mid_day),  "30m-20m_night":  rate(mid_night),
        "17m-15m_day":   rate(hi_day),   "17m-15m_night":  rate(hi_night),
        "12m-10m_day":   rate(vhi_day),  "12m-10m_night":  rate(vhi_night),
    }


class SpaceWeatherPoller:
    """
    Daemon threads keeping solar indices in memory and SQLite.
    Call start() once from app.py after init_db().
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._current: dict = {}
        self._khistory: list = []

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._fetch_indices()
        self._fetch_khistory()
        threading.Thread(target=self._indices_loop,  daemon=True, name="sw-indices").start()
        threading.Thread(target=self._khistory_loop, daemon=True, name="sw-khistory").start()
        logger.info("[SpaceWx] Poller started — SFI=%s K=%s",
                    self._current.get("sfi", "?"), self._current.get("kindex", "?"))

    def get_current(self) -> dict:
        with self._lock:
            return dict(self._current)

    def get_kindex_history(self) -> list:
        with self._lock:
            return list(self._khistory)

    # ── background loops ─────────────────────────────────────────────────────

    def _indices_loop(self):
        while True:
            time.sleep(POLL_INDICES_S)
            self._fetch_indices()

    def _khistory_loop(self):
        while True:
            time.sleep(POLL_KHISTORY_S)
            self._fetch_khistory()

    # ── fetch helpers ─────────────────────────────────────────────────────────

    def _fetch_indices(self) -> None:
        try:
            sfi_resp = requests.get(SFI_URL,    timeout=15)
            kp_resp  = requests.get(KINDEX_URL, timeout=15)
            sfi_resp.raise_for_status()
            kp_resp.raise_for_status()

            # SFI: [{"flux": 139, "time_tag": "..."}]
            sfi_data  = sfi_resp.json()
            sfi_val   = float(sfi_data[-1]["flux"]) if sfi_data else None
            sfi_time  = sfi_data[-1].get("time_tag", "") if sfi_data else ""

            # Kp + Ap: [{"time_tag":..., "Kp":..., "a_running":...}, ...]
            kp_data   = kp_resp.json()
            last_kp   = kp_data[-1] if kp_data else {}
            kp_val    = _safe_float(last_kp.get("Kp"))
            ap_val    = _safe_float(last_kp.get("a_running"))

            # K-storm text label
            kp_n = kp_val or 0
            if kp_n < 2:   ktext = "No Storm"
            elif kp_n < 3: ktext = "Unsettled"
            elif kp_n < 4: ktext = "Active"
            elif kp_n < 5: ktext = "Minor Storm (G1)"
            elif kp_n < 6: ktext = "Moderate Storm (G2)"
            elif kp_n < 7: ktext = "Strong Storm (G3)"
            elif kp_n < 8: ktext = "Severe Storm (G4)"
            else:           ktext = "Extreme Storm (G5)"

            # Sunspot number from SILSO (monthly; get last entry)
            sn_val = self._current.get("sn")   # keep cached value unless we fetch fresh
            try:
                sn_resp = requests.get(SN_URL, timeout=10, allow_redirects=True)
                sn_resp.raise_for_status()
                last_line = [l for l in sn_resp.text.strip().splitlines() if l.strip()][-1]
                parts = last_line.split(";")
                sn_val = round(float(parts[3].strip())) if len(parts) >= 4 else sn_val
            except Exception:
                pass   # keep previous value

            bands  = _band_forecast(sfi_val or 100, kp_n)
            now_ts = datetime.now(timezone.utc)

            data = {
                "sfi":         str(int(sfi_val)) if sfi_val is not None else "?",
                "sn":          str(sn_val) if sn_val is not None else "?",
                "aindex":      str(int(ap_val)) if ap_val is not None else "?",
                "kindex":      f"{kp_val:.1f}" if kp_val is not None else "?",
                "kindex_text": ktext,
                "xray":        "—",
                "solar_wind":  "—",
                "mag_field":   "—",
                "updated":     sfi_time[:16].replace("T", " ") + " UTC" if sfi_time else "?",
                "bands":       bands,
                "fetched_at":  now_ts.isoformat(),
            }

            with self._lock:
                self._current = data

            try:
                db.insert_space_weather(data)
            except Exception as exc:
                logger.warning("[SpaceWx] DB write error: %s", exc)

            logger.info("[SpaceWx] Updated — SFI=%s SN=%s A=%s K=%s (%s)",
                        data["sfi"], data["sn"], data["aindex"],
                        data["kindex"], data["kindex_text"])

        except Exception as exc:
            logger.warning("[SpaceWx] indices fetch error: %s", exc)

    def _fetch_khistory(self) -> None:
        try:
            resp = requests.get(KINDEX_URL, timeout=15)
            resp.raise_for_status()
            rows = resp.json()
            history = []
            for row in rows:
                try:
                    history.append({"time": row["time_tag"], "kp": float(row["Kp"])})
                except (KeyError, ValueError, TypeError):
                    continue
            with self._lock:
                self._khistory = history[-672:]
            logger.info("[SpaceWx] Kp history: %d readings", len(history))
        except Exception as exc:
            logger.warning("[SpaceWx] Kp history fetch error: %s", exc)
