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
    return low.endswith(".mesh") and not any(k in low for k in SKIP_RES)


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
ROWS_VERSION = 1


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
    R = {k: [] for k in ("rid", "aid", "kind", "pos", "rot", "scl", "dist")}
    F = {k: [] for k in ("rid", "aid", "pos", "rot", "scl", "res", "begin", "count")}
    for e in sec.get("entries") or []:
        t, res = e.get("type"), e.get("res") or ""
        w = ok.get(res)
        if w is None:
            w = ok[res] = wanted(res)
        if not w:
            continue
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
            continue
        el = e.get("elements")
        items = el.get("items") if el and el.get("kind") == "instances" else None
        dist = float(e.get("dist") or 0.0)
        for it in (items if items is not None else [e]):
            R["rid"].append(r)
            R["aid"].append(a)
            R["kind"].append(kind)
            R["pos"].append(it["pos"][:3])
            R["rot"].append(it["rot"][:4])
            R["scl"].append((it.get("scale") or [1.0, 1.0, 1.0])[:3])
            R["dist"].append(dist)
    out = {"strings": local, "level": level}
    for k, dt, w in (("rid", np.int32, 0), ("aid", np.int32, 0), ("kind", np.uint8, 0), ("pos", np.float64, 3),
                     ("rot", np.float64, 4), ("scl", np.float32, 3), ("dist", np.float32, 0)):
        out[k] = np.asarray(R[k], dtype=dt).reshape((-1, w) if w else (-1,))
    for k, dt, w in (("rid", np.int32, 0), ("aid", np.int32, 0), ("pos", np.float64, 3), ("rot", np.float64, 4),
                     ("scl", np.float32, 0), ("res", np.int32, 0), ("begin", np.int32, 0), ("count", np.int32, 0)):
        out["f" + k] = np.asarray(F[k], dtype=dt).reshape((-1, w) if w else (-1,))
    return out


def globalize(rows: dict, strings: Strings) -> dict:
    """rows_of's local names -> the shared table's ids."""
    m = strings.ids_of(rows["strings"])
    out = dict(rows)
    for k in ("rid", "aid", "frid", "faid", "fres"):
        out[k] = m[rows[k]] if len(rows[k]) else rows[k]
    out["strings"] = None
    return out


def save_rows(fp: str, rows: dict, tag: int = 0) -> None:
    """rows_of's arrays to a file (``tag``: the version of the sector they were read from)."""
    import os
    tmp = fp + ".tmp.npz"
    arrays = {k: v for k, v in rows.items() if isinstance(v, np.ndarray)}
    np.savez(tmp, v=np.array([ROWS_VERSION, int(tag)]), level=np.array([rows["level"]]),
             strings=np.frombuffer("\n".join(rows["strings"]).encode("utf-8"), np.uint8), **arrays)
    os.replace(tmp, fp)


def load_rows(fp: str, tag: int = 0) -> dict | None:
    try:
        with np.load(fp, allow_pickle=False) as z:
            if len(z["v"]) < 2 or int(z["v"][0]) != ROWS_VERSION or int(z["v"][1]) != int(tag):
                return None
            out = {k: z[k] for k in z.files if k not in ("v", "level", "strings")}
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
