"""Maps and MLOs in the Studio: a FiveM map resource converted into an Open77 map (a baked
sector the game streams, plus the in-game map manager f2o77_maps that moves it), looked at before
any build, built with WolvenKit, checked, tried in the local game and installed on servers.

    <workspace>/projects/<id>/              project.json "kind": "map"
        map.json                            the conversion options (normalize_options)
        input/<file|dir>                    the FiveM resource
        cache/look-<key>.json               the first look (scene, parts, sizes, warnings)
        cache/<key>-<mode>-<view>.png       renders (plan with a metre grid, 3/4 views)
        cache/<key>.glb                     the 3D view (a mesh per part, a node per placement)
        builds/<n>/out/<map> + f2o77_maps   build n (map.json: what the build used)

The look runs the converter's scene + build steps without WolvenKit (MapConverter.plan); it
takes about a minute for a big MLO, so it runs as a job and everything it makes is cached per
options key.  The model itself is kept in memory for one map at a time, ten minutes at most.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import time

from ..pipeline import VERSION

LOOK_VERSION = 4               # bump when the look's contents change (4: doorways)
CITY_RADIUS = 400.0            # a district's default radius (m): what one build of a city holds well
MODEL_IDLE = 600               # seconds a planned map model stays in memory

DEFAULTS = {
    "name": "",                # resource id; empty = from the FiveM resource name (pinned at the first look)
    "display_name": "",        # shown in game; empty = from the resource name
    "coords": None,            # [x, y, z] the FiveM spawn point (default: a coords .txt in the input)
    "anchor": None,            # [x, y, z] GTA world point the map is cut around (default: around coords)
    "area": None,              # [x, y, radius] GTA world metres: convert only what stands there (cities)
    "entity_sets": "default",  # MLO entity sets: default | all | none | [names]
    "exclude": [],             # archetype names to leave out
    "collision": True,         # False: visual only
    "coverage": True,          # check how much of the FiveM collision the boxes reproduce
    "doors": "devices",        # devices: doors and gates open by themselves; static: they do not
    "solid_doors": False,      # static doors: doors and shutters block (default: walk through)
    "open_gates": False,       # static doors: gates and barriers do not block
    "cell": 48.0,              # chunk cell (m)
    "ground_spacing": 2.0,     # ground collision grid (m)
    "collision_budget": 2500,  # max colliders per collision source
    "instance_min_tris": 1500, # objects with this many triangles get their own mesh
    "place": None,             # {"x", "y", "z", "yaw"}: Night City spot the build is baked at
    "door_map": {},            # the Doors tab: {"objects": {name: mode}, "doorways": {"<mlo>#<n>": mode}} (mlo/doorways.py)
    "quiet_interiors": "off",  # off | bottom | all: ambient-noise suppression volumes over MLO floors (mlo/ambience.py)
}
# options that change what is built (the look and the renders): not the names, not where it is baked
LOOK_KEYS = ("coords", "anchor", "area", "entity_sets", "exclude", "collision", "doors", "solid_doors",
             "open_gates", "cell", "ground_spacing", "collision_budget", "instance_min_tris", "door_map")
NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
ITEM_RX = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
RANGES = {"cell": (16.0, 256.0), "ground_spacing": (0.5, 8.0), "collision_budget": (100, 20000),
          "instance_min_tris": (100, 100000)}
NC_LIMIT = 20000.0             # |x|, |y| of a Night City spot (the playable city is about +-6 km)


class MapOptionsError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _num(v):
    if isinstance(v, bool):
        raise ValueError
    f = float(v)
    if not math.isfinite(f):
        raise ValueError
    return f


def _vec3(v, what, errors):
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, str):
        v = [p for p in re.split(r"[,\s]+", v.strip()) if p]
    try:
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            raise ValueError
        out = [_num(x) for x in v]
    except (TypeError, ValueError):
        errors.append(f"{what}: three numbers x, y, z")
        return None
    if any(abs(x) > NC_LIMIT for x in out):
        errors.append(f"{what}: coordinates beyond {NC_LIMIT:.0f} m")
        return None
    return [round(x, 4) for x in out]


def parse_place(v, errors=None, what="place"):
    """{x, y, z, yaw} | [x, y, z(, yaw)] | "x,y,z(,yaw)" -> {x, y, z, yaw} (yaw in [-180, 180))."""
    own = errors is None
    errors = [] if own else errors
    if v is None or v == "" or v == {}:
        return None
    try:
        if isinstance(v, str):
            v = [p for p in re.split(r"[,\s]+", v.strip()) if p]
        if isinstance(v, (list, tuple)):
            if len(v) not in (3, 4):
                raise ValueError
            v = {"x": v[0], "y": v[1], "z": v[2], "yaw": v[3] if len(v) == 4 else 0.0}
        if not isinstance(v, dict) or set(v) - {"x", "y", "z", "yaw"}:
            raise ValueError
        x, y, z = _num(v["x"]), _num(v["y"]), _num(v["z"])
        yaw = _num(v.get("yaw") or 0.0)
    except (KeyError, TypeError, ValueError):
        errors.append(f"{what}: x, y, z and an optional yaw (degrees)")
        if own:
            raise MapOptionsError(errors)
        return None
    if abs(x) > NC_LIMIT or abs(y) > NC_LIMIT or abs(z) > 3000:
        errors.append(f"{what}: outside Night City (|x|, |y| up to {NC_LIMIT:.0f} m, |z| up to 3000 m)")
        if own:
            raise MapOptionsError(errors)
        return None
    yaw = ((yaw + 180.0) % 360.0) - 180.0
    return {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3), "yaw": round(yaw, 2)}


def normalize_options(raw: dict | None) -> dict:
    """Merged with the defaults and checked; raises MapOptionsError with every problem."""
    raw = dict(raw or {})
    errors = []
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        errors.append("unknown options: " + ", ".join(unknown))
    o = copy.deepcopy(DEFAULTS)
    o.update({k: v for k, v in raw.items() if k in DEFAULTS})
    name = str(o["name"] or "").strip().lower()
    if name and not NAME_RX.match(name):
        errors.append("name: 3-48 characters a-z 0-9 _, starting with a letter (the resource id on the server)")
    o["name"] = name
    disp = str(o["display_name"] or "").strip()
    if len(disp) > 80 or any(ord(ch) < 32 for ch in disp) or '"' in disp or "\\" in disp:
        errors.append("display_name: up to 80 characters, no quotes or backslashes")
    o["display_name"] = disp
    o["coords"] = _vec3(o["coords"], "coords", errors)
    o["anchor"] = _vec3(o["anchor"], "anchor", errors)
    area = _vec3(o["area"], "area", errors)
    if area is not None and not (1.0 <= area[2] <= 5000.0):
        errors.append("area: x, y and a radius of 1 to 5000 m")
        area = None
    o["area"] = area
    es = o["entity_sets"]
    if isinstance(es, str) and es in ("default", "all", "none"):
        pass
    else:
        if isinstance(es, str):
            es = [p.strip() for p in es.split(",") if p.strip()]
        if not isinstance(es, list) or not es or not all(isinstance(x, str) and ITEM_RX.match(x) for x in es):
            errors.append("entity_sets: default, all, none or a list of entity set names")
        else:
            es = sorted(set(es))
    o["entity_sets"] = es
    ex = o["exclude"] or []
    if isinstance(ex, str):
        ex = [p.strip() for p in ex.split(",") if p.strip()]
    if not isinstance(ex, list) or len(ex) > 500 or not all(isinstance(x, str) and ITEM_RX.match(x) for x in ex):
        errors.append("exclude: a list of archetype names (letters, digits, _ . -)")
        ex = []
    o["exclude"] = sorted(set(ex))
    for k in ("collision", "coverage", "solid_doors", "open_gates"):
        if not isinstance(o[k], bool):
            errors.append(f"{k}: true or false")
    if o["doors"] not in ("devices", "static"):
        errors.append("doors: devices (they open by themselves) or static")
    if o["quiet_interiors"] not in ("off", "bottom", "all"):
        errors.append("quiet_interiors: off (default), bottom (the lower floor of every MLO) or all (every floor)")
    for k, (lo, hi) in RANGES.items():
        try:
            v = _num(o[k])
            if not lo <= v <= hi:
                raise ValueError
            o[k] = int(v) if isinstance(lo, int) else round(v, 3)
        except (TypeError, ValueError):
            errors.append(f"{k}: {lo} to {hi}")
    o["place"] = parse_place(o["place"], errors)
    from ..mlo.doorways import DoorMapError, normalize_door_map
    try:
        dm = normalize_door_map(o["door_map"])
        o["door_map"] = dm if (dm["objects"] or dm["doorways"]) else {}
    except DoorMapError as e:
        errors.append(str(e))
        o["door_map"] = {}
    if errors:
        raise MapOptionsError(errors)
    return o


def merge(base: dict, patch: dict) -> dict:
    """A JSON merge patch over the raw options (null removes an option = back to its default)."""
    out = copy.deepcopy(base or {})
    for k, v in (patch or {}).items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = copy.deepcopy(v)
    return out


def converter_opts(o: dict, version: str | None = None, keep_work=False, place=True) -> dict:
    """normalize_options() -> MapConverter keyword options."""
    es = o["entity_sets"]
    out = {"name": o["name"] or None, "display_name": o["display_name"] or None,
           "coords": ",".join(str(v) for v in o["coords"]) if o["coords"] else None,
           "anchor": ",".join(str(v) for v in o["anchor"]) if o["anchor"] else None,
           "area": ",".join(str(v) for v in o["area"]) if o.get("area") else None,
           "entity_sets": es if isinstance(es, str) else ",".join(es),
           "exclude": ",".join(o["exclude"]) or None,
           "no_collision": not o["collision"], "no_coverage": not o["coverage"],
           "static_doors": o["doors"] == "static", "solid_doors": o["solid_doors"], "open_gates": o["open_gates"],
           "cell": o["cell"], "ground_spacing": o["ground_spacing"], "collision_budget": o["collision_budget"],
           "instance_min_tris": o["instance_min_tris"], "keep_work": keep_work}
    if o.get("door_map"):
        out["door_map"] = json.dumps(o["door_map"], sort_keys=True)
    if o.get("quiet_interiors") and o["quiet_interiors"] != "off":
        out["quiet_interiors"] = o["quiet_interiors"]
    if version:
        out["version"] = version
    if place and o["place"]:
        p = o["place"]
        out["place"] = f"{p['x']},{p['y']},{p['z']},{p['yaw']}"
    return out


def look_key(o: dict, source_hash: str) -> str:
    sub = {k: o[k] for k in LOOK_KEYS}
    return hashlib.sha256(json.dumps({"o": sub, "s": source_hash, "v": VERSION, "l": LOOK_VERSION},
                                     sort_keys=True).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- the first look
RENDERS = (("material", "plan"), ("parts", "plan"), ("material", "tile"), ("material", "iso"), ("parts", "iso"),
           ("material", "iso_back"))
VIEW_NAMES = ("plan", "tile", "iso", "iso_back", "north", "west", "south", "east")
PLAN_SIZE = (1024, 1024)
VIEW_SIZE = (960, 600)


class _ModelCache:
    """One planned map model in memory (they are big), dropped after MODEL_IDLE seconds."""

    def __init__(self):
        self.lock = threading.RLock()
        self.key = None
        self.value = None
        self.used = 0.0
        self.building = threading.Lock()

    def get(self, key):
        with self.lock:
            if self.key == key and self.value is not None:
                self.used = time.time()
                return self.value
            return None

    def put(self, key, value):
        with self.lock:
            self.key, self.value, self.used = key, value, time.time()

    def drop(self, key=None):
        with self.lock:
            if key is None or key == self.key:
                self.key, self.value = None, None

    def expire(self):
        with self.lock:
            if self.value is not None and time.time() - self.used > MODEL_IDLE:
                self.key, self.value = None, None


MODELS = _ModelCache()


class MapLook:
    """The first look of one map project (cached per options key)."""

    def __init__(self, project):
        self.project = project

    def key(self, opts=None):
        opts = opts or self.project.map_options()
        return look_key(opts, self.project.data["source"].get("hash", ""))

    def _path(self, *parts):
        return self.project.path("cache", *parts)

    def cached(self, opts=None) -> dict | None:
        opts = opts or self.project.map_options()
        p = self._path(f"look-{self.key(opts)}.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    look = json.load(f)
            except (OSError, ValueError):
                return None
            # the names are options, not geometry: the look shows the current ones
            look.setdefault("converter_name", look["map"])
            look.setdefault("converter_display", look["display_name"])
            if opts.get("name"):
                look["map"] = opts["name"]
            if opts.get("display_name"):
                look["display_name"] = opts["display_name"]
            return look
        return None

    def model(self, opts=None, log=None, check=None):
        """(scene, model, coords, resource name) for these options: from memory or planned again."""
        from ..mlo.pipeline import MapConverter
        opts = opts or self.project.map_options()
        key = self.key(opts)
        MODELS.expire()
        got = MODELS.get(key)
        if got is not None:
            return got
        with MODELS.building:
            got = MODELS.get(key)
            if got is not None:
                return got
            MODELS.drop()            # free the old one before planning the next

            def lg(*a):
                if check:
                    check()
                if log:
                    log(" ".join(str(x) for x in a))
            conv = MapConverter(None, None, ".", log=lg, **dict(converter_opts(opts, place=False), no_coverage=True))
            got = conv.plan(self.project.source)
            MODELS.put(key, got)
            return got

    def ensure(self, log=print, check=None, progress=None) -> dict:
        """The look for the current options: cached, or planned, rendered and saved now.  A whole
        city is not planned: its look is the city overview (the districts to pick one from)."""
        from ..imaging import png_bytes
        from ..mlo import preview as mp
        from ..mlo.pipeline import WholeCityError
        opts = self.project.map_options()
        have = self.cached(opts)
        key = self.key(opts)
        if have is not None and have.get("city"):
            return have
        if have is not None and all(os.path.isfile(self._path(f"{key}-{m}-{v}.png")) for m, v in RENDERS) \
                and os.path.isfile(self._path(f"{key}.glb")):
            return have
        t0 = time.time()
        if progress:
            progress(0.05, "reading the FiveM map")
        try:
            scene, model, coords, res_name = self.model(opts, log=log, check=check)
        except WholeCityError as e:
            look = city_look(e, key, time.time() - t0)
            mp.write_json(look, self._path(f"look-{key}.json"))
            log(f"{e.resource} is a whole city: {look['overview']['objects']:,} objects in {look['overview']['ymaps']:,} "
                f"ymaps, {len(look['districts'])} districts. Pick a district to convert ({time.time() - t0:.0f} s)")
            return look
        log(f"scene and parts ready ({time.time() - t0:.0f} s)")
        look = mp.summary(scene, model, coords, res_name)
        look["key"] = key
        look["time"] = time.time()
        look["seconds"] = round(time.time() - t0, 1)
        if progress:
            progress(0.6, "rendering previews")
        colors = mp.material_colors(model)
        tri = {m: mp.map_triangles(model, m, colors=colors if m == "material" else None) for m in ("material", "parts")}
        for mode, view in RENDERS:
            if check:
                check()
            t, c = tri[mode]
            if view in ("plan", "tile"):
                img, fr = mp.render_plan(t, c, look["visual_bounds"], PLAN_SIZE, spawn=look.get("spawn_local"),
                                         tile=view == "tile")
                look["plan_frame"] = fr
            else:
                img = mp.render_view(t, c, view, VIEW_SIZE)
            with open(self._path(f"{key}-{mode}-{view}.png"), "wb") as f:
                f.write(png_bytes(img))
        thumb = mp.render_view(tri["material"][0], tri["material"][1], "iso", (480, 300))
        with open(self._path("thumb.png"), "wb") as f:
            f.write(png_bytes(thumb))
        if progress:
            progress(0.8, "writing the 3D preview")
        glb = mp.export_map_glb(model, self._path(f"{key}.glb.tmp"))
        os.replace(self._path(f"{key}.glb.tmp"), self._path(f"{key}.glb"))
        look["glb"] = glb
        mp.write_json(look, self._path(f"look-{key}.json"))
        log(f"look ready: {look['placements']} placements of {len(look['parts'])} parts, "
            f"{look['size'][0]:.0f} x {look['size'][1]:.0f} x {look['size'][2]:.0f} m, 3D preview "
            f"{glb['bytes'] / 1048576:.1f} MiB ({time.time() - t0:.0f} s)")
        return look

    def render(self, view="plan", mode="material", size=None) -> bytes:
        """A render: from the cache, or drawn now (needs the model: planned again when it is not
        in memory, which takes as long as the look)."""
        from ..imaging import png_bytes
        from ..mlo import preview as mp
        if mode not in ("material", "parts"):
            raise ValueError("mode: material or parts")
        if view not in VIEW_NAMES:
            raise ValueError(f"view: one of {', '.join(VIEW_NAMES)}")
        opts = self.project.map_options()
        key = self.key(opts)
        default = PLAN_SIZE if view in ("plan", "tile") else VIEW_SIZE
        w, h = (int(size[0]), int(size[1])) if size else default
        w, h = max(64, min(2048, w)), max(64, min(2048, h))
        p = self._path(f"{key}-{mode}-{view}.png" if (w, h) == default else f"{key}-{mode}-{view}-{w}x{h}.png")
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        look = self.cached(opts)
        if look is None:
            raise LookMissing("the map has not been looked at yet: run the look first")
        if look.get("city"):
            raise LookMissing("this is a whole city: pick a district first (Overview)")
        scene, model, coords, res = self.model(opts)
        t, c = mp.map_triangles(model, mode)
        if view in ("plan", "tile"):
            img, _ = mp.render_plan(t, c, look["visual_bounds"], (w, h), spawn=look.get("spawn_local"), tile=view == "tile")
        else:
            img = mp.render_view(t, c, view, (w, h))
        png = png_bytes(img)
        with open(p, "wb") as f:
            f.write(png)
        return png

    def glb_path(self) -> str | None:
        p = self._path(f"{self.key()}.glb")
        return p if os.path.isfile(p) else None


class LookMissing(ValueError):
    pass


def city_look(e, key: str, seconds: float) -> dict:
    """The look of a whole city: what to pick a district from (mlo.pipeline.WholeCityError)."""
    return {"city": True, "key": key, "time": time.time(), "seconds": round(seconds, 1), "map": e.name,  # "city" first
            "display_name": e.display_name, "resource": e.resource, "message": str(e), "districts": e.districts,
            "overview": e.overview, "warnings": [], "radius": CITY_RADIUS}


def district_name(city: str, district: str) -> str:
    """The resource id of one district of a city: <city>_<district>, as the server knows it."""
    c = re.sub(r"[^a-z0-9_]+", "_", (city or "city").lower()).strip("_") or "city"
    d = re.sub(r"[^a-z0-9_]+", "_", (district or "district").lower()).strip("_") or "district"
    if d.startswith(c):
        d = d[len(c):].strip("_") or "district"
    name = f"{c}_{d}"
    if not name[0].isalpha():
        name = "m_" + name
    name = re.sub(r"_+", "_", name)[:48].rstrip("_")
    return name if NAME_RX.match(name) else (name + "_map")[:48]


def district_display(city_display: str, district: str) -> str:
    words = re.sub(r"[_-]+", " ", district or "").strip()
    words = words.title() if words == words.lower() else words
    return f"{city_display} {words}".strip()[:80]


# ----------------------------------------------------------------------------- builds
STEPS = (("] scene:", 0.3, "scene read"), ("boxes cover", 0.55, "collision checked"),
         ("-> WolvenKit", 0.6, "writing game files"), ("] baking ", 0.88, "baking the sector"), ("] done in", 1.0, "done"))


def build_summary(r: dict, n: int, seconds: float) -> dict:
    """What the project keeps about a map build (the report stays in the build folder)."""
    rep = r["report"]
    b = rep.get("build") or {}
    cov = b.get("coverage") or {}
    col = cov.get("collision") or {}
    vis = cov.get("visual_floors") or {}
    baked = r.get("baked") or rep.get("baked")
    return {"n": n, "ok": True, "kind": "map", "version": rep.get("version"), "name": r["name"],
            "display_name": rep.get("display_name"), "resources": [r["name"], os.path.basename(r["manager"])],
            "packages": rep.get("packages") or [], "parts": len(rep.get("parts") or {}), "placements": rep.get("placements"),
            "textures": rep.get("textures"), "materials": rep.get("materials"),
            "doors": sum(d.get("devices", 0) for d in b.get("doors") or []),
            "glass": sum((b.get("glass") or {}).values()) if isinstance(b.get("glass"), dict) else b.get("glass"),
            "coverage": {"walkable": (col.get("walkable") or {}).get("covered_pct"),
                         "walls": (col.get("walls") or {}).get("covered_pct"),
                         "floors": vis.get("solid_pct"), "holes": len(col.get("holes") or []),
                         "colliders": col.get("colliders")} if cov else None,
            "baked": {"anchor": baked.get("anchor"), "id": baked.get("id"), "bytes": baked.get("bytes")} if baked else None,
            "warnings": rep.get("warnings") or [], "seconds": round(seconds, 1)}


def map_dirs(out: str) -> tuple:
    """(map resource dir, manager dir) of a build's out folder."""
    from ..mlo.resource import MANAGER
    maps = [d for d in sorted(os.listdir(out)) if os.path.isfile(os.path.join(out, d, "layout.json"))] \
        if os.path.isdir(out) else []
    if not maps:
        raise ValueError("this build has no map resource")
    man = os.path.join(out, MANAGER)
    return os.path.join(out, maps[0]), (man if os.path.isdir(man) else None)
