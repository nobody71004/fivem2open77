"""FiveM map / MLO resource -> the placed GTA scene: which drawable goes where.

Reads every .ytyp (archetypes, MLO interiors) and .ymap (entities, MLO instances) of a resource and
resolves them to drawables shipped in the same resource.  Transform conventions follow CodeWalker:

  * a ymap CEntityDef stores the *inverse* rotation: world = T(pos) R(q^-1) S(scaleXY, scaleXY, scaleZ)
  * a CMloInstanceDef stores the rotation as is:         instance = T(pos) R(q)
  * an MLO child entity is like a ymap entity, relative to its instance: world = instance * local

Only HD and orphan-HD entities are kept (LOD / SLOD proxies are skipped).  Entities whose archetype
has no drawable in the resource (vanilla GTA props, vanilla map pieces a map mod repositions) are
counted in ``missing`` so the report can name them; they are not converted.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from ..gta.bounds import Collision, load_ybn, read_bound, ybn_bounds
from ..gta.drawable import quat_to_mat3
from ..gta.jenkhash import joaat
from ..gta.metafile import MetaHash, load_meta
from ..gta.rsc7 import ResourceError
from ..gta.texture import load_ytd
from ..gta.ydr import load_ydd, load_ydr

LOD_HD, LOD_ORPHANHD = 0, 5
KEEP_LODS = (LOD_HD, LOD_ORPHANHD)
ASSET_FRAGMENT, ASSET_DRAWABLE, ASSET_DICTIONARY, ASSET_NONE = 1, 2, 3, 4

# vanilla map-cell names ("ch2_03d_1", "hi@ch2_03d_1", "apa_ch2_03d_strm_5"): map mods ship edited
# copies of these to clear or reshape the original GTA area.  They are GTA world data, not the MLO.
VANILLA_CELL = re.compile(r"^(hi@)?([a-z]{2,4}_)?[a-z]{1,3}\d{1,2}_\d{1,2}[a-z]?(_.*)?$")


def quat_matrix(q) -> np.ndarray:
    return quat_to_mat3(q)


def q_inverse(q):
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    if n == 0:
        return (0.0, 0.0, 0.0, 1.0)
    return (-x / n, -y / n, -z / n, w / n)


def trs(pos, q, scale=(1.0, 1.0, 1.0)) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = quat_matrix(q) * np.asarray(scale, float)[None, :]
    m[:3, 3] = pos
    return m


@dataclass
class Archetype:
    hash: int
    name: str | None
    kind: str                       # base | time | mlo
    asset: int
    txd: int
    ydd: int
    physics: int
    asset_type: int
    bb_min: tuple
    bb_max: tuple
    lod_dist: float
    flags: int
    source: str
    mlo: dict | None = None


@dataclass
class Instance:
    archetype: int
    name: str                       # best known name (drawable file stem or 0xHASH)
    matrix: np.ndarray              # GTA world (or MLO-local for MLO children)
    lod_level: int
    source: str                     # ymap name, or "<mlo>#<n>" for MLO children
    room: str | None = None
    entity_set: str | None = None
    tint: int = 0

    @property
    def yaw_only(self) -> bool:
        r = self.matrix[:3, :3]
        s = np.linalg.norm(r, axis=0)
        return abs(r[2, 2] / max(s[2], 1e-9) - 1.0) < 1e-4

    @property
    def unit_scale(self) -> bool:
        s = np.linalg.norm(self.matrix[:3, :3], axis=0)
        return bool(np.all(np.abs(s - 1.0) < 1e-4))


@dataclass
class MloPlacement:
    archetype: int
    name: str
    matrix: np.ndarray              # instance transform (world)
    source: str
    children: list = field(default_factory=list)   # Instance, matrix MLO-local
    collision: Collision | None = None             # MLO-local
    rooms: list = field(default_factory=list)
    portals: int = 0
    entity_sets: list = field(default_factory=list)


@dataclass
class StreamIndex:
    ydr: dict = field(default_factory=dict)    # joaat(stem) -> path
    ydd: dict = field(default_factory=dict)
    yft: dict = field(default_factory=dict)
    ytd: dict = field(default_factory=dict)
    ybn: dict = field(default_factory=dict)
    ytyp: list = field(default_factory=list)
    ymap: list = field(default_factory=list)
    names: dict = field(default_factory=dict)  # hash -> stem
    other: list = field(default_factory=list)


def index_stream(files) -> StreamIndex:
    idx = StreamIndex()
    for f in files:
        base = os.path.basename(f)
        stem, ext = os.path.splitext(base)
        stem_l, ext = stem.lower(), ext.lower()
        h = joaat(stem_l)
        if ext in (".ydr", ".ydd", ".yft", ".ytd", ".ybn"):
            if ext == ".yft" and stem_l.endswith("_hi"):
                continue
            getattr(idx, ext[1:]).setdefault(h, f)
            idx.names.setdefault(h, stem_l)
            if stem_l.startswith("hi@"):
                idx.names.setdefault(joaat(stem_l[3:]), stem_l[3:])
        elif ext == ".ytyp":
            idx.ytyp.append(f)
            idx.names.setdefault(h, stem_l)
        elif ext == ".ymap":
            idx.ymap.append(f)
            idx.names.setdefault(h, stem_l)
        else:
            idx.other.append(f)
    return idx


DENSITY_CELL = 32.0         # metres: the city overview's grid
DENSITY_MAX = 40000         # cells kept (a 20 x 20 km city at 32 m has fewer)


def core_extent(density: dict, cell: float, keep: float = 0.994) -> list | None:
    """[x0, y0, x1, y1] around `keep` of the entities of a density grid ({(ix, iy): n}), padded."""
    if not density:
        return None
    total = float(sum(density.values()))
    cut = (1.0 - keep) / 2.0 * total

    def span(axis):
        acc = {}
        for k, n in density.items():
            acc[k[axis]] = acc.get(k[axis], 0) + n
        keys = sorted(acc)
        run, lo = 0.0, keys[0]
        for k in keys:
            run += acc[k]
            if run > cut:
                lo = k
                break
        run, hi = 0.0, keys[-1]
        for k in reversed(keys):
            run += acc[k]
            if run > cut:
                hi = k
                break
        return lo * cell, (hi + 1) * cell

    x0, x1 = span(0)
    y0, y1 = span(1)
    pad = 0.04 * max(x1 - x0, y1 - y0, 4 * cell)
    return [round(x0 - pad, 1), round(y0 - pad, 1), round(x1 + pad, 1), round(y1 + pad, 1)]


def district_key(ymap: str) -> str:
    """The district a ymap belongs to by its name: bronx_e_stream3, manhatsw_strbig0_straight_ms and
    manhat02_lod all name theirs first (bronx_e, manhatsw, manhat02)."""
    key = re.sub(r"(_strbig|_stream|_slod|_lod|_milo_|_interior|_occl|_critical).*$", "", ymap)
    key = re.sub(r"(_\d+)+$", "", key)
    return key or ymap


def _wmedian(vals) -> float:
    """The weighted median of [(value, weight)]."""
    vals = sorted(vals)
    half = sum(w for _, w in vals) / 2.0
    run = 0.0
    for v, w in vals:
        run += w
        if run >= half:
            return v
    return vals[-1][0]


def districts_of(ymap_info, limit=40) -> list:
    """ymaps grouped into districts by name (bronx_e_stream3 -> bronx_e): where most of a district's
    objects stand (a weighted median centre) and the radius that holds 95% of them, so a ymap of
    the group placed far from the rest does not make the district kilometres wide."""
    groups = {}
    for info in ymap_info:
        if "bbox" not in info:
            continue
        key = district_key(info["ymap"])
        g = groups.setdefault(key, {"name": key, "ymaps": 0, "entities": 0, "parts": []})
        g["ymaps"] += 1
        g["entities"] += info["entities"]
        b = info["bbox"]
        c = info.get("center") or [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]
        g["parts"].append((c, b, max(1, info["entities"])))
    out = []
    for g in groups.values():
        parts = g.pop("parts")
        cx = _wmedian([(c[0], w) for c, _, w in parts])
        cy = _wmedian([(c[1], w) for c, _, w in parts])
        # each ymap's reach from the centre (its far corner), the radius holding 95% of the entities
        reach = sorted((max(math.hypot(x - cx, y - cy) for x in (b[0], b[2]) for y in (b[1], b[3])), w)
                       for _, b, w in parts)
        total = sum(w for _, w in reach)
        run, r = 0.0, reach[-1][0]
        for d, w in reach:
            run += w
            if run >= 0.95 * total:
                r = d
                break
        inside = [b for c, b, _ in parts if math.hypot(c[0] - cx, c[1] - cy) <= r + 1.0] or [b for _, b, _ in parts]
        g["bbox"] = [round(min(b[0] for b in inside), 1), round(min(b[1] for b in inside), 1),
                     round(max(b[2] for b in inside), 1), round(max(b[3] for b in inside), 1)]
        g["center"] = [round(cx, 1), round(cy, 1)]
        g["radius"] = round(max(30.0, r), 1)
        out.append(g)
    out.sort(key=lambda g: -g["entities"])
    return out[:limit] if limit else out


class MapScene:
    """Everything the converter needs from a FiveM map resource."""

    def __init__(self, stream_files, entity_sets="default", log=print, area=None, city_limit=None):
        """area: (x, y, radius) in GTA world metres -- only what stands there is kept (a whole city
        such as Liberty V is converted district by district).  city_limit: past this many objects
        (and no area) the map is a city: its collision is not read (the converter stops and lists the
        districts to pick from), and self.city is True."""
        self.log = log
        self.area = tuple(float(v) for v in area) if area else None
        self.out_of_area = 0
        self.city = False
        self.density = {}                  # (ix, iy) of DENSITY_CELL m cells -> entities (the city overview)
        self.gtxd_files = [f for f in stream_files if "gtxd" in os.path.basename(f).lower()
                           and os.path.splitext(f)[1].lower() in (".meta", ".xml")]
        self.idx = index_stream([f for f in stream_files if f not in self.gtxd_files])
        for h, n in self.idx.names.items():
            MetaHash.names.setdefault(h, n)
        self.entity_sets = entity_sets
        self.archetypes: dict = {}
        self.instances: list = []          # world-space custom entities (outside MLOs)
        self.mlos: list = []               # MloPlacement
        self.missing = Counter()           # archetype name -> count (no drawable in resource)
        self.missing_by_source = Counter()
        self.skipped_lod = 0
        self.world_collisions: list = []   # (name, Collision world space)
        self.skipped_files: list = []      # (file, reason)
        self.warnings: list = []
        self.ymap_info: list = []
        self._drawables = {}
        self._mlo_ybns = set()
        self._ydds = {}
        self._ybn_cache = {}
        self._ytd_cache = {}
        self._tex_index = None
        self.txd_parent = {}               # child txd hash -> parent txd hash (gtxd.meta)
        self._load_gtxd()
        self._load_types()
        self._load_maps()
        if city_limit and not self.area and self.object_count() > city_limit:
            self.city = True               # a whole city: its collision is read district by district
            self.log(f"{self.object_count()} objects in {len(self.ymap_info)} ymaps: a whole city (pick a district)")
        else:
            self._load_world_collision()

    def object_count(self) -> int:
        """The objects the conversion would place (custom entities and MLO children)."""
        return len(self.instances) + sum(len(m.children) for m in self.mlos)

    # ------------------------------------------------------------------ names
    def name_of(self, h: int) -> str:
        a = self.archetypes.get(h)
        if a and a.name:
            return a.name
        return self.idx.names.get(h) or f"0x{h:08X}"

    # ------------------------------------------------------------------ types
    def _load_types(self):
        for path in self.idx.ytyp:
            try:
                root = load_meta(path, name=os.path.basename(path))
            except (ResourceError, ValueError) as e:
                self.skipped_files.append((os.path.basename(path), f"cannot read: {e}"))
                continue
            for a in root.get("archetypes") or []:
                t = a.get("_type")
                kind = {"CBaseArchetypeDef": "base", "CTimeArchetypeDef": "time",
                        "CMloArchetypeDef": "mlo"}.get(t)
                if kind is None:
                    continue
                h = int(a.get("name") or 0)
                arch = Archetype(
                    hash=h, name=self.idx.names.get(h), kind=kind, asset=int(a.get("assetName") or 0),
                    txd=int(a.get("textureDictionary") or 0), ydd=int(a.get("drawableDictionary") or 0),
                    physics=int(a.get("physicsDictionary") or 0), asset_type=int(a.get("assetType") or 0),
                    bb_min=tuple(a.get("bbMin") or (0, 0, 0)), bb_max=tuple(a.get("bbMax") or (0, 0, 0)),
                    lod_dist=float(a.get("lodDist") or 0), flags=int(a.get("flags") or 0),
                    source=os.path.basename(path), mlo=a if kind == "mlo" else None)
                self.archetypes[h] = arch

    # ------------------------------------------------------------------ drawables
    def drawable_for(self, h: int):
        """Archetype hash -> Drawable (or None).  Cached."""
        if h in self._drawables:
            return self._drawables[h]
        d = None
        a = self.archetypes.get(h)
        asset = a.asset if a and a.asset else h
        try:
            if a and a.asset_type == ASSET_DICTIONARY and a.ydd:
                dd = self._ydd(a.ydd)
                d = dd.get(asset) if dd else None
            elif asset in self.idx.ydr:
                d = load_ydr(self.idx.ydr[asset], name=self.idx.names.get(asset, ""))
            elif asset in self.idx.yft:
                from ..gta.frag import load_yft
                d = load_yft(self.idx.yft[asset], name=self.idx.names.get(asset, "")).drawable
            elif a is None and h in self.idx.ydr:
                d = load_ydr(self.idx.ydr[h])
        except (ResourceError, ValueError, IndexError) as e:
            self.warnings.append(f"cannot read the drawable of {self.name_of(h)}: {e}")
            d = None
        if d is not None and not getattr(d, "name", ""):
            d.name = self.name_of(h)
        self._drawables[h] = d
        return d

    def _ydd(self, h):
        if h not in self._ydds:
            p = self.idx.ydd.get(h)
            self._ydds[h] = load_ydd(p, name=os.path.basename(p)) if p else None
        return self._ydds[h]

    def _ytd(self, txd: int) -> dict:
        if txd not in self._ytd_cache:
            p = self.idx.ytd.get(txd)
            if not p:
                self._ytd_cache[txd] = {}
            else:
                try:
                    self._ytd_cache[txd] = load_ytd(p, name=p)
                except (ResourceError, ValueError) as e:
                    self.warnings.append(f"cannot read {os.path.basename(p)}: {e}")
                    self._ytd_cache[txd] = {}
        return self._ytd_cache[txd]

    def textures_for(self, h: int) -> dict:
        """External textures of an archetype ({lower name: GtaTexture}): its texture dictionary and
        the dictionaries gtxd.meta makes its parents (nearest first)."""
        a = self.archetypes.get(h)
        txd = a.txd if a else 0
        out = {}
        seen = set()
        while txd and txd not in seen and len(seen) < 12:
            seen.add(txd)
            for k, v in self._ytd(txd).items():
                out.setdefault(k, v)
            txd = self.txd_parent.get(txd)
        return out

    def texture_index(self) -> dict:
        """{lower texture name: txd hash} over every .ytd of the resource, read once without the
        pixels (a city has thousands of dictionaries: loading them all at once does not fit)."""
        if self._tex_index is None:
            from ..gta.texture import ytd_names
            idx = {}
            for h, p in self.idx.ytd.items():
                try:
                    for n in ytd_names(p):
                        idx.setdefault(n, h)
                except (ResourceError, ValueError, IndexError, OSError):
                    continue
            self._tex_index = idx
        return self._tex_index

    BIG_TXD_INDEX = 600      # more dictionaries than this: no resource-wide search (a city)

    def find_texture(self, name: str):
        """A texture anywhere in the resource by name (None when it is not shipped).  A city's
        thousands of dictionaries are not searched: there a texture comes from the archetype's
        dictionary and its gtxd parents, as in GTA."""
        if len(self.idx.ytd) > self.BIG_TXD_INDEX:
            return None
        h = self.texture_index().get(name.lower())
        return self._ytd(h).get(name.lower()) if h is not None else None

    def find_texture_loose(self, loose: str):
        """A texture whose name matches ignoring case, spaces and punctuation, when only one does."""
        if len(self.idx.ytd) > self.BIG_TXD_INDEX:
            return None
        if not hasattr(self, "_loose_index"):
            li = {}
            for n, h in self.texture_index().items():
                k = re.sub(r"[^a-z0-9]", "", n)
                li[k] = None if (k in li and li[k] != (n, h)) else (n, h)
            self._loose_index = li
        hit = self._loose_index.get(loose)
        return self._ytd(hit[1]).get(hit[0]) if hit else None

    def all_ytd_textures(self) -> dict:
        out = {}
        for h in self.idx.ytd:
            for k, v in self._ytd(h).items():
                out.setdefault(k, v)
        return out

    # ------------------------------------------------------------------ texture parenting
    def _load_gtxd(self):
        """gtxd.meta (CMapParentTxds): child txd -> parent txd, so textures shared by many
        dictionaries are found in the parent."""
        for path in self.gtxd_files:
            try:
                text = open(path, "rb").read().decode("utf-8", errors="replace")
            except OSError:
                continue
            for parent, child in re.findall(r"<parent>\s*([^<\s]+)\s*</parent>\s*<child>\s*([^<\s]+)\s*</child>", text, re.I):
                self.txd_parent[joaat(child.lower())] = joaat(parent.lower())
            for child, parent in re.findall(r"<child>\s*([^<\s]+)\s*</child>\s*<parent>\s*([^<\s]+)\s*</parent>", text, re.I):
                self.txd_parent.setdefault(joaat(child.lower()), joaat(parent.lower()))
        if self.txd_parent:
            self.log(f"gtxd: {len(self.txd_parent)} texture dictionaries take textures from a parent")

    def ybn(self, h: int) -> Collision | None:
        if h in self._ybn_cache:
            return self._ybn_cache[h]
        p = self.idx.ybn.get(h)
        hi = self.idx.ybn.get(joaat("hi@" + self.idx.names.get(h, "")))
        c = None
        for cand in (p, hi):
            if cand:
                try:
                    c = load_ybn(cand, name=os.path.basename(cand))
                    break
                except (ResourceError, ValueError) as e:
                    self.warnings.append(f"cannot read {os.path.basename(cand)}: {e}")
        self._ybn_cache[h] = c
        return c

    def entity_collision(self, h: int) -> Collision:
        """Model-space collision of one archetype: its physics dictionary .ybn, else the bounds
        embedded in its drawable."""
        a = self.archetypes.get(h)
        if a and a.physics and a.physics in self.idx.ybn and a.kind != "mlo":
            c = self.ybn(a.physics)
            if c is not None:
                return c
        d = self.drawable_for(h)
        if d is not None and getattr(d, "bound_ptr", 0):
            try:
                return read_bound(d.reader, d.bound_ptr)
            except (ResourceError, ValueError, IndexError, Exception) as e:  # noqa: BLE001
                self.warnings.append(f"cannot read the collision of {self.name_of(h)}: {e}")
        return Collision()

    def has_drawable(self, h: int) -> bool:
        a = self.archetypes.get(h)
        if a and a.kind == "mlo":
            return False
        asset = a.asset if a and a.asset else h
        if a and a.asset_type == ASSET_DICTIONARY and a.ydd:
            return a.ydd in self.idx.ydd
        return asset in self.idx.ydr or asset in self.idx.yft or h in self.idx.ydr

    # ------------------------------------------------------------------ maps
    def _entity_matrix(self, e, invert=True):
        q = tuple(e.get("rotation") or (0, 0, 0, 1))
        if invert and q != (0.0, 0.0, 0.0, 1.0):
            q = q_inverse(q)
        s = float(e.get("scaleXY") or 1.0)
        sz = float(e.get("scaleZ") or 1.0)
        return trs(e.get("position") or (0, 0, 0), q, (s, s, sz))

    def _load_maps(self):
        for path in sorted(self.idx.ymap):
            ymap = os.path.splitext(os.path.basename(path))[0].lower()
            try:
                root = load_meta(path, name=os.path.basename(path))
            except (ResourceError, ValueError) as e:
                self.skipped_files.append((os.path.basename(path), f"cannot read: {e}"))
                continue
            ents = root.get("entities") or []
            kept = custom = 0
            xy = [tuple(e.get("position") or (0, 0, 0))[:2] for e in ents]
            for e in ents:
                h = int(e.get("archetypeName") or 0)
                lod = int(e.get("lodLevel") or 0)
                if self.area and not self.in_area(e.get("position") or (0, 0, 0),
                                                  60.0 if e.get("_type") == "CMloInstanceDef" else 0.0):
                    self.out_of_area += 1
                    continue
                if e.get("_type") == "CMloInstanceDef":
                    self._add_mlo(e, ymap)
                    kept += 1
                    continue
                if lod not in KEEP_LODS:
                    self.skipped_lod += 1
                    continue
                kept += 1
                if not self.has_drawable(h):
                    self.missing[self.name_of(h)] += 1
                    self.missing_by_source[ymap] += 1
                    continue
                custom += 1
                self.instances.append(Instance(archetype=h, name=self.name_of(h), matrix=self._entity_matrix(e),
                                               lod_level=lod, source=ymap, tint=int(e.get("tintValue") or 0)))
            info = {"ymap": ymap, "entities": len(ents), "kept": kept, "converted": custom,
                    "vanilla_cell": bool(VANILLA_CELL.match(ymap))}
            if xy:
                a = np.asarray(xy, np.float64)
                if len(a) >= 20:          # a stray entity far away does not make the ymap kilometres wide
                    lo, hi = np.percentile(a, 1.0, axis=0), np.percentile(a, 99.0, axis=0)
                else:
                    lo, hi = a.min(axis=0), a.max(axis=0)
                info["bbox"] = [round(float(v), 1) for v in (lo[0], lo[1], hi[0], hi[1])]
                info["center"] = [round(float(v), 1) for v in np.median(a, axis=0)]
                if not self.area:
                    cells, counts = np.unique(np.floor(a / DENSITY_CELL).astype(np.int64), axis=0, return_counts=True)
                    for (ix, iy), n in zip(cells.tolist(), counts.tolist()):
                        self.density[(ix, iy)] = self.density.get((ix, iy), 0) + n
            self.ymap_info.append(info)

    def in_area(self, pos, margin=0.0) -> bool:
        if not self.area:
            return True
        x, y, r = self.area
        return (float(pos[0]) - x) ** 2 + (float(pos[1]) - y) ** 2 <= (r + margin) ** 2

    def districts(self, limit=40) -> list:
        """The map's ymaps grouped into districts by name (bronx_e_stream3 -> bronx_e), with their
        extent and size: what to pick an area from when the map is a whole city."""
        return districts_of(self.ymap_info, limit)

    def overview(self) -> dict:
        """A whole city at a glance: where its objects stand (DENSITY_CELL m cells) and its extent:
        the box around 99.4% of them (Liberty V places a few entities 12 km away from the rest),
        with the full box beside it."""
        boxes = [i["bbox"] for i in self.ymap_info if "bbox" in i]
        full = ([round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1),
                 round(max(b[2] for b in boxes), 1), round(max(b[3] for b in boxes), 1)] if boxes else None)
        cells = sorted(([ix, iy, n] for (ix, iy), n in self.density.items()), key=lambda c: -c[2])[:DENSITY_MAX]
        return {"extent": core_extent(self.density, DENSITY_CELL) or full, "full_extent": full,
                "objects": self.object_count(), "ymaps": len(self.ymap_info),
                "mlo_instances": len(self.mlos), "entities": sum(i["entities"] for i in self.ymap_info),
                "grid": {"cell": DENSITY_CELL, "cells": cells}}

    def _add_mlo(self, e, ymap):
        h = int(e.get("archetypeName") or 0)
        a = self.archetypes.get(h)
        name = self.name_of(h)
        if a is None or a.kind != "mlo":
            self.warnings.append(f"{ymap}: MLO instance of {name} but no MLO archetype in the resource")
            return
        q = tuple(e.get("rotation") or (0, 0, 0, 1))
        inst = MloPlacement(archetype=h, name=name, matrix=trs(e.get("position") or (0, 0, 0), q),
                            source=f"{ymap}:{name}")
        mlo = a.mlo
        rooms = mlo.get("rooms") or []
        inst.rooms = [r.get("name") for r in rooms]
        inst.portals = len(mlo.get("portals") or [])
        room_of = {}
        for ri, r in enumerate(rooms):
            for idx in r.get("attachedObjects") or []:
                room_of.setdefault(int(idx), r.get("name"))
        children = []
        for i, ce in enumerate(mlo.get("entities") or []):
            children.append((ce, room_of.get(i), None))
        wanted = self._wanted_sets(mlo, e)
        for es in mlo.get("entitySets") or []:
            sname = str(es.get("name"))
            inst.entity_sets.append({"name": sname, "entities": len(es.get("entities") or []),
                                     "enabled": sname in wanted})
            if sname not in wanted:
                continue
            locs = es.get("locations") or []
            for j, ce in enumerate(es.get("entities") or []):
                ri = int(locs[j]) if j < len(locs) else -1
                children.append((ce, rooms[ri].get("name") if 0 <= ri < len(rooms) else None, sname))
        for k, (ce, room, sname) in enumerate(children):
            ch = int(ce.get("archetypeName") or 0)
            lod = int(ce.get("lodLevel") or 0)
            if lod not in KEEP_LODS:
                self.skipped_lod += 1
                continue
            if not self.has_drawable(ch):
                self.missing[self.name_of(ch)] += 1
                self.missing_by_source[f"mlo:{name}"] += 1
                continue
            inst.children.append(Instance(archetype=ch, name=self.name_of(ch), matrix=self._entity_matrix(ce),
                                          lod_level=lod, source=f"{name}#{k}", room=room, entity_set=sname,
                                          tint=int(ce.get("tintValue") or 0)))
        # interior collision is MLO-local: the archetype's physics dictionary, else (the FiveM
        # convention) a .ybn named after the MLO archetype or its asset
        for ch in (a.physics, h, a.asset, joaat(name + "_col")):
            if ch and ch in self.idx.ybn:
                inst.collision = self.ybn(ch)
                self._mlo_ybns.add(ch)
                break
        if inst.collision is None:
            self.warnings.append(f"MLO {name}: no collision .ybn in the resource (looked for "
                                 f"{self.name_of(a.physics) if a.physics else '-'}, {name}, {name}_col)")
        self.mlos.append(inst)

    def _wanted_sets(self, mlo, inst_def):
        sets = [str(s.get("name")) for s in mlo.get("entitySets") or []]
        mode = self.entity_sets
        if mode == "all":
            return set(sets)
        if mode == "none":
            return set()
        if mode == "default" or not mode:
            return {str(x) for x in (inst_def.get("defaultEntitySets") or [])}
        return {s.strip() for s in str(mode).split(",") if s.strip()}

    # ------------------------------------------------------------------ world collision
    def _load_world_collision(self):
        """Standalone .ybn files placed in world space.  A map resource streams these as static
        collision; we keep the ones that belong to its own geometry (named after one of its
        archetypes / drawables / ymaps) and skip edited vanilla map cells."""
        referenced = {a.physics for a in self.archetypes.values() if a.physics} | self._mlo_ybns
        own = {n for n in self.idx.names.values()}
        for h, path in sorted(self.idx.ybn.items(), key=lambda kv: kv[1]):
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            if h in referenced:
                continue
            base = stem[3:] if stem.startswith("hi@") else stem
            if stem.startswith("hi@") and joaat(base) in self.idx.ybn:
                continue    # the plain file is used; hi@ is GTA's vehicle-detail twin
            root = re.sub(r"_col(_\d+)?$", "", base)
            if VANILLA_CELL.match(base) and root not in own - {stem, base}:
                self.skipped_files.append((os.path.basename(path), "edited vanilla GTA map cell collision"))
                continue
            belongs = root in own and root not in (stem, base)
            if not belongs:
                belongs = any(n != stem and n != base and (root.startswith(n) or n.startswith(root))
                              for n in own if len(n) > 3)
            if not belongs:
                self.skipped_files.append((os.path.basename(path), "not referenced and not named after this map"))
                continue
            if self.area:
                try:
                    bmin, bmax = ybn_bounds(path, name=os.path.basename(path))
                    x, y, r = self.area
                    dx = max(bmin[0] - x, 0.0, x - bmax[0])
                    dy = max(bmin[1] - y, 0.0, y - bmax[1])
                    if dx * dx + dy * dy > (r + 30.0) ** 2:
                        continue                 # nowhere near the area: not even read
                except (ResourceError, ValueError, IndexError, OSError):
                    pass
            c = self.ybn(h)
            if c is not None and not c.empty:
                if self.area and len(c.triangles):
                    t = c.triangles.reshape(-1, 3)
                    x, y, r = self.area
                    near = ((t[:, 0] - x) ** 2 + (t[:, 1] - y) ** 2) <= (r + 30.0) ** 2
                    if not near.any():
                        continue
                self.world_collisions.append((stem, c))

    # ------------------------------------------------------------------ summary
    def summary(self) -> dict:
        return {
            "archetypes": len(self.archetypes),
            "mlo_archetypes": sum(1 for a in self.archetypes.values() if a.kind == "mlo"),
            "mlo_instances": [{"name": m.name, "source": m.source, "position": [round(float(x), 3) for x in m.matrix[:3, 3]],
                               "children": len(m.children), "rooms": m.rooms, "portals": m.portals,
                               "entity_sets": m.entity_sets,
                               "collision_triangles": int(len(m.collision.triangles)) if m.collision else 0}
                              for m in self.mlos],
            "world_entities": len(self.instances),
            "area": list(self.area) if self.area else None,
            "out_of_area": self.out_of_area,
            "districts": self.districts(),
            "skipped_lod_entities": self.skipped_lod,
            "missing_archetypes": dict(self.missing.most_common()),
            "missing_total": int(sum(self.missing.values())),
            "ymaps": self.ymap_info,
            "world_collisions": [{"file": n, "triangles": int(len(c.triangles)), "boxes": len(c.boxes)}
                                 for n, c in self.world_collisions],
            "skipped_files": [{"file": f, "reason": r} for f, r in self.skipped_files],
            "warnings": self.warnings,
        }
