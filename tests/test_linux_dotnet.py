""".NET 8 for Linux, the map baker's runtime (studio/linuxdotnet.py): the Windows app carries it
from 1.7.3 (it never did, so Settings > Tools showed it missing and maps installed from Windows got
no baker), Settings > Tools downloads it where it is missing, and the toolkit built from it runs
WolvenKit on a Linux server host.

The checks with the real files use the pinned downloads in out/package-downloads (fetch_inputs.py)
when they are there and skip otherwise."""
import hashlib
import io
import json
import os
import platform
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "package"))

from fivem2open77.studio import linuxdotnet as LD  # noqa: E402

DOWNLOADS = os.path.join(ROOT, "out", "package-downloads")
REAL_TGZ = os.path.join(DOWNLOADS, LD.NAME)
REAL_WK = os.path.join(DOWNLOADS, "wolvenkit")
ON_LINUX_X64 = sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64")


def _tgz(entries) -> bytes:
    """A .tar.gz from [(name, bytes | None for a folder | ('sym', target), mode)]."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(data, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = data[1]
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


RUNTIME = [(".", None, 0o755), ("./host", None, 0o755), ("./host/fxr", None, 0o755), ("./host/fxr/8.0.99", None, 0o755),
           ("./host/fxr/8.0.99/libhostfxr.so", b"\x7fELF fxr", 0o755), ("./dotnet", b"\x7fELF host", 0o755),
           ("./LICENSE.txt", b"MIT", 0o644), ("./shared", None, 0o755), ("./shared/Microsoft.NETCore.App", None, 0o755),
           ("./shared/Microsoft.NETCore.App/8.0.99", None, 0o755),
           ("./shared/Microsoft.NETCore.App/8.0.99/System.Private.CoreLib.dll", b"MZ corelib", 0o644)]


def test_unpack_takes_a_runtime_and_refuses_anything_else(tmp_path):
    good = tmp_path / "good.tar.gz"
    good.write_bytes(_tgz(RUNTIME))
    dest = tmp_path / "dotnet-linux-x64"
    dest.mkdir()
    (dest / "stale.txt").write_text("from before")
    res = LD.unpack(str(good), str(dest))
    assert res == {"path": str(dest), "files": 4, "runtime": "8.0.99"}
    assert LD.is_root(str(dest)) and not (dest / "stale.txt").exists()
    if os.name != "nt":
        assert os.stat(dest / "dotnet").st_mode & 0o111 and not os.stat(dest / "LICENSE.txt").st_mode & 0o111
    bad = {
        "a path out of the folder": RUNTIME + [("./../evil", b"x", 0o644)],
        "an absolute path": RUNTIME + [("/etc/evil", b"x", 0o644)],
        "a link": RUNTIME + [("./shared/link", ("sym", "/etc/passwd"), 0o777)],
        "a file that is not the runtime's": RUNTIME + [("./evil.sh", b"rm -rf /", 0o755)],
        "no host/fxr": [e for e in RUNTIME if "fxr" not in e[0]],
        "a host that is not Linux's": [e if e[0] != "./dotnet" else ("./dotnet", b"MZ windows", 0o755) for e in RUNTIME],
    }
    for why, entries in bad.items():
        p = tmp_path / "bad.tar.gz"
        p.write_bytes(_tgz(entries))
        with pytest.raises(ValueError):
            LD.unpack(str(p), str(dest))
        # the runtime that was there is still there, whole
        assert LD.is_root(str(dest)), why
        assert not os.path.exists(str(dest) + ".part"), why
    assert not (tmp_path / "evil").exists()


class _Files(BaseHTTPRequestHandler):
    files = {}
    hits = []

    def do_GET(self):
        self.hits.append(self.path)
        body = self.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def files_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Files.files, _Files.hits = {}, []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_download_checks_the_file_and_installs_it(tmp_path, files_server):
    blob = _tgz(RUNTIME)
    _Files.files["/rt.tar.gz"] = blob
    sha = hashlib.sha256(blob).hexdigest()
    dest, cache = str(tmp_path / "rt"), str(tmp_path / "dl")
    seen = []
    res = LD.download(dest, cache, url=files_server + "/rt.tar.gz", sha256=sha, size=len(blob), log=lambda *_: None,
                      progress=lambda d, t: seen.append((d, t)))
    assert res["runtime"] == "8.0.99" and LD.is_root(dest)
    assert seen[-1] == (len(blob), len(blob))
    # a second install reads the downloaded file again
    LD.download(dest, cache, url=files_server + "/rt.tar.gz", sha256=sha, size=len(blob), log=lambda *_: None)
    assert _Files.hits == ["/rt.tar.gz"]
    # a file that is not the pinned one installs nothing and leaves nothing behind
    _Files.files["/other.tar.gz"] = _tgz(RUNTIME + [("./ThirdPartyNotices.txt", b"changed", 0o644)])
    with pytest.raises(ValueError, match="not the pinned file"):
        LD.download(str(tmp_path / "rt2"), cache, url=files_server + "/other.tar.gz", sha256=sha, size=0, log=lambda *_: None)
    assert not (tmp_path / "rt2").exists() and not any(f.endswith(".part") for f in os.listdir(cache))
    # stopped half-way (a cancelled job): nothing installed
    def stop():
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        LD.download(str(tmp_path / "rt3"), str(tmp_path / "dl3"), url=files_server + "/rt.tar.gz", sha256=sha, size=len(blob),
                    log=lambda *_: None, check=stop)
    assert not (tmp_path / "rt3").exists()


def test_the_pins_agree():
    import build_windows
    import fetch_inputs
    pin = [x for x in fetch_inputs.PINNED if x[0].endswith("linux-x64.tar.gz")]
    assert pin == [(LD.NAME, LD.URL, LD.SHA256)]
    win = [x for x in fetch_inputs.PINNED if x[0].startswith("dotnet-runtime-") and x[0].endswith("win-x64.zip")]
    assert win and LD.VERSION in win[0][0]                     # the same runtime as the Windows app's
    import fnmatch
    assert fnmatch.fnmatch(LD.NAME, "dotnet-runtime-8.0.*-linux-x64.tar.gz")
    src = open(build_windows.__file__, encoding="utf-8").read()
    assert '"tools", "baker", "dotnet-linux-x64"' in src and "linuxdotnet.unpack(" in src
    tools_src = open(os.path.join(ROOT, "fivem2open77", "studio", "tools.py"), encoding="utf-8").read()
    assert 'bundled("tools", "baker", "dotnet-linux-x64")' in tools_src
    if os.path.isfile(REAL_TGZ):
        h = hashlib.sha256()
        with open(REAL_TGZ, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        assert h.hexdigest() == LD.SHA256 and os.path.getsize(REAL_TGZ) == LD.SIZE


def test_the_studio_finds_the_bundled_runtime_first_then_the_downloaded_one(tmp_path, monkeypatch):
    from fivem2open77.studio import tools as T
    rt = _tgz(RUNTIME)
    (tmp_path / "rt.tgz").write_bytes(rt)
    home = tmp_path / "app"
    (home / "runtime").mkdir(parents=True)
    LD.unpack(str(tmp_path / "rt.tgz"), str(home / "tools" / "baker" / "dotnet-linux-x64"))
    ws = tmp_path / "ws"
    LD.unpack(str(tmp_path / "rt.tgz"), T.linux_dotnet_download_dir(str(ws)))
    monkeypatch.setenv("F2O77_HOME", str(home))
    tb = T.Toolbox({"paths": {}}, str(ws))
    st = tb.status()["linux_dotnet"]
    assert st["ok"] and st["path"] == os.path.abspath(home / "tools" / "baker" / "dotnet-linux-x64")
    assert st["download"] == {"version": LD.VERSION, "bytes": LD.SIZE, "url": LD.URL}
    monkeypatch.delenv("F2O77_HOME")
    monkeypatch.setattr(T, "app_home", lambda: None)
    monkeypatch.setattr(T, "bundled", lambda *parts: None)
    tb = T.Toolbox({"paths": {}}, str(ws))
    assert tb.linux_dotnet == os.path.abspath(T.linux_dotnet_download_dir(str(ws)))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_settings_tools_download_it_through_the_api(tmp_path, files_server, monkeypatch):
    """The button's route: POST /api/tools/linux_dotnet runs the download as a job, and Settings >
    Tools then shows it ok, from the workspace."""
    from fivem2open77.studio import server as srv
    from fivem2open77.studio import tools as T
    blob = _tgz(RUNTIME)
    _Files.files["/ms/" + LD.NAME] = blob
    monkeypatch.setattr(LD, "URL", files_server + "/ms/" + LD.NAME)
    monkeypatch.setattr(LD, "SHA256", hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(LD, "SIZE", len(blob))
    ws = str(tmp_path / "ws")
    port, token = _free_port(), "t" * 20
    threading.Thread(target=srv.serve, kwargs=dict(port=port, workspace=ws, token=token), daemon=True).start()

    def req(method, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", "X-F2O77-Token": token})
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    job = req("POST", "/api/tools/linux_dotnet")
    assert job["kind"] == "tools" and "NET 8 for Linux" in job["title"]
    for _ in range(200):
        j = req("GET", f"/api/jobs/{job['id']}")
        if j["state"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    assert j["result"]["ok"] and j["result"]["runtime"] == "8.0.99"
    st = req("GET", "/api/tools?refresh=1")["linux_dotnet"]
    assert st["ok"] and st["path"] == os.path.abspath(T.linux_dotnet_download_dir(ws))
    assert _Files.hits == ["/ms/" + LD.NAME]


def test_the_ui_offers_the_download_where_it_is_missing():
    web = os.path.join(ROOT, "fivem2open77", "studio", "web", "js", "pages")
    settings = open(os.path.join(web, "settings.js"), encoding="utf-8").read()
    assert 'api.post("/tools/linux_dotnet")' in settings and '"data-fix": "linux_dotnet"' in settings
    assert "t.linux_dotnet.ok ? null : linuxFix()" in settings
    deploy = open(os.path.join(web, "map.js"), encoding="utf-8").read()
    assert 'h("a", { href: "#/settings/tools" }, "Settings > Tools"), " downloads it (.NET 8 for Linux)."' in deploy


@pytest.mark.skipif(not os.path.isfile(REAL_TGZ), reason="the pinned download is not in out/package-downloads")
def test_the_real_runtime_unpacks_and_runs(tmp_path):
    res = LD.unpack(REAL_TGZ, str(tmp_path / "rt"))
    assert res["runtime"] == LD.VERSION and res["files"] == 188
    if not ON_LINUX_X64:
        pytest.skip("runs the Linux host only on Linux x64")
    out = subprocess.run([str(tmp_path / "rt" / "dotnet"), "--list-runtimes"], capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, DOTNET_ROOT=str(tmp_path / "rt"), DOTNET_CLI_TELEMETRY_OPTOUT="1"))
    assert out.returncode == 0 and f"Microsoft.NETCore.App {LD.VERSION}" in out.stdout, out.stdout + out.stderr


@pytest.mark.skipif(not (os.path.isfile(REAL_TGZ) and os.path.isfile(os.path.join(REAL_WK, "WolvenKit.CLI.dll"))),
                    reason="the pinned downloads are not in out/package-downloads")
def test_the_toolkit_from_the_windows_bundle_installs_and_runs_wolvenkit_on_a_linux_host(tmp_path):
    """What a map deploy from Windows does now: the toolkit packed from the app's own runtime and
    WolvenKit (the Windows release, which carries libkraken.so), installed by baker_install.sh on a
    Linux host, runs WolvenKit and loads the baker -- with a stand-in systemctl."""
    if not ON_LINUX_X64:
        pytest.skip("a Linux host is needed")
    from fivem2open77.mlo import toolkit as tk
    rt = str(tmp_path / "app" / "tools" / "baker" / "dotnet-linux-x64")
    LD.unpack(REAL_TGZ, rt)
    built = tk.build(str(tmp_path / "toolkit.tgz"), rt, REAL_WK, log=lambda *_: None)
    assert built["runtime"] == LD.VERSION and built["version"] == tk.version_of(rt, REAL_WK)
    server = tmp_path / "srv" / "open77-test"
    (server / "resources").mkdir(parents=True)
    calls = tmp_path / "systemctl.log"
    stub = tmp_path / "systemctl"
    # a user-unit systemctl ("... --user", as on the XyberRP hosts), so every unit file goes under
    # the test's own home and nothing touches the machine's
    stub.write_text("#!/bin/sh\n[ \"$1\" = --user ] && shift\necho \"$@\" >> '%s'\n"
                    "case \"$1\" in is-active) echo active;; show) echo always;; esac\nexit 0\n" % calls)
    stub.chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home" / ".config"),
               SYSTEMCTL=f"{stub} --user")
    os.makedirs(env["HOME"])
    script = os.path.join(ROOT, "tools", "deploy", "remote", "baker_install.sh")
    out = subprocess.run(["bash", script, str(tmp_path / "toolkit.tgz"), str(server)], capture_output=True, text=True,
                         timeout=600, env=env)
    log = out.stdout + out.stderr
    assert out.returncode == 0, log
    assert f"installing toolkit {built['version']}" in log and "WolvenKit: " in log and "f2o77-mapbaker-open77-test.service: active" in log
    home = tmp_path / "home" / ".f2o77" / "baker"
    assert (home / "VERSION").read_text().strip() == built["version"]
    assert os.access(home / "dotnet" / "dotnet", os.X_OK)
    assert "restart f2o77-mapbaker-open77-test.service" in calls.read_text()
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    assert (units / "f2o77-mapbaker-open77-test.service").is_file()
    assert (units / "open77-test.service.d" / "f2o77-maps.conf").is_file()
