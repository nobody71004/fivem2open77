"""A real OpenSSH server on 127.0.0.1 for the Studio's SSH tests (deploys, the live map link).

    with Sshd(env={"FAKE": ..., "PATH": ...}) as s:
        server = {"host": "127.0.0.1", "port": s.port, "user": s.user, "auth": "key", "key_file": s.key, ...}

Runs /usr/sbin/sshd -D on a free port with its own host key, a client key in authorized_keys and
`SetEnv` for the session environment (the fake systemctl on PATH, a test HOME...), so commands the
Studio runs over SSH see a fake Open77 host.  Needs root (sshd) and ssh-keygen: available() says.
"""
from __future__ import annotations

import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import time

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"


def available() -> bool:
    return os.name == "posix" and os.path.isfile(SSHD) and shutil.which("ssh-keygen") is not None and os.geteuid() == 0


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sshd:
    def __init__(self, env=None):
        self.dir = tempfile.mkdtemp(prefix="f2o77-sshd-")
        os.chmod(self.dir, 0o755)
        self.port = _free_port()
        self.user = pwd.getpwuid(os.geteuid()).pw_name
        self.key = os.path.join(self.dir, "client_ed25519")
        self.env = dict(env or {})
        self.proc = None

    def __enter__(self):
        d = self.dir
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(d, "host_ed25519")], check=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.key], check=True)
        shutil.copy(self.key + ".pub", os.path.join(d, "authorized_keys"))
        os.chmod(os.path.join(d, "authorized_keys"), 0o600)
        setenv = " ".join(f"{k}={v}" for k, v in self.env.items())
        cfg = os.path.join(d, "sshd_config")
        with open(cfg, "w") as f:
            f.write(f"""Port {self.port}
ListenAddress 127.0.0.1
HostKey {d}/host_ed25519
PidFile {d}/sshd.pid
AuthorizedKeysFile {d}/authorized_keys
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
StrictModes no
UsePAM no
AllowTcpForwarding yes
Subsystem sftp internal-sftp
LogLevel ERROR
""" + (f"SetEnv {setenv}\n" if setenv else ""))
        os.makedirs("/run/sshd", exist_ok=True)
        self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", cfg], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError("sshd did not start: " + self.proc.stderr.read().decode(errors="replace")[-800:])
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("sshd did not open its port")

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
