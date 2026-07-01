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
  '40m': '#ff6600',
  '30m': '#ffcc00',
  '20m': '#00f5ff',
  '17m': '#00ff88',
  '15m': '#ff0090',
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

  document.getElementById('content-wspr').style.display  = (tab === 'wspr')  ? '' : 'none';
  document.getElementById('content-wefax').className = (tab === 'wefax') ? 'active' : '';

  document.getElementById('tab-wspr').classList.toggle('active',  tab === 'wspr');
  document.getElementById('tab-wefax').classList.toggle('active', tab === 'wefax');

  if (tab === 'wspr') {
    // Map was hidden while WEFAX tab was shown — force a Leaflet relayout
    setTimeout(() => { if (window._map) window._map.invalidateSize(); }, 50);
  }
  if (tab === 'wefax') {
    loadWefaxGallery();
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

  connectSSE();
})();
