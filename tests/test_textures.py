"""Texture names the way GTA finds them.  Dictionaries converted with OpenIV (almost all of Liberty
V's: 37,181 of its 39,065 textures) store the long form 'pack:/tl_church_marble.dds'; GTA keys the
texture by 'tl_church_marble' (the dictionary's hash list holds joaat of exactly that) and shaders
name it so.  The converter keyed by the long form, found none of them, and a Liberty V district
came out with 594 textures "missing" -- 592 of them were in the resource all along."""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.gta import texture as T  # noqa: E402
from fivem2open77.gta.jenkhash import joaat  # noqa: E402


def test_texture_name():
    assert T.texture_name("pack:/tl_church_marble.dds") == "tl_church_marble"
    assert T.texture_name("pack:/sub/dir/Body_D.DDS") == "Body_D"
    assert T.texture_name("memory:lights_bw.png") == "lights_bw"
    assert T.texture_name("C:\\work\\ytd\\paint_spec.tga") == "paint_spec"
    assert T.texture_name("vehicle_genericmud_car") == "vehicle_genericmud_car"
    assert T.texture_name("logo.v2") == "logo.v2"           # not an image extension: kept
    assert T.texture_name(".dds") == ".dds"
    assert T.texture_name(None) == "" and T.texture_name("") == ""
    assert {"none", "null", "givemechecker"} <= T.NO_TEXTURE


def _ytd(names):
    """A legacy texture dictionary (RSC7 v13) with one 4x4 BC1 texture per name."""
    sys_seg, gfx_seg = bytearray(0x2000), bytearray(0x2000)
    S, G = 0x50000000, 0x60000000
    n = len(names)
    struct.pack_into("<QHHI", sys_seg, 0x20, S + 0x100, n, n, 0)       # hashes
    struct.pack_into("<QHHI", sys_seg, 0x30, S + 0x200, n, n, 0)       # textures
    for i, name in enumerate(names):
        struct.pack_into("<I", sys_seg, 0x100 + 4 * i, joaat(T.texture_name(name).lower()))
        tex = 0x400 + 0x100 * i
        struct.pack_into("<Q", sys_seg, 0x200 + 8 * i, S + tex)
        struct.pack_into("<Q", sys_seg, tex + 0x28, S + 0x1000 + 0x80 * i)
        struct.pack_into("<HHHHI", sys_seg, tex + 0x50, 4, 4, 1, 8, T.FMT_DXT1)
        struct.pack_into("<B", sys_seg, tex + 0x5D, 1)
        struct.pack_into("<Q", sys_seg, tex + 0x70, G + 16 * i)
        raw = name.encode() + b"\0"
        sys_seg[0x1000 + 0x80 * i:0x1000 + 0x80 * i + len(raw)] = raw
        gfx_seg[16 * i:16 * i + 8] = bytes([0xFF, 0xFF, 0, 0, 0, 0, 0, 0])
    flags = 1 << 17                                                     # 16 pages of 0x200
    body = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = body.compress(bytes(sys_seg) + bytes(gfx_seg)) + body.flush()
    return b"RSC7" + struct.pack("<III", 13, flags, flags) + data


def test_dictionary_keys_are_the_names_gta_uses():
    blob = _ytd(["pack:/tl_church_marble.dds", "pack:/ab_church_brick_n.dds", "Body_D"])
    d = T.load_ytd(blob, name="p1_church.ytd")
    assert sorted(d) == ["ab_church_brick_n", "body_d", "tl_church_marble"]
    assert d["tl_church_marble"].name == "tl_church_marble" and d["body_d"].name == "Body_D"
    assert d["body_d"].supported and len(d["body_d"].data) == 8
    assert T.ytd_names(blob) == ["tl_church_marble", "ab_church_brick_n", "body_d"]
