"""The Studio's world edits (what the API, the World page and the agents call).

A world edit project changes Night City itself: objects and ground pieces deleted, moved, replaced,
objects added.  The edits are kept as a list (world.json) with an undo history; a build turns them
into an Open77 resource (an ArchiveXL package: an ``.xl`` that patches the game's own sectors as
they stream in, and a sector of its own for added objects), which installs on servers like a car
(players get it with the server's required mods at their next launch).

Mixed into studio.app.Studio: uses self.projects, self.jobs, self.settings, self.secrets,
self.toolbox() and self.workspace.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import zipfile

from ..world import edits as E
from ..world import ops as O
from ..world import package as P
from ..world.index import WorldIndex
from . import deploy as deploy_mod
from .projects import ProjectError

NAME_RX = P.NAME_RX
KIND_CHOICES = ("mesh", "instanced", "terrain", "collision", "entity", "decal", "light", "foliage", "occluder",
                "effect", "spawner", "water", "road", "traffic", "other")


class WorldOps:
    _world_lock = None
    _world_index = None
    world_extractor = None          # tests (and tools) can hand in their own extract(paths) -> {path: json}

    # ------------------------------------------------------------------ the world index
    def _wlock(self):
        if self._world_lock is None:
            self._world_lock = threading.RLock()
        return self._world_lock

    def world_index(self) -> WorldIndex:
        with self._wlock():
            if self._world_index is None:
                self._world_index = WorldIndex(self.workspace, extract=self._world_extract, log=self._world_log)
            return self._world_index

    def _world_log(self, msg):
        job = getattr(self._wtls(), "job", None)
        if job is not None:
            job.log(msg)

    def _wtls(self):
        if not hasattr(self, "_world_tls"):
            self._world_tls = threading.local()
        return self._world_tls

    def _world_extract(self, paths):
        if self.world_extractor is not None:
            return self.world_extractor(paths)
        from ..world.extract import GameExtractor
        tb = self.toolbox(refresh=False)
        if not tb.game_dir:
            raise RuntimeError("Cyberpunk 2077 was not found: set its folder in Settings > Tools")
        if not tb.wolvenkit:
            raise RuntimeError("WolvenKit CLI not found: set it up in Settings > Tools")
        wk = tb.wolvenkit_obj(log=self._world_log)
        ex = GameExtractor(wk, tb.game_dir, os.path.join(self.workspace, "world", "tmp"), log=self._world_log)
        try:
            return _Extracted(ex(paths), ex)
        except Exception:
            ex.cleanup()
            raise

    def world_index_status(self) -> dict:
        idx = self.world_index()
        sd = os.path.join(idx.dir, "sectors")
        cached = len([f for f in os.listdir(sd) if f.endswith(".json")]) if os.path.isdir(sd) else 0
        tb = self.toolbox()
        return {"ready": idx.has_index(), "sectors": len(idx.rows()) if idx.has_index() else 0, "cached": cached,
                "game": tb.game_dir, "wolvenkit": bool(tb.wolvenkit)}

    def world_prepare(self, force=False):
        """A job: read the world's sector list from the game (once; a minute or two)."""
        for j in self.jobs.list():
            if j["kind"] == "world-index" and j["state"] in ("queued", "running"):
                return self.jobs.get(j["id"])

        def run(job):
            self._wtls().job = job
            try:
                with self._wlock():
                    idx = self.world_index()
                    if idx.has_index() and not force:
                        return self.world_index_status()
                    job.set_progress(0.1, "reading the sector list")
                    idx.build_index()
                    return self.world_index_status()
            finally:
                self._wtls().job = None

        return self.jobs.submit("world-index", "Read Night City's sector list from the game", run, heavy=True)

    def _with_index(self, fn):
        with self._wlock():
            idx = self.world_index()
            if not idx.has_index():
                if self.world_extractor is None and not self.toolbox().game_dir:
                    raise RuntimeError("the world is not read yet: set the Cyberpunk 2077 folder in Settings > Tools")
                idx.build_index()
            return fn(idx)

    # ------------------------------------------------------------------ projects
    def _world(self, pid):
        return self.projects.get(pid).need("world")

    def create_world_project(self, name: str | None = None, display: str | None = None, center=None):
        display = (display or "").strip()[:80] or "World edit"
        if not name:
            base = re.sub(r"[^a-z0-9_]+", "_", display.lower()).strip("_") or "edit"
            name = ("world_" + base)[:40]
            if not re.match(r"^[a-z]", name):
                name = "w_" + name
        name = name.lower()
        if not NAME_RX.match(name):
            raise ValueError(f"name {name!r}: lower case letters, digits and _ (3-48), starting with a letter")
        taken = {(p.get("world") or {}).get("name") for p in self.projects.list() if p.get("kind") == "world"}
        if name in taken:
            raise ValueError(f"a world edit named {name} exists already")
        c = _center(center) if center is not None else None
        return self.projects.create_world(name, display, c)

    def world_state(self, pid) -> dict:
        p = self._world(pid)
        w = p.world()
        edits = w.get("edits") or []
        try:
            summ, problem = E.summary(edits), None
        except E.EditError as e:
            summ, problem = None, str(e)
        running = [j for j in self.jobs.list(project=p.id) if j["state"] in ("queued", "running")]
        return {"project": p.summary(), "name": w.get("name"), "display": w.get("display"), "center": w.get("center"),
                "edits": edits, "actions": O.describe(edits), "summary": summ, "problem": problem,
                "can_undo": bool(w.get("undo")), "can_redo": bool(w.get("redo")), "jobs": running,
                "builds": list(reversed(p.data.get("builds", []))), "deployments": p.data.get("deployments", [])[-20:],
                "game": self.world_game_status(pid), "index": self.world_index_status()}

    def world_set(self, pid, name=None, display=None, center=None) -> dict:
        p = self._world(pid)
        with p.lock:
            w = p.world()
            if name is not None:
                name = str(name).lower()
                if not NAME_RX.match(name):
                    raise ValueError(f"name {name!r}: lower case letters, digits and _ (3-48), starting with a letter")
                if p.data.get("deployments") and name != w.get("name"):
                    raise ValueError("this edit is installed on a server under its name: keep the name")
                w["name"] = name
            if display is not None:
                w["display"] = str(display).strip()[:80] or w.get("display")
                p.data["name"] = w["display"]
            if center is not None:
                w["center"] = _center(center)
            p.data["world"] = {"name": w.get("name"), "display": w.get("display"), "edits": len(w.get("edits") or [])}
            p.save_world(w)
        return self.world_state(pid)

    # ------------------------------------------------------------------ the area
    def world_query(self, x, y, radius=40.0, z=None, dz=None, kinds=None, text=None, limit=400, element_limit=200):
        x, y, radius = float(x), float(y), max(1.0, min(400.0, float(radius)))
        if kinds:
            kinds = [k for k in kinds if isinstance(k, str)][:20]
        return self._with_index(lambda idx: idx.query(x, y, radius, z=None if z is None else float(z),
                                                      dz=None if dz is None else float(dz), kinds=kinds or None,
                                                      text=text, limit=int(limit), element_limit=int(element_limit)))

    def world_object(self, sector, index, element=None) -> dict:
        """One object in full: the entry, and what stands exactly with it (its collision, sharers)."""
        def fn(idx):
            sec, e = idx.entry(sector, int(index))
            comp = O.companions(idx, sec, e, None if element is None else int(element))
            out = dict(e, sector=sec["path"], expectedNodes=sec["nodes"], level=sec.get("level"))
            el = out.get("elements")
            if el and element is None and len(el.get("items") or []) > 200:
                out["elements"] = dict(el, items=[dict(it, k=k) for k, it in enumerate(el["items"][:200])], truncated=True)
            return {"object": out, "collision": comp["collision"], "sharers": comp["sharers"]}
        return self._with_index(fn)

    # ------------------------------------------------------------------ edits
    def world_apply(self, pid, action: dict) -> dict:
        """One action -> its edits (one group) at the end of the list.  action: {op: delete | move |
        replace | add | clear_area, ...}; ``dry_run`` answers what it would add without adding it."""
        p = self._world(pid)
        op = action.get("op")
        notes = []
        col = action.get("with_collision", True) is not False

        def targets_of(a):
            t = a.get("targets")
            if t is None:
                t = [{"sector": a.get("sector"), "index": a.get("index"), "element": a.get("element")}]
            out = []
            for x in t[:2000]:
                if not isinstance(x, dict) or not x.get("sector") or x.get("index") is None:
                    raise ValueError("a target is {sector, index, element?}")
                el = x.get("element")
                out.append((str(x["sector"]), int(x["index"]), None if el is None else int(el)))
            return out

        def build(idx):
            if op == "delete":
                tg = targets_of(action)
                if len(tg) == 1:
                    return O.delete(idx, tg[0][0], tg[0][1], element=tg[0][2], with_collision=col, notes=notes)
                return O.delete_many(idx, tg, with_collision=col, notes=notes)
            if op == "move":
                (sector, i, el), = targets_of(action)
                return O.move(idx, sector, i, element=el, pos=action.get("pos"), yaw=action.get("yaw"),
                              rot=action.get("rot"), delta=action.get("delta"), dyaw=action.get("dyaw"),
                              scale=action.get("scale"), with_collision=col, notes=notes)
            if op == "replace":
                (sector, i, _el), = targets_of(action)
                return O.replace(idx, sector, i, res=action.get("res"), app=action.get("app"))
            if op == "add":
                res = str(action.get("res") or "").lower()
                mode = action.get("collision") or "mesh"
                if res.endswith(".mesh") and mode == "mesh":
                    try:
                        info = idx.mesh_info(res)
                        if info is None:
                            notes.append(f"{res}: not found in the game's files (check the path)")
                        elif not info.get("physics"):
                            notes.append(f"{res.split(chr(92))[-1]} has no collision shapes of its own: players walk "
                                         "through it (an .ent of the game brings its own collision)")
                    except Exception as e:  # noqa: BLE001 - checking the mesh is a nicety
                        notes.append(f"could not check {res}: {e}")
                return O.add(res, action.get("pos"), action.get("yaw") or 0.0, app=action.get("app"),
                             collision=mode, label=action.get("label"))
            if op == "clear_area":
                kinds = action.get("kinds") or ["mesh", "instanced", "entity", "decal"]
                return O.clear_area(idx, float(action["x"]), float(action["y"]), max(0.5, min(150.0, float(action.get("radius", 10)))),
                                    kinds=kinds, text=action.get("text"), z=action.get("z"), dz=action.get("dz"),
                                    with_collision=col)
            raise ValueError("op: delete, move, replace, add or clear_area")

        if op == "add":
            with self._wlock():                       # no sector list needed: the mesh is read on its own
                new = build(self.world_index())
        else:
            new = self._with_index(build)
        if not new:
            return {"added": 0, "notes": notes or ["nothing matched"], "edits": [], "group": None}
        with p.lock:
            w = p.world()
            seq = int(w.get("seq") or 0)
            for e in new:
                seq += 1
                e["id"] = f"e{seq}"
            norm_new = E.normalize_all(new)              # each edit checked
            all_edits = (w.get("edits") or []) + norm_new
            E.normalize_all(all_edits)                   # and the whole list (conflicts)
            group = norm_new[0].get("group")
            if action.get("dry_run"):
                return {"dry_run": True, "group": group, "edits": norm_new, "actions": O.describe(norm_new),
                        "notes": notes, "summary": E.summary(norm_new)}
            w["seq"] = seq
            w["edits"] = all_edits
            w.setdefault("undo", []).append(group)
            w["undo"] = w["undo"][-200:]
            w["redo"] = []
            p.data["world"] = {"name": w.get("name"), "display": w.get("display"), "edits": len(all_edits)}
            p.save_world(w)
        return {"group": group, "edits": norm_new, "actions": O.describe(norm_new), "notes": notes,
                "summary": E.summary(all_edits)}

    def world_undo(self, pid) -> dict:
        p = self._world(pid)
        with p.lock:
            w = p.world()
            if not w.get("undo"):
                raise ValueError("nothing to undo")
            g = w["undo"].pop()
            gone = [e for e in w.get("edits") or [] if e.get("group") == g]
            w["edits"] = [e for e in w.get("edits") or [] if e.get("group") != g]
            w.setdefault("redo", []).append({"group": g, "edits": gone})
            p.data["world"] = {"name": w.get("name"), "display": w.get("display"), "edits": len(w["edits"])}
            p.save_world(w)
        return {"undone": O.describe(gone), "summary": E.summary(w["edits"])}

    def world_redo(self, pid) -> dict:
        p = self._world(pid)
        with p.lock:
            w = p.world()
            if not w.get("redo"):
                raise ValueError("nothing to redo")
            r = w["redo"].pop()
            edits = (w.get("edits") or []) + r["edits"]
            E.normalize_all(edits)
            w["edits"] = edits
            w.setdefault("undo", []).append(r["group"])
            p.data["world"] = {"name": w.get("name"), "display": w.get("display"), "edits": len(edits)}
            p.save_world(w)
        return {"redone": O.describe(r["edits"]), "summary": E.summary(edits)}

    def world_drop(self, pid, group: str) -> dict:
        """Take one action (group) out of the list, wherever it is."""
        p = self._world(pid)
        with p.lock:
            w = p.world()
            gone = [e for e in w.get("edits") or [] if e.get("group") == group]
            if not gone:
                raise ValueError(f"no action {group}")
            w["edits"] = [e for e in w["edits"] if e.get("group") != group]
            w["undo"] = [g for g in w.get("undo") or [] if g != group]
            p.data["world"] = {"name": w.get("name"), "display": w.get("display"), "edits": len(w["edits"])}
            p.save_world(w)
        return {"dropped": O.describe(gone), "summary": E.summary(w["edits"])}

    # ------------------------------------------------------------------ builds
    def world_build(self, pid):
        p = self._world(pid)
        w = p.world()
        edits = E.normalize_all(w.get("edits") or [])           # checked before queuing
        if not edits:
            raise ValueError("nothing to build: no edits yet")
        needs_wk = any(e["op"] == "add" for e in edits)

        def run(job):
            n = p.next_build()
            bdir = p.build_dir(n)
            out = os.path.join(bdir, "out")
            os.makedirs(out, exist_ok=True)
            job.meta["build"] = n
            t0 = time.time()
            version = f"1.0.{n}"
            info = {"n": n, "time": t0, "ok": False, "kind": "world", "version": version, "name": w["name"]}
            try:
                wk = None
                if needs_wk:
                    tb = self.toolbox(refresh=True)
                    if not tb.wolvenkit:
                        raise RuntimeError("adding objects needs WolvenKit CLI: set it up in Settings > Tools")
                    wk = tb.wolvenkit_obj(log=job.log)
                    job.on_cancel.append(wk.kill)
                job.set_progress(0.1, "checking the sectors against this game")
                checks = self._world_check(edits, job.log)
                checks.update(self._pairing_check(edits, job.log))
                job.set_progress(0.3, "writing the package")
                r = P.build(w["name"], w.get("display") or w["name"], edits, out, wk=wk, log=job.log, version=version)
                info.update(ok=True, summary=r["summary"], bytes=r["bytes"], sha256=r["sha256"], checks=checks,
                            resource=w["name"])
                with open(os.path.join(bdir, "edits.xl"), "w", encoding="utf-8") as f:
                    f.write(r["xl"])
                return info
            except Exception as e:
                info["error"] = str(e) or e.__class__.__name__
                raise
            finally:
                info["seconds"] = round(time.time() - t0, 1)
                p.add_build(info)

        return self.jobs.submit("build", f"Build {p.data['name']}", run, project=p.id, heavy=needs_wk)

    def _world_check(self, edits, log) -> dict:
        """Every sector an edit patches still has the node count the edit was made against (a game
        update renumbers sectors: ArchiveXL would skip the patch)."""
        sectors = sorted({e["sector"] for e in edits if e["op"] != "add"})
        if not sectors:
            return {"sectors": 0, "checked": 0}
        idx = self.world_index()
        have = {}
        for s in sectors:
            sec = idx._cached(s)
            if sec:
                have[s] = sec["nodes"]
        bad = [s for s in sectors if s in have and have[s] != next(e["expectedNodes"] for e in edits if e.get("sector") == s)]
        if bad:
            raise RuntimeError(f"{len(bad)} sector(s) changed since the edits were made (a game update?): {', '.join(bad[:3])}"
                               " -- make those edits again")
        log(f"{len(have)}/{len(sectors)} sector(s) checked against this game's files")
        return {"sectors": len(sectors), "checked": len(have)}

    def _pairing_check(self, edits, log) -> dict:
        """Mesh/collision pairing safety net, run on every build.

        Catches an orphan collision (a collision left standing whose meshes all go -> an invisible
        barrier) and a walk-through mesh (a mesh left standing whose collision goes -> players walk
        through it).  Uses the game index so shared collision kept for a neighbouring object that
        stays is not flagged.  Raises on either, so a broken pair never ships."""
        idx = self.world_index()

        def elem(e):
            els = e.get("elements") or []
            return int(els[0]) if els else None

        def key(sector, i, element):
            return (str(sector).lower(), int(i), None if element is None else int(element))

        deleted_mesh, deleted_col, mesh_pos = set(), set(), []
        for e in edits:
            if e.get("op") != "delete":
                continue
            k = key(e["sector"], e["index"], elem(e))
            if e.get("role") == "collision":
                deleted_col.add(k)
            else:
                deleted_mesh.add(k)
                p = (e.get("was") or {}).get("pos")
                if p:
                    mesh_pos.append(p)

        orphan, walkthrough, checked, done = [], [], 0, set()
        for e in edits:
            if e.get("op") != "delete" or e.get("role") == "collision":
                continue
            sec, entry = idx.entry(e["sector"], e["index"])
            comp = O.companions(idx, sec, entry, elem(e))
            coll = comp["collision"]
            if not coll:
                continue
            place = tuple(round(v, 2) for v in coll[0]["pos"])
            if place in done:
                continue
            done.add(place)
            checked += 1
            col_kept = any(key(c["sector"], c["index"], c.get("element")) not in deleted_col for c in coll)
            kept_sharers = [s for s in comp["sharers"]
                            if key(s["sector"], s["index"], s.get("element")) not in deleted_mesh]
            if col_kept and not kept_sharers:
                orphan.append(str(e.get("label") or e["index"]))
            elif not col_kept and kept_sharers:
                walkthrough.append(str(e.get("label") or e["index"]))

        # walk-through also when a collision is deleted but no mesh at its place is (its mesh stays)
        for e in edits:
            if e.get("op") != "delete" or e.get("role") != "collision":
                continue
            p = (e.get("was") or {}).get("pos")
            if p and not any(all(abs(a - b) <= 1.0 for a, b in zip(p, q)) for q in mesh_pos):
                walkthrough.append(f"collision at ({p[0]:.0f},{p[1]:.0f},{p[2]:.0f}) with its mesh kept")

        log(f"pairing check: {checked} shared place(s), {len(orphan)} orphan collision(s), "
            f"{len(walkthrough)} walk-through mesh(es)")
        if orphan or walkthrough:
            raise RuntimeError("mesh/collision pairing broken: "
                               f"{len(orphan)} orphan collision(s) (invisible barrier), "
                               f"{len(walkthrough)} walk-through mesh(es): "
                               + "; ".join((orphan + walkthrough)[:4]))
        return {"paired": checked, "orphan": len(orphan), "walkthrough": len(walkthrough)}

    def world_build_info(self, p, n) -> dict:
        b = next((x for x in p.data.get("builds", []) if x.get("n") == n), None)
        if b is None:
            raise ProjectError("no such build")
        xl = None
        fp = os.path.join(p.build_dir(n), "edits.xl")
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                xl = f.read()
        return {"build": b, "xl": xl, "out": os.path.join(p.build_dir(n), "out")}

    def _world_res(self, p, build=None):
        b = (next((x for x in p.data.get("builds", []) if x.get("n") == int(build) and x.get("ok")), None)
             if build else p.latest_build())
        if not b:
            raise ProjectError("no successful build: build the world edit first")
        res = os.path.join(p.build_dir(b["n"]), "out", b.get("resource") or b.get("name"))
        if not os.path.isfile(os.path.join(res, "open77.lua")):
            raise ProjectError(f"build {b['n']} has no resource folder")
        return b, res

    # ------------------------------------------------------------------ servers
    def world_deploy(self, pid, sid, *, names=None, build=None, mode=None):
        p = self._world(pid)
        server = self._server(sid)
        b, res = self._world_res(p, build)
        mode = mode or self.settings.get()["deploy"]["mode"]
        if mode not in ("stage", "restart", "files"):
            raise ValueError("mode: stage, restart or files")

        def run(job):
            tmp = tempfile.mkdtemp(prefix="f2o77-world-")
            try:
                bundle = P.bundle([res], os.path.join(tmp, "world.tgz"))
                job.log(f"bundle: {', '.join(bundle['resources'])} ({bundle['bytes'] / 1024:.0f} KiB)")
                out = deploy_mod.deploy(server, self.secrets, bundle["path"], mode=mode, names=names, log=job.log)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            entry = {"time": time.time(), "server": sid, "names": out["names"], "mode": mode, "build": b["n"],
                     "version": b.get("version"), "kind": "world", "staged": len(out["staged"]), "live": len(out["live"]),
                     "pending": len(out["pending"])}
            with p.lock:
                p.data.setdefault("deployments", []).append(entry)
                p.data["deployments"] = p.data["deployments"][-50:]
                p.save()
            return out

        return self.jobs.submit("deploy", f"Deploy {p.data['name']} to {server['label']} ({mode})", run, project=p.id)

    def _world_project_named(self, resource):
        for info in self.projects.list():
            if info.get("kind") != "world":
                continue
            try:
                p = self.projects.get(info["id"])
            except Exception:  # noqa: BLE001
                continue
            if p.world().get("name") == resource:
                return p
        return None

    def world_remove(self, sid, resource, *, names=None, restart=False):
        """Take a world edit off a server (its resource and resources.load entry; backed up there).  The
        world edit project of that name, when there is one, notes it under its deployments."""
        server = self._server(sid)
        if not NAME_RX.match(resource or ""):
            raise ValueError(f"'{resource}' is not the name of a world edit")
        p = self._world_project_named(resource)

        def run(job):
            out = deploy_mod.remove_world(server, self.secrets, resource, names=names, restart=restart, log=job.log)
            if p is not None:
                entry = {"time": time.time(), "server": sid, "names": out["names"], "kind": "world", "removed": True,
                         "restart": bool(restart), "pending": len(out["pending"])}
                with p.lock:
                    p.data.setdefault("deployments", []).append(entry)
                    p.data["deployments"] = p.data["deployments"][-50:]
                    p.save()
            return out

        return self.jobs.submit("deploy", f"Remove {resource} from {server['label']}", run,
                                project=p.id if p is not None else None, meta={"server": sid})

    # ------------------------------------------------------------------ the local game (test boots)
    def world_game_status(self, pid) -> dict:
        p = self._world(pid)
        name = p.world().get("name")
        tb = self.toolbox()
        if not tb.game_dir or not name:
            return {"game": tb.game_dir, "files": [], "name": name}
        mod = os.path.join(tb.game_dir, "archive", "pc", "mod")
        stem = P.archive_name(name)
        files = sorted(f for f in os.listdir(mod) if f.startswith(stem + ".")) if os.path.isdir(mod) else []
        return {"game": tb.game_dir, "files": files, "name": name, "build": (p.data.get("game") or {}).get("build") if files else None}

    def world_game(self, pid, build=None, uninstall=False):
        """Write (or remove) a build's package in the local game's archive/pc/mod, for a direct test
        boot.  Only this edit's own fivem2open77_world_<name>.* files are touched."""
        p = self._world(pid)
        b, res = (None, None) if uninstall else self._world_res(p, build)

        def run(job):
            tb = self.toolbox(refresh=True)
            if not tb.game_dir:
                raise RuntimeError("Cyberpunk 2077 was not found: set its folder in Settings > Tools")
            mod = os.path.join(tb.game_dir, "archive", "pc", "mod")
            os.makedirs(mod, exist_ok=True)
            stem = P.archive_name(p.world()["name"])
            removed, written = [], []
            for f in os.listdir(mod):
                if f.startswith(stem + "."):
                    os.remove(os.path.join(mod, f))
                    removed.append(f)
            if not uninstall:
                with zipfile.ZipFile(os.path.join(res, "dist", os.path.basename(res) + ".zip")) as z:
                    for n in z.namelist():
                        base = n.split("/")[-1]
                        if not n.startswith("archive/pc/mod/") or not base.startswith(stem + "."):
                            continue
                        with z.open(n) as src, open(os.path.join(mod, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        written.append(base)
            for f in written:
                job.log(f"written {f}")
            for f in removed:
                if f not in written:
                    job.log(f"removed {f}")
            with p.lock:
                p.data["game"] = None if uninstall else {"build": b["n"], "time": time.time(), "dir": tb.game_dir}
                p.save()
            return {"written": written, "removed": [f for f in removed if f not in written], "modDir": mod}

        what = "Remove" if uninstall else "Install"
        return self.jobs.submit("game", f"{what} {p.data['name']} {'from' if uninstall else 'in'} the game", run, project=p.id)


class _Extracted(dict):
    """The extractor's answer, with the cleanup of its temporary files (the index calls it)."""

    def __init__(self, files, ex):
        super().__init__(files)
        self._ex = ex

    def cleanup(self):
        self._ex.cleanup()


def _center(c):
    if isinstance(c, dict):
        vals = [c.get("x"), c.get("y"), c.get("z", 0.0)]
    elif isinstance(c, (list, tuple)) and len(c) in (2, 3):
        vals = list(c) + ([0.0] if len(c) == 2 else [])
    else:
        raise ValueError("center: {x, y, z} or [x, y, z]")
    try:
        x, y, z = (float(v) for v in vals)
    except (TypeError, ValueError):
        raise ValueError("center: numbers") from None
    if any(v != v or abs(v) > 100000 for v in (x, y, z)):
        raise ValueError("center: out of range")
    return {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)}
