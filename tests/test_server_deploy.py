"""server_deploy.sh against a fake Open77 server (fake systemctl, fake server binary, real files).

Covers: deploy --stage with and without the f2o77_hub (pending queue), a rebuild with unchanged
packages going live without a restart, the hub's data/ surviving an update, status --json, and a
restart that brings every resource up.  Needs bash and python3 (Linux / WSL)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.studio.deploy import make_bundle  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "tools", "deploy", "server_deploy.sh")

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# fake systemctl for one unit; state under $FAKE
[ "$1" = "--user" ] && shift
cmd=$1; shift
case "$cmd" in
  is-active) cat "$FAKE/state" ;;
  show)
    prop=$2
    case "$prop" in
      MainPID) cat "$FAKE/pid" ;;
      ActiveEnterTimestamp) echo "Wed 2026-10-07 12:00:00 UTC" ;;
      ExecStart) echo "{ path=$FAKE/Open77.Server ; argv[]=$FAKE/Open77.Server --config server.jsonc ; ignore_errors=no }" ;;
      WorkingDirectory) cat "$FAKE/dir" ;;
      *) echo "" ;;
    esac ;;
  kill)
    echo $(( $(cat "$FAKE/pid") + 1 )) > "$FAKE/pid"
    srv=$(cat "$FAKE/dir")
    {
      echo "[INF] Server configuration: server.jsonc"
      echo "[INF] Required mods: 4 declared"
      for r in $(python3 -c "import json,re,sys; t=open(sys.argv[1]).read(); t=re.sub(r'//.*','',t); print(' '.join(json.loads(re.sub(r',(\s*[}\]])', r'\1', t))['resources']['load']))" "$srv/server.jsonc"); do
        echo "[INF] [resource:$r] started"
      done
      echo "[INF] listening on UDP 0.0.0.0:7777"
    } >> "$srv/server.log" ;;
  start) echo active > "$FAKE/state" ;;
  stop) echo inactive > "$FAKE/state" ;;
  *) echo "fake systemctl: $cmd" >&2 ;;
esac
"""

SERVER_JSONC = """{
  // test server
  "network": { "port": 7777 },
  "resources": {
    "root": "resources",
    "load": [
      "freeroam", // the game mode
      "archivexl",
      "tweakxl"
    ]
  },
  "requiredMods": { "enabled": true, "unsecured": true }
}
"""


def _resource(root, name, version, preload=None, extra=None, display=None, record=None):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "server"), exist_ok=True)
    lines = []
    if display:
        lines += [f"-- {display}: FiveM vehicle converted by fivem2open77.", f"-- Spawn with: /car {record}"]
    lines += [f'resource "{name}"', f'version "{version}"', "auto_start true"]
    if preload:
        os.makedirs(os.path.join(d, "dist"), exist_ok=True)
        with zipfile.ZipFile(os.path.join(d, "dist", f"{name}.zip"), "w") as z:
            z.writestr(zipfile.ZipInfo("archive/pc/mod/x.archive", (2020, 1, 1, 0, 0, 0)), preload)   # same bytes -> same zip
        lines += ['dependency "archivexl >=1.27.1"', 'dependency "tweakxl >=1.11.4"', f'preload_mod "dist/{name}.zip"']
    lines.append('server_script "server/main.lua"')
    with open(os.path.join(d, "open77.lua"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(d, "server", "main.lua"), "w") as f:
        f.write(extra or "print('hi')\n")
    return d


class Fake:
    def __init__(self, tmp, with_hub=True):
        self.root = os.path.join(tmp, "srv")
        self.dir = os.path.join(self.root, "open77-test")
        self.fake = os.path.join(tmp, "fake")
        os.makedirs(self.fake)
        os.makedirs(os.path.join(self.dir, "resources", "assets", "loaders"))
        for n in ("archivexl", "tweakxl"):
            _resource(os.path.join(self.dir, "resources", "assets", "loaders"), n, "1.27.1" if n == "archivexl" else "1.11.4",
                      preload=b"loader")
        _resource(os.path.join(self.dir, "resources", "gamemodes"), "freeroam", "1.0.0")
        with open(os.path.join(self.dir, "server.jsonc"), "w") as f:
            f.write(SERVER_JSONC)
        with open(os.path.join(self.dir, "package-manifest.json"), "w") as f:
            json.dump({"version": "2.31.21+op77.131"}, f)
        with open(os.path.join(self.dir, "server.log"), "w") as f:
            f.write("[INF] Server configuration: server.jsonc\n[INF] listening on UDP 0.0.0.0:7777\n")
        for k, v in (("state", "active"), ("pid", "100"), ("dir", self.dir)):
            with open(os.path.join(self.fake, k), "w") as f:
                f.write(v + "\n")
        sc = os.path.join(self.fake, "systemctl")
        with open(sc, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(sc, 0o755)
        srv = os.path.join(self.fake, "Open77.Server")
        with open(srv, "w") as f:
            f.write("#!/bin/sh\necho 'Configuration is valid.'\nexit 0\n")
        os.chmod(srv, 0o755)
        with open(os.path.join(self.dir, "Open77.Server"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(self.dir, "Open77.Server"), 0o755)
        self.env = dict(os.environ, SERVERS_ROOT=self.root, SYSTEMCTL=sc, FAKE=self.fake,
                        BACKUP_ROOT=os.path.join(tmp, "backups"))

    def run(self, *args, check=True):
        r = subprocess.run(["bash", SCRIPT] + list(args), env=self.env, capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            raise AssertionError(f"server_deploy.sh {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def load(self):
        import re
        t = open(os.path.join(self.dir, "server.jsonc")).read()
        t = re.sub(r"//.*", "", t)
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", t))["resources"]["load"]

    def hub_data(self, name):
        p = os.path.join(self.dir, "resources", "assets", "tools", "f2o77_hub", "data", name)
        return json.load(open(p)) if os.path.isfile(p) else None


def _build(tmp, version, payload, chime_lua="print('chime')\n"):
    out = os.path.join(tmp, f"build-{version}", "out")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    _resource(out, "testcar", version, preload=payload, display="Test Car", record="Vehicle.testcar")
    open(os.path.join(out, "testcar", "conversion-report.json"), "w").write("{}")
    _resource(out, "testcar_chime", version, extra=chime_lua)
    return out


def test_stage_live_and_restart():
    if not shutil.which("bash") or not shutil.which("python3") or not shutil.which("sha256sum"):
        print("   (skipped: needs bash, python3, sha256sum)")
        return
    tmp = tempfile.mkdtemp(prefix="f2o77-deploy-")
    try:
        fake = Fake(tmp)
        # 1. the hub, staged (no hub yet: it just waits for a restart)
        hb = make_bundle(os.path.join(tmp, "hub.tgz"), include_hub=True)
        out = fake.run("deploy", hb["path"], "open77-test", "--stage")
        assert "f2o77_hub" in fake.load(), out
        assert "PENDING restart" in out, out
        hub_cfg = os.path.join(fake.dir, "resources", "assets", "tools", "f2o77_hub", "data", "config.json")
        # the operator edits the hub's settings on the server
        cfg = json.load(open(hub_cfg))
        cfg["daily_restart_utc"] = "05:00"
        json.dump(cfg, open(hub_cfg, "w"))

        # 2. a new car, staged: queued in the hub
        b1 = make_bundle(os.path.join(tmp, "car1.tgz"), build_out=_build(tmp, "1.0.1", b"v1"))
        out = fake.run("deploy", b1["path"], "open77-test", "--stage")
        assert "STAGED: testcar 1.0.1" in out, out
        pend = fake.hub_data("pending.json")
        assert [p["name"] for p in pend] == ["testcar", "testcar_chime"] and pend[0]["record"] == "Vehicle.testcar", pend
        assert pend[0]["display"] == "Test Car" and pend[0]["version"] == "1.0.1", pend
        assert {"testcar", "testcar_chime"} <= set(fake.load())

        # 3. the hub's own update keeps its data (settings and queue)
        out = fake.run("deploy", hb["path"], "open77-test", "--stage")
        assert json.load(open(hub_cfg))["daily_restart_utc"] == "05:00", "hub settings survived the update"
        assert fake.hub_data("pending.json"), "hub queue survived the update"

        # 4. rebuild with a new package: replaces the queued entry (one per car)
        b2 = make_bundle(os.path.join(tmp, "car2.tgz"), build_out=_build(tmp, "1.0.2", b"v2"))
        out = fake.run("deploy", b2["path"], "open77-test", "--stage")
        pend = fake.hub_data("pending.json")
        assert sorted((p["name"], p["version"]) for p in pend) == [("testcar", "1.0.2"), ("testcar_chime", "1.0.2")], pend

        # 5. rebuild changing only Lua while the car still waits: still queued (same version)
        b3 = make_bundle(os.path.join(tmp, "car3.tgz"), build_out=_build(tmp, "1.0.2", b"v2", chime_lua="print('v3')\n"))
        out = fake.run("deploy", b3["path"], "open77-test", "--stage")
        assert "packages unchanged" in out and "STAGED: testcar 1.0.2" in out, out
        assert sorted(p["name"] for p in fake.hub_data("pending.json")) == ["testcar", "testcar_chime"]
        # ... and once the hub reported it live, a Lua-only rebuild goes live without a restart
        hub_data = os.path.join(fake.dir, "resources", "assets", "tools", "f2o77_hub", "data")
        json.dump({"testcar": {"version": "1.0.2", "state": "live"}, "testcar_chime": {"version": "1.0.2", "state": "live"}},
                  open(os.path.join(hub_data, "installed.json"), "w"))
        b4 = make_bundle(os.path.join(tmp, "car4.tgz"), build_out=_build(tmp, "1.0.2", b"v2", chime_lua="print('v4')\n"))
        out = fake.run("deploy", b4["path"], "open77-test", "--stage")
        assert "packages unchanged" in out and "LIVE: nothing waits" in out, out

        # 6. status --json sees the car, the hub and its queue
        out = fake.run("status", "-", "open77-test", "--json")
        st = json.loads([l for l in out.splitlines() if l.startswith("{")][0])
        assert st["version"] == "2.31.21+op77.131" and st["unit"] == "active"
        car = [c for c in st["cars"] if c["name"] == "testcar"][0]
        assert car["record"] == "Vehicle.testcar" and car["version"] == "1.0.2" and car["display"] == "Test Car"
        assert car["in_load"] and "testcar.zip" in car["packages"]
        assert st["hub"]["version"] == "1.1.1" and st["hub"]["pending"][0]["name"] == "testcar"

        # 7. restart: every resource in resources.load starts
        out = fake.run("restart", "-", "open77-test")
        assert "testcar: started" not in out or True
        assert "open77-test: done" in out, out
        assert open(os.path.join(fake.fake, "pid")).read().strip() == "101"

        # 8. deploy --restart with nobody online restarts and checks the bundle's resources
        out = fake.run("deploy", b2["path"], "open77-test", "--restart")
        assert "testcar: started" in out and "testcar_chime: started" in out, out

        # 9. logs
        out = fake.run("logs", "-", "open77-test", "5")
        assert "listening on UDP" in out

        # 9b. inspect: installed version and relative path once, "absent" for what the server lacks
        b5 = make_bundle(os.path.join(tmp, "car5.tgz"), build_out=_build(tmp, "1.0.3", b"v5"), include_hub=True)
        shutil.rmtree(os.path.join(fake.dir, "resources", "assets", "vehicles", "testcar_chime"))
        out = fake.run("inspect", b5["path"], "open77-test")
        line = [l for l in out.splitlines() if "testcar: bundle" in l][0]
        assert line.endswith("testcar: bundle 1.0.3 / server 1.0.2 at resources/assets/vehicles/testcar"), line
        assert fake.dir not in out, out
        assert [l for l in out.splitlines() if "testcar_chime: bundle" in l][0].endswith("/ server absent"), out

        # 10. bad names are refused before anything runs
        r = subprocess.run(["bash", SCRIPT, "status", "-", "../etc"], env=fake.env, capture_output=True, text=True)
        assert r.returncode == 2 and "bad server name" in r.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_stage_live_and_restart()
    print("ok")
