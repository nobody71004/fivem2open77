"""A streaming sector as WolvenKit writes it in JSON, read into what an edit needs.

A sector has ``nodes`` (what to draw or do: a mesh, an entity, collision, a light...) and
``nodeData``: the node *instances*, each a node index with a transform.  ArchiveXL addresses an
edit by the instance's index in ``nodeData`` and checks the sector still has ``expectedNodes``
instances (the length of ``nodeData``).  Inside an instance two node types have elements of their
own, which ArchiveXL can delete or move one by one:

    worldInstancedMeshNode             its instances (``worldTransformsBuffer``: a slice of a
                                       transform buffer the sector's instanced nodes share)
    worldInstancedDestructibleMeshNode its instances (``cookedInstanceTransforms``)
    worldCollisionNode                 its actors (``compiledData`` Actors; positions in the
                                       engine's fixed point, 1/131072 m)

Everything here is world coordinates (Cyberpunk: x east, y north, z up, metres).
"""
from __future__ import annotations

import math
import re

FIXED = 131072.0               # WorldPosition FixedPoint: 17 fractional bits

# the node kinds an editor shows, by the engine type
KINDS = {
    "worldStaticMeshNode": "mesh", "worldMeshNode": "mesh", "worldBendedMeshNode": "mesh",
    "worldInstancedMeshNode": "instanced", "worldInstancedDestructibleMeshNode": "instanced",
    "worldTerrainMeshNode": "terrain", "worldTerrainCollisionNode": "terrain",
    "worldCollisionNode": "collision", "worldEntityNode": "entity", "worldDeviceNode": "entity",
    "worldPopulationSpawnerNode": "spawner", "worldStaticDecalNode": "decal", "worldDecalNode": "decal",
    "worldStaticLightNode": "light", "worldStaticOccluderMeshNode": "occluder", "worldInstancedOccluderNode": "occluder",
    "worldFoliageNode": "foliage", "worldStaticParticleNode": "effect", "worldEffectNode": "effect",
    "worldWaterPatchNode": "water", "worldRoadProxyMeshNode": "road", "worldTrafficLanesSpotsNode": "traffic",
}
# resource fields, in the order a node type names its main resource
RESOURCE_KEYS = ("mesh", "entityTemplate", "material", "effect", "particleSystem", "foliageResource", "data", "iesProfile")
APPEARANCE_KEYS = ("meshAppearance", "appearanceName", "appearance")


def _v(x):
    """CName / ResourcePath / enum wrappers -> plain value."""
    if isinstance(x, dict):
        if "DepotPath" in x:
            return _v(x["DepotPath"])
        if "$value" in x:
            v = x["$value"]
            return "" if v in (None, "None", "0") and x.get("$type") in ("CName", "ResourcePath") else v
    return x


def _vec(d, keys=("X", "Y", "Z")):
    if not isinstance(d, dict):
        return None
    try:
        return [float(d.get(k, 0.0)) for k in keys]
    except (TypeError, ValueError):
        return None


def _quat(d):
    if not isinstance(d, dict):
        return [0.0, 0.0, 0.0, 1.0]
    return [float(d.get("i", 0.0)), float(d.get("j", 0.0)), float(d.get("k", 0.0)), float(d.get("r", 1.0))]


def quat_rotate(q, v):
    """Rotate vector v by quaternion q = [i, j, k, r]."""
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)]


def yaw_of(q) -> float:
    """Heading (degrees, counter-clockwise from +y seen from above) of a quaternion [i, j, k, r]."""
    fx, fy, _ = quat_rotate(q, [0.0, 1.0, 0.0])
    return math.degrees(math.atan2(-fx, fy))


def quat_yaw(deg: float):
    r = math.radians(deg) / 2.0
    return [0.0, 0.0, math.sin(r), math.cos(r)]


def _walk_handles(obj, out):
    """Every ``HandleId`` -> its Data, anywhere below obj (shared buffers are written once and
    referenced by ``HandleRefId`` afterwards)."""
    if isinstance(obj, dict):
        hid = obj.get("HandleId")
        if hid is not None and "Data" in obj:
            out[str(hid)] = obj["Data"]
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_handles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                _walk_handles(v, out)


def _deref(obj, handles):
    if isinstance(obj, dict) and "HandleRefId" in obj:
        return handles.get(str(obj["HandleRefId"]))
    if isinstance(obj, dict) and "Data" in obj and "HandleId" in obj:
        return obj["Data"]
    return obj


def _instances(node, handles):
    """World transforms of an instanced node's instances: [(pos, quat, scale)]."""
    t = node.get("$type")
    if t == "worldInstancedMeshNode":
        buf = node.get("worldTransformsBuffer") or {}
        start, count = int(buf.get("startIndex") or 0), int(buf.get("numElements") or 0)
        shared = _deref(buf.get("sharedDataBuffer"), handles) or {}
        data = ((shared.get("buffer") or {}).get("Data") or {}).get("Transforms") or []
        out = []
        for tr in data[start:start + count]:
            out.append((_vec(tr.get("translation")) or [0.0, 0.0, 0.0], _quat(tr.get("rotation")),
                        _vec(tr.get("scale")) or [1.0, 1.0, 1.0]))
        return out, count
    if t == "worldInstancedDestructibleMeshNode":
        buf = node.get("cookedInstanceTransforms") or {}
        start, count = int(buf.get("startIndex") or 0), int(buf.get("numElements") or 0)
        shared = _deref(buf.get("sharedDataBuffer"), handles) or {}
        data = ((shared.get("buffer") or {}).get("Data") or {}).get("Transforms") or []
        return [(_vec(tr.get("translation")) or [0.0, 0.0, 0.0], _quat(tr.get("rotation")),
                 _vec(tr.get("scale")) or [1.0, 1.0, 1.0]) for tr in data[start:start + count]], count
    return [], 0


def _actors(node):
    """A collision node's actors: [(pos, quat, shapes, scale)]."""
    cd = node.get("compiledData") or {}
    actors = ((cd.get("Data") or {}).get("Actors")) or []
    out = []
    for a in actors:
        p = a.get("Position") or {}
        try:
            pos = [int(p[k]["Bits"]) / FIXED for k in ("x", "y", "z")]
        except (KeyError, TypeError, ValueError):
            pos = [0.0, 0.0, 0.0]
        shapes = [s.get("ShapeType", "?") for s in a.get("Shapes") or []]
        out.append((pos, _quat(a.get("Orientation")), shapes, _vec(a.get("Scale")) or [1.0, 1.0, 1.0]))
    return out


def quat_mul(a, b):
    """Hamilton product a * b of quaternions [i, j, k, r] (b applied first)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def quat_inv(q):
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w or 1.0
    return [-x / n, -y / n, -z / n, w / n]


def quat_norm(q):
    n = math.sqrt(sum(v * v for v in q)) or 1.0
    return [v / n for v in q]


def same_rotation(a, b, tol: float = 0.0005) -> bool:
    """Two quaternions turn things the same way (q and -q are one rotation)."""
    d = abs(sum(x * y for x, y in zip(quat_norm(a), quat_norm(b))))
    return d >= 1.0 - tol


def read_sector(doc: dict, path: str, element_cap: int | None = None) -> dict:
    """WolvenKit's JSON of one ``.streamingsector`` -> {path, level, category, nodes (the number of
    node instances: ArchiveXL's expectedNodes), entries: [...]}.  An entry:

        i        the instance's index in nodeData (what ArchiveXL edits)
        type     the node's engine type; kind: mesh / instanced / collision / entity / ...
        name     its debug name; res: its main resource (mesh, entity template...); app: appearance
        pos, rot, scale   the instance's transform (rot: quaternion i j k r)
        elements {kind: instances|actors, count, items: [{pos, rot, yaw, scale?, shapes?}]} for nodes
                 that have them (every element: edits and the collision that goes with an object
                 need them all; ``element_cap`` keeps fewer, for a quick look)
        center   a representative world point (the elements' middle for instanced/collision nodes)
    """
    root = (doc.get("Data") or {}).get("RootChunk") or doc
    handles = {}
    _walk_handles(root.get("nodes") or [], handles)
    nodes = []
    for n in root.get("nodes") or []:
        nodes.append(_deref(n, handles) or {})
    nd = root.get("nodeData") or {}
    items = nd.get("Data") if isinstance(nd, dict) else nd
    items = items or []
    entries = []
    for i, e in enumerate(items):
        ni = int(e.get("NodeIndex", -1))
        node = nodes[ni] if 0 <= ni < len(nodes) else {}
        t = node.get("$type", "?")
        pos = _vec(e.get("Position")) or [0.0, 0.0, 0.0]
        rot = _quat(e.get("Orientation"))
        scale = _vec(e.get("Scale")) or [1.0, 1.0, 1.0]
        res = ""
        for k in RESOURCE_KEYS:
            if k in node:
                res = _v(node[k]) or ""
                if res:
                    break
        app = ""
        for k in APPEARANCE_KEYS:
            if k in node:
                app = _v(node[k]) or ""
                if app:
                    break
        ent = {"i": i, "n": ni, "type": t, "kind": KINDS.get(t, "other"), "name": _v(node.get("debugName")) or "",
               "res": str(res), "app": str(app), "pos": [round(v, 4) for v in pos], "rot": [round(v, 6) for v in rot],
               "yaw": round(yaw_of(rot), 2), "scale": [round(v, 4) for v in scale],
               "dist": round(float(e.get("UkFloat1") or 0.0), 1)}
        center = pos
        if t in ("worldInstancedMeshNode", "worldInstancedDestructibleMeshNode"):
            inst, count = _instances(node, handles)
            pts = []
            for ip, iq, isc in inst:
                wp = [a + b for a, b in zip(pos, quat_rotate(rot, ip))]
                wq = quat_mul(rot, iq)
                pts.append({"pos": [round(v, 4) for v in wp], "rot": [round(v, 6) for v in wq],
                            "yaw": round(yaw_of(wq), 2), "scale": [round(v, 4) for v in isc]})
            ent["elements"] = {"kind": "instances", "count": count, "items": pts[:element_cap] if element_cap else pts}
            if pts:
                center = [sum(p["pos"][k] for p in pts) / len(pts) for k in range(3)]
        elif t == "worldCollisionNode":
            acts = _actors(node)
            its = [{"pos": [round(v, 4) for v in p], "rot": [round(v, 6) for v in q], "yaw": round(yaw_of(q), 2),
                    "shapes": s[:6], "scale": [round(v, 4) for v in sc]} for p, q, s, sc in acts]
            ent["elements"] = {"kind": "actors", "count": len(acts), "items": its[:element_cap] if element_cap else its}
            if acts:
                center = [sum(p[k] for p, _q, _s, _c in acts) / len(acts) for k in range(3)]
        elif t == "worldFoliageNode":
            # the instances are in the sector's compiled foliage (.cfoliage): its Populations slice
            # [stancesBegin, stancesBegin + stancesCount), positions relative to this node
            span = node.get("populationSpanInfo") or {}
            ent["foliage"] = {"res": str(_v(node.get("foliageResource")) or ""),
                              "begin": int(span.get("stancesBegin") or 0), "count": int(span.get("stancesCount") or 0)}
        ent["center"] = [round(v, 3) for v in center]
        entries.append(ent)
    return {"path": path, "level": root.get("level"), "category": root.get("category"), "nodes": len(items),
            "entries": entries}


GRID_RX = re.compile(r"(?:^|\\)(exterior|interior|quest|navigation|proxy)_(-?\d+)_(-?\d+)_(-?\d+)_(\d+)\.streamingsector$", re.I)
CELL = 64.0                    # a level-0 sector's cell (metres); level L: CELL * 2**L


def grid_of(path: str):
    """(category, x, y, z, level) of a grid sector's depot path, else None (quest_<hash> ...)."""
    m = GRID_RX.search(path or "")
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))


def cell_box(path: str):
    """The x/y square a grid sector's objects sit in (they spill over its edges a little), or None."""
    g = grid_of(path)
    if not g:
        return None
    size = CELL * (2 ** g[4])
    return g[1] * size, g[2] * size, (g[1] + 1) * size, (g[2] + 1) * size, size


def entry_points(e: dict):
    """The world points an entry occupies (its elements', or its own)."""
    el = e.get("elements")
    if el and el.get("items"):
        return [it["pos"] for it in el["items"]]
    return [e["pos"]]


def near(e: dict, x: float, y: float, radius: float, z: float | None = None, dz: float | None = None) -> bool:
    for p in entry_points(e):
        if (p[0] - x) ** 2 + (p[1] - y) ** 2 <= radius * radius and (z is None or dz is None or abs(p[2] - z) <= dz):
            return True
    return False
