"""Canonical hashing. Excludes run ids and clocks by never accepting them here."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize(obj: Any) -> Any:
    if obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, (list, tuple)):
        return [normalize(item) for item in obj]
    if isinstance(obj, dict):
        if any(not isinstance(key, str) for key in obj):
            raise TypeError("canonical dict keys must be strings")
        return {key: normalize(obj[key]) for key in sorted(obj)}
    raise TypeError(f"not canonical: {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    payload = json.dumps(
        normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return payload.encode("utf-8")


def semantic_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
