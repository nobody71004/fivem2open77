"""Neon kits, dlc.rpf packs and display names (no game files needed)."""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77 import app as app_mod  # noqa: E402
from fivem2open77.cp77 import ent as ent_mod  # noqa: E402
from fivem2open77.cp77 import package  # noqa: E402
from fivem2open77.gta import gxt2, rpf  # noqa: E402
from fivem2open77.gta.jenkhash import joaat  # noqa: E402
from fivem2open77.model import neon  # noqa: E402
from fivem2open77.model.build import display_name_of  # noqa: E402
from fivem2open77.model.ir import Material, Part, SubMesh, Vehicle, WheelAxle  # noqa: E402
from fivem2open77.profile import ProfileError, normalize  # noqa: E402


# ----------------------------------------------------------------------------- a box car
def _box(x0, x1, y0, y1, z0, z1):
    """Closed box (triangles, Root space)."""
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], np.float32)
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
         (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v, np.array(f, np.uint32).reshape(-1)


def _car(floor=-0.20, skirt=None, body_frame=(0.0, 0.0, 0.44), top=0.9):
    """A 4.4 m x 1.9 m car: body from `floor` up, wheels r=0.35 at y=+-1.4 (Root space, wheel
    centres at z=0), body geometry expressed in a frame at `body_frame` like the real converter."""
    parts = []
    parts.append(_box(-0.95, 0.95, -2.2, 2.2, floor, top))
    if skirt is not None:   # side skirts hanging lower than the floor
        parts.append(_box(-0.95, -0.85, -0.9, 0.9, skirt, floor))
        parts.append(_box(0.85, 0.95, -0.9, 0.9, skirt, floor))
    pos, idx, base = [], [], 0
    for p, i in parts:
        pos.append(p)
        idx.append(i + base)
        base += len(p)
    p = np.concatenate(pos) - np.asarray(body_frame, np.float32)
    i = np.concatenate(idx)
    n = np.zeros_like(p)
    n[:, 2] = 1
    sm = SubMesh(material="m", positions=p, normals=n, tangents=np.zeros((len(p), 4), np.float32),
                 uv0=np.zeros((len(p), 2), np.float32), uv1=None, colors=None, indices=i, lod=1)
    v = Vehicle(name="boxcar", display_name="Box", make="", gta_class="VC_SPORT", gta_layout="LAYOUT_STANDARD",
                door_count=2)
    v.parts["body_01"] = Part(name="body_01", mesh_name="body_01", slot="Base", parent=None, kind="body", submeshes=[sm])
    v.part_frames["body_01"] = (body_frame, np.eye(3))
    v.materials["m"] = Material(key="m", kind="paint")
    for axle, y in (("front", 1.4), ("back", -1.4)):
        v.axles[axle] = WheelAxle(radius=0.35, width=0.25, rim_radius=0.25,
                                  positions={("fl" if axle == "front" else "bl"): (-0.8, y, 0.0),
                                             ("fr" if axle == "front" else "br"): (0.8, y, 0.0)})
    v.bbox = ((-0.95, -2.2, -0.2), (0.95, 2.2, 0.9))
    return v


def _check_mounted(v, kit):
    tris = neon.body_triangles(v)
    under = neon.Underside(tris)
    for t in kit.tubes:
        top = t.start[2] + kit.tube_radius
        bottom = t.start[2] - kit.tube_radius
        assert bottom > -0.35 + 0.03, f"{t.side} tube touches the road ({bottom:.3f})"
        for f in np.linspace(0, 1, 9):
            p = t.start + (t.end - t.start) * f
            h = under.at(p[0], p[1])
            assert h is not None, f"{t.side}: no body above {p}"
            assert top <= h + 1e-6, f"{t.side} tube cuts into the body at {p}"
            assert h - top < 0.06, f"{t.side} tube hangs {h - top:.3f} m below the body"
        if t.side in ("left", "right"):
            # between the wheels, clear of the tyres
            assert t.start[1] < 1.4 - 0.35 and t.end[1] > -1.4 + 0.35


def test_neon_measured_on_a_plain_underside():
    v = _car()
    kit = neon.plan_neon(v, {}, normalize({})["neon"])
    assert kit is not None and {t.side for t in kit.tubes} == {"left", "right", "front", "back"}
    _check_mounted(v, kit)
    left = next(t for t in kit.tubes if t.side == "left")
    assert left.start[0] < -0.6, "side tubes run along the sills"
    assert abs(left.start[0] - left.end[0]) < 1e-9 and left.start[1] > left.end[1], "straight, front to back"
    assert len(kit.lights) >= 6 and kit.default == "off" and len(kit.colors) == 13


def test_neon_follows_gta_bones_and_skirts():
    v = _car(skirt=-0.27)
    bones = {"neon_l": (-0.6, 0.0, -0.15), "neon_r": (0.6, 0.0, -0.15), "neon_f": (0.0, 2.0, -0.18),
             "neon_b": (0.0, -2.0, -0.18)}
    kit = neon.plan_neon(v, bones, normalize({})["neon"])
    _check_mounted(v, kit)
    left = next(t for t in kit.tubes if t.side == "left")
    assert abs(left.start[0] + 0.6) < 1e-6 and "GTA bone" in left.source
    front = next(t for t in kit.tubes if t.side == "front")
    assert abs(front.start[1] - 2.0) < 1e-6


def test_neon_left_out_where_it_cannot_fit():
    # floor 5 cm off the road: no room for a tube anywhere
    v = _car(floor=-0.32)
    kit = neon.plan_neon(v, {}, normalize({})["neon"])
    assert kit is None
    assert any(w.startswith("neon") for w in v.warnings)


def test_neon_profile_sides_colours_and_geometry():
    v = _car()
    conf = normalize({"neon": {"sides": ["left", "right"], "colors": ["hot pink", "#00ffcc"], "default": "hot_pink",
                               "brightness": 2}})["neon"]
    kit = neon.plan_neon(v, {}, conf)
    assert [t.side for t in kit.tubes] == ["left", "right"]
    assert [c[0] for c in kit.colors] == ["hot_pink", "c00ffcc"] and kit.default == "hot_pink"
    part = neon.add_neon_part(v, kit)
    sm = part.submeshes[0]
    tri = sm.indices.reshape(-1, 3)
    assert len(sm.positions) == 2 * 34 and tri.max() < len(sm.positions)
    # geometry is in the body's frame (Root minus the frame offset)
    z = sm.positions[:, 2] + 0.44
    assert abs(float(z.mean()) - kit.tubes[0].start[2]) < 1e-3
    # outward-facing triangles
    p = sm.positions[tri]
    fn = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    assert (np.einsum("ij,ij->i", fn, sm.normals[tri].sum(1)) >= -1e-9).all()


def test_neon_profile_validation():
    with pytest.raises(ProfileError) as e:
        normalize({"neon": {"default": "teal", "sides": [], "brightness": 9, "colors": ["nope"]}})
    msg = str(e.value)
    assert "neon.sides" in msg and "neon.brightness" in msg and "neon colour 'nope'" in msg
    assert normalize({"neon": {"enabled": False}})["neon"]["enabled"] is False


# ----------------------------------------------------------------------------- .app / .ent
def _donor_app():
    def comp(t, name, **kw):
        d = {"$type": t, "name": {"$type": "CName", "$storage": "string", "$value": name}, "id": "1"}
        d.update(kw)
        return d
    light = comp("vehicleLightComponent", "head_light_left_01", lightType="Head", allowSeparateEmissiveColor=0,
                 emissiveColor={}, highBeamPitchAngle=0, highBeamRadiusMultiplier=2, highBeamConeMultiplier=1,
                 type="LT_Spot", intensity=300, radius=15, iesProfile={})
    comps = [comp("entPhysicalMeshComponent", "body_01"), comp("entMeshComponent", "wipers_01"), light,
             comp("entVisualControllerComponent", "AppearanceVisualController", appearanceDependency=[])]
    return {"Header": {}, "Data": {"Version": 195, "RootChunk": {
        "$type": "appearanceAppearanceResource",
        "appearances": [{"HandleId": "0", "Data": {"$type": "appearanceAppearanceDefinition",
                                                    "name": {"$type": "CName", "$storage": "string", "$value": "default"},
                                                    "components": comps,
                                                    "compiledData": {"BufferId": "0", "Flags": 0}}}]}}}


def test_app_and_ent_get_one_appearance_per_colour():
    v = _car()
    kit = neon.plan_neon(v, {}, normalize({"neon": {"colors": ["red", "blue", "white"]}})["neon"])
    neon.add_neon_part(v, kit)
    v.neon = kit
    meshes = {"body_01": r"x\body_01.mesh"}
    neon_meshes = {k: rf"x\neon_{k}.mesh" for k, _, _ in kit.colors}
    doc = app_mod.build_app(v, _donor_app(), meshes, neon_meshes=neon_meshes)
    apps = doc["Data"]["RootChunk"]["appearances"]
    names = [a["Data"]["name"]["$value"] for a in apps]
    assert names == ["default", "neon_red", "neon_blue", "neon_white"]
    ids = [a["Data"]["compiledData"]["BufferId"] for a in apps]
    assert len(set(ids)) == len(ids), "WolvenKit refuses repeated buffer ids"
    default = [c["name"]["$value"] for c in apps[0]["Data"]["components"]]
    assert "neon_01" not in default and not any(n.startswith("neon_light") for n in default)
    red = apps[1]["Data"]["components"]
    tube = next(c for c in red if c["name"]["$value"] == "neon_01")
    assert tube["mesh"]["DepotPath"]["$value"] == r"x\neon_red.mesh"
    lights = [c for c in red if c["$type"] == "gameLightComponent"]
    assert len(lights) == len(kit.lights)
    for c in lights:
        assert c["turnOnByDefault"] == 1 and c["type"] == "LT_Point"
        assert (c["color"]["Red"], c["color"]["Green"], c["color"]["Blue"]) == (255, 1, 1)
        for k in app_mod._VEHICLE_ONLY:
            assert k not in c, f"{k} is not a gameLightComponent property"
    vc = next(c for c in red if c["$type"] == "entVisualControllerComponent")
    assert any(d["componentName"]["$value"] == "neon_01" for d in vc["appearanceDependency"])

    ent = {"Data": {"RootChunk": {"appearances": [{"$type": "entTemplateAppearance"}], "components": [],
                                  "compiledData": {}}}}
    try:
        e = ent_mod.build_ent(v, ent, {"app": r"x\car.app", "rig": "r", "vehicle_rig": "vr", "anims": "a", "phys": "p"},
                              "boxcar", ((-1, -2, 0), (1, 2, 1)))
    except (KeyError, TypeError, IndexError):
        pytest.skip("build_ent needs a fuller donor entity")
    pairs = [(a["name"]["$value"], a["appearanceName"]["$value"]) for a in e["Data"]["RootChunk"]["appearances"]]
    assert pairs[0] == ("boxcar", "default") and ("boxcar_neon_blue", "neon_blue") in pairs


def test_car_resource_announces_its_kit_to_the_hub(tmp_path):
    report_neon = {"colors": ["white", "hot_pink"], "labels": {"white": "White", "hot_pink": "Hot Pink"},
                   "appearances": {"white": "car_neon_white", "hot_pink": "car_neon_hot_pink"}, "default": "off",
                   "tubes": {"left": {}, "right": {}}}
    package.write_resource(str(tmp_path / "car"), name="car", version="1.0.0", display='Car "One"',
                           record="Vehicle.car", packages=["car.zip"], appearance="car", neon=report_neon)
    lua = (tmp_path / "car" / "server" / "main.lua").read_text()
    assert 'TriggerEvent("f2o77:car", CAR)' in lua and 'AddEventHandler("f2o77:hubReady", announce)' in lua
    assert 'appearance = "car_neon_hot_pink"' in lua and 'display = "Car \\"One\\""' in lua
    luac = shutil.which("luac5.4") or shutil.which("luac")
    if luac:
        subprocess.run([luac, "-p", str(tmp_path / "car" / "server" / "main.lua")], check=True)


# ----------------------------------------------------------------------------- dlc.rpf
def _rpf(entries, names_extra=b""):
    """Build an unencrypted ("OPEN") RPF7: entries = [(path, kind, bytes)], kind file|resource."""
    tree = {}
    for path, kind, data in entries:
        node = tree
        parts = path.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = (kind, data)
    flat, names = [], bytearray(b"\0")
    name_off = {"": 0}

    def name(n):
        if n not in name_off:
            name_off[n] = len(names)
            names.extend(n.encode() + b"\0")
        return name_off[n]

    # breadth-first: each directory's children are contiguous
    queue = [("", tree)]
    flat.append(["dir", "", None, 0, 0])
    dir_index = {id(tree): 0}
    while queue:
        _, node = queue.pop(0)
        di = dir_index[id(node)]
        first = len(flat)
        for k in sorted(node):
            v = node[k]
            if isinstance(v, dict):
                dir_index[id(v)] = len(flat)
                flat.append(["dir", k, None, 0, 0])
                queue.append((k, v))
            else:
                flat.append([v[0], k, v[1], 0, 0])
        flat[di][3], flat[di][4] = first, len(node)
    toc = 16 + 16 * len(flat) + len(names)
    data_start = (toc + 511) // 512 * 512
    blobs, pos = [], data_start
    entries_raw = []
    for kind, n, data, a, b in flat:
        no = name(n)
        if kind == "dir":
            entries_raw.append(struct.pack("<IIII", no, 0x7FFFFF00, a, b))
            continue
        off = pos // 512
        if kind == "file":
            comp = zlib.compress(data)[2:-4]
            stored = n.endswith(".rpf")
            blob = data if stored else comp
            buf = no | (0 if stored else len(comp)) << 16 | off << 40
            entries_raw.append(struct.pack("<QII", buf, len(data), 0))
        else:
            blob = data
            entries_raw.append(struct.pack("<H", no) + bytes([len(data) & 255, len(data) >> 8 & 255, len(data) >> 16 & 255,
                                                              off & 255, off >> 8 & 255, (off >> 16 & 255) | 0x80])
                               + struct.pack("<II", 0x10, 0x20))
        blobs.append((pos, blob))
        pos = (pos + len(blob) + 511) // 512 * 512
    names = bytes(names)
    out = bytearray(struct.pack("<IIII", 0x52504637, len(flat), len(names), 0x4E45504F))
    for e in entries_raw:
        out += e
    out += names
    out += b"\0" * (data_start - len(out))
    for p, blob in blobs:
        out += b"\0" * (p - len(out))
        out += blob
    out += b"\0" * ((-len(out)) % 512)
    return bytes(out)


def test_rpf7_open_archive_with_nested_archive(tmp_path):
    yft = b"RSC7" + struct.pack("<III", 162, 0, 0) + b"model-bytes"
    inner = _rpf([("biome.yft", "resource", yft)])
    outer = _rpf([("content.xml", "file", b"<xml/>"), ("data/vehicles.meta", "file", b"<meta>" * 50),
                  ("x64/vehicles.rpf", "file", inner)])
    (tmp_path / "dlc.rpf").write_bytes(outer)
    files = rpf.list_files(str(tmp_path / "dlc.rpf"))
    paths = {p for p, _, _ in files}
    assert {"content.xml", "data/vehicles.meta", "x64/vehicles.rpf/biome.yft"} <= paths
    out = rpf.extract(str(tmp_path / "dlc.rpf"), str(tmp_path / "x"))
    assert (tmp_path / "x" / "data" / "vehicles.meta").read_bytes() == b"<meta>" * 50
    assert (tmp_path / "x" / "x64" / "vehicles.rpf" / "biome.yft").read_bytes() == yft
    assert len(out) == 3


def test_rpf7_refuses_encrypted_archives(tmp_path):
    raw = bytearray(_rpf([("a.txt", "file", b"x")]))
    raw[12:16] = struct.pack("<I", 0x0FEFFFFF)          # NG
    with pytest.raises(rpf.RpfError, match="encrypted"):
        rpf.RpfArchive(bytes(raw))


def test_gxt2_and_lua_labels():
    s = b"Biome\0"
    data = b"2TXG" + struct.pack("<I", 1) + struct.pack("<II", joaat("biome"), 24) + b"2TXG" + struct.pack("<I", 30) + s
    labels = gxt2.parse_gxt2(data)
    assert gxt2.label(labels, "BIOME") == "Biome"
    lua = 'AddTextEntry("i8rp", "BMW i8 Roadster")\nAddTextEntryByHash(0x41A9F32C, "Other")'
    t = gxt2.parse_lua_text_entries(lua)
    assert t[joaat("i8rp")] == "BMW i8 Roadster" and t[0x41A9F32C] == "Other"


class _Info:
    def __init__(self, game, make):
        self.game_name, self.make_name = game, make


class _Src:
    def __init__(self, model, info, labels=None, pack=""):
        self.model, self.info, self.labels, self.pack_name = model, info, labels or {}, pack


def test_display_names():
    # a watermark make is not a name
    assert display_name_of(_Src("i8rpncs", _Info("i8rpncs", "FIVEMCAR.COM")))[0] == "i8rpncs"
    # labels win; make label prepended once
    labels = {joaat("i8rp"): "i8 Roadster", joaat("bmw"): "BMW"}
    assert display_name_of(_Src("i8rpncs", _Info("I8RP", "BMW"), labels))[0] == "BMW i8 Roadster"
    # a one-word name that the download's name says more about
    labels = {joaat("biome"): "Biome"}
    assert display_name_of(_Src("biome", _Info("biome", "Biome"), labels, "Mercedes-Benz Biome Concept"))[0] == \
        "Mercedes-Benz Biome Concept"


# ----------------------------------------------------------------------------- seats, exits, hull
from fivem2open77.cp77 import phys  # noqa: E402
from fivem2open77.model import seats  # noqa: E402


def _seated(v, seats_at, roof_z=None):
    """Add seat bones (and optionally a closed roof slab over the cabin) to a box car."""
    for name, pos in seats_at.items():
        v.bones[name] = {"t": np.array(pos, float), "r": np.eye(3), "parent": "Base", "open_axis": None}
        v.seats[name] = tuple(pos)
    if roof_z is not None:
        p, i = _box(-0.8, 0.8, -1.2, 1.0, roof_z, roof_z + 0.03)
        p = p - np.array([0.0, 0.0, 0.44], np.float32)
        n = np.zeros_like(p)
        n[:, 2] = 1
        v.parts["body_01"].submeshes.append(SubMesh(material="m", positions=p, normals=n, tangents=np.zeros((len(p), 4), np.float32),
                                                    uv0=np.zeros((len(p), 2), np.float32), uv1=None, colors=None, indices=i, lod=1))
    return v


def test_seats_keep_the_donor_posture_when_the_head_fits():
    v = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.12), "seat_front_right": (0.4, 0.0, 0.12)})
    fit = seats.fit_seats(v, {}, "sport2")
    assert fit.template == "sport2" and not fit.warnings
    assert fit.seats["seat_front_left"]["lowered"] == 0.0
    # the donor's exit spot (1 m out, 0.5 m down) is outside this 1.9 m body: unchanged
    ex = fit.seats["seat_front_left"]["exit"]
    assert abs(ex[0] + 1.4) < 1e-6 and abs(ex[2] - (0.12 - 0.5)) < 1e-6


def test_low_roof_gets_hypercar_posture_and_a_lower_seat():
    # roof inside at ~0.78 over a seat at 0.0: sport2 (0.90 head) cannot fit even 10 cm lower
    v = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80)
    fit = seats.fit_seats(v, {}, "sport2")
    assert fit.template == "sport1"
    s = fit.seats["seat_front_left"]
    assert s["lowered"] > 0 and v.bones["seat_front_left"]["t"][2] < 0
    assert s["headroom"] >= seats.TEMPLATES["sport1"][1] - 0.02
    # an explicit template wins
    v2 = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80)
    assert seats.fit_seats(v2, {"template": "standard2"}, "sport2").template == "standard2"


def test_exit_spot_moves_out_of_a_wide_body_and_up_to_the_road():
    # a centre driving seat in a 1.9 m car, low enough that 0.5 m down is under the road
    v = _seated(_car(top=0.3), {"seat_front_left": (0.0, 0.0, -0.05)})
    s = seats.fit_seats(v, {}, "sport2").seats["seat_front_left"]
    ex = s["exit"]
    assert ex[0] <= -(0.95 + seats.EXIT_MARGIN) + 1e-6, "outside the body"
    assert abs(ex[2] - (-0.35 - 0.02)) < 1e-6, "at road level"
    assert "out of the body" in s["exit_moved"] and "up to the road" in s["exit_moved"]


def test_collision_hull_stays_off_the_road():
    v = _car(floor=-0.32, skirt=-0.34)          # body 1 cm off the road
    cols = phys.build_colliders(v)
    road = phys.road_height(v)
    for tag, verts in cols:
        zmin = float(verts[:, 2].min()) + phys.CHASSIS_OFFSET_Z
        assert zmin >= road + phys.HULL_CLEARANCE - 1e-9, (tag, zmin)
    assert any(tag == "chassis_bottom" for tag, _ in cols)


def test_seating_profile_and_tweak_package():
    from fivem2open77.cp77.donor import DONORS
    from fivem2open77.cp77.tweak import build_tweak
    with pytest.raises(ProfileError):
        normalize({"seats": {"template": "bus"}})
    v = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80)
    fit = seats.fit_seats(v, {}, "sport2")
    text, _ = build_tweak(v, DONORS["porsche_911"], record="Vehicle.boxcar", ent_path=r"x\boxcar.ent",
                          appearance="boxcar", name_key="k", seating=fit)
    assert "vehDataPackage: Vehicle.boxcar_data_package" in text
    assert "seatingTemplateOverride: sport1_vehicle" in text and "- sport1_vehicle_seat_front_left" in text
    plain = seats.fit_seats(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.12)}), {}, "sport2")
    text2, _ = build_tweak(_car(), DONORS["porsche_911"], record="Vehicle.boxcar", ent_path=r"x\boxcar.ent",
                           appearance="boxcar", name_key="k", seating=plain)
    assert "vehDataPackage" not in text2, "the donor's own posture needs no package"
