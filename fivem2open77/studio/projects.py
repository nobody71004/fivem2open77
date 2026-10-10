"""Projects: one FiveM resource being converted, with its settings, previews and builds.

    <workspace>/projects/<id>/
        project.json        kind (car | map | world), name, source, builds, deployments
        profile.json        a car's conversion profile (see fivem2open77/profile.py)
        map.json            a map's conversion options (see studio/maps.py)
        world.json          a world edit: its resource name, the area it is about, the edit list and
                            the undo history (see studio/worldops.py; no input/ folder)
        input/<file|dir>    copy of the FiveM resource
        assets/             files the profile points at (door chime...)
        cache/              inspect-<hash>.json, preview-<hash>.glb, render-<hash>-<view>.png, thumb.png
                            (maps: look-<key>.json, <key>.glb, <key>-<mode>-<view>.png)
        builds/<n>/out/     car: <car>/ and <car>_chime/; map: <map>/ and f2o77_maps/ of build n
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

from .. import profile as profile_mod
from ..cp77 import sound as sound_mod
from ..cp77.donor import DONORS

_ID = re.compile(r"^[a-f0-9]{12}$")


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    if os.path.isdir(path):
        for dp, dn, fns in sorted(os.walk(path)):
            dn.sort()
            for fn in sorted(fns):
                fp = os.path.join(dp, fn)
                h.update(os.path.relpath(fp, path).encode())
                h.update(str(os.path.getsize(fp)).encode())
                h.update(str(int(os.path.getmtime(fp))).encode())
        return h.hexdigest()[:16]
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


class ProjectError(ValueError):
    pass


KINDS = ("car", "map", "world")


class Project:
    def __init__(self, root: str):
        self.dir = root
        self.id = os.path.basename(root)
        self.lock = threading.RLock()
        with open(os.path.join(root, "project.json"), "r", encoding="utf-8") as f:
            self.data = json.load(f)

    # ---- paths
    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    @property
    def source(self) -> str:
        if not self.data.get("source"):
            raise ProjectError("a world edit has no FiveM source")
        return self.path("input", self.data["source"]["file"])

    @property
    def kind(self) -> str:
        return self.data.get("kind") or "car"

    def need(self, kind: str):
        if self.kind != kind:
            raise ProjectError(f"this is a {self.kind} project; that works on {kind} projects")
        return self

    def save(self):
        with self.lock:
            tmp = self.path("project.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path("project.json"))

    # ---- profile
    def raw_profile(self) -> dict:
        try:
            with open(self.path("profile.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def profile(self) -> dict:
        """Normalized profile, chime path made absolute."""
        p = profile_mod.normalize(self.raw_profile(), donors=DONORS, sounds=sound_mod.CATALOGUE)
        f = p["chime"].get("file")
        if f and not os.path.isabs(f):
            p["chime"]["file"] = self.path(f)
        return p

    def set_profile(self, new: dict) -> dict:
        norm = profile_mod.normalize(new, donors=DONORS, sounds=sound_mod.CATALOGUE)   # raises ProfileError
        models = self.data.get("models") or []
        if norm.get("model") and models and norm["model"].lower() not in [m.lower() for m in models]:
            raise profile_mod.ProfileError([f"model: '{norm['model']}' is not a car in this resource "
                                            f"(it has: {', '.join(models)}); the shown name is display_name"])
        with self.lock:
            profile_mod.save(new, self.path("profile.json"))
            self.data["updated"] = time.time()
            self.save()
        return norm

    def patch_profile(self, patch: dict) -> dict:
        return self.set_profile(profile_mod.merge(self.raw_profile(), patch))

    def profile_hash(self) -> str:
        return _hash({"profile": self.profile(), "source": self.data["source"].get("hash")})

    # ---- map options
    def raw_map_options(self) -> dict:
        try:
            with open(self.path("map.json"), "r", encoding="utf-8") as f:
                v = json.load(f)
                return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    def map_options(self) -> dict:
        from . import maps as maps_mod
        return maps_mod.normalize_options(self.raw_map_options())

    def set_map_options(self, new: dict) -> dict:
        from . import maps as maps_mod
        norm = maps_mod.normalize_options(new)            # raises MapOptionsError
        with self.lock:
            tmp = self.path("map.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in (new or {}).items()}, f, indent=2)
            os.replace(tmp, self.path("map.json"))
            self.data["updated"] = time.time()
            self.save()
        return norm

    def patch_map_options(self, patch: dict) -> dict:
        from . import maps as maps_mod
        return self.set_map_options(maps_mod.merge(self.raw_map_options(), patch))

    # ---- world edit
    def world(self) -> dict:
        try:
            with open(self.path("world.json"), "r", encoding="utf-8") as f:
                v = json.load(f)
                return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_world(self, w: dict):
        with self.lock:
            tmp = self.path("world.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(w, f, indent=1)
            os.replace(tmp, self.path("world.json"))
            self.data["updated"] = time.time()
            self.save()

    # ---- builds
    def next_build(self) -> int:
        with self.lock:
            n = int(self.data.get("build_counter", 0)) + 1
            self.data["build_counter"] = n
            self.save()
            return n

    def add_build(self, info: dict):
        with self.lock:
            self.data.setdefault("builds", []).append(info)
            self.data["builds"] = self.data["builds"][-50:]
            self.data["updated"] = time.time()
            self.save()

    def build_dir(self, n: int) -> str:
        return self.path("builds", str(int(n)))

    def latest_build(self, ok_only=True) -> dict | None:
        for b in reversed(self.data.get("builds", [])):
            if not ok_only or b.get("ok"):
                return b
        return None

    def summary(self) -> dict:
        d = self.data
        thumb = self.path("cache", "thumb.png")
        return {"id": self.id, "kind": self.kind, "name": d.get("name"), "created": d.get("created"),
                "updated": d.get("updated"), "source": (d.get("source") or {}).get("original"), "models": d.get("models"),
                "car": d.get("car"), "map": d.get("map"), "world": d.get("world"), "builds": len(d.get("builds", [])),
                "last_build": self.latest_build(ok_only=False), "thumb": os.path.isfile(thumb),
                "deployments": d.get("deployments", [])[-5:]}


class ProjectStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._cache = {}
        self.lock = threading.Lock()

    def list(self) -> list:
        out = []
        for name in sorted(os.listdir(self.root)):
            if _ID.match(name) and os.path.isfile(os.path.join(self.root, name, "project.json")):
                try:
                    out.append(self.get(name).summary())
                except (OSError, ValueError, ProjectError):
                    continue
        out.sort(key=lambda p: -(p.get("updated") or p.get("created") or 0))
        return out

    def get(self, pid: str) -> Project:
        if not _ID.match(pid or ""):
            raise ProjectError("no such project")
        d = os.path.join(self.root, pid)
        if not os.path.isfile(os.path.join(d, "project.json")):
            raise ProjectError("no such project")
        with self.lock:
            p = self._cache.get(pid)
            if p is None:
                p = self._cache[pid] = Project(d)
            return p

    def create(self, source: str, name: str | None = None, original: str | None = None, move=False,
               kind: str = "car") -> Project:
        if kind not in KINDS:
            raise ProjectError("kind: car or map")
        if not os.path.exists(source):
            raise ProjectError(f"not found: {source}")
        low = source.lower()
        if os.path.isfile(source) and not low.endswith((".zip", ".rar", ".7z")):
            raise ProjectError("a FiveM resource folder, or a .zip / .rar / .7z of one")
        pid = uuid.uuid4().hex[:12]
        d = os.path.join(self.root, pid)
        os.makedirs(os.path.join(d, "input"))
        for sub in ("assets", "cache", "builds"):
            os.makedirs(os.path.join(d, sub))
        base = os.path.basename(os.path.normpath(source))
        dst = os.path.join(d, "input", base)
        if os.path.isdir(source):
            shutil.copytree(source, dst)
        elif move:
            shutil.move(source, dst)
        else:
            shutil.copyfile(source, dst)
        data = {"id": pid, "kind": kind, "name": name or os.path.splitext(base)[0], "created": time.time(),
                "updated": time.time(), "source": {"file": base, "original": original or source, "hash": _file_hash(dst)},
                "builds": [], "build_counter": 0, "deployments": []}
        if name:
            data["custom_name"] = True
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if kind == "car":
            profile_mod.save({}, os.path.join(d, "profile.json"))
        else:
            with open(os.path.join(d, "map.json"), "w", encoding="utf-8") as f:
                json.dump({}, f)
        return self.get(pid)

    def create_world(self, name: str, display: str, center=None) -> Project:
        """A world edit project: no FiveM source, an edit list (world.json)."""
        pid = uuid.uuid4().hex[:12]
        d = os.path.join(self.root, pid)
        for sub in ("cache", "builds"):
            os.makedirs(os.path.join(d, sub))
        data = {"id": pid, "kind": "world", "name": display, "created": time.time(), "updated": time.time(),
                "source": None, "builds": [], "build_counter": 0, "deployments": [],
                "world": {"name": name, "display": display, "edits": 0}}
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        w = {"name": name, "display": display, "center": center, "edits": [], "undo": [], "redo": [], "seq": 0}
        with open(os.path.join(d, "world.json"), "w", encoding="utf-8") as f:
            json.dump(w, f, indent=1)
        return self.get(pid)

    def delete(self, pid: str):
        p = self.get(pid)
        with self.lock:
            self._cache.pop(pid, None)
        shutil.rmtree(p.dir, ignore_errors=True)


def copy_profile(p: dict) -> dict:
    return copy.deepcopy(p)
