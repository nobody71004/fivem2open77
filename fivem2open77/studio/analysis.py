"""Fast looks at a project without a full build: the FiveM resource stays extracted in memory
while a project is open, and the car model (IR) is rebuilt only when the profile changes.
Inspect data, preview models and renders are cached on disk per profile and converter version
(a new Studio must not show what the old one worked out)."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import threading

from ..gta.resource import scan_resource
from ..inspect import summarize
from ..pipeline import VERSION, build_ir
from ..preview import export_glb
from ..render import VIEWS, _triangles, render as render_view
from ..imaging import png_bytes


def _key(prof: dict, source_hash: str) -> str:
    return hashlib.sha256(json.dumps({"p": prof, "s": source_hash, "v": VERSION}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


class Session:
    def __init__(self, project):
        self.project = project
        self.res = None
        self.lock = threading.RLock()
        self.ir = collections.OrderedDict()       # key -> (src, vehicle)
        self.tris = {}                            # (key, mode) -> (tris, cols)

    def scanned(self):
        with self.lock:
            if self.res is None:
                self.res = scan_resource(self.project.source)
                models = [s.model for s in self.res.vehicles]
                if self.project.data.get("models") != models:
                    self.project.data["models"] = models
                    self.project.save()
            return self.res

    def close(self):
        with self.lock:
            if self.res is not None:
                self.res.cleanup()
                self.res = None
            self.ir.clear()
            self.tris.clear()

    def key(self, prof):
        return _key(prof, self.project.data["source"].get("hash", ""))

    def vehicle(self, prof):
        k = self.key(prof)
        with self.lock:
            if k in self.ir:
                self.ir.move_to_end(k)
                return self.ir[k]
            res = self.scanned()
            if not res.vehicles:
                raise ValueError("no vehicles found in this resource (vehicles.meta + .yft)")
            src = res.vehicles[0]
            if prof.get("model"):
                match = [s for s in res.vehicles if s.model.lower() == prof["model"].lower()]
                if not match:
                    raise ValueError(f"model '{prof['model']}' is not in this resource ({', '.join(s.model for s in res.vehicles)})")
                src = match[0]
            v = build_ir(src, prof, log=lambda *_: None)
            self.ir[k] = (src, v)
            while len(self.ir) > 2:
                old, _ = self.ir.popitem(last=False)
                for tk in [t for t in self.tris if t[0] == old]:
                    del self.tris[tk]
            return src, v

    def inspect(self, prof) -> dict:
        k = self.key(prof)
        path = self.project.path("cache", f"inspect-{k}.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        with self.lock:
            src, v = self.vehicle(prof)
            data = summarize(src, v, prof)
            data["models"] = [s.model for s in self.scanned().vehicles]
            data["resource_warnings"] = self.scanned().warnings_for(src)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
            car = {"name": data["name"], "display_name": data["display_name"], "class": data["class"],
                   "record": f"Vehicle.{data['name']}"}
            changed = False
            if self.project.data.get("car") != car:
                self.project.data["car"] = car
                changed = True
            # the project is named after the car until the user renames it
            if not self.project.data.get("custom_name") and self.project.data.get("name") != data["display_name"]:
                self.project.data["name"] = data["display_name"]
                changed = True
            if changed:
                self.project.save()
            return data

    def preview(self, prof) -> str:
        k = self.key(prof)
        path = self.project.path("cache", f"preview-{k}.glb")
        if os.path.isfile(path):
            return path
        with self.lock:
            _, v = self.vehicle(prof)
            export_glb(v, path + ".tmp")
            os.replace(path + ".tmp", path)
            return path

    def _tris(self, prof, mode):
        k = self.key(prof)
        with self.lock:
            if (k, mode) not in self.tris:
                _, v = self.vehicle(prof)
                self.tris[(k, mode)] = _triangles(v, mode)
            return self.tris[(k, mode)]

    def render(self, prof, view="front_3q", mode="material", size=(640, 400)) -> bytes:
        if view not in VIEWS:
            raise ValueError(f"view: one of {', '.join(VIEWS)}")
        if mode not in ("material", "parts"):
            raise ValueError("mode: material or parts")
        w, h = max(64, min(2048, int(size[0]))), max(64, min(2048, int(size[1])))
        k = self.key(prof)
        path = self.project.path("cache", f"render-{k}-{mode}-{view}-{w}x{h}.png")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
        tris, cols = self._tris(prof, mode)
        png = png_bytes(render_view(tris, cols, view, (w, h)))
        with open(path, "wb") as f:
            f.write(png)
        return png

    def sheet(self, prof, mode="material", size=(480, 300)) -> bytes:
        import numpy as np
        k = self.key(prof)
        path = self.project.path("cache", f"sheet-{k}-{mode}.png")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
        tris, cols = self._tris(prof, mode)
        ims = [render_view(tris, cols, v, size) for v in ("front_3q", "rear_3q", "left", "top")]
        sheet = np.concatenate([np.concatenate(ims[:2], 1), np.concatenate(ims[2:], 1)], 0)
        png = png_bytes(sheet)
        with open(path, "wb") as f:
            f.write(png)
        return png

    def neon_sheet(self, prof, color=None, size=(480, 300)) -> bytes:
        """The kit from below and from the sides, the tubes in `color` (a kit colour key), and the
        road it lights at night (from above and from the front corner)."""
        import numpy as np
        from ..render import glow_views, light_markers, neon_triangles
        k = self.key(prof)
        with self.lock:
            _, v = self.vehicle(prof)
            kit = getattr(v, "neon", None)
            if kit is None:
                raise ValueError("this car has no neon kit (see the warnings, or turn the kit on)")
            c = kit.color(color or "") or kit.color(kit.default) or kit.colors[0]
            if (k, "noneon") not in self.tris:
                self.tris[(k, "noneon")] = _triangles(v, "material", skip_kinds=("neon",))
            body = self.tris[(k, "noneon")]
            tube = neon_triangles(v, c[2])
            if not len(tube[0]):                 # light only (tubes hidden): where the light comes from
                tube = light_markers(v, c[2])
        path = self.project.path("cache", f"neon-{k}-{c[0]}.png")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
        tris = np.concatenate([body[0], tube[0]]) if len(tube[0]) else body[0]
        cols = np.concatenate([body[1], tube[1]]) if len(tube[0]) else body[1]
        ims = [render_view(tris, cols, vw, size) for vw in ("bottom", "under_3q", "left", "under_rear_3q")]
        ims += glow_views(v, c[2], size, body=body)
        sheet = np.concatenate([np.concatenate(ims[i:i + 2], 1) for i in range(0, len(ims), 2)], 0)
        png = png_bytes(sheet)
        with open(path, "wb") as f:
            f.write(png)
        return png

    def thumbnail(self, prof) -> bytes:
        png = self.render(prof, "front_3q", "material", (480, 300))
        with open(self.project.path("cache", "thumb.png"), "wb") as f:
            f.write(png)
        return png


class Analyzer:
    def __init__(self, keep=3):
        self.keep = keep
        self.sessions = collections.OrderedDict()
        self.lock = threading.Lock()

    def session(self, project) -> Session:
        with self.lock:
            s = self.sessions.get(project.id)
            if s is None:
                s = self.sessions[project.id] = Session(project)
            self.sessions.move_to_end(project.id)
            while len(self.sessions) > self.keep:
                _, old = self.sessions.popitem(last=False)
                old.close()
            return s

    def forget(self, pid):
        with self.lock:
            s = self.sessions.pop(pid, None)
        if s:
            s.close()

    def close(self):
        with self.lock:
            items = list(self.sessions.values())
            self.sessions.clear()
        for s in items:
            s.close()
