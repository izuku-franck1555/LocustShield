/* =====================================================================
   R0 Suitability Station — front-end logic
   Vanilla JS + Leaflet (CDN). Self-contained. No build step.

   Renders the "R0" environmental-suitability index (a closed-form
   function of air temperature and relative humidity) per grid cell.

   While the backend is offline, a synthetic mock FeatureCollection is
   generated client-side. Flip CONFIG.USE_MOCK to false to read the live
   API instead (see the API contract in the README / task brief).
   ===================================================================== */
'use strict';

/* =====================================================================
   CONFIG  ——  all editable domain copy + settings live here.
   The team lead localises the descriptive text later; keep keys stable.
   ===================================================================== */
const CONFIG = {
  /* --- Backend wiring --- */
  API_BASE: '',                 // same origin; '/api/...' is appended
  USE_MOCK: false,              // false → live backend API; true → synthetic field
  FALLBACK_TO_MOCK: false,      // a decision tool must show an explicit error on API
                                // failure — never silent synthetic data.
  mockLatency: 750,             // ms — simulated compute time for the loader (mock only)

  /* --- Brand / header copy — NEUTRAL PLACEHOLDER (team-lead injects the
         domain-specific product name + descriptor here, mirroring index.html). --- */
  brand: {
    title: 'LocustShield',
    sub:   'Biocontrol suitability for desert locust',
  },

  /* --- Smooth interpolated surface overlay (optional; rendered as an
         L.imageOverlay when the backend endpoint is live, else cell fallback). --- */
  USE_SURFACE: true,            // try GET /api/r0_surface; fall back to cells

  /* --- Continuous colour ramp — DIVERGING, anchored on the R0 = 1 decision
         break and tightened to the live data range (~0.75–1.05) so the
         variation actually reads instead of washing out. Colours align with
         the 5-class hues for consistency with the classified surface PNG.
         rampColor() interpolates blue→cream below 1.0 and cream→red above. --- */
  rampCenter: 1.0,              // diverging midpoint = the suitability threshold
  rampDomain: [0.8, 1.1],       // [low, high] clamp around the centre
  rampLow:  ['#2c7bb6', '#74add1', '#eef0c0'], // 0.8 → 1.0  (deep blue → cream)
  rampHigh: ['#f6e9a0', '#fdae61', '#d7191c'], // 1.0 → 1.1  (cream → orange → red)

  /* --- Neutral / technical labels (localised later) --- */
  labels: {
    index: 'Suitability index R0',
    temperature: 'Temperature',
    humidity: 'Relative humidity',
  },

  /* --- About panel copy --- */
  about: {
    lede:
      "LocustShield maps R0 — the basic reproduction number of the biocontrol " +
      "fungus Metarhizium acridum (the 'Green Muscle' biopesticide) in a desert-locust " +
      "population — computed as a closed-form function of monthly temperature and " +
      "relative humidity (Kamga et al. 2022). Where R0 > 1 (orange/red) the fungus can " +
      "establish and help suppress a locust outbreak; where R0 < 1 (blue) it is unlikely " +
      "to persist. Pick a region and month to see where and when a Green Muscle " +
      "application is climatically favourable. This is a relative suitability index for " +
      "planning, not a validated forecast; computed from live NASA POWER climate it " +
      "reproduces the published spatial pattern but reads lower in absolute terms (less " +
      "red) than the original 1 km study.",
    sourceNote:
      'Model: Kamga et al. 2022, Scientific Reports 12:7535 ' +
      '(doi.org/10.1038/s41598-022-11424-0).',
  },

  /* --- Caveats shown if a response omits meta.caveats --- */
  caveatsFallback: [
    'Relative suitability index for planning — not a validated forecast of locust presence or control success.',
    'Macroclimatic, monthly-mean basis (NASA POWER); sub-monthly or forecast use is an extrapolation.',
    'Reproduces the published spatial pattern but reads lower in absolute terms than the 1 km source study (live humidity differs).',
    'Parameters follow the reference R script (Kamga et al. 2022).',
  ],
};

/* =====================================================================
   Small utilities
   ===================================================================== */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];
const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/* Deterministic pseudo-random from coordinates (stable per cell) */
function hash2(x, y) {
  const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return s - Math.floor(s);
}

/* Colour helpers */
function hexToRgb(h) {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHex([r, g, b]) {
  const f = (v) => clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0');
  return `#${f(r)}${f(g)}${f(b)}`;
}
function lerp(a, b, t) { return a + (b - a) * t; }

// Interpolate within an array of hex stops by a 0..1 position.
function rampArray(stops, t) {
  const n = stops.length - 1;
  const x = clamp(t, 0, 1) * n;
  const i = Math.min(Math.floor(x), n - 1);
  const k = x - i;
  const ca = hexToRgb(stops[i]), cb = hexToRgb(stops[i + 1]);
  return rgbToHex([lerp(ca[0], cb[0], k), lerp(ca[1], cb[1], k), lerp(ca[2], cb[2], k)]);
}

// Diverging continuous ramp anchored on rampCenter (R0 = 1, the suitability
// threshold): blue → cream below the break, cream → red above it. The two
// arms are scaled independently so the cream sits exactly at R0 = 1.
function rampColor(r0) {
  const c = CONFIG.rampCenter;
  const [lo, hi] = CONFIG.rampDomain;
  if (r0 <= c) return rampArray(CONFIG.rampLow,  (r0 - lo) / (c - lo));
  return rampArray(CONFIG.rampHigh, (r0 - c) / (hi - c));
}

/* =====================================================================
   SYNTHETIC FIELD  ——  stands in for the backend until it is live.
   T and RH are smooth synthetic fields; R0 is a closed-form response.
   ===================================================================== */

// Region presets: [west, south, east, north]
const MOCK_REGIONS = [
  { code: 'KEN', name: 'Kenya',        level: 0, bbox: [33.9, -4.7, 41.9,  5.5] },
  { code: 'ETH', name: 'Ethiopia',     level: 0, bbox: [33.0,  3.4, 48.0, 14.9] },
  { code: 'SOM', name: 'Somalia',      level: 0, bbox: [40.9, -1.7, 51.4, 12.0] },
  { code: 'SDN', name: 'Sudan',        level: 0, bbox: [21.8,  8.7, 38.6, 22.2] },
  { code: 'ERI', name: 'Eritrea',      level: 0, bbox: [36.4, 12.4, 43.1, 18.0] },
  { code: 'YEM', name: 'Yemen',        level: 0, bbox: [42.5, 12.1, 54.5, 19.0] },
  { code: 'SAU', name: 'Saudi Arabia', level: 0, bbox: [34.5, 16.0, 55.7, 32.2] },
  { code: 'NER', name: 'Niger',        level: 0, bbox: [ 0.2, 11.7, 16.0, 23.5] },
  { code: 'MLI', name: 'Mali',         level: 0, bbox: [-12.3, 10.2, 4.3, 25.0] },
  { code: 'MRT', name: 'Mauritania',   level: 0, bbox: [-17.1, 14.7, -4.8, 27.3] },
];

// Backend-supplied class legend (colours are read FROM here, not hardcoded
// in the renderer). RdYlBu-reversed diverging scheme.
const MOCK_LEGEND = [
  { cls: 'lt08', label: 'R0 < 0.8',   color: '#2c7bb6' },
  { cls: '08_1', label: '0.8 – 1.0',  color: '#abd9e9' },
  { cls: 'eq1',  label: 'R0 ≈ 1.0',   color: '#ffffbf' },
  { cls: '1_11', label: '1.0 – 1.1',  color: '#fdae61' },
  { cls: 'ge11', label: 'R0 ≥ 1.1',   color: '#d7191c' },
];

function classify(r0) {
  if (r0 < 0.8)  return 'lt08';
  if (r0 < 0.99) return '08_1';
  if (r0 <= 1.01) return 'eq1';
  if (r0 <= 1.1) return '1_11';
  return 'ge11';
}

// Smooth synthetic temperature (°C) and relative humidity (%) fields.
function synthClimate(lon, lat, month) {
  const hemi = lat >= 0 ? 1 : -1;
  const seas = Math.cos((month - 7) * Math.PI / 6) * hemi; // +1 ~ warm season
  let t = 30
    + 7.5 * seas
    - 0.55 * (Math.abs(lat) - 12)
    + 3.2 * Math.sin(lon * 0.45 + 0.6)
    + 2.4 * Math.cos(lat * 0.7 - 0.3);
  let rh = 48
    - 0.85 * (t - 29)
    + 22 * Math.sin(lat * 0.4 + lon * 0.18 + month * 0.25)
    + 4.0 * seas;
  return { t: clamp(t, 8, 46), rh: clamp(rh, 3, 98) };
}

// Closed-form suitability index: thermal optimum × moisture response.
function r0FromClimate(t, rh) {
  const fT = Math.exp(-Math.pow((t - 32) / 9, 2));        // optimum near 32°C
  const fR = 0.42 + 0.58 / (1 + Math.exp(-(rh - 36) / 9)); // moisture sigmoid
  return 1.34 * fT * fR;
}

// Build a mock /api/r0 FeatureCollection for a region + time.
function buildMockField(region, params) {
  const bbox = region.bbox;
  const [w, s, e, n] = bbox;
  const area = (e - w) * (n - s);
  const step = clamp(Math.sqrt(area / 700), 0.35, 1.1);

  // Year offset nudges the historical field slightly off climatology.
  const yOff = params.mode === 'month' ? ((params.year - 2003) % 7) * 0.18 : 0;

  // Ellipse inscribed in bbox → defines the data extent / region shape.
  const cx = (w + e) / 2, cy = (s + n) / 2;
  const rx = (e - w) / 2 * 0.97, ry = (n - s) / 2 * 0.97;
  const inEllipse = (x, y) =>
    Math.pow((x - cx) / rx, 2) + Math.pow((y - cy) / ry, 2) <= 1;

  const features = [];
  for (let y = s; y < n - 1e-9; y += step) {
    for (let x = w; x < e - 1e-9; x += step) {
      const x0 = x, x1 = Math.min(x + step, e);
      const y0 = y, y1 = Math.min(y + step, n);
      const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;

      let props;
      const gap = hash2(mx * 3.3, my * 3.3) < 0.035;     // sparse sensor gaps
      if (!inEllipse(mx, my) || gap) {
        props = { r0: null, t: null, rh: null, cls: null };
      } else {
        const cm = synthClimate(mx, my, params.month);
        const t = cm.t + yOff * 2;
        const rh = clamp(cm.rh - yOff * 4, 3, 98);
        const r0 = +(r0FromClimate(t, rh)).toFixed(3);
        props = { r0, t: +t.toFixed(1), rh: +rh.toFixed(0), cls: classify(r0) };
      }
      features.push({
        type: 'Feature',
        properties: props,
        geometry: {
          type: 'Polygon',
          coordinates: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
        },
      });
    }
  }

  // Boundary polygon = the data ellipse (48-gon).
  const ring = [];
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * Math.PI * 2;
    ring.push([cx + rx * Math.cos(a), cy + ry * Math.sin(a)]);
  }

  return {
    type: 'FeatureCollection',
    features,
    boundary: { type: 'Polygon', coordinates: [ring] },
    legend: MOCK_LEGEND,
    bbox,
    legendMode: params.ramp,
    meta: {
      mode: params.mode,
      month: params.month,
      year: params.mode === 'month' ? params.year : null,
      source: 'Synthetic mock field · closed-form T × RH response',
      resolution: `${step.toFixed(2)}°`,
      n_cells: features.length,
      caveats: [
        'Synthetic demonstration field — generated from a closed-form T × RH response, not observed climate data.',
        'Grid cells are uniform rectangles; no land–sea mask or terrain correction is applied in this preview.',
        'R0 is a relative suitability index, not an absolute population growth rate.',
        'Classified colours use a fixed five-band scheme; the ≈ 1.0 band marks the suitability threshold.',
        'Historical mode reuses the climatological field with a small year-dependent offset in this mock.',
      ],
    },
  };
}

// Mock occurrence points — clustered in the more suitable cells.
function buildMockOccurrences(region, field) {
  const pts = [];
  const cells = (field ? field.features : []).filter(
    (f) => f.properties.r0 != null && f.properties.r0 >= 1.02);
  for (let i = 0; i < cells.length && pts.length < 55; i++) {
    const f = cells[i];
    const r = f.geometry.coordinates[0];
    if (hash2(r[0][0] * 9.1, r[0][1] * 7.3) > 0.6) {
      const lon = lerp(r[0][0], r[2][0], hash2(r[0][0], r[0][1] + i));
      const lat = lerp(r[0][1], r[2][1], hash2(r[0][1] + i, r[0][0]));
      pts.push({
        type: 'Feature',
        properties: { date: `obs-${(i % 12) + 1}` },
        geometry: { type: 'Point', coordinates: [lon, lat] },
      });
    }
  }
  return { type: 'FeatureCollection', features: pts };
}

/* =====================================================================
   API layer  ——  live fetch with graceful mock fallback
   ===================================================================== */
async function apiGet(path) {
  const res = await fetch(CONFIG.API_BASE + path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function getData(path, mockFn, latency = CONFIG.mockLatency) {
  if (CONFIG.USE_MOCK) { await sleep(latency); return mockFn(); }
  try {
    return await apiGet(path);
  } catch (err) {
    console.warn(`[R0] live call failed (${path}): ${err.message}`);
    if (CONFIG.FALLBACK_TO_MOCK) { await sleep(latency); return mockFn(); }
    throw err;
  }
}

const api = {
  regions: () =>
    getData('/api/regions', () => ({ presets: MOCK_REGIONS }), 120),

  r0: (region, params) => {
    const q = new URLSearchParams({
      region: region.code, level: 0, mode: params.mode,
      month: params.month, ramp: params.ramp,
    });
    if (params.mode === 'month') q.set('year', params.year);
    return getData(`/api/r0?${q}`, () => buildMockField(region, params));
  },

  occurrences: (region, field) =>
    getData(`/api/occurrences?region=${region.code}`,
      () => buildMockOccurrences(region, field), 350),
};

/* =====================================================================
   Application state + map
   ===================================================================== */
const state = {
  regions: [],
  region: null,
  mode: 'climatology',
  month: 1,
  year: 2024,
  view: 'cells',       // 'cells' = discrete cells (grid) · 'smooth' = vivid 5-class surface
  showOcc: false,
  field: null,         // last /api/r0 response
  legendMap: {},       // cls -> color
  surfaceActive: false,// true when the smooth surface overlay is the visible layer (continuous mode)
  surface: null,       // cached smooth CONTINUOUS surface (classified uses discrete cells)
  labelsOn: false,     // basemap place-labels toggle
};

let map, cellLayer = null, boundaryLayer = null, occLayer = null,
    surfaceLayer = null, labelsLayer = null;

/* ONE shared value-probe tooltip for the whole cell layer. We manage a single
   L.tooltip instance ourselves (never per-feature) so it is always replaced and
   removed — it can never accumulate. A short hide delay prevents flicker when
   the cursor crosses from one cell to the next. */
let probeTip = null, probeHideTimer = null;
function showProbe(latlng, html) {
  if (!probeTip) {
    probeTip = L.tooltip({ className: 'cell-tip', direction: 'top', offset: [0, -6], opacity: 1 });
  }
  probeTip.setContent(html).setLatLng(latlng);
  if (map && !map.hasLayer(probeTip)) probeTip.addTo(map);
}
function hideProbe() {
  if (probeTip && map && map.hasLayer(probeTip)) probeTip.remove();
}

function initMap() {
  map = L.map('map', { zoomControl: true, attributionControl: true, minZoom: 2 })
    .setView([12, 30], 4);

  // Clean, near-white basemap WITHOUT labels — the data is the figure.
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd',
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · ' +
      '&copy; <a href="https://carto.com/attributions">CARTO</a>',
  }).addTo(map);

  // Place-labels overlay in a dedicated pane ABOVE the data so area names stay
  // readable over the surface/cells (ON by default; toggle to hide).
  map.createPane('labelsPane');
  map.getPane('labelsPane').style.zIndex = 550;       // above surface/cells (400), below markers (600)
  map.getPane('labelsPane').style.pointerEvents = 'none';
  labelsLayer = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', maxZoom: 19, pane: 'labelsPane', opacity: 0.95,
  });

  L.control.scale({ imperial: false, position: 'bottomright', maxWidth: 140 }).addTo(map);

  // Cursor coordinate readout
  const readout = $('#cursorReadout');
  map.on('mousemove', (ev) => {
    readout.classList.add('is-on');
    $('#curLat').textContent = fmtLat(ev.latlng.lat);
    $('#curLon').textContent = fmtLon(ev.latlng.lng);
  });
  map.on('mouseout', () => readout.classList.remove('is-on'));

  // Keep the map in sync when the viewport changes size (otherwise Leaflet's
  // panes desync — the surface/cells and the boundary outline can drift apart).
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (!map) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      map.invalidateSize({ animate: false });
      if (state.field && state.field.bbox) {
        const [w, s, e, n] = state.field.bbox;
        map.fitBounds([[s, w], [n, e]], { padding: [28, 28], animate: false });
      }
    }, 160);
  });
}

/* Basemap place-labels toggle */
function setLabels(on) {
  state.labelsOn = on;
  const btn = $('#labelsToggle');
  if (btn) btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  if (!labelsLayer || !map) return;
  if (on) { labelsLayer.addTo(map); refreshLabels(); }
  else labelsLayer.remove();
}

/* Keep the labels overlay on top and at full opacity. Tiles added to a custom
   pane during a fit/zoom animation can freeze mid-fade (e.g. ~0.28), so we
   re-assert opacity once now and again after the map settles. */
function refreshLabels() {
  if (!labelsLayer || !map || !state.labelsOn) return;
  labelsLayer.bringToFront();   // opacity forced to full via CSS (.leaflet-labels-pane tiles)
}

function fmtLat(v) {
  return `${Math.abs(v).toFixed(2)}°${v >= 0 ? 'N' : 'S'}`;
}
function fmtLon(v) {
  return `${Math.abs(v).toFixed(2)}°${v >= 0 ? 'E' : 'W'}`;
}

/* =====================================================================
   Smooth interpolated surface overlay (optional).
   Fetches GET /api/r0_surface (same params as /api/r0) and returns the
   parsed payload, or null on any failure so the caller falls back to
   painting cells. This is additive presentation — it never alters the
   /api/r0 wiring that drives hover/query and the legend.
   ===================================================================== */
async function fetchSurface(region, params) {
  if (CONFIG.USE_MOCK || !CONFIG.USE_SURFACE) return null;
  try {
    const q = new URLSearchParams({
      region: region.code, level: 0, mode: params.mode,
      month: params.month, ramp: params.ramp,
    });
    if (params.mode === 'month') q.set('year', params.year);
    const res = await fetch(`${CONFIG.API_BASE}/api/r0_surface?${q}`, {
      headers: { Accept: 'application/json' },
    });
    if (!res.ok) return null;
    const j = await res.json();
    if (!j || !j.image || !j.bounds) return null;
    return j;
  } catch (err) {
    console.warn('[R0] smooth surface unavailable, using cells:', err.message);
    return null;
  }
}

/* =====================================================================
   Cell styling + rendering
   When a smooth surface overlay is shown, cells become a transparent
   hover/query layer (pointer events kept alive via the .r0-cell class).
   ===================================================================== */
function styleCell(feature) {
  const p = feature.properties;
  const surf = state.surfaceActive;    // Smooth view → cells are the transparent probe layer
  // Cells view shows a visible grid: a thin hairline border on every 0.5° cell.
  const grid = { weight: surf ? 0 : 0.6, stroke: !surf, color: 'rgba(24,34,44,0.28)', lineJoin: 'round' };
  if (p.r0 == null) {
    return { className: 'r0-cell', ...grid, fillColor: '#e7ebee', fillOpacity: surf ? 0 : 0.5 };
  }
  const fill = state.legendMap[p.cls] || '#cccccc';   // always the 5-class colour
  return { className: 'r0-cell', ...grid, fillColor: fill, fillOpacity: surf ? 0 : 0.92 };
}

function cellTooltip(p) {
  if (p.r0 == null) return '<span class="cell-tip__null">no data</span>';
  return `<span class="cell-tip__r0">R₀ ${p.r0.toFixed(2)}</span>` +
    ` · T ${p.t.toFixed(1)}°C · RH ${Math.round(p.rh)}%`;
}

function renderField(data, surface) {
  state.field = data;
  state.legendMap = {};
  (data.legend || []).forEach((l) => { state.legendMap[l.cls] = l.color; });
  state.surface = surface || null;
  const haveSurface = !!(surface && surface.image && surface.bounds);
  // View design: CELLS = discrete opaque cells with a visible grid (one colour
  // per 0.5° cell); SMOOTH = the vivid 5-class surface PNG (the paper look). So
  // the surface overlay is shown ONLY in Smooth view; Cells view paints cells.
  state.surfaceActive = haveSurface && state.view === 'smooth';

  // Clear prior layers (and any open probe)
  clearTimeout(probeHideTimer); hideProbe();
  if (surfaceLayer)  { surfaceLayer.remove();  surfaceLayer = null; }
  if (cellLayer)     { cellLayer.remove();     cellLayer = null; }
  if (boundaryLayer) { boundaryLayer.remove(); boundaryLayer = null; }

  // Smooth interpolated surface (continuous mode only) — added below the cells
  // so the transparent cells stay the hover/probe layer on top.
  if (state.surfaceActive) {
    surfaceLayer = L.imageOverlay(surface.image, surface.bounds, {
      opacity: 1, interactive: false, className: 'r0-surface',
    }).addTo(map);
  }

  // Cells — painted when no surface; transparent hover/probe geometry when a
  // surface is shown. NO per-feature tooltips and NO bringToFront. Instead, a
  // SINGLE shared probe (managed above) is updated on the layer's mousemove and
  // removed on mouseout — one element, always replaced, so it cannot accumulate.
  cellLayer = L.geoJSON(data, { style: styleCell }).addTo(map);
  cellLayer.on('mousemove', (e) => {
    const lyr = e.layer || e.propagatedFrom;
    const p = lyr && lyr.feature && lyr.feature.properties;
    if (!p) return;
    clearTimeout(probeHideTimer);
    showProbe(e.latlng, cellTooltip(p));
  });
  // Delay the hide so crossing from one cell to the next (mouseout→mousemove)
  // doesn't flicker; a real exit has no following mousemove, so it hides.
  cellLayer.on('mouseout', () => {
    clearTimeout(probeHideTimer);
    probeHideTimer = setTimeout(hideProbe, 60);
  });

  // Boundary outline — crisp dark stroke that defines the figure
  if (data.boundary) {
    boundaryLayer = L.geoJSON(data.boundary, {
      interactive: false,
      style: { color: '#1b2733', weight: 1.4, opacity: 0.92, fill: false, lineJoin: 'round' },
    }).addTo(map);
  }

  // Fit to bbox — invalidate first so we always fit to the CURRENT container
  // size, and re-fit on the next frame once layout has settled (guards against
  // the panel/window having resized since map init, which otherwise leaves the
  // image overlay and the vector boundary on mismatched pixel origins).
  if (data.bbox) {
    const [w, s, e, n] = data.bbox;
    const fit = () => {
      map.invalidateSize({ animate: false });
      map.fitBounds([[s, w], [n, e]], { padding: [28, 28], animate: false });
    };
    fit();
    requestAnimationFrame(fit);
  }

  renderLegend(data);
  renderMeta(data.meta);
  renderCaveats(data.meta);
  updateContext(data.meta);

  // Keep place-labels overlay on top of freshly added layers (and un-freeze fade)
  refreshLabels();

  // Re-apply / refresh occurrence overlay if toggled on
  if (state.showOcc) loadOccurrences();
}

/* Live context readout (top bar + legend caption) */
function updateContext(meta) {
  const region = state.region ? state.region.name : '—';
  const modeLabel = (meta && meta.mode === 'month') ? 'Historical' : 'Climatology';
  const monthName = MONTHS[((meta && meta.month) || state.month || 1) - 1];
  const year = meta && meta.year != null ? meta.year : (state.mode === 'month' ? state.year : null);
  const timeStr = year != null ? `${MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1]} ${year}`
                                : MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1];

  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#ctxRegionV', region);
  set('#ctxModeV', modeLabel);
  set('#ctxTimeV', timeStr);
  set('#sourceLine', meta && meta.source ? `Source — ${meta.source}` : 'Source — awaiting field');

  // Legend caption
  set('#legendTime', (year != null ? `${MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1]} ${year}`
                                    : monthName));
  set('#legendRegion', region);
}

/* =====================================================================
   Legend, metadata, caveats
   ===================================================================== */
function renderLegend(data) {
  const box = $('#legendBox');
  const list = $('#legendList');
  const cont = $('#legendContinuous');
  const note = $('#legendNote');
  box.hidden = false;
  list.innerHTML = '';

  const hasNull = data.features.some((f) => f.properties.cls == null);

  // Classified swatches, read straight from the response legend.
  (data.legend || []).forEach((l) => {
    const isThr = l.cls === 'eq1';
    const li = document.createElement('li');
    li.className = 'map-legend__row' + (isThr ? ' is-threshold' : '');
    li.innerHTML =
      `<span class="map-legend__swatch" style="background:${l.color}"></span>` +
      `<span class="map-legend__rowlabel">R<sub>0</sub> ${l.label}</span>`;
    list.appendChild(li);
  });
  if (hasNull) {
    const li = document.createElement('li');
    li.className = 'map-legend__row';
    li.innerHTML =
      `<span class="map-legend__swatch map-legend__swatch--null"></span>` +
      `<span class="map-legend__rowlabel">no data</span>`;
    list.appendChild(li);
  }

  // Both views (Cells + Smooth) share the same 5-class classified legend.
  if (cont) cont.hidden = true;
  list.style.display = '';
  if (note) note.hidden = false;
}

function renderMeta(meta) {
  if (!meta) return;
  const box = $('#metaBox');
  const grid = $('#metaGrid');
  box.hidden = false;
  const rows = [
    ['Mode', meta.mode === 'month' ? 'Historical' : 'Climatology'],
    ['Month', MONTHS[(meta.month || 1) - 1]],
    ['Year', meta.year != null ? meta.year : '—'],
    ['Resolution', meta.resolution || '—'],
    ['Cells', meta.n_cells != null ? meta.n_cells.toLocaleString() : '—'],
    ['Source', meta.source || '—'],
  ];
  grid.innerHTML = rows
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`)
    .join('');
}

function renderCaveats(meta) {
  $('#aboutLede').textContent = CONFIG.about.lede;
  $('#aboutSource').textContent = (meta && meta.source ? `Source — ${meta.source}. ` : '') + CONFIG.about.sourceNote;
  const list = $('#caveatList');
  const caveats = (meta && meta.caveats && meta.caveats.length) ? meta.caveats : CONFIG.caveatsFallback;
  list.innerHTML = caveats.map((c) => `<li>${c}</li>`).join('');
}

/* =====================================================================
   Occurrence overlay
   ===================================================================== */
async function loadOccurrences() {
  if (occLayer) { occLayer.remove(); occLayer = null; }
  if (!state.showOcc || !state.region) return;
  try {
    const fc = await api.occurrences(state.region, state.field);
    occLayer = L.geoJSON(fc, {
      pointToLayer: (feat, latlng) =>
        L.marker(latlng, {
          icon: L.divIcon({ className: 'occ-icon', html: '<span class="occ-dot"></span>',
            iconSize: [11, 11], iconAnchor: [5.5, 5.5] }),
        }).bindTooltip('Observed occurrence', { className: 'cell-tip', direction: 'top' }),
    }).addTo(map);
  } catch (err) {
    console.warn('[R0] occurrence load failed:', err.message);
  }
}

/* =====================================================================
   Loader
   ===================================================================== */
function showLoader(msg, isError) {
  $('#loaderText').textContent = msg || 'Computing suitability field…';
  const el = $('#loader');
  el.classList.toggle('is-error', !!isError);
  el.hidden = false;
}
function hideLoader() { const el = $('#loader'); el.hidden = true; el.classList.remove('is-error'); }

/* =====================================================================
   Compute (fetch /api/r0)
   ===================================================================== */
let computing = false;
async function compute() {
  if (computing || !state.region) return;
  computing = true;
  const btn = $('#computeBtn');
  btn.disabled = true;
  showLoader(`Computing ${state.region.name} · ${MONTHS_SHORT[state.month - 1]}…`);
  try {
    const params = { mode: state.mode, month: state.month, year: state.year, ramp: 'bins' };
    // Cells (authoritative for hover/query + legend) plus the smooth 5-class
    // surface (the vivid BINS PNG) are fetched concurrently. Cells view paints
    // discrete cells; Smooth view shows the surface — both share the 5 classes.
    const [data, surface] = await Promise.all([
      api.r0(state.region, params),
      fetchSurface(state.region, params),
    ]);
    renderField(data, surface);
  } catch (err) {
    console.error('[R0] compute failed:', err);
    showLoader('Could not compute the field — check the backend connection, then retry.', true);
    setTimeout(hideLoader, 2600);
    computing = false; btn.disabled = false;
    return;
  }
  hideLoader();
  computing = false;
  btn.disabled = false;
}

/* =====================================================================
   Controls wiring
   ===================================================================== */
function updateComputeMeta() {
  const m = state.mode === 'month' ? `historical ${state.year}` : 'climatology';
  $('#computeMeta').textContent = `${m} · ${MONTHS_SHORT[state.month - 1]}`;
}

function setMode(mode) {
  state.mode = mode;
  $$('#modeToggle .segmented__opt').forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const yearField = $('#yearField');
  const yearInput = $('#yearInput');
  const historical = mode === 'month';
  yearField.classList.toggle('is-disabled', !historical);
  yearInput.disabled = !historical;
  $('#yearHint').textContent = historical ? `1981 – 2025` : 'climatology — disabled';
  updateComputeMeta();
}

function setView(view) {
  state.view = view;
  $$('#viewToggle .segmented__opt').forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  if (!cellLayer) return;  // nothing rendered yet

  // SMOOTH shows the vivid 5-class surface (cached at compute) over transparent
  // cells; CELLS removes the surface and paints discrete cells with a grid.
  const s = state.surface;
  const haveSurface = !!(s && s.image && s.bounds);
  state.surfaceActive = haveSurface && view === 'smooth';
  if (state.surfaceActive) {
    if (surfaceLayer) {
      surfaceLayer.setOpacity(1);
    } else {
      surfaceLayer = L.imageOverlay(s.image, s.bounds, {
        opacity: 1, interactive: false, className: 'r0-surface',
      }).addTo(map);
      cellLayer.bringToFront();           // keep transparent cells on top for hover
    }
  } else if (surfaceLayer) {
    surfaceLayer.remove(); surfaceLayer = null;  // cells view → discrete painted cells
  }
  refreshLabels();

  cellLayer.setStyle(styleCell);          // transparent when surface active, else painted + grid
  renderLegend(state.field);
}

function setMonth(m) {
  state.month = m;
  $('#monthValue').textContent = MONTHS[m - 1];
  const range = $('#monthRange');
  const pct = ((m - 1) / 11) * 100;
  range.style.setProperty('--p', `${pct}%`);
  updateComputeMeta();
}

function wireControls() {
  // Region
  const sel = $('#regionSelect');
  sel.addEventListener('change', () => {
    state.region = state.regions.find((r) => r.code === sel.value) || state.regions[0];
    compute(); // region change recomputes the field
  });

  // Basemap place-labels toggle
  const labelsBtn = $('#labelsToggle');
  if (labelsBtn) labelsBtn.addEventListener('click', () => setLabels(!state.labelsOn));

  // Mode
  $$('#modeToggle .segmented__opt').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));

  // View (Cells / Smooth)
  $$('#viewToggle .segmented__opt').forEach((b) =>
    b.addEventListener('click', () => setView(b.dataset.view)));

  // Month
  const range = $('#monthRange');
  range.addEventListener('input', () => setMonth(parseInt(range.value, 10)));

  // Year
  const yearInput = $('#yearInput');
  yearInput.addEventListener('change', () => {
    state.year = clamp(parseInt(yearInput.value, 10) || 2024, 1981, 2025);
    yearInput.value = state.year;
    updateComputeMeta();
  });

  // Occurrences
  $('#occToggle').addEventListener('change', (e) => {
    state.showOcc = e.target.checked;
    loadOccurrences();
  });

  // Compute
  $('#computeBtn').addEventListener('click', compute);

  // About
  const aboutToggle = $('#aboutToggle');
  aboutToggle.addEventListener('click', () => {
    const open = aboutToggle.getAttribute('aria-expanded') === 'true';
    aboutToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    $('#aboutBody').hidden = open;
  });

  // Month ticks
  $('#monthTicks').innerHTML =
    ['Jan', 'Apr', 'Jul', 'Oct', 'Dec'].map((m) => `<span>${m}</span>`).join('');
}

/* =====================================================================
   Bootstrap
   ===================================================================== */
async function init() {
  initMap();
  wireControls();

  // Inject NEUTRAL placeholder brand copy from CONFIG (team-lead localises later).
  const titleEl = $('#appTitle'); if (titleEl && CONFIG.brand) titleEl.textContent = CONFIG.brand.title;
  const subEl = $('#appSub');     if (subEl && CONFIG.brand) subEl.innerHTML = CONFIG.brand.sub.replace(/R0\b/, 'R<sub>0</sub>');

  setMonth(1);
  setMode('climatology');
  setView('cells');
  setLabels(true);     // place-labels ON by default (toggle to hide)

  // Populate regions
  try {
    const res = await api.regions();
    state.regions = res.presets || [];
  } catch (err) {
    console.error('[R0] could not load regions:', err.message);
    state.regions = MOCK_REGIONS;
  }
  const sel = $('#regionSelect');
  sel.innerHTML = state.regions
    .map((r) => `<option value="${r.code}">${r.name}</option>`)
    .join('');
  state.region = state.regions[0];
  if (state.region) {
    sel.value = state.region.code;
    $('#ctxRegionV').textContent = state.region.name;
  }
  renderCaveats(null); // seed About copy before first compute

  // First field
  await compute();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
