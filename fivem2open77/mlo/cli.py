"""`python -m fivem2open77 mlo ...` commands (kept out of __main__ so the map tool stays one file).

    mlo          <fivem map>  --out <dir> [--place x,y,z[,yaw]]   convert (and bake if --place)
    mlo-inspect  <fivem map>  [--json]                             what would be built
    mlo-bake     <out>/<map>  --anchor x,y,z[,yaw] | --from-server URL   (the manager's new spot)
    mlo-baker    run | once | promote | status | bake | unstage  --server-dir DIR   (on the server host)
    mlo-install  <out>/<map>  --game <Cyberpunk 2077 folder> [--uninstall]   direct test boots
    mlo-bundle   <out>/<map>  --out bundle.tgz                     map + manager for server_deploy.sh
    mlo-manager-bundle --out update.tgz [--map <out>/<map>] [--ghost map=file] [--roads <dir>]
                 the manager's code, wireframes and roads for server_deploy.sh manager-update (no restart)
    mlo-roads    --game <Cyberpunk 2077 folder> --out <dir>        Night City's road lanes for the manager
    mlo-roads    --roads <dir> --near x,y [--radius 60] [--kinds rhi]   what is near a point

Every command takes --json: one JSON object on stdout (logs on stderr), {"ok": false, "error"}
on failure, so agents can drive the whole loop.
"""
from __future__ import annotations

import json
import os
import sys


def _jsonable(o):
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _log(a):
    return (lambda *x: print(*x, file=sys.stderr)) if getattr(a, "json", False) else print


def _emit(a, obj, human):
    if getattr(a, "json", False):
        print(json.dumps(dict(obj, ok=True), indent=2, default=_jsonable))
    else:
        for line in human:
            print(line)
    return 0


def _mlo_opts(a) -> dict:
    return {k: getattr(a, k, None) for k in ("name", "display_name", "anchor", "coords", "place", "entity_sets",
                                             "exclude", "no_collision", "no_coverage", "static_doors", "solid_doors", "open_gates",
                                             "door_map", "cell",
                                             "ground_spacing",
                                             "collision_budget", "instance_min_tris", "agent_token", "keep_work",
                                             "version")}


def _wk(a, log):
    from ..cp77.wolvenkit import WolvenKit
    return WolvenKit(a.wolvenkit, verbose=a.verbose, log=log)


def cmd_mlo(a, game_dir, cache_cls):
    from .pipeline import MapConverter
    from .resource import MANAGER
    log = _log(a)
    wk = _wk(a, log)
    cache = cache_cls(wk, game_dir(a.game), a.cache, log=log)
    r = MapConverter(wk, cache, a.out, log=log, **_mlo_opts(a)).convert(a.resource)
    rep = r["report"]
    name = r["name"]
    obj = {"map": name, "resource": r["resource"], "manager": r["manager"], "packages": rep["packages"],
           "parts": len(rep["parts"]), "placements": rep["placements"], "baked": r.get("baked"),
           "command": "maps", "acl_right": f"command.mlo.{name}", "acl_right_all_maps": "command.maps",
           "agent_token": bool(r.get("agent_token")),
           "resources_load": [name, MANAGER], "warnings": rep["warnings"],
           "report": os.path.join(r["resource"], "conversion-report.json")}
    human = [f"\n{name}: {r['resource']}  (+ the map manager {r['manager']})"]
    human += [f"  package  dist/{p['file']}  {p['bytes'] / 1048576:.1f} MiB  ({p['files']} files)" for p in rep["packages"]]
    human.append(f"  parts    {len(rep['parts'])} meshes, {rep['placements']} placements, {rep['textures']} textures")
    if r.get("baked"):
        b = r["baked"]
        human.append(f"  baked    at {b['anchor']}  dist/{b['package']}")
    else:
        human.append("  not baked yet: deploy it, place it in game (F9) and Apply, or mlo-bake --anchor")
    human.append(f'  resources.load: "{name}", "{MANAGER}"   (needs the archivexl loader resource)')
    human += [f"  warning  {w}" for w in rep["warnings"][:20]]
    return _emit(a, obj, human)


def cmd_mlo_inspect(a):
    from .pipeline import MapConverter
    log = lambda *x: print(*x, file=sys.stderr)  # noqa: E731
    conv = MapConverter(None, None, ".", log=log, **_mlo_opts(a))
    scene, model, coords, res_name = conv.plan(a.resource)
    out = {"resource": res_name, "map": model.name, "display_name": model.display_name, "coords": coords,
           "anchor_gta": model.anchor, "spawn_local": model.spawn, "scene": scene.summary(), "build": model.report,
           "parts": {n: {"kind": p.kind, "vertices": p.vertex_count, "triangles": p.triangle_count,
                         "submeshes": len(p.submeshes), "piece_of": p.piece_of,
                         "colliders": len(p.colliders), "collision_only": p.collision_only,
                         "sources": dict(p.sources.most_common(10))} for n, p in model.parts.items()},
           "placements": [{"part": pl.part, "position": [round(v, 3) for v in pl.position], "yaw": round(pl.yaw, 2),
                           "source": pl.source} for pl in model.placements],
           "materials": len(model.materials), "textures": len(model.textures)}
    if not a.json:
        miss = out["scene"]["missing_archetypes"]
        out["scene"]["missing_archetypes"] = dict(list(miss.items())[:25])
    print(json.dumps(out, indent=2, default=_jsonable))
    return 0


def cmd_mlo_bake(a):
    from . import bake
    log = _log(a)
    if a.anchor:
        anchor = bake.parse_anchor(a.anchor)
    elif a.from_server:
        name = bake.load_layout(a.map)["name"]
        anchor = bake.anchor_from_server(a.from_server, name)
    else:
        raise ValueError("give --anchor x,y,z[,yaw] or --from-server URL")
    r = bake.bake(a.map, anchor, _wk(a, log), log=log, keep_work=a.keep_work, out_dir=a.stage)
    b = r["baked"]
    return _emit(a, {"map": r["name"], "resource": r["resource"], "out": r["out"], "baked": b, "files": r["files"],
                     "remove": r["remove"], "managerDef": r["managerDef"], "packages": r["packages"],
                     "next": "deploy + restart the server (players re-download the sector package)"},
                 [f"{r['name']}: baked at {b['anchor']} -> {r['out']}/dist/{b['package']} ({b['bytes'] / 1024:.0f} KiB)",
                  f"  packages: {', '.join(r['packages'])}",
                  f"  manager map file: {r['managerDef'] or '(no f2o77_maps next to the map)'}",
                  "  next: deploy + restart the server; players pick it up on their next launch"])


def cmd_mlo_baker_toolkit(a):
    from . import toolkit
    dotnet_root = a.dotnet_root or os.environ.get("DOTNET_ROOT")
    if not dotnet_root:
        import shutil
        d = shutil.which("dotnet")
        dotnet_root = os.path.dirname(os.path.realpath(d)) if d else None
    if not dotnet_root:
        raise ValueError("--dotnet-root <a linux-x64 .NET 8 install> is required")
    wk = a.wolvenkit or os.environ.get("WOLVENKIT_CLI")
    if not wk:
        raise ValueError("--wolvenkit <WolvenKit.Console folder or WolvenKit.CLI.dll> is required")
    r = toolkit.build(a.out, dotnet_root, wk, log=_log(a))
    return _emit(a, r, [f"baker toolkit {r['version']} -> {r['path']} ({r['bytes'] / 1048576:.1f} MiB)"])


def cmd_mlo_baker(a):
    from . import baker
    argv = [a.baker_cmd] + ([a.map_name] if a.map_name else []) + ["--server-dir", a.server_dir]
    for opt in ("wolvenkit", "anchor"):
        if getattr(a, opt):
            argv += ["--" + opt, getattr(a, opt)]
    return baker.main(argv)


def cmd_mlo_install(a, game_dir):
    from .deploy import install_game
    game = game_dir(a.game)
    if not game:
        raise ValueError("--game <Cyberpunk 2077 folder> is required")
    r = install_game(a.map, game, uninstall=a.uninstall)
    return _emit(a, r, [f"{r['map']}: {len(r['written'])} written, {len(r['unchanged'])} unchanged, "
                        f"{len(r['removed'])} removed in {r['modDir']}"] + [f"  {f}" for f in r["written"] + r["removed"]])


def cmd_mlo_bundle(a):
    from .deploy import bundle
    r = bundle(a.map, a.out, category=a.category)
    return _emit(a, r, [f"bundle {r['path']} ({r['bytes'] / 1048576:.1f} MiB): {', '.join(r['resources'])}"])


def cmd_mlo_roads(a):
    from . import roads as roads_mod
    log = _log(a)
    if a.near:
        if not a.roads:
            raise SystemExit("--near needs --roads <dir> (the tiles mlo-roads wrote)")
        x, y = (float(v) for v in a.near.split(",")[:2])
        r = roads_mod.Roads(a.roads)
        found = r.near(x, y, a.radius, a.kinds or None, limit=a.limit)
        lanes = [{k: v for k, v in ln.items() if k != "points"} | {"points": len(ln["points"])} for ln in found]
        return _emit(a, {"x": x, "y": y, "radius": a.radius, "lanes": lanes},
                     [f"{len(lanes)} lane(s) within {a.radius:g} m of {x:g}, {y:g}"] +
                     [f"  {ln['distance']:7.1f} m  {roads_mod.KINDS.get(ln['kind'], ln['kind']):18s} heading {ln['heading']:6.1f}"
                      f"  width {ln['width']:g} m  (lane {ln['id']})" for ln in lanes])
    if not a.out:
        raise SystemExit("--out <dir> is needed to write the road tiles")
    if a.from_json:
        idx = roads_mod.from_json(a.from_json, a.out)
    else:
        game = a.game or os.environ.get("CP77_GAME_DIR")
        if not game:
            raise SystemExit("--game <Cyberpunk 2077 folder> (or CP77_GAME_DIR)")
        idx = roads_mod.extract(game, _wk(a, log), a.out, log=log)
    return _emit(a, {"out": a.out, "lanes": idx["lanes"], "tiles": len(idx["tiles"]), "kinds": idx["kinds"],
                     "source": idx["source"]},
                 [f"roads: {idx['lanes']} lanes in {len(idx['tiles'])} tiles -> {a.out} ({idx['source']})"])


def cmd_mlo_manager_bundle(a):
    from .deploy import manager_bundle
    ghosts = {}
    for g in a.ghost or []:
        name, sep, path = g.partition("=")
        if not sep or not name or not path:
            raise SystemExit(f"--ghost wants map=file, not {g!r}")
        with open(path, encoding="utf-8") as f:
            ghosts[name] = f.read()
    r = manager_bundle(a.out, maps=a.map or [], ghosts=ghosts, roads=a.roads)
    return _emit(a, r, [f"manager update {r['path']} (f2o77_maps {r['manager']}, {r['bytes'] / 1024:.0f} KiB)"] +
                 [f"  wireframe {n}: {c} lines" for n, c in sorted(r["ghosts"].items())])


def add_parsers(sub, common, game_dir, cache_cls):
    def mlo_common(sp):
        sp.add_argument("resource", help="FiveM map/MLO resource folder or archive (.zip/.rar/.7z)")
        sp.add_argument("--name", help="map/resource id (default: the FiveM resource name)")
        sp.add_argument("--display-name", help="name shown in game")
        sp.add_argument("--coords", help="FiveM spawn coords x,y,z (default: a coords .txt in the input)")
        sp.add_argument("--anchor", help="GTA world point x,y,z the map is placed by (default: around --coords)")
        sp.add_argument("--entity-sets", default="default", help="MLO entity sets: default | all | none | a,b")
        sp.add_argument("--exclude", help="comma-separated archetype names to leave out")
        sp.add_argument("--no-collision", action="store_true", help="visual only")
        sp.add_argument("--no-coverage", action="store_true", help="skip the collision coverage check (faster)")
        sp.add_argument("--static-doors", action="store_true",
                        help="doors and gates do not open (default: they become Door devices that open by "
                             "themselves); static doors get no collision, static gates stay solid")
        sp.add_argument("--solid-doors", action="store_true", help="with --static-doors: doors and shutters block")
        sp.add_argument("--open-gates", action="store_true", help="with --static-doors: gates and barriers get no collision")
        sp.add_argument("--door-map", help='door mapping, JSON text or a .json file: {"objects": {"<name>": '
                                           '"hinged|sliding|rolling|open|solid|remove"}, "doorways": {"<mlo>#<n>": '
                                           '"open|door-hinged|door-double|door-sliding|door-rolling"}} (see mlo/doorways.py)')
        sp.add_argument("--cell", type=float, help="chunk cell size in metres (default 48)")
        sp.add_argument("--ground-spacing", type=float, help="ground collision grid in metres (default 2)")
        sp.add_argument("--collision-budget", type=int, help="max colliders per collision source (default 2500)")
        sp.add_argument("--instance-min-tris", type=int, help="objects with this many triangles get their own mesh")
        sp.add_argument("--json", action="store_true", help="machine-readable output (logs go to stderr)")

    def wk_args(sp):
        sp.add_argument("--wolvenkit", help="WolvenKit.CLI(.exe|.dll) path (or WOLVENKIT_CLI)")
        sp.add_argument("-v", "--verbose", action="store_true")
        sp.add_argument("--json", action="store_true", help="machine-readable output (logs go to stderr)")

    m = sub.add_parser("mlo", help="convert a FiveM map/MLO into Open77 map resources (baked map + the map manager)")
    mlo_common(m)
    m.add_argument("--out", required=True, help="output folder (resources <out>/<name> and <out>/f2o77_maps)")
    m.add_argument("--place", help="bake right away at this Night City anchor x,y,z[,yaw]")
    m.add_argument("--agent-token", help="token for the manager's agent HTTP API in <out>/f2o77_maps/data "
                                         "(or 'auto'; a server generates its own at the first start)")
    m.add_argument("--version", help="resource version (default 1.0.0)")
    m.add_argument("--keep-work", action="store_true")
    common(m)
    m.set_defaults(fn=lambda a: cmd_mlo(a, game_dir, cache_cls))

    mi = sub.add_parser("mlo-inspect", help="what the map converter sees and would build (no game files written)")
    mlo_common(mi)
    mi.set_defaults(fn=cmd_mlo_inspect)

    b = sub.add_parser("mlo-bake", help="bake a converted map at an anchor (sector package)")
    b.add_argument("map", help="the converted map resource folder (<out>/<name>)")
    b.add_argument("--anchor", help="x,y,z[,yaw] in Night City")
    b.add_argument("--from-server", metavar="URL", help="read the new spot saved in the map manager "
                                                        "(e.g. http://127.0.0.1:11781)")
    b.add_argument("--stage", metavar="DIR", help="write the build to DIR instead of the map resource")
    b.add_argument("--keep-work", action="store_true")
    wk_args(b)
    b.set_defaults(fn=cmd_mlo_bake)

    ins = sub.add_parser("mlo-install", help="unpack a map's packages into a game folder (direct test boots)")
    ins.add_argument("map", help="the converted map resource folder (<out>/<name>)")
    ins.add_argument("--game", help="Cyberpunk 2077 install folder (or CP77_GAME_DIR)")
    ins.add_argument("--uninstall", action="store_true", help="remove the map's files instead")
    ins.add_argument("--json", action="store_true")
    ins.set_defaults(fn=lambda a: cmd_mlo_install(a, game_dir))

    bk = sub.add_parser("mlo-baker", help="the map baker on a server host (see fivem2open77/mlo/baker.py)")
    bk.add_argument("baker_cmd", choices=("run", "once", "promote", "status", "bake", "unstage"))
    bk.add_argument("map_name", nargs="?", metavar="map", help="bake / unstage: the map")
    bk.add_argument("--server-dir", required=True, help="the Open77 server folder (server.jsonc, resources/)")
    bk.add_argument("--wolvenkit", help="WolvenKit.CLI(.exe|.dll) path (or WOLVENKIT_CLI)")
    bk.add_argument("--anchor", help="bake: x,y,z[,yaw]")
    bk.set_defaults(fn=cmd_mlo_baker)

    tk = sub.add_parser("mlo-baker-toolkit", help="the map baker's toolkit for a linux server host (.NET runtime, "
                                                  "WolvenKit CLI, the baker)")
    tk.add_argument("--out", required=True, help="toolkit path (.tgz)")
    tk.add_argument("--dotnet-root", help="a linux-x64 .NET 8 install (default: DOTNET_ROOT or the dotnet on PATH)")
    tk.add_argument("--wolvenkit", help="the WolvenKit.Console folder or its WolvenKit.CLI.dll (or WOLVENKIT_CLI)")
    tk.add_argument("--json", action="store_true")
    tk.set_defaults(fn=cmd_mlo_baker_toolkit)

    bu = sub.add_parser("mlo-bundle", help="deploy bundle (.tgz) of the map and the map manager for server_deploy.sh")
    bu.add_argument("map", help="the converted map resource folder (<out>/<name>)")
    bu.add_argument("--out", required=True, help="bundle path (.tgz)")
    bu.add_argument("--category", default="maps", help="resources/assets/<category>/ on the server (default maps)")
    bu.add_argument("--json", action="store_true")
    bu.set_defaults(fn=cmd_mlo_bundle)

    mb = sub.add_parser("mlo-manager-bundle", help="update bundle (.tgz) of the map manager's code and map wireframes "
                                                   "for server_deploy.sh manager-update (no server restart)")
    mb.add_argument("--out", required=True, help="bundle path (.tgz)")
    mb.add_argument("--map", action="append", help="a converted map folder (<out>/<name>) whose ghost.txt goes along "
                                                   "(repeatable)")
    mb.add_argument("--ghost", action="append", metavar="MAP=FILE", help="a wireframe file for a map (repeatable)")
    mb.add_argument("--roads", help="the road tiles `mlo-roads` wrote (the manager's road overlay and road tools)")
    mb.add_argument("--json", action="store_true")
    mb.set_defaults(fn=cmd_mlo_manager_bundle)

    rd = sub.add_parser("mlo-roads", help="Night City's road lanes from the game's traffic data, for the map manager "
                                          "(or what is near a point)")
    rd.add_argument("--game", help="Cyberpunk 2077 install folder (or CP77_GAME_DIR)")
    rd.add_argument("--from-json", help="a folder with all.traffic_persistent.json and all.lane_polygons.json instead")
    rd.add_argument("--out", help="the folder to write the tiles to (index.json + <x>_<y>.lanes)")
    rd.add_argument("--roads", help="query: the tiles folder")
    rd.add_argument("--near", metavar="X,Y", help="query: lanes near this point")
    rd.add_argument("--radius", type=float, default=60.0)
    rd.add_argument("--kinds", help="query: lane kinds, e.g. rhi (cars) or pjc (people)")
    rd.add_argument("--limit", type=int, default=50)
    rd.add_argument("--wolvenkit", help="WolvenKit.CLI(.exe|.dll) path (or WOLVENKIT_CLI)")
    rd.add_argument("-v", "--verbose", action="store_true")
    rd.add_argument("--json", action="store_true")
    rd.set_defaults(fn=cmd_mlo_roads)
