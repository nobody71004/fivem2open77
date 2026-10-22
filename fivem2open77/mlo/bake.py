"""Bake a map's anchor into its sector package.

    python -m fivem2open77 mlo-bake <out>/<map> --anchor x,y,z[,yaw]
    python -m fivem2open77 mlo-bake <out>/<map> --from-server http://127.0.0.1:11781    (the manager's new spot)

Writes dist/<map>_sector.zip (archive/pc/mod/fivem2open77_map_<map>_sector.archive + .archive.xl),
adds it to the map's open77.lua, records the anchor and the bake id in baked.json and in the map
resource's server script (its `baked` export), and the map manager's shared/maps/<map>.lua.  A map
with doors also gets <map>.devices in the sector archive (merged into the world's device list by
the same .xl) and their open77_doors registration in the map resource's server script.  The assets
package is not touched, so a re-bake is seconds and players re-download a few kilobytes.  Same
anchor in, same package bytes out (pinned archive and zip timestamps).

``out_dir`` sends everything a bake writes to another folder (the server's map baker stages a
build there and installs it while the server is stopped; see baker.py), with the manager's map file
as <out_dir>/manager/<map>.lua.

Standard library only: the server's map baker runs this with the system python3.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import urllib.request

from ..cp77 import package
from ..cp77.wkjson import save
from . import doors as doors_mod
from . import resource as resource_mod
from . import world

MOD_ROOT = "fivem2open77"


def sector_paths(name: str) -> tuple:
    base = rf"{MOD_ROOT}\maps\{name}\sector"
    return rf"{base}\{name}.streamingsector", rf"{base}\{name}.streamingblock"


def devices_path(name: str) -> str:
    return rf"{MOD_ROOT}\maps\{name}\sector\{name}.devices"


def sector_archive(name: str) -> str:
    return f"{MOD_ROOT}_map_{name}_sector"


def parse_anchor(text: str) -> dict:
    vals = [float(v) for v in str(text).replace(" ", ",").split(",") if v.strip()]
    if len(vals) < 3 or not all(math.isfinite(v) for v in vals):
        raise ValueError(f"anchor must be x,y,z[,yaw]: {text!r}")
    return normalize_anchor({"x": vals[0], "y": vals[1], "z": vals[2], "yaw": vals[3] if len(vals) > 3 else 0.0})


def normalize_anchor(a: dict) -> dict:
    for k in ("x", "y", "z"):
        v = float(a[k])
        if not math.isfinite(v) or abs(v) > 100000:
            raise ValueError(f"invalid anchor {k}: {a[k]!r}")
    yaw = float(a.get("yaw") or 0.0)
    if not math.isfinite(yaw):
        raise ValueError(f"invalid anchor yaw: {a.get('yaw')!r}")
    return {"x": round(float(a["x"]), 3), "y": round(float(a["y"]), 3), "z": round(float(a["z"]), 3),
            "yaw": round(world.wrap_yaw(yaw), 2)}


def new_bake_id(name: str, origin: str = "cli") -> str:
    return f"{name}-{origin}-{int(time.time())}"


def server_map(url: str, name: str, timeout=10) -> dict:
    """One map as the map manager reports it (GET /f2o77_maps/maps/<map>, no token needed)."""
    u = url.rstrip("/")
    if not u.endswith("/" + resource_mod.MANAGER):
        u = f"{u}/{resource_mod.MANAGER}"
    with urllib.request.urlopen(urllib.request.Request(f"{u}/maps/{name}"), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def anchor_from_server(url: str, name: str, timeout=10) -> dict:
    """The new spot saved in the map manager (or, without one, where the map runs now)."""
    m = server_map(url, name, timeout)
    a = m.get("pending") or m.get("running")
    if not a:
        raise RuntimeError(f"{name} has no spot on the server yet (place it with the map manager first)")
    return normalize_anchor(a)


def load_layout(res_dir: str) -> dict:
    p = os.path.join(res_dir, "layout.json")
    if not os.path.isfile(p):
        raise RuntimeError(f"{res_dir} is not a converted map resource (no layout.json)")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def read_ghost(res_dir: str) -> str:
    p = os.path.join(res_dir, "ghost.txt")
    if not os.path.isfile(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bake(res_dir: str, anchor: dict, wk, log=print, keep_work=False, out_dir: str | None = None,
         bake_id: str | None = None, manager_dir: str | None = None) -> dict:
    """Build the sector package of the map in ``res_dir`` for ``anchor``.

    Without ``out_dir`` the map resource is updated in place, and so is the manager's map file when
    the manager sits next to it (<out>/f2o77_maps; or ``manager_dir``).  With ``out_dir`` nothing in
    ``res_dir`` changes: the result is that folder, ``files`` (relative paths to copy over the map
    resource), ``remove`` (paths to take away) and ``managerDef`` (the manager's map file)."""
    res_dir = os.path.abspath(res_dir)
    layout = load_layout(res_dir)
    name = layout["name"]
    # the sector package is one more preload package on top of the map's own (and its NCWE imports):
    # a map already at Open77's limit cannot take it, so refuse before anything is written.  Builds
    # of fivem2open77 1.7.4 and later always leave that slot free; an older build of a big map may not
    if len(layout.get("packages", [])) + 1 > package.MAX_PACKAGES:
        raise RuntimeError(
            f"{name} has {len(layout.get('packages', []))} preload packages and placing it adds a sector package, "
            f"but Open77 allows {package.MAX_PACKAGES} per resource: rebuild the map with Studio 1.7.4 or later "
            "(it packs big maps tighter and keeps a package free for the sector) and deploy it again, then place it")
    anchor = normalize_anchor(anchor)
    bake_id = bake_id or new_bake_id(name)
    target = os.path.abspath(out_dir) if out_dir else res_dir
    staged = target != res_dir
    sector_path, block_path = sector_paths(name)
    sdoc, box = world.sector_doc(layout, anchor)
    bdoc = world.block_doc(sector_path, box)
    devs = world.door_devices(layout, anchor)
    files = [(sdoc, sector_path), (bdoc, block_path)]
    dev_path = None
    if devs:
        dev_path = devices_path(name)
        files.append((doors_mod.devices_doc([(d["ref"], d["position"]) for d in devs]), dev_path))

    work = tempfile.mkdtemp(prefix=f"f2o77-bake-{name}-")
    zname = f"{name}_sector.zip"
    zpath = os.path.join(target, "dist", zname)
    try:
        jdir, adir, pdir = (os.path.join(work, d) for d in ("json", "archive", "packed"))
        for doc, depot_path in files:
            save(doc, os.path.join(jdir, depot_path.replace("\\", os.sep) + ".json"))
        log(f"[{name}] baking {len(layout['placements'])} placements ({len(devs)} doors) at "
            f"{anchor['x']:.2f} {anchor['y']:.2f} {anchor['z']:.2f} yaw {anchor['yaw']:.1f}")
        wk.deserialize_tree(jdir, adir, work)
        aname = sector_archive(name)
        src_root = os.path.join(work, "pack", aname)
        for _, depot_path in files:
            s = os.path.join(adir, depot_path.replace("\\", os.sep))
            if not os.path.exists(s):
                raise RuntimeError(f"WolvenKit did not write {depot_path}")
            d = os.path.join(src_root, depot_path.replace("\\", os.sep))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)
        wk.pack(src_root, pdir)
        out = os.path.join(pdir, aname + ".archive")
        if not os.path.exists(out):
            raise RuntimeError(f"WolvenKit did not produce {aname}.archive")
        package.normalize_archive(out)
        xl = os.path.join(work, aname + ".archive.xl")
        with open(xl, "w", encoding="utf-8", newline="\n") as f:
            f.write(world.archive_xl(block_path, dev_path))
        zsize = package.write_zip(zpath, {f"archive/pc/mod/{aname}.archive": out,
                                          f"archive/pc/mod/{aname}.archive.xl": xl})
        if keep_work:
            log(f"[{name}] bake work kept in {work}")
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)

    baked = {
        "anchor": anchor, "id": bake_id, "package": zname, "bytes": zsize, "sha256": _sha256(zpath),
        "archive": aname + ".archive", "sector": sector_path, "block": block_path,
        "nodes": len(layout["placements"]) + len(layout.get("ambience") or []),
        "quietAreas": len(layout.get("ambience") or []),
        "streamingBox": {"min": list(box[0]), "max": list(box[1])},
        "doors": len(devs), "devices": dev_path,
        "bakedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(target, "baked.json"), "w", encoding="utf-8") as f:
        json.dump(baked, f, indent=1)
    packages = list(layout.get("packages", [])) + [zname]
    written = ["dist/" + zname, "baked.json"]
    written += resource_mod.write_map_resource(target, layout, packages, anchor, doors=devs, bake_id=bake_id,
                                               write_layout=not staged)
    remove = []
    doors_json = os.path.join(target, "doors.json")
    if devs:
        with open(doors_json, "w", encoding="utf-8") as f:
            json.dump(devs, f, indent=1)
        written.append("doors.json")
    else:
        remove.append("doors.json")
        if os.path.exists(doors_json):
            os.remove(doors_json)
    ghost = read_ghost(res_dir)
    manager_def = None
    if staged:
        manager_def = os.path.join(target, "manager", f"{name}.lua")
        os.makedirs(os.path.dirname(manager_def), exist_ok=True)
        with open(manager_def, "w", encoding="utf-8", newline="\n") as f:
            f.write(resource_mod.map_def_lua(layout, anchor, ghost, bake_id))
    else:
        mdir = manager_dir or resource_mod.manager_dir_for(res_dir)
        if os.path.isfile(os.path.join(mdir, "open77.lua")):
            manager_def = resource_mod.write_map_def(mdir, layout, anchor, ghost, bake_id)
    where = "staged in " + target if staged else "dist/" + zname
    log(f"[{name}] sector package {where} ({zsize / 1024:.0f} KiB); players pick it up after the next server "
        "restart and their next launch")
    return {"name": name, "resource": res_dir, "out": target, "baked": baked, "packages": packages,
            "files": sorted(set(written) - {"layout.json"}), "remove": remove, "managerDef": manager_def}
