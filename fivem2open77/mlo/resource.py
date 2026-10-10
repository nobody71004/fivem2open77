"""The Open77 resources of converted maps.

    <out>/<map>/                the map itself: preload packages only (server-side manifest)
        open77.lua              preload_mod dist/<map>.zip [, _2 ...] [, dist/<map>_sector.zip once baked]
                                dependency archivexl (the sector is merged by ArchiveXL)
        server/main.lua         where the map is baked (server export `baked`), door registration
        layout.json             parts, placements, entity paths, bounds: what a bake needs
        ghost.txt               the wireframe the manager draws while someone moves the map
        baked.json              the anchor (and bake id) the sector package was built for
        dist/*.zip              game-root-relative packages (archive/pc/mod/...)
    <out>/f2o77_maps/           the map manager, one for every converted map on a server: the in-game
                                panel (F9) and editor, the server side that asks the map baker for a
                                new build and restarts the server, the agent HTTP API
        open77.lua, shared/config.lua, shared/maps/<map>.lua (one per map), server/, client/, web/

Why the client side lives in the manager: a resource with a loader `dependency` and a client script
fails to activate on clients (the loaders have no client side; see cp77/package.py).
"""
from __future__ import annotations

import json
import math
import os
import shutil

LOADERS = (("archivexl", "1.27.1"),)

MANAGER = "f2o77_maps"
MANAGER_SRC = os.path.join(os.path.dirname(__file__), "manager")
MANAGER_VERSION = "1.3.8"
MANAGER_PERMISSIONS = [
    "network.events",     # panel <-> server
    "acl.read",           # who may move maps (command.maps, command.mlo.<map>)
    "players.life.read",  # Open77.players.get: "bring here"
    "players.teleport",   # go to a map
    "players.screenshot", # agent API: a picture from a client
    "http.serve",         # agent API (only when the server enables httpHandlers)
    "input.actions",      # rebindable keys (RegisterKeyMapping, Open77.input)
    "world.query",        # crosshair ray, ground height (client and server)
    "world.debug",        # the wireframe (Open77.debugDraw)
    "ui.vanilla.hud",     # notifications while the panel is closed
    "filesystem.read",    # data/: state, the baker's answers, settings
    "filesystem.write",   # data/: state, bake requests
    "runtime.restart",    # restart the server so a move goes live
    "camera.script",      # an agent's camera over the map (maps view), given back on any key of the manager
]
# the files of the manager that are code (an update replaces them); shared/maps/ and data/ are not
MANAGER_CODE = ("open77.lua", "README.md", "shared/config.lua", "shared/maps/_index.lua", "server/roads.lua",
                "server/main.lua", "client/main.lua", "web/index.html", "web/app.css", "web/app.js")


def lua_literal(v, indent=0) -> str:
    pad = "    " * indent
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "0.0"
        return repr(round(v, 6))
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # \\ \" \n escapes are valid Lua; UTF-8 stays raw
    if isinstance(v, (list, tuple)):
        if not v:
            return "{}"
        if all(not isinstance(x, (dict, list, tuple)) for x in v):
            return "{ " + ", ".join(lua_literal(x) for x in v) + " }"
        inner = ",\n".join(pad + "    " + lua_literal(x, indent + 1) for x in v)
        return "{\n" + inner + ",\n" + pad + "}"
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = []
        for k, x in v.items():
            if x is None:
                continue
            key = k if isinstance(k, str) and k.isidentifier() else "[" + json.dumps(str(k)) + "]"
            items.append(f"{pad}    {key} = {lua_literal(x, indent + 1)}")
        if not items:
            return "{}"
        return "{\n" + ",\n".join(items) + ",\n" + pad + "}"
    raise TypeError(f"cannot write {type(v).__name__} to Lua")


def _long_string(s: str) -> str:
    level = 0
    while ("]" + "=" * level + "]") in s:
        level += 1
    eq = "=" * level
    return f"[{eq}[\n{s}]{eq}]"


def door_count(layout: dict) -> int:
    doors = {p for p, d in layout.get("parts", {}).items() if d.get("kind") == "door"}
    return sum(1 for pl in layout.get("placements", []) if pl.get("part") in doors)


def map_def(layout: dict, baked: dict | None, bake_id: str | None = None) -> dict:
    """What the manager knows about one map (no mesh paths, no placement list: the map is baked)."""
    return {
        "name": layout["name"], "display": layout.get("display", layout["name"]),
        "version": layout.get("version", "1.0.0"), "resource": layout["name"],
        "partCount": len(layout["parts"]), "placementCount": len(layout["placements"]),
        "doors": door_count(layout),
        "spawn": layout.get("spawn"), "bounds": layout.get("bounds"),
        "default": layout.get("default"), "baked": baked, "bakeId": bake_id,
    }


GHOST_COMMENT = ("-- wireframe: x1 y1 z1 x2 y2 z2 kind per line (4 outline at ground level, 5 gates, 0 box, "
                 "1 buildings, 2 walls, 3 ground)")


def map_def_lua(layout: dict, baked: dict | None, ghost_text: str, bake_id: str | None = None) -> str:
    name = layout["name"]
    key = json.dumps(name)
    out = [f"-- Generated by fivem2open77: what the map manager knows about {layout.get('display', name)}.",
           "-- Positions are metres in the map's own frame (anchor at 0, 0, 0, yaw 0).  `baked` is where the",
           "-- sector package on this server puts the map; the map baker rewrites this file when a move goes live.",
           "F2O77_MAPS = F2O77_MAPS or {}",
           f"F2O77_MAPS[{key}] = {lua_literal(map_def(layout, baked, bake_id))}"]
    if ghost_text:
        out.append(GHOST_COMMENT)
        out.append(f"F2O77_MAPS[{key}].ghost = {_long_string(ghost_text)}")
    return "\n".join(out) + "\n"


MAP_README = """# {display}

FiveM map converted to Open77 by fivem2open77 (`mlo`): {parts} meshes, {placements} placements.
The map is baked into Night City as an ArchiveXL streaming sector.  It is moved in game with the
map manager (`f2o77_maps`, F9), which has the map baker rebuild this resource's sector package on
the server host.

## Install
Copy `{name}/` and `f2o77_maps/` under the server's `resources/` (e.g. `resources/assets/maps/`
and `resources/assets/tools/`), add `"{name}"` and `"f2o77_maps"` to `resources.load` (the
ArchiveXL loader resource `archivexl` must be loaded too), keep `requiredMods.enabled` on and
restart the server.  Players get the packages through the launcher before the game starts.  The
fivem2open77 deploy scripts (tools/deploy) do all of it, and install the map baker service.

## Move it
In game (ACL right `command.maps`, or `command.mlo.{name}` for this map only): press **F9**
(rebindable).  Pick the map, "Move in the world" (a wireframe follows: arrows move, PgUp/PgDn
height, Home/End rotate, Shift fast, Ctrl fine, Ins follow the crosshair, Del drop to the ground,
Enter save, Backspace cancel), then **Apply now** (the server restarts after a countdown) or
**Apply when empty**.  Console / chat: `maps help`.

Without the in-game manager: `python -m fivem2open77 mlo-bake {name} --anchor x,y,z,yaw`, deploy,
restart.
"""

MANAGER_README = """# f2o77_maps: the map manager

Moves the FiveM maps fivem2open77 converted (each one its own resource with preload packages),
in game.  Press **F9** (rebind it under Pause > Settings > KEY BINDINGS, or in the panel's Keys
tab) with the ACL right `command.maps` (every map) or `command.mlo.<map>` (one map).

* **Maps**: where each map is now, a new spot (move the wireframe in the world, type numbers, bring
  it where you stand, drop it on the ground, nudge it), saved positions, history.
* **Apply**: the map baker (a service next to the server, `python3 -m fivem2open77.mlo.baker run`)
  builds the sector package for the new spot in `<server>/f2o77-maps/staged/`, never touching the
  live map; the server restarts after a countdown (or once nobody is online) and the baker's start
  hook (ExecStartPre of the server unit) installs the build before the server comes back.  Players
  rejoin to see the map at its new spot.  Cancel takes a build back until the restart.
* Console / chat: `maps help`.  Agent HTTP API (server option httpHandlers.enabled):
  `GET /f2o77_maps/status | /maps/<map> | /baker`, `GET /players` and
  `POST /f2o77_maps/maps/<map>/<place|move|here|discard|revert|ground|drop|goto|apply|cancel|presetSave|presetUse|presetDelete>`
  with `Authorization: Bearer <data/agent_token.txt>` (written at the first start).
* **Roads**: `data/roads/` holds Night City's traffic lanes (from the game's own data:
  `fivem2open77 mlo-roads`; the manager update installs them).  While a map is moved the lanes
  around it are drawn with the wireframe, so it can be lined up with the streets.
* **Agents** (Claude through the map agent on a player's computer, or any tool that types commands
  for a player): `maps @<id> <command...>` runs a command as that player and puts the answer in
  that player's game log as one `@agent {json}` line.  The parts that happen in that player's game:
  `maps view <map> top|oblique|gate <n>|off` (a camera over the map, given back after two minutes
  or as soon as the player opens the panel), `maps wire <map> on|off`, `maps roads <map> on|off`,
  `maps fit <map>` (the ground check), `maps probe x,y ...` (static ground under points),
  `maps scan [x y z] [h=200] [cols=40] [rows=25]` (what a camera above that point sees, by screen
  rays: terrain, roads, roofs -- the heights a placement is judged against), `maps tp x y z`,
  `maps shot <http://127.0.0.1:port/...>` (a screenshot to the player's own computer, nowhere
  else), `maps say <text>`; and the server's: `maps version | state [map] | log [n] | players | client`.

Server-side settings that survive updates: `data/config.json` (see shared/config.lua for the names).
One file per map in `shared/maps/` (written by fivem2open77 and by the baker); the rest of this
folder is replaced when a newer version is installed.
"""

MAPS_INDEX_LUA = """-- The maps this manager knows: one file per map (<map>.lua) next to this one, written by
-- fivem2open77 when it converts or bakes a map, and by the map baker when a move goes live.
F2O77_MAPS = F2O77_MAPS or {}
"""


def map_manifest(name: str, version: str, display: str, packages: list, server_script=True) -> str:
    lines = [
        f"-- {display}: FiveM map converted by fivem2open77 (mlo).  Baked geometry: preload packages",
        "-- plus the ArchiveXL loader.  Server-side only on purpose: the map manager (f2o77_maps) has",
        "-- the client side.",
        f'resource "{name}"',
        f'version "{version}"',
        "auto_start true",
        "",
    ]
    lines += [f'dependency "{loader} >={ver}"' for loader, ver in LOADERS]
    lines += [f'preload_mod "dist/{p}"' for p in packages]
    if server_script:
        lines += ["", 'server_script "server/main.lua"']
    return "\n".join(lines) + "\n"


def manager_manifest(version: str = MANAGER_VERSION) -> str:
    lines = [
        "-- fivem2open77 map manager: moves the converted FiveM maps in game (F9, rebindable), has the",
        "-- map baker rebuild a map for its new spot and restarts the server to apply it.  See README.md.",
        f'resource "{MANAGER}"',
        f'version "{version}"',
        "auto_start true",
        "",
        'shared_scripts { "shared/config.lua", "shared/maps/*.lua" }',
        'server_scripts { "server/roads.lua", "server/main.lua" }',
        'client_script "client/main.lua"',
        'web_files { "web/**" }',
        "",
        "permissions {",
    ]
    lines += [f'    "{p}",' for p in MANAGER_PERMISSIONS]
    lines += ["}"]
    return "\n".join(lines) + "\n"


MAP_SERVER_LUA = """-- {display}: where the baked sector puts the map (written by fivem2open77 when it bakes).
local BAKED = {baked}
if BAKED then
    print(("[{name}] baked at %.2f %.2f %.2f yaw %.1f ({packages})"):format(BAKED.x, BAKED.y, BAKED.z, BAKED.yaw))
else
    print("[{name}] not baked yet: place it with the map manager (f2o77_maps, F9) and Apply")
end
-- the map manager (f2o77_maps) asks which spot the package this server started with puts the map at
exports("baked", function() return BAKED end)
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def map_server_lua(layout: dict, packages: list, baked: dict | None, bake_id: str | None = None,
                   doors: list | None = None) -> str:
    from .doors import register_lua
    name = layout["name"]
    b = dict(baked, id=bake_id) if baked else None
    server = MAP_SERVER_LUA.format(display=layout.get("display", name), name=name,
                                   baked=lua_literal(b).replace("\n", " "), packages=", ".join(packages))
    if doors:
        server += register_lua(name, [{"id": d["id"], "position": d["center"], "radius": d["radius"]} for d in doors])
    return server


def write_map_resource(res_dir: str, layout: dict, packages: list, baked: dict | None, report: dict | None = None,
                       doors: list | None = None, bake_id: str | None = None, write_layout: bool = True) -> list:
    """``doors``: the baked door devices (world.door_devices), registered with open77_doors.
    Returns the files written, relative to ``res_dir``."""
    name = layout["name"]
    display = layout.get("display", name)
    _write(os.path.join(res_dir, "open77.lua"), map_manifest(name, layout.get("version", "1.0.0"), display, packages))
    _write(os.path.join(res_dir, "server", "main.lua"), map_server_lua(layout, packages, baked, bake_id, doors))
    _write(os.path.join(res_dir, "README.md"),
           MAP_README.format(display=display, name=name, parts=len(layout["parts"]), placements=len(layout["placements"])))
    files = ["open77.lua", "server/main.lua", "README.md"]
    if write_layout:
        with open(os.path.join(res_dir, "layout.json"), "w", encoding="utf-8") as f:
            json.dump(layout, f, indent=1)
        files.append("layout.json")
    if report is not None:
        with open(os.path.join(res_dir, "conversion-report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        files.append("conversion-report.json")
    return files


def write_manager_resource(manager_dir: str, agent_token: str = "") -> str:
    """The manager's code (replaced as a whole); shared/maps/<map>.lua and data/ are left alone."""
    for rel in MANAGER_CODE:
        dst = os.path.join(manager_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if rel == "open77.lua":
            _write(dst, manager_manifest())
        elif rel == "README.md":
            _write(dst, MANAGER_README)
        elif rel == "shared/maps/_index.lua":
            _write(dst, MAPS_INDEX_LUA)
        else:
            shutil.copyfile(os.path.join(MANAGER_SRC, *rel.split("/")), dst)
    if agent_token:
        _write(os.path.join(manager_dir, "data", "agent_token.txt"), agent_token + "\n")
    return manager_dir


def write_map_def(manager_dir: str, layout: dict, baked: dict | None, ghost_text: str, bake_id: str | None = None) -> str:
    path = os.path.join(manager_dir, "shared", "maps", f"{layout['name']}.lua")
    _write(path, map_def_lua(layout, baked, ghost_text, bake_id))
    return path


def manager_dir_for(res_dir: str) -> str:
    """The manager next to a converted map in an output folder (<out>/f2o77_maps)."""
    return os.path.join(os.path.dirname(os.path.abspath(res_dir)), MANAGER)
