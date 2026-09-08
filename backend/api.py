"""FastAPI application for the R0 suitability platform.

Endpoints
  GET  /api/regions       -> preset regions
  GET  /api/sources       -> climate-source capabilities
  GET  /api/r0            -> per-cell R0 GeoJSON for a region/time (NASA POWER)
  GET  /api/r0_surface    -> R0 surface PNG (native 1 km from CHELSA, or an
                             interpolated surface from NASA POWER)
  GET  /api/r0_point      -> R0 / T / RH sampled at one lat-lon
  GET  /api/occurrences   -> occurrence points for a region (GeoJSON)
  POST /api/occurrences   -> parse an uploaded CSV into GeoJSON (and persist)

Two climate sources are available and selected with the ``source`` parameter:
  ``chelsa`` CHELSA V2.1, ~1 km, 1981-2010 climatology + 1979-2019/04 monthly.
  ``power``  NASA POWER, 0.5 deg x 0.625 deg, climatology + recent monthly.
  ``auto``   CHELSA where it has data, NASA POWER otherwise (reported in meta).

The compiled frontend (app/frontend) is mounted at "/".
"""

from __future__ import annotations

import base64
import csv
import io
import os
import time
from typing import List, Optional

import numpy as np
import shapely
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from scipy.interpolate import griddata

from . import regions
from .climate import chelsa as chelsa_mod
from .climate.chelsa import ChelsaProvider
from .climate.nasa_power import NasaPowerProvider
from .r0_model import classify_r0, compute_r0

# Half-extent of one native NASA POWER cell (0.5 deg lat x 0.625 deg lon).
_HALF_LON = 0.3125
_HALF_LAT = 0.25

_FRONTEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend")
)
_OCCURRENCE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "occurrences")
)

# --- basemap credentials ---------------------------------------------------
# The basemap tile key is a CLIENT-side credential: the browser requests tiles
# directly from the provider, so the value has to reach the page. It is still
# never hardcoded, never committed and never logged — it comes from the
# environment, and .env / .env.* are excluded from both git and the Docker
# build context.
_BASEMAP_KEY_VAR = "CARTO_API_KEY"
_BASEMAP_PROVIDER = "carto"
_ENV_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _read_env_file_value(name: str, path: str) -> Optional[str]:
    """Read one KEY=value from a local .env, for development convenience.

    A real environment variable always wins; this is only consulted when the
    variable is unset, so Docker / systemd deployments are unaffected. Kept
    dependency-free and deliberately small: blank lines, ``#`` comments, an
    optional ``export`` prefix, and matching surrounding quotes.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value
    return None


def _basemap_key() -> Optional[str]:
    """Current basemap key, or None when unset (keyless fallback).

    Resolved per request rather than at import, so the key can be rotated
    without restarting the service. Never logged.
    """
    value = os.environ.get(_BASEMAP_KEY_VAR)
    if value is None or not value.strip():
        value = _read_env_file_value(_BASEMAP_KEY_VAR, _ENV_FILE)
    value = (value or "").strip()
    return value or None

LEGEND = [
    {"cls": "lt08", "label": "< 0.8", "color": "#2c7bb6"},
    {"cls": "08_1", "label": "0.8 – 1", "color": "#abd9e9"},
    {"cls": "eq1", "label": "= 1", "color": "#ffffbf"},
    {"cls": "1_11", "label": "1 – 1.1", "color": "#fdae61"},
    {"cls": "ge11", "label": "≥ 1.1", "color": "#d7191c"},
]

_CAVEATS_HEAD = [
    "Relative suitability index, not a validated prediction.",
    "Macroclimatic monthly-mean basis; sub-monthly/forecast use is extrapolation.",
]
_CAVEATS_TAIL = [
    "Index uses reference-script parameters (k=43.13).",
    # Property of the model, not of the climate source: applies on both paths.
    "The published figures show large areas in the top class (R0 \u2265 1.1). That "
    "class requires relative humidity above ~93%; measured monthly-mean humidity "
    "reaches only ~75% (CHELSA) / ~83% (NASA POWER), so it does not occur here. "
    "The spatial pattern reproduces the published maps; the absolute class values "
    "do not.",
]
_SOURCE_CAVEAT = {
    "power": "Climate from NASA POWER (T2M, RH2M) at 0.5°×0.625°.",
    "chelsa": (
        "Climate from CHELSA V2.1 (tas, hurs) at ~1 km; the climatology is the "
        "1981-2010 normal and the monthly series ends in April 2019."
    ),
}

# Backwards-compatible default caveat list (NASA POWER wording).
CAVEATS = _CAVEATS_HEAD + [_SOURCE_CAVEAT["power"]] + _CAVEATS_TAIL


def _caveats(source_id: str) -> List[str]:
    return (
        _CAVEATS_HEAD
        + [_SOURCE_CAVEAT.get(source_id, _SOURCE_CAVEAT["power"])]
        + _CAVEATS_TAIL
    )


app = FastAPI(title="R0 Suitability Platform", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_provider = NasaPowerProvider()
_chelsa = ChelsaProvider()

_SOURCE_LABEL = {
    "power": "NASA POWER",
    "chelsa": "CHELSA V2.1",
}
_SOURCE_RESOLUTION = {
    "power": "0.5°×0.625°",
    "chelsa": "~1 km (30 arcsec)",
}

# Ground distance of one degree of latitude. Used only to turn a grid step in
# degrees into a human-readable "~N km", never for computation.
_KM_PER_DEG = 111.32

# A read is "decimated" once its step exceeds the native step by more than
# rounding noise.
_DECIMATION_TOLERANCE = 1.01


def _is_decimated(res_deg: float, native_res_deg: float) -> bool:
    return float(res_deg) > float(native_res_deg) * _DECIMATION_TOLERANCE


def _resolution_label(res_deg: float, native_res_deg: float) -> str:
    """Resolution string that stays honest when a read was decimated.

    Large regions are read at a coarser step to stay inside the pixel budget,
    so the returned grid is NOT always native. Reporting "~1 km (30 arcsec)"
    for those would overstate the map's resolution, so the effective step is
    derived from the actual number rather than hardcoded.
    """
    if not _is_decimated(res_deg, native_res_deg):
        return _SOURCE_RESOLUTION["chelsa"]
    return f"~{float(res_deg) * _KM_PER_DEG:.1f} km (resampled from 30 arcsec)"


def _chelsa_grid_meta(shape, res_deg: float, native_res_deg: float) -> dict:
    """Provenance metadata describing how a CHELSA field was actually read."""
    ny, nx = int(shape[0]), int(shape[1])
    decimated = _is_decimated(res_deg, native_res_deg)
    meta = {
        "shape": [ny, nx],
        "grid": [nx, ny],
        "resolution": _resolution_label(res_deg, native_res_deg),
        "resolution_deg": round(float(res_deg), 6),
        "native_res_deg": float(native_res_deg),
        "decimated": decimated,
        "resampling_factor": round(float(res_deg) / float(native_res_deg), 3),
        "render": "resampled-from-1km" if decimated else "native-1km",
    }
    if decimated:
        # Distinct from the NASA POWER surface note: these are still source
        # pixels, read at a coarser step. Nothing is interpolated or invented.
        meta["note"] = (
            "region too large for a full-resolution read; sampled from the "
            "1 km source at a coarser step (real source pixels, not "
            "interpolation)"
        )
    return meta

# --- surface rendering constants ------------------------------------------
# Class order matches classify_r0 precedence: lt08, 08_1, eq1, 1_11, ge11.
_CLASS_ORDER = ["lt08", "08_1", "eq1", "1_11", "ge11"]
_CLASS_RGB = {
    "lt08": (44, 123, 182),   # #2c7bb6
    "08_1": (171, 217, 233),  # #abd9e9
    "eq1": (255, 255, 191),   # #ffffbf
    "1_11": (253, 174, 97),   # #fdae61
    "ge11": (215, 25, 28),    # #d7191c
}
# Continuous DIVERGING ramp centred on the R0 = 1 threshold, domain [0.8, 1.1],
# clamped outside. Mirrors the frontend rampLow/rampHigh so the PNG matches the
# continuous legend exactly.
_RAMP_MIN, _RAMP_MAX = 0.8, 1.1
# 0.8 -> 1.0: deep-blue -> cream  ['#2c7bb6','#74add1','#eef0c0']
_RAMP_LOW_X = [0.8, 0.9, 1.0]
_RAMP_LOW_R = [44, 116, 238]
_RAMP_LOW_G = [123, 173, 240]
_RAMP_LOW_B = [182, 209, 192]
# 1.0 -> 1.1: cream -> orange -> red  ['#f6e9a0','#fdae61','#d7191c']
_RAMP_HIGH_X = [1.0, 1.05, 1.1]
_RAMP_HIGH_R = [246, 253, 215]
_RAMP_HIGH_G = [233, 174, 25]
_RAMP_HIGH_B = [160, 97, 28]
_SURF_STEP = 0.05          # fine-grid spacing in degrees (~10x native)
_SURF_MAX_PX = 800         # safety cap per axis


# --- source selection ------------------------------------------------------
def _resolve_source(source: str, mode: str, month: int, year: Optional[int]):
    """Pick the climate source actually used. Returns (source_id, note).

    ``power``  always NASA POWER.
    ``chelsa`` CHELSA, or HTTP 400 when CHELSA has no data for the slice.
    ``auto``   CHELSA when it covers the slice, else NASA POWER with a note.
    """
    if source == "power":
        return "power", None
    reason = _chelsa.unsupported_reason(mode, year, month)
    if source == "chelsa":
        if reason:
            raise HTTPException(
                status_code=400,
                detail=f"source=chelsa has no data for this time slice: {reason}",
            )
        return "chelsa", None
    # auto
    if reason:
        return "power", f"CHELSA unavailable ({reason}); served from NASA POWER."
    return "chelsa", None


def _resolve_bbox(region: str, level: int):
    """Resolve a region to (geometry, geojson, bbox); HTTP 404 on failure."""
    try:
        geom, boundary_geojson = regions.get_boundary(region, level)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=404, detail=f"region not found: {region} ({exc})"
        )
    return geom, boundary_geojson, regions.bounds_of(geom)


# --- shared pipeline -------------------------------------------------------
def _resolve_grid(region, level, mode, month, year):
    """Resolve boundary + native grid and attach R0 per cell.

    Returns (geom, boundary_geojson, bbox, points). Each point gains:
      r0 (rounded 3dp or None), r0_raw (float or None), cls (class or None).
    Raises HTTPException on boundary / climate failures.
    """
    if mode == "month" and year is None:
        raise HTTPException(status_code=400, detail="year is required when mode='month'")

    try:
        geom, boundary_geojson = regions.get_boundary(region, level)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=404, detail=f"region not found: {region} ({exc})"
        )
    bbox = regions.bounds_of(geom)

    try:
        points = _provider.get_grid(bbox, mode=mode, month=month, year=year)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"climate fetch failed: {exc}")

    if points:
        T = np.array(
            [p["t"] if p["t"] is not None else np.nan for p in points], dtype=float
        )
        H = np.array(
            [p["h"] if p["h"] is not None else np.nan for p in points], dtype=float
        )
        R = compute_r0(T, H)
        for p, r in zip(points, np.atleast_1d(R)):
            valid = not (r is None or np.isnan(r))
            p["r0"] = round(float(r), 3) if valid else None
            p["r0_raw"] = float(r) if valid else None
            p["cls"] = classify_r0(r)
    return geom, boundary_geojson, bbox, points


def _interpolate_surface(lons, lats, vals, GX, GY):
    """Interpolate scattered (lon, lat, R0) onto a fine grid.

    Tries cubic, then fills any gaps with linear and finally nearest so the
    in-hull surface has no NaN holes.
    """
    pts = np.column_stack([lons, lats])

    def _g(method):
        try:
            return griddata(pts, vals, (GX, GY), method=method)
        except Exception:  # noqa: BLE001 - too few / collinear points
            return np.full(GX.shape, np.nan)

    Z = _g("cubic")
    for method in ("linear", "nearest"):
        nan = ~np.isfinite(Z)
        if not nan.any():
            break
        Zf = _g(method)
        Z[nan] = Zf[nan]
    return Z


def _points_in_polygon(GX, GY, geom):
    """Boolean mask of fine-grid points inside the (Multi)Polygon."""
    try:
        return shapely.contains_xy(geom, GX, GY)
    except Exception:  # noqa: BLE001 - very old shapely fallback
        from shapely.geometry import Point
        from shapely.prepared import prep

        prepared = prep(geom)
        flat = np.array(
            [prepared.contains(Point(x, y)) for x, y in zip(GX.ravel(), GY.ravel())]
        )
        return flat.reshape(GX.shape)


def _raster_polygon_mask(geom, bounds, shape):
    """Boolean "inside the polygon" mask for a north-up raster grid.

    Rasterising the polygon is far cheaper than a point-in-polygon test at
    1 km over a country (millions of pixels). Falls back to the point test if
    rasterio's features module is unavailable.
    """
    w, s, e, n = bounds
    ny, nx = shape
    try:
        from rasterio.features import geometry_mask
        from rasterio.transform import from_bounds as transform_from_bounds

        transform = transform_from_bounds(w, s, e, n, nx, ny)
        return geometry_mask(
            [geom], out_shape=(ny, nx), transform=transform, invert=True
        )
    except Exception:  # noqa: BLE001 - no rasterio.features / bad geometry
        dx = (e - w) / nx
        dy = (n - s) / ny
        lons = w + (np.arange(nx) + 0.5) * dx
        lats = n - (np.arange(ny) + 0.5) * dy
        GX, GY = np.meshgrid(lons, lats)
        return _points_in_polygon(GX, GY, geom)


def _relative_breaks(values):
    """Quintile break points (4 values) of the finite R0 values in one map."""
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    breaks = np.percentile(finite, [20.0, 40.0, 60.0, 80.0])
    # Degenerate fields (near-constant R0) can produce repeated breaks; nudge
    # them apart so np.digitize still yields five usable classes.
    breaks = np.asarray(breaks, dtype=float)
    for i in range(1, breaks.size):
        if breaks[i] <= breaks[i - 1]:
            breaks[i] = np.nextafter(breaks[i - 1], np.inf)
    return breaks


def _classify_by_breaks(Z, breaks):
    """Class index (0-4) per pixel from explicit break points; -1 for NaN."""
    cls = np.full(Z.shape, -1, dtype=np.int8)
    valid = np.isfinite(Z)
    if breaks is None:
        return cls
    idx = np.digitize(np.where(valid, Z, 0.0), breaks, right=False)
    cls[valid] = idx[valid].astype(np.int8)
    return cls


def _relative_legend(breaks, vmin, vmax):
    """Legend rows describing the actual numeric quintile ranges."""
    edges = [vmin] + [float(b) for b in breaks] + [vmax]
    rows = []
    for i, key in enumerate(_CLASS_ORDER):
        lo, hi = edges[i], edges[i + 1]
        rows.append(
            {
                "cls": f"q{i + 1}",
                "label": f"{lo:.3f} – {hi:.3f}",
                "color": "#%02x%02x%02x" % _CLASS_RGB[key],
            }
        )
    return rows


def _classify_index(Z):
    """Vectorized class index (0-4) per pixel; -1 for NaN. Matches classify_r0."""
    cls = np.full(Z.shape, -1, dtype=np.int8)
    valid = np.isfinite(Z)
    eq1 = valid & (np.abs(Z - 1.0) < 0.005)
    lt08 = valid & ~eq1 & (Z < 0.8)
    b081 = valid & ~eq1 & ~lt08 & (Z < 1.0)
    b111 = valid & ~eq1 & ~lt08 & ~b081 & (Z < 1.1)
    ge11 = valid & ~eq1 & ~lt08 & ~b081 & ~b111
    cls[lt08] = 0
    cls[b081] = 1
    cls[eq1] = 2
    cls[b111] = 3
    cls[ge11] = 4
    return cls


def _render_surface_png(Z, inside, ramp, cls_index=None):
    """Build an RGBA PNG (north-up) and return base64 string.

    ``Z`` and ``inside`` are south-up (row 0 = southern edge); the image is
    flipped on save so row 0 becomes the northern edge for Leaflet.
    ``cls_index`` optionally supplies a precomputed 0-4 class index (used by
    the relative/per-map stretch); otherwise the absolute classes are used.
    """
    ny, nx = Z.shape
    rgba = np.zeros((ny, nx, 4), dtype=np.uint8)
    valid = inside & np.isfinite(Z)

    if ramp == "bins":
        cls = _classify_index(Z) if cls_index is None else cls_index
        for idx, key in enumerate(_CLASS_ORDER):
            r, g, b = _CLASS_RGB[key]
            m = valid & (cls == idx)
            rgba[m, 0] = r
            rgba[m, 1] = g
            rgba[m, 2] = b
    else:
        # Diverging ramp split at R0 = 1.0 (low: blue->cream, high: cream->red).
        zc = np.clip(np.where(np.isfinite(Z), Z, 1.0), _RAMP_MIN, _RAMP_MAX)
        low = zc <= 1.0
        high = ~low
        r = np.empty(zc.shape, dtype=float)
        g = np.empty(zc.shape, dtype=float)
        b = np.empty(zc.shape, dtype=float)
        r[low] = np.interp(zc[low], _RAMP_LOW_X, _RAMP_LOW_R)
        g[low] = np.interp(zc[low], _RAMP_LOW_X, _RAMP_LOW_G)
        b[low] = np.interp(zc[low], _RAMP_LOW_X, _RAMP_LOW_B)
        r[high] = np.interp(zc[high], _RAMP_HIGH_X, _RAMP_HIGH_R)
        g[high] = np.interp(zc[high], _RAMP_HIGH_X, _RAMP_HIGH_G)
        b[high] = np.interp(zc[high], _RAMP_HIGH_X, _RAMP_HIGH_B)
        rgba[..., 0] = np.where(valid, r.astype(np.uint8), 0)
        rgba[..., 1] = np.where(valid, g.astype(np.uint8), 0)
        rgba[..., 2] = np.where(valid, b.astype(np.uint8), 0)

    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    # Image row 0 must be the northern edge for Leaflet imageOverlay.
    img = Image.fromarray(np.flipud(rgba), mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --- API routes ------------------------------------------------------------
@app.get("/api/regions")
def get_regions():
    return {"presets": regions.presets()}


@app.get("/api/config")
def get_config():
    """Client configuration. Basemap credentials ONLY — nothing else.

    ``basemap.key`` is null when ``CARTO_API_KEY`` is unset, so the frontend
    falls back to the keyless basemap (watermarked, but fully functional)
    rather than rendering a broken map.

    Deliberately narrow: this endpoint is public, so it must never grow into a
    general environment or settings dump. Anything added here is published to
    every visitor.
    """
    return {"basemap": {"key": _basemap_key(), "provider": _BASEMAP_PROVIDER}}


@app.get("/api/r0")
def get_r0(
    region: str = Query(..., description="ISO3 region code, e.g. KEN"),
    level: int = Query(0, ge=0, le=2),
    mode: str = Query("climatology", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    ramp: str = Query("bins", pattern="^(bins|continuous)$"),
    source: str = Query("power", pattern="^(chelsa|power|auto)$"),
):
    # The per-cell view is a NASA POWER product: one Polygon per native 0.5°
    # cell. A ~1 km CHELSA field is millions of pixels, so it is served as a
    # raster instead (/api/r0_surface) and probed pointwise (/api/r0_point).
    if source == "chelsa":
        raise HTTPException(
            status_code=400,
            detail=(
                "source=chelsa is not available on the per-cell endpoint; use "
                "/api/r0_surface for the 1 km raster and /api/r0_point to probe it"
            ),
        )

    # 1-4. boundary + bbox + tiled NASA POWER fetch + per-cell R0/classify
    geom, boundary_geojson, bbox, points = _resolve_grid(
        region, level, mode, month, year
    )

    # 5. mask points to polygon
    kept = regions.mask_points_to_polygon(points, geom)

    # 6. build a Polygon rectangle per cell
    features = []
    for p in kept:
        lon = p["lon"]
        lat = p["lat"]
        w, e = lon - _HALF_LON, lon + _HALF_LON
        s, n = lat - _HALF_LAT, lat + _HALF_LAT
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[w, s], [e, s], [e, n], [w, n], [w, s]]
                    ],
                },
                "properties": {
                    "r0": p.get("r0"),
                    "t": None if p["t"] is None else round(float(p["t"]), 2),
                    "rh": None if p["h"] is None else round(float(p["h"]) * 100.0, 1),
                    "cls": p.get("cls"),
                },
            }
        )

    return JSONResponse(
        {
            "type": "FeatureCollection",
            "features": features,
            "boundary": boundary_geojson,
            "legend": LEGEND,
            "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
            "meta": {
                "region": region,
                "level": level,
                "mode": mode,
                "month": month,
                "year": year,
                "ramp": ramp,
                "scale": "absolute",
                "source": "NASA POWER",
                "source_id": "power",
                "source_requested": source,
                "resolution": "0.5°×0.625°",
                "n_cells": len(features),
                "caveats": _caveats("power"),
            },
        }
    )


def _surface_from_chelsa(geom, bbox, mode, month, year):
    """Native ~1 km R0 field for ``bbox``.

    Returns (Z, inside, bounds, extra_meta) with Z/inside SOUTH-UP so the
    downstream classify/render path is shared with the NASA POWER surface.
    """
    try:
        field = _chelsa.get_raster(bbox, mode=mode, month=month, year=year)
    except ValueError as exc:  # unsupported time slice
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - network / raster failure
        raise HTTPException(status_code=502, detail=f"CHELSA fetch failed: {exc}")

    R = compute_r0(field["T"], field["H"])          # per-pixel, north-up
    inside = _raster_polygon_mask(geom, field["bounds"], field["shape"])
    extra = _chelsa_grid_meta(
        field["shape"], field["res_deg"], field["native_res_deg"]
    )
    # flip to south-up (row 0 = southern edge) for the shared renderer
    return np.flipud(R), np.flipud(inside), field["bounds"], extra


def _surface_from_power(geom, bbox, mode, month, year, level, region):
    """Interpolated R0 surface from the coarse NASA POWER grid (south-up)."""
    _geom, _bg, _bbox, points = _resolve_grid(region, level, mode, month, year)
    w, s, e, n = bbox

    lons, lats, vals = [], [], []
    for p in points:
        if p.get("r0_raw") is not None:
            lons.append(p["lon"])
            lats.append(p["lat"])
            vals.append(p["r0_raw"])

    nx = min(_SURF_MAX_PX, max(2, int(round((e - w) / _SURF_STEP)) + 1))
    ny = min(_SURF_MAX_PX, max(2, int(round((n - s) / _SURF_STEP)) + 1))
    gx = np.linspace(w, e, nx)
    gy = np.linspace(s, n, ny)
    GX, GY = np.meshgrid(gx, gy)

    if vals:
        Z = _interpolate_surface(
            np.asarray(lons), np.asarray(lats), np.asarray(vals), GX, GY
        )
        inside = _points_in_polygon(GX, GY, geom)
    else:
        Z = np.full(GX.shape, np.nan)
        inside = np.zeros(GX.shape, dtype=bool)

    extra = {
        "shape": [ny, nx],
        "grid": [nx, ny],
        "step_deg": _SURF_STEP,
        # n_cells stays the count of NATIVE source cells (not the interpolated
        # pixels) so the metadata panel keeps reporting real data density.
        "n_cells": len(vals),
        "render": "interpolated-surface",
        "note": "cosmetic interpolation of native 0.5° data; not added resolution",
    }
    return Z, inside, (w, s, e, n), extra


@app.get("/api/r0_surface")
def get_r0_surface(
    region: str = Query(..., description="ISO3 region code, e.g. KEN"),
    level: int = Query(0, ge=0, le=2),
    mode: str = Query("climatology", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    ramp: str = Query("bins", pattern="^(bins|continuous)$"),
    source: str = Query("auto", pattern="^(chelsa|power|auto)$"),
    scale: str = Query("absolute", pattern="^(absolute|relative)$"),
):
    if mode == "month" and year is None:
        raise HTTPException(
            status_code=400, detail="year is required when mode='month'"
        )
    started = time.time()
    source_id, source_note = _resolve_source(source, mode, month, year)
    geom, boundary_geojson, bbox = _resolve_bbox(region, level)

    if source_id == "chelsa":
        Z, inside, out_bounds, extra = _surface_from_chelsa(
            geom, bbox, mode, month, year
        )
    else:
        Z, inside, out_bounds, extra = _surface_from_power(
            geom, bbox, mode, month, year, level, region
        )
    w, s, e, n = out_bounds

    # --- classify -----------------------------------------------------------
    valid = inside & np.isfinite(Z)
    vals_in = Z[valid]
    if scale == "relative":
        breaks = _relative_breaks(vals_in)
        cls_index = _classify_by_breaks(np.where(valid, Z, np.nan), breaks)
        if breaks is None:
            legend = LEGEND
            scale_used = "absolute"
            break_list = None
        else:
            legend = _relative_legend(
                breaks, float(np.min(vals_in)), float(np.max(vals_in))
            )
            scale_used = "relative"
            break_list = [round(float(b), 4) for b in breaks]
    else:
        cls_index = None
        legend = LEGEND
        scale_used = "absolute"
        break_list = None

    image_b64 = _render_surface_png(Z, inside, ramp, cls_index=cls_index)

    n_valid = int(valid.sum())
    meta = {
        "region": region,
        "level": level,
        "mode": mode,
        "month": month,
        "year": year,
        "ramp": ramp,
        "scale": scale_used,
        "source": _SOURCE_LABEL[source_id],
        "source_id": source_id,
        "source_requested": source,
        "resolution": _SOURCE_RESOLUTION[source_id],
        # CHELSA: one 1 km pixel per cell, so n_cells IS the pixel count. The
        # NASA POWER path overrides n_cells below with its native cell count.
        "n_cells": n_valid,
        "n_valid_px": n_valid,
        "r0_min": round(float(np.min(vals_in)), 4) if n_valid else None,
        "r0_max": round(float(np.max(vals_in)), 4) if n_valid else None,
        "r0_mean": round(float(np.mean(vals_in)), 4) if n_valid else None,
        "elapsed_s": round(time.time() - started, 2),
        "caveats": _caveats(source_id),
    }
    meta.update(extra)
    if break_list is not None:
        meta["breaks"] = break_list
    if scale == "relative" and ramp == "continuous":
        meta["scale_note"] = (
            "scale=relative restyles the classified breaks and legend; the "
            "continuous ramp keeps its fixed [0.8, 1.1] domain"
        )
    if source_note:
        meta["source_note"] = source_note

    return JSONResponse(
        {
            "image": "data:image/png;base64," + image_b64,
            "bounds": [[s, w], [n, e]],
            # Same geometry /api/r0 returns, on BOTH source paths, so the
            # surface response is self-contained: the client can draw the
            # country outline without a second call just to get it.
            "boundary": boundary_geojson,
            "legend": legend,
            "bbox": [w, s, e, n],
            "meta": meta,
        }
    )


@app.get("/api/r0_point")
def get_r0_point(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
    region: str = Query(..., description="ISO3 region code, e.g. KEN"),
    level: int = Query(0, ge=0, le=2),
    mode: str = Query("climatology", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    source: str = Query("auto", pattern="^(chelsa|power|auto)$"),
):
    """Sample R0 / T / RH at one coordinate.

    Serves the map hover probe: at 1 km a per-cell GeoJSON is not viable, so
    the client asks for the single pixel under the cursor. Reads the cached
    raster, so it is near-instant once the surface has been rendered.
    Returns nulls (not an error) where there is no data.
    """
    if mode == "month" and year is None:
        raise HTTPException(
            status_code=400, detail="year is required when mode='month'"
        )
    source_id, source_note = _resolve_source(source, mode, month, year)
    geom, _boundary, bbox = _resolve_bbox(region, level)

    t = h = None
    # The probe reads the same raster as the surface, so it inherits the same
    # decimation and must report the same (possibly resampled) resolution.
    grid_meta = None
    if source_id == "chelsa":
        try:
            field = _chelsa.get_raster(bbox, mode=mode, month=month, year=year)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"CHELSA fetch failed: {exc}")
        grid_meta = _chelsa_grid_meta(
            field["shape"], field["res_deg"], field["native_res_deg"]
        )
        idx = chelsa_mod.sample_index(field, lat, lon)
        if idx is not None:
            row, col = idx
            tv = float(field["T"][row, col])
            hv = float(field["H"][row, col])
            t = None if not np.isfinite(tv) else tv
            h = None if not np.isfinite(hv) else hv
    else:
        try:
            points = _provider.get_grid(bbox, mode=mode, month=month, year=year)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"climate fetch failed: {exc}")
        best = None
        best_d = None
        for p in points:
            d = (p["lon"] - lon) ** 2 + (p["lat"] - lat) ** 2
            if best_d is None or d < best_d:
                best, best_d = p, d
        # only accept a hit within roughly one native cell
        if best is not None and best_d is not None and best_d <= (0.6 ** 2):
            t, h = best.get("t"), best.get("h")

    r0 = None
    cls = None
    if t is not None and h is not None:
        val = compute_r0(float(t), float(h))
        if np.isfinite(val):
            r0 = round(float(val), 3)
            cls = classify_r0(val)

    meta = {
        "region": region,
        "mode": mode,
        "month": month,
        "year": year,
        "source": _SOURCE_LABEL[source_id],
        "source_id": source_id,
        "resolution": _SOURCE_RESOLUTION[source_id],
        "lat": lat,
        "lon": lon,
    }
    if grid_meta is not None:
        meta["resolution"] = grid_meta["resolution"]
        meta["resolution_deg"] = grid_meta["resolution_deg"]
        meta["native_res_deg"] = grid_meta["native_res_deg"]
        meta["decimated"] = grid_meta["decimated"]
    if source_note:
        meta["source_note"] = source_note

    return JSONResponse(
        {
            "r0": r0,
            "t": None if t is None else round(float(t), 2),
            "rh": None if h is None else round(float(h) * 100.0, 1),
            "cls": cls,
            "meta": meta,
        }
    )


@app.get("/api/sources")
def get_sources():
    """Climate-source capabilities, for building the source / time pickers.

    ``year_max`` is the last year with ANY data, which for CHELSA is a partial
    year. A picker driven off ``year_max`` alone would offer months that do not
    exist, so coverage is also given month-precisely:

      year_max_complete  last year whose 12 months are all published
      partial_years      {year: {month_min, month_max}} for anything past it

    ``months_for_year(year)`` on the client is then just: full range up to
    ``year_max_complete``, the partial entry if listed, otherwise nothing.
    """
    partial = {
        str(chelsa_mod.PARTIAL_YEAR): {
            "month_min": 1,
            "month_max": chelsa_mod.PARTIAL_YEAR_LAST_MONTH,
        }
    }
    return {
        "default": "auto",
        "modes": ["chelsa", "power", "auto"],
        "sources": [
            {
                "id": "chelsa",
                "label": "CHELSA V2.1",
                "resolution": "~1 km (30 arcsec)",
                "supports_climatology": True,
                "year_min": chelsa_mod.MONTHLY_YEAR_MIN,
                "year_max": chelsa_mod.PARTIAL_YEAR,
                "year_max_complete": chelsa_mod.MONTHLY_YEAR_MAX,
                "partial_years": partial,
                "partial_note": (
                    f"{chelsa_mod.PARTIAL_YEAR} is published for months 01-"
                    f"{chelsa_mod.PARTIAL_YEAR_LAST_MONTH:02d} only; later months "
                    "of that year fall back to NASA POWER."
                ),
                "notes": (
                    "Native ~1 km rendering. Climatology is the 1981-2010 normal. "
                    f"Monthly series is complete for "
                    f"{chelsa_mod.MONTHLY_YEAR_MIN}-{chelsa_mod.MONTHLY_YEAR_MAX}; "
                    f"{chelsa_mod.PARTIAL_YEAR} covers months 01-"
                    f"{chelsa_mod.PARTIAL_YEAR_LAST_MONTH:02d} only and later "
                    "years are not published. First read of a region takes "
                    "seconds; repeats are cached."
                ),
            },
            {
                "id": "power",
                "label": "NASA POWER",
                "resolution": "0.5°×0.625°",
                "supports_climatology": True,
                "year_min": 1981,
                "year_max": None,
                "year_max_complete": None,
                "partial_years": {},
                "partial_note": None,
                "notes": (
                    "Coarse but current: covers recent years CHELSA does not, and "
                    "is the only source for the per-cell view. Surfaces from this "
                    "source are interpolated for appearance, not added resolution. "
                    "year_max is null because coverage tracks the present; the "
                    "most recent months may lag by a few weeks."
                ),
            },
        ],
    }


@app.get("/api/coverage")
def get_coverage(
    mode: str = Query("month", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    source: str = Query("auto", pattern="^(chelsa|power|auto)$"),
):
    """Answer "would this time slice render at 1 km?" without computing it.

    Lets the time picker warn BEFORE the user triggers a multi-second render,
    using exactly the rule the surface endpoint will apply.
    """
    reason = _chelsa.unsupported_reason(mode, year, month)
    chelsa_ok = reason is None
    if source == "power":
        resolved, note = "power", None
    elif source == "chelsa":
        resolved, note = ("chelsa", None) if chelsa_ok else (None, reason)
    else:
        resolved, note = _resolve_source("auto", mode, month, year)
    return {
        "mode": mode,
        "month": month,
        "year": year,
        "source_requested": source,
        "chelsa_available": chelsa_ok,
        "chelsa_reason": reason,
        "resolved_source": resolved,
        "resolved_label": _SOURCE_LABEL[resolved] if resolved else None,
        "resolved_resolution": _SOURCE_RESOLUTION[resolved] if resolved else None,
        "would_error": resolved is None,
        "note": note,
    }


# --- occurrences -----------------------------------------------------------
def _empty_fc() -> dict:
    return {"type": "FeatureCollection", "features": []}


def _rows_to_geojson(rows: List[dict]) -> dict:
    features = []
    for row in rows:
        lat = _to_float(row.get("lat"))
        lon = _to_float(row.get("lon"))
        if lat is None or lon is None:
            continue
        props = {"label": row.get("label")} if row.get("label") else {}
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _to_float(value):
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _read_csv_rows(text: str) -> List[dict]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): v for k, v in raw.items()})
    return rows


@app.get("/api/occurrences")
def get_occurrences(region: str = Query(...)):
    path = os.path.join(_OCCURRENCE_DIR, f"{region}.csv")
    if not os.path.exists(path):
        return _empty_fc()
    with open(path, "r", encoding="utf-8") as fh:
        rows = _read_csv_rows(fh.read())
    return _rows_to_geojson(rows)


@app.post("/api/occurrences")
async def post_occurrences(
    file: UploadFile = File(...),
    region: Optional[str] = Query(None),
):
    raw = (await file.read()).decode("utf-8", errors="replace")
    rows = _read_csv_rows(raw)
    geojson = _rows_to_geojson(rows)
    if region:
        os.makedirs(_OCCURRENCE_DIR, exist_ok=True)
        with open(
            os.path.join(_OCCURRENCE_DIR, f"{region}.csv"), "w", encoding="utf-8"
        ) as fh:
            fh.write(raw)
    return geojson


# --- static frontend (mounted last so /api/* wins) -------------------------
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
