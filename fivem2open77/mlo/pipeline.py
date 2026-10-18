"""FiveM map / MLO resource -> Open77 map resources (baked map + the map manager), one command.

    find stream files -> MapScene (ymap/ytyp/ydr/ybn) -> MapBuilder (parts, placements, colliders)
    -> MapWriter (WolvenKit JSON + glb -> CR2W meshes, host entities -> archives -> zips)
    -> <out>/<map> + <out>/f2o77_maps  [-> bake.py: sector package for an anchor (--place)]
"""
from __future__ import annotations

import os
import re
from collections import Counter
import secrets
import time

from ..gta.resource import _extract_archive
from ..gta.rsc7 import EscrowProtectedError
from .build import MapBuilder, _sanitize
from .scene import MapScene
from .writer import MapWriter

STREAM_EXTS = (".ydr", ".ydd", ".yft", ".ytd", ".ybn", ".ytyp", ".ymap")
_FLOAT = r"(-?\d+(?:\.\d+)?)"
_COORDS_RE = re.compile(rf"{_FLOAT}\s*,\s*{_FLOAT}\s*,\s*{_FLOAT}(?:\s*,\s*{_FLOAT})?")


HUGE_MAP = 40000      # objects: past this a map is a city, converted by area


class MapInputError(Exception):
    pass


class WholeCityError(MapInputError):
    """A whole city (Liberty V): converted a district at a time.  Carries what to pick one from:
    the districts (ymaps grouped by name, biggest first) and where the objects stand."""

    def __init__(self, message, *, resource, name, display_name, districts, overview):
        super().__init__(message)
        self.resource = resource
        self.name = name
        self.display_name = display_name
        self.districts = districts
        self.overview = overview


def find_map_files(path: str):
    """-> (root, stream files, coords or None, resource name, tmpdir)."""
    tmpdir = None
    if os.path.isfile(path):
        tmpdir = _extract_archive(path)
        root = tmpdir
    elif os.path.isdir(path):
        root = path
    else:
        raise MapInputError(f"input not found: {path}")
    files = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            files.append(os.path.join(dp, f))
    # a single-player map ships as an OpenIV dlc.rpf (the ymaps, ytyps and models in a nested
    # x64/levels/.../<map>.rpf): unpacked next to the rest, like a single-player car (gta/rpf.py)
    rpfs = [f for f in files if f.lower().endswith(".rpf")]
    if rpfs:
        from ..gta import rpf as rpf_mod
        if tmpdir is None:
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="f2o77-rpf-")
        for f in rpfs:
            dest = os.path.join(tmpdir, "rpf", os.path.relpath(f, root))
            try:
                files.extend(rpf_mod.extract(f, dest))
            except rpf_mod.RpfError as e:
                raise MapInputError(f"{os.path.basename(f)}: {e}") from e
    if any(f.lower().endswith(".fxap") for f in files):
        raise EscrowProtectedError("this resource is protected by Cfx.re asset escrow (.fxap found). Escrowed assets "
                                   "are encrypted for one server licence and cannot be converted.")
    # stream files, plus the texture parenting (gtxd.meta: a txd's textures can live in a parent)
    stream = [f for f in files if f.lower().endswith(STREAM_EXTS)
              or re.search(r"gtxd[^/\\]*\.(meta|xml)$", os.path.basename(f).lower())]
    if not stream:
        raise MapInputError("no GTA map files (.ymap/.ytyp/.ydr/.ybn) found in the input")
    coords = None
    for f in sorted(files, key=lambda x: (0 if "coord" in os.path.basename(x).lower() else 1, len(x))):
        if not f.lower().endswith(".txt") or os.path.getsize(f) > 4096:
            continue
        try:
            m = _COORDS_RE.search(open(f, encoding="utf-8", errors="replace").read())
        except OSError:
            continue
        if m:
            coords = tuple(float(v) for v in m.groups()[:3])
            break
    manifests = [f for f in files if os.path.basename(f).lower() in ("fxmanifest.lua", "__resource.lua")]
    if manifests:
        res_name = os.path.basename(os.path.dirname(manifests[0]))
    else:
        res_name = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    return root, stream, coords, res_name, tmpdir


def _parse_vec(text, n_min=3, n_max=4, what="value"):
    if text is None:
        return None
    parts = [p for p in re.split(r"[,\s]+", str(text).strip()) if p]
    if not (n_min <= len(parts) <= n_max):
        raise ValueError(f"{what}: expected {n_min}..{n_max} numbers, got '{text}'")
    return [float(p) for p in parts]


def _door_map(v):
    """--door-map: JSON text, or a JSON file (doorways.normalize_door_map checks it)."""
    if isinstance(v, str) and v.strip() and not v.lstrip().startswith("{"):
        with open(v, "r", encoding="utf-8") as f:
            v = f.read()
    return v


class MapConverter:
    def __init__(self, wk, cache, out_dir, log=print, **opts):
        self.wk = wk
        self.cache = cache
        self.out_dir = out_dir
        self.log = log
        self.opts = opts

    def plan(self, resource_path):
        """Scene + build without writing game files (for `mlo inspect` and agents)."""
        root, stream, coords, res_name, tmp = find_map_files(resource_path)
        try:
            scene, model, coords = self._build(stream, coords, res_name)
            return scene, model, coords, res_name
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    def _build(self, stream, coords, res_name):
        o = self.opts
        if o.get("coords"):
            coords = tuple(_parse_vec(o["coords"], 3, 3, "--coords"))
        name = _sanitize(o.get("name") or res_name)
        if len(name) < 3:               # Open77 resource ids: [a-z][a-z0-9_]{2,57}
            name = (name + "_map")[:48]
        display = o.get("display_name") or re.sub(r"[_-]+", " ", res_name).strip()
        if display == display.lower() or display == display.upper():
            display = display.title()                 # keep the author's casing ("EstateV2")
        t0 = time.time()
        area = _parse_vec(o.get("area"), 3, 3, "--area") if o.get("area") else None
        if area and area[2] <= 0:
            raise MapInputError("--area: the radius must be more than 0")
        scene = MapScene(stream, entity_sets=o.get("entity_sets", "default"), log=self.log, area=area,
                         city_limit=None if area else HUGE_MAP)
        objects = scene.object_count()
        if not area and (scene.city or objects > HUGE_MAP):
            top = scene.districts(8)
            raise WholeCityError(
                f"{res_name} is a whole city ({objects} objects in {len(scene.ymap_info)} ymaps): convert it a "
                f"district at a time with an area (x,y,radius in GTA metres). Biggest districts: " + "; ".join(
                    f"{d['name']} ({d['entities']} objects) {d['center'][0]:.0f},{d['center'][1]:.0f},{min(d['radius'], 400):.0f}"
                    for d in top),
                resource=res_name, name=name, display_name=display, districts=scene.districts(0),
                overview=scene.overview())
        if area and not objects:
            raise MapInputError(f"nothing of {res_name} stands within {area[2]:.0f} m of {area[0]:.0f},{area[1]:.0f} "
                                "(GTA world metres); pick an area from the map's districts")
        builder = MapBuilder(scene, name, display, log=self.log, coords=coords,
                             anchor=_parse_vec(o.get("anchor"), 3, 3, "--anchor"),
                             cell=float(o.get("cell") or 48.0),
                             exclude=[e.strip() for e in (o.get("exclude") or "").split(",") if e.strip()],
                             static_doors=bool(o.get("static_doors")),
                             solid_doors=bool(o.get("solid_doors")), open_gates=bool(o.get("open_gates")),
                             no_collision=bool(o.get("no_collision")),
                             collision_budget=int(o.get("collision_budget") or 2500),
                             ground_spacing=float(o.get("ground_spacing") or 2.0),
                             instance_min_tris=int(o.get("instance_min_tris") or 1500),
                             door_map=_door_map(o.get("door_map")))
        model = builder.build()
        self.log(f"[{name}] scene: {len(scene.mlos)} MLO instance(s), {len(scene.instances)} entities, "
                 f"{sum(scene.missing.values())} vanilla GTA entities skipped; "
                 f"{len(model.parts)} parts / {len(model.placements)} placements ({time.time() - t0:.1f}s)")
        doors = model.report.get("doors") or []
        if doors:
            self.log(f"[{name}] {sum(d['devices'] for d in doors)} doors and gates open by themselves "
                     f"(Door devices from {len(doors)} models: "
                     + ", ".join(f"{d['part'][5:]} {d['opening'].lower()} x{d['devices']}" for d in doors) + ")")
        ways = model.report.get("doorways") or []
        if ways:
            by = Counter(d["status"] for d in ways)
            self.log(f"[{name}] doorways: " + ", ".join(f"{n} {k}" for k, n in sorted(by.items())))
        if not o.get("no_collision") and not o.get("no_coverage"):
            t1 = time.time()
            from . import coverage as cov
            door = builder.walkthrough()
            c = cov.coverage(scene, model, density=0.5, max_samples=120_000, door_re=door, top=8)
            v = cov.visual_floor_coverage(scene, model, density=0.5, max_samples=80_000, door_re=door, top=8)
            model.report["coverage"] = {"collision": c, "visual_floors": v}
            self.log(f"[{name}] collision: {c['colliders']} boxes cover {c['walkable']['covered_pct']}% of the map's "
                     f"walkable collision and {c['walls']['covered_pct']}% of its walls; visual floors "
                     f"{v.get('solid_pct', 100.0)}% solid ({time.time() - t1:.1f}s)")
        return scene, model, coords

    def convert(self, resource_path) -> dict:
        root, stream, coords, res_name, tmp = find_map_files(resource_path)
        try:
            scene, model, coords = self._build(stream, coords, res_name)
            place = _parse_vec(self.opts.get("place"), 3, 4, "--place")
            defaults = None
            if place:
                defaults = {"x": place[0], "y": place[1], "z": place[2], "yaw": place[3] if len(place) > 3 else 0.0}
            token = self.opts.get("agent_token") or ""
            if token == "auto":
                token = secrets.token_hex(16)
            writer = MapWriter(self.wk, self.cache, self.out_dir, log=self.log, keep_work=self.opts.get("keep_work"),
                               version=self.opts.get("version") or "1.0.0")
            result = writer.write(model, defaults=defaults,
                                  source_info={"resource": res_name, "input": os.path.basename(resource_path),
                                               "stream_files": len(stream), "coords": coords},
                                  scene_summary=scene.summary(), agent_token=token,
                                  quiet_interiors=self.opts.get("quiet_interiors") or "off",
                                  extra_packages=self.opts.get("extra_packages") or ())
            result["agent_token"] = token
            return result
        finally:
            if tmp:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
