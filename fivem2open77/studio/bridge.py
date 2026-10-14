"""``python -m fivem2open77 mcp``: the Studio's MCP server for clients that start a command (stdio).

It relays newline-delimited JSON-RPC between the client and the running Studio's ``/mcp`` endpoint
(mcp.py).  The Studio serving the workspace is found by its ``run/studio.json``; when none runs, the
bridge starts it (the Studio app with its window on Windows, so the user sees what the agent does;
``--headless``, or no app: the server alone, which stops after a quarter of an hour of nothing).
When the Studio restarts in between, the bridge finds it again and opens a new session for the client
on its own.  Standard library only.

A bridge started by a Microsoft Store (MSIX) app runs in that app's package container: files it writes
under AppData land in the app's own copy.  It never starts the Studio there (a Studio started in the
container would keep new projects and builds where the Studio started normally does not see them) and
it only reads the token the Studio made.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .mcp import read_token
from .paths import app_home, default_workspace


class BridgeError(Exception):
    pass


def updating(max_age=600.0) -> bool:
    """True while the setup replaces the installed app: it leaves %LOCALAPPDATA%\\fivem2open77\\updating for
    that time (a marker older than ten minutes is a setup that did not finish)."""
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        return False
    try:
        return time.time() - os.path.getmtime(os.path.join(base, "fivem2open77", "updating")) < max_age
    except OSError:
        return False


def in_package() -> bool:
    """True inside a packaged (Microsoft Store / MSIX) app's container (Windows)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentPackageFullName
    except (OSError, AttributeError):
        return False
    f.restype = wintypes.LONG
    f.argtypes = (ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
    n = wintypes.UINT(0)
    rc = f(ctypes.byref(n), None)
    return rc in (0, 122)              # ERROR_INSUFFICIENT_BUFFER: it has a package; 15700: it has none


class Bridge:
    def __init__(self, workspace: str, attach_only=False, headless=False, log=None, out=None):
        self.ws = os.path.abspath(workspace)
        self.attach_only = attach_only
        self.headless = headless
        self.logf = log
        self.out = out or sys.stdout.buffer
        self.out_lock = threading.Lock()
        self.port = None
        self.session = None
        self.protocol = None
        self.init_params = None
        self.init_done = threading.Event()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp-bridge")
        self.launched_at = 0.0

    # ---- log
    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        try:
            if self.logf:
                with open(self.logf, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass
        try:
            sys.stderr.write("[fivem2open77 mcp] " + msg + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    # ---- finding (or starting) the Studio
    def find(self):
        from .server import running_instance
        info = running_instance(self.ws)
        return int(info["port"]) if info else None

    def launch(self):
        home = app_home()
        exe = os.path.join(home, "fivem2open77-studio.exe") if home else None
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        logdir = os.path.join(self.ws, "logs")
        os.makedirs(logdir, exist_ok=True)
        if exe and os.path.isfile(exe) and not self.headless and os.environ.get("F2O77_WORKSPACE") in (None, "", self.ws):
            self.log(f"starting the Studio: {exe}")
            cmd = [exe]
            out = subprocess.DEVNULL
        else:
            self.log("starting the Studio without a window (it stops after 15 minutes of nothing)")
            cmd = [sys.executable, "-X", "utf8", "-m", "fivem2open77", "studio", "--port", "0", "--workspace", self.ws,
                   "--idle-exit", "900"]
            out = open(os.path.join(logdir, time.strftime("studio-agents-%Y%m%d-%H%M%S.log")), "ab")
        env = dict(os.environ)
        env["F2O77_WORKSPACE"] = self.ws
        kw = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": out, "env": env, "close_fds": True}
        if os.name == "nt":
            for extra in (0x01000000, 0):                   # CREATE_BREAKAWAY_FROM_JOB when the job allows it
                try:
                    subprocess.Popen(cmd, creationflags=flags | extra, **kw)
                    break
                except OSError:
                    if not extra:
                        raise
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        self.launched_at = time.time()

    def ensure(self, wait=120.0):
        port = self.find()
        if port:
            self.port = port
            return port
        if self.attach_only:
            raise BridgeError("the fivem2open77 Studio is not running (open it, or run the bridge without --attach-only)")
        if in_package():
            raise BridgeError("the fivem2open77 Studio is not running: open it (Start menu: fivem2open77 Studio) and try "
                              "again. This app is a Microsoft Store build, which cannot start the Studio itself.")
        held = updating()
        if held:
            # starting the old copy now would hold the folder the setup is replacing: the setup starts the new one
            self.log("the Studio is being updated: waiting for the new one")
            wait = max(wait, 300.0)
        elif time.time() - self.launched_at > 60:
            self.launch()
        t0 = time.time()
        while time.time() - t0 < wait:
            port = self.find()
            if port:
                self.port = port
                self.log(f"the Studio answers on port {port}")
                return port
            if held and not updating():                 # the setup is done (or gave up) and none answers yet
                held = False
                if time.time() - self.launched_at > 60:
                    time.sleep(3.0)                     # the one the setup starts comes up first
                    if not self.find():
                        self.launch()
            time.sleep(0.5)
        raise BridgeError("the fivem2open77 Studio did not start (see its log in the workspace's logs folder)")

    # ---- talking to it
    def _post(self, msg: dict, timeout=900.0):
        port = self.port or self.ensure()
        token = read_token(self.ws)
        if not token:
            raise BridgeError("the Studio has no MCP token yet: open the fivem2open77 Studio (1.3 or newer) once")
        body = json.dumps(msg).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + token}
        if self.session and msg.get("method") != "initialize":
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            c.request("POST", "/mcp", body, headers)
            r = c.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            c.close()

    def _initialize(self, params, mid):
        st, h, data = self._post({"jsonrpc": "2.0", "id": mid, "method": "initialize", "params": params})
        if st != 200:
            raise BridgeError(f"the Studio refused the session ({st}): {data[:200]!r}")
        ans = json.loads(data)
        self.session = h.get("mcp-session-id")
        self.protocol = ((ans.get("result") or {}).get("protocolVersion"))
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return ans

    def _reconnect(self):
        """The Studio restarted (or went away): find it again and open a new session like the first."""
        with self.lock:
            self.port = None
            self.session = None
            self.ensure()
            if self.init_params is not None:
                self._initialize(self.init_params, "f2o77-bridge-reinit")
                self.log("new session with the Studio (it was restarted)")

    def relay(self, msg: dict):
        """One message from the client -> the Studio; its answer (or None)."""
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            with self.lock:
                self.init_params = msg.get("params") or {}
                try:
                    self.ensure()
                    ans = self._initialize(self.init_params, mid)
                finally:
                    self.init_done.set()
            return ans
        if not self.init_done.is_set():
            # before initialize: a client of the stateless 2026-07-28 era asks server/discover first and falls
            # back to initialize when it is not spoken (as the Studio would answer); ping needs no session
            if method == "server/discover":
                return None if mid is None else _err(mid, -32601, "server/discover is not supported: use initialize")
            if method == "ping":
                return None if mid is None else {"jsonrpc": "2.0", "id": mid, "result": {}}
        if not self.init_done.wait(300):
            return _err(mid, -32002, "not initialized")
        for attempt in (1, 2):
            try:
                st, _h, data = self._post(msg)
            except (OSError, http.client.HTTPException) as e:
                if attempt == 2:
                    return None if mid is None else _err(mid, -32003, f"the Studio is not reachable: {e}")
                self.log(f"lost the Studio ({e}): looking for it again")
                try:
                    self._reconnect()
                except (BridgeError, OSError) as e2:
                    return None if mid is None else _err(mid, -32003, str(e2))
                continue
            if st == 404 and attempt == 1:             # unknown session: the Studio restarted
                try:
                    self._reconnect()
                except (BridgeError, OSError) as e2:
                    return None if mid is None else _err(mid, -32003, str(e2))
                continue
            if st == 202 or not data:
                return None
            if st == 401:
                return None if mid is None else _err(mid, -32004, "the Studio refused the MCP token (was it reset?)")
            if st == 503:
                return None if mid is None else _err(mid, -32005, "MCP is turned off in the Studio (Settings > Agents)")
            try:
                return json.loads(data)
            except ValueError:
                return None if mid is None else _err(mid, -32603, f"bad answer from the Studio ({st})")
        return None

    def send(self, obj):
        if obj is None:
            return
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        with self.out_lock:
            self.out.write(line)
            self.out.flush()

    def handle_line(self, raw: bytes):
        raw = raw.strip()
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self.send(_err(None, -32700, "parse error"))
            return
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if not isinstance(m, dict):
                self.send(_err(None, -32600, "invalid request"))
                continue
            if m.get("method") == "initialize":
                self.send(self._safe(m))          # first, in order
            else:
                self.pool.submit(lambda m=m: self.send(self._safe(m)))

    def _safe(self, m):
        try:
            return self.relay(m)
        except BridgeError as e:
            self.log(str(e))
            return None if m.get("id") is None else _err(m.get("id"), -32003, str(e))
        except Exception as e:  # noqa: BLE001
            self.log(f"bridge error: {e!r}")
            return None if m.get("id") is None else _err(m.get("id"), -32603, f"bridge error: {e}")

    def run(self, stdin=None):
        stdin = stdin or sys.stdin.buffer
        self.log(f"bridge for workspace {self.ws}")
        for line in iter(stdin.readline, b""):
            self.handle_line(line)
        self.pool.shutdown(wait=True)


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(a=None) -> int:
    ws = os.path.abspath(getattr(a, "workspace", None) or os.environ.get("F2O77_WORKSPACE") or default_workspace())
    logf = getattr(a, "log", None) or os.path.join(ws, "logs", "mcp-bridge.log")
    try:
        os.makedirs(os.path.dirname(logf), exist_ok=True)
        if os.path.isfile(logf) and os.path.getsize(logf) > 4 * 1024 * 1024:
            os.replace(logf, logf + ".old")
    except OSError:
        pass
    b = Bridge(ws, attach_only=bool(getattr(a, "attach_only", False)), headless=bool(getattr(a, "headless", False)), log=logf)
    b.run()
    return 0
