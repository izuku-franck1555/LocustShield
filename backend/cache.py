"""Simple on-disk JSON cache for climate API responses.

Cache entries live under app/data/cache/ and are keyed by a tuple of
(endpoint, parameter, chunk-bbox, year). Once a chunk is cached the network
is skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "cache")
)


def _ensure_dir() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)


def make_key(endpoint: str, param: str, bbox, year: Optional[int]) -> str:
    """Build a stable cache key from request components."""
    w, s, e, n = bbox
    raw = "{}|{}|{:.4f},{:.4f},{:.4f},{:.4f}|{}".format(
        endpoint, param, float(w), float(s), float(e), float(n),
        "none" if year is None else int(year),
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    safe_param = param.replace("/", "_")
    return f"{endpoint}_{safe_param}_{digest}"


def get(key: str) -> Optional[Any]:
    """Return the cached payload for ``key`` or None if absent/unreadable."""
    path = os.path.join(_CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def put(key: str, payload: Any) -> None:
    """Persist ``payload`` under ``key``."""
    _ensure_dir()
    path = os.path.join(_CACHE_DIR, f"{key}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
