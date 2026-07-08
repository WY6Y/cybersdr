# CyberSDR

A real-time WSPR decoding and propagation dashboard for amateur radio operators. Runs on a Raspberry Pi (or any Linux host), pulls IQ samples from a remote RTL-SDR via `rtl_tcp`, decodes with `wsprd`, and presents everything in a cyberpunk-themed web UI.

---

## Features

### WSPR Tab
- **Live decoding** — rotates through 8 HF bands (80m → 10m), 2 min per band, 16-min full sweep
- **Leaflet map** — decoded spots plotted with bearing lines from QTH; dark CARTO tiles
- **Greyline overlay** — real-time day/night terminator computed from solar declination, updates every 60 s; toggle on/off
- **Band Activity HUD** — per-band decode count, avg SNR, farthest DX, and condition score (DARK / WEAK / FAIR / OPEN / STRONG) for the past 2 hours
- **Solar mini-panel** — SFI, SN, A-index, K-index tiles in the left column
- **Storm badge** — flashes red in the header when Kp ≥ 4
- **Spot table** — scrollable live log with call, band, SNR, distance, grid, country
- **Callsign lookups** — click a call in the map popup or spot table to open it on QRZ or HamQTH in a new tab
- **Country tagging** — each spot's grid square is reverse-geocoded to a country/region name (see [Callsign Lookups & Country Tagging](#callsign-lookups--country-tagging))
- **DX Intel** — farthest DX, best SNR, unique calls, total spots for the UTC day
- **Decoder status** — current state, band, dial frequency, countdown to next slot
- **WSPRnet upload** — spots uploaded automatically after each decode cycle

### PROP Tab
- **Solar KPI tiles** — SFI, SN, A-index, Kp with geomagnetic storm label (G1–G5)
- **K storm meter** — colour-coded 0–9 bar, updates from NOAA every 3 hours
- **HF band forecast** — day/night conditions for 80m–40m, 30m–20m, 17m–15m, 12m–10m, computed from SFI + Kp
- **SFI 7-day sparkline** — canvas chart with propagation-quality colour bands
- **WSPR vs K 48h correlation** — decode rate (green bars) overlaid with Kp (orange line) and K=4 storm threshold

---

## Hardware

| Component | Notes |
|-----------|-------|
| Decode host | Raspberry Pi 5 (or any Linux machine with Docker) |
| SDR | RTL-SDR Blog V4 (or any RTL-SDR supported device) |
| SDR host | Any machine running `rtl_tcp` — can be local or remote over LAN/VPN |
| Antenna | HF wire or vertical |

The decode host pulls IQ samples over the network via a pure-Python `rtl_tcp` client (no `rtl_fm` dependency). `wsprd` (from the `wsjtx` Debian package) runs inside the Docker container.

> **Remote SDR tip:** If your SDR is on a separate machine, run `rtl_tcp -a 0.0.0.0 -p 1234` there and set `RTL_TCP_HOST` to its IP. Works great over a VPN (Tailscale, WireGuard, etc.).

---

## Stack

- **Flask + Waitress** — HTTP server, SSE for live updates
- **SQLite** — spot history, space weather history (7-day rolling)
- **Docker** — `network_mode: host`; templates and static files are volume-mounted so UI changes are live without a rebuild
- **Caddy** (optional) — reverse proxy for HTTPS

---

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/WY6Y/cybersdr.git
cd cybersdr
cp .env.example .env
$EDITOR .env          # set RTL_TCP_HOST, MY_CALL, MY_GRID at minimum

# 2. Build and run
docker compose up -d

# 3. Open in browser
http://localhost:5020

# Watch logs
docker compose logs -f

# UI-only changes (HTML/CSS/JS) — no rebuild needed, just reload the browser

# Python or Dockerfile changes — rebuild required
docker compose down && docker compose build && docker compose up -d
```

Or use `install.sh` for a one-shot setup that also installs a systemd service.

---

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `127.0.0.1` | Host running `rtl_tcp` |
| `RTL_TCP_PORT` | `1234` | `rtl_tcp` port |
| `MY_CALL` | `N0CALL` | Your callsign (used for WSPRnet upload) |
| `MY_GRID` | `AA00aa` | Your Maidenhead grid locator (4 or 6 char) |
| `RTL_GAIN` | `20` | Gain in dB — fallback for any band not in `RTL_GAIN_BANDS` |
| `RTL_GAIN_BANDS` | (see `.env.example`) | Per-band manual gain override, `band:gain,band:gain,...` in dB |
| `WSPRNET_UPLOAD` | `true` | Upload spots to WSPRnet after each decode |
| `PORT` | `5020` | Flask listen port |
| `DB_PATH` | `/data/cybersdr.db` | SQLite database path (inside container) |
| `NOMINATIM_CONTACT` | _(blank)_ | Contact email added to the Nominatim User-Agent (per their usage policy) |

> **Sharing the dongle with SDR++:** only one client can hold `rtl_tcp` at a time. `POST /api/decoder/stop` fully releases the socket so another app (SDR++, etc.) can connect — CyberSDR also resets the tuner to auto-gain at the end of every capture, so the dongle is never left at a high manual gain from the last WSPR band when you switch over.

---

## Band Rotation

8 bands, 2 minutes each — full sweep every 16 minutes:

| Band | Dial (MHz) |
|------|-----------|
| 80m | 3.5926 |
| 40m | 7.0386 |
| 30m | 10.1387 |
| 20m | 14.0956 |
| 17m | 18.1046 |
| 15m | 21.0946 |
| 12m | 24.9246 |
| 10m | 28.1246 |

---

## Space Weather Data Sources

All free, no API key required:

| Data | Source | Update interval |
|------|--------|----------------|
| SFI (Solar Flux Index) | NOAA SWPC `10cm-flux.json` | 3 hours |
| Kp + Ap | NOAA SWPC `noaa-planetary-k-index.json` | 3 hours |
| Sunspot number (SN) | SILSO monthly CSV | Monthly |

HF band conditions are derived from SFI + Kp using propagation rules — no third-party forecast API needed.

---

## Callsign Lookups & Country Tagging

- **QRZ / HamQTH** — clicking a callsign (map popup or spot table) opens `qrz.com/db/<call>` or `hamqth.com/<call>` in a new tab. No API key, no backend involved — a missing QRZ listing (common for DXpeditions, Antarctic bases, or exotic/rare entities) just shows QRZ's own "not found" page.
- **Country tagging** — each spot's decoded grid square is reverse-geocoded to a country/region name via [OpenStreetMap Nominatim](https://nominatim.org/) (free, no API key). This is independent of the callsign, so it still identifies the location even when QRZ has no record for that call.
  - Runs on its own background thread (`decoder/geocode.py` → `GeocodePoller`), polling every 15 s for spots still missing a country and backfilling `spots.country` in SQLite.
  - Deliberately decoupled from the WSPR capture loop — a slow or rate-limited geocode lookup must never delay the next capture, which has to start on the next even UTC minute.
  - Throttled to Nominatim's usage policy (≤1 request/sec) and cached per grid square (`geocode_cache` table) so a repeat spotter or common DX path is only looked up once.
  - A blank country means either "not backfilled yet" (brand-new grid, resolves within ~15–30 s) or "no match" (open ocean / unmapped area — Nominatim genuinely has no boundary data there, which itself is a useful signal for a suspiciously remote low-SNR decode).

---

## Caddy Config (optional HTTPS)

```
sdr.example.com {
    tls internal
    reverse_proxy 127.0.0.1:5020
}
```

---

## Project Layout

```
cybersdr/
├── app.py                  # Flask app, SSE, API routes
├── db.py                   # SQLite schema + helpers
├── decoder/
│   ├── capture.py          # Pure-Python rtl_tcp IQ capture + USB demod
│   ├── geocode.py          # Nominatim reverse-geocode poller (spot country tagging)
│   ├── grid.py             # Maidenhead grid distance/bearing
│   ├── spaceweather.py     # NOAA/SILSO space weather poller
│   ├── upload.py           # WSPRnet upload
│   └── wspr.py             # WSPRDecoder state machine
├── templates/
│   └── index.html          # Single-page app (all CSS inline)
├── static/
│   └── js/app.js           # All frontend JS
├── Dockerfile
├── docker-compose.yml
├── install.sh              # One-shot install + systemd service
├── requirements.txt
└── .env.example            # Config template — copy to .env
```

---

## License

MIT
