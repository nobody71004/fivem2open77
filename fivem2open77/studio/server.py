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
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import profile as profile_mod
from ..cp77 import sound as sound_mod
from ..cp77.donor import DONORS
from .app import Studio
from .paths import default_workspace, web_dir
from .projects import ProjectError
from .settings import SettingsError

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
        self._send(status, json.dumps(obj, default=str).encode("utf-8"), headers=headers)

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
        except HttpError as e:
            self.json({"error": str(e), **e.extra}, status=e.status)
        except profile_mod.ProfileError as e:
            self.json({"error": "invalid profile", "errors": e.errors}, status=400)
        except SettingsError as e:
            self.json({"error": "invalid settings", "errors": e.errors}, status=400)
        except (ProjectError, ValueError, KeyError) as e:
            self.json({"error": str(e) or e.__class__.__name__}, status=400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self.json({"error": f"{e.__class__.__name__}: {e}"}, status=500)
            except OSError:
                pass
        finally:
            self._drain()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _dispatch

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

    def file(self, path, ctype=None, download_name=None):
        if not os.path.isfile(path):
            raise HttpError(404, "not found")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
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

    def raw_body(self):
        self._body_read = True
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise HttpError(400, "empty upload")
        if n > 4 * 1024 * 1024 * 1024:
            raise HttpError(413, "upload too large")
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


# ---------------------------------------------------------------- uploads / projects
@route("POST", r"/api/uploads")
def api_upload(h, q):
    stream, n = h.raw_body()
    info = h.studio.save_upload(q.get("name", "upload"), stream, n)
    info.pop("path", None)
    h.json(info)


@route("GET", r"/api/projects")
def api_projects(h, q):
    h.json(h.studio.projects.list())


@route("POST", r"/api/projects")
def api_project_create(h, q):
    b = h.body_json()
    p, job = h.studio.create_project(path=b.get("path"), upload_id=b.get("upload_id"), name=b.get("name"))
    h.json({"project": p.summary(), "job": job.summary()})


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
    h.studio.analyzer.forget(pid)
    h.studio.projects.delete(pid)
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
        h.studio.analyzer.session(p).thumbnail(p.profile())
    h.file(path, "image/png")


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
    h.json(list(reversed(h.studio.projects.get(pid).data.get("builds", []))))


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
                          build=b.get("build"), include_hub=bool(b.get("include_hub")))
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
    h.studio.set_secret(f"server:{sid}:password", None)
    h.studio.set_secret(f"server:{sid}:passphrase", None)
    h.json({"ok": ok})


@route("POST", r"/api/servers/([a-z0-9_-]{1,32})/(test|status|restart|hub)")
def api_server_action(h, q, sid, action):
    b = h.body_json()
    job = h.studio.server_job(sid, action, names=b.get("names") or None, mode=b.get("mode", "stage"),
                              force=bool(b.get("force")))
    h.json(job.summary())


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
    h.json({"cancelled": j.cancel()})


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
            h.wfile.write(b"data: " + json.dumps(ev, default=str).encode("utf-8") + b"\n\n")
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


# ====================================================================== run
class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, studio, token):
        super().__init__(addr, Handler)
        self.studio = studio
        self.token = token
        self.stopping = False


def serve(host="127.0.0.1", port=0, workspace=None, token=None, ready_file=None, open_browser=False):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("the Studio only listens on this machine (127.0.0.1)")
    studio = Studio(workspace or os.environ.get("F2O77_WORKSPACE") or default_workspace())
    try:
        from .agent.manager import AgentManager
        studio.agents = AgentManager(studio)
    except ImportError as e:   # pragma: no cover
        print(f"AI agent unavailable: {e}", file=sys.stderr)
    token = token or os.environ.get("F2O77_TOKEN") or pysecrets.token_urlsafe(24)
    httpd = StudioServer((host, port), studio, token)
    real_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{real_port}/auth?token={token}"
    info = {"port": real_port, "token": token, "url": url, "pid": os.getpid(), "workspace": studio.workspace}
    if ready_file:
        tmp = ready_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, ready_file)
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
    return 0


def main(args=None):
    return serve(host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 0),
                 workspace=getattr(args, "workspace", None), token=getattr(args, "token", None),
                 ready_file=getattr(args, "ready_file", None), open_browser=getattr(args, "open", False))
