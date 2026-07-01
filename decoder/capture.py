"""
Pure-Python RTL-TCP capture + USB demodulator.
Replaces the rtl_fm | sox pipeline so we don't need a native rtl_fm
with network-device support.
"""
import struct
import socket
import time
import wave
import logging
import numpy as np

logger = logging.getLogger(__name__)

# RTL-TCP command IDs
CMD_SET_FREQ        = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE   = 0x03  # 0=auto, 1=manual
CMD_SET_GAIN        = 0x04  # gain in tenths of dB
CMD_SET_FREQ_CORR   = 0x05
CMD_SET_IF_GAIN     = 0x06
CMD_SET_TEST_MODE   = 0x07
CMD_SET_AGC_MODE    = 0x08  # 0=off, 1=on (RTL2832 internal AGC)
CMD_SET_DIRECT_SAMP = 0x09
CMD_SET_OFFSET_TUNE = 0x0A
CMD_SET_RTL_XTAL    = 0x0B
CMD_SET_TUNER_XTAL  = 0x0C
CMD_SET_TUNER_GAIN  = 0x0D

SAMPLE_RATE = 240_000   # Hz — 20× decimation to 12 kHz audio
AUDIO_RATE  = 12_000    # Hz — what wsprd wants
DECIMATE    = SAMPLE_RATE // AUDIO_RATE   # 20

WAV_PATH    = "/tmp/wspr_capture.wav"


def _send_cmd(sock: socket.socket, cmd: int, value: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, value))


def _build_fir(cutoff_hz: float, sample_rate: float, n_taps: int = 127) -> np.ndarray:
    """Windowed-sinc low-pass FIR, normalised cutoff = cutoff_hz / (sample_rate/2)."""
    fc = cutoff_hz / (sample_rate / 2)
    n = np.arange(n_taps) - (n_taps - 1) / 2
    with np.errstate(invalid="ignore"):
        h = np.sinc(fc * n)
    h *= np.blackman(n_taps)
    h /= h.sum()
    return h.astype(np.float32)


def capture_wspr(host: str, port: int, freq_hz: int,
                 duration_s: int = 120, gain_tenths: int = 200) -> bool:
    """
    Connect to rtl_tcp, tune to freq_hz, record duration_s seconds of IQ,
    USB-demodulate, and write a 12 kHz mono WAV to WAV_PATH.

    Returns True on success, False on error.
    """
    total_samples = SAMPLE_RATE * duration_s
    # Each sample is 2 bytes (I, Q each U8)
    total_bytes   = total_samples * 2

    buf = bytearray()

    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        logger.error("[capture] Cannot connect to rtl_tcp %s:%d — %s", host, port, exc)
        return False

    try:
        # Read 12-byte server header: "RTL0" + tuner_type(4) + gain_count(4)
        hdr = b""
        deadline = time.monotonic() + 5
        while len(hdr) < 12:
            if time.monotonic() > deadline:
                logger.error("[capture] Timeout waiting for rtl_tcp handshake")
                return False
            chunk = sock.recv(12 - len(hdr))
            if not chunk:
                logger.error("[capture] rtl_tcp closed connection during handshake")
                return False
            hdr += chunk

        if hdr[:4] != b"RTL0":
            logger.error("[capture] Bad magic from rtl_tcp: %r", hdr[:4])
            return False

        logger.info("[capture] Connected to rtl_tcp — tuner type %d", struct.unpack(">I", hdr[4:8])[0])

        # Configure the tuner
        _send_cmd(sock, CMD_SET_SAMPLE_RATE, SAMPLE_RATE)
        _send_cmd(sock, CMD_SET_FREQ,        freq_hz)
        _send_cmd(sock, CMD_SET_GAIN_MODE,   1)           # manual gain
        _send_cmd(sock, CMD_SET_GAIN,        gain_tenths) # e.g. 200 = 20.0 dB
        _send_cmd(sock, CMD_SET_AGC_MODE,    1)           # RTL AGC on

        sock.settimeout(5)

        logger.info(
            "[capture] Recording %d s at %.4f MHz, sr=%d, gain=%.1f dB",
            duration_s, freq_hz / 1e6, SAMPLE_RATE, gain_tenths / 10
        )

        deadline = time.monotonic() + duration_s + 10  # 10 s grace
        while len(buf) < total_bytes:
            if time.monotonic() > deadline:
                logger.warning("[capture] Recording deadline exceeded (%d/%d bytes)", len(buf), total_bytes)
                break
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                logger.warning("[capture] rtl_tcp closed stream early")
                break
            buf.extend(chunk)

    except OSError as exc:
        logger.error("[capture] Socket error: %s", exc)
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if len(buf) < total_bytes // 2:
        logger.error("[capture] Too few bytes received: %d", len(buf))
        return False

    # ── USB demodulation ────────────────────────────────────────────────────────
    # RTL-TCP streams interleaved U8 I/Q (0–255, centre = 127.4)
    raw = np.frombuffer(bytes(buf[:total_bytes]), dtype=np.uint8)
    I = raw[0::2].astype(np.float32) - 127.4
    Q = raw[1::2].astype(np.float32) - 127.4
    iq = I + 1j * Q  # complex baseband

    # Low-pass to WSPR bandwidth (±300 Hz) then decimate 20× to 12 kHz
    fir = _build_fir(cutoff_hz=350.0, sample_rate=SAMPLE_RATE, n_taps=255)

    # Filter both I and Q channels
    I_f = np.convolve(iq.real, fir, mode="same")
    Q_f = np.convolve(iq.imag, fir, mode="same")

    # USB: upper sideband = I + Q  (for a zero-IF receiver tuned to carrier)
    audio = (I_f + Q_f)[::DECIMATE]

    # Normalise and convert to 16-bit PCM
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 28000   # leave ~15% headroom
    audio_i16 = audio.astype(np.int16)

    # Write WAV
    with wave.open(WAV_PATH, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(AUDIO_RATE)
        wf.writeframes(audio_i16.tobytes())

    logger.info("[capture] Wrote %d samples → %s", len(audio_i16), WAV_PATH)
    return True
