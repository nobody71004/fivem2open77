"""Studio application state and operations (everything the API and the AI agent can do)."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid

from .. import profile as profile_mod
from ..cp77 import sound as sound_mod
from ..cp77.donor import DONORS, DonorCache
from ..cp77.tweak import handling_profile
from ..pipeline import Converter, VERSION
from . import deploy as deploy_mod
from . import qa as qa_mod
from .analysis import Analyzer
from .jobs import EventBus, JobManager
from .mapops import MapOps
from .maplink import LinkPool
from .projects import ProjectError, ProjectStore
from .secrets import SecretStore
from .settings import Settings
from .tools import Toolbox
from .worldops import WorldOps

APP_VERSION = "1.3.1"
SECRET_KEYS = re.compile(r"^(anthropic_api_key|server:[a-z0-9_-]{1,32}:(password|passphrase))$")


class Studio(MapOps, WorldOps):
    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self.settings = Settings(os.path.join(self.workspace, "settings.json"))
        self.secrets = SecretStore(os.path.join(self.workspace, "secrets.json"))
        self.bus = EventBus()
        self.jobs = JobManager(self.bus)
        self.projects = ProjectStore(os.path.join(self.workspace, "projects"))
        self.analyzer = Analyzer()
        self.uploads_dir = os.path.join(self.workspace, "uploads")
        os.makedirs(self.uploads_dir, exist_ok=True)
        deploy_mod.Remote.known_hosts = os.path.join(self.workspace, "known_hosts")
        self._toolbox = None
        self._tb_lock = threading.Lock()
        self.links = LinkPool()     # live map manager links (maplink.py)
        self.agents = None          # set by agent.manager
        from .mcp import McpHub
        self.mcp = McpHub(self)     # agents outside the Studio (mcp.py)

    # ------------------------------------------------------------------ tools
    def toolbox(self, refresh=False) -> Toolbox:
        with self._tb_lock:
            if self._toolbox is None or refresh:
                self._toolbox = Toolbox(self.settings.get(), self.workspace)
                self._toolbox.apply()
            return self._toolbox

    def tools_status(self, refresh=False) -> dict:
        return self.toolbox(refresh).status()

    def prepare_donors(self):
        def run(job):
            tb = self.toolbox(refresh=True)
            if not tb.game_dir:
                raise RuntimeError("Cyberpunk 2077 was not found: set its folder in Settings")
            wk = tb.wolvenkit_obj(log=job.log)
            job.on_cancel.append(wk.kill)
            cache = DonorCache(wk, tb.game_dir, tb.cache_dir, log=job.log)
            for i, name in enumerate(DONORS):
                job.set_progress(i / len(DONORS), f"donor {name}")
                job.check()
                cache.ensure(name)
                job.log(f"donor {name}: ready")
            return tb.status()["donor"]
        return self.jobs.submit("donor", "Prepare donor files from the game", run, heavy=True)

    # ------------------------------------------------------------------ state
    def redacted_settings(self) -> dict:
        s = self.settings.get()
        for srv in s["servers"]:
            srv["password_set"] = self.secrets.has(f"server:{srv['id']}:password")
            srv["passphrase_set"] = self.secrets.has(f"server:{srv['id']}:passphrase")
        s["ai"]["anthropic_key_set"] = self.secrets.has("anthropic_api_key")
        s["secrets_encrypted"] = self.secrets.encrypted
        return s

    def state(self) -> dict:
        return {"version": APP_VERSION, "converter": VERSION, "workspace": self.workspace,
                "settings": self.redacted_settings(), "tools": self.tools_status(),
                "projects": self.projects.list(), "jobs": self.jobs.list()[:50]}

    def set_secret(self, key: str, value: str | None):
        if not SECRET_KEYS.match(key or ""):
            raise ValueError("unknown secret")
        self.secrets.set(key, value)

    # ------------------------------------------------------------------ projects
    def save_upload(self, name: str, stream, length: int) -> dict:
        safe = re.sub(r"[^A-Za-z0-9._ ()+-]+", "_", os.path.basename(name or "upload"))[:120] or "upload"
        uid = uuid.uuid4().hex[:12]
        d = os.path.join(self.uploads_dir, uid)
        os.makedirs(d)
        path = os.path.join(d, safe)
        left = length
        with open(path, "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError("upload interrupted")
        return {"upload_id": uid, "name": safe, "bytes": length, "path": path}

    def upload_path(self, uid: str) -> str:
        if not re.match(r"^[a-f0-9]{12}$", uid or ""):
            raise ValueError("bad upload id")
        d = os.path.join(self.uploads_dir, uid)
        files = os.listdir(d) if os.path.isdir(d) else []
        if not files:
            raise ValueError("upload not found")
        return os.path.join(d, files[0])

    def create_project(self, *, path: str | None = None, upload_id: str | None = None, name=None, kind="car",
                       display=None, center=None):
        if kind == "world":
            return self.create_world_project(name=name, display=display, center=center), None
        if kind not in ("car", "map"):
            raise ValueError("kind: car, map or world")
        if upload_id:
            src = self.upload_path(upload_id)
            p = self.projects.create(src, name=name, original=os.path.basename(src), move=True, kind=kind)
            shutil.rmtree(os.path.dirname(src), ignore_errors=True)
        elif path:
            p = self.projects.create(path, name=name, original=path, kind=kind)
        else:
            raise ValueError("path or upload_id")
        if kind == "map":
            return p, self.map_look(p.id)
        # look inside at once: models, car name, thumbnail
        job = self.jobs.submit("inspect", f"Inspect {p.data['name']}", lambda job: self._first_look(p, job), project=p.id)
        return p, job

    def _first_look(self, p, job):
        job.log("reading the FiveM resource...")
        s = self.analyzer.session(p)
        prof = p.profile()
        data = s.inspect(prof)
        job.log(f"{data['display_name']} ({data['model']}): {len(data['parts'])} parts, {data['textures']['used']} textures, "
                f"{len(data['warnings'])} warning(s)")
        job.log("rendering a thumbnail...")
        s.thumbnail(prof)
        return {"car": p.data.get("car"), "models": data.get("models")}

    def inspect(self, pid) -> dict:
        p = self.projects.get(pid).need("car")
        return self.analyzer.session(p).inspect(p.profile())

    def handling_preview(self, pid, overrides: dict) -> dict:
        p = self.projects.get(pid)
        prof = p.profile()
        prof = profile_mod.normalize(profile_mod.merge(prof, {"handling": overrides}), donors=DONORS,
                                     sounds=sound_mod.CATALOGUE)
        s = self.analyzer.session(p)
        src, v = s.vehicle(prof)
        from ..cp77.donor import choose_donor
        donor = choose_donor(v, None if prof["donor"] == "auto" else prof["donor"])
        hp = handling_profile(v, DONORS[donor], prof["handling"])
        return json.loads(json.dumps(hp, default=lambda o: list(o) if isinstance(o, tuple) else str(o)))

    def save_chime(self, pid, name, stream, length) -> dict:
        p = self.projects.get(pid)
        ext = os.path.splitext(name or "")[1].lower()
        if ext not in (".mp3", ".wav"):
            raise ValueError("the chime must be an .mp3 or .wav")
        if length > 8 * 1024 * 1024:
            raise ValueError("the chime is limited to 8 MiB")
        rel = f"assets/door_chime{ext}"
        dst = p.path(rel)
        with open(dst + ".tmp", "wb") as f:
            left = length
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        from ..cp77.package import audio_duration
        try:
            dur = audio_duration(dst + ".tmp")
        except Exception as e:  # noqa: BLE001
            os.remove(dst + ".tmp")
            raise ValueError(f"not a playable mp3/wav: {e}") from e
        for old in ("assets/door_chime.mp3", "assets/door_chime.wav"):
            if os.path.exists(p.path(old)):
                os.remove(p.path(old))
        os.replace(dst + ".tmp", dst)
        prof = p.patch_profile({"chime": {"enabled": True, "file": rel}})
        return {"file": rel, "seconds": round(dur, 3), "profile": prof}

    # ------------------------------------------------------------------ builds
    def build(self, pid, *, auto_qa=True):
        p = self.projects.get(pid)
        if p.kind == "map":
            return self.map_build(pid, auto_qa=auto_qa)
        if p.kind == "world":
            return self.world_build(pid)
        prof = p.profile()                      # validated now, so a bad profile fails before queuing

        def run(job):
            n = p.next_build()
            out = os.path.join(p.build_dir(n), "out")
            os.makedirs(out, exist_ok=True)
            job.meta["build"] = n
            tb = self.toolbox(refresh=True)
            missing = [k for k in ("wolvenkit",) if not getattr(tb, k)]
            if missing:
                raise RuntimeError("WolvenKit CLI not found: set it up in Settings > Tools")
            wk = tb.wolvenkit_obj(log=job.log)
            job.on_cancel.append(wk.kill)
            cache = DonorCache(wk, tb.game_dir, tb.cache_dir, log=job.log)
            steps = {"writing game files": 0.35, "done in": 1.0}

            def log(msg):
                job.log(msg)
                for k, v in steps.items():
                    if k in msg:
                        job.set_progress(v, k)

            job.set_progress(0.05, "reading the FiveM resource")
            conv = Converter(wk, cache, out, log=log, profile=prof, cancel=job.cancel_event,
                             keep_work=self.settings.get()["build"]["keep_work"], version=f"1.0.{n}")
            t0 = time.time()
            info = {"n": n, "time": time.time(), "ok": False, "version": f"1.0.{n}"}
            try:
                results = conv.convert(p.source)
                if not results:
                    raise RuntimeError("nothing was converted (check the model setting)")
                r = results[0]
                rep = r["report"]
                info.update({"ok": True, "name": r["name"], "record": r["record"], "resources": r["load"],
                             "packages": rep["packages"], "warnings": rep["warnings"], "sound": rep.get("sound"),
                             "seconds": round(time.time() - t0, 1), "display_name": rep.get("display_name")})
                if auto_qa:
                    job.set_progress(0.97, "checking the output")
                    q = qa_mod.check_build(out)
                    info["qa"] = {"ok": q["ok"], "failed": [c["message"] for c in q["checks"] if not c["ok"] and c["level"] == "error"]}
                    with open(os.path.join(p.build_dir(n), "qa.json"), "w", encoding="utf-8") as f:
                        json.dump(q, f, indent=1)
                    job.log("QA: " + ("all checks passed" if q["ok"] else "FAILED: " + "; ".join(info["qa"]["failed"])))
                return info
            except Exception as e:
                info["error"] = str(e)
                raise
            finally:
                info["seconds"] = round(time.time() - t0, 1)
                p.add_build(info)

        return self.jobs.submit("build", f"Build {p.data['name']}", run, project=p.id, heavy=True)

    def build_info(self, pid, n) -> dict:
        p = self.projects.get(pid)
        n = int(n)
        if p.kind == "map":
            return self.map_build_info(p, n)
        if p.kind == "world":
            return self.world_build_info(p, n)
        b = next((x for x in p.data.get("builds", []) if x.get("n") == n), None)
        if b is None:
            raise ProjectError("no such build")
        out = os.path.join(p.build_dir(n), "out")
        rep = None
        if b.get("name") and os.path.isfile(os.path.join(out, b["name"], "conversion-report.json")):
            with open(os.path.join(out, b["name"], "conversion-report.json"), "r", encoding="utf-8") as f:
                rep = json.load(f)
        qa = None
        qp = os.path.join(p.build_dir(n), "qa.json")
        if os.path.isfile(qp):
            with open(qp, "r", encoding="utf-8") as f:
                qa = json.load(f)
        return {"build": b, "report": rep, "qa": qa, "out": out}

    def run_qa(self, pid, n) -> dict:
        p = self.projects.get(pid)
        if p.kind == "map":
            return self.map_qa(p, n)
        if p.kind == "world":
            info = self.world_build_info(p, int(n))
            ok = bool(info["build"].get("ok"))
            return {"ok": ok, "checks": [{"id": "build", "ok": ok, "level": "error",
                                          "message": "the package was written" if ok else info["build"].get("error", "failed")}],
                    "summary": info["build"].get("summary")}
        out = os.path.join(p.build_dir(int(n)), "out")
        if not os.path.isdir(out):
            raise ProjectError("no such build")
        q = qa_mod.check_build(out)
        with open(os.path.join(p.build_dir(int(n)), "qa.json"), "w", encoding="utf-8") as f:
            json.dump(q, f, indent=1)
        return q

    def export_build(self, pid, n) -> str:
        """Zip of a build's resources (to copy to a server by hand)."""
        p = self.projects.get(pid)
        out = os.path.join(p.build_dir(int(n)), "out")
        if not os.path.isdir(out):
            raise ProjectError("no such build")
        stem = ((p.data.get("map") or {}).get("name", "map") if p.kind == "map" else
                (p.data.get("world") or {}).get("name", "world") if p.kind == "world" else
                (p.data.get("car") or {}).get("name", "car"))
        z = os.path.join(p.build_dir(int(n)), f"{stem}-build{int(n)}-resources")
        if not os.path.isfile(z + ".zip"):
            shutil.make_archive(z, "zip", out)
        return z + ".zip"

    # ------------------------------------------------------------------ servers / deploy
    def _server(self, sid) -> dict:
        s = self.settings.server(sid)
        if not s:
            raise ValueError(f"no server '{sid}'")
        return s

    def deploy(self, pid, sid, *, names=None, mode=None, build=None, include_hub=False, keep_placement=True,
               restart=True, baker=True):
        p = self.projects.get(pid)
        if p.kind == "map":
            return self.map_deploy(pid, sid, names=names, build=build, keep_placement=keep_placement, restart=restart,
                                   baker=baker)
        if p.kind == "world":
            return self.world_deploy(pid, sid, names=names, build=build, mode=mode)
        server = self._server(sid)
        mode = mode or self.settings.get()["deploy"]["mode"]
        if mode not in ("stage", "restart", "files"):
            raise ValueError("mode: stage, restart or files")
        b = (next((x for x in p.data.get("builds", []) if x.get("n") == int(build) and x.get("ok")), None)
             if build else p.latest_build())
        if not b:
            raise ProjectError("no successful build to deploy: build the car first")
        out = os.path.join(p.build_dir(b["n"]), "out")

        def run(job):
            tmp = tempfile.mkdtemp(prefix="f2o77-bundle-")
            try:
                bundle = deploy_mod.make_bundle(os.path.join(tmp, "bundle.tgz"), build_out=out, include_hub=include_hub)
                job.log(f"bundle: {', '.join(bundle['resources'])} ({bundle['bytes'] / 1048576:.1f} MiB)")
                res = deploy_mod.deploy(server, self.secrets, bundle["path"], mode=mode, names=names, log=job.log)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            entry = {"time": time.time(), "server": sid, "names": res["names"], "mode": mode, "build": b["n"],
                     "version": b.get("version"), "staged": len(res["staged"]), "live": len(res["live"])}
            with p.lock:
                p.data.setdefault("deployments", []).append(entry)
                p.data["deployments"] = p.data["deployments"][-50:]
                p.save()
            return res

        return self.jobs.submit("deploy", f"Deploy {p.data['name']} to {server['label']} ({mode})", run, project=p.id)

    def server_job(self, sid, action, **kw):
        server = self._server(sid)
        if action == "test":
            fn = lambda job: deploy_mod.test_connection(server, self.secrets)   # noqa: E731
            title = f"Test {server['label']}"
        elif action == "status":
            fn = lambda job: deploy_mod.status(server, self.secrets, kw.get("names"), log=job.log)   # noqa: E731
            title = f"Status of {server['label']}"
        elif action == "restart":
            def fn(job):
                code = deploy_mod.restart(server, self.secrets, kw.get("names"), log=job.log, force=kw.get("force", False))
                if code != 0:
                    raise RuntimeError("restart did not complete cleanly (see the log)")
                return {"code": code}
            title = f"Restart {server['label']}"
        elif action == "hub":
            mode = kw.get("mode", "stage")

            def fn(job):
                tmp = tempfile.mkdtemp(prefix="f2o77-hub-")
                try:
                    b = deploy_mod.make_bundle(os.path.join(tmp, "hub.tgz"), include_hub=True)
                    return deploy_mod.deploy(server, self.secrets, b["path"], mode=mode, names=kw.get("names"), log=job.log)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            title = f"Install the car hub on {server['label']}"
        elif action == "agent-api":
            return self.agent_api_job(sid, kw.get("names"), enable=kw.get("enable", True), restart=kw.get("restart", False))
        else:
            raise ValueError("unknown server action")
        return self.jobs.submit(f"server-{action}", title, fn, meta={"server": sid})

    def server_logs(self, sid, name, lines=200) -> str:
        return deploy_mod.logs(self._server(sid), self.secrets, name, lines)

    def close(self):
        self.analyzer.close()
        self.links.close()
