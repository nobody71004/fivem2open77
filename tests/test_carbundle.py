"""Car bundles (studio/carbundle.py): every car on a server in a few resources, cars keeping their
bundle from one deploy to the next, read back from what the server has; installed with
server_deploy.sh against the fake server of test_server_deploy."""
import json
import os
import subprocess
import sys
import types
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77 import package as pkg  # noqa: E402
from fivem2open77.studio import carbundle as cb  # noqa: E402
from fivem2open77.studio import deploy as deploy_mod  # noqa: E402

from test_server_deploy import Fake  # noqa: E402


def _car(out, name, version="1.0.2", files=None, json_info=True):
    """A built car resource as cp77.package.write_resource writes it."""
    d = os.path.join(out, name)
    zp = os.path.join(d, "dist", f"{name}.zip")
    os.makedirs(os.path.dirname(zp), exist_ok=True)
    with zipfile.ZipFile(zp, "w") as z:
        for arc, data in (files or {f"archive/pc/mod/fivem2open77_{name}.archive": name.encode() * 50,
                                    f"r6/tweaks/fivem2open77/{name}.yaml": f"Vehicle.{name}: {{}}\n".encode()}).items():
            z.writestr(zipfile.ZipInfo(arc, (2026, 1, 1, 0, 0, 0)), data)
    neon = {"colors": ["white", "blue"], "labels": {"white": "White", "blue": "Blue"},
            "appearances": {"white": f"{name}_neon_white", "blue": f"{name}_neon_blue"}, "default": "white",
            "tubes": {"left": 1, "right": 1}}
    pkg.write_resource(d, name=name, version=version, display=name.upper(), record=f"Vehicle.{name}",
                       packages=[f"{name}.zip"], neon=neon)
    if not json_info:
        os.remove(os.path.join(d, "server", "car.json"))
    return d


def test_lua_tables_read_back():
    info = pkg.car_info("a80", "Vehicle.a80", 'Supra "A80"', "a80", "1.0.2",
                       {"colors": ["white"], "labels": {"white": "White"}, "appearances": {"white": "a80_w"},
                        "default": "white", "tubes": {"left": 1}})
    text = "local CAR = " + pkg._lua(info) + "\n"
    back, _ = cb.parse_lua_table(text, len("local CAR = "))
    assert back == json.loads(json.dumps(info))
    v, _ = cb.parse_lua_table('{ 1, -2.5, true, false, { x = "y\\n" } }')
    assert v == [1, -2.5, True, False, {"x": "y\n"}]
    v, _ = cb.parse_lua_table('{ ["a b"] = 3, c = "d" }')
    assert v == {"a b": 3, "c": "d"}


def test_car_resources_with_and_without_car_json(tmp_path):
    out = str(tmp_path / "out")
    _car(out, "urus")
    _car(out, "ikx3_revsv27b", json_info=False)                 # a 1.6.0 build: no car.json
    os.makedirs(os.path.join(out, "urus_chime"))
    open(os.path.join(out, "urus_chime", "open77.lua"), "w").write('resource "urus_chime"\n')
    cars = cb.car_resources(out)
    assert [c["record"] for c in cars] == ["Vehicle.ikx3_revsv27b", "Vehicle.urus"]
    assert cars[0]["info"]["neon"]["colors"][0]["appearance"] == "ikx3_revsv27b_neon_white"
    assert cb.addons_of(out) == ["urus_chime"]


def test_plan_keeps_cars_in_their_bundle():
    existing = {"f2o77_cars_1": ["Vehicle.a", "Vehicle.b", "Vehicle.gone"], "f2o77_cars_2": ["Vehicle.x", "Vehicle.y"]}
    p = cb.plan(["Vehicle.y", "Vehicle.a", "Vehicle.new1", "Vehicle.b", "Vehicle.x", "Vehicle.new2", "Vehicle.new3"],
                existing, size=3)
    assert p["f2o77_cars_1"] == ["Vehicle.a", "Vehicle.b", "Vehicle.new1"]   # the slot "gone" left is filled
    assert p["f2o77_cars_2"] == ["Vehicle.new2", "Vehicle.x", "Vehicle.y"]
    assert p["f2o77_cars_3"] == ["Vehicle.new3"]
    # the first deploy onto a server with none: alphabetical, BUNDLE_SIZE each
    recs = [f"Vehicle.c{i:02d}" for i in range(26)]
    p = cb.plan(recs, {})
    assert list(p) == ["f2o77_cars_1", "f2o77_cars_2", "f2o77_cars_3"] and len(p["f2o77_cars_3"]) == 2
    assert sum(len(v) for v in p.values()) == 26
    # a bundle that lost every car is not planned (left to the operator); numbers are not reused
    p = cb.plan(["Vehicle.z"], {"f2o77_cars_1": ["Vehicle.old"]}, size=2)
    assert p == {"f2o77_cars_2": ["Vehicle.z"]}
    assert cb.next_version(None) == "1.1.0" and cb.next_version("1.0.0") == "1.1.0" and cb.next_version("1.4.0") == "1.5.0"


def test_write_bundle(tmp_path):
    out = str(tmp_path / "out")
    _car(out, "vxr")
    _car(out, "a80")
    cars = cb.car_resources(out)
    r = cb.write_bundle(str(tmp_path / "f2o77_cars_1"), "f2o77_cars_1", "1.1.0", cars)
    assert r["cars"] == ["Vehicle.a80", "Vehicle.vxr"]
    d = tmp_path / "f2o77_cars_1"
    lua = (d / "open77.lua").read_text()
    assert "converted by fivem2open77" in lua and 'preload_mod "dist/f2o77_cars_1.zip"' in lua
    assert cb.parse_bundle_lua(lua) == ["Vehicle.a80", "Vehicle.vxr"]
    assert "-- Spawn with: /car Vehicle.a80, /car Vehicle.vxr" in lua
    with zipfile.ZipFile(d / "dist" / "f2o77_cars_1.zip") as z:
        names = sorted(z.namelist())
    assert names == ["archive/pc/mod/fivem2open77_a80.archive", "archive/pc/mod/fivem2open77_vxr.archive",
                     "r6/tweaks/fivem2open77/a80.yaml", "r6/tweaks/fivem2open77/vxr.yaml"]
    main = (d / "server" / "main.lua").read_text()
    cars_lua, _ = cb.parse_lua_table(main, main.index("local CARS = ") + len("local CARS = "))
    assert [c["record"] for c in cars_lua] == ["Vehicle.a80", "Vehicle.vxr"]
    assert all(c["resource"] == "f2o77_cars_1" for c in cars_lua) and cars_lua[0]["neon"]["default"] == "white"
    assert 'TriggerEvent("f2o77:car", car)' in main
    bj = json.loads((d / "bundle.json").read_text())
    assert bj["cars"][0] == {"record": "Vehicle.a80", "resource": "a80", "version": "1.0.2"}
    # two cars shipping the same file cannot share a package
    _car(str(tmp_path / "dup"), "dupa", files={"r6/tweaks/fivem2open77/same.yaml": b"a"})
    _car(str(tmp_path / "dup"), "dupb", files={"r6/tweaks/fivem2open77/same.yaml": b"b"})
    with pytest.raises(cb.BundleError):
        cb.write_bundle(str(tmp_path / "f2o77_cars_9"), "f2o77_cars_9", "1.1.0", cb.car_resources(str(tmp_path / "dup")))


def test_bundles_install_and_read_back(tmp_path):
    fk = Fake(str(tmp_path))
    out = str(tmp_path / "build" / "out")
    for n in ("a80", "urus", "vxr"):
        _car(out, n)
    cars = cb.car_resources(out)
    stage = tmp_path / "stage"
    cb.write_bundle(str(stage / "f2o77_cars_1"), "f2o77_cars_1", "1.1.0", cars[:2])
    cb.write_bundle(str(stage / "f2o77_cars_2"), "f2o77_cars_2", "1.1.0", cars[2:])
    b = deploy_mod.make_bundle(str(tmp_path / "bundle.tgz"),
                               resource_dirs={"f2o77_cars_1": str(stage / "f2o77_cars_1"),
                                              "f2o77_cars_2": str(stage / "f2o77_cars_2")})
    assert b["resources"] == ["f2o77_cars_1", "f2o77_cars_2"]
    log = fk.run("deploy", b["path"], "open77-test")
    assert "installed f2o77_cars_1 1.1.0 at resources/assets/vehicles/f2o77_cars_1" in log, log
    assert {"f2o77_cars_1", "f2o77_cars_2"} <= set(fk.load())
    # what the Studio reads back before the next deploy
    r = subprocess.run([sys.executable, "-c", cb.INVENTORY_PY, fk.root, "open77-test"], capture_output=True, text=True)
    inv = cb.parse_inventory(r.stdout)["open77-test"]
    assert inv["bundles"]["f2o77_cars_1"]["records"] == ["Vehicle.a80", "Vehicle.urus"]
    assert inv["bundles"]["f2o77_cars_1"]["cars"] == {"Vehicle.a80": "1.0.2", "Vehicle.urus": "1.0.2"}
    assert inv["versions"]["f2o77_cars_2"] == "1.1.0" and inv["standalone"] == {}
    # an older bundle (before bundle.json / "-- Records:") still says which cars it carries
    lua = os.path.join(fk.dir, "resources", "assets", "vehicles", "f2o77_cars_2", "open77.lua")
    text = open(lua).read().replace("-- Records: Vehicle.vxr\n", "")
    open(lua, "w").write(text)
    os.remove(os.path.join(os.path.dirname(lua), "bundle.json"))
    r = subprocess.run([sys.executable, "-c", cb.INVENTORY_PY, fk.root, "open77-test"], capture_output=True, text=True)
    inv = cb.parse_inventory(r.stdout)["open77-test"]
    assert inv["bundles"]["f2o77_cars_2"] == {"records": ["Vehicle.vxr"], "cars": {}}


class _Job:
    def __init__(self):
        self.lines = []

    def log(self, s):
        self.lines.append(s)

    def check(self):
        pass


class _Proj:
    def __init__(self, pid):
        import threading
        self.id, self.lock, self.data, self.saved = pid, threading.Lock(), {}, 0

    def save(self):
        self.saved += 1


def test_studio_repacks_only_changed_bundles(tmp_path, monkeypatch):
    from fivem2open77.studio.app import Studio
    out = str(tmp_path / "out")
    best = {}
    for n in ("a80", "urus", "vxr", "i8rpncs"):
        _car(out, n, version="1.0.3" if n == "urus" else "1.0.2")
    for c in cb.car_resources(out):
        best[c["record"]] = {"project": _Proj(c["resource"]), "name": c["resource"], "build": {"n": 2, "version": "1.0.x"},
                             "time": 1, "out": out, "car": c, "addons": []}
    inv = {"bundles": {"f2o77_cars_1": {"records": ["Vehicle.a80", "Vehicle.vxr"],
                                        "cars": {"Vehicle.a80": "1.0.2", "Vehicle.vxr": "1.0.2"}},
                       "f2o77_cars_2": {"records": ["Vehicle.urus"], "cars": {"Vehicle.urus": "1.0.2"}}},
           "standalone": {"i8rpncs": "Vehicle.i8rpncs"}, "versions": {"f2o77_cars_1": "1.1.0", "f2o77_cars_2": "1.1.0"}}
    sent = {}

    def fake_deploy(server, secrets, path, mode, names, log):
        import tarfile
        with tarfile.open(path) as t:
            sent["names"] = sorted({m.name.split("/")[3] for m in t.getmembers()})
            sent["lua"] = t.extractfile("resources/assets/vehicles/f2o77_cars_2/open77.lua").read().decode()
        return {"names": names, "staged": ["STAGED: x"], "live": [], "pending": []}

    monkeypatch.setattr(deploy_mod, "deploy", fake_deploy)
    job = _Job()
    me = types.SimpleNamespace(secrets=None)
    r = Studio._deploy_car_bundles(me, job, {"id": "xyberrp"}, "open77-xbuniverse", inv, best, "stage")
    # bundle 1 is unchanged; bundle 2 has the new urus; the car installed on its own is updated in place
    assert r["bundles"] == ["f2o77_cars_2"] and sent["names"] == ["f2o77_cars_2", "i8rpncs"], (r, sent, job.lines)
    assert 'version "1.2.0"' in sent["lua"]
    assert any("f2o77_cars_1 unchanged" in l for l in job.lines)
    assert best["Vehicle.urus"]["project"].data["deployments"][-1]["bundle"] == "f2o77_cars_2"
    assert "deployments" not in best["Vehicle.a80"]["project"].data


def test_studio_installs_nothing_when_the_server_cannot_be_read(tmp_path, monkeypatch):
    """A car is never installed on its own on a guess: the server could carry it in a car bundle,
    and its record would then be defined twice. A server whose bundles cannot be read refuses."""
    from fivem2open77.studio.app import Studio
    from fivem2open77.studio.projects import ProjectError
    out = str(tmp_path / "proj" / "builds" / "2" / "out")
    _car(out, "a80")
    p = _Proj("p1")
    p.kind = "car"
    p.data = {"name": "a80", "builds": [{"n": 2, "ok": True}]}
    p.latest_build = lambda: p.data["builds"][-1]
    p.build_dir = lambda n: str(tmp_path / "proj" / "builds" / str(n))
    ran = {}

    def submit(kind, title, fn, project=None, **kw):
        job = _Job()
        try:
            ran["result"] = fn(job)
        except Exception as e:  # noqa: BLE001 - the job's own failure is what is checked
            ran["error"] = e
        ran["lines"] = job.lines
        return job

    def unreachable(*a, **k):
        raise deploy_mod.DeployError("cannot reach 203.0.113.7:22: timed out")

    def must_not_install(*a, **k):
        raise AssertionError("a car was installed on a guess")

    monkeypatch.setattr(deploy_mod, "car_inventory", unreachable)
    monkeypatch.setattr(deploy_mod, "deploy", must_not_install)
    me = types.SimpleNamespace(projects=types.SimpleNamespace(get=lambda pid: p),
                               settings=types.SimpleNamespace(get=lambda: {"deploy": {"mode": "stage"}}),
                               jobs=types.SimpleNamespace(submit=submit), secrets=None,
                               _server=lambda sid: {"id": sid, "label": "XyberRP"})
    Studio.deploy(me, "p1", "xyberrp", mode="files")
    assert isinstance(ran.get("error"), ProjectError), ran
    assert "nothing was installed" in str(ran["error"]) and "timed out" in str(ran["error"])
    assert "deployments" not in p.data
