"""NCWE's native toolset through its own stdio MCP server; no private engine reimplementation.

Catalogue captured with NCWE Studio beta.3's NCWE.Mcp.exe --list-tools. Only the public schemas
are bundled, never NCWE binaries/game data/account credentials. Calls attach to the user's
editor unless they explicitly enable launching it. NCWE retains its own sign-in/access checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from .tools import Tool, ToolError

CATALOGUE = json.loads(Path(__file__).with_name("ncwe_tools.json").read_text(encoding="utf-8"))["tools"]
NCWE_TOOLS = tuple("ncwe_" + t["name"] for t in CATALOGUE)
# Export and raw commands can write arbitrary files or alter editor state. Mutations all use
# the strongest existing gate; readOnlyHint is used only for the shipped, reviewed catalogue.
READ_TOOLS = frozenset(t["name"] for t in CATALOGUE if t.get("annotations", {}).get("readOnlyHint"))


def command(settings):
    path = settings["paths"].get("ncwe_mcp") or os.environ.get("NCWE_MCP_EXE", "")
    if not path or not Path(path).is_file():
        raise ToolError("NCWE is not configured: Settings > Agents > NCWE MCP executable. "
                        "Use NCWE.Mcp.exe from your NCWE installation; keep the signed-in editor open.")
    args = [str(Path(path).resolve())]
    if not settings["mcp"].get("ncwe_launch", False):
        args.append("--attach-only")
    return args


class StdioClient:
    """One isolated MCP connection per call, always reaped; no shell or executable arguments from tools."""
    def __init__(self, cmd, stopping=lambda: False, timeout=180):
        self.cmd, self.stopping, self.timeout = cmd, stopping, timeout
        self.proc = None
        self.responses = queue.Queue(maxsize=256)
        self.counter = 0
        self.errors = []

    def __enter__(self):
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=str(Path(self.cmd[0]).parent),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise ToolError(f"NCWE MCP could not start: {e}") from e
        def stdout():
            for raw in self.proc.stdout:
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                try:
                    self.responses.put(value, timeout=1)
                except queue.Full:
                    break
            try:
                self.responses.put(None, timeout=1)
            except queue.Full:
                pass
        def stderr():
            for raw in self.proc.stderr:
                self.errors.append(raw.decode("utf-8", "replace")[:500])
                self.errors[:] = self.errors[-10:]
        self.readers = [threading.Thread(target=fn, daemon=True) for fn in (stdout, stderr)]
        for t in self.readers:
            t.start()
        return self

    def __exit__(self, *_):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()
            for t in self.readers:
                t.join(timeout=1)

    def rpc(self, method, params=None, notify=False):
        self.counter += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = self.counter
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError as e:
            raise ToolError("NCWE MCP closed its input") from e
        if notify:
            return None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.stopping():
                raise ToolError("NCWE call stopped by user")
            try:
                reply = self.responses.get(timeout=0.1)
            except queue.Empty:
                continue
            if reply is None:
                raise ToolError("NCWE MCP exited: " + "".join(self.errors)[-1200:])
            if reply.get("id") != msg["id"]:
                continue
            if "error" in reply:
                raise ToolError("NCWE MCP: " + str(reply["error"].get("message", reply["error"])))
            return reply.get("result", {})
        raise ToolError(f"NCWE {method} timed out after {self.timeout}s; no success is assumed")

    def initialize(self):
        from ..app import APP_VERSION        # noqa: PLC0415 (the Studio imports this module)
        result = self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "fivem2open77-studio", "version": APP_VERSION}})
        self.rpc("notifications/initialized", notify=True)
        return result


def call(ctx, name, args):
    with StdioClient(command(ctx.studio.settings.get()), stopping=lambda: ctx.session.stopping) as client:
        client.initialize()
        offered = client.rpc("tools/list").get("tools", [])
        if name not in {t.get("name") for t in offered}:
            raise ToolError(f"Installed NCWE does not expose {name}; update NCWE or use its available tools")
        result = client.rpc("tools/call", {"name": name, "arguments": args})
    blocks = []
    for b in result.get("content", []):
        if b.get("type") == "text":
            blocks.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "image" and b.get("mimeType") in ("image/png", "image/jpeg"):
            blocks.append({"type": "image", "png": b["data"], "mimeType": b["mimeType"]})
        elif b.get("type") == "resource":
            resource = b.get("resource", {})
            blocks.append({"type": "text", "text": resource.get("text") or json.dumps(resource)})
    if result.get("isError"):
        raise ToolError("NCWE: " + "\n".join(b["text"] for b in blocks if b["type"] == "text"))
    return blocks or [{"type": "text", "text": json.dumps(result)}]


def _mesh_asset(blocks):
    """The custom mesh NCWE assigned: the reply's mesh.asset (tolerant to the reply's shape)."""
    for b in blocks:
        if b.get("type") != "text":
            continue
        t = b.get("text") or ""
        try:
            data = json.loads(t)
        except ValueError:
            data = None
        if isinstance(data, dict):
            mesh = data.get("mesh")
            if isinstance(mesh, dict) and mesh.get("asset"):
                return str(mesh["asset"])
            if data.get("asset"):
                return str(data["asset"])
        m = re.search(r'"([A-Za-z0-9_\\/.:-]+\.mesh)"', t)
        if m:
            return m.group(1)
    return None


def _placed_ids(blocks):
    for b in blocks:
        if b.get("type") != "text":
            continue
        try:
            data = json.loads(b.get("text") or "")
        except ValueError:
            continue
        if isinstance(data, dict):
            ids = data.get("ids") if isinstance(data.get("ids"), list) else data.get("id")
            if ids is not None:
                return ids if isinstance(ids, list) else [ids]
    return []


def import_model_glb(ctx, path, *, name=None, position=None, yaw=0.0, place=True, height=None,
                     analyze_only=False):
    """Import a model file into NCWE's custom mesh library and place it in the world.

    Our map exports are glTF .glb (Y up, metres) — NCWE's import_model reads them straight.
    Placement goes to Night City ``position`` ([x, y, z]; [x, y] drops it on the surface below) at
    ``yaw`` degrees. Returns {asset, placed, import, place}: the mesh asset NCWE assigned, the new
    object ids, and the raw replies.
    """
    if not os.path.isfile(path):
        raise ToolError(f"no such file: {path}")
    if place and position is None and not analyze_only:
        raise ToolError("nowhere to place it: pass position [x, y, z] (or [x, y] to drop it on the surface below)")
    args = {"path": os.path.abspath(path), "up_axis": "Y"}
    if name:
        args["name"] = name
    if height:
        args["height"] = float(height)
    if analyze_only:
        args["analyze_only"] = True
    blocks = call(ctx, "import_model", args)
    out = {"asset": _mesh_asset(blocks) if not analyze_only else None, "placed": [],
           "import": blocks}
    if analyze_only:
        return out
    if not out["asset"]:
        raise ToolError("NCWE imported the model but named no mesh asset to place; see the import reply")
    if place:
        item = {"asset": out["asset"], "position": [float(v) for v in position],
                "rotation": {"yaw": float(yaw or 0.0)}, "ground": "bottom", "collision": "box"}
        if name:
            item["name"] = name
        out["place"] = call(ctx, "place_objects", {"items": [item]})
        out["placed"] = _placed_ids(out["place"])
    return out


def registry():
    out = {}
    for spec in CATALOGUE:
        name = spec["name"]
        risk = "read" if name in READ_TOOLS else "deploy"
        fn = lambda ctx, args, name=name: call(ctx, name, args)
        tool = Tool("ncwe_" + name,
                    "NCWE editor (separate from Studio's conversion project). " + spec["description"],
                    spec["inputSchema"], risk, fn)
        out[tool.name] = tool
    return out
