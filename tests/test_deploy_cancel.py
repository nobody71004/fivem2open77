"""Cancelling a deploy (studio/deploy.py, studio/jobs.py) against an SSH server in the test process
(tests/sshstub.py): a cancel stops an upload on a slow link at once and removes what was sent; one
that comes while the server installs waits for the end of that (a server stopped half-way through
installing could be left broken), and stops before the next server folder."""
import os
import threading
import time

import pytest

from fivem2open77.studio import deploy as D
from fivem2open77.studio.agent.tools import Context, ToolError
from fivem2open77.studio.jobs import EventBus, JobManager


def _log(job) -> str:
    return "\n".join(l["text"] for l in job.lines)


def _wait_for(cond, timeout, what="the condition"):
    t0 = time.time()
    while not cond():
        if time.time() - t0 > timeout:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _script(tmp_path, name, body) -> str:
    p = tmp_path / "scripts" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    return str(p)


@pytest.fixture
def sshstub():
    """The SSH server class (tests/sshstub.py); skipped where paramiko is not installed."""
    pytest.importorskip("paramiko", reason="the SSH tests need paramiko (bundled with the Studio)")
    from sshstub import SshStub
    return SshStub


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """The stub's "server": the upload folders go under tmp_path/remote."""
    root = tmp_path / "remote"
    root.mkdir()
    monkeypatch.setattr(D, "REMOTE_DIR", str(root / "f2o77-studio"))
    return root


def test_a_cancel_stops_a_slow_upload_at_once(tmp_path, monkeypatch, remote, sshstub):
    stub = sshstub(write_delay=0.05)                  # ~640 KiB/s: the 6 MiB bundle would take ~10 s
    try:
        fake = _script(tmp_path, "server_deploy.sh", "echo installed; exit 0")
        monkeypatch.setattr(D, "deploy_script", lambda: fake)
        bundle = tmp_path / "bundle.tgz"
        bundle.write_bytes(os.urandom(6 << 20))
        jobs = JobManager(EventBus())
        server = stub.server()
        job = jobs.submit("deploy", "Deploy", lambda job: D.deploy(server, stub.secrets(), str(bundle),
                                                                      names=["open77-test"], log=job.log, job=job))
        _wait_for(lambda: stub.written > (512 << 10), 30, "the upload")
        t0 = time.time()
        assert job.cancel()
        jobs.wait(job, 20)
        took = time.time() - t0
        log = _log(job)
        assert job.state == "cancelled", (job.state, job.error, log[-2000:])
        assert took < 5, f"the cancel took {took:.1f} s"
        assert stub.written < bundle.stat().st_size, "the upload went on to the end"
        assert not any("server_deploy.sh" in c and " deploy " in c for c in stub.commands), stub.commands
        assert "cancelled while uploading: nothing was installed" in log, log
        assert "removed the partial upload from the server" in log, log
        _wait_for(lambda: not list(remote.iterdir()), 5, "the upload folder to go")
    finally:
        stub.close()


def test_a_cancel_while_the_server_installs_waits_for_the_end(tmp_path, monkeypatch, remote, sshstub):
    stub = sshstub()
    try:
        fake = _script(tmp_path, "server_deploy.sh", 'echo "installing"; sleep 2; echo "  LIVE: fakecar"; exit 0')
        monkeypatch.setattr(D, "deploy_script", lambda: fake)
        bundle = tmp_path / "bundle.tgz"
        bundle.write_bytes(os.urandom(64 << 10))
        jobs = JobManager(EventBus())
        server = stub.server()
        job = jobs.submit("deploy", "Deploy", lambda job: D.deploy(server, stub.secrets(), str(bundle),
                                                                      names=["open77-test"], log=job.log, job=job))
        _wait_for(lambda: "installing" in _log(job), 30, "the install")
        t0 = time.time()
        assert job.cancel()
        assert job.summary()["stopping"] and job.cancel_late == "the install on the server"
        assert not job.cancel_event.is_set()
        jobs.wait(job, 20)
        log = _log(job)
        assert job.state == "done", (job.state, job.error, log[-2000:])
        assert time.time() - t0 > 1.0, "the install was cut"
        assert job.result["live"] == ["  LIVE: fakecar"], job.result
        assert "cancel requested during the install on the server" in log, log
        assert "finished: the cancel came during the install on the server, which completed" in log, log
        assert not list(remote.iterdir()), "the upload folder is removed as after any deploy"
    finally:
        stub.close()


def test_a_cancel_before_the_install_installs_nothing(tmp_path, monkeypatch, remote, sshstub):
    """Cancelled right as the upload ends: the install script never starts."""
    stub = sshstub()
    try:
        fake = _script(tmp_path, "server_deploy.sh", "echo installed; exit 0")
        monkeypatch.setattr(D, "deploy_script", lambda: fake)
        bundle = tmp_path / "bundle.tgz"
        bundle.write_bytes(os.urandom(64 << 10))
        jobs = JobManager(EventBus())
        server = stub.server()
        real_put = D.Remote.put
        holder = {}

        def put(self, local, remote_path, progress=None):
            real_put(self, local, remote_path, progress)
            if remote_path.endswith("bundle.tgz"):
                holder["job"].cancel()                 # the cancel lands as the last block is acknowledged

        def fn(job):
            holder["job"] = job
            return D.deploy(server, stub.secrets(), str(bundle), names=["open77-test"], log=job.log, job=job)

        monkeypatch.setattr(D.Remote, "put", put)
        job = jobs.submit("deploy", "Deploy", fn)
        jobs.wait(job, 20)
        log = _log(job)
        assert job.state == "cancelled", (job.state, log)
        assert not any("server_deploy.sh" in c and " deploy " in c for c in stub.commands), stub.commands
        assert "cancelled before the server installed anything" in log, log
        _wait_for(lambda: not list(remote.iterdir()), 5, "the upload folder to go")
    finally:
        stub.close()


def test_a_cancel_during_one_server_folder_stops_before_the_next(tmp_path, monkeypatch, remote, sshstub):
    stub = sshstub()
    try:
        run_sh = _script(tmp_path, "map_deploy.sh",
                         'echo "start $1"; sleep 1.5; echo \'MAP-DEPLOY-RESULT {"ok": true}\'; exit 0')
        other = _script(tmp_path, "other.sh", "exit 0")
        monkeypatch.setattr(D, "remote_script", lambda name: run_sh if name == "map_deploy.sh" else other)
        monkeypatch.setattr(D, "deploy_script", lambda: other)
        bundle = tmp_path / "map.tgz"
        bundle.write_bytes(os.urandom(64 << 10))
        jobs = JobManager(EventBus())
        server = stub.server(names=("a", "b"))
        job = jobs.submit("deploy", "Deploy a map", lambda job: D.deploy_map(
            server, stub.secrets(), str(bundle), names=["a", "b"], log=job.log, job=job))
        _wait_for(lambda: "start a" in _log(job), 30, "folder a")
        assert job.cancel()
        jobs.wait(job, 20)
        log = _log(job)
        assert job.state == "cancelled", (job.state, job.error, log)
        assert "cancelled: installed on a, not on b" in log, log
        assert sum("bash run.sh" in c for c in stub.commands) == 1, stub.commands
        assert not list(remote.iterdir())
    finally:
        stub.close()


def test_stale_upload_folders_are_swept(tmp_path, monkeypatch, remote, sshstub):
    """A folder a stopped deploy could not remove (the network was gone) goes with a later deploy."""
    stub = sshstub()
    try:
        fake = _script(tmp_path, "server_deploy.sh", "exit 0")
        monkeypatch.setattr(D, "deploy_script", lambda: fake)
        old = remote / "f2o77-studio-deadbeef"
        old.mkdir()
        (old / "bundle.tgz").write_bytes(b"x" * 100)
        recent = remote / "f2o77-studio-cafecafe"           # another deploy of now: left alone
        recent.mkdir()
        t = time.time() - (D.STALE_UPLOADS_MIN + 60) * 60
        os.utime(old, (t, t))
        bundle = tmp_path / "b.tgz"
        bundle.write_bytes(b"y" * 100)
        D.deploy(stub.server(), stub.secrets(), str(bundle), names=["open77-test"], log=lambda *_: None)
        assert sorted(p.name for p in remote.iterdir()) == ["f2o77-studio-cafecafe"]
    finally:
        stub.close()


def test_job_shield():
    jobs = JobManager(EventBus())

    # a cancel during the step waits for it; nothing after it: the job is done
    inside, go = threading.Event(), threading.Event()

    def step_only(job):
        with job.shield("the step"):
            inside.set()
            go.wait(5)
        return {"ok": 1}

    j = jobs.submit("t", "t", step_only)
    assert inside.wait(5)
    assert j.cancel() and j.cancel() and j.cancel_late == "the step" and not j.cancel_event.is_set()
    assert j.summary()["stopping"]
    go.set()
    jobs.wait(j, 5)
    assert j.state == "done" and j.result == {"ok": 1}
    assert sum("cancel requested during the step" in l["text"] for l in j.lines) == 1

    # what comes after the step does not run
    inside, go, ran = threading.Event(), threading.Event(), []

    def step_then_more(job):
        with job.shield("the step"):
            inside.set()
            go.wait(5)
        job.check()
        ran.append(1)

    j = jobs.submit("t", "t", step_then_more)
    assert inside.wait(5)
    j.cancel()
    go.set()
    jobs.wait(j, 5)
    assert j.state == "cancelled" and not ran
    assert "stopping now: the step is over" in _log(j)

    # nested steps: the cancel applies when the outer one ends
    inside, go, seen = threading.Event(), threading.Event(), []

    def nested(job):
        with job.shield("outer"):
            with job.shield("inner"):
                inside.set()
                go.wait(5)
            seen.append(job.cancel_event.is_set())
        job.check()

    j = jobs.submit("t", "t", nested)
    assert inside.wait(5)
    j.cancel()
    go.set()
    jobs.wait(j, 5)
    assert j.state == "cancelled" and seen == [False] and j.cancel_late == "inner"

    # outside a step: at once, and the callbacks run
    called = []

    def loop(job):
        job.on_cancel.append(lambda: called.append(1))
        while True:
            job.check()
            time.sleep(0.01)

    j = jobs.submit("t", "t", loop)
    time.sleep(0.1)
    t0 = time.time()
    assert j.cancel()
    jobs.wait(j, 5)
    assert j.state == "cancelled" and called == [1] and time.time() - t0 < 1
    assert not j.cancel()                                  # over: nothing to cancel


def test_make_bundle_stops_when_cancelled(tmp_path):
    d = tmp_path / "car"
    d.mkdir()
    (d / "open77.lua").write_text("-- x")
    ev = threading.Event()
    ev.set()
    with pytest.raises(D.DeployCancelled):
        D.make_bundle(str(tmp_path / "b.tgz"), resource_dirs={"car": str(d)}, cancel=ev)


def test_a_tool_stopped_during_a_step_that_cant_stop_waits_for_the_job():
    """The terminal's ^C (an agent's stop) during a server install: the tool answers when the job ends."""
    jobs = JobManager(EventBus())
    inside = threading.Event()

    def fn(job):
        with job.shield("the install on the server"):
            inside.set()
            time.sleep(0.6)
        return {"installed": True}

    class Session:
        stopping = False
        soft_timeout = None

    s = Session()
    ctx = Context(None, s)
    j = jobs.submit("deploy", "d", fn)
    assert inside.wait(5)
    s.stopping = True
    assert ctx.wait_job(j, 10) == {"installed": True}

    # outside such a step it stops at once, as before
    k = jobs.submit("deploy", "d", lambda job: [job.check() or time.sleep(0.01) for _ in range(1000)])
    time.sleep(0.05)
    with pytest.raises(ToolError, match="stopped by the user"):
        ctx.wait_job(k, 10)
    jobs.wait(k, 5)
    assert k.state == "cancelled"
