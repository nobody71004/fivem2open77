"""GTA collision -> Cyberpunk mesh colliders.

Cyberpunk static meshes carry their collision in a ``meshMeshParamPhysics`` parameter: a
physicsSystemResource with one static body whose shapes are boxes, spheres, capsules and convex
hulls.  Triangle meshes need PhysX-cooked data (``physicsColliderMesh`` has no raw vertex field),
but convex hulls are cooked by the engine at load time from plain vertices -- the vanilla vehicle
.phys files ship exactly that -- so triangle soup is turned into convex pieces here:

  1. triangles are welded and split into planar, edge-connected regions;
  2. each region is cut into convex polygons (greedy Hertel-Mehlhorn merge of its triangles);
  3. every convex polygon becomes a slab: the polygon plus a copy pushed ``thickness`` metres
     behind the surface (into the solid, against the face normal).  Rectangles become boxes,
     which need no cooking at all.

Dense collision (terrain-like ground) is first simplified by vertex clustering until the piece
count fits the budget.  GTA analytic primitives map 1:1 (box, sphere, capsule; cylinders become
16-sided convex prisms).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from ..gta.bounds import Collision, material_name

# GTA material family -> Cyberpunk physics material (base\physics\physicsmaterials.physmatlib)
_PHYSMAT_RULES = [
    ("GLASS", "glass.physmat"), ("PERSPEX", "plexiglass.physmat"), ("TARMAC", "tarmac.physmat"),
    ("RUMBLE", "asphalt.physmat"), ("CONCRETE", "concrete.physmat"), ("BREEZE", "concrete.physmat"),
    ("PAVING", "concrete.physmat"), ("BRICK", "brick.physmat"), ("ROCK", "stone.physmat"),
    ("STONE", "stone.physmat"), ("COBBLE", "stone.physmat"), ("MARBLE", "stone.physmat"),
    ("SAND", "sand.physmat"), ("GRAVEL", "dirt.physmat"), ("DIRT", "dirt.physmat"), ("MUD", "mud.physmat"),
    ("MARSH", "mud.physmat"), ("SOIL", "dirt.physmat"), ("CLAY", "dirt.physmat"), ("SNOW", "sand.physmat"),
    ("ICE", "glass.physmat"), ("GRASS", "grass.physmat"), ("HAY", "grass.physmat"), ("BUSH", "leaves.physmat"),
    ("TWIG", "leaves.physmat"), ("LEAVES", "leaves.physmat"), ("WOODCHIP", "dirt.physmat"),
    ("TREE_BARK", "wood_tree.physmat"), ("METAL_CHAINLINK", "metal_semitransparent.physmat"),
    ("METAL_GRILLE", "metal_catwalk.physmat"), ("METAL_HOLLOW", "metal_hollow.physmat"),
    ("METAL", "metal.physmat"), ("WOOD", "wood.physmat"), ("CERAMIC", "ceramic.physmat"),
    ("ROOF_TILE", "tiles.physmat"), ("ROOF_FELT", "plaster.physmat"), ("FIBREGLASS", "plastic.physmat"),
    ("TARPAULIN", "fabrics.physmat"), ("PLASTIC", "plastic.physmat"), ("RUBBER", "rubber.physmat"),
    ("LINOLEUM", "linoleum.physmat"), ("LAMINATE", "linoleum.physmat"), ("CARPET", "carpet.physmat"),
    ("CLOTH", "fabrics.physmat"), ("PLASTER", "plaster.physmat"), ("CARDBOARD", "cardboard.physmat"),
    ("PAPER", "paper.physmat"), ("FOAM", "furniture_upholstery.physmat"),
    ("FEATHER", "furniture_upholstery.physmat"), ("POLYSTYRENE", "plastic.physmat"),
    ("LEATHER", "furniture_leather.physmat"), ("TVSCREEN", "glass_electronics.physmat"),
    ("SLATTED", "plastic.physmat"), ("WATER", "water.physmat"), ("EMISSIVE", "neon.physmat"),
]
DEFAULT_PHYSMAT = "concrete.physmat"


def physmat_for(gta_material_index: int) -> str:
    name = material_name(int(gta_material_index))
    for key, pm in _PHYSMAT_RULES:
        if key in name:
            return pm
    return DEFAULT_PHYSMAT


@dataclass
class Collider:
    kind: str                     # box | sphere | capsule | convex
    physmat: str
    center: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (0.0, 0.0, 0.0, 1.0)   # quaternion x, y, z, w
    half: tuple = (0.0, 0.0, 0.0)            # box half extents
    radius: float = 0.0
    height: float = 0.0                      # capsule: distance between the two sphere centres
    vertices: np.ndarray | None = None       # convex hull points
    source: str = ""                         # what it came from: an object name, "collision", "glass"


def _quat_from_axes(ax: np.ndarray):
    """Rows of ax = unit x, y, z axes of the local frame -> quaternion (x, y, z, w)."""
    m = np.asarray(ax, float).T   # columns = axes
    # orthonormalise (Gram-Schmidt) and keep right-handed
    x = m[:, 0] / (np.linalg.norm(m[:, 0]) or 1)
    y = m[:, 1] - x * np.dot(x, m[:, 1])
    y = y / (np.linalg.norm(y) or 1)
    z = np.cross(x, y)
    m = np.stack([x, y, z], 1)
    t = m[0, 0] + m[1, 1] + m[2, 2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)
    q = np.array(q, float)
    q /= np.linalg.norm(q)
    return tuple(float(v) for v in q)


# ----------------------------------------------------------------------------- triangle soup
def _weld(tris: np.ndarray, tol: float):
    """-> (vertices (V,3), faces (F,3) int) with vertices merged on a grid of size tol."""
    pts = tris.reshape(-1, 3)
    key = np.round(pts / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = pts[first]
    faces = inv.reshape(-1, 3)
    ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return verts, faces, ok


def _face_planes(verts, faces):
    p0, p1, p2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    n = np.cross(p1 - p0, p2 - p0)
    area2 = np.linalg.norm(n, axis=1)
    n = n / np.maximum(area2, 1e-12)[:, None]
    d = (n * p0).sum(1)
    return n, d, area2 * 0.5


def _regions(verts, faces, normals, dists, angle_tol_deg, dist_tol):
    """Edge-connected, (nearly) coplanar regions -> list of face index lists."""
    edge_faces = defaultdict(list)
    for fi, (a, b, c) in enumerate(faces):
        for u, v in ((a, b), (b, c), (c, a)):
            edge_faces[(u, v) if u < v else (v, u)].append(fi)
    cos_tol = math.cos(math.radians(angle_tol_deg))
    seen = np.zeros(len(faces), bool)
    regions = []
    for start in range(len(faces)):
        if seen[start]:
            continue
        n0, d0 = normals[start], dists[start]
        stack = [start]
        seen[start] = True
        region = []
        while stack:
            f = stack.pop()
            region.append(f)
            a, b, c = faces[f]
            for u, v in ((a, b), (b, c), (c, a)):
                for g in edge_faces[(u, v) if u < v else (v, u)]:
                    if seen[g]:
                        continue
                    if np.dot(normals[g], n0) < cos_tol:
                        continue
                    pg = verts[faces[g]]
                    if np.abs(pg @ n0 - d0).max() > dist_tol:
                        continue
                    seen[g] = True
                    stack.append(g)
        regions.append(region)
    return regions


def _is_convex(poly2d: np.ndarray, eps=1e-9) -> bool:
    n = len(poly2d)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        a, b, c = poly2d[i], poly2d[(i + 1) % n], poly2d[(i + 2) % n]
        cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cr) <= eps:
            continue
        s = 1 if cr > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return sign != 0


def _drop_collinear(loop, pts2d, eps=1e-7):
    out = list(loop)
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, b, c = pts2d[out[i - 1]], pts2d[out[i]], pts2d[out[(i + 1) % len(out)]]
            cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cr) <= eps:
                out.pop(i)
                changed = True
                break
    return out


def _convex_pieces(verts, faces, region, normal, max_verts):
    """Greedy merge of a planar region's triangles into convex polygons (vertex index loops)."""
    # 2D frame in the plane
    n = normal / np.linalg.norm(normal)
    u = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    used = sorted({int(x) for f in region for x in faces[f]})
    pts2d = {i: (float(verts[i] @ u), float(verts[i] @ v)) for i in used}
    polys = {}
    for k, f in enumerate(region):
        a, b, c = (int(x) for x in faces[f])
        loop = [a, b, c]
        p = np.array([pts2d[x] for x in loop])
        area = (p[1, 0] - p[0, 0]) * (p[2, 1] - p[0, 1]) - (p[1, 1] - p[0, 1]) * (p[2, 0] - p[0, 0])
        if area < 0:
            loop = [a, c, b]
        polys[k] = loop
    edge_owner = {}
    for k, loop in polys.items():
        for i in range(len(loop)):
            edge_owner[(loop[i], loop[(i + 1) % len(loop)])] = k
    merged = True
    while merged:
        merged = False
        for k in list(polys.keys()):
            if k not in polys:
                continue
            loop = polys[k]
            for i in range(len(loop)):
                a, b = loop[i], loop[(i + 1) % len(loop)]
                other = edge_owner.get((b, a))
                if other is None or other == k or other not in polys:
                    continue
                ol = polys[other]
                if b not in ol:
                    continue        # stale edge entry (the edge was merged away)
                j = ol.index(b)
                if ol[(j + 1) % len(ol)] != a:
                    continue
                # splice: loop ... a | (other from a around to b) | b ...
                new = loop[:i + 1] + [ol[(j + 2 + t) % len(ol)] for t in range(len(ol) - 2)] + loop[i + 1:]
                if len(set(new)) != len(new):
                    continue
                new = _drop_collinear(new, pts2d)
                if len(new) > max_verts:
                    continue
                if not _is_convex(np.array([pts2d[x] for x in new])):
                    continue
                polys[k] = new
                del polys[other]
                for t in range(len(new)):
                    edge_owner[(new[t], new[(t + 1) % len(new)])] = k
                merged = True
                break
    return list(polys.values()), pts2d, (u, v, n)


def _slab(points3d: np.ndarray, normal: np.ndarray, thickness: float):
    back = points3d - normal * thickness
    return np.concatenate([points3d, back])


def _rect_box(loop_pts: np.ndarray, normal: np.ndarray, thickness: float, slack=1.06, max_extra=None):
    """A planar convex polygon that (nearly) fills its minimum-area bounding rectangle ->
    (center, quat, half) of the slab box; otherwise None.  Boxes need no PhysX cooking."""
    if len(loop_pts) < 3:
        return None
    n = normal / (np.linalg.norm(normal) or 1.0)
    a = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    a /= np.linalg.norm(a)
    b = np.cross(n, a)
    p2 = np.stack([loop_pts @ a, loop_pts @ b], 1)
    x, y = p2[:, 0], p2[:, 1]
    area = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    if area < 1e-6:
        return None
    best = None
    for i in range(len(p2)):
        e = p2[(i + 1) % len(p2)] - p2[i]
        ln = np.hypot(e[0], e[1])
        if ln < 1e-6:
            continue
        ex = e / ln
        ey = np.array([-ex[1], ex[0]])
        px, py = p2 @ ex, p2 @ ey
        ra = (px.max() - px.min()) * (py.max() - py.min())
        if best is None or ra < best[0]:
            best = (ra, ex, ey, px.min(), px.max(), py.min(), py.max())
    if best is None or best[0] > area * slack or (max_extra and best[0] - area > max_extra):
        return None
    ra, ex, ey, x0, x1, y0, y1 = best
    ux = a * ex[0] + b * ex[1]
    uy = a * ey[0] + b * ey[1]
    if np.dot(np.cross(ux, uy), n) < 0:
        uy = -uy
        y0, y1 = -y1, -y0
    off = float(np.mean(loop_pts @ n))
    center = n * off + ux * ((x0 + x1) * 0.5) + uy * ((y0 + y1) * 0.5) - n * (thickness * 0.5)
    return center, _quat_from_axes(np.stack([ux, uy, np.cross(ux, uy)])), ((x1 - x0) * 0.5, (y1 - y0) * 0.5, thickness * 0.5)


def _hull2d(points):
    """Monotone-chain convex hull of 2D points -> indices in CCW order."""
    pts = sorted(range(len(points)), key=lambda i: (points[i][0], points[i][1]))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (points[a][0] - points[o][0]) * (points[b][1] - points[o][1]) - \
               (points[a][1] - points[o][1]) * (points[b][0] - points[o][0])

    lower, upper = [], []
    for i in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], i) <= 1e-12:
            lower.pop()
        lower.append(i)
    for i in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], i) <= 1e-12:
            upper.pop()
        upper.append(i)
    return lower[:-1] + upper[:-1]


def _poly_area(p2):
    x, y = p2[:, 0], p2[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


# ----------------------------------------------------------------------------- boxes only
# A convex collider inside a mesh needs PhysX-cooked data: vanilla meshes ship it in
# ``compiledGeometryBuffer`` (no vertices), and a vertices-only convex makes the game crash in
# PhysX the moment the mesh's collision loads (measured: Cedar Crest, 2026-10-08).  Boxes, spheres
# and capsules need no cooking, so every hull is turned into boxes: a flat hull (a slab) into a
# few strips that cover its polygon, a solid hull into its tightest upright (or PCA) box.

def _min_area_rect(p2: np.ndarray):
    """(area, ex, ey, x0, x1, y0, y1) of the minimum-area rectangle around 2D points; ey is ex
    turned +90 degrees, so (ex, ey) keeps the orientation of the input frame."""
    hull = p2[_hull2d(p2)] if len(p2) >= 3 else p2
    if len(hull) < 2:
        hull = p2
    dirs = []
    for i in range(len(hull)):
        e = hull[(i + 1) % len(hull)] - hull[i]
        ln = float(np.hypot(e[0], e[1]))
        if ln > 1e-9:
            dirs.append(e / ln)
    if not dirs:
        dirs = [np.array([1.0, 0.0])]
    best = None
    for ex in dirs:
        ey = np.array([-ex[1], ex[0]])
        px, py = p2 @ ex, p2 @ ey
        ra = float((px.max() - px.min()) * (py.max() - py.min()))
        if best is None or ra < best[0] - 1e-12:
            best = (ra, ex, ey, float(px.min()), float(px.max()), float(py.min()), float(py.max()))
    return best


def _clip_x(poly: np.ndarray, xa: float, xb: float) -> np.ndarray:
    """Convex polygon (CCW, 2D) clipped to xa <= x <= xb (Sutherland-Hodgman)."""
    def clip(pts, keep, cut):
        out = []
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            ka, kb = keep(a), keep(b)
            if ka:
                out.append(a)
            if ka != kb:
                t = (cut - a[0]) / (b[0] - a[0])
                out.append(a + (b - a) * t)
        return out
    pts = list(poly)
    pts = clip(pts, lambda q: q[0] >= xa, xa) if pts else pts
    pts = clip(pts, lambda q: q[0] <= xb, xb) if pts else pts
    return np.array(pts) if pts else np.zeros((0, 2))


def _plane_basis(n: np.ndarray):
    a = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    a /= np.linalg.norm(a)
    return a, np.cross(n, a)


def polygon_boxes(points3d: np.ndarray, normal, thickness: float, physmat: str, *, max_boxes=4,
                  slack=1.25, top=True, max_extra=None) -> list:
    """Boxes covering a planar convex polygon given by its (coplanar) points.  The slab is
    ``thickness`` deep behind the plane (top=True: the plane is the walkable/visible face) or
    centred on it (top=False).  The strips spill past slanted edges; ``max_extra`` (m2) bounds
    that spill per strip (up to ``max_boxes`` strips) instead of the default 25 % rule, for
    places where a spill can close a passage (interiors)."""
    n = np.asarray(normal, float)
    n = n / (np.linalg.norm(n) or 1.0)
    p3 = np.asarray(points3d, float)
    if len(p3) < 3 or not np.isfinite(p3).all():
        return []
    a, b = _plane_basis(n)
    off = float(np.mean(p3 @ n))
    p2 = np.stack([p3 @ a, p3 @ b], 1)
    idx = _hull2d(p2)
    if len(idx) < 3:
        return []
    hull = p2[idx]
    area = _poly_area(hull)
    if area < 1e-6:
        return []
    ra, ex, ey, x0, x1, y0, y1 = _min_area_rect(hull)
    ux, uy = a * ex[0] + b * ex[1], a * ey[0] + b * ey[1]
    q = _quat_from_axes(np.stack([ux, uy, n]))
    local = np.stack([hull @ ex, hull @ ey], 1)
    if max_extra:
        k = int(min(max_boxes, max(1, math.ceil((ra - area) / max_extra))))
    else:
        k = 1 if ra <= area * slack else int(min(max_boxes, max(2, math.ceil((ra / area - 1.0) * 4))))
    xs = np.linspace(x0, x1, k + 1)
    depth = max(float(thickness), 0.02)
    shift = -depth * 0.5 if top else 0.0
    out = []
    for i in range(k):
        piece = local if k == 1 else _clip_x(local, xs[i], xs[i + 1])
        if len(piece) < 3:
            continue
        xa, xb = (x0, x1) if k == 1 else (float(xs[i]), float(xs[i + 1]))
        ya, yb = float(piece[:, 1].min()), float(piece[:, 1].max())
        if xb - xa < 1e-3 or yb - ya < 1e-3:
            continue
        c = n * (off + shift) + ux * ((xa + xb) * 0.5) + uy * ((ya + yb) * 0.5)
        out.append(Collider("box", physmat, center=tuple(float(v) for v in c), rotation=q,
                            half=(max((xb - xa) * 0.5, 0.01), max((yb - ya) * 0.5, 0.01), depth * 0.5)))
    return out


def solid_box(points: np.ndarray, physmat: str, min_half=0.02) -> Collider | None:
    """Tightest of: the upright box (minimum-area rectangle seen from above x height) and the
    principal-axes box around the points."""
    p = np.asarray(points, float)
    p = p[np.isfinite(p).all(1)]
    if len(p) < 2:
        return None
    ra, ex, ey, x0, x1, y0, y1 = _min_area_rect(p[:, :2])
    z0, z1 = float(p[:, 2].min()), float(p[:, 2].max())
    up_vol = max(ra, 1e-6) * max(z1 - z0, 1e-3)
    ux = np.array([ex[0], ex[1], 0.0])
    uy = np.array([ey[0], ey[1], 0.0])
    best = (up_vol, np.stack([ux, uy, [0.0, 0.0, 1.0]]), np.array([x0, y0, z0]), np.array([x1, y1, z1]))
    c0 = p.mean(0)
    try:
        _, vecs = np.linalg.eigh(np.cov((p - c0).T))
        ax = vecs.T[::-1]
        if np.linalg.det(ax) < 0:
            ax[2] = -ax[2]
        loc = p @ ax.T
        lo, hi = loc.min(0), loc.max(0)
        vol = float(np.prod(np.maximum(hi - lo, 1e-3)))
        if vol < best[0] * 0.8:
            best = (vol, ax, lo, hi)
    except (np.linalg.LinAlgError, ValueError):
        pass
    _, ax, lo, hi = best
    mid = (np.asarray(lo) + np.asarray(hi)) * 0.5
    center = ax.T @ mid
    half = np.maximum((np.asarray(hi) - np.asarray(lo)) * 0.5, min_half)
    return Collider("box", physmat, center=tuple(float(v) for v in center), rotation=_quat_from_axes(ax),
                    half=tuple(float(v) for v in half))


def convex_to_boxes(c: Collider, *, max_boxes=4) -> list:
    """Any hull -> boxes: flat hulls (one extent much smaller than the others) -> polygon_boxes
    on their mid-plane, solid ones -> solid_box."""
    p = np.asarray(c.vertices, float)
    if len(p) < 3:
        return []
    c0 = p.mean(0)
    _, vecs = np.linalg.eigh(np.cov((p - c0).T))
    ax = vecs.T[::-1]
    loc = (p - c0) @ ax.T
    ext = loc.max(0) - loc.min(0)
    if ext[2] <= 0.6 and ext[2] < 0.3 * ext[1]:
        n = ax[2]
        mid = c0 + n * float((loc[:, 2].max() + loc[:, 2].min()) * 0.5)
        flat = p - np.outer((p - mid) @ n, n)
        return polygon_boxes(flat, n, float(ext[2]), c.physmat, max_boxes=max_boxes, top=False)
    b = solid_box(p, c.physmat)
    return [b] if b is not None else []


def boxify(colliders: list) -> list:
    out = []
    for c in colliders:
        if c.kind == "convex":
            out.extend(convex_to_boxes(c))
        else:
            out.append(c)
    return out


def _hull_filled(verts, faces, u, v, hull2d, extra, max_samples=2500) -> bool:
    """Whether a planar region's triangles cover its 2D hull but for ``extra`` m2: a grid of
    samples inside the hull, each looked up in the triangles.  Overlapping triangles (two
    triangulations of one face) can make the area test pass for a region with a hole in it."""
    hull2d = np.asarray(hull2d, float)
    area = _poly_area(hull2d)
    if area <= 1e-6:
        return True
    lo, hi = hull2d.min(0), hull2d.max(0)
    cell = max(0.08, math.sqrt(float((hi - lo).prod()) / max_samples))
    xs = np.arange(lo[0] + cell * 0.5, hi[0], cell)
    ys = np.arange(lo[1] + cell * 0.5, hi[1], cell)
    if not len(xs) or not len(ys):
        return True
    S = np.stack(np.meshgrid(xs, ys, indexing="ij"), -1).reshape(-1, 2)
    # inside the (convex, counter-clockwise) hull
    inside = np.ones(len(S), bool)
    for i in range(len(hull2d)):
        a, b = hull2d[i], hull2d[(i + 1) % len(hull2d)]
        inside &= (b[0] - a[0]) * (S[:, 1] - a[1]) - (b[1] - a[1]) * (S[:, 0] - a[0]) >= -1e-9
    S = S[inside]
    if not len(S):
        return True
    P = np.stack([verts @ u, verts @ v], 1)
    T = P[np.asarray(faces)]
    covered = np.zeros(len(S), bool)
    for k in range(0, len(T), 1024):
        t = T[k:k + 1024]
        a, b, c = t[:, 0][None], t[:, 1][None], t[:, 2][None]
        p = S[~covered][:, None, :]
        if not len(p):
            break
        d1 = (p[..., 0] - b[..., 0]) * (a[..., 1] - b[..., 1]) - (a[..., 0] - b[..., 0]) * (p[..., 1] - b[..., 1])
        d2 = (p[..., 0] - c[..., 0]) * (b[..., 1] - c[..., 1]) - (b[..., 0] - c[..., 0]) * (p[..., 1] - c[..., 1])
        d3 = (p[..., 0] - a[..., 0]) * (c[..., 1] - a[..., 1]) - (c[..., 0] - a[..., 0]) * (p[..., 1] - a[..., 1])
        eps = 1e-6
        neg = (d1 < -eps) | (d2 < -eps) | (d3 < -eps)
        pos = (d1 > eps) | (d2 > eps) | (d3 > eps)
        hit = (~(neg & pos)).any(1)
        idx = np.nonzero(~covered)[0]
        covered[idx[hit]] = True
    return float((~covered).sum()) * cell * cell <= max(extra, cell * cell)


def triangles_to_colliders(tris: np.ndarray, mats: np.ndarray, *, thickness=0.15, weld=0.005,
                           angle_tol=1.5, dist_tol=0.01, max_verts=24, min_area=0.0, min_extent=0.0,
                           hull_slack=1.12, hull_extra=0.2, skip_ceilings=True, max_extra=None,
                           max_boxes=4) -> list:
    """Planar regions -> convex slabs.  A region whose 2D convex hull is at most ``hull_slack``
    times its own area becomes one slab (its hull); others are cut into convex pieces."""
    if not len(tris):
        return []
    verts, faces, ok = _weld(np.asarray(tris, float), weld)
    fmats = np.asarray(mats)[ok] if len(mats) else np.zeros(int(ok.sum()), np.int32)
    faces = faces[ok]
    if not len(faces):
        return []
    # the same face twice counts once (collision children that overlap -- a copy of an entrance
    # hall over the whole interior's collision -- or a face drawn from both sides): doubled area
    # made a wall with a doorway in it pass the hull test below as a filled rectangle, and the
    # slab closed the doorway (Sea Dome's main entrance)
    _, first = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
    first.sort()
    faces, fmats = faces[first], fmats[first]
    normals, dists, areas = _face_planes(verts, faces)
    keep = areas > 1e-6
    if skip_ceilings:
        keep &= normals[:, 2] > -0.7
    faces, fmats, normals, dists, areas = faces[keep], fmats[keep], normals[keep], dists[keep], areas[keep]
    out = []
    for region in _regions(verts, faces, normals, dists, angle_tol, dist_tol):
        reg_area = float(areas[region].sum())
        if reg_area < min_area:
            continue
        rv = verts[np.unique(faces[region].reshape(-1))]
        if min_extent and float((rv.max(0) - rv.min(0)).max()) < min_extent:
            continue
        w = areas[region]
        nrm = (normals[region] * w[:, None]).sum(0)
        nrm /= np.linalg.norm(nrm) or 1.0
        mat = int(np.bincount(fmats[region].astype(np.int64)).argmax()) if len(region) else 0
        pm = physmat_for(mat)
        # nearly convex region: one slab from its hull
        u = np.cross(nrm, [0.0, 0.0, 1.0]) if abs(nrm[2]) < 0.9 else np.cross(nrm, [1.0, 0.0, 0.0])
        u /= np.linalg.norm(u)
        v = np.cross(nrm, u)
        p2 = np.stack([rv @ u, rv @ v], 1)
        hull = _hull2d(p2)
        # the hull may only add a sliver: a doorway, a window or a stairwell inside a big wall or
        # floor is a small share of its area but must stay open
        if len(region) > 1 and len(hull) >= 3 and len(hull) <= max_verts and \
                _poly_area(p2[hull]) <= min(reg_area * hull_slack, reg_area + hull_extra) and \
                (len(region) <= 2 or _hull_filled(verts, faces[region], u, v, p2[hull], hull_extra)):
            loops = [None]
            hp = rv[hull]
            hp = hp + nrm * (np.dot(nrm, rv.mean(0)) - hp @ nrm)[:, None] * 0   # already planar
            pieces = [hp]
        else:
            loops, _, _ = _convex_pieces(verts, faces, region, nrm, max_verts)
            pieces = [verts[loop] for loop in loops]
        for p in pieces:
            box = _rect_box(p, nrm, thickness, max_extra=max_extra)
            if box is not None:
                c, q, h = box
                out.append(Collider("box", pm, center=tuple(c), rotation=q, half=tuple(h)))
            else:
                out.extend(polygon_boxes(p, nrm, thickness, pm, max_extra=max_extra, max_boxes=max_boxes))
    return out


def _cylinder_hull(a, b, r, sides=16):
    axis = b - a
    ln = np.linalg.norm(axis)
    if ln < 1e-6:
        return None
    n = axis / ln
    u = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    ring = [u * math.cos(2 * math.pi * k / sides) * r + v * math.sin(2 * math.pi * k / sides) * r for k in range(sides)]
    return np.array([a + p for p in ring] + [b + p for p in ring])


def primitive_colliders(col: Collision) -> list:
    out = []
    for c, ax, h, m in col.boxes:
        if min(h) <= 1e-4:
            h = np.maximum(np.asarray(h, float), 0.01)
        out.append(Collider("box", physmat_for(m), center=tuple(map(float, c)), rotation=_quat_from_axes(ax),
                            half=tuple(map(float, h))))
    for c, r, m in col.spheres:
        out.append(Collider("sphere", physmat_for(m), center=tuple(map(float, c)), radius=float(r)))
    for a, b, r, m in col.capsules:
        a, b = np.asarray(a, float), np.asarray(b, float)
        axis = b - a
        ln = float(np.linalg.norm(axis))
        if ln < 1e-5:
            out.append(Collider("sphere", physmat_for(m), center=tuple(a), radius=float(r)))
            continue
        # PhysX capsules run along their local X axis
        x = axis / ln
        y = np.cross([0.0, 0.0, 1.0], x) if abs(x[2]) < 0.9 else np.cross([0.0, 1.0, 0.0], x)
        y /= np.linalg.norm(y)
        z = np.cross(x, y)
        out.append(Collider("capsule", physmat_for(m), center=tuple((a + b) * 0.5),
                            rotation=_quat_from_axes(np.stack([x, y, z])), radius=float(r), height=ln))
    for a, b, r, m in col.cylinders:
        hull = _cylinder_hull(np.asarray(a, float), np.asarray(b, float), float(r))
        if hull is not None:
            box = solid_box(hull, physmat_for(m))
            if box is not None:
                out.append(box)
    return out


def support_hull_collider(points: np.ndarray, physmat: str, max_vertices=48) -> Collider | None:
    """One convex hull around a (small, solid) object: furniture, props."""
    from ..cp77.phys import support_hull
    p = np.asarray(points, float)
    if len(p) < 4:
        return None
    ext = p.max(0) - p.min(0)
    if np.min(ext) < 0.02:
        # flat object: give it a minimal thickness so the hull is not degenerate
        k = int(np.argmin(ext))
        q = p.copy()
        q[:, k] += 0.02
        p = np.concatenate([p, q])
    return solid_box(support_hull(p, max_vertices), physmat)



# ----------------------------------------------------------------------------- planar union path
def _plane_groups(normals, dists, ang_bins=60.0, dist_bin=0.04):
    key_n = np.round(normals * ang_bins).astype(np.int64)
    key_d = np.round(dists / dist_bin).astype(np.int64)
    groups = defaultdict(list)
    for i in range(len(normals)):
        groups[(key_n[i, 0], key_n[i, 1], key_n[i, 2], key_d[i])].append(i)
    return list(groups.values())


def _plane_frame(normal, edge_dirs):
    """u, v in the plane, u aligned with the dominant edge direction (mod 90 degrees)."""
    n = normal / np.linalg.norm(normal)
    a = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    a /= np.linalg.norm(a)
    b = np.cross(n, a)
    if len(edge_dirs):
        x, y = edge_dirs @ a, edge_dirs @ b
        w = np.hypot(x, y)
        ang = np.arctan2(y, x)
        # dominant direction modulo 90 degrees (4x angle trick)
        c4, s4 = (w * np.cos(4 * ang)).sum(), (w * np.sin(4 * ang)).sum()
        if abs(c4) + abs(s4) > 1e-9:
            t = math.atan2(s4, c4) / 4.0
            a, b = a * math.cos(t) + b * math.sin(t), -a * math.sin(t) + b * math.cos(t)
    return a, b, n


def _rect_decompose(poly, tol):
    """Orthogonal polygon (axis-aligned in its frame) -> list of rectangles (x0, y0, x1, y1), or
    None when the polygon is not orthogonal."""
    from shapely.geometry import box as sbox
    rings = [np.asarray(poly.exterior.coords)] + [np.asarray(r.coords) for r in poly.interiors]
    xs, ys = [], []
    for r in rings:
        d = np.diff(r, axis=0)
        if np.any((np.abs(d[:, 0]) > tol) & (np.abs(d[:, 1]) > tol)):
            return None
        xs += list(r[:, 0])
        ys += list(r[:, 1])

    def uniq(vals):
        vals = sorted(vals)
        out = [vals[0]]
        for v in vals[1:]:
            if v - out[-1] > tol:
                out.append(v)
        return out

    gx, gy = uniq(xs), uniq(ys)
    if len(gx) * len(gy) > 40000:
        return None
    nx, ny = len(gx) - 1, len(gy) - 1
    inside = np.zeros((nx, ny), bool)
    prep = poly.buffer(tol * 0.5)
    from shapely.geometry import Point
    for i in range(nx):
        cx = (gx[i] + gx[i + 1]) * 0.5
        for j in range(ny):
            inside[i, j] = prep.contains(Point(cx, (gy[j] + gy[j + 1]) * 0.5))
    rects = []
    used = np.zeros_like(inside)
    for i in range(nx):
        for j in range(ny):
            if not inside[i, j] or used[i, j]:
                continue
            j2 = j
            while j2 + 1 < ny and inside[i, j2 + 1] and not used[i, j2 + 1]:
                j2 += 1
            i2 = i
            while i2 + 1 < nx and np.all(inside[i2 + 1, j:j2 + 1]) and not np.any(used[i2 + 1, j:j2 + 1]):
                i2 += 1
            used[i:i2 + 1, j:j2 + 1] = True
            rects.append((gx[i], gy[j], gx[i2 + 1], gy[j2 + 1]))
    return rects


def _convex_decompose(poly, max_verts):
    """Polygon (with holes) -> convex vertex loops (2D), by constrained triangulation and a greedy
    Hertel-Mehlhorn merge."""
    import shapely
    tri = shapely.constrained_delaunay_triangles(poly)
    pts, faces = [], []
    index = {}
    for t in getattr(tri, "geoms", []):
        c = list(t.exterior.coords)[:3]
        f = []
        for x, y in c:
            k = (round(x, 6), round(y, 6))
            if k not in index:
                index[k] = len(pts)
                pts.append((x, y, 0.0))
            f.append(index[k])
        faces.append(f)
    if not faces:
        return []
    verts = np.array(pts)
    faces = np.array(faces)
    loops, _, _ = _convex_pieces(verts, faces, list(range(len(faces))), np.array([0.0, 0.0, 1.0]), max_verts)
    return [verts[l][:, :2] for l in loops]


def planar_union_colliders(tris, mats, *, thickness=0.15, min_extent=0.35, simplify=0.01,
                           max_verts=32, skip_ceilings=True, ang_bins=60.0, dist_bin=0.04):
    """Coplanar triangles are unioned in their plane (removes T-junction fragmentation), tiny
    details are dropped, orthogonal shapes become boxes and the rest convex slabs."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    tris = np.asarray(tris, float)
    if not len(tris):
        return []
    e1, e2 = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    nrm = np.cross(e1, e2)
    area2 = np.linalg.norm(nrm, axis=1)
    ok = area2 > 2e-6
    nrm = nrm / np.maximum(area2, 1e-12)[:, None]
    if skip_ceilings:
        ok &= nrm[:, 2] > -0.7
    idx = np.nonzero(ok)[0]
    dists = (nrm[idx] * tris[idx, 0]).sum(1)
    out = []
    mats = np.asarray(mats) if len(mats) else np.zeros(len(tris), np.int32)
    for g in _plane_groups(nrm[idx], dists, ang_bins, dist_bin):
        sel = idx[g]
        w = area2[sel]
        n = (nrm[sel] * w[:, None]).sum(0)
        n /= np.linalg.norm(n) or 1.0
        d = float(((tris[sel].reshape(-1, 3) @ n)).mean())
        edges = np.concatenate([tris[sel, 1] - tris[sel, 0], tris[sel, 2] - tris[sel, 1], tris[sel, 0] - tris[sel, 2]])
        u, v, n = _plane_frame(n, edges)
        p2 = np.stack([tris[sel].reshape(-1, 3) @ u, tris[sel].reshape(-1, 3) @ v], -1).reshape(-1, 3, 2)
        polys = [Polygon(t) for t in p2]
        polys = [p for p in polys if p.is_valid and p.area > 1e-8]
        if not polys:
            continue
        shape = unary_union(polys).buffer(simplify * 0.5).buffer(-simplify * 0.5).simplify(simplify)
        pm = physmat_for(int(np.bincount(mats[sel].astype(np.int64)).argmax()))
        for poly in getattr(shape, "geoms", [shape]):
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            minx, miny, maxx, maxy = poly.bounds
            if max(maxx - minx, maxy - miny) < min_extent:
                continue
            origin = n * d

            def to3(x, y):
                return origin + u * x + v * y

            rects = _rect_decompose(poly, simplify)
            if rects is not None:
                for x0, y0, x1, y1 in rects:
                    if max(x1 - x0, y1 - y0) < min_extent * 0.5:
                        continue
                    c = to3((x0 + x1) * 0.5, (y0 + y1) * 0.5) - n * (thickness * 0.5)
                    out.append(Collider("box", pm, center=tuple(c), rotation=_quat_from_axes(np.stack([u, v, n])),
                                        half=((x1 - x0) * 0.5, (y1 - y0) * 0.5, thickness * 0.5)))
                continue
            for loop in _convex_decompose(poly, max_verts):
                p3 = np.array([to3(x, y) for x, y in loop])
                out.extend(polygon_boxes(p3, n, thickness, pm))
    return out


def build_colliders(col: Collision, *, budget=1500, thickness=0.15, prop_hull=False, fill=True) -> tuple:
    """Collision -> (colliders, note).  prop_hull: one convex hull for the whole triangle set
    (used for furniture and small props)."""
    out = primitive_colliders(col)
    note = {"triangles": int(len(col.triangles)), "primitives": len(out)}
    if not len(col.triangles):
        return out, note
    if prop_hull:
        mat = int(np.bincount(col.tri_materials.astype(np.int64)).argmax()) if len(col.tri_materials) else 0
        h = support_hull_collider(col.triangles.reshape(-1, 3), physmat_for(mat))
        if h is not None:
            out.append(h)
        note["mode"] = "hull"
        return out, note
    # coarser merging stops here: past 6 cm of plane tolerance, stair treads (15-20 cm apart) and
    # the floor round a stairwell merge into thick blocks you cannot climb or walk under.  A part
    # that needs more boxes than `budget` keeps them (parts spill into collision-only parts).
    params = [
        dict(weld=0.005, angle_tol=1.5, dist_tol=0.01, min_area=0.0),
        dict(weld=0.05, angle_tol=4.0, dist_tol=0.03, min_area=0.01),
        dict(weld=0.15, angle_tol=8.0, dist_tol=0.06, min_area=0.05),
    ]
    best = None
    for p in params:
        cols = triangles_to_colliders(col.triangles, col.tri_materials, thickness=max(thickness, p["dist_tol"] * 2),
                                      max_extra=0.15, max_boxes=16, **p)
        best = (cols, p)
        if len(cols) + len(out) <= budget:
            break
    cols, p = best
    note.update({"mode": "slabs", "pieces": len(cols), "boxes": sum(1 for c in cols if c.kind == "box"),
                 "simplification": p,
                 "over_budget": len(cols) + len(out) > budget})
    out = out + cols
    if fill:
        extra, info = fill_uncovered(out, col, thickness=thickness, max_extra=0.15)
        out += extra
        note.update(info)
    return out, note


# ----------------------------------------------------------------------------- terrain-like ground
def heightfield_colliders(tris: np.ndarray, mats: np.ndarray, *, spacing=0.5, tol=0.04, max_block=32,
                          thickness=0.4, min_up=0.35, layers=1, layer_gap=1.8) -> tuple:
    """Upward-facing collision surfaces sampled on a grid and merged into planar quads by a
    quadtree; flat quads become boxes, sloped ones slabs.  ``layers`` > 1: the floors under the top
    surface (a yard under a carport roof, a patio under a terrace: at least ``layer_gap`` lower)
    get heightfields of their own.  Returns (colliders, mask of the upward triangles, sampler of
    the top surface)."""
    tris = np.asarray(tris, float)

    def nothing(x, y):
        return np.full(np.shape(x), np.nan)

    if not len(tris):
        return [], np.zeros(0, bool), nothing
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1)
    nz = n[:, 2] / np.maximum(ln, 1e-12)
    up = (nz > min_up) & (ln > 1e-9)
    t = tris[up]
    if not len(t):
        return [], up, nothing
    mats = np.asarray(mats) if len(mats) else np.zeros(len(tris), np.int32)
    tm = mats[up]
    kw = dict(spacing=spacing, tol=tol, max_block=max_block, thickness=thickness)
    out, top = _heightfield_layer(t, tm, **kw)
    sample = top
    for _ in range(max(0, int(layers) - 1)):
        h = sample(t[:, :, 0].mean(1), t[:, :, 1].mean(1))
        lower = np.isfinite(h) & (t[:, :, 2].mean(1) < h - layer_gap)
        if not lower.any():
            break
        t, tm = t[lower], tm[lower]
        more, sample = _heightfield_layer(t, tm, **kw)
        out += more
    return out, up, top


def _heightfield_layer(t, tm, *, spacing, tol, max_block, thickness, step_slack=0.25, step_min=0.3):
    """One heightfield: the highest of the given upward triangles at every grid point.  A cell whose
    corners sit on different surfaces (a roof edge over the yard, the top and the foot of a wall)
    would make a ramp nobody built: such a cell (its plane at least ``step_slack`` steeper, in
    normal z, than the surfaces under its corners, and corners more than ``step_min`` apart in
    height) is left out; the fill pass covers the real surfaces on both sides."""
    tn = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    tnz = tn[:, 2] / np.maximum(np.linalg.norm(tn, axis=1), 1e-12)
    x0, y0 = t[:, :, 0].min() - spacing, t[:, :, 1].min() - spacing
    nx = int(math.ceil((t[:, :, 0].max() - x0) / spacing)) + 2
    ny = int(math.ceil((t[:, :, 1].max() - y0) / spacing)) + 2
    size = 1
    while size < max(nx, ny):
        size *= 2
    H = np.full((size + 1, size + 1), np.nan)
    M = np.zeros((size + 1, size + 1), np.int32)
    NZ = np.ones((size + 1, size + 1))
    for k in range(len(t)):
        a, b, c = t[k]
        i0 = int(math.floor((min(a[0], b[0], c[0]) - x0) / spacing))
        i1 = int(math.ceil((max(a[0], b[0], c[0]) - x0) / spacing))
        j0 = int(math.floor((min(a[1], b[1], c[1]) - y0) / spacing))
        j1 = int(math.ceil((max(a[1], b[1], c[1]) - y0) / spacing))
        gi, gj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="ij")
        px = x0 + gi * spacing
        py = y0 + gj * spacing
        v0, v1 = b - a, c - a
        d00 = v0[0] * v0[0] + v0[1] * v0[1]
        d01 = v0[0] * v1[0] + v0[1] * v1[1]
        d11 = v1[0] * v1[0] + v1[1] * v1[1]
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-12:
            continue
        qx, qy = px - a[0], py - a[1]
        d20 = qx * v0[0] + qy * v0[1]
        d21 = qx * v1[0] + qy * v1[1]
        vv = (d11 * d20 - d01 * d21) / den
        ww = (d00 * d21 - d01 * d20) / den
        inside = (vv >= -1e-6) & (ww >= -1e-6) & (vv + ww <= 1 + 1e-6)
        if not inside.any():
            continue
        z = a[2] + vv * v0[2] + ww * v1[2]
        gi, gj, z = gi[inside], gj[inside], z[inside]
        ok = (gi >= 0) & (gj >= 0) & (gi <= size) & (gj <= size)
        gi, gj, z = gi[ok], gj[ok], z[ok]
        cur = H[gi, gj]
        better = np.isnan(cur) | (z > cur)
        H[gi[better], gj[better]] = z[better]
        M[gi[better], gj[better]] = tm[k]
        NZ[gi[better], gj[better]] = tnz[k]

    out = []
    # per-cell plane z = za + gx*dx + gy*dy from three corners; the fourth must agree within tol
    za = H[:-1, :-1]
    zb = H[1:, :-1]
    zc = H[:-1, 1:]
    zd = H[1:, 1:]
    valid = ~(np.isnan(za) | np.isnan(zb) | np.isnan(zc) | np.isnan(zd))
    gx = (zb - za) / spacing
    gy = (zc - za) / spacing
    # steps: the cell's own slope (steepest of its two halves) against the surfaces at its corners
    with np.errstate(invalid="ignore"):
        gx2, gy2 = (zd - zc) / spacing, (zd - zb) / spacing
        cell_nz = np.minimum(1.0 / np.sqrt(1.0 + gx * gx + gy * gy), 1.0 / np.sqrt(1.0 + gx2 * gx2 + gy2 * gy2))
        corner_nz = np.minimum(np.minimum(NZ[:-1, :-1], NZ[1:, :-1]), np.minimum(NZ[:-1, 1:], NZ[1:, 1:]))
        rise = np.maximum(np.maximum(za, zb), np.maximum(zc, zd)) - np.minimum(np.minimum(za, zb), np.minimum(zc, zd))
        step = valid & (cell_nz < corner_nz - step_slack) & (np.nan_to_num(rise) > step_min)
    valid &= ~step
    planar = valid & (np.abs(za + gx * spacing + gy * spacing - zd) <= tol)
    ii, jj = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
    # plane offset at the grid origin, so neighbouring cells of one plane share a key
    z0 = za - gx * ii * spacing - gy * jj * spacing
    kx = np.round(np.nan_to_num(gx) / 0.01).astype(np.int64)
    ky = np.round(np.nan_to_num(gy) / 0.01).astype(np.int64)
    k0 = np.round(np.nan_to_num(z0) / max(tol, 1e-3)).astype(np.int64)
    used = np.zeros((size, size), bool)
    max_cells = max(1, int(max_block / spacing))

    def same(i, j, key):
        return planar[i, j] and not used[i, j] and kx[i, j] == key[0] and ky[i, j] == key[1] and k0[i, j] == key[2]

    def emit_rect(i0, j0, i1, j1):
        # plane of the first cell, evaluated at the rectangle corners
        a0, gxx, gyy = za[i0, j0], gx[i0, j0], gy[i0, j0]
        xa, ya = x0 + i0 * spacing, y0 + j0 * spacing
        w, h = (i1 - i0 + 1) * spacing, (j1 - j0 + 1) * spacing
        pm = physmat_for(int(M[(i0 + i1) // 2, (j0 + j1) // 2]))
        if abs(gxx) < 0.01 and abs(gyy) < 0.01:
            zt = float(np.mean([za[i0, j0], zd[i1, j1]]))
            out.append(Collider("box", pm, center=(xa + w * 0.5, ya + h * 0.5, zt - thickness * 0.5),
                                half=(w * 0.5, h * 0.5, thickness * 0.5)))
            return
        top = np.array([[xa, ya, a0], [xa + w, ya, a0 + gxx * w], [xa, ya + h, a0 + gyy * h],
                        [xa + w, ya + h, a0 + gxx * w + gyy * h]])
        out.extend(convex_to_boxes(Collider("convex", pm, vertices=np.concatenate([top, top - [0.0, 0.0, thickness]]))))

    for i in range(size):
        for j in range(size):
            if used[i, j] or not valid[i, j]:
                continue
            if not planar[i, j]:
                # a folded cell: two triangles, one slab each
                used[i, j] = True
                xa, ya = x0 + i * spacing, y0 + j * spacing
                pa = [xa, ya, za[i, j]]
                pb = [xa + spacing, ya, zb[i, j]]
                pc = [xa, ya + spacing, zc[i, j]]
                pd = [xa + spacing, ya + spacing, zd[i, j]]
                pm = physmat_for(int(M[i, j]))
                for tri in ((pa, pb, pd), (pa, pd, pc)):
                    t3 = np.array(tri)
                    out.extend(convex_to_boxes(Collider("convex", pm, vertices=np.concatenate([t3, t3 - [0.0, 0.0, thickness]]))))
                continue
            key = (kx[i, j], ky[i, j], k0[i, j])
            j1 = j
            while j1 + 1 < size and j1 + 1 - j < max_cells and same(i, j1 + 1, key):
                j1 += 1
            i1 = i
            while i1 + 1 < size and i1 + 1 - i < max_cells and all(same(i1 + 1, jj_, key) for jj_ in range(j, j1 + 1)):
                i1 += 1
            used[i:i1 + 1, j:j1 + 1] = True
            emit_rect(i, j, i1, j1)

    def sample(x, y):
        """Top-surface height near (x, y) (max of the four grid neighbours), NaN outside."""
        fi = np.clip(np.floor((np.asarray(x) - x0) / spacing).astype(np.int64), 0, size - 1)
        fj = np.clip(np.floor((np.asarray(y) - y0) / spacing).astype(np.int64), 0, size - 1)
        q = np.stack([H[fi, fj], H[fi + 1, fj], H[fi, fj + 1], H[fi + 1, fj + 1]])
        with np.errstate(all="ignore"):
            return np.nanmax(np.where(np.isnan(q), -np.inf, q), axis=0)
    return out, sample


def ground_colliders(col: Collision, *, spacing=2.0, tol=0.12, wall_min_height=0.6,
                     wall_min_extent=1.0, fill=True, layers=1, max_extra=0.15) -> tuple:
    """Large terrain-like collision (map ground): heightfield quads for the walkable surface plus
    slabs for walls and other steep surfaces that stand out of it."""
    out = primitive_colliders(col)
    note = {"triangles": int(len(col.triangles)), "primitives": len(out), "mode": "ground"}
    if not len(col.triangles):
        return out, note
    hf, up, top = heightfield_colliders(col.triangles, col.tri_materials, spacing=spacing, tol=tol, layers=layers)
    # steep faces: keep the ones that stand out of the walkable surface (walls, fences, hedges);
    # the rest are terrain slopes the heightfield already spans
    t = col.triangles
    cz = t[:, :, 2].max(1)
    ground = top(t[:, :, 0].mean(1), t[:, :, 1].mean(1))
    stands = (~up) & (cz > ground + wall_min_height) & np.isfinite(ground)
    stands |= (~up) & ~np.isfinite(ground)
    mats = col.tri_materials[stands] if len(col.tri_materials) else []
    walls = triangles_to_colliders(t[stands], mats, weld=0.08, angle_tol=5.0, dist_tol=0.05,
                                   min_extent=wall_min_extent, max_verts=32, max_extra=max_extra, max_boxes=16)
    note.update({"heightfield": len(hf), "walls": len(walls), "spacing": spacing,
                 "steep_triangles": int((~up).sum()), "standing_triangles": int(stands.sum())})
    out = out + hf + walls
    if fill:
        extra, info = fill_uncovered(out, col, max_extra=max_extra)
        out += extra
        note.update(info)
    return out, note


def fill_uncovered(colliders: list, col: Collision, *, thickness=0.15, gap=0.3, max_extra=None) -> tuple:
    """Slabs for the triangles the colliders leave open: floors with nothing under them (edges and
    narrow strips the heightfield grid misses, lower levels under a terrace or a deck) and walls
    with nothing behind them.  Returns (extra colliders, note)."""
    from .coverage import uncovered_triangles
    t = col.triangles
    if not len(t):
        return [], {}
    miss = uncovered_triangles(colliders, t, gap=gap)
    if not miss.any():
        return [], {"filled_triangles": 0, "fill": 0}
    mats = col.tri_materials[miss] if len(col.tri_materials) else []
    extra = triangles_to_colliders(t[miss], mats, thickness=thickness, weld=0.01, angle_tol=2.0, dist_tol=0.02,
                                   max_verts=24, max_extra=max_extra, max_boxes=16 if max_extra else 4)
    return extra, {"filled_triangles": int(miss.sum()), "fill": len(extra)}


GLASS_PHYSMAT = "glass.physmat"


def glass_colliders(colliders: list, tris: np.ndarray, *, gap=0.3, min_area=0.15, min_extent=0.4,
                    thickness=0.06) -> list:
    """Thin slabs for window glass the colliders leave open.  GTA maps often keep glazing out of
    their collision (a window is a hole in the wall's collision, the pane only drawn), so a player
    walks through it: every upright pane (|normal z| < 0.35, at least ``min_area`` m2 and
    ``min_extent`` m across) with no collider within ``gap`` of it gets a slab behind its face.  A
    pane drawn as two faces gets one slab: the second face is checked against the first's."""
    from .coverage import uncovered_triangles
    t = np.asarray(tris, float).reshape(-1, 3, 3)
    if not len(t):
        return []
    cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(cr, axis=1)
    n = cr / np.maximum(ln, 1e-12)[:, None]
    t, n = t[(np.abs(n[:, 2]) < 0.35) & (ln > 2e-4)], n[(np.abs(n[:, 2]) < 0.35) & (ln > 2e-4)]
    if not len(t):
        return []
    # one facing first (a canonical horizontal direction), then the other against the result
    first = (n[:, 0] + 0.5 * n[:, 1]) >= 0
    out = []
    for sel in (first, ~first):
        part = t[sel]
        if not len(part):
            continue
        miss = uncovered_triangles(list(colliders) + out, part, gap=gap, walk_nz=2.0, wall_nz=0.35,
                                   min_area=0.0, min_wall_height=0.0, density=4.0)
        if not miss.any():
            continue
        for c in triangles_to_colliders(part[miss], np.zeros(int(miss.sum()), np.int32), thickness=thickness,
                                        weld=0.01, angle_tol=2.0, dist_tol=0.02, min_area=min_area,
                                        min_extent=min_extent, max_boxes=8):
            c.physmat = GLASS_PHYSMAT
            c.source = "glass"
            out.append(c)
    return out


def prop_colliders(col: Collision, *, budget=300, hull_max=2.5, piece_max=2.5, max_pieces=24,
                   low_max=1.6) -> list:
    """Collision of one placed object.  Small solid things (a chair, a fridge): one box around it.
    A set of separate small pieces (a table and its chairs): one box per piece.  Low furniture of
    any size (an L-shaped sofa, a kitchen run, a bed): solid columns under its top surface, so
    the floor around and inside its outline stays open.  Anything taller (stairs, a shelving wall,
    a bath enclosure): slabs from its own faces, so steps stay climbable."""
    if col is None or col.empty:
        return []
    if not len(col.triangles):
        return build_colliders(col)[0]
    mn, mx = col.bounds()
    ext = np.asarray(mx) - np.asarray(mn)
    size = float(np.max(ext))
    if size <= hull_max and len(col.triangles) < 4000:
        return build_colliders(col, prop_hull=True)[0]
    pieces = component_hulls(col, max_components=max_pieces + 1)
    boxes = [c for c in pieces if c.kind == "box"]
    if len(boxes) <= max_pieces and boxes and all(2.0 * max(c.half) <= piece_max for c in boxes):
        return pieces
    if float(ext[2]) <= low_max:
        cols = solid_columns(col)
        if cols:
            return primitive_colliders(col) + cols
    return build_colliders(col, budget=budget)[0]


def solid_columns(col: Collision, *, cell=0.2, htol=0.08, max_boxes=150, min_up=0.05) -> list:
    """Low objects as upright boxes from their lowest point to their top surface, on a grid of
    ``cell`` metres merged into rectangles of equal height (within ``htol``).  Vertical panels
    count along their footprint line.  Coarsens until at most ``max_boxes`` boxes."""
    t = np.asarray(col.triangles, float)
    if not len(t):
        return []
    mats = np.asarray(col.tri_materials) if len(col.tri_materials) else np.zeros(len(t), np.int32)
    pm = physmat_for(int(np.bincount(mats.astype(np.int64)).argmax()))
    base = float(t[:, :, 2].min())
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(n, axis=1)
    nz = np.abs(n[:, 2]) / np.maximum(ln, 1e-12)
    for c_size, h_tol in ((cell, htol), (cell * 1.5, htol * 2), (cell * 2.5, htol * 3)):
        x0, y0 = float(t[:, :, 0].min()), float(t[:, :, 1].min())
        nx = max(1, int(math.ceil((float(t[:, :, 0].max()) - x0) / c_size)))
        ny = max(1, int(math.ceil((float(t[:, :, 1].max()) - y0) / c_size)))
        H = np.full((nx, ny), -np.inf)
        for k in range(len(t)):
            a, b, c = t[k]
            if nz[k] > min_up and ln[k] > 1e-9:
                i0 = max(0, int(math.floor((min(a[0], b[0], c[0]) - x0) / c_size)))
                i1 = min(nx - 1, int(math.floor((max(a[0], b[0], c[0]) - x0) / c_size)))
                j0 = max(0, int(math.floor((min(a[1], b[1], c[1]) - y0) / c_size)))
                j1 = min(ny - 1, int(math.floor((max(a[1], b[1], c[1]) - y0) / c_size)))
                gi, gj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1), indexing="ij")
                px, py = x0 + (gi + 0.5) * c_size, y0 + (gj + 0.5) * c_size
                v0, v1 = b - a, c - a
                d00, d01, d11 = v0[0] * v0[0] + v0[1] * v0[1], v0[0] * v1[0] + v0[1] * v1[1], v1[0] * v1[0] + v1[1] * v1[1]
                den = d00 * d11 - d01 * d01
                if abs(den) < 1e-12:
                    continue
                qx, qy = px - a[0], py - a[1]
                d20, d21 = qx * v0[0] + qy * v0[1], qx * v1[0] + qy * v1[1]
                vv, ww = (d11 * d20 - d01 * d21) / den, (d00 * d21 - d01 * d20) / den
                ins = (vv >= -1e-6) & (ww >= -1e-6) & (vv + ww <= 1 + 1e-6)
                if not ins.any():
                    # a triangle smaller than a cell: its centroid's cell
                    m = t[k].mean(0)
                    ci = min(nx - 1, max(0, int((m[0] - x0) / c_size)))
                    cj = min(ny - 1, max(0, int((m[1] - y0) / c_size)))
                    H[ci, cj] = max(H[ci, cj], float(t[k][:, 2].max()))
                    continue
                z = a[2] + vv * v0[2] + ww * v1[2]
                np.maximum.at(H, (gi[ins], gj[ins]), z[ins])
            else:
                # a vertical face: the cells along its footprint, at its top
                top = float(t[k][:, 2].max())
                for p, q in ((a, b), (b, c), (c, a)):
                    steps = max(2, int(math.ceil(np.linalg.norm((q - p)[:2]) / (c_size * 0.5))) + 1)
                    for f in np.linspace(0.0, 1.0, steps):
                        x, y = p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f
                        ci = min(nx - 1, max(0, int((x - x0) / c_size)))
                        cj = min(ny - 1, max(0, int((y - y0) / c_size)))
                        H[ci, cj] = max(H[ci, cj], top)
        occ = np.isfinite(H) & (H > base + 0.02)
        qz = np.where(occ, np.ceil((H - base) / h_tol), -1).astype(np.int64)
        used = np.zeros_like(occ)
        out = []
        for i in range(nx):
            for j in range(ny):
                if not occ[i, j] or used[i, j]:
                    continue
                key = qz[i, j]
                j1 = j
                while j1 + 1 < ny and occ[i, j1 + 1] and not used[i, j1 + 1] and qz[i, j1 + 1] == key:
                    j1 += 1
                i1 = i
                while i1 + 1 < nx and all(occ[i1 + 1, jj] and not used[i1 + 1, jj] and qz[i1 + 1, jj] == key
                                          for jj in range(j, j1 + 1)):
                    i1 += 1
                used[i:i1 + 1, j:j1 + 1] = True
                h = float(H[i:i1 + 1, j:j1 + 1].max()) - base
                cx, cy = x0 + (i + i1 + 1) * 0.5 * c_size, y0 + (j + j1 + 1) * 0.5 * c_size
                out.append(Collider("box", pm, center=(cx, cy, base + h * 0.5),
                                    half=((i1 - i + 1) * c_size * 0.5, (j1 - j + 1) * c_size * 0.5, max(h * 0.5, 0.02))))
        if len(out) <= max_boxes:
            return out
    return out


def quat_mats(q: np.ndarray) -> np.ndarray:
    """(N, 4) quaternions (x, y, z, w) -> (N, 3, 3) rotation matrices."""
    q = np.asarray(q, float).reshape(-1, 4)
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1)], 1)


def quats_from_mats(m: np.ndarray) -> np.ndarray:
    """(N, 3, 3) matrices whose columns are a frame's axes -> (N, 4) unit quaternions, the axes
    orthonormalised first (Gram-Schmidt, right-handed): _quat_from_axes for many at once."""
    m = np.asarray(m, float).reshape(-1, 3, 3)
    if not len(m):
        return np.zeros((0, 4))
    x = m[:, :, 0]
    nx = np.linalg.norm(x, axis=1, keepdims=True)
    x = x / np.where(nx > 0, nx, 1.0)
    y = m[:, :, 1] - x * np.sum(x * m[:, :, 1], axis=1, keepdims=True)
    ny = np.linalg.norm(y, axis=1, keepdims=True)
    y = y / np.where(ny > 0, ny, 1.0)
    z = np.cross(x, y)
    a = np.stack([x, y, z], 2)
    m00, m11, m22 = a[:, 0, 0], a[:, 1, 1], a[:, 2, 2]
    t = m00 + m11 + m22
    q = np.empty((len(a), 4))
    b0 = t > 0
    b1 = ~b0 & (m00 > m11) & (m00 > m22)
    b2 = ~b0 & ~b1 & (m11 > m22)
    b3 = ~(b0 | b1 | b2)
    if b0.any():
        s = np.sqrt(t[b0] + 1.0) * 2
        k = a[b0]
        q[b0] = np.stack([(k[:, 2, 1] - k[:, 1, 2]) / s, (k[:, 0, 2] - k[:, 2, 0]) / s, (k[:, 1, 0] - k[:, 0, 1]) / s, 0.25 * s], 1)
    if b1.any():
        k = a[b1]
        s = np.sqrt(1.0 + k[:, 0, 0] - k[:, 1, 1] - k[:, 2, 2]) * 2
        q[b1] = np.stack([0.25 * s, (k[:, 0, 1] + k[:, 1, 0]) / s, (k[:, 0, 2] + k[:, 2, 0]) / s, (k[:, 2, 1] - k[:, 1, 2]) / s], 1)
    if b2.any():
        k = a[b2]
        s = np.sqrt(1.0 + k[:, 1, 1] - k[:, 0, 0] - k[:, 2, 2]) * 2
        q[b2] = np.stack([(k[:, 0, 1] + k[:, 1, 0]) / s, 0.25 * s, (k[:, 1, 2] + k[:, 2, 1]) / s, (k[:, 0, 2] - k[:, 2, 0]) / s], 1)
    if b3.any():
        k = a[b3]
        s = np.sqrt(1.0 + k[:, 2, 2] - k[:, 0, 0] - k[:, 1, 1]) * 2
        q[b3] = np.stack([(k[:, 0, 2] + k[:, 2, 0]) / s, (k[:, 1, 2] + k[:, 2, 1]) / s, 0.25 * s, (k[:, 1, 0] - k[:, 0, 1]) / s], 1)
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def transform_colliders(cols: list, m: np.ndarray) -> list:
    """Colliders moved by a 4x4 rigid transform (uniform scale tolerated).  All at once: a city
    district moves hundreds of thousands of them."""
    if not cols:
        return []
    r, tr = np.asarray(m, float)[:3, :3], np.asarray(m, float)[:3, 3]
    s = float(np.cbrt(abs(np.linalg.det(r)))) or 1.0
    rn = r / s
    rot = np.einsum("ij,njk->nik", rn, quat_mats([c.rotation for c in cols]))
    quats = quats_from_mats(rot).tolist()
    centers = (np.array([c.center for c in cols], float) @ r.T + tr).tolist()
    out = []
    for c, ctr, q in zip(cols, centers, quats):
        out.append(Collider(c.kind, c.physmat, center=tuple(ctr), rotation=tuple(q), half=tuple(float(v) * s for v in c.half),
                            radius=c.radius * s, height=c.height * s,
                            vertices=None if c.vertices is None else np.asarray(c.vertices) @ r.T + tr,
                            source=c.source))
    return out


def component_hulls(col: Collision, *, max_components=24, min_extent=0.15, weld=0.01) -> list:
    """One convex hull per connected piece of a prop's collision (sofa seat, back, table top...).
    Small pieces are dropped; when there are too many, the largest ones are kept."""
    from ..cp77.phys import support_hull
    out = primitive_colliders(col)
    t = col.triangles
    if not len(t):
        return out
    verts, faces, ok = _weld(t, weld)
    faces = faces[ok]
    fm = col.tri_materials[ok] if len(col.tri_materials) else np.zeros(len(faces), np.int32)
    parent = list(range(len(verts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b, c in faces:
        ra, rb, rc = find(a), find(b), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    comp = defaultdict(list)
    for fi, (a, _, _) in enumerate(faces):
        comp[find(a)].append(fi)
    items = []
    for fis in comp.values():
        pts = verts[np.unique(faces[fis].reshape(-1))]
        ext = pts.max(0) - pts.min(0)
        if ext.max() < min_extent:
            continue
        items.append((float(np.prod(np.maximum(ext, 0.02))), pts, fis))
    items.sort(key=lambda x: -x[0])
    for _, pts, fis in items[:max_components]:
        p = pts
        ext = p.max(0) - p.min(0)
        if np.min(ext) < 0.02:
            k = int(np.argmin(ext))
            q = p.copy()
            q[:, k] += 0.02
            p = np.concatenate([p, q])
        mat = int(np.bincount(fm[fis].astype(np.int64)).argmax())
        box = solid_box(support_hull(p, 48), physmat_for(mat))
        if box is not None:
            out.append(box)
    return out
