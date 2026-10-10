"""Colliders -> the ``meshMeshParamPhysics`` parameter of a static Cyberpunk mesh.

The layout copies vanilla static meshes (e.g. base\\environment\\...\\freight_ep1_a_wall_a.mesh):
one static physicsSystemBody whose collisionShapes are boxes / spheres / capsules, each with the
"World Static" filter.  No convex hulls: in a mesh they need PhysX-cooked data (vanilla ships it in
``compiledGeometryBuffer``), and a vertices-only convex crashes the game in PhysX when the mesh's
collision loads; collision.boxify turns any hull that gets here into boxes.
"""
from __future__ import annotations

import math

from ..cp77.wkjson import Handles, cname

FILTER = {
    "$type": "physicsFilterData",
    "customFilterData": None,
    "preset": cname("World Static"),
    "queryFilter": {"$type": "physicsQueryFilter", "mask1": "0", "mask2": "71942148"},
    "simulationFilter": {"$type": "physicsSimulationFilter", "mask1": "376836", "mask2": "5070"},
}


def _transform(pos=(0.0, 0.0, 0.0), q=(0.0, 0.0, 0.0, 1.0)) -> dict:
    return {"$type": "Transform",
            "orientation": {"$type": "Quaternion", "i": float(q[0]), "j": float(q[1]), "k": float(q[2]), "r": float(q[3])},
            "position": {"$type": "Vector4", "W": 0, "X": float(pos[0]), "Y": float(pos[1]), "Z": float(pos[2])}}


def _common(c, filt) -> dict:
    return {
        "filterData": filt,
        "isImported": 0,
        "isQueryShapeOnly": 0,
        "localToBody": _transform(c.center, c.rotation),
        "material": cname(c.physmat),
        "materialApperanceOverrides": [],
        "tag": cname("None"),
        "volumeModifier": 1,
    }


def _finite(c) -> bool:
    vals = list(c.center) + list(c.rotation) + list(c.half) + [c.radius, c.height]
    return all(math.isfinite(float(v)) for v in vals)


def collider_json(c, filt) -> dict:
    d = _common(c, filt)
    if c.kind == "box":
        d.update({"$type": "physicsColliderBox", "isObstacle": 0,
                  "halfExtents": {"$type": "Vector3", "X": float(max(c.half[0], 0.005)),
                                  "Y": float(max(c.half[1], 0.005)), "Z": float(max(c.half[2], 0.005))}})
    elif c.kind == "sphere":
        d.update({"$type": "physicsColliderSphere", "radius": float(c.radius)})
    elif c.kind == "capsule":
        d.update({"$type": "physicsColliderCapsule", "radius": float(c.radius), "height": float(c.height)})
    else:
        raise ValueError(f"unknown collider kind {c.kind}")
    # key order like WolvenKit writes it ($type first)
    return {"$type": d.pop("$type"), **dict(sorted(d.items()))}


def physics_param(colliders, handles: Handles, body_name: str, simulation: str = "Static") -> dict:
    """-> {"HandleId", "Data": meshMeshParamPhysics} ready to append to CMesh.parameters.
    ``simulation``: "Static" for map geometry, "Kinematic" for a door leaf the game moves."""
    from .collision import boxify
    colliders = [c for c in boxify(colliders) if _finite(c)]
    filt_def = handles.new(dict(FILTER))
    filt_ref = {"HandleRefId": filt_def["HandleId"]}
    shapes = []
    for i, c in enumerate(colliders):
        shapes.append(handles.new(collider_json(c, filt_def if i == 0 else filt_ref)))
    body = {
        "$type": "physicsSystemBody",
        "collisionShapes": shapes,
        "isQueryBodyOnly": 0,
        "localToModel": _transform(),
        "mappedBoneName": cname("None"),
        "mappedBoneToBody": _transform(),
        "name": cname(body_name),
        "params": {
            "$type": "physicsSystemBodyParams", "angularDamping": 0, "comOffset": _transform(),
            "inertia": {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1}, "linearDamping": 0, "mass": 1000,
            "maxAngularVelocity": -1, "maxContactImpulse": -1, "maxDepenetrationVelocity": -1,
            "simulationType": simulation, "solverIterationsCountPosition": 4, "solverIterationsCountVelocity": 1,
        },
    }
    resource = {"$type": "physicsSystemResource", "bodies": [handles.new(body)], "cookingPlatform": "PLATFORM_None",
                "joints": []}
    return handles.new({"$type": "meshMeshParamPhysics", "physicsData": handles.new(resource)})
