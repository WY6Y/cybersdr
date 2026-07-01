"""
wefax.py — WEFAX (HF Weather Fax) receiver.

Streams IQ from rtl_tcp, demodulates USB audio at 8000 Hz,
pipes to multimon-ng for WEFAX576 decoding.

State machine:
    IDLE → PHASING (connect + tune) → RECEIVING (multimon-ng decoding)
         → DONE (end-of-fax or timeout or stop)
         → ERROR on failure

Usage from app.py:
    wefax = WefaxReceiver(rtl_host, rtl_port, on_done=decoder.resume)
    wefax.start(freq_mhz, station)   # launches daemon thread
    wefax.stop()                     # signals graceful stop
    wefax.get_status()               # dict
"""

import glob
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── WEFAX station presets ─────────────────────────────────────────────────────

WEFAX_STATIONS = [
    {"name": "NMG New Orleans (Gulf)",   "freqs": [4.3179, 8.5039, 12.7895]},
    {"name": "NMF Boston (N Atlantic)",  "freqs": [4.235,  6.3405, 9.110,  12.750]},
    {"name": "NMC Pt Reyes (Pacific)",   "freqs": [4.346,  8.682,  12.786, 17.151]},
    {"name": "NMN Chesapeake (S Atl)",   "freqs": [6.3405, 8.080,  12.750]},
]

# ── file paths ────────────────────────────────────────────────────────────────

WEFAX_DIR   = "/data/wefax"
CURRENT_PNG = os.path.join(WEFAX_DIR, "current.png")
MAX_IMAGES  = 20

# ── SDR / audio parameters ───────────────────────────────────────────────────

RTL_RATE    = 240_000   # Hz — sample rate from rtl_tcp
AUDIO_RATE  = 8_000     # Hz — multimon-ng input rate
DECIMATE    = RTL_RATE // AUDIO_RATE   # 30
CHUNK_S     = 1         # process 1 second of IQ at a time
CHUNK_BYTES = RTL_RATE * CHUNK_S * 2  # 1 s × 2 bytes/sample (I + Q)
MAX_MINUTES = 25        # hard timeout

# RTL-TCP command IDs
CMD_SET_FREQ        = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE   = 0x03
CMD_SET_GAIN        = 0x04
CMD_SET_AGC_MODE    = 0x08


# ── helpers ───────────────────────────────────────────────────────────────────

def _send_cmd(sock: socket.socket, cmd: int, value: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, value))


def _build_fir(cutoff_hz: float, sample_rate: float, n_taps: int = 127) -> np.ndarray:
    """Windowed-sinc low-pass FIR, cutoff in Hz."""
    fc = cutoff_hz / (sample_rate / 2)
    n = np.arange(n_taps) - (n_taps - 1) / 2
    with np.errstate(invalid="ignore"):
        h = np.sinc(fc * n)
    h *= np.blackman(n_taps)
    h /= h.sum()
    return h.astype(np.float32)


def _make_1x1_dark_png() -> bytes:
    """Return bytes for a minimal 1×1 dark PNG (colour #080810)."""
    import zlib as _zlib

    def _chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", _zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw  = b"\x00\x08\x08\x10"           # filter byte + R G B
    idat = _chunk(b"IDAT", _zlib.compress(raw, 9))
    iend = _chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


# Pre-built 1×1 placeholder PNG
_1x1_PNG: bytes = _make_1x1_dark_png()


# ── main class ────────────────────────────────────────────────────────────────

class WefaxReceiver:
    """
    Daemon-thread WEFAX receiver.  Call start() / stop() from Flask threads.
    on_done callback is invoked (in the receiver thread) when state reaches
    DONE or ERROR — use it to resume the WSPR decoder.
    """

    def __init__(
        self,
        rtl_host: str,
        rtl_port: int,
        gain_tenths: int = 200,
        on_done: Optional[Callable] = None,
    ):
        self.rtl_host    = rtl_host
        self.rtl_port    = rtl_port
        self.gain_tenths = gain_tenths
        self.on_done     = on_done

        self.state: str                     = "IDLE"
        self.station: str                   = ""
        self.freq_mhz: float                = 0.0
        self.image_path: str                = ""
        self.progress_lines: int            = 0
        self.started_at: Optional[datetime] = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def start(self, freq_mhz: float, station: str) -> None:
        """Begin a WEFAX receive session (non-blocking — spawns daemon thread)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                logger.warning("[WEFAX] start() while already running — ignored")
                return

            self.freq_mhz       = freq_mhz
            self.station        = station
            self.state          = "PHASING"
            self.progress_lines = 0
            self.started_at     = datetime.now(timezone.utc)
            self.image_path     = ""

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="wefax-receiver",
                daemon=True,
            )
            self._thread.start()

        logger.info("[WEFAX] Session started  station=%r  freq=%.4f MHz", station, freq_mhz)

    def stop(self) -> None:
        """Signal the receive thread to stop.  Returns immediately."""
        self._stop_event.set()
        logger.info("[WEFAX] Stop requested")

    def get_status(self) -> dict:
        with self._lock:
            elapsed = None
            if self.started_at:
                elapsed = round(
                    (datetime.now(timezone.utc) - self.started_at).total_seconds()
                )
            return {
                "state":          self.state,
                "station":        self.station,
                "freq_mhz":       self.freq_mhz,
                "progress_lines": self.progress_lines,
                "started_at":     self.started_at.isoformat() if self.started_at else None,
                "elapsed_s":      elapsed,
                "image_path":     self.image_path,
            }

    # ── private: thread body ──────────────────────────────────────────────────

    def _run(self) -> None:
        """Main receive loop — runs in its daemon thread."""
        os.makedirs(WEFAX_DIR, exist_ok=True)

        # Clean up temp PNM files from previous sessions
        for old in glob.glob(os.path.join(WEFAX_DIR, "fax*.pnm")):
            try:
                os.remove(old)
            except OSError:
                pass

        # USB offset: tune SDR to (freq - 1.75 kHz) so WEFAX tones appear
        # centred at ~1750 Hz in the audio passband.
        tune_hz = int((self.freq_mhz - 0.00175) * 1e6)

        logger.info("[WEFAX] Tuning to %.6f MHz (%.4f MHz carrier)",
                    tune_hz / 1e6, self.freq_mhz)

        # Pre-build FIR for this session (3.5 kHz LPF, 127 taps)
        fir = _build_fir(cutoff_hz=3500.0, sample_rate=RTL_RATE, n_taps=127)

        # ── connect to rtl_tcp ────────────────────────────────────────────────
        try:
            sock = socket.create_connection((self.rtl_host, self.rtl_port), timeout=10)
        except OSError as exc:
            logger.error("[WEFAX] Cannot connect to rtl_tcp %s:%d — %s",
                         self.rtl_host, self.rtl_port, exc)
            self._finish("ERROR")
            return

        proc: Optional[subprocess.Popen] = None
        try:
            # RTL-TCP handshake
            hdr = b""
            deadline = time.monotonic() + 5
            while len(hdr) < 12:
                if time.monotonic() > deadline:
                    logger.error("[WEFAX] Timeout during rtl_tcp handshake")
                    self._finish("ERROR")
                    return
                chunk = sock.recv(12 - len(hdr))
                if not chunk:
                    logger.error("[WEFAX] rtl_tcp closed during handshake")
                    self._finish("ERROR")
                    return
                hdr += chunk

            if hdr[:4] != b"RTL0":
                logger.error("[WEFAX] Bad magic from rtl_tcp: %r", hdr[:4])
                self._finish("ERROR")
                return

            _send_cmd(sock, CMD_SET_SAMPLE_RATE, RTL_RATE)
            _send_cmd(sock, CMD_SET_FREQ,        tune_hz)
            _send_cmd(sock, CMD_SET_GAIN_MODE,   1)
            _send_cmd(sock, CMD_SET_GAIN,        self.gain_tenths)
            _send_cmd(sock, CMD_SET_AGC_MODE,    1)
            sock.settimeout(5)

            logger.info("[WEFAX] RTL-SDR configured — %d ksps, gain %.1f dB",
                        RTL_RATE // 1000, self.gain_tenths / 10)

            # ── launch multimon-ng ────────────────────────────────────────────
            try:
                proc = subprocess.Popen(
                    [
                        "multimon-ng",
                        "-t", "raw",
                        "-s", str(AUDIO_RATE),
                        "-b", "16",
                        "-c", "1",
                        "-a", "WEFAX576",
                        "-",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=WEFAX_DIR,
                )
            except FileNotFoundError:
                logger.error("[WEFAX] multimon-ng not found — add it to the Dockerfile")
                self._finish("ERROR")
                return

            # Stdout reader thread: updates state and progress_lines
            self._start_stdout_reader(proc)

            # ── IQ receive loop ───────────────────────────────────────────────
            deadline_abs  = time.monotonic() + MAX_MINUTES * 60
            phasing_start = time.monotonic()
            buf           = bytearray()
            last_img_chk  = time.monotonic()

            while not self._stop_event.is_set() and time.monotonic() < deadline_abs:
                # Auto-advance from PHASING if audio is flowing but multimon-ng
                # hasn't printed a "start" line (version-dependent output)
                if self.state == "PHASING" and time.monotonic() - phasing_start > 30:
                    self._set_state("RECEIVING")

                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    logger.warning("[WEFAX] rtl_tcp closed early")
                    break
                buf.extend(data)

                # Process complete 1-second IQ chunks
                while len(buf) >= CHUNK_BYTES:
                    chunk = bytes(buf[:CHUNK_BYTES])
                    buf   = buf[CHUNK_BYTES:]

                    # USB demodulate: I channel only, LPF, decimate ×30
                    raw_np = np.frombuffer(chunk, dtype=np.uint8)
                    I      = raw_np[0::2].astype(np.float32) - 127.4
                    audio  = np.convolve(I, fir, mode="same")[::DECIMATE]

                    peak = float(np.max(np.abs(audio)))
                    if peak > 0:
                        audio = audio / peak * 28000
                    audio_i16 = audio.astype(np.int16)

                    try:
                        proc.stdin.write(audio_i16.tobytes())
                        proc.stdin.flush()
                    except BrokenPipeError:
                        logger.warning("[WEFAX] multimon-ng pipe closed")
                        self._stop_event.set()
                        break

                # Periodically update the live PNG
                if time.monotonic() - last_img_chk > 3:
                    last_img_chk = time.monotonic()
                    self._update_current_png()

            # ── shutdown ──────────────────────────────────────────────────────
            logger.info("[WEFAX] Receive loop ended (state=%s, stop=%s, timeout=%s)",
                        self.state, self._stop_event.is_set(),
                        time.monotonic() >= deadline_abs)
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

            # Final image save
            self._update_current_png()
            self._save_completed_image()

            final = self.state if self.state == "ERROR" else "DONE"
            self._finish(final)

        except Exception as exc:
            logger.error("[WEFAX] Unexpected error in run(): %s", exc, exc_info=True)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._finish("ERROR")

        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ── private helpers ───────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.state = state
        logger.info("[WEFAX] → %s", state)

    def _finish(self, state: str) -> None:
        """Set terminal state and call on_done callback."""
        self._set_state(state)
        if self.on_done:
            try:
                self.on_done()
            except Exception as exc:
                logger.warning("[WEFAX] on_done() raised: %s", exc)

    def _start_stdout_reader(self, proc: subprocess.Popen) -> None:
        """Parse multimon-ng stdout in a background thread."""
        def _reader():
            for raw_line in proc.stdout:
                try:
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    logger.debug("[multimon-ng] %s", text)

                    if "WEFAX576" not in text:
                        continue

                    lo = text.lower()
                    if "start" in lo or "phasing" in lo:
                        self._set_state("RECEIVING")
                    elif "line" in lo:
                        # Parse "WEFAX576: line 123" or similar
                        parts = text.split()
                        for i, p in enumerate(parts):
                            if p.lower() == "line" and i + 1 < len(parts):
                                try:
                                    with self._lock:
                                        self.progress_lines = int(parts[i + 1])
                                    if self.state == "PHASING":
                                        self._set_state("RECEIVING")
                                except ValueError:
                                    pass
                    elif "end" in lo or "stop" in lo:
                        self._set_state("DONE")
                        self._stop_event.set()
                except Exception:
                    pass

        t = threading.Thread(target=_reader, name="wefax-stdout", daemon=True)
        t.start()

    def _find_pnm_file(self) -> Optional[str]:
        """Return the most recently modified PNM/PPM/PGM/PBM in WEFAX_DIR."""
        candidates = []
        for ext in ("*.pnm", "*.ppm", "*.pgm", "*.pbm"):
            candidates.extend(glob.glob(os.path.join(WEFAX_DIR, ext)))
        # Exclude our own archived images (timestamped names start with 20xx)
        candidates = [
            f for f in candidates
            if os.path.basename(f)[:2] not in ("20",)
            and "current" not in os.path.basename(f)
        ]
        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)

    def _update_current_png(self) -> None:
        """Convert latest PNM to current.png using Pillow (if available)."""
        try:
            from PIL import Image
        except ImportError:
            return

        pnm = self._find_pnm_file()
        if not pnm:
            return
        try:
            img = Image.open(pnm)
            img.save(CURRENT_PNG, format="PNG")
            with self._lock:
                self.image_path = CURRENT_PNG
        except Exception as exc:
            logger.debug("[WEFAX] Image update error: %s", exc)

    def _save_completed_image(self) -> None:
        """Archive current.png with a timestamped name; prune old images."""
        if not os.path.exists(CURRENT_PNG):
            return

        ts          = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name   = "".join(c if c.isalnum() or c == " " else "_"
                               for c in self.station)[:20].strip("_")
        archive     = os.path.join(WEFAX_DIR, f"{ts}_{safe_name}.png")

        try:
            shutil.copy2(CURRENT_PNG, archive)
            logger.info("[WEFAX] Archived completed image → %s", archive)
        except Exception as exc:
            logger.warning("[WEFAX] Could not archive image: %s", exc)
            return

        # Prune gallery — keep newest MAX_IMAGES
        all_imgs = sorted(glob.glob(os.path.join(WEFAX_DIR, "20*.png")))
        while len(all_imgs) > MAX_IMAGES:
            oldest = all_imgs.pop(0)
            try:
                os.remove(oldest)
                logger.info("[WEFAX] Pruned: %s", oldest)
            except OSError:
                pass
