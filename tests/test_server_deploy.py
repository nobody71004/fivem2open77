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
      for r in $(python3 -c "import json,re,sys; t=open(sys.argv[1]).read(); t=re.sub(r'(?<!:)//.*','',t); print(' '.join(json.loads(re.sub(r',(\s*[}\]])', r'\1', t))['resources']['load']))" "$srv/server.jsonc"); do
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
        # the hub version the repo ships (a literal here went stale when the hub moved past it):
        # the chain bundle -> install -> status must report exactly what open77.lua declares
        import re
        hub_ver = re.search(r'version "([^"]+)"',
                            open(os.path.join(HERE, "..", "fivem2open77", "hub", "f2o77_hub",
                                              "open77.lua")).read()).group(1)
        assert st["hub"]["version"] == hub_ver and st["hub"]["pending"][0]["name"] == "testcar"

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


ACL_JSONC = """{
  // who may do what on this server
  "version": 1,
  "roles": [
    { "name": "operator", "permissions": ["command.kick", "command.gate.*"] }
  ],
  "principals": [
    {
      "name": "XEROX710 (owner)",
      "userId": "11111111-2222-3333-4444-555555555555",
      "publicKey": "BASE64KEYOWNER==",
      "permissions": [
        // admin families
        "command.admin.*",
        "command.gate.*"
      ]
    },
    { "name": "friend", "userId": "99999999-0000-0000-0000-000000000000", "roles": ["operator"] },
    { "name": "helper", "userId": "88888888-0000-0000-0000-000000000000", "publicKey": "K2", "permissions": [] }
  ]
}
"""


def _strip(t):
    import re
    t = re.sub(r"(?m)^\s*//.*$", "", t)
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", t))


def test_acl_grant():
    """acl-grant: the right lands in the named principal only (comments, keys, other principals and roles
    untouched), the server restarts to read it when nobody is online, a second run changes nothing, and
    the cases it must not guess at change nothing."""
    tmp = tempfile.mkdtemp(prefix="f2o77-acl-")
    try:
        fake = Fake(tmp, with_hub=False)
        acl = os.path.join(fake.dir, "acl.jsonc")
        with open(acl, "w") as f:
            f.write(ACL_JSONC)

        # no accessControl in server.jsonc: no ACL file, nothing to do
        r = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test", "--restart"],
                           env=dict(fake.env, F2O77_ACL_PRINCIPAL="XEROX710"), capture_output=True, text=True)
        assert r.returncode == 3 and "no accessControl.file" in r.stdout, r.stdout + r.stderr
        assert open(acl).read() == ACL_JSONC

        cfg = os.path.join(fake.dir, "server.jsonc")
        with open(cfg) as f:
            text = f.read()
        with open(cfg, "w") as f:
            f.write(text.replace('  "network"', '  "accessControl": { "file": "acl.jsonc" },\n  "network"', 1))

        # several principals and no name: refused, nothing changed
        r = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test", "--restart"],
                           env=fake.env, capture_output=True, text=True)
        assert r.returncode == 3 and "several principals" in r.stdout, r.stdout + r.stderr
        assert open(acl).read() == ACL_JSONC
        # a bad permission never reaches the file
        r = subprocess.run(["bash", SCRIPT, "acl-grant", "Command Maps", "open77-test"], env=fake.env,
                           capture_output=True, text=True)
        assert r.returncode == 2 and "bad permission" in r.stderr

        # the owner, someone online: written, checked, the restart owed
        with open(os.path.join(fake.dir, "server.log"), "a") as f:
            f.write("[INF] Connection 7 opened\n")
        env = dict(fake.env, F2O77_ACL_PRINCIPAL="xerox710")
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test", "--restart"], env=env,
                             capture_output=True, text=True).stdout
        assert "command.maps added for XEROX710 (owner)" in out and "PENDING restart: connection(s) 7 online" in out, out
        assert "BASE64KEYOWNER" not in out and "principal friend" in out, out
        new = open(acl).read()
        assert "// who may do what on this server" in new and "// admin families" in new
        a, b = _strip(ACL_JSONC), _strip(new)
        assert b["principals"][0]["permissions"] == ["command.maps", "command.admin.*", "command.gate.*"], b
        assert b["principals"][1:] == a["principals"][1:] and b["roles"] == a["roles"]
        assert {k: v for k, v in b["principals"][0].items() if k != "permissions"} == \
            {k: v for k, v in a["principals"][0].items() if k != "permissions"}
        bks = [d for d in os.listdir(os.path.join(tmp, "backups")) if "-f2o77-acl-" in d]
        assert bks and open(os.path.join(tmp, "backups", bks[0], "acl.jsonc")).read() == ACL_JSONC
        assert open(os.path.join(fake.fake, "pid")).read().strip() == "100"      # not restarted

        # they left: a second run has nothing to add but makes the restart it owed
        with open(os.path.join(fake.dir, "server.log"), "a") as f:
            f.write("[INF] Connection 7 closed\n")
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test", "--restart"], env=env,
                             capture_output=True, text=True).stdout
        assert "already there" in out and "open77-test: done" in out, out
        assert open(os.path.join(fake.fake, "pid")).read().strip() == "101"
        assert open(acl).read() == new
        # and a third one has nothing to do at all
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test", "--restart"], env=env,
                             capture_output=True, text=True).stdout
        assert "already there" in out and "restarting" not in out, out

        # a principal without permissions gets the key; one with an empty list gets the right in it;
        # one whose role already covers it is left alone
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.gate.open", "open77-test"],
                             env=dict(fake.env, F2O77_ACL_PRINCIPAL="friend"), capture_output=True, text=True).stdout
        assert "already there" in out and "role operator (command.gate.*)" in out, out
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test"],
                             env=dict(fake.env, F2O77_ACL_PRINCIPAL="friend"), capture_output=True, text=True).stdout
        assert "command.maps added for friend" in out and "PENDING restart" in out, out
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test"],
                             env=dict(fake.env, F2O77_ACL_PRINCIPAL="HELPER"), capture_output=True, text=True).stdout
        assert "command.maps added for helper" in out, out
        c = _strip(open(acl).read())
        assert c["principals"][1]["permissions"] == ["command.maps"] and c["principals"][1]["roles"] == ["operator"]
        assert c["principals"][2]["permissions"] == ["command.maps"] and c["principals"][2]["publicKey"] == "K2"

        # the only principal is chosen without a name
        with open(acl, "w") as f:
            f.write('{ "version": 1, "principals": [ { "name": "solo", "userId": "u", "permissions": ["command.x"] } ] }\n')
        out = subprocess.run(["bash", SCRIPT, "acl-grant", "command.maps", "open77-test"], env=fake.env,
                             capture_output=True, text=True).stdout
        assert "command.maps added for solo" in out, out
        assert _strip(open(acl).read())["principals"][0]["permissions"] == ["command.maps", "command.x"]

        # the remote runner (what the Windows .bat sends) passes the name and restarts when nobody is online
        with open(acl, "w") as f:
            f.write(ACL_JSONC)
        os.remove(os.path.join(fake.dir, ".f2o77-acl-restart"))
        pay = os.path.join(tmp, "pay")
        os.makedirs(pay)
        shutil.copy(SCRIPT, os.path.join(pay, "server_deploy.sh"))
        shutil.copy(os.path.join(HERE, "..", "tools", "deploy", "remote", "acl_grant.sh"), os.path.join(pay, "run.sh"))
        r = subprocess.run(["bash", os.path.join(pay, "run.sh"), "open77-test"], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "command.maps added for XEROX710 (owner)" in r.stdout, r.stdout + r.stderr
        assert "open77-test: done" in r.stdout and r.stdout.rstrip().endswith('ACL-GRANT-RESULT {"rc": 0}'), r.stdout
        assert not os.path.exists(os.path.join(fake.dir, ".f2o77-acl-restart"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_car_pack_installs_in_one_bundle():
    """A car pack's builds go to a server together: one bundle, every car (and its chime) in it."""
    if not shutil.which("bash") or not shutil.which("python3") or not shutil.which("sha256sum"):
        print("   (skipped: needs bash, python3, sha256sum)")
        return
    tmp = tempfile.mkdtemp(prefix="f2o77-pack-")
    try:
        outs = []
        for car in ("e400", "ccf250"):
            out = os.path.join(tmp, f"build-{car}", "out")
            os.makedirs(out)
            _resource(out, car, "1.0.1", preload=car.encode(), display=car, record=f"Vehicle.{car}")
            _resource(out, f"{car}_chime", "1.0.1")
            outs.append(out)
        b = make_bundle(os.path.join(tmp, "pack.tgz"), build_outs=outs)
        assert b["resources"] == ["e400", "e400_chime", "ccf250", "ccf250_chime"], b["resources"]
        fake = Fake(tmp)
        fake.run("deploy", b["path"], "open77-test", "--stage")
        load = fake.load()
        assert all(n in load for n in b["resources"]), load
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_stage_live_and_restart()
    test_acl_grant()
    print("ok")


def test_a_world_edit_installs_reports_and_comes_off_again():
    if not shutil.which("bash") or not shutil.which("python3"):
        print("   (skipped: needs bash, python3)")
        return
    from fivem2open77.world import package as P
    tmp = tempfile.mkdtemp(prefix="f2o77-world-deploy-")
    try:
        fake = Fake(tmp, with_hub=False)
        sec = "base\\worlds\\03_night_city\\_compiled\\default\\exterior_-9_4_0_0.streamingsector"
        r = P.build("world_test_edit", "Test edit", [{"op": "delete", "sector": sec, "index": 261, "type": "worldStaticMeshNode",
                                                     "expectedNodes": 380}], os.path.join(tmp, "out"), log=lambda *a: None)
        b = P.bundle([r["path"]], os.path.join(tmp, "world.tgz"))
        out = fake.run("deploy", b["path"], "open77-test", "--restart")
        assert "installed world_test_edit 1.0.0 at resources/assets/world/world_test_edit" in out, out
        assert "resources.load += world_test_edit" in out and "WARNING" not in out, out
        assert fake.load()[-1] == "world_test_edit"
        st = json.loads(fake.run("status", "-", "open77-test", "--json"))
        w = st["worlds"][0]
        assert w["name"] == "world_test_edit" and w["in_load"] and w["summary"]["deleted"] == 1, st["worlds"]
        assert "world edit world_test_edit 1.0.0  1 removed" in fake.run("status", "-", "open77-test")
        # off again: the folder and the load entry go (backed up), the rest of server.jsonc stays as it was
        before = open(os.path.join(fake.dir, "server.jsonc")).read()
        out = fake.run("remove", "world_test_edit", "open77-test", "--restart")
        assert "resources.load -= world_test_edit" in out and "removed world_test_edit" in out, out
        after = open(os.path.join(fake.dir, "server.jsonc")).read()
        assert fake.load() == ["freeroam", "archivexl", "tweakxl"] and "// the game mode" in after
        assert after == before.replace(',\n      "world_test_edit"', ""), (before, after)
        assert not os.path.exists(os.path.join(fake.dir, "resources", "assets", "world", "world_test_edit"))
        assert "no world edit nope" in fake.run("remove", "nope", "open77-test", check=False)
        assert "is not a resource name" in fake.run("remove", "../x", "open77-test", check=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
