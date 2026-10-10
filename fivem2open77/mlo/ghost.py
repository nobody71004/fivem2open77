"""Wireframe preview of a map for the in-game placer.

Open77 cannot draw the map's own meshes at a moving position (the map is baked into a sector), so
while someone places it the client draws this wireframe with ``Open77.debugDraw``.  It is built for
lining the map up with the world around it, most useful lines first:

  4 footprint   the map's outline at ground level: the edge of everything at or above the main
                ground (platform, drive, yards), traced from above, at the height of that ground
  5 gates       every gate leaf (and any door wider than 3 m): where roads and paths come in
  0 bounds      the box around everything that can be seen
  1 buildings   the box of each building and big object (no props, no door leaves)
  2 walls       the largest walls and floors of those buildings (thin slabs as one rectangle)

The client draws them in that order, in that order of importance (thick yellow footprint, green
gates, cyan buildings) and refreshes the first ones first while the map moves.  The engine caps
debug geometry at 8192 vertices per client and 512 lines per batch, and the client works through
the lines a slice per frame, so the whole preview is budgeted (DEFAULT_BUDGET lines).
"""
from __future__ import annotations

import math

import numpy as np

KIND_BOUNDS, KIND_PART, KIND_WALL, KIND_GROUND, KIND_FOOT, KIND_GATE = 0, 1, 2, 3, 4, 5
DEFAULT_BUDGET = 1800
FOOT_BUDGET = 500       # lines for the footprint (more detail is simplified away)
GATE_BUDGET = 320
PART_BUDGET = 700
THIN = 0.35             # a box thinner than this (half extent, metres) is drawn as a rectangle
BAND = 4.0              # the footprint takes what reaches up to this far below the main ground
GATE_WIDTH = 3.0        # a door this wide is drawn as a gate (a drive goes through it)

_BOX_EDGES = [(0, 1), (1, 3), (3, 2), (2, 0), (4, 5), (5, 7), (7, 6), (6, 4), (0, 4), (1, 5), (2, 6), (3, 7)]


def _quat_mat(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def box_lines(center, axes, half) -> list:
    """Edges of an oriented box; axes = 3x3 matrix whose COLUMNS are the box axes."""
    c = np.asarray(center, float)
    a = np.asarray(axes, float)
    h = np.asarray(half, float)
    if min(h) < THIN:
        # a slab: one rectangle through its middle, spanned by the two largest axes
        i, j = sorted(range(3), key=lambda k: -h[k])[:2]
        u, v = a[:, i] * h[i], a[:, j] * h[j]
        p = [c - u - v, c + u - v, c + u + v, c - u + v]
        return [(p[0], p[1]), (p[1], p[2]), (p[2], p[3]), (p[3], p[0])]
    corners = [c + a @ (h * np.array([sx, sy, sz])) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    return [(corners[i], corners[j]) for i, j in _BOX_EDGES]


def aabb_lines(mn, mx) -> list:
    mn, mx = np.asarray(mn, float), np.asarray(mx, float)
    return box_lines((mn + mx) / 2, np.eye(3), np.maximum((mx - mn) / 2, THIN))


def _placement_matrix(pl) -> tuple:
    a = math.radians(pl.yaw)
    r = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    return r, np.asarray(pl.position, float)


def _collider_boxes(part) -> list:
    """(importance, center, axes, half) of the part's box colliders, biggest faces first."""
    out = []
    for c in part.colliders:
        if c.kind != "box":
            continue
        h = np.asarray(c.half, float)
        big = sorted(h)[-2:]
        area = 4.0 * big[0] * big[1]
        if area < 2.0:          # skip clutter: the preview is about walls and floors
            continue
        out.append((area, np.asarray(c.center, float), _quat_mat(c.rotation), h))
    out.sort(key=lambda t: -t[0])
    return out


# ------------------------------------------------------------------ footprint (traced from above)
def ground_level(tris) -> float | None:
    """The main ground of the map: the height with the most upward-facing surface (0.5 m bins)."""
    if len(tris) == 0:
        return None
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    n = np.cross(b - a, c - a)
    nl = np.linalg.norm(n, axis=1)
    up = (nl > 1e-9) & (n[:, 2] > 0.8 * np.maximum(nl, 1e-12))
    if not up.any():
        return None
    w = np.abs(n[up, 2]) / 2
    z = np.round(tris[up, :, 2].mean(1) / 0.5).astype(np.int64)
    lo = int(z.min())
    hist = np.bincount(z - lo, weights=w)
    return float((int(np.argmax(hist)) + lo) * 0.5)


def _dilate(g):
    o = g.copy()
    o[1:, :] |= g[:-1, :]
    o[:-1, :] |= g[1:, :]
    o[:, 1:] |= g[:, :-1]
    o[:, :-1] |= g[:, 1:]
    o[1:, 1:] |= g[:-1, :-1]
    o[:-1, :-1] |= g[1:, 1:]
    o[1:, :-1] |= g[:-1, 1:]
    o[:-1, 1:] |= g[1:, :-1]
    return o


def _erode(g):
    return ~_dilate(~g)


def _fill_holes(g):
    """g with every empty region that does not touch the border filled."""
    outside = np.zeros_like(g)
    outside[0, :] = outside[-1, :] = outside[:, 0] = outside[:, -1] = True
    outside &= ~g
    while True:
        nxt = _dilate(outside) & ~g
        if (nxt == outside).all():
            return ~outside
        outside = nxt


def boundary_loops(solid) -> list:
    """Closed loops of grid corners (i, j) around the solid cells, the solid on the left."""
    s = np.pad(solid, 1)
    edges: dict = {}

    def add(p, q):
        edges.setdefault(p, []).append(q)

    ii, jj = np.nonzero(solid)
    for i, j in zip(ii.tolist(), jj.tolist()):
        I, J = i + 1, j + 1
        if not s[I, J - 1]:
            add((i, j), (i + 1, j))
        if not s[I + 1, J]:
            add((i + 1, j), (i + 1, j + 1))
        if not s[I, J + 1]:
            add((i + 1, j + 1), (i, j + 1))
        if not s[I - 1, J]:
            add((i, j + 1), (i, j))
    loops = []
    while edges:
        start = next(iter(edges))
        loop, cur, prev = [start], start, None
        while True:
            outs = edges.get(cur)
            if not outs:
                break
            if len(outs) > 1 and prev is not None:
                # where two outlines touch at a corner, keep turning left (the solid stays left)
                outs.sort(key=lambda q: -(prev[0] * (q[1] - cur[1]) - prev[1] * (q[0] - cur[0])))
            nxt = outs.pop(0)
            if not outs:
                del edges[cur]
            prev = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start:
                break
            loop.append(cur)
        if len(loop) >= 4:
            loops.append(loop)
    return loops


def _rdp(pts, tol):
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    ab = b - a
    length = math.hypot(ab[0], ab[1])
    if length < 1e-9:
        d = np.hypot(pts[:, 0] - a[0], pts[:, 1] - a[1])
    else:
        d = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / length
    k = int(np.argmax(d))
    if d[k] <= tol:
        return np.array([a, b])
    return np.vstack([_rdp(pts[:k + 1], tol)[:-1], _rdp(pts[k:], tol)])


def simplify_loop(loop, tol) -> np.ndarray:
    """Douglas-Peucker for a closed loop (split at its two farthest points)."""
    p = np.asarray(loop, float)
    c = p.mean(0)
    i = int(np.argmax(np.hypot(p[:, 0] - c[0], p[:, 1] - c[1])))
    j = int(np.argmax(np.hypot(p[:, 0] - p[i, 0], p[:, 1] - p[i, 1])))
    if i > j:
        i, j = j, i
    if j - i < 2 or len(p) - (j - i) < 2:
        return p
    h1 = _rdp(p[i:j + 1], tol)
    h2 = _rdp(np.vstack([p[j:], p[:i + 1]]), tol)
    return np.vstack([h1[:-1], h2[:-1]])


def prune_loop(p, tol) -> np.ndarray:
    """Drop corners that stand less than tol off the line between their neighbours (the small jogs
    a Douglas-Peucker split can keep), until none is left."""
    p = np.asarray(p, float)
    while len(p) > 3:
        prev, nxt = np.roll(p, 1, axis=0), np.roll(p, -1, axis=0)
        ab = nxt - prev
        length = np.hypot(ab[:, 0], ab[:, 1])
        d = np.abs(ab[:, 0] * (p[:, 1] - prev[:, 1]) - ab[:, 1] * (p[:, 0] - prev[:, 0])) / np.maximum(length, 1e-9)
        d[length < 1e-9] = 0.0
        k = int(np.argmin(d))
        if d[k] > tol:
            break
        p = np.delete(p, k, axis=0)
    return p


def loop_area(p) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def footprint(tris, z0=None, cell=None, max_lines=FOOT_BUDGET, seed=7) -> list:
    """Outline loops [(N, 3) array of x, y, z] of everything at or above the main ground, seen from
    above, at the height of the ground along it.  tris: (N, 3, 3) map-local triangles."""
    tris = np.asarray(tris, np.float64)
    if len(tris) == 0:
        return []
    if z0 is None:
        z0 = ground_level(tris)
        if z0 is None:
            return []
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    n = np.cross(b - a, c - a)
    nl = np.linalg.norm(n, axis=1)
    up = (nl > 1e-9) & (n[:, 2] > 0.7 * np.maximum(nl, 1e-12))
    proj = np.abs(n[:, 2]) / 2
    lo_xy, hi_xy = tris[:, :, :2].reshape(-1, 2).min(0), tris[:, :, :2].reshape(-1, 2).max(0)
    if cell is None:
        cell = max(0.5, float(max(hi_xy - lo_xy)) / 500.0)
    # points on every triangle (more on big ones), near its corners too so thin walls count; the
    # corners themselves are pulled a little inwards: a corner exactly on a cell border would mark
    # the cell beyond the edge
    rng = np.random.default_rng(seed)
    k = np.minimum(1 + np.floor(proj / (cell * cell) * 3.0).astype(np.int64), 4000)
    idx = np.repeat(np.arange(len(tris)), k)
    r1, r2 = rng.random(len(idx)), rng.random(len(idx))
    flip = r1 + r2 > 1
    r1[flip], r2[flip] = 1 - r1[flip], 1 - r2[flip]
    r1, r2 = 0.01 + 0.98 * r1, 0.01 + 0.98 * r2
    cen = (a + b + c) / 3
    pts = np.concatenate([a[idx] + r1[:, None] * (b - a)[idx] + r2[:, None] * (c - a)[idx],
                          a + 0.02 * (cen - a), b + 0.02 * (cen - b), c + 0.02 * (cen - c)])
    upf = np.concatenate([up[idx], up, up, up])
    keep = pts[:, 2] >= z0 - BAND
    pts, upf = pts[keep], upf[keep]
    if len(pts) == 0:
        return []
    org = pts[:, :2].min(0) - 3 * cell
    size = np.ceil((pts[:, :2].max(0) + 3 * cell - org) / cell).astype(int) + 1
    ix = ((pts[:, 0] - org[0]) / cell).astype(int)
    iy = ((pts[:, 1] - org[1]) / cell).astype(int)
    occ = np.zeros(tuple(size), bool)
    occ[ix, iy] = True
    ground = np.full(tuple(size), np.inf)
    np.minimum.at(ground, (ix[upf], iy[upf]), pts[upf, 2])
    top = np.full(tuple(size), -np.inf)
    np.maximum.at(top, (ix, iy), pts[:, 2])
    # close gaps of a cell or two (between samples, between buildings and the ground), take off
    # one-cell spikes (a sample just past an edge), fill holes
    solid = _erode(_erode(_dilate(_dilate(occ))))
    solid = _dilate(_erode(solid))
    solid[0, :] = solid[-1, :] = solid[:, 0] = solid[:, -1] = False
    solid = _fill_holes(solid)
    loops = boundary_loops(solid)
    # the ground's height along the edge: the lowest upward face in the cell; a cell with only
    # walls in it takes their top (kept near the ground); a cell the clean-up filled has none
    zg = np.where(np.isfinite(ground), ground,
                  np.where(np.isfinite(top), np.clip(top, z0 - BAND, z0 + 1.0), np.nan))

    def z_at(i, j):
        vals = [zg[p, q] for p, q in ((i - 1, j - 1), (i, j - 1), (i - 1, j), (i, j))
                if 0 <= p < size[0] and 0 <= q < size[1] and solid[p, q] and np.isfinite(zg[p, q])]
        return float(np.median(vals)) if vals else float(z0)

    tol = max(0.75, cell * 1.5)
    for _ in range(8):
        out, total = [], 0
        for lp in loops:
            p = prune_loop(simplify_loop(lp, tol / cell), tol / cell)      # grid units
            if len(p) < 3 or abs(loop_area(p)) * cell * cell < 6.0:
                continue
            xyz = np.array([[org[0] + i * cell, org[1] + j * cell, z_at(int(round(i)), int(round(j)))] for i, j in p])
            out.append(xyz)
            total += len(xyz)
        if total <= max_lines:
            break
        tol *= 1.6
    out.sort(key=lambda p: -abs(loop_area(p)))
    kept, total = [], 0
    for p in out:
        if total + len(p) > max_lines:
            break
        kept.append(p)
        total += len(p)
    return kept


# ------------------------------------------------------------------ gates and buildings
def _is_gate(name: str, door: dict | None) -> bool:
    if not door:
        return False
    return "gate" in (name or "").lower() or float(door.get("width") or 0) >= GATE_WIDTH


def gate_segments(model) -> list:
    """Each gate leaf as a rectangle (bottom, top, two posts), map-local."""
    out = []
    for pl in model.placements:
        part = model.parts.get(pl.part)
        if part is None or part.kind != "door" or not _is_gate(pl.part, part.door):
            continue
        d = part.door
        r, t = _placement_matrix(pl)
        ax = np.array([1.0, 0.0, 0.0]) if d.get("axis", "x") == "x" else np.array([0.0, 1.0, 0.0])
        cx, cy = (list(d.get("center") or [0.0, 0.0]) + [0.0, 0.0])[:2]
        mid = np.array([float(cx), float(cy), 0.0])
        half = ax * float(d.get("width") or 1.0) / 2
        z0, z1 = float(d.get("zmin") or 0.0), float(d.get("zmax") or 2.0)
        p = [mid - half + [0, 0, z0], mid + half + [0, 0, z0], mid + half + [0, 0, z1], mid - half + [0, 0, z1]]
        q = [r @ v + t for v in p]
        out += [(q[0], q[1]), (q[3], q[2]), (q[0], q[3]), (q[1], q[2])]
    return out


def _buildings(model, foot_area: float) -> list:
    """(volume, segments, part kind) for each placed building or big object, biggest first.  A part
    whose mesh was cut into render-only pieces (``piece_of``) is one building with their bounds."""
    pieces = {}
    for p in model.parts.values():
        if getattr(p, "piece_of", None):
            pieces.setdefault(p.piece_of, []).append(p)
    seen, out = set(), []
    for pl in model.placements:
        part = model.parts.get(pl.part)
        if part is None or part.collision_only or part.kind in ("chunk", "door") or getattr(part, "piece_of", None):
            continue
        mn, mx = (np.asarray(v, float) for v in part.bounds())
        for p in pieces.get(pl.part, ()):
            pmn, pmx = (np.asarray(v, float) for v in p.bounds())
            mn, mx = np.minimum(mn, pmn), np.maximum(mx, pmx)
        ex, ey, ez = mx - mn
        if max(ex, ey) < 2.5 or ez < 1.0:
            continue                                   # props: lamps, benches, plants
        if foot_area > 0 and ex * ey > 0.4 * foot_area:
            continue                                   # the ground itself: the footprint shows it
        r, t = _placement_matrix(pl)
        key = (pl.part, tuple(np.round(t, 1)), round(pl.yaw % 360, 0))
        if key in seen:
            continue
        seen.add(key)
        segs = [(r @ p + t, r @ q + t) for p, q in aabb_lines(mn, mx)]
        out.append((ex * ey * ez, segs, part.kind))
    out.sort(key=lambda b: -b[0])
    return out


def footprint_triangles(model, max_tris: int = 400_000) -> np.ndarray:
    """(N, 3, 3) map-local triangles of what the footprint is traced from: every placed part that
    can be seen, its most detailed LOD, without door leaves and gates (drawn as gates)."""
    local, count = {}, {}
    for pl in model.placements:
        count[pl.part] = count.get(pl.part, 0) + 1
    for name, part in model.parts.items():
        if part.collision_only or part.kind == "door" or name not in count:
            continue
        subs = [sm for sm in part.submeshes if len(sm.indices)]
        if not subs:
            continue
        best = min(getattr(sm, "lod", 1) for sm in subs)
        t = [np.asarray(sm.positions, np.float64)[np.asarray(sm.indices, np.int64).reshape(-1, 3)]
             for sm in subs if getattr(sm, "lod", 1) == best]
        local[name] = np.concatenate(t)
    total = sum(len(local[n]) * count[n] for n in local)
    if total > max_tris:
        keep = max_tris / total
        for name, t in list(local.items()):
            n = max(1, int(len(t) * keep))
            if n < len(t):
                area = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
                local[name] = t[np.argpartition(-area, n - 1)[:n]]
    out = []
    for pl in model.placements:
        t = local.get(pl.part)
        if t is None:
            continue
        r, p = _placement_matrix(pl)
        out.append(t @ r.T + p)
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def ghost_lines(model, budget: int = DEFAULT_BUDGET, tris=None) -> list:
    """[(x1, y1, z1, x2, y2, z2, kind)] in map-local coordinates (anchor at 0, yaw 0), most useful
    first: footprint, gates, bounds, buildings, walls."""
    lines = []

    def add(segs, kind):
        for p, q in segs:
            if len(lines) >= budget:
                return
            lines.append((*(float(v) for v in p), *(float(v) for v in q), kind))

    if tris is None:
        tris = footprint_triangles(model)
    tris = np.asarray(tris, np.float64)
    loops = footprint(tris, max_lines=min(FOOT_BUDGET, budget // 3)) if len(tris) else []
    foot_area = sum(abs(loop_area(p)) for p in loops)
    for p in loops:
        add([(p[i], p[(i + 1) % len(p)]) for i in range(len(p))], KIND_FOOT)
    add(gate_segments(model)[:GATE_BUDGET], KIND_GATE)
    if len(tris):
        pts = tris.reshape(-1, 3)
        mn, mx = pts.min(0), pts.max(0)
    else:
        mn, mx = model.bounds()
    add(aabb_lines(mn, mx), KIND_BOUNDS)
    parts_used = 0
    for _, segs, _kind in _buildings(model, foot_area):
        if parts_used + len(segs) > PART_BUDGET:
            break
        add(segs, KIND_PART)
        parts_used += len(segs)
    # then walls and floors, round-robin over placements so every building gets some
    queues = []
    for pl in model.placements:
        part = model.parts[pl.part]
        if part.kind in ("chunk", "door"):
            continue
        boxes = _collider_boxes(part)
        if boxes:
            r, t = _placement_matrix(pl)
            queues.append([part.kind, r, t, boxes, 0])
    order = sorted(queues, key=lambda q: {"mlo": 0, "object": 1}.get(q[0], 2))
    progress = True
    while progress and len(lines) < budget:
        progress = False
        for q in order:
            kind, r, t, boxes, k = q
            if k >= len(boxes):
                continue
            _, c, ax, h = boxes[k]
            q[4] = k + 1
            segs = box_lines(c, ax, h)
            if len(lines) + len(segs) > budget:
                continue
            add([(r @ p + t, r @ q2 + t) for p, q2 in segs], KIND_WALL)
            progress = True
            if len(lines) >= budget:
                break
    return lines[:budget]


def encode(lines) -> str:
    """Compact text form for the Lua layout: 7 numbers per line, centimetre precision."""
    out = []
    for ln in lines:
        out.append(" ".join(f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v) for v in ln))
    return "\n".join(out)
