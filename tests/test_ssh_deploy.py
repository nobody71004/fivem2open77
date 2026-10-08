"""The Studio's SSH deploy path (paramiko) against a real sshd and a fake Open77 server.

Opt-in: needs an sshd on 127.0.0.1:2222 whose user's PATH has the fake systemctl of
tests/test_server_deploy.py (see the F2O77_SSH_TEST_* variables below).  Skipped otherwise."""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ENV = {
    "host": os.environ.get("F2O77_SSH_TEST_HOST", "127.0.0.1"),
    "port": int(os.environ.get("F2O77_SSH_TEST_PORT", "2222")),
    "user": os.environ.get("F2O77_SSH_TEST_USER", "o77test"),
    "pwfile": os.environ.get("F2O77_SSH_TEST_PWFILE", "/tmp/claude-0/o77test.pw"),
    "root": os.environ.get("F2O77_SSH_TEST_ROOT", "/home/o77test/fakeenv/srv"),
    "build": os.environ.get("F2O77_SSH_TEST_BUILD", "/opt/f2o77/out2"),
}


def _ready():
    return os.path.isfile(ENV["pwfile"]) and os.path.isdir(ENV["build"])


def test_ssh_deploy_end_to_end():
    if not _ready():
        print("   (skipped: no test sshd / build)")
        return
    from fivem2open77.studio import deploy as D
    from fivem2open77.studio.app import Studio
    ws = tempfile.mkdtemp(prefix="f2o77-ssh-")
    try:
        studio = Studio(ws)
        server = studio.settings.put_server({
            "id": "local", "label": "Local test", "host": ENV["host"], "port": ENV["port"], "user": ENV["user"],
            "auth": "password", "servers_root": ENV["root"], "names": ["open77-test"], "systemctl": "systemctl"})
        studio.set_secret("server:local:password", open(ENV["pwfile"]).read().strip())

        t = D.test_connection(server, studio.secrets)
        assert t["ok"] and t["python3"] and t["servers"] == {"open77-test": True}, t
        assert os.path.isfile(os.path.join(ws, "known_hosts")), "host key remembered on first use"

        # wrong password: a clear error, no hang
        studio.set_secret("server:local:password", "wrong-password")
        try:
            D.test_connection(server, studio.secrets)
            raise AssertionError("bad password accepted")
        except D.DeployError as e:
            assert "authentication failed" in str(e), e
        studio.set_secret("server:local:password", open(ENV["pwfile"]).read().strip())

        # the hub, through the Studio's job system
        job = studio.server_job("local", "hub", mode="stage")
        studio.jobs.wait(job, 300)
        assert job.state == "done", (job.error, [l["text"] for l in job.lines][-15:])
        # first install: waits for a restart; already installed (a rerun): nothing to wait for
        assert any(("f2o77_hub" in l["text"] and "PENDING restart" in l["text"]) or "LIVE:" in l["text"] for l in job.lines)

        # restart so the hub "runs" (fake server): every resource in resources.load starts
        code = D.restart(server, studio.secrets, log=lambda *_: None)
        assert code == 0

        # the real i8 build, staged for the hub
        tmp = tempfile.mkdtemp()
        b = D.make_bundle(os.path.join(tmp, "car.tgz"), build_out=ENV["build"])
        lines = []
        res = D.deploy(server, studio.secrets, b["path"], mode="stage", log=lines.append)
        assert res["staged"] and "i8rpncs" in res["staged"][0], res
        shutil.rmtree(tmp, ignore_errors=True)

        st = D.status(server, studio.secrets)
        s = st[0]
        car = [c for c in s["cars"] if c["name"] == "i8rpncs"][0]
        assert car["record"] == "Vehicle.i8rpncs" and car["in_load"] and "i8rpncs.zip" in car["packages"], car
        assert s["hub"] and [p["name"] for p in s["hub"]["pending"]][0] == "i8rpncs", s["hub"]

        log = D.logs(server, studio.secrets, "open77-test", 20)
        assert "listening on UDP" in log

        # names outside the server entry are refused before anything runs
        try:
            D.logs(server, studio.secrets, "../../etc", 5)
            raise AssertionError("bad name accepted")
        except D.DeployError:
            pass
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    test_ssh_deploy_end_to_end()
    print("ok")
