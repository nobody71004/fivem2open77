"""The Studio end to end with the real tools, the way a user drives it:

  a FiveM map (Cedar Crest) imported and looked at -> its spot and names set -> built with WolvenKit
  and baked -> installed in (and removed from) a game folder -> installed over SSH on a fake Open77
  host with the REAL map baker toolkit the Studio packs (the Linux .NET runtime + the WolvenKit CLI +
  the baker) -> a move requested the way the in-game manager requests it is built on the host by that
  baker (WolvenKit under the toolkit's runtime) -> the server's start hook installs it at the restart
  -> a second install only refreshes the baker's package.

Opt-in (about 8 minutes): F2O77_TEST_MAP (default the Cedar Crest test copy), a WolvenKit CLI
(F2O77_TEST_WOLVENKIT / WOLVENKIT_CLI), a .NET 8 runtime for Linux (DOTNET_ROOT or /opt/dotnet), the
template cache (F2O77_TEST_CACHE) and root + OpenSSH.  Skipped when any is missing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

MAP = os.environ.get("F2O77_TEST_MAP", "/home/claude/work/src_cedar")
WK = os.environ.get("F2O77_TEST_WOLVENKIT") or os.environ.get("WOLVENKIT_CLI") or "/home/claude/tools/wk8174/WolvenKit.CLI.dll"
CACHE = os.environ.get("F2O77_TEST_CACHE", "/home/claude/cache/donor")
DOTNET = os.environ.get("DOTNET_ROOT") or "/opt/dotnet"


def _ready():
    import sshd_fixture
    from fivem2open77.studio.tools import _is_linux_dotnet_root
    return (os.path.exists(MAP) and os.path.isfile(WK) and os.path.isdir(CACHE) and _is_linux_dotnet_root(DOTNET)
            and sshd_fixture.available())


def _job(studio, job, seconds):
    t0 = time.time()
    studio.jobs.wait(job, seconds)
    assert job.state == "done", (job.title, job.state, job.error, [x["text"] for x in job.lines][-30:])
    return job.result, time.time() - t0


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _wait(cond, seconds, what):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = cond()
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def test_studio_map_end_to_end_with_the_real_tools():
    if not _ready():
        print("   (skipped: needs the test map, WolvenKit, .NET for Linux, the template cache and root + OpenSSH)")
        return
    import sshd_fixture
    from fivem2open77.mlo import toolkit as TK
    from fivem2open77.studio.app import Studio
    from test_mlo_deploy import MapServer
    tmp = tempfile.mkdtemp(prefix="f2o77-e2e-")
    srv = studio = None
    os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    try:
        studio = Studio(os.path.join(tmp, "ws"))
        studio.settings.update({"paths": {"wolvenkit": WK, "dotnet": os.path.join(DOTNET, "dotnet"), "cache_dir": CACHE,
                                          "linux_dotnet": DOTNET}})
        tb = studio.tools_status(refresh=True)
        assert tb["wolvenkit"]["ok"] and tb["linux_dotnet"]["ok"], tb

        # ------------------------------------------------------------ import + look
        p, job = studio.create_project(path=MAP, kind="map")
        imported, _ = _job(studio, job, 1200)            # the import names the job it starts next: the look
        look, secs = _job(studio, studio.jobs.get(imported["next_job"]), 1200)
        print(f"   look {secs:.0f} s: {look['placements']} placements, {look['size']}")
        assert look["placements"] > 100 and look["parts"] > 10
        r = studio.map_set_options(p.id, {"name": "cedar_crest", "display_name": "Cedar Crest Estate",
                                          "place": {"x": 480, "y": 1800, "z": 174, "yaw": 0}})
        assert r["look_job"] is None
        st = studio.map_state(p.id)
        assert st["look"]["map"] == "cedar_crest" and st["project"]["map"]["name"] == "cedar_crest"
        assert studio.map_render(p.id, "tile", "material")[:4] == b"\x89PNG"

        # ------------------------------------------------------------ build with WolvenKit
        info, secs = _job(studio, studio.build(p.id), 3600)
        print(f"   build {secs:.0f} s: {info['packages']} baked {info['baked']}")
        assert info["ok"] and info["qa"]["ok"], info["qa"]
        assert info["baked"]["anchor"] == {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0}
        assert info["coverage"]["walkable"] > 95 and info["doors"] > 0, info["coverage"]
        detail = studio.build_info(p.id, info["n"])
        assert detail["qa"]["ok"] and detail["report"]["map"] == "cedar_crest"

        # ------------------------------------------------------------ the local game
        game = os.path.join(tmp, "game")
        os.makedirs(os.path.join(game, "archive", "pc", "content"))
        studio.settings.update({"paths": {"game_dir": game}})
        res, _ = _job(studio, studio.map_game(p.id), 300)
        files = sorted(os.listdir(os.path.join(game, "archive", "pc", "mod")))
        assert files and all(f.startswith("fivem2open77_map_cedar_crest") for f in files), files
        assert studio.map_game_status(p.id)["files"] == files
        _job(studio, studio.map_game(p.id, uninstall=True), 300)
        assert not os.listdir(os.path.join(game, "archive", "pc", "mod"))

        # ------------------------------------------------------------ install over SSH with the real baker
        srv = MapServer(tmp)
        env = {"PATH": f"{srv.fake}:/usr/local/bin:/usr/bin:/bin", "HOME": srv.home, "FAKE": srv.fake,
               "XDG_CONFIG_HOME": os.path.join(srv.home, ".config"), "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
               "BACKUP_ROOT": os.path.join(tmp, "backups")}
        with sshd_fixture.Sshd(env=env) as sshd:
            studio.settings.put_server({"id": "vm", "label": "VM", "host": "127.0.0.1", "port": sshd.port, "user": sshd.user,
                                        "auth": "key", "key_file": sshd.key, "servers_root": srv.root,
                                        "names": ["open77-test"], "systemctl": "systemctl --user"})
            res, secs = _job(studio, studio.deploy(p.id, "vm"), 1800)
            s0 = res["servers"][0]
            want = TK.version_of(DOTNET, WK)
            print(f"   install {secs:.0f} s, toolkit {res['toolkit']['bytes'] / 1048576:.1f} MiB")
            assert s0["code"] == 0 and s0["result"]["baker"] == "installed" and s0["result"]["restarted"] == 1, s0
            assert res["toolkit"]["host_had"] is None and res["toolkit"]["bytes"] > 20_000_000
            log = "\n".join(s0["lines"])
            assert "cedar_crest: started" in log and f"installing toolkit {want}" in log, log
            assert "WolvenKit: " in log and "f2o77-mapbaker-open77-test.service: active" in log, log
            baker_home = os.path.join(srv.home, ".f2o77", "baker")
            assert open(os.path.join(baker_home, "VERSION")).read().strip() == want

            # the baker answers a move the way the manager asks for one
            bake_dir = os.path.join(srv.manager(), "data", "bake")
            _wait(lambda: _read(os.path.join(bake_dir, "baker.json")), 60, "baker.json")
            req = {"id": "cedar_crest-1790000000-1", "action": "bake", "map": "cedar_crest", "resource": "cedar_crest",
                   "anchor": {"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0}, "requestedBy": "the e2e test",
                   "requestedAt": 1790000000, "mode": "now"}
            os.makedirs(os.path.join(bake_dir, "requests"), exist_ok=True)
            with open(os.path.join(bake_dir, "requests", "cedar_crest.json"), "w") as f:
                json.dump(req, f)
            t0 = time.time()
            stat = _wait(lambda: (_read(os.path.join(bake_dir, "status", "cedar_crest.json")) or {}).get("state")
                         in ("done", "failed") and _read(os.path.join(bake_dir, "status", "cedar_crest.json")), 600,
                         "the baker's answer")
            svc_log = open(os.path.join(srv.fake, "svc-f2o77-mapbaker-open77-test.service.log")).read()
            assert stat["state"] == "done", (stat, svc_log[-3000:])
            print(f"   the host's baker built the move in {time.time() - t0:.0f} s")
            staged = os.path.join(srv.dir, "f2o77-maps", "staged", "cedar_crest")
            assert os.path.isfile(os.path.join(staged, "build.json")), os.listdir(os.path.dirname(staged))
            # the restart: the start hook installs the staged build
            subprocess.run(srv.env["SYSTEMCTL"].split() + ["kill", "--signal=SIGINT", "open77-test.service"], env=srv.env,
                           check=True, timeout=300)
            baked = _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))
            assert baked["id"] == req["id"] and baked["anchor"] == req["anchor"], baked
            assert _read(os.path.join(bake_dir, "status", "cedar_crest.json"))["state"] == "promoted"

            # a second install: the light toolkit, the spot it was moved to kept
            res, _ = _job(studio, studio.deploy(p.id, "vm", restart=False), 1800)
            s1 = res["servers"][0]
            assert res["toolkit"]["host_had"] == want and res["toolkit"]["bytes"] < 500_000, res["toolkit"]
            assert f"toolkit {want} already installed" in "\n".join(s1["lines"]), s1["lines"]
            assert s1["result"]["kept"] == ["cedar_crest"], s1["result"]
            assert _read(os.path.join(srv.map_dir("cedar_crest"), "baked.json"))["anchor"] == req["anchor"]
            st = _job(studio, studio.server_job("vm", "status"), 300)[0][0]
            assert st["maps"][0]["anchor"] == req["anchor"] and st["mapBaker"]["toolkit"] == want, st
    finally:
        if studio:
            studio.close()
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_studio_map_end_to_end_with_the_real_tools()
    print("ok")
