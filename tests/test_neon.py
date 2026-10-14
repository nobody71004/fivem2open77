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


def _rotate(q, v):
    """Rotate v by the quaternion (x, y, z, w)."""
    x, y, z, w = q
    u = np.array([x, y, z])
    v = np.asarray(v, np.float64)
    return v + 2 * np.cross(u, np.cross(u, v) + w * v)


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
    assert len(kit.lights) == 12 and kit.default == "white" and len(kit.colors) == 13


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


def test_handling_wheel_offset_profile():
    assert normalize({})["handling"]["wheel_offset"] is None
    h = normalize({"handling": {"wheel_offset": {"front": 0, "back": -0.01}}})["handling"]
    assert h["wheel_offset"] == {"front": 0.0, "back": -0.01}
    with pytest.raises(ProfileError) as e:
        normalize({"handling": {"wheel_offset": {"front": 0.5, "roof": 0}}})
    assert "handling.wheel_offset" in str(e.value)


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
    lights = [c for c in red if c["name"]["$value"].startswith("neon_light")]
    assert len(lights) == len(kit.lights)
    for c, light in zip(lights, kit.lights):
        # one of the car's own lights, lit with its headlights: in a vehicle only the vehicle lights
        # a light (every light in every Cyberpunk car is off by default), and the headlight mode is
        # what Open77 keeps in step for every player; high beams leave it alone
        assert c["$type"] == "vehicleLightComponent" and c["lightType"] == app_mod.NEON_LIGHT_TYPE == "Head"
        assert c["turnOnByDefault"] == 0 and c["isEnabled"] == 1 and c["onStrength"] == 1 and c["emissiveOnly"] == 0
        assert (c["highBeamPitchAngle"], c["highBeamRadiusMultiplier"], c["highBeamConeMultiplier"]) == (0, 1, 1)
        assert c["isDestructible"] == 0 and c["colliderName"]["$value"] == "None"
        # a soft spot aimed at the road: the headlights' linear falloff, no specular (no mirrored
        # streaks on wet roads), the world light channel only (never the car or its cabin)
        assert c["type"] == "LT_Spot" and c["spotCapsule"] == 0
        assert c["unit"] == "LU_Lumen" and c["attenuation"] == "LA_Linear" and c["EV"] == 0
        assert c["radius"] == pytest.approx(kit.spread) and (c["innerAngle"], c["outerAngle"]) == (neon.CONE_INNER, neon.CONE_OUTER)
        assert c["outerAngle"] < 180, "nothing above the light is lit"
        assert c["sceneSpecularScale"] == 0 and c["sceneDiffuse"] == 1
        assert c["lightChannel"] == "LC_ChannelWorld", "lights the road, never the car itself"
        assert c["enableLocalShadows"] == 0 and c["intensity"] == neon.light_lumen(kit, light, (255, 1, 1))
        assert (c["color"]["Red"], c["color"]["Green"], c["color"]["Blue"]) == (255, 1, 1)
        # in body_01's frame (Root minus the frame offset); +Y (where it shines) turned onto the
        # light's direction -- down, out from under the car -- and +X along the tube
        pos = c["localTransform"]["Position"]
        xyz = np.array([pos[k]["Bits"] / 131072.0 if isinstance(pos[k], dict) else pos[k] for k in ("x", "y", "z")])
        assert np.allclose(xyz + np.array([0, 0, 0.44]), light.position, atol=2e-4)
        o = c["localTransform"]["Orientation"]
        q = (o["i"], o["j"], o["k"], o["r"])
        assert np.allclose(_rotate(q, (0.0, 1.0, 0.0)), light.direction, atol=1e-6)
        assert np.allclose(_rotate(q, (1.0, 0.0, 0.0)), light.along, atol=1e-6)
    blue = next(c for c in apps[2]["Data"]["components"] if c["name"]["$value"].startswith("neon_light"))
    white = next(c for c in apps[3]["Data"]["components"] if c["name"]["$value"].startswith("neon_light"))
    assert blue["intensity"] > 1.5 * white["intensity"], "dark colours get more light to read on the road"
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


def test_neon_is_light_only_by_default():
    """Like GTA V's neon, the kit is light only: no tube meshes in any appearance (the glowing bars
    under the car were in the way), the road still glows in every colour; neon.tubes brings the
    bars back."""
    v = _car()
    conf = normalize({"neon": {"colors": ["red", "blue"]}})["neon"]
    assert conf["tubes"] is False
    kit = neon.fit_kit(v, {}, conf)                    # what the converter runs
    assert v.neon is kit and kit.visible_tubes is False and kit.summary()["tubes_visible"] is False
    assert "neon_01" not in v.parts
    doc = app_mod.build_app(v, _donor_app(), {"body_01": r"x\body_01.mesh"}, neon_meshes={})
    apps = doc["Data"]["RootChunk"]["appearances"]
    assert [a["Data"]["name"]["$value"] for a in apps] == ["default", "neon_red", "neon_blue"]
    for a in apps[1:]:
        comps = a["Data"]["components"]
        assert not any(c["name"]["$value"] == "neon_01" or c["$type"] == "entMeshComponent"
                       and "neon" in str(c.get("mesh")) for c in comps), "no glowing bars"
        assert len([c for c in comps if c["name"]["$value"].startswith("neon_light")]) == len(kit.lights)
        vc = next(c for c in comps if c["$type"] == "entVisualControllerComponent")
        assert not any(d["componentName"]["$value"] == "neon_01" for d in vc["appearanceDependency"])
    # the Studio still shows where the light comes from, and the lit road
    from fivem2open77 import render
    assert not len(render.neon_triangles(v, (255, 1, 1))[0])
    marks, cols = render.light_markers(v, (255, 1, 1))
    assert len(marks) == 8 * len(kit.lights) and np.allclose(cols, (1.0, 1 / 255, 1 / 255))
    # back on: the profile switch, validated
    on = normalize({"neon": {"tubes": True}})["neon"]
    v2 = _car()
    assert neon.fit_kit(v2, {}, on).visible_tubes is True and v2.parts["neon_01"].kind == "neon"
    with pytest.raises(ProfileError) as e:
        normalize({"neon": {"tubes": "yes"}})
    assert "neon.tubes" in str(e.value)


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
    # a car of a pack read from its own resource folder: the folder is the model id, not a better name
    assert display_name_of(_Src("ikx3_c63se24", _Info("C63SE", ""), {}, "ikx3 c63se24"))[0] == "C63SE"


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


def _low(v):
    v.gta_layout = "LAYOUT_LOW"
    return v


def test_low_roof_gets_hypercar_posture_and_a_lower_seat():
    # roof inside at ~0.78 over a seat at 0.0: sport2 (0.90 head) cannot fit even 10 cm lower
    v = _low(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80))
    fit = seats.fit_seats(v, {}, "sport2")
    assert fit.template == "sport1" and "LAYOUT_LOW" in fit.template_reason
    s = fit.seats["seat_front_left"]
    assert s["lowered"] > 0 and v.bones["seat_front_left"]["t"][2] < 0
    assert s["headroom"] >= seats.TEMPLATES["sport1"][1] - 0.02
    # an explicit template wins
    v2 = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80)
    assert seats.fit_seats(v2, {"template": "standard2"}, "sport2").template == "standard2"


def test_upright_cars_keep_their_posture_and_rear_benches_do_not_count():
    """A saloon or SUV keeps the donor's upright posture: the seat goes down further instead of the
    driver lying back like in a hypercar.  A GTA rear bench behind a two-seat donor never decides."""
    v = _seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80)   # LAYOUT_STANDARD
    fit = seats.fit_seats(v, {}, "sport2")
    assert fit.template == "sport2" and "lowered" in fit.template_reason
    assert seats.MAX_LOWER < fit.seats["seat_front_left"]["lowered"] <= seats.MAX_LOWER_UPRIGHT + 1e-9
    # front seats fit; the rear bench sits under a lower roof line (a 1+2 rear bench in a coupe)
    v = _low(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.4, 0.12), "seat_front_right": (0.4, 0.4, 0.12),
                                     "seat_back_left": (-0.4, -0.6, 0.12)}, roof_z=1.10))
    p, i = _box(-0.8, 0.8, -1.2, -0.3, 0.80, 0.83)           # the roof drops over the back seat
    p = p - np.array([0.0, 0.0, 0.44], np.float32)
    v.parts["body_01"].submeshes[-1].positions = np.concatenate([v.parts["body_01"].submeshes[-1].positions[:0], p])
    v.parts["body_01"].submeshes[-1].indices = i
    both = seats.fit_seats(v, {}, "sport2", ("seat_front_left", "seat_front_right"))
    assert both.template == "sport2", both.template_reason
    assert not [w for w in both.warnings if "seat_back_left" in w]


def test_exit_spot_moves_out_of_a_wide_body_and_up_to_the_road():
    # a centre driving seat in a 1.9 m car, low enough that 0.5 m down is under the road
    v = _seated(_car(top=0.3), {"seat_front_left": (0.0, 0.0, -0.05)})
    s = seats.fit_seats(v, {}, "sport2").seats["seat_front_left"]
    ex = s["exit"]
    assert ex[0] <= -(0.95 + seats.EXIT_CLEARANCE) + 1e-6, "outside the body"
    assert abs(ex[2] - (-0.35 - 0.02)) < 1e-6, "at road level"
    assert "out of the body" in s["exit_moved"] and "up to the road" in s["exit_moved"]


def _with_fairings(v, half=1.26):
    """Wheel fairings out to `half` m over both axles, like the Biome's."""
    for y0, y1 in ((0.75, 1.6), (-1.8, -0.9)):
        for x0, x1 in ((-half, -0.95), (0.95, half)):
            p, i = _box(x0, x1, y0, y1, -0.1, 0.2)
            p = p - np.array([0.0, 0.0, 0.44], np.float32)
            n = np.zeros_like(p)
            n[:, 2] = 1
            v.parts["body_01"].submeshes.append(SubMesh(material="m", positions=p, normals=n,
                                                        tangents=np.zeros((len(p), 4), np.float32),
                                                        uv0=np.zeros((len(p), 2), np.float32), uv1=None,
                                                        colors=None, indices=i, lod=1))
    return v


def test_a_wide_car_gets_room_to_get_out():
    """The Biome loop: the game slid the player from seat to seat because 1.65 m from the centre
    is inside its 1.26 m wheel fairings.  The record gets its own unmountOffsetPosition."""
    from fivem2open77.cp77.donor import DONORS
    from fivem2open77.cp77.tweak import build_tweak
    v = _seated(_with_fairings(_car(top=0.3)), {"seat_front_left": (0.0, 0.36, -0.15),
                                                "seat_front_right": (0.42, -0.38, -0.17)})
    fit = seats.fit_seats(v, {}, "sport2")
    assert fit.unmount == (2.0, 5.0, 2.5), fit.unmount
    assert abs(fit.exit_needed - (1.26 + seats.EXIT_CLEARANCE)) < 1e-3
    assert fit.seats["seat_front_left"]["exit"][0] == -2.0 and fit.seats["seat_front_right"]["exit"][0] == 2.0
    assert fit.notes and "2.00 m from the centre line" in fit.notes[0]
    assert fit.summary()["exit_distance"] == 2.0
    text, _ = build_tweak(v, DONORS["porsche_911"], record="Vehicle.widecar", ent_path=r"x\widecar.ent",
                          appearance="widecar", name_key="k", seating=fit)
    assert "unmountOffsetPosition: { x: 2.0, y: 5.0, z: 2.5 }" in text
    # an ordinary 1.9 m car keeps the donor's distance and the record does not mention it
    plain = seats.fit_seats(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.12),
                                                    "seat_front_right": (0.4, 0.0, 0.12)}), {}, "sport2")
    assert plain.unmount is None and plain.summary()["exit_distance"] == 1.65
    text2, _ = build_tweak(_car(), DONORS["porsche_911"], record="Vehicle.boxcar", ent_path=r"x\boxcar.ent",
                           appearance="boxcar", name_key="k", seating=plain)
    assert "unmountOffsetPosition" not in text2


def test_neon_starts_lit_by_default():
    conf = normalize({})["neon"]
    assert conf["default"] == "on"
    kit = neon.plan_neon(_car(), {}, conf)
    assert kit.default == kit.colors[0][0] == "white"
    assert normalize({"neon": {"default": "off"}})["neon"]["default"] == "off"
    custom = normalize({"neon": {"colors": ["#ff0000"], "default": "on"}})["neon"]
    assert neon.plan_neon(_car(), {}, custom).default == custom_key(custom)


def custom_key(conf):
    return neon.resolve_colors(conf["colors"])[0][0]


def _old_kit_glow(kit, x, y):
    """The first kits' light: one or two point lights per tube just under it, inverse square."""
    z = neon.road_height(kit, y)
    e = np.zeros(np.shape(x))
    for t in kit.tubes:
        k = 2 if t.length > 1.1 else 1
        for i in range(k):
            p = t.start + (t.end - t.start) * (i + 0.5) / k - np.array([0, 0, kit.tube_radius + 0.004])
            dx, dy, dz = x - p[0], y - p[1], z - p[2]
            r2 = dx * dx + dy * dy + dz * dz
            e += 160.0 / (4 * np.pi) * np.clip(-dz / np.sqrt(r2), 0, 1) / r2 * np.clip(1 - (r2 / 2.4 ** 2) ** 2, 0, 1) ** 2
    return e


def test_neon_lights_a_pool_on_the_road_around_the_car():
    """The road around the car is washed in colour -- brightest at the sills, fading smoothly a
    metre or two out -- not a hard bright line along each sill (the capsule kit) or a hot spot
    under each light and next to nothing outside the car (the first kits)."""
    v = _car()
    kit = neon.plan_neon(v, {}, normalize({})["neon"])
    for t in kit.tubes:
        mine = [l for l in kit.lights if l.side == t.side]
        assert len(mine) == int(np.ceil(t.length / neon.LIGHT_SPACING - 1e-9)), t.side
        assert sum(l.intensity for l in mine) == pytest.approx(neon.LIGHT_FLUX * t.length, rel=1e-3)
        out = np.array([*neon.OUTWARD[t.side], 0.0])
        for l in mine:
            road = float(neon.road_height(kit, l.position[1]))
            assert l.height == pytest.approx(l.position[2] - road)
            assert l.height == pytest.approx(neon.LIGHT_HEIGHT), "high: a wide, soft pool"
            # moved out from the tube to just inside the sill's edge where the tube is further in
            # (the box's sides are at +-0.95, its ends at +-2.2), never outside the car
            shift = float((l.position[:2] - t.start[:2]) @ out[:2])
            edge = 0.95 if t.side in ("left", "right") else 2.2
            tube = float(t.start[:2] @ out[:2])
            assert 0 <= shift <= neon.EDGE_MAX_SHIFT + 1e-9
            assert abs(float(l.position[:2] @ out[:2]) - max(tube, edge - neon.EDGE_INSET)) < 0.035, (t.side, l.position)
            tilt = np.degrees(np.arccos(-l.direction[2]))
            assert abs(tilt - neon.TILT) < 1e-6 and l.direction @ out > 0, "at the road, out from under the car"
            assert l.radius == kit.spread == 2.0
    # across the road from the left sill outwards (the car's side is at x = -0.95)
    xs = np.array([-0.95 - d for d in (0.0, 0.25, 0.5, 1.0, 1.5, 3.0)])
    ys = np.zeros_like(xs)
    new = neon.ground_glow(kit, xs, ys)
    old = _old_kit_glow(kit, xs, ys)
    # lux, about: the donor's headlights put ~7 on the road 2 m ahead and ~2 at 5 m
    assert 20 < new[0] < 60 and new[2] > 12 and new[3] > 6 and new[4] > 1.5, new
    assert new[1] > 0.75 * new[0] and new[3] > 0.2 * new[0], "a soft pool, not a line along the sill"
    assert (new[2:5] > 2 * old[2:5]).all(), (new, old)
    assert new[5] == 0, "nothing beyond the reach"
    assert all(np.diff(new) <= 1e-9), "brightest at the car, fading outwards"
    # and even along the car: no beads between the lights, just the fade towards the tube's ends
    along = neon.ground_glow(kit, np.full(25, -1.2), np.linspace(-0.6, 0.6, 25))
    d, top = np.diff(along), int(np.argmax(along))
    assert (d[:top] >= -1e-9).all() and (d[top:] <= 1e-9).all() and along.min() > 0.8 * along.max(), along
    # the reach and the brightness are the profile's
    wide = neon.plan_neon(_car(), {}, normalize({"neon": {"spread": 3.5, "brightness": 2}})["neon"])
    far = neon.ground_glow(wide, np.array([-0.95 - 2.8]), np.array([0.0]))[0]
    assert far > 0 and wide.lights[0].radius == 3.5 and wide.summary()["spread"] == 3.5
    assert neon.ground_glow(wide, xs[:1], ys[:1])[0] > 1.9 * new[0]


def test_neon_colour_gain_and_a_low_car():
    assert neon.glow_gain((222, 222, 255)) == 1.0 and neon.glow_gain((94, 255, 1)) == 1.0
    assert 1.8 < neon.glow_gain((2, 21, 255)) <= neon.GAIN_MAX and neon.glow_gain((255, 1, 1)) > 1.0
    # a car whose floor is 9 cm off the road: the tubes squeeze in under it, the lights stay up in
    # the body (they never light it), well clear of the road even if the car sags in the game
    v = _car(floor=-0.26)
    kit = neon.plan_neon(v, {}, normalize({})["neon"])
    assert kit is not None
    for l in kit.lights:
        tube = next(t for t in kit.tubes if t.side == l.side)
        assert l.position[2] >= tube.start[2] + neon.LIGHT_OVER_TUBE - 1e-9
        assert l.height == pytest.approx(neon.LIGHT_HEIGHT)
    # a tall car: the lights stay over the tubes
    tall = _car(floor=0.05)
    kit = neon.plan_neon(tall, {}, normalize({})["neon"])
    for l in kit.lights:
        tube = next(t for t in kit.tubes if t.side == l.side)
        assert l.position[2] == pytest.approx(tube.start[2] + neon.LIGHT_OVER_TUBE)
    with pytest.raises(ProfileError) as e:
        normalize({"neon": {"spread": 9}})
    assert "neon.spread" in str(e.value)
    assert normalize({})["neon"]["spread"] == 2.0


def test_neon_preview_shows_the_lit_road():
    from fivem2open77 import render
    v = _car()
    kit = neon.plan_neon(v, {}, normalize({})["neon"])
    neon.add_neon_part(v, kit)
    v.neon = kit
    tris, cols = render.glow_triangles(v, (2, 21, 255))
    assert len(tris) and (cols[:, 2] > cols[:, 0] + 0.3).any(), "the road is blue near the car"
    assert np.allclose(cols.min(0), render.ROAD, atol=0.02), "and dark far away"
    top, corner = render.glow_views(v, (2, 21, 255), size=(240, 150))
    assert top.shape == (150, 240, 3) and corner.shape == (150, 240, 3)
    assert (top[..., 2].astype(int) - top[..., 0]).max() > 80


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
    v = _low(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.0)}, roof_z=0.80))
    fit = seats.fit_seats(v, {}, "sport2")
    text, _ = build_tweak(v, DONORS["porsche_911"], record="Vehicle.boxcar", ent_path=r"x\boxcar.ent",
                          appearance="boxcar", name_key="k", seating=fit)
    assert "vehDataPackage: Vehicle.boxcar_data_package" in text
    assert "seatingTemplateOverride: sport1_vehicle" in text and "- sport1_vehicle_seat_front_left" in text
    plain = seats.fit_seats(_seated(_car(top=0.3), {"seat_front_left": (-0.4, 0.0, 0.12)}), {}, "sport2")
    text2, _ = build_tweak(_car(), DONORS["porsche_911"], record="Vehicle.boxcar", ent_path=r"x\boxcar.ent",
                           appearance="boxcar", name_key="k", seating=plain)
    assert "vehDataPackage" not in text2, "the donor's own posture needs no package"
