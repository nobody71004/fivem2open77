"""Doors and gates that open: FiveM door and gate objects become real Cyberpunk ``Door`` devices.

A baked map cannot move anything, so a door has to be a device the game itself animates.  The
template is the game's own transform-animated gate (``fence_barred_large.ent``: a ``Door`` with
``animationType = TRANSFORM``): its leaf is an ``entPhysicalMeshComponent`` called ``mesh`` under a
``gameTransformAnimatorComponent`` that turns it 90 degrees about Z (``doorOpenHinged`` /
``doorOpenHingedBack``) or slides it (``doorSlideHorizontally`` / ``doorSlideVertically``) when the
``DoorController`` opens it.  Per door model we swap the leaf mesh for the converted one (with one
box as its collision), aim the slide, size the trigger areas and make the controller automatic;
the template is read from the player's own game (never shipped).

Frames.  GTA hinges a door on its model origin, so a hinged leaf keeps its model frame and the
device stands on the hinge.  A sliding gate or a rolling shutter has no pivot: its mesh is shifted
so the device stands in the middle of the leaf, which is also the point door services measure
player distance from.

In the sector each door is a ``worldDeviceNode`` whose node reference (``QuestPrefabRefHash``) is
``$/fivem2open77/<map>/#door_<n>``; the game gives its entity the 64-bit FNV-1a hash of that path
without the ``#`` (checked against every vanilla device in ``03_night_city.devices``).  The device
is listed in a ``.devices`` file that ArchiveXL merges into ``03_night_city.devices``, which is how
the game knows the node is a device with a ``DoorControllerPS``.  Open77's ``open77_doors`` then
finds a real ``Door`` with a live controller, synchronises it between players and opens automatic
doors for whoever comes close; the map resource registers each door under that entity id with a
radius that fits its width (a 7 m gate opens before you reach its middle).  Without Open77's door
service the controller still opens automatic doors by itself (its trigger area).
"""
from __future__ import annotations

import copy
import math

from ..cp77.wkjson import cname, depot, document

DOOR_TEMPLATE = r"base\gameplay\devices\doors\gates\fence_barred_large.ent"
DEVICE_CLASS = "DoorControllerPS"
WORLD_DEVICES = r"base\worlds\03_night_city\_compiled\default\03_night_city.devices"

HINGED = "HINGED"
SLIDING = "SLIDING_HORIZONTALLY"
ROLLING = "SLIDING_VERTICALLY"
ROLLING_WORDS = ("shutter", "garage", "roller")

# vanilla hinged_c.ent: a wooden door leaf (solid, blocks sight); the template's own "Device
# transparent" (a barred gate: blocks players, not sight) is kept for gates
SOLID_DOOR_FILTER = {
    "$type": "physicsFilterData",
    "customFilterData": None,
    "preset": cname("Device solid visible"),
    "queryFilter": {"$type": "physicsQueryFilter", "mask1": "0", "mask2": "70107400"},
    "simulationFilter": {"$type": "physicsSimulationFilter", "mask1": "114696", "mask2": "23627"},
}
MIN_LEAF_THICKNESS = 0.12


def _r(v):
    return round(float(v), 3)


# ----------------------------------------------------------------------------- which objects open
def door_kind(name: str, mn, mx, *, is_door: bool, is_gate: bool, opening: str | None = None) -> dict | None:
    """How a door-like object opens, from its name class and its model-space bounds, or None to
    keep it static.  A door is a leaf (thinner than 0.6 m, at most 6 m wide and tall): it swings
    on its hinge, or rolls up when it is a shutter.  A gate is a panel (thin, at most 12 m wide,
    6 m tall): wider than 3 m it slides along its length, narrower it swings.  Anything bigger
    (pillars, a gatehouse, walls named after a gate) stays static and solid.

    The answer (all lengths in metres, in the device frame: origin on the hinge for a swinging
    leaf, in the middle of the leaf otherwise; x/y as in the model):
      opening    HINGED | SLIDING_HORIZONTALLY | SLIDING_VERTICALLY
      axis       "x" or "y": the leaf's long horizontal axis
      pivot      [x, y] model-space point the device stands on
      center     [x, y] middle of the leaf, device frame
      width, height, thickness, zmin, zmax (device frame = model z)
      slide      signed travel along `axis` for a sliding leaf (past the end nearer the origin)
    ``opening`` (the door mapping, or a doorway that tells a garage door) overrides the guess.
    """
    if not (is_door or is_gate):
        return None
    mn = [float(v) for v in mn]
    mx = [float(v) for v in mx]
    ex, ey, ez = (mx[k] - mn[k] for k in range(3))
    if ez < 0.5 or max(ex, ey) < 0.3:
        return None
    ax = 0 if ex >= ey else 1
    width, thick = (ex, ey) if ax == 0 else (ey, ex)
    if thick > 0.6 or ez > 6.0:
        return None
    lname = (name or "").lower()
    if is_door:
        if width > 6.0:
            return None
        opening = opening or (ROLLING if any(w in lname for w in ROLLING_WORDS) else HINGED)
    else:
        if width > 12.0:
            return None
        opening = opening or (SLIDING if width > 3.0 else HINGED)
    if opening not in (HINGED, SLIDING, ROLLING):
        raise ValueError(f"opening: {opening}")
    lo, hi = mn[ax], mx[ax]
    mid = [(mn[0] + mx[0]) * 0.5, (mn[1] + mx[1]) * 0.5]
    if opening == HINGED:
        if min(abs(lo), abs(hi)) <= 0.25 * width:
            pivot = [0.0, 0.0]                      # the model origin is the hinge (GTA)
        else:                                       # origin mid-leaf: swing about the nearer edge
            pivot = list(mid)
            pivot[ax] = lo if abs(lo) <= abs(hi) else hi
    else:
        pivot = list(mid)
    return {
        "opening": opening, "axis": "xy"[ax],
        "width": _r(width), "height": _r(ez), "thickness": _r(thick),
        "pivot": [_r(pivot[0]), _r(pivot[1])],
        "center": [_r(mid[0] - pivot[0]), _r(mid[1] - pivot[1])],
        "zmin": _r(mn[2]), "zmax": _r(mx[2]),
        "slide": _r(-width if abs(lo) <= abs(hi) else width),
        "solid": bool(is_door),
    }


def leaf_box(info: dict, physmat: str):
    """The leaf's collision: one box over the leaf, in the device frame."""
    from .collision import Collider
    w, t = float(info["width"]), max(float(info["thickness"]), MIN_LEAF_THICKNESS)
    zmin, zmax = float(info["zmin"]), float(info["zmax"])
    half = (w * 0.5, t * 0.5) if info["axis"] == "x" else (t * 0.5, w * 0.5)
    cx, cy = (float(v) for v in info["center"])
    return Collider(kind="box", physmat=physmat, center=(cx, cy, (zmin + zmax) * 0.5),
                    half=(half[0], half[1], (zmax - zmin) * 0.5))


def node_ref(map_name: str, index: int) -> str:
    return f"$/fivem2open77/{map_name}/#door_{index:03d}"


def node_ref_hash(ref: str) -> int:
    """The 64-bit id the game gives the entity of a node reference: FNV-1a 64 of the path
    without '#' (the hash 03_night_city.devices lists for every vanilla device)."""
    h = 0xCBF29CE484222325
    for b in ref.replace("#", "").encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def door_id(ref: str) -> str:
    """open77_doors names a door by its entity id in hex ('0xFC85EAE29622BAC2')."""
    return f"0x{node_ref_hash(ref):016X}"


# ----------------------------------------------------------------------------- the device entity
def _vec3(x, y, z):
    return {"$type": "Vector3", "X": float(x), "Y": float(y), "Z": float(z)}


def _fixed(v):
    return {"$type": "FixedPoint", "Bits": int(round(float(v) * 131072))}


def _set_pos(comp, x, y, z):
    lt = comp.get("localTransform")
    if isinstance(lt, dict) and isinstance(lt.get("Position"), dict):
        lt["Position"] = {"$type": "WorldPosition", "x": _fixed(x), "y": _fixed(y), "z": _fixed(z)}


def _handles(obj, out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        if "HandleId" in obj and "Data" in obj:
            out[obj["HandleId"]] = obj["Data"]
        for v in obj.values():
            _handles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _handles(v, out)
    return out


def _deref(obj, handles):
    if isinstance(obj, dict):
        if "HandleRefId" in obj:
            return handles.get(obj["HandleRefId"])
        if "HandleId" in obj and "Data" in obj:
            return obj["Data"]
    return obj


def _rect(x0, x1, y0, y1, along_x):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if not along_x:                                 # the leaf runs along y: swap the axes
        pts = [(y, x) for x, y in pts]
    return [_vec3(x, y, 0.0) for x, y in pts]


DISABLED = ("NumberPlateScreenMesh_side_1", "NumberPlateScreenMesh_side_2",
            "number_plate_ui_side_1", "number_plate_ui_side_2")


def door_entity_doc(template: dict, mesh_path: str, info: dict) -> dict:
    """The template ``Door`` with this leaf, opening the way ``info`` (door_kind) says."""
    doc = copy.deepcopy(template)
    data = doc["Data"]
    data["EmbeddedFiles"] = []
    root = data["RootChunk"]
    root["inplaceResources"] = []
    root["resolvedDependencies"] = []
    handles = _handles(root)
    package = root["compiledData"]["Data"]["Chunks"]
    w, h = float(info["width"]), float(info["height"])
    zmin = float(info["zmin"])
    cx, cy = (float(v) for v in info["center"])
    along_x = info["axis"] == "x"
    span = w * 0.5

    for entity in (package[0], root["entity"]["Data"]):
        if entity.get("$type") != "Door":
            raise ValueError("door template: the first package chunk is not a Door")
        entity["animationType"] = "TRANSFORM"
        entity["doorOpeningType"] = info["opening"]

    seen = set()
    for comp in list(package) + list(root["components"]):
        name = (comp.get("name") or {}).get("$value")
        if name == "mesh":
            comp["mesh"] = depot(mesh_path)
            comp["meshAppearance"] = cname("default")
            _set_pos(comp, 0.0, 0.0, 0.0)
            filt = _deref(comp.get("filterData"), handles)
            if info.get("solid") and isinstance(filt, dict) and id(filt) not in seen:
                seen.add(id(filt))
                filt.clear()
                filt.update(copy.deepcopy(SOLID_DOOR_FILTER))
        elif name in DISABLED:
            comp["isEnabled"] = 0
        elif name == "interaction":
            _set_pos(comp, cx, cy, zmin + min(1.5, h * 0.6))
        elif name in ("trigger", "SideOne", "SideTwo"):
            _set_pos(comp, cx, cy, zmin - (0.25 if name == "trigger" else 1.0))
            outline = _deref(comp.get("outline"), handles)
            if isinstance(outline, dict) and id(outline) not in seen:
                seen.add(id(outline))
                if name == "trigger":
                    outline["points"] = _rect(-span - 1.0, span + 1.0, -2.5, 2.5, along_x)
                    outline["height"] = round(max(2.0, h + 0.5), 2)
                elif name == "SideOne":
                    outline["points"] = _rect(-span - 2.0, span + 2.0, 0.2, 5.0, along_x)
                    outline["height"] = round(max(4.0, h + 2.0), 2)
                else:
                    outline["points"] = _rect(-span - 2.0, span + 2.0, -5.0, -0.2, along_x)
                    outline["height"] = round(max(4.0, h + 2.0), 2)
        elif name == "DoorTransformAnimator":
            for anim in comp.get("animations") or []:
                aname = (anim.get("name") or {}).get("$value")
                for item in (anim.get("timeline") or {}).get("items") or []:
                    impl = _deref(item.get("impl"), handles)
                    if not isinstance(impl, dict) or impl.get("$type") != "gameTransformAnimation_Move":
                        continue
                    target = _deref(impl.get("targetPositionEvaluator"), handles)
                    if not isinstance(target, dict) or not isinstance(target.get("position"), dict):
                        continue
                    if aname == "doorSlideHorizontally":
                        s = float(info["slide"])
                        target["position"] = _vec3(s, 0.0, 0.0) if along_x else _vec3(0.0, s, 0.0)
                        item["duration"] = round(max(1.5, min(3.0, abs(s) / 3.0)), 2)   # 3 m/s, a car can follow
                    elif aname == "doorSlideVertically":
                        target["position"] = _vec3(0.0, 0.0, round(h + 0.1, 3))
                        item["duration"] = round(max(1.0, min(3.0, h / 2.0)), 2)
        elif name == "controller":
            ps = _deref(comp.get("persistentState"), handles)
            if isinstance(ps, dict) and id(ps) not in seen and isinstance(ps.get("doorProperties"), dict):
                seen.add(id(ps))
                props = ps["doorProperties"]
                props["doorType"] = "AUTOMATIC"
                props["automaticallyClosesItself"] = 1
                props["initialDoorState"] = "CLOSED"
                props["isShutter"] = 0
    return doc


# ----------------------------------------------------------------------------- the sector side
def device_node(ent_path: str, debug_name: str) -> dict:
    """A ``worldDeviceNode`` spawning a door entity (deviceClassName None, like every vanilla device
    node: the class comes from the .devices entry)."""
    return {
        "$type": "worldDeviceNode",
        "alphaHackStreamingDistanceOverride": 0,
        "appearanceName": cname("default"),
        "debugName": cname(debug_name),
        "deviceClassName": cname("None"),
        "deviceConnections": [],
        "entityLod": 0,
        "entityTemplate": depot(ent_path, "Soft"),
        "instanceData": None,
        "ioPriority": "Immediate",
        "isHostOnly": 0,
        "isVisibleInGame": 1,
        "proxyScale": None,
        "sourcePrefabHash": "0",
        "tag": "None",
        "tagExt": "None",
    }


def node_ref_json(ref: str) -> dict:
    return {"$type": "NodeRef", "$storage": "string", "$value": ref}


def devices_doc(entries) -> dict:
    """``.devices`` resource listing door devices: [(node ref, world position)]."""
    rows = []
    for ref, pos in entries:
        rows.append({"$type": "gameDeviceResourceData_Cls1", "children": [],
                     "className": cname(DEVICE_CLASS), "hash": str(node_ref_hash(ref)),
                     "nodePosition": _vec3(*pos), "parents": []})
    root = {"$type": "gameDeviceResource", "cookingPlatform": "PLATFORM_PC",
            "data": {"HandleId": "0", "Data": {"$type": "gameDeviceResourceData", "unk1": rows, "version": 2}}}
    return document(root)


def door_radius(info: dict) -> float:
    """open77_doors opening radius for a door of this width (1-12 m), from the leaf's middle."""
    return float(max(3.5, min(12.0, float(info["width"]) * 0.5 + 3.0)))


def door_center_world(pos, yaw, info) -> tuple:
    """World position of the middle of a placed leaf at its foot (the device stands on `pos`)."""
    a = math.radians(yaw)
    cx, cy = (float(v) for v in info["center"])
    return (pos[0] + math.cos(a) * cx - math.sin(a) * cy, pos[1] + math.sin(a) * cx + math.cos(a) * cy,
            pos[2] + float(info["zmin"]))


def register_lua(map_name: str, doors: list) -> str:
    """Server Lua that hands every baked door to open77_doors: automatic, opening for anybody
    within a radius that fits the door's width.  open77_doors is optional: without it the doors
    still open by themselves for each player (vanilla automatic doors)."""
    lines = ["", "-- doors and gates (Door devices in the sector): open77_doors keeps them in sync between players",
             "local MAP_DOORS = {"]
    for d in doors:
        p = d["position"]
        lines.append(f'    {{ id = "{d["id"]}", x = {p[0]:.3f}, y = {p[1]:.3f}, z = {p[2]:.3f}, radius = {d["radius"]:.1f} }},')
    lines += [
        "}",
        "",
        "local function doorsCall(method, ...)",
        "    local pending, err = Open77.exports.call('open77_doors', method, ...)",
        "    if not pending then return nil, err end",
        "    return pending:await()",
        "end",
        "",
        "if #MAP_DOORS > 0 then",
        "    CreateThread(function()",
        "        local ok, failed, why = 0, 0, nil",
        "        for attempt = 1, 12 do                  -- open77_doors may start after this resource",
        "            Wait(attempt == 1 and 2000 or 5000)",
        "            local probe, reason = doorsCall('list', 0, 0, 1)",
        "            if probe then why = nil; break end",
        "            why = reason",
        "        end",
        "        if why then",
        f"            print(('[{map_name}] open77_doors not available (%s): the doors open by themselves for each player'):format(tostring(why)))",
        "            return",
        "        end",
        "        for _, d in ipairs(MAP_DOORS) do",
        "            local pos = { x = d.x, y = d.y, z = d.z }",
        "            local door, reason = doorsCall('register', { id = d.id, bucket = 0, position = pos, automatic = true, autoClose = true })",
        "            if door then",
        "                local done, r2 = doorsCall('configure', d.id, 0, { automatic = true, autoClose = true,",
        "                    openRadius = d.radius, closeRadius = math.min(12.0, d.radius + 1.5), closeDelay = 1.2 })",
        "                if done then ok = ok + 1 else failed = failed + 1; reason = r2 end",
        "            else",
        "                failed = failed + 1",
        "            end",
        f"            if not door or reason then print(('[{map_name}] door %s: %s'):format(d.id, tostring(reason))) end",
        "        end",
        f"        print(('[{map_name}] %d door(s) registered with open77_doors, %d failed'):format(ok, failed))",
        "    end)",
        "end",
    ]
    return "\n".join(lines) + "\n"
