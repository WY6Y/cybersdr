"""
app.py — CyberSDR Flask/Waitress dashboard for WSPR HF monitoring.

Endpoints:
    GET  /              dashboard HTML
    GET  /api/status    decoder state JSON
    GET  /api/spots     last 200 spots from SQLite
    GET  /api/bands     per-band summary for today
    GET  /api/stats     today's totals
    GET  /api/band_conditions   band openness for last 2 hours
    POST /api/decoder/stop      pause decoder (frees RTL-SDR)
    POST /api/decoder/start     resume decoder
    GET  /stream        SSE push of spot/status/stats events

    POST /api/wefax/start               body: {freq_mhz, station}
    POST /api/wefax/stop
    GET  /api/wefax/status
    GET  /api/wefax/image/current.png   live image (no-cache)
    GET  /api/wefax/gallery             JSON list of past images
    GET  /api/wefax/images/<filename>   serve past image file
"""

import json
import logging
import os
import queue
import threading

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

import db
from decoder.wspr import WSPRDecoder
from decoder.spaceweather import SpaceWeatherPoller
from decoder.geocode import GeocodePoller

# ── config ────────────────────────────────────────────────────────────────────

RTL_TCP_HOST = os.getenv("RTL_TCP_HOST", "127.0.0.1")
RTL_TCP_PORT = int(os.getenv("RTL_TCP_PORT", "1234"))
MY_CALL = os.getenv("MY_CALL", "WY6Y")
MY_GRID = os.getenv("MY_GRID", "EL29")
PORT = int(os.getenv("PORT", "5020"))

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ── SSE subscriber registry ───────────────────────────────────────────────────

_sse_clients: list = []
_sse_lock = threading.Lock()


def _push(event_type: str, data: dict) -> None:
    """Enqueue a Server-Sent Event to all connected clients."""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ── decoder ───────────────────────────────────────────────────────────────────

decoder = WSPRDecoder(
    rtl_host=RTL_TCP_HOST,
    rtl_port=RTL_TCP_PORT,
    my_call=MY_CALL,
    my_grid=MY_GRID,
    sse_push=_push,
)

# Space weather poller
space_wx = SpaceWeatherPoller()

# Backfills spots.country via Nominatim, throttled, off the WSPR capture path
geo_poller = GeocodePoller()

# ── routes ────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html", my_call=MY_CALL, my_grid=MY_GRID)


@app.route("/api/status")
def api_status():
    status = decoder.get_status()
    status["rtl_host"] = RTL_TCP_HOST
    status["rtl_port"] = RTL_TCP_PORT
    return jsonify(status)


@app.route("/api/spots")
def api_spots():
    return jsonify(db.get_recent_spots(200))


@app.route("/api/bands")
def api_bands():
    return jsonify(db.get_band_summary())


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_today_stats(MY_GRID))


@app.route("/api/band_conditions")
def band_conditions():
    return jsonify(db.get_band_conditions(MY_GRID))


@app.route("/api/decoder/stop", methods=["POST"])
def decoder_stop():
    decoder.pause()
    return jsonify({"ok": True, "state": decoder.state})


@app.route("/api/decoder/start", methods=["POST"])
def decoder_start():
    decoder.resume()
    return jsonify({"ok": True, "state": decoder.state})


# ── Space weather routes ──────────────────────────────────────────────────────


@app.route("/api/spaceweather")
def api_spaceweather():
    return jsonify(space_wx.get_current())


@app.route("/api/spaceweather/khistory")
def api_khistory():
    return jsonify(space_wx.get_kindex_history())


@app.route("/api/spaceweather/history")
def api_sw_history():
    return jsonify(db.get_space_weather_history(7))


@app.route("/api/wspr/hourly")
def api_wspr_hourly():
    return jsonify(db.get_wspr_hourly_counts(48))


# ── SSE stream ────────────────────────────────────────────────────────────────


@app.route("/stream")
def stream():
    """Server-Sent Events endpoint — keep-alive with heartbeat every 25 s."""
    client_q: queue.Queue = queue.Queue(maxsize=200)
    with _sse_lock:
        _sse_clients.append(client_q)

    def generate():
        # Immediately send current decoder state to the new subscriber
        status_json = json.dumps(decoder.get_status())
        yield f"event: status\ndata: {status_json}\n\n"

        try:
            while True:
                try:
                    msg = client_q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(client_q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    logger.info("[CyberSDR] DB initialised at %s", os.getenv("DB_PATH", "/data/cybersdr.db"))

    dec_thread = threading.Thread(target=decoder.run, name="wspr-decoder", daemon=True)
    dec_thread.start()
    logger.info("[CyberSDR] Decoder thread started")

    space_wx.start()
    logger.info("[CyberSDR] Space weather poller started")

    geo_poller.start()
    logger.info("[CyberSDR] Geocode poller started")

    logger.info("[CyberSDR] Serving on 0.0.0.0:%d  call=%s  grid=%s", PORT, MY_CALL, MY_GRID)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=300)
