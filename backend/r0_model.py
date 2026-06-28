"""R0 environmental-suitability index.

R0 is a closed-form function of two climate variables:
  - air temperature T in degrees Celsius
  - relative humidity H as a fraction in [0, 1]

The model is numpy-vectorized: every public function accepts scalars OR
numpy arrays and returns float / ndarray, using NaN for masked cells.

Reference-script parameters (k = 43.13143) are used as published.
"""

from __future__ import annotations

import numpy as np

# --- Model constants -------------------------------------------------------
A = 0.871

# Validity envelope for the input climate variables.
T_MIN, T_MAX = -30.0, 60.0      # degrees Celsius
H_MIN, H_MAX = 0.0, 1.0         # fraction

# Denominator guard: cells whose (1 - omega) * (1 - epsilon) falls at or below
# this threshold are masked to avoid blow-ups / sign flips.
DENOM_FLOOR = 1e-3


def _omega(T):
    """Temperature term omega(T)."""
    return (
        np.exp(0.00938 * T)
        - np.exp(0.00938 * 43.13143 - (43.13143 - T) / 4.8372)
        - 1.09599
    )


def _epsilon(T, H):
    """Temperature/humidity term epsilon(T, H)."""
    return 1e-4 * np.exp(13.94 * H) * (
        np.exp(0.0015 * T)
        - np.exp(0.0015 * 266.881 - (266.881 - T) / 111.142)
        - 0.867
    )


def compute_r0(T, H):
    """Compute R0 from temperature T (degC) and humidity H (fraction 0-1).

    Accepts scalars or numpy arrays. Returns a float for scalar input or an
    ndarray for array input. Masked / invalid cells are returned as np.nan.

    Masking:
      * H < 0 or H > 1
      * T < -30 or T > 60
      * non-finite inputs (e.g. missing data)
      * (1 - omega) * (1 - epsilon) <= 1e-3
    """
    T_arr = np.asarray(T, dtype=float)
    H_arr = np.asarray(H, dtype=float)
    scalar = T_arr.ndim == 0 and H_arr.ndim == 0

    T_b, H_b = np.broadcast_arrays(T_arr, H_arr)

    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        omega = _omega(T_b)
        eps = _epsilon(T_b, H_b)
        denom = (1.0 - omega) * (1.0 - eps)
        r0 = A / denom

    invalid = (
        ~np.isfinite(T_b)
        | ~np.isfinite(H_b)
        | (H_b < H_MIN)
        | (H_b > H_MAX)
        | (T_b < T_MIN)
        | (T_b > T_MAX)
        | ~np.isfinite(denom)
        | (denom <= DENOM_FLOOR)
    )

    r0 = np.where(invalid, np.nan, r0)

    if scalar:
        return float(r0)
    return r0


def classify_r0(r0):
    """Map an R0 value to a suitability class.

    Returns one of "lt08", "08_1", "eq1", "1_11", "ge11", or None for
    NaN / missing input. Order of tests is significant.
    """
    if r0 is None:
        return None
    try:
        val = float(r0)
    except (TypeError, ValueError):
        return None
    if np.isnan(val):
        return None
    if abs(val - 1.0) < 0.005:
        return "eq1"
    if val < 0.8:
        return "lt08"
    if val < 1.0:
        return "08_1"
    if val < 1.1:
        return "1_11"
    return "ge11"
