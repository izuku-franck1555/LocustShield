# LocustShield

**LocustShield** is a decision-support web map for planning biological control
of the desert locust (*Schistocerca gregaria*). It computes **R0**, the basic
reproduction number of the entomopathogenic fungus *Metarhizium acridum* acting
against the locust, as a closed-form function of air temperature and relative
humidity (Kamga et al. 2022, *Scientific Reports* 12:7535). R0 summarises how
climatically favourable a country and month are for this microbial biocontrol
agent to establish against the pest — higher R0 (≳ 1) flags where it is more
likely to be effective. LocustShield evaluates R0 over **live NASA POWER**
temperature and humidity for a chosen country and renders it as an interactive
Leaflet map.

Technically, R0 is a published environmental-suitability index — a closed-form
function of two climate variables: air temperature *T* (°C) and relative
humidity *H* (fraction 0–1). For a selected region and time, the backend fetches
gridded *T* and *H* from the public NASA POWER API, evaluates R0 per grid cell,
and returns GeoJSON (plus an optional smooth surface PNG) for the map.

> R0 is a **relative suitability index, not a validated prediction**. The
> macroclimatic monthly-mean basis means sub-monthly / forecast use is
> extrapolation. The index uses reference-script parameters (k = 43.13).

## Layout

```
app/
  backend/
    r0_model.py          # compute_r0 / classify_r0 (numpy-vectorized)
    regions.py           # GADM/geoBoundaries boundaries + spatial helpers
    cache.py             # on-disk JSON cache for climate responses
    api.py               # FastAPI app (also serves the frontend)
    climate/
      base.py            # ClimateProvider ABC
      nasa_power.py      # NASA POWER provider (T2M + RH2M)
      gee.py             # Google Earth Engine provider (phase-2 stub)
  frontend/              # static Leaflet single-page app (served at /)
  data/
    boundaries/          # local country-boundary store (geoBoundaries, CC BY 4.0)
    cache/               # cached climate responses (runtime, gitignored)
    occurrences/         # <CODE>.csv occurrence files (cols: lat,lon[,label])
  tests/                 # pytest suite
  requirements.txt
  run.sh                 # dev launcher
  Dockerfile             # container image (python:3.12-slim)
  docker-compose.yml     # one-command deploy
  DEPLOY.md              # VPS deployment guide
```

## Setup & run

```bash
cd app
./run.sh                 # creates .venv on first run, then serves on :8000
```

Then open <http://127.0.0.1:8000/>. The API lives under `/api`.

Manual setup:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/uvicorn backend.api:app --reload
```

### Configuration

One optional environment variable, `CARTO_API_KEY` — a CARTO Basemaps key that
removes the "API KEY REQUIRED" watermark from the basemap tiles.

```bash
cp .env.example .env      # .env is gitignored — never commit a real key
$EDITOR .env              # CARTO_API_KEY=<your key>
```

Leaving it unset is safe: the map still works, just watermarked. The key is
read per request (so it can be rotated without a restart), a real environment
variable takes precedence over `.env`, and it is served to the page by
`GET /api/config`. That last part is by design — the browser fetches tiles
directly from CARTO, so the credential is client-side and cannot be hidden;
scope and rotate it at the provider rather than trying to conceal it.
`.env` and `.env.*` are excluded from both git and the Docker build context.

> **Attribution is a licence condition.** CARTO's free tier (5,000,000 tile
> requests/month) requires the CARTO **and** OpenStreetMap attribution to
> remain visible on the map. The attribution line in `frontend/app.js`
> satisfies this — do not remove it.

## Deploy to a server

For VPS deployment (Docker Compose or bare-metal systemd, with an Nginx
reverse-proxy and optional TLS), see **[DEPLOY.md](DEPLOY.md)**. The quickest
path is `docker compose up -d --build`, which serves the app on `:8000`.

## The R0 model

With *A* = 0.871:

```
omega(T)     = exp(0.00938*T) - exp(0.00938*43.13143 - (43.13143 - T)/4.8372) - 1.09599
epsilon(T,H) = 1e-4 * exp(13.94*H) * (exp(0.0015*T) - exp(0.0015*266.881 - (266.881 - T)/111.142) - 0.867)
R0(T,H)      = A / ((1 - omega(T)) * (1 - epsilon(T,H)))
```

`compute_r0(T, H)` accepts scalars or numpy arrays and returns `np.nan` for
masked cells (H outside [0,1], T outside [-30, 60] °C, missing inputs, or a
denominator `(1-omega)*(1-epsilon) <= 1e-3`). `classify_r0` bins a value into
`lt08`, `08_1`, `eq1`, `1_11`, `ge11`, or `None`.

## API

| Method | Path | Notes |
| ------ | ---- | ----- |
| GET | `/api/regions` | `{ "presets": [{code, name}, ...] }` |
| GET | `/api/config` | `{ "basemap": {"key": <str\|null>, "provider": "carto"} }` — basemap credentials only |
| GET | `/api/sources` | climate-source capabilities for the source picker |
| GET | `/api/coverage` | `mode`, `month`, `year`, `source` — which source a slice would use, without rendering it |
| GET | `/api/r0` | params: `region`, `level` (0), `mode` (`climatology`\|`month`), `month` (1–12), `year` (for `month`), `ramp` (`bins`\|`continuous`), `source` (default `power`) |
| GET | `/api/r0_surface` | `region` **or** `regions` (multi), plus `source` (`chelsa`\|`power`\|`auto`, default `auto`) and `scale` (`absolute`\|`relative`, default `absolute`) |
| GET | `/api/r0_point` | `lat`, `lon`, `region` **or** `regions`, `level`, `mode`, `month`, `year`, `source` — one sampled pixel |
| GET | `/api/occurrences?region=CODE` | GeoJSON points from `data/occurrences/<CODE>.csv` |
| POST | `/api/occurrences` | multipart CSV upload (`file`); optional `?region=CODE` persists it |

`/api/r0` returns a `FeatureCollection` of one Polygon per native cell
(0.5° lat × 0.625° lon) with properties `{r0, t, rh, cls}`, plus `boundary`,
`legend`, `bbox`, and a `meta` block (mode, month, year, source, resolution,
n_cells, caveats). This view is NASA POWER only — `source=chelsa` returns 400,
because a ~1 km field is millions of pixels; use `/api/r0_surface` for the
raster and `/api/r0_point` to probe it.

`/api/r0_surface` returns a country-clipped choropleth as a base64 PNG:
`{ image, bounds: [[s,w],[n,e]], boundary, legend, bbox: [w,s,e,n], meta }`,
sized for a Leaflet `imageOverlay`. `boundary` is the same GeoJSON geometry
`/api/r0` returns for that region and level, on **both** source paths, so the
response is self-contained: a client can draw the country outline without a
second call, and the outline is identical in the cells and surface views.
Two render paths, chosen by `source`:

* **CHELSA** — R0 is evaluated per source pixel, classified, masked to the
  polygon and coloured. Nothing is interpolated. A region small enough to read
  in full reports `meta.render = "native-1km"` and
  `meta.resolution = "~1 km (30 arcsec)"`; a region too large for the pixel
  budget is read at a coarser step and says so —
  `meta.render = "resampled-from-1km"`,
  `meta.resolution = "~1.1 km (resampled from 30 arcsec)"` (derived from the
  actual step, so it scales), plus a `meta.note`. Either way `meta` carries
  `shape` `[ny, nx]`, `resolution_deg`, `native_res_deg`, `decimated` and
  `resampling_factor`. **A decimated map must never claim native resolution:**
  those are still real source pixels, just sampled at a coarser step, and the
  strings are what the UI shows the user. `/api/r0_point` reads the same raster
  and reports the same resolution fields.
* **NASA POWER (`meta.render = "interpolated-surface"`)** — unchanged: the
  native 0.5° field is interpolated (scipy `griddata`, cubic → linear →
  nearest) onto a ~0.05° grid. `meta.note` still flags that this is **cosmetic
  smoothing of coarse data, not added resolution**.

Both paths honour `ramp` (`bins` = the five suitability classes; `continuous` =
a diverging blue→cream→red ramp centred on R0 = 1 over R0 ∈ [0.8, 1.1]) and
report the source actually used in `meta.source` / `meta.source_id`.

`scale` controls the class breaks and the legend:

* `absolute` (default) — the fixed thresholds `< 0.8`, `0.8–1`, `= 1`, `1–1.1`,
  `≥ 1.1`. Comparable across regions and months.
* `relative` — quintiles of the finite R0 values **in this map only**. The
  legend rows carry the real numeric ranges (e.g. `0.783 – 0.968`) with class
  ids `q1…q5`, and `meta.breaks` lists the break points. Use it to see
  structure in a field that clusters near R0 = 1; do not compare two relative
  maps to each other. (It restyles the classified render; the continuous ramp
  keeps its fixed domain.)

### Multiple regions

`/api/r0_surface?regions=KEN,ETH,SOM` computes several countries at once and
returns them as **layers** — one raster per country, not one raster over the
union:

```jsonc
{
  "layers": [ {"code","name","image","bounds","boundary","meta"}, ... ],
  "failed": [ {"code","name","reason"}, ... ],   // [] when all succeeded
  "bbox": [w,s,e,n],   // union, for fitting the map only — never read from
  "legend": [...],     // shared across layers
  "meta": { ...aggregate... }
}
```

**Partial success.** If some regions compute and others fail, the response is
still a 200 carrying the layers that worked, with every failure named in
`failed[]`. One region's transient fetch error does not discard four good
results, and nothing is degraded or dropped silently. Aggregates describe what
actually rendered (`regions`, `n_regions`) alongside what was asked for
(`regions_requested`, `n_requested`, `n_failed`), so a partial result is never
ambiguous. If **every** region fails the request errors rather than returning
an empty success. An impossible time slice is caught up front as a 400 — the
same status the single-region path returns — because source availability is
temporal and does not depend on the region.

**Why per-country rather than a union bbox.** A union over a non-adjacent
selection is mostly empty ocean and desert, and its size forces heavy
decimation. Measured, Morocco + Kenya: the union spans 59° × 41° ≈ **34.5 M
pixels** at native 1 km, whereas reading each country separately touches
**3.7 M** — 9.4× less work — and keeps Kenya at full native resolution instead
of degrading it to fit a box that is mostly sea.

* Countries are fetched and computed **concurrently** (pool of 4). Measured on
  cold data, three countries take 5.2 s together versus 9.0 s as three separate
  single-region calls; warm, 0.42 s. Wall time tracks the slowest country, not
  the sum. Caching is per country, so an already-computed country is instant.
* Order is preserved, duplicates collapse, and the cap is
  `MAX_REGIONS` (12) — beyond it, a 400. Unknown codes 400 *before* any climate
  work, because boundaries resolve first.
* `region=` and `regions=` are mutually exclusive; single-region responses are
  byte-compatible with before.

**Provenance stays per layer.** Each layer's `meta` carries its own `source`,
`resolution`, `render` and R0 statistics. The aggregate `meta` reports
`per_region`, plus `mixed_sources` / `mixed_resolutions`; when they differ the
aggregate says `"mixed"` rather than picking one source and implying it covers
everything, and `provenance_note` names what differs — resolutions when the
source is uniform, sources otherwise. A mixed-source selection carries both
sources' caveats.

**`scale=relative` pools across layers.** The legend is shared, so the stretch
must be too: breaks are quintiles of every selected region's values combined
(successful ones only). Per-layer stretches would each be honest alone but
would make one legend describe five different mappings, and the layers would
not be comparable to each other. `meta.scale_note` states the pooling
explicitly so nobody assumes per-country stretching.

`/api/r0_point?regions=...` resolves the probe to whichever selected country
contains the point and samples that country's raster — the same one its layer
was rendered from — returning clean nulls (HTTP 200) outside them all.

`/api/coverage?regions=...` adds a per-region resolution forecast. CHELSA
availability is temporal, but *resolution* is regional, since a large country
gets decimated — so the UI can warn "2 of 4 at native 1 km" before a
multi-second render. The estimate is computed from bbox size and the pixel
budget alone, needs no network, and is asserted against a real read in the
tests.

`/api/r0_point` samples a single coordinate from the cached raster —
`{ r0, t, rh, cls, meta }`, with `rh` in percent and all four null (HTTP 200,
not an error) outside the data. It replaces per-cell GeoJSON for hover at 1 km.

Example:

```bash
curl 'http://127.0.0.1:8000/api/r0?region=KEN&mode=climatology&month=7'
curl 'http://127.0.0.1:8000/api/r0_surface?region=KEN&mode=climatology&month=7&source=chelsa'
curl 'http://127.0.0.1:8000/api/r0_point?lat=-0.15&lon=37.31&region=KEN&mode=climatology&month=7'
```

## Climate data

Two sources. `source=auto` (the `/api/r0_surface` default) picks CHELSA
wherever it has data and falls back to NASA POWER otherwise, reporting the
choice — and the reason for any fallback — in `meta.source` /
`meta.source_note`.

### CHELSA V2.1 — primary, ~1 km

Global 30-arcsec GeoTIFFs on a public object store, read directly over HTTP:

* climatology `…/climatologies/{var}/1981-2010/CHELSA_{var}_{MM}_1981-2010_V.2.1.tif`
* monthly     `…/monthly/{var}/{year}/CHELSA_{var}_{MM}_{year}_V.2.1.tif`
* variables `tas` (temperature) and `hurs` (relative humidity), fetched
  concurrently.

The files are tiled (512×512), DEFLATE-compressed and carry 7 overview levels,
so `rasterio` + `/vsicurl/` reads only the byte ranges for the requested
window, and decimates on read (via `out_shape`) to keep the returned array
under ~2.5 M pixels. Kenya fits at native 1 km; Mali and other large countries
come back at ~1.1 km.

**Unit conversions (from the band tags — do not simplify):**

| var | stored | conversion |
| --- | ------ | ---------- |
| `tas` | K, scale 0.1 | `°C = raw * 0.1 - 273.15` |
| `hurs` | percent, scale 0.01 | `fraction = raw * 0.01 / 100` |

NoData is `65535` → NaN. A naive `tas * 0.01` yields a physically impossible
flat 26–30 °C field; the correct conversion gives −9.75 °C (Mt Kenya) to
31.05 °C for Kenya in July.

**Coverage:** climatology always; monthly complete for 1979–2018, 2019 months
01–04 only, 2020 onward absent. `ChelsaProvider.supports(mode, year, month)`
encodes this, and the API falls back to NASA POWER (with a reason) rather than
failing.

Because the last year is **partial**, `/api/sources` reports coverage
month-precisely — a picker built on `year_max` alone would offer 2019 months
that do not exist:

| field | chelsa | meaning |
| --- | --- | --- |
| `year_min` | 1979 | first year with any data |
| `year_max` | 2019 | last year with **any** data |
| `year_max_complete` | 2018 | last year with all 12 months |
| `partial_years` | `{"2019": {"month_min": 1, "month_max": 4}}` | months available past `year_max_complete` |
| `partial_note` | *"2019 is published for months 01–04 only…"* | display string |

So the client's selectable months for a year are: the full range up to
`year_max_complete`, the `partial_years` entry if listed, otherwise none.

`GET /api/coverage?mode=&month=&year=&source=` answers the same question for one
slice without rendering it — useful to warn *before* a multi-second render:
`{mode, month, year, source_requested, chelsa_available, chelsa_reason,
resolved_source, resolved_label, resolved_resolution, would_error, note}`.
It applies exactly the rule `/api/r0_surface` will apply, so the two cannot
drift.

Results are cached as compressed `.npz` under `data/cache/chelsa/` plus a small
in-process LRU, so a repeat request is near-instant.

### NASA POWER — recent years and the per-cell view

NASA POWER regional API, endpoints
`temporal/{climatology|monthly}/regional`, parameters `T2M` (°C, used as-is)
and `RH2M` (percent → fraction). The API allows one parameter per call, so the
provider issues two calls and joins by `(lon, lat)`. Bounding boxes are tiled
into ≤ 10° × 10° chunks, fetched with retries and a short inter-call delay, and
cached on disk. Coarse (0.5° × 0.625°) but current, and the only source for
`/api/r0`.

## Boundaries

Boundary resolution is **local-data first** so the platform does not depend on
a live remote boundary server at request time:

1. **Local store** — `data/boundaries/<CODE>_<LEVEL>.geojson`. Consulted first;
   no network when populated.
2. **GADM** via `pygadm` — primary remote source, attempted only when the GADM
   data host is reachable (a fast probe avoids blocking on its long timeouts).
3. **geoBoundaries** open API — network fallback when GADM is unreachable.
4. **Built-in bounding box** — last resort for presets (`FALLBACK_BBOX`).

Any boundary resolved over the network is written to the local store, so the
first online run permanently populates it. Pre-populate all presets at once:

```bash
./.venv/bin/python -m backend.regions
```

> Note: in the current environment the GADM data host
> (`geodata.ucdavis.edu`) is unreachable, so boundaries are sourced from
> geoBoundaries and then served from the local store.

## Tests

```bash
cd app
./.venv/bin/python -m pytest -q
```

## Verification

End-to-end checks performed for the integration pass:

- **Unit tests** — `pytest` passes (14/14): `compute_r0` numeric values,
  `classify_r0` class boundaries, input masking, and numpy vectorization.
- **Model fidelity** — `compute_r0` / `classify_r0` are an exact port of the
  reference script `mappingEPF_Ro.R::estimate_Ro`
  (ρ = 0.00938, k = 43.13143, δ = 4.8372, λ = −1.09599; second term with
  0.0015 / 266.881 / 111.142 / −0.867; `R0 = 0.871 / ((1−ω)·(1−ε))`).
- **API + UI** — `uvicorn backend.api:app` serves the SPA at `/`; with
  `CONFIG.USE_MOCK = false` the frontend reads `/api/regions`, `/api/r0`,
  `/api/occurrences` from the same origin. Verified in-browser for KEN and MAR:
  live GADM/geoBoundaries boundaries, per-cell classification, response-driven
  legend, hover readout (R0 · T · RH), classified/continuous ramp toggle, and
  the loading state — with no console errors.

> **Read the maps as pattern, not cell-for-cell absolute values.** The model is
> a line-for-line port, so the spatial *pattern* reproduces the paper (cool
> highlands less suitable, hot lowlands more suitable). What differs is the
> climate *source*: this platform reads live NASA POWER monthly-mean / climatology
> at 0.5° × 0.625°, whereas the paper used ~1 km CHELSA temperature with a higher
> relative-humidity climatology. Because NASA POWER monthly-mean RH rarely exceeds
> ~90% and the red "≥ 1.1" class needs RH ≳ 93%, our absolute R0 runs about one
> class lower than the paper — e.g. Kenya/July reads orange "1 – 1.1" where the
> paper reads red, and the field clusters in the 0.8–1.1 range around R0 ≈ 1.
> Paper-level resolution and magnitudes are the goal of the Google Earth Engine
> phase-2 backend (`climate/gee.py`).

## Data attribution & citation

- **Model** — R0 formulation from Kamga, S. F., Ndjomatchoua, F. T., Guimapi,
  R. A., Klingen, I., Tchawoua, C., Roer Hjelkrem, A.-G., Thunes, K. H. &
  Kakmeni, F. M. (2022). *The effect of climate variability in the efficacy of
  the entomopathogenic fungus* Metarhizium acridum *against the desert locust*
  Schistocerca gregaria. **Scientific Reports** 12:7535.
  <https://doi.org/10.1038/s41598-022-11424-0>
- **Climate** — temperature (T2M) and relative humidity (RH2M) from the
  **NASA POWER** project (<https://power.larc.nasa.gov>), keyless and free to use.
- **Boundaries** — country polygons from **geoBoundaries** (Runfola et al.),
  released under **CC BY 4.0** (<https://www.geoboundaries.org>); GADM is the
  optional primary source when reachable.

## Phase 2

`climate/gee.py` is a stub (`NotImplementedError("phase-2")`) reserved for a
Google Earth Engine backend.
