"""The map manager's agent API on a server, reached through the Studio's SSH connection.

The manager (resource f2o77_maps) answers on the server's HTTP handler listener (server.jsonc
httpHandlers.listenUrl, loopback 127.0.0.1:11781 by default).  The Studio opens a direct-tcpip
channel to that address over its SSH connection and speaks HTTP/1.1 over it: nothing is opened on
the server's network, and the listener can stay on loopback.

Writes need the manager's agent token.  The Studio reads it on the server
(resources/**/f2o77_maps/data/agent_token.txt, written by the manager at its first start) over the
same connection and keeps it in memory only: it is never written to disk, logged, returned by the
Studio's API or shown to the AI agent.

    link = LINKS.get(server, secrets, "open77-xbuniverse")
    link.status(); link.map("cedar_crest"); link.op("cedar_crest", "move", dx=5)
    link.wait("cedar_crest", until="applied")
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
import threading
import time
import urllib.parse

MANAGER = "f2o77_maps"
OPS = ("place", "move", "here", "discard", "revert", "ground", "drop", "goto", "apply", "cancel",
       "presetSave", "presetUse", "presetDelete")
BUSY = ("requested", "queued", "baking", "unstaging")
STAGED = ("built", "restart_scheduled", "waiting_empty")
IDLE_CLOSE = 180          # seconds an unused link keeps its SSH connection

# runs on the server host: where the manager is, whether the listener is on, the token
PROBE = r"""
import json, os, re, sys
d = sys.argv[1]
def strip_jsonc(t):
    out, i, n, s = [], 0, len(t), False
    while i < n:
        c = t[i]
        if s:
            out.append(c)
            if c == "\\": out.append(t[i + 1]); i += 1
            elif c == '"': s = False
        elif c == '"': s = True; out.append(c)
        elif t.startswith("//", i):
            while i < n and t[i] != "\n": i += 1
            continue
        elif t.startswith("/*", i): i = t.index("*/", i) + 2; continue
        else: out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
out = {"exists": os.path.isfile(os.path.join(d, "server.jsonc")), "http": None, "manager": None, "token": None,
       "manager_version": None}
if out["exists"]:
    try:
        c = json.loads(strip_jsonc(open(os.path.join(d, "server.jsonc"), encoding="utf-8").read()))
        h = c.get("httpHandlers") or {}
        out["http"] = {"enabled": bool(h.get("enabled")), "listenUrl": h.get("listenUrl") or "http://127.0.0.1:11781"}
    except Exception as e:
        out["config_error"] = str(e)[:200]
    root = os.path.join(d, "resources")
    for dp, dn, fn in os.walk(root):
        if dp[len(root):].count(os.sep) >= 4:
            dn[:] = []
        if os.path.basename(dp) == "f2o77_maps" and "open77.lua" in fn:
            out["manager"] = os.path.relpath(dp, d)
            m = re.search(r'^version\s+"([^"]+)"', open(os.path.join(dp, "open77.lua"), encoding="utf-8", errors="replace").read(), re.M)
            out["manager_version"] = m.group(1) if m else None
            t = os.path.join(dp, "data", "agent_token.txt")
            if os.path.isfile(t):
                out["token"] = open(t, encoding="utf-8").read().strip() or None
            break
print(json.dumps(out))
"""


class LinkError(Exception):
    """A problem reaching the manager (the message says what to do about it)."""


class Unreachable(LinkError):
    """The listener did not answer (server stopped or restarting, or the agent API is off)."""


def as_list(v) -> list:
    """A list the manager sent.  Lua's JSON has no empty array: an empty table goes out as {} (no
    saved spots yet: "presets": {}), and a table with holes as {"1": .., "3": ..}."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        if all(isinstance(k, str) and k.isdigit() for k in v):
            return [v[k] for k in sorted(v, key=int)]
        return list(v.values())
    return []


MAP_LISTS = ("presets", "history")


def normalize_map(m):
    """A map as the manager describes it, with its lists as lists."""
    if isinstance(m, dict):
        for k in MAP_LISTS:
            if k in m:
                m[k] = as_list(m[k])
    return m


def normalize(value):
    """Lists stay lists in what the manager answers (status, a map, players, an operation)."""
    if not isinstance(value, dict):
        return value
    if "maps" in value:
        value["maps"] = [normalize_map(m) for m in as_list(value["maps"]) if isinstance(m, dict)]
    if "players" in value:
        value["players"] = [p for p in as_list(value["players"]) if isinstance(p, dict)]
    if isinstance(value.get("map"), dict):
        normalize_map(value["map"])
    if "name" in value and any(k in value for k in MAP_LISTS):
        normalize_map(value)
    return value


def _parse_listen(url: str) -> tuple:
    u = urllib.parse.urlsplit(url or "http://127.0.0.1:11781")
    if u.scheme != "http" or not u.hostname:
        raise LinkError(f"httpHandlers.listenUrl {url!r} is not an http:// address")
    host = u.hostname
    if host in ("0.0.0.0", "::", "[::]", "*", "+"):
        host = "127.0.0.1"
    return host, int(u.port or 80)


def _read_response(chan, timeout):
    """status, headers, body of one HTTP/1.1 response on a channel (Content-Length, chunked, or EOF)."""
    buf = b""
    deadline = time.time() + timeout
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise LinkError("the map manager did not answer in time")
        chunk = chan.recv(65536)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        raise Unreachable("the connection closed before an answer (the server may be restarting)")
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    m = re.match(r"^HTTP/1\.[01] (\d{3})", lines[0])
    if not m:
        raise LinkError(f"not an HTTP answer: {lines[0][:80]!r}")
    status = int(m.group(1))
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    def more():
        if time.time() > deadline:
            raise LinkError("the map manager's answer stalled")
        return chan.recv(65536)

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            while b"\r\n" not in rest:
                c = more()
                if not c:
                    raise LinkError("truncated chunked answer")
                rest += c
            size_line, rest = rest.split(b"\r\n", 1)
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                break
            while len(rest) < size + 2:
                c = more()
                if not c:
                    raise LinkError("truncated chunked answer")
                rest += c
            body += rest[:size]
            rest = rest[size + 2:]
        return status, headers, body
    if "content-length" in headers:
        n = int(headers["content-length"])
        while len(rest) < n:
            c = more()
            if not c:
                break
            rest += c
        return status, headers, rest[:n]
    while True:
        c = more()
        if not c:
            break
        rest += c
    return status, headers, rest


class ManagerLink:
    """One server folder's map manager over one SSH connection (see the module doc)."""

    def __init__(self, server: dict, secrets, name: str, remote_factory=None, log=None):
        from . import deploy as deploy_mod
        if name not in server["names"]:
            raise LinkError(f"'{name}' is not a server of {server['id']}")
        self.server = server
        self.secrets = secrets
        self.name = name
        self.dir = posixpath.join(server["servers_root"], name)
        self._factory = remote_factory or (lambda: deploy_mod.Remote(server, secrets, log=log or (lambda *_: None)))
        self.remote = None
        self.info = None              # the probe, without the token
        self.probed = 0.0
        self.failed = False           # the last status call failed: look at the server again
        self._token = None
        self.lock = threading.RLock()
        self.used = time.time()

    # ------------------------------------------------------------------ connection
    def _transport(self):
        with self.lock:
            if self.remote is None or not self.remote.client.get_transport() or \
                    not self.remote.client.get_transport().is_active():
                self.close()
                self.remote = self._factory()
                self.info = None
            self.used = time.time()
            return self.remote.client.get_transport()

    def probe(self, force=False, max_age=None) -> dict:
        """Where the manager is, whether the agent API listener is on, whether a token exists
        (read again when forced or older than max_age seconds)."""
        with self.lock:
            fresh = max_age is None or time.time() - self.probed < max_age
            if self.info is not None and not force and fresh:
                return self.info
            self._transport()
            code, out = self.remote.run(f"python3 - {shlex.quote(self.dir)} <<'F2O77PY'\n{PROBE}\nF2O77PY", timeout=60,
                                        check=False)
            line = next((x for x in reversed((out or "").splitlines()) if x.strip().startswith("{")), None)
            if code != 0 or not line:
                raise LinkError(f"could not look at {self.dir} on {self.server['host']} (python3 missing?): "
                                + (out or "")[-300:].replace(self._token or "\0", "***"))
            data = json.loads(line)
            self._token = data.pop("token", None)
            data["token"] = bool(self._token)
            data["dir"] = self.dir
            self.info = data
            self.probed = time.time()
            return data

    def close(self):
        with self.lock:
            if self.remote is not None:
                try:
                    self.remote.close()
                except Exception:  # noqa: BLE001
                    pass
            self.remote = None

    # ------------------------------------------------------------------ HTTP
    def _need_api(self):
        info = self.probe()
        if not info.get("exists"):
            raise LinkError(f"no Open77 server at {self.dir}")
        if not info.get("manager"):
            raise LinkError(f"the map manager (f2o77_maps) is not installed on {self.name}: deploy a map first")
        http = info.get("http") or {}
        if not http.get("enabled"):
            raise LinkError(f"the agent API is off on {self.name}: turn it on (Servers > agent API, a server "
                            "restart) so the Studio can move maps")
        return _parse_listen(http.get("listenUrl"))

    def request(self, method: str, path: str, body: dict | None = None, token=False, timeout=25.0) -> tuple:
        """(status, JSON value) of one call; path is relative to /f2o77_maps."""
        import paramiko
        host, port = self._need_api()
        if token and not self._token:
            self.probe(force=True)
            if not self._token:
                raise LinkError(f"the map manager on {self.name} has no agent token yet: it writes one at its "
                                "first start (start or restart the server once after the deploy)")
        transport = self._transport()
        try:
            chan = transport.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=10)
        except paramiko.ChannelException as e:
            if getattr(e, "code", None) == 1:      # administratively prohibited
                raise LinkError(f"the SSH server of {self.server['host']} does not allow port forwarding, which the Studio "
                                "uses to reach the map manager: set AllowTcpForwarding yes (or local) in "
                                "/etc/ssh/sshd_config for this user and reload sshd") from e
            raise Unreachable(f"the agent API on {self.name} ({host}:{port}) is not answering: the server is stopped "
                              "or restarting") from e
        except (paramiko.SSHException, OSError, EOFError) as e:
            self.close()                           # the SSH connection itself broke: reconnect next time
            raise Unreachable(f"the SSH connection to {self.server['host']} dropped ({e.__class__.__name__}); "
                              "it reconnects on the next call") from e
        try:
            chan.settimeout(timeout)
            data = b"" if body is None else json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")
            full = "/" + MANAGER + path
            head = [f"{method} {full} HTTP/1.1", f"Host: {host}:{port}", "Connection: close", "Accept: application/json",
                    "User-Agent: fivem2open77-studio"]
            if self._token:
                head.append(f"Authorization: Bearer {self._token}")
            if body is not None:
                head.append("Content-Type: application/json")
            head.append(f"Content-Length: {len(data)}")
            chan.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + data)
            try:
                status, _, raw = _read_response(chan, timeout)
            except OSError as e:          # socket.timeout and friends
                raise Unreachable(f"the agent API on {self.name} stopped answering ({e.__class__.__name__})") from e
        finally:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            value = {"raw": raw.decode("utf-8", "replace")[:500]}
        if status == 401:
            # a redeploy can bring a new token: read it again once
            self.probe(force=True)
            raise LinkError("the map manager refused the agent token (it was read again; retry)")
        if status == 404 and isinstance(value, dict) and value.get("error") == "unknown_map":
            raise LinkError(f"no map '{path.split('/')[2] if path.count('/') >= 2 else '?'}' in the manager on {self.name}")
        if status == 404 and not (isinstance(value, dict) and value.get("error") in ("unknown_op", "unknown_route")):
            raise LinkError(f"the map manager is not running on {self.name} (no {full} on the listener): "
                            "is f2o77_maps in resources.load and started?")
        if status >= 400 and status != 409:
            raise LinkError(f"HTTP {status} from the map manager: {json.dumps(value)[:300]}")
        return status, value

    # ------------------------------------------------------------------ API
    def status(self) -> dict:
        return normalize(self.request("GET", "/status")[1])

    def map(self, name: str) -> dict:
        return normalize(self.request("GET", "/maps/" + urllib.parse.quote(name))[1])

    def baker(self) -> dict:
        return self.request("GET", "/baker")[1]

    def players(self) -> dict:
        return normalize(self.request("GET", "/players", token=True)[1])

    def op(self, map_name: str, op_name: str, args: dict | None = None) -> dict:
        if op_name not in OPS:
            raise LinkError(f"unknown map operation {op_name}")
        status, value = self.request("POST", f"/maps/{urllib.parse.quote(map_name)}/{op_name}", args or {}, token=True)
        return normalize(value)

    def screenshot(self, player: int, url: str, options=None) -> dict:
        return self.request("POST", "/screenshot", {"playerId": player, "url": url, "options": options or {}}, token=True)[1]

    def wait(self, name, until="applied", timeout=900.0, interval=2.0, log=None, cancelled=None) -> dict:
        """Poll a map until its move is `until`: built (staged for the restart), applied (live after the
        restart) or settled (nothing in progress).  The restart in the middle is waited out."""
        if until not in ("built", "applied", "settled"):
            raise LinkError("until: built, applied or settled")
        deadline = time.time() + timeout
        last = object()
        while True:
            if cancelled and cancelled():
                raise LinkError("cancelled")
            try:
                m = self.map(name)
                a = m.get("apply") or {}
                state = a.get("state")
                if state != last:
                    if log:
                        log(f"{name}: {state or 'nothing in progress'}" + (f" - {a.get('message')}" if a.get("message") else ""))
                    last = state
                if state == "failed":
                    raise LinkError(f"{name}: the move failed: {a.get('message') or 'see the baker log'}")
                if state == "cancelled" and until != "settled":
                    raise LinkError(f"{name}: the move was cancelled")
                if until == "built" and (state in STAGED or state == "applied"):
                    return m
                if until == "applied" and state == "applied":
                    return m
                if until == "settled" and state not in BUSY and state not in STAGED:
                    return m
            except Unreachable as e:
                if log:
                    log(f"{name}: server not answering ({e}); waiting")
                self.info = None              # re-read the listener after a restart
            if time.time() >= deadline:
                raise LinkError(f"{name}: not {until} after {timeout:.0f} s (last state: {last if isinstance(last, str) else '?'})")
            time.sleep(interval)


class LinkPool:
    """Links kept per (server id, server folder) so a page that polls does not reconnect each time."""

    def __init__(self, factory=ManagerLink):
        self.factory = factory
        self.links = {}
        self.lock = threading.Lock()

    def get(self, server: dict, secrets, name: str) -> ManagerLink:
        key = (server["id"], name, server["host"], server["user"], int(server.get("port", 22)))
        with self.lock:
            self._expire()
            link = self.links.get(key)
            if link is None:
                link = self.links[key] = self.factory(server, secrets, name)
            link.used = time.time()
            return link

    def _expire(self):
        now = time.time()
        for k, link in list(self.links.items()):
            if now - link.used > IDLE_CLOSE:
                link.close()
                del self.links[k]

    def forget(self, server_id=None):
        with self.lock:
            for k, link in list(self.links.items()):
                if server_id is None or k[0] == server_id:
                    link.close()
                    del self.links[k]

    def close(self):
        self.forget()
