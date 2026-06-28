"""Google Earth Engine climate provider (phase-2 stub)."""

from __future__ import annotations

from typing import List, Optional

from .base import ClimateProvider, GridPoint


class GeeProvider(ClimateProvider):
    """Placeholder for a future Google Earth Engine backend."""

    def get_grid(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
    ) -> List[GridPoint]:
        raise NotImplementedError("phase-2")
