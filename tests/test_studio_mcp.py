"""The Studio's MCP server: the JSON-RPC core, Streamable HTTP with the official MCP Python SDK
client (token, sessions, tools, resources, prompts, long calls answered as running jobs), the stdio
bridge (python -m fivem2open77 mcp) finding the Studio again after a restart, and the registration
files for the agent apps.

The SDK parts need the `mcp` package (pip install mcp); they are skipped without it."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from test_world import SEC, _index_with, synthetic_sector  # noqa: E402


def _sdk():
    try:
        import httpx2  # noqa: F401
        from mcp.client.client import Client  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class _Served:
    """A Studio served on a free port in this process (its Studio object reachable)."""

    def __init__(self, ws=None):
        from fivem2open77.studio import server as srv
        self.ws = ws or tempfile.mkdtemp(prefix="f2o77-mcp-")
        self.ready = threading.Event()
        self.thread = threading.Thread(target=srv.serve, kwargs=dict(port=0, workspace=self.ws, on_ready=self._on),
                                       daemon=True)
        self.thread.start()
        assert self.ready.wait(30), "the Studio did not start"

    def _on(self, httpd, studio):
        self.httpd, self.studio = httpd, studio
        self.port = httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.ready.set()

    def stop(self):
        self.httpd.shutdown()
        self.thread.join(10)


def _rpc(hub, sid, method, params=None, mid=1):
    ans, sid = hub.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, sid)
    return ans, sid


def _world_studio(studio):
    """The world tools work on the synthetic sector (no game needed)."""
    tmp = tempfile.mkdtemp()
    idx, _ = _index_with(tmp, {SEC: synthetic_sector()})
    os.makedirs(os.path.join(studio.workspace, "world"), exist_ok=True)
    import shutil
    shutil.copyfile(idx.index_file, os.path.join(studio.workspace, "world", "blocks.json"))
    studio.world_extractor = idx.extract
    studio._world_index = None


# ----------------------------------------------------------------------------- the core
def test_json_rpc_core_tools_resources_prompts():
    st = _Served()
    try:
        hub = st.studio.mcp
        ans, sid = _rpc(hub, None, "server/discover", {})
        assert ans["error"]["code"] == -32601, "the stateless era is declined: clients fall back to initialize"
        ans, sid = _rpc(hub, None, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                  "clientInfo": {"name": "claude-code", "version": "2.1"}})
        r = ans["result"]
        assert r["protocolVersion"] == "2025-11-25" and r["serverInfo"]["name"] == "fivem2open77-studio"
        assert "f2o77://guide" in r["instructions"] and sid
        ans2, _ = _rpc(hub, None, "initialize", {"protocolVersion": "1999-01-01", "clientInfo": {}})
        assert ans2["result"]["protocolVersion"] == "2025-11-25", "an unknown version gets ours"
        tools = {t["name"]: t for t in _rpc(hub, sid, "tools/list")[0]["result"]["tools"]}
        for n in ("studio_status", "wait_job", "import_resource", "build_car", "build_map", "deploy_map",
                  "set_map_spot", "apply_map_move", "world_find", "world_delete", "world_move", "world_add",
                  "build_world_edit", "deploy_world_edit"):
            assert n in tools, n
        assert "ask_user" not in tools and "finish" not in tools
        assert tools["world_find"]["annotations"]["readOnlyHint"] is True
        assert tools["deploy_world_edit"]["annotations"]["destructiveHint"] is True
        assert tools["apply_map_move"]["annotations"]["openWorldHint"] is True
        assert "project" in tools["build_car"]["inputSchema"]["properties"], "project tools take a project"
        res = _rpc(hub, sid, "resources/list")[0]["result"]["resources"]
        assert {"f2o77://guide", "f2o77://projects", "f2o77://servers"} <= {x["uri"] for x in res}
        g = _rpc(hub, sid, "resources/read", {"uri": "f2o77://guide"})[0]["result"]["contents"][0]["text"]
        assert "## World edits" in g and "wait_job" in g and "ask_user" not in g
        assert _rpc(hub, sid, "resources/read", {"uri": "f2o77://nope"})[0]["error"]["code"] == -32602
        pr = _rpc(hub, sid, "prompts/get", {"name": "convert_car", "arguments": {"path": "C:/cars/i8.zip", "server": "vm"}})
        txt = pr[0]["result"]["messages"][0]["content"]["text"]
        assert "import_resource path=C:/cars/i8.zip" in txt and "deploy it to server vm" in txt
        assert _rpc(hub, sid, "prompts/get", {"name": "convert_car", "arguments": {}})[0]["error"]["code"] == -32602
        assert _rpc(hub, "bogus", "tools/list")[0]["error"]["code"] == -32001
        out = _rpc(hub, sid, "tools/call", {"name": "studio_status", "arguments": {}})[0]["result"]
        assert out["isError"] is False and json.loads(out["content"][0]["text"])["studio"]["workspace"] == st.ws
        bad = _rpc(hub, sid, "tools/call", {"name": "open_project", "arguments": {"project": "nope"}})[0]["result"]
        assert bad["isError"] and "no project" in bad["content"][0]["text"]
        assert _rpc(hub, sid, "tools/call", {"name": "nope", "arguments": {}})[0]["error"]["code"] == -32602
        assert hub.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)[0] is None
        v = hub.sessions_view()
        assert v[0]["label"] == "Claude Code" and v[0]["calls"][-1]["tool"] == "nope" or v[0]["calls"]
    finally:
        st.stop()


def test_http_auth_and_session_rules():
    st = _Served()
    try:
        def post(body, headers=None):
            h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            h.update(headers or {})
            r = urllib.request.Request(st.url, data=json.dumps(body).encode(), method="POST", headers=h)
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {}}}
        assert post(init)[0] == 401
        assert post(init, {"Authorization": "Bearer wrong"})[0] == 401
        tok = {"Authorization": "Bearer " + st.studio.mcp.token}
        assert post(init, dict(tok, Origin="http://evil.example"))[0] == 403
        code, h, body = post(init, tok)
        sid = h.get("Mcp-Session-Id")
        assert code == 200 and sid and json.loads(body)["result"]["protocolVersion"] == "2025-06-18"
        code, _, body = post({"jsonrpc": "2.0", "method": "notifications/initialized"}, dict(tok, **{"Mcp-Session-Id": sid}))
        assert code == 202 and body == b""
        code, _, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, dict(tok, **{"Mcp-Session-Id": "x" * 32}))
        assert code == 404, "an unknown session: the client initializes again"
        r = urllib.request.Request(st.url, method="GET", headers=tok)
        try:
            urllib.request.urlopen(r, timeout=10)
            raise AssertionError("GET")
        except urllib.error.HTTPError as e:
            assert e.code == 405
        # the token survives restarts of the Studio (the bridge and the apps keep it)
        assert open(os.path.join(st.ws, "mcp", "token")).read().strip() == st.studio.mcp.token
        # turned off in Settings: 503
        st.studio.settings.update({"mcp": {"enabled": False}})
        assert post(init, tok)[0] == 503
    finally:
        st.stop()


# ----------------------------------------------------------------------------- the official SDK over HTTP
def test_an_mcp_client_edits_the_world_and_follows_a_long_build():
    if not _sdk():
        print("   (skipped: the mcp package is not installed)")
        return
    import httpx2
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client
    st = _Served()
    _world_studio(st.studio)
    st.studio.settings.update({"mcp": {"call_seconds": 10}})

    # a build that outlives the call: answered as running, followed with wait_job
    from fivem2open77.world import package as P
    real_build = P.build

    def slow_build(*a, **kw):
        time.sleep(13)
        return real_build(*a, **kw)

    async def run():
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + st.studio.mcp.token}, timeout=120) as http:
            async with Client(streamable_http_client(st.url, http_client=http), mode="auto") as c:
                tools = await c.list_tools()
                names = {t.name for t in tools.tools}
                assert {"world_find", "world_delete", "build_world_edit", "wait_job"} <= names
                g = await c.read_resource("f2o77://guide")
                assert "World edits" in g.contents[0].text
                r = await c.call_tool("new_world_edit", {"display_name": "Clear the railing", "name": "world_clear_test"})
                assert not r.is_error, r.content[0].text
                r = await c.call_tool("world_find", {"x": 102, "y": 198, "radius": 3})
                found = json.loads(r.content[0].text)
                rail = next(o for o in found["objects"] if o["type"] == "worldStaticMeshNode")
                assert rail["res"].endswith("railing_a.mesh") and rail["index"] == 0
                r = await c.call_tool("world_delete", {"objects": [{"sector": rail["sector"], "index": rail["index"]}]})
                d = json.loads(r.content[0].text)
                assert d["actions"][0]["text"] == "delete railing_a (+ its collision: 1 piece)", d
                r = await c.call_tool("world_move", {"sector": SEC, "index": 2, "element": 0, "delta": [0, 1.5, 0]})
                assert not r.is_error and "collision" not in json.loads(r.content[0].text)["actions"][0]["text"]
                r = await c.call_tool("world_undo", {})
                assert json.loads(r.content[0].text)["undone"][0]["op"] == "move"
                P.build = slow_build
                t0 = time.time()
                r = await c.call_tool("build_world_edit", {})
                out = json.loads(r.content[0].text)
                assert out["status"] == "running" and 9 < time.time() - t0 < 13, out
                job = out["job"]
                for _ in range(5):
                    r = await c.call_tool("wait_job", {"job": job, "seconds": 5})
                    w = json.loads(r.content[0].text)
                    if w["status"] != "running":
                        break
                assert w["status"] == "done" and w["result"]["ok"] and w["result"]["summary"]["deleted"] == 1, w
                st_ = await c.call_tool("world_status", {})
                s2 = json.loads(st_.content[0].text)
                assert s2["project"]["builds"][0]["ok"] is True
                v = st.studio.mcp.sessions_view()[0]
                assert [x["tool"] for x in v["calls"]][:3] == ["new_world_edit", "world_find", "world_delete"]
                assert v["project"] and v["label"]
                return w

    try:
        asyncio.run(run())
        p = [x for x in st.studio.projects.list() if x["kind"] == "world"][0]
        b = st.studio.projects.get(p["id"]).latest_build()
        zp = os.path.join(st.studio.projects.get(p["id"]).build_dir(b["n"]), "out", "world_clear_test", "dist",
                          "world_clear_test.zip")
        import yaml
        import zipfile
        with zipfile.ZipFile(zp) as z:
            xl = yaml.safe_load(z.read("archive/pc/mod/fivem2open77_world_world_clear_test.xl"))
        dels = {d["index"]: d for d in xl["streaming"]["sectors"][0]["nodeDeletions"]}
        assert dels[0] == {"index": 0, "type": "worldStaticMeshNode"} and dels[4]["actorDeletions"] == [2]
        assert st.studio.mcp.sessions_view() == [], "the client ended its session when it closed"
    finally:
        P.build = real_build
        st.stop()


# ----------------------------------------------------------------------------- the stdio bridge
def test_the_bridge_relays_and_finds_the_studio_again_after_a_restart():
    if not _sdk():
        print("   (skipped: the mcp package is not installed)")
        return
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters
    st = _Served()
    ws = st.ws
    env = dict(os.environ, PYTHONPATH=os.path.abspath(ROOT))
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "fivem2open77", "mcp", "--workspace", ws, "--attach-only"],
                                   env=env, cwd=tempfile.gettempdir())
    holder = {"st": st}

    async def run():
        t0 = time.time()
        # mode auto: the client asks server/discover first (the 2026-07-28 era) and falls back to initialize
        async with Client(params, mode="auto", read_timeout_seconds=60) as c:
            assert time.time() - t0 < 6, "the bridge answers server/discover at once (the client waits 10 s otherwise)"
            tools = await c.list_tools()
            assert "studio_status" in {t.name for t in tools.tools}
            r = await c.call_tool("studio_status", {})
            assert json.loads(r.content[0].text)["studio"]["workspace"] == ws
            # the Studio restarts (another port, no sessions): the bridge finds it and opens a session itself
            holder["st"].stop()
            holder["st"] = _Served(ws)
            r = await c.call_tool("list_projects", {})
            assert not r.is_error, r.content[0].text
            assert len(holder["st"].studio.mcp.sessions_view()) == 1

    try:
        asyncio.run(run())
        log = open(os.path.join(ws, "logs", "mcp-bridge.log")).read()
        assert "new session with the Studio (it was restarted)" in log
    finally:
        holder["st"].stop()


def test_the_bridge_says_so_when_the_studio_is_not_running():
    ws = tempfile.mkdtemp(prefix="f2o77-mcp-none-")
    env = dict(os.environ, PYTHONPATH=os.path.abspath(ROOT))
    p = subprocess.run([sys.executable, "-m", "fivem2open77", "mcp", "--workspace", ws, "--attach-only"],
                       input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {"protocolVersion": "2025-06-18", "clientInfo": {}}}) + "\n").encode(),
                       capture_output=True, env=env, cwd=tempfile.gettempdir(), timeout=60)
    ans = json.loads(p.stdout.decode().strip().splitlines()[0])
    assert ans["id"] == 1 and "not running" in ans["error"]["message"]


# ----------------------------------------------------------------------------- the apps
def test_registering_with_the_agent_apps():
    from fivem2open77.studio import mcpclients as M
    home = tempfile.mkdtemp(prefix="f2o77-home-")
    old = {k: os.environ.get(k) for k in ("HOME", "APPDATA", "CODEX_HOME", "PATH")}
    os.environ.update(HOME=home, APPDATA=os.path.join(home, "AppData", "Roaming"))
    os.environ.pop("CODEX_HOME", None)
    try:
        ws = tempfile.mkdtemp()
        cur = M.config_path("cursor")
        os.makedirs(os.path.dirname(cur))
        with open(cur, "w") as f:
            f.write('{\n  // my servers\n  "mcpServers": {"other": {"command": "x"},},\n}\n')
        assert M.status("cursor", ws)["state"] == "not_registered"
        r = M.connect("cursor", ws)
        assert r["state"] == "registered" and r["done"]
        data = json.load(open(cur))
        assert data["mcpServers"]["other"] == {"command": "x"}, "other servers stay"
        assert data["mcpServers"]["fivem2open77"]["args"][-1] in ("mcp", ws)
        assert os.path.isfile(cur + ".f2o77-backup")
        assert M.connect("cursor", ws, remove=True)["state"] == "not_registered"
        os.makedirs(os.path.join(home, ".codex"))
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write('model = "o4"\n\n[mcp_servers.other]\ncommand = "y"\n')
        r = M.connect("codex", ws)
        t = open(os.path.join(home, ".codex", "config.toml")).read()
        assert r["state"] == "registered" and "[mcp_servers.other]" in t and "tool_timeout_sec = 1800" in t
        M.connect("codex", ws)
        assert open(os.path.join(home, ".codex", "config.toml")).read().count("[mcp_servers.fivem2open77]") == 1
        vs = M.connect("vscode", ws)
        assert vs["state"] == "registered" and json.load(open(M.config_path("vscode")))["servers"]["fivem2open77"]["type"] == "stdio"
        os.environ["PATH"] = home                                         # no claude command
        cc = M.connect("claude_code", ws)
        assert cc["done"] is False and "claude mcp add --scope user fivem2open77 --" in cc["manual"]
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_microsoft_store_apps_get_the_entry_in_their_own_copy():
    """A Store (MSIX) build of Claude Desktop reads its own copy of claude_desktop_config.json (in its
    package's LocalCache) when it has one: the entry goes there too, and the rest of the file stays."""
    from fivem2open77.studio import mcpclients as M
    home = tempfile.mkdtemp(prefix="f2o77-home-")
    old = {k: os.environ.get(k) for k in ("HOME", "APPDATA", "LOCALAPPDATA")}
    os.environ.update(HOME=home, APPDATA=os.path.join(home, "AppData", "Roaming"),
                      LOCALAPPDATA=os.path.join(home, "AppData", "Local"))
    was = M._windows
    M._windows = lambda: True
    try:
        ws = tempfile.mkdtemp()
        pkg = os.path.join(home, "AppData", "Local", "Packages", "Claude_pzs8sxrjxfjjc")
        os.makedirs(pkg)
        st = M.status("claude_desktop", ws)
        assert st["state"] == "not_registered" and st["store_app"], st      # installed from the Store, no file yet
        own = os.path.join(pkg, "LocalCache", "Roaming", "Claude", "claude_desktop_config.json")
        os.makedirs(os.path.dirname(own))
        with open(own, "w") as f:
            json.dump({"preferences": {"menuBarEnabled": False}}, f)
        st = M.status("claude_desktop", ws)
        assert st["file"] == own and st["state"] == "not_registered", st
        r = M.connect("claude_desktop", ws)
        assert r["state"] == "registered" and "Microsoft Store" in r["note"], r
        real = os.path.join(home, "AppData", "Roaming", "Claude", "claude_desktop_config.json")
        assert sorted(r["files"]) == sorted([own, real]), r["files"]
        mine = json.load(open(own))
        assert mine["preferences"] == {"menuBarEnabled": False} and mine["mcpServers"]["fivem2open77"]["args"][:4] == ["-X", "utf8", "-m", "fivem2open77"]
        assert json.load(open(real))["mcpServers"]["fivem2open77"] == mine["mcpServers"]["fivem2open77"]
        r = M.connect("claude_desktop", ws, remove=True)
        assert r["state"] == "not_registered" and "fivem2open77" not in json.load(open(own))["mcpServers"]
    finally:
        M._windows = was
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_bridge_in_a_store_app_never_starts_the_studio_or_makes_a_token():
    from fivem2open77.studio import bridge as B
    ws = tempfile.mkdtemp(prefix="f2o77-mcp-store-")
    b = B.Bridge(ws, log=os.path.join(ws, "bridge.log"))
    started = []
    b.launch = lambda: started.append(1)
    was = B.in_package
    B.in_package = lambda: True
    try:
        try:
            b.ensure(wait=2)
            raise AssertionError("no Studio runs: ensure must fail")
        except B.BridgeError as e:
            assert "open it" in str(e) and "Microsoft Store" in str(e), e
        assert not started, "nothing was started inside the app's container"
    finally:
        B.in_package = was
    b.port = 9                                   # a Studio that never made a token (older than 1.3)
    try:
        b._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        raise AssertionError("no token: the bridge must say so")
    except B.BridgeError as e:
        assert "no MCP token" in str(e), e
    assert not os.path.exists(os.path.join(ws, "mcp", "token")), "the bridge only reads the token"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            f()
            print("ok  ", n)
