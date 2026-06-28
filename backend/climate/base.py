"""Abstract climate-data provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, TypedDict


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


class ClimateProvider(ABC):
    """Provider of gridded temperature/humidity for a bounding box."""

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
