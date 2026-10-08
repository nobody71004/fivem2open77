"""Chassis collision (.phys) built from the converted body.

The body is cut into slices along its length; each slice becomes one convex collider made of its
support points in a fixed set of directions (a k-DOP-like hull that never pokes outside the
mesh).  A flat convex under the floor is tagged `chassis_bottom` like Cyberpunk's own cars.
PhysX cooks the hulls from these vertices when the resource loads.
"""
from __future__ import annotations

import copy

import numpy as np

CHASSIS_OFFSET_Z = 0.5      # vehicleChassisComponent sits 0.5 m above the entity root
HULL_CLEARANCE = 0.15       # m the collision hull stays above the road (the donor's is ~0.23 m): a
                            # hull that reaches down to a low body scrapes every bump and kerb and
                            # the car hops; the wheels and suspension are what should meet the road


def _directions(n=58):
    """Axis, edge and corner directions plus a Fibonacci spread: n unit vectors."""
    base = []
    for v in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        base += [v, tuple(-x for x in v)]
    for a in (-1, 1):
        for b in (-1, 1):
            base += [(a, b, 0), (a, 0, b), (0, a, b)]
            for c in (-1, 1):
                base.append((a, b, c))
    dirs = [np.array(d, float) / np.linalg.norm(d) for d in base]
    k = max(0, n - len(dirs))
    golden = np.pi * (3 - np.sqrt(5))
    for i in range(k):
        y = 1 - 2 * (i + 0.5) / k
        r = np.sqrt(1 - y * y)
        t = golden * i
        dirs.append(np.array([np.cos(t) * r, y, np.sin(t) * r]))
    return np.array(dirs)


_DIRS = _directions()


def support_hull(points: np.ndarray, max_vertices=64) -> np.ndarray:
    """Extreme points of `points` along every direction (deduplicated)."""
    p = np.asarray(points, float)
    idx = np.unique(np.argmax(p @ _DIRS.T, axis=0))
    out = p[idx]
    return out[:max_vertices]


def body_points(vehicle) -> np.ndarray:
    """Root-space vertices of everything that should collide (body shell and openings)."""
    pts = []
    for name, part in vehicle.parts.items():
        if part.kind not in ("body", "door", "hood", "trunk", "glass", "lights", "window"):
            continue
        frame = vehicle.part_frames.get(name)
        if frame is None:
            continue
        t, r = (np.asarray(x, float) for x in frame)
        for s in part.submeshes:
            if s.lod != 1 or not len(s.positions):
                continue
            # IR geometry is bone-local: loc = (root - t) @ r
            pts.append(s.positions.astype(np.float64) @ r.T + t)
    if not pts:
        raise ValueError("no body geometry to build collision from")
    return np.concatenate(pts)


def road_height(vehicle) -> float:
    """Highest road level under the car in Root space (front and rear tyres can differ)."""
    radii = [a.radius for a in vehicle.axles.values()] or [0.34]
    return -min(radii)


def build_colliders(vehicle, slices=3):
    """[(tag, vertices in chassis space)]"""
    p = body_points(vehicle)
    p = p[np.isfinite(p).all(1)]
    # drop stray far-away vertices (antennas, misplaced pieces): keep the central 99.8 %
    lo = np.percentile(p, 0.1, axis=0)
    hi = np.percentile(p, 99.9, axis=0)
    p = p[((p >= lo - 0.02) & (p <= hi + 0.02)).all(1)]
    floor = road_height(vehicle) + HULL_CLEARANCE
    p = p.copy()
    p[:, 2] = np.maximum(p[:, 2], floor)
    p = p - [0.0, 0.0, CHASSIS_OFFSET_Z]
    out = []
    y0, y1 = p[:, 1].min(), p[:, 1].max()
    edges = np.linspace(y0, y1, slices + 1)
    overlap = 0.05
    for i in range(slices):
        sel = p[(p[:, 1] >= edges[i] - overlap) & (p[:, 1] <= edges[i + 1] + overlap)]
        if len(sel) >= 8:
            out.append(("None", support_hull(sel)))
    # floor slab
    z0 = p[:, 2].min()
    floor = p[p[:, 2] <= z0 + 0.12]
    if len(floor) >= 8:
        out.append(("chassis_bottom", support_hull(floor, 34)))
    return out


def build_phys(vehicle, donor_phys_doc) -> dict:
    from .handles import IdAllocator, normalize
    from .wkjson import index_handles

    doc = copy.deepcopy(donor_phys_doc)
    index = index_handles(doc)
    ids = IdAllocator(doc)
    root = doc["Data"]["RootChunk"]
    bw = root["bodies"][0]
    body = bw["Data"] if "Data" in bw else bw
    shapes = body["collisionShapes"]

    def data(s):
        if "HandleRefId" in s:
            return index[str(s["HandleRefId"])]["Data"]
        return s["Data"] if "Data" in s else s

    tmpl = next((data(s) for s in shapes if data(s).get("$type") == "physicsColliderConvex"), None)
    if tmpl is None:
        raise ValueError("donor collision has no convex collider to use as template")
    new = []
    for tag, verts in build_colliders(vehicle):
        s = copy.deepcopy(tmpl)
        s["localToBody"]["position"].update({"X": 0.0, "Y": 0.0, "Z": 0.0, "W": 0.0})
        s["localToBody"]["orientation"].update({"i": 0.0, "j": 0.0, "k": 0.0, "r": 1.0})
        s["vertices"] = [{"$type": "Vector3", "X": float(x), "Y": float(y), "Z": float(z)} for x, y, z in verts]
        s["indexBuffer"] = []
        s["polygonVertices"] = []
        s["compiledGeometryBuffer"] = None
        s["tag"] = {"$type": "CName", "$storage": "string", "$value": tag} if isinstance(tmpl.get("tag"), dict) else tag
        ids.fresh_ids(s)
        new.append(ids.handle(s))
    body["collisionShapes"] = new
    body["name"] = {"$type": "CName", "$storage": "string", "$value": f"{vehicle.name} chassis"}
    normalize(doc, index)
    return doc
