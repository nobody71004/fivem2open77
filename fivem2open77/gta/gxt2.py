"""Display names: GTA V text tables (global.gxt2 in a dlc) and FiveM's AddTextEntry calls.

A vehicles.meta ``gameName`` / ``vehicleMakeName`` is a text label; the game shows the text the
label's hash maps to.  Single-player packs ship those texts as ``.gxt2`` files; FiveM resources
register them from a client script with ``AddTextEntry("LABEL", "Text")``.

GXT2 layout: "2TXG", uint32 count, count x (uint32 label hash, uint32 offset), "2TXG",
uint32 total size, then the null-terminated UTF-8 strings the offsets point at.
"""
from __future__ import annotations

import re
import struct

from .jenkhash import joaat

_MAGIC = b"2TXG"


def parse_gxt2(data: bytes) -> dict:
    """{label hash: text}.  Damaged tables give what could be read."""
    out = {}
    if len(data) < 8 or data[:4] != _MAGIC:
        return out
    count = struct.unpack_from("<I", data, 4)[0]
    if count > 100000 or 8 + count * 8 > len(data):
        return out
    for i in range(count):
        h, off = struct.unpack_from("<II", data, 8 + i * 8)
        if off >= len(data):
            continue
        end = data.find(b"\0", off)
        if end < 0:
            end = len(data)
        try:
            out[h] = data[off:end].decode("utf-8")
        except UnicodeDecodeError:
            out[h] = data[off:end].decode("latin-1")
    return out


_ADD_TEXT = re.compile(r"""AddTextEntry\s*\(\s*(['"])(.*?)\1\s*,\s*(['"])(.*?)\3\s*\)""", re.S)
_ADD_TEXT_HASH = re.compile(r"""AddTextEntryByHash\s*\(\s*(0x[0-9A-Fa-f]+|\d+|`[^`]+`)\s*,\s*(['"])(.*?)\2\s*\)""", re.S)


def parse_lua_text_entries(text: str) -> dict:
    """{label hash: text} from AddTextEntry / AddTextEntryByHash calls in a FiveM script."""
    out = {}
    for _, key, _, val in _ADD_TEXT.findall(text):
        out[joaat(key.lower())] = val
    for key, _, val in _ADD_TEXT_HASH.findall(text):
        if key.startswith("`"):
            out[joaat(key.strip("`").lower())] = val
        else:
            out[int(key, 0) & 0xFFFFFFFF] = val
    return out


def label(labels: dict, key: str | None) -> str | None:
    """The text a label shows, or None."""
    if not key or not labels:
        return None
    t = labels.get(joaat(key.lower()))
    if t is None:
        return None
    t = t.strip()
    return t or None
