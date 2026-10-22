"""Getting a converted map in front of players.

* ``bundle``: a deploy bundle (.tgz of resources/assets/maps/<map>/ and resources/assets/tools/f2o77_maps/)
  in the shape tools/deploy/server_deploy.sh and the Studio's deploy install on a server host.  The
  server keeps each resource's data/ folder (the manager's state, presets and agent token) and
  merges the manager's shared/maps/<map>.lua files, so every map deployed there stays known.
* ``manager_bundle``: an update of the map manager alone (.tgz of resources/assets/tools/f2o77_maps/
  code and ghosts/<map>.txt wireframes) for ``server_deploy.sh manager-update``: the manager's code
  is replaced and each installed map gets the new wireframe, keeping where it is baked.  Lua and
  page files reload by themselves, so it needs no restart.
* ``install_game``: unpack the map's packages straight into a game folder, for direct test boots
  (OP77_CONNECT without the launcher).  Only ``archive/pc/mod/fivem2open77_map_<map>*`` files are
  written or removed, so nothing else in the folder is touched.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import zipfile

from . import resource as resource_mod

SKIP_DIRS = {"data", "_work", "__pycache__"}


def _add_tree(tar, src, arc):
    for dp, dn, fns in os.walk(src):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            info = tar.gettarinfo(p, arcname=f"{arc}/{rel}")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = int(os.path.getmtime(p))
            info.mode = 0o644
            with open(p, "rb") as f:
                tar.addfile(info, f)


def bundle(map_dir: str, out_path: str, category: str = "maps") -> dict:
    """The map resource, and the map manager as this version of fivem2open77 writes it with the
    map's own shared/maps/<map>.lua (from the map's layout.json, baked.json and ghost.txt)."""
    import shutil
    import tempfile
    map_dir = os.path.abspath(map_dir)
    name = os.path.basename(map_dir.rstrip("/\\"))
    if not os.path.isfile(os.path.join(map_dir, "layout.json")):
        raise RuntimeError(f"{map_dir} is not a converted map resource")
    with open(os.path.join(map_dir, "layout.json"), encoding="utf-8") as f:
        layout = json.load(f)
    baked, bake_id = None, None
    if os.path.isfile(os.path.join(map_dir, "baked.json")):
        with open(os.path.join(map_dir, "baked.json"), encoding="utf-8") as f:
            b = json.load(f)
        baked, bake_id = b.get("anchor"), b.get("id")
    ghost = ""
    if os.path.isfile(os.path.join(map_dir, "ghost.txt")):
        with open(os.path.join(map_dir, "ghost.txt"), encoding="utf-8") as f:
            ghost = f.read()
    tmp = tempfile.mkdtemp(prefix="f2o77-manager-")
    try:
        manager = resource_mod.write_manager_resource(os.path.join(tmp, resource_mod.MANAGER))
        resource_mod.write_map_def(manager, layout, baked, ghost, bake_id)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            _add_tree(tar, map_dir, f"resources/assets/{category}/{name}")
            # the manager: its code and THIS map's file (the other maps' files on a server stay)
            _add_tree(tar, manager, f"resources/assets/tools/{resource_mod.MANAGER}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": out_path, "resources": [name, resource_mod.MANAGER], "bytes": os.path.getsize(out_path)}


def check_ghost(text: str) -> int:
    """The number of lines of a wireframe ("x1 y1 z1 x2 y2 z2 kind" per line); ValueError when one
    is anything else (it ends up inside a Lua long string on the server)."""
    import math
    n = 0
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        try:
            vals = [float(v) for v in parts[:6]]
            kind = int(parts[6])
        except (ValueError, IndexError):
            raise ValueError(f"ghost line {i} is not 'x1 y1 z1 x2 y2 z2 kind': {line[:80]!r}") from None
        if len(parts) != 7 or not all(math.isfinite(v) and abs(v) < 1e6 for v in vals) or not 0 <= kind <= 9:
            raise ValueError(f"ghost line {i} is not 'x1 y1 z1 x2 y2 z2 kind': {line[:80]!r}")
        n += 1
    return n


def manager_bundle(out_path: str, maps=(), ghosts: dict | None = None, roads: str | None = None) -> dict:
    """The map manager's code as this version writes it, the wireframes of `maps` (converted map
    folders: their ghost.txt) and `ghosts` ({map name: ghost text}), and the road tiles in `roads`
    (what `mlo-roads` wrote: they go to the manager's data/roads/), for server_deploy.sh manager-update."""
    import re
    import shutil
    import tempfile
    found = {}
    for m in maps:
        m = os.path.abspath(m)
        name = os.path.basename(m.rstrip("/\\"))
        p = os.path.join(m, "ghost.txt")
        if not os.path.isfile(p):
            raise RuntimeError(f"{m} has no ghost.txt (convert the map again with this version)")
        with open(p, encoding="utf-8") as f:
            found[name] = f.read()
    found.update(ghosts or {})
    counts = {}
    for name, text in found.items():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError(f"bad map name {name!r}")
        counts[name] = check_ghost(text)
    road_files = []
    if roads:
        if not os.path.isfile(os.path.join(roads, "index.json")):
            raise RuntimeError(f"{roads} has no index.json (write the tiles with mlo-roads)")
        with open(os.path.join(roads, "index.json"), encoding="utf-8") as f:
            ridx = json.load(f)
        road_files = ["index.json"] + [k + ".lanes" for k in sorted(ridx.get("tiles") or {})]
        for fn in road_files:
            if not re.fullmatch(r"(index\.json|-?\d+_-?\d+\.lanes)", fn) or not os.path.isfile(os.path.join(roads, fn)):
                raise RuntimeError(f"road tiles: {fn} missing or misnamed in {roads}")
            if os.path.getsize(os.path.join(roads, fn)) > 4 * 1024 * 1024:
                raise RuntimeError(f"road tiles: {fn} is over the 4 MiB Open77 reads")
    tmp = tempfile.mkdtemp(prefix="f2o77-manager-update-")
    try:
        manager = resource_mod.write_manager_resource(os.path.join(tmp, resource_mod.MANAGER))
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            _add_tree(tar, manager, f"resources/assets/tools/{resource_mod.MANAGER}")
            for fn in road_files:
                info = tar.gettarinfo(os.path.join(roads, fn), arcname=f"roads/{fn}")
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mode = 0o644
                with open(os.path.join(roads, fn), "rb") as f:
                    tar.addfile(info, f)
            for name, text in sorted(found.items()):
                data = (text.strip("\n") + "\n").encode("utf-8")
                info = tarfile.TarInfo(f"ghosts/{name}.txt")
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": out_path, "manager": resource_mod.MANAGER_VERSION, "ghosts": counts,
            "roads": max(0, len(road_files) - 1), "bytes": os.path.getsize(out_path)}


def _game_mod_dir(game_dir: str) -> str:
    if not os.path.isdir(os.path.join(game_dir, "archive", "pc", "content")):
        raise RuntimeError(f"{game_dir} does not look like a Cyberpunk 2077 install (no archive/pc/content)")
    d = os.path.join(game_dir, "archive", "pc", "mod")
    os.makedirs(d, exist_ok=True)
    return d


def installed(game_dir: str, name: str) -> list:
    d = _game_mod_dir(game_dir)
    prefix = f"fivem2open77_map_{name}"
    return sorted(f for f in os.listdir(d) if f == prefix + ".archive" or f.startswith(prefix + ".")
                  or f.startswith(prefix + "_"))


def install_game(map_dir: str, game_dir: str, uninstall: bool = False, sector: str | None = None,
                 previous=()) -> dict:
    """Write (or remove) the map's packages in a game folder.  Files of an older build of the same
    map that the current packages no longer contain are removed.

    ``sector``: a sector package to use instead of the build's own (baked elsewhere for the spot the
    map should stand at).  NCWE asset imports ship their own archive names: those are written too,
    reported as ``extra``, and removed again when ``previous`` (an earlier result's ``extra``) names
    them and the new packages do not."""
    map_dir = os.path.abspath(map_dir)
    with open(os.path.join(map_dir, "layout.json"), encoding="utf-8") as f:
        layout = json.load(f)
    name = layout["name"]
    prefix = f"fivem2open77_map_{name}"
    mod_dir = _game_mod_dir(game_dir)
    before = installed(game_dir, name) + sorted(
        b for b in set(previous or ()) if not b.startswith(prefix) and os.path.isfile(os.path.join(mod_dir, b)))
    imported = set(a for a in layout.get("archives", []) if not a.startswith(prefix))
    wanted = {}
    if not uninstall:
        sources = [(os.path.join(map_dir, "dist", z), z) for z in layout.get("packages", [])]
        own = os.path.join(map_dir, "dist", f"{name}_sector.zip")
        if sector:
            sources.append((sector, f"{name}_sector.zip"))
        elif os.path.isfile(own):
            sources.append((own, f"{name}_sector.zip"))
        for zpath, zname in sources:
            with zipfile.ZipFile(zpath) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    if not info.filename.startswith("archive/pc/mod/"):
                        raise RuntimeError(f"{zname}: unexpected entry {info.filename}")
                    base = info.filename.split("/")[-1]
                    if not base.startswith(prefix) and not (zname.startswith(f"{name}_ncwe_") and (
                            base in imported or base.endswith(".xl") and base[:-3] in imported)):
                        raise RuntimeError(f"{zname}: {base} is not one of this map's files")
                    wanted[base] = (zpath, info.filename)
    written, unchanged, removed = [], [], []
    for base, (zpath, arc) in sorted(wanted.items()):
        with zipfile.ZipFile(zpath) as z:
            data = z.read(arc)
        dst = os.path.join(mod_dir, base)
        if os.path.isfile(dst) and os.path.getsize(dst) == len(data):
            with open(dst, "rb") as f:
                if f.read() == data:
                    unchanged.append(base)
                    continue
        tmp = dst + ".f2o77-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        written.append(base)
    for base in before:
        if base not in wanted:
            os.remove(os.path.join(mod_dir, base))
            removed.append(base)
    return {"map": name, "modDir": mod_dir, "written": written, "unchanged": unchanged, "removed": removed,
            "extra": sorted(b for b in wanted if not b.startswith(prefix))}
