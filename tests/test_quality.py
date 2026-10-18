"""Four-door donors, parts the donor has no bone for, wheels on their bones, mod-kit parts the car
needs or not, and the quality checks on a converted car."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77 import pipeline, qc  # noqa: E402
from fivem2open77.cp77 import donor as donor_mod  # noqa: E402
from fivem2open77.cp77.donor import DONORS, choose_donor, corners_needed  # noqa: E402
from fivem2open77.cp77.tweak import build_tweak, camera_class  # noqa: E402
from fivem2open77.model import kit  # noqa: E402
from fivem2open77.model.ir import Part, SubMesh  # noqa: E402

from test_neon import _box, _car  # noqa: E402


def _panel(name, slot, x0, x1, y0, y1, z0, z1, kind="door", frame=(0.0, 0.0, 0.44)):
    p, i = _box(x0, x1, y0, y1, z0, z1)
    p = p - np.asarray(frame, np.float32)
    n = np.zeros_like(p)
    n[:, 0] = 1
    sm = SubMesh(material="m", positions=p, normals=n, tangents=np.tile([0, 1, 0, 1], (len(p), 1)).astype(np.float32),
                 uv0=np.zeros((len(p), 2), np.float32), uv1=None, colors=None, indices=i, lod=1)
    return Part(name=name, mesh_name=name, slot=slot, parent=None, kind=kind, submeshes=[sm]), (np.asarray(frame), np.eye(3))


def _doors(v, rear=True, rear_flap=False):
    for name, slot, x0, x1, y0, y1 in (("door_fl_a", "door_front_left_a", -0.95, -0.85, 0.0, 1.1),
                                       ("door_fr_a", "door_front_right_a", 0.85, 0.95, 0.0, 1.1)):
        v.parts[name], v.part_frames[name] = _panel(name, slot, x0, x1, y0, y1, 0.0, 0.8)
    if rear:
        for name, slot, x0, x1 in (("door_bl_a", "door_back_left_a", -0.95, -0.85),
                                   ("door_br_a", "door_back_right_a", 0.85, 0.95)):
            v.parts[name], v.part_frames[name] = _panel(name, slot, x0, x1, -1.0, -0.05, 0.0, 0.8)
    if rear_flap:      # a two-door supercar's engine-cover flaps hung on the rear door bones
        for name, slot, x0, x1 in (("door_bl_a", "door_back_left_a", -0.7, -0.4),
                                   ("door_br_a", "door_back_right_a", 0.4, 0.7)):
            v.parts[name], v.part_frames[name] = _panel(name, slot, x0, x1, -1.8, -0.9, 0.7, 0.85)
    return v


def test_cars_with_rear_doors_get_the_four_door_donor():
    assert choose_donor(_doors(_car(), rear=True)) == "archer_hella"
    assert choose_donor(_doors(_car(), rear=False)) == "porsche_911"
    flaps = _doors(_car(), rear=False, rear_flap=True)
    assert corners_needed(flaps) == {"fl", "fr"}, "engine-cover flaps on rear door bones are no doors"
    assert choose_donor(flaps) == "porsche_911"
    # a profile asking for the two-door donor on a four-door car would leave the rear doors behind
    assert choose_donor(_doors(_car()), "porsche_911") == "archer_hella"
    assert choose_donor(_doors(_car(), rear=False), "archer_hella") == "archer_hella"
    hella = DONORS["archer_hella"]
    assert hella["doors"] == 4 and set(hella["occupant_seats"]) == set(donor_mod.ALL_SEATS)
    assert {"door_back_left_a", "door_back_right_a"} <= set(hella["open_axes"])
    # every donor shares the proven templates and physics
    for name in ("app", "mesh_template", "paint_setup", "phys"):
        assert hella[name] == DONORS["porsche_911"][name]
    assert hella["tweak"]["drive_model"] == DONORS["porsche_911"]["tweak"]["drive_model"]


def _rig_doc(bones):
    return {"Data": {"RootChunk": {"boneNames": [{"$type": "CName", "$storage": "string", "$value": b} for b in bones]}}}


def _ent_doc(slots):
    comp = {"$type": "entSlotComponent", "name": {"$type": "CName", "$storage": "string", "$value": "vehicle_slots"},
            "slots": [{"$type": "entSlot", "slotName": {"$type": "CName", "$storage": "string", "$value": s},
                       "boneName": {"$type": "CName", "$storage": "string", "$value": b}} for s, b in slots.items()]}
    return {"Data": {"RootChunk": {"components": [{"HandleId": "0", "Data": comp}]}}}


def test_parts_without_a_donor_bone_are_fixed_to_the_body():
    v = _doors(_car(), rear=False)
    # a two-door's rear quarter window: GTA's window_lr bone, no such bone in the 911's rig
    part, _ = _panel("window_bl_a", "window_back_left_a", -0.95, -0.9, -1.2, -0.6, 0.5, 0.8, kind="window",
                     frame=(-0.9, -0.9, 1.1))
    v.parts["window_bl_a"] = part
    v.part_frames["window_bl_a"] = (np.array([-0.9, -0.9, 1.1]), np.eye(3))
    before = v.parts["window_bl_a"].submeshes[0].positions.astype(np.float64) + [-0.9, -0.9, 1.1]
    rig = _rig_doc(["Root", "Base", "door_front_left_a", "door_front_right_a", "window_front_left_a"])
    ent = _ent_doc({"door_front_left_a": "door_front_left_a", "door_front_right_a": "door_front_right_a",
                    "window_back_left_a": "window_back_left_a"})
    b = pipeline.bind_parts(v, rig, ent)
    assert b["detached"] == ["window_bl_a"]
    w = v.parts["window_bl_a"]
    assert w.slot is None and w.parent == "body_01"
    t, r = v.part_frames["window_bl_a"]
    after = w.submeshes[0].positions.astype(np.float64) @ np.asarray(r).T + np.asarray(t)
    assert np.allclose(before, after, atol=1e-5), "the window stays where it was"
    assert v.parts["door_fl_a"].slot == "door_front_left_a"
    assert any("fixed to the body" in x for x in v.warnings)


def _tweak(v, donor="porsche_911", **kw):
    return build_tweak(v, DONORS[donor], record="Vehicle.t", ent_path=r"x\t.ent", appearance="t", name_key="k", **kw)


def test_wheels_sit_on_their_bones_upright():
    text, _ = _tweak(_car())
    assert text.count("wheelOffset: 0.0") == 2, "the GTA wheel centres are the wheel bones already"
    for f in ("leftFrontCamber: 0", "rightFrontCamber: 0", "leftBackCamber: 0", "rightBackCamber: 0"):
        assert f in text
    assert "leftBackCamberOffset: { x: 0.0, y: 0.0, z: 0.0 }" in text
    text2, _ = _tweak(_car(), handling={"drive": "auto", "gears": None, "mass": None, "steer": None,
                                        "wheel_offset": {"front": 0.02, "back": -0.01}, "multipliers": {}})
    assert "wheelOffset: 0.02" in text2 and "wheelOffset: -0.01" in text2


def test_record_parts_come_from_the_donor_and_the_camera_from_the_size():
    v = _doors(_car())
    text, _ = _tweak(v, "archer_hella")
    assert "$base: Vehicle.v_standard2_archer_hella\n" in text
    assert "$base: Vehicle.VehicleDestructionParamsHella" in text
    assert "$base: Vehicle.VehicleDriveModelData_911Turbo" in text, "the physics the handling maps onto"
    low = _car(top=0.7)
    low.bbox = ((-0.95, -2.2, -0.2), (0.95, 2.2, 0.7))
    assert camera_class(low) == "sport"
    assert "tppCameraPresets" not in _tweak(low)[0], "a low car keeps the 911's camera"
    suv = _car(top=1.4)
    suv.bbox = ((-0.95, -2.2, -0.2), (0.95, 2.2, 1.4))
    assert camera_class(suv) == "suv"
    t2, _ = _tweak(suv)
    assert "- Camera.VehicleTPP_v_standard25_mahir_supron_CameraPreset_High_Close" in t2
    truck = _car(top=2.0)
    truck.bbox = ((-1.0, -3.2, -0.2), (1.0, 3.2, 2.0))
    assert camera_class(truck) == "truck"


def _keys(*boxes):
    pts = []
    for x0, x1, y0, y1, z0, z1 in boxes:
        ax = [(np.arange(round(a / 0.05), round(b / 0.05)) + 0.5) * 0.05 for a, b in ((x0, x1), (y0, y1), (z0, z1))]
        pts.append(np.stack(np.meshgrid(*ax, indexing="ij"), -1).reshape(-1, 3))
    return kit.voxel_keys(np.concatenate(pts))


def _shell(x0, x1, y0, y1, z0, z1, t=0.1):
    """A hollow box's walls (floor, roof, sides, ends) t thick."""
    return [(x0, x1, y0, y1, z0, z0 + t), (x0, x1, y0, y1, z1 - t, z1), (x0, x0 + t, y0, y1, z0, z1),
            (x1 - t, x1, y0, y1, z0, z1), (x0, x1, y0, y0 + t, z0, z1), (x0, x1, y1 - t, y1, z0, z1)]


def test_mod_kit_parts_the_car_needs_and_accessories_it_does_not():
    car = _shell(-1.0, 1.0, -2.2, 2.2, 0.0, 1.4)
    base = _keys(*car)
    roof_box = {"id": ("mod", "roof_box"), "keys": _keys((-0.5, 0.5, -1.0, 1.0, 1.45, 1.9)), "verts": 900,
                "replaced": 0, "slots": {"VMT_ROOF"}}
    out = kit.plan(base, [roof_box])
    assert out[("mod", "roof_box")][0] is False and "accessory" in out[("mod", "roof_box")][1]
    # a truck shipped as a frame, with its cab and its bed as two mod-kit parts
    frame = _keys((-0.9, 0.9, -3.0, 3.0, 0.0, 0.25))
    cab = {"id": ("mod", "cab"), "keys": _keys(*_shell(-1.0, 1.0, 0.5, 2.8, 0.25, 1.6)), "verts": 5000,
           "replaced": 0, "slots": {"VMT_ROOF"}}
    bed = {"id": ("mod", "bed"), "keys": _keys(*_shell(-1.0, 1.0, -3.0, 0.4, 0.25, 0.9)), "verts": 3000,
           "replaced": 0, "slots": {"VMT_TRUNK"}}
    out = kit.plan(frame, [cab, bed])
    assert out[("mod", "cab")][0] and out[("mod", "bed")][0], out
    # a small optional add-on (a spoiler) stays off: GTA fits no mod by default
    spoiler = {"id": ("mod", "wing"), "keys": _keys((-0.8, 0.8, -2.2, -1.9, 1.1, 1.35)), "verts": 400,
               "replaced": 0, "slots": {"VMT_SPOILER"}}
    out = kit.plan(base, [spoiler])
    assert out[("mod", "wing")][0] is False
    # something hidden inside the body (a roll cage in the cabin) is no missing piece
    cage = _keys((-0.5, 0.5, -1.0, 1.0, 0.3, 1.0))
    assert kit.seen_share(base, cage) < 0.05
    assert kit.seen_share(frame, _keys((-1.0, 1.0, 0.5, 2.8, 1.5, 1.6))) > 0.9, "a missing roof is in plain view"


def _facts(**over):
    v = _doors(_car())
    bindings = {"bones": {n: p.slot for n, p in v.parts.items() if p.slot},
                "rig": ["Base", "door_front_left_a", "door_front_right_a", "door_back_left_a", "door_back_right_a"],
                "detached": []}
    names = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}
    frames = {}
    for axle in v.axles.values():
        for c, p in axle.positions.items():
            m = np.eye(4)
            m[:3, 3] = p
            frames[f"wheel_{names[c]}"] = m
    text, hp = _tweak(v, "archer_hella")
    f = qc.car_facts(v, "archer_hella", DONORS["archer_hella"], bindings, frames, text, hp, camera="sedan")
    f.update(over)
    return f


def test_quality_checks_pass_a_good_car_and_catch_the_old_faults():
    good = qc.check_car({"qc": _facts()})
    assert good["ok"], [c for c in good["checks"] if not c["ok"]]
    stale = qc.check_car({"version": "1.4.2"})
    assert not stale["ok"] and stale["stale"]
    f = _facts()
    f["tweak"]["wheelOffset"] = [0.1, 0.08]
    bad = qc.check_car({"qc": f})
    assert not bad["ok"] and any(c["id"] == "wheels.offset" and not c["ok"] for c in bad["checks"])
    f = _facts()
    f["parts"]["door_bl_a"]["bone_in_rig"] = False
    bad = qc.check_car({"qc": f})
    assert any(c["id"] == "parts.attached" and not c["ok"] for c in bad["checks"])
    f = _facts()
    f["donor"]["corners"] = ["fl", "fr"]
    bad = qc.check_car({"qc": f})
    assert any(c["id"] == "doors.donor" and not c["ok"] for c in bad["checks"])
    f = _facts()
    f["version"] = qc.QC_VERSION - 1
    assert qc.check_car({"qc": f})["stale"]


# ----------------------------------------------------------------------------- the Studio's sweep
def _car_project(ws, pid, report, name="boxcar"):
    import json as _json
    d = os.path.join(ws, "projects", pid)
    out = os.path.join(d, "builds", "1", "out", name)
    os.makedirs(os.path.join(out, "dist"), exist_ok=True)
    with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
        _json.dump({"id": pid, "kind": "car", "name": name, "created": 1.0, "updated": 1.0,
                    "source": {"file": "car.zip", "original": "car.zip", "hash": "h1"},
                    "builds": [{"n": 1, "ok": True, "version": "1.0.1", "name": name, "record": f"Vehicle.{name}"}],
                    "build_counter": 1}, f)
    with open(os.path.join(out, "conversion-report.json"), "w", encoding="utf-8") as f:
        _json.dump(report, f)
    with open(os.path.join(out, "open77.lua"), "w", encoding="utf-8") as f:
        f.write(f'resource "{name}"\nversion "1.0.1"\n')
    return d


def test_quality_sweep_finds_stale_builds_and_rebuilds_them_once():
    import tempfile
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-qc-")
    st = Studio(ws)
    try:
        _car_project(ws, "aaaaaaaaaaaa", {"version": "1.4.2", "record": "Vehicle.oldcar"}, name="oldcar")
        good = _facts()
        _car_project(ws, "bbbbbbbbbbbb", {"version": pipeline.VERSION, "record": "Vehicle.newcar", "qc": good}, name="newcar")
        rep = st.qc.sweep(rebuild=False)
        rows = rep["projects"]
        assert rows["aaaaaaaaaaaa"]["status"] == "stale", rows["aaaaaaaaaaaa"]
        # the new car fails only the package checks of this stub build, never the car's own checks
        assert not any(e.startswith("car.") for e in rows["bbbbbbbbbbbb"]["errors"]), rows["bbbbbbbbbbbb"]
        assert os.path.isfile(os.path.join(ws, "qc", "report.json")) and os.path.isfile(os.path.join(ws, "qc", "report.html"))
        built = []

        def fake_build(pid, **kw):
            built.append(pid)
            return st.jobs.submit("build", "build", lambda job: {"ok": False}, project=pid)

        st.build = fake_build
        st.qc.sweep(rebuild=True, only=["aaaaaaaaaaaa"])
        assert built == ["aaaaaaaaaaaa"]
        st.qc.sweep(rebuild=True, only=["aaaaaaaaaaaa"])
        assert built == ["aaaaaaaaaaaa"], "a rebuild that did not help is not repeated until something changes"
        v = st.qc.view()
        assert v["counts"]["stale"] >= 1 and v["projects"][0]["status"] in ("stale", "fail")
    finally:
        st.close()
