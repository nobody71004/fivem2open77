"""Doorways of MLO interiors, the door mapping, and doors created where a doorway has none.

GTA interiors list every doorway: a portal joins two rooms (room 0 is outside) through a quad of
four corners in MLO space.  That makes doors findable by where they stand, not only by name: a
door leaf (a thin panel, model origin on its hinge) standing in a doorway is a door whatever its
model is called (Cookies' front door is two ``cookiedesp`` leaves).

Every vertical doorway gets a status in the conversion report:

    open      a passage at least PASSAGE_WIDTH wide and PASSAGE_HEIGHT tall
    door      a door leaf (a Door device) stands in it
    blocked   something the conversion made solid closes it: a door-like object that stayed
              static, or a glass slab added for a pane (``blockers`` says what)
    closed    the FiveM map's own static collision closes it (a storefront pane, a sealed
              portal): solid in GTA too
    window    its sill is above the floor, or it is too small to walk through

The door mapping (map option ``door_map``, the Studio's Doors panel) overrides the guesses::

    {"objects":  {"<archetype name>": "auto|hinged|sliding|rolling|open|solid|remove"},
     "doorways": {"<mlo>#<portal>": "auto|open|door-hinged|door-double|door-sliding|door-rolling"}}

Objects: ``hinged``/``sliding``/``rolling`` make the object a Door device that opens that way
(rolling = a garage door or shutter going up); ``open`` keeps it without collision; ``solid`` keeps
it static and solid (never a door); ``remove`` leaves it out.  Doorways: ``open`` cuts the static
collision out of the doorway; ``door-*`` does that and puts a generated leaf there (for the many
MLOs whose doors are GTA's own models, which a conversion cannot carry).
"""
from __future__ import annotations

import math
import re

import numpy as np

OBJECT_MODES = ("auto", "hinged", "sliding", "rolling", "open", "solid", "remove")
DOORWAY_MODES = ("auto", "open", "door-hinged", "door-double", "door-sliding", "door-rolling")
NAME_RX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
KEY_RX = re.compile(r"^[a-z0-9_.-]{1,64}#\d{1,4}$")

PASSAGE_WIDTH = 0.6            # metres a player needs to get through
PASSAGE_HEIGHT = 1.8
SILL = 0.45                    # a doorway whose bottom is higher than this above the floor is a window
PLANE_TOL = 0.35               # a leaf this close to a doorway's plane stands in it
GARAGE_WIDTH = 2.4             # a single leaf at least this wide filling its doorway rolls up
GENERATED_THICKNESS = 0.06


class DoorMapError(ValueError):
    pass


def normalize_door_map(raw) -> dict:
    """{"objects": {name: mode}, "doorways": {key: mode}} with names lower-cased; raises
    DoorMapError listing every problem.  "auto" entries are dropped (they are the default)."""
    if raw in (None, "", {}):
        return {"objects": {}, "doorways": {}}
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise DoorMapError(f"door_map: not JSON ({e})") from None
    if not isinstance(raw, dict) or set(raw) - {"objects", "doorways"}:
        raise DoorMapError('door_map: {"objects": {name: mode}, "doorways": {"<mlo>#<n>": mode}}')
    errors, out = [], {"objects": {}, "doorways": {}}
    objs = raw.get("objects") or {}
    ways = raw.get("doorways") or {}
    if not isinstance(objs, dict) or not isinstance(ways, dict) or len(objs) > 2000 or len(ways) > 2000:
        raise DoorMapError("door_map: objects and doorways are maps (at most 2000 entries each)")
    for k, v in objs.items():
        if not isinstance(k, str) or not NAME_RX.match(k):
            errors.append(f"door_map object {k!r}: an archetype name (letters, digits, _ . -)")
        elif v not in OBJECT_MODES:
            errors.append(f"door_map object {k}: {v!r} is not one of {', '.join(OBJECT_MODES)}")
        elif v != "auto":
            out["objects"][k.lower()] = v
    for k, v in ways.items():
        if not isinstance(k, str) or not KEY_RX.match(k.lower()):
            errors.append(f"door_map doorway {k!r}: '<mlo name>#<portal number>'")
        elif v not in DOORWAY_MODES:
            errors.append(f"door_map doorway {k}: {v!r} is not one of {', '.join(DOORWAY_MODES)}")
        elif v != "auto":
            out["doorways"][k.lower()] = v
    if errors:
        raise DoorMapError("; ".join(errors))
    return out


# ----------------------------------------------------------------------------- doorways
class Doorway:
    """A vertical MLO portal in MLO space: ``origin`` is its bottom-left corner, ``u`` runs along
    its width, ``n`` is its (horizontal) normal."""

    def __init__(self, mlo: str, index: int, rooms, outside: bool, corners: np.ndarray, flags: int = 0):
        self.mlo, self.index, self.rooms, self.outside, self.flags = mlo, index, list(rooms), outside, flags
        c = np.asarray(corners, float)
        self.corners = c
        self.zmin, self.zmax = float(c[:, 2].min()), float(c[:, 2].max())
        flat = c[:, :2]
        d = np.linalg.norm(flat[:, None, :] - flat[None, :, :], axis=2)
        i, j = np.unravel_index(int(np.argmax(d)), d.shape)
        a, b = (flat[i], flat[j]) if flat[i][0] + 1e-6 * flat[i][1] <= flat[j][0] + 1e-6 * flat[j][1] else (flat[j], flat[i])
        self.width = float(np.linalg.norm(b - a))
        u = (b - a) / max(self.width, 1e-9)
        self.u = np.array([u[0], u[1], 0.0])
        self.n = np.array([-u[1], u[0], 0.0])
        self.origin = np.array([a[0], a[1], self.zmin])
        self.center = self.origin + self.u * self.width * 0.5 + np.array([0.0, 0.0, (self.zmax - self.zmin) * 0.5])
        self.height = self.zmax - self.zmin
        self.floor = None              # the floor under it (set by classify)
        self.kind = "door"             # door | window

    @property
    def key(self) -> str:
        return f"{self.mlo}#{self.index}"

    def local(self, p) -> np.ndarray:
        """MLO-space points -> (along u from the left edge, along n, height above the bottom)."""
        q = np.asarray(p, float) - self.origin
        return np.stack([q @ self.u, q @ self.n, q[..., 2]], -1)

    def transformed(self, m: np.ndarray) -> "Doorway":
        """The same doorway in another frame (m: 4x4 from this frame to that one, yaw only)."""
        m = np.asarray(m, float)
        c = self.corners @ m[:3, :3].T + m[:3, 3]
        out = Doorway(self.mlo, self.index, self.rooms, self.outside, c, self.flags)
        out.floor = None if self.floor is None else self.floor + float(m[2, 3])
        out.kind = self.kind
        return out

    def world(self, s, d, h) -> np.ndarray:
        return self.origin + self.u * s + self.n * d + np.array([0.0, 0.0, h])

    def yaw(self) -> float:
        return math.degrees(math.atan2(self.u[1], self.u[0]))


def mlo_doorways(mlo_def: dict, mlo_name: str) -> list:
    """Every vertical portal of an MLO archetype definition (``Archetype.mlo``), in MLO space."""
    out = []
    rooms = [str((r or {}).get("name") or i) for i, r in enumerate((mlo_def or {}).get("rooms") or [])]
    for i, p in enumerate((mlo_def or {}).get("portals") or []):
        c = np.asarray(p.get("corners") or [], float)
        if c.shape != (4, 3) or not np.all(np.isfinite(c)):
            continue
        nrm = np.cross(c[1] - c[0], c[3] - c[0])
        ln = np.linalg.norm(nrm)
        if ln < 1e-6 or abs(nrm[2] / ln) > 0.5:
            continue                     # flat (a hatch, a skylight) or degenerate
        rf, rt = int(p.get("roomFrom") or 0), int(p.get("roomTo") or 0)
        names = [rooms[k] if 0 <= k < len(rooms) else str(k) for k in (rf, rt)]
        w = Doorway(_lower(mlo_name), i, names, outside=(rf == 0 or rt == 0), corners=c, flags=int(p.get("flags") or 0))
        if w.width < 0.3 or w.height < 0.5:
            continue
        out.append(w)
    return out


def _lower(s):
    return re.sub(r"[^a-z0-9_.-]+", "_", str(s or "").lower()).strip("_") or "mlo"


def classify(doorways: list, tris: np.ndarray) -> None:
    """Door or window: a doorway whose bottom is more than SILL above the floor next to it (the
    highest upward surface of the collision within half a metre on either side), or that is too
    small to pass, is a window."""
    t = np.asarray(tris, float).reshape(-1, 3, 3) if tris is not None and len(tris) else np.zeros((0, 3, 3))
    if len(t):
        nrm = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        ln = np.linalg.norm(nrm, axis=1)
        up = (ln > 1e-9) & (nrm[:, 2] / np.maximum(ln, 1e-12) > 0.7)
        t = t[up]
    for w in doorways:
        floor = None
        if len(t):
            for d in (-0.5, 0.5):
                for s in (0.25, 0.5, 0.75):
                    p = w.world(w.width * s, d, 0.0)
                    z = _surface_below(t, p[0], p[1], w.zmin + 0.3)
                    if z is not None and (floor is None or z > floor):
                        floor = z
        w.floor = floor
        sill = (w.zmin - floor) if floor is not None else 0.0
        too_small = w.width < PASSAGE_WIDTH or w.height < 1.5
        w.kind = "window" if (sill > SILL or too_small) else "door"


def _surface_below(t, x, y, ztop):
    """Height of the highest triangle under (x, y) at or below ztop, or None."""
    a, b, c = t[:, 0], t[:, 1], t[:, 2]
    lo = np.minimum(np.minimum(a[:, :2], b[:, :2]), c[:, :2])
    hi = np.maximum(np.maximum(a[:, :2], b[:, :2]), c[:, :2])
    sel = np.nonzero((lo[:, 0] <= x) & (hi[:, 0] >= x) & (lo[:, 1] <= y) & (hi[:, 1] >= y))[0]
    best = None
    for k in sel:
        v0, v1 = b[k] - a[k], c[k] - a[k]
        den = v0[0] * v1[1] - v0[1] * v1[0]
        if abs(den) < 1e-12:
            continue
        px, py = x - a[k][0], y - a[k][1]
        bu = (px * v1[1] - py * v1[0]) / den
        bv = (v0[0] * py - v0[1] * px) / den
        if bu < -1e-6 or bv < -1e-6 or bu + bv > 1 + 1e-6:
            continue
        z = a[k][2] + bu * v0[2] + bv * v1[2]
        if z <= ztop and (best is None or z > best):
            best = float(z)
    return best


# ----------------------------------------------------------------------------- leaves in doorways
def leaf_frame(mn, mx, m: np.ndarray):
    """A leaf's box (model bounds mn, mx) placed by the 4x4 m: (centre, long axis, width,
    thickness, zmin, zmax) in the placed frame."""
    mn, mx = np.asarray(mn, float), np.asarray(mx, float)
    ext = mx - mn
    ax = 0 if ext[0] >= ext[1] else 1
    r = np.asarray(m, float)[:3, :3]
    t = np.asarray(m, float)[:3, 3]
    corners = np.array([[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])])
    pts = corners @ r.T + t
    long_axis = r[:, ax] / max(np.linalg.norm(r[:, ax]), 1e-9)
    return pts.mean(0), long_axis, float(ext[ax]), float(ext[1 - ax]), float(pts[:, 2].min()), float(pts[:, 2].max())


def leaf_doorway(mn, mx, m: np.ndarray, doorways: list):
    """The doorway a leaf-shaped object (model bounds mn, mx; placement m, MLO space) stands in,
    or None.  It stands in it when it is near the doorway's plane, runs along it, covers most of
    its own width inside it and most of its own height."""
    if not doorways:
        return None
    centre, long_axis, width, thick, z0, z1 = leaf_frame(mn, mx, m)
    height = z1 - z0
    if height < 1.2 or width < 0.3 or thick > 0.6:
        return None
    best = None
    for w in doorways:
        if w.kind == "window":
            continue
        loc = w.local(centre)
        if abs(loc[1]) > PLANE_TOL:
            continue
        if abs(float(np.dot(long_axis, w.u))) < 0.9:
            continue
        s0, s1 = loc[0] - width * 0.5, loc[0] + width * 0.5
        over = min(s1, w.width + 0.05) - max(s0, -0.05)
        if over < 0.6 * width:
            continue
        vover = min(z1, w.zmax + 0.1) - max(z0, w.zmin - 0.1)
        if vover < 0.6 * height:
            continue
        score = abs(loc[1]) + abs(loc[0] - w.width * 0.5) * 0.01
        if best is None or score < best[0]:
            best = (score, w)
    return best[1] if best else None


def opening_for(name: str, width: float, height: float, doorway=None) -> str:
    """How a leaf found by its doorway opens: a garage door or shutter rolls up, else it swings."""
    from .doors import HINGED, ROLLING, ROLLING_WORDS
    lname = (name or "").lower()
    if any(wd in lname for wd in ROLLING_WORDS):
        return ROLLING
    if doorway is not None and width >= GARAGE_WIDTH and width >= 0.8 * doorway.width and height >= 1.8:
        return ROLLING
    return HINGED


# ----------------------------------------------------------------------------- what blocks a doorway
def box_arrays(colliders):
    """(the box colliders, centres (N, 3), rotations (N, 3, 3), half extents (N, 3)): what the
    doorway checks read, built once for all of an MLO's doorways."""
    from .collision import quat_mats
    keep = [c for c in colliders if c.kind == "box"]
    if not keep:
        return keep, np.zeros((0, 3)), np.zeros((0, 3, 3)), np.zeros((0, 3))
    C = np.array([c.center for c in keep], float)
    R = quat_mats([c.rotation for c in keep])
    H = np.array([c.half for c in keep], float)
    return keep, C, R, H


_box_arrays = box_arrays


def _quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _samples(w: Doorway, step=0.1, inset=0.12):
    """Sample points over the doorway's passage: columns along u, rows up to PASSAGE_HEIGHT,
    three depths through the wall.  -> (points (S, D, Z, 3), column positions)"""
    cols = np.arange(inset, max(inset + 1e-6, w.width - inset) + 1e-9, step)
    if not len(cols):
        cols = np.array([w.width * 0.5])
    base = w.floor if w.floor is not None and w.floor > w.zmin - 0.5 else w.zmin
    top = min(w.zmax - 0.08, base + PASSAGE_HEIGHT)
    rows = np.linspace(base + 0.3, max(base + 0.31, top), 6)
    depths = np.array([-0.12, 0.0, 0.12])
    pts = (w.origin[None, None, None, :] + cols[:, None, None, None] * w.u
           + depths[None, :, None, None] * w.n + (rows[None, None, :, None] - w.zmin) * np.array([0.0, 0.0, 1.0]))
    return pts, cols


def blocked_columns(w: Doorway, colliders, boxes=None) -> tuple:
    """(blocked flag per column, {collider index: hit count}) for the static colliders.
    ``boxes``: box_arrays(colliders) when the caller checks many doorways against them."""
    keep, C, R, H = boxes if boxes is not None else box_arrays(colliders)
    pts, cols = _samples(w)
    flat = pts.reshape(-1, 3)
    hit_any = np.zeros(len(flat), bool)
    hits = {}
    if len(keep):
        near = np.nonzero(np.linalg.norm(C - w.center, axis=1) < (np.linalg.norm(H, axis=1) + w.width + w.height))[0]
        for k in near:
            loc = (flat - C[k]) @ R[k]
            inside = np.all(np.abs(loc) <= H[k] + 1e-3, axis=1)
            if inside.any():
                hits[k] = int(inside.sum())
                hit_any |= inside
    per_col = hit_any.reshape(len(cols), -1).any(1)
    return per_col, hits, keep


def widest_gap(per_col, step=0.1) -> float:
    best = run = 0
    for b in per_col:
        run = 0 if b else run + 1
        best = max(best, run)
    return best * step


def doorway_status(w: Doorway, colliders, door_names=(), suspects=(), boxes=None) -> dict:
    """The report entry of one doorway: status, passage width and what blocks it.  ``suspects``:
    object names that look like door leaves (a doorway they close is blocked, not closed)."""
    per_col, hits, keep = blocked_columns(w, colliders, boxes)
    gap = widest_gap(per_col)
    blockers = {}
    for k, n in hits.items():
        c = keep[k]
        src = getattr(c, "source", "") or "collision"
        kind = "glass" if src == "glass" else ("collision" if src in ("collision", "") else "object")
        b = blockers.setdefault((src, kind), 0)
        blockers[(src, kind)] = b + n
    sus = {x.lower() for x in suspects}
    if w.kind == "window":
        status = "window"
    elif door_names:
        status = "door"
    elif gap >= PASSAGE_WIDTH:
        status = "open"
    elif any(k == "glass" or s.lower() in sus for s, k in blockers):
        status = "blocked"
    else:
        status = "closed"
    return {
        "key": w.key, "mlo": w.mlo, "portal": w.index, "rooms": w.rooms, "outside": w.outside, "kind": w.kind,
        "width": round(w.width, 2), "height": round(w.height, 2),
        "center": [round(float(v), 3) for v in w.center], "yaw": round(w.yaw(), 1),
        "floor": None if w.floor is None else round(w.floor, 3), "status": status,
        "passage": round(gap, 2), "blocked_pct": round(100.0 * float(per_col.mean()) if len(per_col) else 0.0, 1),
        "blockers": [{"source": s, "kind": k, "hits": n} for (s, k), n in sorted(blockers.items(), key=lambda kv: -kv[1])][:8],
        "doors": sorted(door_names),
    }


def carve(w: Doorway, colliders) -> tuple:
    """Colliders with the doorway's passage cut out: a box mostly inside the doorway goes; a wall
    box running across it (lined up with the doorway) keeps its parts beside and above it.
    -> (colliders, removed count, boxes it could not cut)."""
    from .collision import Collider, _quat_from_axes
    per_col, hits, keep = blocked_columns(w, colliders)
    if not hits:
        return list(colliders), 0, []
    drop = set()
    extra, stuck = [], []
    cut_lo, cut_hi = -0.02, w.width + 0.02
    top = (w.floor if w.floor is not None else w.zmin) + PASSAGE_HEIGHT + 0.2
    top = max(top, w.zmax - 0.02)
    for k in hits:
        c = keep[k]
        R = _quat_matrix(c.rotation)
        H = np.asarray(c.half, float)
        # the box in the doorway frame: its axes must line up with u, n, z to be cut
        A = np.stack([w.u, w.n, [0.0, 0.0, 1.0]])
        M = A @ R                       # doorway axes in box axes
        perm = np.argmax(np.abs(M), axis=1)
        aligned = len(set(perm.tolist())) == 3 and np.all(np.abs(M[np.arange(3), perm]) > 0.98)
        cl = w.local(np.asarray(c.center, float))
        if aligned:
            ext = H[perm]               # half extents along u, n, z
        else:
            corners = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float) * H
            pts = w.local(corners @ R.T + np.asarray(c.center, float))
            ext = (pts.max(0) - pts.min(0)) * 0.5
        s0, s1 = cl[0] - ext[0], cl[0] + ext[0]
        z0, z1 = cl[2] - ext[2] + w.zmin, cl[2] + ext[2] + w.zmin
        inside_w = s0 >= cut_lo - 0.3 and s1 <= cut_hi + 0.3
        inside_h = z1 <= top + 0.6
        if inside_w and inside_h:
            drop.add(id(c))
            continue
        if not aligned:
            # a pane or frame strip lying in the doorway's plane (turned in it): it goes when its
            # middle is in the doorway and it reaches at most a little past its edges
            thin = int(np.argmin(H))
            planar = abs(float(np.dot(R[:, thin], w.n))) > 0.95
            mid_in = -0.3 <= cl[0] <= w.width + 0.3 and -0.3 <= cl[2] <= w.height + 0.3
            over = max(cut_lo - s0, s1 - cut_hi, z1 - (top + 0.6), 0.0)
            if planar and mid_in and over <= 0.6:
                drop.add(id(c))
            else:
                stuck.append(getattr(c, "source", "") or "collision")
            continue
        drop.add(id(c))
        pieces = []
        if s0 < cut_lo:
            pieces.append((s0, min(s1, cut_lo), z0, z1))
        if s1 > cut_hi:
            pieces.append((max(s0, cut_hi), s1, z0, z1))
        if z1 > top:
            pieces.append((max(s0, cut_lo), min(s1, cut_hi), max(z0, top), z1))
        zb = w.floor if w.floor is not None else w.zmin
        if z0 < zb + 0.05:              # a floor slab under the doorway stays
            pieces.append((max(s0, cut_lo), min(s1, cut_hi), z0, min(z1, zb + 0.05)))
        for a0, a1, b0, b1 in pieces:
            if a1 - a0 < 0.02 or b1 - b0 < 0.02:
                continue
            mid_s, mid_z = (a0 + a1) * 0.5, (b0 + b1) * 0.5 - w.zmin
            centre = w.world(mid_s, cl[1], mid_z)
            extra.append(Collider("box", c.physmat, center=tuple(float(v) for v in centre),
                                  rotation=_quat_from_axes(np.stack([w.u, w.n, [0.0, 0.0, 1.0]])),
                                  half=((a1 - a0) * 0.5, float(ext[1]), (b1 - b0) * 0.5),
                                  source=getattr(c, "source", "")))
    out = [c for c in colliders if id(c) not in drop] + extra
    return out, len(drop), stuck


# ----------------------------------------------------------------------------- generated doors
def door_info(width: float, height: float, opening: str, thickness: float = GENERATED_THICKNESS) -> dict:
    """door_kind()'s answer for a generated leaf: model frame x along the leaf from the hinge (or
    centred for a sliding / rolling leaf), z from 0."""
    from .doors import HINGED
    w, h = float(width), float(height)
    if opening == HINGED:
        pivot, center = [0.0, 0.0], [round(w * 0.5, 3), 0.0]
    else:
        pivot, center = [0.0, 0.0], [0.0, 0.0]
    return {"opening": opening, "axis": "x", "width": round(w, 3), "height": round(h, 3),
            "thickness": round(thickness, 3), "pivot": pivot, "center": center, "zmin": 0.0, "zmax": round(h, 3),
            "slide": round(-w, 3), "solid": True, "generated": True}


def leaf_mesh(info: dict, material: str):
    """A box leaf in the device frame (a SubMesh): hinge at x = 0 for a swinging leaf, centred
    otherwise; 24 vertices so every face has its own normal."""
    from ..model.ir import SubMesh
    w, h, t = float(info["width"]), float(info["height"]), float(info["thickness"])
    x0 = 0.0 if info["opening"] == "HINGED" else -w * 0.5
    lo = np.array([x0, -t * 0.5, 0.0])
    hi = np.array([x0 + w, t * 0.5, h])
    pos, nrm, tan, uv, idx = [], [], [], [], []
    for axis in range(3):
        a1, a2 = [k for k in range(3) if k != axis]
        for sign in (1.0, -1.0):
            n = np.zeros(3)
            n[axis] = sign
            base = len(pos)
            for i, j in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = np.zeros(3)
                p[axis] = hi[axis] if sign > 0 else lo[axis]
                p[a1] = hi[a1] if i else lo[a1]
                p[a2] = hi[a2] if j else lo[a2]
                pos.append(p)
                nrm.append(n)
                tv = np.zeros(4)
                tv[a1], tv[3] = 1.0, 1.0
                tan.append(tv)
                uv.append((p[a1] - lo[a1], p[a2] - lo[a2]))
            q = [base, base + 1, base + 2, base, base + 2, base + 3]
            p0, p1, p2 = pos[base], pos[base + 1], pos[base + 2]
            if np.dot(np.cross(p1 - p0, p2 - p0), n) < 0:      # outward winding
                q = [base, base + 2, base + 1, base, base + 3, base + 2]
            idx += q
    return SubMesh(material=material, positions=np.array(pos, np.float32), normals=np.array(nrm, np.float32),
                   tangents=np.array(tan, np.float32), uv0=np.array(uv, np.float32), uv1=None, colors=None,
                   indices=np.array(idx, np.uint32), lod=1)


def generated_leaves(w: Doorway, mode: str) -> list:
    """[(door info, MLO-space 4x4 of the device)] for a doorway mode door-hinged|double|sliding|rolling."""
    from .doors import HINGED, ROLLING, SLIDING
    base = w.floor if w.floor is not None and abs(w.floor - w.zmin) < 0.6 else w.zmin
    h = max(1.0, min(w.zmax, base + 3.2) - base - 0.02)
    yaw = math.radians(w.yaw())

    def frame(s, flip=False):
        a = yaw + (math.pi if flip else 0.0)
        m = np.eye(4)
        m[:3, :3] = [[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]]
        m[:3, 3] = w.origin + w.u * s + np.array([0.0, 0.0, base - w.zmin])
        return m

    width = w.width - 0.02
    if mode == "door-hinged":
        return [(door_info(width, h, HINGED), frame(0.01))]
    if mode == "door-double":
        half = width * 0.5 - 0.005
        return [(door_info(half, h, HINGED), frame(0.01)), (door_info(half, h, HINGED), frame(w.width - 0.01, flip=True))]
    if mode in ("door-sliding", "door-rolling"):
        return [(door_info(width, h, SLIDING if mode == "door-sliding" else ROLLING), frame(w.width * 0.5))]
    raise ValueError(f"unknown doorway mode {mode}")
