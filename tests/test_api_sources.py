"""Offline tests for the source-selection and scaling logic in the API.

No network: the pure helpers are exercised directly, and the one endpoint test
stubs out the climate fetch and reads boundaries from the local store.
"""

import json
import os
import sys

import numpy as np
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend import api  # noqa: E402


# --- source resolution -----------------------------------------------------
def test_source_power_is_always_power():
    assert api._resolve_source("power", "month", 7, 2024)[0] == "power"
    assert api._resolve_source("power", "climatology", 7, None)[0] == "power"


def test_source_auto_prefers_chelsa_when_covered():
    assert api._resolve_source("auto", "climatology", 7, None) == ("chelsa", None)
    assert api._resolve_source("auto", "month", 7, 2015)[0] == "chelsa"


def test_source_auto_falls_back_with_a_reason():
    source_id, note = api._resolve_source("auto", "month", 7, 2021)
    assert source_id == "power"
    assert note and "2021" in note and "NASA POWER" in note


def test_source_auto_falls_back_for_late_2019_months():
    assert api._resolve_source("auto", "month", 4, 2019)[0] == "chelsa"
    assert api._resolve_source("auto", "month", 5, 2019)[0] == "power"


def test_source_chelsa_raises_when_uncovered():
    with pytest.raises(HTTPException) as exc:
        api._resolve_source("chelsa", "month", 7, 2021)
    assert exc.value.status_code == 400


# --- relative (per-map) scaling -------------------------------------------
def test_relative_breaks_are_quintiles():
    vals = np.linspace(0.8, 1.05, 1001)
    breaks = api._relative_breaks(vals)
    assert breaks.size == 4
    assert np.all(np.diff(breaks) > 0)
    assert breaks[0] == pytest.approx(0.85, abs=0.01)
    assert breaks[3] == pytest.approx(1.00, abs=0.01)


def test_relative_breaks_ignores_nan_and_empty():
    assert api._relative_breaks(np.array([np.nan, np.nan])) is None
    breaks = api._relative_breaks(np.array([0.9, np.nan, 1.0, 1.1, 0.95, 1.05]))
    assert breaks is not None and np.all(np.isfinite(breaks))


def test_relative_breaks_survive_a_constant_field():
    breaks = api._relative_breaks(np.full(100, 0.97))
    assert breaks is not None
    assert np.all(np.diff(breaks) > 0)  # nudged apart, still five usable bins


def test_classify_by_breaks_fills_all_five_classes():
    Z = np.linspace(0.8, 1.05, 1000).reshape(20, 50)
    breaks = api._relative_breaks(Z)
    cls = api._classify_by_breaks(Z, breaks)
    assert set(np.unique(cls)) == {0, 1, 2, 3, 4}


def test_classify_by_breaks_marks_nan_as_minus_one():
    Z = np.array([[0.9, np.nan]])
    cls = api._classify_by_breaks(Z, np.array([0.85, 0.95, 1.0, 1.05]))
    assert cls[0, 1] == -1
    assert cls[0, 0] >= 0


def test_relative_legend_reports_the_actual_numbers():
    breaks = np.array([0.9, 0.95, 1.0, 1.02])
    legend = api._relative_legend(breaks, 0.8, 1.05)
    assert [row["cls"] for row in legend] == ["q1", "q2", "q3", "q4", "q5"]
    assert legend[0]["label"] == "0.800 – 0.900"
    assert legend[4]["label"] == "1.020 – 1.050"
    # colours stay the shared five-class palette
    assert legend[0]["color"] == "#2c7bb6"
    assert legend[4]["color"] == "#d7191c"


# --- absolute classes are untouched ---------------------------------------
def test_absolute_class_index_matches_the_legend_order():
    Z = np.array([[0.7, 0.9, 1.0, 1.05, 1.2, np.nan]])
    assert list(api._classify_index(Z)[0]) == [0, 1, 2, 3, 4, -1]


# --- resolution provenance -------------------------------------------------
# A large region is read at a coarser step to stay inside the pixel budget. The
# reported resolution / render strings MUST say so: claiming "native-1km" for a
# resampled map overstates it on a decision-support tool.
_NATIVE = 0.0083333333          # CHELSA 30 arcsec
_KEN_SHAPE, _KEN_RES = (1217, 960), _NATIVE          # fits at native
_MLI_SHAPE, _MLI_RES = (1501, 1665), 0.009905        # decimated ~1.19x


def test_native_read_keeps_the_original_strings():
    """Kenya must be byte-identical to before this fix."""
    meta = api._chelsa_grid_meta(_KEN_SHAPE, _KEN_RES, _NATIVE)
    assert meta["render"] == "native-1km"
    assert meta["resolution"] == "~1 km (30 arcsec)"
    assert meta["decimated"] is False
    assert meta["resampling_factor"] == 1.0
    assert "note" not in meta


def test_decimated_read_does_not_claim_native():
    meta = api._chelsa_grid_meta(_MLI_SHAPE, _MLI_RES, _NATIVE)
    assert meta["render"] == "resampled-from-1km"
    assert meta["decimated"] is True
    assert "resampled" in meta["resolution"]
    assert "1.1" in meta["resolution"]        # derived from res_deg, not fixed
    assert meta["resampling_factor"] == pytest.approx(1.19, abs=0.01)
    assert "note" in meta


def test_decimated_and_native_strings_differ():
    """The regression guard: these two must never render the same claim."""
    native = api._chelsa_grid_meta(_KEN_SHAPE, _KEN_RES, _NATIVE)
    coarse = api._chelsa_grid_meta(_MLI_SHAPE, _MLI_RES, _NATIVE)
    assert native["render"] != coarse["render"]
    assert native["resolution"] != coarse["resolution"]


def test_resolution_label_scales_with_the_step():
    """A much larger bbox must read as much coarser, not as '1 km'."""
    labels = [
        api._resolution_label(_NATIVE * f, _NATIVE) for f in (1.0, 2.0, 5.0)
    ]
    assert labels[0] == "~1 km (30 arcsec)"
    assert "1.9 km" in labels[1]
    assert "4.6 km" in labels[2]
    assert len(set(labels)) == 3


def test_rounding_noise_is_not_treated_as_decimation():
    assert not api._is_decimated(_NATIVE * 1.001, _NATIVE)
    assert api._is_decimated(_NATIVE * 1.05, _NATIVE)


# --- raster polygon mask ---------------------------------------------------
def test_raster_polygon_mask_matches_a_known_box():
    from shapely.geometry import box

    geom = box(0.0, 0.0, 1.0, 1.0)
    mask = api._raster_polygon_mask(geom, (0.0, 0.0, 2.0, 2.0), (4, 4))
    assert mask.shape == (4, 4)
    # the box is the SW quadrant; row 0 is the NORTHERN edge
    assert not mask[0, 0]
    assert mask[3, 0]
    assert not mask[3, 3]


# --- /api/r0_surface response contract ------------------------------------
# The surface must be self-contained: the client draws the country outline from
# the same response, on either source path, without a second call to /api/r0.
def _stub_surface(*args, **kwargs):
    """Stand in for a climate fetch: a tiny synthetic field over Kenya."""
    Z = np.array([[0.90, 1.00], [1.05, 0.70]])
    inside = np.ones((2, 2), dtype=bool)
    return Z, inside, (33.91, -4.70, 41.91, 5.43), {"render": "stub", "shape": [2, 2]}


def _call_surface(monkeypatch, source):
    monkeypatch.setattr(api, "_surface_from_chelsa", _stub_surface)
    monkeypatch.setattr(api, "_surface_from_power", _stub_surface)
    response = api.get_r0_surface(
        region="KEN",
        level=0,
        mode="climatology",
        month=7,
        year=None,
        ramp="bins",
        source=source,
        scale="absolute",
    )
    return json.loads(response.body)


@pytest.mark.parametrize("source", ["chelsa", "power"])
def test_surface_returns_a_boundary_on_both_source_paths(monkeypatch, source):
    payload = _call_surface(monkeypatch, source)
    boundary = payload.get("boundary")
    assert boundary is not None
    assert boundary["type"] in {"Polygon", "MultiPolygon"}
    assert boundary["coordinates"]


def test_surface_boundary_matches_the_cells_endpoint(monkeypatch):
    """Same geometry as /api/r0, so both views draw an identical outline."""
    from backend import regions

    _geom, expected = regions.get_boundary("KEN", 0)
    assert _call_surface(monkeypatch, "chelsa")["boundary"] == expected


@pytest.mark.parametrize("source", ["chelsa", "power"])
def test_surface_keeps_every_pre_existing_key(monkeypatch, source):
    """boundary is purely additive — nothing the UI already reads may vanish."""
    payload = _call_surface(monkeypatch, source)
    assert set(payload) >= {"image", "bounds", "legend", "bbox", "meta"}
    assert payload["image"].startswith("data:image/png;base64,")
    assert payload["bounds"] == [[-4.70, 33.91], [5.43, 41.91]]
    assert payload["bbox"] == [33.91, -4.70, 41.91, 5.43]


# --- shared parameter defaults must agree across endpoints ----------------
# /api/coverage exists to predict what /api/r0_surface will do, and
# /api/r0_point probes the raster /api/r0_surface rendered. If a shared
# parameter defaults differently between them, the same request is predicted
# one way and served another — the advice drifts from the behaviour at the
# SIGNATURE level, which no amount of shared logic underneath can prevent.
_AGREEING_ENDPOINTS = ("get_r0_surface", "get_r0_point", "get_coverage")

#: Deliberate divergences, with the reason. Anything not listed must match.
_INTENTIONAL_DIVERGENCE = {
    ("get_r0", "source"): "cells are a NASA POWER product; source=chelsa 400s",
    ("get_r0", "region"): "the cells endpoint has no regions=, so region stays required",
}


def _declared_defaults(func):
    import inspect

    out = {}
    for name, param in inspect.signature(func).parameters.items():
        if isinstance(param.default, api._PARAM_SENTINEL):
            default = param.default.default
            out[name] = (
                "REQUIRED"
                if type(default).__name__ == "PydanticUndefinedType"
                else default
            )
    return out


def test_shared_defaults_agree_across_endpoints():
    """The specific bug: coverage defaulted mode='month', surface 'climatology'."""
    declared = {
        name: _declared_defaults(getattr(api, name)) for name in _AGREEING_ENDPOINTS
    }
    shared = set.intersection(*(set(d) for d in declared.values()))
    for param in sorted(shared):
        values = {name: declared[name][param] for name in _AGREEING_ENDPOINTS}
        assert len(set(values.values())) == 1, (
            f"{param!r} defaults differ across endpoints that must agree: "
            f"{values} — the pre-flight would advise on a different request "
            "than the one the surface endpoint serves"
        )


def test_coverage_and_surface_agree_on_mode_specifically():
    assert _declared_defaults(api.get_coverage)["mode"] == "climatology"
    assert _declared_defaults(api.get_r0_surface)["mode"] == "climatology"


def test_coverage_advice_matches_what_the_surface_would_do(monkeypatch):
    """End-to-end on the defaults: same omitted params, same conclusion."""
    advice = api.get_coverage(month=7, region_codes="KEN")
    assert advice["resolved_source"] == "chelsa"
    # the surface endpoint, given the same omitted parameters, agrees
    source_id, _note = api._resolve_source(
        "auto", advice["mode"], 7, None
    )
    assert source_id == advice["resolved_source"]


def test_cells_endpoint_divergences_are_the_documented_ones():
    """/api/r0 legitimately differs — but only in the ways we recorded."""
    cells = _declared_defaults(api.get_r0)
    surface = _declared_defaults(api.get_r0_surface)
    for param, value in cells.items():
        if param not in surface:
            continue
        if value == surface[param]:
            continue
        assert ("get_r0", param) in _INTENTIONAL_DIVERGENCE, (
            f"/api/r0 defaults {param!r}={value!r} against "
            f"{surface[param]!r} on /api/r0_surface, and that divergence is "
            "not documented as intentional"
        )


# --- /api/sources capability payload --------------------------------------
def test_sources_payload_shape():
    payload = api.get_sources()
    assert payload["default"] == "auto"
    assert set(payload["modes"]) == {"chelsa", "power", "auto"}
    ids = {s["id"] for s in payload["sources"]}
    assert ids == {"chelsa", "power"}
    for src in payload["sources"]:
        assert set(src) >= {
            "id",
            "label",
            "resolution",
            "supports_climatology",
            "year_min",
            "year_max",
            "year_max_complete",
            "partial_years",
            "notes",
        }
    chelsa = next(s for s in payload["sources"] if s["id"] == "chelsa")
    assert chelsa["year_min"] == 1979
    assert chelsa["year_max"] == 2019


def test_sources_expose_the_partial_last_year():
    """year_max alone (2019) would let a picker offer months that don't exist."""
    chelsa = next(
        s for s in api.get_sources()["sources"] if s["id"] == "chelsa"
    )
    assert chelsa["year_max_complete"] == 2018
    assert chelsa["partial_years"] == {"2019": {"month_min": 1, "month_max": 4}}
    assert "2019" in chelsa["partial_note"]


def test_power_has_no_partial_years():
    power = next(s for s in api.get_sources()["sources"] if s["id"] == "power")
    assert power["year_max"] is None
    assert power["year_max_complete"] is None
    assert power["partial_years"] == {}


def test_partial_year_payload_agrees_with_supports():
    """The advertised coverage must match what the provider actually accepts."""
    from backend.climate.chelsa import ChelsaProvider

    provider = ChelsaProvider()
    chelsa = next(
        s for s in api.get_sources()["sources"] if s["id"] == "chelsa"
    )
    complete = chelsa["year_max_complete"]
    partial_year, span = next(iter(chelsa["partial_years"].items()))
    year = int(partial_year)

    assert all(provider.supports("month", complete, m) for m in range(1, 13))
    for m in range(span["month_min"], span["month_max"] + 1):
        assert provider.supports("month", year, m)
    assert not provider.supports("month", year, span["month_max"] + 1)
    assert not provider.supports("month", year + 1, 1)


# --- /api/coverage ---------------------------------------------------------
def test_coverage_reports_1km_availability():
    ok = api.get_coverage(mode="month", month=3, year=2019)
    assert ok["chelsa_available"] is True
    assert ok["resolved_source"] == "chelsa"
    assert ok["would_error"] is False

    late = api.get_coverage(mode="month", month=7, year=2019)
    assert late["chelsa_available"] is False
    assert late["resolved_source"] == "power"
    assert late["would_error"] is False
    assert "2019" in late["chelsa_reason"]


def test_coverage_flags_an_explicit_chelsa_request_that_would_fail():
    out = api.get_coverage(mode="month", month=7, year=2021, source="chelsa")
    assert out["resolved_source"] is None
    assert out["would_error"] is True
    assert out["chelsa_reason"]


def test_coverage_climatology_is_always_1km():
    out = api.get_coverage(mode="climatology", month=7)
    assert out["chelsa_available"] is True
    assert out["resolved_source"] == "chelsa"
    assert out["resolved_resolution"] == "~1 km (30 arcsec)"


def test_caveats_name_the_source_used():
    assert any("CHELSA" in c for c in api._caveats("chelsa"))
    assert any("NASA POWER" in c for c in api._caveats("power"))
