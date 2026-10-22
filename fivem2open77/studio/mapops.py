"""The Studio's map operations (what the API, the pages and the AI agent call for maps).

Mixed into studio.app.Studio: uses self.projects, self.jobs, self.settings, self.secrets,
self.toolbox() and self.links (the live map manager links).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from . import deploy as deploy_mod
from . import maps as maps_mod
from . import qa as qa_mod
from .maplink import LinkError, Unreachable
from .projects import ProjectError

MAP_NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
SERVER_NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MapOps:
    # ------------------------------------------------------------------ look
    def _map(self, pid):
        return self.projects.get(pid).need("map")

    def map_look(self, pid, force=False):
        """A job that reads the map and draws its previews (cached per options)."""
        p = self._map(pid)
        for j in self.jobs.list(project=p.id):
            if j["kind"] == "look" and j["state"] in ("queued", "running"):
                return self.jobs.get(j["id"])
        look = maps_mod.MapLook(p)
        if force:
            for f in os.listdir(p.path("cache")):
                if f.startswith(("look-", look.key())):
                    try:
                        os.remove(p.path("cache", f))
                    except OSError:
                        pass

        def run(job):
            job.log(f"reading {p.data['source'].get('original') or p.data['source']['file']} ...")
            data = look.ensure(log=job.log, check=job.check, progress=job.set_progress)
            self._after_look(p, data)
            if data.get("city"):
                ov = data["overview"]
                return {"city": True, "map": data["map"], "display_name": data["display_name"],
                        "objects": ov["objects"], "ymaps": ov["ymaps"], "districts": len(data["districts"]),
                        "next": "pick a district (an area) to convert"}
            return {"map": data["map"], "display_name": data["display_name"], "size": data["size"],
                    "placements": data["placements"], "parts": len(data["parts"]), "warnings": data["warnings"][:20]}

        return self.jobs.submit("look", f"Look at {p.data['name']}", run, project=p.id, heavy=True)

    def _after_look(self, p, data):
        """Pin the map's resource id, remember what it is, name the project after it."""
        raw = p.raw_map_options()
        if data.get("city"):
            # a whole city: no resource id yet (each district gets its own: <city>_<district>)
            ov = data["overview"]
            info = {"name": raw.get("name") or data["map"], "display_name": raw.get("display_name") or data["display_name"],
                    "city": True, "objects": ov["objects"], "districts": len(data["districts"]),
                    "resource": data["resource"], "size": _extent_size(ov.get("extent"))}
            with p.lock:
                changed = p.data.get("map") != info
                p.data["map"] = info
                if not p.data.get("custom_name") and p.data.get("name") != data["display_name"]:
                    p.data["name"] = data["display_name"]
                    changed = True
                if changed:
                    p.data["updated"] = time.time()
                    p.save()
            return
        if not raw.get("name"):
            raw["name"] = data["map"]
            p.set_map_options(raw)
        opts = p.map_options()
        info = {"name": opts.get("name") or data["map"], "display_name": opts.get("display_name") or data["display_name"],
                "size": data["size"], "placements": data["placements"], "parts": len(data["parts"]), "resource": data["resource"]}
        with p.lock:
            changed = p.data.get("map") != info
            p.data["map"] = info
            if not p.data.get("custom_name") and p.data.get("name") != info["display_name"]:
                p.data["name"] = info["display_name"]
                changed = True
            if changed:
                p.data["updated"] = time.time()
                p.save()

    def map_state(self, pid) -> dict:
        p = self._map(pid)
        look = maps_mod.MapLook(p)
        raw = p.raw_map_options()
        try:
            norm, errors = p.map_options(), []
        except maps_mod.MapOptionsError as e:
            norm, errors = None, e.errors
        running = [j for j in self.jobs.list(project=p.id) if j["state"] in ("queued", "running")]
        return {"project": p.summary(), "options": raw, "normalized": norm, "option_errors": errors,
                "defaults": maps_mod.DEFAULTS, "look": look.cached(norm) if norm else None,
                "look_key": look.key(norm) if norm else None, "jobs": running,
                "builds": list(reversed(p.data.get("builds", []))), "deployments": p.data.get("deployments", [])[-20:],
                "build_stale": self.map_stale(p),
                "game": self.map_game_status(pid)}

    def map_set_options(self, pid, options: dict, patch=True) -> dict:
        """Save options (a merge patch by default); a change to what is built starts a new look."""
        p = self._map(pid)
        before = None
        try:
            before = p.map_options()
        except maps_mod.MapOptionsError:
            pass
        norm = p.patch_map_options(options) if patch else p.set_map_options(options)
        with p.lock:                     # what the project shows follows the names in the options
            info = dict(p.data.get("map") or {})
            if info:
                if norm.get("name"):
                    info["name"] = norm["name"]
                if norm.get("display_name"):
                    info["display_name"] = norm["display_name"]
                if info != p.data.get("map"):
                    p.data["map"] = info
                    if not p.data.get("custom_name") and norm.get("display_name"):
                        p.data["name"] = norm["display_name"]
                    p.save()
        look_job = None
        if before is None or any(before[k] != norm[k] for k in maps_mod.LOOK_KEYS):
            if maps_mod.MapLook(p).cached(norm) is None:
                look_job = self.map_look(pid)
        return {"options": p.raw_map_options(), "normalized": norm,
                "look_job": look_job.summary() if look_job else None}

    def _city_look_file(self, p):
        """The newest city overview in a project's cache (the look of the whole city), or None."""
        best = None
        for f in os.listdir(p.path("cache")):
            if not (f.startswith("look-") and f.endswith(".json")):
                continue
            fp = p.path("cache", f)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    head = fh.read(64)
            except OSError:
                continue
            if '"city": true' in head and (best is None or os.path.getmtime(fp) > os.path.getmtime(best)):
                best = fp
        return best

    def map_city(self, pid) -> dict:
        """The city overview a district project was picked from (its districts and where the city's
        objects stand)."""
        p = self._map(pid)
        fp = self._city_look_file(p)
        if not fp:
            raise maps_mod.LookMissing("no city overview for this map (it is not a whole city, or it was never looked "
                                       "at without an area)")
        with open(fp, "r", encoding="utf-8") as f:
            city = json.load(f)
        city["taken"] = self._districts_of(p)
        return city

    def _districts_of(self, p) -> list:
        """The districts of a city converted so far: the map projects reading the same files with an area."""
        lib = (p.data.get("source") or {}).get("lib")
        users = self.projects.using(lib) if lib else [p]
        out = []
        for q in users:
            if q.kind != "map":
                continue
            try:
                o = q.map_options()
            except maps_mod.MapOptionsError:
                continue
            if o.get("area"):
                out.append({"project": q.id, "name": o.get("name") or (q.data.get("map") or {}).get("name"),
                            "display_name": q.data.get("name"), "area": o["area"]})
        return out

    def _name_taken(self, name, but=None):
        """The map project (not `but`) whose resource id is `name`, or None."""
        for s in self.projects.list():
            if s.get("kind") != "map" or s["id"] == but:
                continue
            try:
                q = self.projects.get(s["id"])
                own = q.raw_map_options().get("name")
                info = q.data.get("map") or {}
            except (OSError, ValueError, ProjectError):
                continue
            # a city's overview project has the city's name until a district is picked: not a map yet
            if name == own or (not own and not info.get("city") and info.get("name") == name):
                return q
        return None

    def map_district(self, pid, area, *, name=None, display_name=None, district=None, new_project=False):
        """Convert one district of a whole city: in this project (its area and names change), or as
        a new project reading the same files (another district next to the ones already built)."""
        p = self._map(pid)
        cfile = self._city_look_file(p)
        city = None
        if cfile:
            with open(cfile, "r", encoding="utf-8") as f:
                city = json.load(f)
        base = (city or {}).get("map") or (p.data.get("map") or {}).get("name") or "city"
        base_disp = (city or {}).get("display_name") or p.data.get("name") or base
        if not name:
            name = maps_mod.district_name(base, district or f"x{int(float(area[0]))}_y{int(float(area[1]))}")
        if not display_name:
            display_name = maps_mod.district_display(base_disp, district or "district")
        patch = {"area": area, "name": name, "display_name": display_name}
        maps_mod.normalize_options(maps_mod.merge(p.raw_map_options(), patch))      # raises MapOptionsError
        other = self._name_taken(name, but=None if new_project else p.id)
        if other is not None:
            raise maps_mod.MapOptionsError([f"name: {name} is already the resource id of {other.data.get('name')}: "
                                            "each district needs its own (pick another district, or change the id)"])
        if not new_project:
            r = self.map_set_options(pid, patch)
            job = self.jobs.get(r["look_job"]["id"]) if r.get("look_job") else self.map_look(pid)
            return p, job
        if p.importing:
            raise ProjectError("the city is still being imported")
        lib = self.projects.share(p)
        s = p.data["source"]
        keep = {k: v for k, v in p.raw_map_options().items() if k not in ("area", "name", "display_name", "place")}
        q = self.projects.create_shared("map", lib, s.get("sub") or "", s.get("original") or "", name=display_name,
                                        options=dict(keep, **patch), file=s.get("file"))
        if s.get("hash"):                      # the same files: the same source key as the city's project
            with q.lock:
                q.data["source"]["hash"] = s["hash"]
                q.save()
        if cfile:                              # the city overview, to pick the next district from there too
            shutil.copyfile(cfile, q.path("cache", os.path.basename(cfile)))
        return q, self.map_look(q.id)

    def map_render(self, pid, view="plan", mode="material", size=None) -> bytes:
        return maps_mod.MapLook(self._map(pid)).render(view, mode, size)

    def map_glb(self, pid) -> str:
        path = maps_mod.MapLook(self._map(pid)).glb_path()
        if not path:
            raise maps_mod.LookMissing("no 3D preview yet: the look has not run for these options")
        return path

    # ------------------------------------------------------------------ builds
    def map_build(self, pid, *, auto_qa=True):
        from ..cp77.donor import DonorCache
        from ..mlo.pipeline import MapConverter
        p = self._map(pid)
        opts = p.map_options()                           # validated before queuing
        look = maps_mod.MapLook(p).cached(opts)
        if look and look.get("city"):
            raise ProjectError(f"{p.data.get('name')} is a whole city ({look['overview']['objects']:,} objects): pick a "
                               "district to convert first (the map's Overview, or set its area in Options)")

        def run(job):
            n = p.next_build()
            bdir = p.build_dir(n)
            out = os.path.join(bdir, "out")
            os.makedirs(out, exist_ok=True)
            job.meta["build"] = n
            with open(os.path.join(bdir, "map.json"), "w", encoding="utf-8") as f:
                json.dump(opts, f, indent=1)
            tb = self.toolbox(refresh=True)
            if not tb.wolvenkit:
                raise RuntimeError("WolvenKit CLI not found: set it up in Settings > Tools")
            wk = tb.wolvenkit_obj(log=job.log)
            job.on_cancel.append(wk.kill)
            cache = DonorCache(wk, tb.game_dir, tb.cache_dir, log=job.log)

            def log(msg):
                job.check()
                job.log(msg)
                for needle, prog, label in maps_mod.STEPS:
                    if needle in msg:
                        job.set_progress(prog, label)

            job.set_progress(0.03, "reading the FiveM map")
            version = f"1.0.{n}"
            t0 = time.time()
            from . import ncweassets
            info = {"n": n, "time": time.time(), "ok": False, "kind": "map", "version": version,
                    "place": opts.get("place"), "imports": ncweassets.fingerprint(p)}
            try:
                from . import ncweassets
                prepared, asset_warnings = ncweassets.prepare(p.path("assets", "ncwe"),
                                                              os.path.join(bdir, "_ncwe"), log=job.log)
                for w in asset_warnings:
                    job.log("NCWE assets: " + w)
                conv = MapConverter(wk, cache, out, log=log, extra_packages=prepared,
                                    **maps_mod.converter_opts(opts, version=version,
                                                              keep_work=self.settings.get()["build"]["keep_work"]))
                r = conv.convert(p.source)
                info.update(maps_mod.build_summary(r, n, time.time() - t0))
                info["time"] = t0
                info["place"] = opts.get("place")
                shutil.rmtree(os.path.join(out, "_work"), ignore_errors=True) \
                    if not self.settings.get()["build"]["keep_work"] else None
                if auto_qa:
                    job.set_progress(0.97, "checking the output")
                    q = qa_mod.check_map_build(out)
                    info["qa"] = {"ok": q["ok"], "failed": [c["message"] for c in q["checks"] if not c["ok"] and c["level"] == "error"],
                                  "warnings": [c["message"] for c in q["checks"] if not c["ok"] and c["level"] == "warn"]}
                    with open(os.path.join(bdir, "qa.json"), "w", encoding="utf-8") as f:
                        json.dump(q, f, indent=1)
                    job.log("QA: " + ("all checks passed" if q["ok"] else "FAILED: " + "; ".join(info["qa"]["failed"])))
                return info
            except Exception as e:
                info["error"] = str(e) or e.__class__.__name__
                raise
            finally:
                info["seconds"] = round(time.time() - t0, 1)
                from ..pipeline import VERSION as CONVERTER
                info["converter"] = CONVERTER
                p.add_build(info)
                qc = getattr(self, "qc", None)
                if qc is not None:
                    qc.after_build(p.id)

        return self.jobs.submit("build", f"Build {p.data['name']}", run, project=p.id, heavy=True)

    def map_build_info(self, p, n) -> dict:
        b = next((x for x in p.data.get("builds", []) if x.get("n") == n), None)
        if b is None:
            raise ProjectError("no such build")
        out = os.path.join(p.build_dir(n), "out")
        rep = qa = opts = None
        name = b.get("name")
        if name and os.path.isfile(os.path.join(out, name, "conversion-report.json")):
            with open(os.path.join(out, name, "conversion-report.json"), "r", encoding="utf-8") as f:
                rep = json.load(f)
            # the per-part table is long and the Studio shows it from the look: keep the rest
            rep["parts"] = {k: {kk: v[kk] for kk in ("kind", "vertices", "triangles", "colliders", "collision_only")
                                if kk in v} for k, v in (rep.get("parts") or {}).items()}
        for fn, var in (("qa.json", "qa"), ("map.json", "opts")):
            fp = os.path.join(p.build_dir(n), fn)
            if os.path.isfile(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    if var == "qa":
                        qa = json.load(f)
                    else:
                        opts = json.load(f)
        doors = None
        if name and os.path.isfile(os.path.join(out, name, "doors.json")):
            try:
                with open(os.path.join(out, name, "doors.json"), "r", encoding="utf-8") as f:
                    d = json.load(f)
                doors = {"count": len(d.get("devices", d) if isinstance(d, dict) else d)}
            except (OSError, ValueError, TypeError):
                doors = None
        return {"build": b, "report": rep, "qa": qa, "options": opts, "doors": doors, "out": out}

    def map_qa(self, p, n) -> dict:
        out = os.path.join(p.build_dir(int(n)), "out")
        if not os.path.isdir(out):
            raise ProjectError("no such build")
        q = qa_mod.check_map_build(out)
        with open(os.path.join(p.build_dir(int(n)), "qa.json"), "w", encoding="utf-8") as f:
            json.dump(q, f, indent=1)
        return q

    def _map_build(self, p, build=None) -> dict:
        b = (next((x for x in p.data.get("builds", []) if x.get("n") == int(build) and x.get("ok")), None)
             if build else p.latest_build())
        if not b:
            raise ProjectError("no successful build: build the map first")
        return b

    def map_stale(self, p) -> dict | None:
        """{reason, target} when the last build no longer matches the project's NCWE imports; target
        is where the map was last deployed (for a one-click rebuild and deploy), else None."""
        from . import ncweassets
        reason = ncweassets.build_stale(p, p.latest_build(ok_only=False))
        if not reason:
            return None
        dep = (p.data.get("deployments") or [None])[-1]
        target = None
        if dep:
            try:
                label = self._server(dep["server"])["label"]
            except Exception:  # noqa: BLE001 - the server may have been removed since
                label = dep["server"]
            target = {"server": dep["server"], "label": label, "names": dep.get("names")}
        return {"reason": reason, "target": target}

    def map_rebuild_deploy(self, pid, sid=None, *, names=None, keep_placement=True, restart=True, baker=True):
        """One click: rebuild the map (picking up the current NCWE imports) and deploy that fresh
        build where the map already lives — the last deployment's server and folders when none is
        named."""
        p = self._map(pid)
        if not sid:
            deps = p.data.get("deployments") or []
            if not deps:
                raise ProjectError("this map is not on a server yet: deploy it once first, or name a server")
            sid = deps[-1]["server"]
            names = names or deps[-1].get("names")
        server = self._server(sid)                 # fail early on a bad id

        def run(job):
            job.log(f"rebuild and deploy to {server['label']}")
            bj = self.map_build(pid)
            job.on_cancel.append(bj.cancel)
            job.set_progress(0.02, "building")
            self.jobs.wait(bj)
            job.check()
            if bj.state != "done":
                raise RuntimeError(f"the build did not finish ({bj.state}): {bj.error or 'see the build log'}")
            b = bj.result or {}
            job.log(f"build #{b.get('n')} {b.get('version')} done; deploying")
            job.set_progress(0.6, "deploying")
            dj = self.map_deploy(pid, sid, names=names, build=b.get("n"), keep_placement=keep_placement,
                                 restart=restart, baker=baker)
            job.on_cancel.append(dj.cancel)
            self.jobs.wait(dj)
            job.check()
            if dj.state != "done":
                raise RuntimeError(f"the deploy did not finish ({dj.state}): {dj.error or 'see the deploy log'}")
            return {"build": {"n": b.get("n"), "version": b.get("version")},
                    "deployed": {"server": sid, "label": server["label"],
                                 "names": (dj.result or {}).get("names")}}

        return self.jobs.submit("rebuild-deploy", f"Rebuild and deploy {p.data['name']}", run, project=p.id)

    # ------------------------------------------------------------------ NCWE (the world editor)
    def ncwe_import(self, pid, *, name=None, position=None, yaw=0.0, place=None, height=None, analyze=False):
        """The converted map into the NCWE world editor: exported as a glb (Y up, metres) and
        imported into NCWE's custom mesh library, then placed in the NCWE world at the map's Night
        City spot when one is set (or at an explicit position). Needs the user's NCWE editor: its
        MCP executable in Settings > Agents, open and signed in."""
        from types import SimpleNamespace

        from ..mlo import preview as mp
        from .agent import ncwe
        p = self._map(pid)
        if position is None:
            spot = (p.map_options() or {}).get("place") or {}
            if spot.get("x") is not None and spot.get("y") is not None:
                position = [spot["x"], spot["y"]] + ([spot["z"]] if spot.get("z") is not None else [])
                yaw = float(spot.get("yaw") or yaw or 0.0)
        if place is None:
            place = position is not None
        if place and position is None:
            raise ProjectError("nowhere to place it in NCWE: set the map's spot in Options or pass a position "
                               "[x, y, z] (or [x, y] to drop it on the surface below)")

        def run(job):
            job.set_progress(0.05, "planning the map")
            _scene, model, _coords, _resource = maps_mod.MapLook(p).model(log=job.log, check=job.check)
            glb = p.path("cache", "ncwe_import.glb")
            os.makedirs(os.path.dirname(glb), exist_ok=True)
            job.set_progress(0.3, "exporting the glb")
            exported = mp.export_map_glb(model, glb)
            job.check()
            job.set_progress(0.5, "importing into NCWE")
            session = SimpleNamespace(stopping=False)
            job.on_cancel.append(lambda: setattr(session, "stopping", True))
            r = ncwe.import_model_glb(SimpleNamespace(studio=self, session=session), glb,
                                     name=name or model.display_name or model.name,
                                     position=position, yaw=yaw, place=place, height=height,
                                     analyze_only=analyze)
            job.set_progress(1.0, "done")
            note = ("analyzed only: nothing was imported" if analyze else
                    (f"placed in NCWE at {position} (yaw {yaw:g})" if place else
                     "imported into NCWE's custom mesh library (not placed)"))
            return {"map": model.name, "name": name or model.display_name or model.name, "glb": glb,
                    "glb_summary": exported, "position": position, "asset": r.get("asset"),
                    "placed": r.get("placed") or [], "note": note}

        return self.jobs.submit("ncwe-import", f"Import {p.data['name']} into NCWE", run, project=p.id)

    # ------------------------------------------------------------------ the local game (test boots)
    def map_game_status(self, pid) -> dict:
        from ..mlo.deploy import installed
        p = self._map(pid)
        name = (p.data.get("map") or {}).get("name")
        tb = self.toolbox()
        if not tb.game_dir:
            return {"game": None, "files": [], "name": name}
        try:
            files = installed(tb.game_dir, name) if name else []
        except (OSError, RuntimeError):
            files = []
        g = p.data.get("game") or {}
        return {"game": tb.game_dir, "files": files, "name": name,
                "build": g.get("build") if files else None, "spot": g.get("spot") if files else None}

    def map_game(self, pid, build=None, uninstall=False):
        """Write (or remove) a build's packages in the local game's archive/pc/mod, for direct test
        boots.  Only the map's own fivem2open77_map_<map>* files are touched.

        The map stands at the spot in its Options: a build baked somewhere else (or not baked at
        all) gets a sector package baked for that spot here, the build itself unchanged.  Without a
        spot in Options it stands where the build was baked; a build that was never baked and has no
        spot would put nothing in the game, so that is refused with the way out."""
        from ..mlo import bake as bake_mod
        from ..mlo.deploy import install_game
        p = self._map(pid)
        b = None if uninstall else self._map_build(p, build)
        spot = None
        if not uninstall:
            try:
                spot = (p.map_options() or {}).get("place")
            except maps_mod.MapOptionsError:
                spot = None
            if not spot and not b.get("baked"):
                raise ProjectError(f"{p.data['name']} has no spot yet, so nothing of it would show in your game: set "
                                   "the Night City spot it stands at (Options > Placement, or the 3D city), then "
                                   "install it again")

        def run(job):
            tb = self.toolbox(refresh=True)
            if not tb.game_dir:
                raise RuntimeError("Cyberpunk 2077 was not found: set its folder in Settings > Tools")
            at = None
            if uninstall:
                name = (p.data.get("map") or {}).get("name")
                if not name:
                    raise RuntimeError("this map has no name yet (look at it or build it first)")
                bdir = None
                for x in reversed(p.data.get("builds", [])):
                    if x.get("ok") and x.get("name") == name:
                        bdir = os.path.join(p.build_dir(x["n"]), "out", name)
                        break
                if not bdir:
                    raise RuntimeError("no build of this map to know its files by")
                r = install_game(bdir, tb.game_dir, uninstall=True,
                                 previous=(p.data.get("game") or {}).get("extra") or ())
            else:
                mdir, _ = maps_mod.map_dirs(os.path.join(p.build_dir(b["n"]), "out"))
                baked = (b.get("baked") or {}).get("anchor")
                previous = (p.data.get("game") or {}).get("extra") or ()
                stage = None
                try:
                    if spot and (not baked or bake_mod.normalize_anchor(baked) != bake_mod.normalize_anchor(spot)):
                        if not tb.wolvenkit:
                            raise RuntimeError("WolvenKit CLI not found: set it up in Settings > Tools")
                        job.log(f"build #{b['n']} is " + (f"baked at {_spot_text(baked)}" if baked else "not baked") +
                                f": baking it for its spot {_spot_text(spot)} for your game (the build stays as it is)")
                        job.set_progress(0.1, "baking the sector for the spot")
                        wk = tb.wolvenkit_obj(log=job.log)
                        job.on_cancel.append(wk.kill)
                        stage = tempfile.mkdtemp(prefix="f2o77-game-")
                        name = b.get("name") or os.path.basename(mdir)
                        bake_mod.bake(mdir, spot, wk, log=job.log, out_dir=stage,
                                      bake_id=bake_mod.new_bake_id(name, "game"))
                        at = bake_mod.normalize_anchor(spot)
                        job.set_progress(0.7, "writing the packages")
                        r = install_game(mdir, tb.game_dir, sector=os.path.join(stage, "dist", f"{name}_sector.zip"),
                                         previous=previous)
                    else:
                        at = bake_mod.normalize_anchor(baked)
                        r = install_game(mdir, tb.game_dir, previous=previous)
                finally:
                    if stage:
                        shutil.rmtree(stage, ignore_errors=True)
            for f in r["written"]:
                job.log(f"written {f}")
            for f in r["removed"]:
                job.log(f"removed {f}")
            job.log(f"{len(r['written'])} written, {len(r['unchanged'])} unchanged, {len(r['removed'])} removed in {r['modDir']}")
            if at:
                job.log(f"it stands at {_spot_text(at)}: start the game and go there")
                r["spot"] = at
            with p.lock:
                p.data["game"] = None if uninstall else {"build": b["n"], "time": time.time(), "dir": tb.game_dir,
                                                         "spot": at, "extra": r.get("extra") or []}
                p.save()
            return r

        what = "Remove" if uninstall else "Install"
        return self.jobs.submit("game", f"{what} {p.data['name']} {'from' if uninstall else 'in'} the game", run, project=p.id)

    # ------------------------------------------------------------------ the map baker's toolkit
    def baker_toolkit_version(self) -> str | None:
        from ..mlo import toolkit as tk
        tb = self.toolbox()
        if not tb.linux_dotnet or not tb.wolvenkit:
            return None
        try:
            return tk.version_of(tb.linux_dotnet, tb.wolvenkit)
        except (OSError, RuntimeError):
            return None

    def baker_toolkit(self, host_version, log=print) -> str | None:
        """The toolkit to upload to a host that has `host_version`: the light package when the
        runtime matches, the full one (built once, cached in the workspace) otherwise; None when
        this PC has no Linux .NET runtime to send."""
        from ..mlo import toolkit as tk
        tb = self.toolbox(refresh=True)
        if not tb.linux_dotnet:
            log("no Linux .NET 8 runtime on this PC (Settings > Tools): the map baker is not installed or updated")
            return None
        if not tb.wolvenkit:
            log("no WolvenKit CLI on this PC: the map baker is not installed or updated")
            return None
        want = tk.version_of(tb.linux_dotnet, tb.wolvenkit)
        d = os.path.join(self.workspace, "baker")
        os.makedirs(d, exist_ok=True)
        if host_version == want:
            path = os.path.join(d, f"baker-lib-{want}.tgz")
            tk.build_lib(path, want, log=log)          # small; rebuilt so the package matches this Studio
            return path
        path = os.path.join(d, f"baker-toolkit-{want}-{_lib_hash()}.tgz")
        if not os.path.isfile(path):
            log(f"packing the map baker toolkit {want} (once per Studio version) ...")
            tk.build(path + ".part", tb.linux_dotnet, tb.wolvenkit, log=log)
            os.replace(path + ".part", path)
            for f in os.listdir(d):                    # older toolkits
                if f.startswith("baker-toolkit-") and f != os.path.basename(path):
                    try:
                        os.remove(os.path.join(d, f))
                    except OSError:
                        pass
        return path

    # ------------------------------------------------------------------ servers
    def map_deploy(self, pid, sid, *, names=None, build=None, keep_placement=True, restart=True, baker=True):
        from ..mlo import deploy as mlo_deploy
        p = self._map(pid)
        server = self._server(sid)
        b = self._map_build(p, build)
        out = os.path.join(p.build_dir(b["n"]), "out")
        mdir, _ = maps_mod.map_dirs(out)

        def run(job):
            from ..cp77 import package as package_mod
            name = b.get("name") or os.path.basename(mdir)
            n_pkgs = len(b.get("packages") or [])
            if not b.get("baked") and n_pkgs + 1 > package_mod.MAX_PACKAGES:
                job.log(f"!! build #{b['n']} has {n_pkgs} packages: Open77 allows {package_mod.MAX_PACKAGES} per resource and "
                        "placing the map adds its sector package, so it cannot be placed on the server. Rebuild it "
                        "with this Studio (it packs big maps into fewer packages) and deploy that build")
            tmp = tempfile.mkdtemp(prefix="f2o77-map-")
            try:
                bundle = mlo_deploy.bundle(mdir, os.path.join(tmp, "bundle.tgz"))
                job.log(f"bundle: {', '.join(bundle['resources'])} ({bundle['bytes'] / 1048576:.1f} MiB)")
                tk = (lambda have: self.baker_toolkit(have, log=job.log)) if baker else None
                res = deploy_mod.deploy_map(server, self.secrets, bundle["path"], names=names, toolkit=tk,
                                            keep_placement=keep_placement, restart=restart, log=job.log, job=job)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if not b.get("baked"):
                # an unplaced build stays unplaced unless the server had it placed (then it is rebuilt there)
                res["unplaced"] = [x["name"] for x in res["servers"]
                                   if x.get("code") == 0 and name not in ((x.get("result") or {}).get("kept") or [])]
                for srv in res["unplaced"]:
                    job.log(f"!! {name} is on {srv} but not placed: players see nothing of it until it has a spot. Set "
                            "its spot in Options and deploy again, or place it in game (F9, then Apply)")
            self.links.forget(sid)            # the manager may have a new token / listener
            entry = {"time": time.time(), "server": sid, "names": res["names"], "build": b["n"], "version": b.get("version"),
                     "kind": "map", "results": [{"name": x["name"], "code": x["code"], **(x["result"] or {})}
                                                for x in res["servers"]]}
            with p.lock:
                p.data.setdefault("deployments", []).append(entry)
                p.data["deployments"] = p.data["deployments"][-50:]
                p.save()
            return res

        return self.jobs.submit("deploy", f"Deploy {p.data['name']} to {server['label']}", run, project=p.id)

    def agent_api_job(self, sid, names=None, enable=True, restart=False):
        server = self._server(sid)

        def run(job):
            code = deploy_mod.agent_api(server, self.secrets, names, enable=enable, restart=restart, log=job.log)
            self.links.forget(sid)
            if code != 0:
                raise RuntimeError("the agent API switch did not complete (see the log)")
            return {"code": code, "enabled": enable}

        return self.jobs.submit("server-agent-api", f"{'Turn on' if enable else 'Turn off'} the agent API on {server['label']}",
                                run, meta={"server": sid})

    # ------------------------------------------------------------------ live map control
    def _link(self, sid, name):
        server = self._server(sid)
        if not name:
            name = server["names"][0]
        if not SERVER_NAME_RX.match(name or "") or name not in server["names"]:
            raise ValueError(f"'{name}' is not a server folder of {sid}")
        return self.links.get(server, self.secrets, name)

    def live_status(self, sid, name=None) -> dict:
        """The manager on a server folder: link info, every map, the baker, the restart (or why not)."""
        link = self._link(sid, name)
        try:
            # a page polls this every few seconds: look at the server's files again only now and then,
            # or right after something went wrong
            settled = bool(link.info and link.info.get("token") and link.info.get("manager"))
            info = link.probe(force=link.failed or not settled, max_age=30)
        except (LinkError, deploy_mod.DeployError) as e:
            link.failed = True
            return {"ok": False, "server": sid, "name": link.name, "error": str(e), "link": None}
        out = {"ok": False, "server": sid, "name": link.name, "link": info}
        try:
            out["status"] = link.status()
            out["ok"] = True
            link.failed = False
        except Unreachable as e:
            out.update(error=str(e), reachable=False)
            link.failed = True
        except (LinkError, deploy_mod.DeployError) as e:
            out["error"] = str(e)
            link.failed = True
            out["link"] = link.info
        return out

    def live_map(self, sid, name, map_name) -> dict:
        if not MAP_NAME_RX.match(map_name or ""):
            raise ValueError("bad map name")
        return self._link(sid, name).map(map_name)

    def live_op(self, sid, name, map_name, op, args=None) -> dict:
        if not MAP_NAME_RX.match(map_name or ""):
            raise ValueError("bad map name")
        return self._link(sid, name).op(map_name, op, _clean_args(op, args or {}))

    def live_players(self, sid, name=None) -> dict:
        return self._link(sid, name).players()

    def live_wait(self, sid, name, map_name, until="applied", timeout=900):
        if not MAP_NAME_RX.match(map_name or ""):
            raise ValueError("bad map name")
        link = self._link(sid, name)

        def run(job):
            return link.wait(map_name, until=until, timeout=float(timeout), interval=2.0, log=job.log,
                             cancelled=job.cancel_event.is_set)

        return self.jobs.submit("map-wait", f"Wait for {map_name} on {link.name} ({until})", run,
                                meta={"server": sid, "map": map_name})


def _spot_text(a) -> str:
    a = a or {}
    return f"{float(a.get('x', 0)):.1f} {float(a.get('y', 0)):.1f} {float(a.get('z', 0)):.1f} yaw {float(a.get('yaw') or 0):.0f}"


def _extent_size(ext):
    if not ext:
        return None
    return [round(ext[2] - ext[0], 1), round(ext[3] - ext[1], 1), 0.0]


_ARG_KEYS = {
    "place": ("x", "y", "z", "yaw"), "move": ("dx", "dy", "dz", "dyaw", "frame"), "here": ("playerId", "yaw", "dz"),
    "ground": ("x", "y", "playerId"), "drop": ("x", "y", "dz", "playerId"), "goto": ("playerId", "which", "offset"),
    "apply": ("mode", "countdown", "anchor"), "presetSave": ("name", "anchor"), "presetUse": ("name",),
    "presetDelete": ("name",), "discard": (), "revert": (), "cancel": (),
}


def _clean_args(op, args) -> dict:
    """Only the fields the manager knows for that operation, with plain JSON types."""
    keys = _ARG_KEYS.get(op)
    if keys is None:
        raise ValueError(f"unknown map operation {op}")
    out = {}
    for k in keys:
        v = args.get(k)
        if v is None:
            continue
        if k in ("frame", "which", "mode", "name"):
            if not isinstance(v, str) or len(v) > 64:
                raise ValueError(f"{k}: text")
            out[k] = v
        elif k == "anchor":
            a = maps_mod.parse_place(v)
            out[k] = a
        elif k in ("playerId", "countdown"):
            out[k] = int(v)
        elif k == "offset":
            out[k] = [float(x) for x in v][:3] if isinstance(v, (list, tuple)) else float(v)
        else:
            f = float(v)
            if f != f or abs(f) > 1e6:
                raise ValueError(f"{k}: a number")
            out[k] = f
    return out


def _lib_hash() -> str:
    """Which baker package a cached toolkit carries."""
    import hashlib
    from ..mlo import toolkit as tk
    h = hashlib.sha256()
    for rel in tk.BAKER_LIB:
        with open(os.path.join(tk.PACKAGE_ROOT, *rel.split("/")), "rb") as f:
            h.update(rel.encode() + b"\0" + f.read())
    return h.hexdigest()[:10]
