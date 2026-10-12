"""Interior ambient-noise suppression: ``worldAmbientAreaNode`` volumes over an MLO's rooms.

Night City carries ambient sound zones (``worldAmbientAreaNode`` sectors: street overrides, window
areas, interior room tones).  A map built elsewhere leaves its interiors inside the city's outdoor
zones, so the night-city ambience plays at full volume inside a basement.  The game's own fix is an
interior area with the ``amb_interior`` RTPC turned up (vanilla ``amb_int_main_basement``:
``amb_interior`` 1.0 at Priority 10 with ``revb_interior_room_medium``; street-level areas sit at
Priority 8-9 below it), which ducks the city ambience while the player is inside the prism.

Authoring conventions, measured from vanilla sectors (WolvenKit JSON):

* the outline prism extrudes upward from ``nodeData.Position``: polygon points in the node's local
  XY scaled by ``Scale`` (vanilla often writes a unit square and lets ``Scale`` size it), the shape
  from ``z = 0`` up to ``height`` scaled by ``Scale.Z`` (unit square, ``height`` 2 and
  ``Scale.Z`` = half the room height is the vanilla room pattern);
* the outline lives in ``AreaShapeOutline`` twice: readable ``points`` + ``height`` and the runtime
  ``buffer`` = ``u32 count`` + count x (x, y, 0, 1) float32 + one trailing height float;
* area nodes carry the nodeData flags ``Uk10`` 1056, ``Uk11`` 65024, ``Uk12`` 1.

Volumes are authored in the map's anchor-local frame and baked with the map, so they move with it.

Standard library only: the server's map baker runs ``bake.py`` (and thus ``world.py``) with the
system python3.  Floor detection reads the finished collider tuples, so it needs nothing either.
"""
from __future__ import annotations

import base64
import math
import struct

from ..cp77.wkjson import cname, vec3

# the vanilla interior authoring (amb_int_main_basement): full interior-ness, high priority, room reverb
INTERIOR_RTPC = "amb_interior"
INTERIOR_VALUE = 1.0
INTERIOR_PRIORITY = 10
INTERIOR_REVERB = "revb_interior_room_medium"
OUTLINE_HEIGHT = 2.0            # unit shape height; Scale.Z carries the real room height
UNIT_POINTS = ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))

MODES = ("off", "bottom", "all")


# ----------------------------------------------------------------------------- the shape
def outline_buffer(points, height: float) -> str:
    """AreaShapeOutline.buffer: u32 count, count x (x, y, 0, 1) f32, then the height f32 (base64)."""
    pts = [(float(x), float(y)) for x, y in points]
    out = [struct.pack("<I", len(pts))]
    for x, y in pts:
        out.append(struct.pack("<ffff", x, y, 0.0, 1.0))
    out.append(struct.pack("<f", float(height)))
    return base64.b64encode(b"".join(out)).decode("ascii")


def outline(points, height: float) -> dict:
    """The AreaShapeOutline handle payload (both the buffer and the readable points)."""
    pts = [(float(x), float(y)) for x, y in points]
    return {
        "$type": "AreaShapeOutline",
        "buffer": outline_buffer(pts, height),
        "height": float(height),
        "points": [{"$type": "Vector3", "X": x, "Y": y, "Z": 0.0} for x, y in pts],
    }


def settings(*, interior=INTERIOR_VALUE, priority=INTERIOR_PRIORITY, reverb=INTERIOR_REVERB,
             events=()) -> dict:
    """``audioAmbientAreaSettings``: the ``amb_interior`` RTPC ducks the city ambience while active."""
    return {
        "$type": "audioAmbientAreaSettings",
        "ambientPaletteEntries": [],
        "EmitterDecorator": cname("None"),
        "eventOverrides": cname("None"),
        "EventsOnActive": [{"$type": "audioAudEventStruct", "event": cname(e)} for e in events],
        "EventsOnEnter": [],
        "EventsOnExit": [],
        "FootstepMaterialOverride": cname("None"),
        "insideSourceDistance": 0,
        "isMusic": 0,
        "mergeRoomsWithinArea": 0,
        "MetadataParent": cname("None"),
        "mixingContext": cname("None"),
        "name": cname("None"),
        "outdoorness": 0,
        "outdoornessOverride": 0,
        "outerDistance": 1,
        "Parameters": [{
            "$type": "audioAudParameter", "enterCurveTime": 1, "enterCurveType": "Linear",
            "exitCurveTime": 1, "exitCurveType": "Linear", "name": cname(INTERIOR_RTPC),
            "value": float(interior),
        }],
        "Priority": int(priority),
        "Reverb": cname(reverb),
        "reverbTransitionTime": 0,
        "SoundBanks": [],
        "soundsLimitingSettings": cname("None"),
        "Switches": [],
        "verticalOuterDistance": 0,
        "VoReverb": cname("None"),
        "groupingSettings": {
            "$type": "audioAmbientAreaGroupingSettings", "GroupAvgDistanceRtpc": cname("None"),
            "GroupCountRtpc": cname("None"), "GroupCountTag": cname("None"),
            "groupingVariant": "AllEntities", "GroupingVerticallimit": 3, "MaxDistance": 100,
            "MinDistance": 0,
        },
        "quadSettings": {
            "$type": "audioQuadEmitterSettings", "Angle": 0, "Enabled": 0,
            "Events": {"Elements": [{"$type": "audioAudEventStruct", "event": cname("None")}
                                    for _ in range(4)]},
            "Interleaved": 0, "Offset": vec3(0, 0, 0), "Radius": 0,
        },
    }


def area_node(debug_name: str, points, height: float, handle: int) -> dict:
    """A ``worldAmbientAreaNode``'s Data: one interior notifier over the given outline.

    ``handle`` is the first free HandleId in the document; the notifier, its settings and the
    outline take ``handle``, ``handle + 1``, ``handle + 2``.
    """
    return {
        "$type": "worldAmbientAreaNode",
        "color": {"$type": "Color", "Alpha": 0, "Blue": 0, "Green": 0, "Red": 0},
        "debugName": cname(debug_name),
        "isHostOnly": 0,
        "isVisibleInGame": 1,
        "notifiers": [{
            "HandleId": str(handle), "Data": {
                "$type": "audioAmbientAreaNotifier",
                "acousticRepositioningEnabled": 0,
                "excludeChannels": 0,
                "includeChannels": "TC_SoundAmbientArea",
                "isEnabled": 1,
                "obstructionChangeTime": 0.2,
                "occlusionEnabled": 0,
                "overrideRolloff": 0,
                "rolloffOverride": 1,
                "Settings": {"HandleId": str(handle + 1), "Data": settings()},
            },
        }],
        "outline": {"HandleId": str(handle + 2), "Data": outline(points, height)},
        "proxyScale": None,
        "sourcePrefabHash": "0",
        "tag": "None",
        "tagExt": "None",
        "useCustomColor": 0,
    }


# ----------------------------------------------------------------------------- where the floors are
def _placed_box(col, position, yaw_deg):
    """(x0, y0, z0, x1, y1, z1) of a box collider placed at ``position`` with ``yaw_deg``, or None."""
    x, y, z, w = (float(v) for v in col.rotation)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz, wx, wy, wz = x * y, x * z, y * z, w * x, w * y, w * z
    r = ((1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)),
         (2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)),
         (2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)))
    a = math.radians(yaw_deg)
    c, s = math.cos(a), math.sin(a)
    hx, hy, hz = (float(v) for v in col.half)
    cx, cy, cz = (float(v) for v in col.center)
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for sx in (-hx, hx):
        for sy in (-hy, hy):
            for sz in (-hz, hz):
                px = cx + r[0][0] * sx + r[0][1] * sy + r[0][2] * sz
                py = cy + r[1][0] * sx + r[1][1] * sy + r[1][2] * sz
                pz = cz + r[2][0] * sx + r[2][1] * sy + r[2][2] * sz
                wx2 = position[0] + c * px - s * py
                wy2 = position[1] + s * px + c * py
                wz2 = position[2] + pz
                for k, v in enumerate((wx2, wy2, wz2)):
                    lo[k] = min(lo[k], v)
                    hi[k] = max(hi[k], v)
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def _floor_boxes(model):
    """(top z, xy half sizes, centre xy) of walkable box tops of the MLO parts, anchor-local.

    Only MLO parts and their collision spools: the building shell around an MLO brings the street
    and the roof, which are outdoors and must stay loud.  A walkable box is wide in x and y (floor
    slabs and the fill under them; walls and steps are thin in one horizontal direction).
    """
    out = []
    for pl in model.placements:
        part = model.parts.get(pl.part)
        if part is None or not (part.kind == "mlo" or part.collision_only):
            continue
        for col in part.colliders or ():
            if col.kind != "box":
                continue
            box = _placed_box(col, pl.position, pl.yaw)
            if box is None:
                continue
            x0, y0, z0, x1, y1, z1 = box
            if min(x1 - x0, y1 - y0) < 1.2:      # walls, steps, rails: thin in one direction
                continue
            if (x1 - x0) * (y1 - y0) < 3.0:
                continue
            out.append((z1, x0, y0, x1, y1))
    return out


def floor_volumes(model, mode: str = "bottom", *, gap=0.75, level_min=40.0, grow=1.2) -> list:
    """Interior volumes to quiet, one per floor level, bottom first.  Anchor-local boxes
    ``[x0, y0, z0, x1, y1, z1]``.

    Floor levels are z clusters of walkable box tops whose largest box is a real floor slab
    (``level_min`` m2; stair runs and platforms cluster below that).  Each volume runs from inside
    its floor up to just under the next level's slab, so a player on the floor above stays outside.

    ``mode``: ``bottom`` quiets only the lowest level (the enclosed lower floor of an MLO), ``all``
    every level, ``off`` nothing.
    """
    if mode not in MODES:
        raise ValueError(f"quiet_interiors: {', '.join(MODES)}")
    if mode == "off":
        return []
    slabs = sorted(_floor_boxes(model))
    clusters: list = []
    for z, x0, y0, x1, y1 in slabs:
        if clusters and z - clusters[-1][-1][0] <= gap:
            clusters[-1].append((z, x0, y0, x1, y1))
        else:
            clusters.append([(z, x0, y0, x1, y1)])
    levels = [cl for cl in clusters if max((b[3] - b[1]) * (b[4] - b[2]) for b in cl) >= level_min]
    levels.sort(key=lambda cl: cl[0][0])
    volumes = []
    for i, cl in enumerate(levels):
        z0 = min(b[0] for b in cl) - 0.5
        z1 = (min(b[0] for b in levels[i + 1]) - 0.45) if i + 1 < len(levels) else z0 + 4.0
        if z1 - z0 < 1.0:
            continue
        volumes.append([min(b[1] for b in cl) - grow, min(b[2] for b in cl) - grow, z0,
                        max(b[3] for b in cl) + grow, max(b[4] for b in cl) + grow, z1])
    if mode == "bottom":
        volumes = volumes[:1]
    return [[round(v, 2) for v in box] for box in volumes]
