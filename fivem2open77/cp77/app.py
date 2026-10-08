"""Appearance resource (.app) for the converted vehicle.

The donor's .app supplies component templates (mesh, physical mesh, light, visual controller) so
every field the engine expects is present; we then lay out our own components:

  body_01 / interior_01 ............ vehicle_slots/Base
  door_*_a / window_*_a ............ vehicle_slots/door_* / window_*   (rig bones that animate)
  hood_a / trunk_a / steering_wheel  vehicle_slots/<same name>
  wheel_01_<c>_a ................... vehicle_slots/wheel_<corner>      (spin + steer)
  tire_01_<c>_a .................... parented to its wheel component
  hub_<c> .......................... vehicle_slots/brake_<corner>      (steer, no spin)
  window_f/_b, lights_* ............ parented to body_01
  vehicle light components ......... parented to the light mesh whose material zone they drive
"""
from __future__ import annotations

import copy
import hashlib
import math

from .handles import IdAllocator, empty_package, materialize, normalize
from .wkjson import cname, depot, index_handles, value_of, world_transform

CORNERS = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}
AXLE_OF = {"fl": "front", "fr": "front", "bl": "back", "br": "back"}
BRAKE_SLOT = {"fl": "brake_front_left", "fr": "brake_front_right", "bl": "brake_back_left", "br": "brake_back_right"}
ZONE_NAMES = {0: "Zero", 1: "One", 2: "Two", 3: "Three"}
INTERIOR_LOD = "VehicleDecoration"


def cruid(*parts) -> str:
    """Stable 64-bit component id (CRUID) derived from names."""
    h = hashlib.sha1("/".join(parts).encode()).digest()
    v = int.from_bytes(h[:8], "little") & ((1 << 62) - 1)
    return str(v | (1 << 60))


def _components(app_doc, appearance="default"):
    root = app_doc["Data"]["RootChunk"]
    for a in root["appearances"]:
        d = a["Data"] if "Data" in a else a
        if value_of(d["name"]) == appearance:
            return a, d
    a = root["appearances"][0]
    return a, (a["Data"] if "Data" in a else a)


class _Templates:
    def __init__(self, donor_app_doc):
        self.index = index_handles(donor_app_doc)
        _, ad = _components(donor_app_doc)
        chunks = ad.get("compiledData", {}).get("Data", {}).get("Chunks") or []
        comps = chunks if chunks else ad["components"]
        self.by_name = {}
        self.by_type = {}
        for c in comps:
            c = self._data(c)
            self.by_name.setdefault(value_of(c.get("name")), c)
            self.by_type.setdefault(c["$type"], c)

    def _data(self, c):
        if "HandleId" in c and "Data" in c:
            return c["Data"]
        if "HandleRefId" in c:
            return self.index[str(c["HandleRefId"])]["Data"]
        return c

    def get(self, name=None, typ=None):
        src = self.by_name.get(name) if name else None
        if src is None and typ:
            src = self.by_type.get(typ)
        if src is None:
            raise KeyError(f"donor appearance has no template component {name or typ}")
        return materialize(src, self.index)


def _binding(ids: IdAllocator, bind: str, slot: str | None):
    return ids.handle({
        "$type": "entHardTransformBinding",
        "bindName": cname(bind),
        "enabled": 1,
        "enableMask": {"$type": "entTagMask",
                       "excludedTags": {"$type": "redTagList", "tags": [cname("NoBinding")]},
                       "hardTags": {"$type": "redTagList", "tags": []},
                       "softTags": {"$type": "redTagList", "tags": []}},
        "slotName": cname(slot),
    })


def _quat_from_axis_angle(axis, deg):
    a = math.radians(deg) / 2.0
    s = math.sin(a)
    x, y, z = axis
    return (x * s, y * s, z * s, math.cos(a))


def _quat_mul(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2)


def neon_appearance_name(key: str) -> str:
    return f"neon_{key}"


def build_app(vehicle, donor_app_doc, mesh_paths: dict, *, app_name: str = "default",
              base_entity_type: str | None = None, neon_meshes: dict | None = None) -> dict:
    """mesh_paths: IR part name -> depot path of its .mesh.
    neon_meshes: colour key -> depot path of that colour's tube mesh; each colour becomes an
    appearance ``neon_<key>`` = the default appearance + the tubes + their lights."""
    tpl = _Templates(donor_app_doc)
    doc = copy.deepcopy(donor_app_doc)
    ids = IdAllocator(doc)
    root = doc["Data"]["RootChunk"]
    wrapper, _ = _components(doc)
    comps = []
    names_used = set()

    def mesh_component(name, part, bind, slot, physical=True):
        c = tpl.get("body_01" if physical else "wipers_01",
                    "entPhysicalMeshComponent" if physical else "entMeshComponent")
        ids.fresh_ids(c)
        c["name"] = cname(name)
        c["id"] = cruid(vehicle.name, "comp", name)
        c["mesh"] = depot(mesh_paths[part])
        c["meshAppearance"] = cname("default")
        c["chunkMask"] = "18446744073709551615"
        c["localTransform"] = world_transform()
        c["parentTransform"] = _binding(ids, bind, slot)
        c["castShadows"] = "Always"
        c["castLocalShadows"] = "Always"
        c["castRayTracedGlobalShadows"] = "Always"
        c["autoHideDistance"] = 0
        c["forcedLodDistance"] = INTERIOR_LOD if part in ("interior_01", "steering_wheel") else "Default"
        if "visualScale" in c:
            c["visualScale"] = {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1}
        names_used.add(name)
        comps.append(c)
        return c

    parts = vehicle.parts
    # ---- body, cabin, openings
    for name, part in parts.items():
        if part.kind in ("wheel", "tire", "hub", "neon"):
            continue
        if part.slot:
            mesh_component(name, name, "vehicle_slots", part.slot)
        else:
            mesh_component(name, name, part.parent or "body_01", None)
    # ---- wheels: one mesh per axle, instanced on both sides (right side mirrored by the rig)
    for c, corner in CORNERS.items():
        axle = AXLE_OF[c]
        wname, tname = f"wheel_01_{c}_a", f"tire_01_{c}_a"
        if f"wheel_{axle}" in parts:
            mesh_component(wname, f"wheel_{axle}", "vehicle_slots", f"wheel_{corner}")
        if f"tire_{axle}" in parts:
            if wname in names_used:
                mesh_component(tname, f"tire_{axle}", wname, None)
            else:
                mesh_component(tname, f"tire_{axle}", "vehicle_slots", f"wheel_{corner}")
        if f"hub_{c}" in parts:
            mesh_component(f"hub_{c}", f"hub_{c}", "vehicle_slots", BRAKE_SLOT[c])

    # ---- lights
    for spec in vehicle.lights:
        if spec.part not in names_used:
            continue
        comps.append(_light_component(tpl, ids, vehicle, spec))

    # ---- visual controller (lists every mesh component and the appearance it uses)
    def visual_controller(components):
        vc = tpl.get("AppearanceVisualController", "entVisualControllerComponent")
        ids.fresh_ids(vc)
        deps = []
        for c in components:
            if c["$type"] in ("entMeshComponent", "entPhysicalMeshComponent"):
                deps.append({"$type": "entVisualControllerDependency", "appearanceName": copy.deepcopy(c["meshAppearance"]),
                             "componentName": c["name"], "mesh": copy.deepcopy(c["mesh"])})
        vc["appearanceDependency"] = deps
        vc["id"] = cruid(vehicle.name, "comp", "AppearanceVisualController")
        return vc

    base_comps = list(comps)
    comps.append(visual_controller(comps))

    # ---- appearance definitions
    buffers = iter(range(1 << 16))

    def define(wrap, name, components):
        ad = wrap["Data"] if "Data" in wrap else wrap
        ad["name"] = cname(name)
        ad["components"] = components
        ad["compiledData"] = empty_package(ad.get("compiledData", {}).get("Flags", 0))
        ad["compiledData"]["BufferId"] = str(next(buffers))     # unique per file, like the game's
        ad["partsValues"] = []
        ad["partsOverrides"] = []
        ad["partsMasks"] = []
        ad["proxyMesh"] = depot(None)
        ad["proxyMeshAppearance"] = cname("None")
        ad["looseDependencies"] = []
        ies = sorted({value_of(c.get("iesProfile")) for c in components if c["$type"] == "vehicleLightComponent"} - {None, "0"})
        ad["resolvedDependencies"] = [depot(p) for p in ies]
        ad["hitRepresentationOverrides"] = []
        return wrap

    wrappers = []
    pristine = copy.deepcopy(wrapper)
    wrappers.append(define(wrapper, app_name, comps))
    kit = getattr(vehicle, "neon", None)
    if kit is not None and neon_meshes:
        for key, label, rgb in kit.colors:
            if key not in neon_meshes:
                continue
            nc = [ids.fresh_ids(copy.deepcopy(c)) for c in base_comps]
            tube = tpl.get("wipers_01", "entMeshComponent")
            ids.fresh_ids(tube)
            tube["name"] = cname("neon_01")
            tube["id"] = cruid(vehicle.name, "comp", "neon_01")
            tube["mesh"] = depot(neon_meshes[key])
            tube["meshAppearance"] = cname("default")
            tube["chunkMask"] = "18446744073709551615"
            tube["localTransform"] = world_transform()
            tube["parentTransform"] = _binding(ids, "body_01", None)
            tube["castShadows"] = "Never"
            tube["castLocalShadows"] = "Never"
            tube["castRayTracedGlobalShadows"] = "Never"
            tube["autoHideDistance"] = 0
            tube["forcedLodDistance"] = "Default"
            if "visualScale" in tube:
                tube["visualScale"] = {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1}
            nc.append(tube)
            for i, light in enumerate(kit.lights):
                nc.append(_neon_light(tpl, ids, vehicle, kit, light, i, rgb))
            nc.append(visual_controller(nc))
            w = ids.fresh_ids(copy.deepcopy(pristine))
            wrappers.append(define(w, neon_appearance_name(key), nc))
    root["appearances"] = wrappers
    root["baseEntityType"] = cname(base_entity_type or vehicle.name)
    root["DismWoundConfig"] = {"$type": "entdismembermentWoundsConfigSet", "Configs": []}
    root["alternateAppearanceMapping"] = []
    root["censorshipMapping"] = []
    root["proxyPolyCount"] = 0
    root["cookingPlatform"] = "PLATFORM_PC"
    normalize(doc, tpl.index)
    return doc


# ----------------------------------------------------------------------------- lights
def _light_component(tpl, ids, vehicle, spec):
    """vehicleLightComponent driving one material zone of a light mesh (and, for head and brake
    lights, casting real light)."""
    kind = spec.name.split("_")[0]
    real = kind in ("head", "brake")
    template = {"head": "head_light_left_01", "brake": "break_light_left_01"}.get(kind, "position_light_left_01")
    c = tpl.get(template, "vehicleLightComponent")
    ids.fresh_ids(c)
    c["name"] = cname(spec.name)
    c["id"] = cruid(vehicle.name, "light", spec.name)
    c["lightType"] = spec.light_type
    c["materialZone"] = ZONE_NAMES.get(spec.zone, "Zero")
    c["emissiveOnly"] = 0 if real else 1
    c["colliderName"] = cname("None")
    c["colliderTag"] = cname("None")
    c["isDestructible"] = 0
    r, g, b = spec.color
    c["color"] = {"$type": "Color", "Alpha": 255, "Blue": int(b), "Green": int(g), "Red": int(r)}
    x, y, z = spec.position
    front = spec.direction[1] >= 0
    if front:
        q = _quat_from_axis_angle((1, 0, 0), -10.0)        # spot points along +Y, aim slightly down
    else:
        q = _quat_mul(_quat_from_axis_angle((0, 0, 1), 180.0), _quat_from_axis_angle((1, 0, 0), -10.0))
    c["localTransform"] = world_transform((x, y, z), q)
    c["parentTransform"] = _binding(ids, spec.part, None)
    if kind == "brake":
        c["intensity"] = 100
        c["radius"] = 2
    return c


# vehicleLightComponent-only properties: a neon light is a plain gameLightComponent, so the car's
# light controller (headlights, brake lights, blinkers) never switches it -- the appearance does
_VEHICLE_ONLY = ("allowSeparateEmissiveColor", "emissiveColor", "lightType", "highBeamPitchAngle",
                 "highBeamRadiusMultiplier", "highBeamConeMultiplier")


def _neon_light(tpl, ids, vehicle, kit, light, index, rgb):
    """A point light under the car that throws the kit's colour on the road (always on)."""
    c = tpl.get("head_light_left_01", "vehicleLightComponent")
    ids.fresh_ids(c)
    for k in _VEHICLE_ONLY:
        c.pop(k, None)
    c["$type"] = "gameLightComponent"
    name = f"neon_light_{light.side}_{index}"
    c["name"] = cname(name)
    c["id"] = cruid(vehicle.name, "light", name)
    c["type"] = "LT_Point"
    c["unit"] = "LU_Lumen"
    c["attenuation"] = "LA_InverseSquare"
    c["intensity"] = round(160.0 * kit.brightness, 2)
    c["radius"] = 2.4
    c["EV"] = 0
    c["sourceRadius"] = 0.05
    c["innerAngle"] = 30
    c["outerAngle"] = 120
    c["color"] = {"$type": "Color", "Alpha": 255, "Blue": int(rgb[2]), "Green": int(rgb[1]), "Red": int(rgb[0])}
    c["iesProfile"] = {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"}, "Flags": "Soft"}
    c["enableLocalShadows"] = 0
    c["enableContactShadows"] = 0
    c["contactShadows"] = "CSR_None"
    c["turnOnByDefault"] = 1
    c["isEnabled"] = 1
    c["emissiveOnly"] = 0
    c["isDestructible"] = 0
    c["colliderName"] = cname("None")
    c["colliderTag"] = cname("None")
    c["meshBrokenAppearance"] = cname("None")
    c["autoHideDistance"] = 60
    c["useInParticles"] = 1
    c["scaleVolFog"] = 0
    c["useInFog"] = 0
    # body_01 hangs from vehicle_slots Base: light positions are in that frame
    frame = vehicle.part_frames.get("body_01")
    t = frame[0] if frame is not None else (0.0, 0.0, 0.0)
    x, y, z = (float(light.position[i]) - float(t[i]) for i in range(3))
    c["localTransform"] = world_transform((x, y, z))
    c["parentTransform"] = _binding(ids, "body_01", None)
    return c
