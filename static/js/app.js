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
  document.getElementById('content-wefax').className    = (tab === 'wefax') ? 'active' : '';
  document.getElementById('content-prop').className     = (tab === 'prop')  ? 'active' : '';

  document.getElementById('tab-wspr').classList.toggle('active',  tab === 'wspr');
  document.getElementById('tab-wefax').classList.toggle('active', tab === 'wefax');
  document.getElementById('tab-prop').classList.toggle('active',  tab === 'prop');

  if (tab === 'wspr') {
    setTimeout(() => { if (window._map) window._map.invalidateSize(); }, 50);
  }
  if (tab === 'wefax') {
    loadWefaxGallery();
  }
  if (tab === 'prop') {
    refreshSpaceWeather();
    renderCharts();
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

const map = L.map('map', {
  center: [20, 0],
  zoom: 2,
  zoomControl: true,
  attributionControl: true,
});
window._map = map;

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
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
  const color = bandColor(spot.band);
  const key = `${spot.call}-${spot.timestamp}`;

  // Prune oldest if needed
  if (mapSpotQueue.length >= MAX_MAP_SPOTS) {
    const old = mapSpotQueue.shift();
    if (spotLayers[old]) {
      spotLayers[old].forEach(l => map.removeLayer(l));
      delete spotLayers[old];
    }
  }

  const line = L.polyline([qthLatLon, latLon], {
    color: color,
    weight: 1.5,
    opacity: 0.65,
    dashArray: '4 4',
  }).addTo(map);

  const marker = L.circleMarker(latLon, {
    radius: 5,
    color: color,
    fillColor: color,
    fillOpacity: 0.8,
    weight: 1,
  }).addTo(map)
    .bindPopup(
      `<div style="font-family:monospace;font-size:12px;background:#0d0d1a;color:#b0c4cc;border:1px solid ${color};padding:6px 10px">` +
      `<b style="color:${color}">${spot.call}</b> · ${spot.grid}<br>` +
      `Band: ${spot.band} · SNR: ${spot.snr} dB<br>` +
      `${spot.distance_km ? spot.distance_km.toLocaleString() + ' km' : 'dist?'} · ${spot.power} dBm` +
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

  const tr = document.createElement('tr');
  tr.className = 'row-new';
  tr.style.borderLeft = `3px solid ${color}`;
  tr.innerHTML = `
    <td class="td-time">${timeStr}</td>
    <td class="td-call" style="color:${color}">${spot.call}</td>
    <td>${spot.grid}</td>
    <td style="color:${color}">${spot.band}</td>
    <td class="td-snr">${spot.snr > 0 ? '+' : ''}${spot.snr}</td>
    <td class="td-dist">${distStr}</td>
    <td class="td-pwr">${spot.power}</td>
  `;

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
// WEFAX tab
// ═══════════════════════════════════════════════════════════════════════════════

const WEFAX_STATIONS = [
  { name: "NMG New Orleans (Gulf)",   freqs: [4.3179, 8.5039, 12.7895] },
  { name: "NMF Boston (N Atlantic)",  freqs: [4.235,  6.3405, 9.110,  12.750] },
  { name: "NMC Pt Reyes (Pacific)",   freqs: [4.346,  8.682,  12.786, 17.151] },
  { name: "NMN Chesapeake (S Atl)",   freqs: [6.3405, 8.080,  12.750] },
];

let _wfState       = 'IDLE';
let _wfPollTimer   = null;
let _wfImageTimer  = null;

// ── Build station / frequency selects ────────────────────────────────────────

function initWefaxSelects() {
  const stationSel = document.getElementById('wf-station');
  WEFAX_STATIONS.forEach((s, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = s.name;
    stationSel.appendChild(opt);
  });
  wfStationChanged();
}

function wfStationChanged() {
  const stationSel = document.getElementById('wf-station');
  const freqSel    = document.getElementById('wf-freq');
  const idx = parseInt(stationSel.value, 10);
  const station = WEFAX_STATIONS[idx];

  freqSel.innerHTML = '';
  station.freqs.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f;
    opt.textContent = f.toFixed(4) + ' MHz';
    freqSel.appendChild(opt);
  });
}

// ── Start / Stop button ───────────────────────────────────────────────────────

function wfToggle() {
  if (_wfState === 'IDLE' || _wfState === 'DONE' || _wfState === 'ERROR') {
    wfStartReceive();
  } else {
    wfStopReceive();
  }
}

function wfStartReceive() {
  const stationSel = document.getElementById('wf-station');
  const freqSel    = document.getElementById('wf-freq');
  const idx        = parseInt(stationSel.value, 10);
  const station    = WEFAX_STATIONS[idx].name;
  const freq_mhz   = parseFloat(freqSel.value);

  fetch('/api/wefax/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ freq_mhz, station }),
  })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        _wfState = d.state;
        wfUpdateUI(d);
        wfStartPolling();
        wfStartImageRefresh();
      }
    })
    .catch(() => {});
}

function wfStopReceive() {
  fetch('/api/wefax/stop', { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      // Polling will detect the DONE/ERROR state and clean up
    })
    .catch(() => {});
}

// ── Status polling (while active) ────────────────────────────────────────────

function wfStartPolling() {
  wfStopPolling();
  _wfPollTimer = setInterval(wfPollStatus, 2000);
}

function wfStopPolling() {
  if (_wfPollTimer) { clearInterval(_wfPollTimer); _wfPollTimer = null; }
}

function wfPollStatus() {
  fetch('/api/wefax/status')
    .then(r => r.json())
    .then(d => {
      _wfState = d.state;
      wfUpdateUI(d);
      if (d.state === 'IDLE' || d.state === 'DONE' || d.state === 'ERROR') {
        wfStopPolling();
        wfStopImageRefresh();
        if (d.state === 'DONE') loadWefaxGallery();
      }
    })
    .catch(() => {});
}

// ── Live image refresh ────────────────────────────────────────────────────────

function wfStartImageRefresh() {
  wfStopImageRefresh();
  _wfImageTimer = setInterval(wfRefreshImage, 3000);
}

function wfStopImageRefresh() {
  if (_wfImageTimer) { clearInterval(_wfImageTimer); _wfImageTimer = null; }
}

function wfRefreshImage() {
  if (_wfState !== 'PHASING' && _wfState !== 'RECEIVING') return;
  const img = document.getElementById('wf-live-img');
  img.src = '/api/wefax/image/current.png?t=' + Date.now();
  img.style.display = 'block';
  document.getElementById('wf-img-label').style.display = 'none';
}

// ── Update WEFAX UI from status dict ─────────────────────────────────────────

function wfUpdateUI(d) {
  const state  = d.state || 'IDLE';
  const badge  = document.getElementById('wf-state-badge');
  const btn    = document.getElementById('wf-btn');

  // Badge
  badge.textContent = state;
  badge.className   = `wf-badge-${state}`;

  // Button label / class
  const active = (state === 'PHASING' || state === 'RECEIVING');
  if (active) {
    btn.textContent = '⏹ STOP RECEIVE';
    btn.className   = 'stopping';
  } else {
    btn.textContent = '▶ START RECEIVE';
    btn.className   = '';
  }
  btn.disabled = false;

  // Stats
  document.getElementById('wf-lines').textContent =
    (d.progress_lines > 0) ? d.progress_lines : '---';

  if (d.elapsed_s != null) {
    const m = Math.floor(d.elapsed_s / 60);
    const s = d.elapsed_s % 60;
    document.getElementById('wf-elapsed').textContent =
      `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  } else {
    document.getElementById('wf-elapsed').textContent = '---';
  }

  document.getElementById('wf-freq-display').textContent =
    d.freq_mhz ? d.freq_mhz.toFixed(4) + ' MHz' : '---';

  // Image label
  const label = document.getElementById('wf-img-label');
  if (state === 'PHASING') {
    label.textContent = 'PHASING — WAITING FOR START TONE…';
    label.style.display = '';
  } else if (state === 'RECEIVING') {
    label.style.display = 'none';
  } else if (state === 'DONE') {
    label.textContent = 'RECEIVE COMPLETE';
    label.style.display = '';
  } else if (state === 'ERROR') {
    label.textContent = 'ERROR — CHECK LOGS';
    label.style.display = '';
  } else {
    label.textContent = 'IDLE — NO IMAGE';
    label.style.display = '';
    document.getElementById('wf-live-img').style.display = 'none';
  }
}

// ── Gallery ───────────────────────────────────────────────────────────────────

function loadWefaxGallery() {
  fetch('/api/wefax/gallery')
    .then(r => r.json())
    .then(images => renderWefaxGallery(images))
    .catch(() => {});
}

function renderWefaxGallery(images) {
  const el = document.getElementById('wf-gallery');
  if (!el) return;

  if (!images || images.length === 0) {
    el.innerHTML = '<span class="gallery-empty">NO IMAGES YET</span>';
    return;
  }

  el.innerHTML = '';
  images.forEach(img => {
    const thumb = document.createElement('div');
    thumb.className = 'gallery-thumb';
    thumb.title = `${img.station}\n${img.timestamp}\n${img.size_kb} kB`;
    thumb.onclick = () => window.open('/api/wefax/images/' + img.filename, '_blank');

    // Format timestamp for display
    let tsDisplay = img.timestamp ? img.timestamp.replace('T', ' ').slice(0, 16) : '';

    thumb.innerHTML = `
      <img src="/api/wefax/images/${img.filename}" alt="${img.station}" loading="lazy">
      <div class="gallery-thumb-info">${img.station || img.filename}<br>${tsDisplay}</div>
    `;
    el.appendChild(thumb);
  });
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
  drawGreyline();
}

function startGreylineClock() {
  drawGreyline();
  _greylineTimer = setInterval(drawGreyline, 60000);
}


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
  setText('sw-updated-ts', d.updated ? 'DATA: ' + d.updated : '---');

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


// ═══════════════════════════════════════════════════════════════════════════════
// CHARTS — SFI sparkline + WSPR vs K correlation
// ═══════════════════════════════════════════════════════════════════════════════

function renderCharts() {
  renderSfiChart();
  renderCorrChart();
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

    // Build hour-aligned K lookup
    const kMap = {};
    kdata.forEach(row => {
      if (!row.time) return;
      const hour = row.time.slice(0, 13) + ':00:00';
      kMap[hour] = row.kp;
    });

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

    // K-index line
    ctx.beginPath();
    ctx.strokeStyle = '#ff6600';
    ctx.lineWidth   = 2;
    ctx.shadowColor = '#ff6600';
    ctx.shadowBlur  = 6;
    let firstK = true;
    wspr.forEach((w, i) => {
      const hourKey = w.hour ? w.hour.slice(0, 13) + ':00:00' : null;
      const kp = hourKey ? kMap[hourKey] : null;
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
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width  = Math.floor(rect.width)  || 400;
  canvas.height = Math.floor(rect.height - 20) || 140;
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
// Bootstrap
// ═══════════════════════════════════════════════════════════════════════════════

(function init() {
  // Populate WEFAX station selector
  initWefaxSelects();

  // Load initial WSPR data
  fetch('/api/status').then(r => r.json()).then(updateStatus).catch(() => {});
  fetch('/api/stats').then(r => r.json()).then(updateStats).catch(() => {});

  // Seed the spot table with stored history
  fetch('/api/spots')
    .then(r => r.json())
    .then(spots => {
      // spots are newest-first from the API; addSpotRow inserts at top so
      // we reverse to get chronological insertion order, resulting in newest on top.
      spots.reverse().forEach(spot => {
        addSpotRow(spot);
        addSpotToMap(spot);
      });
    })
    .catch(() => {});

  // Poll initial WEFAX status (may already be active if server restarted mid-session)
  fetch('/api/wefax/status')
    .then(r => r.json())
    .then(d => {
      _wfState = d.state;
      wfUpdateUI(d);
      if (d.state === 'PHASING' || d.state === 'RECEIVING') {
        wfStartPolling();
        wfStartImageRefresh();
      }
    })
    .catch(() => {});

  // Space weather — fetch immediately, then poll every 5 min (done in refreshSpaceWeather interval above)
  refreshSpaceWeather();

  // Greyline — start after map is ready
  setTimeout(startGreylineClock, 500);

  connectSSE();
})();
