"""Baked map geometry: one host entity per part, a streaming sector that places them, and the
ArchiveXL streaming block that adds the sector to Night City.

Why baked.  Open77 props cannot draw an arbitrary mesh: a raw ``.mesh`` prop is a marker cylinder
with a mesh path written into it that the engine never re-resolves, and only curated aliases get
an authored host entity (Open77 research doc §10, §14, §17; Props.cpp ``PropHostEntity``).  New
buildings are "baked geometry" in Open77's own terms (server-mods guide: ``.archive`` meshes and
sectors plus an ArchiveXL ``.xl``).  So the map ships as:

* assets package (anchor independent): meshes, textures, materials and one host ``.ent`` per part
  (root ``entEntity`` with an ``entPhysicalMeshComponent``, the shape of vanilla
  ``monitor_a.ent``: the component draws the mesh and publishes the mesh's own collision shapes;
  parts without collision get a plain ``entMeshComponent``);
* sector package (one per anchor): ``<map>.streamingsector`` with a ``worldEntityNode`` per
  placement, ``<map>.streamingblock`` describing it, and an ``.archive.xl`` that merges the block
  into ``03_night_city.streamingworld`` (ArchiveXL ``streaming: blocks``).  Doors and gates are
  ``worldDeviceNode``s with a node reference, listed in ``<map>.devices`` which the same ``.xl``
  merges into ``03_night_city.devices`` (ArchiveXL ``resource: patch``; see doors.py).

Moving the map therefore means re-baking the (small) sector package and restarting; the in-game
placer previews the move with a wireframe until then.

Node data follows what Entity Spawner's WolvenKit import writes (keanuwheeze/CP77_entSpawner
``import_object_spawner.wscript``), which is the shape thousands of world mods ship with: node id
0, a unique ``QuestPrefabRefHash`` per node, bounds and pivot at the node position.
"""
from __future__ import annotations

import hashlib
import math
import struct

from ..cp77.wkjson import cname, depot, document, vec3, vec4
from . import ambience, doors

RED_PACKAGE = "WolvenKit.RED4.Archive.Buffer.RedPackage, WolvenKit.RED4, Version=8.17.4.0, Culture=neutral, PublicKeyToken=null"
NODE_BUFFER = "WolvenKit.RED4.Archive.Buffer.worldNodeDataBuffer, WolvenKit.RED4, Version=8.17.4.0, Culture=neutral, PublicKeyToken=null"
BUFFER_FLAGS = 4063232

# vanilla monitor_a.ent / Open77's prop hosts: solid, non-moving world geometry
WORLD_STATIC_FILTER = {
    "$type": "physicsFilterData",
    "customFilterData": None,
    "preset": cname("World Static"),
    "queryFilter": {"$type": "physicsQueryFilter", "mask1": "0", "mask2": "71942148"},
    "simulationFilter": {"$type": "physicsSimulationFilter", "mask1": "376836", "mask2": "5070"},
}

SECTOR_CATEGORY = "Interior"     # Entity Spawner's default; vanilla uses Exterior/Interior alike
SECTOR_LEVEL = 1


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def stable_id(*parts) -> int:
    """A stable, non-zero 64-bit id for a name (component CRUIDs, node refs)."""
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    v = int.from_bytes(hashlib.sha1(raw).digest()[:8], "little")
    return v or 1


def cruid(*parts) -> str:
    # vanilla CRUIDs are positive 63-bit values with the low byte clear
    return str((stable_id("cruid", *parts) & 0x7FFFFFFFFFFFFF00) | 0x100000000000000)


# ----------------------------------------------------------------------------- host entity
# properties entPhysicalMeshComponent adds to entMeshComponent (WolvenKit.RED4 8.17.4 type library)
PHYSICAL_KEYS = ("filterData", "filterDataSource", "simulationType", "startInactive", "useResourceSimulationType",
                 "visibilityAnimationParam")


def _mesh_component(mesh_path: str, cid: str, filter_handle, auto_hide: float) -> dict:
    """``filter_handle`` None: a plain ``entMeshComponent`` (draws only, no physics body)."""
    comp = {
        "$type": "entPhysicalMeshComponent",
        "autoHideDistance": float(auto_hide),
        "castLocalShadows": "Always",
        "castRayTracedGlobalShadows": "Always",
        "castRayTracedLocalShadows": "Default",
        "castShadows": "Always",
        "chunkMask": "9223372036854775807",
        "filterData": filter_handle,
        "filterDataSource": "Parent",
        "forcedLodDistance": "Default",
        "forceLODLevel": -1,
        "id": cid,
        "isEnabled": 1,
        "isReplicable": 0,
        "localTransform": {
            "$type": "WorldTransform",
            "Orientation": {"$type": "Quaternion", "i": 0, "j": 0, "k": 0, "r": 1},
            "Position": {"$type": "WorldPosition", "x": {"$type": "FixedPoint", "Bits": 0},
                         "y": {"$type": "FixedPoint", "Bits": 0}, "z": {"$type": "FixedPoint", "Bits": 0}},
        },
        "LODMode": "AlwaysVisible",
        "mesh": depot(mesh_path),
        "meshAppearance": cname("default"),
        "motionBlurScale": 1,
        "name": cname("f2o77_map_part"),
        "navigationImpact": {"$type": "NavGenNavigationSetting", "navmeshImpact": "Ignored"},
        "numInstances": 0,
        "objectTypeID": "ROT_Static",
        "order": 0,
        "overrideMeshNavigationImpact": 1,
        "parentTransform": None,
        "renderingPlane": "RPl_Scene",
        "renderSceneLayerMask": "Default",
        "simulationType": "Static",
        "startInactive": 0,
        "useResourceSimulationType": 0,
        "version": 0,
        "visibilityAnimationParam": cname("None"),
        "visualScale": {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1},
    }
    if filter_handle is None:
        comp["$type"] = "entMeshComponent"
        for k in PHYSICAL_KEYS:
            del comp[k]
    return comp


def host_entity_doc(map_name: str, part: str, mesh_path: str, auto_hide: float = 0.0,
                    physical: bool = True) -> dict:
    """The entity a ``worldEntityNode`` spawns for one part: root ``entEntity`` plus one mesh
    component.  ``physical``: an ``entPhysicalMeshComponent`` (draws the mesh, and hands the mesh's
    ``meshMeshParamPhysics`` shapes to the physics scene); otherwise a plain ``entMeshComponent``
    (parts without collision shapes: nothing for the physics scene to build)."""
    cid = cruid(map_name, part)
    entity = {"$type": "entEntity", "customCameraTarget": "ECCTV_All", "renderSceneLayerMask": "Default"}
    # WolvenKit numbers handles in document order: compiledData (package) comes first
    if physical:
        in_package = _mesh_component(mesh_path, cid, {"HandleId": "0", "Data": dict(WORLD_STATIC_FILTER)}, auto_hide)
        in_list = _mesh_component(mesh_path, cid, {"HandleRefId": "0"}, auto_hide)
    else:
        in_package = _mesh_component(mesh_path, cid, None, auto_hide)
        in_list = _mesh_component(mesh_path, cid, None, auto_hide)
    root = {
        "$type": "entEntityTemplate",
        "appearances": [],
        "backendDataOverrides": [],
        "bindingOverrides": [],
        "compiledData": {
            "BufferId": "0",
            "Flags": BUFFER_FLAGS,
            "Type": RED_PACKAGE,
            "Data": {"Version": 4, "Sections": 7, "CruidIndex": 0, "CruidDict": {"0": "0", "1": cid},
                     "Chunks": [dict(entity), in_package]},
        },
        "compiledEntityLODFlags": 0,
        "componentResolveSettings": [],
        "components": [in_list],
        "cookingPlatform": "PLATFORM_PC",
        "defaultAppearance": cname("default"),
        "entity": {"HandleId": "1" if physical else "0", "Data": dict(entity)},
        "includeInstanceBuffer": None,
        "includes": [],
        "inplaceResources": [],
        "localData": None,
        "resolvedDependencies": [],
        "visualTagsSchema": None,
    }
    return document(root)


# ----------------------------------------------------------------------------- placement maths
def wrap_yaw(y: float) -> float:
    y = math.fmod(y, 360.0)
    if y > 180.0:
        y -= 360.0
    if y <= -180.0:
        y += 360.0
    return y


def place(anchor: dict, local_xyz, local_yaw: float = 0.0):
    """World position and yaw (degrees, counter-clockwise seen from above) of a map-local point."""
    a = math.radians(float(anchor.get("yaw", 0.0)))
    c, s = math.cos(a), math.sin(a)
    x, y, z = (float(v) for v in local_xyz)
    return ((float(anchor["x"]) + c * x - s * y, float(anchor["y"]) + s * x + c * y, float(anchor["z"]) + z),
            wrap_yaw(float(anchor.get("yaw", 0.0)) + float(local_yaw)))


def yaw_quat(deg: float) -> dict:
    h = math.radians(deg) / 2.0
    return {"$type": "Quaternion", "i": 0.0, "j": 0.0, "k": math.sin(h), "r": math.cos(h)}


def streaming_range(radius: float) -> tuple:
    """(primary, secondary) streaming distances for a part of this bounding radius.  Big pieces
    of a building have no distant proxy, so they stay loaded much further than vanilla props."""
    secondary = min(1000.0, max(150.0, 150.0 + 6.0 * radius))
    return secondary * 1.25, secondary


# ----------------------------------------------------------------------------- sector + block
def _node_data(index: int, pos, yaw: float, ref, primary: float, secondary: float) -> dict:
    """``ref``: a 64-bit id (plain entity nodes) or a node reference path (devices)."""
    x, y, z = (float(v) for v in pos)
    if isinstance(ref, str):
        quest_ref = doors.node_ref_json(ref)
    else:
        quest_ref = {"$type": "NodeRef", "$storage": "uint64", "$value": str(ref)}
    return {
        "Id": "0",
        "NodeIndex": index,
        "Position": vec4(x, y, z, 0),
        "Orientation": yaw_quat(yaw),
        "Scale": vec3(1, 1, 1),
        "Pivot": vec3(x, y, z),
        "Bounds": {"$type": "Box", "Max": vec4(x, y, z, 0), "Min": vec4(x, y, z, 0)},
        "QuestPrefabRefHash": quest_ref,
        "UkHash1": {"$type": "NodeRef", "$storage": "uint64", "$value": "0"},
        "CookedPrefabData": {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"},
                             "Flags": "Default"},
        "MaxStreamingDistance": float(secondary),
        "UkFloat1": float(primary),
        "Uk10": 1024,
        "Uk11": 512,
        "Uk12": 0,
        "Uk13": "0",
        "Uk14": "0",
    }


def _entity_node(ent_path: str, debug_name: str) -> dict:
    return {
        "$type": "worldEntityNode",
        "appearanceName": cname("default"),
        "debugName": cname(debug_name),
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


def sector_nodes(layout: dict, anchor: dict) -> list:
    """[(part, world position, yaw, primary, secondary)] for every placement."""
    out = []
    for pl in layout["placements"]:
        part = layout["parts"][pl["part"]]
        pos, yaw = place(anchor, (pl["x"], pl["y"], pl["z"]), pl["yaw"])
        primary, secondary = streaming_range(float(part.get("radius", 10.0)))
        out.append((pl["part"], pos, yaw, primary, secondary))
    return out


def door_devices(layout: dict, anchor: dict) -> list:
    """Every door placement as a device: node reference, open77_doors id, where the device stands
    (``position``, ``yaw``), the middle of its leaf at the foot (``center``) and the radius it opens
    at, in placement order (the order of the sector's device nodes)."""
    out = []
    for pl in layout["placements"]:
        info = layout["parts"][pl["part"]].get("door")
        if not info:
            continue
        pos, yaw = place(anchor, (pl["x"], pl["y"], pl["z"]), pl["yaw"])
        ref = doors.node_ref(layout["name"], len(out))
        out.append({"index": len(out), "ref": ref, "id": doors.door_id(ref), "part": pl["part"],
                    "opening": info["opening"], "position": [round(v, 3) for v in pos], "yaw": round(yaw, 3),
                    "center": [round(v, 3) for v in doors.door_center_world(pos, yaw, info)],
                    "radius": round(doors.door_radius(info), 2)})
    return out


def sector_doc(layout: dict, anchor: dict) -> tuple:
    """(sector document, streaming box (min, max)) for the map placed at `anchor`."""
    name = layout["name"]
    nodes, data, refs = [], [], []
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    placed = sector_nodes(layout, anchor)
    amb = layout.get("ambience") or []
    nested = len(placed) + len(amb)      # nested HandleIds sit above every node's own index
    for i, (part, pos, yaw, primary, secondary) in enumerate(placed):
        meta = layout["parts"][part]
        ent = meta["ent"]
        if meta.get("door"):
            ref = doors.node_ref(name, len(refs))
            refs.append(doors.node_ref_json(ref))
            nodes.append({"HandleId": str(i), "Data": doors.device_node(ent, f"{name}_{part}_{i:03d}")})
        else:
            ref = stable_id("node", name, i, part, round(pos[0], 3), round(pos[1], 3), round(pos[2], 3), round(yaw, 3))
            nodes.append({"HandleId": str(i), "Data": _entity_node(ent, f"{name}_{part}_{i:03d}")})
        data.append(_node_data(i, pos, yaw, ref, primary, secondary))
        reach = min(primary, 600.0)
        for k in range(3):
            lo[k] = min(lo[k], pos[k] - reach)
            hi[k] = max(hi[k], pos[k] + reach)
    for a in amb:
        # quiet interiors (mlo/ambience.py): a worldAmbientAreaNode prism per floor volume
        i = len(data)
        x0, y0, z0, x1, y1, z1 = (float(v) for v in a["box"])
        hx, hy, hz = (x1 - x0) / 2.0, (y1 - y0) / 2.0, (z1 - z0) / 2.0
        pos, yaw = place(anchor, ((x0 + x1) / 2.0, (y0 + y1) / 2.0, z0))
        primary, secondary = streaming_range(math.hypot(hx, hy))
        nodes.append({"HandleId": str(i),
                      "Data": ambience.area_node(f"{name}_quiet_{i:03d}", ambience.UNIT_POINTS,
                                                 ambience.OUTLINE_HEIGHT, nested)})
        nested += 3
        nd = _node_data(i, pos, yaw, 0, primary, secondary)
        nd["Scale"] = vec3(hx, hy, hz)
        nd["Pivot"] = vec3(pos[0], pos[1], pos[2] + hz)
        nd["Uk10"], nd["Uk11"], nd["Uk12"] = 1056, 65024, 1   # the game's area-node nodeData flags
        data.append(nd)
        reach = min(primary, 600.0)
        for k in range(3):
            lo[k] = min(lo[k], pos[k] - reach)
            hi[k] = max(hi[k], pos[k] + reach)
    root = {
        "$type": "worldStreamingSector",
        "category": SECTOR_CATEGORY,
        "cookingPlatform": "PLATFORM_PC",
        "externInplaceResource": depot(None, "Soft"),
        "level": SECTOR_LEVEL,
        "localInplaceResource": [],
        "nodeData": {"BufferId": "0", "Flags": BUFFER_FLAGS, "Type": NODE_BUFFER, "Data": data},
        "nodeRefs": refs,
        "nodes": nodes,
        "persistentNodeIndex": 0,
        "persistentNodes": [],
        "variantIndices": [0],
        "variantNodes": [],
        "version": 62,
    }
    return document(root), (tuple(lo), tuple(hi))


def block_doc(sector_path: str, box) -> dict:
    lo, hi = box
    index = {"$type": "worldStreamingBlockIndex", "oup": "Base", "rldGridCell": 0}
    desc = {
        "$type": "worldStreamingSectorDescriptor",
        "blockIndex": dict(index),
        "category": SECTOR_CATEGORY,
        "data": depot(sector_path, "Soft"),
        "level": SECTOR_LEVEL,
        "numNodeRanges": 1,
        "questPrefabNodeRef": {"$type": "NodeRef", "$storage": "uint64", "$value": "0"},
        "streamingBox": {"$type": "Box", "Max": vec4(hi[0], hi[1], hi[2], 1), "Min": vec4(lo[0], lo[1], lo[2], 1)},
        "variants": [],
    }
    return document({"$type": "worldStreamingBlock", "cookingPlatform": "PLATFORM_PC", "descriptors": [desc],
                     "index": dict(index)})


def archive_xl(block_path: str, devices_path: str | None = None) -> str:
    """ArchiveXL extension merging our streaming block into Night City and, when the map has doors,
    our device list into the world's.  Plain YAML scalars keep the backslashes of depot paths
    literally."""
    text = ("# fivem2open77: adds the baked map sector to Night City (ArchiveXL world streaming)\n"
            "streaming:\n"
            "  blocks:\n"
            f"    - {block_path}\n")
    if devices_path:
        text += ("# its doors and gates are devices: list them with the world's (ArchiveXL resource patch)\n"
                 "resource:\n"
                 "  patch:\n"
                 f"    {devices_path}:\n"
                 "      targets:\n"
                 f"        - {doors.WORLD_DEVICES}\n")
    return text


def f32(v: float) -> float:
    """Round-trip a value through float32 (what the game stores), for comparisons in tests."""
    return struct.unpack("<f", struct.pack("<f", float(v)))[0]
