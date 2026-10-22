"""Offline checks (no game, no WolvenKit): python -m pytest tests  or  python tests/test_offline.py"""
import os
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77 import package  # noqa: E402
from fivem2open77.cp77.handles import IdAllocator, materialize, normalize  # noqa: E402
from fivem2open77.cp77.rig import align_axis  # noqa: E402
from fivem2open77.cp77.wkjson import index_handles  # noqa: E402


def test_crc64_xz_check_value():
    assert package._crc64_xz(b"123456789") == 0x995DC9BBDF1939FA


def _fake_archive(entries=3, stamp=123):
    table = bytearray(struct.pack("<III", entries, 0, 0))
    for i in range(entries):
        table += struct.pack("<Qq5I20s", 1000 + i, stamp + i, 0, 0, 0, 0, 0, b"\0" * 20)
    index_pos = 44
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, index_pos, len(table) + 16, 0, 0, 0) + b"\0\0\0\0"
    index = struct.pack("<IIQ", 8, len(table) + 8, package._crc64_xz(bytes(table))) + table
    return head + index


def test_normalize_archive_is_deterministic():
    paths = []
    for stamp in (111, 999999):
        fd, p = tempfile.mkstemp(suffix=".archive")
        os.write(fd, _fake_archive(stamp=stamp))
        os.close(fd)
        assert package.normalize_archive(p) == 3
        paths.append(p)
    a, b = (open(p, "rb").read() for p in paths)
    assert a == b
    for p in paths:
        os.remove(p)


def test_normalize_handles_moves_definition_before_reference():
    donor = {"defs": {"HandleId": "1", "Data": {"$type": "X", "v": 1}}}
    copied = {"a": {"HandleRefId": "1"}, "b": {"HandleRefId": "1"}}
    doc = materialize(copied, index_handles(donor))
    assert doc["a"]["HandleId"] == "1" and doc["b"] == {"HandleRefId": "1"}
    ids = IdAllocator(donor)
    ids.fresh_ids(doc)
    assert doc["a"]["HandleId"] == "2" and doc["b"]["HandleRefId"] == "2"
    root = {"first": {"HandleRefId": "2"}, "second": doc["a"]}
    normalize(root, index_handles(doc))
    assert root["first"]["HandleId"] == "2" and root["second"] == {"HandleRefId": "2"}


def test_align_axis_maps_donor_axis_onto_gta_hinge():
    for d, a in (((0, 0, -1), (0, 0, -1)), ((1, 0, 0), (-1, 0, 0)), ((0, 0, 1), (1, 0, 0))):
        q = align_axis(d, a)
        assert np.allclose(q @ np.array(d, float), a)
        assert np.isclose(np.linalg.det(q), 1.0)


def test_door_chime_script_renders():
    text = package.DOOR_CHIME.format(display="Car", record="Vehicle.car", name="car_chime", duration="5.434",
                                     volume="0.80", range="12.0", sound="audio/door_chime.mp3")
    assert 'local RECORD = "Vehicle.car"' in text and "Play3DSound" in text and "{" in text
    assert "local DURATION = 5.434" in text and "HOLD" not in text
    assert 'AddEventHandler("onVehicleOccupancyChanged"' in text and "duration = DURATION" in text


def test_door_chime_logic_in_lua():
    """Runs the rendered server script against a fake Open77 server (skipped without lua5.4)."""
    import shutil as _sh
    import subprocess
    lua = _sh.which("lua5.4") or _sh.which("lua")
    if not lua:
        print("   (skipped: no lua5.4)")
        return
    text = package.DOOR_CHIME.format(display="Car", record="Vehicle.car", name="car_chime", duration="5.434",
                                     volume="0.80", range="12.0", sound="audio/door_chime.mp3")
    fd, p = tempfile.mkstemp(suffix=".lua")
    os.write(fd, text.encode())
    os.close(fd)
    harness = os.path.join(os.path.dirname(__file__), "lua", "door_chime_harness.lua")
    r = subprocess.run([lua, harness, p], capture_output=True, text=True)
    os.remove(p)
    assert r.returncode == 0, r.stdout + r.stderr


def _mp3_frames(n, bitrate_index=9, mpeg1=True):
    """n silent MPEG Layer III frames (128 kbit/s, 44.1 kHz for MPEG-1)."""
    ver = 3 if mpeg1 else 2
    h = (0x7FF << 21) | (ver << 19) | (1 << 17) | (1 << 16) | (bitrate_index << 12) | (0 << 10)
    rate = 44100 if mpeg1 else 22050
    kbps = (128 if mpeg1 else 64)
    length = (144 if mpeg1 else 72) * kbps * 1000 // rate
    return (struct.pack(">I", h) + b"\0" * (length - 4)) * n, (1152 if mpeg1 else 576) * n / rate


def test_audio_duration_mp3_and_wav():
    d = tempfile.mkdtemp()
    frames, want = _mp3_frames(200)
    tag = b"ID3\x03\x00\x00" + bytes([0, 0, 0, 20]) + b"\0" * 20       # 20-byte ID3v2 body
    p = os.path.join(d, "a.mp3")
    with open(p, "wb") as f:
        f.write(tag + frames)
    assert abs(package.audio_duration(p) - want) < 1e-9
    info = bytearray(_mp3_frames(1)[0])                                  # LAME/Xing header frame
    info[36:40] = b"Info"
    with open(p, "wb") as f:
        f.write(tag + bytes(info) + frames)
    assert abs(package.audio_duration(p) - want) < 1e-9                 # not counted as audio
    frames2, want2 = _mp3_frames(100, bitrate_index=8, mpeg1=False)
    with open(p, "wb") as f:
        f.write(frames2)
    assert abs(package.audio_duration(p) - want2) < 1e-9
    w = os.path.join(d, "a.wav")
    pcm = b"\0\0" * 44100 * 2                                          # 2 s mono 16-bit 44.1 kHz
    fmt = struct.pack("<HHIIHH", 1, 1, 44100, 88200, 2, 16)
    with open(w, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
    assert abs(package.audio_duration(w) - 2.0) < 1e-9
    junk = os.path.join(d, "junk.mp3")
    with open(junk, "wb") as f:
        f.write(b"not audio at all" * 10)
    try:
        package.audio_duration(junk)
        raise AssertionError("junk accepted")
    except ValueError:
        pass


def _directives(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("--")]


def test_car_resource_is_server_only_and_chime_has_no_dependencies():
    """Clients check `dependency` lines and never receive the server-only loader resources, so a
    car resource with loader dependencies must not carry client content (missing_dependency)."""
    out = tempfile.mkdtemp()
    chime = os.path.join(out, "chime.mp3")
    with open(chime, "wb") as f:
        f.write(_mp3_frames(50)[0])
    res = os.path.join(out, "mycar")
    names = package.write_resource(res, name="mycar", version="1.0.0", display="My Car", record="Vehicle.mycar",
                                   packages=["mycar.zip"], door_chime=chime, report={"x": 1})
    assert names == ["archivexl", "tweakxl", "mycar", "mycar_chime"]
    car = _directives(os.path.join(res, "open77.lua"))
    assert 'dependency "archivexl >=1.27.1"' in car and 'preload_mod "dist/mycar.zip"' in car
    assert not any(l.startswith(("client_script", "files", "shared_script", "web_files")) for l in car)
    side = _directives(os.path.join(out, "mycar_chime", "open77.lua"))
    assert 'resource "mycar_chime"' in side and 'server_script "server/door_chime.lua"' in side
    assert 'permissions { "world.vehicles", "audio.network" }' in side
    assert not any(l.startswith(("dependency", "dependencies", "preload_mod", "client_script")) for l in side)
    assert os.path.isfile(os.path.join(out, "mycar_chime", "audio", "door_chime.mp3"))
    with open(os.path.join(out, "mycar_chime", "server", "door_chime.lua"), encoding="utf-8") as f:
        assert "local DURATION = 1.306" in f.read()          # 50 frames x 1152 / 44100
    # without a chime the sibling disappears again
    names = package.write_resource(res, name="mycar", version="1.0.0", display="My Car", record="Vehicle.mycar",
                                   packages=["mycar.zip"])
    assert names == ["archivexl", "tweakxl", "mycar"] and not os.path.exists(os.path.join(out, "mycar_chime"))


def _rig_doc(bones):
    """bones: [(name, parent index, (x, y, z))] -> minimal WolvenKit rig JSON."""
    return {"Data": {"RootChunk": {
        "boneNames": [{"$type": "CName", "$storage": "string", "$value": n} for n, _, _ in bones],
        "boneParentIndexes": [p for _, p, _ in bones],
        "boneTransforms": [{"Translation": {"X": t[0], "Y": t[1], "Z": t[2]},
                            "Rotation": {"i": 0, "j": 0, "k": 0, "r": 1}} for _, _, t in bones],
    }}}


def _car_with_lids(lids):
    from fivem2open77.model.ir import LightSpec, Part, Vehicle, WheelAxle
    v = Vehicle(name="car", display_name="Car", make="", gta_class="VC_SPORT", gta_layout="", door_count=2)
    v.axles = {"front": WheelAxle(0.35, 0.25, 0.25, {"fl": (-0.8, 1.4, 0), "fr": (0.8, 1.4, 0)}),
               "back": WheelAxle(0.35, 0.25, 0.25, {"bl": (-0.8, -1.3, 0), "br": (0.8, -1.3, 0)})}
    for name, y in lids.items():
        v.bones[name] = {"t": np.array([0.0, y, 0.6]), "r": np.eye(3), "parent": "Base", "open_axis": None}
        v.parts[name] = Part(name=name, mesh_name=name, slot=name, parent=None, kind=name[:-2])
        v.part_frames[name] = (np.zeros(3), np.eye(3))
    v.lights.append(LightSpec(name="brake_l", light_type="Brake", zone=3, part="trunk_a",
                              position=(0, 0, 0), direction=(0, -1, 0)))
    return v


def test_lids_follow_the_donor_layout():
    from fivem2open77.cp77.rig import lid_mapping
    common = [("Root", -1, (0, 0, 0)), ("Base", 0, (0, 0, 0.44)),
              ("wheel_front_left", 1, (-0.7, 1.26, -0.44)), ("wheel_back_left", 1, (-0.7, -1.26, -0.44))]
    porsche = _rig_doc(common + [("hood_a", 1, (-0.45, -1.53, 0.19)), ("trunk_a", 1, (-0.66, 0.69, 0.15))])
    sedan = _rig_doc(common + [("hood_a", 1, (0, 0.9, 0.2)), ("trunk_a", 1, (0, -1.6, 0.2))])

    v = _car_with_lids({"hood_a": 1.0, "trunk_a": -1.6})        # GTA bonnet front, boot rear
    m = lid_mapping(v, porsche)
    assert m == {"hood_a": "trunk_a", "trunk_a": "hood_a"}, m
    v.rename_parts(m)
    assert v.bones["trunk_a"]["t"][1] == 1.0 and v.bones["hood_a"]["t"][1] == -1.6
    assert v.parts["trunk_a"].slot == "trunk_a" and v.parts["trunk_a"].kind == "trunk"
    assert v.parts["hood_a"].mesh_name == "hood_a" and v.lights[0].part == "hood_a"
    assert lid_mapping(_car_with_lids({"hood_a": 1.0, "trunk_a": -1.6}), sedan) == {}
    assert lid_mapping(_car_with_lids({"trunk_a": -1.6}), porsche) == {"trunk_a": "hood_a"}
    assert lid_mapping(_car_with_lids({"hood_a": 1.0, "trunk_a": 1.2}), porsche) == {}


def test_resource_ids_follow_open77_name_rules():
    from fivem2open77.model.build import _sanitize
    import re
    for raw in ("i8rpncs", "911turbo", "GT-R 34", "a", "x" * 80, "--"):
        s = _sanitize(raw)
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,57}", s), (raw, s)
    assert _sanitize("i8rpncs") == "i8rpncs" and _sanitize("911turbo") == "v_911turbo"


def _fake_model(floors):
    """A model of box colliders only: [(center, half, kind, part kind)] -> the shapes ambience reads."""
    import types
    parts, placements = {}, []
    by_part = {}
    for center, half, kind, pkind in floors:
        by_part.setdefault(("m" if pkind == "mlo" else "o", pkind), []).append((center, half, kind))
    for (pname, pkind), boxes in by_part.items():
        cols = [types.SimpleNamespace(kind=kind, center=center, half=half, rotation=(0, 0, 0, 1))
                for center, half, kind in boxes]
        parts[pname] = types.SimpleNamespace(kind=pkind, collision_only=False, colliders=cols)
        placements.append(types.SimpleNamespace(part=pname, position=(0.0, 0.0, 0.0), yaw=0.0))
    return types.SimpleNamespace(parts=parts, placements=placements)


def test_ambient_outline_buffer_is_count_points_height():
    import base64
    from fivem2open77.mlo import ambience
    raw = base64.b64decode(ambience.outline_buffer([(-1, -1), (1, -1), (1, 1), (-1, 1)], 2.0))
    (count,) = struct.unpack("<I", raw[:4])
    assert count == 4 and len(raw) == 4 + 4 * 16 + 4
    pts = [struct.unpack("<ffff", raw[4 + 16 * i:20 + 16 * i]) for i in range(4)]
    assert pts == [(-1.0, -1.0, 0.0, 1.0), (1.0, -1.0, 0.0, 1.0), (1.0, 1.0, 0.0, 1.0), (-1.0, 1.0, 0.0, 1.0)]
    (height,) = struct.unpack("<f", raw[-4:])
    assert height == 2.0
    node = ambience.area_node("test_quiet", ambience.UNIT_POINTS, ambience.OUTLINE_HEIGHT, 7)
    assert node["notifiers"][0]["HandleId"] == "7"
    settings = node["notifiers"][0]["Data"]["Settings"]["Data"]
    assert settings["Parameters"][0]["name"]["$value"] == "amb_interior"
    assert settings["Parameters"][0]["value"] == 1.0
    assert settings["Priority"] == 10 and settings["Reverb"]["$value"] == "revb_interior_room_medium"


def test_quiet_interiors_volumes_follow_the_floor_levels():
    from fivem2open77.mlo import ambience
    box = lambda c, h, kind="box", pkind="mlo": (c, h, kind, pkind)
    model = _fake_model([
        box((0, 0, -0.15), (10, 8, 0.15)),            # lower floor, top z 0
        box((0, 0, 4.85), (10, 8, 0.15)),             # upper floor, top z 5
        box((0, 0, 2.5), (0.15, 8, 2.5)),             # a wall: thin in x, must not be a floor
        box((0, 0, 2.41), (1.5, 1.2, 0.09)),          # a stair landing: walkable but too small for a level
        box((0, 0, 8.0), (10, 8, 0.2), "box", "object"),  # the shell's roof: outdoors, leave loud
    ])
    assert ambience.floor_volumes(model, "off") == []
    bottom = ambience.floor_volumes(model, "bottom")
    assert bottom == [[-11.2, -9.2, -0.5, 11.2, 9.2, 4.55]], bottom
    both = ambience.floor_volumes(model, "all")
    assert len(both) == 2 and both[0] == bottom[0]
    assert both[1] == [-11.2, -9.2, 4.5, 11.2, 9.2, 8.5], both[1]
    try:
        ambience.floor_volumes(model, "sometimes")
    except ValueError as e:
        assert "quiet_interiors" in str(e)
    else:
        raise AssertionError("bad mode accepted")


def test_sector_doc_emits_quiet_interior_areas():
    from fivem2open77.mlo import ambience, world
    layout = {
        "name": "testmap",
        "parts": {"p": {"mesh": "m.mesh", "ent": "p.ent", "kind": "mlo", "radius": 5.0,
                         "collision": True, "bounds": [[-1, -1, -1], [1, 1, 1]]}},
        "placements": [{"part": "p", "x": 1.0, "y": 2.0, "z": 3.0, "yaw": 30.0}],
        "ambience": [{"box": [-10, -8, -0.5, 10, 8, 4.5]}],
    }
    anchor = {"x": 100.0, "y": 200.0, "z": 30.0, "yaw": -33.0}
    doc, box = world.sector_doc(layout, anchor)
    root = doc["Data"]["RootChunk"]
    areas = [n for n in root["nodes"] if n["Data"]["$type"] == "worldAmbientAreaNode"]
    assert len(areas) == 1 and len(root["nodes"]) == 2
    node = areas[0]["Data"]
    settings = node["notifiers"][0]["Data"]["Settings"]["Data"]
    assert settings["Parameters"][0]["name"]["$value"] == "amb_interior"
    outline = node["outline"]["Data"]
    assert outline["height"] == ambience.OUTLINE_HEIGHT and len(outline["points"]) == 4
    ids = []
    def walk(o):
        if isinstance(o, dict):
            if "HandleId" in o:
                ids.append(str(o["HandleId"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(root["nodes"])
    assert len(ids) == len(set(ids)), ids
    area_data = [d for d in root["nodeData"]["Data"] if d["Uk12"] == 1]
    assert len(area_data) == 1
    d = area_data[0]
    assert (d["Uk10"], d["Uk11"], d["Uk12"]) == (1056, 65024, 1)
    # unit square scaled by Scale: half extents 10 x 8, prism height (4.5 - -0.5) * Scale.Z = 5
    assert abs(d["Scale"]["X"] - 10.0) < 1e-6 and abs(d["Scale"]["Y"] - 8.0) < 1e-6
    assert abs(d["Scale"]["Z"] - 2.5) < 1e-6
    # the prism starts at the volume's floor and rises: position z == box z0
    assert abs(d["Position"]["Z"] - 30.0 - -0.5) < 1e-6


def test_baker_toolkit_imports_and_emits_quiet_areas_without_source_tree(tmp_path):
    """The Linux baker gets only BAKER_LIB, not Studio's full package or numpy."""
    import json
    import subprocess
    import tarfile
    from fivem2open77.mlo import toolkit
    archive = tmp_path / "baker-lib.tgz"
    toolkit.build_lib(str(archive), "test-version", log=lambda *_: None)
    with tarfile.open(archive) as tar:
        assert "toolkit/lib/fivem2open77/mlo/ambience.py" in tar.getnames()
        tar.extractall(tmp_path / "unpacked", filter="data")
    lib = tmp_path / "unpacked" / "toolkit" / "lib"
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from fivem2open77.mlo import bake, world; "
        "doc, box = world.sector_doc({'name':'testmap', 'parts':{}, 'placements':[], "
        "'ambience':[{'box':[-10,-8,0,10,8,4]}]}, {'x':0,'y':0,'z':0,'yaw':0}); "
        "print(json.dumps(doc['Data']['RootChunk']['nodes'][0]['Data']))"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(lib)],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    area = json.loads(result.stdout)
    assert area["$type"] == "worldAmbientAreaNode"
    settings = area["notifiers"][0]["Data"]["Settings"]["Data"]
    assert settings["Parameters"][0]["value"] == 1


def test_map_writer_ships_ncwe_imports_within_the_package_budget(tmp_path):
    """End to end through the map writer, offline (no game, no WolvenKit): NCWE imports repacked into
    preload packages, the shipped manifest declaring each one, and Open77's 8-package budget enforced.
    """
    import io
    import zipfile
    from fivem2open77.mlo import build as build_mod
    from fivem2open77.mlo import writer as writer_mod
    from fivem2open77.studio import ncweassets, qa

    def quiet(_msg):
        pass

    # ---- the NCWE imports a user drops in: a zip export, a folder, a loose archive
    assets = tmp_path / "assets"
    (assets / "pack" / "archive" / "pc" / "mod").mkdir(parents=True)
    (assets / "pack" / "archive" / "pc" / "mod" / "extra.archive").write_bytes(_fake_archive())
    (assets / "pack" / "notes.txt").write_text("kept, but not shipped")
    (assets / "solo.archive").write_bytes(_fake_archive())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("MyMod/archive/pc/mod/props.archive", _fake_archive())
    (assets / "props.zip").write_bytes(buf.getvalue())
    prepared, warnings = ncweassets.prepare(str(assets), str(tmp_path / "staging"), log=quiet)
    assert [p["source"] for p in prepared] == ["pack", "props.zip", "solo.archive"], prepared
    assert any(w.startswith("pack:") and "left out" in w for w in warnings), warnings

    # ---- the map the writer ships: one part (no mesh of its own: the writer gives it a placeholder)
    part = build_mod.MapPart(name="body_01", kind="object")
    model = build_mod.MapModel(name="cookiemap", display_name="Cookie", anchor=(0, 0, 0), spawn=None,
                               parts={"body_01": part},
                               placements=[build_mod.Placement(part="body_01", position=(0, 0, 0), yaw=0)])

    class _Wk:
        """Just enough WolvenKit CLI offline: JSON->CR2W mirrored as stubs, pack -> a synthetic
        archive the real normalize_archive accepts."""

        def deserialize_tree(self, jdir, adir, work):
            for dp, _, fns in os.walk(jdir):
                for fn in fns:
                    rel = os.path.relpath(os.path.join(dp, fn), jdir)
                    dst = os.path.join(adir, rel[:-5] if rel.endswith(".json") else rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(b"CR2W" + rel.encode("utf-8"))

        def import_glb(self, rdir, adir):
            pass

        def pack(self, src_root, pdir):
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, os.path.basename(src_root) + ".archive"), "wb") as f:
                f.write(_fake_archive())

    class _Cache:
        """The one donor mesh template the writer clones."""

        def ensure_paths(self, paths):
            pass

        def load(self, path):
            return {"Data": {"RootChunk": {"materialEntries": [], "localMaterialBuffer": {"materials": []},
                                           "appearances": [{"Data": {"chunkMaterials": [], "name": ""}}]}}}

    r = writer_mod.MapWriter(_Wk(), _Cache(), str(tmp_path / "out"), log=quiet, version="1.0.7").write(
        model, extra_packages=prepared)

    # ---- the shipped manifest: the map's package plus one preload package per import
    res = tmp_path / "out" / "cookiemap"
    man = qa.parse_manifest((res / "open77.lua").read_text(encoding="utf-8"))
    assert man["resource"] == "cookiemap"
    assert man["preload_mod"] == ["dist/cookiemap.zip", "dist/cookiemap_ncwe_pack.zip",
                                  "dist/cookiemap_ncwe_props_zip.zip", "dist/cookiemap_ncwe_solo_archive.zip"], man
    assert len(man["preload_mod"]) <= package.MAX_PACKAGES, man
    assert all((res / rel).is_file() for rel in man["preload_mod"]), man
    assert not man["client_scripts"] and any(d.split()[0] == "archivexl" for d in man["dependencies"]), man
    assert r["layout"]["archives"] == ["fivem2open77_map_cookiemap.archive",
                                       "extra.archive", "props.archive", "solo.archive"], r["layout"]
    with zipfile.ZipFile(res / "dist" / "cookiemap_ncwe_props_zip.zip") as z:
        assert z.namelist() == ["archive/pc/mod/props.archive"]

    # ---- the build passes the same QA the Studio runs after every build
    q = qa.check_map_build(str(tmp_path / "out"))
    by = {c["id"]: c for c in q["checks"]}
    assert by["archive:props.archive:imported"]["ok"] and by["archive:solo.archive:imported"]["ok"], by
    assert by["archive:fivem2open77_map_cookiemap.archive:name"]["ok"], by
    assert q["ok"], [c for c in q["checks"] if not c["ok"]]

    # ---- the budget: Open77 allows 8 packages per resource and placing the map adds its sector
    # package, so 1 + 6 imports fit (8 once placed) and 1 + 7 is refused with the reason named
    def extras(n):
        return [{"zip": prepared[i % len(prepared)]["zip"], "source": f"import{i}", "archives": [f"import{i}.archive"]}
                for i in range(n)]

    r7 = writer_mod.MapWriter(_Wk(), _Cache(), str(tmp_path / "out7"), log=quiet).write(model,
                                                                                        extra_packages=extras(6))
    assert len(r7["layout"]["packages"]) == package.MAX_PACKAGES - 1
    try:
        writer_mod.MapWriter(_Wk(), _Cache(), str(tmp_path / "out8"), log=quiet).write(model,
                                                                                       extra_packages=extras(7))
        raise AssertionError("the writer shipped a map that could never be placed")
    except RuntimeError as e:
        msg = str(e)
        assert "7 NCWE asset import(s) leave no room" in msg and f"Open77 allows {package.MAX_PACKAGES}" in msg \
            and "sector package" in msg, msg


def test_install_game_writes_ncwe_imports_and_takes_them_out_again(tmp_path):
    import json
    import zipfile
    from fivem2open77.mlo import deploy as mlo_deploy
    game = tmp_path / "game"
    (game / "archive" / "pc" / "content").mkdir(parents=True)
    mod = game / "archive" / "pc" / "mod"
    mod.mkdir()
    (mod / "unrelated.archive").write_bytes(b"x")
    res = tmp_path / "cookiemap"
    (res / "dist").mkdir(parents=True)

    def zipped(name, members):
        with zipfile.ZipFile(res / "dist" / name, "w") as z:
            for arc, data in members.items():
                z.writestr(arc, data)

    zipped("cookiemap.zip", {"archive/pc/mod/fivem2open77_map_cookiemap.archive": b"map"})
    zipped("cookiemap_ncwe_props_zip.zip", {"archive/pc/mod/props.archive": b"props",
                                            "archive/pc/mod/props.archive.xl": b"xl"})
    layout = {"name": "cookiemap", "packages": ["cookiemap.zip", "cookiemap_ncwe_props_zip.zip"],
              "archives": ["fivem2open77_map_cookiemap.archive", "props.archive"]}
    (res / "layout.json").write_text(json.dumps(layout))
    r = mlo_deploy.install_game(str(res), str(game))
    assert sorted(r["written"]) == ["fivem2open77_map_cookiemap.archive", "props.archive", "props.archive.xl"], r
    assert r["extra"] == ["props.archive", "props.archive.xl"], r
    # an import that is not in the layout is not this map's to write
    zipped("cookiemap_ncwe_props_zip.zip", {"archive/pc/mod/other.archive": b"?"})
    try:
        mlo_deploy.install_game(str(res), str(game), previous=r["extra"])
        raise AssertionError("wrote an archive the map does not declare")
    except RuntimeError as e:
        assert "not one of this map's files" in str(e), e
    # the import dropped from the map: its files leave the game with the next install
    layout["packages"], layout["archives"] = ["cookiemap.zip"], ["fivem2open77_map_cookiemap.archive"]
    (res / "layout.json").write_text(json.dumps(layout))
    r2 = mlo_deploy.install_game(str(res), str(game), previous=r["extra"])
    assert sorted(r2["removed"]) == ["props.archive", "props.archive.xl"] and r2["unchanged"] == [
        "fivem2open77_map_cookiemap.archive"], r2
    r3 = mlo_deploy.install_game(str(res), str(game), uninstall=True)
    assert r3["removed"] == ["fivem2open77_map_cookiemap.archive"]
    assert sorted(os.listdir(mod)) == ["unrelated.archive"]


# ----------------------------------------------------------------------------- big maps
def test_depot_hash_is_wolvenkits_archive_hash():
    assert package.depot_hash("") == 0xCBF29CE484222325 and package.depot_hash("a") == 0xAF63DC4C8601EC8C
    # a file of a map archive WolvenKit 8.17 packed (Liberty build 2, libertyv_manhat02_3.archive)
    p = "fivem2open77\\maps\\libertyv_manhat02\\meshes\\chunk_490_501.mesh"
    assert package.depot_hash(p) == 0xB20BFC1DF070EB7C
    assert package.depot_hash(p.upper()) == package.depot_hash(p.replace("\\", "/")) == 0xB20BFC1DF070EB7C


def _packed_archive(src_root) -> bytes:
    """What WolvenKit's pack makes of a folder, in miniature: every file one zlib segment, indexed by
    its depot hash (u32 table offset, u32 table size, u64 crc, then files, segments)."""
    import zlib
    blobs = []
    for dp, _, fns in os.walk(src_root):
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            depot = os.path.relpath(full, src_root).replace(os.sep, "\\")
            with open(full, "rb") as f:
                raw = f.read()
            blobs.append((package.depot_hash(depot), raw, zlib.compress(raw, 9)))
    blobs.sort()
    data, segs, entries = bytearray(), bytearray(), bytearray()
    for i, (h, raw, z) in enumerate(blobs):
        segs += struct.pack("<QII", 40 + len(data), len(z), len(raw))
        entries += struct.pack("<Qq5I20s", h, 0, 0, i, i + 1, 0, 0, b"\0" * 20)
        data += z
    table = struct.pack("<III", len(blobs), len(blobs), 0) + bytes(entries) + bytes(segs)
    index_pos = 40 + len(data)
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, index_pos, len(table) + 16, 0, 0, 0)
    return head + bytes(data) + struct.pack("<IIQ", 8, len(table) + 8, package._crc64_xz(table)) + table


def _archive_hashes(data: bytes) -> list:
    import io
    files, _segs = package._read_index(io.BytesIO(data))
    return [h for h, _, _ in files]


def _big_map_writer(tmp_path, nparts, raw_bytes):
    from fivem2open77.mlo import build as build_mod
    from fivem2open77.mlo import writer as writer_mod

    class _Wk:
        packs = 0

        def deserialize_tree(self, jdir, adir, work):
            for dp, _, fns in os.walk(jdir):
                for fn in fns:
                    rel = os.path.relpath(os.path.join(dp, fn), jdir)
                    dst = os.path.join(adir, rel[:-5] if rel.endswith(".json") else rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:   # big on disk, tiny once packed (like the real CR2W files)
                        f.write(b"CR2W" + rel.encode("utf-8") + b"\0" * raw_bytes)

        def import_glb(self, rdir, adir):
            pass

        def pack(self, src_root, pdir):
            _Wk.packs += 1
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, os.path.basename(src_root) + ".archive"), "wb") as f:
                f.write(_packed_archive(src_root))

    class _Cache:
        def ensure_paths(self, paths):
            pass

        def load(self, path):
            return {"Data": {"RootChunk": {"materialEntries": [], "localMaterialBuffer": {"materials": []},
                                           "appearances": [{"Data": {"chunkMaterials": [], "name": ""}}]}}}

    parts = {f"chunk_{i:03d}": build_mod.MapPart(name=f"chunk_{i:03d}", kind="object") for i in range(nparts)}
    model = build_mod.MapModel(name="bigcity", display_name="Big City", anchor=(0, 0, 0), spawn=None, parts=parts,
                               placements=[build_mod.Placement(part=p, position=(0, 0, 0), yaw=0) for p in parts])
    logs = []
    return writer_mod.MapWriter(_Wk(), _Cache(), str(tmp_path / "out"), log=logs.append), model, logs, _Wk


def test_a_big_map_is_regrouped_by_packed_size_and_keeps_the_sector_slot(tmp_path, monkeypatch):
    """A map that needs more than 7 archives by raw size (Liberty City: 8 by raw size, 208 MiB once
    packed) is measured and regrouped, so it ships in fewer packages and can still be placed."""
    import zipfile
    from fivem2open77.mlo import writer as writer_mod
    monkeypatch.setattr(writer_mod, "ARCHIVE_RAW_BUDGET", 6000)     # ~1 part (mesh + ent) per archive by raw size
    monkeypatch.setattr(writer_mod, "PACKED_BUDGET", 2000)          # ~12 files per archive once measured
    w, model, logs, wk = _big_map_writer(tmp_path, nparts=40, raw_bytes=2500)
    r = w.write(model)
    pkgs = r["layout"]["packages"]
    assert 1 < len(pkgs) <= package.MAX_PACKAGES - 1, pkgs
    assert wk.packs > 2 * len(pkgs), wk.packs                       # packed twice: by raw size, then measured
    assert any("regrouped into" in m for m in logs), logs
    res = tmp_path / "out" / "bigcity"
    seen = []
    for z in pkgs:
        assert (res / "dist" / z).stat().st_size <= package.MAX_PACKAGE
        with zipfile.ZipFile(res / "dist" / z) as zf:
            (arc,) = zf.namelist()
            seen += _archive_hashes(zf.read(arc))
    want = sorted(package.depot_hash(m) for p in r["layout"]["parts"].values() for m in (p["mesh"], p["ent"]))
    assert sorted(seen) == want                                     # every file shipped, exactly once
    assert len(pkgs) + 1 <= package.MAX_PACKAGES                    # what bake.py checks before placing it
    # the map's files are still found by the QA every Studio build runs
    from fivem2open77.studio import qa
    q = qa.check_map_build(str(tmp_path / "out"))
    assert not [c for c in q["checks"] if not c["ok"] and c["level"] == "error"], q["checks"]


def test_a_map_too_big_even_packed_is_refused_with_the_way_out(tmp_path, monkeypatch):
    from fivem2open77.mlo import writer as writer_mod
    monkeypatch.setattr(writer_mod, "ARCHIVE_RAW_BUDGET", 6000)
    monkeypatch.setattr(writer_mod, "PACKED_BUDGET", 300)           # ~2 files per archive: 40 parts need 40
    w, model, _logs, _wk = _big_map_writer(tmp_path, nparts=40, raw_bytes=2500)
    try:
        w.write(model)
        raise AssertionError("the writer shipped more packages than Open77 allows")
    except RuntimeError as e:
        msg = str(e)
        assert "packed" in msg and f"Open77 allows {package.MAX_PACKAGES}" in msg and "smaller area" in msg, msg


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
