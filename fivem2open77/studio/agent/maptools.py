"""The agent's tools for maps, servers and the Studio itself (projects, imports).

Map tools work on the session's map project by default (its map name, the server it was last
installed on); every live tool can name another server, server folder or map.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time

from .. import ncweassets
from ..maplink import LinkError
from ..maps import MapOptionsError
from .tools import Tool, ToolError, _obj, text

SERVER = {"type": "string", "description": "server id from list_servers (default: where this map was installed)"}
FOLDER = {"type": "string", "description": "server folder (default: the server's first, or where this map was installed)"}
MAPN = {"type": "string", "description": "map resource id on the server (default: this project's map)"}
VIEWS = ["plan", "iso", "iso_back", "north", "south", "east", "west"]


def _map_project(ctx):
    p = ctx.project
    if p.kind != "map":
        raise ToolError("the open project is a car; use open_project to switch to a map")
    return p


def _target(ctx, args, need_map=True):
    """(server id, folder, map name) from the arguments, the project's last deployment, the defaults."""
    sid, folder, name = args.get("server"), args.get("name"), args.get("map")
    pid = ctx.session.project
    p = ctx.studio.projects.get(pid) if pid else None
    if p is not None and p.kind == "map":
        dep = (p.data.get("deployments") or [None])[-1]
        if dep and not sid:
            sid = dep.get("server")
            if not folder and dep.get("names"):
                folder = dep["names"][0]
        if not name:
            name = (p.data.get("map") or {}).get("name")
    servers = ctx.studio.settings.get()["servers"]
    if not sid:
        sid = ctx.studio.settings.get()["deploy"].get("default_server") or (servers[0]["id"] if len(servers) == 1 else None)
    if not sid:
        raise ToolError("which server? (list_servers)")
    server = ctx.studio.settings.server(sid)
    if not server:
        raise ToolError(f"no server '{sid}' (list_servers)")
    if not folder:
        folder = server["names"][0]
    if need_map and not name:
        raise ToolError("which map? (live_maps lists the maps on the server)")
    return sid, folder, name


def _compact_look(look):
    if not look:
        return None
    if look.get("city"):
        ov = look.get("overview") or {}
        return {"city": True, "map": look["map"], "display_name": look["display_name"], "objects": ov.get("objects"),
                "ymaps": ov.get("ymaps"), "extent_gta": ov.get("extent"),
                "districts": [{"name": d["name"], "objects": d["entities"], "center": d["center"],
                               "radius": min(d["radius"], 400.0)} for d in look["districts"][:30]],
                "next": "a whole city is converted a district at a time: pick_district with a district's center and "
                        "radius (or set_map_options area=[x, y, radius] plus a name), then the look runs for it"}
    return {"map": look["map"], "display_name": look["display_name"], "size_m": look["size"],
            "visual_bounds_local": look["visual_bounds"], "spawn_local": look.get("spawn_local"),
            "anchor_gta": look["anchor_gta"], "fivem_spawn": look.get("coords"),
            "interiors": [{"name": m["name"], "rooms": m.get("rooms"), "entity_sets": m.get("entity_sets")}
                          for m in look["scene"]["mlo_instances"]],
            "entities": look["scene"].get("world_entities"), "vanilla_gta_entities_left_out": look["scene"].get("missing_total"),
            "parts": [{k: x[k] for k in ("name", "kind", "triangles", "colliders", "instances")} for x in look["parts"][:25]],
            "parts_total": len(look["parts"]), "placements": look["placements"], "triangles_placed": look["triangles_placed"],
            "colliders": look["colliders"], "doors": [{"part": d["part"], "opening": d["opening"], "devices": d["devices"]}
                                                     for d in look.get("doors") or []],
            "glass_slabs": look.get("glass"), "entity_sets": look.get("entity_sets"), "warnings": look["warnings"][:30],
            "doorways": [{k: w.get(k) for k in ("key", "rooms", "outside", "kind", "width", "height", "status", "passage",
                                                "blockers", "doors", "mode")} for w in (look.get("doorways") or [])[:60]],
            "door_candidates": (look.get("door_candidates") or [])[:30], "door_map": look.get("door_map"),
            "object_names": sorted({s for x in look["parts"] for s in (x.get("sources") or {})})[:80]}


# ----------------------------------------------------------------------------- studio
def t_list_projects(ctx, args):
    kind = args.get("kind")
    out = []
    for p in ctx.studio.projects.list():
        if kind and p.get("kind") != kind:
            continue
        lb = p.get("last_build") or {}
        out.append({"id": p["id"], "kind": p.get("kind"), "name": p["name"],
                    "car": (p.get("car") or {}).get("record"), "map": (p.get("map") or {}).get("name"),
                    "world": (p.get("world") or {}).get("name"),
                    "last_build": {"n": lb.get("n"), "ok": lb.get("ok")} if lb else None,
                    "deployed_to": sorted({d.get("server") for d in p.get("deployments") or []})})
    return text(out or "no projects yet")


def t_open_project(ctx, args):
    want = str(args.get("project") or "").strip()
    match = None
    for p in ctx.studio.projects.list():
        names = {p["id"], (p.get("name") or "").lower(), ((p.get("map") or {}).get("name") or "").lower(),
                 ((p.get("car") or {}).get("name") or "").lower(), ((p.get("world") or {}).get("name") or "").lower()}
        if want in names or want.lower() in names:
            match = p
            break
    if not match:
        raise ToolError(f"no project '{want}' (list_projects)")
    ctx.session.switch_project(match["id"])
    return text({"opened": match["id"], "kind": match.get("kind"), "name": match["name"],
                 "note": "the tools now work on this project"})


def t_import(ctx, args):
    path = str(args.get("path") or "").strip()
    kind = args.get("kind")
    if kind not in ("car", "map"):
        raise ToolError("kind: car or map")
    if not path or not os.path.exists(path):
        raise ToolError(f"not found on this PC: {path}")
    p, job = ctx.studio.create_project(path=path, kind=kind)
    ctx.session.attach_job(job)
    ctx.session.switch_project(p.id)
    try:
        ctx.wait_job(job, 3600)                     # the files: copied, extracted or linked
    except ToolError as e:
        return text({"project": p.id, "kind": kind, "imported": False, "error": str(e)})
    nxt = ctx.studio.jobs.get((job.result or {}).get("next_job") or "")
    if nxt is not None:                             # then the first look (map) or inspection (car)
        ctx.session.attach_job(nxt)
        try:
            ctx.wait_job(nxt, 3600)
        except ToolError as e:
            return text({"project": p.id, "kind": kind, "imported": True, "first_look": f"failed: {e}"})
    p = ctx.studio.projects.get(p.id)
    out = {"project": p.id, "kind": kind, "name": p.data.get("name"), "imported": True,
           "how": (job.result or {}).get("how"), "next": "get_map" if kind == "map" else "inspect_car"}
    if kind == "map" and (p.data.get("map") or {}).get("city"):
        out["city"] = True
        out["next"] = "a whole city: get_map lists its districts; pick_district converts one"
    if kind == "car" and len(p.data.get("models") or []) > 1:
        out["models"] = p.data["models"]
        out["note"] = "this resource holds several cars: update_profile model=<one of models> picks the one converted"
    return text(out)


def t_import_pack(ctx, args):
    """A car pack (one download, many cars): imported once, then a project per car picked."""
    path = str(args.get("path") or "").strip()
    lid = str(args.get("pack") or "").strip()
    models = args.get("models")
    if args.get("all"):
        models = "all"
    if not lid:
        if not path or not os.path.exists(path):
            raise ToolError(f"path: a car pack (folder or .zip/.rar/.7z) on this PC, or pack: the id of one imported before")
        e, job = ctx.studio.create_pack(path=path)
        ctx.session.attach_job(job)
        ctx.wait_job(job, 3600)
        lid = e["id"]
    pack = ctx.studio.pack(lid)
    out = {"pack": lid, "name": pack["name"],
           "cars": [{"model": c["model"], "name": c["display"], "class": c["class"], "project": (c["projects"] or [None])[0]}
                    for c in pack["cars"]]}
    if not models:
        out["next"] = "import_car_pack pack=<id> with models=[...] (or all=true) makes a project per car"
        return text(out)
    made, job = ctx.studio.import_pack_cars(lid, models)
    if job is not None:
        ctx.session.attach_job(job)
        ctx.wait_job(job, 7200)
        out["read"] = job.result
    out["projects"] = [{"project": q.id, "model": q.raw_profile().get("model"), "name": ctx.studio.projects.get(q.id).data.get("name")}
                       for q in made]
    if len(made) == 1:
        ctx.session.switch_project(made[0].id)
        out["note"] = "the session now works on that car"
    else:
        out["note"] = "open_project switches to one of them; the Cars page builds or installs the whole pack"
    return text(out)


# ----------------------------------------------------------------------------- map project
def t_get_map(ctx, args):
    p = _map_project(ctx)
    st = ctx.studio.map_state(p.id)
    return text({"project": p.id, "name": p.data.get("name"), "options": st["options"], "normalized": st["normalized"],
                 "option_errors": st["option_errors"], "look": _compact_look(st["look"]) or "not looked at yet: look_at_map",
                 "builds": [{k: b.get(k) for k in ("n", "ok", "version", "baked", "coverage", "qa", "error", "seconds")}
                            for b in st["builds"][:5]],
                 "build_stale": st.get("build_stale") or "the last build matches the project's NCWE imports",
                 "deployments": st["deployments"][-5:], "local_game": st["game"]})


def t_pick_district(ctx, args):
    p = _map_project(ctx)
    st = ctx.studio.map_state(p.id)
    look = st.get("look") or {}
    area = args.get("area")
    district = args.get("district")
    if district and not area:
        city = look if look.get("city") else None
        if city is None:
            raise ToolError("this map has no city overview with districts: look_at_map first, or give an area")
        d = next((x for x in city["districts"] if x["name"].lower() == str(district).lower()), None)
        if d is None:
            raise ToolError(f"no district '{district}' (get_map lists them)")
        area = [d["center"][0], d["center"][1], min(d["radius"], 400.0)]
    if not area:
        raise ToolError("area [x, y, radius] (GTA metres) or district (a name from get_map)")
    q, job = ctx.studio.map_district(p.id, area, name=args.get("name"), display_name=args.get("display_name"),
                                     district=district, new_project=bool(args.get("new_project")))
    ctx.session.attach_job(job)
    if q.id != p.id:
        ctx.session.switch_project(q.id)
    ctx.wait_job(job, 1800)
    out = _compact_look(ctx.studio.map_state(q.id)["look"]) or {}
    out["project"] = q.id
    return text(out)


def t_look(ctx, args):
    p = _map_project(ctx)
    job = ctx.studio.map_look(p.id, force=bool(args.get("again")))
    ctx.session.attach_job(job)
    ctx.wait_job(job, 1800)
    return text(_compact_look(ctx.studio.map_state(p.id)["look"]))


def t_render_map(ctx, args):
    p = _map_project(ctx)
    if not ctx.session.vision:
        return text("this model cannot see images; use get_map's look instead")
    mode = args.get("mode", "material")
    views = args.get("views") or ["iso", "plan"]
    blocks = [{"type": "text", "text": f"renders ({mode}; parts colours: interiors beige, objects green, cells grey-green, "
                                       "doors blue, glass light blue; plan: north up, a line every 10 m, bold every 50 m, the white "
                                       "cross is the anchor)"}]
    for v in views[:4]:
        if v not in VIEWS:
            raise ToolError(f"view: one of {', '.join(VIEWS)}")
        png = ctx.studio.map_render(p.id, v, mode, (960, 600) if v != "plan" else None)
        blocks.append({"type": "text", "text": f"view {v}:"})
        blocks.append({"type": "image", "png": base64.b64encode(png).decode("ascii")})
    return blocks


def t_set_map_options(ctx, args):
    p = _map_project(ctx)
    patch = args.get("patch")
    if not isinstance(patch, dict):
        raise ToolError("patch must be an object")
    try:
        r = ctx.studio.map_set_options(p.id, patch)
    except MapOptionsError as e:
        raise ToolError("invalid options: " + "; ".join(e.errors))
    out = {"saved": True, "normalized": r["normalized"]}
    if r["look_job"]:
        job = ctx.studio.jobs.get(r["look_job"]["id"])
        ctx.session.attach_job(job)
        ctx.wait_job(job, 1800)
        out["look"] = "looked at again with the new options"
    ctx.log("map options updated")
    return text(out)


def t_build_map(ctx, args):
    p = _map_project(ctx)
    job = ctx.studio.build(p.id)
    ctx.session.attach_job(job)
    info = ctx.wait_job(job, 5400)
    return text({k: info.get(k) for k in ("n", "ok", "version", "name", "packages", "baked", "coverage", "doors", "glass",
                                         "qa", "warnings", "seconds")})


def t_build_report(ctx, args):
    p = _map_project(ctx)
    n = args.get("build") or (p.latest_build(ok_only=False) or {}).get("n")
    if not n:
        raise ToolError("no build yet")
    info = ctx.studio.build_info(p.id, n)
    rep = info.get("report") or {}
    bd = rep.get("build") or {}
    cov = bd.get("coverage") or {}
    q = info.get("qa") or {}
    return text({"build": {k: info["build"].get(k) for k in ("n", "ok", "version", "baked", "error", "seconds")},
                 "coverage": {"collision": {k: (cov.get("collision") or {}).get(k) for k in ("colliders", "walkable", "walls")},
                              "holes_map_local": ((cov.get("collision") or {}).get("holes") or [])[:10],
                              "visual_floors": cov.get("visual_floors")} if cov else "coverage check was off",
                 "doors": bd.get("doors"), "glass": bd.get("glass"), "packages": rep.get("packages"),
                 "qa": {"ok": q.get("ok"), "failed": [c["message"] for c in q.get("checks", []) if not c["ok"]]},
                 "warnings": rep.get("warnings")})


def t_game(ctx, args):
    p = _map_project(ctx)
    job = ctx.studio.map_game(p.id, build=args.get("build"), uninstall=bool(args.get("uninstall")))
    ctx.session.attach_job(job)
    r = ctx.wait_job(job, 900)
    return text({"written": len(r["written"]), "unchanged": len(r["unchanged"]), "removed": len(r["removed"]), "dir": r["modDir"],
                 "spot": r.get("spot"), "imports": r.get("extra") or []})


def t_deploy_map(ctx, args):
    p = _map_project(ctx)
    sid = args.get("server")
    if not sid:
        raise ToolError("server is required (list_servers)")
    kw = dict(names=args.get("names"), keep_placement=args.get("keep_placement", True) is not False,
              restart=args.get("restart", True) is not False, baker=args.get("baker", True) is not False)
    if args.get("rebuild"):
        job = ctx.studio.map_rebuild_deploy(p.id, sid, **kw)   # build first: ships changed NCWE imports
        ctx.session.attach_job(job)
        out = ctx.wait_job(job, 5400) or {}
        return text({"build": out.get("build"), "deployed": out.get("deployed")})
    job = ctx.studio.deploy(p.id, sid, build=args.get("build"), **kw)
    ctx.session.attach_job(job)
    res = ctx.wait_job(job, 3600)
    return text({"servers": [{"name": x["name"], "exit": x["code"], "result": x["result"],
                              "last_lines": [l for l in x["lines"] if l.strip()][-12:]} for x in res["servers"]],
                 "baker_upload": res["toolkit"],
                 **({"not_placed_on": res["unplaced"], "note": "players see nothing of it there until it has a spot: "
                     "set_map_spot / place it in game, or set the place option and deploy again"}
                    if res.get("unplaced") else {})})


# ----------------------------------------------------------------------------- live
def _short_map(m):
    keys = ("name", "display", "running", "runningId", "pending", "previous", "moved", "apply", "presets", "runningSpawn")
    out = {k: m.get(k) for k in keys if k in m}
    if m.get("history"):
        out["history"] = m["history"][:5]
    if isinstance(m.get("bake"), dict):
        out["bake"] = {k: m["bake"].get(k) for k in ("state", "message", "id") if k in m["bake"]}
    return out


def t_live_maps(ctx, args):
    sid, folder, _ = _target(ctx, args, need_map=False)
    r = ctx.studio.live_status(sid, folder)
    if not r["ok"]:
        return text({"server": sid, "folder": folder, "ok": False, "error": r.get("error"), "link": r.get("link")})
    st = r["status"]
    return text({"server": sid, "folder": folder, "maps": [_short_map(m) for m in st.get("maps", [])],
                 "baker": st.get("baker"), "restart": st.get("restart"), "manager": r["link"].get("manager_version")})


def t_live_map(ctx, args):
    sid, folder, name = _target(ctx, args)
    try:
        return text(_short_map(ctx.studio.live_map(sid, folder, name)) | {"server": sid, "folder": folder})
    except LinkError as e:
        raise ToolError(str(e))


def _op(ctx, args, op, body):
    sid, folder, name = _target(ctx, args)
    try:
        r = ctx.studio.live_op(sid, folder, name, op, body)
    except (LinkError, ValueError) as e:
        raise ToolError(str(e))
    out = {"ok": r.get("ok"), "reason": r.get("reason"), "result": r.get("result")}
    if isinstance(r.get("map"), dict):
        out["map"] = _short_map(r["map"])
    if not r.get("ok"):
        raise ToolError(f"{op} refused: {r.get('reason')}")
    return text(out)


def t_set_spot(ctx, args):
    action = args.get("action")
    body = {k: args[k] for k in ("x", "y", "z", "yaw", "dx", "dy", "dz", "dyaw", "frame", "playerId") if k in args}
    if action not in ("place", "move", "here", "drop", "discard", "revert"):
        raise ToolError("action: place, move, here, drop, discard or revert")
    return _op(ctx, args, action, body)


def t_ground(ctx, args):
    return _op(ctx, args, "ground", {k: args[k] for k in ("x", "y", "playerId") if k in args})


def t_preset(ctx, args):
    op = {"save": "presetSave", "use": "presetUse", "delete": "presetDelete"}.get(args.get("action"))
    if not op:
        raise ToolError("action: save, use or delete")
    return _op(ctx, args, op, {"name": args.get("preset")})


def t_apply(ctx, args):
    body = {"mode": args.get("mode", "now")}
    if body["mode"] == "now":
        body["countdown"] = int(args.get("countdown", 60))
    return _op(ctx, args, "apply", body)


def t_cancel(ctx, args):
    return _op(ctx, args, "cancel", {})


def t_goto(ctx, args):
    return _op(ctx, args, "goto", {"playerId": args.get("playerId"), "which": args.get("which", "running")})


def t_wait(ctx, args):
    sid, folder, name = _target(ctx, args)
    job = ctx.studio.live_wait(sid, folder, name, until=args.get("until", "applied"),
                               timeout=min(3600, max(30, int(args.get("timeout", 900)))))
    ctx.session.attach_job(job)
    m = ctx.wait_job(job, 3700)
    return text(_short_map(m))


def t_players(ctx, args):
    sid, folder, _ = _target(ctx, args, need_map=False)
    try:
        r = ctx.studio.live_players(sid, folder)
    except LinkError as e:
        raise ToolError(str(e))
    return text([{k: p.get(k) for k in ("playerId", "name", "position")} for p in r.get("players", [])] or "nobody online")


def t_agent_api(ctx, args):
    sid = args.get("server")
    if not sid:
        raise ToolError("server is required")
    job = ctx.studio.server_job(sid, "agent-api", names=args.get("names"), enable=args.get("enable", True) is not False,
                                restart=bool(args.get("restart")))
    ctx.session.attach_job(job)
    ctx.wait_job(job, 900)
    return text({"done": True, "lines": [l["text"] for l in job.lines if "AGENT API" in l["text"] or "PENDING" in l["text"]
                                         or "restart" in l["text"]][-6:]})


PLACE_PROPS = {"server": SERVER, "name": FOLDER, "map": MAPN}


# ------------------------------------------------------------------ NCWE asset imports
def t_ncwe_assets_list(ctx, args):
    p = _map_project(ctx)
    items = ncweassets.list_imports(p)
    return text({"imports": items, "total_bytes": sum(i["bytes"] for i in items),
                 "collisions": ncweassets.collisions(p),
                 "note": "imports that carry game archives are repacked into preload packages and ship with the map at "
                         "build; collisions are archive name clashes to fix before building, "
                         "ncwe_assets_verify says what will be packed and rechecks them"})


def t_ncwe_assets_import(ctx, args):
    p = _map_project(ctx)
    src = os.path.expanduser(str(args.get("path") or "").strip())
    if not src or not os.path.exists(src):
        raise ToolError(f"no such file or folder: {src or '(path)'}")
    try:
        info = ncweassets.import_path(p, src, str(args.get("name") or "").strip())
    except ncweassets.AssetError as e:
        raise ToolError(str(e)) from None
    ctx.log(f"NCWE asset imported: {info['name']}")
    return text({"imported": info, "collisions": ncweassets.collisions(p),
                 "note": "ships with the map at build when it carries game archives; ncwe_assets_verify checks it"})


def t_ncwe_assets_verify(ctx, args):
    p = _map_project(ctx)
    staging = tempfile.mkdtemp(prefix="f2o77-ncwe-verify-")
    try:
        packages, warnings = ncweassets.prepare(p.path("assets", "ncwe"), staging, log=lambda m: None)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return text({"packages": [{"source": q["source"], "archives": q["archives"]} for q in packages],
                 "preload_packages": len(packages), "warnings": warnings,
                 "collisions": ncweassets.collisions(p),
                 "note": "Open77 allows 8 preload packages per resource (the map's own included); the writer enforces "
                         "the total at build; collisions and warnings are archive name clashes to fix before building"})


def t_ncwe_import(ctx, args):
    p = _map_project(ctx)
    pos = args.get("position")
    if pos is not None:
        if not isinstance(pos, list) or len(pos) not in (2, 3):
            raise ToolError("position is [x, y] (drop on the surface below) or [x, y, z] metres")
        pos = [float(v) for v in pos]
    job = ctx.studio.ncwe_import(p.id, name=str(args.get("name") or "").strip() or None,
                                 position=pos, yaw=float(args.get("yaw") or 0.0),
                                 place=None if args.get("place") is None else bool(args.get("place")),
                                 height=args.get("height"), analyze=bool(args.get("analyze_only")))
    ctx.session.attach_job(job)
    r = ctx.wait_job(job, 1800)
    return text({"map": r.get("map"), "name": r.get("name"), "asset": r.get("asset"),
                 "placed": r.get("placed"), "position": r.get("position"), "note": r.get("note"),
                 "follow_up": "place or move it with ncwe_place_objects / ncwe_transform_objects; "
                              "ncwe_custom_meshes tweaks its materials"})


def registry() -> dict:
    tools = [
        # ------------------------------------------------------------ studio
        Tool("list_projects", "The Studio's projects: cars, maps and world edits, with their last build and the servers they "
             "were installed on.", _obj({"kind": {"type": "string", "enum": ["car", "map", "world"]}}), "read", t_list_projects),
        Tool("open_project", "Switch this session to another project (by id, project name, car or map name); the tools then work on it.",
             _obj({"project": {"type": "string"}}, ["project"]), "read", t_open_project),
        Tool("import_resource", "Import a FiveM resource from a folder or a .zip/.rar/.7z on this PC as a new car or map project and "
             "look at it (a map takes about a minute). The session switches to the new project.",
             _obj({"path": {"type": "string"}, "kind": {"type": "string", "enum": ["car", "map"]}}, ["path", "kind"]), "change", t_import),
        Tool("import_car_pack", "Import a car pack (one folder or .zip/.rar/.7z with many FiveM cars, like a 'civ car pack') once, "
             "list its cars, and make a project per car: models=[...] picks them, all=true takes every car. Without models "
             "it only lists them; pack= reuses a pack imported before.",
             _obj({"path": {"type": "string"}, "pack": {"type": "string"}, "models": {"type": "array", "items": {"type": "string"}},
                   "all": {"type": "boolean"}}), "change", t_import_pack),
        Tool("enable_agent_api", "Turn the map manager's agent API (the server's HTTP handler listener, loopback only) on or off on a "
             "server's folders. It takes effect at the next server start; restart=true restarts now when nobody is online.",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}},
                   "enable": {"type": "boolean"}, "restart": {"type": "boolean"}}, ["server"]), "deploy", t_agent_api),
        # ------------------------------------------------------------ map project
        Tool("pick_district", "A whole city (Liberty V) is converted a district at a time: pick one by name (district, from "
             "get_map) or by area [x, y, radius] in GTA metres, in this project or (new_project=true) as a new project "
             "reading the same files. The look runs for that district.",
             _obj({"district": {"type": "string"}, "area": {"type": "array", "items": {"type": "number"}},
                   "name": {"type": "string"}, "display_name": {"type": "string"}, "new_project": {"type": "boolean"}}),
             "change", t_pick_district),
        Tool("get_map", "The open map project: options (the spot it is baked at, names, what is included), the look (size, "
             "interiors, parts, doors, glass, the converter's warnings, the map's object names), builds and installs.",
             _obj(), "read", t_get_map),
        Tool("look_at_map", "Read the map again with the current options (scene, parts, previews). again=true redoes it even "
             "when a look is cached.", _obj({"again": {"type": "boolean"}}), "read", t_look),
        Tool("render_map", "Renders of the map as it will be built: plan (from above, north up, 10 m grid), iso / iso_back "
             "(3/4 views), north/south/east/west (side views from that side). mode 'parts' colours each kind.",
             _obj({"views": {"type": "array", "items": {"type": "string", "enum": VIEWS}, "maxItems": 4},
                   "mode": {"type": "string", "enum": ["material", "parts"]}}), "read", t_render_map),
        Tool("set_map_options", "Change the map's conversion options with a JSON merge patch (null = back to the default). Keys: "
             "name (resource id, a-z0-9_, keep it once installed), display_name, place {x,y,z,yaw} (Night City metres, yaw "
             "degrees counter-clockwise; the spot builds are baked at, null = not baked), entity_sets (default|all|none|[names]), "
             "exclude [archetype names], collision (bool), coverage (bool), doors (devices|static), solid_doors, open_gates, "
             "quiet_interiors (off|bottom|all: ambient-noise suppression volumes over the MLO floors -- bottom quiets the "
             "lower floor of every MLO so Night City's ambience cannot be heard down there, all every floor), "
             "cell (16-256), ground_spacing (0.5-8), collision_budget, instance_min_tris, coords [x,y,z] (FiveM spawn), anchor "
             "[x,y,z] (GTA point), door_map {objects: {<archetype name>: auto|hinged|sliding|rolling|open|solid|remove}, "
             "doorways: {<mlo>#<portal>: auto|open|door-hinged|door-double|door-sliding|door-rolling}} (the Doors tab: get_map "
             "lists the doorways with their status -- open, door, blocked, closed (in GTA too), window -- what blocks them "
             "and door-like objects; a whole door_map replaces the old one). A change to what is built looks at the map again.",
             _obj({"patch": {"type": "object"}}, ["patch"]), "change", t_set_map_options),
        Tool("build_map", "Build the map with WolvenKit (a few minutes for a big MLO) and check it. Returns packages, the spot it "
             "was baked at, collision coverage, doors, glass and QA.", _obj(), "change", t_build_map),
        Tool("map_build_report", "A build's report: collision coverage (walkable, walls, visible floors), the biggest gaps in "
             "map-local metres, doors, glass, packages, QA checks, warnings.",
             _obj({"build": {"type": "integer"}}), "read", t_build_report),
        Tool("install_map_in_game", "Write (or uninstall=true: remove) a build's packages in the local game's archive/pc/mod for "
             "a direct test boot. Only this map's files are touched. The map stands at the spot in its options (its sector "
             "is baked for that spot here when the build was baked elsewhere or not at all), else where the build was "
             "baked; with neither it is refused (set_map_options place first). Returns the spot it stands at.",
             _obj({"build": {"type": "integer"}, "uninstall": {"type": "boolean"}}), "change", t_game),
        Tool("deploy_map", "Install the latest (or given) map build on a server with the map manager and the map baker. "
             "keep_placement (default true): a map already there is rebuilt at the spot it was moved to. restart (default "
             "true): restart when nobody is online. baker (default true): install/update the map baker. rebuild (default "
             "false): build the map first (so changed NCWE imports ship) and deploy that fresh build; the build argument is "
             "then ignored.",
             _obj({"server": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}},
                   "build": {"type": "integer"}, "keep_placement": {"type": "boolean"}, "restart": {"type": "boolean"},
                   "baker": {"type": "boolean"}, "rebuild": {"type": "boolean"}}, ["server"]), "deploy", t_deploy_map),
        Tool("ncwe_assets_list", "NCWE asset imports in this map project (props, lights and meshes exported from the NCWE "
             "world editor and shipped with the map): what is imported, the archives it ships and any archive name "
             "collisions to fix before building.",
             _obj(), "read", t_ncwe_assets_list),
        Tool("ncwe_assets_import", "Import an NCWE export into the map project from this PC: a .zip/.7z/.rar ArchiveXL mod, "
             "a game archive, or a folder of them (stored under the project's assets/ncwe/; name defaults to the file's or "
             "folder's own). Imports carrying game archives are repacked and shipped with the map at build.",
             _obj({"path": {"type": "string"}, "name": {"type": "string"}}, ["path"]), "change", t_ncwe_assets_import),
        Tool("ncwe_assets_verify", "Dry-run the build's asset packaging without building: which imports become preload "
             "packages (and which game archives they carry), what is skipped and why, any archive name collisions to fix "
             "before building, and how many of Open77's 8 preload packages the map would use.", _obj(), "read", t_ncwe_assets_verify),
        Tool("ncwe_import_map", "Import this converted map/MLO into the user's NCWE world editor: the map is exported as "
             "a glb and imported into NCWE's custom mesh library (import_model), then placed in the NCWE world at the "
             "map's Night City spot when one is set (or at position [x, y, z]; [x, y] drops it on the surface below). "
             "Needs the user's NCWE editor open and signed in (Settings > Agents > NCWE MCP executable). analyze_only "
             "reports what would be imported without importing. Returns NCWE's mesh asset and the placed object ids.",
             _obj({"name": {"type": "string"},
                   "position": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                   "yaw": {"type": "number"}, "place": {"type": "boolean"}, "height": {"type": "number"},
                   "analyze_only": {"type": "boolean"}}), "deploy", t_ncwe_import),
        # ------------------------------------------------------------ live (the map manager on a server)
        Tool("live_maps", "The maps on a server as its map manager has them: where each stands (running), the new spot "
             "(pending), the previous spot, presets, a move in progress (apply.state), the baker and a scheduled restart.",
             _obj({"server": SERVER, "name": FOLDER}), "read", t_live_maps),
        Tool("live_map", "One map on a server in detail (spots, presets, apply, history).", _obj(PLACE_PROPS), "read", t_live_map),
        Tool("list_players", "Players online on a server with their positions (for here/drop/goto).",
             _obj({"server": SERVER, "name": FOLDER}), "read", t_players),
        Tool("ground_height", "Height of the static ground under x, y (default: the new spot), measured by a player within "
             "150 m of the point (the server has no physics).",
             _obj(dict(PLACE_PROPS, x={"type": "number"}, y={"type": "number"}, playerId={"type": "integer"})), "read", t_ground),
        Tool("set_map_spot", "Set the new spot of a map on a server (nothing moves in game until apply_map_move). action: place "
             "{x,y,z,yaw} absolute; move {dx,dy,dz,dyaw, frame world|map} relative; here {playerId, yaw?, dz?} the player's "
             "feet; drop {x?,y?,dz?} onto the ground (needs a player nearby); discard (forget the new spot); revert (the "
             "previous spot becomes the new spot).",
             _obj(dict(PLACE_PROPS, action={"type": "string", "enum": ["place", "move", "here", "drop", "discard", "revert"]},
                       x={"type": "number"}, y={"type": "number"}, z={"type": "number"}, yaw={"type": "number"},
                       dx={"type": "number"}, dy={"type": "number"}, dz={"type": "number"}, dyaw={"type": "number"},
                       frame={"type": "string", "enum": ["world", "map"]}, playerId={"type": "integer"}), ["action"]),
             "change", t_set_spot),
        Tool("map_preset", "Saved spots of a map: save (the new spot, or where it stands), use (it becomes the new spot), delete.",
             _obj(dict(PLACE_PROPS, action={"type": "string", "enum": ["save", "use", "delete"]}, preset={"type": "string"}),
                  ["action", "preset"]), "change", t_preset),
        Tool("apply_map_move", "Apply the new spot: the server's map baker builds the map there, then the server restarts "
             "(mode now: after countdown seconds, 5-3600, players are warned and rejoin; mode empty: when nobody is online). "
             "Ask the user first.",
             _obj(dict(PLACE_PROPS, mode={"type": "string", "enum": ["now", "empty"]}, countdown={"type": "integer"})),
             "deploy", t_apply),
        Tool("cancel_map_move", "Call a move off before the restart (the build is thrown away).", _obj(PLACE_PROPS), "deploy", t_cancel),
        Tool("wait_map_move", "Wait until a move is built (staged), applied (live after the restart) or settled; the restart "
             "in the middle is waited out.",
             _obj(dict(PLACE_PROPS, until={"type": "string", "enum": ["built", "applied", "settled"]},
                       timeout={"type": "integer"})), "read", t_wait),
        Tool("send_player_to_map", "Teleport a player to a map (which: running or pending spot).",
             _obj(dict(PLACE_PROPS, playerId={"type": "integer"}, which={"type": "string", "enum": ["running", "pending"]}),
                  ["playerId"]), "deploy", t_goto),
    ]
    return {t.name: t for t in tools}


STUDIO_TOOLS = ("list_projects", "open_project", "import_resource", "import_car_pack", "enable_agent_api")
LIVE_TOOLS = ("live_maps", "live_map", "list_players", "ground_height", "set_map_spot", "map_preset", "apply_map_move",
              "cancel_map_move", "wait_map_move", "send_player_to_map")
CITY_TOOLS = ("city_map_status", "build_city_map")     # Live maps' 3D city (this computer, not a server)
MAP_TOOLS = ("get_map", "look_at_map", "pick_district", "render_map", "set_map_options", "build_map", "map_build_report",
             "install_map_in_game", "deploy_map", "ncwe_assets_list", "ncwe_assets_import", "ncwe_assets_verify",
             "ncwe_import_map")
