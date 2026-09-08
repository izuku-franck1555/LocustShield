"""Tests for multi-region (`regions=`) computation.

Offline: the climate fetch is stubbed, boundaries come from the local store.
The stub varies by region so cross-region behaviour (pooled stretch, mixed
provenance, ordering) is actually exercised rather than assumed.
"""

import json
import os
import sys

import numpy as np
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend import api  # noqa: E402

# Distinct value ranges per region so a pooled stretch is observable.
_FIELDS = {
    "KEN": (0.70, 0.95),
    "ETH": (0.85, 1.05),
    "SOM": (1.00, 1.15),
    "MLI": (0.60, 0.80),
}


def _stub_chelsa(geom, bbox, mode, month, year, _code=None):
    return _stub_for(bbox, "native-1km", 0.0083333333)


def _stub_power(geom, bbox, mode, month, year, level, region):
    return _stub_for(bbox, "interpolated-surface", 0.05, code=region)


def _stub_for(bbox, render, res_deg, code=None):
    lo, hi = _FIELDS.get(code, (0.80, 1.10))
    Z = np.linspace(lo, hi, 16).reshape(4, 4)
    inside = np.ones((4, 4), dtype=bool)
    extra = {"render": render, "shape": [4, 4], "resolution_deg": res_deg}
    return Z, inside, tuple(float(v) for v in bbox), extra


@pytest.fixture
def stubbed(monkeypatch):
    """Stub both source paths, keyed by region so layers differ."""
    def chelsa(geom, bbox, mode, month, year):
        code = _code_for_bbox(bbox)
        return _stub_for(bbox, "native-1km", 0.0083333333, code=code)

    def power(geom, bbox, mode, month, year, level, region):
        return _stub_for(bbox, "interpolated-surface", 0.05, code=region)

    monkeypatch.setattr(api, "_surface_from_chelsa", chelsa)
    monkeypatch.setattr(api, "_surface_from_power", power)


_BBOX_CACHE = {}


def _code_for_bbox(bbox):
    """Reverse-map a bbox to its region code so the chelsa stub can vary."""
    if not _BBOX_CACHE:
        from backend import regions as regions_mod

        for code in _FIELDS:
            try:
                geom, _ = regions_mod.get_boundary(code, 0)
                _BBOX_CACHE[code] = regions_mod.bounds_of(geom)
            except Exception:  # noqa: BLE001
                continue
    for code, known in _BBOX_CACHE.items():
        if all(abs(a - b) < 1e-6 for a, b in zip(known, bbox)):
            return code
    return None


def _multi(codes, **kw):
    params = dict(
        level=0, mode="climatology", month=7, year=None,
        ramp="bins", source="chelsa", scale="absolute",
    )
    params.update(kw)
    response = api._multi_region_surface(codes, **params)
    return json.loads(response.body)


# --- shape and ordering ----------------------------------------------------
def test_one_layer_per_code(stubbed):
    payload = _multi(["KEN", "ETH", "SOM"])
    assert len(payload["layers"]) == 3
    assert [layer["code"] for layer in payload["layers"]] == ["KEN", "ETH", "SOM"]


def test_layer_order_follows_the_request(stubbed):
    payload = _multi(["SOM", "KEN", "ETH"])
    assert [layer["code"] for layer in payload["layers"]] == ["SOM", "KEN", "ETH"]


def test_each_layer_is_self_contained(stubbed):
    for layer in _multi(["KEN", "ETH"])["layers"]:
        assert set(layer) == {"code", "name", "image", "bounds", "boundary", "meta"}
        assert layer["image"].startswith("data:image/png;base64,")
        assert layer["boundary"]["type"] in {"Polygon", "MultiPolygon"}
        assert layer["name"] and layer["name"] != layer["code"]


def test_top_level_shape(stubbed):
    payload = _multi(["KEN", "ETH"])
    assert set(payload) == {"layers", "failed", "bbox", "legend", "meta"}
    assert len(payload["bbox"]) == 4


def test_union_bbox_covers_every_layer(stubbed):
    payload = _multi(["KEN", "MLI"])          # far apart, non-adjacent
    w, s, e, n = payload["bbox"]
    for layer in payload["layers"]:
        (ls, lw), (ln, le) = layer["bounds"]
        assert w <= lw + 1e-9 and s <= ls + 1e-9
        assert e >= le - 1e-9 and n >= ln - 1e-9


# --- aggregate meta --------------------------------------------------------
def test_aggregate_meta_totals(stubbed):
    meta = _multi(["KEN", "ETH"])["meta"]
    assert meta["regions"] == ["KEN", "ETH"]
    assert meta["n_regions"] == 2
    assert meta["n_valid_px"] == 32          # two 4x4 fields
    assert meta["r0_min"] == pytest.approx(0.70, abs=1e-3)
    assert meta["r0_max"] == pytest.approx(1.05, abs=1e-3)


def test_uniform_selection_is_not_reported_as_mixed(stubbed):
    meta = _multi(["KEN", "ETH"])["meta"]
    assert meta["mixed_sources"] is False
    assert meta["source"] == "CHELSA V2.1"
    assert "provenance_note" not in meta


def test_per_region_breakdown_is_present(stubbed):
    meta = _multi(["KEN", "ETH"])["meta"]
    assert [r["code"] for r in meta["per_region"]] == ["KEN", "ETH"]
    for entry in meta["per_region"]:
        assert set(entry) >= {"code", "name", "source", "resolution", "n_valid_px"}


# --- mixed provenance ------------------------------------------------------
def test_mixed_sources_are_declared_not_flattened(stubbed, monkeypatch):
    """A selection resolving to two sources must say so, not pick one."""
    real = api._resolve_source

    def split(source, mode, month, year, _seen=[]):
        # First region resolves to chelsa, second falls back to power.
        _seen.append(1)
        return ("chelsa", None) if len(_seen) % 2 else ("power", "fell back")

    monkeypatch.setattr(api, "_resolve_source", split)
    meta = _multi(["KEN", "ETH"], source="auto")["meta"]
    assert meta["mixed_sources"] is True
    assert meta["source"].startswith("mixed (")
    assert "CHELSA" in meta["source"] and "NASA POWER" in meta["source"]
    assert "provenance_note" in meta
    assert set(meta["source_ids"]) == {"chelsa", "power"}
    api._resolve_source = real


def test_resolution_only_difference_names_resolutions_not_sources(monkeypatch):
    """When one source serves everything, the note must describe RESOLUTION."""
    def chelsa(geom, bbox, mode, month, year):
        code = _code_for_bbox(bbox)
        # KEN native, everything else resampled
        res = 0.0083333333 if code == "KEN" else 0.0099
        Z, inside, bounds, extra = _stub_for(bbox, "native-1km", res, code=code)
        extra["resolution"] = api._resolution_label(res, 0.0083333333)
        return Z, inside, bounds, extra

    monkeypatch.setattr(api, "_surface_from_chelsa", chelsa)
    meta = _multi(["KEN", "MLI"])["meta"]
    assert meta["mixed_sources"] is False
    assert meta["mixed_resolutions"] is True
    note = meta["provenance_note"]
    assert "do not share one resolution" in note
    assert "KEN:" in note and "MLI:" in note
    assert "resampled" in note


def test_mixed_selection_keeps_both_source_caveats(stubbed, monkeypatch):
    def split(source, mode, month, year, _seen=[]):
        _seen.append(1)
        return ("chelsa", None) if len(_seen) % 2 else ("power", None)

    monkeypatch.setattr(api, "_resolve_source", split)
    caveats = _multi(["KEN", "ETH"], source="auto")["meta"]["caveats"]
    assert any("CHELSA" in c for c in caveats)
    assert any("NASA POWER" in c for c in caveats)


def test_per_layer_provenance_stays_authoritative(stubbed, monkeypatch):
    def split(source, mode, month, year, _seen=[]):
        _seen.append(1)
        return ("chelsa", None) if len(_seen) % 2 else ("power", None)

    monkeypatch.setattr(api, "_resolve_source", split)
    layers = _multi(["KEN", "ETH"], source="auto")["layers"]
    ids = [layer["meta"]["source_id"] for layer in layers]
    assert set(ids) == {"chelsa", "power"}
    for layer in layers:
        # each layer names its OWN source, never the aggregate "mixed"
        assert layer["meta"]["source"] in {"CHELSA V2.1", "NASA POWER"}


# --- relative stretch is shared across layers -----------------------------
def test_relative_breaks_are_pooled_across_regions(stubbed):
    """One shared legend means one shared stretch, computed over all layers."""
    payload = _multi(["KEN", "SOM"], scale="relative")
    meta = payload["meta"]
    assert meta["scale"] == "relative"
    breaks = meta["breaks"]
    assert len(breaks) == 4
    # pooled range is KEN's 0.70 low to SOM's 1.15 high
    assert breaks[0] > 0.70 and breaks[-1] < 1.15
    assert [row["cls"] for row in payload["legend"]] == ["q1", "q2", "q3", "q4", "q5"]


def test_absolute_scale_uses_the_shared_class_legend(stubbed):
    payload = _multi(["KEN", "ETH"], scale="absolute")
    assert payload["legend"] == api.LEGEND
    assert payload["meta"]["scale"] == "absolute"
    assert "breaks" not in payload["meta"]


# --- partial success -------------------------------------------------------
# One region failing must not discard the others' work, but the failure has to
# be named — never a silently shorter layer list.
def _failing_for(bad_codes):
    def chelsa(geom, bbox, mode, month, year):
        code = _code_for_bbox(bbox)
        if code in bad_codes:
            raise RuntimeError(f"simulated fetch failure for {code}")
        return _stub_for(bbox, "native-1km", 0.0083333333, code=code)

    return chelsa


def test_partial_success_returns_the_layers_that_worked(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"ETH"}))
    payload = _multi(["KEN", "ETH", "SOM"])
    assert [layer["code"] for layer in payload["layers"]] == ["KEN", "SOM"]
    assert payload["meta"]["n_regions"] == 2


def test_partial_success_names_the_failure(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"ETH"}))
    failed = _multi(["KEN", "ETH", "SOM"])["failed"]
    assert len(failed) == 1
    assert failed[0]["code"] == "ETH"
    assert failed[0]["name"] == "Ethiopia"
    assert "simulated fetch failure" in failed[0]["reason"]


def test_failed_is_always_present_and_empty_on_full_success(stubbed):
    assert _multi(["KEN", "ETH"])["failed"] == []


def test_partial_result_is_unambiguous_about_what_was_asked(monkeypatch):
    """Requested vs rendered must both be visible, never inferred."""
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"ETH"}))
    meta = _multi(["KEN", "ETH", "SOM"])["meta"]
    assert meta["regions"] == ["KEN", "SOM"]          # what rendered
    assert meta["regions_requested"] == ["KEN", "ETH", "SOM"]
    assert meta["n_requested"] == 3
    assert meta["n_regions"] == 2
    assert meta["n_failed"] == 1


def test_aggregates_exclude_the_failed_region(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"SOM"}))
    meta = _multi(["KEN", "SOM"])["meta"]
    assert meta["n_valid_px"] == 16                   # one 4x4 field, not two
    assert [r["code"] for r in meta["per_region"]] == ["KEN"]
    # SOM's range (1.00-1.15) must not leak into the pooled statistics
    assert meta["r0_max"] == pytest.approx(0.95, abs=1e-3)


def test_union_bbox_covers_only_rendered_layers(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"MLI"}))
    payload = _multi(["KEN", "MLI"])
    (ls, lw), (ln, le) = payload["layers"][0]["bounds"]
    assert payload["bbox"] == pytest.approx([lw, ls, le, ln], abs=1e-9)


def test_impossible_time_slice_is_a_400_not_a_fanned_out_502(stubbed):
    """Availability is temporal, so this is a bad request — and it must be
    caught before fanning out, exactly as the single-region path does."""
    with pytest.raises(HTTPException) as exc:
        _multi(["KEN", "ETH"], mode="month", month=7, year=2021, source="chelsa")
    assert exc.value.status_code == 400
    assert "2021" in exc.value.detail


def test_all_regions_failing_is_an_error_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"KEN", "ETH"}))
    with pytest.raises(HTTPException) as exc:
        _multi(["KEN", "ETH"])
    assert exc.value.status_code == 502
    assert "KEN" in exc.value.detail and "ETH" in exc.value.detail


def test_relative_stretch_pools_only_successful_regions(monkeypatch):
    monkeypatch.setattr(api, "_surface_from_chelsa", _failing_for({"SOM"}))
    meta = _multi(["KEN", "SOM"], scale="relative")["meta"]
    assert meta["n_regions"] == 1
    assert all(b <= 0.95 for b in meta["breaks"])     # SOM's values excluded


# --- pooled-stretch disclosure --------------------------------------------
def test_relative_scale_note_declares_pooling(stubbed):
    meta = _multi(["KEN", "SOM"], scale="relative")["meta"]
    assert "POOLED" in meta["scale_note"]
    assert "not" in meta["scale_note"] and "per country" in meta["scale_note"]


def test_absolute_scale_has_no_pooling_note(stubbed):
    assert "scale_note" not in _multi(["KEN", "SOM"], scale="absolute")["meta"]


def test_relative_continuous_keeps_both_statements(stubbed):
    note = _multi(["KEN", "SOM"], scale="relative", ramp="continuous")["meta"][
        "scale_note"
    ]
    assert "POOLED" in note and "[0.8, 1.1]" in note


# --- request validation ----------------------------------------------------
def test_codes_are_parsed_trimmed_and_uppercased():
    assert api._parse_regions(" ken , eth ,som ") == ["KEN", "ETH", "SOM"]


def test_duplicate_codes_collapse_preserving_order():
    assert api._parse_regions("KEN,ETH,KEN") == ["KEN", "ETH"]


def test_empty_selection_is_rejected():
    for raw in ("", "   ", ",,"):
        with pytest.raises(HTTPException) as exc:
            api._parse_regions(raw)
        assert exc.value.status_code == 400


def test_malformed_codes_are_rejected():
    with pytest.raises(HTTPException) as exc:
        api._parse_regions("KEN,XX,TOOLONG")
    assert exc.value.status_code == 400
    assert "XX" in exc.value.detail


def test_region_cap_is_enforced():
    codes = ",".join(f"{c}{c}{c}" for c in "ABCDEFGHIJKLMNOP")   # 16 codes
    with pytest.raises(HTTPException) as exc:
        api._parse_regions(codes)
    assert exc.value.status_code == 400
    assert str(api.MAX_REGIONS) in exc.value.detail


def test_cap_boundary_is_inclusive():
    codes = [f"{c}{c}{c}" for c in "ABCDEFGHIJKL"][: api.MAX_REGIONS]
    assert len(api._parse_regions(",".join(codes))) == api.MAX_REGIONS


def test_unknown_code_fails_fast_with_400():
    """Resolution happens before any climate work, so this is cheap and clear."""
    with pytest.raises(HTTPException) as exc:
        api._resolve_many(["KEN", "ZZZ"], 0)
    assert exc.value.status_code == 400
    assert "ZZZ" in exc.value.detail


def test_region_and_regions_together_is_rejected():
    with pytest.raises(HTTPException) as exc:
        api.get_r0_surface(
            region="KEN", region_codes="ETH,SOM", level=0,
            mode="climatology", month=7, year=None,
            ramp="bins", source="auto", scale="absolute",
        )
    assert exc.value.status_code == 400


def test_neither_region_nor_regions_is_rejected():
    with pytest.raises(HTTPException) as exc:
        api.get_r0_surface(
            region=None, region_codes=None, level=0,
            mode="climatology", month=7, year=None,
            ramp="bins", source="auto", scale="absolute",
        )
    assert exc.value.status_code == 400


# --- direct invocation must not leak FastAPI Query sentinels --------------
# Calling an endpoint directly skips FastAPI's injection, so any parameter left
# unsupplied arrives as the Query object itself. A leaked STRING sentinel is
# truthy and silently picks the multi-region branch; a leaked INT reaches
# numeric code and surfaces as a per-region "CHELSA fetch failed" — which
# partial success then reports as a failed REGION rather than as a bug.
def test_optional_params_omitted_do_not_leak_sentinels(stubbed):
    """team-lead's repro: regions= supplied, everything optional omitted."""
    response = api.get_r0_surface(region_codes="KEN,SOM", month=7)
    payload = json.loads(response.body)
    assert [layer["code"] for layer in payload["layers"]] == ["KEN", "SOM"]
    assert payload["failed"] == []
    assert payload["meta"]["level"] == 0             # the declared default
    assert payload["meta"]["mode"] == "climatology"
    assert payload["meta"]["ramp"] == "bins"
    assert payload["meta"]["scale"] == "absolute"


def test_single_region_with_optionals_omitted(stubbed):
    payload = json.loads(json.dumps(
        json.loads(api.get_r0_surface(region="KEN", month=7).body)
    ))
    assert payload["meta"]["region"] == "KEN"
    assert payload["meta"]["level"] == 0
    assert "layers" not in payload


def test_point_with_optionals_omitted(stubbed):
    payload = json.loads(
        api.get_r0_point(lat=-1.29, lon=36.82, region_codes="KEN,ETH", month=7).body
    )
    assert payload["meta"]["region"] == "KEN"


def test_coverage_with_optionals_omitted():
    payload = api.get_coverage(month=7, mode="climatology", region_codes="KEN")
    assert payload["n_regions"] == 1
    assert payload["per_region"][0]["code"] == "KEN"


def test_missing_required_param_fails_loudly_not_as_a_failed_region(stubbed):
    """A sentinel must never travel deep enough to look like a data problem."""
    with pytest.raises(TypeError) as exc:
        api.get_r0_surface(region_codes="KEN,SOM")      # month omitted
    assert "month" in str(exc.value)


def test_query_val_unwraps_every_parameter_type():
    from fastapi import Query

    assert api._query_val(Query(0, ge=0, le=2), "level") == 0
    assert api._query_val(Query(None, alias="regions"), "regions") is None
    assert api._query_val(Query("bins"), "ramp") == "bins"
    assert api._query_val("already-a-value") == "already-a-value"
    assert api._query_val(7) == 7
    assert api._query_val(None) is None
    with pytest.raises(TypeError):
        api._query_val(Query(..., ge=1, le=12), "month")


def test_no_endpoint_leaves_a_parameter_unnormalised():
    """Guard for the whole class: every Query-defaulted param is normalised."""
    import inspect

    for func in (api.get_r0_surface, api.get_r0_point, api.get_coverage):
        source = inspect.getsource(func)
        signature = inspect.signature(func)
        declared = [
            name
            for name, param in signature.parameters.items()
            if isinstance(param.default, api._PARAM_SENTINEL)
        ]
        for name in declared:
            assert f"_query_val({name}" in source, (
                f"{func.__name__} does not normalise {name!r} — a leaked "
                "Query sentinel would reach the logic"
            )


# --- single-region path is untouched --------------------------------------
def test_single_region_response_shape_is_unchanged(stubbed):
    response = api.get_r0_surface(
        region="KEN", region_codes=None, level=0,
        mode="climatology", month=7, year=None,
        ramp="bins", source="chelsa", scale="absolute",
    )
    payload = json.loads(response.body)
    assert set(payload) == {"image", "bounds", "boundary", "legend", "bbox", "meta"}
    assert "layers" not in payload
    assert payload["meta"]["region"] == "KEN"


# --- union bbox is for fitting only, never for reading --------------------
def test_non_adjacent_selection_reads_each_region_separately(stubbed):
    """The design point: a union bbox would be mostly empty and force
    decimation, so each country is read on its own grid."""
    payload = _multi(["MAR", "KEN"])
    w, s, e, n = payload["bbox"]
    union_span = (e - w) * (n - s)
    layer_span = sum(
        (le - lw) * (ln - ls)
        for (ls, lw), (ln, le) in (layer["bounds"] for layer in payload["layers"])
    )
    # the union is far larger than what we actually read
    assert union_span > layer_span * 1.5
    for layer in payload["layers"]:
        assert layer["meta"]["render"] == "native-1km"


# --- point probe over a multi-region selection ----------------------------
def test_point_resolves_to_the_containing_region():
    assert api._region_containing(["KEN", "ETH"], 0, -1.29, 36.82) == "KEN"
    assert api._region_containing(["KEN", "ETH"], 0, 9.03, 38.75) == "ETH"


def test_point_outside_every_region_is_none():
    assert api._region_containing(["KEN", "ETH"], 0, 48.85, 2.35) is None


def test_point_endpoint_returns_clean_nulls_outside_selection():
    response = api.get_r0_point(
        lat=48.85, lon=2.35, region=None, region_codes="KEN,ETH",
        level=0, mode="climatology", month=7, year=None, source="chelsa",
    )
    payload = json.loads(response.body)
    assert payload["r0"] is None and payload["cls"] is None
    assert payload["meta"]["region"] is None
    assert payload["meta"]["regions"] == ["KEN", "ETH"]


# --- coverage preflight over a selection ----------------------------------
def test_coverage_reports_per_region_resolution():
    payload = api.get_coverage(
        mode="climatology", month=7, year=None, source="chelsa",
        region_codes="KEN,MLI", level=0,
    )
    assert payload["n_regions"] == 2
    codes = [entry["code"] for entry in payload["per_region"]]
    assert codes == ["KEN", "MLI"]
    by_code = {entry["code"]: entry for entry in payload["per_region"]}
    # Kenya fits at native 1 km; Mali is large enough to be decimated.
    assert by_code["KEN"]["decimated"] is False
    assert by_code["MLI"]["decimated"] is True
    assert "resampled" in by_code["MLI"]["resolution"]
    assert payload["n_native_1km"] == 1
    assert payload["summary"] == "1 of 2 at native 1 km"


def test_coverage_without_regions_is_unchanged():
    payload = api.get_coverage(
        mode="month", month=7, year=2021, source="auto", region_codes=None, level=0
    )
    assert "per_region" not in payload
    assert payload["resolved_source"] == "power"


def test_coverage_estimate_matches_the_served_resolution():
    """The preflight must not promise something the render won't deliver."""
    from backend import regions as regions_mod

    geom, _ = regions_mod.get_boundary("MLI", 0)
    estimate = api._estimate_region_grid(regions_mod.bounds_of(geom))
    assert estimate["decimated"] is True
    # within a pixel of what a real read produces (1665 wide, 0.009905 deg)
    assert estimate["shape"][1] == pytest.approx(1665, abs=2)
    assert estimate["resolution_deg"] == pytest.approx(0.009905, abs=1e-4)
