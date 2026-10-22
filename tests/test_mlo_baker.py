"""The map baker (fivem2open77/mlo/baker.py) on a fake server folder, with the fake WolvenKit of
test_mlo: requests from the manager, staged builds that never touch the live map, the start hook
(promote) honouring the manager's latest intent, cancel / unstage, failures, a crashed baker, the
install-before-restart request, the locks, the command line, and Python 3.8."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fivem2open77.mlo import bake as B  # noqa: E402
from fivem2open77.mlo import baker as K  # noqa: E402
from fivem2open77.mlo import resource as R  # noqa: E402
from test_mlo import _FakeWk, _converted_map  # noqa: E402


def _server(tmp):
    """resources/assets/maps/cedar_crest (baked at 480,1800,174) + resources/assets/tools/f2o77_maps."""
    out = os.path.join(tmp, "out")
    res = _converted_map(out)
    B.bake(res, {"x": 480, "y": 1800, "z": 174}, _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-convert-1")
    srv = os.path.join(tmp, "open77-test")
    shutil.copytree(res, os.path.join(srv, "resources", "assets", "maps", "cedar_crest"))
    shutil.copytree(os.path.join(out, R.MANAGER), os.path.join(srv, "resources", "assets", "tools", R.MANAGER))
    with open(os.path.join(srv, "server.jsonc"), "w") as f:
        f.write("{}\n")
    return srv


def _paths(srv):
    m = os.path.join(srv, "resources", "assets", "tools", R.MANAGER)
    return {
        "map": os.path.join(srv, "resources", "assets", "maps", "cedar_crest"),
        "manager": m,
        "req": os.path.join(m, "data", "bake", "requests", "cedar_crest.json"),
        "status": os.path.join(m, "data", "bake", "status", "cedar_crest.json"),
        "def": os.path.join(m, "shared", "maps", "cedar_crest.lua"),
        "staged": os.path.join(srv, "f2o77-maps", "staged", "cedar_crest"),
    }


def _request(srv, rid, anchor=(600, 1900, 200, 45), action="bake", **extra):
    req = {"id": rid, "action": action, "map": "cedar_crest", "resource": "cedar_crest",
           "anchor": dict(zip("x y z yaw".split(), anchor)), "requestedBy": "player1 (#1)", "requestedAt": 1, "mode": "now"}
    req.update(extra)
    K.write_json(_paths(srv)["req"], req)
    return req


def _snapshot(*dirs):
    out = {}
    for d in dirs:
        for dp, _, fns in os.walk(d):
            if os.sep + "data" in dp[len(d):]:
                continue
            for fn in fns:
                p = os.path.join(dp, fn)
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


def _status(srv):
    with open(_paths(srv)["status"]) as f:
        return json.load(f)


def _srv(srv, logs=None):
    return K.Server(srv, log=(logs.append if logs is not None else (lambda *a: None)))


def test_bake_is_staged_and_the_start_hook_installs_it():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        assert S.manager() == P["manager"] and S.map_resource("cedar_crest") == P["map"]
        assert S.handle_requests(lambda: _FakeWk()) == [], "no request, nothing to do"
        _request(srv, "cedar_crest-1-1")
        live = _snapshot(P["map"], P["manager"])
        assert S.handle_requests(lambda: _FakeWk()) == [("cedar_crest", "bake")]
        st = _status(srv)
        assert st["state"] == "done" and st["id"] == "cedar_crest-1-1" and st["anchor"]["yaw"] == 45.0
        assert st["package"]["bytes"] > 0 and len(st["package"]["sha256"]) == 64
        assert _snapshot(P["map"], P["manager"]) == live, "the live map and manager are untouched while the server runs"
        build = json.load(open(os.path.join(P["staged"], "build.json")))
        assert build["id"] == "cedar_crest-1-1" and build["resourceDir"] == os.path.join("resources", "assets", "maps", "cedar_crest")
        assert sorted(build["files"]) == ["README.md", "baked.json", "dist/cedar_crest_sector.zip", "open77.lua", "server/main.lua"]
        assert S.handle_requests(lambda: _FakeWk()) == [], "answered once"
        # the restart: the start hook installs it
        done = S.promote()
        assert len(done) == 1 and done[0]["id"] == "cedar_crest-1-1"
        baked = json.load(open(os.path.join(P["map"], "baked.json")))
        assert baked["anchor"] == {"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0} and baked["id"] == "cedar_crest-1-1"
        server = open(os.path.join(P["map"], "server", "main.lua")).read()
        assert '"cedar_crest-1-1"' in server and "1900.0" in server
        mdef = open(P["def"]).read()
        assert 'bakeId = "cedar_crest-1-1"' in mdef and "ghost = [" in mdef
        assert _status(srv)["state"] == "promoted"
        assert not os.path.exists(P["req"]), "a spent request goes: nothing can run it twice"
        assert not os.path.exists(P["staged"]) and len(os.listdir(os.path.join(srv, "f2o77-maps", "applied"))) == 1
        bdir = os.path.join(srv, "f2o77-maps", "backups", "cedar_crest")
        (stamp,) = os.listdir(bdir)
        old = json.load(open(os.path.join(bdir, stamp, "baked.json")))
        assert old["id"] == "cedar_crest-convert-1", "the replaced files are kept"
        assert os.path.isfile(os.path.join(bdir, stamp, "_manager", "cedar_crest.lua"))
        assert S.promote() == [], "nothing staged any more"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cancel_unstage_superseded_and_withdrawn_builds():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        live = _snapshot(P["map"])
        # built, then cancelled: the unstage request throws it away
        _request(srv, "a-1")
        S.handle_requests(lambda: _FakeWk())
        assert os.path.isdir(P["staged"])
        _request(srv, "a-1", action="unstage")
        assert S.handle_requests(lambda: _FakeWk()) == [("cedar_crest", "unstage")]
        assert _status(srv)["state"] == "unstaged" and not os.path.exists(P["staged"]) and not os.path.exists(P["req"])
        assert S.handle_requests(lambda: _FakeWk()) == []
        # built, cancelled while the baker was down: the start hook honours the cancel
        _request(srv, "a-2")
        S.handle_requests(lambda: _FakeWk())
        _request(srv, "a-2", action="unstage")
        assert S.promote() == [] and _status(srv)["state"] == "unstaged" and not os.path.exists(P["staged"])
        # a staged build the manager no longer asks for (a newer request) is dropped, not installed
        _request(srv, "a-3")
        S.handle_requests(lambda: _FakeWk())
        _request(srv, "a-4", anchor=(1, 2, 3, 0))
        assert S.promote() == [] and not os.path.exists(P["staged"])
        assert _snapshot(P["map"]) == live, "nothing reached the live map"
        S.handle_requests(lambda: _FakeWk())
        assert _status(srv)["id"] == "a-4" and _status(srv)["state"] == "done"
        # the request disappears during the bake (the player cancelled before the baker answered)

        class Withdrawn(_FakeWk):
            def pack(self, src, out_dir):
                os.remove(P["req"])
                super().pack(src, out_dir)
        _request(srv, "a-5")
        S.handle_requests(lambda: Withdrawn())
        st = _status(srv)
        assert st["state"] == "cancelled" and st["id"] == "a-5"
        assert not [e for e in os.listdir(os.path.join(srv, "f2o77-maps", "staged")) if e.startswith(".")], "temp cleaned"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failures_and_a_crashed_baker():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)

        class Broken(_FakeWk):
            def pack(self, src, out_dir):
                raise RuntimeError("WolvenKit pack failed (exit 1):\nboom")
        _request(srv, "f-1")
        S.handle_requests(lambda: Broken())
        st = _status(srv)
        assert st["state"] == "failed" and "WolvenKit pack failed" in st["message"] and "boom" in st["log"]
        assert not os.path.exists(P["req"])
        assert not os.path.exists(P["staged"])
        assert S.handle_requests(lambda: _FakeWk()) == [], "a failed request is not retried by itself"
        # no WolvenKit at all
        _request(srv, "f-2")
        S.handle_requests(lambda: None)
        assert _status(srv)["state"] == "failed" and "WolvenKit" in _status(srv)["message"]
        # a baker that died mid-bake left "baking": the next one bakes it again
        _request(srv, "f-3")
        K.write_json(P["status"], {"id": "f-3", "state": "baking"})
        S.handle_requests(lambda: _FakeWk())
        assert _status(srv)["state"] == "done"
        # the map resource is gone
        _request(srv, "f-4")
        req = json.load(open(P["req"]))
        req["resource"] = "nowhere_map"
        K.write_json(P["req"], req)
        S.handle_requests(lambda: _FakeWk())
        assert _status(srv)["state"] == "failed" and "not found" in _status(srv)["message"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_newer_request_is_never_retired_with_an_older_answer():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        _request(srv, "n-2")
        S.set_status("cedar_crest", {"id": "n-1", "state": "failed", "message": "old"})
        assert json.load(open(P["req"]))["id"] == "n-2"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_install_before_restart_without_the_start_hook():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        _request(srv, "p-1")
        S.handle_requests(lambda: _FakeWk())
        assert json.load(open(os.path.join(P["map"], "baked.json")))["id"] == "cedar_crest-convert-1"
        _request(srv, "p-1", promoteNow=True)            # the manager, seconds before the restart
        assert S.handle_requests(lambda: _FakeWk()) == [("cedar_crest", "promote")]
        assert json.load(open(os.path.join(P["map"], "baked.json")))["id"] == "p-1"
        assert _status(srv)["state"] == "promoted"
        assert S.handle_requests(lambda: _FakeWk()) == [], "once"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_requests_are_ignored():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        for bad in ({"id": "../../x", "map": "cedar_crest", "anchor": {"x": 1, "y": 2, "z": 3}},
                    {"id": "ok-1", "map": "other_map", "anchor": {"x": 1, "y": 2, "z": 3}},
                    {"id": "ok-2", "map": "cedar_crest", "anchor": {"x": "nan", "y": 2, "z": 3}},
                    {"id": "ok-3", "map": "cedar_crest", "anchor": {"x": 1, "y": 2}},
                    {"id": "ok-4", "map": "cedar_crest", "action": "rm -rf", "anchor": {"x": 1, "y": 2, "z": 3}},
                    {"id": "ok-5", "map": "cedar_crest", "resource": "../x", "anchor": {"x": 1, "y": 2, "z": 3}},
                    ["not", "an", "object"]):
            K.write_json(P["req"], bad)
            assert S.handle_requests(lambda: _FakeWk()) == [], bad
        with open(P["req"], "w") as f:
            f.write("{half a json")
        assert S.handle_requests(lambda: _FakeWk()) == []
        assert not os.path.exists(P["status"])
        try:
            K._safe_rel("../escape")
            raise AssertionError("path escape accepted")
        except K.BakerError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_locks_service_loop_and_command_line():
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        P = _paths(srv)
        S = _srv(srv)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with K.file_lock(S.lock):
                held.set()
                release.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        t0 = time.time()
        try:
            with K.file_lock(S.lock, timeout=0.5):
                raise AssertionError("got a held lock")
        except K.BakerError:
            pass
        assert time.time() - t0 < 3
        release.set()
        t.join()
        # the service loop: introduces itself to the manager, answers, stops
        os.makedirs(os.path.join(srv, "f2o77-maps"), exist_ok=True)
        drop_in = os.path.join(tmp, "f2o77-maps.conf")
        with open(drop_in, "w") as f:
            f.write("[Service]\nExecStartPre=-/usr/bin/env python3 -m fivem2open77.mlo.baker promote --server-dir x\n")
        K.write_json(os.path.join(srv, "f2o77-maps", "hook.json"), {"unit": "open77-test.service", "dropIn": drop_in})
        _request(srv, "l-1")
        logs = []
        K.run(_srv(srv, logs), lambda: _FakeWk(), wolvenkit_path="/x/WolvenKit.CLI.dll", once=True)
        info = json.load(open(os.path.join(P["manager"], "data", "bake", "baker.json")))
        assert info["hook"] is True and info["version"] == K.VERSION and info["wolvenkit"] == "/x/WolvenKit.CLI.dll"
        assert _status(srv)["state"] == "done" and any("start hook installed" in line for line in logs)
        # command line: status JSON, promote never fails the server start
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, ".."))
        r = subprocess.run([sys.executable, "-m", "fivem2open77.mlo.baker", "status", "--server-dir", srv],
                           capture_output=True, text=True, env=env, timeout=60)
        st = json.loads(r.stdout)
        assert st["maps"]["cedar_crest"]["staged"]["id"] == "l-1" and st["hook"]["installed"] is True
        r = subprocess.run([sys.executable, "-m", "fivem2open77.mlo.baker", "promote", "--server-dir", srv],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "1 build(s) installed" in r.stdout, r.stdout + r.stderr
        r = subprocess.run([sys.executable, "-m", "fivem2open77.mlo.baker", "promote", "--server-dir",
                            os.path.join(tmp, "no-such-server")], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 2 and "not an Open77 server folder" in r.stdout
        with open(os.path.join(srv, "f2o77-maps", "staged", "junk"), "w") as f:
            f.write("not a folder")
        r = subprocess.run([sys.executable, "-m", "fivem2open77.mlo.baker", "promote", "--server-dir", srv],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, "a broken staging area never stops the server from starting"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_runs_on_python38_with_only_its_own_files():
    py38 = shutil.which("python3.8")
    if not py38:
        import glob
        cands = glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.8*/bin/python3.8"))
        py38 = cands[0] if cands else None
    if not py38:
        print("   (skipped: no python3.8)")
        return
    from fivem2open77.mlo import toolkit as TK
    tmp = tempfile.mkdtemp()
    try:
        srv = _server(tmp)
        lib = os.path.join(tmp, "lib")
        for rel in TK.BAKER_LIB:
            dst = os.path.join(lib, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(HERE, "..", *rel.split("/")), dst)
        _request(srv, "py38-1")
        # a fake WolvenKit CLI (an executable) so the whole bake runs in the 3.8 process
        wk = os.path.join(tmp, "fakewk")
        with open(wk, "w") as f:
            f.write("#!" + sys.executable + "\n" + FAKE_WK_CLI)
        os.chmod(wk, 0o755)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": lib, "HOME": tmp}
        for cmd in (["once", "--wolvenkit", wk], ["promote"], ["status"]):
            r = subprocess.run([py38, "-W", "error::SyntaxWarning", "-m", "fivem2open77.mlo.baker", cmd[0], "--server-dir", srv]
                               + cmd[1:], capture_output=True, text=True, env=env, timeout=120)
            assert r.returncode == 0, (cmd, r.stdout, r.stderr)
        P = _paths(srv)
        assert json.load(open(os.path.join(P["map"], "baked.json")))["id"] == "py38-1"
        assert _status(srv)["state"] == "promoted"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# a stand-in for WolvenKit.CLI: `convert deserialize <dir> -o <out>` copies x.json -> x, `pack <dir> -o <out>`
# writes a small archive, `--version` answers
FAKE_WK_CLI = r'''
import os, struct, sys
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("8.17.4"); sys.exit(0)
if a[:2] == ["convert", "deserialize"]:
    src, out = a[2], a[a.index("-o") + 1]
    os.makedirs(out, exist_ok=True)
    for fn in os.listdir(src):
        if fn.endswith(".json"):
            with open(os.path.join(src, fn), "rb") as s, open(os.path.join(out, fn[:-5]), "wb") as d:
                d.write(s.read())
    print("done"); sys.exit(0)
if a[:1] == ["pack"]:
    src, out = a[1], a[a.index("-o") + 1]
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, os.environ.get("F2O77_TESTS", ""))
    table = bytearray(struct.pack("<III", 1, 0, 0)) + struct.pack("<Qq5I20s", 1000, 5, 0, 0, 0, 0, 0, b"\0" * 20)
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, 44, len(table) + 16, 0, 0, 0) + b"\0\0\0\0"
    import zlib
    def crc64(data):
        poly, crc = 0xC96C5795D7870F42, 0xFFFFFFFFFFFFFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        return crc ^ 0xFFFFFFFFFFFFFFFF
    with open(os.path.join(out, os.path.basename(src.rstrip("/")) + ".archive"), "wb") as f:
        f.write(head + struct.pack("<IIQ", 8, len(table) + 8, crc64(bytes(table))) + table)
    print("done"); sys.exit(0)
print("unknown", a); sys.exit(1)
'''


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
