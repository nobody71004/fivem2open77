"""The Studio's maps: options, the live link's HTTP over SSH channels, the agent API switch, and
the whole server side over a real sshd: a map build deployed with map_deploy.sh and the baker to a
fake Open77 host, its status, the agent API turned on, and the map moved through the SSH tunnel
against a fake map manager (fake_manager.FakeManager) until the move is applied.

The SSH part needs root and OpenSSH (tests/sshd_fixture.py) and skips otherwise."""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fivem2open77.studio import maps as M  # noqa: E402
from fivem2open77.studio import maplink as L  # noqa: E402
from fivem2open77.studio.mapops import _clean_args  # noqa: E402

DEPLOY = os.path.join(HERE, "..", "tools", "deploy")


# ----------------------------------------------------------------------------- options
def test_map_options_defaults_and_rules():
    o = M.normalize_options({})
    assert o == M.DEFAULTS
    o = M.normalize_options({"name": "Cedar_Crest", "place": "480, 1800, 174, 390", "entity_sets": "a, b",
                             "exclude": "prop_a,prop_b,prop_a", "coords": [781.3, 1472.4, 346.2], "cell": "32"})
    assert o["name"] == "cedar_crest" and o["place"] == {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 30.0}
    assert o["entity_sets"] == ["a", "b"] and o["exclude"] == ["prop_a", "prop_b"] and o["cell"] == 32.0
    assert M.parse_place({"x": 1, "y": 2, "z": 3, "yaw": -190}) == {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 170.0}
    assert M.parse_place([1, 2, 3]) == {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0}
    try:
        M.normalize_options({"name": "1bad", "display_name": 'say "hi"', "coords": [1, 2], "entity_sets": [],
                             "exclude": ["a;rm"], "collision": "yes", "doors": "open", "cell": 2, "place": {"x": 1},
                             "bogus": 1})
        raise AssertionError("bad options accepted")
    except M.MapOptionsError as e:
        text = " | ".join(e.errors)
        for needle in ("unknown options: bogus", "name:", "display_name:", "coords:", "entity_sets:", "exclude:",
                       "collision:", "doors:", "cell:", "place:"):
            assert needle in text, (needle, text)
    try:
        M.normalize_options({"place": [99999, 0, 0]})
        raise AssertionError("a spot outside the city")
    except M.MapOptionsError as e:
        assert "outside Night City" in str(e)
    # the converter gets the CLI's options
    c = M.converter_opts(M.normalize_options({"name": "cedar_crest", "exclude": ["x"], "doors": "static",
                                              "place": [1, 2, 3, 45], "collision": False}), version="1.0.7")
    assert c["name"] == "cedar_crest" and c["exclude"] == "x" and c["static_doors"] and c["no_collision"]
    assert c["place"] == "1.0,2.0,3.0,45.0" and c["version"] == "1.0.7" and c["entity_sets"] == "default"
    assert "place" not in M.converter_opts(M.normalize_options({"place": [1, 2, 3]}), place=False)
    # what is built decides the look, not the names or where it is baked
    base = M.normalize_options({})
    k = M.look_key(base, "h")
    assert M.look_key(dict(base, name="other", display_name="X", place={"x": 1, "y": 2, "z": 3, "yaw": 0}), "h") == k
    assert M.look_key(dict(base, exclude=["a"]), "h") != k and M.look_key(base, "h2") != k
    assert M.merge({"a": 1, "b": 2}, {"a": None, "c": 3}) == {"b": 2, "c": 3}


def test_door_map_option():
    o = M.normalize_options({"door_map": {"objects": {"CookieDesp": "hinged", "x": "auto"},
                                          "doorways": {"miloscookiescol#3": "door-double"}}})
    assert o["door_map"] == {"objects": {"cookiedesp": "hinged"}, "doorways": {"miloscookiescol#3": "door-double"}}
    c = M.converter_opts(o)
    assert json.loads(c["door_map"]) == o["door_map"]
    assert M.normalize_options({"door_map": {"objects": {"x": "auto"}}})["door_map"] == {}
    assert "door_map" not in M.converter_opts(M.normalize_options({}))
    try:
        M.normalize_options({"door_map": {"objects": {"a": "swing"}}})
    except M.MapOptionsError as e:
        assert "swing" in "; ".join(e.errors)
    else:
        raise AssertionError("a bad door mode passed")
    # the door mapping changes what is built: a different look
    assert M.look_key(o, "s") != M.look_key(M.normalize_options({}), "s")


def test_live_op_arguments_are_cleaned():
    assert _clean_args("move", {"dx": "5", "dyaw": 15, "frame": "map", "evil": "x"}) == {"dx": 5.0, "dyaw": 15.0, "frame": "map"}
    assert _clean_args("apply", {"mode": "empty", "countdown": "60"}) == {"mode": "empty", "countdown": 60}
    assert _clean_args("presetSave", {"name": "Hill", "anchor": [1, 2, 3, 4]})["anchor"] == {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 4.0}
    for op, args in (("nope", {}), ("move", {"dx": "x"}), ("move", {"dx": float("nan")}), ("place", {"x": 1e9})):
        try:
            _clean_args(op, args)
            raise AssertionError((op, args))
        except ValueError:
            pass


# ----------------------------------------------------------------------------- HTTP over a channel
class _Chan:
    def __init__(self, data, step=7):
        self.data, self.step, self.sent = data, step, b""

    def recv(self, n):
        out, self.data = self.data[:min(n, self.step)], self.data[min(n, self.step):]
        return out


def test_http_answers_are_read_whole():
    body = json.dumps({"ok": True, "maps": [1, 2, 3]}).encode()
    s, h, b = L._read_response(_Chan(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)), 5)
    assert s == 200 and json.loads(b)["maps"] == [1, 2, 3]
    chunked = b"HTTP/1.1 409 Conflict\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{\"ok\"\r\n9;x=y\r\n: false}\r\n0\r\n\r\n"
    s, h, b = L._read_response(_Chan(chunked), 5)
    assert s == 409 and json.loads(b) == {"ok": False}
    s, h, b = L._read_response(_Chan(b"HTTP/1.0 200 OK\r\n\r\n{\"a\": 1}"), 5)
    assert json.loads(b) == {"a": 1}
    for bad, err in ((b"", L.Unreachable), (b"garbage\r\n\r\n", L.LinkError)):
        try:
            L._read_response(_Chan(bad), 5)
            raise AssertionError(bad)
        except err:
            pass
    assert L._parse_listen("http://0.0.0.0:12000") == ("127.0.0.1", 12000)
    assert L._parse_listen("http://127.0.0.1:11781") == ("127.0.0.1", 11781)
    try:
        L._parse_listen("https://x")
        raise AssertionError("https")
    except L.LinkError:
        pass


# ----------------------------------------------------------------------------- server scripts
def test_agent_api_switch_on_a_fake_server():
    from test_server_deploy import Fake
    tmp = tempfile.mkdtemp(prefix="f2o77-api-")
    try:
        srv = Fake(tmp)
        cfg = os.path.join(srv.dir, "server.jsonc")
        before = open(cfg).read()
        out = srv.run("agent-api", "-", "open77-test")
        text = open(cfg).read()
        assert "AGENT API on" in out and "PENDING restart" in out, out
        assert '"httpHandlers": { "enabled": true, "listenUrl": "http://127.0.0.1:11781" }' in text, text
        assert "// test server" in text and "// the game mode" in text, "comments kept"
        backups = [d for d in os.listdir(os.path.join(tmp, "backups"))]
        assert backups and open(os.path.join(tmp, "backups", backups[0], "server.jsonc")).read() == before
        out = srv.run("agent-api", "-", "open77-test")
        assert "already on" in out, out
        st = json.loads(srv.run("status", "-", "open77-test", "--json"))
        assert st["http"] == {"enabled": True, "listenUrl": "http://127.0.0.1:11781"}, st
        out = srv.run("agent-api", "-", "open77-test", "--off", "--restart")
        assert "AGENT API off" in out and "done" in out, out
        assert json.loads(srv.run("status", "-", "open77-test", "--json"))["http"]["enabled"] is False
        # a config the server rejects is put back
        with open(os.path.join(srv.fake, "Open77.Server"), "w") as f:
            f.write("#!/bin/sh\necho 'httpHandlers.listenUrl: invalid'\nexit 1\n")
        r = srv.run("agent-api", "-", "open77-test", check=False)
        assert "check-config FAILED" in r and '"enabled": false' in open(cfg).read(), r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_baker_install_refuses_a_light_toolkit_on_a_new_host():
    from fivem2open77.mlo import toolkit as TK
    from test_mlo_deploy import MapServer
    tmp = tempfile.mkdtemp(prefix="f2o77-light-")
    try:
        srv = MapServer(tmp)
        light = TK.build_lib(os.path.join(tmp, "light.tgz"), "net8.0.99-wkabc", log=lambda *a: None)
        r = subprocess.run(["bash", os.path.join(DEPLOY, "remote", "baker_install.sh"), light["path"], srv.dir],
                           env=srv.env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 6 and "send the full toolkit" in r.stdout + r.stderr, r.stdout + r.stderr
        assert not os.path.exists(os.path.join(srv.home, ".f2o77", "baker"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- over SSH
def _wait(cond, seconds=60.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _job(studio, job, seconds=600):
    studio.jobs.wait(job, seconds)
    assert job.state == "done", (job.title, job.state, job.error, [x["text"] for x in job.lines][-70:])
    return job.result


def _fake_map_build(studio, tmp, name="cedar_crest"):
    """A map project with one finished build (the converter's output, baked by a fake WolvenKit)."""
    from fivem2open77.mlo import bake as B
    from test_mlo import _FakeWk, _converted_map
    src = os.path.join(tmp, "fivem_map")
    os.makedirs(os.path.join(src, "stream"))
    open(os.path.join(src, "stream", "x.ymap"), "wb").close()
    p = studio.projects.create(src, kind="map")
    n = p.next_build()
    out = os.path.join(p.build_dir(n), "out")
    os.makedirs(out)
    res = _converted_map(out)
    os.remove(os.path.join(out, "f2o77_maps", "data", "agent_token.txt"))     # a build carries no token
    B.bake(res, {"x": 480, "y": 1800, "z": 174}, _FakeWk(), log=lambda *a: None, bake_id=f"{name}-convert-1")
    p.add_build({"n": n, "ok": True, "kind": "map", "name": name, "version": f"1.0.{n}", "time": time.time()})
    with p.lock:
        p.data["map"] = {"name": name, "display_name": "Cedar Crest"}
        p.save()
    return p


def test_studio_deploys_a_map_and_moves_it_over_ssh():
    import sshd_fixture
    if not sshd_fixture.available():
        print("   (skipped: needs root and OpenSSH)")
        return
    from fivem2open77.mlo import toolkit as TK
    from fivem2open77.studio.app import Studio
    from fake_manager import TOKEN, FakeManager, _serve
    from test_mlo_deploy import MapServer, _fake_toolkit
    tmp = tempfile.mkdtemp(prefix="f2o77-studio-ssh-")
    srv = fake_http = studio = None
    try:
        srv = MapServer(tmp)
        fake = FakeManager()
        fake_http, url = _serve(fake)
        port = fake_http.server_address[1]
        cfg = os.path.join(srv.dir, "server.jsonc")
        text = open(cfg).read().replace("{\n  // test server", '{\n  // test server\n  "httpHandlers": { "enabled": false, '
                                        f'"listenUrl": "http://127.0.0.1:{port}" }},', 1)
        open(cfg, "w").write(text)
        env = {"PATH": f"{srv.fake}:/usr/local/bin:/usr/bin:/bin", "HOME": srv.home, "FAKE": srv.fake,
               "XDG_CONFIG_HOME": os.path.join(srv.home, ".config"), "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
               "BACKUP_ROOT": os.path.join(tmp, "backups")}
        with sshd_fixture.Sshd(env=env) as sshd:
            ws = os.path.join(tmp, "ws")
            studio = Studio(ws)
            studio.settings.put_server({"id": "local", "label": "Local test", "host": "127.0.0.1", "port": sshd.port,
                                        "user": sshd.user, "auth": "key", "key_file": sshd.key,
                                        "servers_root": srv.root, "names": ["open77-test"], "systemctl": "systemctl --user"})
            p = _fake_map_build(studio, tmp)
            full = _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz"))
            asked = []

            def toolkit(have, log=print):
                asked.append(have)
                if have == "nettest-wkfake":
                    return TK.build_lib(os.path.join(tmp, "light.tgz"), have, log=log)["path"]
                return full
            studio.baker_toolkit = toolkit

            # ------------------------------------------------------------ the first deploy installs everything
            res = _job(studio, studio.deploy(p.id, "local"))
            s0 = res["servers"][0]
            assert s0["code"] == 0 and s0["result"] == {"rc": 0, "maps": ["cedar_crest"], "baker": "installed",
                                                       "restarted": 1, "kept": []}, s0
            assert asked == [None] and res["toolkit"] == {"host_had": None, "uploaded": True,
                                                          "bytes": os.path.getsize(full)}, res["toolkit"]
            log = "\n".join(s0["lines"])
            assert "cedar_crest: started" in log and "f2o77_maps: started" in log, log
            dep = studio.projects.get(p.id).data["deployments"][-1]
            assert dep["kind"] == "map" and dep["results"][0]["baker"] == "installed" and dep["build"] == 1

            st = _job(studio, studio.server_job("local", "status"))[0]
            assert [m["name"] for m in st["maps"]] == ["cedar_crest"] and st["maps"][0]["anchor"]["x"] == 480.0, st
            assert st["mapManager"]["maps"] == ["cedar_crest"] and st["mapManager"]["token"] is False
            assert st["mapBaker"]["service"] == "active" and st["mapBaker"]["toolkit"] == "nettest-wkfake"
            assert st["mapBaker"]["hook"] is True and st["http"]["enabled"] is False

            # ------------------------------------------------------------ live: off until the agent API is on
            live = studio.live_status("local")
            assert not live["ok"] and "agent API is off" in live["error"] and live["link"]["manager"], live
            _job(studio, studio.server_job("local", "agent-api", names=["open77-test"], enable=True))
            assert f'"enabled": true, "listenUrl": "http://127.0.0.1:{port}"' in open(cfg).read()
            live = studio.live_status("local")
            assert live["ok"] and live["link"]["token"] is False, live          # reads need no token
            try:
                studio.live_op("local", None, "cedar_crest", "discard")
                raise AssertionError("a write without the token")
            except L.LinkError as e:
                assert "no agent token yet" in str(e), e
            # the manager writes its token at its first start
            os.makedirs(os.path.join(srv.manager(), "data"), exist_ok=True)
            with open(os.path.join(srv.manager(), "data", "agent_token.txt"), "w") as f:
                f.write(TOKEN + "\n")
            live = studio.live_status("local")
            assert live["ok"] and live["status"]["maps"][0]["name"] == "cedar_crest", live
            assert live["link"]["token"] is True and TOKEN not in json.dumps(live), "the token never leaves the link"
            r = studio.live_op("local", None, "cedar_crest", "place", {"x": 600, "y": 1900, "z": 200, "yaw": 45, "junk": 1})
            assert r["ok"] and fake.calls[-1][2] == {"x": 600.0, "y": 1900.0, "z": 200.0, "yaw": 45.0}, fake.calls[-1]
            r = studio.live_op("local", "open77-test", "cedar_crest", "move", {"dx": 5})
            assert r["map"]["pending"]["x"] == 605.0
            assert studio.live_op("local", None, "cedar_crest", "revert")["ok"] is False      # a 409 is an answer
            assert studio.live_players("local")["players"][0]["name"] == "V"
            assert studio.live_map("local", None, "cedar_crest")["pending"]["x"] == 605.0
            r = studio.live_op("local", None, "cedar_crest", "apply", {"mode": "now", "countdown": 30})
            assert r["ok"] and r["result"]["state"] == "requested"
            j = studio.live_wait("local", None, "cedar_crest", until="applied", timeout=120)
            m = _job(studio, j, 150)
            assert m["running"]["x"] == 605.0 and m["apply"]["state"] == "applied", m
            lines = [x["text"] for x in j.lines]
            assert any("server not answering" in x for x in lines), lines   # the restart in the middle
            try:
                studio.live_op("local", "../etc", "cedar_crest", "discard")
                raise AssertionError("a folder that is not the server's")
            except ValueError:
                pass
            # the manager gone (server stopped): a clear answer, not a hang
            fake_http.shutdown()
            fake_http.server_close()
            live = studio.live_status("local")
            assert not live["ok"] and live.get("reachable") is False and "not answering" in live["error"], live
            fake_http = None

            # ------------------------------------------------------------ a redeploy: light toolkit, spot kept
            res = _job(studio, studio.deploy(p.id, "local", restart=False))
            s1 = res["servers"][0]
            assert asked == [None, "nettest-wkfake"] and res["toolkit"]["host_had"] == "nettest-wkfake", asked
            assert res["toolkit"]["bytes"] < 200_000, "only the baker's package goes up again"
            assert "toolkit nettest-wkfake already installed" in "\n".join(s1["lines"]), s1["lines"]
            assert s1["result"]["restarted"] == 0 and s1["result"]["kept"] == ["cedar_crest"], s1["result"]
            assert open(os.path.join(srv.manager(), "data", "agent_token.txt")).read().strip() == TOKEN, "data/ kept"
    finally:
        if fake_http:
            fake_http.shutdown()
        if studio:
            studio.close()
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)


# ----------------------------------------------------------------------------- the agent with maps
def _agent_wait(cond, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_agent_tools_follow_the_session_kind():
    from fivem2open77.studio.agent.manager import AgentManager, build_system
    from fivem2open77.studio.agent.providers import ScriptedProvider
    from fivem2open77.studio.app import Studio
    tmp = tempfile.mkdtemp(prefix="f2o77-agent-kind-")
    studio = Studio(os.path.join(tmp, "ws"))
    try:
        p = _fake_map_build(studio, tmp)
        prov = ScriptedProvider([
            [{"type": "tool_call", "name": "list_projects", "input": {}}],
            [{"type": "tool_call", "name": "build_car", "input": {}}],                    # not in a studio session
            [{"type": "tool_call", "name": "open_project", "input": {"project": "cedar_crest"}}],
            [{"type": "tool_call", "name": "get_map", "input": {}}],
            [{"type": "tool_call", "name": "inspect_car", "input": {}}],                  # not in a map session
            [{"type": "tool_call", "name": "set_map_options", "input": {"patch": {"place": [500, 1800, 175, 15], "cell": 3}}}],
            [{"type": "tool_call", "name": "set_map_options", "input": {"patch": {"place": [500, 1800, 175, 15]}}}],
            [{"type": "tool_call", "name": "finish", "input": {"summary": "spot set"}}],
        ])
        mgr = AgentManager(studio, provider_factory=lambda name=None: prov)
        s = mgr.sessions[mgr.start(goal="set the spot of cedar crest", autonomy="full")["id"]]
        assert _agent_wait(lambda: s.status == "finished"), [e for e in s.events if e["kind"] in ("tool_result", "error")]
        res = {e["call_id"]: e for e in s.events if e["kind"] == "tool_result"}
        calls = [e for e in s.events if e["kind"] == "tool_call"]
        by = lambda i: res[calls[i]["call_id"]]   # noqa: E731
        assert '"map": "cedar_crest"' in by(0)["text"] and not by(0)["error"]
        assert by(1)["error"] and "not available in a session with no project" in by(1)["text"]
        assert not by(2)["error"] and s.project == p.id
        assert '"options"' in by(3)["text"] and not by(3)["error"]
        assert by(4)["error"] and "not available in a session with a map project" in by(4)["text"]
        assert by(5)["error"] and "cell" in by(5)["text"]
        assert not by(6)["error"] and studio.projects.get(p.id).map_options()["place"] == {"x": 500.0, "y": 1800.0, "z": 175.0, "yaw": 15.0}
        # the model saw the tools of the kind at each step, and the matching instructions
        assert "import_resource" in prov.calls[0]["tools"] and "get_map" not in prov.calls[0]["tools"]
        assert "get_map" in prov.calls[3]["tools"] and "inspect_car" not in prov.calls[3]["tools"]
        assert prov.calls[0]["system"] == build_system(None) and prov.calls[3]["system"] == build_system("map")
        assert "map baker" in build_system("map") and "Porsche" not in build_system("map")
    finally:
        studio.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_moves_a_map_over_ssh():
    import sshd_fixture
    if not sshd_fixture.available():
        print("   (skipped: needs root and OpenSSH)")
        return
    from fivem2open77.studio.agent.manager import AgentManager
    from fivem2open77.studio.agent.providers import ScriptedProvider
    from fivem2open77.studio.app import Studio
    from fake_manager import TOKEN, FakeManager, _serve
    from test_mlo_deploy import MapServer, _fake_toolkit
    tmp = tempfile.mkdtemp(prefix="f2o77-agent-ssh-")
    srv = fake_http = studio = None
    try:
        srv = MapServer(tmp)
        fake = FakeManager()
        fake_http, _ = _serve(fake)
        port = fake_http.server_address[1]
        cfg = os.path.join(srv.dir, "server.jsonc")
        text = open(cfg).read().replace("{\n  // test server", '{\n  // test server\n  "httpHandlers": { "enabled": true, '
                                        f'"listenUrl": "http://127.0.0.1:{port}" }},', 1)
        with open(cfg, "w") as f:
            f.write(text)
        env = {"PATH": f"{srv.fake}:/usr/local/bin:/usr/bin:/bin", "HOME": srv.home, "FAKE": srv.fake,
               "XDG_CONFIG_HOME": os.path.join(srv.home, ".config"), "XDG_RUNTIME_DIR": os.path.join(tmp, "run"),
               "BACKUP_ROOT": os.path.join(tmp, "backups")}
        with sshd_fixture.Sshd(env=env) as sshd:
            studio = Studio(os.path.join(tmp, "ws"))
            studio.settings.put_server({"id": "vm", "label": "VM", "host": "127.0.0.1", "port": sshd.port, "user": sshd.user,
                                        "auth": "key", "key_file": sshd.key, "servers_root": srv.root, "names": ["open77-test"],
                                        "systemctl": "systemctl --user"})
            p = _fake_map_build(studio, tmp)
            full = _fake_toolkit(os.path.join(tmp, "baker-toolkit.tgz"))
            studio.baker_toolkit = lambda have, log=print: full
            # installed once, and the manager started (it writes its token at its first start)
            _job(studio, studio.deploy(p.id, "vm"))
            with open(os.path.join(srv.manager(), "data", "agent_token.txt"), "w") as f:
                f.write(TOKEN)
            prov = ScriptedProvider([
                [{"type": "text", "text": "Updating the install first."},
                 {"type": "tool_call", "name": "deploy_map", "input": {"server": "vm", "restart": False}}],
                [{"type": "tool_call", "name": "server_status", "input": {"server": "vm"}}],
                [{"type": "tool_call", "name": "live_maps", "input": {}}],
                [{"type": "tool_call", "name": "set_map_spot", "input": {"action": "place", "x": 610, "y": 1910, "z": 201, "yaw": 30}}],
                [{"type": "tool_call", "name": "set_map_spot", "input": {"action": "move", "dx": -10}}],
                [{"type": "tool_call", "name": "map_preset", "input": {"action": "save", "preset": "Plaza"}}],
                [{"type": "tool_call", "name": "set_map_spot", "input": {"action": "revert"}}],          # refused: no previous
                [{"type": "tool_call", "name": "apply_map_move", "input": {"mode": "now", "countdown": 30}}],
                [{"type": "tool_call", "name": "wait_map_move", "input": {"until": "applied", "timeout": 120}}],
                [{"type": "tool_call", "name": "live_map", "input": {}}],
                [{"type": "tool_call", "name": "finish", "input": {"summary": "moved to 600, 1910"}}],
            ])
            mgr = AgentManager(studio, provider_factory=lambda name=None: prov)
            studio.agents = mgr
            s = mgr.sessions[mgr.start(project=p.id, goal="install and move it", autonomy="auto_safe")["id"]]
            # auto_safe: changes run, anything touching the server waits for the user
            assert _agent_wait(lambda: s.status == "waiting_approval"), s.status
            assert s.pending["tool"] == "deploy_map"
            mgr.approve(s.id, s.pending["call_id"], True)
            assert _agent_wait(lambda: s.status == "waiting_approval" and s.pending["tool"] == "apply_map_move", 300), \
                [(e["kind"], e.get("tool"), e.get("text", "")[:200]) for e in s.events[-8:]]
            assert fake.maps["cedar_crest"]["pending"] == {"x": 600.0, "y": 1910.0, "z": 201.0, "yaw": 30.0}
            assert [x["name"] for x in fake.maps["cedar_crest"]["presets"]] == ["Plaza"]
            mgr.approve(s.id, s.pending["call_id"], True)
            assert _agent_wait(lambda: s.status == "finished", 300), [(e["kind"], e.get("tool"), e.get("text", "")[:300]) for e in s.events[-10:]]
            res = {e["call_id"]: e for e in s.events if e["kind"] == "tool_result"}
            calls = [e for e in s.events if e["kind"] == "tool_call"]
            got = [(c["tool"], res[c["call_id"]]["error"]) for c in calls]
            assert got == [("deploy_map", False), ("server_status", False), ("live_maps", False), ("set_map_spot", False),
                           ("set_map_spot", False), ("map_preset", False), ("set_map_spot", True), ("apply_map_move", False),
                           ("wait_map_move", False), ("live_map", False), ("finish", False)], got
            assert "no_previous_spot" in res[calls[6]["call_id"]]["text"]
            st = res[calls[1]["call_id"]]["text"]
            assert '"map_baker"' in st and '"agent_api"' in st and "cedar_crest" in st and '"started": true' in st, st
            live = json.loads(res[calls[9]["call_id"]]["text"])
            assert live["running"] == {"x": 600.0, "y": 1910.0, "z": 201.0, "yaw": 30.0} and live["apply"]["state"] == "applied", live
            assert s.summary == "moved to 600, 1910"
            assert TOKEN not in json.dumps(s.events) and TOKEN not in json.dumps(prov.calls), "the token never reaches the model"
    finally:
        if fake_http:
            fake_http.shutdown()
        if studio:
            studio.close()
        if srv:
            srv.stop_services()
        shutil.rmtree(tmp, ignore_errors=True)
