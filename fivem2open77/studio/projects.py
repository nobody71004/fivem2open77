"""Projects: one FiveM resource being converted, with its profile, previews and builds.

    <workspace>/projects/<id>/
        project.json        name, source, builds
        profile.json        conversion profile (see fivem2open77/profile.py)
        input/<file|dir>    copy of the FiveM resource
        assets/             files the profile points at (door chime...)
        cache/              inspect-<hash>.json, preview-<hash>.glb, render-<hash>-<view>.png, thumb.png
        builds/<n>/out/     <car>/ and <car>_chime/ Open77 resources of build n
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
        return self.path("input", self.data["source"]["file"])

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
        return {"id": self.id, "name": d.get("name"), "created": d.get("created"), "updated": d.get("updated"),
                "source": d.get("source", {}).get("original"), "models": d.get("models"),
                "car": d.get("car"), "builds": len(d.get("builds", [])),
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

    def create(self, source: str, name: str | None = None, original: str | None = None, move=False) -> Project:
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
        data = {"id": pid, "name": name or os.path.splitext(base)[0], "created": time.time(), "updated": time.time(),
                "source": {"file": base, "original": original or source, "hash": _file_hash(dst)},
                "builds": [], "build_counter": 0, "deployments": []}
        with open(os.path.join(d, "project.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        profile_mod.save({}, os.path.join(d, "profile.json"))
        return self.get(pid)

    def delete(self, pid: str):
        p = self.get(pid)
        with self.lock:
            self._cache.pop(pid, None)
        shutil.rmtree(p.dir, ignore_errors=True)


def copy_profile(p: dict) -> dict:
    return copy.deepcopy(p)
