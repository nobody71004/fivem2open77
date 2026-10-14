"""Placed GTA scene -> a Cyberpunk map model: meshes ("parts"), where they go, materials, textures.

Placement model (what the Open77 placer needs)
----------------------------------------------
Everything is expressed relative to an *anchor*: a point on the ground in the middle of the map.
Each part is one Cyberpunk .mesh with its own local origin and a list of placements
``(x, y, z, yaw)`` relative to the anchor.  Open77 props take a position and a yaw (no pitch or
roll), so a placement only ever carries a yaw; anything that is tilted is baked into a mesh.

  * an MLO interior becomes ONE part in MLO-local space (shell + furniture + its collision),
    placed once per MLO instance (Cedar Crest: one house, eight placements);
  * a big standalone object that only turns around Z becomes its own part (one placement per
    entity of that model);
  * everything else (small, tilted, scaled) is baked into chunk parts on a grid of cells, each
    with its origin at the cell centre so it streams by distance like the rest;
  * standalone world collision (.ybn) is cut by the same cells and carried by the chunk parts.

Moving the anchor (position + yaw) moves the whole map rigidly, which is all the placer does.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from ..gta.bounds import Collision
from ..gta.texture import NO_TEXTURE
from ..model.build import _Accum, compute_tangents
from ..model.ir import SubMesh
from . import doors as doors_mod
from . import doorways as ways_mod
from .collision import (build_colliders, glass_colliders, ground_colliders, physmat_for, prop_colliders,
                        transform_colliders)
from .materials import map_material

# objects that open.  By default every door leaf and gate panel becomes a Door device that opens
# by itself (doors.py).  With --static-doors (or for door-named objects that are not a leaf: frames,
# gatehouses) the old rule applies: doors and shutters get no collision (a closed door you cannot
# open is a wall) unless --solid-doors; gates and barriers stay solid unless --open-gates.
DOOR_RE = re.compile(r"(door|shutter)", re.I)
GATE_RE = re.compile(r"(gate|barrier)", re.I)


def passable_re(solid_doors=False, open_gates=False):
    """The name pattern of the objects built without collision, or None."""
    words = ([] if solid_doors else ["door", "shutter"]) + (["gate", "barrier"] if open_gates else [])
    return re.compile("(" + "|".join(words) + ")", re.I) if words else None
# collision shapes per mesh: a part with more spills the rest into collision-only parts placed the
# same way (1574 shapes in one mesh is proven in game; keep every body around that size)
MAX_SHAPES = 1500
# render chunks (submeshes) per mesh: a part with more hands the rest, in spatially compact groups,
# to render-only pieces placed the same way.  The game keeps a mesh's chunk count in 8 bits: Sea
# Dome's 271-chunk dome wrapped to 15 and crashed the game the moment it streamed in.  64 leaves a
# wide margin and is about what the game's own big meshes use.
MAX_CHUNKS = 64
# and, where the submeshes allow, vertices per mesh: smaller pieces cull and stream better (the dome
# was 1.17 million in one mesh)
MAX_MESH_VERTS = 400_000


def _loose_tex(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _sanitize(name: str) -> str:
    s = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", str(name).lower())).strip("_") or "part"
    if not s[0].isalpha():
        s = "p_" + s
    return s[:48]


def yaw_of(m: np.ndarray) -> float:
    return math.degrees(math.atan2(m[1, 0], m[0, 0]))


@dataclass
class MapPart:
    name: str
    kind: str                       # mlo | object | chunk
    submeshes: list = field(default_factory=list)
    colliders: list = field(default_factory=list)
    collision_note: dict = field(default_factory=dict)
    sources: Counter = field(default_factory=Counter)
    collision_only: bool = False
    door: dict | None = None        # kind "door": how the Door device opens (doors.door_kind)
    piece_of: str | None = None     # a render-only piece of that part (too many submeshes for one mesh)

    @property
    def vertex_count(self):
        return sum(s.vertex_count for s in self.submeshes)

    @property
    def triangle_count(self):
        return sum(len(s.indices) // 3 for s in self.submeshes)

    def bounds(self):
        pts = [s.positions for s in self.submeshes if len(s.positions)]
        for c in self.colliders:
            if c.vertices is not None:
                pts.append(np.asarray(c.vertices, np.float32))
            else:
                r = max(c.half) if c.kind == "box" else (c.radius + c.height * 0.5)
                pts.append(np.array([np.subtract(c.center, r), np.add(c.center, r)], np.float32))
        if not pts:
            return np.zeros(3), np.zeros(3)
        p = np.concatenate(pts)
        return p.min(0), p.max(0)


@dataclass
class Placement:
    part: str
    position: tuple                 # anchor-local metres
    yaw: float                      # degrees
    source: str = ""


@dataclass
class MapModel:
    name: str
    display_name: str
    anchor: tuple                   # GTA world position of the anchor
    spawn: tuple | None             # anchor-local spawn point (FiveM coords) if known
    parts: dict = field(default_factory=dict)
    placements: list = field(default_factory=list)
    materials: dict = field(default_factory=dict)
    textures: dict = field(default_factory=dict)     # key -> GtaTexture
    warnings: list = field(default_factory=list)
    report: dict = field(default_factory=dict)

    def bounds(self):
        """Anchor-local axis-aligned bounds of the placed map (yaw 0)."""
        pts = []
        for pl in self.placements:
            p = self.parts[pl.part]
            mn, mx = p.bounds()
            c, s = math.cos(math.radians(pl.yaw)), math.sin(math.radians(pl.yaw))
            for x in (mn[0], mx[0]):
                for y in (mn[1], mx[1]):
                    for z in (mn[2], mx[2]):
                        pts.append((pl.position[0] + c * x - s * y, pl.position[1] + s * x + c * y, pl.position[2] + z))
        if not pts:
            return (0, 0, 0), (0, 0, 0)
        p = np.array(pts)
        return tuple(p.min(0)), tuple(p.max(0))


class MapBuilder:
    def __init__(self, scene, name: str, display_name: str | None = None, log=print, **opts):
        self.scene = scene
        self.name = _sanitize(name)
        self.display_name = display_name or name
        self.log = log
        self.opts = opts
        self.cell = float(opts.get("cell", 48.0))
        self.instance_min_tris = int(opts.get("instance_min_tris", 1500))
        self.solid_doors = bool(opts.get("solid_doors", False))
        self.open_gates = bool(opts.get("open_gates", False))
        self.passable = passable_re(self.solid_doors, self.open_gates)
        self.exclude = {e.lower() for e in opts.get("exclude", []) or []}
        self.collision_budget = int(opts.get("collision_budget", 2500))
        self.no_collision = bool(opts.get("no_collision", False))
        self.ground_spacing = float(opts.get("ground_spacing", 2.0))
        self.dynamic_doors = not bool(opts.get("static_doors", False)) and not self.no_collision
        self.door_map = ways_mod.normalize_door_map(opts.get("door_map"))
        self._door_parts = {}           # archetype -> door part name, or None (stays static by its name)
        self._door_how = {}             # door part -> how it was found: name | doorway | mapped | generated
        self._gen_parts = {}            # (opening, width, height) -> generated door part
        self._mlo_ways = {}             # MLO part -> (doorways, {doorway key: [door parts]}, first instance)
        self.walkthrough_names = set()  # objects without collision (door devices, mapped open or removed)
        self.model = MapModel(name=self.name, display_name=self.display_name, anchor=(0, 0, 0), spawn=None)
        self._geo = {}
        self._tex_keys = {}
        self._all_ytd = None
        self.missing_textures = Counter()
        self.dropped_untextured = Counter()

    # ------------------------------------------------------------------ textures / materials
    def _texture_key(self, tex):
        k = (tex.name.lower(), tex.width, tex.height, tex.format, hash(tex.data[:4096]))
        if k not in self._tex_keys:
            base = _sanitize(tex.name)
            key = base
            n = 1
            while key in self.model.textures:
                n += 1
                key = f"{base}_{n}"
            self._tex_keys[k] = key
            self.model.textures[key] = tex
        return self._tex_keys[k]

    def _lookup_factory(self, h, drawable):
        own = dict(drawable.shader_group.textures)
        ext = self.scene.textures_for(h)
        missing = self.missing_textures

        def lookup(tname):
            if not tname:
                return None
            n = tname.lower()
            if n in NO_TEXTURE:
                return None
            t = own.get(n) or ext.get(n)
            if t is None:
                t = self.scene.find_texture(n)          # any dictionary of the resource (names indexed)
            if t is None:
                # a near name: shaders misspell textures ("lightsBW" for "lights BW")
                loose = _loose_tex(n)
                cand = [k for k in list(own) + list(ext) if _loose_tex(k) == loose]
                if cand:
                    t = own.get(cand[0]) or ext.get(cand[0])
                else:
                    alt = self.scene.find_texture_loose(loose)
                    t = alt
            if t is None or not t.supported or not t.data:
                missing[n] += 1
                return None
            return self._texture_key(t)
        return lookup

    # ------------------------------------------------------------------ geometry per drawable
    def drawable_geometry(self, h):
        """[(material key, attrs, tri)] in model space, cached per archetype."""
        if h in self._geo:
            return self._geo[h]
        d = self.scene.drawable_for(h)
        out = []
        if d is None:
            self._geo[h] = out
            return out
        lookup = self._lookup_factory(h, d)
        shaders = d.shader_group.shaders
        sk = d.skeleton
        world = [sk.world_matrix(i) for i in range(len(sk.bones))] if sk else []
        models = d.models.get("high") or d.models.get("med") or d.models.get("low") or d.models.get("vlow") or []
        for model in models:
            for g in model.geometries:
                a = g.attrs
                if "position" not in a or g.triangle_count == 0:
                    continue
                pos = a["position"].astype(np.float64)
                n = len(pos)
                tri = g.indices.reshape(-1, 3).astype(np.int64)
                tri = tri[(tri < n).all(1)]
                if not len(tri):
                    continue
                nrm = a.get("normal")
                nrm = nrm.astype(np.float64) if nrm is not None else None
                tan = a.get("tangent")
                tan = tan.astype(np.float64) if tan is not None and tan.shape[1] >= 4 else None
                if not model.has_skin and world and model.bone_index < len(world):
                    w = world[model.bone_index]
                    if not np.allclose(w, np.eye(4)):
                        pos = pos @ w[:3, :3].T + w[:3, 3]
                        if nrm is not None:
                            nrm = nrm @ w[:3, :3].T
                        if tan is not None:
                            tan = np.concatenate([tan[:, :3] @ w[:3, :3].T, tan[:, 3:4]], 1)
                if nrm is None:
                    nrm = _vertex_normals(pos, tri)
                uv0 = a.get("texcoord0")
                uv0 = uv0.astype(np.float64)[:, :2] if uv0 is not None else np.zeros((n, 2))
                uv1 = a.get("texcoord1")
                uv1 = uv1.astype(np.float64)[:, :2] if uv1 is not None else None
                if tan is None:
                    tan = compute_tangents(pos, _normalize(nrm), uv0, tri).astype(np.float64)
                nrm, tan = _repair_frames(pos, tri, nrm, tan)
                shader = shaders[g.shader_index] if g.shader_index < len(shaders) else None
                mat = map_material(shader, lookup)
                if getattr(mat, "missing_diffuse", None) and mat.alpha in ("test", "blend") \
                        and mat.kind not in ("glass", "water"):
                    # a decal, a grass card or a cut-out fence whose texture is GTA's own: without
                    # it that is an opaque white sheet over the surface below; leave it out
                    self.dropped_untextured[mat.missing_diffuse] += 1
                    continue
                self.model.materials.setdefault(mat.key, mat)
                out.append((mat.key, {"position": pos, "normal": nrm, "tangent": tan, "uv0": uv0, "uv1": uv1,
                                      "color": None}, tri))
        self._geo[h] = out
        return out

    # ------------------------------------------------------------------ helpers
    def _add_geometry(self, accs, h, m: np.ndarray):
        """Bake archetype h with transform m (4x4, part space) into the part accumulators."""
        r3 = m[:3, :3]
        det = np.linalg.det(r3)
        try:
            nmat = np.linalg.inv(r3).T
        except np.linalg.LinAlgError:
            return
        for mat, a, tri in self.drawable_geometry(h):
            pos = a["position"] @ r3.T + m[:3, 3]
            nrm = _normalize(a["normal"] @ nmat.T)
            t3 = _normalize(a["tangent"][:, :3] @ r3.T)
            tw = a["tangent"][:, 3:4] * (1.0 if det >= 0 else -1.0)
            t = tri if det >= 0 else tri[:, [0, 2, 1]]
            attrs = {"position": pos, "normal": nrm, "tangent": np.concatenate([t3, tw], 1),
                     "uv0": a["uv0"], "uv1": a["uv1"], "color": None}
            acc = accs.setdefault(mat, _Accum(mat, 1))
            acc.add(attrs, t)

    @staticmethod
    def _finish(accs) -> list:
        subs = []
        for mat in sorted(accs):
            for sm in accs[mat].build():
                subs.append(sm)
        return subs

    def _is_door(self, name: str) -> bool:
        """Built without collision: mapped "open" in the door mapping, or (unless mapped "solid")
        doors and shutters by name, gates and barriers with --open-gates."""
        mode = self.door_map["objects"].get((name or "").lower(), "auto")
        if mode in ("open", "remove"):
            return True
        if mode == "solid":
            return False
        return bool(self.passable and self.passable.search(name or ""))

    def walkthrough(self):
        """What the coverage check leaves out of the FiveM collision it compares against: an
        object with a .search(name) like a regex (door devices, mapped open or removed, doors
        by name)."""
        names, passable, mapped = set(self.walkthrough_names), self.passable, self.door_map["objects"]

        class _Match:
            def search(self, name):
                n = (name or "").lower()
                if n in names or mapped.get(n) in ("open", "remove"):
                    return True
                if mapped.get(n) == "solid":
                    return False
                return bool(passable and passable.search(name or ""))
        return _Match()

    def _entity_colliders(self, h, m, name):
        """Collision of one placed object (furniture, props, stairs): see prop_colliders."""
        if self.no_collision or self._is_door(name):
            return []
        col = self.scene.entity_collision(h)
        if col is None or col.empty:
            return []
        # in the object's own frame (its grid follows its edges), then placed
        return transform_colliders(prop_colliders(col), m)

    # ------------------------------------------------------------------ doors and gates
    def _door_part(self, h, name, mode="auto", matrix=None, doorways=None):
        """Name of the door part of archetype h when its objects become opening Door devices, or
        None when they stay static.  A door is found by its name (door, shutter, gate, barrier),
        by the door mapping (``mode`` hinged / sliding / rolling), or -- for an MLO child placed by
        ``matrix`` -- by standing in one of the MLO's ``doorways`` as a door leaf."""
        if not self.dynamic_doors or mode in ("open", "solid", "remove"):
            return None
        cached = self._door_parts.get(h, "?")
        if cached not in ("?", None):
            return cached
        forced = {"hinged": doors_mod.HINGED, "sliding": doors_mod.SLIDING, "rolling": doors_mod.ROLLING}.get(mode)
        is_door, is_gate = bool(DOOR_RE.search(name or "")), bool(GATE_RE.search(name or ""))
        info, how, phys_door = None, None, is_door
        if forced or ((is_door or is_gate) and cached != None):  # noqa: E711 - "?" = not decided yet
            geo = self.drawable_geometry(h)
            if geo:
                pts = np.concatenate([a["position"] for _, a, _ in geo])
                if forced:
                    info = doors_mod.door_kind(name, pts.min(0), pts.max(0), is_door=True, is_gate=False, opening=forced)
                    how, phys_door = "mapped", True
                    if info is None:
                        ext = pts.max(0) - pts.min(0)
                        self._warn_once(f"door_map: {name} is not a door leaf ({ext[0]:.2f} x {ext[1]:.2f} x {ext[2]:.2f} m: "
                                        "thinner than 0.6 m, at most 6 m wide and tall); it stays static")
                else:
                    info = doors_mod.door_kind(name, pts.min(0), pts.max(0), is_door=is_door, is_gate=is_gate)
                    how = "name"
        if info is None and not forced and doorways and matrix is not None:
            geo = self.drawable_geometry(h)
            if geo:
                pts = np.concatenate([a["position"] for _, a, _ in geo])
                mn, mx = pts.min(0), pts.max(0)
                w = ways_mod.leaf_doorway(mn, mx, matrix, doorways)
                if w is not None:
                    _c, _ax, width, _t, z0, z1 = ways_mod.leaf_frame(mn, mx, matrix)
                    opening = ways_mod.opening_for(name, width, z1 - z0, w)
                    info = doors_mod.door_kind(name, mn, mx, is_door=True, is_gate=False, opening=opening)
                    how, phys_door = "doorway", True
        if info is None:
            if forced or is_door or is_gate:
                self._door_parts[h] = None          # decided by its name: never a door
            return None
        pname = "door_" + _sanitize(name)
        n = 1
        while pname in self.model.parts:
            n += 1
            pname = f"door_{_sanitize(name)[:40]}_{n}"
        part = MapPart(name=pname, kind="door", door=info)
        accs = {}
        # device frame: on the hinge (model origin) or the middle of a sliding leaf
        self._add_geometry(accs, h, _translate([-info["pivot"][0], -info["pivot"][1], 0.0]))
        part.submeshes = self._finish(accs)
        part.colliders = [doors_mod.leaf_box(info, self._leaf_physmat(h, phys_door))]
        part.sources[name] += 0
        self.model.parts[pname] = part
        self._door_parts[h] = pname
        self._door_how[pname] = how
        self.walkthrough_names.add((name or "").lower())
        return pname

    def _leaf_way(self, ch, ways):
        """The doorway an MLO child (a door leaf) stands in, or None."""
        geo = self.drawable_geometry(ch.archetype) if ways else []
        if not geo:
            return None
        pts = np.concatenate([a["position"] for _, a, _ in geo])
        return ways_mod.leaf_doorway(pts.min(0), pts.max(0), ch.matrix, ways)

    def _generated_door_part(self, info):
        """A door made for a doorway that has none: a plain leaf (one part per size and opening)."""
        key = (info["opening"], round(info["width"], 2), round(info["height"], 2))
        if key in self._gen_parts:
            return self._gen_parts[key]
        from ..model.ir import Material
        metal = info["opening"] != doors_mod.HINGED
        mkey = "m_door_metal" if metal else "m_door_wood"
        if mkey not in self.model.materials:
            self.model.materials[mkey] = Material(key=mkey, kind="textured",
                                                  color=(0.42, 0.44, 0.46, 1.0) if metal else (0.30, 0.20, 0.13, 1.0),
                                                  roughness=0.45 if metal else 0.7, metalness=0.6 if metal else 0.0)
        pname = f"door_made_{info['opening'].lower()}_{int(round(info['width'] * 100))}x{int(round(info['height'] * 100))}"
        part = MapPart(name=pname, kind="door", door=dict(info))
        part.submeshes = [ways_mod.leaf_mesh(info, mkey)]
        part.colliders = [doors_mod.leaf_box(info, "metal.physmat" if metal else "wood.physmat")]
        self.model.parts[pname] = part
        self._gen_parts[key] = pname
        self._door_how[pname] = "generated"
        return pname

    def _warn_once(self, msg):
        if msg not in self.model.warnings:
            self.model.warnings.append(msg)
            self.log(f"[{self.name}] {msg}")

    def _doorways(self, report):
        """Every MLO doorway's status (open, door, blocked, window) and what blocks it, after the
        door mapping cut its doorways open (again: glass slabs come after the cut).  Other parts'
        collision near the MLO (a building shell around it) counts as a blocker too."""
        out, cands = [], []
        anchors = {pl.part: pl for pl in self.model.placements}
        for pname, (ways, in_way, first) in self._mlo_ways.items():
            part = self.model.parts[pname]
            pl0 = anchors.get(pname)
            counts = Counter(pl.part for pl in self.model.placements)
            for w in ways:
                if self.door_map["doorways"].get(w.key, "auto") == "auto" or self.no_collision:
                    continue
                part.colliders, _, _ = ways_mod.carve(w, part.colliders)
                if pl0 is None:
                    continue
                # a building shell (or any object placed once) closing the doorway is cut too
                for pl in self.model.placements:
                    op = self.model.parts[pl.part]
                    if pl.part == pname or op.kind == "door" or not op.colliders or counts[pl.part] != 1:
                        continue
                    if np.linalg.norm(np.subtract(pl.position, pl0.position)) > 400.0:
                        continue
                    w2 = w.transformed(np.linalg.inv(_place_matrix(pl)) @ _place_matrix(pl0))
                    op.colliders, removed, _ = ways_mod.carve(w2, op.colliders)
                    if removed:
                        self.log(f"[{self.name}] doorway {w.key}: {removed} boxes of {pl.part} cut out")
            # everything else's collision in this MLO's frame (first placement): only the boxes that
            # can reach one of its doorways (a city district has hundreds of thousands around)
            others = []
            if pl0 is not None and ways:
                M0 = np.linalg.inv(_place_matrix(pl0))
                radius = max((w.width + w.height for w in ways), default=0.0) + 60.0
                wc = np.array([w.center for w in ways], float)
                reach = np.array([w.width + w.height for w in ways], float)
                for pl in self.model.placements:
                    if pl.part == pname or self.model.parts[pl.part].kind == "door":
                        continue
                    if np.linalg.norm(np.subtract(pl.position, pl0.position)) > radius + 200.0:
                        continue
                    op = self.model.parts[pl.part]
                    boxes = [c for c in op.colliders if c.kind == "box"]
                    if not boxes:
                        continue
                    T = M0 @ _place_matrix(pl)
                    sc = float(np.cbrt(abs(np.linalg.det(T[:3, :3])))) or 1.0
                    ctr = np.array([c.center for c in boxes], float) @ T[:3, :3].T + T[:3, 3]
                    size = np.linalg.norm(np.array([c.half for c in boxes], float), axis=1) * sc
                    gap = (np.linalg.norm(ctr[:, None, :] - wc[None, :, :], axis=2) - reach[None, :]).min(1)
                    near = [c for c, ok in zip(boxes, gap < size + 1.0) if ok]
                    if not near:
                        continue
                    moved = transform_colliders(near, T)
                    for c in moved:
                        c.source = c.source if c.source and c.source not in ("collision",) else pl.part
                    others += moved
            # leaf-shaped objects near a doorway that stayed static: the Studio offers them as doors
            mine = []
            for ch in first.children:
                lname = ch.name.lower()
                if lname in self.walkthrough_names or self._door_parts.get(ch.archetype) or lname in self.exclude:
                    continue
                geo = self.drawable_geometry(ch.archetype)
                if not geo:
                    continue
                pts = np.concatenate([a["position"] for _, a, _ in geo])
                mn, mx = pts.min(0), pts.max(0)
                if doors_mod.door_kind(ch.name, mn, mx, is_door=True, is_gate=False) is None:
                    continue
                c, _ax, width, thick, z0, z1 = ways_mod.leaf_frame(mn, mx, ch.matrix)
                doorlike = [w for w in ways if w.kind == "door"
                            and abs(z0 - (w.floor if w.floor is not None else w.zmin)) < 0.5]
                near = min(((float(np.linalg.norm((c - w.center)[:2])), w) for w in doorlike), key=lambda t: t[0],
                           default=None)
                if near is None or near[0] > 2.5 or z1 - z0 < 1.5:
                    continue
                mine.append({"name": ch.name, "mlo": pname, "doorway": near[1].key, "distance": round(near[0], 2),
                             "width": round(width, 2), "height": round(z1 - z0, 2), "thickness": round(thick, 2),
                             "mode": self.door_map["objects"].get(lname, "auto")})
            cands += mine
            suspects = {c["name"] for c in mine}
            pool = list(part.colliders) + others
            boxes = ways_mod.box_arrays(pool)
            for w in ways:
                entry = ways_mod.doorway_status(w, pool, in_way.get(w.key, ()), suspects, boxes=boxes)
                entry["mode"] = self.door_map["doorways"].get(w.key, "auto")
                out.append(entry)
        seen, uniq = set(), []
        for c in cands:
            if (c["name"], c["doorway"]) not in seen:
                seen.add((c["name"], c["doorway"]))
                uniq.append(c)
        report["door_candidates"] = uniq[:200]
        bad = [d for d in out if d["status"] == "blocked"]
        if bad:
            self.log(f"[{self.name}] doorways blocked: " + ", ".join(
                f"{d['key']} ({', '.join(b['source'] for b in d['blockers'][:3]) or 'collision'})" for d in bad[:8])
                + ("" if len(bad) <= 8 else f" and {len(bad) - 8} more") + " -- the door mapping opens them")
        return out

    def _leaf_physmat(self, h, is_door):
        col = self.scene.entity_collision(h)
        if col is not None and len(col.tri_materials):
            vals, counts = np.unique(np.asarray(col.tri_materials, np.int64), return_counts=True)
            return physmat_for(int(vals[np.argmax(counts)]))
        return "wood.physmat" if is_door else "metal.physmat"

    def _door_placement(self, pname, L, source, name):
        """One Door device: the object's anchor-local transform L (yaw only), on its pivot."""
        part = self.model.parts[pname]
        pv = L @ np.array([part.door["pivot"][0], part.door["pivot"][1], 0.0, 1.0])
        self.model.placements.append(Placement(pname, tuple(float(v) for v in pv[:3]), yaw_of(L), source))
        part.sources[name] += 1

    # ------------------------------------------------------------------ anchor
    def _choose_anchor(self):
        opt = self.opts.get("anchor")
        coords = self.opts.get("coords")
        mins, maxs = [], []
        for inst in self.scene.instances:
            mins.append(inst.matrix[:3, 3])
        for mlo in self.scene.mlos:
            mins.append(mlo.matrix[:3, 3])
        pts = np.array(mins) if mins else np.zeros((1, 3))
        if opt:
            ax, ay, az = (float(v) for v in opt)
            return (ax, ay, az), coords
        if coords:
            ax, ay = coords[0], coords[1]
            top = coords[2] + 2.0
        else:
            ax, ay = float((pts[:, 0].min() + pts[:, 0].max()) / 2), float((pts[:, 1].min() + pts[:, 1].max()) / 2)
            top = float(pts[:, 2].max()) + 20.0
        ground = self._ground_at(ax, ay, top)
        if ground is None:
            ground = (coords[2] - 1.0) if coords else float(pts[:, 2].min())
        return (float(ax), float(ay), float(ground)), coords

    def _ground_at(self, x, y, top):
        """Highest upward-facing collision surface below `top` at (x, y) (GTA world)."""
        best = None
        cols = [c for _, c in self.scene.world_collisions]
        for mlo in self.scene.mlos:
            if mlo.collision is not None:
                cols.append(mlo.collision.transformed(mlo.matrix))
        for c in cols:
            t = c.triangles
            if not len(t):
                continue
            mn, mx = t.min(1), t.max(1)
            sel = (mn[:, 0] <= x) & (mx[:, 0] >= x) & (mn[:, 1] <= y) & (mx[:, 1] >= y)
            for tr in t[sel]:
                z = _tri_height(tr, x, y)
                if z is not None and z <= top and (best is None or z > best):
                    best = z
        return best

    # ------------------------------------------------------------------ build
    def build(self) -> MapModel:
        sc = self.scene
        self.missing_textures = Counter()
        self.dropped_untextured = Counter()
        anchor, coords = self._choose_anchor()
        self.model.anchor = anchor
        A = np.eye(4)
        A[:3, 3] = -np.asarray(anchor)
        if coords:
            self.model.spawn = tuple(float(c - a) for c, a in zip(coords[:3], anchor))
        report = {"mlo_parts": [], "object_parts": [], "chunk_parts": [], "skipped": Counter(), "tilted_mlo": 0}

        # ---------------- MLO interiors: one part per MLO archetype, placed per instance
        by_arch = defaultdict(list)
        for mlo in sc.mlos:
            by_arch[mlo.archetype].append(mlo)
        for arch, insts in by_arch.items():
            first = insts[0]
            pname = "mlo_" + _sanitize(first.name)
            part = MapPart(name=pname, kind="mlo")
            accs = {}
            cols = []
            doors = []
            a = (getattr(self.scene, "archetypes", None) or {}).get(arch)
            ways = ways_mod.mlo_doorways(a.mlo if a is not None else None, first.name)
            ways_mod.classify(ways, first.collision.triangles if first.collision is not None else None)
            in_way = defaultdict(list)          # doorway key -> door parts standing in it
            for ch in first.children:
                lname = ch.name.lower()
                if lname in self.exclude:
                    report["skipped"][ch.name] += 1
                    continue
                mode = self.door_map["objects"].get(lname, "auto")
                if mode == "remove":
                    report["skipped"][ch.name] += 1
                    self.walkthrough_names.add(lname)
                    continue
                dp = None
                if ch.yaw_only and ch.unit_scale:
                    dp = self._door_part(ch.archetype, ch.name, mode=mode, matrix=ch.matrix, doorways=ways)
                elif mode in ("hinged", "sliding", "rolling"):
                    self._warn_once(f"door_map: {ch.name} is tilted or scaled in {first.name}; a door device only "
                                    "turns about Z, so it stays static")
                if dp:
                    doors.append((dp, ch))
                    w = self._leaf_way(ch, ways)
                    if w is not None:
                        in_way[w.key].append(dp)
                    continue
                self._add_geometry(accs, ch.archetype, ch.matrix)
                new = self._entity_colliders(ch.archetype, ch.matrix, ch.name)
                for c in new:
                    c.source = ch.name
                cols += new
                part.sources[ch.name] += 1
            if first.collision is not None and not self.no_collision:
                mlo_cols, note = build_colliders(first.collision, budget=self.collision_budget)
                for c in mlo_cols:
                    c.source = c.source or "collision"
                cols += mlo_cols
                part.collision_note = note
            # doorways the door mapping opens, or gives a generated door
            gen = []
            for w in ways:
                wm = self.door_map["doorways"].get(w.key, "auto")
                if wm == "auto" or self.no_collision:
                    continue
                cols, removed, stuck = ways_mod.carve(w, cols)
                if stuck:
                    self._warn_once(f"door_map: doorway {w.key} has wall boxes across it that are not lined up "
                                    f"with it ({', '.join(sorted(set(stuck)))}); they stay")
                if wm.startswith("door-") and self.dynamic_doors:
                    for info, frame in ways_mod.generated_leaves(w, wm):
                        gp = self._generated_door_part(info)
                        gen.append((gp, frame, w))
                        in_way[w.key].append(gp)
            part.submeshes = self._finish(accs)
            part.colliders = cols
            self.model.parts[pname] = part
            if ways:
                self._mlo_ways[pname] = (ways, in_way, first)
            for inst in insts:
                L = A @ inst.matrix
                r = L[:3, :3]
                if abs(r[2, 2] - 1.0) > 1e-3:
                    report["tilted_mlo"] += 1
                    self.model.warnings.append(f"{inst.source}: MLO instance is tilted; placed with its yaw only")
                self.model.placements.append(Placement(pname, tuple(float(v) for v in L[:3, 3]), yaw_of(L), inst.source))
                for dp, ch in doors:
                    self._door_placement(dp, L @ ch.matrix, f"{inst.source}#{ch.name}", ch.name)
                for gp, frame, w in gen:
                    self._door_placement(gp, L @ frame, f"{inst.source}#{w.key}", gp)
            report["mlo_parts"].append({"part": pname, "instances": len(insts), "children": len(first.children),
                                        "doors": len(doors) + len(gen), "doorways": len(ways),
                                        "vertices": part.vertex_count, "triangles": part.triangle_count,
                                        "colliders": len(part.colliders)})

        # ---------------- world entities: objects (instanced) or baked chunks
        groups = defaultdict(list)
        for inst in sc.instances:
            if inst.name.lower() in self.exclude:
                report["skipped"][inst.name] += 1
                continue
            groups[inst.archetype].append(inst)
        chunk_accs = defaultdict(dict)
        chunk_cols = defaultdict(list)
        chunk_src = defaultdict(Counter)

        def cell_of(p):
            return (int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell)))

        def cell_origin(c):
            return np.array([(c[0] + 0.5) * self.cell, (c[1] + 0.5) * self.cell, 0.0])

        for arch, insts in groups.items():
            mode = self.door_map["objects"].get(insts[0].name.lower(), "auto")
            if mode == "remove":
                report["skipped"][insts[0].name] += len(insts)
                self.walkthrough_names.add(insts[0].name.lower())
                continue
            if all(i.yaw_only and i.unit_scale for i in insts):
                dp = self._door_part(arch, insts[0].name, mode=mode)
                if dp:
                    for inst in insts:
                        self._door_placement(dp, A @ inst.matrix, inst.source, inst.name)
                    continue
            geo = self.drawable_geometry(arch)
            tris = sum(len(t) for _, _, t in geo)
            if not geo:
                report["skipped"][insts[0].name] += len(insts)
                continue
            as_object = tris >= self.instance_min_tris and all(i.yaw_only and i.unit_scale for i in insts)
            if as_object:
                pname = "obj_" + _sanitize(insts[0].name)
                part = MapPart(name=pname, kind="object")
                accs = {}
                self._add_geometry(accs, arch, np.eye(4))
                part.submeshes = self._finish(accs)
                part.colliders = self._object_colliders(arch, insts[0].name)
                part.sources[insts[0].name] += len(insts)
                self.model.parts[pname] = part
                for inst in insts:
                    L = A @ inst.matrix
                    self.model.placements.append(Placement(pname, tuple(float(v) for v in L[:3, 3]), yaw_of(L),
                                                           inst.source))
                report["object_parts"].append({"part": pname, "instances": len(insts), "vertices": part.vertex_count,
                                               "triangles": part.triangle_count, "colliders": len(part.colliders)})
                continue
            for inst in insts:
                L = A @ inst.matrix
                c = cell_of(L[:3, 3])
                C = np.eye(4)
                C[:3, 3] = -cell_origin(c)
                self._add_geometry(chunk_accs[c], arch, C @ L)
                chunk_cols[c] += self._entity_colliders(arch, C @ L, inst.name)
                chunk_src[c][inst.name] += 1

        # ---------------- standalone world collision, cut by cells
        if not self.no_collision:
            for wname, col in sc.world_collisions:
                lc = col.transformed(A)
                if len(lc.triangles):
                    cent = lc.triangles.mean(1)
                    keys = np.floor(cent[:, :2] / self.cell).astype(np.int64)
                    for key in {tuple(k) for k in keys.tolist()}:
                        sel = (keys[:, 0] == key[0]) & (keys[:, 1] == key[1])
                        sub = Collision(triangles=lc.triangles[sel] - cell_origin(key), tri_materials=lc.tri_materials[sel])
                        cols, note = ground_colliders(sub, spacing=self.ground_spacing)
                        chunk_cols[key] += cols
                        chunk_src[key][f"{wname}.ybn"] += 0
                prim = Collision(boxes=lc.boxes, spheres=lc.spheres, capsules=lc.capsules, cylinders=lc.cylinders)
                if not prim.empty:
                    for kind_list in ("boxes", "spheres", "capsules", "cylinders"):
                        for item in getattr(prim, kind_list):
                            centre = np.asarray(item[0], float)
                            key = cell_of(centre)
                            one = Collision(**{kind_list: [item]})
                            cols, _ = build_colliders(one.transformed(_translate(-cell_origin(key))))
                            chunk_cols[key] += cols

        for key in sorted(set(chunk_accs) | set(chunk_cols)):
            pname = f"chunk_{key[0] + 500:03d}_{key[1] + 500:03d}"
            part = MapPart(name=pname, kind="chunk")
            part.submeshes = self._finish(chunk_accs.get(key, {}))
            part.colliders = chunk_cols.get(key, [])
            part.sources = chunk_src.get(key, Counter())
            if not part.submeshes:
                if not part.colliders:
                    continue
                part.collision_only = True
            self.model.parts[pname] = part
            o = cell_origin(key)
            self.model.placements.append(Placement(pname, tuple(float(v) for v in o), 0.0, "chunk"))
            report["chunk_parts"].append({"part": pname, "vertices": part.vertex_count, "triangles": part.triangle_count,
                                          "colliders": len(part.colliders), "collision_only": part.collision_only})

        report["glass"] = self._glass(report)
        report["doorways"] = self._doorways(report)
        self._split_heavy_parts(report)
        self._split_wide_parts(report)
        report["doors"] = [{"part": p.name, "opening": p.door["opening"], "width": p.door["width"],
                            "height": p.door["height"], "devices": sum(1 for pl in self.model.placements if pl.part == p.name),
                            "sources": dict(p.sources), "found": self._door_how.get(p.name, "name")}
                           for p in self.model.parts.values() if p.kind == "door"]
        report["door_map"] = self.door_map
        report["skipped"] = dict(report["skipped"])
        report["textures_missing"] = dict(self.missing_textures.most_common())
        report["untextured_cutouts_dropped"] = dict(self.dropped_untextured.most_common())
        if self.missing_textures:
            guessed = sum(1 for m in self.model.materials.values() if getattr(m, "missing_diffuse", None))
            self.model.warnings.append(
                f"{len(self.missing_textures)} textures are GTA's own (the map only names them, e.g. "
                + ", ".join(list(self.missing_textures)[:6]) + f"): {guessed} surfaces keep the colour their name "
                f"suggests, {sum(self.dropped_untextured.values())} decal/cut-out pieces that would be white sheets are left out")
        report["materials"] = len(self.model.materials)
        report["textures"] = len(self.model.textures)
        report["anchor_gta"] = [round(v, 3) for v in anchor]
        mn, mx = self.model.bounds()
        report["bounds_local"] = [[round(float(v), 2) for v in mn], [round(float(v), 2) for v in mx]]
        self.model.report = report
        return self.model

    def _glass(self, report):
        """Window panes with no collision get thin slabs (collision.glass_colliders), per part in
        its own frame.  Door leaves are left alone (their leaf box covers them)."""
        glass = {k for k, m in self.model.materials.items() if m.kind == "glass"}
        out = {}
        if self.no_collision or not glass:
            return out
        for pname, part in self.model.parts.items():
            if part.kind == "door" or part.collision_only:
                continue
            tris = [np.asarray(sm.positions, float)[np.asarray(sm.indices, np.int64).reshape(-1, 3)]
                    for sm in part.submeshes if sm.material in glass and len(sm.indices)]
            if not tris:
                continue
            extra = glass_colliders(part.colliders, np.concatenate(tris))
            if extra:
                part.colliders = list(part.colliders) + extra
                out[pname] = len(extra)
        if out:
            self.log(f"[{self.name}] window glass without collision: {sum(out.values())} slabs added "
                     f"({', '.join(f'{k} {v}' for k, v in out.items())})")
        return out

    def _split_heavy_parts(self, report):
        """Parts with more than MAX_SHAPES colliders keep the first MAX_SHAPES (spatially sorted, so
        every body stays compact) and hand the rest to collision-only parts `<part>_c<n>` with
        the same placements."""
        report["split_parts"] = {}
        for pname in list(self.model.parts):
            part = self.model.parts[pname]
            if len(part.colliders) <= MAX_SHAPES:
                continue
            cols = sorted(part.colliders, key=lambda c: (math.floor(c.center[0] / 8.0), math.floor(c.center[1] / 8.0),
                                                          c.center[2]))
            part.colliders = cols[:MAX_SHAPES]
            places = [p for p in self.model.placements if p.part == pname]
            names = []
            for i in range(MAX_SHAPES, len(cols), MAX_SHAPES):
                sub = MapPart(name=f"{pname}_c{i // MAX_SHAPES}", kind=part.kind, colliders=cols[i:i + MAX_SHAPES],
                              collision_only=True)
                sub.sources = Counter(part.sources)
                self.model.parts[sub.name] = sub
                names.append(sub.name)
                for p in places:
                    self.model.placements.append(Placement(sub.name, p.position, p.yaw, p.source))
            report["split_parts"][pname] = names

    def _split_wide_parts(self, report):
        """Parts with more than MAX_CHUNKS submeshes (or far more than MAX_MESH_VERTS vertices) keep
        one spatially compact group of them, and their colliders, and hand the other groups to
        render-only pieces `<part>_m<n>` (``piece_of`` the part) with the same placements.  Door
        leaves stay whole: a Door device has one mesh."""
        report["mesh_split_parts"] = {}
        for pname in list(self.model.parts):
            part = self.model.parts[pname]
            subs = part.submeshes
            if part.kind == "door" or len(subs) < 2:
                continue
            if len(subs) <= MAX_CHUNKS and part.vertex_count <= MAX_MESH_VERTS:
                continue
            if len({getattr(s, "lod", 1) for s in subs}) > 1:
                self.model.warnings.append(f"{pname}: {len(subs)} submeshes over several LODs left in one mesh")
                continue
            groups = _chunk_groups(subs)
            if len(groups) < 2:
                continue
            groups.sort(key=lambda g: -sum(s.vertex_count for s in g))
            part.submeshes = groups[0]
            places = [p for p in self.model.placements if p.part == pname]
            names = []
            for i, g in enumerate(groups[1:], 1):
                piece = MapPart(name=f"{pname}_m{i}", kind=part.kind, submeshes=g, piece_of=pname)
                piece.sources = Counter(part.sources)
                self.model.parts[piece.name] = piece
                names.append(piece.name)
                for p in places:
                    self.model.placements.append(Placement(piece.name, p.position, p.yaw, p.source))
            report["mesh_split_parts"][pname] = {"pieces": names, "chunks": [len(g) for g in groups],
                                                 "vertices": [sum(s.vertex_count for s in g) for g in groups]}
        if report["mesh_split_parts"]:
            self.log(f"[{self.name}] big meshes in pieces: " + ", ".join(
                f"{k} {len(v['chunks'])} ({'+'.join(str(c) for c in v['chunks'])} submeshes)"
                for k, v in report["mesh_split_parts"].items()))

    def _object_colliders(self, arch, name):
        if self.no_collision or self._is_door(name):
            return []
        col = self.scene.entity_collision(arch)
        if col is None or col.empty:
            return []
        return prop_colliders(col)


# ----------------------------------------------------------------------------- small helpers
def _normalize(a):
    a = np.asarray(a, np.float64)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(n, 1e-12)


def _repair_frames(pos, tri, nrm, tan):
    """(normals, tangents) a mesh can use.  Some sources carry zero or broken normals (Sea Dome: 349
    in the dome) that reach the game as NaN once normalised: those take the surface's own normal.
    Broken tangents get a direction across their normal, and a broken handedness +1."""
    nrm = np.where(np.isfinite(nrm), nrm, 0.0)
    bad = np.linalg.norm(nrm, axis=1) < 1e-4
    if bad.any():
        nrm = nrm.copy()
        nrm[bad] = _vertex_normals(pos, tri)[bad]
    if tan is None:
        return nrm, tan
    t3 = tan[:, :3]
    w = tan[:, 3:4]
    tbad = ~np.isfinite(t3).all(1) | (np.linalg.norm(np.where(np.isfinite(t3), t3, 0.0), axis=1) < 1e-4)
    wbad = ~np.isfinite(w[:, 0]) | (w[:, 0] == 0.0)
    if not (tbad.any() or wbad.any()):
        return nrm, tan
    tan = tan.copy()
    if tbad.any():
        n = _normalize(nrm[tbad])
        axis = np.where(np.abs(n[:, :1]) > 0.9, [[0.0, 1.0, 0.0]], [[1.0, 0.0, 0.0]])
        t = axis - n * np.sum(axis * n, axis=1, keepdims=True)
        tan[tbad, :3] = _normalize(t)
    if wbad.any():
        tan[wbad, 3] = 1.0
    return nrm, tan


def _chunk_groups(subs, max_chunks=None, max_verts=None):
    """Submeshes in spatially compact groups for meshes of their own: at most ``max_chunks`` each
    and, where the submeshes allow, about ``max_verts`` vertices each.  Splits along the longest
    side of the submesh centres, in proportion to the groups each side still needs."""
    max_chunks = max_chunks or MAX_CHUNKS
    max_verts = max_verts or MAX_MESH_VERTS
    n = len(subs)
    if n == 0:
        return []
    verts = np.array([max(1, s.vertex_count) for s in subs], np.float64)
    k = max(math.ceil(n / max_chunks), math.ceil(float(verts.sum()) / max_verts), 1)
    k = min(k, n)
    cent = np.array([np.asarray(s.positions, np.float64).mean(0) if len(s.positions) else np.zeros(3)
                     for s in subs])

    def split(idx, k):
        if k <= 1:
            return [idx]
        pts = cent[idx]
        axis = int(np.argmax(pts.max(0) - pts.min(0)))
        order = idx[np.argsort(pts[:, axis], kind="stable")]
        k1 = k // 2
        k2 = k - k1
        w = np.cumsum(verts[order])
        cut = int(np.searchsorted(w, w[-1] * k1 / k)) + 1
        lo = max(k1, len(order) - k2 * max_chunks)        # each side: at least one per group, at most
        hi = min(len(order) - k2, k1 * max_chunks)        # max_chunks per group
        cut = min(max(cut, lo), hi)
        return split(order[:cut], k1) + split(order[cut:], k2)

    groups = split(np.arange(n), k)
    return [[subs[i] for i in sorted(g.tolist())] for g in groups]


def _vertex_normals(pos, tri):
    fn = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    vn = np.zeros_like(pos)
    for k in range(3):
        np.add.at(vn, tri[:, k], fn)
    vn = _normalize(vn)
    vn[np.linalg.norm(vn, axis=1) < 0.5] = (0.0, 0.0, 1.0)
    return vn


def _translate(t):
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _place_matrix(pl) -> np.ndarray:
    """A placement (anchor-local position, yaw) as a 4x4."""
    a = math.radians(pl.yaw)
    m = np.eye(4)
    m[:3, :3] = [[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]]
    m[:3, 3] = pl.position
    return m


def _tri_height(tr, x, y):
    """z of triangle tr (3x3) above point (x, y), or None (only upward-facing triangles)."""
    a, b, c = tr
    v0, v1 = b - a, c - a
    n = np.cross(v0, v1)
    if n[2] <= 1e-6:
        return None
    d00 = v0[0] * v0[0] + v0[1] * v0[1]
    d01 = v0[0] * v1[0] + v0[1] * v1[1]
    d11 = v1[0] * v1[0] + v1[1] * v1[1]
    px, py = x - a[0], y - a[1]
    d20 = px * v0[0] + py * v0[1]
    d21 = px * v1[0] + py * v1[1]
    den = d00 * d11 - d01 * d01
    if abs(den) < 1e-12:
        return None
    v = (d11 * d20 - d01 * d21) / den
    w = (d00 * d21 - d01 * d20) / den
    if v < -1e-6 or w < -1e-6 or v + w > 1 + 1e-6:
        return None
    return float(a[2] + v * v0[2] + w * v1[2])
