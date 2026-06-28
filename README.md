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
| GET | `/api/r0` | params: `region`, `level` (0), `mode` (`climatology`\|`month`), `month` (1–12), `year` (for `month`), `ramp` (`bins`\|`continuous`) |
| GET | `/api/r0_surface` | same params as `/api/r0`; returns a smooth, country-clipped R0 surface PNG |
| GET | `/api/occurrences?region=CODE` | GeoJSON points from `data/occurrences/<CODE>.csv` |
| POST | `/api/occurrences` | multipart CSV upload (`file`); optional `?region=CODE` persists it |

`/api/r0` returns a `FeatureCollection` of one Polygon per native cell
(0.5° lat × 0.625° lon) with properties `{r0, t, rh, cls}`, plus `boundary`,
`legend`, `bbox`, and a `meta` block (mode, month, year, source, resolution,
n_cells, caveats). The frontend uses it for hover / cell query.

`/api/r0_surface` returns a paper-style smooth choropleth as a base64 PNG:
`{ image, bounds: [[s,w],[n,e]], legend, bbox: [w,s,e,n], meta }`. The native
0.5° R0 field is interpolated (scipy `griddata`, cubic → linear → nearest) onto
a ~0.05° grid, clipped to the country polygon (transparent outside), and
coloured by class (`ramp=bins`) or a continuous diverging blue→cream→red ramp
centred on the R0 = 1 threshold over R0 ∈ [0.8, 1.1] (`ramp=continuous`,
matching the frontend legend). The image is sized for Leaflet `imageOverlay`
with `bounds`. `meta.render = "interpolated-surface"` and `meta.note` flag that
this is **cosmetic smoothing of coarse data, not added resolution** (true ~1 km
is the GEE phase-2).

Example:

```bash
curl 'http://127.0.0.1:8000/api/r0?region=KEN&mode=climatology&month=7'
```

## Climate data

NASA POWER regional API, endpoints
`temporal/{climatology|monthly}/regional`, parameters `T2M` (°C, used as-is)
and `RH2M` (percent → fraction). The API allows one parameter per call, so the
provider issues two calls and joins by `(lon, lat)`. Bounding boxes are tiled
into ≤ 10° × 10° chunks, fetched with retries and a short inter-call delay, and
cached on disk.

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
