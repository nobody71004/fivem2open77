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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
