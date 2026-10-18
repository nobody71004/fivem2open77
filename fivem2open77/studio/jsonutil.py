"""JSON as the Studio answers it: strict JSON, whatever a report holds.

Python writes a NaN or an infinity as ``NaN`` / ``Infinity``, which is not JSON: a browser's
``JSON.parse`` (the Studio's own pages) and most MCP clients refuse the whole answer.  A map whose
floor was too small to sample wrote ``"floor_holes_pct": NaN`` into its build report (fixed in
mlo/coverage.py); reports already on disk still hold it.  Such numbers go out as null."""
from __future__ import annotations

import json
import math


def finite(v):
    """v with every NaN / infinity replaced by None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [finite(x) for x in v]
    return v


def dumps(obj, **kw) -> str:
    kw.setdefault("default", str)
    try:
        return json.dumps(obj, allow_nan=False, **kw)
    except ValueError:
        return json.dumps(finite(obj), allow_nan=False, **kw)


def dumps_bytes(obj, **kw) -> bytes:
    return dumps(obj, **kw).encode("utf-8")
