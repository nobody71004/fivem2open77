"""NCWE adapter tests use a real stdio process without requiring a beta account."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from fivem2open77.studio.agent import maptools, ncwe
from fivem2open77.studio.agent.tools import ToolError, registry, tools_for
from fivem2open77.studio.agent.manager import Session, build_system
from fivem2open77.studio.settings import validate, SettingsError
from fivem2open77.studio.mcp import guide, _content


def test_full_catalogue_and_permissions():
    tools = registry()
    assert len(ncwe.CATALOGUE) == len(ncwe.NCWE_TOOLS) == 60
    for kind in (None, "car", "map", "world"):
        assert set(ncwe.NCWE_TOOLS) <= set(tools_for(kind))
        assert len(tools_for(kind)) == len(set(tools_for(kind)))
        assert "NCWE integration" in build_system(kind)
    assert "ncwe_studio_status" in guide()
    for spec in ncwe.CATALOGUE:
        tool = tools["ncwe_" + spec["name"]]
        assert tool.schema == spec["inputSchema"]
        assert tool.risk == ("read" if spec["name"] in ncwe.READ_TOOLS else "deploy")
        session = SimpleNamespace(autonomy="auto_safe")
        assert Session._needs_approval(session, tool.risk) == (tool.risk != "read")
    assert tools["ncwe_studio_command"].risk == "deploy"
    assert tools["ncwe_export_mod"].risk == "deploy"


def test_ncwe_asset_tools_map_only():
    """ncwe_assets_* reach the agent and (through MAP_TOOLS) MCP clients: map projects, right risks."""
    names = {"ncwe_assets_list", "ncwe_assets_import", "ncwe_assets_verify"}
    tools = registry()
    assert names <= set(tools)
    assert names <= set(maptools.MAP_TOOLS)      # MCP gives project tools an extra project argument
    assert names <= set(tools_for("map"))
    for kind in (None, "car", "world"):
        assert not names & set(tools_for(kind))
    assert tools["ncwe_assets_list"].risk == "read"
    assert tools["ncwe_assets_verify"].risk == "read"
    assert tools["ncwe_assets_import"].risk == "change"


def test_settings_attach_only(tmp_path):
    exe = tmp_path / "NCWE.Mcp.exe"
    exe.touch()
    settings = validate({"paths": {"ncwe_mcp": str(exe)}})
    assert ncwe.command(settings) == [str(exe), "--attach-only"]
    settings["mcp"]["ncwe_launch"] = True
    assert ncwe.command(settings) == [str(exe)]
    with pytest.raises(ToolError, match="not configured"):
        ncwe.command(validate({"paths": {"ncwe_mcp": str(tmp_path / "missing")}}))
    with pytest.raises(SettingsError, match="ncwe_launch"):
        validate({"mcp": {"ncwe_launch": "false"}})


def fake_server(tmp_path):
    path = tmp_path / "fake_mcp.py"
    path.write_text('''import json, sys, time
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    method = m["method"]
    if method == "initialize": result = {"protocolVersion": "2024-11-05"}
    elif method == "tools/list": result = {"tools": [{"name": "screenshot"}, {"name": "studio_status"}]}
    elif method == "tools/call":
        if m["params"]["arguments"].get("fail"):
            result = {"isError": True, "content": [{"type": "text", "text": "beta sign-in required"}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(m["params"])},
                                  {"type": "image", "mimeType": "image/jpeg", "data": "YWJj"}]}
    elif method == "hang": time.sleep(5); continue
    elif method == "exit": sys.exit(3)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
''', encoding="utf-8")
    return [sys.executable, "-u", str(path)]


def test_stdio_handshake_notifications_errors_and_reaping(tmp_path):
    with ncwe.StdioClient(fake_server(tmp_path), timeout=2) as client:
        assert client.initialize()["protocolVersion"] == "2024-11-05"
        assert len(client.rpc("tools/list")["tools"]) == 2
        with pytest.raises(ToolError, match="unknown"):
            client.rpc("missing")
        proc = client.proc
    assert proc.poll() is not None
    assert all(not t.is_alive() for t in client.readers)


def test_call_forwards_exact_arguments_and_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(ncwe, "command", lambda _: fake_server(tmp_path))
    ctx = SimpleNamespace(studio=SimpleNamespace(settings=SimpleNamespace(get=lambda: {})),
                          session=SimpleNamespace(stopping=False))
    args = {"width": 1280, "note": "résumé"}
    blocks = ncwe.call(ctx, "screenshot", args)
    assert json.loads(blocks[0]["text"]) == {"name": "screenshot", "arguments": args}
    assert _content(blocks)[1] == {"type": "image", "data": "YWJj", "mimeType": "image/jpeg"}
    with pytest.raises(ToolError, match="beta sign-in required"):
        ncwe.call(ctx, "studio_status", {"fail": True})
    with pytest.raises(ToolError, match="does not expose"):
        ncwe.call(ctx, "nonexistent", {})


def fake_import_server(tmp_path):
    """A fake NCWE MCP with the two importer tools: it echoes every call's arguments back."""
    path = tmp_path / "fake_import_mcp.py"
    path.write_text('''import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    method = m["method"]
    if method == "initialize": result = {"protocolVersion": "2024-11-05"}
    elif method == "tools/list":
        result = {"tools": [{"name": "import_model"}, {"name": "place_objects"}]}
    elif method == "tools/call":
        name = m["params"]["name"]
        args = m["params"]["arguments"]
        if name == "import_model":
            reply = ({"would_import": {"height": 12.0, "materials": 3}, "echo": args}
                     if args.get("analyze_only") else {"mesh": {"asset": "base\\\\custom\\\\cookiemap.mesh"}, "echo": args})
        else:
            reply = {"ids": [7, 8], "echo": args}
        result = {"content": [{"type": "text", "text": json.dumps(reply)}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
''', encoding="utf-8")
    return [sys.executable, "-u", str(path)]


def _ctx():
    return SimpleNamespace(studio=SimpleNamespace(settings=SimpleNamespace(get=lambda: {})),
                           session=SimpleNamespace(stopping=False))


def test_import_model_glb_imports_and_places(tmp_path, monkeypatch):
    monkeypatch.setattr(ncwe, "command", lambda _: fake_import_server(tmp_path))
    glb = tmp_path / "cookiemap.glb"
    glb.write_bytes(b"glTF-fake")
    r = ncwe.import_model_glb(_ctx(), str(glb), name="Cookie", position=[10, 20, 30], yaw=90)
    assert r["asset"] == "base\\custom\\cookiemap.mesh", r
    assert r["placed"] == [7, 8], r
    import_args = json.loads(r["import"][0]["text"])["echo"]
    assert import_args == {"path": str(glb), "up_axis": "Y", "name": "Cookie"}, import_args
    place_args = json.loads(r["place"][0]["text"])["echo"]
    assert place_args == {"items": [{"asset": "base\\custom\\cookiemap.mesh",
                                     "position": [10.0, 20.0, 30.0], "rotation": {"yaw": 90.0},
                                     "ground": "bottom", "collision": "box", "name": "Cookie"}]}, place_args


def test_import_model_glb_analyze_place_false_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ncwe, "command", lambda _: fake_import_server(tmp_path))
    glb = tmp_path / "cookiemap.glb"
    glb.write_bytes(b"glTF-fake")
    r = ncwe.import_model_glb(_ctx(), str(glb), analyze_only=True)
    assert r["asset"] is None and "place" not in r
    assert json.loads(r["import"][0]["text"])["echo"] == {"path": str(glb), "up_axis": "Y", "analyze_only": True}
    r = ncwe.import_model_glb(_ctx(), str(glb), place=False)
    assert r["asset"] == "base\\custom\\cookiemap.mesh" and "place" not in r
    with pytest.raises(ToolError, match="nowhere to place"):
        ncwe.import_model_glb(_ctx(), str(glb))
    with pytest.raises(ToolError, match="no such file"):
        ncwe.import_model_glb(_ctx(), str(tmp_path / "missing.glb"), position=[0, 0])


def test_ncwe_import_map_tool_map_only():
    """ncwe_import_map: map projects only, at the NCWE mutation gate (like the ncwe_* editor tools)."""
    tools = registry()
    assert "ncwe_import_map" in tools and "ncwe_import_map" in maptools.MAP_TOOLS
    assert "ncwe_import_map" in tools_for("map")
    for kind in (None, "car", "world"):
        assert "ncwe_import_map" not in tools_for(kind)
    assert tools["ncwe_import_map"].risk == "deploy"


@pytest.mark.parametrize("mode", ["timeout", "cancel", "exit"])
def test_failed_calls_never_claim_success_and_reap(tmp_path, mode):
    with ncwe.StdioClient(fake_server(tmp_path), timeout=0.2) as client:
        client.initialize()
        if mode == "cancel": client.stopping = lambda: True
        with pytest.raises(ToolError, match={"timeout": "timed out", "cancel": "stopped", "exit": "exited"}[mode]):
            client.rpc("exit" if mode == "exit" else "hang")
        proc = client.proc
    assert proc.poll() is not None
