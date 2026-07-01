"""
wspr.py — WSPRDecoder daemon thread.

State machine:
    IDLE → WAITING (sync to even UTC minute) → RECORDING (rtl_fm 120 s)
         → DECODING (wsprd) → UPLOADING → IDLE → ...

If paused=True the loop sleeps without touching the RTL-SDR so SDR++
or any other app can grab the device.
"""
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import db
from decoder.grid import distance_km, bearing
from decoder.upload import upload_to_wsprnet
from decoder.capture import capture_wspr

logger = logging.getLogger(__name__)

# WSPR dial frequencies (MHz) — USB mode, signal centred ~1500 Hz above dial
BANDS = [
    {"name": "40m", "dial": 7.0386},
    {"name": "30m", "dial": 10.1387},
    {"name": "20m", "dial": 14.0956},
    {"name": "17m", "dial": 18.1046},
    {"name": "15m", "dial": 21.0946},
    {"name": "10m", "dial": 28.1246},
]

WAV_PATH = "/tmp/wspr_capture.wav"  # must match decoder/capture.py


class WSPRDecoder:
    """
    Runs as a daemon thread.  Call run() from a Thread; use pause()/resume()
    from the Flask request threads.  sse_push is a callable(event, data_dict)
    set by app.py after construction.
    """

    def __init__(
        self,
        rtl_host: str,
        rtl_port: int,
        my_call: str,
        my_grid: str,
        sse_push: Optional[Callable] = None,
    ):
        self.rtl_host = rtl_host
        self.rtl_port = rtl_port
        self.my_call = my_call
        self.my_grid = my_grid
        self.sse_push = sse_push

        self.state: str = "IDLE"
        self.paused: bool = False
        self._band_idx: int = 0
        self._gain_tenths: int = int(os.getenv("RTL_GAIN", "20")) * 10  # convert to tenths-of-dB
        self._do_upload: bool = os.getenv("WSPRNET_UPLOAD", "true").lower() == "true"

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._capture_active: bool = False

    # ── public interface ──────────────────────────────────────────────────────

    @property
    def current_band(self) -> str:
        return BANDS[self._band_idx]["name"]

    def get_status(self) -> dict:
        band = BANDS[self._band_idx]
        return {
            "state": self.state,
            "paused": self.paused,
            "current_band": band["name"],
            "dial_freq": band["dial"],
            "next_decode_utc": self._next_even_minute_iso(),
        }

    def pause(self) -> None:
        """Signal the capture loop to stop at the next opportunity."""
        with self._lock:
            self.paused = True
        self._set_state("PAUSED")
        logger.info("[WSPRDecoder] Paused — RTL-SDR released")

    def resume(self) -> None:
        """Re-enter the decode loop."""
        with self._lock:
            self.paused = False
        self._set_state("IDLE")
        logger.info("[WSPRDecoder] Resumed")

    def run(self) -> None:
        """Main loop — call from a daemon thread."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        logger.info("[WSPRDecoder] Started  call=%s  grid=%s", self.my_call, self.my_grid)

        while not self._stop.is_set():
            if self.paused:
                time.sleep(2)
                continue

            # ── 1. Wait for the next even UTC minute ──────────────────────────
            self._set_state("WAITING")
            if not self._wait_even_minute():
                continue  # paused or stopped while waiting

            if self.paused or self._stop.is_set():
                continue

            # ── 2. Record 120 s ───────────────────────────────────────────────
            band = BANDS[self._band_idx]
            logger.info(
                "[WSPRDecoder] RECORDING  band=%s  dial=%.4f MHz",
                band["name"], band["dial"],
            )
            self._set_state("RECORDING")
            ok = self._record(band)

            if self.paused or self._stop.is_set():
                continue

            if ok:
                # ── 3. Decode ─────────────────────────────────────────────────
                self._set_state("DECODING")
                spots = self._decode(band)

                if spots:
                    # ── 4. Store + upload ─────────────────────────────────────
                    self._set_state("UPLOADING")
                    for spot in spots:
                        db.insert_spot(spot)
                        if self.sse_push:
                            self.sse_push("spot", spot)
                    logger.info(
                        "[WSPRDecoder] %d spot(s) on %s", len(spots), band["name"]
                    )
                    if self._do_upload:
                        upload_to_wsprnet(spots, self.my_call, self.my_grid)
                    if self.sse_push:
                        self.sse_push("stats", db.get_today_stats(self.my_grid))
                else:
                    logger.info("[WSPRDecoder] No spots on %s", band["name"])
            else:
                logger.warning("[WSPRDecoder] Capture failed on %s", band["name"])

            # Rotate to next band
            self._band_idx = (self._band_idx + 1) % len(BANDS)
            self._set_state("IDLE")

    # ── private helpers ───────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        self.state = state
        logger.info("[WSPRDecoder] → %s  band=%s", state, self.current_band)
        if self.sse_push:
            try:
                self.sse_push("status", self.get_status())
            except Exception:
                pass

    def _wait_even_minute(self) -> bool:
        """Block until second 0–1 of an even UTC minute.  Returns False if interrupted."""
        while not self._stop.is_set() and not self.paused:
            now = datetime.now(timezone.utc)
            if now.minute % 2 == 0 and now.second < 2:
                return True
            time.sleep(0.4)
        return False

    def _next_even_minute_iso(self) -> str:
        now = datetime.now(timezone.utc)
        minute = now.minute
        second = now.second
        # If we're already in second 0-1 of an even minute, that IS the next slot
        if minute % 2 == 0 and second < 2:
            candidate = now.replace(second=0, microsecond=0)
        else:
            steps = (minute // 2 + 1) * 2
            if steps >= 60:
                candidate = (now + timedelta(hours=1)).replace(
                    minute=steps - 60, second=0, microsecond=0
                )
            else:
                candidate = now.replace(minute=steps, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(minutes=2)
        return candidate.isoformat()

    def _record(self, band: dict) -> bool:
        """
        Capture 120 s of IQ from rtl_tcp, USB-demodulate in Python, write WAV.
        Returns True on success.
        """
        freq_hz = int(band["dial"] * 1e6)
        with self._lock:
            self._capture_active = True
        try:
            ok = capture_wspr(
                host=self.rtl_host,
                port=self.rtl_port,
                freq_hz=freq_hz,
                duration_s=120,
                gain_tenths=self._gain_tenths,
            )
        except Exception as exc:
            logger.error("[WSPRDecoder] Capture error: %s", exc)
            ok = False
        finally:
            with self._lock:
                self._capture_active = False
        return ok

    def _decode(self, band: dict) -> list:
        """
        Run wsprd on the captured WAV and return a list of spot dicts.
        wsprd output line format:
            YYMMDD HHMM  SNR  DRIFT  FREQ  CALL  GRID  POWER  [extra]
        Example:
            200630 0400  -12   0.2  14.097094  W4ABC  EM72   5   0
        """
        try:
            result = subprocess.run(
                ["wsprd", "-f", str(band["dial"]), WAV_PATH],
                capture_output=True,
                text=True,
                timeout=90,
            )
        except FileNotFoundError:
            logger.error("[WSPRDecoder] wsprd not found — install wsjtx or wsjt-x")
            return []
        except subprocess.TimeoutExpired:
            logger.error("[WSPRDecoder] wsprd timed out")
            return []
        except Exception as exc:
            logger.error("[WSPRDecoder] Decode subprocess error: %s", exc)
            return []

        if result.returncode not in (0, 1):
            logger.warning(
                "[WSPRDecoder] wsprd exit %d stderr=%s",
                result.returncode, result.stderr[:200],
            )

        spots = []
        for line in result.stdout.splitlines():
            spot = self._parse_line(line, band)
            if spot:
                spots.append(spot)
        return spots

    def _parse_line(self, line: str, band: dict) -> Optional[dict]:
        """Parse one wsprd output line; return dict or None."""
        parts = line.split()
        # Need at least: date time snr drift freq call grid power
        if len(parts) < 8:
            return None
        # Skip header / separator lines (date field must start with a digit)
        if not parts[0][0].isdigit():
            return None
        try:
            ts_str = parts[0] + parts[1]  # YYMMDD + HHMM
            snr = float(parts[2])
            drift = float(parts[3])
            freq = float(parts[4])
            call = parts[5]
            grid = parts[6]
            power = int(parts[7])

            ts = datetime.strptime(ts_str, "%y%m%d%H%M").replace(tzinfo=timezone.utc)

            dist = None
            brng = None
            if len(grid) >= 4:
                try:
                    dist = distance_km(self.my_grid, grid)
                    brng = bearing(self.my_grid, grid)
                except Exception:
                    pass

            return {
                "timestamp": ts.isoformat(),
                "call": call,
                "freq": freq,
                "band": band["name"],
                "snr": snr,
                "drift": drift,
                "grid": grid,
                "power": power,
                "distance_km": dist,
                "bearing": brng,
            }
        except (ValueError, IndexError):
            return None
