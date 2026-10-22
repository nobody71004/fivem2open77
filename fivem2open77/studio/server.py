"""The Studio's local web server: the UI (static files) and the JSON API.

Security: it listens on 127.0.0.1 only; every /api call needs the session token (cookie set by
/auth?token=..., or the X-F2O77-Token header); the Host header must name this machine (DNS
rebinding) and a cross-site Origin is refused (CSRF).  The token is random per run unless given.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import secrets as pysecrets
import sys
import threading
import time
import traceback
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import profile as profile_mod
from ..cp77 import sound as sound_mod
from ..cp77.donor import DONORS
from .app import Studio
from .deploy import DeployError
from .maplink import LinkError, Unreachable
from .maps import LookMissing, MapOptionsError
from .paths import default_workspace, web_dir
from . import jsonutil, ncweassets
from .projects import ProjectError
from .settings import SettingsError
from ..world.edits import EditError

ROUTES = []
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                ".json": "application/json", ".glb": "model/gltf-binary", ".txt": "text/plain; charset=utf-8",
                ".woff2": "font/woff2", ".wasm": "application/wasm"}


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


def error_answer(e: Exception):
    """(status, body) the API answers for an exception a route raised; None for a bug (a 500)."""
    if isinstance(e, HttpError):
        return e.status, {"error": str(e), **e.extra}
    if isinstance(e, profile_mod.ProfileError):
        return 400, {"error": "invalid profile", "errors": e.errors}
    if isinstance(e, SettingsError):
        return 400, {"error": "invalid settings", "errors": e.errors}
    if isinstance(e, MapOptionsError):
        return 400, {"error": "invalid map options", "errors": e.errors}
    if isinstance(e, EditError):
        return 400, {"error": f"invalid edit: {e}"}
    if isinstance(e, LookMissing):
        return 409, {"error": str(e), "look": "missing"}
    if isinstance(e, Unreachable):
        return 503, {"error": str(e), "reachable": False}
    if isinstance(e, LinkError):
        return 409, {"error": str(e)}
    if isinstance(e, DeployError):
        return 502, {"error": str(e)}
    if isinstance(e, (ProjectError, ValueError, KeyError)):
        return 400, {"error": str(e) or e.__class__.__name__}
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "fivem2open77-studio"
    protocol_version = "HTTP/1.1"

    # quiet access log; errors still go to stderr
    def log_message(self, fmt, *args):
        if os.environ.get("F2O77_HTTP_LOG"):
            sys.stderr.write("http: " + fmt % args + "\n")

    @property
    def studio(self) -> Studio:
        return self.server.studio

    # ------------------------------------------------------------------ plumbing
    def _send(self, status, body: bytes, ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, status=200, headers=None):
        self._send(status, jsonutil.dumps_bytes(obj), headers=headers)

    def body_json(self):
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16 * 1024 * 1024:
            raise HttpError(413, "request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as e:
            raise HttpError(400, f"invalid JSON: {e}") from e

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        port = self.server.server_address[1]
        return origin.lower() in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _token_ok(self):
        tok = self.server.token
        h = self.headers.get("X-F2O77-Token")
        if h and pysecrets.compare_digest(h, tok):
            return True
        c = SimpleCookie(self.headers.get("Cookie") or "")
        if "f2o77" in c and pysecrets.compare_digest(c["f2o77"].value, tok):
            return True
        return False

    def handle_one(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if not self._host_ok():
            return self._send(403, b'{"error": "bad host"}')
        if path == "/auth":
            tok = q.get("token", "")
            if not pysecrets.compare_digest(tok, self.server.token):
                return self._send(403, b"bad token", "text/plain")
            return self._send(302, b"", "text/plain", {
                "Location": "/" + (("#" + q["page"]) if q.get("page") else ""),
                "Set-Cookie": f"f2o77={self.server.token}; Path=/; HttpOnly; SameSite=Strict"})
        if path == "/mcp":
            return self.mcp_endpoint()
        if path.startswith("/api/"):
            if path != "/api/health":
                if not self._token_ok():
                    return self._send(401, b'{"error": "not signed in: open the Studio from its window or /auth?token=..."}')
                if self.command not in ("GET", "HEAD") and not self._origin_ok():
                    return self._send(403, b'{"error": "cross-site request refused"}')
            for method, rx, fn in ROUTES:
                if method != self.command and not (method == "GET" and self.command == "HEAD"):
                    continue
                m = rx.match(path)
                if m:
                    return fn(self, q, *m.groups())
            raise HttpError(404, "no such endpoint")
        return self.static(path)

    def _drain(self):
        """A body the endpoint did not read would be parsed as the next request on this keep-alive
        connection: read it away (small) or close the connection (large)."""
        if getattr(self, "_body_read", False):
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return
        if n <= 1 << 20:
            try:
                self.rfile.read(n)
            except OSError:
                self.close_connection = True
        else:
            self.close_connection = True

    def _dispatch(self):
        self._body_read = False
        try:
            self.handle_one()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            ans = error_answer(e)
            if ans is None:
                traceback.print_exc()
                ans = 500, {"error": f"{e.__class__.__name__}: {e}"}
            try:
                self.json(ans[1], status=ans[0])
            except OSError:
                pass
        finally:
            self._drain()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _dispatch

    # ------------------------------------------------------------------ MCP (agents outside the Studio)
    def mcp_endpoint(self):
        origin = self.headers.get("Origin")
        if origin and not self._origin_ok():
            return self._send(403, b'{"error": "cross-site request refused"}')
        body = b""
        if self.command == "POST":
            self._body_read = True
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 * 1024 * 1024:
                raise HttpError(413, "request too large")
            body = self.rfile.read(n) if n else b""
        status, headers, out = self.studio.mcp.http(self.command, self.headers, body)
        self._send(status, out, "application/json", headers)

    # ------------------------------------------------------------------ static UI
    def static(self, path):
        root = web_dir()
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        # fixed types: on Windows mimetypes reads the registry, which can say text/plain for .css/.js
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower()) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        headers = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                              "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' blob: data:; "
                                              "worker-src 'self' blob:"} if full.endswith(".html") else None
        self._send(200, data, ctype, headers)

    def file(self, path, ctype=None, download_name=None, immutable=False):
        """immutable: the URL names this content for good (a ?k= key), so the browser may keep it."""
        if not os.path.isfile(path):
            raise HttpError(404, "not found")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if immutable else "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def raw_body(self, limit=64 << 30):
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise HttpError(400, "empty upload (a folder cannot be uploaded: type its path instead)")
        if n > limit:
            raise HttpError(413, f"upload too large ({n / 1073741824:.1f} GB): type the file's path instead, "
                                 "the Studio reads it where it is")
        return self.rfile, n


# ====================================================================== API
@route("GET", r"/api/health")
def api_health(h, q):
    h.json({"ok": True, "app": "fivem2open77-studio"})


@route("GET", r"/api/state")
def api_state(h, q):
    h.json(h.studio.state())


@route("GET", r"/api/settings")
def api_settings(h, q):
    h.json(h.studio.redacted_settings())


@route("PATCH", r"/api/settings")
def api_settings_patch(h, q):
    patch = h.body_json()
    patch.pop("secrets_encrypted", None)
    for s in patch.get("servers", []) if isinstance(patch.get("servers"), list) else []:
        s.pop("password_set", None)
        s.pop("passphrase_set", None)
    if isinstance(patch.get("ai"), dict):
        patch["ai"].pop("anthropic_key_set", None)
    h.studio.settings.update(patch)
    if "paths" in patch:
        h.studio.toolbox(refresh=True)
    if "servers" in patch:
        h.studio.links.forget()
    h.json(h.studio.redacted_settings())


@route("POST", r"/api/secrets")
def api_secret(h, q):
    b = h.body_json()
    h.studio.set_secret(str(b.get("key", "")), b.get("value") or None)
    h.json({"ok": True, "set": bool(b.get("value"))})


@route("GET", r"/api/tools")
def api_tools(h, q):
    h.json(h.studio.tools_status(refresh=q.get("refresh") == "1"))


@route("POST", r"/api/tools/donor")
def api_tools_donor(h, q):
    h.json(h.studio.prepare_donors().summary())


@route("POST", r"/api/tools/linux_dotnet")
def api_tools_linux_dotnet(h, q):
    h.json(h.studio.get_linux_dotnet().summary())


@route("GET", r"/api/sounds")
def api_sounds(h, q):
    h.json({"sounds": sound_mod.choices(), "characters": sound_mod.BY_CHARACTER})


@route("GET", r"/api/donors")
def api_donors(h, q):
    h.json({k: {"label": v["label"], "record": v["record"], "doors": v["doors"], "sound": v.get("sound"),
                "lid_ends": v.get("lid_ends")} for k, v in DONORS.items()})


@route("GET", r"/api/profile/schema")
def api_profile_schema(h, q):
    h.json({"defaults": profile_mod.default_profile(), "roles": profile_mod.ROLE_VALUES,
            "light_types": profile_mod.LIGHT_TYPES, "multiplier_range": profile_mod.MULT_RANGE,
            "donors": sorted(DONORS)})


# ---------------------------------------------------------------- NCWE asset imports (map and world projects)
def _asset_project(h, pid):
    p = h.studio.projects.get(pid)
    if p.kind not in ("map", "world"):
        raise ProjectError(f"this is a {p.kind} project; NCWE assets work on map and world projects")
    return p


@route("GET", r"/api/projects/([a-f0-9]{12})/ncwe_assets")
def api_ncwe_assets(h, q, pid):
    p = _asset_project(h, pid)
    stale = h.studio.map_stale(p) if p.kind == "map" else h.studio.world_stale(p)
    h.json({"assets": ncweassets.list_imports(p), "collisions": ncweassets.collisions(p),
            "build_stale": stale})


@route("GET", r"/api/projects/([a-f0-9]{12})/ncwe_assets/([^/]+)")
def api_ncwe_asset_contents(h, q, pid, name):
    p = _asset_project(h, pid)
    h.json({"contents": ncweassets.contents(p, name)})


@route("POST", r"/api/projects/([a-f0-9]{12})/ncwe_assets")
def api_ncwe_asset_add(h, q, pid):
    p = _asset_project(h, pid)
    stream, n = h.raw_body()
    h.json({"asset": ncweassets.add_import(p, q.get("name", ""), stream, n)})


@route("DELETE", r"/api/projects/([a-f0-9]{12})/ncwe_assets/([^/]+)")
def api_ncwe_asset_remove(h, q, pid, name):
    p = _asset_project(h, pid)
    h.json({"removed": ncweassets.remove_import(p, name)})


@route("POST", r"/api/projects/([a-f0-9]{12})/ncwe_import")
def api_ncwe_import(h, q, pid):
    b = h.body_json()
    job = h.studio.ncwe_import(pid, name=str(b.get("name") or "").strip() or None,
                               position=b.get("position"), yaw=float(b.get("yaw") or 0.0),
                               place=None if b.get("place") is None else bool(b.get("place")),
                               height=b.get("height"), analyze=bool(b.get("analyze")))
    h.json(job.summary())


# ---------------------------------------------------------------- uploads / projects
@route("POST", r"/api/uploads")
def api_upload(h, q):
    stream, n = h.raw_body()
    from .sources import need_space
    need_space(h.studio.uploads_dir, n, f"the upload of {q.get('name', 'the file')}")    # before reading a byte
    info = h.studio.save_upload(q.get("name", "upload"), stream, n)
    info.pop("path", None)
    h.json(info)


# ---------------------------------------------------------------- car packs (studio/sources.py)
@route("GET", r"/api/packs")
def api_packs(h, q):
    h.json(h.studio.packs())


@route("POST", r"/api/packs")
def api_pack_create(h, q):
    b = h.body_json()
    e, job = h.studio.create_pack(path=b.get("path"), upload_id=b.get("upload_id"), name=b.get("name"))
    h.json({"pack": e, "job": job.summary()})


@route("GET", r"/api/packs/([a-f0-9]{12})")
def api_pack(h, q, lid):
    h.json(h.studio.pack(lid))


@route("POST", r"/api/packs/([a-f0-9]{12})/import")
def api_pack_import(h, q, lid):
    b = h.body_json()
    made, job = h.studio.import_pack_cars(lid, b.get("models"))
    h.json({"projects": [p.summary() for p in made], "job": job.summary() if job else None})


@route("POST", r"/api/packs/([a-f0-9]{12})/build")
def api_pack_build(h, q, lid):
    b = h.body_json()
    h.json(h.studio.pack_build(lid, only_unbuilt=bool(b.get("only_unbuilt"))).summary())


@route("POST", r"/api/packs/([a-f0-9]{12})/deploy")
def api_pack_deploy(h, q, lid):
    b = h.body_json()
    if not b.get("server"):
        raise HttpError(400, "server required")
    h.json(h.studio.pack_deploy(lid, b["server"], names=b.get("names"), mode=b.get("mode"),
                                include_hub=bool(b.get("include_hub"))).summary())


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/cars")
def api_server_all_cars(h, q, sid):
    """Every car with a good build onto this server, in car bundles (studio/carbundle.py)."""
    b = h.body_json()
    h.json(h.studio.car_bundles_deploy(sid, names=b.get("names"), mode=b.get("mode")).summary())


@route("DELETE", r"/api/packs/([a-f0-9]{12})")
def api_pack_delete(h, q, lid):
    h.studio.delete_pack(lid, with_projects=q.get("projects") in ("1", "true", "yes"))
    h.json({"ok": True})


@route("GET", r"/api/projects")
def api_projects(h, q):
    h.json(h.studio.projects.list())


@route("POST", r"/api/projects")
def api_project_create(h, q):
    b = h.body_json()
    p, job = h.studio.create_project(path=b.get("path"), upload_id=b.get("upload_id"), name=b.get("name"),
                                     kind=b.get("kind") or "car", display=b.get("display"), center=b.get("center"),
                                     city=b.get("city"))
    h.json({"project": p.summary(), "job": job.summary() if job is not None and hasattr(job, "summary") else None})


@route("GET", r"/api/projects/([a-f0-9]{12})")
def api_project(h, q, pid):
    p = h.studio.projects.get(pid)
    d = p.summary()
    d["data"] = p.data
    d["profile"] = p.raw_profile()
    h.json(d)


@route("PATCH", r"/api/projects/([a-f0-9]{12})")
def api_project_patch(h, q, pid):
    p = h.studio.projects.get(pid)
    b = h.body_json()
    if "name" in b:
        name = str(b["name"]).strip()[:80]
        if not name:
            raise HttpError(400, "name required")
        p.data["name"] = name
        p.data["custom_name"] = True
        p.save()
    h.json(p.summary())


@route("DELETE", r"/api/projects/([a-f0-9]{12})")
def api_project_delete(h, q, pid):
    h.studio.delete_project(pid)
    h.json({"ok": True})


@route("GET", r"/api/projects/([a-f0-9]{12})/profile")
def api_profile(h, q, pid):
    p = h.studio.projects.get(pid)
    h.json({"profile": p.raw_profile(), "normalized": p.profile()})


@route("PUT", r"/api/projects/([a-f0-9]{12})/profile")
def api_profile_put(h, q, pid):
    p = h.studio.projects.get(pid)
    norm = p.set_profile(h.body_json())
    h.json({"profile": p.raw_profile(), "normalized": norm})


@route("PATCH", r"/api/projects/([a-f0-9]{12})/profile")
def api_profile_patch(h, q, pid):
    p = h.studio.projects.get(pid)
    norm = p.patch_profile(h.body_json())
    h.json({"profile": p.raw_profile(), "normalized": norm})


@route("POST", r"/api/projects/([a-f0-9]{12})/profile/validate")
def api_profile_validate(h, q, pid):
    b = h.body_json()
    try:
        norm = profile_mod.normalize(b, donors=DONORS, sounds=sound_mod.CATALOGUE)
        h.json({"ok": True, "normalized": norm})
    except profile_mod.ProfileError as e:
        h.json({"ok": False, "errors": e.errors})


@route("GET", r"/api/projects/([a-f0-9]{12})/inspect")
def api_inspect(h, q, pid):
    h.json(h.studio.inspect(pid))


@route("GET", r"/api/projects/([a-f0-9]{12})/preview\.glb")
def api_preview(h, q, pid):
    p = h.studio.projects.get(pid)
    path = h.studio.analyzer.session(p).preview(p.profile())
    h.file(path, "model/gltf-binary")


@route("GET", r"/api/projects/([a-f0-9]{12})/render\.png")
def api_render(h, q, pid):
    p = h.studio.projects.get(pid)
    png = h.studio.analyzer.session(p).render(p.profile(), q.get("view", "front_3q"), q.get("mode", "material"),
                                             (int(q.get("w", 640)), int(q.get("h", 400))))
    h._send(200, png, "image/png")


@route("GET", r"/api/projects/([a-f0-9]{12})/sheet\.png")
def api_sheet(h, q, pid):
    p = h.studio.projects.get(pid)
    png = h.studio.analyzer.session(p).sheet(p.profile(), q.get("mode", "material"))
    h._send(200, png, "image/png")


@route("GET", r"/api/projects/([a-f0-9]{12})/neon\.png")
def api_neon_sheet(h, q, pid):
    p = h.studio.projects.get(pid)
    try:
        png = h.studio.analyzer.session(p).neon_sheet(p.profile(), q.get("color"))
    except ValueError as e:
        raise HttpError(409, str(e))
    h._send(200, png, "image/png")


@route("GET", r"/api/projects/([a-f0-9]{12})/thumb\.png")
def api_thumb(h, q, pid):
    p = h.studio.projects.get(pid)
    path = p.path("cache", "thumb.png")
    if not os.path.isfile(path):
        if p.kind == "map":
            raise HttpError(404, "no preview yet")
        h.studio.analyzer.session(p).thumbnail(p.profile())
    h.file(path, "image/png")


# ---------------------------------------------------------------- maps
@route("GET", r"/api/projects/([a-f0-9]{12})/map")
def api_map_state(h, q, pid):
    h.json(h.studio.map_state(pid))


@route("PATCH", r"/api/projects/([a-f0-9]{12})/map/options")
def api_map_options_patch(h, q, pid):
    h.json(h.studio.map_set_options(pid, h.body_json(), patch=True))


@route("PUT", r"/api/projects/([a-f0-9]{12})/map/options")
def api_map_options_put(h, q, pid):
    h.json(h.studio.map_set_options(pid, h.body_json(), patch=False))


@route("POST", r"/api/projects/([a-f0-9]{12})/map/options/validate")
def api_map_options_validate(h, q, pid):
    from .maps import normalize_options
    try:
        h.json({"ok": True, "normalized": normalize_options(h.body_json())})
    except MapOptionsError as e:
        h.json({"ok": False, "errors": e.errors})


@route("GET", r"/api/projects/([a-f0-9]{12})/map/city")
def api_map_city(h, q, pid):
    h.json(h.studio.map_city(pid))


@route("POST", r"/api/projects/([a-f0-9]{12})/map/district")
def api_map_district(h, q, pid):
    """One district of a whole city: here, or as a new project reading the same files."""
    b = h.body_json()
    p, job = h.studio.map_district(pid, b.get("area"), name=b.get("name"), display_name=b.get("display_name"),
                                   district=b.get("district"), new_project=bool(b.get("new_project")))
    h.json({"project": p.summary(), "job": job.summary() if job else None})


@route("POST", r"/api/projects/([a-f0-9]{12})/map/look")
def api_map_look(h, q, pid):
    h.json(h.studio.map_look(pid, force=bool(h.body_json().get("force"))).summary())


@route("GET", r"/api/projects/([a-f0-9]{12})/map/render\.png")
def api_map_render(h, q, pid):
    size = (int(q["w"]), int(q["h"])) if q.get("w") and q.get("h") else None
    h._send(200, h.studio.map_render(pid, q.get("view", "plan"), q.get("mode", "material"), size), "image/png")


@route("GET", r"/api/projects/([a-f0-9]{12})/map/preview\.glb")
def api_map_glb(h, q, pid):
    h.file(h.studio.map_glb(pid), "model/gltf-binary")


@route("POST", r"/api/projects/([a-f0-9]{12})/map/game")
def api_map_game(h, q, pid):
    b = h.body_json()
    h.json(h.studio.map_game(pid, build=b.get("build"), uninstall=bool(b.get("uninstall"))).summary())


# ---------------------------------------------------------------- what went wrong in the page
_DIAG = {"t": 0.0, "n": 0}


@route("POST", r"/api/diag")
def api_diag(h, q):
    """The page's errors (script errors, a lost graphics context, a shader the GPU refused, the GPU
    itself) into the Studio's log, one line each, at most 60 a minute."""
    b = h.body_json()
    now = time.time()
    if now - _DIAG["t"] > 60:
        _DIAG.update(t=now, n=0)
    _DIAG["n"] += 1
    if _DIAG["n"] <= 60:
        def text(v, n):
            return re.sub(r"[\x00-\x1f]+", " ", str(v if v is not None else ""))[:n]
        extra = {k: b[k] for k in ("dpr", "size") if k in b}
        print(f"client {text(b.get('kind'), 24)}: {text(b.get('message'), 600)} [{text(b.get('page'), 80)}]"
              + (f" {json.dumps(extra)}" if extra else ""), file=sys.stderr, flush=True)
    h.json({"ok": True})


# ---------------------------------------------------------------- the 3D city (Live maps)
@route("GET", r"/api/citymap")
def api_citymap_status(h, q):
    h.json(h.studio.citymap_status())


@route("POST", r"/api/citymap/build")
def api_citymap_build(h, q):
    h.json(h.studio.citymap_build(force=bool(h.body_json().get("force"))).summary())


@route("GET", r"/api/citymap/(citymap\.json|citymap\.bin)")
def api_citymap_file(h, q, name):
    ctype = "application/json" if name.endswith(".json") else "application/octet-stream"
    h.file(h.studio.citymap_file(name), ctype, immutable=bool(q.get("k")))


@route("GET", r"/api/city/detail")
def api_city_detail(h, q):
    cam = None
    if all(k in q for k in ("cx", "cy", "cz")):
        cam = (_num(q, "cx", 0.0), _num(q, "cy", 0.0), _num(q, "cz", 0.0))
    h.json(h.studio.city_detail(_num(q, "x", 0.0), _num(q, "y", 0.0), near=_num(q, "near"), far=_num(q, "far"),
                                cam=cam, k=_num(q, "k"), reach=_num(q, "reach"), detail=_num(q, "d"),
                                max_meshes=_num(q, "m")))


@route("GET", r"/api/city/geo/([0-9a-f]{16})")
def api_city_geo(h, q, gid):
    h.file(h.studio.geo_file(gid), "application/octet-stream", immutable=True)


# ---------------------------------------------------------------- world edits
@route("GET", r"/api/world/status")
def api_world_status(h, q):
    h.json(h.studio.world_index_status())


@route("POST", r"/api/world/prepare")
def api_world_prepare(h, q):
    h.json(h.studio.world_prepare(force=bool(h.body_json().get("force"))).summary())


def _num(q, k, default=None):
    v = q.get(k)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        raise HttpError(400, f"{k}: a number") from None


@route("GET", r"/api/world/query")
def api_world_query(h, q):
    kinds = [k for k in (q.get("kinds") or "").split(",") if k]
    h.json(h.studio.world_query(_num(q, "x"), _num(q, "y"), _num(q, "radius", 40.0), z=_num(q, "z"), dz=_num(q, "dz"),
                                kinds=kinds or None, text=q.get("text") or None, limit=int(_num(q, "limit", 400))))


@route("GET", r"/api/world/city")
def api_world_city(h, q):
    h.json(h.studio.night_city())


@route("GET", r"/api/world/object")
def api_world_object(h, q):
    el = q.get("element")
    h.json(h.studio.world_object(q.get("sector", ""), int(_num(q, "index", 0)), None if el in (None, "") else int(el)))


@route("GET", r"/api/projects/([a-f0-9]{12})/world")
def api_world_state(h, q, pid):
    h.json(h.studio.world_state(pid))


@route("PATCH", r"/api/projects/([a-f0-9]{12})/world")
def api_world_patch(h, q, pid):
    b = h.body_json()
    h.json(h.studio.world_set(pid, name=b.get("name"), display=b.get("display"), center=b.get("center"),
                              city=b.get("city")))


@route("POST", r"/api/projects/([a-f0-9]{12})/world/edits")
def api_world_apply(h, q, pid):
    h.json(h.studio.world_apply(pid, h.body_json()))


@route("DELETE", r"/api/projects/([a-f0-9]{12})/world/edits/(g[a-f0-9]{10})")
def api_world_drop(h, q, pid, group):
    h.json(h.studio.world_drop(pid, group))


@route("POST", r"/api/projects/([a-f0-9]{12})/world/(undo|redo)")
def api_world_undo(h, q, pid, which):
    h.json(h.studio.world_undo(pid) if which == "undo" else h.studio.world_redo(pid))


@route("POST", r"/api/projects/([a-f0-9]{12})/world/game")
def api_world_game(h, q, pid):
    b = h.body_json()
    h.json(h.studio.world_game(pid, build=b.get("build"), uninstall=bool(b.get("uninstall"))).summary())


@route("POST", r"/api/projects/([a-f0-9]{12})/handling/preview")
def api_handling_preview(h, q, pid):
    h.json(h.studio.handling_preview(pid, h.body_json()))


@route("POST", r"/api/projects/([a-f0-9]{12})/chime")
def api_chime_upload(h, q, pid):
    stream, n = h.raw_body()
    h.json(h.studio.save_chime(pid, q.get("name", ""), stream, n))


@route("GET", r"/api/projects/([a-f0-9]{12})/chime")
def api_chime_get(h, q, pid):
    p = h.studio.projects.get(pid)
    f = p.profile()["chime"].get("file")
    if not f or not os.path.isfile(f):
        raise HttpError(404, "no chime")
    h.file(f, "audio/mpeg" if f.lower().endswith(".mp3") else "audio/wav")


@route("POST", r"/api/projects/([a-f0-9]{12})/build")
def api_build(h, q, pid):
    h.json(h.studio.build(pid).summary())


@route("GET", r"/api/projects/([a-f0-9]{12})/builds")
def api_builds(h, q, pid):
    p = h.studio.projects.get(pid)
    out = list(reversed(p.data.get("builds", [])))
    if out:                       # the last build goes stale when the NCWE imports change after it
        out[0] = {**out[0], "stale": h.studio.map_stale(p)}
    h.json(out)


@route("GET", r"/api/projects/([a-f0-9]{12})/builds/(\d+)")
def api_build_info(h, q, pid, n):
    info = h.studio.build_info(pid, n)
    info.pop("out", None)
    h.json(info)


@route("POST", r"/api/projects/([a-f0-9]{12})/builds/(\d+)/qa")
def api_build_qa(h, q, pid, n):
    h.json(h.studio.run_qa(pid, n))


@route("GET", r"/api/projects/([a-f0-9]{12})/builds/(\d+)/download")
def api_build_download(h, q, pid, n):
    path = h.studio.export_build(pid, n)
    h.file(path, "application/zip", os.path.basename(path))


@route("POST", r"/api/projects/([a-f0-9]{12})/deploy")
def api_deploy(h, q, pid):
    b = h.body_json()
    job = h.studio.deploy(pid, str(b.get("server", "")), names=b.get("names") or None, mode=b.get("mode"),
                          build=b.get("build"), include_hub=bool(b.get("include_hub")),
                          keep_placement=b.get("keep_placement", True) is not False, restart=b.get("restart", True) is not False,
                          baker=b.get("baker", True) is not False)
    h.json(job.summary())


@route("POST", r"/api/projects/([a-f0-9]{12})/rebuild_deploy")
def api_rebuild_deploy(h, q, pid):
    b = h.body_json()
    job = h.studio.map_rebuild_deploy(pid, str(b.get("server") or "").strip() or None,
                                      names=b.get("names") or None,
                                      keep_placement=b.get("keep_placement", True) is not False,
                                      restart=b.get("restart", True) is not False,
                                      baker=b.get("baker", True) is not False)
    h.json(job.summary())


@route("POST", r"/api/projects/([a-f0-9]{12})/open")
def api_open_folder(h, q, pid):
    p = h.studio.projects.get(pid)
    what = h.body_json().get("what", "project")
    target = p.dir
    if what == "build":
        b = p.latest_build()
        if b:
            target = os.path.join(p.build_dir(b["n"]), "out")
    if os.name == "nt":
        os.startfile(target)      # noqa: S606 - the user's own folder, from their own click
        h.json({"ok": True})
    else:
        h.json({"ok": False, "path": target})


# ---------------------------------------------------------------- servers
@route("GET", r"/api/servers")
def api_servers(h, q):
    h.json(h.studio.redacted_settings()["servers"])


@route("POST", r"/api/servers")
def api_server_put(h, q):
    b = h.body_json()
    password = b.pop("password", None)
    passphrase = b.pop("passphrase", None)
    for k in ("password_set", "passphrase_set"):
        b.pop(k, None)
    s = h.studio.settings.put_server(b)
    h.studio.links.forget(s["id"])
    if password is not None:
        h.studio.set_secret(f"server:{s['id']}:password", password or None)
    if passphrase is not None:
        h.studio.set_secret(f"server:{s['id']}:passphrase", passphrase or None)
    h.json([x for x in h.studio.redacted_settings()["servers"] if x["id"] == s["id"]][0])


@route("POST", r"/api/servers/import")
def api_server_import(h, q):
    """A JSON list of server entries (no passwords): adds the ones whose id is new."""
    b = h.body_json()
    items = b.get("servers") if isinstance(b, dict) else b
    if not isinstance(items, list):
        raise HttpError(400, "expected a list of servers")
    have = {x["id"] for x in h.studio.settings.get()["servers"]}
    added, skipped, errors = [], [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        item = {k: v for k, v in item.items() if k not in ("password", "passphrase", "password_set", "passphrase_set")}
        if item.get("id") in have:
            skipped.append(item.get("id"))
            continue
        try:
            added.append(h.studio.settings.put_server(item)["id"])
        except SettingsError as e:
            errors.append(f"{item.get('id')}: {'; '.join(e.errors)}")
    h.json({"added": added, "skipped": skipped, "errors": errors})


@route("DELETE", r"/api/servers/([a-z0-9_-]{1,32})")
def api_server_delete(h, q, sid):
    ok = h.studio.settings.delete_server(sid)
    h.studio.links.forget(sid)
    h.studio.set_secret(f"server:{sid}:password", None)
    h.studio.set_secret(f"server:{sid}:passphrase", None)
    h.json({"ok": ok})


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/world/remove")
def api_server_world_remove(h, q, sid):
    b = h.body_json()
    job = h.studio.world_remove(sid, str(b.get("resource") or ""), names=b.get("names") or None, restart=bool(b.get("restart")))
    h.json(job.summary())


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/(test|status|restart|hub|agent-api)")
def api_server_action(h, q, sid, action):
    b = h.body_json()
    job = h.studio.server_job(sid, action, names=b.get("names") or None, mode=b.get("mode", "stage"),
                              force=bool(b.get("force")), enable=b.get("enable", True) is not False,
                              restart=bool(b.get("restart")))
    h.json(job.summary())


# ---------------------------------------------------------------- quality checks (qcsweep.py)
@route("GET", r"/api/qc")
def api_qc(h, q):
    h.json(h.studio.qc.view())


@route("POST", r"/api/qc/run")
def api_qc_run(h, q):
    b = h.body_json() or {}
    only = b.get("projects")
    if only is not None and not (isinstance(only, list) and all(isinstance(x, str) for x in only)):
        raise HttpError(400, "projects: a list of project ids")
    rebuild = b.get("rebuild")
    job = h.studio.qc.run_job(rebuild=None if rebuild is None else bool(rebuild), only=only or None)
    h.json(job.summary())


@route("POST", r"/api/qc/schedule")
def api_qc_schedule(h, q):
    from .qcsweep import install_schedule
    b = h.body_json() or {}
    try:
        h.json(install_schedule(int(b.get("hours") or h.studio.settings.get()["qc"]["interval_hours"]),
                                enable=b.get("enable") is not False))
    except RuntimeError as e:
        raise HttpError(400, str(e))


# ---------------------------------------------------------------- fleet: versions and the one-click upgrade
@route("POST", r"/api/fleet/(status|upgrade)")
def api_fleet(h, q, action):
    b = h.body_json() or {}
    if action == "upgrade" and b.get("confirm") is not True:
        raise HttpError(400, "the fleet upgrade needs confirm: true")
    job = h.studio.fleet_job(action, confirm=b.get("confirm") is True)
    h.json(job.summary())


# ---------------------------------------------------------------- live map control (the manager's agent API)
@route("GET", r"/api/servers/([a-z0-9_-]{1,32})/live")
def api_live_status(h, q, sid):
    h.json(h.studio.live_status(sid, q.get("name")))


@route("GET", r"/api/servers/([a-z0-9_-]{1,32})/live/players")
def api_live_players(h, q, sid):
    h.json(h.studio.live_players(sid, q.get("name")))


@route("GET", r"/api/servers/([a-z0-9_-]{1,32})/live/maps/([a-z][a-z0-9_]{2,63})")
def api_live_map(h, q, sid, map_name):
    h.json(h.studio.live_map(sid, q.get("name"), map_name))


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/live/maps/([a-z][a-z0-9_]{2,63})/wait")
def api_live_wait(h, q, sid, map_name):
    b = h.body_json()
    h.json(h.studio.live_wait(sid, q.get("name") or b.get("name"), map_name, until=b.get("until", "applied"),
                              timeout=min(3600, max(10, int(b.get("timeout", 900))))).summary())


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/live/maps/([a-z][a-z0-9_]{2,63})/([A-Za-z]{3,16})")
def api_live_op(h, q, sid, map_name, op):
    b = h.body_json()
    h.json(h.studio.live_op(sid, q.get("name"), map_name, op, b))


@route("GET", r"/api/servers/([a-z0-9_-]{1,32})/logs")
def api_server_logs(h, q, sid):
    text = h.studio.server_logs(sid, q.get("name", ""), int(q.get("lines", 200)))
    h.json({"text": text})


# ---------------------------------------------------------------- jobs + events
@route("GET", r"/api/jobs")
def api_jobs(h, q):
    h.json(h.studio.jobs.list(q.get("project")))


@route("GET", r"/api/jobs/([a-f0-9]{12})")
def api_job(h, q, jid):
    j = h.studio.jobs.get(jid)
    if not j:
        raise HttpError(404, "no such job")
    h.json(j.detail(int(q.get("since", 0))))


@route("POST", r"/api/jobs/([a-f0-9]{12})/cancel")
def api_job_cancel(h, q, jid):
    j = h.studio.jobs.get(jid)
    if not j:
        raise HttpError(404, "no such job")
    ok = j.cancel()
    h.json({"cancelled": ok, **({"stops_after": j.cancel_late} if ok and j.cancel_late else {})})


@route("GET", r"/api/events")
def api_events(h, q):
    sub = h.studio.bus.subscribe()
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "keep-alive")
        h.end_headers()
        h.wfile.write(b": hello\n\n")
        h.wfile.flush()
        h.close_connection = True
        last = time.time()
        while not h.server.stopping:
            try:
                ev = sub.get(timeout=1.0)
            except queue.Empty:
                if time.time() - last > 15:
                    h.wfile.write(b": ping\n\n")
                    h.wfile.flush()
                    last = time.time()
                continue
            h.wfile.write(b"data: " + jsonutil.dumps_bytes(ev) + b"\n\n")
            h.wfile.flush()
            last = time.time()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        h.studio.bus.unsubscribe(sub)


# ---------------------------------------------------------------- AI agent
def _agents(h):
    if h.studio.agents is None:
        raise HttpError(503, "the AI agent is not available")
    return h.studio.agents


@route("GET", r"/api/ai/models")
def api_ai_models(h, q):
    h.json(_agents(h).list_models(q.get("provider")))


@route("POST", r"/api/ai/test")
def api_ai_test(h, q):
    h.json(_agents(h).test(h.body_json().get("provider")))


@route("GET", r"/api/agent/sessions")
def api_agent_sessions(h, q):
    h.json(_agents(h).list(q.get("project")))


@route("POST", r"/api/agent/sessions")
def api_agent_start(h, q):
    b = h.body_json()
    h.json(_agents(h).start(project=b.get("project"), goal=str(b.get("goal") or ""), autonomy=b.get("autonomy"),
                            provider=b.get("provider")))


@route("GET", r"/api/agent/sessions/([a-f0-9]{12})")
def api_agent_get(h, q, sid):
    h.json(_agents(h).get(sid, int(q.get("since", 0))))


@route("POST", r"/api/agent/sessions/([a-f0-9]{12})/message")
def api_agent_msg(h, q, sid):
    h.json(_agents(h).message(sid, str(h.body_json().get("text") or "")))


@route("POST", r"/api/agent/sessions/([a-f0-9]{12})/approve")
def api_agent_approve(h, q, sid):
    b = h.body_json()
    h.json(_agents(h).approve(sid, str(b.get("call_id") or ""), bool(b.get("approve")), b.get("note")))


@route("POST", r"/api/agent/sessions/([a-f0-9]{12})/stop")
def api_agent_stop(h, q, sid):
    h.json(_agents(h).stop(sid))


# ---------------------------------------------------------------- MCP (agents outside the Studio)
@route("GET", r"/api/mcp")
def api_mcp(h, q):
    from . import mcpclients
    hub = h.studio.mcp
    port = h.server.server_address[1]
    h.json({"settings": h.studio.settings.get()["mcp"], "url": f"http://127.0.0.1:{port}/mcp",
            "token_file": __import__("os").path.join(h.studio.workspace, "mcp", "token"),
            "command": mcpclients.bridge_command(h.studio.workspace), "sessions": hub.sessions_view(),
            "pending": hub.pending_view(), "clients": mcpclients.status_all(h.studio.workspace),
            "tools": len(hub.tools)})


@route("GET", r"/api/mcp/token")
def api_mcp_token(h, q):
    h.json({"token": h.studio.mcp.token})


@route("POST", r"/api/mcp/token/reset")
def api_mcp_token_reset(h, q):
    from .mcp import reset_token
    reset_token(h.studio.workspace)
    h.json({"ok": True})


@route("POST", r"/api/mcp/approve")
def api_mcp_approve(h, q):
    b = h.body_json()
    h.json({"ok": h.studio.mcp.decide(str(b.get("id") or ""), bool(b.get("approve")), b.get("note"))})


@route("DELETE", r"/api/mcp/sessions/([a-f0-9]{12})")
def api_mcp_end(h, q, short):
    hub = h.studio.mcp
    full = next((s["id"] for s in hub.sessions_view() if s["id"] == short), None)
    ended = False
    with hub.lock:
        for k in list(hub.sessions):
            if k.startswith(short):
                hub.sessions.pop(k, None)
                ended = True
    h.json({"ok": ended or bool(full)})


@route("POST", r"/api/mcp/connect")
def api_mcp_connect(h, q):
    from . import mcpclients
    b = h.body_json()
    h.json(mcpclients.connect(str(b.get("client") or ""), h.studio.workspace, remove=bool(b.get("remove"))))


# ---------------------------------------------------------------- the terminal (terminal.py)
def _term_errors(fn):
    from .terminal import TermError

    def wrapped(h, q, *a):
        try:
            return fn(h, q, *a)
        except TermError as e:
            raise HttpError(400, str(e)) from e
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


@route("GET", r"/api/terminal/spec")
@_term_errors
def api_term_spec(h, q):
    h.json(h.studio.terminal.spec())


@route("POST", r"/api/terminal/sessions")
@_term_errors
def api_term_session(h, q):
    b = h.body_json() or {}
    h.json(h.studio.terminal.session(str(b.get("id") or "") or None).info())


@route("GET", r"/api/terminal/sessions/([a-f0-9]{12})")
@_term_errors
def api_term_scrollback(h, q, sid):
    h.json(h.studio.terminal.scrollback(sid, int(q.get("since", 0)), int(q.get("limit", 600))))


@route("POST", r"/api/terminal/run")
@_term_errors
def api_term_run(h, q):
    b = h.body_json() or {}
    t = h.studio.terminal
    s = t.session(str(b.get("session") or "") or None)
    r = t.run(s, str(b.get("line") or ""))
    h.json({"run": r.id, "session": s.id})


@route("GET", r"/api/terminal/runs/([a-f0-9]{12})")
@_term_errors
def api_term_run_view(h, q, rid):
    h.json(h.studio.terminal.run_view(rid, int(q.get("since", 0))))


@route("POST", r"/api/terminal/runs/([a-f0-9]{12})/cancel")
@_term_errors
def api_term_cancel(h, q, rid):
    h.json({"cancelled": h.studio.terminal.cancel(rid)})


@route("GET", r"/api/terminal/history")
def api_term_history(h, q):
    h.json(h.studio.terminal.history[-max(1, min(1000, int(q.get("n", 200)))):])


# ====================================================================== the API, called inside the Studio
ROUTE_ABOUT = {
    "api_health": "is the Studio up", "api_state": "everything the window shows at start: settings (no secrets), tools, "
    "projects, jobs, quality", "api_settings": "the settings (no secrets)", "api_settings_patch": "change settings (a JSON "
    "merge patch)", "api_secret": "set a secret (the user's)", "api_tools": "WolvenKit, .NET, 7-Zip, the game and the donor "
    "files: found or not", "api_tools_donor": "extract the donor cars' files from the game", "api_sounds": "Cyberpunk engine "
    "sounds a car can use", "api_donors": "the donor cars", "api_profile_schema": "the car profile's schema and defaults",
    "api_ncwe_assets": "a map's NCWE imports", "api_ncwe_asset_contents": "what one NCWE import holds",
    "api_ncwe_asset_add": "add an NCWE export to a map (upload)", "api_ncwe_asset_remove": "take an NCWE import off a map",
    "api_ncwe_import": "import an NCWE export from a path", "api_upload": "upload a file (the window's drop zone)",
    "api_packs": "the car packs", "api_pack_create": "import a car pack (path)", "api_pack": "one car pack and its cars",
    "api_pack_import": "make projects of a pack's cars", "api_pack_build": "build a pack's cars",
    "api_pack_deploy": "install a pack's cars on a server", "api_server_all_cars": "install every good car on a server "
    "(car bundles)", "api_pack_delete": "delete a car pack", "api_projects": "every project", "api_project_create":
    "import a FiveM car or map (path) as a project", "api_project": "one project", "api_project_patch": "rename a project and "
    "the like", "api_project_delete": "delete a project", "api_profile": "a car's profile (saved and effective)",
    "api_profile_put": "replace a car's profile", "api_profile_patch": "change a car's profile (JSON merge patch)",
    "api_profile_validate": "check a profile without saving it", "api_inspect": "what the converter sees in a car",
    "api_preview": "a car's 3D preview (glb)", "api_render": "a car render (png: view, mode)", "api_sheet": "a car's contact "
    "sheet (png)", "api_neon_sheet": "a car's neon kit renders (png)", "api_thumb": "a project's thumbnail (png)",
    "api_map_state": "a map: look, options, districts, builds", "api_map_options_patch": "change a map's options",
    "api_map_options_put": "replace a map's options", "api_map_options_validate": "check map options without saving",
    "api_map_city": "a whole-city map's districts", "api_map_district": "pick a district of a city map",
    "api_map_look": "look at a map (its first read)", "api_map_render": "a map render (png: view, mode, size)",
    "api_map_glb": "a map's 3D preview (glb)", "api_map_game": "install a map in this PC's game (test boot)",
    "api_diag": "the window's error reports", "api_citymap_status": "the Live maps 3D city: built or not",
    "api_citymap_build": "build the Live maps 3D city from the game", "api_citymap_file": "the 3D city's files",
    "api_world_status": "Night City's world index", "api_world_prepare": "build Night City's world index",
    "api_world_query": "Night City's objects around a spot", "api_world_object": "one Night City object in detail",
    "api_world_state": "a world edit", "api_world_patch": "rename a world edit", "api_world_apply": "add changes to a "
    "world edit", "api_world_drop": "drop one change of a world edit", "api_world_undo": "undo or redo in a world edit",
    "api_world_game": "install a world edit in this PC's game", "api_handling_preview": "a car's handling with overrides "
    "(not saved)", "api_chime_upload": "a car's door chime (upload)", "api_chime_get": "a car's door chime",
    "api_build": "build a project", "api_builds": "a project's builds", "api_build_info": "one build's report",
    "api_build_qa": "check a build again", "api_build_download": "a build as a zip", "api_deploy": "install a project's "
    "build on a server", "api_rebuild_deploy": "rebuild a map and install it", "api_open_folder": "open a project's "
    "folder in Explorer", "api_servers": "the servers (no secrets)", "api_server_put": "add or change a server",
    "api_server_import": "add servers from a list", "api_server_delete": "remove a server", "api_server_world_remove":
    "take a world edit off a server", "api_server_action": "a server job: test, status, restart, hub, agent-api",
    "api_qc": "the latest quality sweep", "api_qc_run": "run a quality sweep", "api_qc_schedule": "the Windows "
    "background sweep", "api_fleet": "the servers' versions; upgrade them all (confirm)", "api_live_status":
    "a server's live maps", "api_live_players": "players online and where", "api_live_map": "one live map",
    "api_live_wait": "wait for a map move", "api_live_op": "a live map operation (spot, preset, apply, cancel, goto)",
    "api_server_logs": "a server's log", "api_jobs": "jobs", "api_job": "one job: log, result", "api_job_cancel":
    "stop a job", "api_events": "the live event stream (the window)", "api_ai_models": "the AI models a provider offers",
    "api_ai_test": "test the AI provider", "api_agent_sessions": "the AI agent's sessions", "api_agent_start":
    "start the AI agent", "api_agent_get": "an AI agent session and its events", "api_agent_msg": "write to the AI agent",
    "api_agent_approve": "decide what the AI agent waits on", "api_agent_stop": "stop the AI agent", "api_mcp": "agents "
    "outside the Studio: settings, sessions, apps", "api_mcp_token": "the MCP token", "api_mcp_token_reset": "a new MCP "
    "token", "api_mcp_approve": "decide an agent's call (the user)", "api_mcp_end": "end an agent's session",
    "api_mcp_connect": "connect an app to the Studio", "api_term_spec": "the terminal's commands, tools and names",
    "api_term_session": "a terminal session", "api_term_scrollback": "a terminal session's output",
    "api_term_run": "run a terminal command line", "api_term_run_view": "a terminal run's output",
    "api_term_cancel": "stop a terminal run (Ctrl+C)", "api_term_history": "the commands typed in the terminal",
}
_GROUP_NAMES = (("projects", "project"), ("packs", "pack"), ("jobs", "job"), ("sessions", "session"), ("servers", "server"),
                ("maps", "map"), ("builds", "n"), ("edits", "group"), ("ncwe_assets", "name"), ("runs", "run"))


def route_template(rx) -> str:
    """/api/projects/{project}/builds/{n} for a route's pattern."""
    pat = rx.pattern[1:-1] if hasattr(rx, "pattern") else str(rx)
    names = dict(_GROUP_NAMES)
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            out.append(pat[i + 1])
            i += 2
            continue
        if c != "(":
            out.append(c)
            i += 1
            continue
        depth, j = 1, i + 1
        while j < len(pat) and depth:
            depth += {"(": 1, ")": -1}.get(pat[j], 0)
            j += 1
        inner = pat[i + 1:j - 1]
        prev = "".join(out).rstrip("/").rsplit("/", 1)[-1]
        if "|" in inner and re.fullmatch(r"[\w\-.\\|]+", inner):
            name = inner.replace("\\", "")
        elif inner.startswith("[A-Za-z]"):
            name = "op"
        else:
            name = names.get(prev, "value")
        out.append("{" + name + "}")
        i = j
    return "".join(out)


def route_list() -> list:
    return [{"method": m, "path": route_template(rx), "about": ROUTE_ABOUT.get(fn.__name__) or
             ((fn.__doc__ or "").strip().splitlines() or [fn.__name__.replace("api_", "").replace("_", " ")])[0]}
            for m, rx, fn in ROUTES]


class _NoServer:
    stopping = False

    def __init__(self, port=0):
        self.server_address = ("127.0.0.1", port)


class _InProcess:
    """A route's view of one API call made inside the Studio (the terminal's `api`, the studio_api
    MCP tool): the same handlers, no socket."""

    def __init__(self, studio, method, body):
        self.studio = studio
        self.command = method
        self._body = body
        self.server = _NoServer(int(getattr(studio, "http_port", 0) or 0))
        self.headers = {}
        self.answer = None
        self.close_connection = True

    def json(self, obj, status=200, headers=None):
        self.answer = {"status": status, "json": json.loads(json.dumps(obj, default=str))}

    def _send(self, status, body: bytes, ctype="application/json", headers=None):
        self.answer = {"status": status, "content_type": ctype.split(";")[0], "data": bytes(body or b"")}

    def file(self, path, ctype=None, download_name=None, immutable=False):
        if not os.path.isfile(path):
            raise HttpError(404, "not found")
        self.answer = {"status": 200, "file": path, "bytes": os.path.getsize(path),
                       "content_type": ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"}
        if (self.answer["content_type"] or "").startswith("image/") and self.answer["bytes"] <= 8 << 20:
            with open(path, "rb") as f:
                self.answer["data"] = f.read()

    def body_json(self):
        if self._body in (None, ""):
            return {}
        if isinstance(self._body, (dict, list)):
            return json.loads(json.dumps(self._body))
        raise HttpError(400, "the body must be a JSON object")

    def raw_body(self, limit=0):
        raise HttpError(400, "uploads are not offered here: give the Studio a path on this PC instead")

    def send_response(self, *a):
        raise HttpError(400, "a live stream cannot be called here")


def call_route(studio, method: str, path: str, body=None, query=None) -> dict:
    """One API call inside the Studio: {"status", "json"} or {"status", "content_type", "data"/"file"}."""
    url = urllib.parse.urlsplit(path)
    p = url.path if url.path.startswith("/api/") else "/api/" + url.path.lstrip("/")
    q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
    q.update({str(k): str(v) for k, v in (query or {}).items()})
    m = method.upper()
    h = _InProcess(studio, m, body)
    for want, rx, fn in ROUTES:
        if want != m and not (want == "GET" and m == "HEAD"):
            continue
        mm = rx.match(p)
        if not mm:
            continue
        if fn.__name__ == "api_events":
            return {"status": 400, "json": {"error": "the live event stream cannot be called here"}}
        try:
            fn(h, q, *mm.groups())
        except Exception as e:  # noqa: BLE001 - answered as the HTTP API would
            ans = error_answer(e)
            if ans is None:
                traceback.print_exc()
                ans = 500, {"error": f"{e.__class__.__name__}: {e}"}
            return {"status": ans[0], "json": ans[1]}
        return h.answer or {"status": 204, "json": None}
    return {"status": 404, "json": {"error": f"no such endpoint: {m} {p}"}}


# ====================================================================== run
class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, studio, token):
        super().__init__(addr, Handler)
        self.studio = studio
        self.token = token
        self.stopping = False


def instance_file(workspace: str) -> str:
    return os.path.join(workspace, "run", "studio.json")


def running_instance(workspace: str) -> dict | None:
    """The Studio serving this workspace now ({pid, port, url, ...}), or None."""
    try:
        with open(instance_file(workspace), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if not _alive(info.get("pid")):
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/api/health", timeout=3) as r:
            ok = json.loads(r.read() or b"{}").get("ok")
    except (OSError, ValueError, KeyError):
        return None
    return info if ok else None


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE                         # a handle, not a 32-bit int
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)                          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                                      # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def serve(host="127.0.0.1", port=0, workspace=None, token=None, ready_file=None, open_browser=False, idle_exit=0,
          on_ready=None):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("the Studio only listens on this machine (127.0.0.1)")
    ws = os.path.abspath(workspace or os.environ.get("F2O77_WORKSPACE") or default_workspace())
    other = running_instance(ws)
    if other and int(other.get("pid") or 0) != os.getpid():
        raise SystemExit(f"another Studio is running on this workspace (pid {other.get('pid')}, port {other.get('port')}): "
                         "close it first")
    studio = Studio(ws)
    try:
        from .agent.manager import AgentManager
        studio.agents = AgentManager(studio)
    except ImportError as e:   # pragma: no cover
        print(f"AI agent unavailable: {e}", file=sys.stderr)
    token = token or os.environ.get("F2O77_TOKEN") or pysecrets.token_urlsafe(24)
    httpd = StudioServer((host, port), studio, token)
    real_port = httpd.server_address[1]
    studio.http_port = real_port
    url = f"http://127.0.0.1:{real_port}/auth?token={token}"
    info = {"port": real_port, "token": token, "url": url, "pid": os.getpid(), "workspace": studio.workspace}
    if ready_file:
        tmp = ready_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, ready_file)
    # where agents (the MCP bridge) find this Studio: no UI token in it (they use the MCP token)
    from .app import APP_VERSION
    inst = instance_file(studio.workspace)
    os.makedirs(os.path.dirname(inst), exist_ok=True)
    with open(inst + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": real_port, "mcp": f"http://127.0.0.1:{real_port}/mcp",
                   "version": APP_VERSION, "started": time.time(), "headless": bool(idle_exit)}, f)
    os.replace(inst + ".tmp", inst)
    if idle_exit:
        threading.Thread(target=_idle_watch, args=(httpd, studio, float(idle_exit)), daemon=True).start()
    studio.qc.start()                   # quality sweeps on their clock (qcsweep.py)
    if on_ready is not None:            # tests: the server and its Studio
        on_ready(httpd, studio)
    print(f"fivem2open77 Studio on http://127.0.0.1:{real_port}/  (open {url})", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stopping = True
        httpd.server_close()
        studio.close()
        try:
            with open(inst, encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(inst)
        except (OSError, ValueError):
            pass
    return 0


def _idle_watch(httpd, studio, seconds):
    """A Studio started for agents (no window) ends after `seconds` with no agent call, no open page and
    no job running."""
    last = time.time()
    while not httpd.stopping:
        time.sleep(5)
        busy = (studio.mcp.last_activity > last or any(j["state"] in ("queued", "running") for j in studio.jobs.list())
                or len(studio.bus._subs) > 0)
        if busy:
            last = max(time.time(), studio.mcp.last_activity)
            continue
        if time.time() - last > seconds:
            print(f"no agent call, page or job for {int(seconds)} s: stopping", flush=True)
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            return


def main(args=None):
    return serve(host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 0),
                 workspace=getattr(args, "workspace", None), token=getattr(args, "token", None),
                 ready_file=getattr(args, "ready_file", None), open_browser=getattr(args, "open", False),
                 idle_exit=getattr(args, "idle_exit", 0) or 0)
