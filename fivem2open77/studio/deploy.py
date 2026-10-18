"""Getting builds onto Open77 servers.

A deploy bundle is a .tgz of resources/assets/<category>/<resource>/ folders: the car, its door
chime, and optionally the f2o77_hub resource and the ArchiveXL/TweakXL loader resources.  It is
copied to the server host over SSH together with server_deploy.sh, which does the installing on
the host (backups, resources.load, --check-config, then restart / stage for the hub / nothing).

Maps go the same way with map_deploy.sh (tools/deploy/remote) in front: the map and the map
manager f2o77_maps, and the map baker (a .NET runtime + the WolvenKit CLI + the baker's Python
package, ~/.f2o77/baker on the host) that rebuilds a map when it is moved in game.  The baker's
runtime is uploaded only when the host does not have that version yet.

SSH goes through paramiko (bundled with the app): key files, passwords and the SSH agent work
the same on Windows and Linux.  Passwords and key passphrases come from the secret store.

A Studio job's cancel stops a deploy while it uploads: the SSH connection is dropped at once (an
SFTP write waiting on a slow link would otherwise finish the upload first), nothing was installed,
and the partial upload is removed over a new connection.  Once the server runs the install script,
a cancel waits for its end: a server stopped half-way through installing could be left broken.
"""
from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shlex
import socket
import struct
import tarfile
import threading
import time
import uuid

from . import fleet
from .paths import deploy_script, hub_dir, remote_script

REMOTE_DIR = "/tmp/f2o77-studio"
STALE_UPLOADS_MIN = 1440            # upload folders a stopped deploy left behind go after a day


class DeployError(RuntimeError):
    pass


class DeployCancelled(DeployError):
    """The Studio job running the deploy was cancelled; the message says what was installed."""
    log_on_cancel = True


class _Stop:
    """A Studio job's cancel as a deploy sees it.  job: the job running the deploy (duck-typed:
    cancel_event, on_cancel, shield); None for a deploy nothing stops."""

    def __init__(self, job=None):
        self.job = job

    def asked(self) -> bool:
        return self.job is not None and self.job.cancel_event.is_set()

    def check(self, why="cancelled: nothing was installed"):
        if self.asked():
            raise DeployCancelled(why)

    @contextlib.contextmanager
    def dropping(self, r: "Remote", what: str):
        """Inside, a cancel drops the SSH connection at once."""
        if self.job is None:
            yield
            return
        live, lock = [True], threading.Lock()

        def drop():
            with lock:
                if live[0]:
                    r.abort()

        self.job.on_cancel.append(drop)           # first: a cancel from now on drops the connection
        try:
            self.check(f"cancelled before {what}: nothing was installed")
            try:
                yield
            except Exception:  # noqa: BLE001 - what a dropped connection raises varies (EOFError, OSError, SSHException)
                if self.asked() or r.aborted:
                    raise DeployCancelled(f"cancelled while {what}: nothing was installed") from None
                raise
        finally:
            with lock:
                live[0] = False
            try:
                self.job.on_cancel.remove(drop)
            except ValueError:
                pass

    @contextlib.contextmanager
    def unstoppable(self, why: str, before: str):
        """A step a cancel must not cut (the server installing, restarting): a cancel asked for during
        it stops the job after it.  before: what a cancel that came before the step says."""
        shield = getattr(self.job, "shield", None)
        if shield is None:
            self.check(before)
            yield
            return
        with shield(why):
            self.check(before)
            yield


# ----------------------------------------------------------------------------- bundles
def _add_dir(tar, src, arc, cancel=None):
    for dp, dn, fns in os.walk(src):
        dn.sort()
        for fn in sorted(fns):
            if cancel is not None and cancel.is_set():
                raise DeployCancelled("cancelled while packing the bundle: nothing was installed")
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            info = tar.gettarinfo(p, arcname=f"{arc}/{rel}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = int(os.path.getmtime(p))
            info.mode = 0o644
            with open(p, "rb") as f:
                tar.addfile(info, f)


def make_bundle(out_path: str, *, build_out: str | None = None, include_hub=False, loaders_dir: str | None = None,
                build_outs=None, resource_dirs=None, cancel=None) -> dict:
    """build_out: a build's out/ folder (<car>/ and <car>_chime/); build_outs: several (a car
    pack: one bundle, one restart); resource_dirs: more resources, {name: folder} (car bundles).
    cancel: an Event that stops the packing (DeployCancelled).  Returns {path, resources}."""
    resources = []
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        if loaders_dir:
            for name in ("archivexl", "tweakxl"):
                d = os.path.join(loaders_dir, name)
                if os.path.isfile(os.path.join(d, "open77.lua")):
                    _add_dir(tar, d, f"resources/assets/loaders/{name}", cancel)
                    resources.append(name)
        for out in ([build_out] if build_out else []) + list(build_outs or []):
            for name in sorted(os.listdir(out)):
                d = os.path.join(out, name)
                if os.path.isfile(os.path.join(d, "open77.lua")) and name not in resources:
                    _add_dir(tar, d, f"resources/assets/vehicles/{name}", cancel)
                    resources.append(name)
        for name, d in sorted((resource_dirs or {}).items()):
            if name not in resources:
                _add_dir(tar, d, f"resources/assets/vehicles/{name}", cancel)
                resources.append(name)
        if include_hub:
            _add_dir(tar, hub_dir(), "resources/assets/tools/f2o77_hub", cancel)
            resources.append("f2o77_hub")
    if not resources:
        raise DeployError("nothing to deploy")
    return {"path": out_path, "resources": resources, "bytes": os.path.getsize(out_path)}


# ----------------------------------------------------------------------------- SSH
class Remote:
    """One SSH connection to a server host (paramiko)."""

    known_hosts = None          # set by the Studio: <workspace>/known_hosts

    def __init__(self, server: dict, secrets, log=print, timeout=20):
        try:
            import paramiko
        except ImportError as e:   # pragma: no cover - bundled with the app
            raise DeployError("SSH support (paramiko) is not installed") from e
        import logging
        logging.getLogger("paramiko").setLevel(logging.CRITICAL)     # failures are reported by the Studio
        self.paramiko = paramiko
        self.server = server
        self.log = log
        c = paramiko.SSHClient()
        kh = Remote.known_hosts
        if kh and os.path.isfile(kh):
            c.load_host_keys(kh)
        c.set_missing_host_key_policy(_TrustOnFirstUse(kh))
        kw = {"hostname": server["host"], "port": int(server.get("port", 22)), "username": server["user"],
              "timeout": timeout, "banner_timeout": timeout, "auth_timeout": timeout,
              "allow_agent": server.get("auth") == "agent", "look_for_keys": False}
        sid = server["id"]
        if server.get("auth") == "password":
            pw = secrets.get(f"server:{sid}:password")
            if not pw:
                raise DeployError(f"no password saved for server '{sid}'")
            kw["password"] = pw
        elif server.get("auth") == "key":
            kf = os.path.expanduser(server.get("key_file") or "")
            if not os.path.isfile(kf):
                raise DeployError(f"key file not found: {kf}")
            kw["key_filename"] = kf
            pp = secrets.get(f"server:{sid}:passphrase")
            if pp:
                kw["passphrase"] = pp
        try:
            c.connect(**kw)
        except paramiko.BadHostKeyException as e:
            raise DeployError(f"the SSH host key of {server['host']} changed since it was first seen. If the server "
                              f"was reinstalled, remove its line from {kh} and connect again.") from e
        except paramiko.AuthenticationException as e:
            raise DeployError(f"{server['user']}@{server['host']}: authentication failed ({e})") from e
        except (OSError, paramiko.SSHException) as e:
            raise DeployError(f"cannot reach {server['host']}:{kw['port']}: {e}") from e
        self.client = c
        self._sftp = None
        self.aborted = False

    def abort(self):
        """Drop the connection now (another thread: a cancel).  The SFTP write or the command this
        connection waits on fails at once, and what was still queued for the server is thrown away
        (a reset, not a close that would first send it all on a slow link)."""
        self.aborted = True
        t = self.client.get_transport()
        if t is None:
            return
        try:
            t.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                              struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
        except (OSError, AttributeError, ValueError):
            pass
        try:
            t.close()
        except Exception:  # noqa: BLE001 - a connection already gone
            pass

    def alive(self) -> bool:
        t = self.client.get_transport()
        return bool(t is not None and t.is_active() and not self.aborted)

    def close(self):
        try:
            if self._sftp:
                self._sftp.close()
        finally:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def run(self, command: str, timeout=1800, on_line=None, check=True) -> tuple[int, str]:
        """Run a shell command; stream stdout+stderr lines to on_line; returns (exit code, output)."""
        chan = self.client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.settimeout(timeout)
        chan.exec_command(command)
        buf, out = b"", []
        t0 = time.time()
        while True:
            if chan.recv_ready():
                data = chan.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.decode("utf-8", "replace").rstrip("\r")
                    out.append(s)
                    if on_line:
                        on_line(s)
            elif chan.exit_status_ready() and not chan.recv_ready():
                break
            else:
                if time.time() - t0 > timeout:
                    chan.close()
                    raise DeployError(f"remote command timed out after {timeout} s")
                time.sleep(0.05)
        while chan.recv_ready():
            buf += chan.recv(65536)
        if buf:
            for line in buf.decode("utf-8", "replace").splitlines():
                out.append(line)
                if on_line:
                    on_line(line)
        code = chan.recv_exit_status()
        chan.close()
        if check and code != 0:
            raise DeployError(f"remote command failed (exit {code}): " + "\n".join(out[-8:]))
        return code, "\n".join(out)

    def put(self, local: str, remote: str, progress=None):
        def step(done, total):
            if self.aborted:
                raise DeployCancelled("the upload was stopped")
            if progress:
                progress(done, total)
        self.sftp.put(local, remote, callback=step)

    def mkdir_private(self, path: str):
        self.run(f"mkdir -p {shlex.quote(path)} && chmod 700 {shlex.quote(path)}")

    def sweep_uploads(self):
        """Remove upload folders of this user that a stopped deploy left behind (older than a day)."""
        parent, base = posixpath.split(REMOTE_DIR)
        self.run(f"find {shlex.quote(parent or '/')} -maxdepth 1 -type d -name {shlex.quote(base + '-*')} "
                 f"-user \"$(id -u)\" -mmin +{int(STALE_UPLOADS_MIN)} -exec rm -rf {{}} + 2>/dev/null || true",
                 timeout=60, check=False)


class _TrustOnFirstUse:
    """Accept a host key the first time and remember it (the client refuses a changed key)."""

    def __init__(self, path=None):
        self.path = path

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        if self.path:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            client.save_host_keys(self.path)


# ----------------------------------------------------------------------------- operations
def _env(server: dict) -> str:
    return (f"SERVERS_ROOT={shlex.quote(server['servers_root'])} SYSTEMCTL={shlex.quote(server['systemctl'])} "
            f"XDG_RUNTIME_DIR=${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}")


def _names(server, names=None) -> list:
    names = list(names or server["names"])
    for n in names:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", n) or n not in server["names"]:
            raise DeployError(f"'{n}' is not a server of {server['id']}")
    return names


def _upload_script(r: Remote, rdir: str) -> str:
    script = deploy_script()
    if not os.path.isfile(script):
        raise DeployError("server_deploy.sh is missing from the app")
    dst = posixpath.join(rdir, "server_deploy.sh")
    r.put(script, dst)
    return dst


def _cleanup(r: Remote, server: dict, secrets, rdir: str, log):
    """Remove the upload folder; over a new connection when a cancel dropped this one."""
    cmd = f"rm -rf {shlex.quote(rdir)}"
    if r.alive():
        try:
            r.run(cmd, timeout=60, check=False)
            return
        except Exception:  # noqa: BLE001 - the connection went away: try a new one
            pass
    try:
        with Remote(server, secrets, log=log, timeout=15) as r2:
            r2.run(cmd, timeout=60, check=False)
        log("removed the partial upload from the server" if r.aborted else "removed the upload folder from the server")
    except Exception as e:  # noqa: BLE001
        log(f"could not remove {rdir} from the server ({e}): the next deploy removes it")


def _progress(log):
    last = [0.0]

    def prog(done, total):
        if total and time.time() - last[0] > 2:
            last[0] = time.time()
            log(f"  {done * 100 // total}%")
    return prog


def deploy(server: dict, secrets, bundle: str, *, mode="stage", names=None, log=print, allow_unsecured=None,
           job=None) -> dict:
    """mode: stage (hub restarts when empty), restart (now, if nobody is online), files (install only).
    job: the Studio job running it, whose cancel stops the upload at once (nothing is installed then);
    a cancel that comes while the server installs waits for the end of that."""
    names = _names(server, names)
    flag = {"stage": "--stage", "restart": "--restart", "files": ""}[mode]
    unsecured = server.get("allow_unsecured") if allow_unsecured is None else allow_unsecured
    stop = _Stop(job)
    stop.check()
    rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
    lines = []

    def on_line(s):
        lines.append(s)
        log(s)

    with Remote(server, secrets, log=log) as r:
        log(f"connected to {server['user']}@{server['host']}")
        try:
            with stop.dropping(r, "uploading"):
                r.sweep_uploads()
                r.mkdir_private(rdir)
                script = _upload_script(r, rdir)
                size = os.path.getsize(bundle)
                log(f"uploading {os.path.basename(bundle)} ({size / 1048576:.1f} MiB)")
                r.put(bundle, posixpath.join(rdir, "bundle.tgz"), progress=_progress(log))
            cmd = (f"{_env(server)} bash {shlex.quote(script)} deploy {shlex.quote(posixpath.join(rdir, 'bundle.tgz'))} "
                   + " ".join(shlex.quote(n) for n in names) + (f" {flag}" if flag else "")
                   + (" --allow-unsecured" if unsecured else ""))
            with stop.unstoppable("the install on the server", "cancelled before the server installed anything"):
                code, _ = r.run(cmd, timeout=900, on_line=on_line, check=False)
        finally:
            _cleanup(r, server, secrets, rdir, log)
    outcome = {"code": code, "names": names, "mode": mode, "lines": lines[-200:]}
    outcome["staged"] = [l for l in lines if "STAGED:" in l]
    outcome["live"] = [l for l in lines if "LIVE:" in l or "no restart needed" in l]
    outcome["pending"] = [l for l in lines if "PENDING restart" in l]
    outcome["started"] = [l.strip() for l in lines if re.search(r"^\s+\S+: (started|NOT started)$", l)]
    if code != 0:
        raise DeployError("server_deploy.sh failed: " + " | ".join(l for l in lines[-6:] if l.strip()))
    return outcome


RESULT_RX = re.compile(r"^MAP-DEPLOY-RESULT (\{.*\})\s*$")


def remote_toolkit_version(r: "Remote") -> str | None:
    """The map baker toolkit (runtime + WolvenKit) installed for the SSH user, if any."""
    _, out = r.run('head -1 "${BAKER_HOME:-$HOME/.f2o77/baker}/VERSION" 2>/dev/null || true', timeout=30, check=False)
    v = (out or "").strip().splitlines()
    v = v[-1].strip() if v else ""
    return v if re.match(r"^[A-Za-z0-9._-]{3,80}$", v) else None


def deploy_map(server: dict, secrets, bundle: str, *, names=None, toolkit=None, keep_placement=True,
               restart=True, log=print, job=None) -> dict:
    """Install a map bundle (mlo.deploy.bundle: the map + f2o77_maps) on each server folder with
    map_deploy.sh.  toolkit(host_version) -> a toolkit .tgz path for that host (the light one when
    the host already has the runtime) or None to leave the baker as it is.  job: as for deploy();
    a cancel while one folder installs stops before the next."""
    names = _names(server, names)
    stop = _Stop(job)
    stop.check()
    rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
    results, lines = [], []

    def on_line(x):
        lines.append(x)
        log(x)

    with Remote(server, secrets, log=log) as r:
        log(f"connected to {server['user']}@{server['host']}")
        try:
            with stop.dropping(r, "uploading"):
                have = remote_toolkit_version(r)
                log(f"map baker on the host: {have or 'not installed'}")
                r.sweep_uploads()
                r.mkdir_private(rdir)
                for name, src in (("run.sh", remote_script("map_deploy.sh")), ("server_deploy.sh", deploy_script()),
                                  ("baker_install.sh", remote_script("baker_install.sh"))):
                    if not os.path.isfile(src):
                        raise DeployError(f"{os.path.basename(src)} is missing from the app")
                    r.put(src, posixpath.join(rdir, name))
                tk = toolkit(have) if toolkit else None
                stop.check("cancelled before the upload: nothing was installed")
                sent = {"host_had": have, "uploaded": bool(tk), "bytes": os.path.getsize(tk) if tk else 0}
                prog = _progress(log)
                if tk:
                    log(f"uploading the map baker ({os.path.getsize(tk) / 1048576:.1f} MiB)")
                    r.put(tk, posixpath.join(rdir, "baker-toolkit.tgz"), progress=prog)
                log(f"uploading {os.path.basename(bundle)} ({os.path.getsize(bundle) / 1048576:.1f} MiB)")
                r.put(bundle, posixpath.join(rdir, "bundle.tgz"), progress=prog)
            for i, name in enumerate(names):
                before = ("cancelled before the server installed anything" if not i else
                          f"cancelled: installed on {', '.join(names[:i])}, not on {', '.join(names[i:])}")
                log(f"== {name}")
                start = len(lines)
                env = (f"{_env(server)} MAP_DEPLOY_KEEP_PLACEMENT={1 if keep_placement else 0} "
                       f"MAP_DEPLOY_RESTART={1 if restart else 0}")
                with stop.unstoppable(f"the install on {name}", before):
                    code, _ = r.run(f"cd {shlex.quote(rdir)} && {env} bash run.sh {shlex.quote(name)}", timeout=1800,
                                    on_line=on_line, check=False)
                summary = None
                for x in reversed(lines[start:]):
                    m = RESULT_RX.match(x.strip())
                    if m:
                        try:
                            summary = json.loads(m.group(1))
                        except ValueError:
                            summary = None
                        break
                results.append({"name": name, "code": code, "result": summary,
                                "lines": [x for x in lines[start:] if not RESULT_RX.match(x.strip())][-250:]})
        finally:
            _cleanup(r, server, secrets, rdir, log)
    failed = [x for x in results if x["code"] not in (0, 5, 6)]
    out = {"names": names, "servers": results, "toolkit": sent}
    if failed:
        raise DeployError("map_deploy.sh failed on " + ", ".join(f"{x['name']} (exit {x['code']})" for x in failed) + ": "
                          + " | ".join(l for l in failed[0]["lines"][-6:] if l.strip()))
    return out


def agent_api(server: dict, secrets, names=None, enable=True, restart=False, log=print) -> int:
    """Turn the map manager's agent API (the server's httpHandlers listener) on or off."""
    names = _names(server, names)
    with Remote(server, secrets, log=log) as r:
        rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
        r.mkdir_private(rdir)
        try:
            script = _upload_script(r, rdir)
            code, _ = r.run(f"{_env(server)} bash {shlex.quote(script)} agent-api - " + " ".join(shlex.quote(n) for n in names)
                            + ("" if enable else " --off") + (" --restart" if restart else ""), timeout=600, on_line=log,
                            check=False)
        finally:
            r.run(f"rm -rf {shlex.quote(rdir)}", check=False)
    return code


def remove_world(server: dict, secrets, resource: str, names=None, restart=False, log=print) -> dict:
    """Take a world edit off each server folder (``server_deploy.sh remove``): its resource folder and
    its resources.load entry, backed up and the config checked like a deploy.  restart: now when nobody
    is online; otherwise players keep the edit until the server's next start."""
    names = _names(server, names)
    if not re.match(r"^[a-z][a-z0-9_]{2,47}$", resource or ""):
        raise DeployError(f"'{resource}' is not the name of a world edit")
    lines = []

    def on_line(s):
        lines.append(s)
        log(s)

    with Remote(server, secrets, log=log) as r:
        log(f"connected to {server['user']}@{server['host']}")
        rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
        r.mkdir_private(rdir)
        try:
            script = _upload_script(r, rdir)
            code, _ = r.run(f"{_env(server)} bash {shlex.quote(script)} remove {shlex.quote(resource)} "
                            + " ".join(shlex.quote(n) for n in names) + (" --restart" if restart else ""),
                            timeout=600, on_line=on_line, check=False)
        finally:
            r.run(f"rm -rf {shlex.quote(rdir)}", check=False)
    out = {"code": code, "names": names, "resource": resource, "restart": bool(restart), "lines": lines[-200:],
           "removed": [l.strip() for l in lines if f": removed {resource} " in l],
           "pending": [l.strip() for l in lines if "PENDING restart" in l],
           "started": [l.strip() for l in lines if re.search(r"^\s+\S+: (started|NOT started)$", l)]}
    if code != 0:
        raise DeployError("server_deploy.sh remove failed: " + " | ".join(l for l in lines[-6:] if l.strip()))
    return out


def status(server: dict, secrets, names=None, log=None) -> list:
    names = _names(server, names)
    with Remote(server, secrets, log=log or (lambda *_: None)) as r:
        rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
        r.mkdir_private(rdir)
        try:
            script = _upload_script(r, rdir)
            code, out = r.run(f"{_env(server)} bash {shlex.quote(script)} status - "
                              + " ".join(shlex.quote(n) for n in names) + " --json", timeout=120, check=False)
        finally:
            r.run(f"rm -rf {shlex.quote(rdir)}", check=False)
    result = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                result.append(json.loads(line))
            except ValueError:
                pass
    if not result:
        raise DeployError("no status from the server: " + out[-500:])
    return result


def car_inventory(server: dict, secrets, names=None, log=None) -> dict:
    """The converted car resources each server folder has: bundles (with the records they carry)
    and cars installed on their own (studio/carbundle.py).  Read only."""
    from . import carbundle
    names = _names(server, names)
    with Remote(server, secrets, log=log or (lambda *_: None)) as r:
        code, out = r.run("python3 -c " + shlex.quote(carbundle.INVENTORY_PY) + " "
                          + shlex.quote(server["servers_root"]) + " " + " ".join(shlex.quote(n) for n in names),
                          timeout=120, check=False)
    inv = carbundle.parse_inventory(out)
    missing = [n for n in names if n not in inv]
    if missing:
        raise DeployError(f"no car inventory from {', '.join(missing)}: " + (out or "")[-300:])
    return inv


# the release versions staged on the host, read without changing anything (no script upload)
RELEASES_PY = (
    "import json, os\n"
    f"R = {fleet.RELEASE_ROOT!r}\n"
    "out = []\n"
    "for n in sorted(os.listdir(R)) if os.path.isdir(R) else []:\n"
    "    p = os.path.join(R, n, 'package-manifest.json')\n"
    "    if n == 'tools' or not os.path.isfile(p):\n"
    "        continue\n"
    "    try:\n"
    "        out.append(json.load(open(p, encoding='utf-8'))['version'])\n"
    "    except (OSError, ValueError, KeyError):\n"
    "        pass\n"
    "print(json.dumps(out))\n"
)


def releases(server: dict, secrets) -> list:
    """The release versions staged on the server's host (read-only)."""
    with Remote(server, secrets) as r:
        _, out = r.run(f"python3 -c {shlex.quote(RELEASES_PY)}", timeout=120, check=False)
    got = fleet.parse_releases(out)
    if got is None:
        raise DeployError("could not read the staged releases: " + out[-300:])
    return got


def upgrade(server: dict, secrets, target: str, names, log=print) -> int:
    """vps_upgrade.sh TARGET NAMES on the server's host: the release must already be staged there.
    Returns the script's exit code (it rolls a server back by itself when it comes up worse)."""
    if not fleet.VERSION_RX.match(target or ""):
        raise DeployError(f"not a version: {target!r}")
    names = _names(server, names)
    blocked = fleet.server_blockers(server)
    if blocked:
        raise DeployError(f"{server['label']}: {blocked}")
    rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
    with Remote(server, secrets, log=log) as r:
        r.mkdir_private(rdir)
        try:
            src = remote_script("vps_upgrade.sh")
            if not os.path.isfile(src):
                raise DeployError("vps_upgrade.sh is missing from the app")
            r.put(src, posixpath.join(rdir, "vps_upgrade.sh"))
            cmd = (f"cd {shlex.quote(rdir)} && bash vps_upgrade.sh {shlex.quote(target)} "
                   + " ".join(shlex.quote(n) for n in names))
            code, _ = r.run(cmd, timeout=7200, on_line=log, check=False)
        finally:
            r.run(f"rm -rf {shlex.quote(rdir)}", check=False)
    return code


def logs(server: dict, secrets, name: str, lines=200) -> str:
    _names(server, [name])
    with Remote(server, secrets) as r:
        cmd = f"tail -n {int(lines)} {shlex.quote(posixpath.join(server['servers_root'], name, 'server.log'))}"
        _, out = r.run(cmd, timeout=60, check=False)
    return out


def restart(server: dict, secrets, names=None, log=print, force=False) -> int:
    names = _names(server, names)
    with Remote(server, secrets, log=log) as r:
        rdir = f"{REMOTE_DIR}-{uuid.uuid4().hex[:8]}"
        r.mkdir_private(rdir)
        try:
            script = _upload_script(r, rdir)
            code, _ = r.run(("F2O77_FORCE=1 " if force else "") + f"{_env(server)} bash {shlex.quote(script)} restart - "
                            + " ".join(shlex.quote(n) for n in names), timeout=600, on_line=log, check=False)
        finally:
            r.run(f"rm -rf {shlex.quote(rdir)}", check=False)
    return code


def test_connection(server: dict, secrets) -> dict:
    with Remote(server, secrets) as r:
        _, out = r.run("uname -sr; command -v python3 >/dev/null && echo python3-ok; id -un", timeout=30, check=False)
        found = {}
        for n in server["names"]:
            d = posixpath.join(server["servers_root"], n)
            code, _ = r.run(f"test -f {shlex.quote(d)}/server.jsonc", timeout=30, check=False)
            found[n] = code == 0
    return {"ok": "python3-ok" in out and all(found.values()), "system": out.splitlines()[0] if out else "",
            "python3": "python3-ok" in out, "servers": found}
