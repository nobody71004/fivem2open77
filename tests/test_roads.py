"""Night City's roads from the game's traffic lanes (fivem2open77/mlo/roads.py): the WolvenKit JSON of
all.traffic_persistent + all.lane_polygons -> lanes with their kind, simplified, written as 512 m
tiles for the map manager, read back and queried (nearest lanes, heading, distance to the edge),
and the extraction from a game folder through WolvenKit (a fake one here)."""
import json
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from fivem2open77.mlo import roads as R  # noqa: E402


def _vec(x, y, z):
    return {"$type": "Vector3", "X": x, "Y": y, "Z": z}


def _lane(flags, width=5.0, speed=15, material="Concrete"):
    return {"$type": "worldTrafficLanePersistent", "flags": flags, "width": width, "maxSpeed": speed,
            "outline": [], "polygon": [], "roadMaterials": [{"$type": "worldRoadMaterialInfo", "material": material,
                                                               "startOffset": 0}] if material else []}


def _docs():
    """Four lanes: a straight road going north along x=100 (with a redundant middle point), its
    pavement 4.5 m east, a highway east-west through two tiles, and a lane without an outline."""
    persistent = {"Header": {"GameVersion": 2310}, "Data": {"RootChunk": {
        "$type": "worldTrafficPersistentResource", "data": {"$type": "worldTrafficPersistentData", "lanes": [
            _lane(0x11), _lane(0x0B, width=2.0, speed=1), _lane(0x4011, width=6.2, speed=17), _lane(0x31)]}}}}
    polygons = {"Data": {"RootChunk": {"$type": "worldTrafficPersistentLanePolygonResource", "data": [
        {"index": 0, "value": {"outline": [_vec(100, 0, 10), _vec(100, 50, 10.02), _vec(100, 100, 10)]}},
        {"index": 1, "value": {"outline": [_vec(104.5, 0, 10.2), _vec(104.5, 100, 10.2)]}},
        {"index": 2, "value": {"outline": [_vec(400, 300, 5), _vec(700, 300, 5)]}},
        {"index": 3, "value": {"outline": [_vec(1, 1, 1)]}},
    ]}}}
    return persistent, polygons


def test_lanes_kinds_and_simplify():
    lanes = R.lanes_from_json(*_docs())
    assert [ln["id"] for ln in lanes] == [0, 1, 2], "a lane without a centre line is left out"
    assert [ln["kind"] for ln in lanes] == ["r", "p", "h"]
    assert R.kind_of(0x31) == "i" and R.kind_of(0x12B) == "c" and R.kind_of(0x2B) == "j" and R.kind_of(0x4) == "t"
    assert lanes[0]["width"] == 5.0 and lanes[0]["speed"] == 15 and lanes[0]["material"] == "Concrete"
    pts = R.simplify(lanes[0]["points"])
    assert len(pts) == 2 and pts[0] == (100, 0, 10) and pts[-1] == (100, 100, 10), pts
    bent = [(0, 0, 0), (10, 0.5, 0), (20, 0, 0)]
    assert len(R.simplify(bent)) == 3, "a real bend stays"


def test_tiles_written_and_queried():
    tmp = tempfile.mkdtemp(prefix="f2o77-roads-")
    try:
        out = os.path.join(tmp, "roads")
        idx = R.write_tiles(R.lanes_from_json(*_docs()), out, source="test")
        assert idx["lanes"] == 3 and idx["kinds"] == {"r": 1, "p": 1, "h": 1}
        assert set(idx["tiles"]) == {"0_-1", "0_0", "1_0"}, idx["tiles"]   # half a lane reaches y < 0; the highway crosses x = 512
        assert all(t["bytes"] < 4 * 1024 * 1024 for t in idx["tiles"].values())
        text = open(os.path.join(out, "0_0.lanes")).read()
        assert text.splitlines()[0] == "0 r 5 15 100 0 10 100 100 10", text
        # writing again replaces the folder as a whole
        R.write_tiles(R.lanes_from_json(*_docs())[:1], out, source="test")
        assert set(json.load(open(os.path.join(out, "index.json")))["tiles"]) == {"0_-1", "0_0"}
        R.write_tiles(R.lanes_from_json(*_docs()), out, source="test")

        roads = R.Roads(out)
        near = roads.near(102.0, 40.0, radius=10)
        assert [ln["id"] for ln in near] == [0, 1], near
        road = near[0]
        assert road["distance"] == 2.0 and road["edge"] == -0.5, "inside the 5 m lane, half a metre from its side"
        assert abs(road["heading"]) < 1e-6, "north is yaw 0"
        assert road["closest"] == (100.0, 40.0, 10.0)
        assert [ln["id"] for ln in roads.near(102.0, 40.0, radius=10, kinds=R.CAR_KINDS)] == [0]
        hw = roads.nearest(600.0, 290.0)
        assert hw["id"] == 2 and hw["kind"] == "h" and abs(hw["heading"] + 90) < 1e-6, "east is yaw -90"
        assert roads.near(600.0, 290.0, radius=50) and roads.near(450.0, 300.0, radius=5)[0]["id"] == 2, "both tiles"
        assert len({ln["id"] for ln in roads.near(512.0, 300.0, radius=100)}) == len(roads.near(512.0, 300.0, radius=100)), \
            "a lane in two tiles is found once"
        assert roads.near(-5000, -5000, radius=20) == []
        assert R.parse_line("0 r 5 15 100 0 10 100 100 10")["points"] == [(100, 0, 10), (100, 100, 10)]
        assert R.parse_line("garbage") is None and R.parse_line("1 r 5 15 1 2") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeWk:
    """unbundle writes the two files where WolvenKit would; serialize writes our JSON for them."""

    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def unbundle(self, archives, out_dir, regex):
        import re
        self.calls.append(("unbundle", [os.path.basename(a) for a in archives], regex))
        for name in R.FILES:
            path = R.TRAFFIC_DIR + "\\" + name
            assert re.match(regex, path), (regex, path)
            dst = os.path.join(out_dir, *path.split("\\"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(b"CR2W")

    def serialize(self, path, out_dir):
        self.calls.append(("serialize", os.path.basename(path)))
        name = os.path.basename(path)
        with open(os.path.join(out_dir, name + ".json"), "w") as f:
            json.dump(self.docs[R.FILES.index(name)], f)


def test_extract_from_a_game_folder():
    tmp = tempfile.mkdtemp(prefix="f2o77-roads-game-")
    try:
        game = os.path.join(tmp, "game")
        for sub, names in (("content", ["basegame_3_nightcity.archive", "basegame_4_gamedata.archive", "lang_en_text.archive"]),
                           ("ep1", ["ep1_1_nightcity.archive"])):
            os.makedirs(os.path.join(game, "archive", "pc", sub))
            for n in names:
                open(os.path.join(game, "archive", "pc", sub, n), "wb").close()
        wk = _FakeWk(_docs())
        idx = R.extract(game, wk, os.path.join(tmp, "roads"), log=lambda *a: None)
        assert idx["lanes"] == 3 and idx["source"] == "Cyberpunk 2077 game data 2310"
        unb = wk.calls[0]
        assert unb[1] == ["basegame_3_nightcity.archive", "basegame_4_gamedata.archive", "ep1_1_nightcity.archive"], \
            "base first, EP1 last (it wins); no language archives"
        assert [c[1] for c in wk.calls[1:]] == list(R.FILES)
        try:
            R.extract(os.path.join(tmp, "nothing"), wk, os.path.join(tmp, "r2"), log=lambda *a: None)
            raise AssertionError("no game: refused")
        except RuntimeError as e:
            assert "no game archives" in str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli():
    import subprocess
    tmp = tempfile.mkdtemp(prefix="f2o77-roads-cli-")
    try:
        src = os.path.join(tmp, "json")
        os.makedirs(src)
        for name, doc in zip(R.FILES, _docs()):
            with open(os.path.join(src, name + ".json"), "w") as f:
                json.dump(doc, f)
        out = os.path.join(tmp, "roads")
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, ".."))
        r = subprocess.run([sys.executable, "-m", "fivem2open77", "mlo-roads", "--from-json", src, "--out", out, "--json"],
                           capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        res = json.loads(r.stdout)
        assert res["ok"] and res["lanes"] == 3 and res["tiles"] == 3, res
        r = subprocess.run([sys.executable, "-m", "fivem2open77", "mlo-roads", "--roads", out, "--near", "102,40",
                            "--radius", "10", "--json"], capture_output=True, text=True, env=env, timeout=120)
        res = json.loads(r.stdout)
        assert res["ok"] and [ln["id"] for ln in res["lanes"]] == [0, 1] and res["lanes"][0]["points"] == 2, res
        r = subprocess.run([sys.executable, "-m", "fivem2open77", "mlo-roads", "--near", "1,2", "--json"],
                           capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode != 0 and "--roads" in (r.stdout + r.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_cli()
    test_lanes_kinds_and_simplify()
    test_tiles_written_and_queried()
    test_extract_from_a_game_folder()
    print("ok")
