"""NCWE adapter tests use a real stdio process without requiring a beta account."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from fivem2open77.studio.agent import ncwe
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


@pytest.mark.parametrize("mode", ["timeout", "cancel", "exit"])
def test_failed_calls_never_claim_success_and_reap(tmp_path, mode):
    with ncwe.StdioClient(fake_server(tmp_path), timeout=0.2) as client:
        client.initialize()
        if mode == "cancel": client.stopping = lambda: True
        with pytest.raises(ToolError, match={"timeout": "timed out", "cancel": "stopped", "exit": "exited"}[mode]):
            client.rpc("exit" if mode == "exit" else "hang")
        proc = client.proc
    assert proc.poll() is not None
