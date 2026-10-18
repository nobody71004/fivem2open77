"""An SSH server inside the test process (paramiko): password login, commands run with bash, and
SFTP onto the local disk, with a delay per written block so an upload is as slow as a bad link."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import uuid

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface


class _Handle(SFTPHandle):
    def __init__(self, f, stub):
        super().__init__(0)
        self.f = f
        self.stub = stub

    def write(self, offset, data):
        if self.stub.write_delay:
            time.sleep(self.stub.write_delay)
        self.f.seek(offset)
        self.f.write(data)
        self.stub.written += len(data)
        return paramiko.SFTP_OK

    def close(self):
        try:
            self.f.close()
        finally:
            super().close()

    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.f.fileno()))


class _Sftp(SFTPServerInterface):
    def __init__(self, server, stub, *a, **kw):
        super().__init__(server, *a, **kw)
        self.stub = stub

    def open(self, path, flags, attr):
        try:
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o644)
            f = os.fdopen(fd, "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb")
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return _Handle(f, self.stub)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    lstat = stat


class _Iface(paramiko.ServerInterface):
    def __init__(self, stub):
        self.stub = stub

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = (username, password) == (self.stub.user, self.stub.password)
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        cmd = command.decode("utf-8", "replace") if isinstance(command, bytes) else str(command)
        self.stub.commands.append(cmd)
        threading.Thread(target=self.stub._exec, args=(channel, cmd), daemon=True).start()
        return True


class SshStub:
    """SshStub(write_delay=0.05): .port, .user, .password; .commands (every command run), .written
    (bytes written over SFTP), .connections; close()."""

    def __init__(self, write_delay=0.0):
        self.user = "tester"
        self.password = "pw-" + uuid.uuid4().hex
        self.write_delay = write_delay
        self.written = 0
        self.commands = []
        self.connections = 0
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.closed = False
        self.transports = []
        threading.Thread(target=self._accept, daemon=True, name="sshstub-accept").start()

    def server(self, sid="stub", names=("open77-test",)) -> dict:
        return {"id": sid, "label": "Stub", "host": "127.0.0.1", "port": self.port, "user": self.user,
                "auth": "password", "names": list(names), "servers_root": "/nonexistent", "systemctl": "systemctl"}

    def secrets(self, sid="stub") -> dict:
        return {f"server:{sid}:password": self.password}

    def _accept(self):
        while not self.closed:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True, name="sshstub-conn").start()

    def _serve(self, c):
        t = paramiko.Transport(c)
        self.transports.append(t)
        t.add_server_key(self.key)
        t.set_subsystem_handler("sftp", SFTPServer, _Sftp, self)
        try:
            t.start_server(server=_Iface(self))
        except (paramiko.SSHException, EOFError, OSError):
            return

    def _exec(self, chan, cmd):
        rc = 255
        try:
            p = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL)
            for chunk in iter(lambda: p.stdout.read1(4096), b""):
                chan.sendall(chunk)
            rc = p.wait()
        except Exception:  # noqa: BLE001 - the client went away
            pass
        try:
            chan.send_exit_status(rc)
            chan.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
