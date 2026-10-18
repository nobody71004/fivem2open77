"""How well the generated colliders cover the map's own FiveM collision.

The converter rebuilds the GTA collision (polygon soups, primitives) as boxes.  This module checks
the result the way a player experiences it: sample points on every walkable FiveM collision
surface and look straight down for a collider; sample points on every wall and look into it.  A
sample with nothing under it within a metre is a hole the player falls through; a wall sample
with nothing behind it is a wall they walk through.  Everything happens in the map's anchor-local
frame, after the placements, so it checks what the game will actually load.

    report = coverage(scene, model)        # {"walkable": {...}, "walls": {...}, "holes": [...]}
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

WALKABLE_NZ = 0.6       # a surface a player stands on (about 53 degrees or flatter)
WALL_NZ = 0.3           # a surface a player walks into
UP = 0.25               # rays start this far above / in front of the surface
FALL = 1.0              # nothing within this far below a floor: the player falls through
SINK = 0.3              # collider more than this below the floor: the player sinks into it
WALL_GAP = 0.5          # nothing within this far behind a wall: the player walks through it


# ----------------------------------------------------------------------------- reference geometry
def reference_triangles(scene, model, solid_doors=False, door_re=None):
    """[(label, triangles (N,3,3))] of the FiveM collision in the map's anchor-local frame."""
    A = np.eye(4)
    A[:3, 3] = -np.asarray(model.anchor, float)
    out = []

    def door(name):
        return (not solid_doors) and door_re is not None and bool(door_re.search(name or ""))

    def add(label, col, m):
        if col is None or col.empty:
            return
        c = col.transformed(m)
        tris = [c.triangles] if len(c.triangles) else []
        for centre, axes, half, _ in c.boxes:          # primitive boxes: their 12 triangles
            tris.append(_box_triangles(np.asarray(centre), np.asarray(axes), np.asarray(half)))
        if tris:
            out.append((label, np.concatenate(tris)))

    for mlo in scene.mlos:
        L = A @ mlo.matrix
        add(f"{mlo.name} (MLO shell)", mlo.collision, L)
        for ch in mlo.children:
            if not door(ch.name):
                add(f"{mlo.name}/{ch.name}", scene.entity_collision(ch.archetype), L @ ch.matrix)
    for inst in scene.instances:
        if not door(inst.name):
            add(inst.name, scene.entity_collision(inst.archetype), A @ inst.matrix)
    for name, col in scene.world_collisions:
        add(f"{name}.ybn", col, A)
    return out


def _box_triangles(c, axes, h):
    ax = np.asarray(axes, float)
    corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float)
    pts = c + (corners * h) @ ax
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, cc, d in quads:
        tris.append([pts[a], pts[b], pts[cc]])
        tris.append([pts[a], pts[cc], pts[d]])
    return np.array(tris)


# ----------------------------------------------------------------------------- placed colliders
def _quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def obb_arrays(colliders, rz=None, t=None):
    """(centres, rotations, half extents) of colliders as oriented boxes, optionally turned by the
    3x3 ``rz`` and moved by ``t``.  Spheres and capsules count as their bounding boxes."""
    C, R, H = [], [], []
    rz = np.eye(3) if rz is None else rz
    t = np.zeros(3) if t is None else np.asarray(t, float)
    for c in colliders:
        if c.kind == "box":
            rot, half = _quat_matrix(c.rotation), np.asarray(c.half, float)
        elif c.kind == "sphere":
            rot, half = np.eye(3), np.full(3, c.radius)
        elif c.kind == "capsule":       # along local x (PhysX), half height + radius
            rot, half = _quat_matrix(c.rotation), np.array([c.height / 2 + c.radius, c.radius, c.radius])
        else:
            continue
        C.append(rz @ np.asarray(c.center, float) + t)
        R.append(rz @ rot)
        H.append(half)
    if not C:
        return np.zeros((0, 3)), np.zeros((0, 3, 3)), np.zeros((0, 3))
    return np.array(C), np.array(R), np.array(H)


def uncovered_triangles(colliders, tris, *, gap=0.3, walk_nz=0.3, wall_nz=0.3, min_area=0.005,
                        min_wall_height=0.6, headroom=0.0, density=2.0, max_points=48):
    """Mask of the triangles a player could fall through or walk into: walkable ones (normal z >=
    ``walk_nz``) with a sample point that has no collider within ``gap`` below it, steep ones
    (|normal z| < ``wall_nz``, at least ``min_wall_height`` tall: not a step riser) with a sample
    point that has nothing within ``gap`` behind it.  Ceilings never count, nor floors with a
    collider less than ``headroom`` above them (nobody stands there: under a deck, a step, a slab)."""
    tris = np.asarray(tris, float)
    n = len(tris)
    mask = np.zeros(n, bool)
    if not n:
        return mask
    cr = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(cr, axis=1)
    nrm = cr / np.maximum(ln, 1e-12)[:, None]
    big = 0.5 * ln > min_area
    walk = big & (nrm[:, 2] >= walk_nz)
    height = tris[:, :, 2].max(1) - tris[:, :, 2].min(1)
    wall = big & (np.abs(nrm[:, 2]) < wall_nz) & (height >= min_wall_height)
    sel = np.flatnonzero(walk | wall)
    if not len(sel):
        return mask
    caster = BoxCaster(*obb_arrays(colliders))
    cen = tris[sel].mean(1)
    # centroid, three points two thirds of the way to the corners, and for big triangles a
    # barycentric lattice (about `density` points per square metre)
    pts = np.concatenate([cen[:, None, :], cen[:, None, :] + (tris[sel] - cen[:, None, :]) * (2.0 / 3.0)], 1)
    P = [pts.reshape(-1, 3)]
    owner = [np.repeat(np.arange(len(sel)), pts.shape[1])]
    area = 0.5 * ln[sel]
    m = np.clip(np.ceil(np.sqrt(area * density * 2.0)).astype(int), 1, int(math.sqrt(2 * max_points)))
    for lvl in np.unique(m[m > 2]):
        which = np.flatnonzero(m == lvl)
        ij = np.array([(i, j) for i in range(lvl + 1) for j in range(lvl + 1 - i)], float) / lvl
        ij = ij * 0.9 + 0.05 / 1.0 * (1.0 / 3.0)                       # keep off the edges a little
        u, v = ij[:, 0], ij[:, 1]
        t = tris[sel[which]]
        q = t[:, None, 0] + u[None, :, None] * (t[:, None, 1] - t[:, None, 0]) + v[None, :, None] * (t[:, None, 2] - t[:, None, 0])
        P.append(q.reshape(-1, 3))
        owner.append(np.repeat(which, len(u)))
    P = np.concatenate(P)
    owner = np.concatenate(owner)
    is_walk = walk[sel][owner]
    N = nrm[sel][owner]
    D = np.where(is_walk[:, None], np.array([0.0, 0.0, -1.0]), -N)
    O = P - D * UP
    d = caster.cast(O, D, UP + gap + 0.05) - UP
    miss = np.zeros(len(sel), bool)
    np.logical_or.at(miss, owner, d > gap)
    if headroom and miss.any():
        w = np.flatnonzero(miss & walk[sel])
        if len(w):
            above = caster.cast(cen[w] + [0.0, 0.0, 0.02], np.tile([0.0, 0.0, 1.0], (len(w), 1)), headroom)
            miss[w[np.isfinite(above)]] = False
    mask[sel[miss]] = True
    return mask


def placed_boxes(model):
    """Every collider of every placement as an oriented box in the anchor-local frame."""
    Cs, Rs, Hs, part_of = [], [], [], []
    for pl in model.placements:
        part = model.parts.get(pl.part)
        if part is None or not part.colliders:
            continue
        a = math.radians(pl.yaw)
        rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
        C, R, H = obb_arrays(part.colliders, rz, pl.position)
        Cs.append(C)
        Rs.append(R)
        Hs.append(H)
        part_of += [pl.part] * len(C)
    if not Cs:
        return np.zeros((0, 3)), np.zeros((0, 3, 3)), np.zeros((0, 3)), []
    return np.concatenate(Cs), np.concatenate(Rs), np.concatenate(Hs), part_of


# ----------------------------------------------------------------------------- ray casting
class BoxCaster:
    """Ray casts against many oriented boxes, bucketed on a 2D grid."""

    def __init__(self, C, R, H, cell=4.0):
        self.C, self.R, self.H, self.cell = C, R, H, cell
        ext = np.abs(R) @ H[..., None] if len(C) else np.zeros((0, 3, 1))
        ext = ext[..., 0]
        self.lo, self.hi = C - ext, C + ext
        self.grid = defaultdict(list)
        for i in range(len(C)):
            x0, y0 = np.floor(self.lo[i, :2] / cell).astype(int)
            x1, y1 = np.floor(self.hi[i, :2] / cell).astype(int)
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    self.grid[(gx, gy)].append(i)
        self.grid = {k: np.array(v) for k, v in self.grid.items()}

    def cast(self, O, D, tmax):
        """Distance to the first box along each ray (inf when none within tmax).  Rays must stay
        within one grid cell horizontally over tmax (vertical rays, or short horizontal ones)."""
        n = len(O)
        best = np.full(n, np.inf)
        if not len(self.C) or not n:
            return best
        keys = np.floor(O[:, :2] / self.cell).astype(int)
        order = np.lexsort((keys[:, 1], keys[:, 0]))
        ks = keys[order]
        cuts = np.flatnonzero(np.any(np.diff(ks, axis=0) != 0, axis=1)) + 1
        for grp in np.split(order, cuts):
            k = tuple(keys[grp[0]])
            idx = set()
            # horizontal rays may leave the cell: also look at the neighbours
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    v = self.grid.get((k[0] + dx, k[1] + dy))
                    if v is not None:
                        idx.update(v.tolist())
            if not idx:
                continue
            ids = np.fromiter(idx, int)
            best[grp] = self._cast_many(O[grp], D[grp], ids, tmax)
        return best

    def _cast_many(self, O, D, ids, tmax):
        out = np.full(len(O), np.inf)
        step = max(1, 1_000_000 // max(1, len(ids)))
        for s in range(0, len(O), step):
            o, d = O[s:s + step], D[s:s + step]
            C, R, H = self.C[ids], self.R[ids], self.H[ids]
            # into each box frame: local = R^T (p - c)
            lo = np.einsum("bji,rbj->rbi", R, o[:, None, :] - C[None])
            ld = np.einsum("bji,rj->rbi", R, d)
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / ld
                t1 = (-H[None] - lo) * inv
                t2 = (H[None] - lo) * inv
            tn = np.where(np.isnan(t1), -np.inf, np.minimum(t1, t2))
            tf = np.where(np.isnan(t2), np.inf, np.maximum(t1, t2))
            # parallel to a slab: inside it or never
            par = np.abs(ld) < 1e-12
            inside = np.abs(lo) <= H[None]
            tn = np.where(par, np.where(inside, -np.inf, np.inf), tn)
            tf = np.where(par, np.where(inside, np.inf, -np.inf), tf)
            enter = tn.max(-1)
            leave = tf.min(-1)
            hit = (enter <= leave) & (leave >= 0) & (enter <= tmax)
            t = np.where(hit, np.maximum(enter, 0.0), np.inf)
            out[s:s + step] = t.min(-1)
        return out


# ----------------------------------------------------------------------------- sampling
def _sample(tris, density, rng, max_samples):
    """Area-weighted random points on triangles; returns (points, normals, triangle index)."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    keep = area > 1e-6
    if not keep.any():
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0, int), 0.0
    total = float(area[keep].sum())
    n = int(min(max_samples, max(1, round(total * density))))
    p = area * keep / area[keep].sum()
    ti = rng.choice(len(tris), size=n, p=p)
    u, v = rng.random(n), rng.random(n)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    pts = a[ti] + u[:, None] * (b[ti] - a[ti]) + v[:, None] * (c[ti] - a[ti])
    nrm = cr[ti] / np.maximum(np.linalg.norm(cr[ti], axis=1, keepdims=True), 1e-12)
    return pts, nrm, ti, total


def coverage(scene, model, *, density=2.0, max_samples=400_000, seed=7, solid_doors=False, door_re=None,
             hole_cell=4.0, top=12):
    """Walkable and wall coverage of the model's colliders against the FiveM collision."""
    rng = np.random.default_rng(seed)
    ref = reference_triangles(scene, model, solid_doors=solid_doors, door_re=door_re)
    C, R, H, _ = placed_boxes(model)
    caster = BoxCaster(C, R, H)
    out = {"colliders": int(len(C)), "sources": len(ref)}
    holes = defaultdict(lambda: [0.0, 0.0, set()])     # grid cell -> [hole area, z sum, labels]
    walk_area = walk_hole = walk_sink = wall_area = wall_hole = 0.0
    walk_n = wall_n = 0
    per_source = []
    for label, tris in ref:
        cr = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        nz = cr[:, 2] / np.maximum(np.linalg.norm(cr, axis=1), 1e-12)
        src = {"source": label}
        # floors: look down
        floor = tris[nz >= WALKABLE_NZ]
        if len(floor):
            pts, _, _, area = _sample(floor, density, rng, max_samples)
            d = caster.cast(pts + [0, 0, UP], np.tile([0.0, 0.0, -1.0], (len(pts), 1)), UP + FALL + 1.0) - UP
            hole = d > FALL
            sink = (d > SINK) & ~hole
            w = area / max(1, len(pts))
            walk_area += area
            walk_hole += hole.sum() * w
            walk_sink += sink.sum() * w
            walk_n += len(pts)
            src.update({"floor_m2": round(area, 1),          # (a floor too small for a sample has no holes to count)
                        "floor_holes_pct": round(100.0 * hole.mean(), 2) if len(hole) else 0.0})
            for p in pts[hole]:
                k = (int(math.floor(p[0] / hole_cell)), int(math.floor(p[1] / hole_cell)))
                holes[k][0] += w
                holes[k][1] += p[2] * w
                holes[k][2].add(label)
        # walls: look into them (only where people can be: not below the lowest floor, which
        # leaves out the skirts maps hang under their terrain)
        wall = tris[np.abs(nz) < WALL_NZ]
        if len(floor) and len(wall):
            wall = wall[wall[:, :, 2].max(1) >= floor[:, :, 2].min() - 1.0]
        if len(wall):
            pts, nrm, _, area = _sample(wall, density, rng, max_samples)
            d = caster.cast(pts + nrm * UP, -nrm, UP + WALL_GAP + 0.5) - UP
            hole = d > WALL_GAP
            w = area / max(1, len(pts))
            wall_area += area
            wall_hole += hole.sum() * w
            wall_n += len(pts)
            src.update({"wall_m2": round(area, 1), "wall_holes_pct": round(100.0 * hole.mean(), 2) if len(hole) else 0.0})
        per_source.append(src)
    out["walkable"] = {"m2": round(walk_area, 1), "samples": walk_n,
                       "covered_pct": round(100.0 * (1 - walk_hole / walk_area), 2) if walk_area else 100.0,
                       "sinks_pct": round(100.0 * walk_sink / walk_area, 2) if walk_area else 0.0}
    out["walls"] = {"m2": round(wall_area, 1), "samples": wall_n,
                    "covered_pct": round(100.0 * (1 - wall_hole / wall_area), 2) if wall_area else 100.0}
    worst = sorted(holes.items(), key=lambda kv: -kv[1][0])[:top]
    out["holes"] = [{"x": round((k[0] + 0.5) * hole_cell, 1), "y": round((k[1] + 0.5) * hole_cell, 1),
                     "z": round(v[1] / v[0], 1) if v[0] else None, "m2": round(v[0], 1),
                     "sources": sorted(v[2])[:4]} for k, v in worst]
    per_source.sort(key=lambda s: -(s.get("floor_m2", 0) * s.get("floor_holes_pct", 0)))
    out["sources_worst"] = per_source[:top]
    return out


# ----------------------------------------------------------------------------- visual floors
class TriCaster:
    """Ray casts against many triangles (Moller-Trumbore), bucketed on a 2D grid."""

    def __init__(self, tris, cell=4.0):
        self.t = np.asarray(tris, float)
        self.cell = cell
        lo, hi = self.t.min(1), self.t.max(1)
        self.grid = defaultdict(list)
        for i in range(len(self.t)):
            x0, y0 = np.floor(lo[i, :2] / cell).astype(int)
            x1, y1 = np.floor(hi[i, :2] / cell).astype(int)
            if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096:          # absurd slivers: clamp
                x1, y1 = min(x1, x0 + 63), min(y1, y0 + 63)
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    self.grid[(gx, gy)].append(i)
        self.grid = {k: np.array(v) for k, v in self.grid.items()}

    def down(self, P, tmax):
        """Distance straight down from each point to the first triangle (inf when none)."""
        best = np.full(len(P), np.inf)
        keys = np.floor(P[:, :2] / self.cell).astype(int)
        order = np.lexsort((keys[:, 1], keys[:, 0]))
        ks = keys[order]
        cuts = np.flatnonzero(np.any(np.diff(ks, axis=0) != 0, axis=1)) + 1
        for grp in np.split(order, cuts):
            ids = self.grid.get(tuple(keys[grp[0]]))
            if ids is None:
                continue
            t = self.t[ids]
            a, b, c = t[:, 0], t[:, 1], t[:, 2]
            step = max(1, 2_000_000 // len(ids))
            for s in range(0, len(grp), step):
                g = grp[s:s + step]
                o = P[g][:, None, :]                       # rays x tris
                e1, e2 = (b - a)[None], (c - a)[None]
                d = np.array([0.0, 0.0, -1.0])
                pv = np.cross(d, e2)
                det = (e1 * pv).sum(-1)
                with np.errstate(divide="ignore", invalid="ignore"):
                    inv = 1.0 / det
                    tv = o - a[None]
                    u = (tv * pv).sum(-1) * inv
                    qv = np.cross(tv, e1)
                    v = (d * qv).sum(-1) * inv
                    tt = (e2 * qv).sum(-1) * inv
                    ok = (np.abs(det) > 1e-12) & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6) & (tt >= 0) & (tt <= tmax)
                tt = np.where(ok, tt, np.inf)
                best[g] = np.minimum(best[g], tt.min(1))
        return best


def _visual_floor_triangles(model, nz_min=0.85):
    """Upward-facing visual triangles of every placement, anchor-local."""
    out = []
    for pl in model.placements:
        part = model.parts.get(pl.part)
        if part is None or not part.submeshes:
            continue
        a = math.radians(pl.yaw)
        rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
        for sm in part.submeshes:
            if getattr(sm, "lod", 1) not in (0, 1):
                continue
            t = np.asarray(sm.positions, float)[np.asarray(sm.indices).reshape(-1, 3)]
            cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            ln = np.linalg.norm(cr, axis=1)
            keep = (ln > 1e-6) & (cr[:, 2] / np.maximum(ln, 1e-12) >= nz_min)
            if keep.any():
                out.append(t[keep] @ rz.T + np.asarray(pl.position, float))
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def visual_floor_coverage(scene, model, *, density=1.0, max_samples=300_000, seed=11, hole_cell=4.0, top=12,
                          door_re=None):
    """Flat visual surfaces (floors, paths, platforms) with nothing solid under them: split into
    gaps the converter left (the FiveM collision has a surface there) and gaps the map itself has
    (no FiveM collision either: the map was walk-through there in GTA too)."""
    rng = np.random.default_rng(seed)
    vis = _visual_floor_triangles(model)
    if not len(vis):
        return {"m2": 0.0}
    pts, _, _, area = _sample(vis, density, rng, max_samples)
    C, R, H, _ = placed_boxes(model)
    d = BoxCaster(C, R, H).cast(pts + [0, 0, UP], np.tile([0.0, 0.0, -1.0], (len(pts), 1)), UP + FALL + 1.0) - UP
    hole = d > FALL
    ref = [t for _, t in reference_triangles(scene, model, door_re=door_re)]
    ref = np.concatenate(ref) if ref else np.zeros((0, 3, 3))
    if len(ref):
        dr = TriCaster(ref).down(pts[hole] + [0, 0, UP], UP + FALL + 1.0) - UP
        fivem_has = dr <= FALL
    else:
        fivem_has = np.zeros(int(hole.sum()), bool)
    w = area / max(1, len(pts))
    hp = pts[hole]
    clusters = defaultdict(lambda: [0.0, 0.0, 0.0])
    for p, f in zip(hp, fivem_has):
        k = (int(math.floor(p[0] / hole_cell)), int(math.floor(p[1] / hole_cell)))
        clusters[k][0] += w
        clusters[k][1] += p[2] * w
        clusters[k][2] += w if f else 0.0
    worst = sorted(clusters.items(), key=lambda kv: -kv[1][0])[:top]
    return {"m2": round(area, 1), "samples": len(pts),
            "solid_pct": round(100.0 * (1 - hole.mean()), 2),
            "converter_gaps_m2": round(float(fivem_has.sum() * w), 1),
            "map_gaps_m2": round(float((~fivem_has).sum() * w), 1),
            "holes": [{"x": round((k[0] + 0.5) * hole_cell, 1), "y": round((k[1] + 0.5) * hole_cell, 1),
                       "z": round(v[1] / v[0], 1), "m2": round(v[0], 1), "fivem_collision_m2": round(v[2], 1)}
                      for k, v in worst]}
