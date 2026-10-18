"""The Studio's terminal (studio/terminal.py), the MCP tools that let agents drive the whole app without
the screen (terminal, studio_api, agent_*, studio_ui), and the bridge telling its client when the
Studio's tool list changed."""
import io
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

import pytest

from fivem2open77.studio import terminal as tm
from fivem2open77.studio.terminal import TermError, parse_args, split_chain, tokenize


# ----------------------------------------------------------------------------- reading a line
def test_tokenize_quotes_paths_and_json():
    w = tokenize('import_resource "C:\\My Cars\\urus.zip" kind=car')
    assert w == ["import_resource", "C:\\My Cars\\urus.zip", "kind=car"]
    assert w[1].quoted and not w[2].quoted
    w = tokenize('update_profile patch={"sound": {"engine": "v_car x"}} --flag')
    assert w == ["update_profile", 'patch={"sound": {"engine": "v_car x"}}', "--flag"]
    w = tokenize('api POST /api/x {"a": "b c", "d": [1, 2]}')
    assert w[-1] == '{"a": "b c", "d": [1, 2]}'
    assert tokenize("  ") == []
    assert tokenize("name='it is'") == ["name=it is"]


def test_split_chain():
    assert split_chain("a && b ; c") == [("a", "&&"), ("b", ";"), ("c", None)]
    assert split_chain('echo "x && y"') == [('echo "x && y"', None)]
    assert split_chain('api POST /x {"a": "b; c"}') == [('api POST /x {"a": "b; c"}', None)]
    # a system command takes the rest of the line: its shell reads && and ;
    assert split_chain("sh dir && echo ok") == [("sh dir && echo ok", None)]
    assert split_chain("status && !dir ; echo x") == [("status", "&&"), ("!dir ; echo x", None)]


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["server"],
          "properties": {"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}},
                         "mode": {"type": "string", "enum": ["stage", "restart", "files"]}, "build": {"type": "integer"},
                         "include_hub": {"type": "boolean"}, "patch": {"type": "object"}}}


def test_parse_args_forms():
    assert parse_args(SCHEMA, tokenize("prod"), "deploy_car") == {"server": "prod"}
    assert parse_args(SCHEMA, tokenize("prod mode=restart build=3 --include-hub"), "d") == \
        {"server": "prod", "mode": "restart", "build": 3, "include_hub": True}
    assert parse_args(SCHEMA, tokenize("--server prod --names a,b"), "d") == {"server": "prod", "names": ["a", "b"]}
    assert parse_args(SCHEMA, tokenize('{"server": "x", "build": 2}'), "d") == {"server": "x", "build": 2}
    # a server named like a number stays text: the schema says string
    assert parse_args(SCHEMA, tokenize("server=007"), "d") == {"server": "007"}
    assert parse_args(SCHEMA, tokenize("prod patch.sound.engine=x patch.handling.mass=1500"), "d") == \
        {"server": "prod", "patch": {"sound": {"engine": "x"}, "handling": {"mass": 1500}}}
    with pytest.raises(TermError, match="needs server"):
        parse_args(SCHEMA, [], "deploy_car")
    with pytest.raises(TermError, match="no argument bogus"):
        parse_args(SCHEMA, tokenize("prod bogus=1"), "d")
    with pytest.raises(TermError, match="too many words"):
        parse_args(SCHEMA, tokenize("prod extra"), "d")
    with pytest.raises(TermError, match="not a number"):
        parse_args(SCHEMA, tokenize("prod build=x"), "d")
    # one optional argument takes a word without its name (check_build 3, list_projects map)
    one = {"type": "object", "properties": {"build": {"type": "integer"}}}
    assert parse_args(one, tokenize("3"), "check_build") == {"build": 3}
    # an array that is the last required argument takes the rest of the words
    arr = {"type": "object", "required": ["names"], "properties": {"names": {"type": "array", "items": {"type": "string"}}}}
    assert parse_args(arr, tokenize("a b c"), "x") == {"names": ["a", "b", "c"]}


def test_api_guard_and_settings_guard():
    assert tm.api_guard("GET", "/api/state", None) == "read"
    assert tm.api_guard("POST", "/api/projects/0123456789ab/build", {}) == "change"
    assert tm.api_guard("POST", "/api/projects/0123456789ab/deploy", {}) == "deploy"
    assert tm.api_guard("POST", "/api/servers/prod/restart", {}) == "deploy"
    assert tm.api_guard("POST", "/api/fleet/upgrade", {}) == "deploy"
    for m, p in (("GET", "/api/mcp/token"), ("POST", "/api/mcp/approve"), ("POST", "/api/secrets"), ("GET", "/api/events"),
                 ("POST", "/api/terminal/run"), ("POST", "/api/agent/sessions"), ("POST", "/api/mcp/connect"),
                 ("POST", "/api/agent/sessions/0123456789ab/approve"), ("POST", "/api/uploads"),
                 ("GET", "/api/terminal/history"), ("GET", "/api/terminal/runs/0123456789ab"),
                 ("POST", "/api/servers"), ("POST", "/api/servers/import"), ("DELETE", "/api/servers/prod")):
        with pytest.raises(TermError, match="not for agents"):
            tm.api_guard(m, p, {})
    with pytest.raises(TermError, match="only you"):
        tm.api_guard("PATCH", "/api/settings", {"mcp": {"approval": "client"}})
    with pytest.raises(TermError, match="only you"):
        tm.settings_guard({"paths": {"wolvenkit": "C:\\evil.exe"}})
    with pytest.raises(TermError, match="only you"):
        tm.settings_guard({"ai": {"anthropic_base_url": "https://example.com"}})
    with pytest.raises(TermError, match="only you"):
        tm.settings_guard({"servers": []})
    with pytest.raises(TermError, match="only you"):
        tm.settings_guard({"deploy": {"allow_unsecured": True}})
    assert tm.api_guard("GET", "/api/terminal/spec", None) == "read"
    assert tm.api_guard("GET", "/api/servers", None) == "read"
    tm.settings_guard({"qc": {"interval_hours": 4}, "ai": {"autonomy": "ask"}})
    assert tm.api_path("qc") == "/api/qc" and tm.api_path("/api/state") == "/api/state"


# ----------------------------------------------------------------------------- the terminal in the Studio
@pytest.fixture
def studio():
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-term-")
    st = Studio(ws)
    try:
        yield st
    finally:
        st.close()
        shutil.rmtree(ws, ignore_errors=True)


def _run(st, line, session=None, wait=60):
    s = session or st.terminal.session()
    r = st.terminal.run(s, line)
    assert r.done.wait(wait), f"{line} did not end"
    return r


def _text(r, kinds=None):
    return "\n".join(l["t"] for l in r.lines if kinds is None or l["k"] in kinds)


def test_builtins_and_tools(studio):
    r = _run(studio, "help")
    assert r.state == "done" and "every Studio tool is a command" in _text(r)
    assert any(l["k"] == "table" and l["cols"][0] == "command" for l in r.lines)
    r = _run(studio, "help deploy_car")
    assert "deploy_car <server>" in _text(r) and "risk: deploy" in _text(r)
    r = _run(studio, "tools quality")
    assert "qc_report" in _text(r) and "run_qc" in _text(r)
    r = _run(studio, "status")
    assert "workspace" in _text(r)
    r = _run(studio, "list_servers")
    assert r.state == "done" and any(l["k"] == "json" for l in r.lines)
    r = _run(studio, "deploy_car")
    assert r.state == "failed" and "needs server" in _text(r, ("err",))
    r = _run(studio, "bulid_car")
    assert r.state == "failed" and "did you mean build_car" in _text(r)
    # chaining: && stops at a failure, ; goes on
    r = _run(studio, "deploy_car && echo never ; echo always")
    assert "never" not in _text(r, ("out",)) and "always" in _text(r, ("out",))
    r = _run(studio, "api GET /api/health")
    assert r.state == "done" and '"ok": true' in _text(r)
    r = _run(studio, "routes terminal")
    assert "/api/terminal/run" in _text(r)
    # every run begins with its line and ends with a control line naming its state
    assert r.lines[0]["k"] == "in" and r.lines[-1]["k"] == "ctl" and r.lines[-1]["state"] == "done"


def test_settings_from_the_terminal(studio):
    r = _run(studio, "settings qc.interval_hours=4")
    assert r.state == "done" and studio.settings.get()["qc"]["interval_hours"] == 4
    r = _run(studio, "settings qc.interval_hours")
    assert "= 4" in _text(r)
    r = _run(studio, "settings deploy.mode=nonsense")
    assert r.state == "failed" and "invalid settings" in _text(r)
    # an agent's terminal cannot change who approves, nor what the Studio runs
    mcp = studio.terminal.session(origin="mcp", label="Claude")
    r = _run(studio, "settings mcp.approval=client", session=mcp)
    assert r.state == "failed" and "only you" in _text(r)


def test_system_commands_for_the_user_and_for_agents(studio):
    r = _run(studio, f'sh "{sys.executable}" -c "print(6*7)"' if os.name == "nt" else "sh echo $((6*7)) && echo ok")
    assert r.state == "done" and "42" in _text(r, ("out",))
    r = _run(studio, "!exit 3")
    assert r.state == "failed" and "exit code 3" in _text(r)
    mcp = studio.terminal.session(origin="mcp", label="Claude")
    r = _run(studio, "!echo hi", session=mcp)
    assert r.state == "failed" and "Settings > Agents" in _text(r)
    # the converter's command line runs what it is told (--wolvenkit): a system command, for agents
    r = _run(studio, "cli sounds", session=mcp)
    assert r.state == "failed" and "Settings > Agents" in _text(r)
    studio.settings.update({"mcp": {"shell": True}})
    r = _run(studio, "!echo hi", session=mcp)
    assert r.state == "done" and "hi" in _text(r, ("out",))


def test_ctrl_c_stops_a_command(studio):
    s = studio.terminal.session()
    r = studio.terminal.run(s, "sh sleep 30" if os.name != "nt" else "sh Start-Sleep 30")
    time.sleep(1.0)
    assert r.state == "running" and r.id in s.active
    assert studio.terminal.cancel(r.id)
    assert r.done.wait(20)
    assert r.state == "cancelled" and r.exit == 130 and r.id not in s.active


def test_jobs_are_followed_with_their_log(studio):
    def work(job):
        for i in range(5):
            job.log(f"step {i}")
            job.set_progress(i / 5, f"part {i}")
            time.sleep(0.15)
        return {"ok": True}
    job = studio.jobs.submit("test", "a test job", work)
    s = studio.terminal.session()
    r = _run(studio, f"wait {job.id[:6]}", session=s)
    assert r.state == "done"
    logs = [l["t"] for l in r.lines if l["k"] == "log"]
    assert logs[:5] == [f"step {i}" for i in range(5)]
    jl = [l for l in r.lines if l["k"] == "job"]
    assert len(jl) == 1 and jl[0]["state"] == "done"            # one line per job: the latest replaces the rest
    r = _run(studio, "jobs", session=s)
    assert job.id in _text(r)
    r = _run(studio, f"log {job.id}", session=s)
    assert "step 4" in _text(r)
    # the scrollback keeps it for the page, the job's line once
    sb = studio.terminal.scrollback(r.session.id)
    assert sum(1 for l in sb["lines"] if l["k"] == "job" and l["job"] == job.id) == 2   # (wait's and log's)


def test_projects_and_the_open_project(studio):
    src = os.path.join(studio.workspace, "car_src")
    os.makedirs(src)
    p = studio.projects.create(src, name="Lambo Urus", kind="car")
    r = _run(studio, "projects")
    assert p.id in _text(r) and "Lambo Urus" in _text(r)
    s = studio.terminal.session()
    r = _run(studio, 'use "lambo urus"', session=s)
    assert r.state == "done" and s.project == p.id
    assert r.lines[-1]["project"]["id"] == p.id               # the prompt follows
    r = _run(studio, "use -", session=s)
    assert s.project is None


def test_ui_pages_and_events(studio):
    q = studio.bus.subscribe()
    r = _run(studio, "ui quality")
    assert r.state == "done"
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    assert any(e.get("type") == "ui" and e.get("hash") == "#/quality" for e in seen)
    assert any(e.get("type") == "term" and e["line"]["k"] == "in" for e in seen)
    r = _run(studio, "ui nowhere")
    assert r.state == "failed"


def test_the_ai_agent_from_the_terminal(studio):
    from fivem2open77.studio.agent.manager import AgentManager
    from fivem2open77.studio.agent.providers import ScriptedProvider
    prov = ScriptedProvider([
        [{"type": "text", "text": "Let me look."}, {"type": "tool_call", "name": "ask_user", "input": {"question": "Which server?"}}],
        [{"type": "tool_call", "name": "new_world_edit", "input": {"resource": "test_edit"}}],
        [{"type": "tool_call", "name": "finish", "input": {"summary": "all set"}}],
    ])
    studio.agents = AgentManager(studio, provider_factory=lambda name=None: prov)
    s = studio.terminal.session()
    r = _run(studio, "agent start --autonomy ask check my servers", session=s)
    assert r.state == "done", _text(r)
    t = _text(r)
    assert "Let me look." in t and "the agent asks: Which server?" in t and "agent say" in t
    assert s.agent
    r = _run(studio, "agent say the test one", session=s)
    assert "waiting for your OK: new_world_edit" in _text(r) and "agent approve" in _text(r)
    # an agent's terminal cannot approve a server change when the user wants to click it; this is a change
    r = _run(studio, "agent decline not now", session=s)
    assert "declined: new_world_edit" in _text(r) and "done: all set" in _text(r)
    r = _run(studio, "agent", session=s)
    assert s.agent in _text(r)
    r = _run(studio, f"agent log {s.agent}", session=s)
    assert "you \u203a the test one" in _text(r)


# ----------------------------------------------------------------------------- over HTTP and MCP
def _free_port():
    so = socket.socket()
    so.bind(("127.0.0.1", 0))
    port = so.getsockname()[1]
    so.close()
    return port


class _Live:
    def __init__(self, provider=None):
        from fivem2open77.studio import server as srv
        self.ws = tempfile.mkdtemp(prefix="f2o77-mcp-")
        self.port = _free_port()
        self.token = "t" * 24
        self.studio = None
        ready = threading.Event()

        def on_ready(httpd, studio):
            self.studio = studio
            if provider is not None:
                from fivem2open77.studio.agent.manager import AgentManager
                studio.agents = AgentManager(studio, provider_factory=lambda name=None: provider)
            ready.set()
        self.thread = threading.Thread(target=srv.serve, kwargs=dict(port=self.port, workspace=self.ws, token=self.token,
                                                                    on_ready=on_ready), daemon=True)
        self.thread.start()
        assert ready.wait(30)
        self.sid = None
        self.n = 0

    def req(self, method, path, body=None):
        h = {"Content-Type": "application/json", "X-F2O77-Token": self.token}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def rpc(self, method, params=None, notify=False):
        tok = self.studio.mcp.token
        h = {"Content-Type": "application/json", "Authorization": "Bearer " + tok}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=json.dumps(msg).encode(), method="POST", headers=h)
        with urllib.request.urlopen(r, timeout=120) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid
            b = resp.read()
            return json.loads(b) if b else None

    def init(self, name="claude-code"):
        a = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}})
        self.rpc("notifications/initialized", notify=True)
        return a

    def call(self, name, args):
        a = self.rpc("tools/call", {"name": name, "arguments": args})
        res = a["result"]
        return "\n".join(c.get("text", "") for c in res["content"] if c["type"] == "text"), res["isError"], res["content"]

    def close(self):
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/api/health"), timeout=2)
        except OSError:
            pass
        shutil.rmtree(self.ws, ignore_errors=True)


def test_terminal_over_http():
    live = _Live()
    try:
        st, s = live.req("POST", "/api/terminal/sessions", {})
        assert st == 200 and len(s["id"]) == 12
        st, spec = live.req("GET", "/api/terminal/spec")
        assert st == 200 and any(t["name"] == "build_car" for t in spec["tools"]) and spec["routes"]
        assert any(b["name"] == "agent" for b in spec["builtins"])
        assert not any(t["name"] in ("terminal", "terminal_output") for t in spec["tools"])
        st, r = live.req("POST", "/api/terminal/run", {"session": s["id"], "line": "echo hello"})
        assert st == 200
        for _ in range(100):
            st, v = live.req("GET", f"/api/terminal/runs/{r['run']}")
            if v["state"] != "running":
                break
            time.sleep(0.05)
        assert v["state"] == "done" and any(l["t"] == "hello" for l in v["lines"])
        st, sb = live.req("GET", f"/api/terminal/sessions/{s['id']}")
        assert any(l["t"] == "hello" for l in sb["lines"])
        st, hist = live.req("GET", "/api/terminal/history")
        assert hist[-1] == "echo hello"
        # the history is kept with the workspace
        assert os.path.isfile(os.path.join(live.ws, "terminal", "history.txt"))
    finally:
        live.close()


def test_mcp_tools_drive_the_whole_app():
    from fivem2open77.studio.agent.providers import ScriptedProvider
    prov = ScriptedProvider([
        [{"type": "text", "text": "On it."}, {"type": "tool_call", "name": "ask_user", "input": {"question": "Which server?"}}],
        [{"type": "tool_call", "name": "restart_server", "input": {"server": "prod"}}],
        [{"type": "tool_call", "name": "finish", "input": {"summary": "nothing restarted"}}],
    ])
    live = _Live(provider=prov)
    try:
        a = live.init()
        caps = a["result"]["capabilities"]
        assert caps["tools"]["listChanged"] is True
        names = {t["name"] for t in live.rpc("tools/list")["result"]["tools"]}
        for want in ("terminal", "terminal_output", "studio_api", "studio_api_routes", "studio_ui", "agent_start",
                     "agent_send", "agent_status", "agent_approve", "agent_stop"):
            assert want in names, want
        # the fingerprint is what the bridge polls, and polling is not activity
        before = live.studio.mcp.last_activity
        fp = live.rpc("f2o77/fingerprint")["result"]
        assert set(fp) >= {"tools", "prompts", "resources", "version"}
        assert live.studio.mcp.last_activity == before

        # the terminal: the same command line as the user's, its lines on the event bus
        q = live.studio.bus.subscribe()
        txt, err, _ = live.call("terminal", {"command": "help"})
        assert not err and "every Studio tool is a command" in txt and '"state": "done"' in txt
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        assert any(e.get("type") == "term" and e.get("origin") == "mcp" and e.get("label") == "Claude Code" for e in evs)
        txt, err, _ = live.call("terminal", {"command": "!echo hi"})
        assert err and "Settings > Agents" in txt
        txt, err, _ = live.call("terminal", {"command": "settings mcp.shell=true"})
        assert err and "only you" in txt
        # a long command answers running; terminal_output follows it
        txt, err, _ = live.call("terminal", {"command": "sh sleep 2 && echo late" if os.name != "nt" else "echo x", "seconds": 1})
        if os.name != "nt":
            assert err  # system commands are off for agents
        live.studio.settings.update({"mcp": {"shell": True}})
        txt, err, content = live.call("terminal", {"command": "sh sleep 2 && echo late", "seconds": 1})
        tail = json.loads(content[0]["text"].rsplit("\n", 1)[-1])
        assert tail["state"] == "running", txt
        seen, since = "", tail["next"]
        for _ in range(5):
            txt, err, content = live.call("terminal_output", {"run": tail["run"], "since": since, "seconds": 10})
            seen += txt
            tail = json.loads(content[0]["text"].rsplit("\n", 1)[-1])
            since = tail["next"]
            if tail["state"] != "running":
                break
        assert "late" in seen and tail["state"] == "done", seen
        # lines format, for the command-line client
        txt, err, _ = live.call("terminal", {"command": "echo a", "format": "lines"})
        v = json.loads(txt)
        assert v["state"] == "done" and [l["t"] for l in v["lines"] if l["k"] == "out"] == ["a"]

        # the API, as the app's buttons call it
        txt, err, _ = live.call("studio_api", {"path": "/api/state"})
        assert not err and txt.startswith("HTTP 200") and '"version"' in txt
        txt, err, _ = live.call("studio_api", {"method": "PATCH", "path": "/api/settings", "body": {"qc": {"interval_hours": 3}}})
        assert not err and live.studio.settings.get()["qc"]["interval_hours"] == 3
        for args in ({"path": "/api/mcp/token"}, {"method": "POST", "path": "/api/mcp/approve", "body": {"id": "x"}},
                     {"method": "PATCH", "path": "/api/settings", "body": {"mcp": {"approval": "client"}}},
                     {"method": "POST", "path": "/api/terminal/run", "body": {"line": "!calc"}}):
            txt, err, _ = live.call("studio_api", args)
            assert err and ("not for agents" in txt or "only you" in txt), (args, txt)
        txt, err, _ = live.call("studio_api", {"path": "/api/projects/0123456789ab"})
        assert err and "HTTP 400" in txt
        txt, err, _ = live.call("studio_api_routes", {"filter": "agent"})
        assert "/api/agent/sessions" in txt and "[the user's]" in txt

        # the window: a page shown to the user
        q = live.studio.bus.subscribe()
        txt, err, _ = live.call("studio_ui", {"page": "quality", "message": "look at the failing cars"})
        assert not err and '"shown": "#/quality"' in txt
        ev = [e for e in iter(lambda: q.get_nowait() if not q.empty() else None, None) if e.get("type") == "ui"]
        assert ev and ev[0]["by"] == "Claude Code" and ev[0]["message"] == "look at the failing cars"

        # the Studio's own AI agent, driven
        txt, err, _ = live.call("agent_start", {"goal": "check my servers", "autonomy": "ask", "seconds": 20})
        v = json.loads(txt)
        assert v["status"] == "waiting_user" and any("Which server?" in e for e in v["events"]), v
        sid = v["id"]
        txt, err, _ = live.call("agent_send", {"session": sid, "text": "prod", "seconds": 20})
        v = json.loads(txt)
        assert v["status"] == "waiting_approval" and v["pending"]["tool"] == "restart_server", v
        # a server restart waits for the user's own click when they asked for that
        live.studio.settings.update({"mcp": {"approval": "studio"}})
        txt, err, _ = live.call("agent_approve", {"session": sid, "approve": True})
        assert err and "user's click" in txt
        txt, err, _ = live.call("agent_approve", {"session": sid, "approve": False, "note": "not now", "seconds": 20})
        v = json.loads(txt)
        assert v["status"] == "finished" and any("nothing restarted" in e for e in v["events"]), v
        txt, err, _ = live.call("agent_status", {})
        assert sid in txt
        txt, err, _ = live.call("agent_status", {"session": sid, "since": 0})
        assert json.loads(txt)["summary"] == "nothing restarted"
        # full autonomy is capped while the user approves installs
        prov.script.append([{"type": "text", "text": "hello"}])
        txt, err, _ = live.call("agent_start", {"goal": "x", "autonomy": "full", "seconds": 10})
        assert json.loads(txt)["autonomy"] == "auto_safe"
    finally:
        live.close()


def test_bridge_tells_its_client_when_the_tools_change(monkeypatch):
    from fivem2open77.studio.bridge import Bridge
    live = _Live()
    try:
        out = io.BytesIO()
        b = Bridge(live.ws, attach_only=True, out=out)
        b.watch_every = 0                  # the test calls the check itself
        b.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).encode())
        b.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
        b.pool.shutdown(wait=True)
        first = [json.loads(l) for l in out.getvalue().splitlines()]
        assert first[0]["result"]["capabilities"]["tools"]["listChanged"] is True
        assert b.client_ready and b.fp and b.fp["tools"]
        n = len(out.getvalue())
        b.check_lists()                    # nothing changed: nothing said
        assert len(out.getvalue()) == n
        real = live.studio.mcp.fingerprint

        def changed():
            d = real()
            d["tools"] = "0" * 16
            return d
        monkeypatch.setattr(live.studio.mcp, "fingerprint", changed)
        b.check_lists()
        said = [json.loads(l) for l in out.getvalue()[n:].splitlines()]
        assert said == [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]
        b.closed = True
    finally:
        live.close()


def test_answers_are_strict_json_whatever_a_report_holds():
    """A NaN in an old build report (a floor too small to sample) went out as NaN, which a browser's
    JSON.parse refuses: the whole answer was lost.  It goes out as null."""
    from fivem2open77.studio import jsonutil
    assert jsonutil.dumps({"a": float("nan"), "b": [1.5, float("inf")], "c": {"d": float("-inf")}}) == \
        '{"a": null, "b": [1.5, null], "c": {"d": null}}'
    assert json.loads(jsonutil.dumps({"x": 1})) == {"x": 1}
    live = _Live()
    try:
        live.studio.bus.publish({"type": "test"})
        from fivem2open77.studio import server as srv
        # a route answering a NaN answers strict JSON over HTTP
        @srv.route("GET", r"/api/__nan_test")
        def _nan(h, q):
            h.json({"floor_holes_pct": float("nan")})
        try:
            st, body = live.req("GET", "/api/__nan_test")
            assert st == 200 and body == {"floor_holes_pct": None}
        finally:
            srv.ROUTES[:] = [r for r in srv.ROUTES if r[2] is not _nan]
    finally:
        live.close()
