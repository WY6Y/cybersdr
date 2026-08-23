"""
wspr.py — WSPRDecoder daemon thread.

State machine:
    IDLE → WAITING (sync to even UTC minute) → RECORDING (rtl_tcp 120 s)
         → DECODING (wsprd) → UPLOADING → IDLE → ...

If paused=True the loop sleeps without touching the RTL-SDR so SDR++
or any other app can grab the device.

Smart rotation (default on):
  - Weighted queue: spend more slots on historically productive bands
  - Skip-if-dark: park a band after N consecutive empty captures
  - Night soft-skip: park 12m/10m during local night (approx. from MY_GRID)
"""
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import db
from decoder.grid import distance_km, bearing, grid_to_latlon
from decoder.upload import upload_to_wsprnet
from decoder.capture import capture_wspr

logger = logging.getLogger(__name__)

# WSPR dial frequencies (MHz) — USB mode, signal centred ~1500 Hz above dial
# 80m dropped: antenna (133' EFHW) and noise floor make it a wasted 2-min slot
BANDS = [
    {"name": "40m", "dial": 7.0386},
    {"name": "30m", "dial": 10.1387},
    {"name": "20m", "dial": 14.0956},
    {"name": "17m", "dial": 18.1046},
    {"name": "15m", "dial": 21.0946},
    {"name": "12m", "dial": 24.9246},
    {"name": "10m", "dial": 28.1246},
]

# Relative slots per full weighted cycle (20m gets the most airtime)
DEFAULT_WEIGHTS = {
    "40m": 2,
    "30m": 2,
    "20m": 3,
    "17m": 2,
    "15m": 1,
    "12m": 1,
    "10m": 1,
}

# High bands: auto-parked during local night unless recently productive
NIGHT_PARK_BANDS = frozenset({"12m", "10m"})

WAV_PATH = "/tmp/wspr_capture.wav"  # must match decoder/capture.py


def _parse_weights(raw: str) -> dict:
    """Parse BAND_WEIGHTS="40m:2,20m:3" into {"40m": 2, "20m": 3}."""
    if not raw or not raw.strip():
        return dict(DEFAULT_WEIGHTS)
    out = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        band, w = pair.split(":", 1)
        try:
            weight = int(w.strip())
            if weight > 0:
                out[band.strip()] = weight
        except ValueError:
            logger.warning("[WSPRDecoder] Bad BAND_WEIGHTS entry: %r", pair)
    return out if out else dict(DEFAULT_WEIGHTS)


class SmartScheduler:
    """
    Weighted band rotation with empty-streak parking and night soft-skip.

    Queue is an expanded list of band indices (into BANDS) according to weights.
    advance() walks the queue and skips bands currently parked.
    """

    def __init__(self, my_grid: str):
        self.my_grid = my_grid
        self.enabled = os.getenv("SMART_ROTATION", "true").lower() in ("1", "true", "yes")
        self.weights = _parse_weights(os.getenv("BAND_WEIGHTS", ""))
        self.skip_empty = max(1, int(os.getenv("BAND_SKIP_EMPTY", "3")))
        self.park_minutes = max(5, int(os.getenv("BAND_PARK_MINUTES", "90")))
        self.night_park = os.getenv("NIGHT_PARK_HIGH", "true").lower() in ("1", "true", "yes")

        self._empty_streak: dict[str, int] = {b["name"]: 0 for b in BANDS}
        self._parked_until: dict[str, datetime] = {}
        self._queue: list[int] = []
        self._pos: int = 0
        self._rebuild_queue()

        # Approximate local longitude for solar-time night estimate
        try:
            _, lon = grid_to_latlon(my_grid)
            self._lon = lon
        except Exception:
            self._lon = -97.0  # rough mid-US fallback

        logger.info(
            "[SmartScheduler] enabled=%s weights=%s skip_empty=%d park=%dm night_park=%s",
            self.enabled, self.weights, self.skip_empty, self.park_minutes, self.night_park,
        )

    def _rebuild_queue(self) -> None:
        q = []
        for i, b in enumerate(BANDS):
            w = self.weights.get(b["name"], 1)
            q.extend([i] * max(1, w))
        self._queue = q or list(range(len(BANDS)))
        self._pos = self._pos % len(self._queue)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _is_local_night(self) -> bool:
        """Rough local solar night: hour 20–07 at QTH longitude."""
        utc = self._now()
        local_h = (utc.hour + utc.minute / 60.0 + self._lon / 15.0) % 24
        return local_h >= 20.0 or local_h < 7.0

    def _expire_parks(self) -> None:
        now = self._now()
        expired = [b for b, until in self._parked_until.items() if until <= now]
        for b in expired:
            del self._parked_until[b]
            self._empty_streak[b] = 0
            logger.info("[SmartScheduler] Unparked %s (timer expired)", b)

    def is_parked(self, band_name: str) -> bool:
        self._expire_parks()
        if band_name in self._parked_until:
            return True
        # At night, skip 12m/10m after one empty pass (still try once at dusk)
        if (
            self.night_park
            and band_name in NIGHT_PARK_BANDS
            and self._is_local_night()
            and self._empty_streak.get(band_name, 0) >= 1
        ):
            return True
        return False

    def current_band_idx(self) -> int:
        if not self.enabled:
            return self._pos % len(BANDS)
        return self._queue[self._pos % len(self._queue)]

    def current_band(self) -> dict:
        return BANDS[self.current_band_idx()]

    def record_result(self, band_name: str, spot_count: int) -> None:
        if not self.enabled:
            return
        if spot_count > 0:
            self._empty_streak[band_name] = 0
            if band_name in self._parked_until:
                del self._parked_until[band_name]
                logger.info("[SmartScheduler] %s unparked after %d spot(s)", band_name, spot_count)
            return

        self._empty_streak[band_name] = self._empty_streak.get(band_name, 0) + 1
        streak = self._empty_streak[band_name]
        if streak >= self.skip_empty and band_name not in self._parked_until:
            until = self._now() + timedelta(minutes=self.park_minutes)
            self._parked_until[band_name] = until
            logger.info(
                "[SmartScheduler] Parking %s for %d min after %d empty captures (until %s)",
                band_name, self.park_minutes, streak, until.strftime("%H:%MZ"),
            )

    def record_failure(self, band_name: str) -> None:
        """Record an RF capture failure without treating it as propagation silence."""
        if not self.enabled:
            return
        logger.info("[SmartScheduler] Capture failure on %s — not updating empty streak", band_name)

    def advance(self) -> None:
        """Move to the next usable band in the (weighted) queue."""
        if not self.enabled:
            self._pos = (self._pos + 1) % len(BANDS)
            return

        n = len(self._queue)
        for _ in range(n):
            self._pos = (self._pos + 1) % n
            idx = self._queue[self._pos]
            name = BANDS[idx]["name"]
            if not self.is_parked(name):
                return
        # All parked — fall through to whatever is next; still try something
        self._pos = (self._pos + 1) % n
        logger.warning("[SmartScheduler] All bands parked — forcing next slot anyway")

    def status_snapshot(self) -> dict:
        self._expire_parks()
        parked = {
            b: until.isoformat()
            for b, until in self._parked_until.items()
        }
        return {
            "smart_rotation": self.enabled,
            "weights": self.weights,
            "empty_streak": dict(self._empty_streak),
            "parked_until": parked,
            "local_night": self._is_local_night(),
            "queue_len": len(self._queue) if self.enabled else len(BANDS),
        }


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
        self._default_gain_tenths: int = int(os.getenv("RTL_GAIN", "20")) * 10
        self._gain_map: dict = self._parse_gain_bands(os.getenv("RTL_GAIN_BANDS", ""))
        self._do_upload: bool = os.getenv("WSPRNET_UPLOAD", "true").lower() == "true"
        self._scheduler = SmartScheduler(my_grid)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._capture_active: bool = False

    # ── public interface ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_gain_bands(raw: str) -> dict:
        """Parse RTL_GAIN_BANDS="40m:20,20m:25,10m:32" into tenths-of-dB map."""
        gain_map = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            band, gain = pair.split(":", 1)
            try:
                gain_map[band.strip()] = int(float(gain.strip()) * 10)
            except ValueError:
                logger.warning("[WSPRDecoder] Bad RTL_GAIN_BANDS entry: %r", pair)
        return gain_map

    def _gain_for_band(self, band_name: str) -> int:
        return self._gain_map.get(band_name, self._default_gain_tenths)

    @property
    def current_band(self) -> str:
        return self._scheduler.current_band()["name"]

    def get_status(self) -> dict:
        band = self._scheduler.current_band()
        status = {
            "state": self.state,
            "paused": self.paused,
            "current_band": band["name"],
            "dial_freq": band["dial"],
            "next_decode_utc": self._next_even_minute_iso(),
        }
        status.update(self._scheduler.status_snapshot())
        return status

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
            band = self._scheduler.current_band()
            capture_start = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            logger.info(
                "[WSPRDecoder] RECORDING  band=%s  dial=%.4f MHz",
                band["name"], band["dial"],
            )
            self._set_state("RECORDING")
            ok = self._record(band)

            if self.paused or self._stop.is_set():
                continue

            spot_count = 0
            if ok:
                # ── 3. Decode ─────────────────────────────────────────────────
                self._set_state("DECODING")
                spots = self._decode(band, capture_start)
                spot_count = len(spots)

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

            if ok:
                self._scheduler.record_result(band["name"], spot_count)
            else:
                self._scheduler.record_failure(band["name"])
            self._scheduler.advance()
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
                gain_tenths=self._gain_for_band(band["name"]),
            )
        except Exception as exc:
            logger.error("[WSPRDecoder] Capture error: %s", exc)
            ok = False
        finally:
            with self._lock:
                self._capture_active = False
        return ok

    def _decode(self, band: dict, capture_start: datetime) -> list:
        """
        Run wsprd on the captured WAV and return a list of spot dicts.
        wsprd output line format:
            YYMMDD HHMM  SNR  DRIFT  FREQ  CALL  GRID  POWER  [extra]
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

        logger.info("[wsprd] exit=%d stdout=%r stderr=%r",
                    result.returncode,
                    result.stdout[:500],
                    result.stderr[:300])

        spots = []
        for line in result.stdout.splitlines():
            spot = self._parse_line(line, band, capture_start)
            if spot:
                spots.append(spot)
        return spots

    def _parse_line(self, line: str, band: dict, capture_start: datetime) -> Optional[dict]:
        """Parse one wsprd output line; return dict or None.

        Two output formats observed from Debian wsjtx's wsprd:
          With date:    YYMMDD HHMM  SNR  dt  freq  drift  call  grid  power
          Without date: ????   SNR  dt  freq  drift  call  grid  power

        wsprd only emits a real YYMMDD/HHMM when it can parse a UTC
        timestamp out of the input filename (it takes no time argument on
        the command line). WAV_PATH is a fixed name, so in practice every
        decode hits the "without date" branch and parts[0] is a meaningless
        fragment of the filename (observed: "ture", from "..._capture.wav"),
        not a placeholder worth trying to parse. capture_start — the even
        UTC minute this 120 s recording began — is the correct timestamp
        for a WSPR spot (which reports the *start* of the TX window), so
        the fallback uses that instead of the decode-time "now".
        """
        parts = line.split()
        if len(parts) < 7:
            return None
        try:
            if len(parts) >= 9 and parts[0][0].isdigit() and len(parts[0]) == 6:
                ts   = datetime.strptime(parts[0] + parts[1], "%y%m%d%H%M").replace(tzinfo=timezone.utc)
                snr   = float(parts[2])
                freq  = float(parts[4])
                drift = float(parts[5])
                call  = parts[6]
                grid  = parts[7]
                power = int(parts[8])
            else:
                float(parts[1])
                ts    = capture_start
                snr   = float(parts[1])
                freq  = float(parts[3])
                drift = float(parts[4])
                call  = parts[5]
                grid  = parts[6]
                power = int(parts[7])

            dist = None
            brng = None
            country = None
            if len(grid) >= 4:
                try:
                    dist = distance_km(self.my_grid, grid)
                    brng = bearing(self.my_grid, grid)
                except Exception:
                    pass
                cached, cached_country = db.get_cached_country(grid)
                if cached:
                    country = cached_country

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
                "country": country,
            }
        except (ValueError, IndexError):
            return None
