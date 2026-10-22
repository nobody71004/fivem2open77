"""The Studio as an MCP server: agents outside the Studio (Claude Code, Claude Desktop, Codex, Cursor,
VS Code...) drive the same tools as the Studio's own AI agent.

Transport: MCP Streamable HTTP on the Studio's own port, ``POST /mcp`` (JSON answers; no
server-initiated stream: ``GET /mcp`` is 405), ``Authorization: Bearer <token>`` where the token is
the workspace's ``mcp/token`` file (made once; only this Windows user can read it).  Clients that
only speak stdio run the bridge, ``python -m fivem2open77 mcp`` (bridge.py): it finds the running
Studio (the workspace's ``run/studio.json``), starts it when it is not running, and relays.

A call that starts a long job (a build, a deploy) answers when the job is done, or after
``mcp.call_seconds`` with ``{"status": "running", "job": ...}``: the job goes on and ``wait_job``
follows it (no client times out waiting).  The agent's own app asks the user before a call (MCP
tool annotations say which calls change things and which touch servers); with ``mcp.approval`` =
``studio`` deploys also wait for a click in the Studio.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import secrets as pysecrets
import threading
import time
import uuid
from collections import deque

from .agent.tools import CAR_TOOLS, Context, StillRunning, Tool, ToolError, _obj, registry, text

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")     # the initialize-handshake era
NOT_OFFERED = ("ask_user", "finish")          # MCP clients talk to their user themselves
# deploy-risk tools that gate what they run themselves (a terminal line, an API call, the AI agent's
# call): the Studio click is asked for the server change inside, not for the call
SELF_GATED = ("terminal", "studio_api", "agent_approve")
SESSION_TTL = 24 * 3600

INSTRUCTIONS = """fivem2open77 Studio: converts FiveM (GTA V) cars and maps/MLOs into Open77 (Cyberpunk 2077
multiplayer) resources, builds them with WolvenKit, installs them on the user's Open77 servers over SSH, moves
converted maps on running servers, and edits Night City itself (delete / move / replace / add objects and ground
pieces, their collision following). Read the resource f2o77://guide first; call studio_status to see what is set
up. Project tools work on the session's project: import_resource, new_world_edit and open_project switch it (or
pass "project" to any project tool). Long calls answer {"status": "running", "job"}: call wait_job until done.
The whole app is yours without the screen: terminal runs any Studio command line (help lists them), studio_api
calls any endpoint of the Studio (studio_api_routes lists them), agent_start / agent_send / agent_status /
agent_approve / agent_stop drive the Studio's own AI agent, studio_ui shows the user a page of the Studio."""


def token_path(workspace: str) -> str:
    return os.path.join(workspace, "mcp", "token")


def ensure_token(workspace: str) -> str:
    """The workspace's MCP token, made on first use (owner-only file)."""
    p = token_path(workspace)
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
            if len(t) >= 32:
                return t
    except OSError:
        pass
    os.makedirs(os.path.dirname(p), exist_ok=True)
    t = pysecrets.token_urlsafe(32)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t)
    os.replace(tmp, p)
    return t


def read_token(workspace: str) -> str | None:
    """The workspace's MCP token if the Studio made one (the bridge only reads it: a bridge started by a
    Microsoft Store app writes into that app's own copy of AppData, where the Studio would not see it)."""
    try:
        with open(token_path(workspace), encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        return None
    return t if len(t) >= 32 else None


def reset_token(workspace: str) -> str:
    try:
        os.remove(token_path(workspace))
    except OSError:
        pass
    return ensure_token(workspace)


# ----------------------------------------------------------------------------- guide, prompts
def guide() -> str:
    from .agent.manager import SYSTEM_CAR, SYSTEM_MAP, SYSTEM_WORLD, SYSTEM_NCWE
    return "\n".join([
        "# fivem2open77 Studio for agents",
        "",
        INSTRUCTIONS,
        "",
        "## Rules",
        "- Act only through the tools and say only what a tool showed you.",
        "- Ask the user before deploying to a server they did not name, before restarting a server or applying a",
        "  map move (it restarts the server for everyone), and before anything that cannot be undone.",
        "- Never restart a server with players online unless the user says so.",
        "- The Studio holds server credentials and tokens: never ask the user for passwords or keys.",
        "- Make the smallest change that fixes a problem; if a tool fails twice the same way, stop and explain.",
        "- Coordinates: Night City metres, x east, y north, z up; yaw degrees counter-clockwise from above",
        "  (0 faces north, 90 west).",
        "",
        "## Cars" + SYSTEM_CAR.replace("ask_user", "ask the user").replace("finish with", "End with"),
        "",
        "## Maps" + SYSTEM_MAP.replace("ask_user", "ask the user").replace("finish with", "End with"),
        "",
        "## World edits" + SYSTEM_WORLD.replace("finish with", "End with"),
        "",
        "## NCWE editor" + SYSTEM_NCWE,
        "",
        "## Long calls",
        "build_car, build_map, build_world_edit and the deploys can take minutes. When a call answers",
        '{"status": "running", "job": "<id>"}, the work goes on in the Studio: call wait_job with that id',
        "(each call waits up to a minute) until it says done or failed. The user sees every job in the Studio.",
        "",
        "## The whole app, without the screen",
        "- terminal: the Studio's command line, the same one the user has in the app (Terminal page). Every tool is a",
        "  command (`build_car`, `deploy_car prod mode=stage`, `wait_job <id>`); built-ins: help, tools, status, projects,",
        "  use <project>, servers, jobs, log <job> -f, wait <job>, cancel <job>, agent ..., api METHOD /api/..., routes,",
        "  cli <converter args>, ui <page>, settings key=value, history. `a && b` chains. Each line you run shows up in",
        "  the user's terminal as it runs. A command that outlives `seconds` answers running with a run id: follow it",
        "  with terminal_output. System commands (sh / !) only when the user allowed them (Settings > Agents).",
        "- studio_api: any endpoint of the Studio's own API (studio_api_routes lists them with what each does): what",
        "  the app's buttons call. Uploads, secrets, the MCP token and approvals are the user's and are refused.",
        "- The Studio's AI agent: agent_start (a goal, optionally a project), then agent_status / agent_send /",
        "  agent_approve / agent_stop. It works in the Studio, the user sees it on the Agent page.",
        "- studio_ui: show the user a page (a project, the quality page, the terminal...) while you work, and a note.",
        "- Installs and restarts (deploy risk) wait for the user's click in the Studio when Settings > Agents says so,",
        "  however they are asked for: a tool, a terminal line, an API call or the AI agent.",
    ])


PROMPTS = {
    "convert_car": {
        "description": "Convert a FiveM car end to end (and install it on a server when one is named).",
        "arguments": [{"name": "path", "description": "the FiveM car: folder or .zip/.rar/.7z on this PC", "required": True},
                      {"name": "server", "description": "server id to install it on (optional)", "required": False}],
        "text": ("Read f2o77://guide. Then: import_resource path={path} kind=car; inspect_car; render_car (parts) and fix "
                 "wrongly assigned bones; check display_name and the engine sound; build_car and read its QA."
                 "{server_step} Tell me the spawn command and what to check in game."),
    },
    "convert_map": {
        "description": "Convert a FiveM map / MLO, bake it at a spot and install it.",
        "arguments": [{"name": "path", "description": "the FiveM map: folder or archive on this PC", "required": True},
                      {"name": "place", "description": "x,y,z,yaw in Night City (optional)", "required": False},
                      {"name": "server", "description": "server id to install it on (optional)", "required": False}],
        "text": ("Read f2o77://guide. Then: import_resource path={path} kind=map; get_map and read the warnings; render_map "
                 "iso and plan; {place_step}build_map and map_build_report (collision coverage, QA).{server_step} Tell me "
                 "where the map stands and what to check in game."),
    },
    "move_map": {
        "description": "Move a converted map on a running server.",
        "arguments": [{"name": "map", "description": "the map's resource id", "required": True},
                      {"name": "where", "description": "where it should go (coordinates, or near a player)", "required": True},
                      {"name": "server", "description": "server id", "required": False}],
        "text": ("Read f2o77://guide. live_maps{server_arg}; set the new spot of {map} ({where}) with set_map_spot; "
                 "show me the new spot and ask me before apply_map_move (it restarts the server); then wait_map_move "
                 "until applied and live_map to confirm."),
    },
    "edit_world": {
        "description": "Change Night City at a place: delete, move, replace or add objects (collision follows).",
        "arguments": [{"name": "location", "description": "x,y[,z] or a description", "required": True},
                      {"name": "goal", "description": "what to change", "required": True}],
        "text": ("Read f2o77://guide (World edits). world_status; new_world_edit if none is open; world_find around "
                 "{location}; then: {goal}. Use dry_run for clearing an area and show me the list first. Read the notes "
                 "of each answer. build_world_edit at the end; deploy only when I say so."),
    },
}


def prompt_messages(name: str, args: dict) -> list:
    p = PROMPTS[name]
    a = {k: str(v) for k, v in (args or {}).items() if v not in (None, "")}
    for arg in p["arguments"]:
        if arg["required"] and not a.get(arg["name"]):
            raise ValueError(f"argument {arg['name']} is required")
    server = a.get("server")
    t = p["text"].format(
        path=a.get("path", ""), map=a.get("map", ""), where=a.get("where", ""), location=a.get("location", ""),
        goal=a.get("goal", ""),
        place_step=(f"set_map_options place {a['place']}; " if a.get("place") else ""),
        server_step=(f" Then deploy it to server {server} (stage mode) and check server_status." if server else
                     " Do not install it anywhere yet."),
        server_arg=(f" server={server}" if server else ""))
    return [{"role": "user", "content": {"type": "text", "text": t}}]


# ----------------------------------------------------------------------------- sessions
class McpSession:
    """What the Studio knows of one connected agent: its app, its open project, its calls."""

    def __init__(self, hub, client: dict, protocol: str):
        self.hub = hub
        self.id = uuid.uuid4().hex
        self.client = {"name": str((client or {}).get("name") or "agent")[:80],
                       "version": str((client or {}).get("version") or "")[:40]}
        self.protocol = protocol
        self.created = self.seen = time.time()
        self.project = None
        self.calls = deque(maxlen=200)
        self.active = {}               # request id -> call
        self.jobs = deque(maxlen=50)
        self.lock = threading.Lock()

    @property
    def label(self) -> str:
        n = self.client["name"].lower()
        for key, nice in (("fivem2open77-term", "Command prompt"), ("fivem2open77-qc", "Quality sweep"),
                          ("claude-code", "Claude Code"), ("claude", "Claude"), ("codex", "Codex"), ("cursor", "Cursor"),
                          ("vscode", "VS Code"), ("visual studio code", "VS Code"), ("windsurf", "Windsurf"),
                          ("f2o77-test", "Test client")):
            if key in n:
                return nice
        return self.client["name"]

    def view(self) -> dict:
        with self.lock:
            return {"id": self.id[:12], "client": self.client, "label": self.label, "protocol": self.protocol,
                    "created": self.created, "seen": self.seen, "project": self.project,
                    "active": [dict(c) for c in self.active.values()], "calls": list(self.calls)[-40:]}

    def publish(self, kind, **data):
        self.hub.studio.bus.publish({"type": "mcp", "kind": kind, "session": self.id[:12], "label": self.label, **data})


class _CallSession:
    """The agent-tool session interface (tools.Context) for one call of an MCP session."""

    vision = True

    def __init__(self, s: McpSession, soft_timeout):
        self.s = s
        self.stopping = False
        self.soft_timeout = soft_timeout
        self.last_change_error = None
        self.finish_warned = False

    @property
    def project(self):
        return self.s.project

    def switch_project(self, pid):
        self.s.hub.studio.projects.get(pid)
        self.s.project = pid
        self.s.publish("project", project=pid)

    def note(self, t):
        self.s.publish("note", text=str(t)[:500])

    def attach_job(self, job):
        self.s.jobs.append(job.id)
        self.s.publish("job", job=job.id, title=job.title)

    def ask_user(self, question):
        raise ToolError("ask the user in your own conversation (this tool is for the Studio's built-in agent)")

    def finish(self, summary):
        pass


# ----------------------------------------------------------------------------- the hub
class McpHub:
    def __init__(self, studio):
        self.studio = studio
        self.sessions = {}
        self.pending = {}               # approval id -> {...}
        self.lock = threading.Lock()
        self.tools = {n: t for n, t in registry().items() if n not in NOT_OFFERED}
        for t in self._own_tools():
            self.tools[t.name] = t
        from .agent.maptools import MAP_TOOLS
        from .agent.worldtools import WORLD_TOOLS
        self.project_tools = set(CAR_TOOLS) | set(MAP_TOOLS) | (set(WORLD_TOOLS) - {"new_world_edit", "world_prepare", "world_find",
                                                                                 "world_object", "night_city"})
        self.last_activity = 0.0
        try:
            ensure_token(studio.workspace)      # made by the Studio at its start, before any bridge reads it
        except OSError:
            pass

    # ---- token
    @property
    def token(self) -> str:
        return ensure_token(self.studio.workspace)

    def settings(self) -> dict:
        return self.studio.settings.get()["mcp"]

    # ---- tools of the MCP server itself
    def _own_tools(self):
        def t_status(ctx, args):
            st = self.studio
            tools = st.tools_status()
            jobs = [j for j in st.jobs.list() if j["state"] in ("queued", "running")]
            return text({"studio": {"version": _app_version(), "workspace": st.workspace, "ready": tools.get("ready")},
                         "tools": {k: {"ok": v.get("ok"), "path": v.get("path")} for k, v in tools.items()
                                   if k in ("wolvenkit", "dotnet", "game", "donor", "linux_dotnet", "sevenzip")},
                         "servers": [{k: s[k] for k in ("id", "label", "host", "names")} for s in st.settings.get()["servers"]],
                         "projects": [{k: p.get(k) for k in ("id", "kind", "name")} for p in st.projects.list()[:30]],
                         "open_project": ctx.session.project, "running_jobs": jobs[:10],
                         "world_index": st.world_index_status()})

        def t_wait(ctx, args):
            jid = str(args.get("job") or "")
            job = self.studio.jobs.get(jid)
            if job is None:
                raise ToolError(f"no job {jid} (it may be from before the Studio restarted)")
            limit = max(1.0, min(55.0, float(args.get("seconds", 50))))
            t0 = time.time()
            while job.state in ("queued", "running") and time.time() - t0 < limit:
                if ctx.session.stopping:
                    break
                time.sleep(0.25)
            out = {"job": job.id, "title": job.title, "status": {"done": "done", "failed": "failed",
                                                                  "cancelled": "cancelled"}.get(job.state, "running"),
                   "progress": job.progress, "step": job.meta.get("step"),
                   "last_lines": [x["text"] for x in job.lines[-12:]]}
            if job.state == "done":
                out["result"] = _compact(job.result)
            elif job.state == "failed":
                out["error"] = job.error
                return [{"type": "text", "text": json.dumps(out, indent=1, default=str)}], True
            return text(out)

        def t_qc_report(ctx, args):
            v = self.studio.qc.view()
            rows = v["projects"]
            want = str(args.get("status") or "")
            if want:
                rows = [r for r in rows if r["status"] == want]
            elif not args.get("all"):
                rows = [r for r in rows if r["status"] != "ok"]
            return text({"time": v["time"], "converter": v["converter"], "ok": v["ok"], "counts": v["counts"],
                         "next_sweep": v["next"], "running": v["running"], "settings": v["settings"],
                         "projects": [{k: r.get(k) for k in ("id", "name", "kind", "build", "status", "donor", "errors",
                                                             "warnings")} for r in rows[:120]]})

        def t_qc_run(ctx, args):
            only = args.get("projects")
            job = self.studio.qc.run_job(rebuild=args.get("rebuild"), only=only or None)
            ctx.wait_job(job)
            if job.state == "failed":
                raise ToolError(job.error or "the quality sweep failed")
            return text(job.result)

        def t_deploy_all(ctx, args):
            job = self.studio.car_bundles_deploy(str(args.get("server") or ""), names=args.get("names"),
                                                 mode=args.get("mode") or None)
            ctx.wait_job(job, 3600)
            if job.state == "failed":
                raise ToolError(job.error or "the deploy failed")
            return text(job.result)

        def t_cancel(ctx, args):
            job = self.studio.jobs.get(str(args.get("job") or ""))
            if job is None:
                raise ToolError("no such job")
            ok = job.cancel()
            return text({"cancelled": ok, **({"stops_after": job.cancel_late} if ok and job.cancel_late else {})})

        # ---------------------------------------------------------------- the whole app, without the screen
        def term_answer(run, since=0, fmt="text"):
            v = run.view(since)
            if fmt == "lines":
                blocks = [{"type": "text", "text": json.dumps({k: v[k] for k in ("run", "state", "exit", "next", "lines")},
                                                               default=str)}]
                return blocks, v["state"] == "failed"
            parts, images = [], []
            for line in v["lines"]:
                k = line.get("k")
                if k == "ctl":
                    continue
                if k == "img" and line.get("data") and len(images) < 4:
                    images.append({"type": "image", "png": line["data"], "mimeType": line.get("mime") or "image/png"})
                parts.append(("$ " if k == "in" else "") + line.get("t", ""))
            body = "\n".join(parts)
            if len(body) > 40000:
                body = "(the start is cut: terminal_output with since reads it all)\n" + body[-40000:]
            tail = {"run": v["run"], "state": v["state"], "exit": v["exit"], "next": v["next"]}
            if v["state"] == "running":
                tail["then"] = "terminal_output with this run (and since=next) follows it; cancel=true stops it"
            blocks = [{"type": "text", "text": body + "\n" + json.dumps(tail)}] + images
            return blocks, v["state"] == "failed"

        def t_terminal(ctx, args):
            line = str(args.get("command") or "").strip()
            if not line:
                raise ToolError("command: a Studio command line (help lists the commands)")
            term = self.studio.terminal
            ts = term.session_for_mcp(ctx.session.s)
            run = term.run(ts, line)
            limit = max(1.0, min(600.0, float(args.get("seconds") or self.settings()["call_seconds"])))
            t0 = time.time()
            while not run.done.wait(0.25):
                if ctx.session.stopping:
                    term.cancel(run.id)
                    run.done.wait(5)
                    break
                if time.time() - t0 > limit:
                    break
            return term_answer(run, 0, args.get("format") or "text")

        def t_terminal_output(ctx, args):
            term = self.studio.terminal
            try:
                run = term.get_run(str(args.get("run") or ""))
            except Exception as e:  # noqa: BLE001 - TermError
                raise ToolError(str(e))
            if run.session.mcp is not ctx.session.s:
                raise ToolError("that run is not one of this session's (terminal answers the run ids it starts)")
            if args.get("cancel"):
                term.cancel(run.id)
                run.done.wait(10)
            since = int(args.get("since") or 0)
            limit = max(0.0, min(600.0, float(args.get("seconds", 30))))
            t0 = time.time()
            while run.state == "running" and time.time() - t0 < limit and not ctx.session.stopping:
                if run.count > since and time.time() - t0 > 1.0:
                    run.done.wait(0.75)                 # new output: answer with it (and with its end, when close)
                    break
                time.sleep(0.25)
            return term_answer(run, since, args.get("format") or "text")

        def t_api(ctx, args):
            from .server import call_route
            from .terminal import TermError, api_guard, api_path
            method = str(args.get("method") or "GET").upper()
            try:
                path = api_path(args.get("path"))
                risk = api_guard(method, path.split("?")[0], args.get("body"))
            except TermError as e:
                raise ToolError(str(e))
            if risk == "deploy" and self.settings()["approval"] == "studio":
                ok, note = self._approve(ctx.session.s, f"{method} {path}", args.get("body") or {}, ctx.session)
                if not ok:
                    raise ToolError("the user declined this in the Studio" + (f": {note}" if note else ""))
            ans = call_route(self.studio, method, path, body=args.get("body"), query=args.get("query"))
            st = ans["status"]
            if "json" in ans:
                txt = json.dumps(ans["json"], indent=1, default=str)
                if len(txt) > 60000:
                    txt = txt[:60000] + "\n... (cut: ask for less)"
                return [{"type": "text", "text": f"HTTP {st}\n{txt}"}], st >= 400
            if ans.get("data") is not None and (ans.get("content_type") or "").startswith("image/"):
                return [{"type": "text", "text": f"HTTP {st} {ans['content_type']}"},
                        {"type": "image", "png": base64.b64encode(ans["data"]).decode(), "mimeType": ans["content_type"]}], False
            if "file" in ans:
                return text({"status": st, "file": ans["file"], "bytes": ans.get("bytes"), "content_type": ans.get("content_type")})
            return text({"status": st, "content_type": ans.get("content_type"), "bytes": len(ans.get("data") or b"")})

        def t_routes(ctx, args):
            from .server import route_list
            from .terminal import API_REFUSED
            flt = str(args.get("filter") or "").lower()
            out = []
            for r in route_list():
                if flt and flt not in r["path"].lower() and flt not in r["about"].lower():
                    continue
                refused = any((m is None or m == r["method"]) and re.match("^" + rx + "$", re.sub(r"\{[^}]+\}", "x", r["path"]))
                              for m, rx, _why in API_REFUSED) and r["path"] not in ("/api/terminal/spec", "/api/terminal/history")
                out.append(f"{r['method']:6s} {r['path']}  -- {r['about']}" + ("  [the user's]" if refused else ""))
            return text("\n".join(out) or "(no endpoint matches)")

        def t_ui(ctx, args):
            from .terminal import TermError
            term = self.studio.terminal
            try:
                target = term.ui_target(page=args.get("page"), project=args.get("project"), tab=args.get("tab")) \
                    if (args.get("page") or args.get("project")) else None
            except TermError as e:
                raise ToolError(str(e))
            except Exception as e:  # noqa: BLE001 - an unknown project
                raise ToolError(str(e))
            msg = str(args.get("message") or "").strip()[:300] or None
            label = ctx.session.s.label
            if target:
                n = term.ui_navigate(target, by=label, message=msg)
            else:
                if not msg:
                    raise ToolError("page, project or message")
                self.studio.bus.publish({"type": "ui", "action": "toast", "text": f"{label}: {msg}", "by": label})
                n = len(self.studio.bus._subs)
            return text({"shown": target, "message": msg, "windows": n,
                         "note": None if n else "no Studio window is open: the user does not see it"})

        def agent_answer(s, since, seconds, cs):
            from .terminal import agent_compact, agent_follow
            if seconds > 0:
                agent_follow(s, since, lambda ev: None, stop=lambda: cs.stopping, timeout=seconds)
            v = agent_compact(s.view(since))
            return text(v), s.status == "failed"

        def t_agent_start(ctx, args):
            from .terminal import TermError, agent_autonomy, agents_of
            from .agent.providers import ProviderError
            goal = str(args.get("goal") or "").strip()
            if not goal:
                raise ToolError("goal: what the Studio's agent should do")
            try:
                m = agents_of(self.studio)
                pid = self._resolve_project(args["project"]) if args.get("project") else ctx.session.project
                v = m.start(project=pid, goal=goal, autonomy=agent_autonomy(self.studio, args.get("autonomy"), "mcp"),
                            provider=args.get("provider"))
            except (TermError, ProviderError) as e:
                raise ToolError(str(e))
            s = m.sessions[v["id"]]
            ctx.session.s.publish("agent", agent=s.id)
            return agent_answer(s, 0, max(0.0, min(300.0, float(args.get("seconds", 45)))), ctx.session)

        def _agent(args):
            from .terminal import TermError, agent_session
            try:
                return agent_session(self.studio, args.get("session"))
            except TermError as e:
                raise ToolError(str(e))

        def t_agent_send(ctx, args):
            s = _agent(args)
            msg = str(args.get("text") or "").strip()
            if not msg:
                raise ToolError("text: what to tell the agent")
            before = len(s.events)
            self.studio.agents.message(s.id, msg)
            return agent_answer(s, before, max(0.0, min(300.0, float(args.get("seconds", 45)))), ctx.session)

        def t_agent_status(ctx, args):
            if not args.get("session"):
                if self.studio.agents is None:
                    raise ToolError("the Studio's AI agent is not available")
                return text([{k: x.get(k) for k in ("id", "status", "goal", "project", "steps", "created")}
                             for x in self.studio.agents.list()[:30]] or "no session yet: agent_start")
            s = _agent(args)
            return agent_answer(s, int(args.get("since") or 0), max(0.0, min(300.0, float(args.get("seconds", 0)))),
                                ctx.session)

        def t_agent_approve(ctx, args):
            s = _agent(args)
            p = s.pending
            if not p:
                raise ToolError("the agent is not waiting for an approval")
            approve = bool(args.get("approve"))
            if approve and p.get("risk") == "deploy" and self.settings()["approval"] == "studio":
                raise ToolError("installs and restarts wait for the user's click in the Studio (its Agent page)")
            before = len(s.events)
            self.studio.agents.approve(s.id, p["call_id"], approve, args.get("note"))
            return agent_answer(s, before, max(0.0, min(300.0, float(args.get("seconds", 45)))), ctx.session)

        def t_agent_stop(ctx, args):
            s = _agent(args)
            self.studio.agents.stop(s.id)
            return text({"session": s.id, "stopping": True})

        SESSION = {"type": "string", "description": "the agent session id (agent_status lists them)"}
        WAIT = {"type": "number", "description": "seconds to follow the agent before answering (default 45, at most 300)"}
        app_tools = [
            Tool("terminal", "Run a command line in the Studio's terminal -- the same one the user has in the app, and they "
                 "see your line run there. Every Studio tool is a command (build_car, deploy_car prod mode=stage); "
                 "built-ins: help, tools, status, projects, use <project>, servers, jobs, log <job> -f, wait <job>, "
                 "cancel <job>, agent start|say|approve|decline|stop|watch, api METHOD /api/..., routes, cli <converter "
                 "args>, ui <page>, settings key=value, history; `a && b` chains; sh/! system commands only when the user "
                 "allowed them. Answers the output; a longer command answers running with its run id (terminal_output).",
                 _obj({"command": {"type": "string"}, "seconds": {"type": "number", "description": "how long to wait "
                       "for it (default: the Studio's call_seconds)"}, "format": {"type": "string", "enum": ["text", "lines"]}},
                      ["command"]), "deploy", t_terminal),
            Tool("terminal_output", "Follow a terminal command that answered running: its output from line `since` on, "
                 "waiting up to `seconds` (default 30) for more or for its end; cancel=true stops it (Ctrl+C).",
                 _obj({"run": {"type": "string"}, "since": {"type": "integer"}, "seconds": {"type": "number"},
                       "cancel": {"type": "boolean"}, "format": {"type": "string", "enum": ["text", "lines"]}}, ["run"]),
                 "read", t_terminal_output),
            Tool("studio_api", "Call any endpoint of the Studio's own API -- what the app's buttons call (studio_api_routes "
                 "lists them). method GET|POST|PUT|PATCH|DELETE, path like /api/projects/<id>/profile, body (JSON) and "
                 "query. PNG answers come back as images. Uploads, secrets, the MCP token and approvals are the user's.",
                 _obj({"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                       "path": {"type": "string"}, "body": {}, "query": {"type": "object"}}, ["path"]),
                 "deploy", t_api),
            Tool("studio_api_routes", "Every endpoint of the Studio's API with what it does (filter by text).",
                 _obj({"filter": {"type": "string"}}), "read", t_routes),
            Tool("studio_ui", "Show the user something in the Studio's window while you work: a page (home, cars, maps, "
                 "world, live, servers, quality, agent, activity, terminal, settings, agents), a project (with a tab: "
                 "overview, mapping, handling, sound, build, deploy...), and/or a short message. No clicks, no screen.",
                 _obj({"page": {"type": "string"}, "project": {"type": "string"}, "tab": {"type": "string"},
                       "message": {"type": "string"}}), "read", t_ui),
            Tool("agent_start", "Start the Studio's own AI agent (Claude or a local Ollama model, set up in the Studio) on a "
                 "goal, optionally on a project, and follow it until it waits (seconds). It works in the Studio with the "
                 "Studio's tools; the user watches it on the Agent page. autonomy: ask (approve every change), auto_safe "
                 "(approve server changes), full (never ask; capped to auto_safe when the user wants to approve installs).",
                 _obj({"goal": {"type": "string"}, "project": {"type": "string"},
                       "autonomy": {"type": "string", "enum": ["ask", "auto_safe", "full"]},
                       "provider": {"type": "string", "enum": ["anthropic", "ollama"]}, "seconds": WAIT}, ["goal"]),
                 "change", t_agent_start),
            Tool("agent_send", "Write to the Studio's AI agent: the answer to its question, or what to do next; then follow "
                 "it until it waits (seconds).", _obj({"session": SESSION, "text": {"type": "string"}, "seconds": WAIT},
                                                      ["session", "text"]), "change", t_agent_send),
            Tool("agent_status", "The Studio's AI agent: without session its sessions; with one what it said and did "
                 "from event `since`, its status and what it waits for (seconds > 0 waits for it to settle).",
                 _obj({"session": SESSION, "since": {"type": "integer"}, "seconds": {"type": "number"}}), "read", t_agent_status),
            Tool("agent_approve", "Decide the call the Studio's AI agent waits on (approve true or false, with a note). "
                 "Installs and restarts wait for the user's own click when they asked for that.",
                 _obj({"session": SESSION, "approve": {"type": "boolean"}, "note": {"type": "string"}, "seconds": WAIT},
                      ["session", "approve"]), "deploy", t_agent_approve),
            Tool("agent_stop", "Stop the Studio's AI agent.", _obj({"session": SESSION}, ["session"]), "change", t_agent_stop),
        ]

        return app_tools + [
            Tool("studio_status", "What is set up in the Studio: WolvenKit, the game folder, the donor files, servers, "
                 "projects, running jobs, the world index. Call it first.", _obj(), "read", t_status),
            Tool("wait_job", "Wait for a Studio job a call answered as running (up to `seconds`, default 50, each call); "
                 "its result when done.", _obj({"job": {"type": "string"}, "seconds": {"type": "number"}}, ["job"]),
                 "read", t_wait),
            Tool("cancel_job", "Stop a running Studio job (a build, a deploy...). A deploy stops at once while it "
                 "uploads (nothing is installed); while the server installs it stops right after that step "
                 "(stops_after names it).", _obj({"job": {"type": "string"}}, ["job"]),
                 "change", t_cancel),
            Tool("qc_report", "The latest quality sweep: every car and map's status (ok, warn, fail, stale = built before "
                 "a converter fix, failed, unbuilt) with the failing checks (parts not on a rig bone, doors the donor "
                 "cannot open, wheels off their bones, geometry left out, map collision/floor coverage). Lists what is "
                 "not ok unless all is true; status filters.",
                 _obj({"status": {"type": "string", "enum": ["ok", "warn", "fail", "failed", "stale", "unbuilt"]},
                       "all": {"type": "boolean"}}), "read", t_qc_report),
            Tool("run_qc", "Run a quality sweep now: check every project's latest build (or only `projects`) and, with "
                 "rebuild (default: the Studio's qc.auto_rebuild setting), rebuild what fails or is stale and check it "
                 "again. Long: answers running, then wait_job.",
                 _obj({"rebuild": {"type": "boolean"}, "projects": {"type": "array", "items": {"type": "string"}}}),
                 "change", t_qc_run),
            Tool("deploy_all_cars", "Install every car whose latest build is good on a server, carried in car bundles "
                 "(f2o77_cars_N, 12 cars each: an Open77 server may require at most 64 mods). Cars keep their bundle; only "
                 "bundles whose cars changed are re-packed; cars installed on their own are updated in place. mode as "
                 "deploy_car (stage: the car hub restarts the server when nobody is online). Long: answers running, "
                 "then wait_job.",
                 _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}},
                       "mode": {"type": "string", "enum": ["stage", "restart", "files"]}}, ["server"]),
                 "deploy", t_deploy_all),
        ]

    def tool_list(self) -> list:
        from .agent.maptools import LIVE_TOOLS
        server_tools = set(LIVE_TOOLS) | {"list_servers", "server_status", "read_server_log", "restart_server",
                                          "deploy_car", "deploy_all_cars", "deploy_map", "deploy_world_edit", "remove_world_edit",
                                          "enable_agent_api", "terminal", "studio_api", "agent_start", "agent_send",
                                          "agent_approve"}
        out = []
        for name in sorted(self.tools):
            t = self.tools[name]
            schema = copy.deepcopy(t.schema)
            if name in self.project_tools:
                schema.setdefault("properties", {})["project"] = {
                    "type": "string", "description": "project id or name (default: the session's open project)"}
            out.append({
                "name": name, "title": name.replace("_", " ").capitalize(), "description": t.description,
                "inputSchema": schema,
                "annotations": {"title": name.replace("_", " ").capitalize(), "readOnlyHint": t.risk == "read",
                                "destructiveHint": t.risk == "deploy", "idempotentHint": t.risk == "read",
                                "openWorldHint": name in server_tools},
            })
        return out

    def fingerprint(self) -> dict:
        """Hashes of what a client lists: tools, prompts, resources (and the Studio's version)."""
        import hashlib

        def h(v):
            return hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return {"version": _app_version(), "tools": h(self.tool_list()),
                "prompts": h({k: [v["description"], v["arguments"]] for k, v in PROMPTS.items()}),
                "resources": h([r["uri"] for r in self.resource_list()])}

    # ---- sessions
    def _session(self, sid) -> McpSession | None:
        now = time.time()
        with self.lock:
            for k in [k for k, s in self.sessions.items() if now - s.seen > SESSION_TTL]:
                del self.sessions[k]
            s = self.sessions.get(sid or "")
        if s:
            s.seen = now
        return s

    def sessions_view(self) -> list:
        with self.lock:
            items = list(self.sessions.values())
        return [s.view() for s in sorted(items, key=lambda s: -s.seen)]

    def end(self, sid) -> bool:
        with self.lock:
            s = self.sessions.pop(sid or "", None)
        if s:
            s.publish("closed")
        return s is not None

    # ---- JSON-RPC
    def handle(self, msg, sid=None):
        """One JSON-RPC message (or a batch) -> (answer or None, the session id to send back)."""
        if isinstance(msg, list):
            answers = []
            for m in msg:
                a, sid = self.handle(m, sid)
                if a is not None:
                    answers.append(a)
            return (answers or None), sid
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, -32600, "invalid request"), sid
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            return (None if mid is None else _err(mid, -32600, "invalid request")), sid
        if method == "f2o77/fingerprint":
            # what the stdio bridge polls to tell its client the tool list changed (a Studio update): not
            # activity, so a Studio started for agents still stops when they are done
            return (None if mid is None else _ok(mid, self.fingerprint())), sid
        if method != "ping":
            self.last_activity = time.time()
        if method == "initialize":
            want = params.get("protocolVersion")
            proto = want if want in PROTOCOLS else PROTOCOLS[0]
            s = McpSession(self, params.get("clientInfo") or {}, proto)
            with self.lock:
                self.sessions[s.id] = s
            s.publish("connected", client=s.client)
            return _ok(mid, {"protocolVersion": proto,
                             "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True, "subscribe": False},
                                              "prompts": {"listChanged": True}, "logging": {}},
                             "serverInfo": {"name": "fivem2open77-studio", "title": "fivem2open77 Studio", "version": _app_version()},
                             "instructions": INSTRUCTIONS}), s.id
        if method == "ping":
            return _ok(mid, {}), sid
        if method == "server/discover":
            # the stateless 2026-07-28 era is not spoken here: clients fall back to initialize
            return (None if mid is None else _err(mid, -32601, "server/discover is not supported: use initialize")), sid
        s = self._session(sid)
        if s is None:
            return (None if mid is None else _err(mid, -32001, "unknown or expired session: initialize again")), sid
        if mid is None:                                     # notifications
            if method == "notifications/cancelled":
                rid = params.get("requestId")
                with s.lock:
                    call = s.active.get(json.dumps(rid))
                if call is not None:
                    call["_ctx"].stopping = True
            return None, sid
        try:
            if method == "tools/list":
                return _ok(mid, {"tools": self.tool_list()}), sid
            if method == "tools/call":
                return _ok(mid, self.call(s, mid, params.get("name"), params.get("arguments") or {})), sid
            if method == "resources/list":
                return _ok(mid, {"resources": self.resource_list()}), sid
            if method == "resources/templates/list":
                return _ok(mid, {"resourceTemplates": [
                    {"uriTemplate": "f2o77://project/{id}", "name": "project", "title": "A Studio project",
                     "description": "A project's state as JSON (car profile, map options and look, world edits...)",
                     "mimeType": "application/json"}]}), sid
            if method == "resources/read":
                return _ok(mid, {"contents": [self.read_resource(str(params.get("uri") or ""))]}), sid
            if method == "prompts/list":
                return _ok(mid, {"prompts": [{"name": k, "title": k.replace("_", " ").capitalize(),
                                              "description": v["description"], "arguments": v["arguments"]}
                                             for k, v in PROMPTS.items()]}), sid
            if method == "prompts/get":
                name = params.get("name")
                if name not in PROMPTS:
                    return _err(mid, -32602, f"unknown prompt {name}"), sid
                return _ok(mid, {"description": PROMPTS[name]["description"],
                                 "messages": prompt_messages(name, params.get("arguments") or {})}), sid
            if method == "logging/setLevel":
                return _ok(mid, {}), sid
            if method == "completion/complete":
                return _ok(mid, {"completion": {"values": [], "total": 0, "hasMore": False}}), sid
            return _err(mid, -32601, f"unknown method {method}"), sid
        except KeyError as e:
            return _err(mid, -32602, str(e).strip("'\"")), sid
        except ValueError as e:
            return _err(mid, -32602, str(e)), sid

    # ---- resources
    def resource_list(self) -> list:
        out = [{"uri": "f2o77://guide", "name": "guide", "title": "How to drive the Studio",
                "description": "Workflows for cars, maps and world edits; the rules; coordinates", "mimeType": "text/markdown"},
               {"uri": "f2o77://projects", "name": "projects", "title": "Projects", "mimeType": "application/json",
                "description": "The Studio's cars, maps and world edits"},
               {"uri": "f2o77://servers", "name": "servers", "title": "Servers", "mimeType": "application/json",
                "description": "The Open77 servers set up in the Studio (no secrets)"}]
        for p in self.studio.projects.list()[:50]:
            out.append({"uri": f"f2o77://project/{p['id']}", "name": f"project-{p['id']}", "title": f"{p.get('kind')}: {p.get('name')}",
                        "mimeType": "application/json"})
        return out

    def read_resource(self, uri: str) -> dict:
        if uri == "f2o77://guide":
            return {"uri": uri, "mimeType": "text/markdown", "text": guide()}
        if uri == "f2o77://projects":
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(self.studio.projects.list(), default=str)}
        if uri == "f2o77://servers":
            srv = [{k: s[k] for k in ("id", "label", "host", "port", "user", "names", "servers_root")}
                   for s in self.studio.settings.get()["servers"]]
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(srv)}
        m = re.match(r"^f2o77://project/([a-f0-9]{12})$", uri)
        if m:
            p = self.studio.projects.get(m.group(1))
            if p.kind == "world":
                d = self.studio.world_state(p.id)
            elif p.kind == "map":
                d = self.studio.map_state(p.id)
                d["look"] = bool(d.get("look"))
            else:
                d = dict(p.summary(), profile=p.raw_profile(), builds=p.data.get("builds", [])[-5:])
            return {"uri": uri, "mimeType": "application/json", "text": json.dumps(d, default=str)}
        raise KeyError(f"unknown resource {uri}")

    # ---- calls
    def _resolve_project(self, want: str) -> str:
        want = str(want).strip()
        for p in self.studio.projects.list():
            names = {p["id"], (p.get("name") or "").lower(), ((p.get("map") or {}).get("name") or "").lower(),
                     ((p.get("car") or {}).get("name") or "").lower(), ((p.get("world") or {}).get("name") or "").lower()}
            if want in names or want.lower() in names:
                return p["id"]
        raise ToolError(f"no project '{want}' (list_projects)")

    def call(self, s: McpSession, rid, name, args) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool {name}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        args = dict(args)
        cs = _CallSession(s, self.settings()["call_seconds"])
        rec = {"id": uuid.uuid4().hex[:10], "tool": name, "args": _short(args), "started": time.time(), "_ctx": cs}
        key = json.dumps(rid)
        with s.lock:
            s.active[key] = rec
        s.publish("call", call={k: v for k, v in rec.items() if k != "_ctx"})
        is_error = False
        try:
            want = args.pop("project", None) if name in self.project_tools else None
            if want:
                cs.switch_project(self._resolve_project(want))
            if tool.risk == "deploy" and name not in SELF_GATED and self.settings()["approval"] == "studio":
                ok, note = self._approve(s, name, args, cs)
                if not ok:
                    raise ToolError("the user declined this in the Studio" + (f": {note}" if note else ""))
            out = tool.fn(Context(self.studio, cs), args)
            if isinstance(out, tuple):
                out, is_error = out
            content = _content(out)
        except StillRunning as e:
            j = e.job
            content = [{"type": "text", "text": json.dumps({
                "status": "running", "job": j.id, "title": j.title, "progress": j.progress, "step": j.meta.get("step"),
                "next": "call wait_job with this job id; the work goes on in the Studio"}, indent=1)}]
        except ToolError as e:
            content, is_error = [{"type": "text", "text": f"error: {e}"}], True
        except Exception as e:  # noqa: BLE001 - reported to the agent
            content, is_error = [{"type": "text", "text": f"error: {e.__class__.__name__}: {e}"}], True
        finally:
            with s.lock:
                s.active.pop(key, None)
        rec.pop("_ctx", None)
        rec.update(seconds=round(time.time() - rec["started"], 2), error=is_error,
                   text=" ".join(c.get("text", "") for c in content if c["type"] == "text")[:600],
                   images=sum(1 for c in content if c["type"] == "image"))
        with s.lock:
            s.calls.append(rec)
        s.publish("result", call=rec)
        self.last_activity = time.time()
        return {"content": content, "isError": is_error}

    # ---- approvals in the Studio (mcp.approval = studio)
    def _approve(self, s, name, args, cs, timeout=900):
        aid = uuid.uuid4().hex[:12]
        ev = threading.Event()
        item = {"id": aid, "session": s.id[:12], "label": s.label, "tool": name, "args": _short(args), "time": time.time(),
                "event": ev, "approved": False, "note": None}
        with self.lock:
            self.pending[aid] = item
        s.publish("approval", approval={k: v for k, v in item.items() if k != "event"})
        t0 = time.time()
        try:
            while not ev.wait(0.5):
                if cs.stopping or time.time() - t0 > timeout:
                    return False, "no answer"
        finally:
            with self.lock:
                self.pending.pop(aid, None)
            s.publish("approval_done", approval={"id": aid, "approved": item["approved"]})
        return item["approved"], item["note"]

    def decide(self, aid, approve, note=None) -> bool:
        with self.lock:
            item = self.pending.get(aid)
        if not item:
            raise ValueError("nothing is waiting for that approval")
        item["approved"], item["note"] = bool(approve), (str(note)[:500] if note else None)
        item["event"].set()
        return True

    def pending_view(self) -> list:
        with self.lock:
            return [{k: v for k, v in x.items() if k != "event"} for x in self.pending.values()]

    # ---- HTTP (Streamable HTTP, JSON answers)
    def http(self, method: str, headers, body: bytes) -> tuple:
        """-> (status, headers, body bytes)."""
        if not self.settings()["enabled"]:
            return 503, {}, b'{"error": "MCP is turned off in the Studio (Settings > Agents)"}'
        auth = headers.get("Authorization") or ""
        tok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not tok or not pysecrets.compare_digest(tok, self.token):
            return 401, {"WWW-Authenticate": 'Bearer realm="fivem2open77-studio"'}, b'{"error": "bad or missing token"}'
        sid = headers.get("Mcp-Session-Id")
        if method == "GET":
            return 405, {"Allow": "POST, DELETE"}, b""
        if method == "DELETE":
            return (200 if self.end(sid) else 404), {}, b""
        if method != "POST":
            return 405, {"Allow": "POST, DELETE"}, b""
        try:
            msg = json.loads(body or b"null")
        except ValueError:
            return 400, {}, json.dumps(_err(None, -32700, "parse error")).encode()
        first = msg[0] if isinstance(msg, list) and msg else msg
        is_init = isinstance(first, dict) and first.get("method") == "initialize"
        if sid and not is_init and self._session(sid) is None:
            return 404, {}, json.dumps(_err(first.get("id") if isinstance(first, dict) else None, -32001,
                                            "unknown or expired session: initialize again")).encode()
        answer, new_sid = self.handle(msg, None if is_init else sid)
        h = {}
        if new_sid:
            h["Mcp-Session-Id"] = new_sid
        if answer is None:
            return 202, h, b""
        from .jsonutil import dumps_bytes
        return 200, h, dumps_bytes(answer)


# ----------------------------------------------------------------------------- helpers
def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _content(blocks) -> list:
    out = []
    for b in blocks or []:
        if b.get("type") == "image":
            data = b.get("png") or b.get("data")
            out.append({"type": "image", "data": data if isinstance(data, str) else base64.b64encode(data).decode(),
                        "mimeType": b.get("mimeType") or "image/png"})
        else:
            out.append({"type": "text", "text": str(b.get("text", ""))})
    return out or [{"type": "text", "text": "(no output)"}]


def _short(v, depth=0):
    if isinstance(v, dict):
        return {k: _short(x, depth + 1) for k, x in list(v.items())[:20]}
    if isinstance(v, list):
        return [_short(x, depth + 1) for x in v[:10]] + (["..."] if len(v) > 10 else [])
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + "..."
    return v


def _compact(result, limit=6000):
    s = json.dumps(result, default=str)
    if len(s) <= limit:
        return result
    if isinstance(result, dict):
        return {k: (v if len(json.dumps(v, default=str)) < 800 else "(long: see the Studio)") for k, v in result.items()}
    return s[:limit] + "..."


def _app_version() -> str:
    from .app import APP_VERSION
    return APP_VERSION
