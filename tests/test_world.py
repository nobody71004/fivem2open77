"""World edits: sectors read into node instances, the world index, the edit list compiled to
ArchiveXL, the resource and its deploy bundle.  A small synthetic sector always; sectors read
from a real game (F2O77_TEST_WORLD, default /home/claude/vanilla/world: WolvenKit JSON of a few
exterior sectors) when present."""
import glob
import json
import os
import sys
import tarfile
import tempfile
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fivem2open77.world import edits as E  # noqa: E402
from fivem2open77.world import index as I  # noqa: E402
from fivem2open77.world import package as P  # noqa: E402
from fivem2open77.world import sector as S  # noqa: E402

REAL = os.environ.get("F2O77_TEST_WORLD", "/home/claude/vanilla/world")
SEC = "base\\worlds\\03_night_city\\_compiled\\default\\exterior_0_0_0_3.streamingsector"   # a 512 m cell from 0,0


def _cn(v):
    return {"$type": "CName", "$storage": "string", "$value": v}


def _rp(v):
    return {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": v}, "Flags": "Soft"}


def _nd(i, x, y, z, yaw_k=0.0, yaw_r=1.0):
    return {"Id": "0", "NodeIndex": i, "Position": {"$type": "Vector4", "X": x, "Y": y, "Z": z, "W": 0},
            "Orientation": {"$type": "Quaternion", "i": 0, "j": 0, "k": yaw_k, "r": yaw_r},
            "Scale": {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1}, "UkFloat1": 100.0}


def synthetic_sector():
    """Three node instances of a static mesh, an instanced mesh with 3 instances (sharing its buffer
    with a second instanced node of 2), a collision node with 2 actors (fixed point positions)."""
    fp = lambda v: {"$type": "FixedPoint", "Bits": int(round(v * 131072))}  # noqa: E731
    tr = lambda x, y: {"$type": "worldNodeTransform", "translation": {"$type": "Vector3", "X": x, "Y": y, "Z": 5},  # noqa: E731
                        "rotation": {"$type": "Quaternion", "i": 0, "j": 0, "k": 0, "r": 1},
                        "scale": {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1}}
    nodes = [
        {"HandleId": "0", "Data": {"$type": "worldStaticMeshNode", "debugName": _cn("railing_a"),
                                   "mesh": _rp("base\\environment\\fences\\railing_a.mesh"), "meshAppearance": _cn("default")}},
        {"HandleId": "1", "Data": {"$type": "worldInstancedMeshNode", "debugName": _cn("None"),
                                   "mesh": _rp("base\\environment\\street\\bollard.mesh"),
                                   "worldTransformsBuffer": {"numElements": 3, "startIndex": 0, "sharedDataBuffer": {
                                       "HandleId": "2", "Data": {"$type": "worldSharedDataBuffer", "buffer": {"Data": {
                                           "Transforms": [tr(100, 200), tr(104, 200), tr(108, 200), tr(300, 300), tr(304, 300)]}}}}}}},
        {"HandleId": "3", "Data": {"$type": "worldInstancedMeshNode", "debugName": _cn("None"),
                                   "mesh": _rp("base\\environment\\street\\bin.mesh"),
                                   "worldTransformsBuffer": {"numElements": 2, "startIndex": 3, "sharedDataBuffer": {"HandleRefId": "2"}}}},
        {"HandleId": "4", "Data": {"$type": "worldCollisionNode", "compiledData": {"Data": {"Actors": [
            {"Position": {"x": fp(101), "y": fp(201), "z": fp(5)}, "Orientation": {"i": 0, "j": 0, "k": 0, "r": 1},
             "Shapes": [{"ShapeType": "Box"}]},
            {"Position": {"x": fp(150), "y": fp(260), "z": fp(5)}, "Orientation": {"i": 0, "j": 0, "k": 0, "r": 1},
             "Shapes": [{"ShapeType": "ConvexMesh"}, {"ShapeType": "Box"}]},
            # the railing's own collision (exactly where the railing stands) and the turned copy's
            {"Position": {"x": fp(102), "y": fp(198), "z": fp(5)}, "Orientation": {"i": 0, "j": 0, "k": 0, "r": 1},
             "Shapes": [{"ShapeType": "TriangleMesh"}]},
            {"Position": {"x": fp(400), "y": fp(400), "z": fp(5)}, "Orientation": {"i": 0, "j": 0, "k": 0.7071068, "r": 0.7071068},
             "Shapes": [{"ShapeType": "TriangleMesh"}]},
            # the second bollard's (instance 1 of node 2)
            {"Position": {"x": fp(104), "y": fp(200), "z": fp(5)}, "Orientation": {"i": 0, "j": 0, "k": 0, "r": 1},
             "Shapes": [{"ShapeType": "ConvexMesh"}]}]}}}},
    ]
    data = [_nd(0, 102, 198, 5), _nd(0, 400, 400, 5, 0.7071068, 0.7071068), _nd(1, 0, 0, 0), _nd(2, 0, 0, 0), _nd(3, 120, 230, 5)]
    return {"Header": {}, "Data": {"RootChunk": {"$type": "worldStreamingSector", "level": 0, "category": "Exterior",
                                                 "nodes": nodes, "nodeData": {"Data": data}}}}


def test_a_sector_becomes_node_instances_in_world_coordinates():
    sec = S.read_sector(synthetic_sector(), SEC)
    assert sec["nodes"] == 5 and len(sec["entries"]) == 5
    a, b, inst, inst2, col = sec["entries"]
    assert a["type"] == "worldStaticMeshNode" and a["kind"] == "mesh" and a["name"] == "railing_a"
    assert a["res"].endswith("railing_a.mesh") and a["app"] == "default" and a["pos"] == [102.0, 198.0, 5.0]
    assert abs(S.yaw_of(b["rot"]) - 90.0) < 1e-3, "a quarter turn counter-clockwise"
    assert inst["elements"]["kind"] == "instances" and inst["elements"]["count"] == 3
    assert [p["pos"][0] for p in inst["elements"]["items"]] == [100.0, 104.0, 108.0]
    assert inst2["elements"]["count"] == 2 and inst2["elements"]["items"][0]["pos"] == [300.0, 300.0, 5.0], \
        "the second node's slice of the shared buffer (a HandleRefId)"
    assert col["elements"]["kind"] == "actors" and col["elements"]["count"] == 5
    assert col["elements"]["items"][0]["pos"] == [101.0, 201.0, 5.0] and col["elements"]["items"][1]["shapes"] == ["ConvexMesh", "Box"]
    assert S.near(inst, 104, 201, 2.0) and not S.near(inst, 200, 200, 2.0)


def _index_with(tmp, sectors):
    """A WorldIndex whose streaming block lists the given {path: doc} and whose extractor writes them."""
    rows = []
    for path, doc in sectors.items():
        sec = S.read_sector(doc, path)
        pts = [p for e in sec["entries"] for p in S.entry_points(e) if any(p)]
        xs, ys, zs = zip(*pts)
        rows.append({"$type": "worldStreamingSectorDescriptor", "category": "Exterior", "level": sec["level"] or 0,
                     "data": _rp(path), "streamingBox": {"Min": {"X": min(xs) - 20, "Y": min(ys) - 20, "Z": min(zs) - 20},
                                                         "Max": {"X": max(xs) + 20, "Y": max(ys) + 20, "Z": max(zs) + 20}}})
    block = os.path.join(tmp, "block.json")
    with open(block, "w") as f:
        json.dump({"Data": {"RootChunk": {"descriptors": rows}}}, f)
    calls = []

    def extract(paths):
        calls.append(list(paths))
        out = {}
        for p in paths:
            if p in sectors:
                fp = os.path.join(tmp, f"x{len(out)}_{len(calls)}.json")
                with open(fp, "w") as f:
                    json.dump(sectors[p], f)
                out[p] = fp
        return out
    idx = I.WorldIndex(os.path.join(tmp, "cache"), extract=extract, log=lambda *a: None)
    idx.build_index(block)
    return idx, calls


def test_the_index_finds_sectors_and_objects_around_a_spot_and_reads_each_sector_once():
    tmp = tempfile.mkdtemp()
    idx, calls = _index_with(tmp, {SEC: synthetic_sector()})
    assert len(idx.rows()) == 1 and idx.sectors_at(104, 200, 5)[0][0] == SEC
    assert idx.sectors_at(5000, 5000, 5) == []
    q = idx.query(104, 200, radius=6)
    names = [(e["type"], e["i"]) for e in q["entries"]]
    assert ("worldInstancedMeshNode", 2) in names and ("worldStaticMeshNode", 0) in names and ("worldCollisionNode", 4) in names
    assert ("worldStaticMeshNode", 1) not in names, "the copy at 400,400 is far away"
    e = q["entries"][0]
    assert e["sector"] == SEC and e["expectedNodes"] == 5 and e["distance"] <= 2.1
    assert idx.query(104, 200, 6, kinds=["collision"])["entries"][0]["type"] == "worldCollisionNode"
    near = next(e for e in q["entries"] if e["type"] == "worldInstancedMeshNode")
    assert [it["k"] for it in near["elements"]["items"]] == [0, 1, 2] and near["elements"]["inArea"] == 3
    assert [it["k"] for it in idx.query(108, 200, 1)["entries"][0]["elements"]["items"]] == [2], "only the instances in the area"
    assert idx.query(104, 200, 50, text="railing")["total"] == 1
    idx.query(300, 300, 10)
    assert len(calls) == 1, "a sector is read from the game once"


def test_edits_compile_to_archivexl_streaming_sectors():
    edits = [
        {"op": "delete", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5, "label": "railing_a"},
        {"op": "delete", "sector": SEC, "index": 2, "type": "worldInstancedMeshNode", "expectedNodes": 5,
         "elements": [2, 0], "expectedElements": 3},
        {"op": "delete", "sector": SEC, "index": 2, "type": "worldInstancedMeshNode", "expectedNodes": 5,
         "elements": [1], "expectedElements": 3},
        {"op": "delete", "sector": SEC, "index": 4, "type": "worldCollisionNode", "expectedNodes": 5,
         "elements": [1], "expectedElements": 2},
        {"op": "move", "sector": SEC, "index": 1, "type": "worldStaticMeshNode", "expectedNodes": 5, "pos": [401, 400, 5]},
        {"op": "replace", "sector": SEC, "index": 1, "type": "worldStaticMeshNode", "expectedNodes": 5,
         "res": "base\\environment\\fences\\railing_b.mesh", "app": "rusty"},
        {"op": "add", "res": "base\\environment\\street\\bollard.mesh", "pos": [110, 200, 5], "yaw": 30},
    ]
    n = E.normalize_all(edits)
    assert [x["id"] for x in n] == ["e1", "e2", "e3", "e4", "e5", "e6", "e7"]
    assert E.summary(n) == {"sectors": 1, "deleted": 1, "elementsDeleted": 4, "changed": 1, "elementsMoved": 0, "added": 1}
    text = E.to_xl(n, "Test", blocks=["fivem2open77\\world\\t\\sector\\t.streamingblock"])
    doc = yaml.safe_load(text)
    st = doc["streaming"]
    assert st["blocks"] == ["fivem2open77\\world\\t\\sector\\t.streamingblock"], "backslashes stay literal"
    s = st["sectors"][0]
    assert s["path"] == SEC.lower() and s["expectedNodes"] == 5
    dels = {d["index"]: d for d in s["nodeDeletions"]}
    assert dels[0] == {"index": 0, "type": "worldStaticMeshNode"}
    assert dels[2] == {"index": 2, "type": "worldInstancedMeshNode", "expectedInstances": 3, "instanceDeletions": [0, 1, 2]}
    assert dels[4] == {"index": 4, "type": "worldCollisionNode", "expectedActors": 2, "actorDeletions": [1]}
    m = s["nodeMutations"][0]
    assert m == {"index": 1, "type": "worldStaticMeshNode", "position": [401, 400, 5],
                 "mesh": "base\\environment\\fences\\railing_b.mesh", "appearance": "rusty"}
    # a whole-node deletion swallows element deletions of the same node
    both = E.normalize_all([edits[1], dict(edits[1], elements=None, expectedElements=None)])
    d2 = yaml.safe_load(E.to_xl(both))["streaming"]["sectors"][0]["nodeDeletions"][0]
    assert d2 == {"index": 2, "type": "worldInstancedMeshNode"}


def test_edits_refuse_what_archivexl_would_not_take():
    bad = [
        ({"op": "delete", "sector": "x.mesh", "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5}, "sector"),
        ({"op": "delete", "sector": SEC, "index": 5, "type": "worldStaticMeshNode", "expectedNodes": 5}, "outside"),
        ({"op": "delete", "sector": SEC, "index": 0, "type": "staticmesh", "expectedNodes": 5}, "type"),
        ({"op": "delete", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5,
          "elements": [0], "expectedElements": 3}, "no elements"),
        ({"op": "delete", "sector": SEC, "index": 2, "type": "worldInstancedMeshNode", "expectedNodes": 5,
          "elements": [3], "expectedElements": 3}, "outside"),
        ({"op": "move", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5}, "pos, rot or scale"),
        ({"op": "move", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5, "pos": [1, 2, float("nan")]}, "range"),
        ({"op": "replace", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5, "res": "a.ent"}, ".mesh"),
        ({"op": "add", "res": "c:\\evil.dll", "pos": [0, 0, 0]}, ".mesh or .ent"),
        ({"op": "warp"}, "op"),
    ]
    for e, needle in bad:
        try:
            E.normalize_all([e])
            raise AssertionError(f"accepted {e}")
        except E.EditError as x:
            assert needle in str(x), (needle, str(x))
    try:
        E.normalize_all([{"op": "delete", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5},
                         {"op": "move", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5, "pos": [0, 0, 0]}])
        raise AssertionError("deleted and moved")
    except E.EditError as x:
        assert "deleted and also moved" in str(x)
    try:
        E.normalize_all([{"op": "delete", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5},
                         {"op": "delete", "sector": SEC, "index": 1, "type": "worldStaticMeshNode", "expectedNodes": 6}])
        raise AssertionError("two node counts")
    except E.EditError as x:
        assert "two different node counts" in str(x)


def test_element_moves_compile_to_instance_and_actor_mutations():
    edits = [
        {"op": "move", "sector": SEC, "index": 2, "type": "worldInstancedMeshNode", "expectedNodes": 5,
         "element": 1, "expectedElements": 3, "pos": [105, 201, 5], "rot": [0, 0, 0.3826834, 0.9238795]},
        {"op": "move", "sector": SEC, "index": 4, "type": "worldCollisionNode", "expectedNodes": 5,
         "element": 4, "expectedElements": 5, "pos": [105, 201, 5]},
        {"op": "delete", "sector": SEC, "index": 4, "type": "worldCollisionNode", "expectedNodes": 5,
         "elements": [0], "expectedElements": 5},
    ]
    n = E.normalize_all(edits)
    assert E.summary(n)["elementsMoved"] == 2 and E.summary(n)["changed"] == 0
    st = yaml.safe_load(E.to_xl(n))["streaming"]["sectors"][0]
    muts = {m["index"]: m for m in st["nodeMutations"]}
    assert muts[2] == {"index": 2, "type": "worldInstancedMeshNode", "expectedInstances": 3,
                       "instanceMutations": [{"index": 1, "position": [105, 201, 5, 0],
                                              "orientation": [0, 0, 0.382683, 0.92388]}]}, "four numbers for an element"
    assert muts[4]["expectedActors"] == 5 and muts[4]["actorMutations"] == [{"index": 4, "position": [105, 201, 5, 0]}]
    assert st["nodeDeletions"] == [{"index": 4, "type": "worldCollisionNode", "expectedActors": 5, "actorDeletions": [0]}]
    for bad, needle in (([edits[2], dict(edits[1], element=0)], "deleted and also moved"),
                        ([dict(edits[0], element=3)], "outside"),
                        ([dict(edits[0], type="worldStaticMeshNode", index=0)], "no elements")):
        try:
            E.normalize_all(bad)
            raise AssertionError(bad)
        except E.EditError as x:
            assert needle in str(x), (needle, str(x))


def test_an_objects_collision_follows_it():
    from fivem2open77.world import ops as O
    tmp = tempfile.mkdtemp()
    idx, _ = _index_with(tmp, {SEC: synthetic_sector()})
    # the railing at 102,198: its own actor (#2) goes with it, the others stay
    g = O.delete(idx, SEC, 0)
    assert [(x["role"], x["type"], x.get("elements")) for x in g] == [("object", "worldStaticMeshNode", None),
                                                                     ("collision", "worldCollisionNode", [2])]
    assert len({x["group"] for x in g}) == 1
    # an instance and its actor (#4 at 104,200)
    g = O.delete(idx, SEC, 2, element=1)
    assert [(x["type"], x["elements"]) for x in g] == [("worldInstancedMeshNode", [1]), ("worldCollisionNode", [4])]
    # turning the copy at 400,400 a further 90 degrees about its spot and moving it 2 m east: its actor turns
    # and moves the same way
    g = O.move(idx, SEC, 1, delta=[2, 0, 0], dyaw=90)
    obj, colm = g
    assert obj["pos"] == [402.0, 400.0, 5.0] and abs(abs(S.yaw_of(obj["rot"])) - 180.0) < 1e-3
    assert colm["element"] == 3 and colm["pos"] == [402.0, 400.0, 5.0] and S.same_rotation(colm["rot"], obj["rot"])
    xl = yaml.safe_load(E.to_xl(E.normalize_all(g)))["streaming"]["sectors"][0]
    assert {m["index"] for m in xl["nodeMutations"]} == {1, 4}
    # the description people see
    lines = O.describe(E.normalize_all(O.delete(idx, SEC, 0)))
    assert lines[0]["text"] == "delete railing_a (+ its collision: 1 piece)"
    # a place two meshes share: its collision is theirs too, so it stays unless both go
    doc = synthetic_sector()
    root = doc["Data"]["RootChunk"]
    root["nodeData"]["Data"].append(_nd(0, 102, 198, 5))               # a second railing at the same spot
    idx2, _ = _index_with(tempfile.mkdtemp(), {SEC: doc})
    notes = []
    g = O.delete(idx2, SEC, 0, notes=notes)
    assert [x["role"] for x in g] == ["object"] and "stand at exactly the same place" in notes[0]
    g = O.delete_many(idx2, [(SEC, 0, None), (SEC, 5, None)])
    assert sorted(x["role"] for x in g) == ["collision", "object", "object"]
    assert next(x for x in g if x["role"] == "collision")["expectedNodes"] == 6


def test_an_entitys_far_view_goes_and_moves_with_it():
    """A sign (worldEntityNode) has a stand-in for far views in a coarser sector: a
    worldEntityProxyMeshNode exactly at its place (as Night City's sectors have them).  Deleting or
    moving the sign takes it along; an entity with no proxy at its place takes nothing."""
    from fivem2open77.world import ops as O
    sec2 = "base\\worlds\\03_night_city\\_compiled\\default\\exterior_0_0_0_4.streamingsector"
    doc = synthetic_sector()
    root = doc["Data"]["RootChunk"]
    root["nodes"].append({"HandleId": "9", "Data": {"$type": "worldEntityNode", "debugName": _cn("road_sign_a_1x1_004"),
                                                    "entityTemplate": _rp("base\\gameplay\\devices\\street_signs\\road_sign_a_1x1.ent"),
                                                    "appearanceName": _cn("default")}})
    root["nodeData"]["Data"].append(_nd(4, 130, 240, 5, -0.898858428, 0.438239098))      # entity #5
    root["nodeData"]["Data"].append(_nd(4, 160, 250, 5))                                  # entity #6: no proxy
    far = {"Header": {}, "Data": {"RootChunk": {"$type": "worldStreamingSector", "level": 1, "category": "Exterior", "nodes": [
        {"HandleId": "0", "Data": {"$type": "worldEntityProxyMeshNode",
                                   "mesh": _rp("base\\worlds\\03_night_city\\sectors\\_external\\proxy\\1\\road_sign_a_1x1.mesh"),
                                   "meshAppearance": _cn("default")}},
        {"HandleId": "1", "Data": {"$type": "worldGenericProxyMeshNode", "mesh": _rp("base\\worlds\\proxy\\block.mesh")}}],
        "nodeData": {"Data": [_nd(0, 130, 240, 5, -0.898858428, 0.438239098), _nd(1, 130, 240, 5)]}}}}
    idx, _ = _index_with(tempfile.mkdtemp(), {SEC: doc, sec2: far})
    sec, ent = idx.entry(SEC, 5)
    comp = O.companions(idx, sec, ent)
    assert [(p["sector"], p["index"], p["type"]) for p in comp["proxies"]] == [(sec2, 0, "worldEntityProxyMeshNode")]
    g = O.delete(idx, SEC, 5)
    assert [(x["role"], x["type"]) for x in g] == [("object", "worldEntityNode"), ("proxy", "worldEntityProxyMeshNode")]
    assert O.describe(E.normalize_all(g))[0]["text"] == "delete road_sign_a_1x1_004 (+ its far view)"
    st = {x["path"]: x for x in yaml.safe_load(E.to_xl(E.normalize_all(g)))["streaming"]["sectors"]}
    assert st[sec2]["nodeDeletions"] == [{"index": 0, "type": "worldEntityProxyMeshNode"}] and st[sec2]["expectedNodes"] == 2
    # the far view is not deleted twice when it is named too
    g = O.delete_many(idx, [(SEC, 5, None), (sec2, 0, None)])
    assert sorted((x["role"], x["sector"].split("\\")[-1]) for x in g) == [("object", "exterior_0_0_0_3.streamingsector"),
                                                                            ("object", "exterior_0_0_0_4.streamingsector")]
    # moved 2 m east and turned 30 degrees: the far view stands where the sign stands, turned the same
    obj, prox = O.move(idx, SEC, 5, delta=[2, 0, 0], dyaw=30)
    assert prox["role"] == "proxy" and prox["pos"] == obj["pos"] == [132.0, 240.0, 5.0]
    assert S.same_rotation(prox["rot"], obj["rot"])
    # no proxy at the other sign's place: nothing comes along
    assert [x["role"] for x in O.delete(idx, SEC, 6)] == ["object"]


def test_clear_area_deletes_everything_of_a_kind_with_its_collision():
    from fivem2open77.world import ops as O
    idx, _ = _index_with(tempfile.mkdtemp(), {SEC: synthetic_sector()})
    g = O.clear_area(idx, 104, 199, 5)
    objs = [(x["type"], x.get("elements")) for x in g if x["role"] == "object"]
    assert ("worldStaticMeshNode", None) in objs and ("worldInstancedMeshNode", [0]) in objs
    assert ("worldInstancedMeshNode", [2]) in objs and len(objs) == 4
    cols = sorted(x["elements"][0] for x in g if x["role"] == "collision")
    assert cols == [2, 4]
    n = E.normalize_all(g)
    st = yaml.safe_load(E.to_xl(n))["streaming"]["sectors"][0]
    dels = {d["index"]: d for d in st["nodeDeletions"]}
    assert dels[2]["instanceDeletions"] == [0, 1, 2] and dels[4]["actorDeletions"] == [2, 4]


def test_a_world_edit_becomes_a_resource_and_a_deploy_bundle():
    from test_mlo import _FakeWk
    tmp = tempfile.mkdtemp()
    edits = [{"op": "delete", "sector": SEC, "index": 0, "type": "worldStaticMeshNode", "expectedNodes": 5}]
    r = P.build("world_test", "Test clearing", edits, tmp, wk=None, log=lambda *a: None)
    res = r["path"]
    man = open(os.path.join(res, "open77.lua")).read()
    assert 'resource "world_test"' in man and 'preload_mod "dist/world_test.zip"' in man and 'dependency "archivexl >=1.27.1"' in man
    with zipfile.ZipFile(r["zip"]) as z:
        assert z.namelist() == ["archive/pc/mod/fivem2open77_world_world_test.xl"], "deletions need no archive"
        xl = yaml.safe_load(z.read("archive/pc/mod/fivem2open77_world_world_test.xl"))
    assert xl["streaming"]["sectors"][0]["nodeDeletions"] == [{"index": 0, "type": "worldStaticMeshNode"}]
    info = json.load(open(os.path.join(res, "edits.json")))
    assert info["summary"]["deleted"] == 1 and info["package"]["sha256"] == r["sha256"]
    assert "exports(\"edits\"" in open(os.path.join(res, "server", "main.lua")).read()
    # adding objects needs WolvenKit: refused without, an archive with the sector and hosts with it
    adds = edits + [{"op": "add", "res": "base\\environment\\street\\bollard.mesh", "pos": [110, 200, 5], "yaw": 30},
                    {"op": "add", "res": "base\\environment\\street\\bollard.mesh", "pos": [112, 200, 5]},
                    {"op": "add", "res": "base\\gameplay\\devices\\vending_machine.ent", "pos": [114, 200, 5], "app": "red"}]
    try:
        P.build("world_test", "Test clearing", adds, tmp, wk=None, log=lambda *a: None)
        raise AssertionError("adds without WolvenKit")
    except RuntimeError as x:
        assert "WolvenKit" in str(x)
    wk = _FakeWk()
    r2 = P.build("world_test", "Test clearing", adds, tmp, wk=wk, log=lambda *a: None)
    with zipfile.ZipFile(r2["zip"]) as z:
        names = sorted(z.namelist())
        xl = yaml.safe_load(z.read("archive/pc/mod/fivem2open77_world_world_test.xl"))
    assert names == ["archive/pc/mod/fivem2open77_world_world_test.archive", "archive/pc/mod/fivem2open77_world_world_test.xl"]
    assert xl["streaming"]["blocks"] == ["fivem2open77\\world\\world_test\\sector\\world_test.streamingblock"]
    docs, _ = P.additions_docs("world_test", E.plan(E.normalize_all(adds))["adds"])
    paths = [p for _, p in docs]
    assert sum(1 for p in paths if p.endswith(".ent")) == 1, "one host entity per distinct mesh; .ent templates used as they are"
    sector = next(d for d, p in docs if p.endswith(".streamingsector"))
    nodes = sector["Data"]["RootChunk"]["nodes"]
    assert len(nodes) == 3 and nodes[2]["Data"]["entityTemplate"]["DepotPath"]["$value"].endswith("vending_machine.ent")
    assert nodes[2]["Data"]["appearanceName"]["$value"] == "red"
    b = P.bundle([res], os.path.join(tmp, "world.tgz"))
    with tarfile.open(b["path"]) as t:
        got = sorted(t.getnames())
    assert "resources/assets/world/world_test/open77.lua" in got and "resources/assets/world/world_test/dist/world_test.zip" in got


def test_real_sectors_when_present():
    files = sorted(glob.glob(os.path.join(REAL, "json*", "exterior_*.streamingsector.json")))
    if not files:
        print("   (skipped: no sectors read from a game)")
        return
    for fp in files:
        doc = json.load(open(fp))
        root = doc["Data"]["RootChunk"]
        sec = S.read_sector(doc, "base\\worlds\\03_night_city\\_compiled\\default\\" + os.path.basename(fp)[:-5])
        assert sec["nodes"] == len(root["nodeData"]["Data"]) == len(sec["entries"]), fp
        for e in sec["entries"]:
            if e["type"] == "worldInstancedMeshNode":
                assert e["elements"]["count"] >= len(e["elements"]["items"]) > 0, (fp, e["i"])
                for it in e["elements"]["items"]:
                    assert abs(it["pos"][0]) > 100 and abs(it["pos"][1]) > 100, "instances are in world coordinates"
            if e["type"] == "worldCollisionNode":
                assert e["elements"]["count"] > 0
                assert all(abs(it["pos"][0]) > 100 for it in e["elements"]["items"])
    # the collision of a mesh lives in another sector: found by its place
    from fivem2open77.world import ops as O
    tmp = tempfile.mkdtemp()
    real = {"base\\worlds\\03_night_city\\_compiled\\default\\" + os.path.basename(fp)[:-5]: json.load(open(fp)) for fp in files}
    idx, _ = _index_with(tmp, real)
    cross = 0
    for path in real:
        sec = idx.sector(path)
        for e in sec["entries"]:
            if e["kind"] != "mesh":
                continue
            g = O.delete(idx, path, e["i"])
            cols = [x for x in g if x["role"] == "collision"]
            cross += sum(1 for x in cols if x["sector"] != path)
            n = E.normalize_all(g)
            for st in yaml.safe_load(E.to_xl(n))["streaming"]["sectors"]:
                assert st["expectedNodes"] == idx.sector(st["path"])["nodes"]
    assert cross > 0, "some mesh's collision sits in a coarser sector"
    # a delete of every railing near the first sector's middle compiles and reads back
    doc = json.load(open(files[0]))
    path = "base\\worlds\\03_night_city\\_compiled\\default\\" + os.path.basename(files[0])[:-5]
    sec = S.read_sector(doc, path)
    rails = [e for e in sec["entries"] if "railing" in e["res"]]
    if rails:
        edits = [{"op": "delete", "sector": path, "index": e["i"], "type": e["type"], "expectedNodes": sec["nodes"]} for e in rails]
        xl = yaml.safe_load(E.to_xl(E.normalize_all(edits)))
        assert len(xl["streaming"]["sectors"][0]["nodeDeletions"]) == len(rails)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            f()
            print("ok  ", n)
