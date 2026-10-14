"""GTA V collision bounds (.ybn, resource version 43, and bounds embedded in drawables).

Layouts follow CodeWalker's ``Bounds.cs`` (MIT).  A bound is one of:

    0 sphere, 1 capsule, 3 box, 4 geometry, 8 geometry with BVH, 10 composite, 12 disc, 13 cylinder

Composite children carry a 4x3 transform each (Matrix4F_s: three axis columns and a translation,
row-vector convention: world = x*C1 + y*C2 + z*C3 + C4).  Geometry vertices are int16 triples
scaled by ``Quantum`` and offset by ``CenterGeom``; polygons are 16-byte records whose low three
bits give the primitive type (triangle, sphere, capsule, box, cylinder).

Everything is flattened into a ``Collision`` holding triangles and analytic primitives in the
coordinate space of the file (world space for map collision, model space for drawables / MLOs).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .reader import ResourceReader
from .rsc7 import ResourceError, read_rsc7

YBN_VERSION = 43

# GTA V materials.dat order (first entries cover everything maps use; the rest fall back to DEFAULT)
GTA_MATERIALS = [
    "DEFAULT", "CONCRETE", "CONCRETE_POTHOLE", "CONCRETE_DUSTY", "TARMAC", "TARMAC_PAINTED",
    "TARMAC_POTHOLE", "RUMBLE_STRIPS", "BREEZE_BLOCK", "ROCK", "ROCK_MOSSY", "STONE", "COBBLESTONE",
    "BRICK", "MARBLE", "PAVING_SLAB", "SANDSTONE_SOLID", "SANDSTONE_BRITTLE", "SAND_LOOSE",
    "SAND_COMPACT", "SAND_WET", "SAND_TRACK", "SAND_UNDERWATER", "SAND_DRY_DEEP", "SAND_WET_DEEP",
    "ICE", "ICE_TARMAC", "SNOW_LOOSE", "SNOW_COMPACT", "SNOW_DEEP", "SNOW_TARMAC", "GRAVEL_SMALL",
    "GRAVEL_LARGE", "GRAVEL_DEEP", "GRAVEL_TRAIN_TRACK", "DIRT_TRACK", "MUD_HARD", "MUD_POTHOLE",
    "MUD_SOFT", "MUD_UNDERWATER", "MUD_DEEP", "MARSH", "MARSH_DEEP", "SOIL", "CLAY_HARD", "CLAY_SOFT",
    "GRASS_LONG", "GRASS", "GRASS_SHORT", "HAY", "BUSHES", "TWIGS", "LEAVES", "WOODCHIPS", "TREE_BARK",
    "METAL_SOLID_SMALL", "METAL_SOLID_MEDIUM", "METAL_SOLID_LARGE", "METAL_HOLLOW_SMALL",
    "METAL_HOLLOW_MEDIUM", "METAL_HOLLOW_LARGE", "METAL_CHAINLINK_SMALL", "METAL_CHAINLINK_LARGE",
    "METAL_CORRUGATED_IRON", "METAL_GRILLE", "METAL_RAILING", "METAL_DUCT", "METAL_GARAGE_DOOR",
    "METAL_MANHOLE", "WOOD_SOLID_SMALL", "WOOD_SOLID_MEDIUM", "WOOD_SOLID_LARGE", "WOOD_SOLID_POLISHED",
    "WOOD_FLOOR_DUSTY", "WOOD_HOLLOW_SMALL", "WOOD_HOLLOW_MEDIUM", "WOOD_HOLLOW_LARGE", "WOOD_CHIPBOARD",
    "WOOD_OLD_CREAKY", "WOOD_HIGH_DENSITY", "WOOD_LATTICE", "CERAMIC", "ROOF_TILE", "ROOF_FELT",
    "FIBREGLASS", "TARPAULIN", "PLASTIC", "PLASTIC_HOLLOW", "PLASTIC_HIGH_DENSITY", "PLASTIC_CLEAR",
    "PLASTIC_HOLLOW_CLEAR", "PLASTIC_HIGH_DENSITY_CLEAR", "FIBREGLASS_HOLLOW", "RUBBER", "RUBBER_HOLLOW",
    "LINOLEUM", "LAMINATE", "CARPET_SOLID", "CARPET_SOLID_DUSTY", "CARPET_FLOORBOARD", "CLOTH",
    "PLASTER_SOLID", "PLASTER_BRITTLE", "CARDBOARD_SHEET", "CARDBOARD_BOX", "PAPER", "FOAM",
    "FEATHER_PILLOW", "POLYSTYRENE", "LEATHER", "TVSCREEN", "SLATTED_BLINDS", "GLASS_SHOOT_THROUGH",
    "GLASS_BULLETPROOF", "GLASS_OPAQUE", "PERSPEX", "CAR_METAL", "CAR_PLASTIC", "CAR_SOFTTOP",
    "CAR_SOFTTOP_CLEAR", "CAR_GLASS_WEAK", "CAR_GLASS_MEDIUM", "CAR_GLASS_STRONG",
    "CAR_GLASS_BULLETPROOF", "CAR_GLASS_OPAQUE", "WATER", "BLOOD", "OIL", "PETROL", "FRESH_MEAT",
    "DRIED_MEAT", "EMISSIVE_GLASS", "EMISSIVE_PLASTIC",
]


def material_name(index: int) -> str:
    return GTA_MATERIALS[index] if 0 <= index < len(GTA_MATERIALS) else "DEFAULT"


@dataclass
class Collision:
    """Flattened collision.  Arrays are float64; every primitive has a GTA material index."""
    triangles: np.ndarray = field(default_factory=lambda: np.zeros((0, 3, 3)))
    tri_materials: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    boxes: list = field(default_factory=list)       # (center(3), axes 3x3 rows = unit axes, half(3), mat)
    spheres: list = field(default_factory=list)     # (center(3), radius, mat)
    capsules: list = field(default_factory=list)    # (p1(3), p2(3), radius, mat)
    cylinders: list = field(default_factory=list)   # (p1(3), p2(3), radius, mat)

    def extend(self, other: "Collision"):
        if len(other.triangles):
            self.triangles = np.concatenate([self.triangles, other.triangles]) if len(self.triangles) else other.triangles
            self.tri_materials = np.concatenate([self.tri_materials, other.tri_materials])
        self.boxes += other.boxes
        self.spheres += other.spheres
        self.capsules += other.capsules
        self.cylinders += other.cylinders

    def transformed(self, m: np.ndarray) -> "Collision":
        """Apply a 4x4 (column-vector) rigid transform (uniform scale tolerated)."""
        r, t = m[:3, :3], m[:3, 3]
        s = float(np.cbrt(abs(np.linalg.det(r)))) or 1.0
        rn = r / s
        out = Collision()
        if len(self.triangles):
            out.triangles = self.triangles @ r.T + t
            out.tri_materials = self.tri_materials.copy()
        out.boxes = [(r @ c + t, (rn @ np.asarray(ax).T).T, np.asarray(h) * s, mt) for c, ax, h, mt in self.boxes]
        out.spheres = [(r @ c + t, rad * s, mt) for c, rad, mt in self.spheres]
        out.capsules = [(r @ a + t, r @ b + t, rad * s, mt) for a, b, rad, mt in self.capsules]
        out.cylinders = [(r @ a + t, r @ b + t, rad * s, mt) for a, b, rad, mt in self.cylinders]
        return out

    @property
    def empty(self) -> bool:
        return not (len(self.triangles) or self.boxes or self.spheres or self.capsules or self.cylinders)

    def bounds(self):
        pts = [self.triangles.reshape(-1, 3)] if len(self.triangles) else []
        for c, ax, h, _ in self.boxes:
            pts.append(np.array([c - np.abs(np.asarray(ax).T @ h), c + np.abs(np.asarray(ax).T @ h)]))
        for c, rad, _ in self.spheres:
            pts.append(np.array([c - rad, c + rad]))
        for a, b, rad, _ in self.capsules + self.cylinders:
            pts.append(np.array([np.minimum(a, b) - rad, np.maximum(a, b) + rad]))
        if not pts:
            return np.zeros(3), np.zeros(3)
        p = np.concatenate(pts)
        return p.min(0), p.max(0)


def _matrix4f(buf, off) -> np.ndarray:
    vals = struct.unpack_from("<16f", buf, off)
    c1, c2, c3, c4 = vals[0:3], vals[4:7], vals[8:11], vals[12:15]
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = c1, c2, c3, c4
    return m


def _read_geometry(rd: ResourceReader, ptr: int, base_mat: int) -> Collision:
    c = rd.at(ptr)
    buf, off, _ = rd.segment(ptr)
    quantum = np.array(struct.unpack_from("<3f", buf, off + 0x90))
    center = np.array(struct.unpack_from("<3f", buf, off + 0xA0))
    (poly_ptr,) = struct.unpack_from("<Q", buf, off + 0x88)
    (vert_ptr,) = struct.unpack_from("<Q", buf, off + 0xB0)
    vcount, pcount = struct.unpack_from("<II", buf, off + 0xD0)
    (mats_ptr,) = struct.unpack_from("<Q", buf, off + 0xF0)
    (pmi_ptr,) = struct.unpack_from("<Q", buf, off + 0x118)
    mcount = buf[off + 0x120]
    del c
    if not vcount or not pcount:
        return Collision()
    vb, vo, _ = rd.segment(vert_ptr)
    verts = np.frombuffer(vb, dtype="<i2", count=vcount * 3, offset=vo).reshape(-1, 3).astype(np.float64)
    verts = verts * quantum + center
    pb, po, _ = rd.segment(poly_ptr)
    polys = np.frombuffer(pb, dtype=np.uint8, count=pcount * 16, offset=po).reshape(-1, 16)
    mat_types = []
    if mats_ptr and mcount:
        mb, mo, _ = rd.segment(mats_ptr)
        for i in range(mcount):
            d1, _d2 = struct.unpack_from("<II", mb, mo + i * 8)
            mat_types.append(d1 & 0xFF)
    if pmi_ptr:
        ib, io, _ = rd.segment(pmi_ptr)
        poly_mat_idx = np.frombuffer(ib, dtype=np.uint8, count=pcount, offset=io)
    else:
        poly_mat_idx = np.zeros(pcount, np.uint8)

    def mat_of(i):
        k = int(poly_mat_idx[i])
        return mat_types[k] if k < len(mat_types) else base_mat

    out = Collision()
    ptype = polys[:, 0] & 7
    tri_sel = np.nonzero(ptype == 0)[0]
    if len(tri_sel):
        t = polys[tri_sel]
        idx = np.stack([t[:, 4:6].copy().view("<u2")[:, 0], t[:, 6:8].copy().view("<u2")[:, 0],
                        t[:, 8:10].copy().view("<u2")[:, 0]], 1).astype(np.int64) & 0x7FFF
        ok = (idx < vcount).all(1)
        idx = idx[ok]
        out.triangles = verts[idx]
        out.tri_materials = np.array([mat_of(i) for i in tri_sel[ok]], np.int32)
    for i in np.nonzero(ptype != 0)[0]:
        rec = bytes(polys[i])
        kind = rec[0] & 7
        m = mat_of(int(i))
        if kind == 1:      # sphere
            _t, vi, radius = struct.unpack_from("<HHf", rec, 0)
            if vi < vcount:
                out.spheres.append((verts[vi], float(radius), m))
        elif kind in (2, 4):   # capsule / cylinder
            _t, i1, radius, i2 = struct.unpack_from("<HHfH", rec, 0)
            if i1 < vcount and i2 < vcount:
                (out.capsules if kind == 2 else out.cylinders).append((verts[i1], verts[i2], float(radius), m))
        elif kind == 3:    # box: four corner indices (two diagonals)
            _t, i1, i2, i3, i4 = struct.unpack_from("<Ihhhh", rec, 0)
            ids = [x & 0x7FFF for x in (i1, i2, i3, i4)]
            if all(x < vcount for x in ids):
                p1, p2, p3, p4 = (verts[x] for x in ids)
                a1 = ((p3 + p4) - (p1 + p2)) * 0.5
                e1 = a1
                e2 = (p3 - a1) - p1
                e3 = (p4 - a1) - p1
                center_b = p1 + (e1 + e2 + e3) * 0.5
                axes, half = [], []
                for e in (e1, e2, e3):
                    ln = float(np.linalg.norm(e))
                    axes.append(e / ln if ln > 1e-9 else np.zeros(3))
                    half.append(ln * 0.5)
                ax = np.array(axes)
                # repair a degenerate axis (flat boxes) with a cross product
                for k in range(3):
                    if not np.any(ax[k]):
                        a, b = ax[(k + 1) % 3], ax[(k + 2) % 3]
                        cr = np.cross(a, b)
                        n = np.linalg.norm(cr)
                        ax[k] = cr / n if n > 1e-9 else np.eye(3)[k]
                out.boxes.append((center_b, ax, np.array(half), m))
    return out


def read_bound(rd: ResourceReader, ptr: int) -> Collision:
    if not ptr:
        return Collision()
    buf, off, _ = rd.segment(ptr)
    btype = buf[off + 0x10]
    base_mat = buf[off + 0x4C]
    radius = struct.unpack_from("<f", buf, off + 0x14)[0]
    margin = struct.unpack_from("<f", buf, off + 0x2C)[0]
    bmax = np.array(struct.unpack_from("<3f", buf, off + 0x20))
    bmin = np.array(struct.unpack_from("<3f", buf, off + 0x30))
    sphere_center = np.array(struct.unpack_from("<3f", buf, off + 0x50))
    out = Collision()
    if btype in (4, 8):
        return _read_geometry(rd, ptr, base_mat)
    if btype == 10:
        children_ptr, t1_ptr, t2_ptr = struct.unpack_from("<QQQ", buf, off + 0x70)
        (count,) = struct.unpack_from("<H", buf, off + 0xA0)
        tptr = t1_ptr or t2_ptr
        for i, cp in enumerate(rd.pointer_array(children_ptr, count)):
            if not cp:
                continue
            child = read_bound(rd, cp)
            if tptr:
                tb, to, _ = rd.segment(tptr)
                child = child.transformed(_matrix4f(tb, to + i * 64))
            out.extend(child)
        return out
    if btype == 3:
        c = (bmin + bmax) * 0.5
        out.boxes.append((c, np.eye(3), (bmax - bmin) * 0.5, base_mat))
    elif btype == 0:
        out.spheres.append((sphere_center, float(radius), base_mat))
    elif btype == 1:
        ext = np.array([0.0, radius - margin, 0.0])
        out.capsules.append((sphere_center - ext, sphere_center + ext, float(margin), base_mat))
    elif btype in (12, 13):
        # disc / cylinder around Y; approximate with a cylinder of the bound box
        c = (bmin + bmax) * 0.5
        h = (bmax - bmin) * 0.5
        out.cylinders.append((c - [0, h[1], 0], c + [0, h[1], 0], float(max(h[0], h[2])), base_mat))
    return out


def ybn_bounds(path_or_bytes, name: str = ""):
    """(min, max) of a .ybn's root bound without reading its polygons (world space for map collision)."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), name=name or str(path_or_bytes))
    rd = ResourceReader(rsc, name=name)
    buf, off, _ = rd.segment(0x50000000)
    bmax = np.array(struct.unpack_from("<3f", buf, off + 0x20))
    bmin = np.array(struct.unpack_from("<3f", buf, off + 0x30))
    return bmin, bmax


def load_ybn(path_or_bytes, name: str = "") -> Collision:
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), name=name or str(path_or_bytes))
    if rsc.version != YBN_VERSION:
        raise ResourceError(f"{name}: unexpected bounds version {rsc.version} (expected {YBN_VERSION})")
    rd = ResourceReader(rsc, name=name)
    return read_bound(rd, 0x50000000)
