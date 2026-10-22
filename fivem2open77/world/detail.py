"""The real city around a point, for the Studio's 3D city: which of the game's meshes stand where.

Night City streams in sectors by level: level 0 cells (64 m) hold the detail -- props, walls,
pipes, signs, trees -- each level up a cell twice as wide, with bigger things and, from level 2 or so
on, the far-view stand-ins (``world*ProxyMeshNode``: a whole building or block as one light mesh,
shown until the real ones stream in).  ``plan`` reads both rings the way the game shows them:

    near (within ``near`` metres)       every real mesh: static, instanced (each instance), bended,
                                        destructible, terrain, foliage (its populations)
    far (``near`` to ``far`` metres)     the stand-ins of buildings, props, entities and terrain, and
                                        the big real meshes of level 3 and up

and groups the instances by mesh and appearance, each instance a 3x4 matrix (the game's world
space: metres, x east, y north, z up; columns x, y, z axis, translation), nearest first, within a
budget.  The meshes themselves come from geocache.py.
"""
from __future__ import annotations

import base64
import math
import re

import numpy as np

from . import sector as S

NEAR_TYPES = {"worldStaticMeshNode", "worldMeshNode", "worldBendedMeshNode", "worldInstancedMeshNode",
              "worldInstancedDestructibleMeshNode", "worldTerrainMeshNode", "worldFoliageNode", "worldRotatingMeshNode",
              "worldPhysicalDestructionNode", "worldBakedDestructionNode", "worldAdvertisementNode",
              "worldDynamicMeshNode"}
PROXY_TYPES = {"worldBuildingProxyMeshNode", "worldGenericProxyMeshNode", "worldTerrainProxyMeshNode",
               "worldEntityProxyMeshNode", "worldDestructibleEntityProxyMeshNode", "worldRoadProxyMeshNode"}
FAR_REAL_LEVEL = 3            # real meshes of this level and up are big enough for the far ring too
SKIP_RES = ("engine\\meshes\\editor\\", "base\\fx\\", "\\_proxyhelper\\collision", "\\occluder")
OCCLUDER_RX = re.compile(r"_occ\d*\.mesh$")       # (the roads' occluders: never drawn, often not even files)
DEFAULT_NEAR, DEFAULT_FAR = 200.0, 900.0
MAX_INSTANCES = 100000
MAX_MESHES = 2500


def quat_matrix(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def matrix(pos, rot, scale) -> np.ndarray:
    """3x4 (rotation x scale | translation) of one instance."""
    m = np.empty((3, 4))
    s = scale if isinstance(scale, (list, tuple, np.ndarray)) else [scale] * 3
    m[:, :3] = quat_matrix(rot) * np.asarray(s, dtype=np.float64)[None, :3]
    m[:, 3] = pos[:3]
    return m


def wanted(res: str) -> bool:
    low = (res or "").lower()
    return low.endswith(".mesh") and not any(k in low for k in SKIP_RES) and not OCCLUDER_RX.search(low)


def sectors_for(index, x: float, y: float, near: float, far: float) -> list:
    """The exterior sectors the two rings read: low levels around the near ring, every level that
    holds stand-ins or big meshes around the far one."""
    near_rows = [r for r in index.sectors_at(x, y, near, categories={"Exterior"}) if r[7] < FAR_REAL_LEVEL]
    far_rows = [r for r in index.sectors_at(x, y, far, categories={"Exterior"}) if r[7] >= 2]
    seen, out = set(), []
    for r in near_rows + far_rows:
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r[0])
    return out


# ----------------------------------------------------------------------------- one sector, as arrays
KIND_NEAR, KIND_FAR, KIND_BOTH = 0, 1, 2      # real meshes; stand-ins; big real meshes (level 3 and up)
LEVEL_REACH = 128.0                             # metres a level 0 sector's nodes stream at (twice that a level up)
ROWS_VERSION = 3                                # 2: streaming distances, stand-ins' hide distance and ids; 3: terrain


class Strings:
    """Resource and appearance names, each once, as small ints (one table for every sector; the
    Studio's request threads share it)."""

    def __init__(self):
        import threading
        self.list, self.ids = [""], {"": 0}
        self._lock = threading.Lock()

    def id(self, s: str) -> int:
        i = self.ids.get(s)
        if i is None:
            with self._lock:
                i = self.ids.get(s)
                if i is None:
                    self.list.append(s)
                    i = self.ids[s] = len(self.list) - 1
        return i

    def ids_of(self, strings) -> np.ndarray:
        return np.array([self.id(s) for s in strings], dtype=np.int32)


def rows_of(sec: dict) -> dict:
    """The drawable nodes of one sector (sector.read_sector) as arrays, each instance a row: what
    plan_rows reads, fast.  Names are local (``strings``) until ``globalize``."""
    level = int(sec.get("level") or 0)
    local, ids = [""], {"": 0}

    def sid(v):
        i = ids.get(v)
        if i is None:
            i = ids[v] = len(local)
            local.append(v)
        return i
    ok = {}
    R = {k: [] for k in ("rid", "aid", "kind", "pos", "rot", "scl", "dist", "hide", "pid", "par", "terr")}
    F = {k: [] for k in ("rid", "aid", "pos", "rot", "scl", "res", "begin", "count", "dist", "par")}
    for e in sec.get("entries") or []:
        t, res = e.get("type"), e.get("res") or ""
        w = ok.get(res)
        if w is None:
            w = ok[res] = wanted(res)
        if not w:
            continue
        if e.get("bend") and sec.get("path"):
            res = bend_name(sec["path"], e["i"], res)   # bent its own way: a mesh of its own (geocache.bend_geo)
        if t in PROXY_TYPES:
            kind = KIND_FAR
        elif t in NEAR_TYPES:
            kind = KIND_BOTH if level >= FAR_REAL_LEVEL else KIND_NEAR
        else:
            continue
        r, a = sid(res), sid(e.get("app") or "")
        if t == "worldFoliageNode":
            fo = e.get("foliage") or {}
            if fo.get("res") and int(fo.get("count") or 0) > 0:
                F["rid"].append(r)
                F["aid"].append(a)
                F["pos"].append(e["pos"][:3])
                F["rot"].append(e["rot"][:4])
                F["scl"].append(float((e.get("scale") or [1.0])[0]))
                F["res"].append(sid(fo["res"]))
                F["begin"].append(int(fo.get("begin") or 0))
                F["count"].append(int(fo.get("count") or 0))
                F["dist"].append(float(e.get("dist") or 0.0) or LEVEL_REACH * 2.0 ** level)
                F["par"].append(_hex64(e.get("par")))
            continue
        el = e.get("elements")
        items = el.get("items") if el and el.get("kind") == "instances" else None
        dist = float(e.get("dist") or 0.0)
        if dist <= 0.0:
            dist = LEVEL_REACH * 2.0 ** level         # (no streaming distance: what its level streams at)
        hide = float(e.get("hide") if e.get("hide") is not None else -1.0)
        pid, par = _hex64(e.get("pid")), _hex64(e.get("par"))
        terr = t == "worldTerrainProxyMeshNode"
        for it in (items if items is not None else [e]):
            R["rid"].append(r)
            R["aid"].append(a)
            R["kind"].append(kind)
            R["pos"].append(it["pos"][:3])
            R["rot"].append(it["rot"][:4])
            R["scl"].append((it.get("scale") or [1.0, 1.0, 1.0])[:3])
            R["dist"].append(dist)
            R["hide"].append(hide)
            R["pid"].append(pid)
            R["par"].append(par)
            R["terr"].append(terr)
    out = {"strings": local, "level": level}
    for k, dt, w in (("rid", np.int32, 0), ("aid", np.int32, 0), ("kind", np.uint8, 0), ("pos", np.float64, 3),
                     ("rot", np.float64, 4), ("scl", np.float32, 3), ("dist", np.float32, 0), ("hide", np.float32, 0),
                     ("pid", np.uint64, 0), ("par", np.uint64, 0), ("terr", np.bool_, 0)):
        out[k] = np.asarray(R[k], dtype=dt).reshape((-1, w) if w else (-1,))
    for k, dt, w in (("rid", np.int32, 0), ("aid", np.int32, 0), ("pos", np.float64, 3), ("rot", np.float64, 4),
                     ("scl", np.float32, 0), ("res", np.int32, 0), ("begin", np.int32, 0), ("count", np.int32, 0),
                     ("dist", np.float32, 0), ("par", np.uint64, 0)):
        out["f" + k] = np.asarray(F[k], dtype=dt).reshape((-1, w) if w else (-1,))
    return out


BEND = "#bend:"


def bend_name(sector: str, i: int, res: str) -> str:
    """The name of one bent instance of a mesh (a road, a rail, a cable): its sector, its node
    instance and the mesh it bends."""
    return f"{BEND}{sector}:{int(i)}:{res}"


def bend_of(name: str):
    """bend_name's (sector, instance, mesh), else None."""
    if not isinstance(name, str) or not name.startswith(BEND):
        return None
    try:
        sector, i, res = name[len(BEND):].split(":", 2)
        return sector, int(i), res
    except ValueError:
        return None


def _hex64(v) -> int:
    try:
        return int(v, 16) if v else 0
    except (TypeError, ValueError):
        return 0


def globalize(rows: dict, strings: Strings) -> dict:
    """rows_of's local names -> the shared table's ids."""
    m = strings.ids_of(rows["strings"])
    out = dict(rows)
    for k in ("rid", "aid", "frid", "faid", "fres"):
        out[k] = m[rows[k]] if len(rows[k]) else rows[k]
    out["strings"] = None
    return out


def save_rows(fp: str, rows: dict, tag=0) -> None:
    """rows_of's arrays to a file (``tag``: what the sector they were read from was read from --
    its version and the game's)."""
    import os
    tmp = fp + ".tmp.npz"
    arrays = {k: v for k, v in rows.items() if isinstance(v, np.ndarray)}
    np.savez(tmp, v=np.array([ROWS_VERSION]), tag=np.frombuffer(str(tag).encode("utf-8") or b" ", np.uint8),
             level=np.array([rows["level"]]), strings=np.frombuffer("\n".join(rows["strings"]).encode("utf-8"), np.uint8),
             **arrays)
    os.replace(tmp, fp)


def load_rows(fp: str, tag=0) -> dict | None:
    try:
        with np.load(fp, allow_pickle=False) as z:
            if int(z["v"][0]) != ROWS_VERSION or "tag" not in z.files or bytes(z["tag"]).decode("utf-8") != (str(tag) or " "):
                return None
            out = {k: z[k] for k in z.files if k not in ("v", "tag", "level", "strings")}
            out["level"] = int(z["level"][0])
            out["strings"] = bytes(z["strings"]).decode("utf-8").split("\n")
            return out
    except (OSError, ValueError, KeyError):
        return None


# ----------------------------------------------------------------------------- the plan
MIN_Z = -90.0                 # deeper than this: the game's hidden rooms (the VR tutorial is 130-290 m down)
SIZE_K = 110.0                # a mesh is seen while nearer than this many times its size (its radius)
MIN_SHOW = 6.0                # ...and anything within this many metres


def quats_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton products a * b of (n, 4) quaternions [i, j, k, r]."""
    ax, ay, az, aw = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    bx, by, bz, bw = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=1)


def quats_matrix(q: np.ndarray) -> np.ndarray:
    """(n, 4) quaternions -> (n, 3, 3) rotations."""
    n = np.linalg.norm(q, axis=1, keepdims=True)
    q = q / np.where(n > 0, n, 1.0)
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    m = np.empty((len(q), 3, 3))
    m[:, 0, 0] = 1 - 2 * (y * y + z * z)
    m[:, 0, 1] = 2 * (x * y - z * w)
    m[:, 0, 2] = 2 * (x * z + y * w)
    m[:, 1, 0] = 2 * (x * y + z * w)
    m[:, 1, 1] = 1 - 2 * (x * x + z * z)
    m[:, 1, 2] = 2 * (y * z - x * w)
    m[:, 2, 0] = 2 * (x * z - y * w)
    m[:, 2, 1] = 2 * (y * z + x * w)
    m[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def _foliage_rows(rows: list, x, y, near, foliage, strings: Strings, margin=150.0) -> tuple:
    """The populations of the foliage nodes standing within the near ring (+ a population's reach)."""
    out = []
    r2 = (near + margin) ** 2
    for rw in rows:
        if not len(rw["frid"]):
            continue
        fp = rw["fpos"]
        sel = np.nonzero((fp[:, 0] - x) ** 2 + (fp[:, 1] - y) ** 2 <= r2)[0]
        if not len(sel):
            continue
        by_res = {}
        for i in sel:
            by_res.setdefault(int(rw["fres"][i]), []).append(int(i))
        for res_id, nodes in by_res.items():
            pops = foliage(strings.list[res_id]) if foliage else None
            if pops is None:
                continue
            ppos, prot, pscl = pops["pos"], pops["rot"], pops["scale"]
            if not isinstance(ppos, np.ndarray):
                ppos, prot, pscl = (np.asarray(ppos, np.float64).reshape(-1, 3), np.asarray(prot, np.float64).reshape(-1, 4),
                                    np.asarray(pscl, np.float64).reshape(-1))
            npop = len(ppos)
            idx, owner = [], []
            for i in nodes:
                b = int(rw["fbegin"][i])
                e = min(b + int(rw["fcount"][i]), npop)
                if e > b:
                    idx.append(np.arange(b, e))
                    owner.append(np.full(e - b, i))
            if not idx:
                continue
            idx, owner = np.concatenate(idx), np.concatenate(owner)
            nrot = rw["frot"][owner]
            pos = rw["fpos"][owner] + np.einsum("nij,nj->ni", quats_matrix(nrot), ppos[idx])
            rot = quats_mul(nrot, prot[idx])
            s = (pscl[idx] * rw["fscl"][owner]).astype(np.float32)
            out.append((rw["frid"][owner], rw["faid"][owner], pos, rot, np.repeat(s[:, None], 3, axis=1)))
    return out


def plan_rows(rows: list, strings: Strings, x: float, y: float, near: float = DEFAULT_NEAR, far: float = DEFAULT_FAR,
              foliage=None, cam=None, k: float = SIZE_K, reach: float = 0.0, radius=None, drawable=None,
              max_instances: int = MAX_INSTANCES, max_meshes: int = MAX_MESHES, min_z: float = MIN_Z) -> dict:
    """The real city around (x, y) from sectors' rows (rows_of + globalize), each instance a 3x4
    matrix, nearest first, grouped by mesh, appearance and ring.

    ``cam`` (x, y, z): where the camera is; with ``radius(res) -> metres | None`` (a mesh's size,
    once read), instances too small to be seen from there -- farther than ``k`` times their size,
    plus ``reach`` (how far the camera may go before asking again) -- are left out.
    ``drawable(res) -> bool``: only those meshes get groups (and the budgets); the others are
    listed in ``pending`` (nearest first), for reading."""
    rows = [r for r in rows if r is not None]
    near2, far2 = near * near, far * far
    parts = []
    if rows:
        pos = np.concatenate([r["pos"] for r in rows]) if rows else np.zeros((0, 3))
        if len(pos):
            kind = np.concatenate([r["kind"] for r in rows])
            d2 = (pos[:, 0] - x) ** 2 + (pos[:, 1] - y) ** 2
            keep = (((kind == KIND_NEAR) & (d2 <= near2)) | ((kind == KIND_FAR) & (d2 > near2) & (d2 <= far2))
                    | ((kind == KIND_BOTH) & (d2 <= far2)))
            sel = np.nonzero(keep)[0]
            parts.append((np.concatenate([r["rid"] for r in rows])[sel], np.concatenate([r["aid"] for r in rows])[sel],
                          pos[sel], np.concatenate([r["rot"] for r in rows])[sel], np.concatenate([r["scl"] for r in rows])[sel]))
        for fr in _foliage_rows(rows, x, y, near, foliage, strings):
            fd2 = (fr[2][:, 0] - x) ** 2 + (fr[2][:, 1] - y) ** 2
            sel = np.nonzero(fd2 <= near2)[0]
            if len(sel):
                parts.append(tuple(a[sel] for a in fr))
    if parts:
        rid = np.concatenate([p[0] for p in parts]).astype(np.int64)
        aid = np.concatenate([p[1] for p in parts]).astype(np.int64)
        pos = np.concatenate([p[2] for p in parts])
        rot = np.concatenate([p[3] for p in parts])
        scl = np.concatenate([p[4] for p in parts]).astype(np.float64)
    else:
        rid = aid = np.zeros(0, np.int64)
        pos, rot, scl = np.zeros((0, 3)), np.zeros((0, 4)), np.zeros((0, 3))
    culled = 0
    ok = pos[:, 2] >= min_z if len(pos) else np.zeros(0, bool)
    area = near_area = None                       # (meshes read, meshes) of the whole area / the near ring, seen or not
    if drawable is not None and len(pos):
        u_all = np.unique(rid[ok])
        is_read = {int(r): bool(drawable(strings.list[int(r)])) for r in u_all}
        area = (sum(is_read.values()), len(u_all))
        u_near = np.unique(rid[ok & ((pos[:, 0] - x) ** 2 + (pos[:, 1] - y) ** 2 <= near2)])
        near_area = (sum(1 for r in u_near if is_read[int(r)]), len(u_near))
    if cam is not None and radius is not None and len(pos):
        uniq = np.unique(rid)
        rad_of = {int(u): radius(strings.list[int(u)]) for u in uniq}
        lut = np.full(int(uniq.max()) + 1, np.nan)
        for u, r in rad_of.items():
            if r is not None:
                lut[u] = float(r)
        r_inst = lut[rid] * np.abs(scl).max(axis=1)
        dc = np.sqrt(((pos - np.asarray(cam, np.float64)[None, :3]) ** 2).sum(axis=1))
        seen = np.isnan(r_inst) | (dc <= np.maximum(MIN_SHOW, (k + 2.0) * np.nan_to_num(r_inst)) + reach)   # (+ its size: its pivot is not its middle)
        culled = int((ok & ~seen).sum())
        ok &= seen
    sel = np.nonzero(ok)[0]
    rid, aid, pos, rot, scl = rid[sel], aid[sel], pos[sel], rot[sel], scl[sel]
    d2 = (pos[:, 0] - x) ** 2 + (pos[:, 1] - y) ** 2
    order = np.argsort(d2, kind="stable")
    rid, aid, pos, rot, scl, d2 = rid[order], aid[order], pos[order], rot[order], scl[order], d2[order]
    total = len(rid)
    pending = []
    # the meshes nearest first, within the budget; then the instances
    if total:
        u, first = np.unique(rid, return_index=True)
        by_first = u[np.argsort(first)]
        if drawable is not None:
            ok_mesh = np.array([bool(drawable(strings.list[int(r)])) for r in by_first], dtype=bool)
            pending = [strings.list[int(r)] for r in by_first[~ok_mesh][:max_meshes]]
            total -= int(np.isin(rid, by_first[~ok_mesh]).sum())
            by_first = by_first[ok_mesh]
        allowed = by_first[:max_meshes]
        m = np.isin(rid, allowed)
        sel = np.nonzero(m)[0][:max_instances]
        rid, aid, pos, rot, scl, d2 = rid[sel], aid[sel], pos[sel], rot[sel], scl[sel], d2[sel]
    kept = len(rid)
    ring = (d2 > near2).astype(np.int64)
    groups, meshes = [], []
    if kept:
        mats = np.empty((kept, 3, 4), np.float32)
        mats[:, :, :3] = quats_matrix(rot) * scl[:, None, :]
        mats[:, :, 3] = pos
        key = (rid * (int(aid.max()) + 1) + aid) * 2 + ring
        uk, first, inv = np.unique(key, return_index=True, return_inverse=True)
        inv = inv.reshape(-1)
        by = np.argsort(inv, kind="stable")
        counts = np.bincount(inv, minlength=len(uk))
        starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
        seen_mesh = set()
        for gi in np.argsort(first, kind="stable"):
            idx = by[starts[gi]:starts[gi] + counts[gi]]
            i0 = idx[0]
            res = strings.list[int(rid[i0])]
            groups.append({"res": res, "app": strings.list[int(aid[i0])], "ring": "far" if ring[i0] else "near",
                           "count": int(counts[gi]), "matrices": mats[idx]})
            if res.lower() not in seen_mesh:
                seen_mesh.add(res.lower())
                meshes.append(res)
    return {"groups": groups, "meshes": meshes, "pending": pending, "area": area, "near_area": near_area,
            "instances": kept, "dropped": total - kept,
            "culled": culled, "center": [x, y], "near": near, "far": far}


# ----------------------------------------------------------------------------- the game's own way
# What the game shows from where the camera is: the sectors whose streaming box holds the camera
# (each level's boxes are twice as big: from high up only the coarse levels stream), and in them
# every node instance nearer than its streaming distance -- except the ones under a far-view
# stand-in that shows: a stand-in shows from its hide distance on (nearer, what it stands for does),
# up to its streaming distance; the stand-ins themselves are under coarser ones.  ``detail`` scales
# every distance (and the boxes): more than 1 shows more than the game does.
DETAIL = 1.0
MAX_ANCESTORS = 6
COARSE_LEVEL = 6
CAM_TOP = 2500.0               # metres: higher up, the city is shown as from this high (the game streams nothing finer)


def lod_camera(cam) -> np.ndarray:
    """Where the camera is for what streams and shows: at most CAM_TOP metres up."""
    c = np.asarray(cam, np.float64)[:3].copy()
    c[2] = min(c[2], CAM_TOP)
    return c


def streaming_rows(index_rows: list, categories=("Exterior",)):
    """The world index's sector rows as arrays for sectors_streaming (keep it: it is reused)."""
    keep = [r for r in index_rows if not categories or r[8] in categories]
    box = np.array([r[1:7] for r in keep], np.float64).reshape(-1, 6)
    return {"paths": [r[0] for r in keep], "box": box, "level": np.array([int(r[7]) for r in keep], np.int16)}


def sectors_streaming(srows: dict, cam, detail: float = DETAIL, reach: float = 0.0, max_level: int = 9) -> list:
    """The sectors streamed in with the camera at ``cam`` (game metres): those whose streaming box,
    grown ``detail`` times about its middle (plus ``reach`` metres), holds it; finest level first.
    From above every box (the top of the coarsest is kilometres up) the coarsest level streams."""
    box = srows["box"]
    if not len(box):
        return []
    c = lod_camera(cam)
    mid = (box[:, :3] + box[:, 3:]) * 0.5
    half = (box[:, 3:] - box[:, :3]) * 0.5 * max(0.1, detail) + reach
    top = float((mid[:, 2] + half[:, 2]).max())
    c[2] = min(c[2], top - 1.0)                     # above everything: as from the top of the highest box
    inside = np.all(np.abs(mid - c[None, :]) <= half, axis=1) & (srows["level"] <= max_level)
    sel = np.nonzero(inside)[0]
    order = np.lexsort((np.abs(mid[sel, 0] - c[0]) + np.abs(mid[sel, 1] - c[1]), srows["level"][sel]))
    return [srows["paths"][i] for i in sel[order]]


def _world_centres(pos, rot, scl, rid, info):
    """(world centre (n, 3), world radius (n,)) of each instance's mesh from ``info(rid) -> (cx, cy,
    cz, r) | None`` (its local bounds' middle and half diagonal); the pivot and 0 when unknown."""
    centre, rad = pos.copy(), np.zeros(len(pos))
    if info is None or not len(pos):
        return centre, rad
    uniq = np.unique(rid)
    lut = np.full((int(uniq.max()) + 1, 4), np.nan)
    for u in uniq:
        got = info(int(u))
        if got is not None:
            lut[int(u)] = got
    li = lut[rid]
    known = ~np.isnan(li[:, 3])
    if known.any():
        k = np.nonzero(known)[0]
        local = li[k, :3] * scl[k].astype(np.float64)
        centre[k] = pos[k] + np.einsum("nij,nj->ni", quats_matrix(rot[k]), local)
        rad[k] = li[k, 3] * np.abs(scl[k]).max(axis=1)
    return centre, rad


def _ancestors_allow(par, ids, dc, hide, ppar, detail, ok_parent):
    """Per row with a stand-in over it (``par``): whether every stand-in above lets it show (the
    camera nearer than that one's hide distance, or that one not drawable)."""
    allow = np.ones(len(par), bool)
    if not len(ids):
        return allow
    cur = par.copy()
    for _ in range(MAX_ANCESTORS):
        has = cur != 0
        if not has.any():
            break
        j = np.searchsorted(ids, cur)
        j = np.minimum(j, len(ids) - 1)
        found = has & (ids[j] == cur)
        lets = ~found | ~ok_parent[j] | (dc[j] < np.maximum(hide[j], 0.0) * detail)
        allow &= lets
        cur = np.where(found, ppar[j], 0).astype(np.uint64)
    return allow


def plan_lod(rows: list, strings: Strings, cam, detail: float = DETAIL, foliage=None, info=None, drawable=None,
             failed=(), max_instances: int = MAX_INSTANCES, max_meshes: int = MAX_MESHES, min_z: float = MIN_Z) -> dict:
    """What the game shows from ``cam`` (x, y, z: game metres) in these sectors' rows (rows_of +
    globalize; sectors_streaming picks the sectors), each instance a 3x4 matrix, nearest first,
    grouped by mesh and appearance (plan_rows' answer).  ``info(rid) -> (cx, cy, cz, r) | None``: a
    read mesh's bounds (distances are to its bounds, not its pivot); ``drawable(res)``: the meshes
    read (the others are ``pending``, nearest first); a stand-in not drawable (or ``failed``) does
    not hide what it stands for."""
    cam = lod_camera(cam)
    rows = [r for r in rows if r is not None]
    detail = max(0.1, float(detail))
    parts = []
    culled = 0
    failed = {f.lower() for f in failed} if failed else set()
    ready_memo = {}

    def ready(rid):
        got = ready_memo.get(rid)
        if got is None:
            res = strings.list[rid]
            got = ready_memo[rid] = bool(drawable(res)) if drawable is not None else True
            if got and res.lower() in failed:
                got = ready_memo[rid] = False
        return got
    if rows:
        cat = lambda k: np.concatenate([r[k] for r in rows])     # noqa: E731
        pos = cat("pos")
        if len(pos):
            rid, aid, rot, scl = cat("rid"), cat("aid"), cat("rot"), cat("scl")
            kind, dist = cat("kind"), cat("dist").astype(np.float64)
            hide = np.concatenate([r["hide"] if "hide" in r else np.full(len(r["pos"]), -1.0, np.float32) for r in rows]).astype(np.float64)
            pid = np.concatenate([r["pid"] if "pid" in r else np.zeros(len(r["pos"]), np.uint64) for r in rows])
            par = np.concatenate([r["par"] if "par" in r else np.zeros(len(r["pos"]), np.uint64) for r in rows])
            centre, rad = _world_centres(pos, rot, scl, rid, info)
            dc = np.maximum(0.0, np.sqrt(((centre - cam[None, :]) ** 2).sum(axis=1)) - rad)
            size = np.where(rad > 0, rad, 2.0)                  # (a mesh not read yet: a couple of metres)
            far = kind == KIND_FAR
            lvl = np.concatenate([np.full(len(r["pos"]), int(r.get("level") or 0), np.int16) for r in rows])
            terr = np.concatenate([r["terr"] if "terr" in r else np.zeros(len(r["pos"]), bool) for r in rows])
            # a stand-in shows from its hide distance on, up to its streaming distance -- the
            # coarsest (level 6, and the terrain's, whose stand-ins stand in for each other) however
            # far, its sector's streaming box bounding it: a map is looked at from higher up than
            # the game, and they are then the city and its ground; what is under a stand-in shows
            # nearer (the parents' rule)
            free = (lvl >= COARSE_LEVEL) | terr                 # (the terrain's stand-ins: its parents' rule is enough)
            own = np.where(far, (dc >= np.maximum(hide, 0.0) * detail) & (free | (dc <= dist * detail)), dc <= dist * detail)
            # the stand-ins (by id) and whether each lets what is under it show
            isp = pid != 0
            order = np.argsort(pid[isp], kind="stable")
            ids = pid[isp][order]
            p_dc, p_hide, p_par = dc[isp][order], hide[isp][order], par[isp][order]
            p_ok = np.array([ready(int(r)) for r in rid[isp][order]], bool) if len(ids) else np.zeros(0, bool)
            allow = _ancestors_allow(par, ids, p_dc, p_hide, p_par, detail, p_ok)
            vis = own & allow
            culled = int(len(vis) - vis.sum())
            sel = np.nonzero(vis)[0]
            parts.append((rid[sel], aid[sel], pos[sel], rot[sel], scl[sel], dc[sel], size[sel]))
            fol_allow = (ids, p_dc, p_hide, p_par, p_ok)
        else:
            fol_allow = None
        # foliage: the populations of the foliage nodes nearer than their streaming distance
        for rw in rows:
            if not len(rw.get("frid", ())) or foliage is None:
                continue
            fp = rw["fpos"]
            fd = np.sqrt(((fp - cam[None, :]) ** 2).sum(axis=1))
            fdist = rw["fdist"].astype(np.float64) if "fdist" in rw else np.full(len(fp), 150.0)
            ok = fd <= fdist * detail + 30.0
            if fol_allow is not None and "fpar" in rw:
                ok &= _ancestors_allow(rw["fpar"], fol_allow[0], fol_allow[1], fol_allow[2], fol_allow[3], detail, fol_allow[4])
            sel = np.nonzero(ok)[0]
            if not len(sel):
                continue
            by_res = {}
            for i in sel:
                by_res.setdefault(int(rw["fres"][i]), []).append(int(i))
            for res_id, nodes in by_res.items():
                pops = foliage(strings.list[res_id])
                if pops is None:
                    continue
                ppos, prot, pscl = pops["pos"], pops["rot"], pops["scale"]
                if not isinstance(ppos, np.ndarray):
                    ppos, prot, pscl = (np.asarray(ppos, np.float64).reshape(-1, 3), np.asarray(prot, np.float64).reshape(-1, 4),
                                        np.asarray(pscl, np.float64).reshape(-1))
                npop = len(ppos)
                idx, owner = [], []
                for i in nodes:
                    b = int(rw["fbegin"][i])
                    e = min(b + int(rw["fcount"][i]), npop)
                    if e > b:
                        idx.append(np.arange(b, e))
                        owner.append(np.full(e - b, i))
                if not idx:
                    continue
                idx, owner = np.concatenate(idx), np.concatenate(owner)
                nrot = rw["frot"][owner]
                fpos = rw["fpos"][owner] + np.einsum("nij,nj->ni", quats_matrix(nrot), ppos[idx])
                frot = quats_mul(nrot, prot[idx])
                fs = (pscl[idx] * rw["fscl"][owner]).astype(np.float64)
                fdc = np.sqrt(((fpos - cam[None, :]) ** 2).sum(axis=1))
                keep = fdc <= rw["fdist"][owner].astype(np.float64) * detail + 30.0 if "fdist" in rw else np.ones(len(fdc), bool)
                k = np.nonzero(keep)[0]
                parts.append((rw["frid"][owner][k], rw["faid"][owner][k], fpos[k], frot[k],
                              np.repeat(fs[k, None], 3, axis=1).astype(np.float32), fdc[k], 3.0 * np.abs(fs[k])))
    if parts:
        rid = np.concatenate([p[0] for p in parts]).astype(np.int64)
        aid = np.concatenate([p[1] for p in parts]).astype(np.int64)
        pos = np.concatenate([p[2] for p in parts])
        rot = np.concatenate([p[3] for p in parts])
        scl = np.concatenate([p[4] for p in parts]).astype(np.float64)
        dcs = np.concatenate([p[5] for p in parts]).astype(np.float64)
        size = np.concatenate([p[6] for p in parts]).astype(np.float64)
    else:
        rid = aid = np.zeros(0, np.int64)
        pos, rot, scl, dcs, size = np.zeros((0, 3)), np.zeros((0, 4)), np.zeros((0, 3)), np.zeros(0), np.zeros(0)
    ok = pos[:, 2] >= min_z if len(pos) else np.zeros(0, bool)
    sel = np.nonzero(ok)[0]
    rid, aid, pos, rot, scl, dcs = rid[sel], aid[sel], pos[sel], rot[sel], scl[sel], dcs[sel]
    # what matters most first (the budgets keep it): the biggest on screen -- size over distance
    order = np.argsort(-(size[sel] / np.maximum(dcs, 5.0)), kind="stable")
    rid, aid, pos, rot, scl, dcs = rid[order], aid[order], pos[order], rot[order], scl[order], dcs[order]
    total = len(rid)
    pending, area = [], None
    if total:
        u, first = np.unique(rid, return_index=True)
        by_first = u[np.argsort(first)]
        ok_mesh = np.array([ready(int(r)) for r in by_first], dtype=bool)
        area = (int(ok_mesh.sum()), len(by_first))
        pending = [strings.list[int(r)] for r in by_first[~ok_mesh][:max_meshes]]       # (failed ones too: to_json counts them)
        total -= int(np.isin(rid, by_first[~ok_mesh]).sum())
        allowed = by_first[ok_mesh][:max_meshes]
        m = np.isin(rid, allowed)
        sel = np.nonzero(m)[0][:max_instances]
        rid, aid, pos, rot, scl, dcs = rid[sel], aid[sel], pos[sel], rot[sel], scl[sel], dcs[sel]
    kept = len(rid)
    groups, meshes = [], []
    if kept:
        mats = np.empty((kept, 3, 4), np.float32)
        mats[:, :, :3] = quats_matrix(rot) * scl[:, None, :]
        mats[:, :, 3] = pos
        key = rid * (int(aid.max()) + 1) + aid
        uk, first, inv = np.unique(key, return_index=True, return_inverse=True)
        inv = inv.reshape(-1)
        by = np.argsort(inv, kind="stable")
        counts = np.bincount(inv, minlength=len(uk))
        starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
        seen_mesh = set()
        for gi in np.argsort(first, kind="stable"):
            idx = by[starts[gi]:starts[gi] + counts[gi]]
            i0 = idx[0]
            res = strings.list[int(rid[i0])]
            groups.append({"res": res, "app": strings.list[int(aid[i0])], "ring": "near", "count": int(counts[gi]),
                           "matrices": mats[idx]})
            if res.lower() not in seen_mesh:
                seen_mesh.add(res.lower())
                meshes.append(res)
    return {"groups": groups, "meshes": meshes, "pending": pending, "area": area, "near_area": area,
            "instances": kept, "dropped": total - kept, "culled": culled, "center": [float(cam[0]), float(cam[1])],
            "near": 0.0, "far": 0.0}


def plan(sectors: dict, x: float, y: float, near: float = DEFAULT_NEAR, far: float = DEFAULT_FAR,
         foliage=None, max_instances: int = MAX_INSTANCES, max_meshes: int = MAX_MESHES, **kw) -> dict:
    """{path: sector (sector.read_sector)} -> {groups: [{res, app, ring, count, matrices (float32
    (n, 3, 4))}], meshes: [res], instances, dropped}.  ``foliage(path) -> {pos, rot, scale}`` arrays of a
    compiled foliage resource (None: foliage left out).  (plan_rows, from the sectors as they are.)"""
    strings = Strings()
    rows = [globalize(rows_of(sec), strings) for sec in sectors.values() if sec]
    return plan_rows(rows, strings, x, y, near, far, foliage=foliage, max_instances=max_instances,
                     max_meshes=max_meshes, **kw)


def foliage_needed_rows(rows: list, strings: Strings, x: float, y: float, near: float, margin: float = 150.0) -> list:
    """foliage_needed, from sectors' rows."""
    r2 = (near + margin) ** 2
    out = set()
    for rw in rows:
        if rw is None or not len(rw["frid"]):
            continue
        fp = rw["fpos"]
        sel = (fp[:, 0] - x) ** 2 + (fp[:, 1] - y) ** 2 <= r2
        for i in np.unique(rw["fres"][sel]):
            out.add(strings.list[int(i)])
    return sorted(out)


def foliage_needed(sectors: dict, x: float, y: float, near: float, margin: float = 150.0) -> list:
    """The compiled foliage resources the near ring draws from: those of foliage nodes standing
    within ``near`` (+ a population's reach) of the point; a level 6 sector names hundreds more,
    kilometres away."""
    r2 = (near + margin) ** 2
    out = set()
    for sec in sectors.values():
        for e in (sec or {}).get("entries") or []:
            fo = e.get("foliage")
            if fo and fo.get("res") and (e["pos"][0] - x) ** 2 + (e["pos"][1] - y) ** 2 <= r2:
                out.add(fo["res"])
    return sorted(out)


def to_json(p: dict, geo_id, ready, failed=()) -> dict:
    """plan() for the browser: each group's matrices as base64 float32 (12 per instance, column by
    column: x axis, y axis, z axis, translation; ``h``: their checksum), its mesh by id, and whether
    that mesh is ready (the matrices only then: the browser draws nothing else)."""
    import zlib
    groups = []
    for g in p["groups"]:
        gid = geo_id(g["res"])
        ok = bool(ready(g["res"]))
        out = {"id": gid, "res": g["res"], "app": g["app"], "ring": g["ring"], "count": g["count"], "ready": ok}
        if ok:
            b = np.ascontiguousarray(np.transpose(g["matrices"], (0, 2, 1)), dtype=np.float32).tobytes()   # (n, 4 cols, 3)
            out["m"] = base64.b64encode(b).decode("ascii")
            out["h"] = zlib.crc32(b)
        groups.append(out)
    pending = p.get("pending") or []
    nready = sum(1 for r in p["meshes"] if ready(r))
    nfailed = sum(1 for r in pending if r.lower() in failed)
    total = len(p["meshes"]) + len(pending)
    meshes = {"total": total, "ready": nready, "failed": nfailed}
    if p.get("area"):                             # the whole area's (what is too small to see from here too)
        meshes.update(ready=p["area"][0], total=max(p["area"][1], total))
    if p.get("near_area"):                        # the near ring's: where the map's own blocks give way
        meshes["near"] = list(p["near_area"])
    return {"center": p["center"], "near": p["near"], "far": p["far"], "instances": p["instances"],
            "dropped": p["dropped"], "culled": p.get("culled", 0), "groups": groups, "meshes": meshes}


def foliage_populations(doc: dict) -> dict | None:
    """A compiled foliage resource (.cfoliage, WolvenKit JSON) -> {pos, rot, scale} lists."""
    root = (doc.get("Data") or {}).get("RootChunk") or doc
    buf = root.get("dataBuffer") or {}
    pops = ((buf.get("Data") or {}).get("Populations")) if isinstance(buf, dict) else None
    if pops is None:
        return None
    pos, rot, scale = [], [], []
    for p in pops:
        v = p.get("Position") or {}
        r = p.get("Rotation") or {}
        pos.append([round(float(v.get("X", 0.0)), 4), round(float(v.get("Y", 0.0)), 4), round(float(v.get("Z", 0.0)), 4)])
        rot.append([round(float(r.get("X", 0.0)), 6), round(float(r.get("Y", 0.0)), 6), round(float(r.get("Z", 0.0)), 6),
                    round(float(r.get("W", 1.0)), 6)])
        scale.append(round(float(p.get("Scale", 1.0)), 4))
    return {"pos": pos, "rot": rot, "scale": scale}
