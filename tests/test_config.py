"""Tests for the basemap-credential config endpoint.

NO REAL KEY APPEARS IN THIS FILE. Every value below is an obvious dummy.

Each test pins BOTH credential sources — the environment variable and the
.env path — so results never depend on whether a real .env happens to exist
on the machine running the suite. Without that pinning these tests would pass
today and fail the moment someone adds their key locally.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend import api  # noqa: E402

DUMMY_KEY = "dummy_key_not_a_real_credential"


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Detach both credential sources from the host machine."""
    monkeypatch.delenv(api._BASEMAP_KEY_VAR, raising=False)
    monkeypatch.setattr(api, "_ENV_FILE", str(tmp_path / "absent.env"))
    return tmp_path


# --- the endpoint ----------------------------------------------------------
def test_config_key_is_null_when_unset(isolated_env):
    payload = api.get_config()
    assert payload["basemap"]["key"] is None
    assert payload["basemap"]["provider"] == "carto"


def test_config_returns_the_key_when_set(isolated_env, monkeypatch):
    monkeypatch.setenv(api._BASEMAP_KEY_VAR, DUMMY_KEY)
    assert api.get_config()["basemap"]["key"] == DUMMY_KEY


def test_config_exposes_nothing_but_the_basemap(isolated_env, monkeypatch):
    """This endpoint is public: it must never become an environment dump."""
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-appear")
    monkeypatch.setenv(api._BASEMAP_KEY_VAR, DUMMY_KEY)
    payload = api.get_config()
    assert set(payload) == {"basemap"}
    assert set(payload["basemap"]) == {"key", "provider"}
    assert "must-not-appear" not in json.dumps(payload)


# --- empty / whitespace values are "unset", not an empty string ------------
@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_env_var_is_treated_as_unset(isolated_env, monkeypatch, value):
    """`CARTO_API_KEY=` (as in .env.example) must not yield an empty string."""
    monkeypatch.setenv(api._BASEMAP_KEY_VAR, value)
    assert api.get_config()["basemap"]["key"] is None


# --- .env fallback ---------------------------------------------------------
def test_env_file_is_used_when_the_variable_is_unset(isolated_env, monkeypatch):
    path = isolated_env / "local.env"
    path.write_text(f"{api._BASEMAP_KEY_VAR}={DUMMY_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(api, "_ENV_FILE", str(path))
    assert api.get_config()["basemap"]["key"] == DUMMY_KEY


def test_real_environment_wins_over_the_env_file(isolated_env, monkeypatch):
    """Docker / systemd set the variable; a stale local file must not win."""
    path = isolated_env / "local.env"
    path.write_text(f"{api._BASEMAP_KEY_VAR}=from_file\n", encoding="utf-8")
    monkeypatch.setattr(api, "_ENV_FILE", str(path))
    monkeypatch.setenv(api._BASEMAP_KEY_VAR, "from_environment")
    assert api.get_config()["basemap"]["key"] == "from_environment"


def test_missing_env_file_is_not_an_error(isolated_env):
    assert api._read_env_file_value("ANYTHING", str(isolated_env / "nope.env")) is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("CARTO_API_KEY=plain", "plain"),
        ('CARTO_API_KEY="double quoted"', "double quoted"),
        ("CARTO_API_KEY='single quoted'", "single quoted"),
        ("export CARTO_API_KEY=exported", "exported"),
        ("CARTO_API_KEY=  spaced  ", "spaced"),
        ("CARTO_API_KEY=has=equals=inside", "has=equals=inside"),
    ],
)
def test_env_file_parsing_forms(tmp_path, line, expected):
    path = tmp_path / "x.env"
    path.write_text(
        "# a comment\n\n  \nOTHER_VAR=ignored\n" + line + "\n", encoding="utf-8"
    )
    assert api._read_env_file_value("CARTO_API_KEY", str(path)) == expected


def test_env_file_ignores_comments_and_other_keys(tmp_path):
    path = tmp_path / "x.env"
    path.write_text(
        "#CARTO_API_KEY=commented_out\nCARTO_API_KEY_OTHER=nope\n", encoding="utf-8"
    )
    assert api._read_env_file_value("CARTO_API_KEY", str(path)) is None


# --- the repo must never carry a real key ---------------------------------
def test_env_example_is_committed_and_valueless():
    """.env.example documents the variable; it must never hold a value."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as fh:
        body = fh.read()
    assert "CARTO_API_KEY=" in body
    for line in body.splitlines():
        if line.strip().startswith("CARTO_API_KEY"):
            assert line.strip() == "CARTO_API_KEY=", "no value may be committed"


def test_dotfiles_ignore_env_but_keep_the_example():
    """Regression guard for the secret-leak vectors: git AND the image."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for name in (".gitignore", ".dockerignore"):
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            rules = {line.strip() for line in fh}
        assert ".env" in rules, f"{name} must ignore .env"
        assert ".env.*" in rules, f"{name} must ignore .env.*"
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
        assert "!.env.example" in {line.strip() for line in fh}
