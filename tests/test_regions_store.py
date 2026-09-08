"""Tests for the local boundary store's path construction.

`data/boundaries/` is a TRACKED directory, and these components become a
filename that gets written. A malformed value therefore creates persistent
junk in the repo rather than erroring — which is exactly what happened when a
leaked FastAPI Query object reached this function and produced three files
named `KEN_annotation=int required=False default=0 alias='level' ...geojson`.
They were untracked in a tracked directory, so `git add -A` would have
committed them permanently.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend import regions  # noqa: E402


def test_valid_components_build_the_expected_name():
    path = regions._local_boundary_path("KEN", 0)
    assert os.path.basename(path) == "KEN_0.geojson"


def test_numeric_string_level_is_accepted():
    assert os.path.basename(regions._local_boundary_path("KEN", "2")) == "KEN_2.geojson"


def test_non_integer_level_raises_instead_of_writing_junk():
    from fastapi import Query

    for bad in (Query(0, ge=0, le=2), object(), None, "abc", [0]):
        with pytest.raises((TypeError, ValueError)):
            regions._local_boundary_path("KEN", bad)


def test_malformed_code_is_rejected():
    for bad in ("../etc/passwd", "KEN/0", "KEN 0", "", None, 3):
        with pytest.raises((TypeError, ValueError)):
            regions._local_boundary_path(bad, 0)


def test_the_boundary_store_holds_only_well_formed_names():
    """Regression guard: no junk file may sit in the tracked store."""
    if not os.path.isdir(regions.BOUNDARIES_DIR):
        pytest.skip("boundary store not populated")
    for name in os.listdir(regions.BOUNDARIES_DIR):
        if name.startswith("."):
            continue
        assert name.endswith(".geojson"), f"unexpected file in store: {name!r}"
        stem = name[: -len(".geojson")]
        code, _, level = stem.partition("_")
        assert code.isalnum() and level.isdigit(), (
            f"malformed boundary filename {name!r} — this directory is "
            "tracked, so junk here gets committed"
        )
