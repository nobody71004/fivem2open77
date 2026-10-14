"""Imports of any size: a big folder is read where it is, a big archive extracted once, small ones
copied, all in a job; car packs (a project per car, reading the pack once); a whole city (Liberty V)
looked at as a city and converted a district at a time."""
import json
import os
import shutil
import sys
import time
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.studio import sources  # noqa: E402

AUDIT = os.environ.get("F2O77_TEST_CARS", "/tmp/claude-0/audit/src")      # real FiveM cars (optional)
LV = os.environ.get("F2O77_TEST_CITY", "/tmp/claude-0/lv/src/libertyv")   # a part of Liberty V (optional)

VEHICLES = """<?xml version="1.0" encoding="UTF-8"?>
<CVehicleModelInfo__InitDataList><InitDatas>
  <Item><modelName>{m}</modelName><txdName>{m}</txdName><handlingId>{M}</handlingId>
        <gameName>{M}</gameName><vehicleMakeName>TEST</vehicleMakeName><vehicleClass>VC_SPORT</vehicleClass></Item>
</InitDatas></CVehicleModelInfo__InitDataList>"""
MANIFEST = "fx_version 'cerulean'\ndata_file 'VEHICLE_METADATA_FILE' 'data/vehicles.meta'\n"


def _car(root, rel, model, extra_ytd=False):
    d = os.path.join(root, *rel.split("/"), model)
    os.makedirs(os.path.join(d, "data"))
    os.makedirs(os.path.join(d, "stream"))
    open(os.path.join(d, "fxmanifest.lua"), "w").write(MANIFEST)
    open(os.path.join(d, "data", "vehicles.meta"), "w").write(VEHICLES.format(m=model, M=model.upper()))
    for f in (f"{model}.yft", f"{model}_hi.yft", f"{model}.ytd"):
        open(os.path.join(d, "stream", f), "wb").write(b"RSC7\x9a\x00\x00\x00" + os.urandom(64))
    if extra_ytd:
        open(os.path.join(d, "stream", f"{model}_mods.ytd"), "wb").write(b"RSC7\x0d\x00\x00\x00" + os.urandom(64))
    return d


def _zip(src_dir, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, _, fns in os.walk(src_dir):
            for f in fns:
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, os.path.dirname(src_dir)))
    return out


# ----------------------------------------------------------------------------- the pieces
def test_plan_and_take_in(tmp_path, monkeypatch):
    src = tmp_path / "res"
    _car(str(src), "", "alpha")
    size, files = sources.measure(str(src))
    assert files == 5 and size > 0
    assert sources.plan(str(src))[0] == "copy"
    monkeypatch.setattr(sources, "LINK_BYTES", 10)
    assert sources.plan(str(src))[0] == "link"
    assert sources.plan(str(src), uploaded=True)[0] == "copy"          # an upload is never read in place
    z = _zip(str(src), str(tmp_path / "res.zip"))
    assert sources.plan(z)[0] == "copy" and sources.plan(z, uploaded=True)[0] == "move"
    monkeypatch.setattr(sources, "EXTRACT_BYTES", 10)
    assert sources.plan(z)[0] == "extract"
    got = sources.take_in(z, str(tmp_path / "in1"), "extract")
    # res.zip holds res/alpha/...: the resource is the folder with the manifest
    assert os.path.basename(got) == "alpha" and os.path.isfile(os.path.join(got, "fxmanifest.lua"))
    got = sources.take_in(str(src), str(tmp_path / "in2"), "copy")
    assert sources.measure(got) == (size, files)
    with pytest.raises(sources.SourceError):
        sources.check_source(str(tmp_path / "nothing"))
    (tmp_path / "x.txt").write_text("x")
    with pytest.raises(sources.SourceError):
        sources.check_source(str(tmp_path / "x.txt"))


def test_extract_refuses_members_outside(tmp_path):
    z = str(tmp_path / "evil.zip")
    with zipfile.ZipFile(z, "w") as zz:
        zz.writestr("ok/fxmanifest.lua", "fx_version 'cerulean'")
        zz.writestr("../escape.txt", "no")
        zz.writestr("/abs.txt", "no")
    lines = []
    sources.extract(z, str(tmp_path / "out"), log=lines.append)
    assert os.path.isfile(tmp_path / "out" / "ok" / "fxmanifest.lua")
    assert not os.path.exists(tmp_path / "escape.txt") and len([x for x in lines if "skipped" in x]) == 2


def test_not_enough_space(tmp_path, monkeypatch):
    class _U:
        free = 2 << 30
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda p: _U())
    with pytest.raises(sources.SourceError, match="not enough free space"):
        sources.need_space(str(tmp_path), 3 << 30, "extracting city.zip")
    sources.need_space(str(tmp_path), 100 << 20, "a small one")


def test_library_link_extract_delete_prune(tmp_path):
    lib = sources.Library(str(tmp_path / "library"))
    src = tmp_path / "pack"
    _car(str(src), "[CARS]/[A]", "alpha")
    _car(str(src), "[CARS]/[B]", "beta")
    e = lib.create("cars", "pack", str(src))
    with pytest.raises(sources.SourceError, match="still being imported"):
        lib.source(e["id"])
    lib.fill(e, str(src), "link")
    e["state"] = "ready"
    lib.save(e)
    assert lib.source(e["id"]) == str(src) and lib.source(e["id"], "[CARS]/[A]/alpha").endswith("alpha")
    lib.delete(e["id"])
    assert os.path.isdir(src), "a linked folder is the user's own: never deleted"
    z = _zip(str(src), str(tmp_path / "pack.zip"))
    e2 = lib.create("cars", "pack", z)
    lib.fill(e2, z, "copy")                         # a car pack archive is always held unpacked
    # the resource is where it stops being one folder in another: pack/[CARS] (it holds [A] and [B])
    assert e2["how"] == "extract" and e2["root"] == "pack/[CARS]"
    e2["state"] = "ready"
    lib.save(e2)
    assert os.path.isfile(os.path.join(lib.source(e2["id"], "[A]/alpha"), "fxmanifest.lua"))
    e3 = lib.create("map", "old", "x")
    e3["created"] = time.time() - 3 * 86400
    lib.save(e3)
    gone = lib.prune(in_use={e2["id"]})
    assert gone == [e3["id"]] and [x["id"] for x in lib.list()] == [e2["id"]]
    # a pack of one car: its resource folder is the car's own
    one = tmp_path / "one"
    _car(str(one), "[CARS]/[A]", "gamma")
    z1 = _zip(str(one), str(tmp_path / "one.zip"))
    e4 = lib.fill(lib.create("cars", "one", z1), z1, "move")
    assert e4["root"] == "one/[CARS]/[A]/gamma"


def test_scan_cars_finds_each_cars_own_folder(tmp_path):
    root = str(tmp_path / "FiveM-Civ-Car-Pack")
    _car(root, "[PLOKS_CARS]/[AUDI]", "r8ppi", extra_ytd=True)
    _car(root, "[PLOKS_CARS]/[MERCEDES]", "e400")
    os.makedirs(os.path.join(root, "ploks_carmenu"))
    open(os.path.join(root, "ploks_carmenu", "fxmanifest.lua"), "w").write("fx_version 'cerulean'")
    cars = sources.scan_cars(root)
    assert [(c["model"], c["sub"]) for c in cars] == [("e400", "[PLOKS_CARS]/[MERCEDES]/e400"),
                                                      ("r8ppi", "[PLOKS_CARS]/[AUDI]/r8ppi")]
    assert all(c["class"] == "sport" for c in cars)
    # a car's other texture dictionaries are its own resource's, kept aside (read by name on demand)
    from fivem2open77.gta.resource import scan_resource
    res = scan_resource(root)
    try:
        by = {v.model: v for v in res.vehicles}
        assert [os.path.basename(p) for p in by["r8ppi"].ytd_extra] == ["r8ppi_mods.ytd"]
        assert by["e400"].ytd_extra == [] and [os.path.basename(p) for p in by["e400"].ytds] == ["e400.ytd"]
    finally:
        res.cleanup()


# ----------------------------------------------------------------------------- the Studio
def _studio(tmp_path):
    from fivem2open77.studio.app import Studio
    return Studio(str(tmp_path / "ws"))


def test_import_job_links_a_big_folder_and_extracts_a_big_archive(tmp_path, monkeypatch):
    st = _studio(tmp_path)
    try:
        src = tmp_path / "bigcar"
        _car(str(src), "", "alpha")
        monkeypatch.setattr(sources, "LINK_BYTES", 10)
        p, job = st.create_project(path=str(src), kind="car")
        assert p.importing
        st.jobs.wait(job, 60)
        assert job.state == "done", job.error
        assert job.result["how"] == "link" and job.result["next_job"]
        p = st.projects.get(p.id)
        assert p.source == str(src) and not os.listdir(p.path("input"))
        assert p.data["source"]["hash"]
        st.jobs.wait(st.jobs.get(job.result["next_job"]), 60)       # the inspection (fails: fake car files)
        # a big archive: extracted once into the library
        monkeypatch.setattr(sources, "EXTRACT_BYTES", 10)
        z = _zip(str(src), str(tmp_path / "bigcar.zip"))
        q, job = st.create_project(path=z, kind="car")
        st.jobs.wait(job, 60)
        assert job.state == "done" and job.result["how"] == "extract", job.error
        q = st.projects.get(q.id)
        assert os.path.isfile(os.path.join(q.source, "fxmanifest.lua"))
        assert os.path.commonpath([q.source, st.library.root]) == st.library.root
        lib = q.data["source"]["lib"]
        st.jobs.wait(st.jobs.get(job.result["next_job"]), 60)
        st.delete_project(q.id)
        assert not os.path.exists(st.library.path(lib)), "nobody reads it any more"
        # an upload is moved in and its upload folder removed
        monkeypatch.setattr(sources, "EXTRACT_BYTES", 1 << 40)
        with open(z, "rb") as f:
            up = st.save_upload("bigcar.zip", f, os.path.getsize(z))
        r, job = st.create_project(upload_id=up["upload_id"], kind="car")
        st.jobs.wait(job, 60)
        assert job.result["how"] == "move" and not os.path.exists(os.path.dirname(up["path"]))
        assert os.path.isfile(st.projects.get(r.id).source)
        st.jobs.wait(st.jobs.get(job.result["next_job"]), 60)
        # a failed import says so, and the project says why
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"PK\x03\x04 not a zip at all")
        monkeypatch.setattr(sources, "EXTRACT_BYTES", 10)
        b, job = st.create_project(path=str(bad), kind="car")
        st.jobs.wait(job, 60)
        assert job.state == "failed"
        b = st.projects.get(b.id)
        assert b.data["source"]["state"] == "failed" and b.summary()["import"] == "failed"
    finally:
        st.close()


def test_import_interrupted_by_a_restart_is_marked_failed(tmp_path):
    st = _studio(tmp_path)
    p = st.projects.create_pending("map", "/somewhere/city")
    st.close()
    st2 = _studio(tmp_path)
    try:
        q = st2.projects.get(p.id)
        assert q.data["source"]["state"] == "failed" and "closed" in q.data["source"]["error"]
    finally:
        st2.close()


@pytest.mark.skipif(not os.path.isdir(os.path.join(AUDIT, "e400")), reason="no real test cars")
def test_car_pack_flow(tmp_path):
    root = tmp_path / "TestPack"
    shutil.copytree(os.path.join(AUDIT, "e400"), root / "[PLOKS_CARS]" / "[MERCEDES]" / "e400")
    shutil.copytree(os.path.join(AUDIT, "ccf250", "ccf250"), root / "[PLOKS_CARS]" / "[FORD]" / "ccf250")
    os.makedirs(root / "ploks_carmenu")
    (root / "ploks_carmenu" / "fxmanifest.lua").write_text("fx_version 'cerulean'")
    z = _zip(str(root), str(tmp_path / "TestPack.zip"))
    st = _studio(tmp_path)
    try:
        e, job = st.create_pack(path=z)
        st.jobs.wait(job, 120)
        assert job.state == "done", job.error
        pack = job.result
        assert pack["name"] == "TestPack" and [c["model"] for c in pack["cars"]] == ["ccf250", "e400"]
        made, j2 = st.import_pack_cars(e["id"], ["e400"])
        st.jobs.wait(j2, 300)
        assert j2.state == "done" and j2.result["read"] == 1, j2.error
        car = st.projects.get(made[0].id)
        assert car.raw_profile()["model"] == "e400" and car.data["pack"]["id"] == e["id"]
        assert car.source.endswith(os.path.join("[MERCEDES]", "e400")) and os.path.isfile(car.path("cache", "thumb.png"))
        assert (car.data.get("car") or {}).get("record") == "Vehicle.e400"
        # the cars already imported are left alone; the rest come in on their own
        made2, j3 = st.import_pack_cars(e["id"], "all")
        assert [q.raw_profile()["model"] for q in made2] == ["ccf250"]
        st.jobs.wait(j3, 300)
        assert {c["model"]: len(c["projects"]) for c in st.pack(e["id"])["cars"]} == {"ccf250": 1, "e400": 1}
        with pytest.raises(ValueError, match="read this pack"):
            st.delete_pack(e["id"])
        st.delete_project(made[0].id)
        assert os.path.isdir(st.library.path(e["id"])), "a pack stays for its other cars"
        st.delete_pack(e["id"], with_projects=True)
        assert not os.path.exists(st.library.path(e["id"])) and not st.projects.list()
    finally:
        st.close()


@pytest.mark.skipif(not os.path.isdir(LV), reason="no Liberty V test data")
def test_whole_city_overview_and_districts(tmp_path, monkeypatch):
    import fivem2open77.mlo.pipeline as mp
    monkeypatch.setattr(mp, "HUGE_MAP", 2000)           # this part of Liberty V counts as a city here
    monkeypatch.setattr(sources, "LINK_BYTES", 10 << 20)
    st = _studio(tmp_path)
    try:
        p, job = st.create_project(path=LV, kind="map")
        st.jobs.wait(job, 120)
        assert job.state == "done" and job.result["how"] == "link", job.error
        look = st.jobs.get(job.result["next_job"])
        st.jobs.wait(look, 600)
        assert look.state == "done" and look.result["city"], look.error
        state = st.map_state(p.id)
        L = state["look"]
        assert L["city"] and L["districts"] and L["overview"]["grid"]["cells"] and L["overview"]["extent"]
        assert state["project"]["map"]["city"] and not p.raw_map_options().get("name")
        with pytest.raises(Exception, match="whole city"):
            st.map_build(p.id)
        d = L["districts"][0]
        area = [d["center"][0], d["center"][1], 250]
        # this project becomes the first district: its options change and a look starts
        r, j = st.map_district(p.id, area, district=d["name"])
        j.cancel()                                     # its look takes minutes: not needed here
        st.jobs.wait(j, 60)
        assert r.id == p.id and p.raw_map_options()["area"] == area and p.raw_map_options()["name"] == f"libertyv_{d['name']}"
        # another district is a project of its own, reading the same files, with an id of its own
        from fivem2open77.studio.maps import MapOptionsError
        with pytest.raises(MapOptionsError, match="already the resource id"):
            st.map_district(p.id, area, district=d["name"], new_project=True)
        q, j2 = st.map_district(p.id, [area[0] + 300, area[1], 200], name="libertyv_east", new_project=True)
        j2.cancel()
        st.jobs.wait(j2, 60)
        q = st.projects.get(q.id)
        assert q.id != p.id and q.data["source"]["lib"] == st.projects.get(p.id).data["source"]["lib"]
        assert q.raw_map_options()["name"] == "libertyv_east"
        city = st.map_city(q.id)
        assert city["districts"] == L["districts"], "the district keeps the city overview"
        assert sorted(t["name"] for t in city["taken"]) == ["libertyv_east", f"libertyv_{d['name']}"]
        lib = q.data["source"]["lib"]
        st.delete_project(q.id)
        assert os.path.isdir(st.library.path(lib))
        st.delete_project(p.id)
        assert not os.path.exists(st.library.path(lib)) and os.path.isdir(LV)
    finally:
        st.close()


def test_district_names():
    from fivem2open77.studio.maps import district_display, district_name
    assert district_name("libertyv", "bronx_e") == "libertyv_bronx_e"
    assert district_name("libertyv", "libertyv_brook") == "libertyv_brook"
    assert district_name("Liberty V", "12 Dukes") == "liberty_v_12_dukes"
    assert district_display("Liberty V", "bronx_e") == "Liberty V Bronx E"
    from fivem2open77.mlo.scene import district_key
    assert [district_key(n) for n in ("bronx_e_stream3", "manhatsw_strbig0_straight_ms", "manhat02_lod", "dukes_bay_12")] == \
        ["bronx_e", "manhatsw", "manhat02", "dukes_bay"]
