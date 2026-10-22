"""What a person (or an agent) does to the world, as edits: delete, move, replace and add objects,
with the collision that belongs to an object following it.

Night City's collision is not in its meshes: a sector's ``worldCollisionNode`` holds the collision of
the meshes around it as actors (often in a coarser sector than the mesh's own), one per mesh
instance, placed exactly where that instance stands.  So an object's collision is found by its place:
the actors at the instance's position and rotation (within a few millimetres), in every sector
around.  Terrain is the same with a ``worldTerrainCollisionNode`` at the terrain mesh's position.
Deleting or moving an object deletes or moves those too; otherwise the player would walk into an
invisible wall where the object was (or through the object where it went).

An entity (a sign, a lamp, a device) has a stand-in for far views: a ``worldEntityProxyMeshNode`` in a
coarser sector, exactly at the entity's place, shown until the entity itself is loaded.  It goes and
moves with its entity too, or the sign would still be seen from down the street.

Every operation returns the edits of one action (one ``group``), ready for the edit list.
"""
from __future__ import annotations

import math
import uuid

from . import edits as E
from . import sector as S

MESH_TYPES = ("worldStaticMeshNode", "worldMeshNode", "worldBendedMeshNode", "worldInstancedMeshNode",
              "worldInstancedDestructibleMeshNode", "worldTerrainMeshNode", "worldStaticOccluderMeshNode",
              "worldFoliageNode", "worldPhysicalDestructionNode", "worldRoadProxyMeshNode")
PROXY_TYPES = ("worldEntityProxyMeshNode", "worldDestructibleEntityProxyMeshNode")
POS_TOL = 0.02          # metres
ROT_TOL = 0.0005        # 1 - |cos(half angle)|


def _gid() -> str:
    return "g" + uuid.uuid4().hex[:10]


def _ref(sec: dict, e: dict) -> dict:
    return {"sector": sec["path"], "index": e["i"], "type": e["type"], "expectedNodes": sec["nodes"]}


def _point(e: dict, element=None):
    """(pos, rot) of a node instance or of one of its elements."""
    if element is None:
        return e["pos"], e["rot"]
    items = (e.get("elements") or {}).get("items") or []
    if not 0 <= element < len(items):
        raise E.EditError(f"{e['type']} #{e['i']} has no element {element} ({len(items)})")
    it = items[element]
    return it["pos"], it.get("rot") or [0.0, 0.0, 0.0, 1.0]


def _label(e: dict, element=None) -> str:
    base = e.get("name") if e.get("name") not in ("", "None") else ""
    res = (e.get("res") or "").split("\\")[-1]
    s = base or res or e["type"]
    return f"{s} #{element}" if element is not None else s


def is_entity(node_type: str) -> bool:
    return node_type == "worldDeviceNode" or (node_type.endswith("EntityNode") and "Proxy" not in node_type)


def companions(index, sec: dict, e: dict, element=None) -> dict:
    """What stands exactly where an object stands: {collision: [the collision that belongs to it:
    actors of collision nodes, or for terrain the terrain collision nodes], sharers: [other meshes at
    the same place and rotation], proxies: [an entity's far-view stand-ins]}.  Each item: {sector,
    index, type, expectedNodes, element?, expectedElements?, pos, rot, label?}.

    Collision found at a place several meshes share cannot be told apart: it belongs to all of them."""
    none = {"collision": [], "sharers": [], "proxies": []}
    entity = is_entity(e["type"])
    if (e["type"] not in MESH_TYPES and not entity) or (entity and element is not None):
        return none
    pos, rot = _point(e, element)
    rows = index.sectors_at(pos[0], pos[1], 2.0, categories={"Exterior", "Interior"})
    secs = index.sectors([r[0] for r in rows])
    col, share, prox = [], [], []

    def here(p, q):
        return (abs(p[0] - pos[0]) <= POS_TOL and abs(p[1] - pos[1]) <= POS_TOL and abs(p[2] - pos[2]) <= POS_TOL
                and S.same_rotation(q or [0, 0, 0, 1], rot, ROT_TOL))

    if entity:
        for s in secs.values():
            for c in s["entries"]:
                if c["type"] in PROXY_TYPES and here(c["pos"], c["rot"]):
                    prox.append(dict(_ref(s, c), pos=c["pos"], rot=c["rot"], label=_label(c)))
        return dict(none, proxies=prox)

    for s in secs.values():
        for c in s["entries"]:
            if e["type"] == "worldTerrainMeshNode" and c["type"] == "worldTerrainCollisionNode":
                if all(abs(a - b) <= 0.05 for a, b in zip(c["pos"], pos)):
                    col.append(dict(_ref(s, c), pos=c["pos"], rot=c["rot"]))
                continue
            items = (c.get("elements") or {}).get("items") or []
            if c["type"] == "worldCollisionNode":
                for k, it in enumerate(items):
                    if here(it["pos"], it.get("rot")):
                        col.append(dict(_ref(s, c), element=k, expectedElements=len(items), pos=it["pos"], rot=it.get("rot")))
                continue
            if c["type"] not in MESH_TYPES or c["type"] == "worldTerrainMeshNode":
                continue
            if items and c["kind"] == "instanced":
                for k, it in enumerate(items):
                    if (s["path"], c["i"], k) == (sec["path"], e["i"], element):
                        continue
                    if here(it["pos"], it.get("rot")):
                        share.append(dict(_ref(s, c), element=k, expectedElements=len(items), pos=it["pos"],
                                          rot=it.get("rot"), label=_label(c, k)))
            elif not (s["path"] == sec["path"] and c["i"] == e["i"] and element is None) and here(c["pos"], c["rot"]):
                share.append(dict(_ref(s, c), pos=c["pos"], rot=c["rot"], label=_label(c)))
    return {"collision": col, "sharers": share, "proxies": prox}


def collision_of(index, sec: dict, e: dict, element=None) -> list:
    return companions(index, sec, e, element)["collision"]


def _was(e: dict, element=None) -> dict:
    pos, rot = _point(e, element)
    w = {"pos": pos, "rot": rot, "yaw": round(S.yaw_of(rot), 2)}
    if element is None:
        w.update(res=e.get("res"), app=e.get("app"), scale=e.get("scale"))
    return w


def delete(index, sector: str, i: int, element=None, with_collision=True, notes=None) -> list:
    """Delete a node instance (or one element of it) and the collision that belongs to it.  Collision
    at a place other meshes share stays (it is theirs too): ``notes`` gets why; delete those
    together (``delete_many``) to take it away."""
    sec, e = index.entry(sector, i)
    g = _gid()
    out = []
    if element is not None:
        el = e.get("elements") or {}
        if not el:
            raise E.EditError(f"{e['type']} has no elements")
        out.append(dict(_ref(sec, e), op="delete", elements=[int(element)], expectedElements=el["count"],
                        group=g, role="object", label=_label(e, element), kind=e["kind"], was=_was(e, element)))
    else:
        out.append(dict(_ref(sec, e), op="delete", group=g, role="object", label=_label(e), kind=e["kind"], was=_was(e)))
    if with_collision:
        comp = companions(index, sec, e, element)
        out += _proxy_deletions(comp, g, _label(e, element))
        if comp["sharers"] and comp["collision"]:
            if notes is not None:
                notes.append(f"{_label(e, element)}: its collision stays: {len(comp['sharers'])} other object(s) stand at "
                             f"exactly the same place ({', '.join(x['label'] for x in comp['sharers'][:4])}) and the "
                             "collision there is theirs too; delete them together to take it away")
            return out
        for c in comp["collision"]:
            d = {k: c[k] for k in ("sector", "index", "type", "expectedNodes")}
            if "element" in c:
                d.update(elements=[c["element"]], expectedElements=c["expectedElements"])
            out.append(dict(d, op="delete", group=g, role="collision", label=f"collision of {_label(e, element)}",
                            kind="collision", was={"pos": c["pos"], "rot": c["rot"]}))
    return out


def _proxy_deletions(comp, group, label) -> list:
    return [dict({k: p[k] for k in ("sector", "index", "type", "expectedNodes")}, op="delete", group=group, role="proxy",
                 label=f"far view of {label}", kind="other", was={"pos": p["pos"], "rot": p["rot"]})
            for p in comp.get("proxies") or []]


def delete_many(index, targets, with_collision=True, notes=None) -> list:
    """Delete several objects as one action: [(sector, i, element|None)].  Collision shared by
    objects that all go goes with them."""
    g = _gid()
    out, gone = [], set()
    for sector, i, element in targets:
        gone.add((sector.lower(), int(i), element))
    seen = set()
    for sector, i, element in targets:
        sec, e = index.entry(sector, i)
        if element is not None:
            out.append(dict(_ref(sec, e), op="delete", elements=[int(element)], expectedElements=(e.get("elements") or {})["count"],
                            group=g, role="object", label=_label(e, element), kind=e["kind"], was=_was(e, element)))
        else:
            out.append(dict(_ref(sec, e), op="delete", group=g, role="object", label=_label(e), kind=e["kind"], was=_was(e)))
        if not with_collision:
            continue
        comp = companions(index, sec, e, element)
        for d in _proxy_deletions(comp, g, _label(e, element)):
            key = (d["sector"].lower(), d["index"], None)
            if key not in seen and key not in gone:
                seen.add(key)
                out.append(d)
        left = [x for x in comp["sharers"] if (x["sector"].lower(), x["index"], x.get("element")) not in gone]
        if left and comp["collision"]:
            if notes is not None:
                notes.append(f"{_label(e, element)}: its collision stays ({len(left)} other object(s) at the same place stay)")
            continue
        for c in comp["collision"]:
            key = (c["sector"].lower(), c["index"], c.get("element"))
            if key in seen:
                continue
            seen.add(key)
            d = {k: c[k] for k in ("sector", "index", "type", "expectedNodes")}
            if "element" in c:
                d.update(elements=[c["element"]], expectedElements=c["expectedElements"])
            out.append(dict(d, op="delete", group=g, role="collision", label=f"collision of {_label(e, element)}",
                            kind="collision", was={"pos": c["pos"], "rot": c["rot"]}))
    return out


def _target_transform(pos, rot, to_pos=None, to_yaw=None, to_rot=None, delta=None, dyaw=None):
    """The new (pos, rot) from an absolute place, a turn, or a shift."""
    npos = list(pos)
    nrot = list(rot)
    if to_pos is not None:
        npos = [float(v) for v in to_pos]
    if delta is not None:
        npos = [a + float(b) for a, b in zip(npos, delta)]
    if to_rot is not None:
        nrot = S.quat_norm([float(v) for v in to_rot])
    elif to_yaw is not None:
        # keep the tilt, set the heading: turn about z by the difference of headings
        turn = float(to_yaw) - S.yaw_of(rot)
        nrot = S.quat_norm(S.quat_mul(S.quat_yaw(turn), rot))
    if dyaw:
        nrot = S.quat_norm(S.quat_mul(S.quat_yaw(float(dyaw)), nrot))
    return npos, nrot


def move(index, sector: str, i: int, element=None, pos=None, yaw=None, rot=None, delta=None, dyaw=None,
         scale=None, with_collision=True, notes=None) -> list:
    """Move / turn a node instance (or one element of it); its collision follows (keeping where it sits
    relative to the object)."""
    sec, e = index.entry(sector, i)
    p0, q0 = _point(e, element)
    p1, q1 = _target_transform(p0, q0, pos, yaw, rot, delta, dyaw)
    g = _gid()
    m = dict(_ref(sec, e), op="move", group=g, role="object", label=_label(e, element), kind=e["kind"],
             was=_was(e, element), pos=[round(v, 4) for v in p1], rot=[round(v, 7) for v in q1])
    if element is not None:
        el = e.get("elements") or {}
        if not el:
            raise E.EditError(f"{e['type']} has no elements")
        m.update(element=int(element), expectedElements=el["count"])
    if scale is not None:
        m["scale"] = [float(v) for v in scale]
    out = [m]
    if scale is not None and notes is not None:
        notes.append(f"{_label(e, element)}: collision does not scale with the object")
    if with_collision:
        dq = S.quat_mul(q1, S.quat_inv(q0))                  # the turn the object makes
        comp = companions(index, sec, e, element)
        for p in comp["proxies"]:                            # an entity's far view stands where it stands
            d = {k: p[k] for k in ("sector", "index", "type", "expectedNodes")}
            out.append(dict(d, op="move", group=g, role="proxy", label=f"far view of {_label(e, element)}", kind="other",
                            was={"pos": p["pos"], "rot": p["rot"]}, pos=[round(v, 4) for v in p1],
                            rot=[round(v, 7) for v in S.quat_norm(S.quat_mul(dq, p["rot"] or [0, 0, 0, 1]))]))
        if comp["sharers"] and comp["collision"]:
            if notes is not None:
                notes.append(f"{_label(e, element)}: its collision stays where it is: {len(comp['sharers'])} other "
                             f"object(s) stand at the same place ({', '.join(x['label'] for x in comp['sharers'][:4])})")
            return out
        for c in comp["collision"]:
            rel = [a - b for a, b in zip(c["pos"], p0)]
            cp = [a + b for a, b in zip(p1, S.quat_rotate(dq, rel))]
            cq = S.quat_norm(S.quat_mul(dq, c["rot"] or [0, 0, 0, 1]))
            d = {k: c[k] for k in ("sector", "index", "type", "expectedNodes")}
            if "element" in c:
                d.update(element=c["element"], expectedElements=c["expectedElements"])
            out.append(dict(d, op="move", group=g, role="collision", label=f"collision of {_label(e, element)}",
                            kind="collision", was={"pos": c["pos"], "rot": c["rot"]},
                            pos=[round(v, 4) for v in cp], rot=[round(v, 7) for v in cq]))
    return out


def replace(index, sector: str, i: int, res=None, app=None) -> list:
    """Another mesh (or appearance) for a node; its collision stays the original's."""
    sec, e = index.entry(sector, i)
    r = dict(_ref(sec, e), op="replace", group=_gid(), role="object", label=_label(e), kind=e["kind"], was=_was(e))
    if res:
        r["res"] = res
    if app is not None:
        r["app"] = app
    return [r]


def add(res: str, pos, yaw: float = 0.0, app: str | None = None, collision: str = "mesh", label=None) -> list:
    a = {"op": "add", "res": res, "pos": [float(v) for v in pos], "yaw": float(yaw or 0.0), "group": _gid(),
         "role": "object", "label": label or res.split("\\")[-1], "kind": "added", "collision": collision}
    if app:
        a["app"] = app
    return [a]


def clear_area(index, x: float, y: float, radius: float, kinds=("mesh", "instanced", "entity", "decal"),
               text=None, z=None, dz=None, with_collision=True, limit=500) -> list:
    """Delete every object of the given kinds in a circle: one group per object (elements of instanced
    nodes one by one).  The caller shows the list before it goes in (a dry run)."""
    q = index.query(x, y, radius, z=z, dz=dz, kinds=list(kinds), text=text, limit=limit, element_limit=100000)
    targets = []
    for e in q["entries"]:
        if e.get("elements"):
            if e["elements"]["kind"] != "instances":
                continue
            targets += [(e["sector"], e["i"], it["k"]) for it in e["elements"]["items"]]
        else:
            targets.append((e["sector"], e["i"], None))
    return delete_many(index, targets, with_collision=with_collision)


def describe(edits) -> list:
    """One line per action (group) for people: what it does and what came along with it."""
    groups = {}
    for e in edits:
        groups.setdefault(e.get("group") or e.get("id"), []).append(e)
    out = []
    for g, items in groups.items():
        obj = next((x for x in items if x.get("role") not in ("collision", "proxy")), items[0])
        cols = sum(1 for x in items if x.get("role") == "collision")
        proxies = sum(1 for x in items if x.get("role") == "proxy")
        verb = {"delete": "delete", "move": "move", "replace": "replace", "add": "add"}[obj["op"]]
        text = f"{verb} {obj.get('label') or obj.get('res') or obj.get('type')}"
        if obj["op"] == "move" and obj.get("pos"):
            text += f" to {', '.join(f'{v:.2f}' for v in obj['pos'])}"
        if cols:
            text += f" (+ its collision: {cols} piece{'s' if cols > 1 else ''})"
        if proxies:
            text += " (+ its far view)"
        out.append({"group": g, "text": text, "edits": [x.get("id") for x in items], "op": obj["op"]})
    return out


def distance(a, b) -> float:
    return math.dist(a[:2], b[:2])
