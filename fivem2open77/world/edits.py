"""The edit list of a world edit, checked, merged and compiled to ArchiveXL.

Operations (each with an ``id``):

    delete   a node instance:   {op, sector, index, type, expectedNodes}
             or some of its elements: + {elements: [k...], expectedElements, elementKind: instances|actors}
    move     a node instance:   {op, sector, index, type, expectedNodes, pos?: [x,y,z], rot?: [i,j,k,r],
                                 scale?: [x,y,z]}
             or one element:    + {element: k, expectedElements, elementKind}  (world coordinates)
    replace  its resource:      {op, sector, index, type, expectedNodes, res?: mesh path, app?: appearance}
    add      a new object:      {op: add, res: a .mesh or .ent of the game, app?, pos: [x,y,z], yaw?: degrees,
                                 collision?: mesh|none}

``label``, ``kind``, ``was`` (the original transform/resource) are kept for people and the
Studio's view; ``group`` ties the edits of one action together (an object and the collision that
goes with it), ``role`` says which is which (object | collision).  A node deleted whole cannot also
be moved; element deletions of one node merge; moves and replacements of one node merge into one
mutation (later fields win); an element deleted cannot also be moved.

ArchiveXL (WorldStreaming, 1.27+) applies them as a sector streams in: a deleted static mesh gets
scale 0, other deleted nodes go 2 km down with scale 0, deleted instances get scale 0, deleted
collision actors go 2 km down; mutations set the node instance's transform / resource / appearance,
and an element's transform (instances, actors).  Nothing about a node's collision follows on its
own: ops.py adds the collision edits.
"""
from __future__ import annotations

import math
import re

SECTOR_RX = re.compile(r"^[a-z0-9_\\.\-]+\.streamingsector$", re.I)
TYPE_RX = re.compile(r"^world[A-Za-z0-9]+Node$")
RES_RX = re.compile(r"^[a-z0-9_\\.\-]+\.(mesh|ent)$", re.I)
APP_RX = re.compile(r"^[A-Za-z0-9_\-]{0,64}$")
OPS = ("delete", "move", "replace", "add")
ELEMENT_TYPES = {"worldInstancedMeshNode": "instances", "worldInstancedDestructibleMeshNode": "instances",
                 "worldCollisionNode": "actors"}
LIMIT = 100000.0
MAX_EDITS = 5000


class EditError(ValueError):
    pass


def _num(v, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise EditError(f"{what}: not a number") from None
    if not math.isfinite(f) or abs(f) > LIMIT:
        raise EditError(f"{what}: out of range")
    return f


def _vec(v, n, what):
    if not isinstance(v, (list, tuple)) or len(v) != n:
        raise EditError(f"{what}: {n} numbers")
    return [_num(x, f"{what}[{i}]") for i, x in enumerate(v)]


def _target(e: dict, out: dict):
    sector = str(e.get("sector") or "")
    if not SECTOR_RX.match(sector):
        raise EditError(f"sector {sector!r}: a .streamingsector depot path")
    t = str(e.get("type") or "")
    if not TYPE_RX.match(t):
        raise EditError(f"type {t!r}: a world node type")
    try:
        index, expected = int(e.get("index")), int(e.get("expectedNodes"))
    except (TypeError, ValueError):
        raise EditError("index and expectedNodes: integers") from None
    if expected < 1 or not 0 <= index < expected:
        raise EditError(f"index {index} outside the sector's {expected} node instances")
    out.update(sector=sector.lower(), type=t, index=index, expectedNodes=expected)


def normalize(e: dict) -> dict:
    """One operation, checked; raises EditError."""
    if not isinstance(e, dict):
        raise EditError("an edit is an object")
    op = e.get("op")
    if op not in OPS:
        raise EditError(f"op {op!r}: one of {', '.join(OPS)}")
    out = {"id": str(e.get("id") or "")[:40], "op": op}
    for k in ("label", "kind", "group", "role"):
        if e.get(k) is not None:
            out[k] = str(e[k])[:120]
    if isinstance(e.get("was"), dict):
        out["was"] = {k: v for k, v in e["was"].items() if k in ("pos", "rot", "scale", "res", "app", "yaw")}
    if op == "add":
        res = str(e.get("res") or "")
        if not RES_RX.match(res):
            raise EditError(f"add: res {res!r}: a .mesh or .ent depot path of the game")
        out["res"] = res.lower()
        out["pos"] = _vec(e.get("pos"), 3, "add: pos")
        out["yaw"] = _num(e.get("yaw", 0.0), "add: yaw") % 360.0
        app = str(e.get("app") or "default")
        if not APP_RX.match(app):
            raise EditError(f"add: appearance {app!r}")
        out["app"] = app
        col = str(e.get("collision") or "mesh")
        if col not in ("mesh", "none"):
            raise EditError("add: collision is mesh (the mesh's own shapes) or none")
        out["collision"] = col
        return out
    _target(e, out)
    if op == "delete":
        if e.get("elements") is not None:
            kind = ELEMENT_TYPES.get(out["type"])
            if not kind:
                raise EditError(f"{out['type']} has no elements to delete one by one")
            try:
                expected = int(e.get("expectedElements"))
                els = sorted({int(k) for k in e["elements"]})
            except (TypeError, ValueError):
                raise EditError("elements: integers, and expectedElements") from None
            if expected < 1 or not els or els[0] < 0 or els[-1] >= expected:
                raise EditError(f"elements outside the node's {expected}")
            out.update(elements=els, expectedElements=expected, elementKind=kind)
        return out
    if op == "move":
        if e.get("element") is not None:
            kind = ELEMENT_TYPES.get(out["type"])
            if not kind:
                raise EditError(f"{out['type']} has no elements to move one by one")
            try:
                k, expected = int(e["element"]), int(e.get("expectedElements"))
            except (TypeError, ValueError):
                raise EditError("element and expectedElements: integers") from None
            if expected < 1 or not 0 <= k < expected:
                raise EditError(f"element {k} outside the node's {expected}")
            out.update(element=k, expectedElements=expected, elementKind=kind)
        if e.get("pos") is not None:
            out["pos"] = _vec(e["pos"], 3, "move: pos")
        if e.get("rot") is not None:
            q = _vec(e["rot"], 4, "move: rot")
            n = math.sqrt(sum(v * v for v in q)) or 1.0
            out["rot"] = [v / n for v in q]
        if e.get("scale") is not None:
            sc = _vec(e["scale"], 3, "move: scale")
            if any(v <= 0 or v > 100 for v in sc):
                raise EditError("move: scale between 0 and 100")
            out["scale"] = sc
        if not any(k in out for k in ("pos", "rot", "scale")):
            raise EditError("move: pos, rot or scale")
        return out
    if op == "replace":
        if e.get("res"):
            res = str(e["res"])
            if not RES_RX.match(res) or not res.lower().endswith(".mesh"):
                raise EditError(f"replace: res {res!r}: a .mesh depot path")
            out["res"] = res.lower()
        if e.get("app") is not None:
            app = str(e["app"])
            if not APP_RX.match(app):
                raise EditError(f"replace: appearance {app!r}")
            out["app"] = app
        if "res" not in out and "app" not in out:
            raise EditError("replace: res or app")
        return out
    raise EditError(op)


def normalize_all(edits) -> list:
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise EditError(f"edits: a list (at most {MAX_EDITS})")
    out, errors = [], []
    for i, e in enumerate(edits):
        try:
            n = normalize(e)
            n["id"] = n["id"] or f"e{i + 1}"
            out.append(n)
        except EditError as x:
            errors.append(f"edit {i + 1}: {x}")
    if errors:
        raise EditError("; ".join(errors[:20]))
    plan(out)                    # conflicts
    return out


def plan(edits) -> dict:
    """Per sector: {sector: {expectedNodes, deletions: {index: {...}}, mutations: {index: {...}}}}, and
    the additions.  Raises EditError on conflicts."""
    sectors, adds = {}, []
    for e in edits:
        if e["op"] == "add":
            adds.append(e)
            continue
        s = sectors.setdefault(e["sector"], {"expectedNodes": e["expectedNodes"], "deletions": {}, "mutations": {}})
        if s["expectedNodes"] != e["expectedNodes"]:
            raise EditError(f"{e['sector']}: two different node counts ({s['expectedNodes']}, {e['expectedNodes']}) "
                            "-- the sector was read from two game versions")
        i = e["index"]
        if e["op"] == "delete":
            d = s["deletions"].get(i)
            if d is None:
                d = s["deletions"][i] = {"type": e["type"]}
                if "elements" in e:
                    d.update(elements=set(e["elements"]), expectedElements=e["expectedElements"], elementKind=e["elementKind"])
            elif "elements" in d:
                if "elements" not in e:
                    for k in ("elements", "expectedElements", "elementKind"):
                        d.pop(k, None)                  # the whole node goes
                else:
                    d["elements"] |= set(e["elements"])
        else:
            m = s["mutations"].setdefault(i, {"type": e["type"]})
            if "element" in e:
                if m.get("expectedElements", e["expectedElements"]) != e["expectedElements"]:
                    raise EditError(f"{e['sector']} node {i}: two different element counts")
                m["expectedElements"], m["elementKind"] = e["expectedElements"], e["elementKind"]
                em = m.setdefault("elements", {}).setdefault(e["element"], {})
                for k in ("pos", "rot", "scale"):
                    if k in e:
                        em[k] = e[k]
            else:
                for k in ("pos", "rot", "scale", "res", "app"):
                    if k in e:
                        m[k] = e[k]
    for path, s in sectors.items():
        for i, d in s["deletions"].items():
            m = s["mutations"].get(i)
            if not m:
                continue
            if "elements" not in d:
                raise EditError(f"{path} node {i}: deleted and also moved or replaced")
            both = set(d["elements"]) & set((m.get("elements") or {}).keys())
            if both:
                raise EditError(f"{path} node {i}: element {min(both)} deleted and also moved")
            if m.get("elements") and d["expectedElements"] != m["expectedElements"]:
                raise EditError(f"{path} node {i}: two different element counts")
    return {"sectors": sectors, "adds": adds}


def _q(path: str) -> str:
    # single-quoted YAML scalar: backslashes stay literal, a quote is doubled
    return "'" + str(path).replace("'", "''") + "'"


def _f(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def to_xl(edits, title: str = "", blocks=()) -> str:
    """ArchiveXL ``.xl`` for the edits (and ``streaming: blocks`` for a sector of added objects)."""
    p = plan(edits)
    lines = ["# fivem2open77 world edit" + (f": {title}" if title else ""),
             "# ArchiveXL patches each sector below as it streams in; expectedNodes stops it on a changed game.",
             "streaming:"]
    if blocks:
        lines.append("  blocks:")
        lines += [f"    - {_q(b)}" for b in blocks]
    if p["sectors"]:
        lines.append("  sectors:")
        for path in sorted(p["sectors"]):
            s = p["sectors"][path]
            lines.append(f"    - path: {_q(path)}")
            lines.append(f"      expectedNodes: {s['expectedNodes']}")
            if s["deletions"]:
                lines.append("      nodeDeletions:")
                for i in sorted(s["deletions"]):
                    d = s["deletions"][i]
                    lines.append(f"        - index: {i}")
                    lines.append(f"          type: {d['type']}")
                    if "elements" in d:
                        kind = d["elementKind"]
                        key, exp = ("instanceDeletions", "expectedInstances") if kind == "instances" else ("actorDeletions", "expectedActors")
                        lines.append(f"          {exp}: {d['expectedElements']}")
                        lines.append(f"          {key}: [{', '.join(str(k) for k in sorted(d['elements']))}]")
            if s["mutations"]:
                lines.append("      nodeMutations:")
                for i in sorted(s["mutations"]):
                    m = s["mutations"][i]
                    lines.append(f"        - index: {i}")
                    lines.append(f"          type: {m['type']}")
                    if "pos" in m:
                        lines.append(f"          position: [{', '.join(_f(v) for v in m['pos'])}]")
                    if "rot" in m:
                        lines.append(f"          orientation: [{', '.join(_f(v) for v in m['rot'])}]")
                    if "scale" in m:
                        lines.append(f"          scale: [{', '.join(_f(v) for v in m['scale'])}]")
                    if "res" in m:
                        lines.append(f"          mesh: {_q(m['res'])}")
                    if "app" in m:
                        lines.append(f"          appearance: {m['app'] or 'default'}")
                    if m.get("elements"):
                        key, exp = (("instanceMutations", "expectedInstances") if m["elementKind"] == "instances"
                                    else ("actorMutations", "expectedActors"))
                        lines.append(f"          {exp}: {m['expectedElements']}")
                        lines.append(f"          {key}:")
                        for k in sorted(m["elements"]):
                            em = m["elements"][k]
                            lines.append(f"            - index: {k}")
                            if "pos" in em:     # an element's position takes four numbers
                                lines.append(f"              position: [{', '.join(_f(v) for v in em['pos'])}, 0]")
                            if "rot" in em:
                                lines.append(f"              orientation: [{', '.join(_f(v) for v in em['rot'])}]")
                            if "scale" in em:
                                lines.append(f"              scale: [{', '.join(_f(v) for v in em['scale'])}]")
    if len(lines) == 3:
        lines.append("  sectors: []")
    return "\n".join(lines) + "\n"


def summary(edits) -> dict:
    p = plan(edits)
    dels = sum(1 for s in p["sectors"].values() for d in s["deletions"].values() if "elements" not in d)
    els = sum(len(d["elements"]) for s in p["sectors"].values() for d in s["deletions"].values() if "elements" in d)
    moved = sum(len(m.get("elements") or {}) for s in p["sectors"].values() for m in s["mutations"].values())
    return {"sectors": len(p["sectors"]), "deleted": dels, "elementsDeleted": els,
            "changed": sum(1 for s in p["sectors"].values() for m in s["mutations"].values()
                           if any(k in m for k in ("pos", "rot", "scale", "res", "app"))),
            "elementsMoved": moved, "added": len(p["adds"])}
