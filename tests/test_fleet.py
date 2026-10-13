"""Open77 version drift across the servers and the one-click fleet upgrade (studio/fleet.py,
studio/deploy.py, studio/app.py).  The SSH calls are faked; the remote release check runs locally
against a temp release tree, and the API is exercised over real HTTP."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.studio import deploy as deploy_mod  # noqa: E402
from fivem2open77.studio import fleet  # noqa: E402
from fivem2open77.studio.app import Studio  # noqa: E402

from test_studio import _Studio  # noqa: E402

T = "2.31.21+op77.132"
OLD = "2.31.21+op77.131"


def srv(**kw):
    base = {"id": "vm", "label": "VM", "host": "10.0.0.5", "user": "root", "auth": "agent",
            "servers_root": "/opt", "systemctl": "systemctl", "names": ["open77-a", "open77-b"]}
    base.update(kw)
    return base


ROWS = [{"name": "open77-a", "version": OLD, "online": []},
        {"name": "open77-b", "version": T, "online": []}]


# ----------------------------------------------------------------------------- pure logic
def test_newest_is_the_highest_build():
    assert fleet.newest([OLD, "2.31.21+op77.99", "not a version!", None, T]) == T
    assert fleet.newest([]) is None and fleet.newest(None) is None


def test_parse_releases_reads_the_last_json_list():
    assert fleet.parse_releases("noise\n[\"" + OLD + "\"]\n") == [OLD]
    assert fleet.parse_releases("nothing here") is None
    assert fleet.parse_releases("") is None


def test_drift_states():
    assert fleet.drift(OLD, T) == "behind"
    assert fleet.drift(T, T) == "current"
    assert fleet.drift("2.31.21+op77.140", T) == "differs"      # newer than the staged target: left alone
    assert fleet.drift(None, T) == "unknown" and fleet.drift(T, None) == "unknown"


def test_plan_upgrades_only_the_folder_behind_the_target():
    names, skipped = fleet.plan(srv(), ROWS, T)
    assert names == ["open77-a"]
    assert skipped == [{"name": "open77-b", "reason": "already on " + T}]


def test_plan_leaves_busy_folders_for_an_empty_server():
    rows = [{"name": "open77-a", "version": OLD, "online": ["1.2.3.4:5"]},
            {"name": "open77-b", "version": OLD, "online": []}]
    names, skipped = fleet.plan(srv(), rows, T)
    assert names == ["open77-b"]
    assert skipped[0]["name"] == "open77-a" and "player" in skipped[0]["reason"]


def test_plan_skips_unknown_newer_and_missing_folders():
    rows = [{"name": "open77-a", "version": None, "online": []},
            {"name": "open77-b", "version": "2.31.21+op77.140", "online": []}]
    names, skipped = fleet.plan(srv(), rows, T)
    assert names == []
    reasons = {x["name"]: x["reason"] for x in skipped}
    assert "unknown" in reasons["open77-a"] and "not older" in reasons["open77-b"]
    names, skipped = fleet.plan(srv(), [], T)
    assert names == [] and all("no status" in x["reason"] for x in skipped)


def test_plan_refuses_layouts_the_host_tool_cannot_drive():
    for bad in (srv(servers_root="/home/op77"), srv(systemctl="systemctl --user")):
        names, skipped = fleet.plan(bad, ROWS, T)
        assert names == [] and len(skipped) == 2
    assert fleet.server_blockers(srv(servers_root="/home/op77")) and fleet.server_blockers(srv()) is None


def test_describe_shows_each_folder_and_the_target():
    d = fleet.describe(srv(), [ROWS[0]], T)
    assert d["target"] == T and d["blocked"] is None
    a, b = d["folders"]
    assert a == {"name": "open77-a", "version": OLD, "drift": "behind", "online": 0}
    assert b == {"name": "open77-b", "version": None, "drift": "unknown", "online": None}


# ----------------------------------------------------------------------------- remote release check
def test_release_check_script_reads_the_staged_tree(tmp_path):
    root = tmp_path / "releases"
    for ver in (OLD, T):
        d = root / ver
        d.mkdir(parents=True)
        (d / "package-manifest.json").write_text(json.dumps({"version": ver}), encoding="utf-8")
    (root / "tools").mkdir()
    (root / "half").mkdir()
    (root / "half" / "package-manifest.json").write_text("{not json", encoding="utf-8")
    code = deploy_mod.RELEASES_PY.replace(fleet.RELEASE_ROOT, root.as_posix())
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    got = fleet.parse_releases(out.stdout)
    assert sorted(got) == [OLD, T], got
    assert fleet.newest(got) == T


# ----------------------------------------------------------------------------- refusals before any SSH
def test_upgrade_refuses_before_connecting():
    with pytest.raises(deploy_mod.DeployError, match="not a version"):
        deploy_mod.upgrade(srv(), None, "2.31; rm -rf /", ["open77-a"])
    with pytest.raises(deploy_mod.DeployError, match="is not a server"):
        deploy_mod.upgrade(srv(), None, T, ["open77-zzz"])
    with pytest.raises(deploy_mod.DeployError, match="not under /opt"):
        deploy_mod.upgrade(srv(servers_root="/home/op77"), None, T, ["open77-a"])


def test_fleet_routes_need_confirm_for_the_upgrade():
    st = _Studio()
    try:
        code, r = st.req("POST", "/api/fleet/upgrade", {})
        assert code == 400 and "confirm" in json.dumps(r), (code, r)
        code, r = st.req("POST", "/api/fleet/upgrade", {"confirm": "yes"})
        assert code == 400, r
        assert st.req("POST", "/api/fleet/bogus", {})[0] == 404
        assert st.req("POST", "/api/fleet/status", {}, token=False)[0] == 401
    finally:
        shutil.rmtree(st.ws, ignore_errors=True)


# ----------------------------------------------------------------------------- the fleet job, SSH faked
def test_fleet_upgrade_job_plans_skips_and_reports(monkeypatch):
    ws = tempfile.mkdtemp(prefix="f2o77-fleet-")
    studio = Studio(ws)
    try:
        studio.settings.put_server({"id": "a", "label": "Alpha", "host": "10.0.0.5", "user": "root", "auth": "agent",
                                    "servers_root": "/opt", "names": ["open77-a"], "systemctl": "systemctl"})
        studio.settings.put_server({"id": "b", "label": "Beta", "host": "10.0.0.6", "user": "root", "auth": "agent",
                                    "servers_root": "/opt", "names": ["open77-b"], "systemctl": "systemctl"})
        staged = {"10.0.0.5": [OLD, T], "10.0.0.6": []}
        calls = []
        monkeypatch.setattr(deploy_mod, "releases", lambda server, secrets: staged[server["host"]])
        monkeypatch.setattr(deploy_mod, "status", lambda server, secrets, names=None, log=None:
                            [{"name": "open77-a", "version": OLD, "online": []}])

        def fake_upgrade(server, secrets, target, names, log=print):
            calls.append((server["id"], target, list(names)))
            return 0

        monkeypatch.setattr(deploy_mod, "upgrade", fake_upgrade)
        job = studio.fleet_job("upgrade", confirm=True)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "done", job.error
        assert calls == [("a", T, ["open77-a"])], calls            # Beta has nothing staged: not touched
        assert job.result["upgraded"] == [{"server": "a", "target": T, "names": ["open77-a"]}], job.result
        assert job.result["skipped"] == [{"server": "b", "reason": "no release staged on the host"}], job.result

        # a failing run is reported as a failed job, after the others were tried
        monkeypatch.setattr(deploy_mod, "upgrade", lambda server, secrets, target, names, log=print: 3)
        job = studio.fleet_job("upgrade", confirm=True)
        studio.jobs.wait(job, timeout=30)
        assert job.state == "failed" and "exited 3" in job.error, (job.state, job.error)

        # the upgrade refuses to run without the confirmation
        with pytest.raises(ValueError, match="confirm"):
            studio.fleet_job("upgrade")
    finally:
        studio.close()
        shutil.rmtree(ws, ignore_errors=True)
