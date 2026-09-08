"""Region boundaries and spatial helpers.

Administrative boundaries are resolved with the following preference order:
  1. GADM via the ``pygadm`` package (primary, level 0-2);
  2. the geoBoundaries open API (fallback, used when GADM is unreachable);
  3. a built-in country bounding box (last resort, presets only).

Resolved boundaries are cached on disk (app/data/cache) so repeated requests
skip the network entirely.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import requests
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

# Level-0 country presets: ISO3 code -> display name.
PRESETS: Dict[str, str] = {
    "MAR": "Morocco",
    "KEN": "Kenya",
    "MLI": "Mali",
    "MRT": "Mauritania",
    "NER": "Niger",
    "DZA": "Algeria",
    "SEN": "Senegal",
    "TCD": "Chad",
    "SDN": "Sudan",
    "SOM": "Somalia",
    "ETH": "Ethiopia",
}

# Last-resort approximate country bounding boxes (w, s, e, n). Only used when
# both GADM and geoBoundaries are unreachable.
FALLBACK_BBOX: Dict[str, Tuple[float, float, float, float]] = {
    "MAR": (-17.1, 21.0, -1.0, 35.95),
    "KEN": (33.9, -4.7, 41.95, 5.55),
    "MLI": (-12.3, 10.1, 4.3, 25.05),
    "MRT": (-17.1, 14.7, -4.8, 27.35),
    "NER": (0.1, 11.65, 16.0, 23.55),
    "DZA": (-8.7, 18.9, 12.0, 37.15),
    "SEN": (-17.6, 12.3, -11.3, 16.75),
    "TCD": (13.4, 7.4, 24.05, 23.5),
    "SDN": (21.8, 8.6, 38.6, 22.25),
    "SOM": (40.9, -1.7, 51.45, 12.05),
    "ETH": (32.9, 3.4, 48.0, 14.95),
}

# Persistent on-disk store of resolved boundaries. This is the LOCAL data
# source: once populated it is consulted before any network call, so the
# platform does not depend on a live remote boundary server (GADM /
# geoBoundaries) at request time. Pre-populate it with:
#     ./.venv/bin/python -m backend.regions
BOUNDARIES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "boundaries")
)


def presets() -> List[Dict[str, str]]:
    """Return preset regions as a list of {code, name} dicts."""
    return [{"code": code, "name": name} for code, name in PRESETS.items()]


# --- local boundary store --------------------------------------------------
def _local_boundary_path(code: str, level: int) -> str:
    """Path of a cached boundary. ``code``/``level`` become a FILENAME.

    Both components are validated because this path is written to, not just
    read: a malformed value creates persistent junk in a tracked directory
    rather than failing. That is not hypothetical — a leaked FastAPI ``Query``
    object once reached here and wrote three files literally named
    ``KEN_annotation=int required=False default=0 alias='level' ...geojson``,
    which would have been committed. Fail loudly instead.
    """
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise TypeError(
            f"boundary level must be an int, got {type(level).__name__}: "
            f"{level!r}"
        )
    if not (isinstance(code, str) and code.isalnum()):
        raise ValueError(f"boundary code must be alphanumeric, got {code!r}")
    return os.path.join(BOUNDARIES_DIR, f"{code}_{level}.geojson")


def _load_local_boundary(code: str, level: int) -> Optional[dict]:
    """Return a locally-stored boundary geometry dict, or None if absent."""
    path = _local_boundary_path(code, level)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_local_boundary(code: str, level: int, geojson: dict) -> None:
    """Persist a resolved boundary to the local store for offline reuse."""
    os.makedirs(BOUNDARIES_DIR, exist_ok=True)
    path = _local_boundary_path(code, level)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh)
    os.replace(tmp, path)


# --- boundary sources ------------------------------------------------------
# Tri-state, process-wide cache of GADM reachability: None unknown / True / False.
_GADM_REACHABLE: Optional[bool] = None


def _gadm_reachable(timeout: float = 4.0) -> bool:
    """Probe the GADM data host once so we can skip the (slow) pygadm path
    when the server is unreachable instead of blocking on its long timeouts."""
    global _GADM_REACHABLE
    if _GADM_REACHABLE is not None:
        return _GADM_REACHABLE
    try:
        requests.head(
            "https://geodata.ucdavis.edu/gadm/gadm4.1/json/", timeout=timeout
        )
        _GADM_REACHABLE = True
    except requests.RequestException:
        _GADM_REACHABLE = False
    return _GADM_REACHABLE


def _gadm_geometry(code: str, level: int):
    """Resolve geometry from GADM via pygadm (tolerating API variants)."""
    import pygadm

    name = PRESETS.get(code, code)
    errors = []
    for kwargs in (
        {"admin": code, "content_level": level},
        {"name": name, "content_level": level},
        {"admin": code},
    ):
        try:
            gdf = pygadm.Items(**kwargs)
            return unary_union(list(gdf.geometry))
        except Exception as exc:  # noqa: BLE001 - probe signatures / network
            errors.append(f"Items({kwargs}): {str(exc)[:80]}")
    raise RuntimeError("pygadm failed: " + " | ".join(errors))


def _geoboundaries_geometry(code: str, level: int):
    """Resolve geometry from the geoBoundaries open API."""
    adm = f"ADM{int(level)}"
    meta = requests.get(
        f"https://www.geoboundaries.org/api/current/gbOpen/{code}/{adm}/",
        timeout=30,
    ).json()
    url = meta.get("simplifiedGeometryGeoJSON") or meta.get("gjDownloadURL")
    if not url:
        raise RuntimeError("geoBoundaries returned no geometry URL")
    gj = requests.get(url, timeout=60).json()
    geoms = [shape(f["geometry"]) for f in gj.get("features", []) if f.get("geometry")]
    if not geoms:
        raise RuntimeError("geoBoundaries returned no geometry")
    return unary_union(geoms)


def get_boundary(code: str, level: int = 0):
    """Return (geometry, geojson_mapping) for a region.

    geometry        : a shapely (Multi)Polygon.
    geojson_mapping : a GeoJSON geometry dict for the response.
    """
    # 1. Local data store first — no network when already populated.
    local = _load_local_boundary(code, level)
    if local is not None:
        return shape(local), local

    errors = []
    geom = None
    # 2. Remote sources. GADM (pygadm) is primary, but only attempt it when the
    #    data host is actually reachable — otherwise pygadm blocks on long
    #    internal timeouts. geoBoundaries is the network fallback.
    sources = []
    if _gadm_reachable():
        sources.append(_gadm_geometry)
    else:
        errors.append("_gadm_geometry: skipped (GADM host unreachable)")
    sources.append(_geoboundaries_geometry)
    for source in sources:
        try:
            geom = source(code, level)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source.__name__}: {str(exc)[:120]}")

    # 3. Last resort: a built-in country bounding box (presets only).
    if geom is None:
        bbox = FALLBACK_BBOX.get(code)
        if bbox is None:
            raise RuntimeError(
                f"could not resolve boundary for {code!r} (level {level}): "
                + " | ".join(errors)
            )
        geom = box(*bbox)

    geojson = mapping(geom)
    # Persist to the local store so subsequent requests are fully offline.
    _save_local_boundary(code, level, geojson)
    return geom, geojson


def bounds_of(geom) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) for a shapely geometry."""
    w, s, e, n = geom.bounds
    return (w, s, e, n)


def tile_bbox(bbox, max_deg: float = 10.0) -> List[Tuple[float, float, float, float]]:
    """Split ``bbox`` (w, s, e, n) into evenly-sized <= max_deg sub-bboxes."""
    import math

    w, s, e, n = bbox
    span_x = max(e - w, 0.0)
    span_y = max(n - s, 0.0)
    nx = max(1, math.ceil(span_x / max_deg)) if span_x > 0 else 1
    ny = max(1, math.ceil(span_y / max_deg)) if span_y > 0 else 1
    dx = span_x / nx
    dy = span_y / ny
    tiles: List[Tuple[float, float, float, float]] = []
    for j in range(ny):
        s0 = s + j * dy
        s1 = n if j == ny - 1 else s + (j + 1) * dy
        for i in range(nx):
            w0 = w + i * dx
            w1 = e if i == nx - 1 else w + (i + 1) * dx
            tiles.append((w0, s0, w1, s1))
    if not tiles:
        tiles.append((w, s, e, n))
    return tiles


def mask_points_to_polygon(points: List[dict], polygon) -> List[dict]:
    """Keep only grid points whose (lon, lat) falls inside ``polygon``."""
    if polygon is None:
        return list(points)
    prepared = prep(polygon)
    kept = []
    for p in points:
        lon = p.get("lon")
        lat = p.get("lat")
        if lon is None or lat is None:
            continue
        if prepared.contains(Point(lon, lat)):
            kept.append(p)
    return kept


def geojson_to_shape(geojson: Optional[dict]):
    """Reconstruct a shapely geometry from a GeoJSON geometry dict."""
    if not geojson:
        return None
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    return shape(geojson)


def prefetch_presets(level: int = 0, overwrite: bool = False) -> Dict[str, str]:
    """Populate the local boundary store for every preset region.

    Run once (online) so the platform can serve boundaries entirely from local
    data afterwards. Returns {code: status} where status is one of
    "cached", "fetched", or "error: ...".
    """
    results: Dict[str, str] = {}
    for code in PRESETS:
        path = _local_boundary_path(code, level)
        if os.path.exists(path) and not overwrite:
            results[code] = "cached"
            continue
        try:
            get_boundary(code, level)
            results[code] = "fetched"
        except Exception as exc:  # noqa: BLE001
            results[code] = f"error: {str(exc)[:100]}"
    return results


if __name__ == "__main__":
    print(f"Populating local boundary store at: {BOUNDARIES_DIR}")
    summary = prefetch_presets()
    for code, status in summary.items():
        print(f"  {code}: {status}")
