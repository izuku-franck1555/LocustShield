"""Abstract climate-data provider interface.

Two capabilities are defined:

``get_grid``   (required)  — scattered point samples, one per native cell.
                             Coarse sources (NASA POWER) use this.
``get_raster`` (optional)  — a dense 2-D field on the source's own grid.
                             High-resolution sources (CHELSA) use this so the
                             index can be evaluated per pixel with no
                             interpolation. Providers that cannot serve a
                             raster leave ``raster_capable`` False.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple, TypedDict


class GridPoint(TypedDict):
    """A single climate grid cell.

    lon, lat : cell-centre coordinates (degrees)
    t        : air temperature in degrees Celsius (None if missing)
    h        : relative humidity as a fraction 0-1 (None if missing)
    """

    lon: float
    lat: float
    t: Optional[float]
    h: Optional[float]


class RasterField(TypedDict):
    """A dense climate field covering a bounding box.

    T             : 2-D float array, air temperature in degrees Celsius.
    H             : 2-D float array, relative humidity as a fraction 0-1.
                    Both use NaN for missing / masked pixels and share the
                    same shape and georeferencing.
    bounds        : (west, south, east, north) of the returned array, in
                    degrees. These are the *actual* pixel-edge bounds of the
                    read window, which may extend slightly past the request.
    shape         : (ny, nx) of the arrays. Row 0 is the NORTHERN edge
                    (standard raster orientation).
    native_res_deg: pixel size of the source grid, in degrees.
    res_deg       : pixel size of the returned (possibly decimated) array.
    """

    T: Any
    H: Any
    bounds: Tuple[float, float, float, float]
    shape: Tuple[int, int]
    native_res_deg: float
    res_deg: float


class ClimateProvider(ABC):
    """Provider of gridded temperature/humidity for a bounding box."""

    #: True when the provider implements ``get_raster``.
    raster_capable: bool = False

    @abstractmethod
    def get_grid(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
    ) -> List[GridPoint]:
        """Return grid points covering ``bbox``.

        bbox  : (west, south, east, north) in degrees.
        mode  : "climatology" or "month".
        month : 1-12.
        year  : required when mode == "month".
        """
        raise NotImplementedError

    # -- optional raster capability -----------------------------------------
    def supports(
        self,
        mode: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> bool:
        """Whether this provider has data for the requested time slice."""
        return self.unsupported_reason(mode, year, month) is None

    def unsupported_reason(
        self,
        mode: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> Optional[str]:
        """Human-readable reason the time slice is unavailable, else None."""
        return None

    def get_raster(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
        max_pixels: Optional[int] = None,
    ) -> RasterField:
        """Return a dense T/H field covering ``bbox`` (see ``RasterField``).

        max_pixels caps the returned array size; providers may decimate on
        read to honour it. Raises NotImplementedError when the provider has
        no raster capability.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide raster fields"
        )
