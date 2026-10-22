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
from . import fleet as fleet_mod
from . import qa as qa_mod
from .analysis import Analyzer
from .jobs import EventBus, JobCancelled, JobManager
from .mapops import MapOps
from .maplink import LinkPool
from .projects import ProjectError, ProjectStore
from . import sources as sources_mod
from .secrets import SecretStore
from .settings import Settings
from .tools import Toolbox
from .worldops import WorldOps

APP_VERSION = "1.7.5"


def _stamp(path) -> list:
    """What tells a file or folder apart from the same one changed: its size and time."""
    try:
        st = os.stat(path)
        return [st.st_size if os.path.isfile(path) else 0, int(st.st_mtime)]
    except OSError:
        return [0, 0]
SECRET_KEYS = re.compile(r"^(anthropic_api_key|server:[a-z0-9_-]{1,32}:(password|passphrase))$")


class Studio(MapOps, WorldOps):
    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self.settings = Settings(os.path.join(self.workspace, "settings.json"))
        self.secrets = SecretStore(os.path.join(self.workspace, "secrets.json"))
        self.bus = EventBus()
        self.jobs = JobManager(self.bus)
        self.library = sources_mod.Library(os.path.join(self.workspace, "library"))
        self.projects = ProjectStore(os.path.join(self.workspace, "projects"), library=self.library)
        self._recover_imports()
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
        from .terminal import Terminal
        self.terminal = Terminal(self)   # one command line for all of it (terminal.py)
        self.http_port = 0          # set by serve(): in-process API calls name it (server.call_route)
        from .qcsweep import QualityControl
        self.qc = QualityControl(self)   # quality checks around the clock (qcsweep.py); serve() starts its clock

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

    def get_linux_dotnet(self):
        """Downloads .NET 8 for Linux (the map baker's runtime, linuxdotnet.py) into the workspace, for
        an install that does not carry it."""
        from . import linuxdotnet
        from .tools import linux_dotnet_download_dir

        def run(job):
            def progress(done, total):
                job.set_progress(done / total if total else None, f"{done / 1048576:.0f} of {total / 1048576:.0f} MB")
            res = linuxdotnet.download(linux_dotnet_download_dir(self.workspace), os.path.join(self.workspace, "downloads"),
                                       log=job.log, progress=progress, check=job.check)
            tb = self.toolbox(refresh=True)
            if not tb.linux_dotnet:
                raise RuntimeError("the runtime was unpacked but the Studio does not find it")
            job.log(f"Settings > Tools: .NET 8 for Linux ok ({tb.linux_dotnet})")
            return dict(tb.status()["linux_dotnet"], runtime=res["runtime"])
        return self.jobs.submit("tools", "Download .NET 8 for Linux (map baker)", run)

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
        qc = self.qc.report or {}
        return {"version": APP_VERSION, "converter": VERSION, "workspace": self.workspace,
                "settings": self.redacted_settings(), "tools": self.tools_status(),
                "projects": self.projects.list(), "jobs": self.jobs.list()[:50],
                "qc": {"time": qc.get("time"), "ok": qc.get("ok"), "counts": qc.get("counts") or {}}}

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

    def _source_of(self, path, upload_id) -> tuple:
        """(source path, uploaded, shown name) of an import, checked."""
        if upload_id:
            src = self.upload_path(upload_id)
            return src, True, os.path.basename(src)
        if path:
            src = sources_mod.check_source(path)
            return src, False, src
        raise ValueError("path or upload_id")

    def _drop_upload(self, src, uploaded):
        if uploaded:
            shutil.rmtree(os.path.dirname(src), ignore_errors=True)

    def create_project(self, *, path: str | None = None, upload_id: str | None = None, name=None, kind="car",
                       display=None, center=None, city=None):
        """A car or map project from a FiveM resource.  The project is there at once; its files come
        in a job (copied, a big archive extracted once, a big folder read where it is), which then
        starts the first look (map) or inspection (car): the job's result names that next job."""
        if kind == "world":
            return self.create_world_project(name=name, display=display, center=center, city=city), None
        if kind not in ("car", "map"):
            raise ValueError("kind: car, map or world")
        src, uploaded, original = self._source_of(path, upload_id)
        p = self.projects.create_pending(kind, original, name=name)

        def run(job):
            try:
                how, size, files = sources_mod.plan(src, uploaded)
                job.log(f"{original}: {sources_mod.fmt_bytes(size)} in {files:,} file(s)")
                tb = self.toolbox()
                if how in ("link", "extract"):
                    # big: held once in the library (a city's districts are projects that share it)
                    e = self.library.create(kind, p.data["name"], original)      # (a pack is kind "cars")
                    job.meta["library"] = e["id"]
                    job.log("a big folder: the project reads it where it is, nothing is copied (keep it there)"
                            if how == "link" else "a big archive: extracted once into the Studio's library")
                    job.set_progress(0.0, "linking" if how == "link" else "extracting")
                    try:
                        self.library.fill(e, src, how, log=job.log, progress=job.set_progress, check=job.check,
                                          sevenzip=getattr(tb, "sevenzip", None))
                    except BaseException:
                        self.library.delete(e["id"])
                        raise
                    e["state"] = "ready"
                    self.library.save(e)
                    self.projects.finish_import(p, lib=e["id"])
                else:
                    job.set_progress(0.0, "copying")
                    got = sources_mod.take_in(src, p.path("input"), how, log=job.log, progress=job.set_progress,
                                              check=job.check, sevenzip=getattr(tb, "sevenzip", None))
                    self.projects.finish_import(p, file=os.path.relpath(got, p.path("input")).replace(os.sep, "/"))
            except BaseException as e:
                self.projects.fail_import(p, "cancelled" if isinstance(e, JobCancelled) else (str(e) or e.__class__.__name__))
                raise
            finally:
                self._drop_upload(src, uploaded)
            job.set_progress(1.0, "imported")
            job.log(f"imported ({how})")
            nxt = self.map_look(p.id) if kind == "map" else self._inspect_job(p)
            job.meta["next"] = nxt.id
            return {"project": p.id, "how": how, "bytes": size, "files": files, "next_job": nxt.id}

        return p, self.jobs.submit("import", f"Import {os.path.basename(original)}", run, project=p.id)

    def _inspect_job(self, p):
        # look inside at once: models, car name, thumbnail
        return self.jobs.submit("inspect", f"Inspect {p.data['name']}", lambda job: self._first_look(p, job), project=p.id)

    def _recover_imports(self):
        """Imports the Studio was closed in the middle of, and library entries nobody reads."""
        in_use = set()
        for name in os.listdir(self.projects.root):
            try:
                p = self.projects.get(name)
            except (OSError, ValueError, ProjectError):
                continue
            src = p.data.get("source") or {}
            if src.get("lib"):
                in_use.add(src["lib"])
            if src.get("state") == "importing":
                self.projects.fail_import(p, "the Studio was closed during the import")
        for e in self.library.list():
            # a pack whose cars were never picked stays a week; a map nobody reads, a day
            fresh = time.time() - float(e.get("created") or 0) < 7 * 86400
            if e.get("kind") == "cars" and e.get("state") == "ready" and fresh:
                in_use.add(e["id"])
        try:
            self.library.prune(in_use)
        except OSError:
            pass

    def delete_project(self, pid):
        self.analyzer.forget(pid)
        lib = self.projects.delete(pid)
        if lib and not self.projects.using(lib):
            try:
                e = self.library.get(lib)
            except sources_mod.SourceError:
                return
            # a pack stays for its other cars (deleted with the pack); one car's is gone with it
            if e.get("kind") != "cars" or len(e.get("cars") or []) <= 1:
                self.library.delete(lib)

    # ------------------------------------------------------------------ car packs
    def _pack_view(self, e: dict) -> dict:
        projects = self.projects.using(e["id"])
        by_model = {}
        for q in projects:
            m = (q.raw_profile().get("model") or "").lower()
            by_model.setdefault(m, []).append(q.id)
        cars = []
        for c in e.get("cars") or []:
            cars.append(dict(c, projects=by_model.get(c["model"].lower(), [])))
        return {k: e.get(k) for k in ("id", "name", "original", "created", "state", "how", "linked", "error")} | {
            "cars": cars, "projects": [q.id for q in projects]}

    def packs(self) -> list:
        return [self._pack_view(e) for e in self.library.list("cars")]

    def pack(self, lid) -> dict:
        e = self.library.get(lid)
        if e.get("kind") != "cars":
            raise sources_mod.SourceError("no such pack")
        return self._pack_view(e)

    def create_pack(self, *, path=None, upload_id=None, name=None):
        """A car pack (or one car) into the library, then the list of its cars: the job's result.
        The cars become projects with import_pack_cars (the ones the user picks)."""
        src, uploaded, original = self._source_of(path, upload_id)
        stem = os.path.splitext(os.path.basename(os.path.normpath(original)))[0]
        stamp = _stamp(src)
        if not uploaded:
            # the same pack again (same file, same size and time): its cars are already here
            for old in self.library.list("cars"):
                if old.get("state") == "ready" and old.get("original") == original and old.get("stamp") == stamp:
                    def again(job, old=old):
                        job.log(f"{os.path.basename(original)} is imported already ({len(old.get('cars') or [])} cars): "
                                "add the cars that have no project yet")
                        return self._pack_view(old)
                    return old, self.jobs.submit("pack", f"Import {os.path.basename(original)}", again, meta={"pack": old["id"]})
        e = self.library.create("cars", name or re.sub(r"\s*\(\d+\)$", "", stem), original)
        e["stamp"] = stamp
        self.library.save(e)

        def run(job):
            try:
                how, size, files = sources_mod.plan(src, uploaded)
                job.log(f"{original}: {sources_mod.fmt_bytes(size)} in {files:,} file(s)")
                if how == "link":
                    job.log("a big folder: the cars are read where it is, nothing is copied (keep it there)")
                job.set_progress(0.0, "linking" if how == "link" else ("copying" if os.path.isdir(src) else "extracting"))
                tb = self.toolbox()
                self.library.fill(e, src, how, log=job.log, progress=job.set_progress, check=job.check,
                                  sevenzip=getattr(tb, "sevenzip", None))
                job.set_progress(0.9, "finding the cars")
                folder = self.library.folder(e["id"], e)
                cars = sources_mod.scan_cars(folder, log=job.log)
                if not cars:
                    raise sources_mod.SourceError("no cars found in it (a car is a vehicles.meta entry with its .yft)")
                e.update(cars=cars, state="ready")
                self.library.save(e)
                job.log(f"{len(cars)} car(s): " + ", ".join(c["display"] for c in cars[:12]) + (" ..." if len(cars) > 12 else ""))
                return self._pack_view(e)
            except BaseException as ex:
                e.update(state="failed", error="cancelled" if isinstance(ex, JobCancelled) else (str(ex) or ex.__class__.__name__))
                try:
                    self.library.save(e)
                except OSError:
                    pass
                raise
            finally:
                self._drop_upload(src, uploaded)

        job = self.jobs.submit("pack", f"Import {os.path.basename(original)}", run, meta={"pack": e["id"]})
        return e, job

    def import_pack_cars(self, lid, models=None):
        """A project per car of the pack (all of them, or `models`); one job reads them one after
        another (name, parts, thumbnail).  Cars that already have a project are skipped."""
        e = self.library.get(lid)
        if e.get("kind") != "cars" or e.get("state") != "ready":
            raise sources_mod.SourceError("this pack is not ready (still importing, or its import failed)")
        view = self._pack_view(e)
        want = None if models in (None, "all") else {str(m).lower() for m in models}
        chosen = [c for c in view["cars"] if (want is None or c["model"].lower() in want) and not c["projects"]]
        if want is not None:
            unknown = want - {c["model"].lower() for c in view["cars"]}
            if unknown:
                raise ValueError(f"not in this pack: {', '.join(sorted(unknown))}")
        made = []
        for c in chosen:
            q = self.projects.create_shared("car", lid, c.get("sub") or "", e.get("original") or "", profile={"model": c["model"]},
                                            file=(c.get("sub") or "").rsplit("/", 1)[-1] or c["model"])
            with q.lock:
                q.data["name"] = c["display"] or c["model"]
                q.data["models"] = [c["model"]]
                q.data["pack"] = {"id": lid, "name": e.get("name")}
                q.save()
            made.append(q)
        if not made:
            return [], None

        def run(job):
            ok = failed = 0
            for i, q in enumerate(made):
                job.check()
                job.set_progress(i / len(made), f"{q.data['name']} ({i + 1} of {len(made)})")
                try:
                    self._first_look(q, job)
                    ok += 1
                except JobCancelled:
                    raise
                except Exception as ex:  # noqa: BLE001 - one car does not stop the pack
                    failed += 1
                    job.log(f"{q.data['name']}: could not be read: {ex}")
                finally:
                    self.analyzer.forget(q.id)       # 60 cars do not stay in memory
            job.log(f"{ok} car(s) read" + (f", {failed} could not be read (open them to see why)" if failed else ""))
            return {"read": ok, "failed": failed, "projects": [q.id for q in made]}

        job = self.jobs.submit("pack-inspect", f"Read {len(made)} car(s) of {e.get('name')}", run, meta={"pack": lid})
        return made, job

    def delete_pack(self, lid, with_projects=False):
        e = self.library.get(lid)
        users = self.projects.using(lid)
        if users and not with_projects:
            raise ValueError(f"{len(users)} project(s) read this pack: delete them with it, or delete them first")
        for q in users:
            self.analyzer.forget(q.id)
            self.projects.delete(q.id)
        self.library.delete(e["id"])

    def pack_build(self, lid, only_unbuilt=False):
        """Build the pack's cars one after another (each is a build job of its own)."""
        users = self.projects.using(lid)
        if only_unbuilt:
            users = [q for q in users if not q.latest_build()]
        if not users:
            raise ValueError("no cars to build" + (" (every car has a build)" if only_unbuilt else ""))
        name = self.library.get(lid).get("name")

        def run(job):
            done, failed = [], []
            for i, q in enumerate(users):
                job.check()
                job.set_progress(i / len(users), f"{q.data['name']} ({i + 1} of {len(users)})")
                try:
                    bj = self.build(q.id)
                except Exception as ex:  # noqa: BLE001 - a bad profile: the next car
                    failed.append(q.data["name"])
                    job.log(f"{q.data['name']}: {ex}")
                    continue
                job.on_cancel.append(bj.cancel)
                self.jobs.wait(bj)
                if bj.state == "done":
                    done.append(q.data["name"])
                    job.log(f"{q.data['name']}: build #{(bj.result or {}).get('n')} done")
                elif bj.state == "cancelled":
                    job.check()
                    failed.append(q.data["name"])
                else:
                    failed.append(q.data["name"])
                    job.log(f"{q.data['name']}: failed: {bj.error}")
            job.log(f"{len(done)} built" + (f", {len(failed)} failed: {', '.join(failed[:10])}" if failed else ""))
            if not done:
                raise RuntimeError("no car of the pack was built (see the log)")
            return {"built": done, "failed": failed}

        return self.jobs.submit("pack-build", f"Build {len(users)} car(s) of {name}", run, meta={"pack": lid})

    def pack_deploy(self, lid, sid, *, names=None, mode=None, include_hub=False):
        """Every built car of the pack to a server in one bundle (one restart)."""
        server = self._server(sid)
        mode = mode or self.settings.get()["deploy"]["mode"]
        if mode not in ("stage", "restart", "files"):
            raise ValueError("mode: stage, restart or files")
        built = [(q, q.latest_build()) for q in self.projects.using(lid)]
        built = [(q, b) for q, b in built if b]
        if not built:
            raise ProjectError("no car of this pack has a successful build: build them first")
        name = self.library.get(lid).get("name")

        def run(job):
            tmp = tempfile.mkdtemp(prefix="f2o77-pack-")
            try:
                outs = [os.path.join(q.build_dir(b["n"]), "out") for q, b in built]
                bundle = deploy_mod.make_bundle(os.path.join(tmp, "bundle.tgz"), build_outs=outs, include_hub=include_hub,
                                                cancel=job.cancel_event)
                job.log(f"bundle: {len(bundle['resources'])} resources ({bundle['bytes'] / 1048576:.1f} MiB)")
                res = deploy_mod.deploy(server, self.secrets, bundle["path"], mode=mode, names=names, log=job.log, job=job)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            for q, b in built:
                entry = {"time": time.time(), "server": sid, "names": res["names"], "mode": mode, "build": b["n"],
                         "version": b.get("version"), "staged": len(res["staged"]), "live": len(res["live"]), "pack": lid}
                with q.lock:
                    q.data.setdefault("deployments", []).append(entry)
                    q.data["deployments"] = q.data["deployments"][-50:]
                    q.save()
            return dict(res, cars=len(built))

        return self.jobs.submit("deploy", f"Deploy {len(built)} car(s) of {name} to {server['label']} ({mode})", run,
                                meta={"pack": lid, "server": sid})

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
                info["converter"] = VERSION
                info["studio"] = APP_VERSION
                p.add_build(info)
                self.qc.after_build(p.id)

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
            # a car the server carries in a car bundle goes out in its bundle (installing it on its own
            # as well would define its record twice)
            from . import carbundle
            records = {c["record"] for c in carbundle.car_resources(out)}
            try:
                inv = deploy_mod.car_inventory(server, self.secrets, names, log=job.log)
            except deploy_mod.DeployError as e:
                # never installed on its own on a guess: a car the server carries in a bundle would then
                # define its record twice (and a server that cannot be read now cannot be written either)
                raise ProjectError(f"could not read the server's car bundles ({e}): nothing was installed; "
                                   "deploy again when the server answers") from e
            inbundle = {f: sorted(b for b, i in (d.get("bundles") or {}).items() if records & set(i.get("records") or []))
                        for f, d in inv.items()}
            if any(inbundle.values()):
                best, _ = self.deployable_cars()
                for r in records:
                    c = best.get(r)
                    if c is None or c["project"].id != p.id or c["build"]["n"] != b["n"]:
                        raise ProjectError(f"{r} is in a car bundle on {server['label']}: deploy it with its bundle "
                                           "(Deploy every car), from its latest good build")
                results = [self._deploy_car_bundles(job, server, f, inv[f], best, mode, only_bundles=inbundle[f])
                           for f in deploy_mod._names(server, names) if inbundle.get(f)]
                return {"bundled": True, "folders": [{k: v for k, v in r.items() if k != "result"} for r in results],
                        "staged": [l for r in results for l in (r["result"] or {}).get("staged", [])],
                        "live": [l for r in results for l in (r["result"] or {}).get("live", [])],
                        "names": [r["folder"] for r in results]}
            tmp = tempfile.mkdtemp(prefix="f2o77-bundle-")
            try:
                bundle = deploy_mod.make_bundle(os.path.join(tmp, "bundle.tgz"), build_out=out, include_hub=include_hub,
                                                cancel=job.cancel_event)
                job.log(f"bundle: {', '.join(bundle['resources'])} ({bundle['bytes'] / 1048576:.1f} MiB)")
                res = deploy_mod.deploy(server, self.secrets, bundle["path"], mode=mode, names=names, log=job.log, job=job)
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

    # ------------------------------------------------------------------ car bundles: every car on a server
    def deployable_cars(self) -> tuple[dict, list]:
        """The latest build of every car record, when it built and passed its checks:
        {record: {project, name, build, time, out, car (carbundle entry), addons}}, and what was left
        out [(project name, why)]."""
        from . import carbundle
        best, skipped = {}, []
        for summary in self.projects.list():
            try:
                q = self.projects.get(summary["id"])
            except Exception:  # noqa: BLE001 - deleted meanwhile
                continue
            if q.kind != "car":
                continue
            name = q.data.get("name") or q.id
            builds = q.data.get("builds") or []
            last = builds[-1] if builds else None
            if not last:
                skipped.append((name, "never built"))
                continue
            if not last.get("ok"):
                skipped.append((name, "its last build failed"))
                continue
            if not (last.get("qa") or {"ok": True}).get("ok", True):
                skipped.append((name, "its last build failed the checks"))
                continue
            out = os.path.join(q.build_dir(last["n"]), "out")
            try:
                cars = carbundle.car_resources(out)
                addons = carbundle.addons_of(out)
            except (OSError, ValueError) as e:
                skipped.append((name, f"its build cannot be read: {e}"))
                continue
            for c in cars:
                rec = c["record"]
                prev = best.get(rec)
                entry = {"project": q, "name": name, "build": last, "time": last.get("time") or 0, "out": out, "car": c,
                         "addons": [a for a in addons if a.startswith(c["resource"] + "_")]}
                if prev is None or entry["time"] > prev["time"]:
                    if prev is not None:
                        skipped.append((prev["name"], f"{rec} is built by {name} too (newer)"))
                    best[rec] = entry
                else:
                    skipped.append((name, f"{rec} is built by {prev['name']} too (newer)"))
        return best, skipped

    def _deploy_car_bundles(self, job, server, folder, inv, best, mode, only_bundles=None) -> dict:
        """One server folder: re-pack the bundles whose cars changed, update cars installed on their
        own in place, one deploy (one restart).  inv: carbundle inventory of the folder."""
        from . import carbundle
        standalone = {rec: res for res, rec in (inv.get("standalone") or {}).items()}
        existing = {b: i.get("records") or [] for b, i in (inv.get("bundles") or {}).items()}
        bundled = sorted(r for r in best if r not in standalone)
        assignment = carbundle.plan(bundled, existing, carbundle.BUNDLE_SIZE)
        if only_bundles:
            assignment = {k: v for k, v in assignment.items() if k in only_bundles}
        tmp = tempfile.mkdtemp(prefix="f2o77-cars-")
        try:
            dirs, changed, deployed = {}, [], []
            for bname, recs in sorted(assignment.items(), key=lambda kv: carbundle._bundle_no(kv[0])):
                job.check()
                old = (inv.get("bundles") or {}).get(bname) or {}
                have = old.get("cars") or {}
                if have and set(have) == set(recs) and all(have.get(r) == best[r]["car"]["info"].get("version") for r in recs):
                    job.log(f"{folder}: {bname} unchanged ({len(recs)} cars)")
                    continue
                ver = carbundle.next_version((inv.get("versions") or {}).get(bname))
                info = carbundle.write_bundle(os.path.join(tmp, bname), bname, ver, [best[r]["car"] for r in recs])
                dirs[bname] = os.path.join(tmp, bname)
                changed.append(bname)
                deployed += recs
                job.log(f"{folder}: {bname} {ver}: {len(recs)} cars ({info['bytes'] / 1048576:.0f} MiB): "
                         + ", ".join(r.split(".", 1)[-1] for r in recs))
            for rec, res in sorted(standalone.items()):
                c = best.get(rec)
                if c is None or only_bundles:          # one bundle re-packed: the rest stays as it is
                    continue
                if c["car"]["resource"] != res:
                    job.log(f"{folder}: {rec} is installed as {res}, the Studio builds it as {c['car']['resource']}: "
                            "left as it is")
                    continue
                dirs[res] = c["car"]["dir"]
                deployed.append(rec)
                job.log(f"{folder}: {res} (installed on its own) updated in place")
            for rec in set(deployed):
                for a in best[rec]["addons"]:
                    dirs[a] = os.path.join(best[rec]["out"], a)
            orphans = sorted(b for b in existing if b not in assignment and not only_bundles)
            if orphans:
                job.log(f"{folder}: {', '.join(orphans)} carry no car the Studio has now: left installed")
            if not dirs:
                job.log(f"{folder}: every car is up to date")
                return {"folder": folder, "bundles": [], "cars": 0, "result": None}
            bundle = deploy_mod.make_bundle(os.path.join(tmp, "bundle.tgz"), resource_dirs=dirs, cancel=job.cancel_event)
            job.log(f"{folder}: uploading {len(dirs)} resources ({bundle['bytes'] / 1048576:.0f} MiB)")
            res = deploy_mod.deploy(server, self.secrets, bundle["path"], mode=mode, names=[folder], log=job.log, job=job)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for rec in set(deployed):
            c = best[rec]
            entry = {"time": time.time(), "server": server["id"], "names": [folder], "mode": mode,
                     "build": c["build"]["n"], "version": c["build"].get("version"),
                     "bundle": next((b for b, rs in assignment.items() if rec in rs), None),
                     "staged": len(res["staged"]), "live": len(res["live"])}
            q = c["project"]
            with q.lock:
                q.data.setdefault("deployments", []).append(entry)
                q.data["deployments"] = q.data["deployments"][-50:]
                q.save()
        return {"folder": folder, "bundles": changed, "cars": len(set(deployed)), "result": res}

    def car_bundles_deploy(self, sid, *, names=None, mode=None):
        """Every car the Studio built well onto a server, carried in car bundles (studio/carbundle.py)."""
        server = self._server(sid)
        mode = mode or self.settings.get()["deploy"]["mode"]
        if mode not in ("stage", "restart", "files"):
            raise ValueError("mode: stage, restart or files")

        def run(job):
            best, skipped = self.deployable_cars()
            for n, why in skipped:
                job.log(f"not deployed: {n}: {why}")
            if not best:
                raise ProjectError("no car has a good build to deploy")
            job.log(f"{len(best)} cars with a good build")
            inv = deploy_mod.car_inventory(server, self.secrets, names, log=job.log)
            results = []
            for folder in deploy_mod._names(server, names):
                job.check()
                results.append(self._deploy_car_bundles(job, server, folder, inv[folder], best, mode))
            return {"server": sid, "mode": mode, "cars": len(best), "skipped": [f"{n}: {w}" for n, w in skipped],
                    "folders": [{k: v for k, v in r.items() if k != "result"} for r in results],
                    "staged": sum(len((r["result"] or {}).get("staged", [])) for r in results),
                    "pending": [l for r in results for l in (r["result"] or {}).get("pending", [])]}

        return self.jobs.submit("deploy", f"Deploy every car to {server['label']} ({mode})", run,
                                meta={"server": sid, "cars": "all"})

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
                    b = deploy_mod.make_bundle(os.path.join(tmp, "hub.tgz"), include_hub=True, cancel=job.cancel_event)
                    return deploy_mod.deploy(server, self.secrets, b["path"], mode=mode, names=kw.get("names"), log=job.log,
                                             job=job)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
            title = f"Install the car hub on {server['label']}"
        elif action == "agent-api":
            return self.agent_api_job(sid, kw.get("names"), enable=kw.get("enable", True), restart=kw.get("restart", False))
        else:
            raise ValueError("unknown server action")
        return self.jobs.submit(f"server-{action}", title, fn, meta={"server": sid})

    def fleet_job(self, action, confirm=False):
        """Open77 versions across every server (status), or the upgrade of the folders that are behind."""
        servers = self.settings.get()["servers"]
        if not servers:
            raise ValueError("no servers yet")
        if action == "status":
            fn = lambda job: self._fleet_status(servers, job.log)   # noqa: E731
            title = "Open77 versions across the servers"
        elif action == "upgrade":
            if not confirm:
                raise ValueError("the fleet upgrade needs confirm")
            fn = lambda job: self._fleet_upgrade(servers, job.log)   # noqa: E731
            title = "Upgrade the servers to the newest staged Open77"
        else:
            raise ValueError("unknown fleet action")
        return self.jobs.submit(f"fleet-{action}", title, fn, meta={"fleet": action})

    def _fleet_status(self, servers, log) -> list:
        out = []
        for s in servers:
            try:
                rel = deploy_mod.releases(s, self.secrets)
                target = fleet_mod.newest(rel)
                rows = deploy_mod.status(s, self.secrets, log=log)
                out.append({"id": s["id"], "label": s["label"], "host": s["host"], "staged": rel,
                            **fleet_mod.describe(s, rows, target)})
                log(f"{s['label']}: target {target or 'none staged'}")
            except deploy_mod.DeployError as e:
                out.append({"id": s["id"], "label": s["label"], "host": s["host"], "error": str(e)})
                log(f"{s['label']}: {e}")
        return out

    def _fleet_upgrade(self, servers, log) -> dict:
        done, skipped, errors = [], [], []
        for s in servers:
            try:
                target = fleet_mod.newest(deploy_mod.releases(s, self.secrets))
                if not target:
                    skipped.append({"server": s["id"], "reason": "no release staged on the host"})
                    log(f"{s['label']}: no release staged on the host; nothing to do")
                    continue
                rows = deploy_mod.status(s, self.secrets, log=log)
                names, why = fleet_mod.plan(s, rows, target)
                for x in why:
                    skipped.append({"server": s["id"], "name": x["name"], "reason": x["reason"]})
                    log(f"{s['label']} {x['name']}: left alone ({x['reason']})")
                if not names:
                    continue
                log(f"{s['label']}: upgrading {', '.join(names)} to {target}")
                code = deploy_mod.upgrade(s, self.secrets, target, names, log=log)
                if code != 0:
                    errors.append(f"{s['label']}: vps_upgrade.sh exited {code}")
                else:
                    done.append({"server": s["id"], "target": target, "names": names})
            except deploy_mod.DeployError as e:
                errors.append(f"{s['label']}: {e}")
                log(f"{s['label']}: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return {"upgraded": done, "skipped": skipped}

    def server_logs(self, sid, name, lines=200) -> str:
        return deploy_mod.logs(self._server(sid), self.secrets, name, lines)

    def close(self):
        self.qc.close()
        self.analyzer.close()
        self.links.close()
