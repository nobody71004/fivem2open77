"""Night City off: the city's streaming blocks without its sectors, packed as a world edit."""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import zipfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fivem2open77.cp77 import package as cpkg  # noqa: E402
from fivem2open77.world import nightcity as NC  # noqa: E402
from fivem2open77.world import package as P  # noqa: E402


def _desc(path, cat, level=0):
    return {"$type": "worldStreamingSectorDescriptor", "category": cat, "level": level,
            "data": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": path}, "Flags": "Soft"},
            "numNodeRanges": 1, "streamingBox": {"$type": "Box", "Max": {"X": 1, "Y": 1, "Z": 1, "W": 1},
                                                 "Min": {"X": 0, "Y": 0, "Z": 0, "W": 1}}, "variants": []}


def _doc(root):
    return {"Header": {"WolvenKitVersion": "8.17.4", "DataType": "CR2W"}, "Data": {"Version": 195, "RootChunk": root}}


BASE = _doc({"$type": "worldStreamingBlock", "cookingPlatform": "PLATFORM_PC", "descriptors": [
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\exterior_0_0_0_0.streamingsector", "Exterior"),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\exterior_0_0_0_1.streamingsector", "Exterior", 1),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\interior_0_0_0_0.streamingsector", "Interior"),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\quest_1.streamingsector", "Quest"),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\navigation_0_0_0_3.streamingsector", "Navigation", 3),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\always_loaded_0.streamingsector", "AlwaysLoaded"),
]})
EP1 = _doc({"$type": "worldStreamingBlock", "cookingPlatform": "PLATFORM_PC",
            "index": {"$type": "worldStreamingBlockIndex", "oup": "EP1", "rldGridCell": 8279898}, "descriptors": [
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\ep1\\exterior_-38_-41_-2_0.streamingsector", "Exterior"),
    _desc("base\\worlds\\03_night_city\\_compiled\\default\\ep1\\always_loaded_0.streamingsector", "AlwaysLoaded"),
]})
LIGHTS = _doc({"$type": "CDistantLightsResource", "cookingPlatform": "PLATFORM_None",
               "data": {"BufferId": "0", "Flags": 0, "Bytes": "o9X/AN2l/kEARQBRADN/fjygF4Mg"}})
GAME = {NC.BLOCKS[0]: BASE, NC.BLOCKS[1]: EP1, NC.DISTANT_LIGHTS: LIGHTS}


def _cats(doc):
    return [d["category"] for d in doc["Data"]["RootChunk"]["descriptors"]]


def test_a_block_keeps_only_what_is_not_the_city():
    out, counts = NC.filter_block(BASE)
    assert _cats(out) == ["Navigation", "AlwaysLoaded"]
    assert counts == {"Exterior": [0, 2], "Interior": [0, 1], "Quest": [0, 1], "Navigation": [1, 0],
                      "AlwaysLoaded": [1, 0]}
    assert len(BASE["Data"]["RootChunk"]["descriptors"]) == 6          # the game's copy is not changed
    assert out["Header"] == BASE["Header"] and out["Data"]["Version"] == 195
    out, counts = NC.filter_block(EP1)
    assert _cats(out) == ["AlwaysLoaded"] and out["Data"]["RootChunk"]["index"]["oup"] == "EP1"
    everything, _ = NC.filter_block(BASE, keep=())
    assert _cats(everything) == []
    with pytest.raises(NC.CityError):
        NC.filter_block(LIGHTS)


def test_the_distant_lights_go_dark():
    out = NC.dark_distant_lights(LIGHTS)
    assert out["Data"]["RootChunk"]["data"] == {"BufferId": "0", "Flags": 0, "Bytes": ""}
    assert LIGHTS["Data"]["RootChunk"]["data"]["Bytes"]                  # untouched
    with pytest.raises(NC.CityError):
        NC.dark_distant_lights(BASE)


def test_documents_and_the_report():
    docs, report = NC.documents(GAME.__getitem__)
    assert [p for _, p in docs] == list(NC.FILES)
    assert NC.removed_sectors(report) == 5 and NC.kept_sectors(report) == 3
    assert NC.normalize_state(None) == "on" and NC.normalize_state("OFF") == "off" and NC.normalize_state(False) == "off"
    with pytest.raises(NC.CityError):
        NC.normalize_state("dim")


# ----------------------------------------------------------------------------- the package
def _archive_of(src_root) -> bytes:
    """WolvenKit's pack in miniature: one stored segment per file, indexed by depot hash."""
    blobs = []
    for dp, _, fns in os.walk(src_root):
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            depot = os.path.relpath(full, src_root).replace(os.sep, "\\")
            blobs.append((cpkg.depot_hash(depot), open(full, "rb").read()))
    blobs.sort()
    data, segs, entries = bytearray(), bytearray(), bytearray()
    for i, (h, raw) in enumerate(blobs):
        segs += struct.pack("<QII", 40 + len(data), len(raw), len(raw))
        entries += struct.pack("<Qq5I20s", h, 0, 0, i, i + 1, 0, 0, b"\0" * 20)
        data += raw
    table = struct.pack("<III", len(blobs), len(blobs), 0) + bytes(entries) + bytes(segs)
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, 40 + len(data), len(table) + 16, 0, 0, 0)
    return head + bytes(data) + struct.pack("<IIQ", 8, len(table) + 8, cpkg._crc64_xz(table)) + table


class _Wk:
    def __init__(self):
        self.docs = {}

    def deserialize_tree(self, jdir, adir, work):
        for dp, _, fns in os.walk(jdir):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), jdir)
                with open(os.path.join(dp, fn), encoding="utf-8") as f:
                    self.docs[rel[:-5].replace(os.sep, "\\")] = json.load(f)
                dst = os.path.join(adir, rel[:-5])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(b"CR2W" + rel.encode())

    def import_glb(self, rdir, adir):
        pass

    def pack(self, src_root, pdir):
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, os.path.basename(src_root) + ".archive"), "wb") as f:
            f.write(_archive_of(src_root))

    def kill(self):
        pass


def _quiet(_m):
    pass


def test_a_world_edit_that_only_turns_the_city_off(tmp_path):
    docs, _ = NC.documents(GAME.__getitem__)
    wk = _Wk()
    r = P.build("nightcity_off", "Night City off", [], str(tmp_path), wk=wk, log=_quiet, city_docs=docs)
    assert r["summary"]["city"] == "off" and r["summary"]["sectors"] == 0
    res = tmp_path / "nightcity_off"
    with zipfile.ZipFile(res / "dist" / "nightcity_off.zip") as z:
        assert z.namelist() == ["archive/pc/mod/fivem2open77_world_nightcity_off.archive"]     # nothing to patch: no .xl
        files, _segs = cpkg._read_index(io.BytesIO(z.read(z.namelist()[0])))
    assert sorted(h for h, _, _ in files) == sorted(cpkg.depot_hash(p) for p in NC.FILES)
    # WolvenKit got the filtered blocks and the dark lights, at the game's own depot paths
    assert _cats(wk.docs[NC.BLOCKS[0]]) == ["Navigation", "AlwaysLoaded"] and _cats(wk.docs[NC.BLOCKS[1]]) == ["AlwaysLoaded"]
    assert wk.docs[NC.DISTANT_LIGHTS]["Data"]["RootChunk"]["data"]["Bytes"] == ""
    man = (res / "open77.lua").read_text(encoding="utf-8")
    assert 'preload_mod "dist/nightcity_off.zip"' in man and "Night City OFF" in man
    main = (res / "server" / "main.lua").read_text(encoding="utf-8")
    assert 'city = "off"' in main and "Night City is OFF" in main
    assert json.loads((res / "edits.json").read_text(encoding="utf-8"))["summary"]["city"] == "off"
    assert "Night City is off" in (res / "README.md").read_text(encoding="utf-8")
    import shutil as _sh
    if _sh.which("luac"):
        import subprocess
        out = subprocess.run(["luac", "-p", str(res / "server" / "main.lua")], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
    # without WolvenKit it cannot be built, and without anything at all there is nothing to build
    with pytest.raises(RuntimeError, match="Night City off needs WolvenKit|turning Night City off needs WolvenKit"):
        P.build("nightcity_off", "Night City off", [], str(tmp_path / "x"), wk=None, log=_quiet, city_docs=docs)
    with pytest.raises(Exception, match="nothing to build"):
        P.build("nightcity_off", "Night City off", [], str(tmp_path / "y"), wk=wk, log=_quiet)


def test_the_city_off_next_to_edits(tmp_path):
    docs, _ = NC.documents(GAME.__getitem__)
    edit = {"op": "delete", "sector": "base\\worlds\\03_night_city\\_compiled\\default\\exterior_-6_-4_0_0.streamingsector",
            "expectedNodes": 120, "index": 7, "type": "worldStaticMeshNode", "group": "g0123456789"}
    r = P.build("my_edit", "My edit", [edit], str(tmp_path), wk=_Wk(), log=_quiet, city_docs=docs)
    with zipfile.ZipFile(tmp_path / "my_edit" / "dist" / "my_edit.zip") as z:
        assert sorted(z.namelist()) == ["archive/pc/mod/fivem2open77_world_my_edit.archive",
                                        "archive/pc/mod/fivem2open77_world_my_edit.xl"]
        assert "exterior_-6_-4_0_0" in z.read("archive/pc/mod/fivem2open77_world_my_edit.xl").decode()
    assert r["summary"]["city"] == "off" and r["summary"]["deleted"] == 1
    r2 = P.build("my_edit", "My edit", [edit], str(tmp_path / "on"), wk=None, log=_quiet)
    assert r2["summary"]["city"] == "on"
    with zipfile.ZipFile(tmp_path / "on" / "my_edit" / "dist" / "my_edit.zip") as z:
        assert z.namelist() == ["archive/pc/mod/fivem2open77_world_my_edit.xl"]


# ----------------------------------------------------------------------------- the Studio
def test_the_studio_switch_builds_tests_and_reports(tmp_path):
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-nc-")
    studio = Studio(ws)
    try:
        jdir = tmp_path / "extracted"
        jdir.mkdir()
        asked = []

        def extract(paths):
            asked.append(list(paths))
            out = {}
            for p in paths:
                if p in GAME:
                    f = jdir / (os.path.basename(p) + f"-{len(out)}.json")
                    f.write_text(json.dumps(GAME[p]), encoding="utf-8")
                    out[p] = str(f)
            return out

        game = tmp_path / "game"
        (game / "archive" / "pc" / "content").mkdir(parents=True)
        wk = _Wk()
        studio.world_extractor = extract
        studio.toolbox = lambda refresh=False: SimpleNamespace(game_dir=str(game), wolvenkit="WolvenKit.CLI.dll",
                                                               wolvenkit_obj=lambda log=None: wk)
        p = studio.create_world_project(name="nightcity_off", display="Night City off", city="off")
        st = studio.world_state(p.id)
        assert st["city"] == "off" and st["display"] == "Night City off"
        assert studio.projects.get(p.id).data["world"]["city"] == "off"
        job = studio.world_build(p.id)                       # no edits: the city switch alone is enough
        studio.jobs.wait(job, timeout=60)
        assert job.state == "done", job.error
        b = job.result
        assert b["ok"] and b["city"] == "off" and b["summary"]["city"] == "off", b
        assert b["checks"]["city"]["removed"] == 5 and b["checks"]["city"]["kept"] == 3, b["checks"]
        assert sorted(asked[0]) == sorted(NC.FILES)
        lines = " ".join(l["text"] if isinstance(l, dict) else str(l) for l in getattr(job, "lines", []) or [])
        st = studio.night_city()
        assert [o["project"] for o in st["off"]] == [p.id] and st["off"][0]["build"]["ok"], st
        # test it in the game: the archive goes in, and the job says the city is off there
        job = studio.world_game(p.id)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "done", job.error
        assert os.listdir(game / "archive" / "pc" / "mod") == ["fivem2open77_world_nightcity_off.archive"]
        assert studio.night_city()["off"][0]["game"] is True
        job = studio.world_game(p.id, uninstall=True)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "done" and os.listdir(game / "archive" / "pc" / "mod") == []
        # back on: nothing left to build in this edit
        assert studio.world_set(p.id, city="on")["city"] == "on"
        with pytest.raises(ValueError, match="nothing to build"):
            studio.world_build(p.id)
        assert studio.night_city()["off"] == []
        with pytest.raises(ValueError):
            studio.world_set(p.id, city="dim")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_the_agent_tool_turns_the_city_off_without_a_project():
    from fivem2open77.studio.app import Studio
    from fivem2open77.studio.agent import worldtools
    from fivem2open77.studio.agent.tools import Context, tools_for
    ws = tempfile.mkdtemp(prefix="f2o77-nc-agent-")
    studio = Studio(ws)
    try:
        assert "night_city" in tools_for(None) and "night_city" in tools_for("world")
        sess = SimpleNamespace(project=None, note=lambda m: None, attach_job=lambda j: None, stopping=False)
        sess.switch_project = lambda pid: setattr(sess, "project", pid)
        ctx = Context(studio, sess)
        out = json.loads(worldtools.t_night_city(ctx, {"state": "off"})[0]["text"])
        assert out["city"] == "off" and out["name"] == "nightcity_off" and sess.project == out["project"]
        st = json.loads(worldtools.t_night_city(ctx, {})[0]["text"])
        assert [o["name"] for o in st["off"]] == ["nightcity_off"]
        out = json.loads(worldtools.t_night_city(ctx, {"state": "on"})[0]["text"])
        assert out["city"] == "on"
    finally:
        shutil.rmtree(ws, ignore_errors=True)
