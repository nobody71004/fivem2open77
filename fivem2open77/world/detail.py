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


def plan(sectors: dict, x: float, y: float, near: float = DEFAULT_NEAR, far: float = DEFAULT_FAR,
         foliage=None, max_instances: int = MAX_INSTANCES, max_meshes: int = MAX_MESHES) -> dict:
    """{path: sector (sector.read_sector)} -> {groups: [{res, app, ring, count, matrices (float32
    (n, 3, 4))}], meshes: [res], instances, dropped}.  ``foliage(path) -> {pos, rot, scale}`` arrays of a
    compiled foliage resource (None: foliage left out)."""
    r_near2, r_far2 = near * near, far * far
    cand = []                                   # (distance², res, app, ring, pos, rot, scale)

    def add(res, app, ring, pos, rot, scale):
        d2 = (pos[0] - x) ** 2 + (pos[1] - y) ** 2
        if ring == "near" and d2 > r_near2:
            return
        if ring == "far" and (d2 <= r_near2 or d2 > r_far2):
            return
        cand.append((d2, res, app or "", ring, pos, rot, scale))

    for path, sec in sectors.items():
        if not sec:
            continue
        level = int(sec.get("level") or 0)
        for e in sec.get("entries") or []:
            t, res = e.get("type"), e.get("res") or ""
            if not wanted(res):
                continue
            if t in PROXY_TYPES:
                ring = "far"
            elif t in NEAR_TYPES:
                ring = "near"
            else:
                continue
            app = e.get("app") or ""
            if t == "worldFoliageNode":
                fo = e.get("foliage") or {}
                pops = foliage(fo.get("res")) if (foliage and fo.get("res")) else None
                if pops is None:
                    continue
                b, n = int(fo.get("begin") or 0), int(fo.get("count") or 0)
                base_r = quat_matrix(e["rot"])
                for k in range(b, min(b + n, len(pops["pos"]))):
                    p = np.asarray(e["pos"]) + base_r @ np.asarray(pops["pos"][k])
                    q = S.quat_mul(e["rot"], pops["rot"][k])
                    s = float(pops["scale"][k]) * float((e.get("scale") or [1.0])[0])
                    add(res, app, "near", p.tolist(), q, [s, s, s])
                continue
            el = e.get("elements")
            if el and el.get("kind") == "instances":
                for it in el.get("items") or []:
                    add(res, app, ring, it["pos"], it["rot"], it.get("scale") or [1.0, 1.0, 1.0])
                continue
            add(res, app, ring, e["pos"], e["rot"], e.get("scale") or [1.0, 1.0, 1.0])
            if ring == "near" and level >= FAR_REAL_LEVEL:
                add(res, app, "far", e["pos"], e["rot"], e.get("scale") or [1.0, 1.0, 1.0])
    cand.sort(key=lambda c: c[0])
    groups, meshes, kept, dropped = {}, {}, 0, 0
    for d2, res, app, ring, pos, rot, scale in cand:
        key = (res.lower(), app, ring)
        if key not in groups:
            if res.lower() not in meshes and len(meshes) >= max_meshes:
                dropped += 1
                continue
            if kept >= max_instances:
                dropped += 1
                continue
            groups[key] = {"res": res, "app": app, "ring": ring, "m": []}
            meshes[res.lower()] = res
        elif kept >= max_instances:
            dropped += 1
            continue
        groups[key]["m"].append(matrix(pos, rot, scale))
        kept += 1
    out = []
    for g in groups.values():
        arr = np.asarray(g["m"], dtype=np.float32).reshape(-1, 3, 4)
        out.append({"res": g["res"], "app": g["app"], "ring": g["ring"], "count": len(arr), "matrices": arr})
    return {"groups": out, "meshes": list(meshes.values()), "instances": kept, "dropped": dropped,
            "center": [x, y], "near": near, "far": far}


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


def to_json(p: dict, geo_id, ready) -> dict:
    """plan() for the browser: each group's matrices as base64 float32 (12 per instance, column by
    column: x axis, y axis, z axis, translation), its mesh by id, and whether that mesh is ready."""
    groups = []
    for g in p["groups"]:
        m = np.ascontiguousarray(np.transpose(g["matrices"], (0, 2, 1)), dtype=np.float32)   # (n, 4 cols, 3)
        gid = geo_id(g["res"])
        groups.append({"id": gid, "res": g["res"], "app": g["app"], "ring": g["ring"], "count": g["count"],
                       "ready": bool(ready(g["res"])), "m": base64.b64encode(m.tobytes()).decode("ascii")})
    return {"center": p["center"], "near": p["near"], "far": p["far"], "instances": p["instances"],
            "dropped": p["dropped"], "groups": groups,
            "meshes": {"total": len(p["meshes"]), "ready": sum(1 for r in p["meshes"] if ready(r))}}


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
