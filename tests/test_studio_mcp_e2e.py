"""The whole pipeline driven by an MCP client (the official MCP Python SDK, over Streamable HTTP with
the Studio's token), with the real tools:

  a FiveM car imported, inspected, renamed, built with WolvenKit and installed on a server over SSH;
  a FiveM map (Cedar Crest) imported, given a spot, built with WolvenKit and installed with the map
  manager; the map moved live through the manager's agent API (a fake manager behind the server's
  HTTP listener, reached through the SSH tunnel) and the move applied; a world edit of Night City
  made on sectors read from the game (a railing deleted with its collision, one moved, an object
  added), built with WolvenKit and installed; the server's status reporting all three.

Every long call answers "running" after mcp.call_seconds and is followed with wait_job, as a real
client would.  Opt-in (about 10 minutes): the mcp package, a WolvenKit CLI, .NET 8, the template
cache with the car donor (F2O77_TEST_CACHE), the Cedar Crest test copy, a FiveM car
(F2O77_TEST_CAR), sectors read from the game (F2O77_TEST_WORLD) and root + OpenSSH."""
import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

MAP = os.environ.get("F2O77_TEST_MAP", "/home/claude/work/src_cedar")
CAR = os.environ.get("F2O77_TEST_CAR", "/mnt/user-data/uploads/Downloads/c08960-Mercedes-Benz Biome Concept.zip")
WK = os.environ.get("F2O77_TEST_WOLVENKIT") or os.environ.get("WOLVENKIT_CLI") or "/home/claude/tools/wk8174/WolvenKit.CLI.dll"
CACHE = os.environ.get("F2O77_TEST_CACHE", "/home/claude/carcache/donor")
DOTNET = os.environ.get("DOTNET_ROOT") or "/opt/dotnet"
WORLD = os.environ.get("F2O77_TEST_WORLD", "/home/claude/vanilla/world")
SECTOR_DIR = "base\\worlds\\03_night_city\\_compiled\\default\\"


def _ready():
    try:
        import httpx2  # noqa: F401
        from mcp.client.client import Client  # noqa: F401
    except Exception:  # noqa: BLE001
        return "the mcp package"
    import sshd_fixture
    for what, ok in (("the test map", os.path.exists(MAP)), ("a FiveM car", os.path.exists(CAR)), ("WolvenKit", os.path.isfile(WK)),
                     (".NET", os.path.isfile(os.path.join(DOTNET, "dotnet"))), ("the template cache", os.path.isdir(CACHE)),
                     ("sectors from the game", bool(glob.glob(os.path.join(WORLD, "json*", "exterior_*.streamingsector.json")))),
                     ("root + OpenSSH", sshd_fixture.available())):
        if not ok:
            return what
    return None


def _real_world(studio, tmp):
    """The world index over the sectors read from the game (a block of their cells), and an extractor
    that hands their JSON out like the game's archives would."""
    from fivem2open77.world import sector as WS
    real = {}
    for fp in sorted(glob.glob(os.path.join(WORLD, "json*", "exterior_*.streamingsector.json"))):
        real[SECTOR_DIR + os.path.basename(fp)[:-5]] = fp
    rows = []
    for path in real:
        x0, y0, x1, y1, _ = WS.cell_box(path)
        rows.append({"category": "Exterior", "level": WS.grid_of(path)[4], "data": {"DepotPath": {"$value": path}},
                     "streamingBox": {"Min": {"X": x0 - 100, "Y": y0 - 100, "Z": -50}, "Max": {"X": x1 + 100, "Y": y1 + 100, "Z": 100}}})
    bj = os.path.join(tmp, "all.streamingblock.json")
    with open(bj, "w") as f:
        json.dump({"Data": {"RootChunk": {"descriptors": rows}}}, f)
    calls = []

    def extract(paths):
        calls.append(list(paths))
        out = {p: real[p] for p in paths if p in real}
        if "base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock" in paths:
            out["base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock"] = bj
        return out
    studio.world_extractor = extract
    studio._world_index = None
    return calls


def test_an_mcp_client_runs_the_whole_pipeline():
    missing = _ready()
    if missing:
        print(f"   (skipped: needs {missing})")
        return
    import httpx2
    import sshd_fixture
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from fivem2open77.studio import server as srvmod
    from fake_manager import TOKEN, FakeManager, _serve
    from test_mlo_deploy import MapServer, _fake_toolkit

    tmp = tempfile.mkdtemp(prefix="f2o77-mcp-e2e-")
    os.environ["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    srv = fake_http = httpd = None
    log = []
    try:
        srv = MapServer(tmp)
        fake = FakeManager()
        fake_http, _ = _serve(fake)
        cfg = os.path.join(srv.dir, "server.jsonc")
        text = open(cfg).read().replace("{\n  // test server", '{\n  // test server\n  "httpHandlers": { "enabled": true, '
                                        f'"listenUrl": "http://127.0.0.1:{fake_http.server_address[1]}" }},', 1)
        with open(cfg, "w") as f:
            f.write(text)
        env = {"PATH": f"{srv.fake}:/usr/local/bin:/usr/bin:/bin", "HOME": srv.home, "FAKE": srv.fake,
               "XDG_CONFIG_HOME": os.path.join(srv.home, ".config"), "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
               "BACKUP_ROOT": os.path.join(tmp, "backups")}
        with sshd_fixture.Sshd(env=env) as sshd:
            ready = threading.Event()
            box = {}

            def on_ready(h, st):
                box.update(httpd=h, studio=st)
                ready.set()
            threading.Thread(target=srvmod.serve, kwargs=dict(port=0, workspace=os.path.join(tmp, "ws"), on_ready=on_ready),
                             daemon=True).start()
            assert ready.wait(60)
            httpd, studio = box["httpd"], box["studio"]
            url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
            studio.settings.update({"paths": {"wolvenkit": WK, "dotnet": os.path.join(DOTNET, "dotnet"), "cache_dir": CACHE,
                                              "linux_dotnet": DOTNET},
                                    "mcp": {"call_seconds": 20}})
            studio.settings.put_server({"id": "local", "label": "Test VM", "host": "127.0.0.1", "port": sshd.port,
                                        "user": sshd.user, "auth": "key", "key_file": sshd.key, "servers_root": srv.root,
                                        "names": ["open77-test"], "systemctl": "systemctl --user"})
            full = _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz"))       # the real baker has its own e2e test
            studio.baker_toolkit = lambda have, log=print: full
            world_calls = _real_world(studio, tmp)

            async def run():
                async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + studio.mcp.token}, timeout=600) as http:
                    async with Client(streamable_http_client(url, http_client=http), mode="auto", read_timeout_seconds=600) as c:

                        async def call(name, args=None, ok=True):
                            t0 = time.time()
                            r = await c.call_tool(name, args or {})
                            txt = r.content[0].text if r.content and getattr(r.content[0], "text", None) is not None else ""
                            out = None
                            try:
                                out = json.loads(txt)
                            except ValueError:
                                out = txt
                            waits = 0
                            while isinstance(out, dict) and out.get("status") == "running" and "job" in out and name != "wait_job":
                                waits += 1
                                w = await c.call_tool("wait_job", {"job": out["job"], "seconds": 50})
                                wt = w.content[0].text
                                wo = json.loads(wt)
                                if wo["status"] == "running":
                                    continue
                                if wo["status"] != "done":
                                    raise AssertionError(f"{name}: job {wo['status']}: {wo.get('error')}\n" + "\n".join(wo["last_lines"]))
                                out, r = wo["result"], w
                            log.append((name, round(time.time() - t0, 1), waits, r.is_error))
                            if ok:
                                assert not r.is_error, f"{name}: {txt[:1500]}"
                            return out

                        tools = {t.name for t in (await c.list_tools()).tools}
                        assert {"import_resource", "build_car", "deploy_car", "build_map", "deploy_map", "set_map_spot",
                                "apply_map_move", "wait_map_move", "world_find", "world_delete", "build_world_edit",
                                "deploy_world_edit", "wait_job"} <= tools
                        st = await call("studio_status")
                        assert st["tools"]["wolvenkit"]["ok"], st

                        # ---------------------------------------------------------------- a car
                        imp = await call("import_resource", {"path": CAR, "kind": "car"})
                        car_pid = imp["project"] if "project" in imp else None
                        ins = await call("inspect_car")
                        assert ins["model"] and ins["parts"], ins
                        await call("update_profile", {"patch": {"display_name": "Mercedes-Benz Biome Concept"}})
                        b = await call("build_car")
                        assert b["ok"] and b["qa"]["ok"], b
                        dep = await call("deploy_car", {"server": "local", "mode": "files"})
                        assert dep["names"] == ["open77-test"], dep

                        # ---------------------------------------------------------------- a map
                        imp = await call("import_resource", {"path": MAP, "kind": "map"})
                        map_pid = imp.get("project") if isinstance(imp, dict) else None
                        gm = await call("get_map")
                        assert gm["look"] != "not looked at yet: look_at_map" and gm["look"]["parts_total"] > 10, str(gm)[:500]
                        await call("set_map_options", {"patch": {"name": "cedar_crest", "display_name": "Cedar Crest Estate",
                                                                 "place": {"x": 480, "y": 1800, "z": 174, "yaw": 0}}})
                        mb = await call("build_map", {"project": map_pid} if map_pid else {})
                        assert mb["ok"] and mb["qa"]["ok"], mb
                        rep = await call("map_build_report")
                        assert rep["build"]["ok"], rep
                        dm = await call("deploy_map", {"server": "local"})
                        assert dm["servers"][0]["exit"] == 0, dm
                        # the manager writes its agent token at its first start on the server
                        with open(os.path.join(srv.manager(), "data", "agent_token.txt"), "w") as f:
                            f.write(TOKEN)
                        lm = await call("live_maps", {"server": "local"})
                        assert any(m["name"] == "cedar_crest" for m in lm["maps"]), lm
                        await call("set_map_spot", {"server": "local", "map": "cedar_crest", "action": "place",
                                                    "x": 610, "y": 1910, "z": 201, "yaw": 30})
                        await call("apply_map_move", {"server": "local", "map": "cedar_crest", "mode": "now", "countdown": 30})
                        mv = await call("wait_map_move", {"server": "local", "map": "cedar_crest", "until": "applied", "timeout": 120})
                        assert mv["running"] == {"x": 610.0, "y": 1910.0, "z": 201.0, "yaw": 30.0}, mv

                        # ---------------------------------------------------------------- a world edit
                        await call("new_world_edit", {"display_name": "Pier clean-up", "name": "world_pier_cleanup"})
                        found = await call("world_find", {"x": -2842, "y": 2458, "radius": 6, "text": "railing"})
                        assert found["objects"], found
                        inst = next(o for o in found["objects"] if o["type"] == "worldInstancedMeshNode")
                        el = inst["elements"]["items"][0]["element"]
                        d = await call("world_delete", {"objects": [{"sector": inst["sector"], "index": inst["index"], "element": el}]})
                        assert "its collision" in d["actions"][0]["text"], d
                        statics = await call("world_find", {"x": -2860, "y": 2470, "radius": 30, "kinds": ["mesh"]})
                        mesh = statics["objects"][0]
                        mvd = await call("world_move", {"sector": mesh["sector"], "index": mesh["index"], "delta": [0, 0, -0.5], "dyaw": 10})
                        assert mvd["actions"][0]["op"] == "move", mvd
                        ad = await call("world_add", {"resource": "base\\environment\\decoration\\furniture\\living_room\\poor_sofa\\poor_sofa_a.mesh",
                                                      "pos": [-2850.0, 2465.0, 8.6], "yaw": 45})
                        assert ad["summary"]["added"] == 1, ad
                        wb = await call("build_world_edit")
                        assert wb["ok"] and wb["summary"]["added"] == 1, wb
                        wd = await call("deploy_world_edit", {"server": "local", "mode": "files"})
                        assert wd["names"] == ["open77-test"], wd
                        ss = await call("server_status", {"server": "local"})
                        s0 = ss[0]
                        assert any(x["name"] == "cedar_crest" for x in s0["maps"]), s0
                        assert any(x["name"] == "world_pier_cleanup" and x["in_load"] for x in s0["world_edits"]), s0
                        assert s0["cars"], s0
                        # what landed on the server, read before the edit is taken off again
                        with zipfile.ZipFile(os.path.join(wdir, "dist", "world_pier_cleanup.zip")) as z:
                            landed["names"] = sorted(z.namelist())
                            landed["xl"] = z.read("archive/pc/mod/fivem2open77_world_world_pier_cleanup.xl").decode()
                        rm = await call("remove_world_edit", {"server": "local"})
                        assert rm["resource"] == "world_pier_cleanup" and rm["removed"] and rm["names"] == ["open77-test"], rm
                        ss = await call("server_status", {"server": "local"})
                        assert not any(x["name"] == "world_pier_cleanup" for x in ss[0]["world_edits"]), ss
                        assert any(x["name"] == "cedar_crest" for x in ss[0]["maps"]), ss
                        g = await c.read_resource("f2o77://guide")
                        assert "World edits" in g.contents[0].text
                        return car_pid

            wdir = os.path.join(srv.dir, "resources", "assets", "world", "world_pier_cleanup")
            landed = {}
            asyncio.run(run())
            # what landed on the server, and that it is gone again
            names, xl = landed["names"], landed["xl"]
            assert not os.path.exists(wdir), "remove_world_edit took the resource off"
            assert "world_pier_cleanup" not in open(os.path.join(srv.dir, "server.jsonc")).read()
            assert names == ["archive/pc/mod/fivem2open77_world_world_pier_cleanup.archive",
                             "archive/pc/mod/fivem2open77_world_world_pier_cleanup.xl"], names
            import yaml
            doc = yaml.safe_load(xl)
            assert doc["streaming"]["blocks"] == ["fivem2open77\\world\\world_pier_cleanup\\sector\\world_pier_cleanup.streamingblock"]
            secs = doc["streaming"]["sectors"]
            assert len(secs) >= 2, "the railing's collision lives in a coarser sector"
            assert any("actorDeletions" in d for s in secs for d in s.get("nodeDeletions", [])), xl
            assert any("instanceDeletions" in d for s in secs for d in s.get("nodeDeletions", [])), xl
            assert any(m.get("orientation") for s in secs for m in s.get("nodeMutations", [])), xl
            assert world_calls, "sectors were read through the extractor"
            cars = [d for d in os.listdir(os.path.join(srv.dir, "resources", "assets", "vehicles"))]
            assert cars, "the car is installed"
            print("   calls:", [(n, s, w) for n, s, w, _e in log])
    finally:
        if httpd:
            httpd.shutdown()
        if fake_http:
            fake_http.shutdown()
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_an_mcp_client_runs_the_whole_pipeline()
    print("ok")
