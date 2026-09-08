"""Tests for the CHELSA V2.1 climate provider (offline: no network).

Covers the three things that are easy to get silently wrong:
  * the raw -> physical-unit conversions (a naive scale factor produces a
    plausible-looking but physically impossible field),
  * NoData handling,
  * the temporal-coverage rules that decide when the API falls back to the
    coarse source.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.climate import chelsa  # noqa: E402
from backend.climate.chelsa import ChelsaProvider  # noqa: E402


# --- unit conversion: tas -> degrees Celsius -------------------------------
def test_convert_tas_freezing_point():
    # 273.15 K stored with scale 0.1 -> raw 2731.5; 2732 -> ~0.05 degC
    assert chelsa.convert_tas(np.array([2732], dtype=np.uint16))[0] == pytest.approx(
        0.05, abs=0.01
    )


def test_convert_tas_known_values():
    raw = np.array([2634, 3042], dtype=np.uint16)  # observed Kenya July min/max
    out = chelsa.convert_tas(raw)
    assert out[0] == pytest.approx(-9.75, abs=0.01)
    assert out[1] == pytest.approx(31.05, abs=0.01)


def test_convert_tas_is_not_naive_hundredth_scale():
    """raw * 0.01 gives a flat, physically impossible tropical field."""
    raw = np.array([2634], dtype=np.uint16)
    naive = float(raw[0]) * 0.01
    assert naive == pytest.approx(26.34, abs=0.01)
    assert chelsa.convert_tas(raw)[0] < 0.0


# --- unit conversion: hurs -> fraction 0-1 ---------------------------------
def test_convert_hurs_full_saturation():
    # 100 % stored with scale 0.01 -> raw 10000 -> fraction 1.0
    assert chelsa.convert_hurs(np.array([10000], dtype=np.uint16))[0] == pytest.approx(
        1.0, abs=1e-6
    )


def test_convert_hurs_mid_range_is_a_fraction():
    out = chelsa.convert_hurs(np.array([5000, 7350], dtype=np.uint16))
    assert out[0] == pytest.approx(0.50, abs=1e-6)
    assert out[1] == pytest.approx(0.735, abs=1e-6)
    assert np.all((out >= 0.0) & (out <= 1.0))


# --- NoData ----------------------------------------------------------------
def test_nodata_becomes_nan_for_both_variables():
    raw = np.array([[chelsa.NODATA, 2900]], dtype=np.uint16)
    t = chelsa.convert_tas(raw)
    h = chelsa.convert_hurs(raw)
    assert math.isnan(float(t[0, 0]))
    assert math.isnan(float(h[0, 0]))
    assert not math.isnan(float(t[0, 1]))
    assert not math.isnan(float(h[0, 1]))


def test_nodata_sentinel_value():
    assert chelsa.NODATA == 65535


# --- URL construction ------------------------------------------------------
def test_climatology_url_pattern():
    url = chelsa.build_url("tas", "climatology", 7)
    assert url.endswith("/climatologies/tas/1981-2010/CHELSA_tas_07_1981-2010_V.2.1.tif")


def test_monthly_url_pattern_zero_pads_month():
    url = chelsa.build_url("hurs", "month", 3, 2015)
    assert url.endswith("/monthly/hurs/2015/CHELSA_hurs_03_2015_V.2.1.tif")


def test_build_url_rejects_bad_month_and_missing_year():
    with pytest.raises(ValueError):
        chelsa.build_url("tas", "climatology", 13)
    with pytest.raises(ValueError):
        chelsa.build_url("tas", "month", 3, None)


# --- temporal coverage / supports() ---------------------------------------
@pytest.mark.parametrize(
    "mode,year,month,expected",
    [
        ("climatology", None, 7, True),
        ("climatology", 2099, 1, True),
        ("month", 1979, 1, True),
        ("month", 2000, 6, True),
        ("month", 2018, 12, True),
        ("month", 2019, 1, True),
        ("month", 2019, 4, True),
        ("month", 2019, 5, False),
        ("month", 2019, 12, False),
        ("month", 2020, 1, False),
        ("month", 2021, 7, False),
        ("month", 1978, 12, False),
        ("month", None, 7, False),
    ],
)
def test_supports_coverage(mode, year, month, expected):
    assert ChelsaProvider().supports(mode, year, month) is expected


def test_unsupported_reason_is_explanatory():
    reason = ChelsaProvider().unsupported_reason("month", 2021, 7)
    assert reason and "2021" in reason


def test_get_raster_refuses_uncovered_slice_without_network():
    with pytest.raises(ValueError):
        ChelsaProvider().get_raster(
            (33.9, -4.7, 41.95, 5.55), mode="month", month=7, year=2021
        )


# --- decimation ------------------------------------------------------------
def test_decimated_shape_passthrough_under_cap():
    assert chelsa.decimated_shape(1231, 967, 2_500_000) == (1231, 967)


def test_decimated_shape_respects_cap_and_aspect():
    ny, nx = chelsa.decimated_shape(2016, 1620, 1_000_000)
    assert ny * nx <= 1_000_000
    assert (ny / nx) == pytest.approx(2016 / 1620, rel=0.02)


def test_decimated_shape_is_deterministic():
    """Both variables must land on identical grids without coordination."""
    assert chelsa.decimated_shape(3000, 2000, 500_000) == chelsa.decimated_shape(
        3000, 2000, 500_000
    )


# --- cache keys ------------------------------------------------------------
def test_cache_key_varies_with_request():
    bbox = (33.9, -4.7, 41.95, 5.55)
    base = chelsa.cache_key(bbox, "climatology", 7, None, 2_500_000)
    assert base == chelsa.cache_key(bbox, "climatology", 7, None, 2_500_000)
    assert base != chelsa.cache_key(bbox, "climatology", 8, None, 2_500_000)
    assert base != chelsa.cache_key(bbox, "month", 7, 2015, 2_500_000)
    assert base != chelsa.cache_key(bbox, "climatology", 7, None, 500_000)
    assert base != chelsa.cache_key((0, 0, 1, 1), "climatology", 7, None, 2_500_000)


# --- coordinate helpers ----------------------------------------------------
def _fake_field():
    return {
        "T": np.array([[10.0, 20.0], [30.0, np.nan]], dtype=np.float32),
        "H": np.array([[0.5, 0.6], [0.7, np.nan]], dtype=np.float32),
        "bounds": (0.0, 0.0, 2.0, 2.0),
        "shape": (2, 2),
        "native_res_deg": chelsa.NATIVE_RES_DEG,
        "res_deg": 1.0,
    }


def test_field_coords_are_pixel_centres_north_down():
    lons, lats = chelsa.field_coords(_fake_field())
    assert list(lons) == pytest.approx([0.5, 1.5])
    assert list(lats) == pytest.approx([1.5, 0.5])  # row 0 is the NORTH edge


def test_sample_index_maps_lat_lon_to_row_col():
    field = _fake_field()
    assert chelsa.sample_index(field, 1.5, 0.5) == (0, 0)   # NW pixel
    assert chelsa.sample_index(field, 0.5, 1.5) == (1, 1)   # SE pixel
    assert chelsa.sample_index(field, 9.0, 0.5) is None     # outside
    assert chelsa.sample_index(field, 1.0, -5.0) is None
