"""The agent's tools for world edits: find Night City's own objects around a spot, delete / move /
replace them (their collision follows), add objects of the game, build the edit and install it.

Positions are Night City metres (x east, y north, z up); yaw is degrees counter-clockwise seen from
above (0 faces +y north, 90 faces west).
"""
from __future__ import annotations

from ...world.edits import EditError
from .tools import Tool, ToolError, _obj, text

NUM = {"type": "number"}
INT = {"type": "integer"}
VEC3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
TARGET = {"type": "object", "properties": {"sector": {"type": "string"}, "index": INT, "element": INT},
          "required": ["sector", "index"], "additionalProperties": False}
KINDS = {"type": "array", "items": {"type": "string", "enum": ["mesh", "instanced", "terrain", "collision", "entity",
                                                                "decal", "light", "foliage", "occluder", "effect",
                                                                "spawner", "water", "road", "traffic", "other"]}}


def _world_project(ctx):
    p = ctx.project
    if p.kind != "world":
        raise ToolError("the open project is not a world edit: new_world_edit makes one (or open_project)")
    return p


def _compact_entry(e, items=24):
    out = {"sector": e["sector"], "index": e["i"], "type": e["type"], "kind": e["kind"],
           "name": e["name"] if e["name"] not in ("", "None") else None, "res": e["res"] or None,
           "pos": [round(v, 2) for v in e["pos"]], "yaw": e.get("yaw"), "distance": e.get("distance"),
           "expectedNodes": e["expectedNodes"]}
    if e.get("app") and e["app"] not in ("default", "None"):
        out["app"] = e["app"]
    if e.get("scale") and any(abs(v - 1.0) > 1e-3 for v in e["scale"]):
        out["scale"] = e["scale"]
    el = e.get("elements")
    if el:
        out["elements"] = {"kind": el["kind"], "count": el["count"], "in_area": el.get("inArea", len(el.get("items") or [])),
                           "items": [{"element": it["k"], "pos": [round(v, 2) for v in it["pos"]], "yaw": it.get("yaw")}
                                     for it in (el.get("items") or [])[:items]]}
    return out


def t_world_status(ctx, args):
    st = ctx.studio.world_index_status()
    out = {"world_index": st}
    pid = ctx.session.project
    if pid:
        p = ctx.studio.projects.get(pid)
        if p.kind == "world":
            ws = ctx.studio.world_state(p.id)
            out["project"] = {"id": p.id, "name": ws["name"], "display": ws["display"], "center": ws["center"],
                              "summary": ws["summary"], "problem": ws["problem"], "actions": ws["actions"][-40:],
                              "builds": [{k: b.get(k) for k in ("n", "ok", "version", "error", "summary")} for b in ws["builds"][:3]],
                              "deployments": ws["deployments"][-3:], "local_game": ws["game"]}
    return text(out)


def t_new_world_edit(ctx, args):
    p = ctx.studio.create_world_project(name=args.get("name"), display=args.get("display_name"), center=args.get("center"))
    ctx.session.switch_project(p.id)
    return text({"project": p.id, "name": p.world()["name"], "display": p.world()["display"],
                 "note": "the tools now work on this world edit: world_find, then world_delete / world_move / world_add"})


def t_world_prepare(ctx, args):
    job = ctx.studio.world_prepare(force=bool(args.get("again")))
    ctx.session.attach_job(job)
    return text(ctx.wait_job(job, 1800))


def t_world_find(ctx, args):
    try:
        q = ctx.studio.world_query(args["x"], args["y"], args.get("radius", 30), z=args.get("z"), dz=args.get("dz"),
                                   kinds=args.get("kinds"), text=args.get("text"), limit=int(args.get("limit", 60)))
    except (KeyError, RuntimeError) as e:
        raise ToolError(str(e))
    lim = max(1, min(300, int(args.get("limit", 60))))
    return text({"center": q["center"], "radius": q["radius"], "found": q["total"], "shown": min(lim, q["total"]),
                 "objects": [_compact_entry(e) for e in q["entries"][:lim]],
                 "sectors_read": len(q["sectors"])})


def t_world_object(ctx, args):
    try:
        r = ctx.studio.world_object(args["sector"], int(args["index"]), args.get("element"))
    except KeyError as e:
        raise ToolError(str(e))
    o = r["object"]
    o2 = {k: o.get(k) for k in ("sector", "i", "type", "kind", "name", "res", "app", "pos", "rot", "yaw", "scale",
                                "expectedNodes", "level")}
    if o.get("elements"):
        el = o["elements"]
        o2["elements"] = {"kind": el["kind"], "count": el["count"],
                          "items": [{"element": it.get("k", k), "pos": it["pos"], "yaw": it.get("yaw")}
                                    for k, it in enumerate(el["items"][:60])]}
    return text({"object": o2, "collision": [{k: c.get(k) for k in ("sector", "index", "type", "element", "pos")}
                                             for c in r["collision"]],
                 "shares_its_place_with": [{k: c.get(k) for k in ("sector", "index", "element", "label")} for c in r["sharers"]]})


def _apply(ctx, action):
    p = _world_project(ctx)
    try:
        r = ctx.studio.world_apply(p.id, action)
    except (EditError, ValueError, KeyError, RuntimeError) as e:
        raise ToolError(str(e))
    ctx.log("; ".join(a["text"] for a in r.get("actions") or []) or "no change")
    return text({k: r.get(k) for k in ("dry_run", "group", "actions", "notes", "summary") if r.get(k) is not None})


def t_world_delete(ctx, args):
    targets = args.get("objects") or []
    if not targets:
        raise ToolError("objects: [{sector, index, element?}] from world_find")
    return _apply(ctx, {"op": "delete", "targets": targets, "with_collision": args.get("with_collision", True),
                        "dry_run": bool(args.get("dry_run"))})


def t_world_move(ctx, args):
    if not any(args.get(k) is not None for k in ("pos", "delta", "yaw", "dyaw", "scale")):
        raise ToolError("give pos, delta, yaw, dyaw or scale")
    return _apply(ctx, {"op": "move", "sector": args.get("sector"), "index": args.get("index"),
                        "element": args.get("element"), "pos": args.get("pos"), "delta": args.get("delta"),
                        "yaw": args.get("yaw"), "dyaw": args.get("dyaw"), "scale": args.get("scale"),
                        "with_collision": args.get("with_collision", True)})


def t_world_replace(ctx, args):
    return _apply(ctx, {"op": "replace", "sector": args.get("sector"), "index": args.get("index"),
                        "res": args.get("mesh"), "app": args.get("appearance")})


def t_world_add(ctx, args):
    return _apply(ctx, {"op": "add", "res": args.get("resource"), "pos": args.get("pos"), "yaw": args.get("yaw", 0),
                        "app": args.get("appearance"), "collision": args.get("collision", "mesh"),
                        "label": args.get("label")})


def t_world_clear(ctx, args):
    return _apply(ctx, {"op": "clear_area", "x": args.get("x"), "y": args.get("y"), "radius": args.get("radius", 8),
                        "kinds": args.get("kinds"), "text": args.get("text"), "z": args.get("z"), "dz": args.get("dz"),
                        "with_collision": args.get("with_collision", True), "dry_run": args.get("dry_run", True) is not False})


def t_world_undo(ctx, args):
    p = _world_project(ctx)
    try:
        r = ctx.studio.world_redo(p.id) if args.get("redo") else ctx.studio.world_undo(p.id)
    except ValueError as e:
        raise ToolError(str(e))
    return text(r)


def t_world_drop(ctx, args):
    p = _world_project(ctx)
    try:
        return text(ctx.studio.world_drop(p.id, str(args.get("group") or "")))
    except ValueError as e:
        raise ToolError(str(e))


def t_world_build(ctx, args):
    p = _world_project(ctx)
    try:
        job = ctx.studio.world_build(p.id)
    except (EditError, ValueError) as e:
        raise ToolError(str(e))
    ctx.session.attach_job(job)
    info = ctx.wait_job(job, 1800)
    return text({k: info.get(k) for k in ("n", "ok", "version", "resource", "summary", "bytes", "checks", "seconds")})


def t_world_deploy(ctx, args):
    p = _world_project(ctx)
    sid = args.get("server")
    if not sid:
        raise ToolError("server is required (list_servers)")
    job = ctx.studio.world_deploy(p.id, sid, names=args.get("names"), build=args.get("build"), mode=args.get("mode", "stage"))
    ctx.session.attach_job(job)
    res = ctx.wait_job(job, 1800)
    return text({k: res.get(k) for k in ("names", "mode", "staged", "live", "pending", "started")})


def t_world_remove(ctx, args):
    sid = args.get("server")
    if not sid:
        raise ToolError("server is required (list_servers)")
    resource = args.get("resource")
    if not resource:
        p = _world_project(ctx)
        resource = p.world().get("name")
    job = ctx.studio.world_remove(sid, resource, names=args.get("names"), restart=bool(args.get("restart")))
    ctx.session.attach_job(job)
    res = ctx.wait_job(job, 900)
    return text({k: res.get(k) for k in ("resource", "names", "removed", "pending", "started")})


def t_world_game(ctx, args):
    p = _world_project(ctx)
    job = ctx.studio.world_game(p.id, build=args.get("build"), uninstall=bool(args.get("uninstall")))
    ctx.session.attach_job(job)
    return text(ctx.wait_job(job, 300))


def registry() -> dict:
    tools = [
        Tool("world_status", "Night City's index (read from the game once) and the open world edit: its actions, what they add "
             "up to, builds and installs.", _obj(), "read", t_world_status),
        Tool("new_world_edit", "Start a world edit (a resource that changes Night City itself: objects and ground pieces "
             "deleted, moved, replaced, objects added). name: resource id (a-z0-9_, starts with a letter; default from the "
             "display name). The session switches to it.",
             _obj({"name": {"type": "string"}, "display_name": {"type": "string"},
                   "center": {"type": "array", "items": NUM, "minItems": 2, "maxItems": 3}}), "change", t_new_world_edit),
        Tool("world_prepare", "Read Night City's sector list from the game (once, a minute or two; world_find does it "
             "when needed). again=true reads it again (after a game update).",
             _obj({"again": {"type": "boolean"}}), "read", t_world_prepare),
        Tool("world_find", "Night City's own objects around a point, nearest first: meshes, instanced meshes (each "
             "instance is an `element`), terrain, collision (actors are elements), entities, decals, lights... Each: "
             "sector + index (+ element) to edit it, its resource, place and yaw. kinds filters, text matches name or "
             "resource. The first look at an area reads its sectors from the game (seconds to a minute).",
             _obj({"x": NUM, "y": NUM, "radius": NUM, "z": NUM, "dz": NUM, "kinds": KINDS, "text": {"type": "string"},
                   "limit": INT}, ["x", "y"]), "read", t_world_find),
        Tool("world_object", "One object in full, the collision that belongs to it and other objects at exactly the same "
             "place.", _obj({"sector": {"type": "string"}, "index": INT, "element": INT}, ["sector", "index"]),
             "read", t_world_object),
        Tool("world_delete", "Delete objects (one action; undo takes it back). Their collision goes with them (actors at "
             "the object's place in the collision nodes around, terrain collision under terrain); collision several "
             "objects share goes only when all of them go. dry_run shows what would be deleted.",
             _obj({"objects": {"type": "array", "items": TARGET, "minItems": 1, "maxItems": 2000},
                   "with_collision": {"type": "boolean"}, "dry_run": {"type": "boolean"}}, ["objects"]),
             "change", t_world_delete),
        Tool("world_move", "Move / turn / scale one object (or one element of an instanced mesh): pos (absolute) or delta "
             "[dx,dy,dz]; yaw (heading, degrees) or dyaw (turn by). Its collision moves and turns with it (collision "
             "does not scale).",
             _obj({"sector": {"type": "string"}, "index": INT, "element": INT, "pos": VEC3, "delta": VEC3, "yaw": NUM,
                   "dyaw": NUM, "scale": VEC3, "with_collision": {"type": "boolean"}}, ["sector", "index"]),
             "change", t_world_move),
        Tool("world_replace", "Give an object another mesh of the game and/or another appearance (its collision stays the "
             "original's).", _obj({"sector": {"type": "string"}, "index": INT, "mesh": {"type": "string"},
                                   "appearance": {"type": "string"}}, ["sector", "index"]), "change", t_world_replace),
        Tool("world_add", "Add an object of the game at a place: resource = a .mesh (collision: its own shapes when it has "
             "them, `none` for none) or an .ent (brings its own components and collision); yaw degrees; appearance.",
             _obj({"resource": {"type": "string"}, "pos": VEC3, "yaw": NUM, "appearance": {"type": "string"},
                   "collision": {"type": "string", "enum": ["mesh", "none"]}, "label": {"type": "string"}},
                  ["resource", "pos"]), "change", t_world_add),
        Tool("world_clear_area", "Delete every object of the given kinds (default mesh, instanced, entity, decal) within "
             "radius metres of x, y, with their collision. dry_run (default TRUE) lists them first; call again with "
             "dry_run=false to do it.",
             _obj({"x": NUM, "y": NUM, "radius": NUM, "kinds": KINDS, "text": {"type": "string"}, "z": NUM, "dz": NUM,
                   "with_collision": {"type": "boolean"}, "dry_run": {"type": "boolean"}}, ["x", "y"]),
             "change", t_world_clear),
        Tool("world_undo", "Undo the last action of the world edit (redo=true: redo).",
             _obj({"redo": {"type": "boolean"}}), "change", t_world_undo),
        Tool("world_drop_action", "Take one action (its group id from world_status) out of the edit.",
             _obj({"group": {"type": "string"}}, ["group"]), "change", t_world_drop),
        Tool("build_world_edit", "Build the world edit into its resource (an ArchiveXL package; WolvenKit only when objects "
             "are added). Checks every sector still matches this game.", _obj(), "change", t_world_build),
        Tool("install_world_edit_in_game", "Write (or uninstall=true: remove) the latest build's package in the local "
             "game's archive/pc/mod for a direct test boot.",
             _obj({"build": INT, "uninstall": {"type": "boolean"}}), "change", t_world_game),
        Tool("deploy_world_edit", "Install the latest (or given) build on a server. mode 'stage' (default): the car hub "
             "restarts the server when nobody is online; 'restart': now if nobody is online; 'files': install only. "
             "Players get the change at their next launch after the restart.",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}}, "build": INT,
                   "mode": {"type": "string", "enum": ["stage", "restart", "files"]}}, ["server"]), "deploy", t_world_deploy),
        Tool("remove_world_edit", "Take a world edit off a server (its resource and its resources.load entry; the server "
             "keeps a backup). resource: the edit's name (default: the open world edit's). restart=true restarts now "
             "when nobody is online; otherwise players keep the change until the server's next start.",
             _obj({"server": {"type": "string"}, "resource": {"type": "string"},
                   "names": {"type": "array", "items": {"type": "string"}}, "restart": {"type": "boolean"}}, ["server"]),
             "deploy", t_world_remove),
    ]
    return {t.name: t for t in tools}


WORLD_TOOLS = ("world_status", "new_world_edit", "world_prepare", "world_find", "world_object", "world_delete",
               "world_move", "world_replace", "world_add", "world_clear_area", "world_undo", "world_drop_action",
               "build_world_edit", "install_world_edit_in_game", "deploy_world_edit", "remove_world_edit")
