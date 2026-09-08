"""CHELSA V2.1 high-resolution climate provider (~1 km / 30 arcsec).

CHELSA V2.1 publishes global GeoTIFFs on a public object store. Each file is a
single-band, tiled (512x512), DEFLATE-compressed uint16 raster on a 43200x20880
global 30-arcsec grid, with 7 overview levels. That layout makes partial
(windowed) and decimated reads over HTTP cheap: GDAL's ``/vsicurl/`` driver
fetches only the byte ranges of the tiles it needs, and picks an overview when
the caller asks for a smaller output shape.

URL patterns (verified)
  climatology (1981-2010):
    {BASE}/climatologies/{var}/1981-2010/CHELSA_{var}_{MM}_1981-2010_V.2.1.tif
  monthly time series:
    {BASE}/monthly/{var}/{year}/CHELSA_{var}_{MM}_{year}_V.2.1.tif

Variables used: ``tas`` (near-surface air temperature) and ``hurs`` (near-surface
relative humidity).

UNITS (verified from the band tags — do not "simplify" these)
  tas  : stored in K with Scale 0.1  ->  degC     = raw * 0.1 - 273.15
  hurs : stored in % with Scale 0.01 ->  fraction = raw * 0.01 / 100
  NoData sentinel: 65535 -> NaN
A naive ``tas * 0.01`` yields a physically impossible flat 26-30 degC field;
the correct conversion gives, for Kenya in July, -9.75 degC (Mt Kenya) to
31.05 degC.

Temporal coverage
  climatology : always available (1981-2010 normals)
  monthly     : 1979-2018 complete; 2019 months 01-04 only; 2020+ absent.

Results are cached on disk as compressed .npz under app/data/cache/chelsa/ and
in a small in-process LRU, so a repeat request is near-instant.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .base import ClimateProvider, GridPoint, RasterField

# --- source constants ------------------------------------------------------
BASE_URL = "https://os.unil.cloud.switch.ch/chelsa02/chelsa/global"
CLIMATOLOGY_PERIOD = "1981-2010"
VERSION = "V.2.1"

VAR_T = "tas"
VAR_H = "hurs"

#: NoData sentinel shared by both variables.
NODATA = 65535

#: tas: degC = raw * TAS_SCALE + TAS_OFFSET
TAS_SCALE = 0.1
TAS_OFFSET = -273.15
#: hurs: fraction = raw * HURS_SCALE  (stored percent, Scale 0.01, /100)
HURS_SCALE = 0.01 / 100.0

#: Native pixel size in degrees (30 arcsec).
NATIVE_RES_DEG = 0.0083333333

#: Default cap on the number of pixels returned by ``get_raster``. Bigger
#: regions are decimated on read (which uses the file's overviews).
DEFAULT_MAX_PIXELS = 2_500_000

#: Monthly-series coverage.
MONTHLY_YEAR_MIN = 1979
MONTHLY_YEAR_MAX = 2018
PARTIAL_YEAR = 2019
PARTIAL_YEAR_LAST_MONTH = 4

#: GDAL/curl tuning. Applied per-call via ``rasterio.Env`` (thread-local) so we
#: never mutate the process environment for other GDAL consumers.
GDAL_ENV: Dict[str, Any] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": True,
    "GDAL_CACHEMAX": 512,
}

CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache", "chelsa")
)

#: Bumped when the on-disk array format or the unit conversions change.
CACHE_VERSION = 1

_MEM_CACHE_SIZE = 6


# --- URL construction ------------------------------------------------------
def climatology_url(var: str, month: int) -> str:
    """URL of the 1981-2010 climatology GeoTIFF for ``var`` and ``month``."""
    return (
        f"{BASE_URL}/climatologies/{var}/{CLIMATOLOGY_PERIOD}/"
        f"CHELSA_{var}_{int(month):02d}_{CLIMATOLOGY_PERIOD}_{VERSION}.tif"
    )


def monthly_url(var: str, month: int, year: int) -> str:
    """URL of the monthly-series GeoTIFF for ``var``, ``month`` and ``year``."""
    return (
        f"{BASE_URL}/monthly/{var}/{int(year)}/"
        f"CHELSA_{var}_{int(month):02d}_{int(year)}_{VERSION}.tif"
    )


def build_url(var: str, mode: str, month: int, year: Optional[int] = None) -> str:
    """Resolve the GeoTIFF URL for a variable / time slice."""
    if not 1 <= int(month) <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    if mode == "climatology":
        return climatology_url(var, month)
    if mode == "month":
        if year is None:
            raise ValueError("year is required when mode='month'")
        return monthly_url(var, month, year)
    raise ValueError(f"unknown mode: {mode!r}")


# --- unit conversion -------------------------------------------------------
def _to_float_masked(raw: np.ndarray) -> np.ndarray:
    """uint16 raster -> float32 with the NoData sentinel replaced by NaN."""
    out = raw.astype(np.float32)
    out[raw == NODATA] = np.nan
    return out


def convert_tas(raw: np.ndarray) -> np.ndarray:
    """CHELSA ``tas`` raw counts -> air temperature in degrees Celsius."""
    return _to_float_masked(np.asarray(raw)) * TAS_SCALE + TAS_OFFSET


def convert_hurs(raw: np.ndarray) -> np.ndarray:
    """CHELSA ``hurs`` raw counts -> relative humidity as a fraction 0-1."""
    return _to_float_masked(np.asarray(raw)) * HURS_SCALE


CONVERTERS = {VAR_T: convert_tas, VAR_H: convert_hurs}


# --- decimation ------------------------------------------------------------
def decimated_shape(ny: int, nx: int, max_pixels: int) -> Tuple[int, int]:
    """Largest (ny, nx)-proportional shape whose pixel count fits max_pixels.

    Deterministic: two concurrent reads of the same window with the same cap
    always agree, so ``tas`` and ``hurs`` come back on identical grids without
    having to synchronise the workers.
    """
    ny = int(ny)
    nx = int(nx)
    if ny <= 0 or nx <= 0:
        raise ValueError("empty window")
    if max_pixels is None or ny * nx <= max_pixels:
        return ny, nx
    scale = math.sqrt(float(max_pixels) / float(ny * nx))
    return max(1, int(ny * scale)), max(1, int(nx * scale))


# --- caching ---------------------------------------------------------------
def cache_key(
    bbox,
    mode: str,
    month: int,
    year: Optional[int],
    max_pixels: int,
) -> str:
    """Stable cache key for a raster request."""
    w, s, e, n = bbox
    raw = "v{}|{:.4f},{:.4f},{:.4f},{:.4f}|{}|{:02d}|{}|{}".format(
        CACHE_VERSION,
        float(w), float(s), float(e), float(n),
        mode, int(month),
        "none" if year is None else int(year),
        int(max_pixels),
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"chelsa_{mode}_{int(month):02d}_{digest}"


class ChelsaProvider(ClimateProvider):
    """Climate provider backed by the CHELSA V2.1 global GeoTIFFs."""

    raster_capable = True

    label = "CHELSA V2.1"
    resolution_label = "~1 km (30 arcsec)"

    def __init__(
        self,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        use_cache: bool = True,
        cache_dir: Optional[str] = None,
    ):
        self.max_pixels = int(max_pixels)
        self.use_cache = use_cache
        self.cache_dir = cache_dir or CACHE_DIR
        self._mem: "OrderedDict[str, RasterField]" = OrderedDict()
        self._lock = threading.Lock()

    # -- availability -------------------------------------------------------
    def unsupported_reason(
        self,
        mode: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> Optional[str]:
        """Reason this time slice is unavailable from CHELSA, else None."""
        if mode == "climatology":
            return None
        if mode != "month":
            return f"unknown mode {mode!r}"
        if year is None:
            return "year is required when mode='month'"
        year = int(year)
        if MONTHLY_YEAR_MIN <= year <= MONTHLY_YEAR_MAX:
            return None
        if year == PARTIAL_YEAR:
            if month is None:
                return (
                    f"CHELSA {PARTIAL_YEAR} covers months 01-"
                    f"{PARTIAL_YEAR_LAST_MONTH:02d} only"
                )
            if int(month) <= PARTIAL_YEAR_LAST_MONTH:
                return None
            return (
                f"CHELSA monthly {PARTIAL_YEAR}-{int(month):02d} is not published "
                f"({PARTIAL_YEAR} stops after month "
                f"{PARTIAL_YEAR_LAST_MONTH:02d})"
            )
        return (
            f"CHELSA monthly series covers {MONTHLY_YEAR_MIN}-{MONTHLY_YEAR_MAX} "
            f"(plus {PARTIAL_YEAR} months 01-{PARTIAL_YEAR_LAST_MONTH:02d}); "
            f"{year} is outside it"
        )

    def supports(
        self,
        mode: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> bool:
        return self.unsupported_reason(mode, year, month) is None

    # -- point interface ----------------------------------------------------
    def get_grid(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
    ) -> List[GridPoint]:
        """Point view of the raster (cell centres).

        Provided for interface completeness; the raster interface is the one
        the surface renderer uses. A 1 km field over a country is millions of
        points, so this decimates hard by default.
        """
        field = self.get_raster(
            bbox, mode=mode, month=month, year=year, max_pixels=20_000
        )
        lons, lats = field_coords(field)
        T, H = field["T"], field["H"]
        points: List[GridPoint] = []
        for j in range(field["shape"][0]):
            for i in range(field["shape"][1]):
                t = float(T[j, i])
                h = float(H[j, i])
                points.append(
                    {
                        "lon": float(lons[i]),
                        "lat": float(lats[j]),
                        "t": None if math.isnan(t) else t,
                        "h": None if math.isnan(h) else h,
                    }
                )
        return points

    # -- raster interface ---------------------------------------------------
    def get_raster(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
        max_pixels: Optional[int] = None,
    ) -> RasterField:
        """Read T (degC) and H (fraction) for ``bbox`` from CHELSA."""
        reason = self.unsupported_reason(mode, year, month)
        if reason:
            raise ValueError(reason)
        cap = int(max_pixels or self.max_pixels)
        key = cache_key(bbox, mode, month, year, cap)

        cached = self._mem_get(key)
        if cached is not None:
            return cached
        if self.use_cache:
            cached = self._disk_get(key)
            if cached is not None:
                self._mem_put(key, cached)
                return cached

        field = self._fetch(bbox, mode, month, year, cap)
        if self.use_cache:
            self._disk_put(key, field)
        self._mem_put(key, field)
        return field

    # -- internals ----------------------------------------------------------
    def _fetch(self, bbox, mode, month, year, max_pixels) -> RasterField:
        """Read both variables concurrently and convert to physical units."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                var: pool.submit(
                    _read_window, build_url(var, mode, month, year), bbox, max_pixels
                )
                for var in (VAR_T, VAR_H)
            }
            raw = {var: fut.result() for var, fut in futures.items()}

        t_arr, t_bounds, t_res = raw[VAR_T]
        h_arr, _, _ = raw[VAR_H]

        # Identical source grids + a deterministic decimation rule mean the two
        # reads always agree; trim defensively rather than trust that blindly.
        if t_arr.shape != h_arr.shape:
            ny = min(t_arr.shape[0], h_arr.shape[0])
            nx = min(t_arr.shape[1], h_arr.shape[1])
            t_arr = t_arr[:ny, :nx]
            h_arr = h_arr[:ny, :nx]

        T = convert_tas(t_arr)
        H = convert_hurs(h_arr)
        return {
            "T": T,
            "H": H,
            "bounds": t_bounds,
            "shape": (int(T.shape[0]), int(T.shape[1])),
            "native_res_deg": NATIVE_RES_DEG,
            "res_deg": float(t_res),
        }

    def _mem_get(self, key: str) -> Optional[RasterField]:
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        return None

    def _mem_put(self, key: str, field: RasterField) -> None:
        with self._lock:
            self._mem[key] = field
            self._mem.move_to_end(key)
            while len(self._mem) > _MEM_CACHE_SIZE:
                self._mem.popitem(last=False)

    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.npz")

    def _disk_get(self, key: str) -> Optional[RasterField]:
        path = self._cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            with np.load(path) as data:
                T = data["T"]
                H = data["H"]
                bounds = tuple(float(v) for v in data["bounds"])
                res = float(data["res_deg"])
        except Exception:  # noqa: BLE001 - corrupt / partial cache file
            return None
        return {
            "T": T,
            "H": H,
            "bounds": bounds,  # type: ignore[typeddict-item]
            "shape": (int(T.shape[0]), int(T.shape[1])),
            "native_res_deg": NATIVE_RES_DEG,
            "res_deg": res,
        }

    def _disk_put(self, key: str, field: RasterField) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        path = self._cache_path(key)
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                np.savez_compressed(
                    fh,
                    T=field["T"].astype(np.float32),
                    H=field["H"].astype(np.float32),
                    bounds=np.asarray(field["bounds"], dtype=np.float64),
                    res_deg=np.asarray(field["res_deg"], dtype=np.float64),
                )
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# --- windowed read ---------------------------------------------------------
def _read_window(url: str, bbox, max_pixels: int):
    """Read the ``bbox`` window of one GeoTIFF, decimating to max_pixels.

    Returns (raw_array, (w, s, e, n), res_deg). Row 0 is the northern edge.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window, from_bounds

    w, s, e, n = (float(v) for v in bbox)
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open("/vsicurl/" + url) as src:
            win = from_bounds(w, s, e, n, transform=src.transform)
            col_off = max(0, int(math.floor(win.col_off)))
            row_off = max(0, int(math.floor(win.row_off)))
            col_end = min(src.width, int(math.ceil(win.col_off + win.width)))
            row_end = min(src.height, int(math.ceil(win.row_off + win.height)))
            if col_end <= col_off or row_end <= row_off:
                raise ValueError(
                    f"requested bbox {bbox} does not overlap the CHELSA grid"
                )
            window = Window(col_off, row_off, col_end - col_off, row_end - row_off)
            out_ny, out_nx = decimated_shape(
                int(window.height), int(window.width), max_pixels
            )
            arr = src.read(
                1,
                window=window,
                out_shape=(out_ny, out_nx),
                resampling=Resampling.nearest,
            )
            left, bottom, right, top = src.window_bounds(window)
    res_deg = (right - left) / float(out_nx)
    return arr, (left, bottom, right, top), res_deg


# --- helpers for consumers -------------------------------------------------
def field_coords(field: RasterField):
    """Return (lons, lats) 1-D arrays of pixel CENTRES for a RasterField.

    ``lats`` is north-to-south, matching row order in the arrays.
    """
    w, s, e, n = field["bounds"]
    ny, nx = field["shape"]
    dx = (e - w) / nx
    dy = (n - s) / ny
    lons = w + (np.arange(nx) + 0.5) * dx
    lats = n - (np.arange(ny) + 0.5) * dy
    return lons, lats


def sample_index(field: RasterField, lat: float, lon: float):
    """Return (row, col) for a lat/lon inside ``field``, or None if outside."""
    w, s, e, n = field["bounds"]
    ny, nx = field["shape"]
    if not (w <= lon <= e and s <= lat <= n):
        return None
    col = int((lon - w) / (e - w) * nx)
    row = int((n - lat) / (n - s) * ny)
    col = min(max(col, 0), nx - 1)
    row = min(max(row, 0), ny - 1)
    return row, col
