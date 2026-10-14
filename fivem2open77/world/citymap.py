"""Night City in 3D: the game's own world-map model, for the Studio's Live maps.

The map screen of Cyberpunk 2077 draws the city from a handful of files under
base\\entities\\cameras\\3dmap: a terrain, the roads and their kerbs, the metro (NCART lines in their
colours), the water, a few landmarks, ~10,000 building volumes (the box colliders of three
collision meshes: what the map's cursor hits) and the district outlines (trigger areas of
3dmap_view.ent, the entity that also places every part).

``build`` reads them out of the player's own game with WolvenKit -- the meshes exported to glTF
(``uncook``), the entity and the collision meshes serialized to JSON -- puts every part where the
entity puts it, in Night City's world coordinates (metres, x east, y north, z up, the frame of every
position the Studio shows), and writes a compact cache: ``citymap.json`` (what is where) and
``citymap.bin`` (float32 positions, uint32 indices, uint8 colours, float32 building boxes) that
the browser reads straight into typed arrays.  The cache stays on this machine: it is made from
the installed game and is never shipped anywhere.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import uuid

import numpy as np

from ..cp77.glbread import accessor, read_glb

VERSION = 1
FOLDER = "base\\entities\\cameras\\3dmap"
REGEX = r"^base\\entities\\cameras\\3dmap\\[^\\]+\.(mesh|ent)$"
VIEW = "3dmap_view.ent"
FIXED = 131072.0                      # WorldPosition FixedPoint: 1/131072 m
WELD = 200.0                          # vertices closer than 5 mm are one
LAYER_OF = {"3dmap_terrain": "terrain", "3dmap_water": "water", "3dmap_roads": "roads",
            "3dmap_roads_borders": "borders", "3dmap_metro": "metro", "3dmap_cliffs": "cliffs"}
COLOURED = ("roads", "metro")         # their vertex colours mean something (road class, NCART line)
SKIP_MESHES = {"3dmap_triangle_soup", "mappin_geo_bulb", "box"}   # district data quads, the cursor pin, a hack
DISTRICTS = {
    "watson": "Watson", "westbrook": "Westbrook", "city_center": "City Center", "heywood": "Heywood",
    "santo_domingo": "Santo Domingo", "pacifica": "Pacifica", "dogtown": "Dogtown", "morro_rock": "Morro Rock",
    "kabuki": "Kabuki", "little_china": "Little China", "northside": "Northside",
    "arasaka_waterfront": "Arasaka Waterfront", "japan_town": "Japantown", "charter_hill": "Charter Hill",
    "north_oak": "North Oak", "corpo_plaza": "Corpo Plaza", "downtown": "Downtown", "glen": "The Glen",
    "vista_del_rey": "Vista del Rey", "wellspring": "Wellsprings", "arroyo": "Arroyo",
    "rancho_coronado": "Rancho Coronado", "coastview": "Coastview", "west_wind_estate": "West Wind Estate",
}
TOP_DISTRICTS = ("watson", "westbrook", "city_center", "heywood", "santo_domingo", "pacifica", "dogtown", "morro_rock")


class CityMapError(Exception):
    pass


# ---------------------------------------------------------------------- transforms
def quat_matrix(q) -> np.ndarray:
    """(x, y, z, w) -> 3x3 rotation."""
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def matrix_quat(m) -> tuple:
    """3x3 rotation -> (x, y, z, w)."""
    m = np.asarray(m, np.float64)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
    if m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        return (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
    if m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        return ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
    return ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)


def _num(v) -> float:
    if isinstance(v, dict):
        return v.get("Bits", 0) / FIXED
    return float(v or 0)


def _vec(v, keys=("X", "Y", "Z")) -> np.ndarray:
    return np.array([float((v or {}).get(k, 0) or 0) for k in keys], np.float64)


def _quat(o) -> tuple:
    o = o or {}
    return (float(o.get("i", 0)), float(o.get("j", 0)), float(o.get("k", 0)), float(o.get("r", 1)))


def _name(v) -> str:
    if isinstance(v, dict):
        return str(v.get("$value") or "")
    return str(v or "")


class _Handles:
    """WolvenKit JSON handles: {"HandleId": n, "Data": ...} defined once, {"HandleRefId": n} after."""

    def __init__(self, doc):
        self.by_id = {}
        stack = [doc]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if "HandleId" in o and isinstance(o.get("Data"), dict):
                    self.by_id[str(o["HandleId"])] = o["Data"]
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)

    def get(self, ref):
        if not isinstance(ref, dict):
            return None
        if isinstance(ref.get("Data"), dict):
            return ref["Data"]
        rid = ref.get("HandleRefId", ref.get("HandleId"))
        return self.by_id.get(str(rid)) if rid is not None else None


class Scene:
    """3dmap_view.ent: its components by name and their world transforms (parent bindings)."""

    def __init__(self, ent_json: dict):
        self.handles = _Handles(ent_json)
        root = ent_json.get("Data", {}).get("RootChunk", ent_json)
        self.components = {}
        for c in root.get("components") or []:
            if isinstance(c, dict) and "$type" not in c and isinstance(c.get("Data"), dict):
                c = c["Data"]
            n = _name(c.get("name"))
            if n and n not in self.components:
                self.components[n] = c
        self._memo = {}

    def parent_of(self, c):
        b = self.handles.get(c.get("parentTransform"))
        return _name(b.get("bindName")) if b else None

    def local(self, c):
        lt = c.get("localTransform") or {}
        p = lt.get("Position") or {}
        t = np.array([_num(p.get("x")), _num(p.get("y")), _num(p.get("z"))], np.float64)
        return quat_matrix(_quat(lt.get("Orientation"))), t

    def world(self, name, _depth=0):
        """(3x3 rotation, translation) of a component in world space."""
        if name in self._memo:
            return self._memo[name]
        c = self.components.get(name)
        if c is None or _depth > 32:
            return np.eye(3), np.zeros(3)
        r, t = self.local(c)
        par = self.parent_of(c)
        if par and par != name and par in self.components:
            pr, pt = self.world(par, _depth + 1)
            r, t = pr @ r, pt + pr @ t
        self._memo[name] = (r, t)
        return r, t

    def ancestors(self, name):
        out, seen = [], {name}
        c = self.components.get(name)
        while c is not None:
            par = self.parent_of(c)
            if not par or par in seen:
                break
            seen.add(par)
            out.append(par)
            c = self.components.get(par)
        return out

    def mesh_components(self):
        """[(name, mesh stem, component)] of the components that draw a mesh on the world map."""
        out = []
        for n, c in self.components.items():
            if c.get("$type") not in ("entMeshComponent", "entPhysicalMeshComponent"):
                continue
            mesh = _name((c.get("mesh") or {}).get("DepotPath"))
            if not mesh:
                continue
            out.append((n, os.path.splitext(mesh.replace("\\", "/").split("/")[-1])[0].lower(), c))
        return out

    def districts(self) -> list:
        """The map's district and sub-district outlines in world space."""
        out, seen = [], set()
        for n, c in self.components.items():
            if c.get("$type") != "gameStaticTriggerAreaComponent" or not n.endswith("_trigger"):
                continue
            key = n[:-len("_trigger")]
            o = self.handles.get(c.get("outline"))
            pts = (o or {}).get("points") or []
            if len(pts) < 3:
                continue
            r, t = self.world(n)
            w = np.array([t + r @ _vec(p) for p in pts])
            sig = tuple(np.round(w[:, :2]).astype(np.int64).ravel()[:12])
            if sig in seen:                       # ncx = morro_rock, worldmap_sub = dogtown
                continue
            seen.add(sig)
            parent = None
            for a in self.ancestors(n):
                if a.endswith("_trigger") and a != n:
                    parent = a[:-len("_trigger")]
                    break
            out.append({"id": key, "label": DISTRICTS.get(key, key.replace("_", " ").title()),
                        "top": key in TOP_DISTRICTS, "parent": parent if key not in TOP_DISTRICTS else None,
                        "height": round(float((o or {}).get("height") or 0), 1),
                        "outline": [[round(float(x), 2), round(float(y), 2), round(float(z), 2)] for x, y, z in w]})
        order = {k: i for i, k in enumerate(TOP_DISTRICTS)}
        out.sort(key=lambda d: (not d["top"], order.get(d["id"], 99), d["label"]))
        return out


# ---------------------------------------------------------------------- geometry
def glb_geometry(path: str, coloured=False):
    """A WolvenKit glTF export -> (positions (N, 3) in the mesh's own Z-up frame, triangles (M, 3),
    colours (N, 4) uint8 or None).  glTF is Y-up: (x, y, z) game = (x, -z, y) glTF."""
    js, binary = read_glb(path)
    node_of = {}
    for nd in js.get("nodes") or []:
        if "mesh" in nd:
            node_of.setdefault(nd["mesh"], nd)
    P, T, C, off = [], [], [], 0
    for mi, m in enumerate(js.get("meshes") or []):
        nd = node_of.get(mi) or {}
        nm = None
        if nd.get("matrix"):
            nm = np.array(nd["matrix"], np.float64).reshape(4, 4).T
        elif nd.get("translation") or nd.get("rotation") or nd.get("scale"):
            nm = np.eye(4)
            nm[:3, :3] = quat_matrix(nd.get("rotation") or (0, 0, 0, 1)) * np.array(nd.get("scale") or (1, 1, 1))
            nm[:3, 3] = nd.get("translation") or (0, 0, 0)
        for prim in m.get("primitives") or []:
            if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                continue
            pos = accessor(js, binary, prim["attributes"]["POSITION"]).astype(np.float64)
            if nm is not None:
                pos = pos @ nm[:3, :3].T + nm[:3, 3]
            if "indices" in prim:
                tri = accessor(js, binary, prim["indices"]).reshape(-1).astype(np.int64)
            else:
                tri = np.arange(len(pos), dtype=np.int64)
            tri = tri[:len(tri) // 3 * 3].reshape(-1, 3)
            P.append(np.stack([pos[:, 0], -pos[:, 2], pos[:, 1]], 1))
            T.append(tri + off)
            if coloured:
                col = prim["attributes"].get("COLOR_0")
                c = accessor(js, binary, col) if col is not None else np.ones((len(pos), 4))
                c = np.asarray(c, np.float64)
                if c.shape[1] == 3:
                    c = np.concatenate([c, np.ones((len(c), 1))], 1)
                if np.issubdtype(np.asarray(c).dtype, np.integer):
                    c = c / 255.0
                C.append(np.clip(np.rint(c * 255), 0, 255).astype(np.uint8))
            off += len(pos)
    if not P:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), None
    return np.concatenate(P), np.concatenate(T), (np.concatenate(C) if coloured else None)


def weld(pos, tri, col=None):
    """Merge coincident vertices (and drop the triangles that collapse)."""
    if not len(pos):
        return pos, tri, col
    key = np.rint(pos * WELD).astype(np.int64)
    if col is not None:
        key = np.concatenate([key, col.astype(np.int64)], 1)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    tri = inv[tri]
    keep = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    tri = tri[keep]
    if len(tri):                                   # the same triangle twice (chunk seams): once
        _, once = np.unique(np.sort(tri, axis=1), axis=0, return_index=True)
        tri = tri[np.sort(once)]
    return pos[first], tri, (col[first] if col is not None else None)


def building_boxes(mesh_json: dict, r=None, t=None) -> np.ndarray:
    """A 3D-map collision mesh -> (N, 10) float64 boxes [cx cy cz hx hy hz qx qy qz qw] (world)."""
    r = np.eye(3) if r is None else r
    t = np.zeros(3) if t is None else t
    handles = _Handles(mesh_json)
    root = mesh_json.get("Data", {}).get("RootChunk", mesh_json)
    out = []
    for prm in root.get("parameters") or []:
        d = handles.get(prm) or {}
        if d.get("$type") != "meshMeshParamPhysics":
            continue
        res = handles.get(d.get("physicsData")) or {}
        for body in res.get("bodies") or []:
            b = handles.get(body) or {}
            bt = b.get("localToModel") or {}
            br, btr = quat_matrix(_quat(bt.get("orientation"))), _vec(bt.get("position"))
            for sh in b.get("collisionShapes") or []:
                s = handles.get(sh) or {}
                if s.get("$type") != "physicsColliderBox":
                    continue
                st = s.get("localToBody") or {}
                sr, stt = quat_matrix(_quat(st.get("orientation"))), _vec(st.get("position"))
                he = np.abs(_vec(s.get("halfExtents")))
                if not np.all(np.isfinite(he)) or he.max() <= 0.01:
                    continue
                rot = r @ br @ sr
                cen = t + r @ (btr + br @ stt)
                out.append([*cen, *he, *matrix_quat(rot)])
    return np.array(out, np.float64).reshape(-1, 10)


# ---------------------------------------------------------------------- the cache
def cache_dir(workspace: str) -> str:
    return os.path.join(workspace, "citymap")


def read_meta(out_dir: str):
    p = os.path.join(out_dir, "citymap.json")
    try:
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if meta.get("version") != VERSION or not os.path.isfile(os.path.join(out_dir, "citymap.bin")):
        return None
    return meta


def game_stamp(game_dir: str | None) -> int:
    """Newest modification time of the game's archives (a patch changes it)."""
    best = 0
    for sub in ("content", "ep1"):
        d = os.path.join(game_dir or "", "archive", "pc", sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".archive"):
                try:
                    best = max(best, int(os.path.getmtime(os.path.join(d, fn))))
                except OSError:
                    pass
    return best


def status(out_dir: str, game_dir: str | None = None) -> dict:
    meta = read_meta(out_dir)
    if not meta:
        return {"ready": False}
    st = {"ready": True, "key": meta.get("key"), "built": meta.get("built"), "stats": meta.get("stats"),
          "bytes": os.path.getsize(os.path.join(out_dir, "citymap.bin"))}
    if game_dir and meta.get("game_stamp"):
        now = game_stamp(game_dir)
        st["stale"] = bool(now and now != meta["game_stamp"])
    return st


class _Writer:
    def __init__(self):
        self.parts, self.size = [], 0

    def add(self, arr) -> list:
        b = np.ascontiguousarray(arr).tobytes()
        off = self.size
        self.parts.append(b)
        self.size += len(b)
        pad = (-self.size) % 16
        if pad:
            self.parts.append(b"\0" * pad)
            self.size += pad
        return [off, int(np.asarray(arr).size)]


def assemble(raw_dir: str, glb_dir: str, json_dir: str, log=print) -> tuple:
    """WolvenKit's output (raw files, glTF exports, JSON) -> (meta dict, binary bytes)."""
    ent = os.path.join(json_dir, VIEW + ".json")
    if not os.path.isfile(ent):
        raise CityMapError(f"{VIEW} was not read from the game")
    with open(ent, encoding="utf-8") as f:
        scene = Scene(json.load(f))
    w = _Writer()
    layers = {}
    acc = {}
    landmarks = []
    boxes = []
    for name, stem, c in scene.mesh_components():
        if stem in SKIP_MESHES or _name(c.get("meshAppearance")).lower() == "seethrough":
            continue
        layer_mask = str(c.get("renderSceneLayerMask") or "")
        if c.get("$type") == "entPhysicalMeshComponent":
            if not stem.startswith("3dmap_coll_buildings"):
                continue
            jp = os.path.join(json_dir, stem + ".mesh.json")
            if not os.path.isfile(jp):
                log(f"citymap: {stem}.mesh was not read: its buildings are missing")
                continue
            with open(jp, encoding="utf-8") as f:
                r, t = scene.world(name)
                b = building_boxes(json.load(f), r, t)
            boxes.append(b)
            log(f"citymap: {stem}: {len(b)} buildings")
            continue
        if layer_mask and "WorldMap" not in layer_mask:
            continue
        gp = os.path.join(glb_dir, stem + ".glb")
        if not os.path.isfile(gp):
            log(f"citymap: {stem}.mesh has no export, left out")
            continue
        layer = LAYER_OF.get(stem, "landmarks")
        pos, tri, col = glb_geometry(gp, coloured=layer in COLOURED)
        if not len(tri):
            continue
        r, t = scene.world(name)
        s = _vec(c.get("visualScale"), ("X", "Y", "Z")) if c.get("visualScale") else np.ones(3)
        s = np.where(np.abs(s) < 1e-6, 1.0, s)
        pos = (pos * s) @ r.T + t
        a = acc.setdefault(layer, {"pos": [], "tri": [], "col": [], "n": 0})
        a["tri"].append(tri + a["n"])
        a["pos"].append(pos)
        if col is not None:
            a["col"].append(col)
        a["n"] += len(pos)
        if layer == "landmarks":
            lo, hi = pos.min(0), pos.max(0)
            landmarks.append({"id": name, "center": [round(float(v), 1) for v in (lo + hi) / 2],
                              "top": round(float(hi[2]), 1)})
    stats = {}
    bounds = None
    for layer in ("terrain", "water", "cliffs", "roads", "borders", "metro", "landmarks"):
        a = acc.get(layer)
        if not a:
            continue
        pos = np.concatenate(a["pos"])
        tri = np.concatenate(a["tri"])
        col = np.concatenate(a["col"]) if a["col"] else None
        pos, tri, col = weld(pos, tri, col)
        entry = {"vertices": int(len(pos)), "triangles": int(len(tri)),
                 "min": [round(float(v), 2) for v in pos.min(0)], "max": [round(float(v), 2) for v in pos.max(0)],
                 "pos": w.add(pos.astype(np.float32)), "idx": w.add(tri.astype(np.uint32))}
        if col is not None:
            entry["col"] = w.add(col.astype(np.uint8))
        layers[layer] = entry
        stats[layer] = int(len(tri))
        if layer != "water":
            lo, hi = pos.min(0), pos.max(0)
            bounds = (lo, hi) if bounds is None else (np.minimum(bounds[0], lo), np.maximum(bounds[1], hi))
    if "terrain" not in layers:
        raise CityMapError("the map's terrain (3dmap_terrain.mesh) was not read from the game")
    meta = {"version": VERSION, "frame": "Night City world: metres, x east, y north, z up",
            "layers": layers, "landmarks": landmarks, "districts": scene.districts()}
    if boxes:
        b = np.concatenate(boxes)
        ext = np.array([np.abs(quat_matrix(q)) @ he for q, he in zip(b[:, 6:10], b[:, 3:6])]).reshape(-1, 3)
        meta["buildings"] = {"count": int(len(b)), "stride": 10, "layout": "cx cy cz hx hy hz qx qy qz qw",
                             "data": w.add(b.astype(np.float32)),
                             "min": [round(float(v), 1) for v in (b[:, :3] - ext).min(0)],
                             "max": [round(float(v), 1) for v in (b[:, :3] + ext).max(0)]}
        stats["buildings"] = int(len(b))
    if boxes:
        lo, hi = np.array(meta["buildings"]["min"]), np.array(meta["buildings"]["max"])
        city = (lo, hi)
    else:
        city = bounds
    meta["city"] = {"min": [round(float(v), 1) for v in city[0]], "max": [round(float(v), 1) for v in city[1]]}
    meta["stats"] = stats
    meta["bytes"] = w.size
    return meta, b"".join(w.parts)


def build(wk, game_dir: str, out_dir: str, scratch: str, log=print, progress=None) -> dict:
    """Read the 3D map out of the game (WolvenKit) and write the cache.  -> its status."""
    if not game_dir or not os.path.isdir(os.path.join(game_dir, "archive", "pc")):
        raise CityMapError("Cyberpunk 2077 was not found: set its folder in Settings > Tools")
    pc = os.path.join(game_dir, "archive", "pc")
    archives = [os.path.join(pc, d) for d in ("content", "ep1") if os.path.isdir(os.path.join(pc, d))]
    if not archives:
        raise CityMapError(f"no game archives under {pc}")
    work = os.path.join(scratch, "citymap-" + uuid.uuid4().hex[:8])
    raw, glb, src, js = (os.path.join(work, x) for x in ("raw", "glb", "src", "json"))
    for d in (raw, glb, src, js):
        os.makedirs(d, exist_ok=True)
    step = progress or (lambda f, m: None)
    try:
        step(0.05, "exporting the 3D map from the game")
        # -o gets the files as they are in the archives, -or the exports (glTF for meshes)
        out = wk.run(["uncook", *archives, "-o", raw, "-or", glb, "-gp", game_dir, "-r", REGEX,
                      "--mesh-export-type", "MeshOnly", "--mesh-export-lod-filter"], timeout=3600, check=False)
        folder = FOLDER.replace("\\", os.sep)
        rdir, gdir = os.path.join(raw, folder), os.path.join(glb, folder)
        if not os.path.isfile(os.path.join(rdir, VIEW)):
            tail = "\n".join(out.splitlines()[-12:])
            raise CityMapError(f"the game's 3D map ({FOLDER}\\{VIEW}) was not found in {', '.join(archives)}:\n{tail}")
        n_glb = len([f for f in os.listdir(gdir)]) if os.path.isdir(gdir) else 0
        log(f"citymap: {len(os.listdir(rdir))} files read, {n_glb} exported")
        step(0.55, "reading the districts and buildings")
        for fn in os.listdir(rdir):
            if fn == VIEW or re.match(r"^3dmap_coll_buildings\d*\.mesh$", fn):
                shutil.copyfile(os.path.join(rdir, fn), os.path.join(src, fn))
        wk.serialize(src, js)
        step(0.75, "building the city")
        meta, data = assemble(rdir, gdir, js, log=log)
        meta["built"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta["game_stamp"] = game_stamp(game_dir)
        meta["key"] = f"{int(time.time())}"
        step(0.95, "saving")
        os.makedirs(out_dir, exist_ok=True)
        tmp_bin = os.path.join(out_dir, "citymap.bin.tmp")
        tmp_json = os.path.join(out_dir, "citymap.json.tmp")
        with open(tmp_bin, "wb") as f:
            f.write(data)
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"))
        os.replace(tmp_bin, os.path.join(out_dir, "citymap.bin"))
        os.replace(tmp_json, os.path.join(out_dir, "citymap.json"))
        log(f"citymap: ready ({', '.join(f'{k} {v}' for k, v in meta['stats'].items())}; {len(meta['districts'])} districts)")
        return status(out_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
