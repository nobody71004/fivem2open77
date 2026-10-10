"""Standalone drawables (.ydr) and drawable dictionaries (.ydd).

    Drawable (0xD0 bytes, legacy): DrawableBase (0x00-0xA8, see drawable.py)
        0xA8 ptr   Name
        0xB0 list  LightAttributes (ResourceSimpleList64: ptr, count u16, cap u16, pad u32)
        0xC0 ptr   Unknown
        0xC8 ptr   Bound (embedded collision, model space)

    DrawableDictionary: ResourceFileBase(0x10) + 2 x u64, Hashes (ptr, count, cap, pad),
        Drawables (pointer array, count, cap, pad).
"""
from __future__ import annotations

from .drawable import Drawable, read_drawable_base, read_lights
from .jenkhash import joaat
from .reader import ResourceReader
from .rsc7 import ResourceError, read_rsc7

YDR_VERSION = 165
YDD_VERSION = 165


def read_drawable(rd: ResourceReader, ptr: int, name: str = "") -> Drawable:
    d, c = read_drawable_base(rd, ptr)
    name_ptr = c.u64()
    d.lights = read_lights(rd, c)
    c.u64()
    d.bound_ptr = c.u64()
    d.name = rd.string_at(name_ptr) or name
    d.reader = rd
    return d


def load_ydr(path_or_bytes, name: str = "") -> Drawable:
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), name=name or str(path_or_bytes))
    if rsc.version != YDR_VERSION:
        raise ResourceError(f"{name}: unexpected drawable version {rsc.version} (expected {YDR_VERSION})")
    rd = ResourceReader(rsc, name=name)
    return read_drawable(rd, 0x50000000, name)


def load_ydd(path_or_bytes, name: str = "") -> dict:
    """-> {name hash: Drawable}; Drawable.name is the drawable's own name when present."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), name=name or str(path_or_bytes))
    rd = ResourceReader(rsc, name=name)
    c = rd.at(0x50000000)
    c.skip(0x10 + 16)
    hashes_ptr, hcount, _ = rd.pointer_list_header(c)
    draw_ptr, dcount, _ = rd.pointer_list_header(c)
    hashes = rd.u32s_at(hashes_ptr, hcount)
    out = {}
    for i, p in enumerate(rd.pointer_array(draw_ptr, dcount)):
        if not p:
            continue
        d = read_drawable(rd, p)
        h = hashes[i] if i < len(hashes) else (joaat(d.name) if d.name else i)
        out[h] = d
    return out
