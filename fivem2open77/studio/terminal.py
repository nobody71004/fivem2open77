"""The Studio's terminal: one command line for everything the Studio does.

Every Studio tool is a command (``build_car``, ``deploy_car prod``, ``wait_job 1a2b3c4d5e6f``); the
built-ins list and follow jobs, drive the Studio's own AI agent (``agent start ...``), call any API
endpoint (``api GET /api/qc``), run the converter's command line (``cli sounds``), show a page of
the Studio's window (``ui quality``) and run a system command (``sh dir`` or ``!dir``: always for
the user, for agents only when Settings > Agents allows it).  The Terminal page, the ``terminal``
MCP tool and ``python -m fivem2open77 term`` all run their lines here, and every line a run prints
goes out on the event bus: what an agent types shows up in the Studio as it runs.

Arguments: ``key=value`` (the value read as JSON when it is JSON: numbers, true/false, [..], {..}),
``--key value``, ``--flag``, a JSON object, or the tool's required arguments in order
(``deploy_car prod``).  ``a.b=1`` builds nested objects (``update_profile patch.sound.engine=x``).
Quotes group words (``"C:\\My Cars\\urus.zip"``); a backslash is a path separator here, never an
escape.  ``&&`` runs the next command when the one before succeeded, ``;`` runs it anyway.
"""
from __future__ import annotations

import base64
import difflib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque

from .agent.tools import Context, StillRunning, ToolError

HEX12 = re.compile(r"^[a-f0-9]{12}$")
MCP_ONLY = ("terminal", "terminal_output")            # the MCP side of this terminal, not commands in it
AGENT_SETTLED = ("waiting_user", "waiting_approval", "finished", "stopped", "failed")
CLI_REFUSED = ("studio", "mcp", "term")               # these start a server or a prompt of their own
SHELLS = ("auto", "powershell", "cmd", "bash", "sh")

PAGES = OrderedDict([
    ("home", "#/home"), ("cars", "#/projects"), ("maps", "#/maps"), ("world", "#/world"), ("live", "#/live"),
    ("servers", "#/servers"), ("quality", "#/quality"), ("agent", "#/agent"), ("activity", "#/jobs"),
    ("terminal", "#/terminal"), ("settings", "#/settings"), ("agents", "#/settings/agents"), ("ai", "#/settings/ai"),
    ("tools", "#/settings"),
])
TABS = {"car": ("overview", "mapping", "handling", "sound", "build", "deploy", "agent"),
        "map": ("overview", "place", "options", "build", "deploy", "agent")}

# Settings an agent may not change: who approves what, which programs the Studio runs, the servers
# (they hold credentials), whether an install may unsecure a server's mods, and where the Claude key
# is sent.  The user changes them in the Studio.
PROTECTED_FOR_AGENTS = ("mcp", "paths", "terminal", "servers", "deploy.allow_unsecured", "ai.anthropic_base_url")

# What agents may not call through the API (the terminal's `api`, the MCP studio_api tool), and why.
API_REFUSED = [
    (None, r"/api/events", "a live event stream cannot be called here"),
    (None, r"/api/secrets", "secrets are set by the user in the Studio"),
    (None, r"/api/mcp/token(/reset)?", "the MCP token is the user's"),
    (None, r"/api/mcp/approve", "an approval is the user's click in the Studio"),
    (None, r"/api/mcp/connect", "the user connects apps in Settings > Agents"),
    ("DELETE", r"/api/mcp/sessions/.*", "the user ends agent sessions in Settings > Agents"),
    (None, r"/api/terminal/(run|sessions.*|runs/.*|history)", "the user's terminal: use the terminal tool"),
    ("POST", r"/api/servers(/import)?", "the user sets up servers (the Servers page): they hold credentials"),
    ("DELETE", r"/api/servers/[a-z0-9_-]{1,32}", "the user removes servers (the Servers page)"),
    ("POST", r"/api/agent/sessions", "use agent_start"),
    ("POST", r"/api/agent/sessions/[a-f0-9]{12}/approve", "use agent_approve"),
    ("POST", r"/api/uploads", "uploads are not offered here: import from a path on this PC"),
]
API_DEPLOY = [
    ("POST", r"/api/projects/[a-f0-9]{12}/(deploy|rebuild_deploy)"),
    ("POST", r"/api/packs/[a-f0-9]{12}/deploy"),
    ("POST", r"/api/servers/[a-z0-9_-]{1,32}/cars"),
    ("POST", r"/api/servers/[a-z0-9_-]{1,32}/(restart|hub|agent-api)"),
    ("POST", r"/api/servers/[a-z0-9_-]{1,32}/world/remove"),
    ("POST", r"/api/servers/[a-z0-9_-]{1,32}/live/maps/.+"),
    ("POST", r"/api/fleet/upgrade"),
]


class TermError(Exception):
    """A command the terminal refuses or cannot run: its message is the answer."""


# ============================================================================ reading a line
class _Tok(str):
    quoted = False


def _tok(text, quoted=False):
    t = _Tok(text)
    t.quoted = quoted
    return t


def _json_end(s: str, i: int) -> int:
    """The index after the JSON object/array that starts at s[i] (or the end of s when it does not close)."""
    depth, n, in_str = 0, len(s), False
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def tokenize(line: str) -> list:
    """Words of a command line.  Quotes ("..." or '...') group words and keep their text as it is
    (no escapes: Windows paths); a JSON object or array -- alone or after key= -- is one word."""
    out, i, n = [], 0, len(line or "")
    s = line or ""
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        if s[i] in "{[":
            j = _json_end(s, i)
            out.append(_tok(s[i:j]))
            i = j
            continue
        buf, quoted = [], False
        while i < n and not s[i].isspace():
            c = s[i]
            if c in "\"'":
                q, i = c, i + 1
                start = i
                while i < n and s[i] != q:
                    i += 1
                buf.append(s[start:i])
                i += 1
                quoted = True
            elif c in "{[" and "".join(buf).endswith("="):
                j = _json_end(s, i)
                buf.append(s[i:j])
                i = j
            else:
                buf.append(c)
                i += 1
        out.append(_tok("".join(buf), quoted))
    return out


def split_chain(line: str) -> list:
    """[(command, joiner)] for `a && b ; c`: joiner says when the NEXT command runs ('&&': if this one
    succeeded, ';': anyway, None: last).  A system command (`sh ...`, `!...`) takes the rest of the line
    as it is: its shell reads its own && and ;."""
    out, s, i, n, start = [], line or "", 0, len(line or ""), 0

    def shell_from(pos):
        rest = s[pos:].lstrip()
        return rest.startswith("!") or re.match(r"^sh(\s|$)", rest) is not None

    if shell_from(0):
        return [(s.strip(), None)]
    in_q, depth = None, 0
    while i < n:
        c = s[i]
        if in_q:
            if c == in_q:
                in_q = None
        elif c in "\"'":
            in_q = c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth = max(0, depth - 1)
        elif depth == 0 and (s.startswith("&&", i) or c == ";"):
            joiner = "&&" if c == "&" else ";"
            out.append((s[start:i].strip(), joiner))
            i += 2 if joiner == "&&" else 1
            start = i
            if shell_from(i):
                out.append((s[i:].strip(), None))
                return [x for x in out if x[0]]
            continue
        i += 1
    out.append((s[start:].strip(), None))
    return [x for x in out if x[0]]


def _set_path(args: dict, key: str, value):
    parts = [p for p in key.split(".") if p]
    if not parts:
        raise TermError(f"bad argument name '{key}'")
    cur = args
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    cur[parts[-1]] = value


def _types(prop) -> tuple:
    t = (prop or {}).get("type")
    if isinstance(t, list):
        return tuple(t)
    return (t,) if t else ()


def read_value(text: str, quoted=False, prop=None):
    """A word as the argument's value: by the schema's type when it has one, else JSON when it reads as
    JSON, else the text."""
    types = _types(prop)
    if quoted:
        if "string" in types or not types:
            return str(text)
    t = str(text)
    if "boolean" in types and not ("string" in types):
        low = t.lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise TermError(f"'{t}' is not true or false")
    if ("integer" in types or "number" in types) and "string" not in types:
        try:
            v = float(t)
        except ValueError as e:
            raise TermError(f"'{t}' is not a number") from e
        return int(v) if "integer" in types and v.is_integer() else v
    if "array" in types and "string" not in types:
        if t.startswith("["):
            try:
                v = json.loads(t)
            except ValueError as e:
                raise TermError(f"bad list {t}: {e}") from e
            if not isinstance(v, list):
                raise TermError(f"'{t}' is not a list")
            return v
        items = (prop or {}).get("items") or {}
        return [read_value(x.strip(), False, items) for x in t.split(",") if x.strip()]
    if "object" in types:
        try:
            v = json.loads(t)
        except ValueError as e:
            raise TermError(f"bad JSON object: {e}") from e
        if not isinstance(v, dict):
            raise TermError(f"'{t}' is not a JSON object")
        return v
    if "string" in types:
        return t
    if t[:1] in "{[\"" or t in ("true", "false", "null") or re.match(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$", t):
        try:
            return json.loads(t)
        except ValueError:
            return t
    return t


def parse_args(schema: dict, words: list, name: str = "") -> dict:
    """A tool's arguments from the words after its name."""
    props = (schema or {}).get("properties") or {}
    required = list((schema or {}).get("required") or [])
    args, positional = {}, []
    i = 0
    while i < len(words):
        w = words[i]
        quoted = getattr(w, "quoted", False)
        if not quoted and w.startswith("{"):
            try:
                obj = json.loads(w)
            except ValueError as e:
                raise TermError(f"bad JSON arguments: {e}") from e
            if not isinstance(obj, dict):
                raise TermError("JSON arguments must be an object")
            args.update(obj)
        elif not quoted and w.startswith("--") and len(w) > 2:
            key, eq, val = w[2:].partition("=")
            key = key.replace("-", "_")
            prop = props.get(key.split(".")[0]) if "." not in key else None
            if eq:
                _set_path(args, key, read_value(val, False, prop))
            elif _types(prop) == ("boolean",) or i + 1 >= len(words) or (
                    words[i + 1].startswith("--") and not getattr(words[i + 1], "quoted", False)):
                _set_path(args, key, True)
            else:
                nxt = words[i + 1]
                _set_path(args, key, read_value(nxt, getattr(nxt, "quoted", False), prop))
                i += 1
        elif not quoted and re.match(r"^[A-Za-z_][\w.\-]*=", w):
            key, _, val = w.partition("=")
            key = key.replace("-", "_")
            prop = props.get(key) if "." not in key else None
            _set_path(args, key, read_value(val, False, prop))
        else:
            positional.append(w)
        i += 1
    if positional:
        slots = [k for k in required if k not in args]
        if not slots and len(props) == 1:
            slots = [k for k in props if k not in args]
        for k in slots:
            if not positional:
                break
            prop = props.get(k) or {}
            if "array" in _types(prop) and k == slots[-1]:
                items = prop.get("items") or {}
                vals = []
                for w in positional:
                    v = read_value(w, getattr(w, "quoted", False), {"type": "array", "items": items})
                    vals.extend(v if isinstance(v, list) else [v])
                args[k] = vals
                positional = []
                break
            w = positional.pop(0)
            args[k] = read_value(w, getattr(w, "quoted", False), prop)
        if positional:
            raise TermError(f"{name}: too many words ({' '.join(positional)}): name the arguments, key=value"
                            + (f" ({', '.join(props)})" if props else ""))
    if (schema or {}).get("additionalProperties") is False and props:
        unknown = [k for k in args if k not in props]
        if unknown:
            raise TermError(f"{name}: no argument {', '.join(unknown)} (it takes: {', '.join(props)})")
    missing = [k for k in required if k not in args]
    if missing:
        raise TermError(f"{name}: needs {', '.join(missing)} -- {usage(name, schema)}")
    return args


def usage(name: str, schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    req = list((schema or {}).get("required") or [])
    parts = [name] + [f"<{k}>" for k in req]
    for k, p in props.items():
        if k in req:
            continue
        enum = p.get("enum")
        t = "|".join(map(str, enum)) if enum else "|".join(t for t in _types(p) if t) or "value"
        parts.append(f"[{k}={t}]")
    return " ".join(parts)


def tool_group(name: str) -> str:
    from .agent.tools import CAR_TOOLS
    from .agent.maptools import CITY_TOOLS, LIVE_TOOLS, MAP_TOOLS
    if name.startswith("agent_"):
        return "AI agent"
    if name in ("studio_api", "studio_api_routes", "studio_ui") + MCP_ONLY:
        return "Studio app"
    if name in CAR_TOOLS or name == "list_engine_sounds":
        return "Cars"
    if name in MAP_TOOLS:
        return "Maps"
    if name.startswith("ncwe_"):
        return "NCWE editor"
    if name in LIVE_TOOLS:
        return "Live maps"
    if name.startswith("world_") or name.endswith("world_edit") or name.endswith("world_edit_in_game"):
        return "World edits"
    if name in CITY_TOOLS:
        return "City map"
    if name in ("list_servers", "server_status", "read_server_log", "restart_server", "enable_agent_api", "deploy_all_cars"):
        return "Servers"
    if name in ("qc_report", "run_qc"):
        return "Quality"
    return "Studio"


GROUP_ORDER = ("Studio", "Cars", "Maps", "Live maps", "World edits", "Servers", "Quality", "City map", "AI agent",
               "Studio app", "NCWE editor")


def _first_sentence(text: str, limit=110) -> str:
    t = " ".join(str(text or "").split())
    m = re.match(r"^(.+?[.:;])(\s|$)", t)
    t = m.group(1).rstrip(".:;") if m else t
    return t if len(t) <= limit else t[: limit - 1] + "\u2026"


def table_text(cols: list, rows: list) -> str:
    cells = [[str(c) for c in cols]] + [["" if v is None else str(v) for v in r] for r in rows]
    widths = [min(60, max(len(r[i]) for r in cells)) for i in range(len(cols))]
    lines = []
    for n, r in enumerate(cells):
        lines.append("  ".join((v if len(v) <= widths[i] else v[: widths[i] - 1] + "\u2026").ljust(widths[i])
                               for i, v in enumerate(r)).rstrip())
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def flat_keys(d, prefix="") -> list:
    out = []
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.extend(flat_keys(v, key))
        else:
            out.append(key)
    return out


def settings_guard(patch: dict):
    """An agent's settings change: refused when it touches what only the user decides."""
    for key in flat_keys(patch if isinstance(patch, dict) else {}):
        for p in PROTECTED_FOR_AGENTS:
            if key == p or key.startswith(p + "."):
                raise TermError(f"{key}: only you can change this, in the Studio's Settings")


def api_guard(method: str, path: str, body) -> str:
    """An agent's API call: refused (TermError) or its risk (read, change, deploy)."""
    m = method.upper()
    for want, rx, why in API_REFUSED:
        if (want is None or want == m) and re.match("^" + rx + "$", path):
            raise TermError(f"{m} {path}: not for agents ({why})")
    if m == "PATCH" and path == "/api/settings":
        settings_guard(body or {})
    if m in ("GET", "HEAD"):
        return "read"
    for want, rx in API_DEPLOY:
        if want == m and re.match("^" + rx + "$", path):
            return "deploy"
    return "change"


def api_path(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        raise TermError("api: which endpoint? (api GET /api/state; studio_api_routes lists them)")
    if p.startswith("http://") or p.startswith("https://"):
        raise TermError("api: a Studio endpoint path, like /api/qc")
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith("/api/"):
        p = "/api" + p
    return p


# ============================================================================ the AI agent, driven
def agents_of(studio):
    if getattr(studio, "agents", None) is None:
        raise TermError("the Studio's AI agent is not available")
    return studio.agents


def agent_session(studio, sid):
    m = agents_of(studio)
    sid = str(sid or "").strip()
    s = m.sessions.get(sid)
    if s is None:
        hits = [k for k in m.sessions if k.startswith(sid)] if sid else []
        if len(hits) == 1:
            s = m.sessions[hits[0]]
    if s is None:
        raise TermError(f"no agent session {sid} (agent lists them)" if sid else "no agent session: agent start <goal>")
    return s


def agent_autonomy(studio, wanted, origin) -> str:
    st = studio.settings.get()
    a = wanted or st["ai"]["autonomy"]
    if a not in ("ask", "auto_safe", "full"):
        raise TermError("autonomy: ask, auto_safe or full")
    if origin == "mcp" and a == "full" and st["mcp"]["approval"] == "studio":
        a = "auto_safe"            # installs and restarts still wait for the user's click (Settings > Agents)
    return a


def agent_event(ev: dict):
    """(kind, text, extra) of one agent event, for a terminal line."""
    k = ev.get("kind")
    t = str(ev.get("text") or "")
    if k == "user":
        return ("agent", f"you \u203a {t}", {"role": "user"}) if t else None
    if k == "assistant":
        return ("agent", t, {"role": "assistant"})
    if k == "tool_call":
        args = json.dumps(ev.get("input") or {}, default=str)
        return ("info", f"\u2192 {ev.get('tool')} {args if args != '{}' else ''}".rstrip()[:400], {"role": "tool"})
    if k == "tool_result":
        head = " ".join(t.split())[:300]
        extra = f" (+{ev['images']} image)" if ev.get("images") else ""
        return ("err" if ev.get("error") else "ok", f"\u2190 {ev.get('tool')}: {head}{extra}", {"role": "tool"})
    if k == "approval":
        return ("warn", f"waiting for your OK: {ev.get('tool')} ({ev.get('risk')}) "
                        f"{json.dumps(ev.get('input') or {}, default=str)[:300]}  -- agent approve | agent decline",
                {"role": "approval"})
    if k == "approval_result":
        return ("info", f"{'allowed' if ev.get('approved') else 'declined'}" + (f": {ev['note']}" if ev.get("note") else ""), {})
    if k == "question":
        return ("warn", f"the agent asks: {t}  -- agent say <answer>", {"role": "question"})
    if k == "finished":
        return ("ok", f"done: {t}", {"role": "finished"})
    if k == "error":
        return ("err", t, {"role": "error"})
    if k in ("note", "job"):
        return ("info", t or f"job {ev.get('job')}: {ev.get('title')}", {})
    return None


def agent_follow(s, since: int, emit, stop=lambda: False, timeout=None) -> int:
    """Emit a session's events from `since` until it waits for someone (a question, an approval, its turn
    ended) or stops, `stop()` says so, or `timeout` passes; the index of the next event.

    It reads the session's own status events, not its status: an answer posted to a question is seen by
    the agent's thread a moment later, and until then the status still says it waits."""
    t0 = time.time()
    i = since
    last = None                                   # the latest status event after `since`
    quiet_at = time.time()
    while True:
        evs = s.events[i:]
        for ev in evs:
            emit(ev)
            if ev.get("kind") == "status":
                last = ev.get("status")
        if evs:
            quiet_at = time.time()
        i += len(evs)
        if last in ("waiting_user", "waiting_approval"):
            break
        if last in ("finished", "stopped", "failed") and not s.running:
            break
        if last is None and time.time() - quiet_at > 1.5 and s.status in AGENT_SETTLED \
                and (not s.running or s.status in ("waiting_user", "waiting_approval")):
            break                                 # it waited already, and nothing happened since
        if stop() or (timeout is not None and time.time() - t0 > timeout):
            break
        time.sleep(0.2)
    time.sleep(0.3)                               # the question / approval event follows its status event
    evs = s.events[i:]
    for ev in evs:
        emit(ev)
    return i + len(evs)


def agent_compact(view: dict) -> dict:
    """An agent session's view for an agent: what it said and did, briefly."""
    out = {k: view.get(k) for k in ("id", "project", "goal", "status", "steps", "summary", "provider", "model", "autonomy")}
    out["pending"] = view.get("pending")
    out["events"] = []
    for ev in view.get("events") or []:
        r = agent_event(ev)
        if r:
            out["events"].append(r[1])
    out["next_event"] = view.get("total_events")
    st = view.get("status")
    out["next"] = ({"waiting_approval": "agent_approve (approve true/false), or agent_stop",
                    "waiting_user": "agent_send with your answer or the next instruction",
                    "finished": "agent_send to go on, or start a new session",
                    "running": "agent_status with seconds to wait for more"}.get(st))
    return out


# ============================================================================ runs and sessions
class Run:
    """One command line being run: its output lines, its state, how to stop it."""
    MAX_LINES = 6000

    def __init__(self, term, session, line):
        self.id = uuid.uuid4().hex[:12]
        self.term = term
        self.session = session
        self.line = line
        self.state = "running"
        self.exit = None
        self.lines = []
        self.started = time.time()
        self.finished = None
        self.stopping = False
        self.on_cancel = []
        self.done = threading.Event()
        self.followed = set()
        self.followers = []
        self.count = 0
        self.lock = threading.Lock()

    # ---- output
    def out(self, kind, text="", **extra):
        entry = {"k": kind, "t": str(text if text is not None else "")}
        entry.update({k: v for k, v in extra.items() if v is not None})
        self._add(entry)
        return entry

    def text(self, text, kind="out"):
        """Several lines of text as one entry each (a JSON answer as one entry, pretty)."""
        t = str(text if text is not None else "")
        s = t.strip()
        if s[:1] in "{[":
            try:
                obj = json.loads(s)
            except ValueError:
                obj = None
            if obj is not None:
                return self.out("json" if kind == "out" else kind, json.dumps(obj, indent=2, ensure_ascii=False, default=str))
        for line in t.splitlines() or [""]:
            self.out(kind, line)
        return None

    def table(self, cols, rows, empty="(none)"):
        if not rows:
            return self.out("info", empty)
        return self.out("table", table_text(cols, rows), cols=list(cols), rows=[list(r) for r in rows])

    def image(self, data_b64: str, mime="image/png", caption=""):
        return self.out("img", f"[image{': ' + caption if caption else ''}]", data=data_b64, mime=mime)

    def job_line(self, job):
        p = job.progress
        pct = f" {round(p * 100)}%" if isinstance(p, (int, float)) and job.state == "running" else ""
        step = job.meta.get("step")
        stopping = job.state in ("queued", "running") and (job.cancel_event.is_set() or job.cancel_late is not None)
        t = (f"job {job.id} {job.title}: {job.state}{pct}" + (" (stopping)" if stopping else "")
             + (f" -- {step}" if step and job.state == "running" else ""))
        if job.state == "failed" and job.error:
            t += f" -- {job.error}"
        self._add({"k": "job", "t": t, "job": job.id, "title": job.title, "state": job.state, "p": p, "step": step,
                   "key": "job:" + job.id})

    def _add(self, entry):
        """A line of output.  A keyed line (a job's progress) replaces the one before it with that key and
        moves to the end, so whoever reads from a line number on sees the latest."""
        with self.lock:
            key = entry.get("key")
            if key:
                for n in range(len(self.lines) - 1, max(-1, len(self.lines) - 400), -1):
                    if self.lines[n].get("key") == key:
                        del self.lines[n]
                        break
            entry["n"] = self.count
            self.count += 1
            self.lines.append(entry)
            if len(self.lines) > self.MAX_LINES:
                del self.lines[: len(self.lines) - self.MAX_LINES]
        self.session.record(self, entry)

    # ---- jobs
    def follow(self, job):
        """Print a job's log and progress here while it runs."""
        with self.lock:
            if job.id in self.followed:
                return
            self.followed.add(job.id)
        t = threading.Thread(target=self._follow, args=(job,), daemon=True, name=f"term-follow-{job.id}")
        self.followers.append((job, t))
        t.start()

    def settle(self, timeout=3.0):
        """Let the followers of the jobs that ended print their last lines (before the answer below them)."""
        t0 = time.time()
        for job, t in list(self.followers):
            if job.state not in ("queued", "running"):
                t.join(max(0.0, timeout - (time.time() - t0)))

    def _follow(self, job):
        i, last = 0, None
        while True:
            n = len(job.lines)
            if i > n:
                i = n
            for entry in job.lines[i:n]:
                self.out("log", entry.get("text", ""), job=job.id)
            i = n
            sig = (job.state, job.progress, job.meta.get("step"), job.error, job.cancel_event.is_set(), job.cancel_late)
            if sig != last:
                self.job_line(job)
                last = sig
            if job.state not in ("queued", "running") and i >= len(job.lines):
                break
            if self.done.is_set() and time.time() - (self.finished or 0) > 2:
                break                                   # the run ended (wait_job gave up waiting): stop here
            time.sleep(0.3)

    def view(self, since=0) -> dict:
        with self.lock:
            lines = [dict(x) for x in self.lines if x["n"] >= since]
        return {"run": self.id, "session": self.session.id, "line": self.line, "state": self.state, "exit": self.exit,
                "started": self.started, "finished": self.finished, "lines": lines, "next": self.count}


class TermSession:
    MAX_SCROLLBACK = 4000

    def __init__(self, term, sid, origin="ui", label="You", mcp=None):
        self.term = term
        self.id = sid
        self.origin = origin           # ui: the user at the Studio; mcp: an agent (gated)
        self.label = label
        self.mcp = mcp                 # the McpSession of an agent's terminal (shares its project)
        self._project = None
        self.cwd = term.studio.workspace
        self.agent = None              # the AI agent session this terminal follows
        self.scroll = deque(maxlen=self.MAX_SCROLLBACK)
        self.seq = 0
        self.lock = threading.Lock()
        self.created = self.seen = time.time()
        self.active = OrderedDict()    # run id -> Run while it runs

    @property
    def project(self):
        return self.mcp.project if self.mcp is not None else self._project

    @project.setter
    def project(self, pid):
        if self.mcp is not None:
            self.mcp.project = pid
        else:
            self._project = pid

    def project_info(self):
        pid = self.project
        if not pid:
            return None
        try:
            p = self.term.studio.projects.get(pid)
            return {"id": p.id, "kind": p.kind, "name": p.data.get("name") or p.id}
        except Exception:  # noqa: BLE001 - a deleted project
            return {"id": pid, "kind": None, "name": pid}

    def record(self, run, entry):
        with self.lock:
            self.seq += 1
            item = dict(entry, run=run.id, s=self.seq)
            if entry.get("key"):
                for n in range(len(self.scroll) - 1, max(-1, len(self.scroll) - 400), -1):
                    if self.scroll[n].get("key") == entry["key"] and self.scroll[n].get("run") == run.id:
                        del self.scroll[n]
                        break
            if entry.get("k") == "img" and len([x for x in self.scroll if x.get("k") == "img"]) > 24:
                for x in self.scroll:          # old images become their caption in the scrollback
                    if x.get("k") == "img" and x.get("data"):
                        x.pop("data", None)
                        x["k"] = "info"
                        break
            self.scroll.append(item)
        self.term.studio.bus.publish({"type": "term", "session": self.id, "origin": self.origin, "label": self.label,
                                     "run": run.id, "line": item})

    def info(self) -> dict:
        return {"id": self.id, "origin": self.origin, "label": self.label, "project": self.project_info(),
                "cwd": self.cwd, "agent": self.agent, "running": list(self.active), "seq": self.seq}


class _TermCall:
    """The agent-tool session interface (tools.Context) for one tool command of a run."""
    vision = True
    soft_timeout = None

    def __init__(self, run):
        self.run = run
        self.last_change_error = None
        self.finish_warned = False

    @property
    def stopping(self):
        return self.run.stopping

    @property
    def project(self):
        return self.run.session.project

    def switch_project(self, pid):
        self.run.term.studio.projects.get(pid)
        self.run.session.project = pid
        info = self.run.session.project_info() or {}
        self.run.out("info", f"project: {info.get('name')} ({info.get('kind')}, {pid})", project=info)

    def note(self, t):
        self.run.out("info", str(t)[:2000])

    def attach_job(self, job):
        self.run.follow(job)

    def ask_user(self, question):
        raise ToolError("the terminal has no one to ask: run the next command yourself")

    def finish(self, summary):
        pass


class _TermContext(Context):
    def wait_job(self, job, timeout=3600):
        self.session.run.follow(job)
        return super().wait_job(job, timeout)


# ============================================================================ the terminal
class Terminal:
    MAX_RUNS = 300
    HISTORY = 1000

    def __init__(self, studio):
        self.studio = studio
        self.sessions = {}
        self.runs = OrderedDict()
        self.lock = threading.Lock()
        self.hist_path = os.path.join(studio.workspace, "terminal", "history.txt")
        self.history = self._load_history()
        self.builtins = self._builtins()

    # ---- sessions
    def session(self, sid=None, *, origin="ui", label="You", mcp=None) -> TermSession:
        sid = (sid or "").strip().lower()
        with self.lock:
            s = self.sessions.get(sid) if sid else None
            if s is None:
                if not HEX12.match(sid):
                    sid = uuid.uuid4().hex[:12]
                s = self.sessions[sid] = TermSession(self, sid, origin, label, mcp)
            s.seen = time.time()
            return s

    def session_for_mcp(self, mcp_session) -> TermSession:
        sid = mcp_session.id[:12]
        with self.lock:
            s = self.sessions.get(sid)
            if s is None or s.mcp is not mcp_session:
                s = self.sessions[sid] = TermSession(self, sid, "mcp", mcp_session.label, mcp_session)
            s.seen = time.time()
            return s

    def scrollback(self, sid, since=0, limit=600) -> dict:
        s = self.sessions.get(sid)
        if s is None:
            s = self.session(sid)
        with s.lock:
            items = [dict(x) for x in s.scroll if x["s"] > since]
        if limit and len(items) > limit:
            items = items[-limit:]
        return dict(s.info(), lines=items)

    # ---- runs
    def run(self, session: TermSession, line: str) -> Run:
        line = str(line or "").replace("\r", "")
        if len(line) > 20000:
            raise TermError("that command line is too long")
        r = Run(self, session, line.strip())
        with self.lock:
            self.runs[r.id] = r
            while len(self.runs) > self.MAX_RUNS:
                old_id, old = next(iter(self.runs.items()))
                if old.state == "running":
                    break
                self.runs.pop(old_id)
        session.active[r.id] = r
        if session.origin == "ui" and r.line:
            self._remember(r.line)
        threading.Thread(target=self._main, args=(r,), daemon=True, name=f"term-{r.id}").start()
        return r

    def get_run(self, rid) -> Run:
        r = self.runs.get(rid)
        if r is None:
            raise TermError(f"no run {rid}")
        return r

    def run_view(self, rid, since=0) -> dict:
        return self.get_run(rid).view(since)

    def cancel(self, rid) -> bool:
        r = self.runs.get(rid)
        if r is None or r.state != "running":
            return False
        r.stopping = True
        for fn in list(r.on_cancel):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        r.out("warn", "^C")
        return True

    def _main(self, run: Run):
        s = run.session
        info = s.project_info()
        run.out("in", run.line, prompt=(info or {}).get("name"), cwd=s.cwd)
        ok = True
        try:
            skip = False
            for cmd, joiner in split_chain(run.line):
                if run.stopping:
                    break
                if not skip:
                    ok = self._one(run, cmd)
                    skip = joiner == "&&" and not ok
                else:
                    skip = joiner == "&&"           # `a && b ; c`: a failed a skips b, and c runs
        except Exception as e:  # noqa: BLE001 - a bug in a command: said, not raised
            run.out("err", f"error: {e.__class__.__name__}: {e}")
            ok = False
        finally:
            run.settle()
            run.finished = time.time()
            run.state = "cancelled" if run.stopping else ("done" if ok else "failed")
            run.exit = 0 if ok and not run.stopping else (130 if run.stopping else 1)
            s.active.pop(run.id, None)
            run.out("ctl", "", action="end", state=run.state, exit=run.exit,
                    seconds=round(run.finished - run.started, 2), project=s.project_info(), cwd=s.cwd)
            run.done.set()

    def _one(self, run: Run, line: str) -> bool:
        """One command; False when it failed."""
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        try:
            if line.startswith("!"):
                return self._shell(run, line[1:].strip())
            words = tokenize(line)
            if not words:
                return True
            name = words[0]
            rest = words[1:]
            raw = re.sub(r"^\S+\s*", "", line, count=1)
            b = self.builtins.get(name.lower())
            if b is not None:
                r = b["fn"](run, rest, raw)
                return r is not False
            tools = self.studio.mcp.tools
            tname = name if name in tools else name.replace("-", "_")
            if tname in tools and tname not in MCP_ONLY:
                return self._tool(run, tname, rest)
            raise TermError(self._unknown(name))
        except TermError as e:
            run.out("err", str(e))
            return False
        except ToolError as e:
            run.out("err", f"error: {e}")
            return False

    def _unknown(self, name):
        names = list(self.builtins) + [t for t in self.studio.mcp.tools if t not in MCP_ONLY]
        near = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
        return f"unknown command: {name}" + (f" (did you mean {', '.join(near)}?)" if near else " (help lists the commands)")

    # ---- tools
    def _tool(self, run: Run, name: str, words: list) -> bool:
        hub = self.studio.mcp
        tool = hub.tools[name]
        if words and words[0] in ("-h", "--help", "help", "?"):
            self._help_tool(run, name)
            return True
        args = parse_args(tool.schema if name not in hub.project_tools else
                          dict(tool.schema, properties=dict((tool.schema or {}).get("properties") or {},
                                                            project={"type": "string"})),
                          words, name)
        cs = _TermCall(run)
        want = args.pop("project", None) if name in hub.project_tools else None
        if want:
            cs.switch_project(hub._resolve_project(want))
        self._gate(run, tool.risk, name, args, cs)
        t0 = time.time()
        try:
            out = tool.fn(_TermContext(self.studio, cs), args)
            is_error = False
            if isinstance(out, tuple):
                out, is_error = out
        except StillRunning as e:
            run.follow(e.job)
            out, is_error = [{"type": "text", "text": f"still running: wait {e.job.id}"}], False
        except ToolError as e:
            run.settle()
            run.out("err", f"error: {e}")
            return False
        run.settle()
        self.render(run, out, error=is_error)
        if not is_error and time.time() - t0 > 3:
            run.out("ok", f"{name}: done in {time.time() - t0:.1f} s")
        return not is_error

    def _gate(self, run: Run, risk: str, what: str, args, cs):
        """An agent's change to a server waits for the user's click when Settings > Agents says so."""
        s = run.session
        if s.origin != "mcp" or risk != "deploy":
            return
        hub = self.studio.mcp
        if hub.settings()["approval"] != "studio":
            return
        run.out("warn", f"waiting for your OK in the Studio (Settings > Agents): {what}")
        ok, note = hub._approve(s.mcp, what, args, cs)
        if not ok:
            raise TermError("the user declined this in the Studio" + (f": {note}" if note else ""))
        run.out("ok", "allowed")

    def render(self, run: Run, content, error=False):
        for b in content or []:
            if b.get("type") == "image":
                data = b.get("png") or b.get("data")
                if isinstance(data, (bytes, bytearray)):
                    data = base64.b64encode(data).decode("ascii")
                run.image(data, b.get("mimeType") or "image/png", b.get("caption") or "")
            else:
                run.text(b.get("text", ""), "err" if error else "out")

    # ---- system commands
    def _shell_cmd(self, line: str) -> list:
        want = self.studio.settings.get().get("terminal", {}).get("shell", "auto")
        if want == "auto":
            want = "powershell" if os.name == "nt" else "sh"
        if want == "powershell":
            return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; "
                    + line]
        if want == "cmd":
            return ["cmd.exe", "/d", "/s", "/c", "chcp 65001>nul & " + line]
        if want == "bash":
            return ["bash", "-c", line]
        return ["/bin/sh", "-c", line]

    def _shell(self, run: Run, line: str) -> bool:
        if not line:
            raise TermError("sh: which command? (sh dir, or !dir)")
        if run.session.origin == "mcp" and not self.studio.settings.get()["mcp"].get("shell"):
            raise TermError("system commands are off for agents: the user turns them on in Settings > Agents "
                            "(\u201cLet agents run system commands\u201d)")
        return self._proc(run, self._shell_cmd(line), cwd=run.session.cwd)

    def _proc(self, run: Run, cmd: list, cwd=None, env=None) -> bool:
        e = dict(os.environ)
        e.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "F2O77_WORKSPACE": self.studio.workspace})
        e.update(env or {})
        kw = {"cwd": cwd or self.studio.workspace, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "env": e}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            p = subprocess.Popen(cmd, **kw)
        except OSError as ex:
            raise TermError(f"could not start {cmd[0]}: {ex}") from ex

        def kill():
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=20)
                else:
                    os.killpg(p.pid, signal.SIGTERM)
            except (OSError, subprocess.SubprocessError):
                try:
                    p.kill()
                except OSError:
                    pass
        run.on_cancel.append(kill)

        def pump(stream, kind):
            for raw in iter(stream.readline, b""):
                run.out(kind, raw.decode("utf-8", "replace").rstrip("\r\n"))
            stream.close()
        threads = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
                   threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True)]
        for t in threads:
            t.start()
        code = p.wait()
        for t in threads:
            t.join(5)
        run.on_cancel.remove(kill)
        if code != 0 and not run.stopping:
            run.out("err", f"exit code {code}")
        return code == 0

    # ---- history
    def _load_history(self) -> list:
        try:
            with open(self.hist_path, encoding="utf-8") as f:
                return [l.rstrip("\n") for l in f if l.strip()][-self.HISTORY:]
        except OSError:
            return []

    def _remember(self, line):
        line = line.replace("\n", " ").strip()
        if not line or (self.history and self.history[-1] == line):
            return
        self.history.append(line)
        try:
            os.makedirs(os.path.dirname(self.hist_path), exist_ok=True)
            if len(self.history) > self.HISTORY + 200:
                self.history = self.history[-self.HISTORY:]
                with open(self.hist_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(self.history) + "\n")
            else:
                with open(self.hist_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    # ---- what the Terminal page completes from
    def spec(self) -> dict:
        hub = self.studio.mcp
        tools = []
        for name in sorted(hub.tools):
            if name in MCP_ONLY:
                continue
            t = hub.tools[name]
            schema = t.schema or {}
            props = dict(schema.get("properties") or {})
            if name in hub.project_tools:
                props["project"] = {"type": "string", "description": "project id or name (default: the open one)"}
            params = [{"name": k, "type": "|".join(x for x in _types(p) if x) or "any", "enum": p.get("enum"),
                       "required": k in (schema.get("required") or []), "description": str(p.get("description") or "")[:200]}
                      for k, p in props.items()]
            tools.append({"name": name, "risk": t.risk, "group": tool_group(name), "summary": _first_sentence(t.description),
                          "usage": usage(name, dict(schema, properties=props)), "params": params})
        from .server import route_list
        st = self.studio.settings.get()
        return {
            "builtins": [{"name": k, "usage": v["usage"], "help": v["help"], "sub": v.get("sub")} for k, v in self.builtins.items()
                         if not v.get("hidden")],
            "tools": tools, "groups": list(GROUP_ORDER),
            "projects": [{"id": p["id"], "kind": p.get("kind"), "name": p.get("name") or p["id"]} for p in self.studio.projects.list()],
            "servers": [{"id": x["id"], "label": x.get("label"), "names": x.get("names")} for x in st["servers"]],
            "pages": list(PAGES), "routes": route_list(),
            "jobs": [{"id": j["id"], "title": j["title"], "state": j["state"]} for j in self.studio.jobs.list()[:30]],
            "agents": [{"id": s["id"], "goal": s.get("goal"), "status": s.get("status")}
                       for s in (self.studio.agents.list() if self.studio.agents else [])][:20],
            "cli": ["convert", "inspect", "profile", "sounds", "qc", "donor", "mlo", "mlo-inspect", "mlo-bake", "mlo-install",
                    "mlo-bundle", "mlo-manager-bundle", "--help"],
            "history": self.history[-300:],
            "shell": st.get("terminal", {}).get("shell", "auto"),
        }

    # ================================================================== built-in commands
    def _builtins(self) -> "OrderedDict[str, dict]":
        b = OrderedDict()

        def add(name, usage_, help_, fn, sub=None, hidden=False, aliases=()):
            b[name] = {"usage": usage_, "help": help_, "fn": fn, "sub": sub, "hidden": hidden}
            for a in aliases:
                b[a] = {"usage": usage_, "help": help_, "fn": fn, "sub": sub, "hidden": True}

        add("help", "help [command]", "what the terminal can do, or one command's arguments", self.c_help, aliases=("?", "man"))
        add("tools", "tools [text]", "the Studio's tools (each one is a command)", self.c_tools)
        add("status", "status", "what is set up in the Studio and what runs", self.c_status)
        add("projects", "projects [text]", "the cars, maps and world edits", self.c_projects, aliases=("ls",))
        add("use", "use [project | -]", "work on a project: its tools default to it (- for none)", self.c_use, aliases=("cd-project",))
        add("servers", "servers", "the Open77 servers set up in the Studio", self.c_servers)
        add("jobs", "jobs [all]", "running and recent jobs", self.c_jobs, aliases=("ps",))
        add("log", "log <job> [-f]", "a job's log; -f follows it until it ends", self.c_log, aliases=("job",))
        add("wait", "wait <job>", "follow a job until it ends", self.c_wait)
        add("cancel", "cancel <job>", "stop a running job", self.c_cancel, aliases=("kill",))
        add("agent", "agent [start|say|approve|decline|stop|attach|log|watch|list] ...",
            "drive the Studio's AI agent (help agent)", self.c_agent,
            sub=["list", "start", "say", "approve", "decline", "stop", "attach", "log", "watch"])
        add("api", "api [METHOD] <path> [json | key=value ...]", "call the Studio's API (api GET /api/qc)", self.c_api)
        add("routes", "routes [text]", "the Studio's API endpoints", self.c_routes)
        add("cli", "cli <converter arguments>", "the converter's command line (cli sounds, cli inspect \"C:\\cars\\x.zip\")",
            self.c_cli)
        add("sh", "sh <command> | !<command>", "a system command (PowerShell on Windows), in the terminal's folder", self.c_sh)
        add("cd", "cd [folder]", "the folder system commands run in (no folder: the workspace)", self.c_cd)
        add("pwd", "pwd", "the folder system commands run in", self.c_pwd)
        add("ui", "ui <page> | ui project <project> [tab]", "show a page of the Studio's window", self.c_ui,
            sub=list(PAGES) + ["project"])
        add("settings", "settings [key | key=value ...]", "show or change the Studio's settings", self.c_settings)
        add("history", "history [n]", "the commands typed here", self.c_history)
        add("echo", "echo <text>", "print text", self.c_echo)
        add("version", "version", "the Studio's and the converter's versions", self.c_version)
        add("clear", "clear", "clear the screen", self.c_clear, aliases=("cls",))
        return b

    # ---- help
    def c_help(self, run, words, raw):
        if words:
            what = words[0].lower()
            if what in self.builtins:
                c = self.builtins[what]
                run.out("ok", c["usage"])
                run.out("out", c["help"])
                if what == "agent":
                    self._help_agent(run)
                return True
            name = what if what in self.studio.mcp.tools else what.replace("-", "_")
            if name in self.studio.mcp.tools and name not in MCP_ONLY:
                self._help_tool(run, name)
                return True
            raise TermError(self._unknown(what))
        run.out("ok", "fivem2open77 Studio terminal: every Studio tool is a command, plus these:")
        rows = [[k, v["usage"], v["help"]] for k, v in self.builtins.items() if not v["hidden"]]
        run.table(["command", "usage", "what"], rows)
        run.out("out", "")
        run.out("out", "A tool: <tool> key=value --key value {json}, or its required arguments in order (deploy_car prod).")
        run.out("out", "Chain with && (stop at a failure) or ; (go on).  tools lists the tools; help <tool> its arguments.")
        run.out("info", "Keys: Tab completes, \u2191/\u2193 history, Ctrl+C stops the running command, Ctrl+L clears.")
        return True

    def _help_tool(self, run, name):
        hub = self.studio.mcp
        t = hub.tools[name]
        schema = dict(t.schema or {})
        props = dict(schema.get("properties") or {})
        if name in hub.project_tools:
            props["project"] = {"type": "string", "description": "project id or name (default: the open project)"}
        run.out("ok", usage(name, dict(schema, properties=props)))
        run.out("info", f"{tool_group(name)} \u00b7 risk: {t.risk}"
                        + (" (an agent's call waits for your OK when Settings > Agents says so)" if t.risk == "deploy" else ""))
        run.text(t.description)
        if props:
            rows = []
            for k, p in props.items():
                enum = p.get("enum")
                rows.append([k, "|".join(map(str, enum)) if enum else ("|".join(x for x in _types(p) if x) or "any"),
                             "yes" if k in (schema.get("required") or []) else "", str(p.get("description") or "")[:120]])
            run.table(["argument", "type", "required", "about"], rows)

    def _help_agent(self, run):
        run.table(["command", "what"], [
            ["agent", "the agent's sessions"],
            ["agent start [--project p] [--autonomy ask|auto_safe|full] [--provider anthropic|ollama] <goal>",
             "start a session and follow it until it waits for you"],
            ["agent say <text>", "answer its question, or tell it what to do next"],
            ["agent approve [note] / agent decline [note]", "decide the call it waits on"],
            ["agent stop", "stop it"],
            ["agent attach <id>", "follow another session here"],
            ["agent log [id]", "a session's whole conversation"],
            ["agent watch", "follow the session until it waits for you (Ctrl+C stops following, not the agent)"],
        ])

    def c_tools(self, run, words, raw):
        hub = self.studio.mcp
        flt = " ".join(words).lower()
        groups = OrderedDict((g, []) for g in GROUP_ORDER)
        for name in sorted(hub.tools):
            if name in MCP_ONLY:
                continue
            t = hub.tools[name]
            if flt and flt not in name and flt not in t.description.lower() and flt not in tool_group(name).lower():
                continue
            groups.setdefault(tool_group(name), []).append([name, t.risk, _first_sentence(t.description, 90)])
        n = 0
        for g, rows in groups.items():
            if not rows:
                continue
            run.out("ok", f"{g} ({len(rows)})")
            run.table(["tool", "risk", "what"], rows)
            n += len(rows)
        if not n:
            run.out("info", f"no tool matches {flt!r}")
        else:
            run.out("info", f"{n} tools \u00b7 help <tool> shows its arguments")
        return True

    # ---- the Studio
    def c_status(self, run, words, raw):
        st = self.studio
        tools = st.tools_status()
        from .app import APP_VERSION
        from .. import pipeline
        run.out("ok", f"fivem2open77 Studio {APP_VERSION} \u00b7 converter {pipeline.VERSION}")
        run.out("out", f"workspace  {st.workspace}")
        rows = [[k, "ok" if v.get("ok") else "missing", v.get("path") or v.get("help") or ""]
                for k, v in tools.items() if isinstance(v, dict) and k in ("game", "wolvenkit", "dotnet", "sevenzip", "donor",
                                                                           "linux_dotnet")]
        run.table(["tool", "state", "where"], rows)
        servers = st.settings.get()["servers"]
        run.out("out", f"servers    {', '.join(s['id'] for s in servers) or '(none)'}")
        projects = st.projects.list()
        kinds = {}
        for p in projects:
            kinds[p.get("kind")] = kinds.get(p.get("kind"), 0) + 1
        run.out("out", "projects   " + (", ".join(f"{n} {k}" for k, n in kinds.items()) or "(none)"))
        running = [j for j in st.jobs.list() if j["state"] in ("queued", "running")]
        run.out("out", f"jobs       {len(running)} running" + (": " + "; ".join(f"{j['id']} {j['title']}" for j in running[:5])
                                                               if running else ""))
        sessions = st.mcp.sessions_view()
        run.out("out", f"agents     {len(sessions)} connected over MCP" + (": " + ", ".join(s["label"] for s in sessions[:6])
                                                                          if sessions else ""))
        if st.agents:
            live = [s for s in st.agents.list() if s.get("status") in ("running", "starting", "waiting_user", "waiting_approval")]
            run.out("out", f"AI agent   {len(live)} session(s) active")
        qc = st.qc.view()
        run.out("out", "quality    " + ", ".join(f"{n} {k}" for k, n in (qc.get("counts") or {}).items() if n) if qc.get("counts")
                else "quality    no sweep yet")
        info = run.session.project_info()
        if info:
            run.out("info", f"open project: {info['name']} ({info['kind']}, {info['id']})")
        return True

    def c_projects(self, run, words, raw):
        flt = " ".join(words).lower()
        rows = []
        for p in self.studio.projects.list():
            name = p.get("name") or p["id"]
            if flt and flt not in name.lower() and flt not in p["id"] and flt != (p.get("kind") or ""):
                continue
            b = p.get("last_build") or {}
            last = (f"#{b.get('n')} {'ok' if b.get('ok') else 'failed'}" if b else "")
            rows.append([p["id"], p.get("kind") or "", name, last, len(p.get("deployments") or [])])
        run.table(["id", "kind", "name", "last build", "installs"], rows, empty="no project" + (f" matches {flt!r}" if flt else ""))
        return True

    def c_use(self, run, words, raw):
        s = run.session
        if not words:
            info = s.project_info()
            run.out("out", f"project: {info['name']} ({info['kind']}, {info['id']})" if info else "no project (use <project>)")
            return True
        want = " ".join(words)
        if want in ("-", "none"):
            s.project = None
            run.out("info", "no project", project=None)
            return True
        pid = self.studio.mcp._resolve_project(want)
        _TermCall(run).switch_project(pid)
        return True

    def c_servers(self, run, words, raw):
        rows = [[x["id"], x.get("label") or "", f"{x.get('user')}@{x.get('host')}:{x.get('port')}", ", ".join(x.get("names") or [])]
                for x in self.studio.redacted_settings()["servers"]]
        run.table(["id", "label", "ssh", "server folders"], rows, empty="no server (Servers page, or api POST /api/servers)")
        return True

    # ---- jobs
    def _job(self, jid):
        jid = str(jid or "").strip()
        j = self.studio.jobs.get(jid)
        if j is None and jid:
            hits = [x["id"] for x in self.studio.jobs.list() if x["id"].startswith(jid)]
            if len(hits) == 1:
                j = self.studio.jobs.get(hits[0])
        if j is None:
            raise TermError(f"no job {jid} (jobs lists them)" if jid else "which job? (jobs lists them)")
        return j

    def c_jobs(self, run, words, raw):
        items = self.studio.jobs.list()
        if not (words and words[0] == "all"):
            items = [j for j in items if j["state"] in ("queued", "running")] + \
                    [j for j in items if j["state"] not in ("queued", "running")][:12]
        now = time.time()
        rows = []
        for j in items:
            p = j.get("progress")
            prog = f"{round(p * 100)}%" if isinstance(p, (int, float)) and j["state"] == "running" else ""
            age = now - (j.get("created") or now)
            rows.append([j["id"], j["state"], prog, j["title"][:70], f"{int(age // 60)} min" if age >= 60 else f"{int(age)} s",
                         (j.get("step") or j.get("error") or "")[:60]])
        run.table(["job", "state", "", "what", "age", "step"], rows, empty="no job yet")
        return True

    def c_log(self, run, words, raw):
        follow = any(w in ("-f", "--follow") for w in words)
        ids = [w for w in words if not w.startswith("-")]
        j = self._job(ids[0] if ids else "")
        if follow:
            return self._wait_job(run, j)
        for entry in j.lines[-2000:]:
            run.out("log", entry.get("text", ""), job=j.id)
        run.job_line(j)
        return True

    def c_wait(self, run, words, raw):
        return self._wait_job(run, self._job(words[0] if words else ""))

    def _wait_job(self, run, j):
        run.follow(j)
        while j.state in ("queued", "running") and not run.stopping:
            time.sleep(0.25)
        run.settle()
        return j.state == "done"

    def c_cancel(self, run, words, raw):
        j = self._job(words[0] if words else "")
        ok = j.cancel()
        said = (f"stops when {j.cancel_late} is over" if ok and j.cancel_late else "cancel requested" if ok
                else f"not running ({j.state})")
        run.out("ok" if ok else "info", f"job {j.id}: {said}")
        return True

    # ---- the AI agent
    def c_agent(self, run, words, raw):
        sub = (words[0].lower() if words else "list")
        rest = words[1:]
        s = run.session
        if sub in ("list", "ls", "sessions"):
            items = agents_of(self.studio).list()
            rows = [[x["id"], x.get("status"), (x.get("goal") or "")[:60], x.get("project") or "", x.get("steps")] for x in items[:30]]
            run.table(["session", "status", "goal", "project", "steps"], rows, empty="no agent session (agent start <goal>)")
            if s.agent:
                run.out("info", f"following: {s.agent}")
            return True
        if sub == "start":
            opts, goal_words, i = {}, [], 0
            while i < len(rest):
                w = rest[i]
                if not getattr(w, "quoted", False) and w.startswith("--") and i + 1 < len(rest):
                    opts[w[2:]] = str(rest[i + 1])
                    i += 2
                    continue
                goal_words.append(str(w))
                i += 1
            goal = " ".join(goal_words).strip()
            if not goal:
                raise TermError("agent start: what should it do? (agent start build this car and check it)")
            project = opts.get("project")
            pid = self.studio.mcp._resolve_project(project) if project else s.project
            m = agents_of(self.studio)
            from .agent.providers import ProviderError
            try:
                v = m.start(project=pid, goal=goal, autonomy=agent_autonomy(self.studio, opts.get("autonomy"), s.origin),
                            provider=opts.get("provider"))
            except ProviderError as e:
                raise TermError(f"agent start: {e}") from e
            s.agent = v["id"]
            run.out("ok", f"agent session {v['id']} ({v.get('provider')}{', ' + v['model'] if v.get('model') else ''}, "
                          f"permissions: {v.get('autonomy')})", agent=v["id"])
            return self._agent_follow(run, m.sessions[v["id"]], 0)
        if sub in ("say", "send", "tell"):
            text_ = re.sub(r"^\S+\s*", "", raw, count=1).strip()
            if not text_:
                raise TermError("agent say: what? (agent say yes, install it on prod)")
            a = agent_session(self.studio, s.agent)
            before = len(a.events)
            agents_of(self.studio).message(a.id, text_)
            return self._agent_follow(run, a, before)
        if sub in ("approve", "allow", "yes", "decline", "deny", "no"):
            a = agent_session(self.studio, s.agent)
            p = a.pending
            if not p:
                raise TermError("the agent is not waiting for an approval")
            approve = sub in ("approve", "allow", "yes")
            if approve and s.origin == "mcp" and p.get("risk") == "deploy" \
                    and self.studio.settings.get()["mcp"]["approval"] == "studio":
                raise TermError("installs and restarts wait for the user's click in the Studio (Agent page)")
            note = re.sub(r"^\S+\s*", "", raw, count=1).strip() or None
            before = len(a.events)
            agents_of(self.studio).approve(a.id, p["call_id"], approve, note)
            run.out("ok", f"{'allowed' if approve else 'declined'}: {p['tool']}")
            return self._agent_follow(run, a, before)
        if sub == "stop":
            a = agent_session(self.studio, rest[0] if rest else s.agent)
            agents_of(self.studio).stop(a.id)
            run.out("ok", f"agent session {a.id}: stopping")
            return True
        if sub == "attach":
            a = agent_session(self.studio, rest[0] if rest else "")
            s.agent = a.id
            run.out("ok", f"following agent session {a.id}: {a.goal[:80]} ({a.status})", agent=a.id)
            for ev in a.events[-12:]:
                self._agent_line(run, ev)
            return True
        if sub in ("log", "history"):
            a = agent_session(self.studio, rest[0] if rest else s.agent)
            for ev in a.events:
                self._agent_line(run, ev)
            run.out("info", f"session {a.id}: {a.status}, {a.steps} step(s)")
            return True
        if sub in ("watch", "follow"):
            a = agent_session(self.studio, rest[0] if rest else s.agent)
            s.agent = a.id
            return self._agent_follow(run, a, len(a.events))
        raise TermError(f"agent {sub}: no such subcommand (help agent)")

    def _agent_line(self, run, ev):
        r = agent_event(ev)
        if r:
            kind, text_, extra = r
            run.out(kind, text_, **extra)

    def _agent_follow(self, run, a, since) -> bool:
        agent_follow(a, since, lambda ev: self._agent_line(run, ev), stop=lambda: run.stopping)
        if run.stopping:
            run.out("info", f"stopped following; the agent goes on (agent watch {a.id})")
            return True
        st = a.status
        if st == "waiting_approval" and a.pending:
            run.out("warn", "the agent waits for your OK: agent approve [note] | agent decline [note]")
        elif st == "waiting_user":
            run.out("info", "the agent waits for you: agent say <text>")
        elif st == "failed":
            return False
        return True

    # ---- the API
    def api_call(self, run, method, path, body=None) -> dict:
        """An API call typed in this terminal: an agent's is checked (api_guard) and its server changes
        gated like its tools'."""
        from .server import call_route
        m = (method or "GET").upper()
        if m not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise TermError(f"api: {m} is not an HTTP method the Studio answers")
        p = api_path(path)
        if run.session.origin == "mcp":
            if api_guard(m, p.split("?")[0], body) == "deploy":
                self._gate(run, "deploy", f"{m} {p}", body, _TermCall(run))
        return call_route(self.studio, m, p, body=body)

    def c_api(self, run, words, raw):
        if not words:
            raise TermError("api [METHOD] <path> [json | key=value ...]  (routes lists the endpoints)")
        method = "GET"
        if words[0].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            method = words[0].upper()
            words = words[1:]
        if not words:
            raise TermError("api: which endpoint?")
        path, rest = str(words[0]), words[1:]
        body = None
        if rest:
            if len(rest) == 1 and not getattr(rest[0], "quoted", False) and rest[0][:1] in "{[":
                try:
                    body = json.loads(rest[0])
                except ValueError as e:
                    raise TermError(f"api: bad JSON body: {e}") from e
            else:
                body = parse_args({}, rest, "api")
        ans = self.api_call(run, method, path, body)
        st = ans["status"]
        kind = "ok" if st < 300 else "err"
        run.out(kind, f"{st} {method} {api_path(path)}")
        if "json" in ans:
            run.out("json", json.dumps(ans["json"], indent=2, ensure_ascii=False, default=str))
        elif ans.get("content_type") == "image/png" and ans.get("data") is not None:
            run.image(base64.b64encode(ans["data"]).decode("ascii"), "image/png", api_path(path))
        elif "file" in ans:
            run.out("out", f"file: {ans['file']} ({ans.get('bytes')} bytes, {ans.get('content_type')})")
        elif ans.get("data") is not None:
            run.out("out", f"{len(ans['data'])} bytes of {ans.get('content_type')}")
        return st < 300

    def c_routes(self, run, words, raw):
        from .server import route_list
        flt = " ".join(words).lower()
        rows = [[r["method"], r["path"], r["about"]] for r in route_list()
                if not flt or flt in r["path"].lower() or flt in r["about"].lower() or flt == r["method"].lower()]
        run.table(["method", "path", "what"], rows, empty="no endpoint matches")
        return True

    # ---- programs
    def c_cli(self, run, words, raw):
        args = [str(w) for w in words] or ["--help"]
        if run.session.origin == "mcp" and not self.studio.settings.get()["mcp"].get("shell"):
            # it runs the programs it is told to (--wolvenkit) and writes where it is told: a system command
            raise TermError("the converter's command line is off for agents, like system commands (Settings > Agents); "
                            "the Studio's tools do the same work")
        if args[0] in CLI_REFUSED:
            raise TermError(f"cli {args[0]}: not from here (it starts a {'server' if args[0] != 'term' else 'prompt'} of its own)")
        # the package's own folder first: a source checkout runs from anywhere too (the app's Python finds
        # it by its ._pth, which ignores PYTHONPATH)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.pathsep.join(x for x in (root, os.environ.get("PYTHONPATH", "")) if x)
        return self._proc(run, [sys.executable, "-X", "utf8", "-m", "fivem2open77"] + args, cwd=run.session.cwd,
                          env={"PYTHONPATH": path})

    def c_sh(self, run, words, raw):
        return self._shell(run, raw.strip())

    def c_cd(self, run, words, raw):
        s = run.session
        target = raw.strip().strip('"').strip("'")
        if not target:
            s.cwd = self.studio.workspace
        else:
            if target == "~":
                target = os.path.expanduser("~")
            path = os.path.abspath(os.path.join(s.cwd, os.path.expandvars(os.path.expanduser(target))))
            if not os.path.isdir(path):
                raise TermError(f"cd: no folder {path}")
            s.cwd = path
        run.out("out", s.cwd, cwd=s.cwd)
        return True

    def c_pwd(self, run, words, raw):
        run.out("out", run.session.cwd)
        return True

    # ---- the window
    def ui_target(self, page=None, project=None, tab=None) -> str:
        if project:
            p = self.studio.projects.get(self.studio.mcp._resolve_project(project))
            if p.kind == "world":
                return f"#/world/{p.id}"
            tabs = TABS.get("map" if p.kind == "map" else "car")
            t = tab if tab in tabs else "overview"
            return f"#/{'map' if p.kind == 'map' else 'project'}/{p.id}/{t}"
        page = (page or "home").lower()
        if page not in PAGES:
            raise TermError(f"ui: no page {page} (pages: {', '.join(PAGES)}; ui project <name> [tab])")
        return PAGES[page]

    def ui_navigate(self, hash_, by="the terminal", message=None) -> int:
        bus = self.studio.bus
        bus.publish({"type": "ui", "action": "navigate", "hash": hash_, "by": by, "message": message})
        return len(bus._subs)

    def c_ui(self, run, words, raw):
        if not words:
            raise TermError("ui <page> | ui project <project> [tab]   pages: " + ", ".join(PAGES))
        if words[0].lower() == "project":
            if len(words) < 2:
                raise TermError("ui project <project> [tab]")
            h = self.ui_target(project=str(words[1]), tab=str(words[2]) if len(words) > 2 else None)
        else:
            h = self.ui_target(page=str(words[0]))
        n = self.ui_navigate(h, by=run.session.label)
        run.out("ok" if n else "info", f"showing {h}" + ("" if n else " (no Studio window is open)"))
        return True

    # ---- settings
    def c_settings(self, run, words, raw):
        st = self.studio
        if not words:
            d = st.redacted_settings()
            d.pop("servers", None)
            run.out("json", json.dumps(d, indent=2, default=str))
            run.out("info", "servers: the servers command \u00b7 change one: settings key=value (dotted keys)")
            return True
        patch, show = {}, []
        for w in words:
            if "=" in w and not getattr(w, "quoted", False):
                key, _, val = w.partition("=")
                _set_path(patch, key, read_value(val))
            else:
                show.append(str(w))
        if patch:
            if run.session.origin == "mcp":
                settings_guard(patch)
            for top in patch:
                if top in ("servers", "schema"):
                    raise TermError(f"settings {top}: use the servers page or api POST /api/servers")
            from .settings import SettingsError
            try:
                st.settings.update(patch)
            except SettingsError as e:
                raise TermError("invalid settings: " + "; ".join(e.errors)) from e
            st.bus.publish({"type": "settings"})
            run.out("ok", "saved: " + ", ".join(flat_keys(patch)))
        d = st.redacted_settings()
        for key in show:
            cur = d
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    raise TermError(f"settings: no {key}")
                cur = cur[part]
            run.text(json.dumps(cur, indent=2, default=str) if isinstance(cur, (dict, list)) else f"{key} = {json.dumps(cur)}")
        return True

    # ---- the rest
    def c_history(self, run, words, raw):
        try:
            n = max(1, min(1000, int(words[0]))) if words else 30
        except ValueError as e:
            raise TermError("history [n]") from e
        start = max(0, len(self.history) - n)
        for i, line in enumerate(self.history[start:], start + 1):
            run.out("out", f"{i:5d}  {line}")
        return True

    def c_echo(self, run, words, raw):
        run.out("out", " ".join(str(w) for w in words))
        return True

    def c_version(self, run, words, raw):
        from .app import APP_VERSION
        from .. import pipeline
        run.out("out", f"fivem2open77 Studio {APP_VERSION} \u00b7 converter {pipeline.VERSION} \u00b7 Python {sys.version.split()[0]}")
        return True

    def c_clear(self, run, words, raw):
        s = run.session
        with s.lock:
            s.scroll.clear()
        run.out("ctl", "", action="clear")
        return True
