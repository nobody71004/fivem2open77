"""Registering the Studio's MCP server with the agent apps on this PC (Settings > Agents > Connect).

Each app keeps its MCP servers in its own file; the entry is named ``fivem2open77`` and runs the
bridge (``python -m fivem2open77 mcp``, bridge.py).  Files are edited in place: the previous file is
kept as ``<file>.f2o77-backup``, written atomically; JSON with comments is read (comments are not
kept).  Claude Code is registered through its own command (``claude mcp add --scope user``).

Microsoft Store (MSIX) builds of Claude Desktop, VS Code and Codex run in a package container: what
such an app writes under AppData lands in its own copy (``%LOCALAPPDATA%\\Packages\\<package>\\LocalCache``)
and where it has one, it reads that one.  So the entry goes into those copies too, and the bridge
such an app starts never starts the Studio itself (see bridge.py).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys

from .paths import app_home, default_workspace

NAME = "fivem2open77"
CLIENTS = ("claude_code", "claude_desktop", "codex", "cursor", "vscode")
LABELS = {"claude_code": "Claude Code", "claude_desktop": "Claude Desktop", "codex": "Codex", "cursor": "Cursor",
          "vscode": "VS Code (Copilot)"}


def bridge_command(workspace: str) -> dict:
    """{command, args} that starts the bridge for this workspace."""
    home = app_home()
    py = os.path.join(home, "runtime", "python", "python.exe") if home else None
    if not py or not os.path.isfile(py):
        py = sys.executable
    args = ["-X", "utf8", "-m", "fivem2open77", "mcp"]
    if os.path.abspath(workspace) != os.path.abspath(default_workspace()):
        args += ["--workspace", os.path.abspath(workspace)]
    return {"command": py, "args": args}


def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _localappdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def _windows() -> bool:
    return os.name == "nt"


# the package family names of the apps' Microsoft Store (MSIX) builds start with these
STORE_PACKAGES = {"claude_desktop": "Claude_", "vscode": "Microsoft.VisualStudioCode_", "codex": "OpenAI.Codex_"}


def store_packages(client: str) -> list:
    """The app's Microsoft Store (MSIX) package folders on this PC."""
    if not _windows() or client not in STORE_PACKAGES:
        return []
    return sorted(glob.glob(os.path.join(_localappdata(), "Packages", STORE_PACKAGES[client] + "*")))


def packaged_copies(client: str, path: str) -> list:
    """The app's own copies of an AppData file: a Store app's writes under AppData land in its package's
    LocalCache, and where it has such a copy it reads that one instead of the file in AppData."""
    out = []
    for base, sub in ((_appdata(), "Roaming"), (_localappdata(), "Local")):
        try:
            rel = os.path.relpath(path, base)
        except ValueError:                      # another drive
            continue
        if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            continue
        for pkg in store_packages(client):
            cand = os.path.join(pkg, "LocalCache", sub, rel)
            if os.path.isfile(cand):
                out.append(cand)
    return out


def config_path(client: str) -> str | None:
    home = os.path.expanduser("~")
    if client == "claude_desktop":
        if _windows():
            return os.path.join(_appdata(), "Claude", "claude_desktop_config.json")
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json")
        return os.path.join(home, ".config", "Claude", "claude_desktop_config.json")
    if client == "cursor":
        return os.path.join(home, ".cursor", "mcp.json")
    if client == "vscode":
        if _windows():
            return os.path.join(_appdata(), "Code", "User", "mcp.json")
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Application Support", "Code", "User", "mcp.json")
        return os.path.join(home, ".config", "Code", "User", "mcp.json")
    if client == "codex":
        return os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "config.toml")
    if client == "claude_code":
        return os.path.join(home, ".claude.json")
    return None


# ----------------------------------------------------------------------------- JSON with comments
def loads_lenient(text: str):
    """JSON that may have // and /* */ comments and trailing commas (as these apps allow)."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    s = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(s) if s.strip() else {}


def _write_atomic(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        shutil.copyfile(path, path + ".f2o77-backup")
    tmp = path + ".f2o77-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        data = loads_lenient(f.read())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


# ----------------------------------------------------------------------------- per client
def _json_key(client: str) -> str:
    return "servers" if client == "vscode" else "mcpServers"


def _entry(client: str, cmd: dict) -> dict:
    e = {"command": cmd["command"], "args": cmd["args"]}
    if client == "vscode":
        e = {"type": "stdio", **e}
    return e


def _codex_block(cmd: dict) -> str:
    def q(s):
        return "'" + s.replace("'", "''") + "'" if "'" not in s else json.dumps(s)
    return "\n".join([f"[mcp_servers.{NAME}]", f"command = {q(cmd['command'])}",
                      "args = [" + ", ".join(q(a) for a in cmd["args"]) + "]",
                      "startup_timeout_sec = 120", "tool_timeout_sec = 1800", ""])


_CODEX_RX = re.compile(r"(?ms)^\[mcp_servers\." + NAME + r"\]\s*\n.*?(?=^\[|\Z)")


def status(client: str, workspace: str) -> dict:
    cmd = bridge_command(workspace)
    path = config_path(client)
    out = {"client": client, "label": LABELS.get(client, client), "file": path, "state": "not_registered"}
    try:
        if client == "claude_code":
            out["cli"] = shutil.which("claude")
            data = _read_json(path) if path and os.path.isfile(path) else {}
            e = (data.get("mcpServers") or {}).get(NAME)
            if e:
                out["state"] = "registered" if e.get("command") == cmd["command"] and e.get("args") == cmd["args"] else "outdated"
            elif not out["cli"] and not os.path.isfile(path or ""):
                out["state"] = "not_installed"
            return out
        store = store_packages(client)
        if store:
            out["store_app"] = True
        if client == "codex":
            if not os.path.isfile(path):
                out["state"] = "not_installed" if not os.path.isdir(os.path.dirname(path)) and not store else "not_registered"
                return out
            with open(path, encoding="utf-8") as f:
                t = f.read()
            m = _CODEX_RX.search(t)
            if m:
                out["state"] = "registered" if m.group(0).strip() == _codex_block(cmd).strip() else "outdated"
            return out
        copies = packaged_copies(client, path)
        if copies:
            out["file"] = path = copies[0]              # the one the app reads
        if not os.path.isdir(os.path.dirname(path)) and not store:
            out["state"] = "not_installed"
            return out
        data = _read_json(path)
        e = (data.get(_json_key(client)) or {}).get(NAME)
        if e:
            out["state"] = "registered" if e.get("command") == cmd["command"] and e.get("args") == cmd["args"] else "outdated"
    except (OSError, ValueError) as e:
        out["state"] = "unreadable"
        out["error"] = str(e)
    return out


def status_all(workspace: str) -> list:
    return [status(c, workspace) for c in CLIENTS]


def connect(client: str, workspace: str, remove=False) -> dict:
    """Register (or remove) the Studio in an agent app.  Returns the new status (+ what was done)."""
    if client not in CLIENTS:
        raise ValueError("client: " + ", ".join(CLIENTS))
    cmd = bridge_command(workspace)
    path = config_path(client)
    if client == "claude_code":
        cli = shutil.which("claude")
        line = (["claude", "mcp", "remove", "--scope", "user", NAME] if remove else
                ["claude", "mcp", "add", "--scope", "user", NAME, "--", cmd["command"], *cmd["args"]])
        if not cli:
            return dict(status(client, workspace), done=False,
                        manual=" ".join(_shell_quote(x) for x in line),
                        note="the claude command is not on PATH here: run this in a terminal")
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}       # CREATE_NO_WINDOW
        subprocess.run([cli, "mcp", "remove", "--scope", "user", NAME], capture_output=True, timeout=60, **kw)
        r = None
        if not remove:
            r = subprocess.run([cli, *line[1:]], capture_output=True, text=True, timeout=60, **kw)
            if r.returncode != 0:
                raise RuntimeError(f"claude mcp add failed: {(r.stderr or r.stdout or '').strip()[-400:]}")
        return dict(status(client, workspace), done=True, note="start a new Claude Code session (or /mcp) to load it")
    if client == "codex":
        t = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                t = f.read()
        t2 = _CODEX_RX.sub("", t).rstrip() + "\n"
        if not remove:
            t2 = (t2 + "\n" if t2.strip() else "") + _codex_block(cmd)
        _write_atomic(path, t2)
        return dict(status(client, workspace), done=True, note="restart Codex to load it" + _store_note(client))
    key = _json_key(client)
    written = []
    for target in packaged_copies(client, path) + [path]:          # the app's own copies, and the file in AppData
        data = _read_json(target)
        servers = data.get(key)
        if not isinstance(servers, dict):
            servers = {}
        if remove:
            servers.pop(NAME, None)
        else:
            servers[NAME] = _entry(client, cmd)
        data[key] = servers
        _write_atomic(target, json.dumps(data, indent=2) + "\n")
        written.append(target)
    return dict(status(client, workspace), done=True, files=written,
                note={"claude_desktop": "quit and start Claude Desktop again to load it",
                      "cursor": "Cursor loads it on its own (Settings > MCP shows it)",
                      "vscode": "VS Code loads it on its own (MCP: List Servers)"}.get(client, "") + _store_note(client))


def _store_note(client: str) -> str:
    if not store_packages(client):
        return ""
    return ("; it is the Microsoft Store build, which cannot start the Studio: keep the Studio open while you use "
            "it there")


def _shell_quote(s: str) -> str:
    return f'"{s}"' if re.search(r"[\s'\"]", s) else s
