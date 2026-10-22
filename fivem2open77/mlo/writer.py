"""MapModel -> Cyberpunk files -> archives -> the Open77 resources of the map.

    parts      -> <MOD_ROOT>\\maps\\<name>\\meshes\\<part>.mesh   (CMesh from a vanilla static mesh
                  template: our materials, our geometry imported from a glb, our colliders in a
                  meshMeshParamPhysics parameter)
               -> <MOD_ROOT>\\maps\\<name>\\ents\\<part>.ent     (host entity a sector node spawns;
                  for a door part the game's own transform-animated Door device, doors.py)
    textures   -> <MOD_ROOT>\\maps\\<name>\\textures\\<tex>.xbm
    archives   -> split so every zip stays under Open77's 64 MiB preload cap (8 packages max per
                  resource, one of them kept free for the sector package a placed map adds; a map
                  that needs more by raw size is regrouped by what its files take once packed)
    resources  -> <out>/<name> (packages, layout.json, ghost.txt) and the map manager
                  <out>/f2o77_maps (shared/maps/<name>.lua: what it knows about this map);
                  the sector package comes from bake.py once the map has an anchor
"""
from __future__ import annotations

import json
import os
import shutil
import time

import numpy as np

from ..cp77 import package
from ..cp77.mesh import ordered_submeshes, template_doc, write_glb
from ..cp77.wkjson import Handles, save
from ..cp77.xbm import xbm_document
from ..model.ir import Material, SubMesh
from . import ambience
from . import bake as bake_mod
from . import doors as doors_mod
from . import ghost as ghost_mod
from . import resource as resource_mod
from . import world
from .materials import material_json
from .physics import physics_param

MOD_ROOT = "fivem2open77"
STATIC_TEMPLATE = r"base\environment\decoration\containers\cargo\freight\freight_ep1_a_wall_a.mesh"
WORLD_TEMPLATES = (STATIC_TEMPLATE,)
ARCHIVE_RAW_BUDGET = 56 * 1024 * 1024      # raw bytes per archive before packing (zip < 64 MiB)
PACKED_BUDGET = 60 * 1024 * 1024           # packed bytes per archive when a big map is regrouped (zip < 64 MiB)
HIDDEN = "m_hidden"


def _placeholder_submesh() -> SubMesh:
    """A 1 mm triangle 50 m below the part origin: lets a collision-only part be a valid mesh."""
    p = np.array([[0, 0, -50.0], [0.001, 0, -50.0], [0, 0.001, -50.0]], np.float32)
    n = np.tile([[0, 0, 1.0]], (3, 1)).astype(np.float32)
    t = np.tile([[1.0, 0, 0, 1.0]], (3, 1)).astype(np.float32)
    uv = np.zeros((3, 2), np.float32)
    return SubMesh(material=HIDDEN, positions=p, normals=n, tangents=t, uv0=uv, uv1=None, colors=None,
                   indices=np.array([0, 1, 2], np.uint32), lod=1)


def _asset_slug(source: str, seen: set) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in str(source).lower())[:24].strip("_") or "import"
    base, n = slug, 2
    while f"{base}.zip" in seen:
        base, n = f"{slug}_{n}", n + 1
    seen.add(f"{base}.zip")
    return base


def attach_extra_packages(res_dir: str, name: str, layout: dict, zips: list, extra_packages, log=print) -> list:
    """NCWE asset imports (studio/ncweassets.prepare): copy each prepared package into dist/ and
    declare it in the manifest, the server script and the layout, so it ships with every bake."""
    added = []
    seen = set(layout["packages"])
    for ex in extra_packages or ():
        slug = _asset_slug(ex.get("source") or "import", seen)
        zname = f"{name}_ncwe_{slug}.zip"
        dst = os.path.join(res_dir, "dist", zname)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(ex["zip"], dst)
        zsize = os.path.getsize(dst)
        if zsize > package.MAX_PACKAGE:
            raise RuntimeError(f"{zname} is {zsize / 1048576:.1f} MiB, over Open77's 64 MiB preload limit")
        layout["packages"].append(zname)
        layout["archives"].extend(ex["archives"])
        info = {"file": zname, "bytes": zsize, "archive": ", ".join(ex["archives"]),
                "files": len(ex["archives"]), "source": ex.get("source")}
        zips.append(info)
        added.append(info)
        log(f"[{name}] imported NCWE asset {ex.get('source')} -> {zname} ({zsize / 1048576:.1f} MiB)")
    return added


class MapWriter:
    def __init__(self, wk, cache, out_dir: str, log=print, keep_work=False, version="1.0.0"):
        self.wk = wk
        self.cache = cache
        self.out_dir = os.path.abspath(out_dir)
        self.log = log
        self.keep_work = keep_work
        self.version = version

    def write(self, model, *, defaults=None, source_info=None, scene_summary=None, agent_token="",
              quiet_interiors="off", extra_packages=()) -> dict:
        t0 = time.time()
        name = model.name
        work = os.path.join(self.out_dir, "_work", name)
        shutil.rmtree(work, ignore_errors=True)
        jdir, rdir, adir, pdir = (os.path.join(work, d) for d in ("json", "raw", "archive", "packed"))
        base = rf"{MOD_ROOT}\maps\{name}"

        def jpath(depot_path):
            return os.path.join(jdir, depot_path.replace("\\", os.sep) + ".json")

        has_doors = any(p.kind == "door" for p in model.parts.values())
        self.cache.ensure_paths(WORLD_TEMPLATES + ((doors_mod.DOOR_TEMPLATE,) if has_doors else ()))
        tpl = self.cache.load(STATIC_TEMPLATE)
        door_tpl = self.cache.load(doors_mod.DOOR_TEMPLATE) if has_doors else None

        # ---------------------------------------------------------- textures
        tex_paths, tex_files = {}, set()

        def tex_path(key, usage):
            k = (key, "normal" if usage == "normal" else "color")
            if k not in tex_paths:
                stem = key + ("_n" if usage == "normal" else "")
                p = rf"{base}\textures\{stem}.xbm"
                save(xbm_document(model.textures[key], usage), jpath(p))
                tex_paths[k] = p
                tex_files.add(p)
            return tex_paths[k]

        mats_json = {key: material_json(m, tex_path) for key, m in model.materials.items()}
        hidden = Material(key=HIDDEN, kind="textured", alpha="test")
        mats_json[HIDDEN] = material_json(hidden, tex_path)

        # ---------------------------------------------------------- meshes
        mesh_paths, ent_paths = {}, {}
        os.makedirs(rdir, exist_ok=True)
        part_meta = {}
        for pname, part in model.parts.items():
            mp = rf"{base}\meshes\{pname}.mesh"
            mesh_paths[pname] = mp
            if not part.submeshes:
                part.submeshes = [_placeholder_submesh()]
            subs = ordered_submeshes(part)
            used = []
            for s in subs:
                if s.material not in used:
                    used.append(s.material)
            doc = template_doc(tpl, {k: mats_json[k] for k in used}, [s.material for s in subs])
            root = doc["Data"]["RootChunk"]
            root["objectType"] = "ROT_Static"
            if part.colliders:
                root["parameters"] = [physics_param(part.colliders, Handles(doc), f"{name} {pname}",
                                                    simulation="Kinematic" if part.kind == "door" else "Static")]
            save(doc, jpath(mp))
            glb = os.path.join(rdir, mp.replace("\\", os.sep)[:-5] + ".glb")
            os.makedirs(os.path.dirname(glb), exist_ok=True)
            write_glb(part, glb)
            mn, mx = part.bounds()
            ep = rf"{base}\ents\{pname}.ent"
            radius = float(np.linalg.norm(np.subtract(mx, mn))) * 0.5
            primary, _ = world.streaming_range(radius)
            if part.kind == "door":
                save(doors_mod.door_entity_doc(door_tpl, mp, part.door), jpath(ep))
            else:
                save(world.host_entity_doc(name, pname, mp, auto_hide=round(primary, 1),
                                          physical=bool(part.colliders)), jpath(ep))
            ent_paths[pname] = ep
            part_meta[pname] = {"mesh": mp, "ent": ep, "radius": round(radius, 2),
                                "kind": part.kind, "vertices": part.vertex_count,
                                "triangles": part.triangle_count, "colliders": len(part.colliders),
                                "collision_only": part.collision_only,
                                "bounds": [[round(float(v), 3) for v in mn], [round(float(v), 3) for v in mx]],
                                "sources": dict(part.sources.most_common(12))}
            if part.door:
                part_meta[pname]["door"] = dict(part.door)
            if part.piece_of:
                part_meta[pname]["piece_of"] = part.piece_of

        # ---------------------------------------------------------- WolvenKit: JSON -> CR2W, glb import
        self.log(f"[{name}] {len(model.parts)} meshes, {len(tex_files)} textures, "
                 f"{sum(len(p.colliders) for p in model.parts.values())} colliders -> WolvenKit...")
        self.wk.deserialize_tree(jdir, adir, work)
        self.wk.import_glb(rdir, adir)
        expected = list(mesh_paths.values()) + list(ent_paths.values()) + sorted(tex_files)
        missing = [p for p in expected if not os.path.exists(os.path.join(adir, p.replace("\\", os.sep)))]
        if missing:
            raise RuntimeError("WolvenKit did not write: " + ", ".join(missing[:10]))

        # ---------------------------------------------------------- archives under the size cap
        files = []
        for p in expected:
            files.append((p, os.path.getsize(os.path.join(adir, p.replace("\\", os.sep)))))
        res_dir = os.path.join(self.out_dir, name)
        shutil.rmtree(os.path.join(res_dir, "dist"), ignore_errors=True)
        packed = self.pack_map(name, files, adir, work, pdir, extra=len(extra_packages))
        zips = []
        for i, (out, group) in enumerate(packed, 1):
            aname = os.path.basename(out)[:-len(".archive")]
            zname = f"{name}.zip" if len(packed) == 1 else f"{name}_{i}.zip"
            zsize = package.write_zip(os.path.join(res_dir, "dist", zname), {f"archive/pc/mod/{aname}.archive": out})
            if zsize > package.MAX_PACKAGE:
                raise RuntimeError(f"{zname} is {zsize / 1048576:.1f} MiB, over Open77's 64 MiB preload limit")
            zips.append({"file": zname, "bytes": zsize, "archive": aname + ".archive", "files": len(group)})

        # ---------------------------------------------------------- the two Open77 resources
        mn, mx = model.bounds()
        quiet = ambience.floor_volumes(model, quiet_interiors or "off")
        layout = {
            "ambience": [{"box": box} for box in quiet],
            "name": name, "display": model.display_name, "version": self.version,
            "parts": {p: {"mesh": m["mesh"], "ent": m["ent"], "kind": m["kind"], "radius": m["radius"],
                          "collision": m["colliders"] > 0, "bounds": m["bounds"],
                          **({"door": m["door"]} if m.get("door") else {})} for p, m in part_meta.items()},
            "placements": [{"part": pl.part, "x": round(pl.position[0], 4), "y": round(pl.position[1], 4),
                            "z": round(pl.position[2], 4), "yaw": round(pl.yaw, 4)} for pl in model.placements],
            "spawn": None if model.spawn is None else {k: round(v, 3) for k, v in zip("xyz", model.spawn)},
            "bounds": {"min": [round(float(v), 2) for v in mn], "max": [round(float(v), 2) for v in mx]},
            "packages": [z["file"] for z in zips],
            "archives": [z["archive"] for z in zips],
            "default": defaults,
        }
        attach_extra_packages(res_dir, name, layout, zips, extra_packages, log=self.log)
        lines = ghost_mod.ghost_lines(model)
        ghost_text = ghost_mod.encode(lines)
        report = {
            "tool": "fivem2open77 mlo", "version": self.version, "seconds": round(time.time() - t0, 1),
            "source": source_info or {}, "map": name, "display_name": model.display_name,
            "anchor_gta": list(model.anchor), "packages": zips, "parts": part_meta,
            "placements": len(model.placements), "materials": len(model.materials), "textures": len(tex_files),
            "ghost_lines": len(lines), "build": model.report, "scene": scene_summary or {}, "warnings": model.warnings,
            "quiet_interiors": {"mode": quiet_interiors or "off", "volumes": quiet},
        }
        manager_dir = os.path.join(self.out_dir, resource_mod.MANAGER)
        # the old per-map placer (fivem2open77 < 1.2) is replaced by the map manager
        old_placer = os.path.join(self.out_dir, f"{name}_placer")
        if os.path.isfile(os.path.join(old_placer, "shared", "layout.lua")):
            shutil.rmtree(old_placer, ignore_errors=True)
        for stale in ("baked.json", "doors.json", os.path.join("dist", f"{name}_sector.zip")):
            if os.path.exists(os.path.join(res_dir, stale)):
                os.remove(os.path.join(res_dir, stale))
        resource_mod.write_map_resource(res_dir, layout, layout["packages"], None, report=report)
        with open(os.path.join(res_dir, "ghost.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(ghost_text)
        resource_mod.write_manager_resource(manager_dir, agent_token=agent_token)
        resource_mod.write_map_def(manager_dir, layout, None, ghost_text)
        baked = None
        if defaults:
            baked = bake_mod.bake(res_dir, defaults, self.wk, log=self.log,
                                  bake_id=bake_mod.new_bake_id(name, "convert"))["baked"]
            report["baked"] = baked
            with open(os.path.join(res_dir, "conversion-report.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, default=str)
        if not self.keep_work:
            shutil.rmtree(work, ignore_errors=True)
        self.log(f"[{name}] done in {report['seconds']}s -> {res_dir} + {manager_dir}")
        return {"name": name, "resource": res_dir, "manager": manager_dir, "report": report, "layout": layout,
                "baked": baked}

    def pack_map(self, name, files, adir, work, pdir, extra=0) -> list:
        """The map's files [(depot path, bytes on disk)] in ``adir`` -> its archives, as few as fit:
        [(archive path, group)].  Placing the map adds its sector package (bake.py) to these and to
        ``extra`` NCWE imports, so one preload slot stays free for it: without that the map could be
        converted but never placed."""
        allowed = package.MAX_PACKAGES - 1 - extra
        if allowed < 1:
            raise RuntimeError(f"{extra} NCWE asset import(s) leave no room for the map: Open77 allows "
                               f"{package.MAX_PACKAGES} packages per resource and the map needs one or more plus its "
                               "sector package (remove some imports in the Studio)")
        archive_base = f"{MOD_ROOT}_map_{name}"
        groups = _bin_pack(files, ARCHIVE_RAW_BUDGET)
        packed = self._pack(groups, adir, os.path.join(work, "pack"), pdir, archive_base)
        if len(packed) <= allowed:
            return packed
        # raw sizes say little about the packed size (meshes and textures pack 2-5x smaller): measure
        # what each file takes in the archives just packed and regroup by that
        cost = {}
        for out, _group in packed:
            cost.update(package.archive_costs(out))
        measured = [(p, cost.get(package.depot_hash(p), s)) for p, s in files]
        total = sum(s for _, s in measured)
        groups = _bin_pack(measured, PACKED_BUDGET)
        self.log(f"[{name}] {total / 1048576:.0f} MiB packed: {len(packed)} archives by raw size, regrouped into "
                 f"{len(groups)} so the sector package fits ({package.MAX_PACKAGES} per resource)")
        if len(groups) > allowed:
            imports = f" and {extra} NCWE import(s)" if extra else ""
            raise RuntimeError(
                f"{name} is {total / 1048576:.0f} MiB packed: that is {len(groups)} packages of at most "
                f"{PACKED_BUDGET // 1048576} MiB, and Open77 allows {package.MAX_PACKAGES} per resource ({allowed} "
                f"for the map next to its sector package{imports}): convert a smaller area (the map's area or "
                "district in Options) or leave parts out")
        shutil.rmtree(pdir, ignore_errors=True)
        return self._pack(groups, adir, os.path.join(work, "pack2"), pdir, archive_base)

    def _pack(self, groups, adir, pack_root, pdir, archive_base) -> list:
        """Each group into its own archive (timestamps pinned): [(archive path, group)]."""
        packed = []
        for i, group in enumerate(groups, 1):
            aname = archive_base if len(groups) == 1 else f"{archive_base}_{i}"
            src_root = os.path.join(pack_root, aname)
            for p, _ in group:
                s = os.path.join(adir, p.replace("\\", os.sep))
                d = os.path.join(src_root, p.replace("\\", os.sep))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                _link_or_copy(s, d)
            self.wk.pack(src_root, pdir)
            out = os.path.join(pdir, aname + ".archive")
            if not os.path.exists(out):
                raise RuntimeError(f"WolvenKit did not produce {aname}.archive")
            package.normalize_archive(out)
            if not self.keep_work:
                shutil.rmtree(src_root, ignore_errors=True)
            packed.append((out, group))
        return packed


def _link_or_copy(src, dst):
    """A hard link where the file system has them (the pack folders are throw-away views of the
    archive folder), else a copy."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _bin_pack(files, budget):
    """First-fit decreasing by size; meshes and their textures may land in different archives,
    which is fine: the depot resolves paths across every mounted archive."""
    groups = []
    for p, s in sorted(files, key=lambda x: -x[1]):
        for g in groups:
            if g[0] + s <= budget:
                g[0] += s
                g[1].append((p, s))
                break
        else:
            groups.append([s, [(p, s)]])
    return [sorted(g[1]) for g in groups]
