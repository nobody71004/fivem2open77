"""Maps on a fake Open77 server, end to end through the real scripts: map_deploy.sh (the payload's
run.sh) + server_deploy.sh + baker_install.sh, with a fake systemctl that really runs the baker
service and the server unit's start hook.

Covers: the map and the map manager land under resources/ and in resources.load and start; the
baker toolkit, its user service and the start hook (ExecStartPre promote, RestartForceExitStatus=10)
are installed; a request written the way the in-game manager writes it is built by the running
baker into the staging area, and the server restart installs it; a second map merges into the
manager; redeploying a map keeps the spot it was moved to in game; an older manager never replaces
a newer one; a server without the archivexl loader is refused.  The manager update (code and
wireframes, no restart) keeps where each map is baked, in the live def and in a staged build, waits
for the map baker's lock and refuses a wireframe that is not numbers.  Needs bash and python3."""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fivem2open77.mlo import bake as B  # noqa: E402
from fivem2open77.mlo import deploy as D  # noqa: E402
from fivem2open77.mlo import resource as R  # noqa: E402
from fivem2open77.mlo import toolkit as TK  # noqa: E402
from fivem2open77.mlo import ghost as G  # noqa: E402
from test_mlo import _FakeWk, _converted_map, _layout, _meshed_model  # noqa: E402
from test_mlo_baker import FAKE_WK_CLI  # noqa: E402
from test_server_deploy import Fake  # noqa: E402

DEPLOY = os.path.join(HERE, "..", "tools", "deploy")

# systemctl for the server unit (as test_server_deploy's) plus real user services: `start`/`restart`
# of a unit file under $XDG_CONFIG_HOME/systemd/user runs its ExecStart in the background; the
# server's restart runs the ExecStartPre lines of its drop-ins first (the start hook)
FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
[ "$1" = "--user" ] && shift
UD="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
cmd=$1; shift
unitfile() { echo "$UD/$1"; }
svc_pid() { cat "$FAKE/svc-$1.pid" 2>/dev/null; }
svc_alive() { local p; p=$(svc_pid "$1"); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
svc_start() {
  local u=$1 f; f=$(unitfile "$u")
  [ -f "$f" ] || { echo "no unit $u" >&2; return 5; }
  local envs=() line exe
  while IFS= read -r line; do envs+=("${line#Environment=}"); done < <(grep '^Environment=' "$f")
  exe=$(sed -n 's/^ExecStart=//p' "$f")
  ( env "${envs[@]}" setsid bash -c "exec $exe" >> "$FAKE/svc-$u.log" 2>&1 & echo $! > "$FAKE/svc-$u.pid" )
  sleep 0.3
}
svc_stop() { local p; p=$(svc_pid "$1"); [ -n "$p" ] && kill "$p" 2>/dev/null; rm -f "$FAKE/svc-$1.pid"; return 0; }
is_server() { [ "$1" = "$(basename "$(cat "$FAKE/dir")").service" ]; }
case "$cmd" in
  is-active)
    if is_server "$1"; then cat "$FAKE/state"; elif svc_alive "$1"; then echo active; else echo inactive; fi ;;
  show)
    prop=$2; u=$4
    case "$prop" in
      MainPID) cat "$FAKE/pid" ;;
      ActiveEnterTimestamp) echo "Wed 2026-10-07 12:00:00 UTC" ;;
      ExecStart) echo "{ path=$FAKE/Open77.Server ; argv[]=$FAKE/Open77.Server --config server.jsonc ; ignore_errors=no }" ;;
      WorkingDirectory) cat "$FAKE/dir" ;;
      Restart) echo "always" ;;
      *) echo "" ;;
    esac ;;
  kill)
    srv=$(cat "$FAKE/dir")
    u="$(basename "$srv").service"
    # the start hook(s) of the drop-ins, as systemd runs them before the new process
    for d in "$UD/$u.d"/*.conf; do
      [ -f "$d" ] || continue
      sed -n 's/^ExecStartPre=-\{0,1\}//p' "$d" | while IFS= read -r pre; do bash -c "$pre" >> "$FAKE/startpre.log" 2>&1; done
    done
    echo $(( $(cat "$FAKE/pid") + 1 )) > "$FAKE/pid"
    {
      echo "[INF] Server configuration: server.jsonc"
      echo "[INF] Required mods: 4 declared"
      for r in $(python3 -c "import json,re,sys; t=open(sys.argv[1]).read(); t=re.sub(r'(?<!:)//.*','',t); print(' '.join(json.loads(re.sub(r',(\s*[}\]])', r'\1', t))['resources']['load']))" "$srv/server.jsonc"); do
        echo "[INF] [resource:$r] started"
      done
      echo "[INF] listening on UDP 0.0.0.0:7777"
    } >> "$srv/server.log" ;;
  start) if is_server "$1"; then echo active > "$FAKE/state"; else svc_alive "$1" || svc_start "$1"; fi ;;
  restart) svc_stop "$1"; svc_start "$1" ;;
  stop) if is_server "$1"; then echo inactive > "$FAKE/state"; else svc_stop "$1"; fi ;;
  list-units)
    for f in "$FAKE"/svc-*.pid; do [ -f "$f" ] || continue; u=${f#"$FAKE"/svc-}; u=${u%.pid}
      svc_alive "$u" && echo "$u loaded active running fake"; done ;;
  daemon-reload|enable) ;;
  status) echo "fake status $1"; tail -5 "$FAKE/svc-$1.log" 2>/dev/null ;;
  *) echo "fake systemctl: $cmd" >&2 ;;
esac
"""

FAKE_DOTNET = """#!/usr/bin/env bash
dll=$1; shift
exec python3 "$(dirname "$dll")/fakewk.py" "$@"
"""


def _fake_toolkit(path, version="nettest-wkfake"):
    """toolkit/VERSION, a 'dotnet' that runs a fake WolvenKit CLI, and the real baker package."""
    with tarfile.open(path, "w:gz") as t:
        def add(arc, data, mode=0o644):
            info = tarfile.TarInfo(arc)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
        add("toolkit/VERSION", (version + "\n").encode())
        add("toolkit/dotnet/dotnet", FAKE_DOTNET.encode(), 0o755)
        add("toolkit/wolvenkit/WolvenKit.CLI.dll", b"not really a dll")
        add("toolkit/wolvenkit/fakewk.py", FAKE_WK_CLI.encode())
        for rel in TK.BAKER_LIB:
            with open(os.path.join(HERE, "..", *rel.split("/")), "rb") as f:
                add("toolkit/lib/" + rel, f.read())
    return path


class MapServer(Fake):
    """test_server_deploy's fake server, with user services and a home for the baker."""

    def __init__(self, tmp):
        super().__init__(tmp)
        sc = os.path.join(self.fake, "systemctl")
        with open(sc, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home)
        self.env.update(SYSTEMCTL=f"{sc} --user", HOME=self.home, XDG_CONFIG_HOME=os.path.join(self.home, ".config"),
                        XDG_RUNTIME_DIR=os.path.join(tmp, "run"))

    def stop_services(self):
        for fn in os.listdir(self.fake):
            if fn.startswith("svc-") and fn.endswith(".pid"):
                try:
                    os.killpg(int(open(os.path.join(self.fake, fn)).read().strip()), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    def manager(self):
        return os.path.join(self.dir, "resources", "assets", "tools", "f2o77_maps")

    def map_dir(self, name):
        return os.path.join(self.dir, "resources", "assets", "maps", name)


def _payload(tmp, bundle, toolkit=None, name="payload"):
    pay = os.path.join(tmp, name)
    os.makedirs(pay)
    shutil.copy(os.path.join(DEPLOY, "remote", "map_deploy.sh"), os.path.join(pay, "run.sh"))
    shutil.copy(os.path.join(DEPLOY, "server_deploy.sh"), pay)
    shutil.copy(bundle, os.path.join(pay, "bundle.tgz"))
    if toolkit:
        shutil.copy(os.path.join(DEPLOY, "remote", "baker_install.sh"), pay)
        shutil.copy(toolkit, os.path.join(pay, "baker-toolkit.tgz"))
    return pay


def _run(pay, srv, extra_env=None):
    env = dict(srv.env, **(extra_env or {}))
    r = subprocess.run(["bash", os.path.join(pay, "run.sh"), "open77-test"], env=env, capture_output=True, text=True,
                       timeout=300)
    return r.returncode, r.stdout + r.stderr


def _wait(cond, seconds=30.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = cond()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def test_maps_deploy_with_the_baker_end_to_end():
    if not shutil.which("bash") or not shutil.which("python3") or not shutil.which("sha256sum") or not shutil.which("setsid"):
        print("   (skipped: needs bash, python3, sha256sum, setsid)")
        return
    tmp = tempfile.mkdtemp(prefix="f2o77-mapdeploy-")
    srv = None
    try:
        res = _converted_map(os.path.join(tmp, "out"))
        B.bake(res, {"x": 480, "y": 1800, "z": 174}, _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-convert-1")
        bundle = D.bundle(res, os.path.join(tmp, "bundle.tgz"))["path"]
        toolkit = _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz"))
        pay = _payload(tmp, bundle, toolkit)
        srv = MapServer(tmp)

        # ---------------------------------------------------------------- first deploy
        rc, out = _run(pay, srv)
        assert rc == 0, out
        for n in ("cedar_crest", "f2o77_maps"):
            assert n in srv.load(), out
            assert f"{n}: started" in out, out
        assert os.path.isfile(os.path.join(srv.map_dir("cedar_crest"), "dist", "cedar_crest_sector.zip"))
        assert os.path.isfile(os.path.join(srv.manager(), "shared", "maps", "cedar_crest.lua"))
        baker_home = os.path.join(srv.home, ".f2o77", "baker")
        assert open(os.path.join(baker_home, "VERSION")).read().strip() == "nettest-wkfake"
        ud = os.path.join(srv.home, ".config", "systemd", "user")
        unit = open(os.path.join(ud, "f2o77-mapbaker-open77-test.service")).read()
        assert "fivem2open77.mlo.baker run --server-dir " + srv.dir in unit and "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1" in unit
        hook = open(os.path.join(ud, "open77-test.service.d", "f2o77-maps.conf")).read()
        assert "ExecStartPre=-" in hook and "baker promote --server-dir " + srv.dir in hook and "RestartForceExitStatus=10" in hook
        assert _read(os.path.join(srv.dir, "f2o77-maps", "hook.json"))["unit"] == "open77-test.service"
        assert "f2o77-mapbaker-open77-test.service: active" in out, out
        assert "start hook:" in out and "WolvenKit: 8.17.4" in out, out

        # ---------------------------------------------------------------- the baker serves the manager
        bake_dir = os.path.join(srv.manager(), "data", "bake")
        info = _wait(lambda: _read(os.path.join(bake_dir, "baker.json")), what="baker.json")
        assert info["hook"] is True and info["serverDir"] == srv.dir
        # west and south of the centre: the spot starts with a minus sign (Sea Dome's, where the deploy's
        # rebuild once failed with "argument --anchor: expected one argument")
        req = {"id": "cedar_crest-1760000000-1", "action": "bake", "map": "cedar_crest", "resource": "cedar_crest",
               "anchor": {"x": -3096.573, "y": -848.458, "z": 67.991, "yaw": -41.65}, "requestedBy": "player1 (#1)",
               "requestedAt": 1760000000, "mode": "now"}
        os.makedirs(os.path.join(bake_dir, "requests"), exist_ok=True)
        with open(os.path.join(bake_dir, "requests", "cedar_crest.json"), "w") as f:
            json.dump(req, f)
        st = _wait(lambda: (_read(os.path.join(bake_dir, "status", "cedar_crest.json")) or {}).get("state") in ("done", "failed")
                   and _read(os.path.join(bake_dir, "status", "cedar_crest.json")), what="the baker's answer")
        log = open(os.path.join(srv.fake, "svc-f2o77-mapbaker-open77-test.service.log")).read()
        assert st["state"] == "done", (st, log)
        assert _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))["id"] == "cedar_crest-convert-1", \
            "nothing changes under the running server"
        # ---------------------------------------------------------------- the restart installs it
        subprocess.run(srv.env["SYSTEMCTL"].split() + ["kill", "--signal=SIGINT", "open77-test.service"], env=srv.env,
                       check=True, timeout=120)
        pre = open(os.path.join(srv.fake, "startpre.log")).read()
        assert "1 build(s) installed" in pre, pre
        baked = _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))
        assert baked["id"] == req["id"] and baked["anchor"]["yaw"] == -41.65
        assert 'bakeId = "cedar_crest-1760000000-1"' in open(os.path.join(srv.manager(), "shared", "maps", "cedar_crest.lua")).read()
        assert _read(os.path.join(bake_dir, "status", "cedar_crest.json"))["state"] == "promoted"
        with open(os.path.join(srv.manager(), "data", "agent_token.txt"), "w") as f:
            f.write("a-token-the-server-made-at-start\n")

        # ---------------------------------------------------------------- a second map joins the manager
        out2 = os.path.join(tmp, "out2")
        os.makedirs(out2)
        res2 = _converted_map(out2)
        L2 = _layout("harbor_dock")
        res2b = os.path.join(out2, "harbor_dock")
        os.rename(res2, res2b)
        R.write_map_resource(res2b, L2, L2["packages"], None)
        os.rename(os.path.join(res2b, "dist", "cedar_crest.zip"), os.path.join(res2b, "dist", "harbor_dock.zip"))
        B.bake(res2b, {"x": -100, "y": 50, "z": 5}, _FakeWk(), log=lambda *a: None, bake_id="harbor_dock-convert-1")
        b2 = D.bundle(res2b, os.path.join(tmp, "bundle2.tgz"))["path"]
        rc, out = _run(_payload(tmp, b2, toolkit, name="payload2"), srv)
        assert rc == 0, out
        maps = sorted(os.listdir(os.path.join(srv.manager(), "shared", "maps")))
        assert maps == ["_index.lua", "cedar_crest.lua", "harbor_dock.lua"], maps
        assert "toolkit nettest-wkfake already installed" in out, out
        assert open(os.path.join(srv.manager(), "data", "agent_token.txt")).read().startswith("a-token"), "data/ kept"
        assert "harbor_dock: started" in out and "f2o77_maps: started" in out, out

        # ---------------------------------------------------------------- redeploying cedar_crest keeps its in-game spot
        rc, out = _run(pay, srv)
        assert rc == 0, out
        assert "was moved in game to -3096.573,-848.458,67.991,-41.65" in out, out
        assert "could not rebuild" not in out and "expected one argument" not in out, out
        baked = _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))
        assert baked["anchor"] == {"x": -3096.573, "y": -848.458, "z": 67.991, "yaw": -41.65} and "-deploy-" in baked["id"], baked
        mdef = open(os.path.join(srv.manager(), "shared", "maps", "cedar_crest.lua")).read()
        assert baked["id"] in mdef and "-848.458" in mdef
        assert '"kept": ["cedar_crest"]' in out, out
        rc, out = _run(pay, srv, {"MAP_DEPLOY_KEEP_PLACEMENT": "0"})
        assert rc == 0 and _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))["id"] == "cedar_crest-convert-1"

        # ---------------------------------------------------------------- an older manager never replaces a newer one
        man = srv.manager()
        txt = open(os.path.join(man, "open77.lua")).read().replace(f'version "{R.MANAGER_VERSION}"', 'version "9.0.0"')
        with open(os.path.join(man, "open77.lua"), "w") as f:
            f.write(txt)
        with open(os.path.join(man, "server", "main.lua"), "a") as f:
            f.write("\n-- newer code\n")
        rc, out = _run(pay, srv)
        assert rc == 0 and f"code 9.0.0 kept (bundle {R.MANAGER_VERSION})" in out, out
        assert open(os.path.join(man, "server", "main.lua")).read().endswith("-- newer code\n")
        assert sorted(os.listdir(os.path.join(man, "shared", "maps"))) == ["_index.lua", "cedar_crest.lua", "harbor_dock.lua"]

        # ---------------------------------------------------------------- status shows maps and the manager
        r = subprocess.run(["bash", os.path.join(DEPLOY, "server_deploy.sh"), "status", "-", "open77-test", "--json"],
                           env=srv.env, capture_output=True, text=True, timeout=120)
        s = json.loads(r.stdout)
        assert sorted(m["name"] for m in s["maps"]) == ["cedar_crest", "harbor_dock"] and not s["cars"], s
        assert s["mapManager"]["maps"] == ["cedar_crest", "harbor_dock"] and s["mapManager"]["baker"]["hook"] is True

        # ---------------------------------------------------------------- a server without the archivexl loader
        srv.stop_services()
        srv2 = MapServer(os.path.join(tmp, "two"))
        shutil.rmtree(os.path.join(srv2.dir, "resources", "assets", "loaders", "archivexl"))
        cfg = os.path.join(srv2.dir, "server.jsonc")
        open(cfg, "w").write(open(cfg).read().replace('"archivexl",\n', ""))
        rc, out = _run(pay, srv2)
        assert rc == 3 and "no archivexl loader" in out, out
        assert not os.path.exists(os.path.join(srv2.dir, "resources", "assets", "maps"))
        with tarfile.open(bundle) as t:
            assert not [m for m in t.getnames() if "/data/" in m]
    finally:
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


def test_prod_deploy_puts_cars_and_maps_on_a_server_in_one_go():
    """prod_deploy.sh (Deploy-Production.bat): the cars, then the maps with the manager and the
    baker, one restart when nobody is online; a second run keeps a map's in-game spot."""
    if not shutil.which("bash") or not shutil.which("setsid"):
        print("   (skipped: needs bash and setsid)")
        return
    from test_server_deploy import _resource
    from fivem2open77.studio.deploy import make_bundle
    tmp = tempfile.mkdtemp(prefix="f2o77-proddeploy-")
    srv = None
    try:
        res = _converted_map(os.path.join(tmp, "out"))
        B.bake(res, {"x": -480, "y": -1800, "z": 74}, _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-convert-1")
        bundle = D.bundle(res, os.path.join(tmp, "bundle.tgz"))["path"]
        cars_out = os.path.join(tmp, "cars")
        _resource(cars_out, "testcar", "1.2.3", preload=b"car", display="Test Car", record="Vehicle.testcar")
        cars = make_bundle(os.path.join(tmp, "cars.tgz"), build_out=cars_out)["path"]
        pay = _payload(tmp, bundle, _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz")))
        os.rename(os.path.join(pay, "run.sh"), os.path.join(pay, "map_deploy.sh"))
        shutil.copy(os.path.join(DEPLOY, "remote", "prod_deploy.sh"), pay)
        shutil.copy(cars, os.path.join(pay, "cars.tgz"))
        srv = MapServer(tmp)

        def run():
            r = subprocess.run(["bash", os.path.join(pay, "prod_deploy.sh")], env=dict(srv.env, SERVER_NAMES="open77-test nothere"),
                               capture_output=True, text=True, timeout=600)
            return r.returncode, r.stdout + r.stderr

        rc, out = run()
        assert rc == 2, out[-3000:]                        # "nothere" is missing: said, and the rest done
        assert "!! no Open77 server at" in out and '"open77-test": {"cars": "ok", "maps": "ok", "worlds": "skipped"}' in out, out[-2000:]
        for n in ("testcar", "cedar_crest", "f2o77_maps"):
            assert n in srv.load(), (n, srv.load())
        assert "MAP-DEPLOY-RESULT" in out and '"restarted": 1' in out, out[-2000:]
        assert os.path.isfile(os.path.join(srv.home, ".f2o77", "baker", "VERSION"))
        # moved in game west and south of the centre: the next deploy rebuilds it there
        man, mapd = srv.manager(), srv.map_dir("cedar_crest")
        layout = json.load(open(os.path.join(mapd, "layout.json")))
        spot = {"x": -3096.573, "y": -848.458, "z": 67.991, "yaw": -41.65}
        R.write_map_def(man, layout, spot, open(os.path.join(mapd, "ghost.txt")).read(), "cedar_crest-1760000001-1")
        b = json.load(open(os.path.join(mapd, "baked.json")))
        b.update(anchor=spot, id="cedar_crest-1760000001-1")
        json.dump(b, open(os.path.join(mapd, "baked.json"), "w"))
        rc, out = run()
        assert "was moved in game to -3096.573,-848.458,67.991,-41.65" in out and "could not rebuild" not in out, out[-3000:]
        assert json.load(open(os.path.join(mapd, "baked.json")))["anchor"] == spot
    finally:
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


def _ghost_in(def_path, name="cedar_crest"):
    import re
    t = open(def_path, encoding="utf-8").read()
    m = re.search(r'F2O77_MAPS\["%s"\]\.ghost = \[(=*)\[\n(.*?)\]\1\]' % name, t, re.S)
    return m.group(2) if m else None


def test_manager_update_without_a_restart():
    if not shutil.which("bash") or not shutil.which("python3") or not shutil.which("sha256sum"):
        print("   (skipped: needs bash, python3, sha256sum)")
        return
    import fcntl
    import threading
    tmp = tempfile.mkdtemp(prefix="f2o77-mgrupdate-")
    try:
        res = _converted_map(os.path.join(tmp, "out"))
        B.bake(res, {"x": 480, "y": 1800, "z": 174}, _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-convert-1")
        srv = MapServer(tmp)
        rc, out = _run(_payload(tmp, D.bundle(res, os.path.join(tmp, "bundle.tgz"))["path"]), srv)
        assert rc == 0, out
        man, mapd = srv.manager(), srv.map_dir("cedar_crest")
        old_ghost = open(os.path.join(mapd, "ghost.txt")).read()
        # what the server looks like after a while: an older manager, a move applied in game, another
        # one built and staged for the next restart, the manager's own data
        layout = json.load(open(os.path.join(mapd, "layout.json")))
        R.write_map_def(man, layout, {"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0}, old_ghost, "cedar_crest-1760000000-1")
        staged = os.path.join(srv.dir, "f2o77-maps", "staged", "cedar_crest")
        os.makedirs(os.path.join(staged, "manager"))
        with open(os.path.join(staged, "build.json"), "w") as f:
            json.dump({"id": "cedar_crest-1760000000-2", "map": "cedar_crest", "managerDef": "manager/cedar_crest.lua"}, f)
        with open(os.path.join(staged, "manager", "cedar_crest.lua"), "w") as f:
            f.write(R.map_def_lua(layout, {"x": 700.0, "y": 1950.0, "z": 210.0, "yaw": 90.0}, old_ghost, "cedar_crest-1760000000-2"))
        with open(os.path.join(man, "open77.lua")) as f:
            manifest = f.read()
        with open(os.path.join(man, "open77.lua"), "w") as f:
            f.write(manifest.replace(f'version "{R.MANAGER_VERSION}"', 'version "1.0.0"'))
        with open(os.path.join(man, "client", "main.lua"), "w") as f:
            f.write("-- the old client\n")
        os.makedirs(os.path.join(man, "data"), exist_ok=True)
        with open(os.path.join(man, "data", "state.json"), "w") as f:
            f.write('{"maps": {"cedar_crest": {"pending": {"x": 1}}}}')
        pid0 = open(os.path.join(srv.fake, "pid")).read()

        new_ghost = G.encode(G.ghost_lines(_meshed_model()))
        assert " 4\n" in new_ghost and new_ghost != old_ghost
        from fivem2open77.mlo import roads as RD
        roads_dir = os.path.join(tmp, "roads")
        RD.write_tiles([{"id": 1, "kind": "r", "width": 5.0, "speed": 15, "material": "",
                         "points": [(400.0, 1800.0, 170.0), (700.0, 1800.0, 170.0)]}], roads_dir, source="test")
        upd = D.manager_bundle(os.path.join(tmp, "update.tgz"), ghosts={"cedar_crest": new_ghost, "nowhere": new_ghost},
                               roads=roads_dir)
        assert upd["manager"] == R.MANAGER_VERSION and upd["ghosts"]["cedar_crest"] == len(new_ghost.strip().splitlines())
        assert upd["roads"] == 2, upd
        with tarfile.open(upd["path"]) as t:
            names = t.getnames()
        assert "resources/assets/tools/f2o77_maps/client/main.lua" in names and "ghosts/cedar_crest.txt" in names
        assert "resources/assets/tools/f2o77_maps/server/roads.lua" in names
        assert "roads/index.json" in names and "roads/0_3.lanes" in names and "roads/1_3.lanes" in names, names
        assert not [n for n in names if "/data/" in n or "/shared/maps/cedar" in n], names

        pay = os.path.join(tmp, "update-pay")
        os.makedirs(pay)
        shutil.copy(os.path.join(DEPLOY, "remote", "manager_update.sh"), os.path.join(pay, "run.sh"))
        shutil.copy(os.path.join(DEPLOY, "server_deploy.sh"), pay)
        shutil.copy(upd["path"], os.path.join(pay, "update.tgz"))
        subprocess.run("sha256sum update.tgz > update.sha256", shell=True, cwd=pay, check=True)

        # the map baker is building something: the wireframe waits for its lock
        lock = os.path.join(srv.dir, "f2o77-maps", "baker.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(2.0, lambda: os.close(fd)).start()
        t0 = time.time()
        rc, out = _run(pay, srv)
        assert rc == 0 and out.rstrip().endswith('MANAGER-UPDATE-RESULT {"rc": 0}'), out
        assert time.time() - t0 >= 1.8, "it waited for the baker's lock"
        assert f"f2o77_maps code 1.0.0 -> {R.MANAGER_VERSION}" in out, out
        assert "nowhere is not installed here: its wireframe skipped" in out, out
        assert "where it is baked kept; the staged build too" in out and "outline" in out and "gates" in out, out
        assert "LIVE: f2o77_maps reloads by itself" in out, out
        assert "road data installed (2 tiles" in out, out
        assert sorted(os.listdir(os.path.join(man, "data", "roads"))) == ["0_3.lanes", "1_3.lanes", "index.json"]
        assert open(os.path.join(srv.fake, "pid")).read() == pid0, "no restart"
        src = os.path.join(HERE, "..", "fivem2open77", "mlo", "manager")
        for rel in ("client/main.lua", "server/main.lua", "web/app.js", "web/index.html", "web/app.css", "shared/config.lua"):
            assert open(os.path.join(man, rel)).read() == open(os.path.join(src, rel)).read(), rel
        assert f'version "{R.MANAGER_VERSION}"' in open(os.path.join(man, "open77.lua")).read()
        assert open(os.path.join(man, "data", "state.json")).read().startswith('{"maps"'), "data/ kept"
        live = open(os.path.join(man, "shared", "maps", "cedar_crest.lua")).read()
        assert 'bakeId = "cedar_crest-1760000000-1"' in live and "600.0" in live and "45.0" in live, live[:800]
        assert _ghost_in(os.path.join(man, "shared", "maps", "cedar_crest.lua")).strip() == new_ghost.strip()
        assert live.count("-- wireframe:") == 1 and R.GHOST_COMMENT in live
        st = os.path.join(staged, "manager", "cedar_crest.lua")
        assert 'bakeId = "cedar_crest-1760000000-2"' in open(st).read() and "700.0" in open(st).read()
        assert _ghost_in(st).strip() == new_ghost.strip()
        assert open(os.path.join(mapd, "ghost.txt")).read().strip() == new_ghost.strip(), "later bakes use it"
        bks = [d for d in os.listdir(os.path.join(tmp, "backups")) if "-f2o77-manager-" in d]
        assert len(bks) == 1
        bk = os.path.join(tmp, "backups", bks[0])
        assert open(os.path.join(bk, "resources", "f2o77_maps", "client", "main.lua")).read() == "-- the old client\n"
        assert open(os.path.join(bk, "ghosts", "cedar_crest", "ghost.txt")).read() == old_ghost
        # the def the server's Lua loads still parses, with the new wireframe in it
        lua = shutil.which("lua5.4") or shutil.which("lua")
        if lua:
            script = os.path.join(tmp, "check_def.lua")
            with open(script, "w") as f:
                f.write("dofile(arg[1]); local m = F2O77_MAPS.cedar_crest\n"
                        "assert(m.bakeId == 'cedar_crest-1760000000-1' and m.baked.x == 600.0)\n"
                        "assert(m.ghost:find(' 4\\n')); print('lua ok')\n")
            chk = subprocess.run([lua, script, os.path.join(man, "shared", "maps", "cedar_crest.lua")],
                                 capture_output=True, text=True)
            assert chk.returncode == 0 and "lua ok" in chk.stdout, chk.stdout + chk.stderr

        # again: nothing to do
        rc, out = _run(pay, srv)
        assert rc == 0 and "already in place" in out and "already this one" in out and "nothing changed" in out, out
        assert "road data already in place (2 tiles)" in out, out

        # a wireframe that is not numbers never reaches the Lua (it would end the long string)
        evil = os.path.join(tmp, "evil.tgz")
        with tarfile.open(evil, "w:gz") as t:
            data = b"1 2 3 4 5 6 1\n]] print('owned') --[[ 1 1 1 1 1 1\n"
            info = tarfile.TarInfo("ghosts/cedar_crest.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        before = open(os.path.join(man, "shared", "maps", "cedar_crest.lua")).read()
        r = subprocess.run(["bash", os.path.join(DEPLOY, "server_deploy.sh"), "manager-update", evil, "open77-test"],
                           env=srv.env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 1 and "wireframe refused: line 2 is not" in r.stdout, r.stdout + r.stderr
        assert open(os.path.join(man, "shared", "maps", "cedar_crest.lua")).read() == before
        assert open(os.path.join(mapd, "ghost.txt")).read().strip() == new_ghost.strip()

        # a newer manager on the server keeps its code; the wireframes still go in
        with open(os.path.join(man, "open77.lua")) as f:
            manifest = f.read()
        with open(os.path.join(man, "open77.lua"), "w") as f:
            f.write(manifest.replace(f'version "{R.MANAGER_VERSION}"', 'version "9.0.0"'))
        upd2 = D.manager_bundle(os.path.join(tmp, "update2.tgz"), ghosts={"cedar_crest": old_ghost})
        r = subprocess.run(["bash", os.path.join(DEPLOY, "server_deploy.sh"), "manager-update", upd2["path"], "open77-test"],
                           env=srv.env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and f"9.0.0 is newer than this update's {R.MANAGER_VERSION}: code kept" in r.stdout, r.stdout
        assert _ghost_in(os.path.join(man, "shared", "maps", "cedar_crest.lua")).strip() == old_ghost.strip()

        # the command line builds the same update from a converted map folder
        cli = subprocess.run([sys.executable, "-m", "fivem2open77", "mlo-manager-bundle", "--out", os.path.join(tmp, "u3.tgz"),
                              "--map", res, "--json"], capture_output=True, text=True, cwd=os.path.join(HERE, ".."), timeout=120)
        j = json.loads(cli.stdout)
        assert j["ok"] and j["ghosts"] == {"cedar_crest": len(old_ghost.strip().splitlines())}, cli.stdout + cli.stderr
    finally:
        if "srv" in locals():
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_maps_deploy_with_the_baker_end_to_end()
    print("ok   test_maps_deploy_with_the_baker_end_to_end")
    test_manager_update_without_a_restart()
    print("ok   test_manager_update_without_a_restart")
