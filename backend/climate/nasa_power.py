"""NASA POWER climate-data provider.

Fetches gridded monthly-mean / climatology temperature (T2M) and relative
humidity (RH2M) from the public NASA POWER regional API and returns them as a
unified list of grid points.

API notes (verified):
  * base: https://power.larc.nasa.gov/api/temporal/{climatology|monthly}/regional
  * one parameter per request -> two calls (T2M then RH2M), joined by (lon,lat)
  * response is a GeoJSON FeatureCollection
      feature.geometry.coordinates = [lon, lat, elev]
      feature.properties.parameter.T2M (or RH2M) -> dict of period -> value
      climatology period keys: "JAN".."DEC" (+ "ANN")
      monthly period keys: "YYYYMM" (e.g. "202007")
  * native grid: 0.5 deg lat x 0.625 deg lon
  * missing value sentinel: -999  -> None
  * units: T2M already in degC; RH2M is percent -> H = RH2M / 100
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

import requests

from .. import cache
from .base import ClimateProvider, GridPoint

_BASE = "https://power.larc.nasa.gov/api/temporal/{endpoint}/regional"
_MONTH_KEYS = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]
_MISSING = -999.0
_COORD_ROUND = 4


def _tile_bbox(bbox, max_deg: float = 10.0) -> List[Tuple[float, float, float, float]]:
    """Split ``bbox`` (w, s, e, n) into <= max_deg x max_deg sub-bboxes.

    Tiles are divided *evenly* so no thin sliver tiles are produced: the NASA
    POWER regional endpoint rejects bounding boxes that are too small in one
    dimension (HTTP 422).
    """
    w, s, e, n = bbox
    span_x = max(e - w, 0.0)
    span_y = max(n - s, 0.0)
    nx = max(1, math.ceil(span_x / max_deg)) if span_x > 0 else 1
    ny = max(1, math.ceil(span_y / max_deg)) if span_y > 0 else 1
    dx = span_x / nx if nx else span_x
    dy = span_y / ny if ny else span_y

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


class NasaPowerProvider(ClimateProvider):
    """ClimateProvider backed by the NASA POWER regional API."""

    def __init__(
        self,
        community: str = "AG",
        retries: int = 3,
        delay: float = 1.0,
        timeout: float = 60.0,
        use_cache: bool = True,
    ):
        self.community = community
        self.retries = retries
        self.delay = delay
        self.timeout = timeout
        self.use_cache = use_cache

    # -- public API ---------------------------------------------------------
    def get_grid(
        self,
        bbox,
        mode: str,
        month: int,
        year: Optional[int] = None,
    ) -> List[GridPoint]:
        endpoint, period_key, year_for_key = self._resolve(mode, month, year)

        merged: Dict[Tuple[float, float], GridPoint] = {}
        for chunk in _tile_bbox(bbox, max_deg=10.0):
            t_vals = self._fetch_param(endpoint, "T2M", chunk, period_key, year_for_key)
            h_vals = self._fetch_param(endpoint, "RH2M", chunk, period_key, year_for_key)
            keys = set(t_vals) | set(h_vals)
            for (lon, lat) in keys:
                t = t_vals.get((lon, lat))
                rh = h_vals.get((lon, lat))
                h = None if rh is None else rh / 100.0
                merged[(lon, lat)] = {
                    "lon": lon,
                    "lat": lat,
                    "t": t,
                    "h": h,
                }
        return list(merged.values())

    # -- internals ----------------------------------------------------------
    @staticmethod
    def _resolve(mode: str, month: int, year: Optional[int]):
        if not 1 <= int(month) <= 12:
            raise ValueError(f"month must be 1-12, got {month}")
        if mode == "climatology":
            return "climatology", _MONTH_KEYS[int(month) - 1], None
        if mode == "month":
            if year is None:
                raise ValueError("year is required when mode='month'")
            return "monthly", f"{int(year)}{int(month):02d}", int(year)
        raise ValueError(f"unknown mode: {mode!r}")

    def _fetch_param(
        self,
        endpoint: str,
        param: str,
        chunk_bbox,
        period_key: str,
        year_for_key: Optional[int],
    ) -> Dict[Tuple[float, float], Optional[float]]:
        """Fetch one parameter for one chunk; return {(lon,lat): value|None}."""
        payload = None
        cache_key = cache.make_key(endpoint, param, chunk_bbox, year_for_key)
        if self.use_cache:
            payload = cache.get(cache_key)

        if payload is None:
            payload = self._request(endpoint, param, chunk_bbox, year_for_key)
            if self.use_cache and payload is not None:
                cache.put(cache_key, payload)

        return self._parse(payload, param, period_key)

    def _request(
        self,
        endpoint: str,
        param: str,
        chunk_bbox,
        year_for_key: Optional[int],
    ):
        w, s, e, n = chunk_bbox
        url = _BASE.format(endpoint=endpoint)
        params = {
            "parameters": param,
            "community": self.community,
            "longitude-min": w,
            "longitude-max": e,
            "latitude-min": s,
            "latitude-max": n,
            "format": "JSON",
        }
        if endpoint == "monthly":
            params["start"] = year_for_key
            params["end"] = year_for_key

        last_exc = None
        for attempt in range(self.retries):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                time.sleep(self.delay)
                return data
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                time.sleep(self.delay * (attempt + 1))
        raise RuntimeError(
            f"NASA POWER request failed for {param} {chunk_bbox}: {last_exc}"
        )

    @staticmethod
    def _parse(
        payload,
        param: str,
        period_key: str,
    ) -> Dict[Tuple[float, float], Optional[float]]:
        out: Dict[Tuple[float, float], Optional[float]] = {}
        if not payload:
            return out
        for feat in payload.get("features", []):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lon = round(float(coords[0]), _COORD_ROUND)
            lat = round(float(coords[1]), _COORD_ROUND)
            props = feat.get("properties") or {}
            param_block = (props.get("parameter") or {}).get(param) or {}
            raw = param_block.get(period_key)
            if raw is None:
                val: Optional[float] = None
            else:
                fval = float(raw)
                val = None if fval <= _MISSING + 1e-6 else fval
            out[(lon, lat)] = val
        return out
