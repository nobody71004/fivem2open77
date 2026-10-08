"""tools/sync_required_mods.py against a local HTTP server that mimics an Open77 server's
/mods/v1/manifest + /mods/v1/blobs/<sha256>.   python tests/test_sync_mods.py"""
import hashlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import sync_required_mods as S  # noqa: E402


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class _World:
    """Serves whatever mods are currently in self.mods: {id: zip bytes}."""

    def __init__(self):
        self.mods = {}
        self.bad_sha = set()
        world = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/mods/v1/manifest":
                    body = json.dumps({"version": 1, "digest": "d" * 64, "enabled": True, "mods": [
                        {"id": mid, "source": "hosted", "version": "1.0.0",
                         "sha256": ("0" * 64 if mid in world.bad_sha else hashlib.sha256(z).hexdigest()),
                         "sizeBytes": len(z), "url": f"blobs/{hashlib.sha256(z).hexdigest()}"}
                        for mid, z in world.mods.items()]}).encode()
                else:
                    sha = self.path.rsplit("/", 1)[-1]
                    body = next((z for z in world.mods.values() if hashlib.sha256(z).hexdigest() == sha), None)
                    if body is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mods/v1/manifest"


def _game():
    g = tempfile.mkdtemp()
    os.makedirs(os.path.join(g, "archive", "pc", "mod"))
    return g


def _read(g, rel):
    with open(os.path.join(g, *rel.split("/")), "rb") as f:
        return f.read()


def test_install_update_and_cleanup():
    w, g, cache = _World(), _game(), tempfile.mkdtemp()
    quiet = lambda *a: None  # noqa: E731
    w.mods["car-assets"] = _zip({"archive/pc/mod/car.archive": b"A1", "r6/tweaks/f2o/car.yaml": b"Y1"})
    w.mods["loader-assets"] = _zip({"red4ext/plugins/L/L.dll": b"DLL"})

    s = S.sync(w.url, g, cache, log=quiet)                      # executable package held back
    assert s["blocked"] == ["loader-assets"] and _read(g, "archive/pc/mod/car.archive") == b"A1"
    assert not os.path.exists(os.path.join(g, "red4ext"))
    s = S.sync(w.url, g, cache, allow_executable=True, log=quiet)
    assert s["written"] == ["red4ext/plugins/L/L.dll"] and s["unchanged"] == 2
    s = S.sync(w.url, g, cache, allow_executable=True, log=quiet)  # idempotent
    assert s["written"] == [] and s["removed"] == [] and s["unchanged"] == 3

    # a launcher-style hard link must be replaced, not written through
    store = os.path.join(tempfile.mkdtemp(), "blob")
    with open(store, "wb") as f:
        f.write(b"STORE")
    target = os.path.join(g, "archive", "pc", "mod", "car.archive")
    os.remove(target)
    os.link(store, target)
    s = S.sync(w.url, g, cache, log=quiet)
    assert _read(g, "archive/pc/mod/car.archive") == b"A1"
    with open(store, "rb") as f:
        assert f.read() == b"STORE"

    # new version: yaml dropped, texture archive added -> stale yaml removed
    w.mods["car-assets"] = _zip({"archive/pc/mod/car.archive": b"A2", "archive/pc/mod/car_tex1.archive": b"T"})
    s = S.sync(w.url, g, cache, log=quiet)
    assert sorted(s["written"]) == ["archive/pc/mod/car.archive", "archive/pc/mod/car_tex1.archive"]
    assert s["removed"] == ["r6/tweaks/f2o/car.yaml"]
    assert not os.path.exists(os.path.join(g, "r6", "tweaks", "f2o", "car.yaml"))

    # a file the user changed after we wrote it is never removed
    with open(os.path.join(g, "archive", "pc", "mod", "car_tex1.archive"), "wb") as f:
        f.write(b"user edit")
    w.mods["car-assets"] = _zip({"archive/pc/mod/car.archive": b"A3"})
    s = S.sync(w.url, g, cache, log=quiet)
    assert s["removed"] == [] and _read(g, "archive/pc/mod/car_tex1.archive") == b"user edit"
    for d in (g, cache):
        shutil.rmtree(d)


def test_refuses_bad_hash_and_escaping_paths():
    w, g, cache = _World(), _game(), tempfile.mkdtemp()
    quiet = lambda *a: None  # noqa: E731
    w.mods["car-assets"] = _zip({"archive/pc/mod/car.archive": b"A1"})
    w.bad_sha.add("car-assets")
    try:
        S.sync(w.url, g, cache, log=quiet)
        raise AssertionError("hash mismatch accepted")
    except ValueError:
        pass
    assert not os.path.exists(os.path.join(g, "archive", "pc", "mod", "car.archive"))
    assert not [f for f in os.listdir(cache) if f.endswith((".zip", ".part"))]
    w.bad_sha.clear()
    for bad in ("../outside.txt", "archive/pc/mod/../../../x.dll", "C:/Windows/x.dll", "bin/x64/Cyberpunk2077.exe"):
        w.mods = {"evil-assets": _zip({bad: b"x"})}
        try:
            S.sync(w.url, g, cache, allow_executable=True, log=quiet)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert not os.path.exists(os.path.join(os.path.dirname(g), "outside.txt"))
    for d in (g, cache):
        shutil.rmtree(d)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
