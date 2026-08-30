/*
 * app.js — CyberSDR frontend logic
 *
 * Globals injected by Flask template:
 *   MY_CALL  — operator callsign  (e.g. "WY6Y")
 *   MY_GRID  — Maidenhead grid    (e.g. "EL29")
 */

'use strict';

// ── Maidenhead helpers ────────────────────────────────────────────────────────

function gridToLatLon(grid) {
  grid = grid.toUpperCase().trim();
  if (grid.length < 4) return [0, 0];
  let lon = (grid.charCodeAt(0) - 65) * 20 - 180;
  let lat = (grid.charCodeAt(1) - 65) * 10 - 90;
  lon += (grid.charCodeAt(2) - 48) * 2;
  lat += (grid.charCodeAt(3) - 48) * 1;
  if (grid.length >= 6) {
    lon += (grid.charCodeAt(4) - 65) * (2 / 24);
    lat += (grid.charCodeAt(5) - 65) * (1 / 24);
    lon += 1 / 24;
    lat += 0.5 / 24;
  } else {
    lon += 1.0;
    lat += 0.5;
  }
  return [lat, lon];
}

// ── Band colour map ───────────────────────────────────────────────────────────

const BAND_COLORS = {
  '80m': '#9966ff',
  '40m': '#ff6600',
  '30m': '#ffcc00',
  '20m': '#00f5ff',
  '17m': '#00ff88',
  '15m': '#ff0090',
  '12m': '#ff9933',
  '10m': '#ff2222',
};

function bandColor(band) {
  return BAND_COLORS[band] || '#aaaaaa';
}

// ── Callsign lookup links ───────────────────────────────────────────────────────

// wsprd wraps a callsign in <> when it was resolved from a WSPR Type 2/3
// hash pair rather than decoded directly (used for compound/portable calls
// or a precise 6-char grid). "<...>" means the hash itself couldn't be
// resolved this session — there is no real callsign to look up at all.
function cleanCall(call) {
  return call.replace(/^<|>$/g, '');
}

function isLookupableCall(call) {
  const cleaned = cleanCall(call);
  return cleaned.length > 0 && cleaned !== '...';
}

function qrzUrl(call) {
  return `https://www.qrz.com/db/${encodeURIComponent(cleanCall(call))}`;
}

function hamqthUrl(call) {
  return `https://www.hamqth.com/${encodeURIComponent(cleanCall(call))}`;
}

// ── UTC clock ─────────────────────────────────────────────────────────────────

function tickClock() {
  const now = new Date();
  const hh = String(now.getUTCHours()).padStart(2, '0');
  const mm = String(now.getUTCMinutes()).padStart(2, '0');
  const ss = String(now.getUTCSeconds()).padStart(2, '0');
  document.getElementById('utc-clock').textContent = `${hh}:${mm}:${ss} UTC`;
}
setInterval(tickClock, 1000);
tickClock();

// ── Tab switching ─────────────────────────────────────────────────────────────

let _activeTab = 'wspr';

function switchTab(tab) {
  _activeTab = tab;

  document.getElementById('content-wspr').style.display = (tab === 'wspr') ? '' : 'none';
  const prop = document.getElementById('content-prop');
  const bal = document.getElementById('content-balloon');
  if (prop) prop.className = (tab === 'prop') ? 'active' : '';
  if (bal) bal.className = (tab === 'balloon') ? 'active' : '';

  document.getElementById('tab-wspr').classList.toggle('active', tab === 'wspr');
  const tabBal = document.getElementById('tab-balloon');
  if (tabBal) tabBal.classList.toggle('active', tab === 'balloon');
  document.getElementById('tab-prop').classList.toggle('active', tab === 'prop');

  if (tab === 'wspr') {
    setTimeout(() => { if (window._map) window._map.invalidateSize(); }, 50);
  }
  if (tab === 'prop') {
    refreshSpaceWeather();
    refreshHangover();
    refreshOpenness();
    renderCharts();
  }
  if (tab === 'balloon') {
    refreshBalloonView();
    setTimeout(() => {
      ensureBalloonMap();
      if (window._balloonMap) window._balloonMap.invalidateSize();
    }, 80);
  }
}

// ── Countdown to next decode slot ─────────────────────────────────────────────

let _nextDecodeUtc = null;

function updateCountdown() {
  const el = document.getElementById('countdown');
  if (!_nextDecodeUtc) { el.textContent = '--:--'; return; }
  const diff = Math.max(0, Math.floor((new Date(_nextDecodeUtc) - Date.now()) / 1000));
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  el.textContent = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
setInterval(updateCountdown, 1000);

// ── Leaflet map ───────────────────────────────────────────────────────────────

// CARTO stamps an "API KEY REQUIRED" watermark into unkeyed tiles. Set your own
// free key (https://carto.com/basemaps/apikey) as CARTO_KEY in .env; blank keeps
// the unkeyed URL so the map still draws, just watermarked.
function cartoDarkUrl() {
  const base = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  const k = (typeof CARTO_KEY !== 'undefined' && CARTO_KEY) ? CARTO_KEY : '';
  return k ? base + '?key=' + encodeURIComponent(k) : base;
}

const map = L.map('map', {
  center: [20, 0],
  zoom: 2,
  zoomControl: true,
  attributionControl: true,
});
window._map = map;

L.tileLayer(cartoDarkUrl(), {
  attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

// QTH marker — pulsing cyan dot
const qthLatLon = gridToLatLon(MY_GRID);
const qthIcon = L.divIcon({
  className: '',
  html: `<div style="
    width:14px;height:14px;
    border-radius:50%;
    background:#00f5ff;
    box-shadow:0 0 0 3px rgba(0,245,255,0.25), 0 0 12px #00f5ff;
    animation:qth-pulse 2s ease-in-out infinite;
  "></div>
  <style>
    @keyframes qth-pulse {
      0%,100%{box-shadow:0 0 0 3px rgba(0,245,255,0.25),0 0 12px #00f5ff;}
      50%{box-shadow:0 0 0 8px rgba(0,245,255,0.1),0 0 24px #00f5ff;}
    }
  </style>`,
  iconSize: [14, 14],
  iconAnchor: [7, 7],
});

L.marker(qthLatLon, { icon: qthIcon })
  .addTo(map)
  .bindPopup(`<b style="color:#00f5ff">${MY_CALL}</b><br>QTH: ${MY_GRID}`)
  .openPopup();

// Store map layers keyed by spot id or call+timestamp
const spotLayers = {};
const MAX_MAP_SPOTS = 150;
let mapSpotQueue = [];

function addSpotToMap(spot) {
  if (!spot.grid || spot.grid.length < 4) return;
  const latLon = gridToLatLon(spot.grid);
  const balloon = isBalloonCall(spot.call);
  const color = balloon ? '#ffc832' : bandColor(spot.band);
  const key = `${spot.call}-${spot.timestamp}`;

  // Prune oldest if needed
  if (mapSpotQueue.length >= MAX_MAP_SPOTS) {
    const old = mapSpotQueue.shift();
    if (spotLayers[old]) {
      spotLayers[old].forEach(l => map.removeLayer(l));
      delete spotLayers[old];
    }
  }

  const pathOpacity = _heatmapOn ? 0.16 : 0.65;
  const markOpacity = _heatmapOn ? 0.25 : 0.8;

  const line = L.polyline([qthLatLon, latLon], {
    color: color,
    weight: balloon ? 2.2 : 1.5,
    opacity: pathOpacity,
    dashArray: balloon ? '2 6' : '4 4',
  }).addTo(map);

  const marker = L.circleMarker(latLon, {
    radius: balloon ? 7 : 5,
    color: balloon ? '#fff7c2' : color,
    fillColor: color,
    fillOpacity: markOpacity,
    opacity: _heatmapOn ? 0.35 : 1,
    weight: balloon ? 2 : 1,
  }).addTo(map)
    .bindPopup(
      `<div style="font-family:monospace;font-size:12px;background:#0d0d1a;color:#b0c4cc;border:1px solid ${color};padding:6px 10px">` +
      `<b style="color:${color}">${spot.call}</b> · ${spot.grid}` +
      (balloon ? ` <span style="color:#ffc832">▲ BALLOON?</span>` : '') + `<br>` +
      (spot.country ? `${spot.country}<br>` : '') +
      `Band: ${spot.band} · SNR: ${spot.snr} dB<br>` +
      `${spot.distance_km ? spot.distance_km.toLocaleString() + ' km' : 'dist?'} · ${spot.power} dBm` +
      (isLookupableCall(spot.call)
        ? `<br><a href="${qrzUrl(spot.call)}" target="_blank" rel="noopener noreferrer" style="color:${color}">QRZ</a>` +
          ` · <a href="${hamqthUrl(spot.call)}" target="_blank" rel="noopener noreferrer" style="color:${color}">HamQTH</a>`
        : '') +
      `</div>`
    );

  spotLayers[key] = [line, marker];
  mapSpotQueue.push(key);
}

// ── Spot table ────────────────────────────────────────────────────────────────

const MAX_TABLE_ROWS = 50;
let spotCount = 0;

function addSpotRow(spot) {
  const tbody = document.getElementById('spot-tbody');

  // Clear placeholder row on first real spot
  if (spotCount === 0) {
    tbody.innerHTML = '';
  }
  spotCount++;

  const ts = new Date(spot.timestamp);
  const timeStr = `${String(ts.getUTCHours()).padStart(2,'0')}:${String(ts.getUTCMinutes()).padStart(2,'0')}`;
  const color = bandColor(spot.band);
  const distStr = spot.distance_km != null ? Math.round(spot.distance_km).toLocaleString() : '---';
  const isBalloon = isBalloonCall(spot.call);

  const tr = document.createElement('tr');
  tr.className = 'row-new' + (isBalloon ? ' balloon-spot' : '');
  tr.style.borderLeft = `3px solid ${isBalloon ? '#ffc832' : color}`;
  tr.innerHTML = `
    <td class="td-time">${timeStr}</td>
    <td class="td-call" style="color:${isBalloon ? '#ffc832' : color}">
      ${isLookupableCall(spot.call)
        ? `<a href="${qrzUrl(spot.call)}" target="_blank" rel="noopener noreferrer" title="Look up ${spot.call} on QRZ">${spot.call}</a>`
        : spot.call}
    </td>
    <td>${spot.grid}${spot.country ? `<span class="td-country">${spot.country}</span>` : ''}</td>
    <td style="color:${color}">${spot.band}</td>
    <td class="td-snr">${spot.snr > 0 ? '+' : ''}${spot.snr}</td>
    <td class="td-dist">${distStr}</td>
    <td class="td-pwr">${spot.power}</td>
  `;
  if (isBalloon) {
    tr.title = 'Suspected balloon / airborne — see BALLOON tab';
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      switchTab('balloon');
      selectBalloon(cleanCall(spot.call));
    });
  }

  // Insert at top
  tbody.insertBefore(tr, tbody.firstChild);

  // Trim excess rows
  while (tbody.rows.length > MAX_TABLE_ROWS) {
    tbody.deleteRow(tbody.rows.length - 1);
  }
}

// ── Decoder status panel ──────────────────────────────────────────────────────

function updateStatus(data) {
  const badge = document.getElementById('state-badge');
  const state = data.state || 'IDLE';

  badge.textContent = state;
  badge.className = `badge-${state}`;

  if (data.current_band) {
    document.getElementById('status-band').textContent = data.current_band;
    const color = bandColor(data.current_band);
    document.getElementById('status-band').style.color = color;
  }
  if (data.dial_freq) {
    document.getElementById('status-freq').textContent = data.dial_freq.toFixed(4) + ' MHz';
  }
  if (data.next_decode_utc) {
    _nextDecodeUtc = data.next_decode_utc;
  }
  if (data.rtl_host) {
    const el = document.getElementById('rtl-host');
    if (el) el.textContent = data.rtl_host + ':' + (data.rtl_port || 1234);
  }

  const rotEl = document.getElementById('status-rotation');
  if (rotEl) {
    if (data.smart_rotation) {
      const parked = data.parked_until ? Object.keys(data.parked_until) : [];
      const night = data.local_night ? ' · NIGHT' : '';
      rotEl.textContent = parked.length
        ? `SMART · PARK ${parked.join(' ')}${night}`
        : `SMART · WEIGHTED${night}`;
      rotEl.title = parked.length
        ? `Parked: ${parked.join(', ')}`
        : 'Weighted band rotation (more time on open bands)';
    } else {
      rotEl.textContent = 'ROUND-ROBIN';
      rotEl.title = 'Equal time per band';
    }
  }

  // Button state
  const isPaused = data.paused;
  document.getElementById('btn-stop').disabled = isPaused;
  document.getElementById('btn-start').disabled = !isPaused;
}

// ── DX Intel panel ────────────────────────────────────────────────────────────

function updateStats(data) {
  document.getElementById('dx-total').textContent = (data.total_spots || 0).toLocaleString();
  document.getElementById('dx-unique').textContent = (data.unique_calls || 0).toLocaleString();

  if (data.farthest_dx) {
    const dx = data.farthest_dx;
    document.getElementById('dx-farthest-km').textContent =
      Math.round(dx.distance_km).toLocaleString() + ' km';
    document.getElementById('dx-farthest-call').textContent =
      `${dx.call} · ${dx.band}`;
  } else {
    document.getElementById('dx-farthest-km').textContent = '--- km';
    document.getElementById('dx-farthest-call').textContent = '---';
  }

  if (data.best_snr) {
    const bs = data.best_snr;
    document.getElementById('dx-best-snr').textContent =
      (bs.snr > 0 ? '+' : '') + bs.snr + ' dB';
    document.getElementById('dx-best-snr-call').textContent =
      `${bs.call} · ${bs.band}`;
  } else {
    document.getElementById('dx-best-snr').textContent = '--- dB';
    document.getElementById('dx-best-snr-call').textContent = '---';
  }

  // Also refresh band conditions when new stats arrive via SSE
  refreshBandConditions();
}

// ── Band activity bars ────────────────────────────────────────────────────────

function refreshBands() {
  fetch('/api/bands')
    .then(r => r.json())
    .then(bands => {
      const max = bands.reduce((m, b) => Math.max(m, b.count), 1);
      bands.forEach(b => {
        const bar = document.getElementById('bar-' + b.band);
        const cnt = document.getElementById('cnt-' + b.band);
        if (bar) bar.style.width = Math.max(2, Math.round((b.count / max) * 100)) + '%';
        if (cnt) cnt.textContent = b.count;
      });
    })
    .catch(() => {});
}
// Refresh band bars every 60 s
setInterval(refreshBands, 60000);
refreshBands();

// ── Band Conditions panel ─────────────────────────────────────────────────────

function refreshBandConditions() {
  fetch('/api/band_conditions')
    .then(r => r.json())
    .then(bands => renderBandConditions(bands))
    .catch(() => {});
}

function renderBandConditions(bands) {
  const list = document.getElementById('cond-list');
  if (!list) return;
  list.innerHTML = '';

  bands.forEach(b => {
    const row  = document.createElement('div');
    row.className = 'cond-row';

    const barW = b.score > 0 ? Math.max(3, Math.round(b.score)) : 0;

    // Stats text: spots / snr / km
    let statsText = '';
    if (b.spot_count > 0) {
      const snrStr  = b.avg_snr   != null ? (b.avg_snr > 0 ? '+' : '') + b.avg_snr + 'dB' : '--';
      const distStr = b.max_distance_km != null
        ? b.max_distance_km >= 1000
          ? Math.round(b.max_distance_km / 100) / 10 + 'k km'
          : b.max_distance_km + ' km'
        : '--';
      statsText = `${b.spot_count}sp ${snrStr} ${distStr}`;
    }

    row.innerHTML = `
      <span class="cond-band-name">${b.band}</span>
      <div class="cond-bar-wrap">
        <div class="cond-bar" style="width:${barW}%;background:${b.color}"></div>
      </div>
      <span class="cond-label" style="color:${b.color}">${b.condition}</span>
      <span class="cond-stats">${statsText}</span>
    `;
    list.appendChild(row);
  });
}

// Poll band conditions every 30 s
setInterval(refreshBandConditions, 30000);
refreshBandConditions();

// ── SSE connection ────────────────────────────────────────────────────────────

let sseSource = null;
let reconnectTimer = null;

function connectSSE() {
  if (sseSource) {
    sseSource.close();
    sseSource = null;
  }

  sseSource = new EventSource('/stream');
  const dot = document.getElementById('conn-dot');

  sseSource.addEventListener('open', () => {
    dot.className = '';
    clearTimeout(reconnectTimer);
  });

  sseSource.addEventListener('status', e => {
    try { updateStatus(JSON.parse(e.data)); } catch (_) {}
  });

  sseSource.addEventListener('spot', e => {
    try {
      const spot = JSON.parse(e.data);
      addSpotRow(spot);
      addSpotToMap(spot);
      refreshBands();
    } catch (_) {}
  });

  sseSource.addEventListener('stats', e => {
    try { updateStats(JSON.parse(e.data)); } catch (_) {}
  });

  sseSource.addEventListener('error', () => {
    dot.className = 'disconnected';
    sseSource.close();
    sseSource = null;
    // Exponential back-off: reconnect in 5 s
    reconnectTimer = setTimeout(connectSSE, 5000);
  });
}

// ── Decoder controls ──────────────────────────────────────────────────────────

function stopDecoder() {
  fetch('/api/decoder/stop', { method: 'POST' })
    .then(r => r.json())
    .then(d => updateStatus({ state: d.state, paused: true }))
    .catch(() => {});
}

function startDecoder() {
  fetch('/api/decoder/start', { method: 'POST' })
    .then(r => r.json())
    .then(d => updateStatus({ state: d.state, paused: false }))
    .catch(() => {});
}


// ═══════════════════════════════════════════════════════════════════════════════
// GREYLINE — night terminator overlay on Leaflet map
// ═══════════════════════════════════════════════════════════════════════════════

let _greylineLayer  = null;
let _greylineOn     = true;
let _greylineTimer  = null;

function getDOY(d) {
  return Math.floor((d - new Date(Date.UTC(d.getUTCFullYear(), 0, 0))) / 86400000);
}

function computeNightPoly(date) {
  const RAD = Math.PI / 180;
  const doy  = getDOY(date);
  const utcH = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;

  // Solar declination (radians)
  const decl = -23.45 * RAD * Math.cos(2 * Math.PI * (doy + 10) / 365);

  // Subsolar longitude (longitude where sun is directly overhead)
  const sunLon = -(utcH / 24 * 360 - 180);

  // Terminator: for each longitude compute latitude where solar elevation = 0
  const pts = [];
  for (let lon = -180; lon <= 180; lon += 1) {
    const H = (lon - sunLon) * RAD;
    let lat;
    if (Math.abs(Math.tan(decl)) < 1e-6) {
      lat = 0;
    } else {
      lat = Math.atan(-Math.cos(H) / Math.tan(decl)) / RAD;
    }
    pts.push([lat, lon]);
  }

  // Close polygon toward night pole (opposite hemisphere from sun)
  const pole = decl >= 0 ? -90 : 90;
  return [...pts, [pole, 180], [pole, -180]];
}

function drawGreyline() {
  if (!window._map) return;
  if (_greylineLayer) { _greylineLayer.remove(); _greylineLayer = null; }
  if (!_greylineOn) return;

  const poly = computeNightPoly(new Date());
  _greylineLayer = L.polygon(poly, {
    color:       'rgba(0,0,80,0)',
    fillColor:   '#000033',
    fillOpacity: 0.35,
    interactive: false,
    smoothFactor: 0,
  }).addTo(window._map);
}

function toggleGreyline() {
  _greylineOn = !_greylineOn;
  const btn = document.getElementById('greyline-toggle');
  btn.textContent = _greylineOn ? '🌍 GREYLINE ON' : '🌍 GREYLINE OFF';
  btn.classList.toggle('off', !_greylineOn);
  drawGreyline();
}

function startGreylineClock() {
  drawGreyline();
  _greylineTimer = setInterval(drawGreyline, 60000);
}


// ═══════════════════════════════════════════════════════════════════════════════
// DX HEATMAP — path density + station heat (time machine + per-band, P83/P84)
// ═══════════════════════════════════════════════════════════════════════════════

let _heatmapLayer = null;
let _heatmapOn    = false;
let _heatmapTimer = null;
let _heatmapHours = 24;
let _heatmapBand  = 'all';
let _heatmapLive  = true;
let _heatmapEndMs = null;       // UTC ms end of window; null => now when live
let _heatmapMeta  = null;       // /api/heatmap/meta payload
let _heatmapFetchSeq = 0;
let _scrubberBound = false;

const HEATMAP_SCRUB_DAYS = 30;
const HEATMAP_SCRUB_STEPS = 1000;

/** Great-circle sample points between two lat/lon pairs (degrees). */
function sampleGreatCircle(lat1, lon1, lat2, lon2, steps) {
  const toRad = Math.PI / 180;
  const toDeg = 180 / Math.PI;
  const φ1 = lat1 * toRad, λ1 = lon1 * toRad;
  const φ2 = lat2 * toRad, λ2 = lon2 * toRad;
  const Δ = 2 * Math.asin(Math.min(1, Math.sqrt(
    Math.sin((φ2 - φ1) / 2) ** 2 +
    Math.cos(φ1) * Math.cos(φ2) * Math.sin((λ2 - λ1) / 2) ** 2
  )));
  if (Δ < 1e-6) return [[lat1, lon1]];

  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const f = i / steps;
    const A = Math.sin((1 - f) * Δ) / Math.sin(Δ);
    const B = Math.sin(f * Δ) / Math.sin(Δ);
    const x = A * Math.cos(φ1) * Math.cos(λ1) + B * Math.cos(φ2) * Math.cos(λ2);
    const y = A * Math.cos(φ1) * Math.sin(λ1) + B * Math.cos(φ2) * Math.sin(λ2);
    const z = A * Math.sin(φ1) + B * Math.sin(φ2);
    const φ = Math.atan2(z, Math.sqrt(x * x + y * y));
    const λ = Math.atan2(y, x);
    pts.push([φ * toDeg, λ * toDeg]);
  }
  return pts;
}

function buildHeatPoints(spots) {
  // Accumulate intensity at destinations + along path arcs
  const gridCount = {};
  spots.forEach(s => {
    if (!s.grid || s.grid.length < 4) return;
    const g = s.grid.substring(0, 4).toUpperCase();
    gridCount[g] = (gridCount[g] || 0) + 1;
  });

  const points = []; // [lat, lon, intensity]

  // Station density (DX heatmap)
  Object.keys(gridCount).forEach(g => {
    const [lat, lon] = gridToLatLon(g);
    const n = gridCount[g];
    // Cap intensity so one busy grid doesn't wash out everything
    const intensity = Math.min(1.0, 0.35 + Math.log10(n + 1) * 0.45);
    points.push([lat, lon, intensity]);
  });

  // Path density — subsample unique grids to keep heat layer light
  const seenPath = new Set();
  let pathBudget = 0;
  const MAX_PATHS = 400;
  for (let i = 0; i < spots.length && pathBudget < MAX_PATHS; i++) {
    const s = spots[i];
    if (!s.grid || s.grid.length < 4) continue;
    const g = s.grid.substring(0, 4).toUpperCase();
    if (seenPath.has(g)) continue;
    seenPath.add(g);
    pathBudget++;
    const [lat, lon] = gridToLatLon(g);
    const pathPts = sampleGreatCircle(qthLatLon[0], qthLatLon[1], lat, lon, 12);
    pathPts.forEach((p, idx) => {
      const t = idx / (pathPts.length - 1 || 1);
      // Mid-path glow shows corridor density
      const mid = 1 - Math.abs(2 * t - 1); // 0 at ends, 1 at midpoint
      const inten = 0.12 + mid * 0.28;
      points.push([p[0], p[1], inten]);
    });
  }

  return points;
}

function setPathLayerOpacity(opacity) {
  Object.keys(spotLayers).forEach(key => {
    const layers = spotLayers[key];
    if (!layers) return;
    layers.forEach(l => {
      if (l.setStyle) {
        // polylines have dashArray; circleMarkers don't
        if (l instanceof L.Polyline && !(l instanceof L.CircleMarker)) {
          l.setStyle({ opacity: opacity * 0.65 });
        } else if (l instanceof L.CircleMarker) {
          l.setStyle({ fillOpacity: opacity * 0.8, opacity: opacity });
        }
      }
    });
  });
}

function clearHeatmapLayer() {
  if (_heatmapLayer && window._map) {
    window._map.removeLayer(_heatmapLayer);
    _heatmapLayer = null;
  }
}

function fmtUtcShort(isoOrMs) {
  const d = new Date(typeof isoOrMs === 'number' ? isoOrMs : isoOrMs);
  if (Number.isNaN(d.getTime())) return '—';
  const mo = d.getUTCMonth() + 1;
  const day = d.getUTCDate();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mo}/${day} ${hh}:${mm}Z`;
}

function heatmapScrubRange() {
  const now = Date.now();
  const latest = _heatmapMeta && _heatmapMeta.latest
    ? new Date(_heatmapMeta.latest).getTime()
    : now;
  const earliestRaw = _heatmapMeta && _heatmapMeta.earliest
    ? new Date(_heatmapMeta.earliest).getTime()
    : now - HEATMAP_SCRUB_DAYS * 86400000;
  const floor = Math.max(earliestRaw, now - HEATMAP_SCRUB_DAYS * 86400000);
  // End of window can sit from (floor + window) … max(latest, now)
  const endMax = Math.max(latest, now);
  const endMin = Math.min(endMax, floor + _heatmapHours * 3600000);
  return { endMin, endMax };
}

function syncHeatmapChips() {
  document.querySelectorAll('.heat-chip[data-band]').forEach(el => {
    const on = el.getAttribute('data-band') === _heatmapBand;
    el.classList.toggle('band-active', on);
    el.classList.toggle('active', on && _heatmapBand === 'all');
  });
  document.querySelectorAll('.heat-chip[data-hours]').forEach(el => {
    el.classList.toggle('active', Number(el.getAttribute('data-hours')) === _heatmapHours);
  });
  const liveBtn = document.getElementById('heatmap-live-btn');
  if (liveBtn) liveBtn.classList.toggle('active', _heatmapLive);
}

function updateHeatmapWindowLabel(startIso, endIso) {
  const el = document.getElementById('heatmap-window-label');
  if (!el) return;
  const band = _heatmapBand === 'all' ? 'ALL BANDS' : _heatmapBand.toUpperCase();
  el.textContent = `${band} · ${fmtUtcShort(startIso)} → ${fmtUtcShort(endIso)}`;
}

function updateHeatmapMeta(data) {
  const el = document.getElementById('heatmap-meta');
  if (!el) return;
  const s = data.summary || {};
  const sw = data.space_weather || {};
  const spots = s.spot_count != null ? s.spot_count : (data.count || 0);
  const grids = s.unique_grids != null ? s.unique_grids : '—';
  const maxKm = s.max_distance_km != null
    ? `${Math.round(s.max_distance_km).toLocaleString()} km`
    : '—';
  const snr = s.avg_snr != null ? `${s.avg_snr} dB` : '—';
  const trunc = s.truncated ? ' · sampled' : '';
  const sfi = sw.sfi_avg != null
    ? `SFI <strong>${sw.sfi_avg}</strong>${sw.sfi_min != null ? ` (${sw.sfi_min}–${sw.sfi_max})` : ''}`
    : 'SFI <strong>—</strong>';
  const k = sw.k_avg != null
    ? `K <strong>${sw.k_avg}</strong>${sw.k_min != null ? ` (${sw.k_min}–${sw.k_max})` : ''}`
    : 'K <strong>—</strong>';
  el.innerHTML = `
    <span class="hm-solar">${sfi}</span>
    <span class="hm-solar">${k}</span>
    <span>SPOTS <strong>${Number(spots).toLocaleString()}</strong>${trunc}</span>
    <span>GRIDS <strong>${grids}</strong></span>
    <span>AVG SNR <strong>${snr}</strong></span>
    <span>MAX DX <strong>${maxKm}</strong></span>
  `;
  if (data.start && data.end) updateHeatmapWindowLabel(data.start, data.end);
}

function renderHeatmap(spots) {
  if (!window._map || typeof L.heatLayer !== 'function') return;
  clearHeatmapLayer();
  if (!_heatmapOn) return;

  const pts = buildHeatPoints(spots || []);
  if (!pts.length) return;

  _heatmapLayer = L.heatLayer(pts, {
    radius: 22,
    blur: 18,
    maxZoom: 6,
    max: 1.0,
    minOpacity: 0.25,
    gradient: {
      0.0: '#0a0a20',
      0.2: '#001a40',
      0.4: '#00f5ff',
      0.6: '#00ff88',
      0.8: '#ffcc00',
      1.0: '#ff3300',
    },
  }).addTo(window._map);

  // Dim individual path lines so heat is readable
  setPathLayerOpacity(0.25);
}

function heatmapQueryUrl() {
  const params = new URLSearchParams();
  params.set('hours', String(_heatmapHours));
  if (_heatmapBand && _heatmapBand !== 'all') params.set('band', _heatmapBand);
  if (!_heatmapLive && _heatmapEndMs != null) {
    params.set('end', new Date(_heatmapEndMs).toISOString());
  }
  return `/api/heatmap?${params.toString()}`;
}

function refreshHeatmap() {
  if (!_heatmapOn) return;
  const seq = ++_heatmapFetchSeq;
  fetch(heatmapQueryUrl())
    .then(r => r.json())
    .then(data => {
      if (seq !== _heatmapFetchSeq || !_heatmapOn) return;
      renderHeatmap(data.spots || []);
      updateHeatmapMeta(data);
    })
    .catch(() => {});
}

function setHeatmapPanelOpen(open) {
  const panel = document.getElementById('heatmap-panel');
  if (panel) panel.classList.toggle('open', !!open);
}

function bindHeatmapScrubber() {
  if (_scrubberBound) return;
  const scrub = document.getElementById('heatmap-scrubber');
  if (!scrub) return;
  _scrubberBound = true;
  let debounce = null;
  const onInput = () => {
    const { endMin, endMax } = heatmapScrubRange();
    const t = Number(scrub.value) / HEATMAP_SCRUB_STEPS;
    const endMs = endMin + t * (endMax - endMin);
    const nearLive = t >= 0.995 || (endMax - endMs) < 15 * 60 * 1000;
    _heatmapLive = nearLive;
    _heatmapEndMs = nearLive ? null : endMs;
    syncHeatmapChips();
    restartHeatmapTimer();
    const startGuess = new Date(endMs - _heatmapHours * 3600000).toISOString();
    updateHeatmapWindowLabel(startGuess, new Date(endMs).toISOString());
    clearTimeout(debounce);
    debounce = setTimeout(refreshHeatmap, 120);
  };
  scrub.addEventListener('input', onInput);
  scrub.addEventListener('change', onInput);
}

function syncScrubberFromState() {
  const scrub = document.getElementById('heatmap-scrubber');
  if (!scrub) return;
  const { endMin, endMax } = heatmapScrubRange();
  scrub.min = '0';
  scrub.max = String(HEATMAP_SCRUB_STEPS);
  if (_heatmapLive || _heatmapEndMs == null) {
    scrub.value = String(HEATMAP_SCRUB_STEPS);
    return;
  }
  const span = Math.max(1, endMax - endMin);
  const t = Math.max(0, Math.min(1, (_heatmapEndMs - endMin) / span));
  scrub.value = String(Math.round(t * HEATMAP_SCRUB_STEPS));
}

function loadHeatmapMeta() {
  const params = new URLSearchParams({ days: String(HEATMAP_SCRUB_DAYS) });
  if (_heatmapBand && _heatmapBand !== 'all') params.set('band', _heatmapBand);
  return fetch(`/api/heatmap/meta?${params.toString()}`)
    .then(r => r.json())
    .then(meta => {
      _heatmapMeta = meta;
      syncScrubberFromState();
      return meta;
    })
    .catch(() => null);
}

function setHeatmapLive() {
  _heatmapLive = true;
  _heatmapEndMs = null;
  syncHeatmapChips();
  syncScrubberFromState();
  refreshHeatmap();
  restartHeatmapTimer();
}

function setHeatmapHours(hours) {
  _heatmapHours = Number(hours) || 24;
  syncHeatmapChips();
  syncScrubberFromState();
  refreshHeatmap();
}

function setHeatmapBand(band) {
  _heatmapBand = band || 'all';
  syncHeatmapChips();
  loadHeatmapMeta().then(() => refreshHeatmap());
}

function restartHeatmapTimer() {
  if (_heatmapTimer) { clearInterval(_heatmapTimer); _heatmapTimer = null; }
  if (_heatmapOn && _heatmapLive) {
    _heatmapTimer = setInterval(refreshHeatmap, 120000);
  }
}

function toggleHeatmap() {
  _heatmapOn = !_heatmapOn;
  const btn = document.getElementById('heatmap-toggle');
  if (btn) {
    btn.textContent = _heatmapOn ? '🔥 HEATMAP ON' : '🔥 HEATMAP OFF';
    btn.classList.toggle('off', !_heatmapOn);
    btn.classList.toggle('heat-on', _heatmapOn);
  }
  setHeatmapPanelOpen(_heatmapOn);
  if (_heatmapOn) {
    bindHeatmapScrubber();
    syncHeatmapChips();
    loadHeatmapMeta().then(() => {
      refreshHeatmap();
      restartHeatmapTimer();
    });
  } else {
    if (_heatmapTimer) { clearInterval(_heatmapTimer); _heatmapTimer = null; }
    clearHeatmapLayer();
    setPathLayerOpacity(1.0);
  }
}

// Expose controls for inline onclick handlers in the template
window.setHeatmapBand = setHeatmapBand;
window.setHeatmapHours = setHeatmapHours;
window.setHeatmapLive = setHeatmapLive;
window.toggleHeatmap = toggleHeatmap;


// ═══════════════════════════════════════════════════════════════════════════════
// SPACE WEATHER
// ═══════════════════════════════════════════════════════════════════════════════

const K_LEVELS = [
  { max: 1, label: 'QUIET',    color: '#00ff88' },
  { max: 2, label: 'UNSETTLED',color: '#aaff00' },
  { max: 3, label: 'ACTIVE',   color: '#ffcc00' },
  { max: 4, label: 'MINOR',    color: '#ffaa00' },
  { max: 5, label: 'MODERATE', color: '#ff6600' },
  { max: 6, label: 'STRONG',   color: '#ff4400' },
  { max: 7, label: 'SEVERE',   color: '#ff2222' },
  { max: 9, label: 'EXTREME',  color: '#ff0090' },
];

function kLevel(k) {
  const n = parseFloat(k);
  return K_LEVELS.find(l => n <= l.max) || K_LEVELS[K_LEVELS.length - 1];
}

const FC_COLORS = {
  'Excellent': '#00ff88',
  'Good':      '#00f5ff',
  'Fair':      '#ffaa00',
  'Poor':      '#ff6600',
};

function fcClass(val) {
  const map = { 'Excellent': 'fc-excellent', 'Good': 'fc-good', 'Fair': 'fc-fair', 'Poor': 'fc-poor' };
  return map[val] || 'fc-none';
}

function renderSpaceWeather(d) {
  if (!d || !d.sfi) return;

  const k    = parseFloat(d.kindex) || 0;
  const lvl  = kLevel(k);

  // ── Mini panel (WSPR tab left column) ──────────────────────────────────
  setText('mini-sfi',    d.sfi   || '---');
  setText('mini-sn',     d.sn    || '---');
  setText('mini-a',      d.aindex|| '---');
  setText('mini-k',      d.kindex|| '---');
  setText('mini-ktext',  d.kindex_text || '---');
  if (d.updated) {
    setText('mini-updated', 'UPD: ' + d.updated);
  }

  // K color on mini tile
  const mkEl = document.getElementById('mini-k');
  if (mkEl) mkEl.style.color = lvl.color;

  // ── Storm badge in header ───────────────────────────────────────────────
  const badge = document.getElementById('storm-badge');
  const stormK = document.getElementById('storm-k');
  if (badge && stormK) {
    if (k >= 4) {
      stormK.textContent = k;
      badge.classList.add('visible');
    } else {
      badge.classList.remove('visible');
    }
  }

  // ── PROP tab KPIs ───────────────────────────────────────────────────────
  setText('kpi-sfi',   d.sfi   || '---');
  setText('kpi-sn',    d.sn    || '---');
  setText('kpi-a',     d.aindex|| '---');
  setText('kpi-k',     d.kindex|| '---');
  setText('kpi-ktext', d.kindex_text || '---');

  // K color on KPI tile
  const kEl = document.getElementById('kpi-k');
  if (kEl) kEl.style.color = lvl.color;

  // K meter bar
  const fill = document.getElementById('k-meter-fill');
  if (fill) {
    fill.style.width          = Math.min(100, k / 9 * 100) + '%';
    fill.style.backgroundColor = lvl.color;
    fill.style.boxShadow       = `0 0 8px ${lvl.color}`;
  }
  const stormText = document.getElementById('k-storm-text');
  if (stormText) {
    stormText.textContent  = lvl.label;
    stormText.style.color  = lvl.color;
  }

  // Extra solar data
  setText('sx-xray', d.xray     || '---');
  setText('sx-wind', d.solar_wind|| '---');
  setText('sx-mag',  d.mag_field || '---');
  // Show NOAA observation time + our last fetch time so user can distinguish
  // "NOAA hasn't published" from "our system hasn't checked"
  let tsLine = d.updated ? 'DATA: ' + d.updated : 'DATA: ---';
  if (d.fetched_at) {
    try {
      const fa = new Date(d.fetched_at);
      const hh = fa.getUTCHours().toString().padStart(2, '0');
      const mm = fa.getUTCMinutes().toString().padStart(2, '0');
      tsLine += '  ·  FETCHED: ' + hh + ':' + mm + ' UTC';
    } catch (_) {}
  }
  setText('sw-updated-ts', tsLine);

  // ── hamqsl band forecast table ──────────────────────────────────────────
  const FORECAST_BANDS = [
    { key: '80m-40m', label: '80m–40m' },
    { key: '30m-20m', label: '30m–20m' },
    { key: '17m-15m', label: '17m–15m' },
    { key: '12m-10m', label: '12m–10m' },
  ];
  const tbody = document.getElementById('forecast-tbody');
  if (tbody && d.bands) {
    tbody.innerHTML = '';
    FORECAST_BANDS.forEach(b => {
      const dayVal   = d.bands[`${b.key}_day`]   || '?';
      const nightVal = d.bands[`${b.key}_night`]  || '?';
      const dayColor   = FC_COLORS[dayVal]   || '#555566';
      const nightColor = FC_COLORS[nightVal] || '#555566';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="fc-band">${b.label}</td>
        <td><span class="fc-dot" style="background:${dayColor}"></span><span class="${fcClass(dayVal)}">${dayVal}</span></td>
        <td><span class="fc-dot" style="background:${nightColor}"></span><span class="${fcClass(nightVal)}">${nightVal}</span></td>
      `;
      tbody.appendChild(tr);
    });
  }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function refreshSpaceWeather() {
  fetch('/api/spaceweather')
    .then(r => r.json())
    .then(d => renderSpaceWeather(d))
    .catch(() => {});
}

// Poll every 5 minutes
setInterval(refreshSpaceWeather, 5 * 60 * 1000);

// ── Storm hangover detector (P93) ────────────────────────────────────────────

function renderHangover(d) {
  const summary = document.getElementById('hangover-summary');
  const table   = document.getElementById('hangover-table');
  const tbody   = document.getElementById('hangover-tbody');
  if (!summary || !table || !tbody) return;

  if (!d || !d.event) {
    table.style.display = 'none';
    summary.textContent = 'No K≥5 geomagnetic storm in the lookback window — nothing to recover from.';
    return;
  }

  const ev = d.event;
  const agoH = ev.hours_ago;
  const agoText = agoH < 1 ? `${Math.round(agoH * 60)}m ago` : `${agoH.toFixed(1)}h ago`;
  summary.innerHTML = `Last storm: peak K <span>${ev.peak_k}</span>, ended ${agoText}. ` +
    `Recovery = 3h spot-rate reaching <span>${Math.round((d.recovery_frac || 0.8) * 100)}%</span> of the pre-storm 48h baseline.`;

  tbody.innerHTML = '';
  (d.bands || []).forEach(b => {
    let recCls = 'ho-none', recText = 'no baseline';
    if (b.baseline_rate > 0) {
      if (b.recovered) {
        recCls = 'ho-recovered';
        recText = b.hours_to_recover === 0 ? 'never dropped' : `back in ${b.hours_to_recover}h`;
      } else {
        recCls = 'ho-recovering';
        const pct = b.pct_of_baseline != null ? Math.round(b.pct_of_baseline * 100) : 0;
        recText = `down, ${pct}%`;
      }
    }
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="fc-band">${b.band}</td>
      <td>${b.baseline_rate}</td>
      <td>${b.current_rate}</td>
      <td class="${recCls}">${recText}</td>
    `;
    tbody.appendChild(tr);
  });
  table.style.display = '';
}

function refreshHangover() {
  fetch('/api/storm_hangover')
    .then(r => r.json())
    .then(d => renderHangover(d))
    .catch(() => {});
}

// Poll every 5 minutes alongside space weather
setInterval(refreshHangover, 5 * 60 * 1000);


// ── Personal band openness model (P92) ───────────────────────────────────────

const OPENNESS_BANDS = ['40m', '30m', '20m', '17m', '15m', '12m', '10m'];

function opennessColor(score) {
  if (score <= 0)  return '#22222e';
  if (score <= 20) return '#ff6600';
  if (score <= 50) return '#ffaa00';
  if (score <= 80) return '#00f5ff';
  return '#00ff88';
}

function renderOpenness(d) {
  const summary = document.getElementById('openness-summary');
  const grid    = document.getElementById('openness-grid');
  const legend  = document.getElementById('openness-legend');
  if (!summary || !grid) return;

  if (!d || !d.bands || Object.keys(d.bands).length === 0) {
    summary.textContent = 'Not enough WSPR history yet to build an openness model.';
    grid.innerHTML = '';
    return;
  }

  const cur = d.current;
  let curLine = 'No live space-weather reading available yet.';
  if (cur) {
    curLine = `Right now: SFI <span class="os-chip" style="color:#00f5ff">${cur.sfi}</span> ` +
      `(<span style="color:rgba(0,245,255,0.7)">${cur.sfi_tier}</span> for this station's history), ` +
      `K <span class="os-chip" style="color:#ffaa00">${cur.kindex}</span> ` +
      `(<span style="color:rgba(255,170,0,0.7)">${cur.k_tier}</span>).`;
  }
  const anyBand = d.bands[OPENNESS_BANDS.find(b => d.bands[b])] || Object.values(d.bands)[0];
  summary.innerHTML = curLine +
    ` &nbsp;·&nbsp; Model built from ${anyBand.sample_days} days of this station's own WSPR decodes (local time, America/Chicago).`;

  // Local hour "now"
  const nowHour = new Date().toLocaleString('en-US', { timeZone: 'America/Chicago', hour: 'numeric', hour12: false });
  const nowH = parseInt(nowHour, 10) % 24;

  grid.innerHTML = '';
  const corner = document.createElement('div');
  grid.appendChild(corner);
  for (let h = 0; h < 24; h++) {
    const lbl = document.createElement('div');
    lbl.className = 'oh-hourlbl' + (h === nowH ? ' oh-now' : '');
    lbl.textContent = h;
    grid.appendChild(lbl);
  }

  OPENNESS_BANDS.forEach(band => {
    const bd = d.bands[band];
    const label = document.createElement('div');
    label.className = 'oh-label';
    label.textContent = band;
    grid.appendChild(label);
    if (!bd) {
      for (let h = 0; h < 24; h++) grid.appendChild(document.createElement('div'));
      return;
    }
    bd.hours.forEach(hEntry => {
      const cell = document.createElement('div');
      cell.className = 'oh-cell' + (hEntry.hour === nowH ? ' oh-now-col' : '');
      cell.style.background = opennessColor(hEntry.adj_score);
      cell.title = `${band} @ ${String(hEntry.hour).padStart(2, '0')}:00 local — ` +
        `${hEntry.avg_spots_per_hr} spots/hr avg, score ${hEntry.adj_score}/100 ` +
        `(x${bd.current_multiplier} for today's solar state), ${bd.sample_spots} spots over ${bd.sample_days}d`;
      grid.appendChild(cell);
    });
  });

  if (legend) {
    legend.innerHTML =
      '<span><i class="leg-swatch" style="background:#22222e"></i>never</span>' +
      '<span><i class="leg-swatch" style="background:#ff6600"></i>rare</span>' +
      '<span><i class="leg-swatch" style="background:#ffaa00"></i>occasional</span>' +
      '<span><i class="leg-swatch" style="background:#00f5ff"></i>usually open</span>' +
      '<span><i class="leg-swatch" style="background:#00ff88"></i>reliably open</span>';
  }
}

function refreshOpenness() {
  fetch('/api/openness_model?days=60')
    .then(r => r.json())
    .then(d => renderOpenness(d))
    .catch(() => {});
}

// Poll every 5 minutes alongside space weather
setInterval(refreshOpenness, 5 * 60 * 1000);


// ═══════════════════════════════════════════════════════════════════════════════
// CHARTS — SFI sparkline + WSPR vs K correlation
// ═══════════════════════════════════════════════════════════════════════════════

function renderCharts() {
  // Defer one frame so the PROP tab's layout is committed before we measure canvas sizes
  requestAnimationFrame(() => {
    renderSfiChart();
    renderCorrChart();
  });
}

// ── SFI 7-day sparkline ───────────────────────────────────────────────────────

function renderSfiChart() {
  const canvas = document.getElementById('sfi-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  Promise.all([
    fetch('/api/spaceweather/history').then(r => r.json()),
  ]).then(([history]) => {
    if (!history || history.length === 0) {
      drawNoData(ctx, canvas, 'NO SFI HISTORY YET');
      return;
    }

    fitCanvas(canvas);
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const sfis = history.map(h => h.sfi).filter(v => v != null);
    if (sfis.length === 0) { drawNoData(ctx, canvas, 'NO DATA'); return; }

    const minV = Math.max(0, Math.min(...sfis) - 5);
    const maxV = Math.max(...sfis) + 5;
    const PAD  = { t: 10, r: 10, b: 24, l: 36 };
    const W2 = W - PAD.l - PAD.r;
    const H2 = H - PAD.t - PAD.b;

    const xScale = i => PAD.l + (i / (history.length - 1 || 1)) * W2;
    const yScale = v => PAD.t + H2 - ((v - minV) / (maxV - minV)) * H2;

    // Grid lines
    drawGrid(ctx, W, H, PAD, minV, maxV, 4, '#00f5ff');

    // SFI reference bands (color-coded propagation quality)
    const bands = [
      { min: 150, max: 300, color: 'rgba(0,255,136,0.06)',  label: 'HIGH' },
      { min: 100, max: 150, color: 'rgba(0,245,255,0.06)',  label: 'MED' },
      { min: 70,  max: 100, color: 'rgba(255,102,0,0.06)',  label: 'LOW' },
    ];
    bands.forEach(band => {
      const y1 = yScale(Math.min(band.max, maxV));
      const y2 = yScale(Math.max(band.min, minV));
      if (y2 > y1) {
        ctx.fillStyle = band.color;
        ctx.fillRect(PAD.l, y1, W2, y2 - y1);
      }
    });

    // SFI line
    ctx.beginPath();
    ctx.strokeStyle = '#00f5ff';
    ctx.lineWidth   = 2;
    ctx.shadowColor = '#00f5ff';
    ctx.shadowBlur  = 6;
    history.forEach((h, i) => {
      if (h.sfi == null) return;
      const x = xScale(i), y = yScale(h.sfi);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Dots at each reading
    ctx.fillStyle = '#00f5ff';
    history.forEach((h, i) => {
      if (h.sfi == null) return;
      ctx.beginPath();
      ctx.arc(xScale(i), yScale(h.sfi), 2.5, 0, 2 * Math.PI);
      ctx.fill();
    });

    // Y-axis labels
    drawYLabels(ctx, PAD, minV, maxV, 4, '#00f5ff');

    // X-axis: date labels (every ~24h of readings)
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font      = '9px monospace';
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.floor(history.length / 7));
    for (let i = 0; i < history.length; i += step) {
      const h = history[i];
      if (!h.ts) continue;
      const d = new Date(h.ts);
      const label = `${(d.getUTCMonth()+1)}/${d.getUTCDate()}`;
      ctx.fillText(label, xScale(i), H - 6);
    }
  }).catch(() => {});
}

// ── WSPR decodes vs K-index correlation ──────────────────────────────────────

function renderCorrChart() {
  const canvas = document.getElementById('corr-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  Promise.all([
    fetch('/api/wspr/hourly').then(r => r.json()),
    fetch('/api/spaceweather/khistory').then(r => r.json()),
  ]).then(([wspr, kdata]) => {

    fitCanvas(canvas);
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    if (!wspr || wspr.length === 0) {
      drawNoData(ctx, canvas, 'NO WSPR DATA YET');
      return;
    }

    // Build hour-aligned K lookup (NOAA Kp is 3-hourly; search ±3h for nearest)
    const kMap = {};
    kdata.forEach(row => {
      if (!row.time) return;
      kMap[row.time.slice(0, 13) + ':00:00'] = row.kp;
    });
    function nearestKp(hourStr) {
      const t = new Date(hourStr + 'Z').getTime();
      for (const delta of [0, -1, 1, -2, 2, -3, 3]) {
        const key = new Date(t + delta * 3600000).toISOString().slice(0, 13) + ':00:00';
        if (kMap[key] != null) return kMap[key];
      }
      return null;
    }

    const counts = wspr.map(w => w.count);
    const maxC   = Math.max(...counts, 1);
    const maxK   = 9;
    const PAD    = { t: 10, r: 10, b: 24, l: 36 };
    const W2 = W - PAD.l - PAD.r;
    const H2 = H - PAD.t - PAD.b;

    const xScale = i => PAD.l + (i / (wspr.length - 1 || 1)) * W2;
    const yScaleC = v => PAD.t + H2 - (v / maxC) * H2;
    const yScaleK = v => PAD.t + H2 - (v / maxK) * H2;
    const barW    = Math.max(2, W2 / wspr.length - 1);

    // Grid
    drawGrid(ctx, W, H, PAD, 0, maxC, 4, '#00ff88');

    // WSPR decode bars
    wspr.forEach((w, i) => {
      const x = xScale(i) - barW / 2;
      const y = yScaleC(w.count);
      ctx.fillStyle = 'rgba(0,255,136,0.55)';
      ctx.fillRect(x, y, barW, PAD.t + H2 - y);
    });

    // K-index line (using nearest 3-hour reading for each WSPR hour)
    ctx.beginPath();
    ctx.strokeStyle = '#ff6600';
    ctx.lineWidth   = 2;
    ctx.shadowColor = '#ff6600';
    ctx.shadowBlur  = 6;
    let firstK = true;
    wspr.forEach((w, i) => {
      const hourKey = w.hour ? w.hour.slice(0, 13) + ':00:00' : null;
      const kp = hourKey ? nearestKp(hourKey) : null;
      if (kp == null) return;
      const x = xScale(i), y = yScaleK(kp);
      firstK ? (ctx.moveTo(x, y), firstK = false) : ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Right-side K axis (0-9) label
    ctx.fillStyle = 'rgba(255,102,0,0.5)';
    ctx.font      = '9px monospace';
    ctx.textAlign = 'right';
    for (let k = 0; k <= 9; k += 3) {
      ctx.fillText(k, W - 4, yScaleK(k) + 3);
    }

    // Left-side WSPR count labels
    drawYLabels(ctx, PAD, 0, maxC, 4, '#00ff88');

    // X-axis hour labels
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font      = '9px monospace';
    ctx.textAlign = 'center';
    const labelStep = Math.max(1, Math.floor(wspr.length / 12));
    wspr.forEach((w, i) => {
      if (i % labelStep !== 0 || !w.hour) return;
      const d = new Date(w.hour + 'Z');
      ctx.fillText(`${d.getUTCHours()}h`, xScale(i), H - 6);
    });

    // K=4 storm threshold line
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(255,34,34,0.4)';
    ctx.lineWidth   = 1;
    ctx.beginPath();
    const y4 = yScaleK(4);
    ctx.moveTo(PAD.l, y4);
    ctx.lineTo(W - PAD.r, y4);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle  = 'rgba(255,34,34,0.5)';
    ctx.font       = '8px monospace';
    ctx.textAlign  = 'left';
    ctx.fillText('K=4 STORM', PAD.l + 2, y4 - 2);

  }).catch(() => {});
}

// ── Chart helpers ─────────────────────────────────────────────────────────────

function fitCanvas(canvas) {
  // offsetWidth/Height are reliable even during tab-switch layout; getBoundingClientRect returns 0
  canvas.width  = canvas.offsetWidth  || 400;
  canvas.height = canvas.offsetHeight || 160;
}

function drawNoData(ctx, canvas, msg) {
  fitCanvas(canvas);
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = 'rgba(0,245,255,0.2)';
  ctx.font      = '10px monospace';
  ctx.textAlign = 'center';
  ctx.fillText(msg, W / 2, H / 2);
}

function drawGrid(ctx, W, H, PAD, minV, maxV, ticks, color) {
  ctx.strokeStyle = `rgba(${hexToRgb(color)},0.1)`;
  ctx.lineWidth   = 1;
  for (let t = 0; t <= ticks; t++) {
    const y = PAD.t + (H - PAD.t - PAD.b) * (1 - t / ticks);
    ctx.beginPath();
    ctx.moveTo(PAD.l, y);
    ctx.lineTo(W - PAD.r, y);
    ctx.stroke();
  }
}

function drawYLabels(ctx, PAD, minV, maxV, ticks, color) {
  ctx.fillStyle = `rgba(${hexToRgb(color)},0.5)`;
  ctx.font      = '9px monospace';
  ctx.textAlign = 'right';
  for (let t = 0; t <= ticks; t++) {
    const v = minV + (maxV - minV) * (t / ticks);
    const H2 = (ctx.canvas.height - PAD.t - PAD.b);
    const y  = PAD.t + H2 - (t / ticks) * H2;
    ctx.fillText(Math.round(v), PAD.l - 3, y + 3);
  }
}

function hexToRgb(hex) {
  const r = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  return r ? `${parseInt(r[1],16)},${parseInt(r[2],16)},${parseInt(r[3],16)}` : '255,255,255';
}

// Re-render charts on window resize
window.addEventListener('resize', () => {
  if (_activeTab === 'prop') renderCharts();
});


// ═══════════════════════════════════════════════════════════════════════════════
// Balloon / airborne watch (P97)
// ═══════════════════════════════════════════════════════════════════════════════

let _balloonCalls = new Set();       // cleaned callsigns for feed highlight
let _balloonList = [];
let _balloonSelected = null;
let _balloonIncludeTelem = true;
let _balloonMap = null;
let _balloonTrackLayer = null;
let _balloonDays = 45;
let _balloonViewMode = 'suspects';  // 'suspects' | 'channels'
let _channelList = [];
let _channelSelected = null;

function isBalloonCall(call) {
  if (!_balloonCalls || _balloonCalls.size === 0) return false;
  return _balloonCalls.has(cleanCall(call));
}

function ensureBalloonMap() {
  const el = document.getElementById('balloon-map');
  if (!el || _balloonMap) return _balloonMap;
  _balloonMap = L.map('balloon-map', {
    center: [30, -90],
    zoom: 3,
    zoomControl: true,
  });
  window._balloonMap = _balloonMap;
  L.tileLayer(cartoDarkUrl(), {
    attribution: '&copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(_balloonMap);
  L.marker(qthLatLon, { icon: qthIcon })
    .addTo(_balloonMap)
    .bindPopup(`${MY_CALL} · ${MY_GRID}`);
  _balloonTrackLayer = L.layerGroup().addTo(_balloonMap);
  return _balloonMap;
}

function toggleBalloonTelem() {
  _balloonIncludeTelem = !_balloonIncludeTelem;
  const btn = document.getElementById('btn-telem-toggle');
  if (btn) btn.textContent = _balloonIncludeTelem ? 'TELEM ON' : 'TELEM OFF';
  refreshBalloonView();
}

function setBalloonView(mode) {
  _balloonViewMode = mode;
  document.getElementById('btn-view-suspects').classList.toggle('active', mode === 'suspects');
  document.getElementById('btn-view-channels').classList.toggle('active', mode === 'channels');
  document.getElementById('balloon-list-hdr').textContent =
    mode === 'channels' ? 'U4B TELEMETRY CHANNELS' : 'SUSPECTED TRACKS';
  document.getElementById('balloon-detail').innerHTML =
    `<div style="color:rgba(0,245,255,0.25);font-size:10px;padding:8px">Pick a ${mode === 'channels' ? 'channel' : 'suspect'} to plot its track.</div>`;
  refreshBalloonView();
}

function refreshBalloonView() {
  if (_balloonViewMode === 'channels') {
    refreshChannels();
  } else {
    refreshBalloons();
  }
}

async function refreshChannels() {
  const listEl = document.getElementById('balloon-list');
  if (!listEl) return;
  try {
    const data = await fetch(`/api/balloons/channels?days=${_balloonDays}`).then(r => r.json());
    _channelList = data.channels || [];
    document.getElementById('balloon-count').textContent = String(data.count || 0);
    document.getElementById('balloon-days').textContent = `${data.days || _balloonDays}d`;
    renderChannelList();
  } catch (err) {
    listEl.innerHTML = `<div style="color:#ff6600;padding:12px;font-size:10px">Failed to load channels: ${err}</div>`;
  }
}

function renderChannelList() {
  const listEl = document.getElementById('balloon-list');
  if (!listEl) return;
  if (!_channelList.length) {
    listEl.innerHTML = `<div style="color:rgba(0,245,255,0.25);padding:16px;text-align:center;font-size:10px">No telemetry channels in the last ${_balloonDays}d</div>`;
    return;
  }
  listEl.innerHTML = '';
  _channelList.forEach(c => {
    const card = document.createElement('div');
    const selected = _channelSelected === c.id13;
    card.className = `balloon-card kind-${c.coherent ? 'balloon' : 'mover'}${selected ? ' active' : ''}`;
    const latest = c.latest || {};
    const when = c.last_seen ? new Date(c.last_seen).toISOString().slice(5, 16).replace('T', ' ') + 'Z' : '—';
    card.innerHTML = `
      <div class="balloon-card-top">
        <span class="balloon-call">CH ${c.id13}</span>
        <span class="balloon-score">${c.coherent ? 'COHERENT' : 'NOISY'}</span>
      </div>
      <div class="balloon-meta">
        ${c.frame_count} frames (${c.basic_count} decoded) · ${(c.bands || []).join(' ')}
        · alt ${c.altitude_min_m ?? '—'}–${c.altitude_max_m ?? '—'}m
        <br>last ${when}${latest.grid6 ? ' · ' + latest.grid6 : ''}${latest.temperature_c != null ? ' · ' + latest.temperature_c + '°C' : ''}
      </div>
      ${c.coherence_note ? `<div class="balloon-reasons">${c.coherence_note}</div>` : ''}
    `;
    card.addEventListener('click', () => selectChannel(c.id13));
    listEl.appendChild(card);
  });
}

async function selectChannel(id13) {
  _channelSelected = id13;
  document.getElementById('balloon-selected').textContent = `CH ${id13}`;
  document.getElementById('balloon-map-label').textContent = `CH ${id13}`;
  renderChannelList();

  const detailEl = document.getElementById('balloon-detail');
  detailEl.innerHTML = `<div style="color:rgba(0,245,255,0.25);font-size:10px;padding:8px">Loading channel ${id13}…</div>`;
  try {
    const d = await fetch(`/api/balloons/channels/${encodeURIComponent(id13)}?days=120`).then(r => r.json());
    if (d.error) {
      detailEl.innerHTML = `<div style="color:#ff6600;padding:8px;font-size:10px">${d.error}</div>`;
      return;
    }
    renderChannelDetail(d);
    plotChannelTrack(d);
  } catch (err) {
    detailEl.innerHTML = `<div style="color:#ff6600;padding:8px;font-size:10px">${err}</div>`;
  }
}

function renderChannelDetail(d) {
  const detailEl = document.getElementById('balloon-detail');
  const track = d.track || [];
  const rows = track.slice(-15).reverse().map(p =>
    `${(p.timestamp || '').slice(5, 16)} · ${p.grid6} · ${p.altitude_m}m · ${p.temperature_c}°C · ${p.voltage_v}V · ${p.speed_knots}kn · GPS ${p.gps_valid ? '✓' : '✕'}`
  ).join('<br>');

  detailEl.innerHTML = `
    <div style="font-size:13px;color:#ffc832;letter-spacing:1px;margin-bottom:6px">
      <b>Channel ${d.id13}</b>
      <span style="font-size:10px;color:rgba(255,200,50,0.55);margin-left:8px">${d.coherent ? 'COHERENT' : 'NOISY — LIKELY MULTIPLE TRACKERS'}</span>
    </div>
    <div class="balloon-meta">
      ${d.frame_count} frames (${d.basic_count} Basic-decoded, ${d.extended_count} Extended/undecoded)
      · alt ${d.altitude_min_m ?? '—'}–${d.altitude_max_m ?? '—'}m
      ${d.coherent ? `· med ${d.median_kmh ?? '—'} / max ${d.max_kmh ?? '—'} km/h · span ${d.span_km != null ? Math.round(d.span_km) + ' km' : '—'}` : ''}
      <br>${(d.first_seen || '').slice(0, 16)} → ${(d.last_seen || '').slice(0, 16)} · ${(d.bands || []).join(' ')}
    </div>
    ${d.coherence_note ? `<div class="balloon-reasons" style="margin-top:6px;color:#ff9955">${d.coherence_note}</div>` : ''}
    ${rows ? `<div class="balloon-meta" style="margin-top:8px"><b>Decoded frames (newest first):</b><br>${rows}</div>` : ''}
  `;
  document.getElementById('balloon-map-sub').textContent =
    d.coherent ? `${d.basic_count} decoded frames · track` : `${d.basic_count} decoded frames · scattered, not a track`;
}

function plotChannelTrack(d) {
  ensureBalloonMap();
  if (!_balloonTrackLayer) return;
  _balloonTrackLayer.clearLayers();

  const track = d.track || [];
  if (!track.length) {
    _balloonMap.setView(qthLatLon, 3);
    return;
  }

  if (d.coherent && track.length >= 2) {
    const latlngs = track.map(p => [p.lat, p.lon]);
    L.polyline(latlngs, { color: '#c084fc', weight: 2.5, opacity: 0.85 }).addTo(_balloonTrackLayer);
  }

  track.forEach((p, i) => {
    const isLast = i === track.length - 1;
    L.circleMarker([p.lat, p.lon], {
      radius: isLast ? 6 : 4,
      color: '#c084fc',
      fillColor: isLast ? '#ffc832' : '#c084fc',
      fillOpacity: 0.8,
      weight: 1.5,
    }).bindPopup(`CH ${d.id13} · ${p.grid6}<br>${p.altitude_m}m · ${p.temperature_c}°C · ${p.voltage_v}V<br>${p.timestamp}`)
      .addTo(_balloonTrackLayer);
  });

  const last = track[track.length - 1];
  _balloonMap.setView([last.lat, last.lon], d.coherent ? 4 : 3);
}

async function refreshBalloons() {
  const listEl = document.getElementById('balloon-list');
  if (!listEl) return;
  try {
    const url = `/api/balloons?days=${_balloonDays}&telemetry=${_balloonIncludeTelem ? 1 : 0}`;
    const data = await fetch(url).then(r => r.json());
    _balloonList = data.balloons || [];
    _balloonCalls = new Set(_balloonList.map(b => cleanCall(b.call_clean || b.call)));
    document.getElementById('balloon-count').textContent = String(data.count || 0);
    document.getElementById('balloon-days').textContent = `${data.days || _balloonDays}d`;
    renderBalloonList();
    if (_balloonSelected) {
      const still = _balloonList.find(b => cleanCall(b.call) === cleanCall(_balloonSelected));
      if (still) selectBalloon(still.call_clean || still.call, false);
    }
  } catch (err) {
    listEl.innerHTML = `<div style="color:#ff6600;padding:12px;font-size:10px">Failed to load balloons: ${err}</div>`;
  }
}

function renderBalloonList() {
  const listEl = document.getElementById('balloon-list');
  if (!listEl) return;
  if (!_balloonList.length) {
    listEl.innerHTML = `<div style="color:rgba(0,245,255,0.25);padding:16px;text-align:center;font-size:10px">No suspects in the last ${_balloonDays}d</div>`;
    return;
  }
  listEl.innerHTML = '';
  _balloonList.forEach(b => {
    const card = document.createElement('div');
    const kind = b.kind || 'mover';
    const selected = _balloonSelected && cleanCall(_balloonSelected) === cleanCall(b.call);
    card.className = `balloon-card kind-${kind}${selected ? ' active' : ''}`;
    const last = b.last_spot || {};
    const when = last.timestamp ? new Date(last.timestamp).toISOString().slice(5, 16).replace('T', ' ') + 'Z' : '—';
    card.innerHTML = `
      <div class="balloon-card-top">
        <span class="balloon-call">${b.call}</span>
        <span class="balloon-score">SCORE ${b.score}</span>
      </div>
      <div class="balloon-meta">
        <span class="balloon-badge">${(kind || '').toUpperCase()}</span>
        <span class="balloon-badge">${(b.status || 'auto').toUpperCase()}</span>
        ${b.grid_count || 0} grids · ${b.spot_count || 0} spots
        · span ${b.span_km != null ? Math.round(b.span_km) + ' km' : '—'}
        · med ${b.median_kmh != null ? b.median_kmh + ' km/h' : '—'}
        <br>last ${when} · ${last.grid || '—'} · P${last.power != null ? last.power : '?'}
        ${(b.powers && b.powers.length) ? ` · powers [${b.powers.join(',')}]` : ''}
      </div>
      <div class="balloon-reasons">${(b.reasons || []).slice(0, 2).join(' · ')}</div>
      ${b.traquito_latest && b.traquito_latest.telemetry_type === 'basic' ? `
      <div class="balloon-meta" style="margin-top:4px;color:#c084fc">
        U4B alt ${b.traquito_latest.altitude_m}m · ${b.traquito_latest.temperature_c}°C · ${b.traquito_latest.voltage_v}V · ${b.traquito_latest.speed_knots}kn
      </div>` : ''}
    `;
    card.addEventListener('click', () => selectBalloon(b.call_clean || b.call));
    listEl.appendChild(card);
  });
}

async function selectBalloon(call, fetchDetail = true) {
  _balloonSelected = call;
  document.getElementById('balloon-selected').textContent = cleanCall(call);
  document.getElementById('balloon-map-label').textContent = cleanCall(call);
  renderBalloonList();
  if (!fetchDetail) return;

  const detailEl = document.getElementById('balloon-detail');
  detailEl.innerHTML = `<div style="color:rgba(0,245,255,0.25);font-size:10px;padding:8px">Loading ${cleanCall(call)}…</div>`;
  try {
    const d = await fetch(`/api/balloons/${encodeURIComponent(call)}?days=120`).then(r => r.json());
    if (d.error) {
      detailEl.innerHTML = `<div style="color:#ff6600;padding:8px;font-size:10px">${d.error}</div>`;
      return;
    }
    renderBalloonDetail(d);
    plotBalloonTrack(d);
  } catch (err) {
    detailEl.innerHTML = `<div style="color:#ff6600;padding:8px;font-size:10px">${err}</div>`;
  }
}

function renderBalloonDetail(d) {
  const detailEl = document.getElementById('balloon-detail');
  const alts = (d.altitude_hints || []).map(a =>
    `P${a.power_dbm} → Zachtek ~${a.zachtek_m}m / WB8ELK ~${a.wb8elk_m}m`
  ).join(' · ');
  const hops = (d.hops || []).slice(-8).map(h =>
    `${h.from}→${h.to} ${h.km}km / ${h.hours}h (${h.kmh} km/h)`
  ).join('<br>');
  const grids = (d.grids || []).join(' → ');

  detailEl.innerHTML = `
    <div style="font-size:13px;color:#ffc832;letter-spacing:1px;margin-bottom:6px">
      <b>${d.call}</b>
      <span style="font-size:10px;color:rgba(255,200,50,0.55);margin-left:8px">${(d.kind || '').toUpperCase()} · SCORE ${d.score} · ${(d.status || 'auto').toUpperCase()}</span>
    </div>
    <div class="balloon-meta">
      ${d.spot_count || 0} spots · ${d.grid_count || 0} grids · span ${d.span_km != null ? Math.round(d.span_km) + ' km' : '—'}
      · track ${d.total_track_km != null ? Math.round(d.total_track_km) + ' km' : '—'}
      · med ${d.median_kmh ?? '—'} / max ${d.max_kmh ?? '—'} km/h
      <br>${(d.first_seen || '').slice(0, 16)} → ${(d.last_seen || '').slice(0, 16)}
      ${(d.bands || []).length ? ' · ' + d.bands.join(' ') : ''}
    </div>
    <div class="balloon-reasons" style="margin-top:6px">${(d.reasons || []).join(' · ')}</div>
    ${alts ? `<div class="balloon-alt-hint">Altitude hints: ${alts}</div>` : ''}
    ${d.traquito_latest && d.traquito_latest.telemetry_type === 'basic' ? `
    <div class="balloon-meta" style="margin-top:8px;color:#c084fc;border-top:1px solid rgba(192,132,252,0.25);padding-top:6px">
      <b>U4B/Traquito Basic Telemetry</b> (channel ${d.traquito_channel}, decoded per-spot — no pairing needed)<br>
      Grid ${d.traquito_latest.grid6} · Alt ${d.traquito_latest.altitude_m}m · ${d.traquito_latest.temperature_c}°C
      · ${d.traquito_latest.voltage_v}V · ${d.traquito_latest.speed_knots}kn · GPS ${d.traquito_latest.gps_valid ? 'lock' : 'no lock'}
    </div>` : ''}
    ${d.kind === 'telemetry' && !(d.traquito_latest && d.traquito_latest.telemetry_type === 'basic') ? `<div class="encoding-note" style="margin-top:6px">Telemetry packet — plotted grids are often sensor encodings, not a flight path.</div>` : ''}
    ${grids && d.kind !== 'telemetry' ? `<div class="balloon-meta" style="margin-top:8px"><b>Track:</b> ${grids}</div>` : ''}
    ${hops && d.kind !== 'telemetry' ? `<div class="balloon-meta" style="margin-top:6px"><b>Recent hops:</b><br>${hops}</div>` : ''}
    <div class="balloon-actions">
      <button class="btn" onclick="flagBalloon('${cleanCall(d.call)}','watch')">★ WATCH</button>
      <button class="btn" onclick="flagBalloon('${cleanCall(d.call)}','confirmed')">✓ CONFIRM</button>
      <button class="btn" onclick="flagBalloon('${cleanCall(d.call)}','dismissed')">✕ DISMISS</button>
      <button class="btn" onclick="flagBalloon('${cleanCall(d.call)}','auto')">↺ AUTO</button>
      ${isLookupableCall(d.call) ? `<a class="btn" href="${qrzUrl(d.call)}" target="_blank" rel="noopener" style="text-decoration:none">QRZ</a>` : ''}
    </div>
  `;
  document.getElementById('balloon-map-sub').textContent =
    d.kind === 'telemetry' ? 'TELEMETRY (grids may be encoded)' :
    `${d.grid_count || 0} grids · ${d.span_km != null ? Math.round(d.span_km) + ' km span' : ''}`;
}

function plotBalloonTrack(d) {
  ensureBalloonMap();
  if (!_balloonTrackLayer) return;
  _balloonTrackLayer.clearLayers();

  const track = d.track || [];
  if (d.kind === 'telemetry' || track.length === 0) {
    if (d.traquito_latest && d.traquito_latest.telemetry_type === 'basic' && d.traquito_latest.grid6) {
      // Decoded U4B position (~3km resolution) — real, not encoded
      const ll = gridToLatLon(d.traquito_latest.grid6);
      L.circleMarker(ll, {
        radius: 6, color: '#c084fc', fillColor: '#c084fc', fillOpacity: 0.8, weight: 1.5,
      }).bindPopup(`${d.call}<br>U4B ${d.traquito_latest.grid6} · ${d.traquito_latest.altitude_m}m`).addTo(_balloonTrackLayer);
      _balloonMap.setView(ll, 5);
      return;
    }
    // Still show QTH; for telem optionally mark last reported "grid" faintly
    if (d.last_spot && d.last_spot.grid && d.last_spot.grid.length >= 4) {
      const ll = gridToLatLon(d.last_spot.grid);
      L.circleMarker(ll, {
        radius: 5, color: '#c084fc', fillColor: '#c084fc', fillOpacity: 0.5, weight: 1,
      }).bindPopup(`telem ${d.call}<br>${d.last_spot.grid} (may be encoded)`).addTo(_balloonTrackLayer);
    }
    _balloonMap.setView(qthLatLon, 3);
    return;
  }

  const latlngs = track.map(p => [p.lat, p.lon]);
  const line = L.polyline(latlngs, {
    color: '#ffc832',
    weight: 2.5,
    opacity: 0.85,
  }).addTo(_balloonTrackLayer);

  track.forEach((p, i) => {
    const isLast = i === track.length - 1;
    L.circleMarker([p.lat, p.lon], {
      radius: isLast ? 7 : 4,
      color: isLast ? '#fff' : '#ffc832',
      fillColor: isLast ? '#ffc832' : '#ffc832',
      fillOpacity: isLast ? 1 : 0.55,
      weight: isLast ? 2 : 1,
    }).bindPopup(
      `<div style="font-family:monospace;font-size:11px">` +
      `<b style="color:#ffc832">${d.call}</b> · ${p.grid}<br>` +
      `${(p.timestamp || '').slice(0, 19)}Z<br>` +
      `${p.band || ''} · P${p.power} · SNR ${p.snr}` +
      `</div>`
    ).addTo(_balloonTrackLayer);
  });

  try {
    _balloonMap.fitBounds(line.getBounds().pad(0.25));
  } catch (_) {
    _balloonMap.setView(latlngs[latlngs.length - 1], 4);
  }
}

async function flagBalloon(call, status) {
  try {
    await fetch(`/api/balloons/${encodeURIComponent(call)}/flag`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    await refreshBalloons();
    if (_balloonSelected) await selectBalloon(_balloonSelected);
  } catch (err) {
    console.warn('flagBalloon failed', err);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════════════════

(function init() {
  // Load initial WSPR data
  fetch('/api/status').then(r => r.json()).then(updateStatus).catch(() => {});
  fetch('/api/stats').then(r => r.json()).then(updateStats).catch(() => {});

  // Balloon suspect set first so historical spot rows get highlighted
  fetch('/api/balloons?days=45&telemetry=1')
    .then(r => r.json())
    .then(data => {
      _balloonList = data.balloons || [];
      _balloonCalls = new Set(_balloonList.map(b => cleanCall(b.call_clean || b.call)));
      return fetch('/api/spots').then(r => r.json());
    })
    .then(spots => {
      if (!spots) return;
      spots.reverse().forEach(spot => {
        addSpotRow(spot);
        addSpotToMap(spot);
      });
    })
    .catch(() => {
      // Fallback if balloons API fails — still load spots
      fetch('/api/spots')
        .then(r => r.json())
        .then(spots => {
          spots.reverse().forEach(spot => {
            addSpotRow(spot);
            addSpotToMap(spot);
          });
        })
        .catch(() => {});
    });

  // Space weather — fetch immediately, then poll every 5 min
  refreshSpaceWeather();

  // Greyline — start after map is ready
  setTimeout(startGreylineClock, 500);

  connectSSE();
})();
