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

  /* --- Climate source + timing ---------------------------------------
         The real capability list comes from GET /api/sources. `sourcesFallback`
         is ONLY the degrade path for a server that predates that endpoint — in
         that case the high-resolution option is shown *disabled* rather than
         offered and silently served from the coarse source.
         A first high-resolution compute reads large rasters, so the client
         timeout has to sit well above the ~15-60 s that costs. --- */
  FETCH_TIMEOUT: 240000,        // ms — compute calls (/api/r0, /api/r0_surface)
  POINT_TIMEOUT: 12000,         // ms — hover probe (/api/r0_point)
  PROBE_DEBOUNCE: 160,          // ms — hover debounce before a point request
  sourcesFallback: [
    {
      id: 'power', label: 'NASA POWER', resolution: '~55 km',
      supports_climatology: true, year_min: 1981, year_max: 2025,
      notes: 'Monthly means on a coarse global grid.',
    },
  ],

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
      "planning, not a validated forecast. Maps are computed on demand at ~1 km from " +
      "CHELSA V2.1, falling back to NASA POWER (~55 km) for periods CHELSA does not " +
      "cover; the map always reports the source it actually used.",
    sourceNote:
      'Model: Kamga et al. 2022, Scientific Reports 12:7535 ' +
      '(doi.org/10.1038/s41598-022-11424-0).',
  },

  /* --- Caveats shown if a response omits meta.caveats --- */
  caveatsFallback: [
    'Relative suitability index for planning — not a validated forecast of locust presence or control success.',
    'Macroclimatic, monthly-mean basis; sub-monthly or forecast use is an extrapolation.',
    'Climate: CHELSA V2.1 ~1 km (climatology 1981–2010; monthly 1979–2018, plus 2019 Jan–Apr), with NASA POWER ~55 km used automatically for periods CHELSA does not cover.',
    'The published figures show large areas in the top class (R0 ≥ 1.1). That class requires relative humidity above ~93%; measured monthly-mean humidity reaches only ~75% (CHELSA) / ~83% (NASA POWER), so it does not occur here. The spatial pattern reproduces the published maps; the absolute class values do not.',
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
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/* Per-source UI framing. Everything factual (label, resolution, year span,
   climatology support) is read from /api/sources — this only supplies the
   speed/detail trade-off wording the capability list does not carry. */
const SOURCE_UI = {
  chelsa: { prefix: 'High resolution', order: 0 },
  power:  { prefix: 'Fast',            order: 1 },
};

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
/* fetch + an explicit timeout. The browser default is far too eager for a first
   high-resolution compute (raster read + R0 + PNG), so every call carries its
   own budget: long for compute, short for the hover probe. */
async function apiGet(path, timeout = CONFIG.FETCH_TIMEOUT, signal) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(new Error('timeout')), timeout);
  if (signal) signal.addEventListener('abort', () => ctl.abort(), { once: true });
  try {
    const res = await fetch(CONFIG.API_BASE + path, {
      headers: { Accept: 'application/json' }, signal: ctl.signal,
    });
    if (!res.ok) {
      // Carry the server's own explanation: a 4xx here is usually a precise
      // refusal ("that year is outside this source") that the UI should relay
      // verbatim rather than replace with a generic failure.
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (_) { /* not JSON */ }
      const err = new Error(detail || `HTTP ${res.status}`);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/* A definite server-side refusal (this query cannot be served by this source),
   as opposed to a transport failure or a missing endpoint. */
function isRefusal(err) {
  return !!(err && err.status >= 400 && err.status < 500 && err.status !== 404);
}

/* Query string shared by /api/r0, /api/r0_surface and /api/r0_point.
   `source` and `scale` are additive: a backend that predates them ignores the
   extra params, which is exactly the graceful-degrade path we want. */
function queryFor(region, params, extra) {
  const q = new URLSearchParams({ level: 0, mode: params.mode, month: params.month });
  // A single country keeps using `region=` — that path is byte-compatible and
  // must not regress. `regions=` is only sent when there really are several.
  if (params.regions && params.regions.length > 1) q.set('regions', params.regions.join(','));
  else q.set('region', (region && region.code) || params.regions[0]);
  if (params.mode === 'month') q.set('year', params.year);
  if (params.ramp)   q.set('ramp', params.ramp);
  if (params.source) q.set('source', params.source);
  if (params.scale)  q.set('scale', params.scale);
  Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v));
  return q;
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

  /* Runtime config (basemap credentials). The key is served at runtime and is
     never stored in this file — see loadBasemapConfig. */
  config: () => apiGet('/api/config', 5000),

  /* Capability list. A server without this endpoint is not an error — it means
     only the coarse source exists, and the UI says so rather than guessing. */
  sources: () => apiGet('/api/sources', 15000),

  /* Pre-flight for ONE time slice: which source would serve it, and why not the
     other. Renders nothing and returns instantly, so the UI can warn about a
     fallback BEFORE the user pays for a multi-second compute. It runs the
     backend's own resolution logic, so its verdict cannot drift from what the
     render endpoints actually do — that is why it, not a client-side rule, is
     the authority for which source we request. */
  coverage: (mode, month, year, source, regions) => {
    const q = new URLSearchParams({ mode, month, source });
    if (mode === 'month') q.set('year', year);
    if (regions && regions.length) q.set('regions', regions.join(','));
    return apiGet(`/api/coverage?${q}`, 15000);
  },

  r0: (region, params) =>
    getData(`/api/r0?${queryFor(region, params)}`, () => buildMockField(region, params)),

  /* Single-point value probe — the hover readout when a raster is displayed and
     there is no per-cell geometry to hit-test. */
  point: (region, params, lat, lon, signal) =>
    apiGet(
      `/api/r0_point?${queryFor(region, params, {
        lat: lat.toFixed(4), lon: lon.toFixed(4),
      })}`,
      CONFIG.POINT_TIMEOUT, signal),

  occurrences: (region, field) =>
    getData(`/api/occurrences?region=${region.code}`,
      () => buildMockOccurrences(region, field), 350),
};

/* =====================================================================
   Application state + map
   ===================================================================== */
const state = {
  regions: [],
  region: null,       // FIRST selected region object — occurrences + single-region paths
  selected: [],       // selected region codes, kept in preset order for stable display
  mode: 'climatology',
  month: 1,
  year: 2024,
  view: 'cells',       // 'cells' = discrete cells (grid) · 'smooth' = the surface raster
  source: 'auto',      // 'auto' | a capability id ('chelsa' | 'power')
  scale: 'absolute',   // 'absolute' = fixed 5 classes · 'relative' = per-map stretch
  showOcc: false,
  field: null,         // last /api/r0 response (cell geometry) — null for a raster-only source
  legendMap: {},       // cls -> color
  surfaceActive: false,// true when the surface raster is the visible layer
  surface: null,       // cached surface raster response
  labelsOn: false,     // basemap place-labels toggle
  hasResult: false,    // a field has been computed at least once (dismisses the welcome state)
  dirty: false,        // a query param changed since the last compute → result is stale

  /* --- source capabilities + provenance of the DISPLAYED result --- */
  sources: [],         // capability list from /api/sources
  sourcesLive: false,  // the endpoint answered (false → only the fallback list)
  requestedSource: null, // the source id asked for on the last compute
  servedSource: null,  // the source id the response says produced the map
  provNote: null,      // { text, warn } — a substitution the user must see
  pointProbe: false,   // hover reads /api/r0_point (no cell geometry to hit-test)
  coverage: null,      // last /api/coverage verdict for the pending query
  coverageKey: null,   // the query that verdict belongs to (guards against staleness)
  coverageLive: true,  // the endpoint exists (false → fall back to local rules)
  basemap: { key: null, provider: 'carto' },  // from /api/config at runtime
  layers: [],          // normalised displayed layers: {code,name,image,bounds,boundary,meta}
};

/* ---------------------------------------------------------------------
   REGION SELECTION
   The map may show one country or several. Selection is a set of codes;
   `state.region` stays the FIRST of them so the single-region paths
   (occurrences, context labels) keep working untouched.
   --------------------------------------------------------------------- */
const STUDY_CODES = ['MAR', 'KEN', 'MLI', 'MRT'];   // the four mapped study countries

function regionByCode(code) {
  return state.regions.find((r) => r.code === code) || null;
}
function selectedRegions() {
  return state.selected.map(regionByCode).filter(Boolean);
}
function isMulti() {
  return state.selected.length > 1;
}
/* Names for display, in preset order. */
function selectedNames() {
  return selectedRegions().map((r) => r.name);
}

/* Apply a new selection: normalise to preset order, drop unknown codes, and
   keep state.region pointing at the first survivor. */
function setSelection(codes) {
  const set = new Set(codes);
  state.selected = state.regions.filter((r) => set.has(r.code)).map((r) => r.code);
  state.region = regionByCode(state.selected[0]) || null;
  syncRegionPicker();
  queryChanged();
}

/* Identity of the current time slice + source selection. Coverage is only
   trusted while this matches, so an in-flight or stale verdict is never
   applied to a different query. */
function queryKey() {
  return [state.mode, state.month, state.mode === 'month' ? state.year : '-',
    state.source, state.selected.join(',')].join('|');
}

/* Build the region checkbox list once the presets are known. */
function renderRegionPicker() {
  const list = $('#regionList');
  if (!list) return;
  list.innerHTML = state.regions.map((r) => `
    <li class="picker__row">
      <label class="picker__opt">
        <input type="checkbox" value="${esc(r.code)}" />
        <span class="picker__box" aria-hidden="true"></span>
        <span class="picker__name">${esc(r.name)}</span>
      </label>
      <button type="button" class="picker__only" data-only="${esc(r.code)}"
              title="Show only ${esc(r.name)}">only</button>
    </li>`).join('');

  list.addEventListener('change', (e) => {
    const cb = e.target.closest('input[type="checkbox"]');
    if (!cb) return;
    const codes = new Set(state.selected);
    if (cb.checked) codes.add(cb.value); else codes.delete(cb.value);
    setSelection([...codes]);
  });
  list.addEventListener('click', (e) => {
    const only = e.target.closest('.picker__only');
    if (!only) return;
    setSelection([only.dataset.only]);   // one click back to a single country
  });
}

/* Reflect state.selected in the checkboxes + the count caption. */
function syncRegionPicker() {
  $$('#regionList input[type="checkbox"]').forEach((cb) => {
    cb.checked = state.selected.includes(cb.value);
  });
  const count = $('#regionCount');
  if (count) {
    const n = state.selected.length;
    count.textContent = n === 0 ? 'none' : (n === 1 ? selectedNames()[0] : `${n} selected`);
  }
  // Nothing selected is a dead end — say so rather than letting Compute no-op.
  const note = $('#regionNote');
  if (note) {
    const empty = state.selected.length === 0;
    note.textContent = empty ? 'Select at least one region to compute a map.' : '';
    note.classList.toggle('note--warn', empty);
    note.hidden = !empty;
  }
}

let map, cellLayer = null, occLayer = null, labelsLayer = null;
/* One raster + one outline PER country: the backend renders each country
   separately so each keeps its own best resolution, so the client draws each
   at its own bounds rather than stretching one image over the union. */
let surfaceLayers = [], boundaryLayers = [];

function clearSurfaceLayers() {
  surfaceLayers.forEach((l) => l.remove());
  boundaryLayers.forEach((l) => l.remove());
  surfaceLayers = []; boundaryLayers = [];
}

/* Draw every layer of a normalised surface. Outlines go on after all rasters so
   no country's fill covers a neighbour's border. */
function addSurfaceLayers(surface) {
  clearSurfaceLayers();
  if (!surface || !surface.layers) return;
  surface.layers.forEach((L0) => {
    surfaceLayers.push(L.imageOverlay(L0.image, L0.bounds, {
      opacity: 1, interactive: false, className: 'r0-surface',
    }).addTo(map));
  });
  surface.layers.forEach((L0) => {
    if (!L0.boundary) return;
    boundaryLayers.push(L.geoJSON(L0.boundary, {
      interactive: false,
      style: { color: '#1b2733', weight: 1.4, opacity: 0.92, fill: false, lineJoin: 'round' },
    }).addTo(map));
  });
}

/* =====================================================================
   CLIMATE SOURCE  ——  capability lookup + honest fallback resolution.

   Rule for the whole module: the UI may never imply a source that did not
   produce the map. Two separate checks enforce that —
     1. resolveSource()  — BEFORE the request, from /api/sources capabilities,
        so an unsupported year is announced rather than discovered.
     2. reconcileProvenance() — AFTER the response, from meta.source, so a
        server-side substitution we did not predict is still surfaced.
   ===================================================================== */
function sourceCap(id) {
  return state.sources.find((s) => s && s.id === id) || null;
}

/* Compact resolution for tight chrome (buttons, hints): the capability list
   carries a precise string like "~1 km (30 arcsec)"; the parenthetical is
   detail that belongs in the metadata panel, not on a button. */
function shortRes(cap) {
  if (!cap || !cap.resolution) return '';
  return String(cap.resolution).replace(/\s*\(.*?\)\s*/g, '').trim();
}

/* Human label for a capability, e.g. "NASA POWER 0.5°×0.625°". */
function sourceName(id, withRes = true) {
  const c = sourceCap(id);
  if (!c) return id || 'unknown source';
  return withRes && c.resolution ? `${c.label} ${shortRes(c)}` : c.label;
}

/* Dropdown option text: "High resolution · ~1 km (30 arcsec) — CHELSA V2.1".
   The resolution string is whatever the capability list reports, verbatim. */
function sourceOptionText(cap) {
  const prefix = (SOURCE_UI[cap.id] || {}).prefix;
  return [prefix, cap.resolution].filter(Boolean).join(' · ') + ` — ${cap.label}`;
}

/* Does the high-resolution source cover the current slice, judged locally?
   Month-precise: a source can be complete through one year and publish only
   part of the next, so a year-only test would claim coverage it does not have.
   This is the FALLBACK path — /api/coverage is preferred when available. */
function chelsaCoversLocally(cap) {
  if (!cap) return false;
  if (state.mode === 'climatology') return cap.supports_climatology !== false;

  const y = state.year;
  const lo = cap.year_min == null ? 1979 : cap.year_min;
  if (y < lo) return false;

  // Years where every month is published.
  if (cap.year_max_complete != null && y <= cap.year_max_complete) return true;

  // A partially published year covers only its stated month range.
  const partial = (cap.partial_years || {})[String(y)];
  if (partial) {
    return state.month >= partial.month_min && state.month <= partial.month_max;
  }
  // Older payload with no month-level detail: fall back to the year bound.
  if (cap.year_max_complete == null && cap.year_max != null) return y <= cap.year_max;
  return false;
}

/* Which source will actually serve the CURRENT query, and why not the other.
   Prefers the backend's own verdict (/api/coverage) so the warning shown here
   cannot disagree with what the render endpoints do. Falls back to the local
   capability rules when that endpoint is absent or its answer is stale. */
function resolveSource() {
  const wanted = state.source;
  const powerId = sourceCap('power') ? 'power' : ((state.sources[0] || {}).id || 'power');

  if (wanted !== 'auto' && wanted !== 'chelsa') {
    return { id: wanted, reason: null, auto: false, reasonText: null };
  }
  const auto = wanted === 'auto';
  const hi = sourceCap('chelsa');
  if (!hi) return { id: powerId, reason: 'unavailable', auto, reasonText: null };

  // Backend verdict, but only for the query it was actually asked about.
  const cov = state.coverage;
  if (cov && state.coverageKey === queryKey()) {
    if (cov.chelsa_available) return { id: 'chelsa', reason: null, auto, reasonText: null };
    // would_error === true means the backend would refuse an explicit request
    // for the high-resolution source; we ask for the coarse one instead and say
    // so, rather than sending a request we know will fail.
    return {
      id: cov.resolved_source || powerId,
      reason: 'coverage', auto,
      reasonText: cov.chelsa_reason || cov.note || null,
    };
  }

  // Local fallback.
  if (chelsaCoversLocally(hi)) return { id: hi.id, reason: null, auto, reasonText: null };
  return {
    id: powerId,
    reason: state.mode === 'climatology' ? 'climatology' : 'year',
    auto, reasonText: null,
  };
}

/* Ask the backend which source serves the pending query. Debounced by the
   caller; safe to call redundantly (a matching key short-circuits). */
let coverageSeq = 0;
async function refreshCoverage() {
  if (!state.sources.length) return;
  if (!sourceCap('chelsa') || !state.coverageLive) return;   // nothing to arbitrate
  const key = queryKey();
  if (state.coverageKey === key && state.coverage) return;
  const seq = ++coverageSeq;
  try {
    const cov = await api.coverage(state.mode, state.month, state.year, state.source, state.selected);
    if (seq !== coverageSeq) return;            // a newer query superseded this
    state.coverage = cov;
    state.coverageKey = key;
  } catch (err) {
    if (err && err.status === 404) {
      state.coverageLive = false;               // older server: use local rules
      console.info('[R0] /api/coverage unavailable — using local coverage rules');
    } else {
      console.warn('[R0] coverage check failed:', err.message);
    }
    if (seq !== coverageSeq) return;
    state.coverage = null; state.coverageKey = null;
  }
  updateSourceNote();
  updateComputeMeta();
}

let coverageTimer = null;
function scheduleCoverage() {
  clearTimeout(coverageTimer);
  // Invalidate immediately so a stale verdict is never shown for a new query.
  if (state.coverageKey !== queryKey()) { state.coverage = null; state.coverageKey = null; }
  coverageTimer = setTimeout(refreshCoverage, 140);
}

/* Read the source id back OUT of a response. The contract carries meta.source;
   accept an explicit meta.source_id when a backend supplies one. */
function detectServedSource(meta) {
  if (!meta) return null;
  if (meta.source_id) return String(meta.source_id).toLowerCase();
  const s = String(meta.source || '').toLowerCase();
  if (s.includes('chelsa')) return 'chelsa';
  if (s.includes('power')) return 'power';
  return null;
}

/* The inline note under the source control, written BEFORE the compute so a
   fallback is never a surprise. Never alarming — it states the substitution
   and the resolution the user will actually get. */
function updateSourceNote() {
  const el = $('#srcNote');
  if (!el) return;
  if (!state.sources.length) { el.hidden = true; return; }  // capabilities not loaded yet
  const r = resolveSource();
  const hi = sourceCap('chelsa');
  const got = sourceName(r.id);
  let html = '', warn = false;

  if (r.reasonText) {
    // The backend's own explanation, verbatim. Only the connective is ours, so
    // the stated reason can never drift from the actual coverage rules.
    warn = true;
    html = `${esc(r.reasonText)} — using ${esc(got)}.`;
  } else if (r.reason === 'year' && hi) {
    warn = true;
    html = `<strong>${state.year}</strong>-${String(state.month).padStart(2, '0')} is not in ` +
      `${esc(hi.label)} — using ${esc(got)}.`;
  } else if (r.reason === 'climatology' && hi) {
    warn = true;
    html = `${esc(hi.label)} has no climatology — using ${esc(got)}.`;
  } else if (r.reason === 'unavailable') {
    // Not a fallback the user chose: the server simply offers one source.
    html = state.sourcesLive
      ? `Only ${esc(got)} is offered by this server.`
      : `This server predates the source API — ${esc(got)} only.`;
  } else if (r.id === 'chelsa') {
    html = `${r.auto ? 'Auto → ' : ''}${esc(got)}. First compute for a ` +
      `region/month reads the rasters (~15–60 s), then it is cached.`;
  } else {
    html = `${r.auto ? 'Auto → ' : ''}${esc(got)}.`;
  }

  // A mixed selection may split across sources; when the backend reports that
  // per region, say how it splits instead of implying one source for all.
  const split = coverageSplit();
  if (split) { html = split.html; warn = split.warn; }

  el.innerHTML = html;
  el.classList.toggle('note--warn', warn);
  el.hidden = false;

  // Year bounds hint reflects whichever sources are actually on offer.
  const yh = $('#yearHint');
  if (yh && state.mode === 'month') yh.textContent = state.sources.map(coverageLabel).join(' · ');

  updateWelcomeHint(r);
  applyScaleConstraints();
}

/* If /api/coverage reports per-region verdicts for a multi-country selection,
   summarise the split ("3 use CHELSA ~1 km · 2 fall back to NASA POWER").
   Returns null when the selection is single, or when the backend gives only one
   verdict — in which case the caller's single-source wording is accurate. */
function coverageSplit() {
  if (!isMulti()) return null;
  const cov = state.coverage;
  if (!cov || state.coverageKey !== queryKey()) return null;

  // NOTE: cov.regions is a list of CODES, not per-region verdicts — only
  // per_region carries objects. Reading the wrong one yields "unknown" labels.
  const per = Array.isArray(cov.per_region) ? cov.per_region : null;
  if (!per || !per.length) return null;

  const buckets = new Map();
  per.forEach((r) => {
    const label = r.resolved_label || r.resolved_source || 'unknown';
    const res = r.resolution || r.resolved_resolution || '';
    const key = `${label}|${res}`;
    if (!buckets.has(key)) buckets.set(key, { label, res, codes: [] });
    buckets.get(key).codes.push(r.code);
  });
  const groups = [...buckets.values()];

  // The backend writes its own one-line summary; prefer it so this wording can
  // never drift from the resolution logic that produced it.
  if (cov.summary) return { html: esc(cov.summary) + '.', warn: groups.length > 1 };

  if (groups.length < 2) {
    const g = groups[0];
    return { html: `All ${per.length} regions → ${esc(g.label)}${g.res ? ' ' + esc(shortRes({ resolution: g.res })) : ''}.`, warn: false };
  }
  return {
    warn: true,
    html: groups
      .map((g) => `${g.codes.length} → ${esc(g.label)}${g.res ? ' ' + esc(shortRes({ resolution: g.res })) : ''}`)
      .join(' · ') + '.',
  };
}

/* First-run hint has to set the right expectation for the resolved source. */
function updateWelcomeHint(r) {
  const el = $('#welcomeHint');
  if (!el) return;
  const res = r || resolveSource();
  el.textContent = res.id === 'chelsa'
    ? `First compute reads ${sourceName(res.id)} rasters — ~15–60 s, then cached.`
    : `First compute reads live ${sourceName(res.id)} climate — ~10 s.`;
}

/* AFTER the response: if what came back is not what we asked for, say so.
   This is the backstop for a substitution decided server-side. */
function reconcileProvenance(meta) {
  const served = detectServedSource(meta);
  state.servedSource = served;
  const asked = state.requestedSource;
  if (!served || !asked || served === asked) { state.provNote = null; return; }

  const el = $('#srcNote');
  if (!el) return;
  const label = meta && meta.source ? meta.source : sourceName(served);
  const res = meta && meta.resolution ? ` ${meta.resolution}` : '';
  state.provNote = { text: `${label}${res}` };
  el.innerHTML =
    `Served by <strong>${esc(label)}${esc(res)}</strong>, not ` +
    `${esc(sourceName(asked, false))} — this map is ${esc(label)} data.`;
  el.classList.add('note--warn');
  el.hidden = false;
}

/* Build the source dropdown from the capability list. */
function renderSourceOptions() {
  const sel = $('#sourceSelect');
  if (!sel) return;
  const ordered = state.sources.slice().sort((a, b) =>
    ((SOURCE_UI[a.id] || {}).order ?? 9) - ((SOURCE_UI[b.id] || {}).order ?? 9));

  const opts = [`<option value="auto">Auto (recommended)</option>`];
  ordered.forEach((c) => {
    opts.push(`<option value="${esc(c.id)}">${esc(sourceOptionText(c))}</option>`);
  });
  // A server that predates /api/sources still shows the high-resolution option,
  // disabled — offering it and quietly serving 55 km data would be a lie.
  if (!sourceCap('chelsa')) {
    opts.splice(1, 0,
      `<option value="chelsa" disabled>High resolution · ~1 km (CHELSA) — unavailable</option>`);
  }
  sel.innerHTML = opts.join('');
  if (state.source !== 'auto' && !sourceCap(state.source)) state.source = 'auto';
  sel.value = state.source;
}

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

/* ---------------------------------------------------------------------
   POINT PROBE (raster sources).
   A ~1 km raster has no per-cell GeoJSON to hit-test, so the hover value
   comes from /api/r0_point. Debounced, single-flight (an in-flight request
   is aborted when the cursor moves on) and — critically — it reuses the SAME
   single probeTip element as the cell layer, so readouts still cannot
   accumulate. Stale responses are dropped by sequence number.
   --------------------------------------------------------------------- */
let probeTimer = null, probeAbort = null, probeSeq = 0;

function inFieldBounds(latlng) {
  const bbox = fieldBbox();
  if (!bbox) return true;
  const [w, s, e, n] = bbox;
  return latlng.lat >= s && latlng.lat <= n && latlng.lng >= w && latlng.lng <= e;
}

function cancelPointProbe() {
  clearTimeout(probeTimer);
  probeSeq++;                       // invalidate anything still in flight
  if (probeAbort) { probeAbort.abort(); probeAbort = null; }
}

function onProbeMove(ev) {
  if (!state.pointProbe || !state.region) return;
  const latlng = ev.latlng;
  clearTimeout(probeTimer);
  if (!inFieldBounds(latlng)) { probeSeq++; hideProbe(); return; }
  probeTimer = setTimeout(() => queryPoint(latlng), CONFIG.PROBE_DEBOUNCE);
}

async function queryPoint(latlng) {
  const seq = ++probeSeq;
  if (probeAbort) probeAbort.abort();
  probeAbort = new AbortController();
  const signal = probeAbort.signal;

  // Placeholder in the one existing tooltip — replaced in place, never added to.
  showProbe(latlng, '<span class="cell-tip__pending">R₀ </span>');
  try {
    // Multi-country: the backend resolves the point to whichever selected
    // country contains it, so the whole selection is sent.
    const params = {
      mode: state.mode, month: state.month, year: state.year,
      source: state.servedSource || state.requestedSource || undefined,
      regions: state.selected.slice(),
    };
    const p = await api.point(state.region, params, latlng.lat, latlng.lng, signal);
    if (seq !== probeSeq) return;                       // a newer hover won
    showProbe(latlng, pointTooltip(p));
  } catch (err) {
    if (seq !== probeSeq || (err && err.name === 'AbortError')) return;
    showProbe(latlng, '<span class="cell-tip__null">value unavailable here</span>');
  }
}

/* Readout for a point response. Adds two things the cell probe cannot know:
   which of the selected countries the value came from (only worth saying when
   several are on screen), and the backend's own explanation when there is no
   value — "outside every selected region" beats a bare "no data". */
function pointTooltip(p) {
  if (!p) return cellTooltip(null);
  const meta = p.meta || {};
  if (p.r0 == null) {
    return meta.note
      ? `<span class="cell-tip__null">${esc(meta.note)}</span>`
      : cellTooltip(p);
  }
  const body = cellTooltip(p);
  if (!isMulti() || !meta.region) return body;
  const reg = regionByCode(meta.region);
  return `<span class="cell-tip__where">${esc((reg && reg.name) || meta.region)}</span> · ${body}`;
}

/* Turn the point probe on/off and mirror it in the map cursor. */
function setPointProbe(on) {
  state.pointProbe = !!on;
  const el = document.getElementById('map');
  if (el) el.classList.toggle('is-probe', !!on);
  if (!on) { cancelPointProbe(); hideProbe(); }
}

/* Basemap tile URL for a CARTO style.

   The access key is fetched from /api/config at runtime and is deliberately NOT
   in this file — it must never be committed. Without a key the SAME tiles still
   load (watermarked), so a missing key degrades the look, never the map.

   Attribution is NOT optional: CARTO's free tier is conditional on displaying
   both the CARTO and OpenStreetMap credits, so the attribution control stays. */
function basemapUrl(style) {
  const url = `https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`;
  const key = state.basemap.key;
  return key ? `${url}?key=${encodeURIComponent(key)}` : url;
}

/* Read the basemap credentials before any tile layer is built. Fetching this
   first (rather than swapping the URL afterwards) matters: L.setUrl re-requests
   every tile, so a late swap briefly paints the unkeyed, watermarked tiles —
   which looks like a failure. Short timeout; failure just means no key. */
async function loadBasemapConfig() {
  try {
    const cfg = await api.config();
    const bm = (cfg && cfg.basemap) || {};
    if (bm.key) state.basemap.key = bm.key;
    if (bm.provider) state.basemap.provider = bm.provider;
  } catch (err) {
    // Deliberately does not log the endpoint response — it carries a credential.
    console.info('[R0] no basemap key configured; using unkeyed tiles');
  }
}

function initMap() {
  map = L.map('map', { zoomControl: true, attributionControl: true, minZoom: 2 })
    .setView([12, 30], 4);

  // Clean, near-white basemap WITHOUT labels — the data is the figure.
  L.tileLayer(basemapUrl('light_nolabels'), {
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
  labelsLayer = L.tileLayer(basemapUrl('light_only_labels'), {
    subdomains: 'abcd', maxZoom: 19, pane: 'labelsPane', opacity: 0.95,
  });

  L.control.scale({ imperial: false, position: 'bottomright', maxWidth: 140 }).addTo(map);

  // Cursor coordinate readout
  const readout = $('#cursorReadout');
  map.on('mousemove', (ev) => {
    readout.classList.add('is-on');
    $('#curLat').textContent = fmtLat(ev.latlng.lat);
    $('#curLon').textContent = fmtLon(ev.latlng.lng);
    onProbeMove(ev);            // no-op unless a raster is the probeable layer
  });
  map.on('mouseout', () => {
    readout.classList.remove('is-on');
    cancelPointProbe();
    hideProbe();
  });
  // Panning/zooming invalidates the pending point under the cursor.
  map.on('movestart zoomstart', () => { cancelPointProbe(); hideProbe(); });

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
/* `required` = the raster IS the map (a native high-resolution source has no
   cell geometry to fall back to), so failures must propagate to the caller
   instead of degrading to a silent blank. For the coarse source the raster is
   only the Smooth view, and cells still stand. */
async function fetchSurface(region, params, required) {
  if (CONFIG.USE_MOCK || !CONFIG.USE_SURFACE) return null;
  try {
    const j = normaliseSurface(await apiGet(`/api/r0_surface?${queryFor(region, params)}`), params);
    if (!j) {
      if (required) throw new Error('empty surface response');
      return null;
    }
    return j;
  } catch (err) {
    if (required) throw err;
    console.warn('[R0] surface unavailable, using cells:', err.message);
    return null;
  }
}

/* Collapse the two response shapes into ONE internal form so nothing
   downstream has to branch:
     single  -> {image, bounds, boundary, legend, bbox, meta}
     multi   -> {layers:[{code,name,image,bounds,boundary,meta}], bbox, legend, meta}
   Both become {layers:[...], bbox, legend, meta}. A one-country selection keeps
   using the single endpoint, so this is also what guarantees no regression. */
function normaliseSurface(resp, params) {
  if (!resp) return null;

  if (Array.isArray(resp.layers)) {
    const layers = resp.layers.filter((l) => l && l.image && l.bounds);
    if (!layers.length) return null;
    return {
      layers,
      bbox: resp.bbox || null,
      legend: resp.legend || [],
      meta: resp.meta || (layers[0] && layers[0].meta) || null,
      // Regions the backend could not render. Silently drawing fewer countries
      // than were asked for would misrepresent the map, so this is surfaced.
      failed: Array.isArray(resp.failed) ? resp.failed : [],
      multi: true,
    };
  }

  if (!resp.image || !resp.bounds) return null;
  const code = (params && params.regions && params.regions[0]) || (state.region && state.region.code);
  const reg = regionByCode(code);
  return {
    layers: [{
      code, name: reg ? reg.name : code,
      image: resp.image, bounds: resp.bounds,
      boundary: resp.boundary || null, meta: resp.meta || null,
    }],
    bbox: resp.bbox || null,
    legend: resp.legend || [],
    meta: resp.meta || null,
    multi: false,
  };
}

/* =====================================================================
   Displayed-result accessors.
   A result is now one of two shapes: cell geometry (+ optional raster) from
   the coarse source, or a raster ONLY from a native high-resolution source.
   Everything downstream reads provenance through these, so the panel, the
   legend and the topbar can never disagree about what is on screen.
   ===================================================================== */
function displayMeta() {
  const s = state.surface, f = state.field;
  if (state.surfaceActive && s) {
    // The multi-region aggregate meta need not repeat every per-layer field, so
    // fill any gaps from the first layer. Nothing is invented — a key is only
    // taken from a layer when the aggregate does not define it at all.
    const first = (s.layers && s.layers[0] && s.layers[0].meta) || null;
    if (s.meta && first) return Object.assign({}, first, s.meta);
    return s.meta || first;
  }
  if (f && f.meta) return f.meta;
  return (s && s.meta) || null;
}

function fieldBbox() {
  if (state.surfaceActive && state.surface && state.surface.bbox) return state.surface.bbox;
  if (state.field && state.field.bbox) return state.field.bbox;
  if (state.surface && state.surface.bbox) return state.surface.bbox;
  // No union supplied — derive it from the layer bounds so a multi-country map
  // still fits everything rather than fitting only the first country.
  const ls = state.layers || [];
  if (ls.length) {
    let w = 180, s = 90, e = -180, n = -90;
    ls.forEach((L0) => {
      const b = L0.bounds; if (!b) return;
      s = Math.min(s, b[0][0]); w = Math.min(w, b[0][1]);
      n = Math.max(n, b[1][0]); e = Math.max(e, b[1][1]);
    });
    if (w <= e && s <= n) return [w, s, e, n];
  }
  return null;
}

/* Provenance grouped by what actually produced each layer. A mixed selection
   must never be flattened into one claim, so this is the single place that
   decides how many distinct claims the map is making. */
function provenanceGroups() {
  const groups = [];
  (state.layers || []).forEach((L0) => {
    const m = L0.meta || {};
    const key = `${m.source_id || m.source || '?'}|${m.resolution || ''}|${m.render || ''}`;
    let g = groups.find((x) => x.key === key);
    if (!g) {
      g = { key, source: m.source || '—', resolution: m.resolution || '', render: m.render ? renderLabel(m) : '', codes: [] };
      groups.push(g);
    }
    g.codes.push(L0.code);
  });
  return groups;
}

/* The legend that describes what is PAINTED right now. In relative mode the
   raster's own breaks are the only truthful ones, so the raster response's
   legend wins whenever the raster is the visible layer. */
function displayLegend() {
  if (state.surfaceActive && state.surface && state.surface.legend) {
    return state.surface.legend;
  }
  if (state.field && state.field.legend) return state.field.legend;
  return (state.surface && state.surface.legend) || [];
}

/* Scale of the DISPLAYED map — read from the response, not from the control,
   so a backend that ignored `scale` cannot leave a "Relative" badge lying. */
function displayScale() {
  const m = displayMeta();
  if (m && m.scale) return String(m.scale).toLowerCase();
  return 'absolute';   // nothing reported → the fixed classes are what was rendered
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

/* Shared by the cell hit-test and the 1 km point probe — both deliver the same
   {r0, t, rh} shape, so the readout is identical whichever probe produced it. */
function cellTooltip(p) {
  if (!p || p.r0 == null) return '<span class="cell-tip__null">no data</span>';
  const bits = [`<span class="cell-tip__r0">R₀ ${Number(p.r0).toFixed(2)}</span>`];
  if (p.t != null)  bits.push(`T ${Number(p.t).toFixed(1)}°C`);
  if (p.rh != null) bits.push(`RH ${Math.round(Number(p.rh))}%`);
  return bits.join(' · ');
}

function renderField(data, surface) {
  state.field = data || null;
  state.surface = surface || null;
  state.layers = (surface && surface.layers) || [];

  const haveSurface = state.layers.length > 0;
  const haveCells = !!(data && data.features && data.features.length);

  // View design: CELLS = discrete opaque cells with a visible grid (one colour
  // per coarse cell); SMOOTH = the surface raster. A native high-resolution
  // source has no cell geometry at all, so the raster is the only layer and
  // Smooth is forced (applyViewConstraints keeps the control honest about it).
  if (!haveCells && haveSurface) state.view = 'smooth';
  state.surfaceActive = haveSurface && state.view === 'smooth';
  syncViewButtons();   // the forced view must show in the control, not just in state

  state.legendMap = {};
  displayLegend().forEach((l) => { state.legendMap[l.cls] = l.color; });

  // Clear prior layers (and any open probe / in-flight point request)
  clearTimeout(probeHideTimer); cancelPointProbe(); hideProbe();
  clearSurfaceLayers();
  if (cellLayer) { cellLayer.remove(); cellLayer = null; }

  // Surface rasters — added below the cells so transparent cells stay the
  // hover/probe layer on top when cell geometry exists.
  if (state.surfaceActive) addSurfaceLayers(surface);

  // Cells — painted when no surface; transparent hover/probe geometry when a
  // surface is shown. NO per-feature tooltips and NO bringToFront. Instead, a
  // SINGLE shared probe (managed above) is updated on the layer's mousemove and
  // removed on mouseout — one element, always replaced, so it cannot accumulate.
  if (haveCells) {
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
  }

  // Raster with no cell geometry → the hover value comes from /api/r0_point.
  // Exactly one of the two probes is ever live.
  setPointProbe(!haveCells && state.surfaceActive);

  // Cells-view outline. In Smooth view each layer already drew its own (see
  // addSurfaceLayers); here the cells response supplies the single-region one.
  if (!state.surfaceActive && data && data.boundary) {
    boundaryLayers.push(L.geoJSON(data.boundary, {
      interactive: false,
      style: { color: '#1b2733', weight: 1.4, opacity: 0.92, fill: false, lineJoin: 'round' },
    }).addTo(map));
  }

  // Fit to bbox — invalidate first so we always fit to the CURRENT container
  // size, and re-fit on the next frame once layout has settled (guards against
  // the panel/window having resized since map init, which otherwise leaves the
  // image overlay and the vector boundary on mismatched pixel origins).
  const bbox = fieldBbox();
  if (bbox) {
    const [w, s, e, n] = bbox;
    const fit = () => {
      map.invalidateSize({ animate: false });
      map.fitBounds([[s, w], [n, e]], { padding: [28, 28], animate: false });
    };
    fit();
    requestAnimationFrame(fit);
  }

  const meta = displayMeta();
  renderLegend();
  renderMeta(meta);
  renderCaveats(meta);
  updateContext(meta);
  applyViewConstraints();
  applyScaleConstraints();

  // Keep place-labels overlay on top of freshly added layers (and un-freeze fade)
  refreshLabels();

  // Re-apply / refresh occurrence overlay if toggled on
  if (state.showOcc) loadOccurrences();
}

/* Live context readout (top bar + legend caption) */
/* Label for the current selection: a name when it is one country, a count when
   it is several (the per-country list lives in the metadata panel). */
function regionLabel() {
  const names = selectedNames();
  if (!names.length) return '—';
  if (names.length === 1) return names[0];
  if (names.length === 2) return names.join(' + ');
  return `${names.length} countries`;
}

function updateContext(meta) {
  const region = regionLabel();
  const modeLabel = (meta && meta.mode === 'month') ? 'Historical' : 'Climatology';
  const monthName = MONTHS[((meta && meta.month) || state.month || 1) - 1];
  const year = meta && meta.year != null ? meta.year : (state.mode === 'month' ? state.year : null);
  const timeStr = year != null ? `${MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1]} ${year}`
                                : MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1];

  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#ctxRegionV', region);
  set('#ctxModeV', modeLabel);
  set('#ctxTimeV', timeStr);

  // Provenance line: the source that actually produced this map, its NATIVE
  // resolution and how it is being drawn. Never the source that was requested.
  set('#sourceLine', meta && meta.source
    ? `Source — ${provenanceText(meta)}`
    : 'Source — awaiting field');

  // Legend caption
  set('#legendTime', (year != null ? `${MONTHS_SHORT[(meta && meta.month ? meta.month : state.month) - 1]} ${year}`
                                    : monthName));
  set('#legendRegion', region);
}

/* =====================================================================
   Legend, metadata, caveats
   ===================================================================== */

/* "CHELSA V2.1 · ~1 km · native raster" — source · native resolution · render.
   `full` keeps the exact resolution string and the long render wording for the
   metadata panel; the compact form is for the topbar and legend footer. */
function provenanceText(meta, full) {
  // A multi-country map may be making SEVERAL different claims at once. Report
  // each one with how many countries it covers — never collapse them into a
  // single source/resolution the whole map does not actually have.
  const groups = provenanceGroups();
  if (groups.length > 1) {
    return groups
      .map((g) => `${g.source}${g.resolution ? ' ' + (full ? g.resolution : shortRes(g)) : ''} (${g.codes.length})`)
      .join(' · ');
  }
  if (groups.length === 1 && (state.layers || []).length > 1) {
    const g = groups[0];
    const res = full ? g.resolution : (shortRes(g) || g.resolution);
    return [`${g.source}`, res, g.render, `all ${g.codes.length}`].filter(Boolean).join(' · ');
  }
  if (!meta) return '—';
  const res = full ? meta.resolution : (shortRes(meta) || meta.resolution);
  return [meta.source || '—', res, renderLabel(meta, full)]
    .filter(Boolean).join(' · ');
}

/* Plain-language render mode. `native-1km` means pixels as delivered — no
   interpolation — which is a materially different claim from the cosmetic
   upsample the coarse source uses, so the two must never read alike. */
function renderLabel(meta, full) {
  if (!meta) return '';
  const r = String(meta.render || '').toLowerCase();
  if (!r) return state.surfaceActive ? 'raster' : 'cells';
  // The resolution is stated alongside this label, so it is not repeated here.
  // Three genuinely different claims, kept lexically distinct:
  //   native    — source pixels, every one of them
  //   resampled — still real source pixels, read at a coarser step (big regions)
  //   interpolated — a cosmetic upsample of a much coarser grid (coarse source)
  // Checked before "native" because "resampled-from-1km" also names the source.
  if (r.includes('resampl')) {
    return full ? 'resampled from 1 km (real source pixels, coarser step)' : 'resampled raster';
  }
  if (r.includes('native')) return full ? 'native raster (no interpolation)' : 'native raster';
  if (r.includes('interpolat')) return full ? 'interpolated raster (cosmetic, not added resolution)' : 'interpolated raster';
  return r;
}

function renderLegend() {
  const box = $('#legendBox');
  const list = $('#legendList');
  const cont = $('#legendContinuous');
  const note = $('#legendNote');
  const tag = $('#legendScaleTag');
  const prov = $('#legendProv');
  box.hidden = false;
  list.innerHTML = '';

  const legend = displayLegend();
  const relative = displayScale() === 'relative';
  const f = state.field;
  const hasNull = !!(f && f.features && f.features.some((x) => x.properties.cls == null));

  // Swatches are read straight from the response legend — in relative mode the
  // backend returns this map's ACTUAL numeric breaks, and those are what the
  // reader sees. The renderer never invents or reuses class labels.
  legend.forEach((l) => {
    const isThr = !relative && l.cls === 'eq1';
    const li = document.createElement('li');
    li.className = 'map-legend__row' + (isThr ? ' is-threshold' : '');
    li.innerHTML =
      `<span class="map-legend__swatch" style="background:${esc(l.color)}"></span>` +
      `<span class="map-legend__rowlabel">R<sub>0</sub> ${esc(l.label)}</span>`;
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

  if (cont) cont.hidden = true;
  list.style.display = '';

  // Relative mode must be unmistakable: a badge on the title AND a note saying
  // the breaks are this map's own range, so nobody reads them as absolute
  // suitability or looks for a threshold that is not in the scale.
  if (tag) tag.hidden = !relative;
  if (note) {
    note.hidden = false;
    note.classList.toggle('is-relative', relative);
    note.innerHTML = relative
      ? 'Relative stretch — breaks span THIS map only; not comparable between maps.'
      : 'R<sub>0</sub> = 1 — suitability threshold';
  }

  // Figure-style provenance under the legend, so an exported screenshot of the
  // map still carries the source it came from.
  const meta = displayMeta();
  if (prov) {
    prov.textContent = meta ? provenanceText(meta) : '';
    prov.hidden = !meta;
  }
}

/* Field metadata — the panel a user checks before trusting the map, so it
   carries full provenance: which source, at what native resolution, drawn how,
   on which colour scale, over how many cells or pixels. */
function renderMeta(meta) {
  if (!meta) return;
  const box = $('#metaBox');
  const grid = $('#metaGrid');
  box.hidden = false;

  // meta.grid is [nx, ny] (width × height); meta.shape is [rows, cols], so it
  // has to be flipped to read as width × height.
  const g = Array.isArray(meta.grid) && meta.grid.length === 2 ? meta.grid
    : (Array.isArray(meta.shape) && meta.shape.length === 2 ? [meta.shape[1], meta.shape[0]] : null);
  const gridSize = g
    ? `${Number(g[0]).toLocaleString()} × ${Number(g[1]).toLocaleString()}`
    : null;
  const relative = displayScale() === 'relative';

  const layers = state.layers || [];
  const multi = state.surfaceActive && layers.length > 1;

  const rows = [
    ['Mode', meta.mode === 'month' ? 'Historical' : 'Climatology'],
    ['Month', MONTHS[(meta.month || 1) - 1]],
    ['Year', meta.year != null ? meta.year : '—'],
  ];

  if (multi) {
    // One Source/Resolution/Render row would assert a single provenance the map
    // may not have. Summarise per distinct source; the per-country rows below
    // carry the detail.
    rows.push(['Countries', String(layers.length)]);
    rows.push(['Sources', provenanceText(meta, true)]);
  } else {
    rows.push(['Source', meta.source || '—']);
    rows.push(['Native resolution', meta.resolution || '—']);
    rows.push(['Render', renderLabel(meta, true) || '—']);
  }
  rows.push(['Colour scale', relative ? 'Relative (this map)' : 'Absolute (fixed classes)']);

  if (multi) {
    const px = layers.reduce((a, L0) => a + (((L0.meta || {}).n_valid_px) || 0), 0);
    if (px) rows.push(['Valid pixels', px.toLocaleString()]);
  } else if (gridSize && state.surfaceActive) {
    rows.push(['Grid', `${gridSize} px`]);
    if (meta.n_valid_px != null) rows.push(['Valid pixels', meta.n_valid_px.toLocaleString()]);
  } else {
    rows.push(['Cells', meta.n_cells != null ? meta.n_cells.toLocaleString() : '—']);
  }

  grid.innerHTML = rows
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join('');

  renderRegionProvenance();

  // meta.note explains a rendering compromise the numbers above cannot show —
  // e.g. why a large region was read at a coarser step. It is absent when there
  // is nothing to say, which is not an error.
  const noteEl = $('#metaNote');
  if (noteEl) {
    noteEl.textContent = meta.note || '';
    noteEl.hidden = !meta.note;
  }
}

/* Some countries were requested but not drawn. The map would otherwise look
   complete while quietly missing a country the user selected. */
function noteFailedRegions() {
  const note = $('#regionNote');
  if (!note) return;
  const failed = (state.surface && state.surface.failed) || [];
  if (!failed.length) return;                  // leave whatever syncRegionPicker set

  const names = failed.map((f) => {
    const code = typeof f === 'string' ? f : (f.code || f.region);
    const reg = regionByCode(code);
    return (reg && reg.name) || code;
  });
  const reason = failed.find((f) => f && f.error)?.error;
  note.innerHTML = `Not drawn: <strong>${esc(names.join(', '))}</strong>` +
    `${reason ? ` — ${esc(reason)}` : ''}. The map shows the remaining ${(state.layers || []).length}.`;
  note.classList.add('note--warn');
  note.hidden = false;
}

/* Per-country provenance rows. Shown whenever more than one country is drawn,
   so the aggregate header is never the only claim on screen. Countries served
   by a minority source are marked, since that is the case a reader must not
   miss when comparing them. */
function renderRegionProvenance() {
  const box = $('#metaRegions');
  if (!box) return;
  const layers = state.layers || [];
  if (!state.surfaceActive || layers.length < 2) { box.hidden = true; box.innerHTML = ''; return; }

  const groups = provenanceGroups();
  const majority = groups.slice().sort((a, b) => b.codes.length - a.codes.length)[0];
  box.innerHTML = layers.map((L0) => {
    const m = L0.meta || {};
    const odd = groups.length > 1 && majority && !majority.codes.includes(L0.code);
    const src = [m.source || '—', shortRes(m)].filter(Boolean).join(' ');
    return `<div class="readout__region${odd ? ' is-odd' : ''}">` +
      `<span class="readout__region-name">${esc(L0.name || L0.code)}</span>` +
      `<span class="readout__region-src">${esc(src)}</span></div>`;
  }).join('');
  box.hidden = false;
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
  if (!state.showOcc || !state.selected.length) return;
  try {
    // /api/occurrences serves one region, so fetch each selected country and
    // merge. A country with no points must not fail the whole overlay.
    const regions = selectedRegions();
    const results = await Promise.all(regions.map((r) =>
      api.occurrences(r, state.field).catch((err) => {
        console.warn(`[R0] occurrences failed for ${r.code}:`, err.message);
        return null;
      })));
    const features = results.filter(Boolean).flatMap((f) => (f && f.features) || []);
    if (!features.length) return;
    const fc = { type: 'FeatureCollection', features };
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
let elapsedTimer = null;

/* A first high-resolution read costs ~15-60 s. That has to read as intentional
   work with visible progress, not as a hang: the loader names what is being
   read, says it will be cached, and counts elapsed seconds. */
function showLoader(msg, sub, isError) {
  $('#loaderText').textContent = msg || 'Computing suitability field…';
  const subEl = $('#loaderSub');
  if (subEl) { subEl.textContent = sub || ''; subEl.hidden = !sub; }

  const el = $('#loader');
  el.classList.toggle('is-error', !!isError);
  el.hidden = false;

  const elEl = $('#loaderElapsed');
  clearInterval(elapsedTimer); elapsedTimer = null;
  if (isError) { if (elEl) elEl.hidden = true; return; }
  if (elEl) {
    let t = 0;
    elEl.textContent = '0 s';
    elEl.hidden = false;
    elapsedTimer = setInterval(() => { t += 1; elEl.textContent = `${t} s`; }, 1000);
  }
}

function hideLoader() {
  clearInterval(elapsedTimer); elapsedTimer = null;
  const el = $('#loader');
  el.hidden = true;
  el.classList.remove('is-error');
  const elEl = $('#loaderElapsed'); if (elEl) elEl.hidden = true;
}

/* Lock the whole control panel while a field computes — during a long read the
   controls must not look like somewhere clicking again would help. */
function setBusy(on) {
  const panel = $('#panel');
  if (panel) { panel.classList.toggle('is-busy', !!on); panel.setAttribute('aria-busy', on ? 'true' : 'false'); }
  const btn = $('#computeBtn'); if (btn) btn.disabled = !!on;
  const wbtn = $('#welcomeCompute'); if (wbtn) wbtn.disabled = !!on;
  if (!on) updateRunButton();   // restore the empty-selection disable, if any
}

/* =====================================================================
   Compute (fetch /api/r0)
   ===================================================================== */
let computing = false;

/* The parameter set for the current query, including the resolved source. */
function computeParams(sourceId) {
  return {
    mode: state.mode, month: state.month, year: state.year, ramp: 'bins',
    source: sourceId, scale: state.scale, regions: state.selected.slice(),
  };
}

/* One attempt at one source. A native high-resolution source has no per-cell
   GeoJSON, so only the raster is requested and the hover value comes from
   /api/r0_point. The coarse source fetches BOTH: cells are authoritative for
   the Cells view and the hover hit-test, the raster is the Smooth view. */
async function runQuery(sourceId) {
  const params = computeParams(sourceId);
  // Cells come from /api/r0, which serves ONE region and does not serve the
  // native high-resolution source. So cells are fetched only for a single
  // coarse-source country; otherwise the raster is the map and must succeed.
  const wantCells = sourceId !== 'chelsa' && params.regions.length === 1;
  const [data, surface] = await Promise.all([
    wantCells ? api.r0(state.region, params) : Promise.resolve(null),
    fetchSurface(state.region, params, !wantCells),
  ]);
  if (!data && !surface) throw new Error('no field returned');
  return [data, surface];
}

/* The high-resolution source refused this specific query. Relay the server's
   own reason — it is more precise than anything the client could infer. */
function noteRefusal(detail) {
  const el = $('#srcNote');
  if (!el) return;
  const meta = displayMeta();
  const got = (meta && meta.source) || sourceName('power');
  el.innerHTML = `${esc(detail)} — this map uses <strong>${esc(got)}</strong>.`;
  el.classList.add('note--warn');
  el.hidden = false;
}

async function compute() {
  if (computing || !state.selected.length) return;
  computing = true;
  setBusy(true);

  // Settle the coverage verdict before choosing a source: it is instant, and it
  // means the request goes to whichever source the backend itself would pick.
  clearTimeout(coverageTimer);
  await refreshCoverage().catch(() => {});

  const resolved = resolveSource();
  state.requestedSource = resolved.id;
  const native = resolved.id === 'chelsa';
  const n = state.selected.length;
  const month = MONTHS_SHORT[state.month - 1];
  const where = n === 1 ? `${state.region.name} · ${month}` : `${n} countries · ${month}`;

  // Naming the countries is the honest form of progress here: the backend
  // returns one response for the whole set, so per-country completion is not
  // observable from the client and inventing it would be a fake progress bar.
  const roster = n > 1 ? selectedNames().join(' · ') : '';
  showLoader(
    native ? `Reading ${sourceName(resolved.id)} rasters — ${where}` : `Computing ${where}…`,
    n > 1
      ? `${roster} — computed concurrently; the first run for each is slower, then cached.`
      : (native ? 'First run for this region and month; the result is cached afterwards.' : ''));

  try {
    let data, surface, refusal = null;
    try {
      [data, surface] = await runQuery(resolved.id);
    } catch (err) {
      // The capability list cannot describe every gap (e.g. a partial final
      // year). If the high-resolution source refuses this exact query, fall
      // back to the coarse one and say why — never fail the map outright.
      if (resolved.id === 'chelsa' && isRefusal(err)) {
        console.info('[R0] high-resolution source refused this query:', err.detail || err.message);
        refusal = err.detail || err.message;
        state.requestedSource = 'power';
        showLoader(`Computing ${where}…`, `Falling back to ${sourceName('power')}.`);
        [data, surface] = await runQuery('power');
      } else {
        throw err;
      }
    }

    renderField(data, surface);
    reconcileProvenance(displayMeta());
    noteFailedRegions();
    if (refusal) noteRefusal(refusal);
    state.hasResult = true;
    state.dirty = false;
    const welcome = $('#welcome'); if (welcome) welcome.hidden = true;  // dismiss first-run state
    updateRunButton();
  } catch (err) {
    console.error('[R0] compute failed:', err);
    const timedOut = err && (err.name === 'AbortError' || /timeout/i.test(err.message || ''));
    showLoader(
      timedOut
        ? 'The field is taking longer than expected.'
        : 'Could not compute the field — check the backend connection, then retry.',
      timedOut ? 'The server may still be reading the rasters. Retry in a moment.' : '',
      true);
    setTimeout(hideLoader, 3200);
    computing = false; setBusy(false);
    return;
  }
  hideLoader();
  computing = false;
  setBusy(false);
}

/* =====================================================================
   Controls wiring
   ===================================================================== */
function queryTimeLabel() {
  return state.mode === 'month'
    ? `${MONTHS_SHORT[state.month - 1]} ${state.year}`
    : MONTHS_SHORT[state.month - 1];
}

/* Widest year span across the available sources — the input stays open across
   all of them so a fallback is explained, not prevented. */
function yearSpan() {
  const now = new Date().getFullYear();
  // year_max === null means "up to the present" (a live source), NOT "unknown" —
  // dropping those would wrongly clamp the input to the archive source's end.
  const mins = state.sources.map((s) => (s.year_min == null ? 1979 : s.year_min));
  const maxs = state.sources.map((s) => (s.year_max == null ? now : s.year_max));
  return [mins.length ? Math.min(...mins) : 1979, maxs.length ? Math.max(...maxs) : now];
}

/* Compact coverage string for the year hint, e.g.
   "CHELSA V2.1 1979–2018 (+2019 01–04) · NASA POWER 1981–now".
   The complete-years span and the partial-year months are stated separately so
   the hint cannot imply a whole year that is only part-published. */
function coverageLabel(s) {
  const lo = s.year_min == null ? 1979 : s.year_min;
  const end = s.year_max == null ? 'now'
    : (s.year_max_complete != null ? s.year_max_complete : s.year_max);
  const pad = (n) => String(n).padStart(2, '0');
  const partial = Object.entries(s.partial_years || {})
    .map(([y, r]) => `${y} ${pad(r.month_min)}–${pad(r.month_max)}`);
  return `${s.label} ${lo}–${end}${partial.length ? ` (+${partial.join(', ')})` : ''}`;
}

function updateComputeMeta() {
  const region = state.selected.length ? regionLabel() : 'a region';
  const time = queryTimeLabel();
  // The button meta describes the PENDING query. Resolution is deliberately NOT
  // repeated here — the source note directly above already states it, and the
  // extra clause only wraps the button label.
  const meta = $('#computeMeta'); if (meta) meta.textContent = `${region} · ${time}`;
  const wl = $('#welcomeBtnLabel'); if (wl) wl.textContent = `Compute ${region} · ${time}`;
  updateRunButton();
}

/* The Compute CTA reflects state: first run, stale (params changed), or up-to-date. */
function updateRunButton() {
  const btn = $('#computeBtn'); if (!btn) return;
  const label = btn.querySelector('.compute__label');

  // Nothing selected → nothing to compute. Disable rather than leaving a button
  // that silently does nothing; the region note says what to do about it.
  if (!computing) {
    const empty = !state.selected.length;
    btn.disabled = empty;
    const wbtn = $('#welcomeCompute'); if (wbtn) wbtn.disabled = empty;
  }
  if (!state.hasResult)      { if (label) label.textContent = 'Compute suitability map'; btn.classList.remove('is-stale'); }
  else if (state.dirty)      { if (label) label.textContent = 'Update map';              btn.classList.add('is-stale'); }
  else                       { if (label) label.textContent = 'Recompute';              btn.classList.remove('is-stale'); }
}

/* Mark the shown result stale when a QUERY param (region / time) changes.
   Display options (Cells/Smooth, occurrences, labels) never mark dirty. */
function markDirty() {
  if (!state.hasResult) return;
  state.dirty = true;
  updateRunButton();
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
  $('#yearHint').textContent = historical ? '' : 'climatology — disabled';
  queryChanged();
}

/* One place for "the pending query changed": refresh the labels, re-ask the
   backend which source will serve it, and mark the shown result stale. Called
   by every query-param mutator so the coverage check can never be forgotten
   for one of them (month in particular — coverage is month-precise). */
function queryChanged() {
  updateComputeMeta();
  updateSourceNote();
  scheduleCoverage();
  markDirty();
}

function syncViewButtons() {
  $$('#viewToggle .segmented__opt').forEach((b) => {
    const on = b.dataset.view === state.view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

function setView(view) {
  // A raster-only result (native high-resolution source) has no cell geometry —
  // Cells is not a view it can offer, and pretending otherwise would show a
  // coarse grid while the panel claims 1 km data.
  if (view === 'cells' && !cellLayer && !!state.surface) return;

  state.view = view;
  syncViewButtons();
  if (!cellLayer && !state.surface) return;  // nothing rendered yet

  // SMOOTH shows the surface rasters (cached at compute) over transparent cells;
  // CELLS removes them and paints discrete cells with a grid.
  const s = state.surface;
  const haveSurface = !!(s && s.layers && s.layers.length);
  state.surfaceActive = haveSurface && view === 'smooth';
  if (state.surfaceActive) {
    if (!surfaceLayers.length) {
      addSurfaceLayers(s);
      if (cellLayer) cellLayer.bringToFront();   // keep transparent cells on top for hover
    }
  } else if (surfaceLayers.length) {
    clearSurfaceLayers();                        // cells view → discrete painted cells
    if (state.field && state.field.boundary) {
      boundaryLayers.push(L.geoJSON(state.field.boundary, {
        interactive: false,
        style: { color: '#1b2733', weight: 1.4, opacity: 0.92, fill: false, lineJoin: 'round' },
      }).addTo(map));
    }
  }
  refreshLabels();

  // Exactly one probe is live: cell hit-test when cells exist, else point probe.
  setPointProbe(!cellLayer && state.surfaceActive);

  if (cellLayer) cellLayer.setStyle(styleCell);  // transparent when surface active, else painted + grid
  state.legendMap = {};
  displayLegend().forEach((l) => { state.legendMap[l.cls] = l.color; });
  renderLegend();
  renderMeta(displayMeta());
  updateContext(displayMeta());
  applyScaleConstraints();
}

/* The Cells option is only real when cell geometry came back. Disable it (with
   a reason) rather than leaving a control that silently does nothing. */
function applyViewConstraints() {
  const btn = $('#viewToggle .segmented__opt[data-view="cells"]');
  const note = $('#viewNote');
  // Keyed off the layers that actually exist, NOT state.hasResult — the first
  // render calls this before hasResult is set, and the control has to be
  // correct on that very first paint.
  const rasterOnly = !cellLayer && !!state.surface;
  if (btn) {
    btn.disabled = rasterOnly;
    btn.setAttribute('aria-disabled', rasterOnly ? 'true' : 'false');
  }
  if (note) {
    const meta = displayMeta();
    let text = '';
    if (rasterOnly && (state.layers || []).length > 1) {
      // Cells come from a single-region endpoint, so a multi-country map has no
      // cell grid regardless of source — say that, not the source-specific line.
      text = 'Several countries are drawn as rasters — select a single country to use the cell grid.';
    } else if (rasterOnly) {
      text = `${(meta && meta.source) || 'This source'} is drawn as a ` +
        `${(meta && meta.resolution) || 'native'} raster — there is no coarse cell grid to show.`;
    }
    note.textContent = text;
    note.hidden = !text;
  }
}

/* Colour scale is a property of the RASTER render, so it only applies while the
   raster is the visible layer. In Cells view the classes are fixed. */
function applyScaleConstraints() {
  const rel = $('#scaleToggle .segmented__opt[data-scale="relative"]');
  const note = $('#scaleNote');
  const canStretch = !state.surface || state.surfaceActive;
  if (rel) {
    rel.disabled = !canStretch;
    rel.setAttribute('aria-disabled', canStretch ? 'false' : 'true');
  }
  if (!note) return;

  if (!canStretch) {
    note.textContent = 'Cells always use the fixed classes; switch to Smooth to stretch the scale.';
    note.classList.remove('note--warn');
    note.hidden = false;
  } else if (state.scale === 'relative' && state.hasResult && displayScale() !== 'relative') {
    // Asked for the stretch, got the fixed classes — say so instead of leaving
    // a "Relative" control lit over an absolute map.
    note.textContent = 'This server rendered the fixed classes — the relative stretch was not applied.';
    note.classList.add('note--warn');
    note.hidden = false;
  } else if (displayScale() === 'relative') {
    note.textContent = 'Breaks are stretched to this map only — for visual comparison, not for reading absolute suitability.';
    note.classList.remove('note--warn');
    note.hidden = false;
  } else {
    note.textContent = 'Fixed 5 classes including the R0 = 1 threshold — comparable between maps.';
    note.classList.remove('note--warn');
    note.hidden = false;
  }
}

/* Colour scale is a DISPLAY option: it re-renders the raster (the climate read
   is already cached server-side) instead of marking the result stale. */
function syncScaleButtons() {
  $$('#scaleToggle .segmented__opt').forEach((b) => {
    const on = b.dataset.scale === state.scale;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

async function setScale(scale) {
  const prev = state.scale;
  state.scale = scale;
  syncScaleButtons();
  if (!state.hasResult || !state.region || computing) { applyScaleConstraints(); return; }

  computing = true;
  setBusy(true);
  showLoader(scale === 'relative'
    ? 'Re-rendering with a per-map stretch…'
    : 'Re-rendering the fixed classes…', '');
  try {
    const params = computeParams(state.servedSource || state.requestedSource);
    const surface = await fetchSurface(state.region, params);
    if (surface) swapSurface(surface);
    else console.warn('[R0] scale change: no surface returned, keeping previous render');
  } catch (err) {
    console.warn('[R0] scale change failed:', err.message);
    state.scale = prev;
    syncScaleButtons();
  }
  hideLoader();
  computing = false;
  setBusy(false);
  applyScaleConstraints();
}

/* Replace the raster in place — same extent, new colouring. Deliberately does
   NOT re-fit the map: a colour-scale change must not throw away the user's pan
   and zoom. Legend + metadata follow the new response's breaks. */
function swapSurface(surface) {
  state.surface = surface;
  state.layers = (surface && surface.layers) || [];
  clearSurfaceLayers();
  if (state.surfaceActive) {
    addSurfaceLayers(surface);
    if (cellLayer) cellLayer.bringToFront();
  }
  state.legendMap = {};
  displayLegend().forEach((l) => { state.legendMap[l.cls] = l.color; });
  if (cellLayer) cellLayer.setStyle(styleCell);
  refreshLabels();
  const meta = displayMeta();
  renderLegend();
  renderMeta(meta);
  updateContext(meta);
}

function setMonth(m) {
  state.month = m;
  $('#monthValue').textContent = MONTHS[m - 1];
  const range = $('#monthRange');
  range.value = m;                       // keep the thumb in sync (e.g. on init defaults)
  const pct = ((m - 1) / 11) * 100;
  range.style.setProperty('--p', `${pct}%`);
  queryChanged();   // coverage is month-precise, so the month matters here
}

function wireControls() {
  // Region presets. Selection STAGES the query; it never auto-computes — the
  // user still runs it, exactly as with the old single select.
  const preset = (id, codes) => {
    const b = $(id);
    if (b) b.addEventListener('click', () => setSelection(codes()));
  };
  preset('#presetStudy', () =>
    STUDY_CODES.filter(regionByCode));            // guard: only codes we actually have
  preset('#presetAll', () => state.regions.map((r) => r.code));
  preset('#presetClear', () => []);

  // Basemap place-labels toggle
  const labelsBtn = $('#labelsToggle');
  if (labelsBtn) labelsBtn.addEventListener('click', () => setLabels(!state.labelsOn));

  // Climate source — a QUERY param: it stages the change and marks stale.
  const srcSel = $('#sourceSelect');
  if (srcSel) srcSel.addEventListener('change', () => {
    state.source = srcSel.value;
    queryChanged();
  });

  // Mode
  $$('#modeToggle .segmented__opt').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));

  // View (Cells / Smooth)
  $$('#viewToggle .segmented__opt').forEach((b) =>
    b.addEventListener('click', () => { if (!b.disabled) setView(b.dataset.view); }));

  // Colour scale (Absolute / Relative) — a DISPLAY option: re-renders the
  // raster from the cached climate read, never marks the result stale.
  $$('#scaleToggle .segmented__opt').forEach((b) =>
    b.addEventListener('click', () => { if (!b.disabled) setScale(b.dataset.scale); }));

  // Month
  const range = $('#monthRange');
  range.addEventListener('input', () => setMonth(parseInt(range.value, 10)));

  // Year
  // Year — clamped to the WIDEST span any available source offers, so a year
  // that only one source covers stays selectable and the note explains the swap.
  const yearInput = $('#yearInput');
  yearInput.addEventListener('change', () => {
    const [lo, hi] = yearSpan();
    state.year = clamp(parseInt(yearInput.value, 10) || state.year, lo, hi);
    yearInput.value = state.year;
    queryChanged();
  });

  // Occurrences (a DISPLAY option — applies to the current result, no recompute)
  $('#occToggle').addEventListener('change', (e) => {
    state.showOcc = e.target.checked;
    if (state.hasResult) loadOccurrences();
  });

  // Compute (panel CTA + welcome-card primary action)
  $('#computeBtn').addEventListener('click', compute);
  const welcomeBtn = $('#welcomeCompute');
  if (welcomeBtn) welcomeBtn.addEventListener('click', compute);

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
  await loadBasemapConfig();   // must precede initMap — see loadBasemapConfig
  initMap();
  wireControls();

  // Inject NEUTRAL placeholder brand copy from CONFIG (team-lead localises later).
  const titleEl = $('#appTitle'); if (titleEl && CONFIG.brand) titleEl.textContent = CONFIG.brand.title;
  const subEl = $('#appSub');     if (subEl && CONFIG.brand) subEl.innerHTML = CONFIG.brand.sub.replace(/R0\b/, 'R<sub>0</sub>');

  setMonth(7);                 // a representative default month (not computed yet)
  setMode('climatology');
  setView('cells');
  setScale('absolute');
  setLabels(true);             // place-labels ON by default (toggle to hide)

  // Open on the locust belt — an inviting overview, NOT a precomputed country.
  map.fitBounds([[-6, -18], [33, 56]], { padding: [20, 20], animate: false });

  // Regions + climate-source capabilities, in parallel.
  const [regionsRes, sourcesRes] = await Promise.all([
    api.regions().catch((err) => {
      console.error('[R0] could not load regions:', err.message);
      return null;
    }),
    api.sources().catch((err) => {
      // Not an error state: a server without /api/sources simply offers the
      // coarse source, and the control says so rather than inventing options.
      console.info('[R0] /api/sources unavailable — coarse source only:', err.message);
      return null;
    }),
  ]);

  state.regions = (regionsRes && regionsRes.presets) || MOCK_REGIONS;

  const caps = Array.isArray(sourcesRes) ? sourcesRes
    : (sourcesRes && Array.isArray(sourcesRes.sources) ? sourcesRes.sources : null);
  state.sourcesLive = !!(caps && caps.length);
  state.sources = state.sourcesLive ? caps : CONFIG.sourcesFallback.slice();
  renderSourceOptions();

  renderRegionPicker();
  // Pre-select ONE representative country (not computed) so the first run stays
  // a single click and the default experience is unchanged from the old select.
  const first = regionByCode('KEN') || state.regions[0];
  state.selected = first ? [first.code] : [];
  state.region = first || null;
  syncRegionPicker();

  // Year input spans every source on offer; the note explains any substitution.
  const [ylo, yhi] = yearSpan();
  const yi = $('#yearInput');
  if (yi) {
    yi.min = ylo; yi.max = yhi;
    state.year = clamp(state.year, ylo, yhi);
    yi.value = state.year;
  }

  renderCaveats(null);         // seed About copy before first compute
  updateSourceNote();          // source note + year hint + welcome hint
  updateComputeMeta();         // populate the Compute / welcome button labels
  scheduleCoverage();          // ask which source serves the pre-selected query

  // No auto-compute: the user runs the field from the welcome card or the panel CTA.
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
