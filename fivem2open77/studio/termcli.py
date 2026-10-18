"""``python -m fivem2open77 term``: the Studio's terminal in a command prompt.

    python -m fivem2open77 term                         a prompt (exit, or Ctrl+D / Ctrl+Z, leaves)
    python -m fivem2open77 term status                  one command line, then its exit code
    python -m fivem2open77 term "build_car project=urus && deploy_car prod"

It talks to the running Studio the way an agent does (MCP: the workspace's run/studio.json and MCP
token), starting it without a window when none runs, and prints what the Studio's terminal prints,
in colour.  The Studio shows the same lines on its Terminal page.  Images (renders) are saved under
<workspace>/terminal/images and their paths printed.  Ctrl+C stops the running command.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from .bridge import Bridge, BridgeError
from .paths import default_workspace

COLORS = {"err": "31", "warn": "33", "ok": "32", "info": "36", "log": "90", "agent": "97", "job": "33", "json": "37",
          "table": "0", "in": "35"}


def _enable_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))       # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001
        return False


class TermClient:
    def __init__(self, workspace, attach_only=False, color=None, out=None):
        self.ws = os.path.abspath(workspace)
        logs = os.path.join(self.ws, "logs")
        os.makedirs(logs, exist_ok=True)
        self.b = Bridge(self.ws, attach_only=attach_only, headless=True, log=os.path.join(logs, "term-cli.log"))
        self.b.watch_every = 0
        self.out = out or sys.stdout
        tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.color = (tty and _enable_ansi()) if color is None else color
        self.n = 10
        self.jobs = {}
        self.prompt_name = None

    # ---- the Studio
    def connect(self):
        from .app import APP_VERSION
        self.b.ensure(wait=240.0)
        self.b._initialize({"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "fivem2open77-term", "version": APP_VERSION}}, 1)

    def call(self, name, args, retry=True):
        self.n += 1
        st, _h, data = self.b._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
        if st == 404 and retry:                       # the Studio restarted: a new session
            self.b._reconnect()
            return self.call(name, args, retry=False)
        if st == 503:
            raise SystemExit("error: MCP is turned off in the Studio (Settings > Agents)")
        if st == 401:
            raise SystemExit("error: the Studio refused the MCP token")
        ans = json.loads(data or b"{}")
        if "error" in ans:
            raise SystemExit(f"error: {ans['error'].get('message')}")
        res = ans.get("result") or {}
        txt = "".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
        try:
            return json.loads(txt)
        except ValueError:
            return {"state": "failed", "exit": 1, "next": 0, "run": None,
                    "lines": [{"k": "err", "t": txt or "no answer"}]}

    # ---- printing
    def paint(self, kind, text):
        if not self.color or kind not in COLORS or COLORS[kind] == "0":
            return text
        return f"\x1b[{COLORS[kind]}m{text}\x1b[0m"

    def show(self, lines, echo=False):
        for line in lines:
            k = line.get("k")
            if k == "ctl":
                if line.get("action") == "end":
                    p = line.get("project")
                    self.prompt_name = p.get("name") if isinstance(p, dict) else None
                continue
            if k == "in" and not echo:
                continue
            if k == "job":
                key = line.get("job")
                sig = (line.get("state"), line.get("step"), round((line.get("p") or 0) * 10))
                if self.jobs.get(key) == sig:
                    continue
                self.jobs[key] = sig
            if k == "img":
                path = self.save_image(line)
                print(self.paint("info", f"[image saved: {path}]" if path else line.get("t", "")), file=self.out)
                continue
            text = line.get("t", "")
            if k == "in":
                text = "$ " + text
            print(self.paint(k, text), file=self.out)
        self.out.flush()

    def save_image(self, line):
        data = line.get("data")
        if not data:
            return None
        d = os.path.join(self.ws, "terminal", "images")
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, time.strftime("%Y%m%d-%H%M%S-") + f"{self.n}-{line.get('n', 0)}.png")
            with open(p, "wb") as f:
                f.write(base64.b64decode(data))
            return p
        except (OSError, ValueError):
            return None

    # ---- a command line
    def run(self, line, echo=False) -> int:
        v = self.call("terminal", {"command": line, "seconds": 20, "format": "lines"})
        self.show(v.get("lines") or [], echo)
        since = v.get("next") or 0
        try:
            while v.get("state") == "running" and v.get("run"):
                v = self.call("terminal_output", {"run": v["run"], "since": since, "seconds": 20, "format": "lines"})
                self.show(v.get("lines") or [], echo)
                since = v.get("next") or since
        except KeyboardInterrupt:
            if v.get("run"):
                v = self.call("terminal_output", {"run": v["run"], "since": since, "cancel": True, "seconds": 5,
                                                  "format": "lines"})
                self.show(v.get("lines") or [], echo)
            return 130
        return int(v.get("exit") or 0) if v.get("state") != "running" else 0

    def repl(self) -> int:
        try:
            import readline  # noqa: F401 - history and editing where Python has it
            hist = os.path.join(self.ws, "terminal", "cli-history")
            try:
                readline.read_history_file(hist)
            except OSError:
                pass
        except ImportError:
            readline, hist = None, None
        print(self.paint("ok", "fivem2open77 Studio terminal") + self.paint("info", "  (help lists the commands; exit leaves)"),
              file=self.out)
        code = 0
        while True:
            name = f" [{self.prompt_name}]" if self.prompt_name else ""
            try:
                line = input(self.paint("info", f"f2o77{name} › ") if self.color else f"f2o77{name} > ")
            except KeyboardInterrupt:
                print(file=self.out)
                continue
            except EOFError:
                print(file=self.out)
                break
            line = line.strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            code = self.run(line)
        if readline is not None and hist:
            try:
                os.makedirs(os.path.dirname(hist), exist_ok=True)
                readline.write_history_file(hist)
            except OSError:
                pass
        return code


def main(a=None) -> int:
    ws = os.path.abspath(getattr(a, "workspace", None) or os.environ.get("F2O77_WORKSPACE") or default_workspace())
    c = TermClient(ws, attach_only=bool(getattr(a, "attach_only", False)),
                   color=False if getattr(a, "no_color", False) else None)
    try:
        c.connect()
    except BridgeError as e:
        raise SystemExit(f"error: {e}")
    words = list(getattr(a, "command", None) or [])
    if words:
        return c.run(join_words(words))
    return c.repl()


def join_words(words) -> str:
    """The command line of the words a shell split: one word is the line as it is ("a && b"); a word of
    several when there are more ("project=My Car") is quoted again."""
    if len(words) == 1:
        return words[0]
    out = []
    for w in words:
        if any(ch.isspace() for ch in w) and not (w[:1] in "{[" and w[-1:] in "}]"):
            q = "'" if '"' in w else '"'
            k, eq, v = w.partition("=")
            out.append(f"{k}={q}{v}{q}" if eq and k.replace("_", "").replace(".", "").isalnum() else f"{q}{w}{q}")
        else:
            out.append(w)
    return " ".join(out)
