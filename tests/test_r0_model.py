"""Tests for the R0 suitability model."""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.r0_model import classify_r0, compute_r0  # noqa: E402


# --- compute_r0 numeric checks --------------------------------------------
def test_compute_r0_warm_humid():
    assert compute_r0(28, 0.95) == pytest.approx(1.136, abs=0.005)


def test_compute_r0_hot_dry_low():
    assert compute_r0(40, 0.40) < 0.70


def test_compute_r0_mild_humid():
    assert compute_r0(20, 0.90) == pytest.approx(1.011, abs=0.01)


def test_compute_r0_near_unity():
    assert compute_r0(33, 0.90) == pytest.approx(1.000, abs=0.01)


# --- classify_r0 boundary checks ------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (0.75, "lt08"),
        (0.9, "08_1"),
        (1.0, "eq1"),
        (1.05, "1_11"),
        (1.2, "ge11"),
    ],
)
def test_classify(value, expected):
    assert classify_r0(value) == expected


def test_classify_none_and_nan():
    assert classify_r0(None) is None
    assert classify_r0(float("nan")) is None


# --- masking ---------------------------------------------------------------
def test_compute_r0_humidity_out_of_range_is_nan():
    assert math.isnan(compute_r0(28, 1.5))


def test_compute_r0_temperature_out_of_range_is_nan():
    assert math.isnan(compute_r0(80, 0.5))
    assert math.isnan(compute_r0(-40, 0.5))


# --- vectorization ---------------------------------------------------------
def test_compute_r0_array_input_returns_array():
    T = np.array([28.0, 40.0, 20.0])
    H = np.array([0.95, 0.40, 0.90])
    out = compute_r0(T, H)
    assert isinstance(out, np.ndarray)
    assert out.shape == (3,)
    assert out[0] == pytest.approx(1.136, abs=0.005)
    assert out[1] < 0.70


def test_compute_r0_array_masks_bad_cells():
    T = np.array([28.0, 28.0, 80.0])
    H = np.array([0.95, 1.5, 0.5])
    out = compute_r0(T, H)
    assert not math.isnan(out[0])
    assert math.isnan(out[1])
    assert math.isnan(out[2])
