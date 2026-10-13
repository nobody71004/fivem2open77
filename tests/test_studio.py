"""Studio checks: profile rules, engine-sound mapping, texture decoding, the HTTP API (auth,
settings, servers, projects), the AI agent loop with a scripted model, and the Claude / Ollama
request formats against fake API servers.  The car-specific parts use the i8 test car when it
is present (F2O77_TEST_CAR, default /opt/f2o77/input/i8rpncs.rar) and skip otherwise."""
import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77 import profile as P  # noqa: E402
from fivem2open77.cp77 import sound as S  # noqa: E402
from fivem2open77.cp77.donor import DONORS  # noqa: E402

CAR = os.environ.get("F2O77_TEST_CAR", "/opt/f2o77/input/i8rpncs.rar")


# ----------------------------------------------------------------------------- profile
def test_profile_defaults_and_merge():
    p = P.normalize({}, donors=DONORS, sounds=S.CATALOGUE)
    assert p["parts"]["lids"] == "auto" and p["sound"]["engine"] == "auto" and p["handling"]["multipliers"]["torque"] == 1.0
    m = P.merge({"parts": {"bone_roles": {"a": "skip", "b": "body"}}}, {"parts": {"bone_roles": {"a": None, "c": "door:fl"}}})
    assert m["parts"]["bone_roles"] == {"b": "body", "c": "door:fl"}
    m = P.merge({"handling": {"mass": 1500}}, {"handling": {"mass": None}})
    assert m["handling"]["mass"] is None


def test_profile_rejects_bad_values():
    bad = {"name": "1bad", "parts": {"lids": "front", "bone_roles": {"x": "wing"}}, "handling": {"gears": 9,
           "multipliers": {"torque": 10}}, "sound": {"engine": "v_car_unknown"}, "paint": {"primary": "red"},
           "chime": {"enabled": True}, "bogus": 1}
    try:
        P.normalize(bad, donors=DONORS, sounds=S.CATALOGUE)
    except P.ProfileError as e:
        text = " | ".join(e.errors)
        for needle in ("unknown settings: bogus", "name:", "parts.lids", "parts.bone_roles.x", "handling.gears",
                       "handling.multipliers.torque", "sound.engine", "paint.primary", "chime.enabled needs chime.file"):
            assert needle in text, (needle, text)
    else:
        raise AssertionError("bad profile accepted")


def test_colors():
    assert P.parse_color("#ff8000") == (1.0, 128 / 255, 0.0)
    assert P.parse_color([255, 0, 0]) == (1.0, 0.0, 0.0)
    assert P.parse_color([0.5, 0.25, 0.0]) == (0.5, 0.25, 0.0)
    assert P.parse_color("red") is None
    assert P.to_hex((1.0, 0.5, 0.0)) == "#ff8000"


# ----------------------------------------------------------------------------- engine sound
class _V:
    def __init__(self, audio, cls="VC_SPORT"):
        self.audio_name, self.gta_class = audio, cls


def test_engine_sound_mapping():
    donor = DONORS["porsche_911"]["sound"]
    cases = {("ELEGY", "VC_SPORT"): "v_car_mizutani_shion", ("ZENTORNO", "VC_SUPER"): "v_car_quadra_turbo_r",
             ("ADDER", "VC_SUPER"): "v_car_herrera_outlaw", ("lambov10", "VC_SUPER"): "v_car_quadra_turbo_r",
             ("rb26dett", "VC_SPORT"): "v_car_mizutani_shion", ("s55b30", "VC_SPORT"): "v_car_chevalier_thrax",
             ("teslapd", "VC_SEDAN"): "v_car_rayfield_aerondight", ("DOMINATOR", "VC_MUSCLE"): "v_car_quadra_type_66",
             ("", "VC_SUV"): "v_car_mahir_supron", ("weirdname", "VC_OFF_ROAD"): "v_car_thorton_mackinaw",
             ("custom_audio", "VC_SEDAN"): "v_car_villefort_cortes"}
    for (audio, cls), want in cases.items():
        r = S.resolve("auto", _V(audio, cls), donor)
        assert r["player"] == want, (audio, cls, r)
        assert r["traffic"] == S.CATALOGUE[want]["traffic"]
    r = S.resolve("auto", _V("", "VC_UNKNOWN"), donor)
    assert r["player"] == donor and not r["override"]
    r = S.resolve("donor", _V("ELEGY"), donor)
    assert not r["override"]
    r = S.resolve("v_car_villefort_alvarado", _V("ELEGY"), donor)
    assert r["player"] == "v_car_villefort_alvarado" and r["override"]
    for name in S.BY_CHARACTER.values():
        assert name in S.CATALOGUE, name


def test_engine_sound_in_tweak():
    from fivem2open77.cp77.tweak import build_tweak

    class Axle:
        radius, rim_radius, width = 0.35, 0.25, 0.25
    v = type("V", (), {"handling": None, "axles": {"front": Axle(), "back": Axle()}, "bbox": ((-1, -2.3, 0), (1, 2.3, 1.3)),
                       "display_name": "Test", "audio_name": "ELEGY", "gta_class": "VC_SPORT"})()
    snd = S.resolve("auto", v, DONORS["porsche_911"]["sound"])
    text, hp = build_tweak(v, DONORS["porsche_911"], record="Vehicle.t", ent_path="x.ent", appearance="t", name_key="k",
                           handling={"drive": "awd", "gears": 5, "mass": 1500, "steer": 45,
                                     "multipliers": {"torque": 1.5, "top_speed": 1.0, "brake": 1, "grip": 1, "spring": 1, "drag": 1}},
                           sound=snd)
    assert "  player_audio_resource: v_car_mizutani_shion\n" in text
    assert "  traffic_audio_resource: v_car_mizutani_shion_traffic\n" in text
    assert hp["layout"] == "awd" and hp["gears"] == 5 and hp["mass"] == 1500 and hp["steer"] == 45
    text2, hp2 = build_tweak(v, DONORS["porsche_911"], record="Vehicle.t", ent_path="x.ent", appearance="t", name_key="k")
    assert "player_audio_resource" not in text2
    assert abs(hp["torque"] / hp2["torque"] - 1.5 * 1500 / 1290 / (hp2["mass"] / 1290)) < 1e-6


# ----------------------------------------------------------------------------- texture decoding
def test_bc_decoders_match_pillow():
    try:
        from PIL import Image
    except ImportError:
        print("   (skipped: no Pillow)")
        return
    import io
    from fivem2open77.gta.texture import FMT_DXT1, FMT_DXT5, GtaTexture
    from fivem2open77.imaging import decode
    rng = np.random.default_rng(7)
    for fmt, bpb in ((FMT_DXT1, 8), (FMT_DXT5, 16)):
        w = h = 32
        data = rng.integers(0, 256, (w // 4) * (h // 4) * bpb, dtype=np.uint8).tobytes()
        tex = GtaTexture(name="t", width=w, height=h, levels=1, format=fmt, stride=0, usage=0, data=data)
        mine = decode(fmt, w, h, data)
        ref = np.asarray(Image.open(io.BytesIO(tex.to_dds())).convert("RGBA"))
        diff = np.abs(mine.astype(int) - ref.astype(int))
        assert diff[..., :3].max() <= 2, (fmt, diff.max())
        if fmt == FMT_DXT5:
            assert diff[..., 3].max() <= 2


def test_png_writer_roundtrip():
    from fivem2open77.imaging import png_bytes
    a = (np.arange(4 * 5 * 4) % 256).astype(np.uint8).reshape(4, 5, 4)
    png = png_bytes(a)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    try:
        from PIL import Image
        import io
        assert np.array_equal(np.asarray(Image.open(io.BytesIO(png))), a)
    except ImportError:
        pass


# ----------------------------------------------------------------------------- HTTP API
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Studio:
    def __init__(self):
        from fivem2open77.studio import server as srv
        self.ws = tempfile.mkdtemp(prefix="f2o77-ws-")
        self.port = _free_port()
        self.token = "t" * 20
        self.thread = threading.Thread(target=srv.serve, kwargs=dict(port=self.port, workspace=self.ws, token=self.token),
                                       daemon=True)
        self.thread.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.05)

    def req(self, method, path, body=None, headers=None, token=True, raw=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-F2O77-Token"] = self.token
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                b = resp.read()
                return resp.status, (json.loads(b) if "json" in resp.headers.get("Content-Type", "") else b)
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except ValueError:
                return e.code, b


def test_api_auth_settings_servers():
    st = _Studio()
    try:
        assert st.req("GET", "/api/state", token=False)[0] == 401
        assert st.req("GET", "/api/state", headers={"Host": "evil.example:80"})[0] == 403
        code, _ = st.req("PATCH", "/api/settings", {"ai": {"autonomy": "full"}}, headers={"Origin": "http://evil.example"})
        assert code == 403
        code, s = st.req("PATCH", "/api/settings", {"ai": {"autonomy": "auto_safe"}})
        assert code == 200 and s["ai"]["autonomy"] == "auto_safe"
        code, s = st.req("PATCH", "/api/settings", {"ai": {"autonomy": "yolo"}})
        assert code == 400 and "ai.autonomy" in " ".join(s["errors"])
        # secrets: stored, never returned
        assert st.req("POST", "/api/secrets", {"key": "anthropic_api_key", "value": "sk-test-123"})[0] == 200
        code, s = st.req("GET", "/api/settings")
        assert s["ai"]["anthropic_key_set"] is True and "sk-test-123" not in json.dumps(s)
        assert st.req("POST", "/api/secrets", {"key": "../evil", "value": "x"})[0] == 400
        # servers: strict validation (fields reach a shell on the host)
        good = {"id": "vm", "label": "VM", "host": "10.0.0.5", "user": "op77", "auth": "password", "password": "pw",
                "servers_root": "/home/op77", "names": ["open77-xbuniverse"], "systemctl": "systemctl --user"}
        code, s = st.req("POST", "/api/servers", good)
        assert code == 200 and s["password_set"] and "pw" not in json.dumps(s)
        for k, v in (("names", ["x; rm -rf /"]), ("servers_root", "/opt;id"), ("host", "a b"), ("systemctl", "sudo x"), ("user", "Root!")):
            code, s = st.req("POST", "/api/servers", dict(good, **{k: v}))
            assert code == 400, (k, v, s)
        code, s = st.req("GET", "/api/state")
        assert [x["id"] for x in s["settings"]["servers"]] == ["vm"]
        assert st.req("DELETE", "/api/servers/vm")[0] == 200
        # profile schema and sounds
        code, s = st.req("GET", "/api/profile/schema")
        assert "door:fl" in s["roles"]
        code, s = st.req("GET", "/api/sounds")
        assert any(x["name"] == "v_car_porsche_911_turbo" for x in s["sounds"])
        # static UI and path traversal
        code, body = st.req("GET", "/", token=False)
        assert code == 200 and b"fivem2open77 Studio" in body
        assert b'/css/universe.css' in body and b'data-nav="home"' in body
        code, body = st.req("GET", "/js/pages/home.js", token=False)
        assert code == 200 and b"homePage" in body and b"prefers-reduced-motion" in body
        code, body = st.req("GET", "/css/universe.css", token=False)
        assert code == 200 and b".featured-grid" in body
        code, s = st.req("PATCH", "/api/settings", {"paths": {"ncwe_mcp": "C:/Games/NCWE/NCWE.Mcp.exe"},
                                                     "mcp": {"ncwe_launch": False}})
        assert code == 200 and s["paths"]["ncwe_mcp"].endswith("NCWE.Mcp.exe") and not s["mcp"]["ncwe_launch"]
        assert st.req("GET", "/../settings.json", token=False)[0] == 404
        assert st.req("GET", "/js/../../server.py", token=False)[0] == 404
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_api_keepalive_with_unread_body():
    """A POST whose endpoint ignores the body must not corrupt the next request on the connection."""
    import http.client
    st = _Studio()
    try:
        c = http.client.HTTPConnection("127.0.0.1", st.port, timeout=30)
        h = {"X-F2O77-Token": st.token, "Content-Type": "application/json"}
        c.request("POST", "/api/tools/donor", body=b"{}", headers=h)   # the endpoint reads no body
        r = c.getresponse()
        r.read()
        assert r.status in (200, 400), r.status
        c.request("GET", "/api/health", headers=h)
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read())["ok"], r.status
        c.close()
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_api_project_flow():
    if not os.path.isfile(CAR):
        print("   (skipped: no test car)")
        return
    st = _Studio()
    try:
        code, r = st.req("POST", "/api/projects", {"path": CAR})
        assert code == 200, r
        pid, jid = r["project"]["id"], r["job"]["id"]
        for _ in range(600):
            code, j = st.req("GET", f"/api/jobs/{jid}")
            if j["state"] not in ("queued", "running"):
                break
            time.sleep(0.2)
        assert j["state"] == "done", j
        code, ins = st.req("GET", f"/api/projects/{pid}/inspect")
        assert ins["model"] and ins["parts"] and ins["bones"]
        code, glb = st.req("GET", f"/api/projects/{pid}/preview.glb")
        assert code == 200 and glb[:4] == b"glTF"
        code, png = st.req("GET", f"/api/projects/{pid}/render.png?view=left&mode=parts&w=320&h=200")
        assert code == 200 and png[:4] == b"\x89PNG"
        code, r = st.req("PATCH", f"/api/projects/{pid}/profile", {"display_name": "Test Car", "sound": {"engine": "v_car_quadra_type_66"}})
        assert code == 200 and r["normalized"]["display_name"] == "Test Car"
        code, ins2 = st.req("GET", f"/api/projects/{pid}/inspect")
        assert ins2["display_name"] == "Test Car" and ins2["sound"]["player"] == "v_car_quadra_type_66"
        code, r = st.req("PATCH", f"/api/projects/{pid}/profile", {"handling": {"gears": 12}})
        assert code == 400 and "handling.gears" in " ".join(r["errors"])
        code, hp = st.req("POST", f"/api/projects/{pid}/handling/preview", {"drive": "fwd", "multipliers": {"top_speed": 1.5}})
        assert code == 200 and hp["layout"] == "fwd"
        # chime upload: a tiny valid WAV
        import struct
        pcm = b"\0\0" * 8000
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)
        code, r = st.req("POST", f"/api/projects/{pid}/chime?name=beep.wav", raw=wav, headers={"Content-Type": "application/octet-stream"})
        assert code == 200 and abs(r["seconds"] - 1.0) < 0.01 and r["profile"]["chime"]["enabled"]
        code, r = st.req("POST", f"/api/projects/{pid}/chime?name=x.exe", raw=b"MZ", headers={"Content-Type": "application/octet-stream"})
        assert code == 400
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_api_ncwe_assets_import_flow():
    """NCWE asset imports on a map project: raw upload, list, remove, and the path rules."""
    import io
    import zipfile as _zip
    st = _Studio()
    try:
        pid = "a1b2c3d4e5f6"
        d = os.path.join(st.ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": "map", "name": "cookiemap", "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [], "build_counter": 0, "deployments": []}, f)
        buf = io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            z.writestr("MyMod/archive/pc/mod/myprops.archive", b"RDAR")
        raw, up = buf.getvalue(), {"Content-Type": "application/octet-stream"}
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=props.zip", raw=raw, headers=up)
        assert code == 200 and r["asset"]["archives"] == 1 and r["asset"]["ready"], r
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets")
        assert code == 200 and [a["name"] for a in r["assets"]] == ["props.zip"], r
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=..%2Fevil.zip", raw=raw, headers=up)
        assert code == 400, r
        code, r = st.req("DELETE", f"/api/projects/{pid}/ncwe_assets/props.zip")
        assert code == 200 and r["removed"] == "props.zip", r
        code, r = st.req("DELETE", f"/api/projects/{pid}/ncwe_assets/props.zip")
        assert code == 400, r
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_api_ncwe_import_flow(monkeypatch):
    """A converted map into NCWE: the job exports a real map glb and calls import_model +
    place_objects with the map's Night City spot (the conversion and the NCWE stdio calls are the
    two seams faked here; the calls themselves are covered for real in test_ncwe.py)."""
    import numpy as np
    from fivem2open77.mlo import build as build_mod
    from fivem2open77.model.ir import SubMesh
    from fivem2open77.studio import maps as maps_mod
    from fivem2open77.studio.agent import ncwe
    st = _Studio()
    try:
        pid = "b1c2d3e4f5a6"
        d = os.path.join(st.ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": "map", "name": "cookiemap", "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [], "build_counter": 0, "deployments": []}, f)
        with open(os.path.join(d, "map.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "cookiemap", "display_name": "Cookie",
                       "place": {"x": 10, "y": 20, "z": 30, "yaw": 90}}, f)
        sm = SubMesh(material="m", positions=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32),
                     normals=np.tile([[0, 0, 1.0]], (3, 1)).astype(np.float32),
                     tangents=np.tile([[1.0, 0, 0, 1.0]], (3, 1)).astype(np.float32),
                     uv0=np.zeros((3, 2), np.float32), uv1=None, colors=None,
                     indices=np.array([0, 1, 2], np.uint32), lod=1)
        model = build_mod.MapModel(name="cookiemap", display_name="Cookie", anchor=(0, 0, 0), spawn=None,
                                   parts={"body_01": build_mod.MapPart(name="body_01", kind="object", submeshes=[sm])},
                                   placements=[build_mod.Placement(part="body_01", position=(0, 0, 0), yaw=0)])
        monkeypatch.setattr(maps_mod.MapLook, "model",
                            lambda self, opts=None, log=None, check=None: (None, model, None, "cookiemap"))
        calls = []

        def fake_call(ctx, name, args):
            calls.append((name, args))
            reply = {"mesh": {"asset": "base\\custom\\cookiemap.mesh"}} if name == "import_model" else {"ids": [42]}
            return [{"type": "text", "text": json.dumps(reply)}]

        monkeypatch.setattr(ncwe, "call", fake_call)

        def run_import(body):
            code, job = st.req("POST", f"/api/projects/{pid}/ncwe_import", body=body)
            assert code == 200, job
            for _ in range(200):
                code, j = st.req("GET", f"/api/jobs/{job['id']}")
                if j.get("state") in ("done", "failed", "cancelled"):
                    return j
                time.sleep(0.05)
            raise AssertionError(f"the ncwe import job did not finish: {j}")

        j = run_import({})
        assert j["state"] == "done", j
        r = j["result"]
        assert r["asset"] == "base\\custom\\cookiemap.mesh" and r["placed"] == [42], r
        assert r["position"] == [10, 20, 30], r
        assert os.path.isfile(r["glb"]) and open(r["glb"], "rb").read(4) == b"glTF", r
        assert [c[0] for c in calls] == ["import_model", "place_objects"], calls
        assert calls[0][1]["name"] == "Cookie" and calls[0][1]["up_axis"] == "Y", calls
        assert calls[1][1]["items"][0]["position"] == [10.0, 20.0, 30.0], calls
        assert calls[1][1]["items"][0]["rotation"] == {"yaw": 90.0}, calls
        # no spot and no position: into the custom mesh library only, with a clear note
        with open(os.path.join(d, "map.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "cookiemap", "display_name": "Cookie"}, f)
        j = run_import({})
        assert j["state"] == "done", j
        assert j["result"]["placed"] == [] and "not placed" in j["result"]["note"], j
        assert [c[0] for c in calls] == ["import_model", "place_objects", "import_model"], calls
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_api_ncwe_assets_contents_and_collisions():
    """The assets tab's data: what an import holds (archives, meshes, props) and name collisions."""
    import io
    import struct as _struct
    import zipfile as _zip
    from fivem2open77.cp77 import package as package_mod
    st = _Studio()

    def fake_archive(entries=2):
        table = _struct.pack("<III", entries, 0, 0)
        for i in range(entries):
            table += _struct.pack("<Qq5I20s", 1000 + i, 123 + i, 0, 0, 0, 0, 0, b"\0" * 20)
        index_pos = 44
        head = _struct.pack("<4sIQIQIQ", b"RDAR", 12, index_pos, len(table) + 16, 0, 0, 0) + b"\0\0\0\0"
        index = _struct.pack("<IIQ", 8, len(table) + 8, package_mod._crc64_xz(bytes(table))) + table
        return head + index

    def zipped(files):
        buf = io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            for arc, data in files.items():
                z.writestr(arc, data)
        return buf.getvalue()

    try:
        pid = "a1b2c3d4e5f6"
        d = os.path.join(st.ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": "map", "name": "cookiemap", "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [], "build_counter": 0, "deployments": []}, f)
        up = {"Content-Type": "application/octet-stream"}
        raw = zipped({
            "MyMod/archive/pc/mod/props.archive": fake_archive(),
            "MyMod/archive/pc/mod/props.archive.xl": json.dumps(
                {"props.archive": ["base\\meshes\\tree.mesh", "base\\entities\\lamp.ent"]}),
            "MyMod/README.md": "hi",
        })
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=props.zip", raw=raw, headers=up)
        assert code == 200 and r["asset"]["archive_names"] == ["props.archive"], r
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=other.zip",
                         raw=zipped({"Other/archive/pc/mod/props.archive": fake_archive()}), headers=up)
        assert code == 200, r
        # the list the tab shows: archive names per import and the collision to fix before building
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets")
        assert code == 200, r
        assert [a["name"] for a in r["assets"]] == ["other.zip", "props.zip"], r
        assert r["assets"][1]["archive_names"] == ["props.archive"], r
        assert len(r["collisions"]) == 1 and "props.archive" in r["collisions"][0]["problem"], r
        assert "other.zip, props.zip" in r["collisions"][0]["problem"], r
        # the contents view: entry counts and the meshes/props the .archive.xl lists
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets/props.zip")
        assert code == 200, r
        c = r["contents"]
        a = c["archives"][0]
        assert a["name"] == "props.archive" and a["entries"] == 2, c
        assert [x["kind"] for x in a["resources"]] == ["mesh", "prop"], c
        assert [f["path"] for f in c["files"]] == ["MyMod/README.md"], c
        assert c["totals"]["meshes"] == 1 and c["totals"]["props"] == 1, c
        # an unreadable archive reports the problem instead of failing the view
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=bad.zip",
                         raw=zipped({"x/archive/pc/mod/bad.archive": b"NOPE" + b"\0" * 20}), headers=up)
        assert code == 200, r
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets/bad.zip")
        assert code == 200 and r["contents"]["archives"][0]["entries"] is None, r
        assert "RDAR" in r["contents"]["archives"][0]["error"], r
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_map_stale_and_rebuild_deploy():
    """The last build goes stale when imports change; rebuild-and-deploy chains build -> deploy."""
    import io
    from fivem2open77.studio import ncweassets
    from fivem2open77.studio.projects import ProjectError
    studio, ws, _ = _agent_studio()
    try:
        pid = "a1b2c3d4e5f6"
        d = os.path.join(ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": "map", "name": "cookiemap", "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [{"n": 1, "ok": True, "kind": "map", "version": "1.0.1", "imports": "bogus"}],
                       "build_counter": 1,
                       "deployments": [{"time": 1.0, "server": "local", "names": ["cookiemap"], "build": 1, "kind": "map"}]}, f)
        p = studio.projects.get(pid)
        ncweassets.add_import(p, "props.zip", io.BytesIO(b"x" * 10), 10)     # imports changed after build #1
        st = studio.map_stale(p)
        assert st["reason"] == "NCWE imports changed since this build", st
        assert st["target"]["server"] == "local" and st["target"]["names"] == ["cookiemap"], st
        # the one-click flow: rebuild, then deploy that fresh build to where the map lives
        calls, studio._server = {}, lambda sid: {"id": sid, "label": "Local server", "names": ["cookiemap"]}

        def fake_build(pid_):
            calls["build"] = pid_
            return studio.jobs.submit("build", "build", lambda job: {"n": 7, "ok": True, "version": "1.0.7"}, project=pid_)

        def fake_deploy(pid_, sid, **kw):
            calls["deploy"] = (pid_, sid, kw)
            return studio.jobs.submit("deploy", "deploy", lambda job: {"names": ["cookiemap"]}, project=pid_)

        studio.map_build, studio.map_deploy = fake_build, fake_deploy
        job = studio.map_rebuild_deploy(pid)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "done", job.error
        assert job.result["build"]["n"] == 7, job.result
        assert job.result["deployed"] == {"server": "local", "label": "Local server", "names": ["cookiemap"]}, job.result
        assert calls["deploy"][1] == "local" and calls["deploy"][2]["build"] == 7, calls
        assert calls["deploy"][2]["names"] == ["cookiemap"], calls          # the last deployment's folders
        # a failed build stops the flow before anything is deployed
        def failing_build(pid_):
            def run(job):
                raise RuntimeError("boom")
            return studio.jobs.submit("build", "build", run, project=pid_)

        studio.map_build = failing_build
        calls.clear()
        job = studio.map_rebuild_deploy(pid)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "failed" and "the build did not finish" in job.error, (job.state, job.error)
        assert "deploy" not in calls, calls
        # the agent's deploy_map rebuild flag runs the same one-click flow
        from types import SimpleNamespace
        from fivem2open77.studio.agent import maptools
        from fivem2open77.studio.agent.tools import Context
        studio.map_build = fake_build
        calls.clear()
        ctx = Context(studio, SimpleNamespace(project=pid, note=lambda m: None, attach_job=lambda j: None,
                                              stopping=False))
        out = json.loads(maptools.t_deploy_map(ctx, {"server": "local", "rebuild": True})[0]["text"])
        assert out["build"]["n"] == 7 and out["deployed"]["names"] == ["cookiemap"], out
        assert calls["deploy"][2]["build"] == 7, calls
        # a map that lives nowhere has no one-click target
        p.data["deployments"] = []
        p.save()
        try:
            studio.map_rebuild_deploy(pid)
            raise AssertionError("accepted a map that is not on a server")
        except ProjectError as e:
            assert "not on a server yet" in str(e), e
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_api_stale_and_rebuild_deploy():
    """What the tabs read: the last build is marked stale, and the one-click endpoint chains the flow."""
    from fivem2open77.studio.mapops import MapOps
    st = _Studio()
    try:
        pid = "a1b2c3d4e5f6"
        d = os.path.join(st.ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": "map", "name": "cookiemap", "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [{"n": 1, "ok": True, "kind": "map", "version": "1.0.1"}],
                       "build_counter": 1, "deployments": []}, f)
        # nothing was imported after the build: not stale
        code, r = st.req("GET", f"/api/projects/{pid}/builds")
        assert code == 200 and r[0]["stale"] is None, r
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets")
        assert r["build_stale"] is None, r
        # an import appears: the last build is marked stale (no server has it yet)
        code, r = st.req("POST", f"/api/projects/{pid}/ncwe_assets?name=props.zip",
                         raw=b"x" * 10, headers={"Content-Type": "application/octet-stream"})
        assert code == 200, r
        code, r = st.req("GET", f"/api/projects/{pid}/builds")
        assert r[0]["stale"]["reason"] == "NCWE imports changed since this build", r
        assert r[0]["stale"]["target"] is None, r
        code, r = st.req("GET", f"/api/projects/{pid}/ncwe_assets")
        assert r["build_stale"]["reason"] == "NCWE imports changed since this build", r
        # the one-click endpoint reaches the flow with the body's target
        got = {}

        def fake(self, pid_, sid=None, **kw):
            got.update(pid=pid_, sid=sid, **kw)
            return self.jobs.submit("rebuild-deploy", "Rebuild and deploy", lambda job: {"ok": True}, project=pid_)

        MapOps.map_rebuild_deploy, was = fake, MapOps.map_rebuild_deploy
        try:
            code, r = st.req("POST", f"/api/projects/{pid}/rebuild_deploy",
                             body={"server": "local", "names": ["cookiemap"]})
            assert code == 200 and r["kind"] == "rebuild-deploy", r
            assert got == {"pid": pid, "sid": "local", "names": ["cookiemap"],
                           "keep_placement": True, "restart": True, "baker": True}, got
        finally:
            MapOps.map_rebuild_deploy = was
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


def test_agent_ncwe_assets_tools():
    """ncwe_assets_*: import an NCWE export on a map project, list it, dry-run the packaging."""
    import io
    import zipfile as _zip
    from types import SimpleNamespace
    from fivem2open77.studio.agent.tools import Context, ToolError, registry
    studio, ws, _ = _agent_studio()

    def project(pid, kind):
        d = os.path.join(ws, "projects", pid)
        os.makedirs(d)
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "kind": kind, "name": pid, "created": 1.0, "updated": 1.0,
                       "source": {"file": "m", "original": "m", "hash": "x"},
                       "builds": [], "build_counter": 0, "deployments": []}, f)
        return Context(studio, SimpleNamespace(project=pid, note=lambda m: None))

    try:
        ctx = project("a1b2c3d4e5f6", "map")
        tools = registry()
        # a zip export carrying one game archive, imported under its own name
        buf = io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            z.writestr("MyMod/archive/pc/mod/myprops.archive", b"RDAR")
        src = os.path.join(ws, "props.zip")
        with open(src, "wb") as f:
            f.write(buf.getvalue())
        out = json.loads(tools["ncwe_assets_import"].fn(ctx, {"path": src})[0]["text"])
        assert out["imported"]["name"] == "props.zip" and out["imported"]["archives"] == 1, out
        # a folder keeps its name: the game archive and the side file both go in
        fold = os.path.join(ws, "drop")
        os.makedirs(os.path.join(fold, "archive", "pc", "mod"))
        with open(os.path.join(fold, "archive", "pc", "mod", "extra.archive"), "wb") as f:
            f.write(b"RDAR2")
        with open(os.path.join(fold, "readme.txt"), "w", encoding="utf-8") as f:
            f.write("notes")
        out = json.loads(tools["ncwe_assets_import"].fn(ctx, {"path": fold})[0]["text"])
        assert out["imported"]["name"] == "drop" and out["imported"]["archives"] == 1, out
        # list shows both
        out = json.loads(tools["ncwe_assets_list"].fn(ctx, {})[0]["text"])
        assert sorted(i["name"] for i in out["imports"]) == ["drop", "props.zip"], out
        # verify dry-runs the build's packaging: two preload packages, the txt left out with a note
        out = json.loads(tools["ncwe_assets_verify"].fn(ctx, {})[0]["text"])
        assert out["preload_packages"] == 2, out
        assert {q["source"] for q in out["packages"]} == {"drop", "props.zip"}, out
        assert any("drop" in w and "left out" in w for w in out["warnings"]), out
        # a colliding archive name is called out before building
        buf = io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            z.writestr("OtherMod/archive/pc/mod/myprops.archive", b"RDAR")
        dup = os.path.join(ws, "dup.zip")
        with open(dup, "wb") as f:
            f.write(buf.getvalue())
        out = json.loads(tools["ncwe_assets_import"].fn(ctx, {"path": dup})[0]["text"])
        assert any("myprops.archive" in c["problem"] for c in out["collisions"]), out
        out = json.loads(tools["ncwe_assets_verify"].fn(ctx, {})[0]["text"])
        assert any("myprops.archive" in w and "rename one before building" in w for w in out["warnings"]), out
        # a path that is not there is refused with a clear error
        try:
            tools["ncwe_assets_import"].fn(ctx, {"path": os.path.join(ws, "missing.zip")})
            raise AssertionError("missing path accepted")
        except ToolError as e:
            assert "no such file" in str(e), e
        # a car project cannot take asset imports
        cctx = project("b1b2c3d4e5f6", "car")
        try:
            tools["ncwe_assets_list"].fn(cctx, {})
            raise AssertionError("car project accepted asset imports")
        except ToolError as e:
            assert "open_project" in str(e), e
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ----------------------------------------------------------------------------- agent loop
def _agent_studio():
    from fivem2open77.studio.app import Studio
    from fivem2open77.studio.agent.manager import AgentManager
    ws = tempfile.mkdtemp(prefix="f2o77-agent-")
    studio = Studio(ws)
    return studio, ws, AgentManager


def _wait(cond, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_agent_loop_approvals_and_questions():
    from fivem2open77.studio.agent.providers import ScriptedProvider
    studio, ws, AgentManager = _agent_studio()
    try:
        prov = ScriptedProvider([
            [{"type": "text", "text": "Looking at the sounds."}, {"type": "tool_call", "name": "list_engine_sounds", "input": {"kind": "electric"}}],
            [{"type": "tool_call", "name": "ask_user", "input": {"question": "Which server?"}}],
            # in "ask" mode a change needs approval; this one gets declined
            # (a tool a no-project session offers: the kind gate would reject update_profile first)
            [{"type": "tool_call", "name": "new_world_edit", "input": {"resource": "test_edit"}}],
            [{"type": "tool_call", "name": "finish", "input": {"summary": "nothing to do"}}],
        ])
        mgr = AgentManager(studio, provider_factory=lambda name=None: prov)
        studio.agents = mgr
        v = mgr.start(goal="test run", autonomy="ask")
        sid = v["id"]
        s = mgr.sessions[sid]
        assert _wait(lambda: s.status == "waiting_user"), s.status
        q = [e for e in s.events if e["kind"] == "question"]
        assert q and q[0]["text"] == "Which server?"
        mgr.message(sid, "the test one")
        assert _wait(lambda: s.status == "waiting_approval"), s.status
        call = s.pending["call_id"]
        mgr.approve(sid, call, False, "not now")
        assert _wait(lambda: s.status in ("finished", "done")), s.status
        kinds = [e["kind"] for e in s.events]
        assert "approval" in kinds and "approval_result" in kinds and "finished" in kinds
        # the model saw the user's answer and the decline
        seen = json.dumps(prov.calls[-1]["messages"])
        assert "The user answered: the test one" in seen and "The user declined this call. Their note: not now" in seen
        # every tool call got a result (Claude's API requires it)
        for m in prov.calls[-1]["messages"]:
            if m["role"] == "assistant":
                ids = [b["id"] for b in m["content"] if b["type"] == "tool_call"]
                if ids:
                    nxt = prov.calls[-1]["messages"][prov.calls[-1]["messages"].index(m) + 1]
                    assert nxt["role"] == "tool" and [r["id"] for r in nxt["results"]] == ids
        # a later message starts another turn
        prov.script.append([{"type": "text", "text": "ok, done again"}])
        mgr.message(sid, "anything else?")
        assert _wait(lambda: any(e["kind"] == "assistant" and e["text"] == "ok, done again" for e in s.events))
        assert _wait(lambda: s.status == "waiting_user")
        # stop from waiting
        mgr.stop(sid)
    finally:
        studio.close()
        shutil.rmtree(ws, ignore_errors=True)


def test_agent_patch_helpers():
    from fivem2open77.studio.agent.tools import _expand, _sound_name
    assert _expand({"sound.engine": "x"}) == {"sound": {"engine": "x"}}
    assert _expand({"parts.bone_roles.a.b": "skip"}) == {"parts": {"bone_roles": {"a.b": "skip"}}}
    assert _expand({"parts": {"bone_roles": {"a.b": "body"}}, "parts.lids": "swap"}) == \
        {"parts": {"bone_roles": {"a.b": "body"}, "lids": "swap"}}
    assert _expand({"handling": {"multipliers.torque": 1.2}}) == {"handling": {"multipliers": {"torque": 1.2}}}
    assert _sound_name("Rayfield Aerondight") == "v_car_rayfield_aerondight"
    assert _sound_name("V_CAR_PORSCHE_911_TURBO") == "v_car_porsche_911_turbo"
    assert _sound_name("auto") == "auto"


def test_agent_finish_after_failed_change_asks_once():
    from fivem2open77.studio.agent.providers import ScriptedProvider
    studio, ws, AgentManager = _agent_studio()
    try:
        prov = ScriptedProvider([
            [{"type": "tool_call", "name": "update_profile", "input": {"patch": {"display_name": "X"}}}],   # no project: fails
            [{"type": "tool_call", "name": "finish", "input": {"summary": "all good"}}],                      # refused once
            [{"type": "tool_call", "name": "finish", "input": {"summary": "could not rename: no project"}}],
        ])
        mgr = AgentManager(studio, provider_factory=lambda name=None: prov)
        s = mgr.sessions[mgr.start(goal="rename", autonomy="full")["id"]]
        assert _wait(lambda: s.status == "finished"), s.status
        results = [e for e in s.events if e["kind"] == "tool_result" and e["tool"] == "finish"]
        assert results[0]["error"] and "did not go through" in results[0]["text"]
        assert s.summary == "could not rename: no project"
    finally:
        studio.close()
        shutil.rmtree(ws, ignore_errors=True)


def test_agent_autonomy_levels():
    from fivem2open77.studio.agent.manager import Session
    s = Session.__new__(Session)
    for autonomy, risk, want in (("ask", "read", False), ("ask", "change", True), ("ask", "deploy", True),
                                 ("auto_safe", "change", False), ("auto_safe", "deploy", True),
                                 ("full", "deploy", False)):
        s.autonomy = autonomy
        assert s._needs_approval(risk) is want, (autonomy, risk)


def test_agent_with_car_inspects_and_edits():
    if not os.path.isfile(CAR):
        print("   (skipped: no test car)")
        return
    from fivem2open77.studio.agent.providers import ScriptedProvider
    studio, ws, AgentManager = _agent_studio()
    try:
        p, job = studio.create_project(path=CAR)
        studio.jobs.wait(job, 300)
        assert job.state == "done", job.error
        prov = ScriptedProvider([
            [{"type": "tool_call", "name": "inspect_car", "input": {}}],
            [{"type": "tool_call", "name": "render_car", "input": {"mode": "parts", "views": ["front_3q"]}}],
            [{"type": "tool_call", "name": "update_profile", "input": {"patch": {"display_name": "BMW i8 Roadster"}}}],
            [{"type": "tool_call", "name": "finish", "input": {"summary": "renamed"}}],
        ])
        mgr = AgentManager(studio, provider_factory=lambda name=None: prov)
        v = mgr.start(project=p.id, goal="rename it", autonomy="auto_safe")
        s = mgr.sessions[v["id"]]
        assert _wait(lambda: s.status in ("finished", "failed"), 300), s.status
        assert s.status == "finished", [e for e in s.events if e["kind"] == "error"]
        assert p.raw_profile()["display_name"] == "BMW i8 Roadster"
        tool_msgs = [m for m in prov.calls[2]["messages"] if m["role"] == "tool"]
        img = [b for r in tool_msgs[-1]["results"] for b in r["content"] if b["type"] == "image"]
        assert img and base64.b64decode(img[0]["png"])[:4] == b"\x89PNG"
        insp = json.loads(tool_msgs[0]["results"][0]["content"][0]["text"])
        assert insp["record"].startswith("Vehicle.") and insp["parts"]
    finally:
        studio.close()
        shutil.rmtree(ws, ignore_errors=True)


# ----------------------------------------------------------------------------- provider formats
class _FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.server.seen.append(("GET", self.path, dict(self.headers), None))
        if self.path.startswith("/v1/models"):
            return self._reply({"data": [{"id": "claude-haiku-x", "display_name": "Haiku"}, {"id": "claude-sonnet-x", "display_name": "Sonnet"}]})
        if self.path == "/api/tags":
            return self._reply({"models": [{"name": "qwen2.5:3b", "size": 1}]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(("POST", self.path, dict(self.headers), body))
        if self.path == "/v1/messages":
            return self._reply({"content": [{"type": "text", "text": "checking"},
                                            {"type": "tool_use", "id": "toolu_1", "name": "inspect_car", "input": {"detail": "summary"}}],
                                "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}})
        if self.path == "/api/show":
            return self._reply({"capabilities": ["completion", "tools"]})
        if self.path == "/api/chat":
            return self._reply({"message": {"role": "assistant", "content": "",
                                            "tool_calls": [{"function": {"name": "inspect_car", "arguments": {"detail": "bones"}}}]},
                                "done_reason": "stop", "prompt_eval_count": 7, "eval_count": 3})
        self.send_response(404)
        self.end_headers()


def _fake_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "convert it"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "looking"}, {"type": "tool_call", "id": "c1", "name": "render_car", "input": {}}]},
    {"role": "tool", "results": [{"id": "c1", "name": "render_car", "is_error": False,
                                  "content": [{"type": "text", "text": "view"}, {"type": "image", "png": "iVBORw0KGgo="}]}]},
    {"role": "user", "content": [{"type": "text", "text": "and?"}]},
]
TOOLS = [{"name": "inspect_car", "description": "d", "schema": {"type": "object", "properties": {}}},
         {"name": "render_car", "description": "r", "schema": {"type": "object", "properties": {}}}]


def test_anthropic_request_format():
    from fivem2open77.studio.agent.providers import AnthropicProvider
    srv = _fake_server()
    try:
        p = AnthropicProvider("sk-test", "", f"http://127.0.0.1:{srv.server_address[1]}")
        assert p.pick_model() == "claude-sonnet-x"          # newest Sonnet preferred
        r = p.chat("SYS", HISTORY, TOOLS)
        assert r.text == "checking" and r.tool_calls[0]["name"] == "inspect_car" and r.tool_calls[0]["id"] == "toolu_1"
        _, path, headers, body = [s for s in srv.seen if s[0] == "POST"][0]
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers.get("x-api-key") == "sk-test" and headers.get("anthropic-version") == "2023-06-01"
        assert body["model"] == "claude-sonnet-x" and body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert [t["name"] for t in body["tools"]] == ["inspect_car", "render_car"] and "input_schema" in body["tools"][0]
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["user", "assistant", "user"], roles          # tool result + next user text merged
        tr = body["messages"][2]["content"]
        assert tr[0]["type"] == "tool_result" and tr[0]["tool_use_id"] == "c1"
        assert tr[0]["content"][1]["type"] == "image" and tr[0]["content"][1]["source"]["media_type"] == "image/png"
        assert tr[1] == {"type": "text", "text": "and?"}
        assert body["messages"][1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "render_car", "input": {}}
    finally:
        srv.shutdown()


def test_ollama_request_format():
    from fivem2open77.studio.agent.providers import OllamaProvider
    srv = _fake_server()
    try:
        p = OllamaProvider(f"http://127.0.0.1:{srv.server_address[1]}", "")
        assert p.pick_model() == "qwen2.5:3b"
        r = p.chat("SYS", HISTORY, TOOLS)
        assert r.tool_calls and r.tool_calls[0]["name"] == "inspect_car" and r.tool_calls[0]["input"] == {"detail": "bones"}
        body = [s for s in srv.seen if s[0] == "POST" and s[1] == "/api/chat"][0][3]
        assert body["stream"] is False and body["model"] == "qwen2.5:3b"
        assert body["messages"][0] == {"role": "system", "content": "SYS"}
        assert body["tools"][0]["function"]["name"] == "inspect_car" and body["tools"][0]["type"] == "function"
        roles = [m["role"] for m in body["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "user"], roles
        assert body["messages"][2]["tool_calls"][0]["function"]["name"] == "render_car"
        assert "images" not in json.dumps(body["messages"][3])        # no vision capability: no images sent
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print("ok")
