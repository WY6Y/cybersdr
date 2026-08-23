"""
app.py — CyberSDR Flask/Waitress dashboard for WSPR HF monitoring.

Endpoints:
    GET  /              dashboard HTML
    GET  /api/status    decoder state JSON
    GET  /api/spots     last 200 spots from SQLite
    GET  /api/bands     per-band summary for today
    GET  /api/stats     today's totals
    GET  /api/band_conditions   band openness for last 2 hours
    GET  /api/storm_hangover    per-band recovery curve after last K>=5 storm (P93)
    GET  /api/openness_model    personal band-openness model: hour-of-day x SFI/K (P92)
    GET  /api/heatmap   path-density / DX heat (hours, optional band + end)
    GET  /api/heatmap/meta  earliest/latest + daily counts for time machine
    GET  /api/ionosphere_snapshot  monthly postcard snapshot (P95)
    GET  /api/export/csv  download spots as CSV (hours query, omit for all)
    GET  /api/balloons          suspected balloon / airborne watch list
    GET  /api/balloons/<call>   full track + spots for one call
    POST /api/balloons/<call>/flag  watch|confirmed|dismissed|auto
    GET  /api/balloons/channels          U4B/Traquito channels (id13-grouped, P101)
    GET  /api/balloons/channels/<id13>   full decoded track for one channel
    POST /api/decoder/stop      pause decoder (frees RTL-SDR)
    POST /api/decoder/start     resume decoder
    GET  /stream        SSE push of spot/status/stats events
"""

import csv
import io
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone

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


@app.route("/api/storm_hangover")
def api_storm_hangover():
    """Per-band recovery curve after the most recent K>=5 geomagnetic storm (P93)."""
    try:
        min_k = float(request.args.get("min_k", 5.0))
    except (TypeError, ValueError):
        min_k = 5.0
    return jsonify(db.get_storm_hangover(min_k=min_k))


@app.route("/api/openness_model")
def api_openness_model():
    """Personal band-openness model: hour-of-day baseline + current-SFI/K multiplier (P92)."""
    try:
        days = int(request.args.get("days", 60))
    except (TypeError, ValueError):
        days = 60
    return jsonify(db.get_openness_model(days=days))


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


@app.route("/api/heatmap")
def api_heatmap():
    """
    Spots for DX / path-density heatmap (client builds heat points + arcs).

    Query:
      hours — window length (1–720, default 48)
      band  — optional HF band filter (40m/30m/20m/17m/15m/12m/10m)
      end   — optional ISO UTC end of window (default: now) for time machine
    """
    try:
        hours = max(1, min(int(request.args.get("hours", 48)), 24 * 30))
    except (TypeError, ValueError):
        hours = 48
    band = (request.args.get("band") or "").strip().lower() or None
    if band == "all":
        band = None
    end = (request.args.get("end") or "").strip() or None
    payload = db.get_spots_for_heatmap(hours=hours, band=band, end=end)
    return jsonify(payload)


@app.route("/api/heatmap/meta")
def api_heatmap_meta():
    """Coverage + daily density for the heatmap time-machine scrubber."""
    try:
        days = max(1, min(int(request.args.get("days", 30)), 120))
    except (TypeError, ValueError):
        days = 30
    band = (request.args.get("band") or "").strip().lower() or None
    if band == "all":
        band = None
    meta = db.get_heatmap_meta()
    meta["days"] = days
    meta["band"] = band or "all"
    meta["daily"] = db.get_heatmap_daily_counts(days=days, band=band)
    return jsonify(meta)


@app.route("/api/ionosphere_snapshot")
def api_ionosphere_snapshot():
    """Monthly 'state of the ionosphere from EL29' postcard data (P95)."""
    try:
        days = max(1, min(int(request.args.get("days", 30)), 120))
    except (TypeError, ValueError):
        days = 30
    return jsonify(db.get_ionosphere_snapshot(days=days))


@app.route("/api/balloons")
def api_balloons():
    """
    Suspected balloon / airborne WSPR watch list (P97).

    Query:
      days — history window (1–365, default 45)
      min_score — classifier floor (default 48)
      telemetry — include 0/Q/1 telem packets (default 1)
    """
    from decoder.balloons import classify_all

    try:
        days = max(1, min(int(request.args.get("days", 45)), 365))
    except (TypeError, ValueError):
        days = 45
    try:
        min_score = max(0, min(int(request.args.get("min_score", 48)), 99))
    except (TypeError, ValueError):
        min_score = 48
    include_telem = request.args.get("telemetry", "1").lower() not in ("0", "false", "no")

    grouped = db.get_spots_grouped_by_call(days=days, min_spots=2)
    flags = db.get_balloon_flags()
    suspects = classify_all(
        grouped,
        flags=flags,
        include_telemetry=include_telem,
        min_score=min_score,
    )
    # Slim list payload (full track on detail endpoint)
    slim = []
    for s in suspects:
        slim.append(
            {
                k: s[k]
                for k in (
                    "call",
                    "call_clean",
                    "kind",
                    "score",
                    "status",
                    "note",
                    "spot_count",
                    "grid_count",
                    "grids",
                    "powers",
                    "bands",
                    "first_seen",
                    "last_seen",
                    "span_hours",
                    "span_km",
                    "max_kmh",
                    "median_kmh",
                    "total_track_km",
                    "reasons",
                    "altitude_hints",
                    "last_spot",
                    "traquito_channel",
                    "traquito_latest",
                )
                if k in s
            }
        )
    return jsonify(
        {
            "days": days,
            "min_score": min_score,
            "count": len(slim),
            "balloons": slim,
            "encoding_notes": {
                "motion": "Multi-grid hop speed + span from stored Type-1 spots",
                "zachtek_power": "altitude_m ≈ power_dbm * 300",
                "wb8elk_power": "power vocabulary index * 1000 m coarse altitude",
                "telemetry": "0*/Q*/1* callsigns are telem packets; grids often encode sensors",
                "traquito_basic": (
                    "U4B/Traquito Basic Telemetry (P101) decoded per-spot — no pairing needed: "
                    "alt/temp/voltage/speed/GPS-valid come from one call+grid+power row"
                ),
                "not_yet": "WB8ELK fine telemetry and Traquito Extended Telemetry frames are not decoded",
            },
        }
    )


@app.route("/api/balloons/channels")
def api_balloon_channels():
    """
    U4B/Traquito telemetry channels (P101), grouped by id13 — not by literal
    callsign, since a telemetry callsign changes with every altitude update.
    Each channel's frames decode independently (no cross-frame pairing), but
    grouping by id13 turns the fragmented per-spot cards into one track.

    Query: days — history window (1-365, default 45)
    """
    from decoder.balloons import classify_channels

    try:
        days = max(1, min(int(request.args.get("days", 45)), 365))
    except (TypeError, ValueError):
        days = 45

    spots = db.get_telemetry_spots(days=days)
    channels = classify_channels(spots)
    return jsonify({"days": days, "count": len(channels), "channels": channels})


@app.route("/api/balloons/channels/<id13>")
def api_balloon_channel_detail(id13: str):
    """Full decoded track for one U4B/Traquito id13 channel (e.g. 'Q6')."""
    from decoder.balloons import classify_channels

    try:
        days = max(1, min(int(request.args.get("days", 120)), 365))
    except (TypeError, ValueError):
        days = 120

    id13 = id13.strip().upper()
    spots = db.get_telemetry_spots(days=days)
    channels = classify_channels(spots)
    match = next((c for c in channels if c["id13"] == id13), None)
    if not match:
        return jsonify({"error": "no frames for channel", "id13": id13}), 404
    match["days"] = days
    return jsonify(match)


@app.route("/api/balloons/<path:call>")
def api_balloon_detail(call: str):
    """Full track + spots for one suspected balloon callsign."""
    from decoder.balloons import classify_call, clean_call

    try:
        days = max(1, min(int(request.args.get("days", 120)), 365))
    except (TypeError, ValueError):
        days = 120

    call_key = call.strip()
    # Try exact, then cleaned (strip hash brackets)
    spots = db.get_spots_for_call(call_key, days=days)
    if not spots:
        alt = clean_call(call_key)
        if alt != call_key.upper():
            spots = db.get_spots_for_call(alt, days=days)
            call_key = alt
        # Also try bracketed form
        if not spots and not call_key.startswith("<"):
            spots = db.get_spots_for_call(f"<{clean_call(call_key)}>", days=days)

    if not spots:
        return jsonify({"error": "no spots", "call": call}), 404

    flags = db.get_balloon_flags()
    flag = flags.get(clean_call(spots[0]["call"]))
    detail = classify_call(spots[0]["call"], spots, flag=flag, force=True)
    if not detail:
        return jsonify({"error": "classify failed", "call": call}), 500

    # Include raw spots (capped) for the detail table
    detail["spots"] = spots[-500:]
    detail["days"] = days
    return jsonify(detail)


@app.route("/api/balloons/<path:call>/flag", methods=["POST"])
def api_balloon_flag(call: str):
    """
    Set watch status for a callsign.

    JSON body: { "status": "watch|confirmed|dismissed|auto", "note": "..." }
    """
    from decoder.balloons import clean_call

    data = request.get_json(silent=True) or {}
    status = (data.get("status") or request.args.get("status") or "watch").strip()
    note = (data.get("note") or request.args.get("note") or "").strip()
    try:
        row = db.set_balloon_flag(clean_call(call), status, note)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **row})


@app.route("/api/export/csv")
def api_export_csv():
    """
    Download spots as CSV.
    ?hours=24|48|168  — window (omit or hours=0 for all, capped at 100k rows)
    """
    raw = request.args.get("hours", "").strip()
    hours = None
    if raw and raw != "0":
        try:
            hours = max(1, min(int(raw), 24 * 365))
        except (TypeError, ValueError):
            hours = 168

    spots = db.get_spots_for_export(hours=hours)
    cols = [
        "timestamp", "call", "freq", "band", "snr", "drift",
        "grid", "power", "distance_km", "bearing", "country",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for s in spots:
        writer.writerow({k: s.get(k, "") for k in cols})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    window = f"{hours}h" if hours else "all"
    filename = f"cybersdr-spots-{window}-{stamp}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


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
