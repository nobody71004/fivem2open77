"""The map baker: builds a map's sector package on the server host when the in-game map manager
(f2o77_maps) asks for a move, and installs it while the server is stopped.

    python3 -m fivem2open77.mlo.baker run      --server-dir DIR [--wolvenkit W] [--dotnet D]   (the service)
    python3 -m fivem2open77.mlo.baker once     --server-dir DIR ...      one pass over the requests, then exit
    python3 -m fivem2open77.mlo.baker promote  --server-dir DIR          install staged builds (the server
                                                                         unit's ExecStartPre: the start hook)
    python3 -m fivem2open77.mlo.baker status   --server-dir DIR          JSON: manager, requests, answers, builds
    python3 -m fivem2open77.mlo.baker bake     <map> --anchor x,y,z[,yaw] --server-dir DIR     (operator)
                                              [--in-place: into the map resource now, for a deploy that
                                              restarts the server anyway]
    python3 -m fivem2open77.mlo.baker unstage  <map> --server-dir DIR                          (operator)

How a move goes:
  1. the manager writes resources/**/f2o77_maps/data/bake/requests/<map>.json (its intent: an id,
     action "bake" with the anchor, or "unstage");
  2. this baker answers in .../data/bake/status/<map>.json (baking -> done | failed | cancelled),
     building the package into <server>/f2o77-maps/staged/<map>/ -- outside resources/, so the live
     map and the files the server serves never change while it runs;
  3. the manager restarts the server; before it comes back the start hook (`promote`) copies the
     staged build over the map resource (backups in <server>/f2o77-maps/backups/) and the manager's
     shared/maps/<map>.lua, and answers "promoted".  A staged build is installed only while the
     manager's request still names it: cancelling (an "unstage" request) or a newer request wins.
     Without the start hook the manager asks for the install in the last seconds of the restart
     countdown (request with promoteNow).

Every file written under a resource folder makes the server re-scan it, so this baker writes only
the small answers there (a handful per move) and baker.json once when it starts.

Standard library only (no numpy): runs with the system python3 (3.8+) next to the server.  POSIX.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import time
import traceback

from . import bake as bake_mod
from . import resource as resource_mod

VERSION = "1.0.0"
MANAGER = resource_mod.MANAGER
WORK = "f2o77-maps"                    # <server>/f2o77-maps: the baker's own folder, outside resources/
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
MAX_DEPTH = 4                          # resources/<a>/<b>/<c>/<resource>
SKIP_DIRS = {"data", "dist", "web", "node_modules", "__pycache__"}
KEEP_BACKUPS = 5
KEEP_APPLIED = 5
STALE_TMP_SECONDS = 3600
BAKE_LOCK_SECONDS = 900
TERMINAL = ("failed", "cancelled", "unstaged", "promoted")   # answers after which a request is spent
PROMOTE_LOCK_SECONDS = 60              # the start hook waits this long for a bake in progress (TimeoutStartSec is 90 s)


class BakerError(Exception):
    pass


def now() -> int:
    return int(time.time())


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, value):
    """Atomic: a reader (the server) sees the old file or the new one, never half of it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, indent=1)
    os.replace(tmp, path)


def copy_atomic(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.f2o77-tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


@contextlib.contextmanager
def file_lock(path, timeout=None, poll=0.25):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = None if timeout is None else time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if deadline is not None and time.time() >= deadline:
                    raise BakerError(f"{path} is held by another baker process") from e
                time.sleep(poll)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _safe_rel(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    if not rel or rel.startswith(("/", "\\")) or ".." in parts or any(p == "" for p in parts):
        raise BakerError(f"refusing path {rel!r}")
    return os.path.join(*parts)


def valid_request(req, name: str) -> bool:
    if not isinstance(req, dict) or not NAME_RE.match(name) or req.get("map") != name:
        return False
    rid = req.get("id")
    if not isinstance(rid, str) or not rid or len(rid) > 128 or not re.match(r"^[A-Za-z0-9_.:-]+$", rid):
        return False
    action = req.get("action", "bake")
    if action not in ("bake", "unstage"):
        return False
    if action == "bake":
        try:
            bake_mod.normalize_anchor(req.get("anchor") or {})
        except (KeyError, TypeError, ValueError):
            return False
    return bool(NAME_RE.match(req.get("resource") or name))


def _prune(folder: str, prefix: str, keep: int):
    if not os.path.isdir(folder):
        return
    entries = sorted(e for e in os.listdir(folder) if e.startswith(prefix))
    for e in entries[:-keep] if keep else entries:
        shutil.rmtree(os.path.join(folder, e), ignore_errors=True)


class Server:
    """One Open77 server folder: its map manager, its maps and the baker's own work folder."""

    def __init__(self, server_dir: str, log=print):
        self.dir = os.path.abspath(server_dir)
        self.resources = os.path.join(self.dir, "resources")
        if not os.path.isdir(self.resources):
            raise BakerError(f"{self.dir} is not an Open77 server folder (no resources/)")
        self.work = os.path.join(self.dir, WORK)
        self.staged = os.path.join(self.work, "staged")
        self.backups = os.path.join(self.work, "backups")
        self.applied = os.path.join(self.work, "applied")
        self.lock = os.path.join(self.work, "baker.lock")
        self.log = log
        self._found = {}
        self._missing = {}

    # ---------------------------------------------------------------- finding resources
    def find(self, name: str, need=("open77.lua",)):
        hit = self._found.get(name)
        if hit and all(os.path.isfile(os.path.join(hit, n)) for n in need):
            return hit
        if time.time() - self._missing.get((name, need), 0) < 20:
            return None                     # looked a moment ago: do not walk the tree every second
        for dp, dns, _ in os.walk(self.resources):
            depth = 0 if dp == self.resources else os.path.relpath(dp, self.resources).count(os.sep) + 1
            dns[:] = sorted(d for d in dns if not d.startswith(".") and not d.endswith(".f2o77-new")
                            and d not in SKIP_DIRS)
            if name in dns:
                cand = os.path.join(dp, name)
                if all(os.path.isfile(os.path.join(cand, n)) for n in need):
                    self._found[name] = cand
                    return cand
            if depth >= MAX_DEPTH - 1:
                dns[:] = []
        self._missing[(name, need)] = time.time()
        return None

    def manager(self):
        return self.find(MANAGER)

    def map_resource(self, name: str):
        return self.find(name, ("open77.lua", "layout.json"))

    def bake_dir(self):
        m = self.manager()
        return os.path.join(m, "data", "bake") if m else None

    # ---------------------------------------------------------------- the manager's files
    def request(self, name: str):
        d = self.bake_dir()
        req = read_json(os.path.join(d, "requests", f"{name}.json")) if d else None
        return req if valid_request(req, name) else None

    def requests(self) -> list:
        d = self.bake_dir()
        folder = os.path.join(d, "requests") if d else None
        if not folder or not os.path.isdir(folder):
            return []
        out = []
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json"):
                req = self.request(fn[:-5])
                if req:
                    out.append(req)
        return out

    def write_request(self, req: dict):
        d = self.bake_dir()
        if d:
            write_json(os.path.join(d, "requests", f"{req['map']}.json"), req)

    def status(self, name: str):
        d = self.bake_dir()
        return read_json(os.path.join(d, "status", f"{name}.json")) if d else None

    def set_status(self, name: str, st: dict):
        st = dict(st, map=name, at=now(), baker=VERSION)
        d = self.bake_dir()
        if d:
            write_json(os.path.join(d, "status", f"{name}.json"), st)
            if st["state"] in TERMINAL:
                self.retire_request(name, st.get("id"))
        self.log(f"[{name}] {st['state']}: {st.get('message', '')}")
        return st

    def retire_request(self, name: str, rid):
        """A request that has its final answer goes, so nothing can run it a second time (a deploy
        replacing the manager's folder under a running baker, a lost answer)."""
        d = self.bake_dir()
        path = os.path.join(d, "requests", f"{name}.json") if d else None
        if path and rid:
            cur = read_json(path)
            if isinstance(cur, dict) and cur.get("id") == rid:
                with contextlib.suppress(OSError):
                    os.remove(path)

    def hook(self) -> dict:
        """The start hook the installer set up (map_deploy.sh writes hook.json next to it)."""
        h = read_json(os.path.join(self.work, "hook.json"))
        if not isinstance(h, dict):
            return {"installed": False}
        drop_in = h.get("dropIn")
        ok = False
        if drop_in and os.path.isfile(drop_in):
            with open(drop_in, encoding="utf-8", errors="replace") as f:
                ok = "promote" in f.read()
        return dict(h, installed=ok)

    def write_info(self, wolvenkit_path):
        d = self.bake_dir()
        if not d:
            return None
        info = {"version": VERSION, "pid": os.getpid(), "startedAt": now(), "hook": self.hook().get("installed", False),
                "wolvenkit": wolvenkit_path, "python": sys.version.split()[0], "serverDir": self.dir}
        write_json(os.path.join(d, "baker.json"), info)
        return info

    def staged_build(self, name: str):
        path = os.path.join(self.staged, name)
        b = read_json(os.path.join(path, "build.json"))
        return (path, b) if isinstance(b, dict) else (None, None)

    # ---------------------------------------------------------------- bake / unstage / promote
    def bake(self, req: dict, wk) -> bool:
        name, rid = req["map"], req["id"]
        rname = req.get("resource") or name
        res = self.map_resource(rname)
        if not res:
            self.set_status(name, {"id": rid, "state": "failed", "finishedAt": now(),
                                   "message": f"map resource {rname} not found under {self.resources}"})
            return False
        anchor = bake_mod.normalize_anchor(req["anchor"])
        self.set_status(name, {"id": rid, "state": "baking", "message": "building the new placement",
                               "startedAt": now(), "anchor": anchor})
        os.makedirs(self.staged, exist_ok=True)
        tmp = os.path.join(self.staged, f".{name}.tmp-{os.getpid()}-{now()}")
        lines = []
        t0 = time.time()
        try:
            if wk is None:
                raise BakerError("WolvenKit is not available to this baker (--wolvenkit / WOLVENKIT_CLI)")
            with file_lock(self.lock, timeout=BAKE_LOCK_SECONDS):
                r = bake_mod.bake(res, anchor, wk, log=lines.append, out_dir=tmp, bake_id=rid)
                cur = self.request(name)
                if self.manager() and (not cur or cur.get("id") != rid or cur.get("action", "bake") != "bake"):
                    shutil.rmtree(tmp, ignore_errors=True)
                    self.set_status(name, {"id": rid, "state": "cancelled", "finishedAt": now(),
                                           "message": "the move was withdrawn while it was being built"})
                    return False
                b = r["baked"]
                build = {"id": rid, "map": name, "resource": rname, "resourceDir": os.path.relpath(res, self.dir),
                         "anchor": b["anchor"], "files": r["files"], "remove": r["remove"],
                         "managerDef": f"manager/{name}.lua",
                         "package": {"file": b["package"], "bytes": b["bytes"], "sha256": b["sha256"]},
                         "requestedBy": req.get("requestedBy"), "requestedAt": req.get("requestedAt"),
                         "builtAt": now(), "baker": VERSION}
                write_json(os.path.join(tmp, "build.json"), build)
                final = os.path.join(self.staged, name)
                if os.path.isdir(final):
                    shutil.rmtree(final)          # an older staged build of this map: superseded
                os.replace(tmp, final)
            secs = round(time.time() - t0, 1)
            self.set_status(name, {"id": rid, "state": "done", "message": f"built in {secs} s", "finishedAt": now(),
                                   "seconds": secs, "anchor": build["anchor"], "package": build["package"]})
            return True
        except Exception as e:  # noqa: BLE001 -- every failure becomes an answer the manager shows
            shutil.rmtree(tmp, ignore_errors=True)
            text = str(e).strip().splitlines()
            tail = "\n".join((lines + traceback.format_exc().splitlines())[-30:])
            self.set_status(name, {"id": rid, "state": "failed", "finishedAt": now(), "log": tail,
                                   "message": (text[0][:300] if text else type(e).__name__)})
            return False

    def unstage(self, req: dict) -> dict:
        name, rid = req["map"], req["id"]
        with file_lock(self.lock, timeout=BAKE_LOCK_SECONDS):
            path, b = self.staged_build(name)
            dropped = bool(path and b.get("id") == rid)
            if dropped:
                shutil.rmtree(path)
        return self.set_status(name, {"id": rid, "state": "unstaged", "finishedAt": now(),
                                      "message": "the new build was thrown away: the map stays where it is"
                                      if dropped else "nothing was staged for that move: the map stays where it is"})

    def promote(self, only: str | None = None, reason: str = "before the server start") -> list:
        """Install every staged build the manager still asks for (all of them without a manager)."""
        done = []
        if not os.path.isdir(self.staged):
            return done
        with file_lock(self.lock, timeout=PROMOTE_LOCK_SECONDS):
            manager = self.manager()
            for entry in sorted(os.listdir(self.staged)):
                path = os.path.join(self.staged, entry)
                if entry.startswith("."):                    # a bake in progress, or a crashed one
                    with contextlib.suppress(OSError):
                        if time.time() - os.path.getmtime(path) > STALE_TMP_SECONDS:
                            shutil.rmtree(path, ignore_errors=True)
                    continue
                if only and entry != only:
                    continue
                b = read_json(os.path.join(path, "build.json"))
                if not isinstance(b, dict) or b.get("map") != entry:
                    self.log(f"[{entry}] staged folder without a valid build.json: removed")
                    shutil.rmtree(path, ignore_errors=True)
                    continue
                name, bid = entry, b.get("id")
                if manager:
                    req = self.request(name)
                    if req and req.get("id") == bid and req.get("action") == "unstage":
                        shutil.rmtree(path, ignore_errors=True)
                        self.set_status(name, {"id": bid, "state": "unstaged", "finishedAt": now(),
                                               "message": "the new build was thrown away: the map stays where it is"})
                        continue
                    if not req or req.get("id") != bid or req.get("action", "bake") != "bake":
                        self.log(f"[{name}] staged build {bid} dropped: the manager no longer asks for it")
                        shutil.rmtree(path, ignore_errors=True)
                        continue
                res = self.map_resource(b.get("resource") or name)
                if not res:
                    self.log(f"[{name}] map resource {b.get('resource') or name} not found: build {bid} stays staged")
                    continue
                try:
                    done.append(self._install(name, path, b, res, manager))
                except Exception as e:  # noqa: BLE001 -- keep going with the other maps
                    self.log(f"[{name}] could not install build {bid}: {e}")
                    continue
                self.set_status(name, {"id": bid, "state": "promoted", "promotedAt": now(), "anchor": b.get("anchor"),
                                       "message": f"installed {reason}"})
        return done

    def _install(self, name, path, b, res, manager) -> dict:
        st = stamp()
        bdir = os.path.join(self.backups, name, st)
        files = [_safe_rel(r) for r in b.get("files") or []]
        remove = [_safe_rel(r) for r in b.get("remove") or []]
        for rel in files:
            if not os.path.isfile(os.path.join(path, rel)):
                raise BakerError(f"the staged build has no {rel}")
        # what is replaced or removed goes to the backup first
        for rel in files + remove:
            live = os.path.join(res, rel)
            if os.path.isfile(live):
                os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
                shutil.copy2(live, os.path.join(bdir, rel))
        for rel in files:
            copy_atomic(os.path.join(path, rel), os.path.join(res, rel))
        for rel in remove:
            live = os.path.join(res, rel)
            if os.path.isfile(live):
                os.remove(live)
        mdef = None
        src_def = os.path.join(path, _safe_rel(b.get("managerDef") or f"manager/{name}.lua"))
        if manager and os.path.isfile(src_def):
            mdef = os.path.join(manager, "shared", "maps", f"{name}.lua")
            if os.path.isfile(mdef):
                os.makedirs(os.path.join(bdir, "_manager"), exist_ok=True)
                shutil.copy2(mdef, os.path.join(bdir, "_manager", f"{name}.lua"))
            copy_atomic(src_def, mdef)
        os.makedirs(self.applied, exist_ok=True)
        os.replace(path, os.path.join(self.applied, f"{name}-{st}"))
        _prune(self.applied, f"{name}-", KEEP_APPLIED)
        _prune(os.path.join(self.backups, name), "", KEEP_BACKUPS)
        self.log(f"[{name}] installed build {b.get('id')} at {b.get('anchor')} into {os.path.relpath(res, self.dir)}"
                 f" (backup {os.path.relpath(bdir, self.dir)})")
        return {"map": name, "id": b.get("id"), "anchor": b.get("anchor"), "resource": res, "backup": bdir,
                "managerDef": mdef, "files": files, "removed": remove}

    # ---------------------------------------------------------------- one pass over the requests
    def handle_requests(self, wk_factory) -> list:
        acted = []
        for req in self.requests():
            name, rid, action = req["map"], req["id"], req.get("action", "bake")
            st = self.status(name)
            same = isinstance(st, dict) and st.get("id") == rid
            state = st.get("state") if same else None
            if action == "unstage":
                if state != "unstaged":
                    self.unstage(req)
                    acted.append((name, "unstage"))
                continue
            if state in ("done", "promoted"):
                path, b = self.staged_build(name)
                if req.get("promoteNow") and state == "done" and b and b.get("id") == rid:
                    self.promote(only=name, reason="just before the restart")
                    acted.append((name, "promote"))
                continue
            if state in ("failed", "cancelled", "unstaged"):
                continue
            # no answer yet, or "queued"/"baking" left by a baker that stopped mid-way: bake it
            self.bake(req, wk_factory())
            acted.append((name, "bake"))
        return acted

    def summary(self) -> dict:
        names = set()
        d = self.bake_dir()
        for sub in ("requests", "status"):
            if d and os.path.isdir(os.path.join(d, sub)):
                names |= {f[:-5] for f in os.listdir(os.path.join(d, sub)) if f.endswith(".json")}
        if os.path.isdir(self.staged):
            names |= {e for e in os.listdir(self.staged) if not e.startswith(".")}
        maps = {}
        for name in sorted(n for n in names if NAME_RE.match(n)):
            path, b = self.staged_build(name)
            maps[name] = {"request": self.request(name), "status": self.status(name),
                          "staged": {k: b.get(k) for k in ("id", "anchor", "builtAt", "package")} if b else None,
                          "resource": self.map_resource(name)}
        return {"baker": VERSION, "serverDir": self.dir, "manager": self.manager(), "hook": self.hook(),
                "info": read_json(os.path.join(d, "baker.json")) if d else None, "maps": maps}


# -------------------------------------------------------------------- service loop
def run(srv: Server, wk_factory, wolvenkit_path=None, interval: float = 1.0, once: bool = False, stop=None):
    """Answer the manager's requests until stopped.  One process per server (daemon.lock)."""
    with file_lock(os.path.join(srv.work, "daemon.lock"), timeout=0 if not once else 30):
        manager_seen = False
        while True:
            try:
                m = srv.manager()
                if m != manager_seen:              # (re)introduce ourselves to a manager that appeared or moved
                    manager_seen = m
                    if m:
                        srv.write_info(wolvenkit_path)
                        srv.log(f"serving {os.path.relpath(m, srv.dir)} (baker {VERSION}, start hook "
                                f"{'installed' if srv.hook().get('installed') else 'MISSING'})")
                    else:
                        srv.log(f"waiting for a {MANAGER} resource under {srv.resources}")
                if m:
                    srv.handle_requests(wk_factory)
            except Exception:  # noqa: BLE001 -- a service keeps going; the next pass retries
                srv.log("pass failed:\n" + traceback.format_exc())
            if once or (stop and stop()):
                return
            time.sleep(interval)


def _wk_factory(args, log):
    cache = {}

    def factory():
        if "wk" not in cache:
            try:
                from ..cp77.wolvenkit import WolvenKit
                cache["wk"] = WolvenKit(args.wolvenkit, dotnet=args.dotnet, log=log)
                cache["path"] = cache["wk"].path
            except Exception as e:  # noqa: BLE001
                log(f"WolvenKit unavailable: {e}")
                cache["wk"] = None
        return cache["wk"]
    return factory


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fivem2open77.mlo.baker", description="the fivem2open77 map baker (server host)")
    p.add_argument("cmd", choices=("run", "once", "promote", "status", "bake", "unstage"))
    p.add_argument("map", nargs="?", help="bake / unstage: the map")
    p.add_argument("--server-dir", required=True, help="the Open77 server folder (server.jsonc, resources/)")
    p.add_argument("--wolvenkit", default=os.environ.get("WOLVENKIT_CLI"), help="WolvenKit.CLI.dll (or .exe)")
    p.add_argument("--dotnet", default=os.environ.get("DOTNET"), help="dotnet executable for a .dll")
    p.add_argument("--anchor", help="bake: x,y,z[,yaw]")
    p.add_argument("--in-place", action="store_true", help="bake: write into the map resource (no staging)")
    p.add_argument("--interval", type=float, default=1.0)
    from ..argv import glue_vectors
    a = p.parse_args(glue_vectors(argv))

    def log(msg):
        print(f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)

    try:
        srv = Server(a.server_dir, log=log)
        if a.cmd == "promote":
            # the start hook: never let a problem here keep the server from starting
            try:
                done = srv.promote()
                log(f"start hook: {len(done)} build(s) installed" if done else "start hook: nothing staged")
            except Exception:  # noqa: BLE001
                log("start hook failed (the server starts anyway):\n" + traceback.format_exc())
            return 0
        if a.cmd == "status":
            print(json.dumps(srv.summary(), indent=1, default=str))
            return 0
        factory = _wk_factory(a, log)
        if a.cmd in ("run", "once"):
            wk = factory()
            run(srv, factory, wolvenkit_path=getattr(wk, "path", None), interval=a.interval, once=a.cmd == "once")
            return 0
        if not a.map or not NAME_RE.match(a.map):
            raise BakerError(f"{a.cmd} needs a map name")
        if a.cmd == "bake" and a.in_place:
            if not a.anchor:
                raise BakerError("bake needs --anchor x,y,z[,yaw]")
            res = srv.map_resource(a.map)
            wk = factory()
            if not res or wk is None:
                raise BakerError(f"map resource {a.map} not found" if not res else "WolvenKit is not available")
            with file_lock(srv.lock, timeout=BAKE_LOCK_SECONDS):
                r = bake_mod.bake(res, bake_mod.parse_anchor(a.anchor), wk, log=log, manager_dir=srv.manager(),
                                  bake_id=bake_mod.new_bake_id(a.map, "deploy"))
            print(json.dumps({"ok": True, "baked": r["baked"], "managerDef": r["managerDef"]}, indent=1))
            return 0
        if a.cmd == "bake":
            if not a.anchor:
                raise BakerError("bake needs --anchor x,y,z[,yaw]")
            req = {"id": f"{a.map}-operator-{now()}", "action": "bake", "map": a.map, "resource": a.map,
                   "anchor": bake_mod.parse_anchor(a.anchor), "requestedBy": "operator", "requestedAt": now(),
                   "mode": "restart"}
            srv.write_request(req)          # so the start hook (and the manager) know this build is wanted
            ok = srv.bake(req, factory())
            print(json.dumps({"ok": ok, "status": srv.status(a.map), "staged": srv.staged_build(a.map)[1]}, indent=1))
            return 0 if ok else 1
        if a.cmd == "unstage":
            path, b = srv.staged_build(a.map)
            if not b:
                print(json.dumps({"ok": True, "staged": None}))
                return 0
            req = {"id": b["id"], "action": "unstage", "map": a.map, "resource": b.get("resource") or a.map,
                   "requestedBy": "operator", "requestedAt": now()}
            srv.write_request(req)
            print(json.dumps({"ok": True, "status": srv.unstage(req)}, indent=1))
            return 0
    except BakerError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
