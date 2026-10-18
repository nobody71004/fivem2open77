"""Studio settings (settings.json in the workspace) and their validation.

Secrets are not here: see secrets.py.  Server entries are used to build shell command lines on
the server host, so every field that reaches a command line is checked against a strict pattern.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading

SCHEMA = 1
AUTONOMY = ("ask", "auto_safe", "full")

DEFAULTS = {
    "schema": SCHEMA,
    "paths": {"game_dir": "", "wolvenkit": "", "dotnet": "", "sevenzip": "", "cache_dir": "", "linux_dotnet": "", "ncwe_mcp": ""},
    "ai": {
        "provider": "anthropic",             # anthropic | ollama | none
        "anthropic_model": "",               # empty: picked from the models the key can use
        "anthropic_base_url": "https://api.anthropic.com",
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "",
        "autonomy": "ask",                   # ask: approve every change; auto_safe: approve deploys only; full
        "max_steps": 40,
        "vision": True,                      # send renders to the model
    },
    "servers": [],
    "deploy": {"default_server": "", "mode": "stage", "allow_unsecured": False},
    "build": {"keep_work": False},
    # quality checks around the clock (qcsweep.py): every project's latest build checked every
    # interval_hours, what fails or was built before a converter fix rebuilt (auto_rebuild)
    "qc": {"enabled": True, "interval_hours": 6, "auto_rebuild": True, "max_rebuilds": 100},
    # agents outside the Studio (Claude Code, Claude Desktop, Codex, Cursor...) over MCP
    "mcp": {
        "enabled": True,
        "approval": "client",                # client: the agent's app asks; studio: deploys wait for a click here
        "ncwe_launch": False,                # attach to the signed-in NCWE editor unless explicitly enabled
        "call_seconds": 45,                  # a call that runs longer answers "running" (wait_job follows it)
        "shell": False,                      # agents may run system commands in the Studio's terminal (sh / !)
    },
    # the Studio's terminal (terminal.py): the shell its system commands run in
    "terminal": {"shell": "auto"},           # auto (PowerShell on Windows, sh elsewhere) | powershell | cmd | bash | sh
}

SERVER_DEFAULTS = {
    "id": "", "label": "", "kind": "ssh", "host": "", "port": 22, "user": "root",
    "auth": "key",                     # key | password | agent
    "key_file": "",
    "servers_root": "/opt",            # Open77 server folders live here (NAME = folder = unit name)
    "names": [],                       # server folders / systemd units, e.g. open77-xbuniverse
    "systemctl": "systemctl",          # or "systemctl --user" for user units
    "allow_unsecured": False,
}

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH = re.compile(r"^/[A-Za-z0-9._/-]*$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[?[0-9a-fA-F:]+\]?$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


class SettingsError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_server(s: dict) -> dict:
    errors = []
    s = _merge(SERVER_DEFAULTS, s)
    unknown = set(s) - set(SERVER_DEFAULTS)
    if unknown:
        errors.append("server: unknown settings " + ", ".join(sorted(unknown)))
    if not _ID.match(str(s["id"])):
        errors.append("server id: 1-32 of a-z 0-9 _ -")
    if s["kind"] != "ssh":
        errors.append("server kind: ssh")
    if not _HOST.match(str(s["host"])):
        errors.append("server host: a host name or IP address")
    try:
        s["port"] = int(s["port"])
        if not 1 <= s["port"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("server port: 1-65535")
    if not _USER.match(str(s["user"])):
        errors.append("server user: a Linux user name")
    if s["auth"] not in ("key", "password", "agent"):
        errors.append("server auth: key, password or agent")
    if s["auth"] == "key" and not s["key_file"]:
        errors.append("server key_file: the private key file (or choose password)")
    if not _PATH.match(str(s["servers_root"])) or ".." in s["servers_root"]:
        errors.append("server servers_root: an absolute path of letters, digits, . _ - /")
    if not isinstance(s["names"], list) or not s["names"] or not all(isinstance(n, str) and _NAME.match(n) for n in s["names"]):
        errors.append("server names: one or more server folder names (letters, digits, . _ -)")
    if s["systemctl"] not in ("systemctl", "systemctl --user"):
        errors.append("server systemctl: 'systemctl' or 'systemctl --user'")
    s["allow_unsecured"] = bool(s["allow_unsecured"])
    s["label"] = str(s.get("label") or s["id"])[:80]
    if errors:
        raise SettingsError(errors)
    return s


def validate(data: dict) -> dict:
    errors = []
    d = _merge(DEFAULTS, data or {})
    if set(d) - set(DEFAULTS):
        errors.append("unknown settings: " + ", ".join(sorted(set(d) - set(DEFAULTS))))
    for k in DEFAULTS["paths"]:
        if not isinstance(d["paths"].get(k), str):
            errors.append(f"paths.{k}: text")
    ai = d["ai"]
    if ai["provider"] not in ("anthropic", "ollama", "none"):
        errors.append("ai.provider: anthropic, ollama or none")
    if ai["autonomy"] not in AUTONOMY:
        errors.append("ai.autonomy: " + ", ".join(AUTONOMY))
    try:
        ai["max_steps"] = max(5, min(200, int(ai["max_steps"])))
    except (TypeError, ValueError):
        errors.append("ai.max_steps: a number")
    for k in ("anthropic_base_url", "ollama_url"):
        if not re.match(r"^https?://[^\s]+$", str(ai[k])):
            errors.append(f"ai.{k}: an http(s) URL")
    servers, ids = [], set()
    for s in d["servers"] if isinstance(d["servers"], list) else []:
        try:
            v = validate_server(s)
        except SettingsError as e:
            errors.extend(e.errors)
            continue
        if v["id"] in ids:
            errors.append(f"server id '{v['id']}' is used twice")
        ids.add(v["id"])
        servers.append(v)
    d["servers"] = servers
    if d["deploy"]["mode"] not in ("stage", "restart", "files"):
        errors.append("deploy.mode: stage, restart or files")
    q = d["qc"]
    q["enabled"] = bool(q.get("enabled"))
    q["auto_rebuild"] = bool(q.get("auto_rebuild"))
    try:
        q["interval_hours"] = max(1, min(168, int(q.get("interval_hours"))))
        q["max_rebuilds"] = max(0, min(1000, int(q.get("max_rebuilds"))))
    except (TypeError, ValueError):
        errors.append("qc.interval_hours / qc.max_rebuilds: numbers")
    m = d["mcp"]
    m["enabled"] = bool(m.get("enabled"))
    if not isinstance(m.get("ncwe_launch"), bool):
        errors.append("mcp.ncwe_launch: boolean")
    if m.get("approval") not in ("client", "studio"):
        errors.append("mcp.approval: client or studio")
    try:
        m["call_seconds"] = max(10, min(600, int(m.get("call_seconds"))))
    except (TypeError, ValueError):
        errors.append("mcp.call_seconds: a number")
    if not isinstance(m.get("shell"), bool):
        errors.append("mcp.shell: true or false")
    t = d["terminal"]
    if t.get("shell") not in ("auto", "powershell", "cmd", "bash", "sh"):
        errors.append("terminal.shell: auto, powershell, cmd, bash or sh")
    if errors:
        raise SettingsError(errors)
    return d


class Settings:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        try:
            return validate(raw)
        except SettingsError:
            # keep what is valid: fall back section by section
            out = copy.deepcopy(DEFAULTS)
            for k in ("paths", "ai", "deploy", "build", "mcp", "qc", "terminal"):
                try:
                    out = validate(_merge(out, {k: raw.get(k, {})}))
                except SettingsError:
                    pass
            good = []
            for s in raw.get("servers", []) if isinstance(raw.get("servers"), list) else []:
                try:
                    good.append(validate_server(s))
                except SettingsError:
                    pass
            out["servers"] = good
            return out

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def get(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, patch: dict) -> dict:
        with self.lock:
            new = validate(_merge(self.data, patch))
            self.data = new
            self.save()
            return copy.deepcopy(new)

    def server(self, sid: str) -> dict | None:
        with self.lock:
            for s in self.data["servers"]:
                if s["id"] == sid:
                    return copy.deepcopy(s)
        return None

    def put_server(self, s: dict) -> dict:
        v = validate_server(s)
        with self.lock:
            servers = list(self.data["servers"])
            for i, x in enumerate(servers):
                if x["id"] == v["id"]:
                    servers[i] = v
                    break
            else:
                servers.append(v)
            self.data["servers"] = servers
            self.save()
        return v

    def delete_server(self, sid: str) -> bool:
        with self.lock:
            before = len(self.data["servers"])
            self.data["servers"] = [x for x in self.data["servers"] if x["id"] != sid]
            self.save()
            return len(self.data["servers"]) != before
