"""The map baker with the REAL toolkit: the .NET runtime and WolvenKit CLI packed by
`mlo-baker-toolkit`, installed on a fake server host by map_deploy.sh/baker_install.sh, the baker
service running under a python3 that is Python 3.8 when one is available, answering a move the way
the in-game manager asks for it; the server restart's start hook installs the build.  The package
it makes is byte-for-byte the one a direct bake makes.

Needs WOLVENKIT_CLI (WolvenKit.CLI.dll) and a linux .NET 8 install (DOTNET_ROOT, or dotnet on PATH);
skipped otherwise.  Takes a minute or two (packing the toolkit, WolvenKit start-up)."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fivem2open77.cp77.wolvenkit import WolvenKit  # noqa: E402
from fivem2open77.mlo import bake as B  # noqa: E402
from fivem2open77.mlo import deploy as D  # noqa: E402
from fivem2open77.mlo import toolkit as TK  # noqa: E402
from test_mlo import _FakeWk, _converted_map  # noqa: E402
from test_mlo_deploy import MapServer, _payload, _read, _run, _wait  # noqa: E402


def _dotnet_root():
    root = os.environ.get("DOTNET_ROOT")
    if root and os.path.isfile(os.path.join(root, "dotnet")):
        return root
    d = shutil.which("dotnet")
    return os.path.dirname(os.path.realpath(d)) if d else None


def _python38():
    p = shutil.which("python3.8")
    if p:
        return p
    c = glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.8*/bin/python3.8"))
    return c[0] if c else None


def test_real_toolkit_bakes_on_the_server_host():
    wk_path = os.environ.get("WOLVENKIT_CLI")
    root = _dotnet_root()
    if not wk_path or not os.path.isfile(wk_path) or not root or not shutil.which("setsid"):
        print("   (skipped: set WOLVENKIT_CLI and DOTNET_ROOT)")
        return
    tmp = tempfile.mkdtemp(prefix="f2o77-realbaker-")
    srv = None
    try:
        res = _converted_map(os.path.join(tmp, "out"))
        B.bake(res, {"x": 480, "y": 1800, "z": 174}, _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-convert-1")
        bundle = D.bundle(res, os.path.join(tmp, "bundle.tgz"))["path"]
        tk = TK.build(os.path.join(tmp, "baker-toolkit.tgz"), root, wk_path, log=lambda *a: None)
        assert tk["version"].startswith("net8.") and tk["bytes"] > 20 * 1024 * 1024
        pay = _payload(tmp, bundle, tk["path"])
        srv = MapServer(tmp)
        extra = {}
        py38 = _python38()
        if py38:
            shim = os.path.join(tmp, "py38bin")
            os.makedirs(shim)
            os.symlink(py38, os.path.join(shim, "python3"))
            extra["PATH"] = shim + os.pathsep + os.environ.get("PATH", "")
            srv.env.update(extra)
        # no dotnet of the host in the way: the toolkit's own runtime must be the one that runs
        for k in ("DOTNET_ROOT", "WOLVENKIT_CLI", "DOTNET"):
            srv.env.pop(k, None)
        rc, out = _run(pay, srv)
        assert rc == 0, out
        assert "WolvenKit: 8." in out, out
        baker_home = os.path.join(srv.home, ".f2o77", "baker")
        assert os.path.isfile(os.path.join(baker_home, "dotnet", "dotnet"))
        assert os.path.isfile(os.path.join(baker_home, "wolvenkit", "WolvenKit.CLI.dll"))
        bake_dir = os.path.join(srv.manager(), "data", "bake")
        info = _wait(lambda: _read(os.path.join(bake_dir, "baker.json")), 60, "baker.json")
        if py38:
            assert info["python"].startswith("3.8"), info
        anchor = {"x": 612.25, "y": 1888.5, "z": 201.75, "yaw": -30.0}
        req = {"id": "cedar_crest-1760000100-1", "action": "bake", "map": "cedar_crest", "resource": "cedar_crest",
               "anchor": anchor, "requestedBy": "player1 (#1)", "requestedAt": 1760000100, "mode": "now"}
        os.makedirs(os.path.join(bake_dir, "requests"), exist_ok=True)
        with open(os.path.join(bake_dir, "requests", "cedar_crest.json"), "w") as f:
            json.dump(req, f)
        status = os.path.join(bake_dir, "status", "cedar_crest.json")
        st = _wait(lambda: (_read(status) or {}).get("state") in ("done", "failed") and _read(status), 300, "the bake")
        svc_log = open(os.path.join(srv.fake, "svc-f2o77-mapbaker-open77-test.service.log")).read()
        assert st["state"] == "done", (st, svc_log[-3000:])
        staged = os.path.join(srv.dir, "f2o77-maps", "staged", "cedar_crest", "dist", "cedar_crest_sector.zip")
        assert os.path.isfile(staged)
        # the same bake done directly with the developer's WolvenKit gives the same package
        ref = os.path.join(tmp, "ref")
        r = B.bake(res, anchor, WolvenKit(wk_path, log=lambda *a: None), log=lambda *a: None, out_dir=ref,
                   bake_id="whatever")
        with open(staged, "rb") as a, open(os.path.join(ref, "dist", "cedar_crest_sector.zip"), "rb") as b:
            assert a.read() == b.read(), "the server's runtime and WolvenKit make the same package"
        assert st["package"]["sha256"] == r["baked"]["sha256"]
        # the restart: the start hook installs it
        subprocess.run(srv.env["SYSTEMCTL"].split() + ["kill", "--signal=SIGINT", "open77-test.service"], env=srv.env,
                       check=True, timeout=300)
        baked = _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))
        assert baked["id"] == req["id"] and baked["anchor"] == anchor, baked
        assert _read(status)["state"] == "promoted"
    finally:
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_real_toolkit_bakes_on_the_server_host()
    print("ok   test_real_toolkit_bakes_on_the_server_host")
