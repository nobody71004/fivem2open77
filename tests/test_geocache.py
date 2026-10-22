"""The game's meshes for the 3D city: WolvenKit's export (glb + Material.json + material repository)
read into small .geo files with a colour per chunk and appearance."""
import json
import os
import struct
import sys
import zlib

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fivem2open77.cp77.glb import GlbWriter  # noqa: E402
from fivem2open77.world import geocache as G  # noqa: E402


def _box(cx, cy, cz, s):
    p = np.array([[cx + dx * s, cy + dy * s, cz + dz * s] for dx in (-1, 1) for dy in (-1, 1) for dz in (-1, 1)], np.float32)
    idx = np.array([0, 1, 3, 0, 3, 2, 4, 6, 7, 4, 7, 5, 0, 4, 5, 0, 5, 1, 2, 3, 7, 2, 7, 6, 0, 2, 6, 0, 6, 4, 1, 5, 7, 1, 7, 3],
                   np.uint32)
    n = p - np.array([cx, cy, cz], np.float32)
    n /= np.linalg.norm(n, axis=1, keepdims=True)
    t = np.tile([1.0, 0.0, 0.0, 1.0], (8, 1)).astype(np.float32)
    uv = np.zeros((8, 2), np.float32)
    return p, n, t, uv, idx


def _glb(path, chunks):
    w = GlbWriter()
    for name, (p, n, t, uv, idx) in chunks:
        w.add_submesh(name, p, n, t, uv, None, None, idx)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w.save(path)


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def _cn(v):
    return {"$type": "CName", "$storage": "string", "$value": v}


def _rp(v):
    return {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": v}, "Flags": "Default"}


def _tga(path, rgb, w=4, h=4):
    data = bytearray(struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, w, h, 24, 0))
    data += bytes([rgb[2], rgb[1], rgb[0]]) * (w * h)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(data))


SETUP = "base\\env\\pipe\\textures\\ml_pipe_red.mlsetup"
TEMPLATE = "base\\surfaces\\materials\\paint\\paint_fine\\paint_fine_clean_matte_01_300.mltemplate"
CONCRETE = "base\\surfaces\\materials\\concrete\\concrete_01_300.mltemplate"


def _export(tmp_path, flat_repo=False):
    """What `uncook --mesh-export-type MeshOnly` writes for one mesh: the glb, its Material.json and
    the repository (nested folders on Windows; one flat file name with backslashes on Linux)."""
    out = tmp_path / "export"
    glb = out / "base" / "env" / "pipe" / "pipe_a.glb"
    _glb(str(glb), [("submesh_00_LOD_1", _box(0, 0, 1, 1)), ("submesh_01_LOD_1", _box(0, 3, 1, 0.5)),
                    ("submesh_02_LOD_1", _box(0, -3, 1, 0.25))])

    def repo(depot, doc):
        if flat_repo:
            _write(str(out / (depot + ".json")), doc)
        else:
            _write(os.path.join(str(out), *depot.split("\\")) + ".json", doc)
    repo(SETUP, {"Header": {}, "Data": {"RootChunk": {"$type": "Multilayer_Setup", "layers": [
        {"$type": "Multilayer_Layer", "material": _rp(CONCRETE), "colorScale": _cn("null_null"), "opacity": 1},
        {"$type": "Multilayer_Layer", "material": _rp(TEMPLATE), "colorScale": _cn("red_paint"), "opacity": 1},
    ]}}})
    repo(TEMPLATE, {"Header": {}, "Data": {"RootChunk": {
        "$type": "Multilayer_LayerTemplate", "colorTexture": _rp("base\\surfaces\\materials\\paint\\paint_white_d.xbm"),
        "overrides": {"colorScale": [{"n": _cn("red_paint"), "v": {"Elements": [0.8, 0.05, 0.04]}},
                                     {"n": _cn("blue_paint"), "v": {"Elements": [0.05, 0.1, 0.7]}}]}}}})
    repo(CONCRETE, {"Header": {}, "Data": {"RootChunk": {"$type": "Multilayer_LayerTemplate",
                                                       "overrides": {"colorScale": []}}}})
    _tga(os.path.join(str(out), "base", "env", "pipe", "pipe_proxy_d.tga"), (200, 100, 50))
    mj = {"Header": {"MaterialJsonVersion": "1.0.0"}, "MaterialRepo": str(out),
          "Materials": [
              {"Name": "ml_pipe_red", "BaseMaterial": "base\\env\\pipe\\textures\\ml_pipe_red.mi",
               "MaterialTemplate": "engine\\materials\\multilayered.mt", "Data": {"MultilayerSetup": SETUP}},
              {"Name": "z_decals_rust", "BaseMaterial": "base\\surfaces\\decal\\rust.mi",
               "MaterialTemplate": "base\\materials\\mesh_decal.mt", "Data": {"DiffuseTexture": "base\\x_d.xbm"}},
              {"Name": "proxy", "BaseMaterial": "engine\\materials\\metal_base.remt", "MaterialTemplate": "",
               "Data": {"BaseColor": "base\\env\\pipe\\pipe_proxy_d.xbm"}},
              {"Name": "window_glass", "BaseMaterial": "base\\materials\\glass.mt", "MaterialTemplate": "",
               "Data": {}},
          ],
          "Appearances": {"default0": ["ml_pipe_red", "z_decals_rust", "proxy"],
                          "glassy1": ["window_glass", "z_decals_rust", "proxy"]}}
    _write(str(out / "base" / "env" / "pipe" / "pipe_a.Material.json"), mj)
    return out, glb, out / "base" / "env" / "pipe" / "pipe_a.Material.json"


def test_glb_back_in_the_games_space(tmp_path):
    _, glb, _ = _export(tmp_path)
    chunks = G.read_mesh_glb(str(glb))
    assert [c["chunk"] for c in chunks] == [0, 1, 2]
    c0 = chunks[0]
    # what went in (game space: z up, the box at y 0 and z 1) comes back out
    assert np.allclose(c0["positions"].min(0), [-1, -1, 0]) and np.allclose(c0["positions"].max(0), [1, 1, 2])
    assert np.allclose(chunks[1]["positions"].mean(0), [0, 3, 1])
    assert len(c0["indices"]) == 36


def test_lower_lods_are_left_out(tmp_path):
    glb = tmp_path / "m.glb"
    _glb(str(glb), [("submesh_00_LOD_1", _box(0, 0, 0, 1)), ("submesh_01_LOD_2", _box(0, 0, 0, 1)),
                    ("submesh_02_LOD_4", _box(0, 0, 0, 1))])
    assert [c["chunk"] for c in G.read_mesh_glb(str(glb))] == [0]


@pytest.mark.parametrize("flat", [False, True])
def test_colours_per_chunk_and_appearance(tmp_path, flat):
    out, glb, mjp = _export(tmp_path, flat_repo=flat)
    templates = G.TemplateColors(str(tmp_path / "templates.json"))
    data = G.build("base\\env\\pipe\\pipe_a.mesh", str(glb), str(mjp), [str(out)], templates)
    header, pos, nrm, idx = G.decode(data)
    assert header["res"] == "base\\env\\pipe\\pipe_a.mesh" and header["vertices"] == 24 and header["triangles"] == 36
    assert header["index"] == "u16" and header["default"] == "default"
    assert [c["chunk"] for c in header["chunks"]] == [0, 1, 2]
    assert idx.max() < 24 and len(pos) == 24 and np.abs(np.linalg.norm(nrm.astype(float) / 127, axis=1) - 1).max() < 0.02
    d = header["apps"]["default"]
    # the multilayer: concrete base (grey) under a red paint layer -> clearly red
    r, g, b, kind = d[0]
    assert kind == G.KIND_SOLID and r > 2 * g and r > 2 * b, d[0]
    assert d[1][3] == G.KIND_HIDDEN                       # the decal is not drawn
    # the proxy's baked colour texture: (200, 100, 50) sRGB, averaged and made linear
    assert d[2][3] == G.KIND_SOLID and abs(d[2][0] - (200 / 255) ** 2.2) < 0.01 and abs(d[2][2] - (50 / 255) ** 2.2) < 0.01
    gl = header["apps"]["glassy"]
    assert gl[0][3] == G.KIND_GLASS and gl[0][2] >= gl[0][0]
    # the templates' colours are kept for the next meshes
    templates.save()
    kept = json.load(open(tmp_path / "templates.json"))
    assert kept[TEMPLATE.lower()]["colors"]["red_paint"] == [0.8, 0.05, 0.04]


def test_without_materials_the_name_decides(tmp_path):
    glb = tmp_path / "base" / "environment" / "vegetation" / "palms" / "palm_a.glb"
    _glb(str(glb), [("submesh_00_LOD_1", _box(0, 0, 0, 1))])
    data = G.build("base\\environment\\vegetation\\palms\\palm_a.mesh", str(glb), None, [], G.TemplateColors())
    header = G.decode(data)[0]
    r, g, b, kind = header["apps"]["default"][0]
    assert g > r and g > b and kind == G.KIND_SOLID


def test_texture_readers(tmp_path):
    _tga(str(tmp_path / "a.tga"), (255, 0, 0))
    assert np.allclose(G.mean_color_file(str(tmp_path / "a.tga")), (1.0, 0.0, 0.0), atol=1e-6)
    # an RLE TGA (type 10) of one run
    w = h = 4
    rle = bytearray(struct.pack("<BBBHHBHHHHBB", 0, 0, 10, 0, 0, 0, 0, 0, w, h, 24, 0))
    rle += bytes([0x80 | 15, 0, 255, 0])
    (tmp_path / "b.tga").write_bytes(bytes(rle))
    assert np.allclose(G.mean_color_file(str(tmp_path / "b.tga")), (0.0, 1.0, 0.0), atol=1e-6)
    # a PNG with every filter type the reader handles
    from fivem2open77.imaging import png_bytes
    img = np.zeros((8, 8, 4), np.uint8)
    img[..., 2] = 255
    img[..., 3] = 255
    (tmp_path / "c.png").write_bytes(png_bytes(img))
    assert np.allclose(G.mean_color_file(str(tmp_path / "c.png")), (0.0, 0.0, 1.0), atol=1e-6)
    raw = b"".join(bytes([f]) + bytes([10, 20, 30] * 4) for f in (0, 1, 2, 3, 4))
    ihdr = struct.pack(">IIBBBBB", 4, 5, 8, 2, 0, 0, 0)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    (tmp_path / "d.png").write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
                                     + chunk(b"IEND", b""))
    assert G._png_rgba((tmp_path / "d.png").read_bytes()).shape == (5, 4, 4)
    # a BC1 DDS: one 4x4 block of pure blue
    dds = bytearray(b"DDS " + struct.pack("<I", 124) + b"\0" * 120)
    struct.pack_into("<II", dds, 12, 4, 4)
    struct.pack_into("<I", dds, 28, 1)
    struct.pack_into("<I4s", dds, 80, 4, b"DXT1")
    dds += struct.pack("<HHI", 0x001F, 0x001F, 0)
    (tmp_path / "e.dds").write_bytes(bytes(dds))
    assert np.allclose(G.mean_color_file(str(tmp_path / "e.dds")), (0.0, 0.0, 1.0), atol=1e-6)
    assert G.mean_color_file(str(tmp_path / "missing.png")) is None
    (tmp_path / "junk.png").write_bytes(b"not an image")
    assert G.mean_color_file(str(tmp_path / "junk.png")) is None


def test_wide_meshes_get_32_bit_indices():
    n = 70000
    pos = np.random.default_rng(1).random((n, 3)).astype(np.float32)
    chunk = {"chunk": 0, "lod": 1, "positions": pos, "normals": np.tile([0, 0, 1.0], (n, 1)).astype(np.float32),
             "indices": np.arange(n - n % 3, dtype=np.uint32)}
    header, p2, _n, idx = G.decode(G.encode("big.mesh", [chunk], {"default": [((0.5, 0.5, 0.5), 0)]}, "default"))
    assert header["index"] == "u32" and idx.dtype == np.uint32 and np.array_equal(p2, pos) and idx[-1] == n - n % 3 - 1
    with pytest.raises(G.GeoError):
        G.encode("empty.mesh", [], {}, None)
