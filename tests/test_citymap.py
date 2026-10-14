"""The 3D city of Live maps (world/citymap.py): the game's world-map entity and meshes put together in
Night City's frame, from a small synthetic 3dmap (WolvenKit-shaped JSON, glTF written like WolvenKit's
exports), and the Studio API around the cache."""
import json
import math
import os
import shutil
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77.glb import GlbWriter  # noqa: E402
from fivem2open77.world import citymap as C  # noqa: E402

FIX = 131072


def _wp(x, y, z):
    return {"$type": "WorldPosition", "x": {"$type": "FixedPoint", "Bits": int(round(x * FIX))},
            "y": {"$type": "FixedPoint", "Bits": int(round(y * FIX))}, "z": {"$type": "FixedPoint", "Bits": int(round(z * FIX))}}


def _q(i=0.0, j=0.0, k=0.0, r=1.0):
    return {"$type": "Quaternion", "i": i, "j": j, "k": k, "r": r}


def _cname(v):
    return {"$type": "CName", "$storage": "string", "$value": v}


def _comp(typ, name, pos=(0, 0, 0), q=None, mesh=None, parent=None, app="default", layer="WorldMap", scale=None, outline=None):
    c = {"$type": typ, "name": _cname(name), "localTransform": {"$type": "WorldTransform", "Orientation": q or _q(), "Position": _wp(*pos)}}
    if mesh:
        c["mesh"] = {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": "base\\entities\\cameras\\3dmap\\" + mesh},
                     "Flags": "Default"}
        c["meshAppearance"] = _cname(app)
        c["renderSceneLayerMask"] = layer
        c["visualScale"] = {"$type": "Vector3", "X": scale or 1, "Y": scale or 1, "Z": scale or 1}
    c["parentTransform"] = parent
    if outline is not None:
        c["outline"] = outline
    return c


def _bind(hid, name):
    return {"HandleId": str(hid), "Data": {"$type": "entHardTransformBinding", "bindName": _cname(name)}}


def _scene_json():
    k = math.sqrt(0.5)
    comps = [
        _comp("entTransformComponent", "lift", pos=(0, 0, -2)),
        _comp("entMeshComponent", "terrain_mesh", mesh="3dmap_terrain.mesh", parent=_bind(1, "lift")),
        # the roads are turned half way round on z, as in the game's entity
        _comp("entMeshComponent", "3dmap_roads", q=_q(0, 0, 1, 0), mesh="3dmap_roads.mesh"),
        _comp("entMeshComponent", "3dmap_roads_seethrough", q=_q(0, 0, 1, 0), mesh="3dmap_roads.mesh", app="SeeThrough"),
        _comp("entMeshComponent", "watson_data", pos=(-2000, 1800, 0), mesh="3dmap_triangle_soup.mesh", app="watson"),
        _comp("entMeshComponent", "obelisk", pos=(100, 50, 3), q=_q(0, 0, k, k), mesh="3dmap_obelisk.mesh", parent=_bind(2, "watson_data")),
        _comp("entMeshComponent", "HACK", mesh="box.w2mesh", layer="0"),
        _comp("entPhysicalMeshComponent", "coll_buildings", mesh="3dmap_coll_buildings.mesh"),
        _comp("gameStaticTriggerAreaComponent", "watson_trigger", parent=_bind(3, "watson_data"), outline={
            "HandleId": "4", "Data": {"$type": "AreaShapeOutline", "height": 100, "points": [
                {"$type": "Vector3", "X": 0, "Y": 0, "Z": 0}, {"$type": "Vector3", "X": 400, "Y": 0, "Z": 0},
                {"$type": "Vector3", "X": 400, "Y": 300, "Z": 0}, {"$type": "Vector3", "X": 0, "Y": 300, "Z": 0}]}}),
        _comp("entTransformComponent", "kabuki_transform", pos=(50, 60, 0), parent=_bind(5, "watson_trigger")),
        _comp("gameStaticTriggerAreaComponent", "kabuki_trigger", parent=_bind(6, "kabuki_transform"), outline={
            "HandleId": "7", "Data": {"$type": "AreaShapeOutline", "height": 50, "points": [
                {"$type": "Vector3", "X": 0, "Y": 0, "Z": 0}, {"$type": "Vector3", "X": 100, "Y": 0, "Z": 0},
                {"$type": "Vector3", "X": 100, "Y": 100, "Z": 0}]}}),
        # a backup copy of an outline, without its parents: left out
        _comp("gameStaticTriggerAreaComponent", "kabuki_trigger_backup", outline={"HandleRefId": "7"}),
    ]
    return {"Header": {}, "Data": {"RootChunk": {"$type": "entEntityTemplate", "components": comps}}}


def _coll_json(boxes):
    shapes = []
    for i, (pos, he, q) in enumerate(boxes):
        shapes.append({"HandleId": str(10 + i), "Data": {"$type": "physicsColliderBox", "halfExtents": {"$type": "Vector3", "X": he[0], "Y": he[1], "Z": he[2]},
                       "localToBody": {"$type": "Transform", "orientation": _q(*q), "position": {"$type": "Vector4", "X": pos[0], "Y": pos[1], "Z": pos[2], "W": 0}}}})
    body = {"HandleId": "3", "Data": {"$type": "physicsSystemBody", "collisionShapes": shapes,
                                       "localToModel": {"$type": "Transform", "orientation": _q(), "position": {"$type": "Vector4", "X": 0, "Y": 0, "Z": 0, "W": 0}}}}
    return {"Data": {"RootChunk": {"$type": "CMesh", "parameters": [{"HandleId": "1", "Data": {"$type": "meshMeshParamPhysics", "physicsData": {
        "HandleId": "2", "Data": {"$type": "physicsSystemResource", "bodies": [body]}}}}]}}}


def _glb(path, pos, tri, colors=None):
    w = GlbWriter()
    n = len(pos)
    w.add_submesh("submesh_00_LOD_1", np.asarray(pos, np.float32), np.tile([0, 0, 1], (n, 1)).astype(np.float32),
                  np.tile([1, 0, 0, 1], (n, 1)).astype(np.float32), np.zeros((n, 2), np.float32),
                  colors=None if colors is None else np.asarray(colors, np.float32), indices=np.asarray(tri, np.uint32).reshape(-1))
    w.save(path)


def _fixture(root):
    raw, glb, js = (os.path.join(root, x) for x in ("raw", "glb", "json"))
    for d in (raw, glb, js):
        os.makedirs(d)
    # terrain: a 2x2 quad grid with two coincident vertices to weld
    _glb(os.path.join(glb, "3dmap_terrain.glb"),
         [[0, 0, 10], [100, 0, 10], [100, 100, 12], [0, 100, 12], [100, 0, 10.001]], [[0, 1, 2], [0, 2, 3], [4, 2, 0]])
    _glb(os.path.join(glb, "3dmap_roads.glb"), [[10, 20, 15], [30, 20, 15], [30, 25, 15]], [[0, 1, 2]],
         colors=[[0.07, 0.3, 0.07, 1], [1, 0, 0, 1], [1, 0, 0, 1]])
    _glb(os.path.join(glb, "3dmap_obelisk.glb"), [[0, 0, 0], [1, 0, 0], [0, 0, 50]], [[0, 1, 2]])
    _glb(os.path.join(glb, "3dmap_triangle_soup.glb"), [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
    with open(os.path.join(js, "3dmap_view.ent.json"), "w") as f:
        json.dump(_scene_json(), f)
    # the map screen's blocks: the soup's material, the point cloud and the grey map (-fb xbm buffers)
    buf = os.path.join(root, "buf")
    os.makedirs(buf)
    with open(os.path.join(js, "3dmap_triangle_soup.mesh.json"), "w") as f:
        json.dump(_soup_json(), f)
    with open(os.path.join(js, "watson_data.xbm.json"), "w") as f:
        json.dump(_xbm_json(3 * SIDE, SIDE, "TCM_None", "TRF_DeepColor"), f)
    with open(os.path.join(js, "c_watson_m.xbm.json"), "w") as f:
        json.dump(_xbm_json(8, 8, "TCM_QualityR", "TRF_TrueColor"), f)
    with open(os.path.join(buf, "watson_data.xbm.0.buffer"), "wb") as f:
        f.write(_cloud([((0.5, 0.5, 0.1), (0, 0, 0, 1), (0.2, 0.4, 0.8)), ((0.25, 0.75, 0.3), (0, 0, 0, 1), (0.1, 0.1, 0.4))]).tobytes())
    with open(os.path.join(buf, "c_watson_m.xbm.0.buffer"), "wb") as f:
        f.write(_bc4_flat(8, 200) + b"\0" * 8)
    k = math.sqrt(0.5)
    with open(os.path.join(js, "3dmap_coll_buildings.mesh.json"), "w") as f:
        json.dump(_coll_json([((-1900, 1850, 40), (10, 20, 40), (0, 0, 0, 1)), ((5, 5, 5), (0.001, 0.001, 0.001), (0, 0, 0, 1)),
                              ((-1950, 1900, 30), (5, 30, 8), (k, 0, 0, k))]), f)
    return raw, glb, js


# ----------------------------------------------------------------------------- the map screen's blocks
SIDE = 4                                  # a 4 x 12 point-cloud texture: 16 slots, two blocks in use


def _soup_json(tmin=(-100, -200, -10), tmax=(100, 200, 90), cube=50.0):
    vals = [{"$type": "Float", "PointCloudTextureHeight": SIDE},
            {"$type": "rRef:ITexture", "WorldPosTex": {"DepotPath": {"$type": "ResourcePath", "$storage": "string",
                                                                     "$value": "base\\fx\\textures\\3dmap\\static\\watson_data.xbm"}}},
            {"$type": "Vector4", "TransMin": {"$type": "Vector4", "X": tmin[0], "Y": tmin[1], "Z": tmin[2], "W": 0}},
            {"$type": "Vector4", "TransMax": {"$type": "Vector4", "X": tmax[0], "Y": tmax[1], "Z": tmax[2], "W": 1}},
            {"$type": "Float", "CubeSize": cube},
            {"$type": "rRef:ITexture", "BaseColor": {"DepotPath": {"$type": "ResourcePath", "$storage": "string",
                                                                   "$value": "base\\fx\\textures\\3dmap\\static\\c_watson_m.xbm"}}},
            {"$type": "Float", "EdgeThickness": 0.0005}, {"$type": "Float", "EdgeSharpnessPower": 50}]
    return {"Data": {"RootChunk": {"$type": "CMesh",
                                   "materialEntries": [{"$type": "CMeshMaterialEntry", "name": _cname("watson"), "index": 0, "isLocalInstance": 1}],
                                   "localMaterialBuffer": {"materials": [{"$type": "CMaterialInstance", "values": vals}]}}}}


def _cloud(blocks):
    """[(position 0..1 xyz, quaternion xyzw, size 0..1 xyz)] -> the uint16 (SIDE, 3*SIDE, 4) texture."""
    a = np.zeros((SIDE, 3 * SIDE, 4), np.uint16)
    for i, (p, q, sz) in enumerate(blocks):
        r, c = divmod(i, SIDE)
        a[r, c] = [*(np.rint(np.array(p) * 65535)), 65535]
        a[r, SIDE + c] = np.rint((np.array(q) + 1.0) / 2.0 * 65535)
        a[r, 2 * SIDE + c] = [*(np.rint(np.array(sz) * 65535)), 65535]
    return a


def _xbm_json(w, h, comp, fmt):
    return {"Data": {"RootChunk": {"$type": "CBitmapTexture", "width": w, "height": h,
                                   "setup": {"$type": "STextureGroupSetup", "compression": comp, "rawFormat": fmt}}}}


def _bc4_flat(n, value):
    """An n x n BC4 texture of one grey (each 4x4 block: both ends `value`, every index 0)."""
    return bytes([value, value, 0, 0, 0, 0, 0, 0]) * ((n // 4) * (n // 4))


def test_material_params_and_block_decoding():
    params = C.material_params(_soup_json())
    w = params["watson"]
    assert w["PointCloudTextureHeight"] == SIDE and w["CubeSize"] == 50.0 and w["TransMin"][:3] == [-100, -200, -10]
    assert w["WorldPosTex"].endswith("watson_data.xbm") and w["BaseColor"].endswith("c_watson_m.xbm")
    k = math.sqrt(0.5)
    tex = _cloud([((0.5, 0.5, 0.1), (0, 0, 0, 1), (0.2, 0.4, 0.8)),          # at the centre of the range
                  ((1.0, 0.0, 0.5), (0, 0, k, k), (0.1, 0.1, 0.1))])        # a corner, turned 90 degrees on z
    b = C.decode_cubes(tex, SIDE, [-100, -200, -10], [100, 200, 90], 50.0, t=[1000, 2000, 0])
    assert b.shape == (2, 10)
    assert np.allclose(b[0, :3], [1000, 2000, 0], atol=0.01) and np.allclose(b[0, 3:6], [5, 10, 20], atol=0.01)
    assert np.allclose(b[0, 6:], [0, 0, 0, 1], atol=1e-4)
    assert np.allclose(b[1, :3], [1100, 1800, 40], atol=0.01) and np.allclose(b[1, 3:6], [2.5, 2.5, 2.5], atol=0.01)
    assert np.allclose(np.abs(b[1, 8:]), [k, k], atol=1e-4)
    # a component turned half way round: the blocks go round with it
    r = C.quat_matrix((0, 0, 1, 0))
    b2 = C.decode_cubes(tex, SIDE, [-100, -200, -10], [100, 200, 90], 50.0, r=r, t=[0, 0, 0])
    assert np.allclose(b2[1, :3], [-100, 200, 40], atol=0.01)
    rq = C.quat_matrix(b2[1, 6:]) @ np.array([1.0, 0, 0])
    assert np.allclose(rq, [0, -1, 0], atol=1e-4)                # 90 + 180 degrees


def test_scene_transforms_and_districts():
    sc = C.Scene(_scene_json())
    r, t = sc.world("terrain_mesh")
    assert np.allclose(t, [0, 0, -2]) and np.allclose(r, np.eye(3))
    r, t = sc.world("3dmap_roads")
    assert np.allclose(r @ [1, 2, 3], [-1, -2, 3])
    r, t = sc.world("obelisk")                      # child of watson_data, turned 90 degrees
    assert np.allclose(t, [-1900, 1850, 3]) and np.allclose(r @ [1, 0, 0], [0, 1, 0], atol=1e-6)
    ds = {d["id"]: d for d in sc.districts()}
    assert set(ds) == {"watson", "kabuki"}, ds.keys()
    w, kb = ds["watson"], ds["kabuki"]
    assert w["top"] and w["label"] == "Watson" and w["parent"] is None and w["height"] == 100
    assert w["outline"][0] == [-2000, 1800, 0] and w["outline"][2] == [-1600, 2100, 0]
    # kabuki: its own transform under watson's trigger, which hangs off watson_data
    assert not kb["top"] and kb["parent"] == "watson" and kb["outline"][1] == [-1850, 1860, 0]


def test_assemble_layers_buildings_and_binary():
    root = tempfile.mkdtemp(prefix="f2o77-city-")
    try:
        raw, glb, js = _fixture(root)
        logs = []
        meta, data = C.assemble(raw, glb, js, log=logs.append, buf_dir=os.path.join(root, "buf"))
        assert set(meta["layers"]) == {"terrain", "roads", "landmarks"}, meta["layers"].keys()
        cu = meta["cubes"]
        assert cu["count"] == 2 and meta["stats"]["blocks"] == 2 and [d["id"] for d in cu["districts"]] == ["watson"]
        cb = np.frombuffer(data, np.float32, cu["data"][1], cu["data"][0]).reshape(-1, 10)
        # watson_data stands at (-2000, 1800): the first block in the middle of its range
        assert np.allclose(cb[0, :3], [-2000, 1800, 0], atol=0.02) and np.allclose(cb[0, 3:6], [5, 10, 20], atol=0.01)
        shade = np.frombuffer(data, np.uint8, cu["shade"][1], cu["shade"][0])
        assert list(shade) == [200, 200]                             # the grey map under each block
        dm = cu["districts"][0]
        assert dm["mask"]["w"] == 8 and dm["mask_min"] == [-2100, 1600] and dm["mask_max"] == [-1900, 2000]
        mask = np.frombuffer(data, np.uint8, dm["mask"]["data"][1], dm["mask"]["data"][0])
        assert mask.size == 64 and mask.min() == mask.max() == 200
        assert len(data) == meta["bytes"] and len(data) % 16 == 0
        t = meta["layers"]["terrain"]
        assert t["vertices"] == 4 and t["triangles"] == 2          # welded, the collapsed triangle dropped
        pos = np.frombuffer(data, np.float32, t["pos"][1], t["pos"][0]).reshape(-1, 3)
        assert np.isclose(pos[:, 2].min(), 8.0) and np.isclose(pos[:, 2].max(), 10.0)   # the parent lowers it 2 m
        rd = meta["layers"]["roads"]
        rp = np.frombuffer(data, np.float32, rd["pos"][1], rd["pos"][0]).reshape(-1, 3)
        assert np.allclose(sorted(rp[:, 0]), [-30, -30, -10]) and np.allclose(sorted(rp[:, 1]), [-25, -20, -20])
        col = np.frombuffer(data, np.uint8, rd["col"][1], rd["col"][0]).reshape(-1, 4)
        assert col[:, 0].max() == 255 and col[:, 0].min() == 18      # major roads keep their red
        lm = meta["layers"]["landmarks"]
        lp = np.frombuffer(data, np.float32, lm["pos"][1], lm["pos"][0]).reshape(-1, 3)
        assert np.allclose(sorted(lp[:, 1]), [1850, 1850, 1851], atol=1e-3)   # turned 90 degrees, moved under watson
        assert meta["landmarks"][0]["id"] == "obelisk"
        b = meta["buildings"]
        assert b["count"] == 2                                       # the degenerate box is dropped
        bx = np.frombuffer(data, np.float32, b["data"][1], b["data"][0]).reshape(-1, 10)
        assert np.allclose(bx[0, :6], [-1900, 1850, 40, 10, 20, 40]) and np.allclose(bx[0, 6:], [0, 0, 0, 1])
        assert np.allclose(abs(bx[1, 6]), math.sqrt(0.5), atol=1e-5)
        assert [d["id"] for d in meta["districts"]] == ["watson", "kabuki"]
        assert meta["stats"]["buildings"] == 2 and meta["city"]["min"][2] == 0 and meta["city"]["max"][2] == 80
        assert meta["city"]["min"][1] == 1830                       # the turned box reaches 8 m (not 30) to the south
        assert not any("missing" in x for x in logs)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_build_writes_the_cache_with_a_fake_wolvenkit():
    root = tempfile.mkdtemp(prefix="f2o77-city-")
    try:
        game = os.path.join(root, "game")
        os.makedirs(os.path.join(game, "archive", "pc", "content"))
        open(os.path.join(game, "archive", "pc", "content", "basegame_1.archive"), "wb").close()
        src = os.path.join(root, "src")
        _, sglb, sjs = _fixture(src)

        class FakeWK:
            calls = []

            def run(self, args, timeout=0, check=True):
                self.calls.append(args)
                assert args[0] == "uncook" and "--mesh-export-type" in args and "MeshOnly" in args
                assert args[args.index("-fb") + 1] == "xbm"                 # the textures' pixel buffers
                out, exp = args[args.index("-o") + 1], args[args.index("-or") + 1]
                d = os.path.join(out, *C.FOLDER.split("\\"))
                e = os.path.join(exp, *C.FOLDER.split("\\"))
                td = os.path.join(out, *C.TEXTURES.split("\\"))
                te = os.path.join(exp, *C.TEXTURES.split("\\"))
                for x in (d, e, td, te):
                    os.makedirs(x, exist_ok=True)
                for fn in ("3dmap_view.ent", "3dmap_coll_buildings.mesh", "3dmap_terrain.mesh", "3dmap_triangle_soup.mesh"):
                    open(os.path.join(d, fn), "wb").close()
                for fn in ("watson_data.xbm", "c_watson_m.xbm"):
                    open(os.path.join(td, fn), "wb").close()
                for fn in os.listdir(sglb):
                    shutil.copy(os.path.join(sglb, fn), e)
                for fn in os.listdir(os.path.join(src, "buf")):
                    shutil.copy(os.path.join(src, "buf", fn), te)
                return "[ 0: Error ] - Failed to export materials ... (ignored)"

            def serialize(self, folder, out):
                self.calls.append(["serialize", sorted(os.listdir(folder))])
                for fn in os.listdir(sjs):
                    shutil.copy(os.path.join(sjs, fn), out)

        wk = FakeWK()
        out = os.path.join(root, "cache")
        st = C.build(wk, game, out, os.path.join(root, "tmp"), log=lambda *a: None)
        assert st["ready"] and st["stats"]["terrain"] == 2 and os.path.isfile(os.path.join(out, "citymap.bin"))
        assert st["stats"]["blocks"] == 2 and st["version"] == C.VERSION and not st["upgrade"]
        assert wk.calls[1] == ["serialize", ["3dmap_coll_buildings.mesh", "3dmap_triangle_soup.mesh", "3dmap_view.ent",
                                             "c_watson_m.xbm", "watson_data.xbm"]]
        assert os.listdir(os.path.join(root, "tmp")) == []          # the work folder is gone
        assert not C.status(out, game).get("stale")
        later = time.time() + 100
        os.utime(os.path.join(game, "archive", "pc", "content", "basegame_1.archive"), (later, later))
        assert C.status(out, game)["stale"]
        try:
            C.build(wk, os.path.join(root, "nogame"), out, root)
        except C.CityMapError as e:
            assert "not found" in str(e)
        else:
            raise AssertionError("a missing game was accepted")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_studio_citymap_api():
    from test_studio import _Studio
    st = _Studio()
    try:
        code, s = st.req("GET", "/api/citymap")
        assert code == 200 and s["ready"] is False and s["game"] in (True, False)
        assert st.req("GET", "/api/citymap/citymap.bin")[0] == 400       # not built yet
        assert st.req("GET", "/api/citymap/../settings.json")[0] in (400, 404)
        # build with a stand-in for the game (the Studio's hook for tests)
        from fivem2open77.studio.worldops import WorldOps
        src = tempfile.mkdtemp(prefix="f2o77-city-")

        def fake_builder(out_dir, job):
            raw, glb, js = _fixture(src)
            meta, data = C.assemble(raw, glb, js)
            meta["key"] = "k1"
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "citymap.bin"), "wb") as f:
                f.write(data)
            with open(os.path.join(out_dir, "citymap.json"), "w") as f:
                json.dump(meta, f)
            return C.status(out_dir)

        WorldOps.citymap_builder = staticmethod(fake_builder)
        try:
            code, job = st.req("POST", "/api/citymap/build", {})
            assert code == 200 and job["kind"] == "citymap"
            for _ in range(100):
                code, j = st.req("GET", f"/api/jobs/{job['id']}")
                if j["state"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
            assert j["state"] == "done", j
        finally:
            WorldOps.citymap_builder = None
            shutil.rmtree(src, ignore_errors=True)
        code, s = st.req("GET", "/api/citymap")
        assert s["ready"] and s["key"] == "k1" and s["stats"]["buildings"] == 2
        code, meta = st.req("GET", "/api/citymap/citymap.json?k=k1")
        assert code == 200 and meta["layers"]["terrain"]["triangles"] == 2
        import urllib.request
        r = urllib.request.Request(f"http://127.0.0.1:{st.port}/api/citymap/citymap.bin?k=k1", headers={"X-F2O77-Token": st.token})
        with urllib.request.urlopen(r) as resp:
            body = resp.read()
            assert len(body) == meta["bytes"] and "immutable" in resp.headers["Cache-Control"]
        # the agents' tools (the Studio's own and MCP clients) see the same status
        from fivem2open77.studio.agent.tools import registry, tools_for

        class Ctx:
            studio = None
        ctx = Ctx()
        from fivem2open77.studio.app import Studio
        ctx.studio = Studio(st.ws)
        out = registry()["city_map_status"].fn(ctx, {})
        body = json.loads(out["content"][0]["text"] if isinstance(out, dict) and "content" in out else
                          (out[0]["text"] if isinstance(out, list) else out))
        assert body["ready"] and body["stats"]["buildings"] == 2
        for kind in (None, "car", "map", "world"):
            assert {"city_map_status", "build_city_map"} <= set(tools_for(kind))
        # the page and its modules are served
        for p in ("/js/pages/city3d.js", "/js/pages/citydata.js", "/js/pages/cityshaders.js", "/css/city3d.css",
                  "/vendor/three/UnrealBloomPass.js", "/vendor/three/EffectComposer.js"):
            code, body = st.req("GET", p, token=False)
            assert code == 200 and len(body) > 200, p
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)
