"""FastAPI application for the R0 suitability platform.

Endpoints
  GET  /api/regions       -> preset regions
  GET  /api/r0            -> per-cell R0 GeoJSON for a region/time
  GET  /api/r0_surface    -> interpolated R0 surface PNG (cosmetic smoothing)
  GET  /api/occurrences   -> occurrence points for a region (GeoJSON)
  POST /api/occurrences   -> parse an uploaded CSV into GeoJSON (and persist)

The compiled frontend (app/frontend) is mounted at "/".
"""

from __future__ import annotations

import base64
import csv
import io
import os
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

LEGEND = [
    {"cls": "lt08", "label": "< 0.8", "color": "#2c7bb6"},
    {"cls": "08_1", "label": "0.8 – 1", "color": "#abd9e9"},
    {"cls": "eq1", "label": "= 1", "color": "#ffffbf"},
    {"cls": "1_11", "label": "1 – 1.1", "color": "#fdae61"},
    {"cls": "ge11", "label": "≥ 1.1", "color": "#d7191c"},
]

CAVEATS = [
    "Relative suitability index, not a validated prediction.",
    "Macroclimatic monthly-mean basis; sub-monthly/forecast use is extrapolation.",
    "Climate from NASA POWER (T2M, RH2M).",
    "Index uses reference-script parameters (k=43.13).",
]

app = FastAPI(title="R0 Suitability Platform", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_provider = NasaPowerProvider()

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


def _render_surface_png(Z, inside, ramp):
    """Build an RGBA PNG (north-up) and return base64 string."""
    ny, nx = Z.shape
    rgba = np.zeros((ny, nx, 4), dtype=np.uint8)
    valid = inside & np.isfinite(Z)

    if ramp == "bins":
        cls = _classify_index(Z)
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


@app.get("/api/r0")
def get_r0(
    region: str = Query(..., description="ISO3 region code, e.g. KEN"),
    level: int = Query(0, ge=0, le=2),
    mode: str = Query("climatology", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    ramp: str = Query("bins", pattern="^(bins|continuous)$"),
):
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
                "source": "NASA POWER",
                "resolution": "0.5°×0.625°",
                "n_cells": len(features),
                "caveats": CAVEATS,
            },
        }
    )


@app.get("/api/r0_surface")
def get_r0_surface(
    region: str = Query(..., description="ISO3 region code, e.g. KEN"),
    level: int = Query(0, ge=0, le=2),
    mode: str = Query("climatology", pattern="^(climatology|month)$"),
    month: int = Query(..., ge=1, le=12),
    year: Optional[int] = Query(None),
    ramp: str = Query("bins", pattern="^(bins|continuous)$"),
):
    # 1-4. shared pipeline: boundary + native grid + per-cell R0
    geom, boundary_geojson, bbox, points = _resolve_grid(
        region, level, mode, month, year
    )
    w, s, e, n = bbox

    # native centroids with a valid R0 -> interpolation sources
    lons, lats, vals = [], [], []
    for p in points:
        if p.get("r0_raw") is not None:
            lons.append(p["lon"])
            lats.append(p["lat"])
            vals.append(p["r0_raw"])

    # 2. fine grid (~0.05 deg, capped)
    nx = min(_SURF_MAX_PX, max(2, int(round((e - w) / _SURF_STEP)) + 1))
    ny = min(_SURF_MAX_PX, max(2, int(round((n - s) / _SURF_STEP)) + 1))
    gx = np.linspace(w, e, nx)
    gy = np.linspace(s, n, ny)
    GX, GY = np.meshgrid(gx, gy)

    if vals:
        Z = _interpolate_surface(
            np.asarray(lons), np.asarray(lats), np.asarray(vals), GX, GY
        )
        inside = _points_in_polygon(GX, GY, geom)  # 3. mask to polygon
    else:
        Z = np.full(GX.shape, np.nan)
        inside = np.zeros(GX.shape, dtype=bool)

    image_b64 = _render_surface_png(Z, inside, ramp)  # 4-5. colormap + PNG

    return JSONResponse(
        {
            "image": "data:image/png;base64," + image_b64,
            "bounds": [[s, w], [n, e]],
            "legend": LEGEND,
            "bbox": [w, s, e, n],
            "meta": {
                "region": region,
                "level": level,
                "mode": mode,
                "month": month,
                "year": year,
                "ramp": ramp,
                "source": "NASA POWER",
                "resolution": "0.5°×0.625°",
                "n_cells": len(vals),
                "grid": [nx, ny],
                "step_deg": _SURF_STEP,
                "render": "interpolated-surface",
                "note": "cosmetic interpolation of native 0.5° data; not added resolution",
                "caveats": CAVEATS,
            },
        }
    )


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
