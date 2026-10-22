"""Night City read straight from the game's files, without WolvenKit (Studio 1.7.5): the CR2W
reader, the archives, update 2.5's cooked sectors, meshes (geometry, materials, textures), BC7,
the game's own level of detail for the 3D city, and the Studio reading it all in a job."""
import os
import shutil
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cr2wfake import Writer, archive, dec4, depot_hash, fnv, record, short4n, vlq  # noqa: E402
from fivem2open77.cp77 import meshread as MR  # noqa: E402
from fivem2open77.cp77.archive import GameArchives  # noqa: E402
from fivem2open77.cp77.cr2w import CR2W  # noqa: E402
from fivem2open77.world import detail as DT  # noqa: E402
from fivem2open77.world import geocache as GC  # noqa: E402
from fivem2open77.world import sector25 as S25  # noqa: E402

D = "base\\worlds\\03_night_city\\_compiled\\default\\"
WALL = "base\\environment\\architecture\\wall_a.mesh"
LAMP = "base\\environment\\decoration\\lamp_post_a.mesh"
ROAD = "base\\worlds\\03_night_city\\sectors\\_external\\road_meshes\\bended\\br_1234_rt_road_6m.mesh"
PROXY = "base\\worlds\\03_night_city\\sectors\\_external\\proxy\\42\\block_a.mesh"
TOWER = "base\\worlds\\03_night_city\\sectors\\_external\\proxy\\43\\tower_far.mesh"
FOL = "base\\worlds\\03_night_city\\sectors\\_generated\\foliage\\f1.cfoliage"
PALM = "base\\environment\\vegetation\\palm_a.mesh"


# ----------------------------------------------------------------------------- a 2.5 sector
def sector_25(level=0):
    """A sector as 2.5 cooks it: a cooked static mesh node (two entries: a wall, a road bent by two
    frames), an instanced node (three lamps), a far-view stand-in (generic, cooked) over the wall,
    a building stand-in (tagged), a foliage node and its embedded foliage."""
    w = Writer()
    # node 0: cooked static meshes -- a wall (entry 0)
    ent = struct.pack("<QQ", depot_hash(WALL), fnv("red")) + b"\0" * 17
    static = w.export("worldCookedStaticMeshNode", {}, vlq(len(ent)) + ent)
    # node 1: cooked bended meshes -- a road (entry 0: frames 0..1)
    bent = struct.pack("<QQIIH", depot_hash(ROAD), fnv("default"), 0, 2, 0) + b"\0" * 4
    frames = np.zeros((2, 4, 4), np.float32)
    frames[0] = np.eye(4)
    frames[1] = np.eye(4)
    frames[1, 3, :3] = [1.0, 6.0, 0.0]                  # the far end 1 m to the side
    bended = w.export("worldCookedBendedMeshNode", {}, vlq(len(bent)) + bent + vlq(2) + frames.tobytes())
    # node 2: instanced lamps (3), their transforms in a cooked buffer
    tr = bytearray()
    for x in (0.0, 10.0, 20.0):
        tr += struct.pack("<3i", int(x * 131072), 0, 0) + b"\0" * 4 + struct.pack("<4f", 0, 0, 0, 1) + struct.pack("<3f", 1, 1, 1) + b"\0" * 4
    tbuf = w.buffer(bytes(tr))
    inst_ent = struct.pack("<8f", -1, -1, 0, 0, 21, 1, 5, 0) + struct.pack("<QQ", depot_hash(LAMP), fnv("default")) + b"\0" * 16
    inst = w.export("worldInstancedMeshNode", {"cookedWorldTransformsBuffer": w.databuffer(tbuf, "DataBuffer"), "version": w.u8(1)},
                    b"\0" + vlq(1) + inst_ent + vlq(1) + struct.pack("<HH", 0, 3))
    # node 3: a cooked generic stand-in (its hide distance at 37)
    gp = struct.pack("<QQ", depot_hash(PROXY), fnv("default")) + struct.pack("<f", 900.0) + b"\xff" * 4 + b"\0" * 13 + struct.pack("<f", 60.0)
    generic = w.export("worldCookedGenericProxyMeshNode", {}, vlq(len(gp)) + gp)
    # node 4: a building stand-in (tagged)
    building = w.export("worldBuildingProxyMeshNode", {"mesh": w.rref(TOWER, "CMesh", kind="raRef"), "meshAppearance": w.cname("default"),
                                                      "nearAutoHideDistance": w.f32(250.0)})
    # node 5: foliage, its populations embedded in the sector
    fol_node = w.export("worldFoliageNode", {"mesh": w.rref(PALM, "CMesh", kind="raRef"), "foliageResource": w.rref(FOL, "worldFoliageCompiledResource", 8, "raRef"),
                                             "populationSpanInfo": w.struct("worldFoliagePopulationSpanInfo", {"stancesBegin": w.u32(1), "stancesCount": w.u32(2)})})
    pops = np.array([[0, 0, 0], [1, 2, 0], [3, 4, 0]], np.float32)
    data = pops.tobytes() + np.array([1.0, 1.5, 2.0], np.float32).tobytes() + np.array([[0, 0, 0, 1]] * 3, np.float16).tobytes()
    fbuf = w.buffer(data)
    fres = w.export("worldFoliageCompiledResource", {"populationCount": w.u32(3), "dataBuffer": w.databuffer(fbuf, "DataBuffer")})
    w.embed(FOL, fres)
    nodes = [static, bended, inst, generic, building, fol_node]
    recs = [record((10, 10, 0), node=0, sub=0, dist=120.0, rid=1, parent=0x77),
            record((50, 0, 0), node=1, sub=0, dist=120.0, rid=2),
            record((0, 30, 0), node=2, sub=0, dist=110.0, rid=3),
            record((12, 12, 0), node=3, sub=0, dist=900.0, rid=4, proxy_id=0x77),
            record((500, 40, 0), node=4, dist=3000.0, rid=5, proxy_id=0x88),
            record((30, 30, 2), node=5, dist=100.0, rid=6)]
    nd = w.buffer(b"".join(recs))
    tail = struct.pack("<III", 68, 0, nd) + vlq(len(nodes)) + struct.pack(f"<{len(nodes)}I", *[n + 1 for n in nodes])
    w.exports.insert(0, ("worldStreamingSector", {"level": w.u8(level), "category": w.enum("worldStreamingSectorCategory", "Exterior")}, tail))
    # (the sector is export 0: every handle above moves one up)
    tail = struct.pack("<III", 68, 0, nd) + vlq(len(nodes)) + struct.pack(f"<{len(nodes)}I", *[n + 2 for n in nodes])
    w.exports[0] = ("worldStreamingSector", w.exports[0][1], tail)
    w.embedded = [(imp, ex + 1) for imp, ex in w.embedded]
    return w.build()


def test_cr2w_reader_and_a_25_sector():
    data = sector_25()
    c = CR2W(data)
    assert c.exports[0]["cls"] == "worldStreamingSector" and S25.is_cooked(c)
    assert c.props(0)["category"] == "Exterior" and FOL.lower() in c.embedded()
    sec = S25.decode(data, D + "exterior_0_0_0_0.streamingsector", None, None)
    assert sec["format"] == "2.5" and sec["nodes"] == 6 and sec["unresolved"] == 4        # (no path list: hash names)
    e = {x["n"]: x for x in sec["entries"]}
    # the wall: cooked static mesh, named by hash (no list knows it here), appearance by hash
    assert e[0]["type"] == "worldStaticMeshNode" and e[0]["res"] == S25.hash_name(depot_hash(WALL))
    assert S25.hash_of(e[0]["res"]) == depot_hash(WALL) and e[0]["app"] == "#%016x" % fnv("red")
    assert e[0]["par"] == "%016x" % 0x77 and e[0]["dist"] == 120.0
    # the road: bent by its frames
    assert e[1]["type"] == "worldBendedMeshNode" and len(e[1]["bend"]) == 2 and e[1]["bend"][1][12:15] == [1.0, 6.0, 0.0]
    # the lamps: three instances, placed by the node
    assert e[2]["elements"]["count"] == 3 and [it["pos"][0] for it in e[2]["elements"]["items"]] == [0.0, 10.0, 20.0]
    # the stand-ins: their own id and hide distance
    assert e[3]["type"] == "worldGenericProxyMeshNode" and e[3]["hide"] == 60.0 and e[3]["pid"] == "%016x" % 0x77
    assert e[4]["type"] == "worldBuildingProxyMeshNode" and e[4]["res"] == TOWER and e[4]["hide"] == 250.0
    # foliage: the node's span, the populations embedded in the sector
    assert e[5]["foliage"] == {"res": FOL, "begin": 1, "count": 2}
    pops = sec["foliage_data"][FOL]
    assert pops["pos"][2] == [3.0, 4.0, 0.0] and pops["scale"] == [1.0, 1.5, 2.0]

    class Paths:
        def path(self, h):
            return {depot_hash(WALL): WALL, depot_hash(LAMP): LAMP}.get(h)
    sec = S25.decode(data, D + "exterior_0_0_0_0.streamingsector", None, Paths())
    e = {x["n"]: x for x in sec["entries"]}
    assert e[0]["res"] == WALL and e[2]["res"] == LAMP and sec["unresolved"] == 2          # (the road's, the stand-in's: in neither)


def test_archives_read_by_path_and_hash_and_the_last_wins(tmp_path):
    pc = tmp_path / "archive" / "pc"
    (pc / "content").mkdir(parents=True)
    (pc / "ep1").mkdir()
    (pc / "content" / "a.archive").write_bytes(archive({WALL: b"base wall", LAMP: b"lamp"}))
    (pc / "ep1" / "b.archive").write_bytes(archive({WALL: b"phantom liberty wall"}))
    g = GameArchives(str(tmp_path), kraken=None)
    assert g.read(WALL) == b"phantom liberty wall" and g.read(depot_hash(LAMP)) == b"lamp"
    assert g.read("base\\nothing.mesh") is None and not g.has("base\\nothing.mesh") and g.has(LAMP)


# ----------------------------------------------------------------------------- a mesh
def mesh_file(textured=True):
    """A CMesh: a box (one chunk, LOD 0) and a smaller box (LOD 1), two appearances, a local
    multilayered material and a textured one (an embedded 4x4 texture of pure orange, DXT1)."""
    w = Writer()
    box = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (0, 2)], np.float64)
    idx = np.array([0, 1, 3, 0, 3, 2, 4, 6, 7, 4, 7, 5], np.uint16)
    scale, offset = np.array([1.0, 1.0, 1.0]), np.array([0.0, 0.0, 1.0])
    pos = short4n(box, scale, offset)
    nrm = dec4(np.tile([0.0, 0.0, 1.0], (8, 1)))
    small = short4n(box * 0.5, scale, offset)
    vb = pos.tobytes() + nrm.tobytes() + small.tobytes() + nrm.tobytes()
    ib_off = (len(vb) + 15) // 16 * 16
    buf = vb + b"\0" * (ib_off - len(vb)) + idx.tobytes() + idx.tobytes()

    def chunk(vstart, istart, lod):
        layout = w.struct("GpuWrapApiVertexLayoutDesc", {
            "elements": w.array("GpuWrapApiVertexPackingPackingElement", [
                w.struct("GpuWrapApiVertexPackingPackingElement", {"type": w.enum("GpuWrapApieVertexPackingElementType", "PT_Short4N"),
                                                                    "usage": w.enum("GpuWrapApieVertexPackingePackingUsage", "PS_Position")}),
                w.struct("GpuWrapApiVertexPackingPackingElement", {"type": w.enum("GpuWrapApieVertexPackingElementType", "PT_Dec4"),
                                                                    "usage": w.enum("GpuWrapApieVertexPackingePackingUsage", "PS_Normal"),
                                                                    "streamIndex": w.u8(1)})]),
            "slotStrides": w.array("Uint8", [struct.pack("<B", 8), struct.pack("<B", 4)] + [b"\0"] * 6, static=8)})
        return w.struct("rendChunk", {
            "chunkVertices": w.struct("rendVertexBufferChunk", {"vertexLayout": layout, "byteOffsets": w.array(
                "Uint32", [struct.pack("<I", vstart), struct.pack("<I", vstart + 64)] + [struct.pack("<I", 0)] * 3, static=5)}),
            "chunkIndices": w.struct("rendIndexBufferChunk", {"pe": w.enum("GpuWrapApieIndexBufferChunkType", "IBCT_IndexUShort"),
                                                              "teOffset": w.u32(istart)}),
            "numVertices": w.u16(8), "numIndices": w.u32(12), "lodMask": w.u8(lod)})
    rb = w.buffer(buf)
    blob = {"header": w.struct("rendRenderMeshBlobHeader", {
        "version": w.u32(20), "renderChunkInfos": w.array("rendChunk", [chunk(0, 0, 1), chunk(96, 24, 2)]),
        "quantizationScale": w.vec(X=1.0, Y=1.0, Z=1.0, W=0.0), "quantizationOffset": w.vec(X=0.0, Y=0.0, Z=1.0, W=1.0),
        "indexBufferOffset": w.u32(ib_off)}), "renderBuffer": w.databuffer(rb)}
    # materials: a multilayered local instance (in the local buffer, a CR2W of its own) and an
    # instance whose base colour is an embedded texture
    m1 = Writer()
    m1.export("CMaterialInstance", {"baseMaterial": m1.rref("engine\\materials\\multilayered.mt", "IMaterial")},
              m1.cooked_params({"MultilayerSetup": m1.rref("base\\x\\red.mlsetup", "Multilayer_Setup")}))
    m2 = Writer()
    m2.export("CMaterialInstance", {"baseMaterial": m2.rref("engine\\materials\\metal_base_proxy.mt", "IMaterial")},
              m2.cooked_params({"BaseColor": m2.rref("base\\x\\orange_d.xbm", "ITexture", 8),
                                "BaseColorScale": m2.vec(X=1.0, Y=1.0, Z=1.0, W=0.0)}))
    raws = [m1.build(), m2.build()]
    lmb = w.buffer(b"".join(raws))
    # the texture: 4x4, one DXT1 block of orange (565: 31, 40, 0)
    c565 = (31 << 11) | (40 << 5)
    texdata = struct.pack("<HHI", c565, c565, 0)
    tb = w.buffer(texdata)
    tex = w.export("CBitmapTexture", {"width": w.u32(4), "height": w.u32(4),
                                      "setup": w.struct("STextureGroupSetup", {"compression": w.enum("ETextureCompression", "TCM_DXTNoAlpha"),
                                                                               "isGamma": w.u8(1)}),
                                      "renderTextureResource": w.struct("rendRenderTextureResource", {"renderResourceBlobPC": w.handle(0)})})
    tblob = w.export("rendRenderTextureBlobPC", {"header": w.struct("rendRenderTextureBlobHeader", {
        "sizeInfo": w.struct("rendRenderTextureBlobSizeInfo", {"width": w.u16(4), "height": w.u16(4)}),
        "mipMapInfo": w.array("rendRenderTextureBlobMipMapInfo", [w.struct("rendRenderTextureBlobMipMapInfo", {
            "placement": w.struct("rendRenderTextureBlobPlacement", {"size": w.u32(8)})})])}),
        "textureData": w.databuffer(tb, "serializationDeferredDataBuffer")})
    app1 = w.export("meshMeshAppearance", {"chunkMaterials": w.array("CName", [w.cname("ml_red").enc(), w.cname("ml_red").enc()])})
    app2 = w.export("meshMeshAppearance", {"name": w.cname("orange"),
                                           "chunkMaterials": w.array("CName", [w.cname("proxy_mat").enc(), w.cname("proxy_mat").enc()])})
    blob_i = w.export("rendRenderMeshBlob", blob)
    cm = {"materialEntries": w.array("CMeshMaterialEntry", [
        w.struct("CMeshMaterialEntry", {"name": w.cname("ml_red"), "isLocalInstance": w.u8(1)}),
        w.struct("CMeshMaterialEntry", {"name": w.cname("proxy_mat"), "index": w.u16(1), "isLocalInstance": w.u8(1)})]),
        "localMaterialBuffer": w.struct("meshMeshMaterialBuffer", {"rawData": w.databuffer(lmb), "rawDataHeaders": w.array(
            "meshLocalMaterialHeader", [w.struct("meshLocalMaterialHeader", {"size": w.u32(len(raws[0]))}),
                                        w.struct("meshLocalMaterialHeader", {"offset": w.u32(len(raws[0])), "size": w.u32(len(raws[1]))})])}),
        "appearances": w.array("handle:meshMeshAppearance", [struct.pack("<i", app1 + 2), struct.pack("<i", app2 + 2)]),
        "renderResourceBlob": w.handle(blob_i + 1)}
    # the mesh's own export first; everything above moves one up
    w.exports.insert(0, ("CMesh", cm, b""))
    w.exports[tex + 1] = ("CBitmapTexture", dict(w.exports[tex + 1][1], renderTextureResource=w.struct(
        "rendRenderTextureResource", {"renderResourceBlobPC": w.handle(tblob + 1)})), b"")
    if textured:
        w.embed("base\\x\\orange_d.xbm", tex + 1)
    return w.build()


def files_for_materials():
    """The setup and template the red material names, as files of the game."""
    s = Writer()
    s.export("Multilayer_Setup", {"layers": s.array("Multilayer_Layer", [s.struct("Multilayer_Layer", {
        "material": s.rref("base\\surfaces\\paint_01.mltemplate", "Multilayer_LayerTemplate"), "colorScale": s.cname("red_paint")})])})
    t = Writer()
    t.export("Multilayer_LayerTemplate", {"overrides": t.struct("Multilayer_LayerTemplateOverrides", {"colorScale": t.array(
        "Multilayer_LayerTemplateOverridesColor", [t.struct("Multilayer_LayerTemplateOverridesColor", {
            "n": t.cname("red_paint"), "v": t.array("Float", [struct.pack("<f", 0.8), struct.pack("<f", 0.05), struct.pack("<f", 0.04)],
                                                       static=3)})])})})
    tm = Writer()
    p = tm.export("CMaterialParameterTexture", {"parameterName": tm.cname("BaseColor"), "texture": tm.rref("engine\\textures\\editor\\grey.xbm", "ITexture")})
    tm.exports.insert(0, ("CMaterialTemplate", {}, b""))
    del p
    return {"base\\x\\red.mlsetup": CR2W(s.build()), "base\\surfaces\\paint_01.mltemplate": CR2W(t.build()),
            "engine\\materials\\metal_base_proxy.mt": CR2W(tm.build())}


def test_a_mesh_read_straight_from_its_file():
    c = CR2W(mesh_file())
    chunks = MR.read_geometry(c)
    assert [ch["chunk"] for ch in chunks] == [0]                                  # the best level of detail only
    p = chunks[0]["positions"]
    assert np.allclose(p.min(0), [-1, -1, 0], atol=1e-3) and np.allclose(p.max(0), [1, 1, 2], atol=1e-3)
    assert np.allclose(chunks[0]["normals"], [0, 0, 1], atol=0.01) and list(chunks[0]["indices"][:3]) == [0, 1, 3]
    assert [ch["chunk"] for ch in MR.read_geometry(c, lod=1)] == [1]
    assert MR.appearances(c) == [("default", ["ml_red", "ml_red"]), ("orange", ["proxy_mat", "proxy_mat"])]
    files = files_for_materials()
    mats = MR.Materials(c, lambda path: files.get(path.lower()))
    red = mats.get("ml_red")
    assert red["MaterialTemplate"] == "engine\\materials\\multilayered.mt" and red["Data"]["MultilayerSetup"] == "base\\x\\red.mlsetup"
    prox = mats.get("proxy_mat")
    assert prox["Data"]["BaseColor"] == "base\\x\\orange_d.xbm" and prox["MaterialTemplate"].endswith("metal_base_proxy.mt")
    img, gamma = MR.texture_rgba(c, c.embedded()["base\\x\\orange_d.xbm"])
    assert gamma and img.shape == (4, 4, 4) and tuple(img[0, 0, :3]) == (255, 40 * 255 // 63, 0)
    # the .geo: geometry as WolvenKit's glTF has it (the winding), colours per appearance
    T = GC.TemplateColors()
    data = GC.build_native("base\\x\\box.mesh", c, lambda path: files.get(path.lower()), T)
    h, pos, nrm, idx, mod = GC.decode_all(data)
    assert h["v"] == GC.GEO_VERSION and h["triangles"] == 4 and list(h["apps"]) == ["default", "orange"]
    r, g, b, kind = h["apps"]["default"][0]
    assert kind == GC.KIND_SOLID and r > 4 * g and r > 4 * b                         # the red paint layer
    o = h["apps"]["orange"][0]
    assert abs(o[0] - 1.0) < 0.01 and abs(o[1] - (161 / 255) ** 2.2) < 0.01 and o[2] < 0.01
    assert not h.get("mod")                                         # (the default look is not textured: no detail)
    # the textured look first: its texture's detail per face
    c2 = CR2W(mesh_file())
    data2 = GC.build_native("base\\x\\box.mesh", c2, lambda path: files.get(path.lower()), T)
    assert GC.decode_all(data2)[0]["triangles"] == 4
    # a missing texture: the template's default (its name) and a tint, not white
    c3 = CR2W(mesh_file(textured=False))
    h3 = GC.decode_all(GC.build_native("base\\x\\box.mesh", c3, lambda path: files.get(path.lower()), T))[0]
    assert max(h3["apps"]["orange"][0][:3]) < 0.9


def test_texture_detail_and_bends():
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], np.float32)
    ch = {"chunk": 0, "positions": pos, "normals": np.tile([0, 0, 1.0], (4, 1)).astype(np.float32),
          "indices": np.array([0, 1, 2, 1, 3, 2], np.uint32)}
    img = np.zeros((2, 2, 4), np.uint8)
    img[..., 3] = 255
    img[:, 0, 0] = 255                                  # left half red, right half green
    img[:, 1, 1] = 255
    uv = np.array([[0.1, 0.5], [0.9, 0.5], [0.1, 0.5], [0.9, 0.5]])
    flat = GC.texture_detail(ch, img, False, uv)
    assert len(flat["positions"]) == 6 and flat["mod"].shape == (6, 3)
    smooth = GC.texture_detail(ch, img, False, uv, flat=False)
    assert len(smooth["positions"]) == 4 and smooth["mod"][0, 0] > smooth["mod"][0, 1] and smooth["mod"][1, 1] > smooth["mod"][1, 0]
    # a bend: the far end of a 6 m piece one metre to the side
    p = np.array([[x, y, 0.0] for x in (-1, 1) for y in (0, 6)], np.float32)
    d = GC.encode("r.mesh", [{"chunk": 0, "positions": p, "normals": np.tile([0, 0, 1.0], (4, 1)).astype(np.float32),
                              "indices": np.array([0, 1, 2, 1, 3, 2], np.uint32)}], {"default": [((0.5, 0.5, 0.5), 0)]}, "default")
    m0, m1 = np.eye(4), np.eye(4)
    m1[3, :3] = [1.0, 6.0, 0.0]
    bent = GC.decode_all(GC.bend_geo(d, [m0, m1]))[1]
    assert np.allclose(bent, [[-1, 0, 0], [0, 6, 0], [1, 0, 0], [2, 6, 0]])


def test_bc7():
    from fivem2open77 import bc7
    rng = np.random.default_rng(3)
    data = rng.integers(0, 256, 64 * 16, dtype=np.uint8)
    data[::16] |= 1                                         # (no reserved mode 8 block)
    img = bc7.decode(data.tobytes(), 32, 32)
    assert img.shape == (32, 32, 4) and img.dtype == np.uint8
    try:
        from PIL import Image
    except ImportError:
        return
    ref = np.asarray(Image.frombytes("RGBA", (32, 32), data.tobytes(), "bcn", 7))
    assert np.array_equal(ref, img)


# ----------------------------------------------------------------------------- the game's level of detail
def _rows(entries, level=0, path=D + "exterior_0_0_0_0.streamingsector", strings=None):
    strings = strings or DT.Strings()
    return DT.globalize(DT.rows_of({"path": path, "level": level, "entries": entries}), strings), strings


def _e(i, t, res, pos, dist, **kw):
    e = {"i": i, "type": t, "res": res, "app": "", "pos": list(pos), "rot": [0, 0, 0, 1], "scale": [1, 1, 1], "dist": dist}
    e.update(kw)
    return e


def test_the_game_level_of_detail():
    strings = DT.Strings()
    near, _ = _rows([_e(0, "worldStaticMeshNode", WALL, (0, 0, 0), 120.0, par="77"),
                     _e(1, "worldStaticMeshNode", LAMP, (100, 0, 0), 60.0)], 0, strings=strings)
    far, _ = _rows([_e(0, "worldGenericProxyMeshNode", PROXY, (0, 0, 0), 900.0, pid="77", hide=80.0),
                    _e(1, "worldGenericProxyMeshNode", TOWER, (3000, 0, 0), 2000.0, pid="99", hide=500.0)], 6, strings=strings)
    ready = lambda r: True                                                          # noqa: E731

    def seen(cam, **kw):
        p = DT.plan_lod([near, far], strings, cam, drawable=kw.pop("drawable", ready), **kw)
        return {g["res"] for g in p["groups"]}
    # up close: the wall itself; its stand-in hides; the lamp 100 m off is beyond its 60 m
    assert seen((0, 0, 20)) == {WALL, TOWER}
    # 200 m out: the stand-in instead of the wall (the coarsest level shows however far)
    assert seen((0, 0, 200)) == {PROXY, TOWER}
    # the stand-in not read yet: the wall still shows meanwhile
    assert WALL in seen((0, 0, 100), drawable=lambda r: r != PROXY)
    # more detail: the game's distances grown
    assert WALL in seen((0, 0, 100), detail=1.5) and PROXY not in seen((0, 0, 100), detail=1.5)
    # sectors stream where their box holds the camera; from above every box, the coarsest ones
    srows = DT.streaming_rows([["a.streamingsector", -100, -100, -100, 100, 100, 150, 0, "Exterior"],
                               ["b.streamingsector", -9000, -9000, -9000, 9000, 9000, 9000, 6, "Exterior"],
                               ["c.streamingsector", -100, -100, -100, 100, 100, 100, 0, "Interior"]])
    assert DT.sectors_streaming(srows, (0, 0, 20)) == ["a.streamingsector", "b.streamingsector"]
    assert DT.sectors_streaming(srows, (0, 0, 400)) == ["b.streamingsector"]
    assert DT.sectors_streaming(srows, (0, 0, 400), detail=3.0) == ["a.streamingsector", "b.streamingsector"]
    assert DT.sectors_streaming(srows, (0, 0, 90000)) == ["b.streamingsector"]


# ----------------------------------------------------------------------------- the Studio
class FakeWorld:
    """The game read straight from its archives, for the Studio's tests: the sector list, the 2.5
    sector above, its foliage and meshes (boxes)."""

    def __init__(self, fail_meshes=()):
        self.reads = []
        self.fail = set(fail_meshes)

    def stamp(self):
        return "stamp-1"

    def block_rows(self):
        return [[D + "exterior_0_0_0_0.streamingsector", -200, -200, -200, 200, 200, 400, 0, "Exterior"],
                [D + "exterior_0_0_0_6.streamingsector", -9000, -9000, -9000, 9000, 9000, 9000, 6, "Exterior"]]

    def sector(self, path):
        self.reads.append(path)
        if path.endswith("_6.streamingsector"):
            return {"path": path, "level": 6, "nodes": 1, "entries": [
                {"i": 0, "n": 0, "type": "worldGenericProxyMeshNode", "kind": "other", "name": "", "res": TOWER, "app": "#%016x" % fnv("night"),
                 "pos": [3000.0, 0.0, 0.0], "rot": [0, 0, 0, 1], "yaw": 0, "scale": [1, 1, 1], "dist": 5000.0, "hide": 400.0,
                 "pid": "%016x" % 0x99, "center": [3000.0, 0.0, 0.0]}]}
        sec = S25.decode(sector_25(), path, None, None)
        # (as the game's path list would name them)
        names = {S25.hash_name(depot_hash(WALL)): WALL, S25.hash_name(depot_hash(LAMP)): LAMP,
                 S25.hash_name(depot_hash(ROAD)): ROAD, S25.hash_name(depot_hash(PROXY)): PROXY}
        for e in sec["entries"]:
            e["res"] = names.get(e["res"], e["res"])
        return sec

    def foliage(self, res):
        return None

    def mesh_geo(self, res, templates):
        if res in self.fail:
            raise GC.GeoError("cannot be read")
        p = np.array([[x, y, z] for x in (-1, 1) for y in (0, 6) for z in (0, 1)], np.float32)
        chunk = {"chunk": 0, "positions": p, "normals": np.tile([0, 0, 1.0], (8, 1)).astype(np.float32),
                 "indices": np.array([0, 1, 3, 0, 3, 2], np.uint32)}
        return GC.encode(res, [chunk], {"default": [((0.4, 0.4, 0.4), 0)], "night": [((0.1, 0.1, 0.3), 0)],
                                        "red": [((0.8, 0.1, 0.1), 0)]}, "default")


def _studio(world):
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-native-")
    studio = Studio(ws)
    game = os.path.join(ws, "game")
    os.makedirs(game)
    studio.toolbox = lambda refresh=False: SimpleNamespace(game_dir=game, wolvenkit=None, wolvenkit_obj=None, status=lambda: {})
    studio._game_world = lambda: world
    return studio, ws


def test_the_studio_reads_the_real_city_straight_from_the_game():
    world = FakeWorld(fail_meshes={LAMP})
    studio, ws = _studio(world)
    try:
        assert studio.world_index_status()["native"] is True
        # the sector list is read at once (no "read it first"); then a job reads what is in view
        first = studio.city_detail(0, 0, cam=(5, 5, 30))
        assert first["state"] == "reading" and first["mode"] == "game" and first["sectors"]["missing"] == 2, first
        job = studio.jobs.get(first["job"])
        studio.jobs.wait(job, timeout=60)
        assert job.state == "done", (job.error, [x["text"] for x in job.lines][-10:])
        st = studio.city_detail(0, 0, cam=(5, 5, 30))
        assert st["state"] == "ready" and st["job"] is None, st
        groups = {g["res"]: g for g in st["groups"]}
        # the wall (its appearance named again from the mesh's own), the bent road as a mesh of its own
        assert groups[WALL]["app"] == "red" and groups[WALL]["ready"]
        bends = [g for g in st["groups"] if g["res"].startswith(DT.BEND)]
        assert len(bends) == 1 and bends[0]["ready"]
        header, pos, _n, _i = GC.decode(open(studio.geo_file(bends[0]["id"]), "rb").read())
        assert np.isclose(pos[:, 0].max(), 2.0)                                          # bent: the far end moved over
        # the lamp could not be read: not asked for again; the far stand-in shows, its look by name
        assert LAMP not in groups and st["meshes"]["failed"] >= 1
        assert groups[TOWER]["app"] == "night"
        # the embedded foliage was kept with the sector
        assert studio.world_index().foliage(FOL) is not None
        # from high up: the coarse stand-in, not the wall
        high = studio.city_detail(0, 0, cam=(5, 5, 1500))
        assert TOWER in {g["res"] for g in high["groups"]} and WALL not in {g["res"] for g in high["groups"]}
        # a game update: everything kept is read again
        world.stamp = lambda: "stamp-2"
        studio._world_index = None
        n = len(world.reads)
        again = studio.city_detail(0, 0, cam=(5, 5, 30))
        assert again["sectors"]["missing"] == 2 or again["state"] == "reading", again
        studio.jobs.wait(studio.jobs.get(again["job"]), timeout=60)
        assert len(world.reads) > n
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_a_failing_read_is_not_retried_in_a_loop():
    class Broken(FakeWorld):
        def sector(self, path):
            raise RuntimeError("the archives moved")
    world = Broken()
    studio, ws = _studio(world)
    try:
        studio.world_index().extract = lambda paths: (_ for _ in ()).throw(RuntimeError("WolvenKit: unknown format"))
        first = studio.city_detail(0, 0, cam=(5, 5, 30))
        job = studio.jobs.get(first["job"])
        studio.jobs.wait(job, timeout=60)
        assert job.state == "failed"
        st = studio.city_detail(0, 0, cam=(5, 5, 30))
        assert st["state"] == "error" and st["job"] is None and "WolvenKit" in (st["error"] or ""), st
        n = len([j for j in studio.jobs.list() if j["kind"] == "city-detail"])
        for _ in range(3):
            studio.city_detail(0, 0, cam=(5, 5, 30))
        assert len([j for j in studio.jobs.list() if j["kind"] == "city-detail"]) == n           # no retry storm
        studio._detail_fail = (time.time() - 120, "old")
        assert studio.city_detail(0, 0, cam=(5, 5, 30))["state"] == "reading"                   # after a minute: again
    finally:
        shutil.rmtree(ws, ignore_errors=True)
