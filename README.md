# CyberSDR

A real-time WSPR decoding and propagation dashboard for amateur radio operators. Runs on a Raspberry Pi 5, pulls IQ samples from a remote RTL-SDR via `rtl_tcp`, decodes with `wsprd`, and presents everything in a cyberpunk-themed web UI.

Live at **`https://sdr.wy6y.net/`** (Tailscale only).

---

## Features

### WSPR Tab
- **Live decoding** — rotates through 8 HF bands (80m → 10m), 2 min per band, 16-min sweep
- **Leaflet map** — decoded spots plotted with bearing lines from QTH; dark CARTO tiles
- **Greyline overlay** — real-time day/night terminator computed from solar declination, updates every 60 s; toggle on/off
- **Band Activity HUD** — per-band decode count, avg SNR, farthest DX, and condition score (DARK/WEAK/FAIR/OPEN/STRONG) for the past 2 hours
- **Solar mini-panel** — SFI, SN, A-index, K-index tiles in the left column
- **Storm badge** — flashes red in the header when Kp ≥ 4
- **Spot table** — scrollable live log with call, band, SNR, distance, grid
- **DX Intel** — farthest DX, best SNR, unique calls, total spots (today)
- **Decoder status** — current state, band, dial frequency, countdown to next slot
- **WSPRnet upload** — spots uploaded automatically after each decode cycle

### PROP Tab
- **Solar KPI tiles** — SFI, SN, A-index, Kp with geomagnetic storm label (G1–G5)
- **K storm meter** — colour-coded 0–9 bar; updates from NOAA in real time
- **HF band forecast** — day/night propagation conditions for 80m–40m, 30m–20m, 17m–15m, 12m–10m, computed from SFI + Kp
- **SFI 7-day sparkline** — canvas chart with propagation-quality colour bands
- **WSPR vs K 48h correlation** — decode rate (green bars) overlaid with Kp (orange line) and K=4 storm threshold

---

## Hardware

| Component | Detail |
|-----------|--------|
| Decode host | Raspberry Pi 5 (`WY6YPi5`) |
| SDR | RTL-SDR Blog V4 on `ECHOLINK-MACHINE` (100.66.32.23) |
| RTL-TCP | `rtl_tcp -a 0.0.0.0 -p 1234` on ECHOLINK-MACHINE |
| Antenna | HF wire — connected to ECHOLINK-MACHINE |

The Pi 5 pulls IQ samples over Tailscale via a pure-Python `rtl_tcp` client (no `rtl_fm` dependency). `wsprd` (from the `wsjtx` Debian package) does the actual decoding inside the Docker container.

---

## Stack

- **Flask + Waitress** — HTTP server, SSE for live updates
- **SQLite** — spot history, space weather history (7-day rolling)
- **Docker** — `network_mode: host`; templates and static files are volume-mounted (no rebuild needed for UI changes)
- **Caddy** — reverse proxy; `sdr.wy6y.net → 127.0.0.1:5020`

---

## Quick Start

```bash
# First time
cp .env.example .env       # fill in RTL_TCP_HOST, MY_CALL, MY_GRID, etc.

# Build and run
docker compose up -d

# Watch logs
docker compose logs -f

# UI-only changes (HTML/CSS/JS) — no rebuild needed
# just reload the browser

# Python/Dockerfile changes — rebuild required
docker compose down && docker compose build && docker compose up -d
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `100.66.32.23` | Host running `rtl_tcp` |
| `RTL_TCP_PORT` | `1234` | `rtl_tcp` port |
| `MY_CALL` | `WY6Y` | Your callsign (used for WSPRnet upload) |
| `MY_GRID` | `EM15fo` | Your Maidenhead grid (4 or 6 char) |
| `RTL_GAIN` | `20` | Gain in dB (tenths used internally) |
| `WSPRNET_UPLOAD` | `true` | Upload spots to WSPRnet after each decode |
| `PORT` | `5020` | Flask listen port |
| `DB_PATH` | `/data/cybersdr.db` | SQLite database path (inside container) |

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

HF band conditions are derived from SFI and Kp using propagation rules — no third-party forecast API needed.

---

## Project Layout

```
cybersdr/
├── app.py                  # Flask app, SSE, API routes
├── db.py                   # SQLite schema + helpers
├── decoder/
│   ├── capture.py          # Pure-Python rtl_tcp IQ capture + USB demod
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
├── requirements.txt
└── install.sh
```

---

## Caddy Config

```
sdr.wy6y.net {
    tls internal
    reverse_proxy 127.0.0.1:5020
}
```

---

*WY6Y — EM15fo*
