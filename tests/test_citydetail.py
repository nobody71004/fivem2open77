"""The real city around a point (Live maps): the game's sectors read into mesh instances by ring,
the meshes read once into .geo files by a job, the browser's JSON."""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fivem2open77.world import detail as DT  # noqa: E402
from fivem2open77.world import geocache as GC  # noqa: E402
from fivem2open77.world import sector as S  # noqa: E402

D = "base\\worlds\\03_night_city\\_compiled\\default\\"
WALL = "base\\environment\\architecture\\common\\wall_a.mesh"
LAMP = "base\\environment\\decoration\\lighting\\lamp_post_a.mesh"
PALM = "base\\environment\\vegetation\\palms\\queen_palm\\queen_palm_med_b.mesh"
TOWER = "base\\worlds\\03_night_city\\sectors\\_external\\proxy\\123\\tower_proxy.mesh"
FOL = "base\\worlds\\03_night_city\\sectors\\_generated\\foliage\\sector128_x1_y1_z1.cfoliage"


def _rp(v):
    return {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": v}, "Flags": "Soft"}


def _nd(i, x, y, z, q=(0, 0, 0, 1)):
    return {"NodeIndex": i, "Position": {"X": x, "Y": y, "Z": z, "W": 1},
            "Orientation": {"i": q[0], "j": q[1], "k": q[2], "r": q[3]}, "Scale": {"X": 1, "Y": 1, "Z": 1}}


def _sector(level, nodes, data):
    return {"Header": {}, "Data": {"RootChunk": {"$type": "worldStreamingSector", "level": level, "category": "Exterior",
                                                 "nodes": [{"HandleId": str(i), "Data": n} for i, n in enumerate(nodes)],
                                                 "nodeData": {"Data": data}}}}


SECTORS = {
    # level 0 by the point: a wall, a row of lamps (instanced), palms (foliage)
    D + "exterior_0_0_0_0.streamingsector": _sector(0, [
        {"$type": "worldStaticMeshNode", "mesh": _rp(WALL), "debugName": {"$value": "wall"}},
        {"$type": "worldInstancedMeshNode", "mesh": _rp(LAMP), "worldTransformsBuffer": {
            "startIndex": 0, "numElements": 3, "sharedDataBuffer": {"HandleId": "9", "Data": {"buffer": {"Data": {"Transforms": [
                {"translation": {"X": t, "Y": 0, "Z": 0}, "rotation": {"i": 0, "j": 0, "k": 0, "r": 1},
                 "scale": {"X": 1, "Y": 1, "Z": 1}} for t in (0.0, 10.0, 20.0)]}}}}}},
        {"$type": "worldFoliageNode", "mesh": _rp(PALM), "foliageResource": _rp(FOL),
         "populationSpanInfo": {"stancesBegin": 1, "stancesCount": 2}},
        {"$type": "worldStaticMeshNode", "mesh": _rp("engine\\meshes\\editor\\box.w2mesh")},
    ], [_nd(0, 10.0, 10.0, 0.0), _nd(1, 5.0, 30.0, 0.0), _nd(2, 30.0, 30.0, 2.0), _nd(3, 0, 0, 0)]),
    # level 3 around: a tower's stand-in far away, and one close (left out: the real one is there)
    D + "exterior_0_0_0_3.streamingsector": _sector(3, [
        {"$type": "worldBuildingProxyMeshNode", "mesh": _rp(TOWER)},
    ], [_nd(0, 500.0, 40.0, 0.0), _nd(0, 50.0, 40.0, 0.0)]),
}
FOLIAGE = {"Header": {}, "Data": {"RootChunk": {"$type": "worldFoliageCompiledResource", "dataBuffer": {"Data": {"Populations": [
    {"Position": {"X": 99, "Y": 99, "Z": 0}, "Scale": 1, "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1}},
    {"Position": {"X": 1, "Y": 0, "Z": 0}, "Scale": 1.5, "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1}},
    {"Position": {"X": 0, "Y": 2, "Z": 0}, "Scale": 0.5, "Rotation": {"X": 0, "Y": 0, "Z": 0.7071068, "W": 0.7071068}},
]}}}}}


def _read_all():
    return {p: S.read_sector(doc, p) for p, doc in SECTORS.items()}


def test_the_rings_and_their_instances():
    secs = _read_all()
    pops = DT.foliage_populations(FOLIAGE)
    p = DT.plan(secs, 10.0, 10.0, near=200.0, far=800.0, foliage=lambda r: pops if r == FOL else None)
    by = {(g["res"], g["ring"]): g for g in p["groups"]}
    assert set(by) == {(WALL, "near"), (LAMP, "near"), (PALM, "near"), (TOWER, "far")}, set(by)
    assert by[(LAMP, "near")]["count"] == 3                      # every instance of the instanced node
    assert by[(TOWER, "far")]["count"] == 1                      # the stand-in by the point is left out
    assert np.allclose(by[(TOWER, "far")]["matrices"][0][:, 3], [500, 40, 0])
    palms = by[(PALM, "near")]["matrices"]                       # populations 1 and 2, relative to the node
    assert len(palms) == 2 and np.allclose(palms[0][:, 3], [31, 30, 2]) and np.allclose(palms[1][:, 3], [30, 32, 2])
    assert np.allclose(np.linalg.norm(palms[0][:, 0]), 1.5)      # scaled
    assert np.allclose(palms[1][:, 0], [0, 0.5, 0], atol=1e-6)   # turned 90 degrees
    assert "engine\\meshes\\editor\\box.w2mesh" not in p["meshes"]
    # nearest first, within the budget
    small = DT.plan(secs, 10.0, 10.0, foliage=lambda r: pops, max_instances=2)
    assert small["instances"] == 2 and small["dropped"] > 0 and small["groups"][0]["res"] == WALL
    js = DT.to_json(p, lambda r: "a" * 16, lambda r: r in (WALL, LAMP))
    g = next(g for g in js["groups"] if g["res"] == LAMP)
    m = np.frombuffer(base64.b64decode(g["m"]), np.float32).reshape(-1, 4, 3)
    assert np.allclose(m[1][3], [15, 30, 0]) and np.allclose(m[1][0], [1, 0, 0])   # columns: x axis ... translation
    assert "m" not in next(g for g in js["groups"] if g["res"] == PALM)          # not read yet: no matrices
    assert js["meshes"] == {"total": 4, "ready": 2, "failed": 0}


def test_what_is_too_small_to_see_or_buried_is_left_out_and_unread_meshes_wait():
    secs = _read_all()
    secs[D + "exterior_0_0_0_0.streamingsector"]["entries"].append(
        {"type": "worldStaticMeshNode", "res": WALL, "app": "", "pos": [12.0, 10.0, -150.0], "rot": [0, 0, 0, 1],
         "scale": [1, 1, 1]})                                    # the game's hidden rooms, far under the city
    strings = DT.Strings()
    rows = [DT.globalize(DT.rows_of(s), strings) for s in secs.values()]
    pops = DT.foliage_populations(FOLIAGE)
    fol = {k: np.asarray(v, np.float64) for k, v in pops.items()}
    every = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol, min_z=-1e9)
    assert sum(g["count"] for g in every["groups"] if g["res"] == WALL) == 2
    p = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol)
    assert sum(g["count"] for g in p["groups"] if g["res"] == WALL) == 1
    # from 150 m up: the lamps (0.5 m: seen to ~55 m) are left out, the wall (20 m) and the palms
    # (size not known yet) are kept
    size = {WALL: 20.0, LAMP: 0.5}.get
    far_cam = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol, cam=(10, 10, 150), radius=size)
    got = {g["res"] for g in far_cam["groups"]}
    assert LAMP not in got and WALL in got and PALM in got and far_cam["culled"] == 3, (got, far_cam["culled"])
    near_cam = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol, cam=(10, 25, 2), radius=size)
    assert LAMP in {g["res"] for g in near_cam["groups"]} and near_cam["culled"] == 0
    # only read meshes get groups; the others wait, nearest first
    part = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol, drawable=lambda r: r == WALL)
    assert {g["res"] for g in part["groups"]} == {WALL} and part["pending"][:2] == [LAMP, PALM], part["pending"]
    # a job reads a budget of them, nearest first; the next one reads on from there
    read = set()
    for want in ([WALL], [LAMP], [PALM]):
        step = DT.plan_rows(rows, strings, 10.0, 10.0, 200.0, 800.0, foliage=lambda r: fol, drawable=lambda r: r in read,
                            max_meshes=1)
        assert step["pending"] == want, (step["pending"], want)
        read.update(want)
    assert step["area"] == (2, 4)
    # rows keep in a file
    d = tempfile.mkdtemp(prefix="f2o77-rows-")
    try:
        fp = os.path.join(d, "s.npz")
        raw = DT.rows_of(secs[D + "exterior_0_0_0_0.streamingsector"])
        DT.save_rows(fp, raw, 3)
        assert DT.load_rows(fp, 2) is None                       # read from another sector version
        back = DT.load_rows(fp, 3)
        assert back["strings"] == raw["strings"] and np.array_equal(back["pos"], raw["pos"]) and back["level"] == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the vectorised quaternions agree with sector.py's
    q1, q2 = [0.1, 0.7, -0.2, 0.68], [0.5, -0.1, 0.3, 0.8]
    assert np.allclose(DT.quats_mul(np.array([q1]), np.array([q2]))[0], S.quat_mul(q1, q2))
    assert np.allclose(DT.quats_matrix(np.array([q1]))[0], DT.quat_matrix(q1))


# ----------------------------------------------------------------------------- the Studio
class _Wk:
    """WolvenKit, offline: an export of each mesh the regex names (a box, one material)."""

    def __init__(self):
        self.calls = []

    def kill(self):
        pass

    def export_meshes(self, archives, out_dir, regex, game_dir=None):
        from test_geocache import _box, _glb
        paths = [p.replace("\\\\", "\\").replace("\\.", ".") for p in re.findall(r"[a-z0-9_\\\\.]+\\.mesh", regex)]
        self.calls.append(paths)
        for p in paths:
            if "tower" in p:
                continue                                         # this one WolvenKit cannot export
            stem = os.path.join(out_dir, *p[:-5].split("\\"))
            _glb(stem + ".glb", [("submesh_00_LOD_1", _box(0, 0, 1, 1))])
            with open(stem + ".Material.json", "w") as f:
                json.dump({"MaterialRepo": out_dir, "Materials": [{"Name": "m", "BaseMaterial": "x.mi", "Data": {}}],
                           "Appearances": {"default0": ["m"]}}, f)


def test_the_studio_reads_the_real_city_in_a_job_and_serves_it():
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-detail-")
    try:
        studio = Studio(ws)
        game = os.path.join(ws, "game")
        os.makedirs(os.path.join(game, "archive", "pc", "content"))
        wk = _Wk()
        studio.toolbox = lambda refresh=False: SimpleNamespace(game_dir=game, wolvenkit="WolvenKit.CLI.dll",
                                                               wolvenkit_obj=lambda log=None: wk, status=lambda: {})
        scratch = os.path.join(ws, "x")
        os.makedirs(scratch)

        def extract(paths):
            out = {}
            for p in paths:
                doc = SECTORS.get(p) or (FOLIAGE if p == FOL else None)
                if doc is not None:
                    fp = os.path.join(scratch, f"{len(os.listdir(scratch))}.json")
                    with open(fp, "w") as f:
                        json.dump(doc, f)
                    out[p] = fp
            return out
        studio.world_extractor = extract
        # no sector list yet: say so
        assert studio.city_detail(10, 10)["state"] == "index"
        block = {"Data": {"RootChunk": {"descriptors": [
            {"data": _rp(p), "category": "Exterior", "level": doc["Data"]["RootChunk"]["level"],
             "streamingBox": {"Min": {"X": -100, "Y": -100, "Z": -100}, "Max": {"X": 100, "Y": 100, "Z": 100}}}
            for p, doc in SECTORS.items()]}}}
        bf = os.path.join(ws, "block.json")
        with open(bf, "w") as f:
            json.dump(block, f)
        studio.world_index().build_index(bf)
        first = studio.city_detail(10, 10)
        assert first["state"] == "reading" and first["groups"] == [] and first["sectors"]["missing"] == 2, first
        job = studio.jobs.get(first["job"])
        studio.jobs.wait(job, timeout=60)
        assert job.state == "done", (job.error, [x["text"] for x in job.lines][-10:])
        assert job.result["meshes"] == 4 and job.result["read"] == 4
        st = studio.city_detail(10, 10)
        # everything read; the tower WolvenKit could not export is not asked for again
        assert st["state"] == "ready" and st["job"] is None, st
        assert st["meshes"] == {"total": 4, "ready": 3, "failed": 1, "near": [3, 3]} and st["missing"] == {"meshes": 0, "foliage": 0}
        ready = {g["res"]: g for g in st["groups"]}
        assert ready[WALL]["ready"] and ready[PALM]["count"] == 2 and TOWER not in ready
        # seen from a camera high above: what is too small to see from there is left out
        high = studio.city_detail(10, 10, cam=(10, 10, 400), reach=20)
        assert LAMP not in {g["res"] for g in high["groups"]} and high["culled"] > 0, high["culled"]
        header, pos, _nrm, idx = GC.decode(open(studio.geo_file(ready[WALL]["id"]), "rb").read())
        assert header["res"] == WALL and header["triangles"] == 12
        assert len(wk.calls) == 1 and sorted(wk.calls[0]) == sorted([WALL, LAMP, PALM, TOWER])
        # the API
        from test_studio import _Studio  # noqa: F401  (the HTTP API is exercised in test_studio)
    finally:
        shutil.rmtree(ws, ignore_errors=True)
