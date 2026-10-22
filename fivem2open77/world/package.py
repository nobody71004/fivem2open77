"""A world edit as an Open77 resource, and the bundle that installs it on a server.

    <out>/<name>/
        open77.lua       server resource: ArchiveXL dependency, preload_mod of the package
        server/main.lua  says what it changes at start; export `edits` for tools
        dist/<name>.zip  archive/pc/mod/fivem2open77_world_<name>.xl (the sector patches, and the
                         streaming block of added objects), + .archive of added objects
        edits.json       the edit list, its summary and the build
        README.md

Deletions and changes need no WolvenKit (the ``.xl`` is text).  Added objects do: their sector,
its streaming block and a host entity per mesh (the map baker's shapes, mlo/world.py) are written
as JSON and turned into an archive by WolvenKit.  So does Night City off (world/nightcity.py): the
city's two streaming blocks without its sectors, and its distant lights without lights, in the
same archive, replacing the game's own.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import tarfile
import tempfile

from ..cp77 import package as cpkg
from ..cp77.wkjson import document, save
from ..mlo import world as mw
from . import edits as E

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
VERSION = "1.0.0"


def archive_name(name: str) -> str:
    return f"fivem2open77_world_{name}"


def depot_root(name: str) -> str:
    return f"fivem2open77\\world\\{name}"


def _host_path(name: str, mesh: str) -> str:
    stem = re.sub(r"[^a-z0-9_]+", "_", mesh.lower().split("\\")[-1].split("#")[0].rsplit(".", 1)[0])[:40]
    h = hashlib.sha1(mesh.lower().encode()).hexdigest()[:8]
    return f"{depot_root(name)}\\hosts\\{stem}_{h}.ent"


def additions_docs(name: str, adds) -> tuple:
    """[(json document, depot path)] for the added objects (sector, block, hosts) and the block path."""
    sector_path = f"{depot_root(name)}\\sector\\{name}.streamingsector"
    block_path = f"{depot_root(name)}\\sector\\{name}.streamingblock"
    files, hosts = [], {}
    nodes, data = [], []
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for i, a in enumerate(adds):
        res = a["res"]
        if res.endswith(".mesh"):
            physical = a.get("collision", "mesh") == "mesh"
            key = (res, physical)
            ent = hosts.get(key)
            if ent is None:
                ent = hosts[key] = _host_path(name, res + ("" if physical else "#nocollision"))
                files.append((mw.host_entity_doc(name, f"h{len(hosts)}", res, physical=physical), ent))
        else:
            ent = res
        label = re.sub(r"[^a-z0-9_]+", "_", (a.get("label") or res.split("\\")[-1]).lower())[:40]
        node = mw._entity_node(ent, f"{name}_{label}_{i:03d}")
        if res.endswith(".ent") and a.get("app"):
            node["appearanceName"] = {"$type": "CName", "$storage": "string", "$value": a["app"]}
        nodes.append({"HandleId": str(i), "Data": node})
        pos = a["pos"]
        primary, secondary = mw.streaming_range(10.0)
        ref = mw.stable_id("world-add", name, i, res, round(pos[0], 3), round(pos[1], 3), round(pos[2], 3))
        data.append(mw._node_data(i, pos, a.get("yaw", 0.0), ref, primary, secondary))
        for k in range(3):
            lo[k] = min(lo[k], pos[k] - primary)
            hi[k] = max(hi[k], pos[k] + primary)
    root = {
        "$type": "worldStreamingSector", "category": mw.SECTOR_CATEGORY, "cookingPlatform": "PLATFORM_PC",
        "externInplaceResource": {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"}, "Flags": "Soft"},
        "level": mw.SECTOR_LEVEL, "localInplaceResource": [],
        "nodeData": {"BufferId": "0", "Flags": mw.BUFFER_FLAGS, "Type": mw.NODE_BUFFER, "Data": data},
        "nodeRefs": [], "nodes": nodes, "persistentNodeIndex": 0, "persistentNodes": [], "variantIndices": [0],
        "variantNodes": [], "version": 62,
    }
    files.append((document(root), sector_path))
    files.append((mw.block_doc(sector_path, (tuple(lo), tuple(hi))), block_path))
    return files, block_path


def _main_lua(name: str, display: str, summ: dict) -> str:
    off = summ.get("city") == "off"
    note = ('print("[{name}] Night City is OFF: none of the city\'s own sectors stream in (only the maps and '
            'objects of resources)")\n').format(name=name) if off else ""
    return f'''-- {display}: a world edit made with fivem2open77 (Studio > World).  The edit itself is the
-- ArchiveXL package this resource preloads; players get it with the server's required mods.
local SUMMARY = {{ sectors = {summ["sectors"]}, deleted = {summ["deleted"]}, elements = {summ["elementsDeleted"]}, changed = {summ["changed"]}, added = {summ["added"]}, city = "{summ.get("city", "on")}" }}
print(("[{name}] world edit: %d object(s) removed, %d part(s) of objects removed, %d changed, %d added, in %d sector(s)")
    :format(SUMMARY.deleted, SUMMARY.elements, SUMMARY.changed, SUMMARY.added, SUMMARY.sectors))
{note}exports("edits", function() return SUMMARY end)
'''


def build(name: str, display: str, edits, out_dir: str, wk=None, log=print, version: str = VERSION,
          extra_packages=(), city_docs=None) -> dict:
    """Write the resource <out_dir>/<name>; returns {path, zip, xl, summary, sha256, bytes, ncwe}.

    extra_packages: NCWE asset imports ([{zip, source, archives}], studio/ncweassets.prepare).  Each
    is copied into dist/ and preloaded by its own preload_mod line, so the editor's props ship with the edit.
    city_docs: Night City off -- [(WolvenKit JSON document, depot path)] of the game files that
    replace the city's (world/nightcity.documents); packed into the edit's archive."""
    if not NAME_RX.match(name or ""):
        raise E.EditError(f"resource name {name!r}: lower case letters, digits and _ (3-48), starting with a letter")
    edits = E.normalize_all(edits)
    p = E.plan(edits)
    summ = E.summary(edits)
    city_docs = list(city_docs or ())
    summ["city"] = "off" if city_docs else "on"
    if not p["sectors"] and not p["adds"] and not city_docs:
        raise E.EditError("nothing to build: no edits yet")
    extra = list(extra_packages or ())
    if 1 + len(extra) > cpkg.MAX_PACKAGES:
        raise E.EditError(f"{len(extra)} NCWE asset package(s) and the edit's own package are over Open77's "
                          f"{cpkg.MAX_PACKAGES} preload packages per resource; remove some imports")
    res = os.path.join(os.path.abspath(out_dir), name)
    work = tempfile.mkdtemp(prefix=f"f2o77-world-{name}-")
    aname = archive_name(name)
    try:
        files = {}
        blocks = []
        if p["adds"] or city_docs:
            if wk is None:
                raise RuntimeError(("adding objects" if p["adds"] else "turning Night City off") +
                                   " needs WolvenKit (Settings > Tools)")
            docs = []
            if p["adds"]:
                docs, block_path = additions_docs(name, p["adds"])
                blocks.append(block_path)
            docs += city_docs
            jdir, adir = os.path.join(work, "json"), os.path.join(work, "archive")
            for doc, depot_path in docs:
                save(doc, os.path.join(jdir, depot_path.replace("\\", os.sep) + ".json"))
            what = ([f"{len(p['adds'])} added object(s)"] if p["adds"] else []) + (
                ["Night City's streaming blocks without the city"] if city_docs else [])
            log(f"[{name}] writing {' and '.join(what)} with WolvenKit")
            wk.deserialize_tree(jdir, adir, work)
            src_root = os.path.join(work, "pack", aname)
            for _, depot_path in docs:
                s = os.path.join(adir, depot_path.replace("\\", os.sep))
                if not os.path.exists(s):
                    raise RuntimeError(f"WolvenKit did not write {depot_path}")
                d = os.path.join(src_root, depot_path.replace("\\", os.sep))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copyfile(s, d)
            pdir = os.path.join(work, "packed")
            wk.pack(src_root, pdir)
            arch = os.path.join(pdir, aname + ".archive")
            if not os.path.exists(arch):
                raise RuntimeError(f"WolvenKit did not produce {aname}.archive")
            cpkg.normalize_archive(arch)
            files[f"archive/pc/mod/{aname}.archive"] = arch
        text = E.to_xl(edits, display, blocks)
        if p["sectors"] or blocks:            # Night City off alone patches nothing: no .xl then
            xl = os.path.join(work, aname + ".xl")
            with open(xl, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            files[f"archive/pc/mod/{aname}.xl"] = xl
        if os.path.isdir(res):
            shutil.rmtree(res)
        os.makedirs(os.path.join(res, "dist"))
        os.makedirs(os.path.join(res, "server"))
        zpath = os.path.join(res, "dist", f"{name}.zip")
        zsize = cpkg.write_zip(zpath, files)
        ncwe = []
        for i, ex in enumerate(extra, 1):
            stem = os.path.splitext(str(ex.get("source") or "import"))[0]
            slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")[:24] or "import"
            zname = f"{name}_ncwe_{i}_{slug}.zip"
            dst = os.path.join(res, "dist", zname)
            shutil.copyfile(ex["zip"], dst)
            nsize = os.path.getsize(dst)
            if nsize > cpkg.MAX_PACKAGE:
                raise E.EditError(f"{ex.get('source')}: {nsize / 1048576:.1f} MiB is over Open77's 64 MiB preload limit")
            ncwe.append({"file": f"dist/{zname}", "bytes": nsize, "source": ex.get("source"),
                         "archives": list(ex.get("archives") or [])})
            log(f"[{name}] NCWE asset {ex.get('source')} -> {zname} ({nsize / 1048576:.1f} MiB)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    preloads = "".join(f'preload_mod "{n["file"]}"\n' for n in ncwe)
    with open(os.path.join(res, "open77.lua"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f'''-- {display}: world edit made with fivem2open77.  The edit is the ArchiveXL package below
-- (inert data: an .xl that patches Night City's own sectors{', and the sector of the objects added' if p["adds"] else ''}{'; Night City OFF: the city streaming blocks without its sectors' if city_docs else ''}).
resource "{name}"
version "{version}"
auto_start true

dependency "archivexl >=1.27.1"
preload_mod "dist/{name}.zip"
{preloads}
server_script "server/main.lua"
''')
    with open(os.path.join(res, "server", "main.lua"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_main_lua(name, display, summ))
    sha = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    info = {"name": name, "display": display, "version": version, "summary": summ, "edits": edits,
            "package": {"file": f"dist/{name}.zip", "bytes": zsize, "sha256": sha}, "ncwe": ncwe,
            "builtAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    with open(os.path.join(res, "edits.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    with open(os.path.join(res, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"# {display}\n\nA world edit made with fivem2open77: {summ['deleted']} object(s) removed, "
                f"{summ['elementsDeleted']} part(s) of objects removed, {summ['changed']} changed, {summ['added']} added "
                f"in {summ['sectors']} sector(s)." + (
                    "\n\n**Night City is off** while this is installed: none of the city's own sectors (terrain, "
                    "roads, buildings, interiors, quest content) stream in, only maps and objects other resources "
                    "add. Remove the resource and restart the server to bring the city back." if city_docs else "") +
                f"\n\nInstall it like any resource (resources/assets/world/{name}) and add "
                f"`{name}` to resources.load; it needs the `archivexl` loader resource and requiredMods enabled. Players "
                "get the change at their next launch after a server restart.\n")
    log(f"[{name}] world edit built: {summ['deleted']} removed, {summ['elementsDeleted']} parts removed, "
        f"{summ['changed']} changed, {summ['added']} added" + ("; Night City off" if city_docs else "") +
        f" ({zsize} bytes)")
    return {"path": res, "zip": zpath, "xl": text, "summary": summ, "sha256": sha, "bytes": zsize, "ncwe": ncwe}


def bundle(res_dirs, out_path: str) -> dict:
    """resources/assets/world/<name>/... for `server_deploy.sh deploy`."""
    names = []
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with tarfile.open(out_path, "w:gz", format=tarfile.PAX_FORMAT) as t:
        for d in res_dirs:
            name = os.path.basename(os.path.abspath(d).rstrip("/\\"))
            if not os.path.isfile(os.path.join(d, "open77.lua")):
                raise RuntimeError(f"{d} is not a built world edit (no open77.lua)")
            for dp, _, fns in os.walk(d):
                for fn in sorted(fns):
                    full = os.path.join(dp, fn)
                    arc = "resources/assets/world/" + name + "/" + os.path.relpath(full, d).replace(os.sep, "/")
                    info = t.gettarinfo(full, arcname=arc)
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = 0
                    info.mode = 0o644
                    with open(full, "rb") as f:
                        t.addfile(info, f)
            names.append(name)
    return {"path": out_path, "resources": names, "bytes": os.path.getsize(out_path)}
