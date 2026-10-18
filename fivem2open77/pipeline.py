"""FiveM vehicle resource -> Open77 resource (one command).

    scan (FiveM resource) -> IR (model/build) -> Cyberpunk files as WolvenKit JSON + glb
    -> WolvenKit deserialize/import/pack -> TweakXL/ArchiveXL -> zip -> Open77 resource
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import time

import numpy as np

from . import profile as profile_mod
from .cp77 import app as app_mod
from .cp77 import ent as ent_mod
from .cp77 import package
from .cp77 import sound as sound_mod
from .cp77.donor import DONORS, DonorCache, choose_donor, missing_corners
from .cp77.materials import material_json, neon_material
from .cp77.mesh import ordered_submeshes, template_doc, write_glb
from .cp77.phys import build_phys
from .cp77.rig import build_rigs, lid_mapping
from .cp77.tweak import build_tweak
from .cp77.wkjson import depot, save, value_of
from .cp77.xbm import xbm_document
from .gta.resource import scan_resource
from .model import neon as neon_mod
from .model import seats as seats_mod
from .model.build import SIDE_NAMES, VehicleBuilder

MOD_ROOT = "fivem2open77"
TEXTURE_PACKAGE_BUDGET = 40 * 1024 * 1024   # raw xbm bytes per texture archive (zip stays < 64 MiB)
VERSION = "1.6.0"


def _sanitize(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return s or "tex"


def _srgb_to_linear(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


class Cancelled(Exception):
    pass


def build_ir(src, prof: dict, log=print):
    """GTA model -> IR with the profile's part, light and paint overrides (no donor files needed)."""
    bopts = {k: prof[k] for k in ("name", "display_name") if prof.get(k)}
    bopts["bone_roles"] = prof["parts"]["bone_roles"]
    bopts["extras"] = prof["parts"]["extras"]
    bopts["mods"] = prof["parts"].get("mods", {})
    v = VehicleBuilder(src, bopts, log=log).build()
    apply_light_overrides(v, prof["lights"])
    if prof["paint"]["primary"] != "auto":
        v.paint_colors = (profile_mod.parse_color(prof["paint"]["primary"]), v.paint_colors[1])
    donor = choose_donor(v, None if prof["donor"] == "auto" else prof["donor"])
    v.seat_fit = seats_mod.fit_seats(v, prof.get("seats") or {}, DONORS[donor].get("seating", "sport2"),
                                     DONORS[donor].get("occupant_seats"))
    neon_conf = prof.get("neon") or {}
    if neon_conf.get("enabled", True):
        neon_mod.fit_kit(v, v.markers, neon_conf)
    return v


def apply_light_overrides(v, conf: dict):
    if not conf:
        return
    known = {l.name for l in v.lights}
    for name in conf:
        if name not in known:
            v.warnings.append(f"profile: light '{name}' does not exist (lights: {', '.join(sorted(known))})")
    keep = []
    for light in v.lights:
        c = conf.get(light.name) or {}
        if c.get("skip"):
            continue
        if "color" in c:
            light.color = tuple(int(round(x * 255)) for x in profile_mod.parse_color(c["color"]))
        if "type" in c:
            light.light_type = c["type"]
        keep.append(light)
    v.lights = keep


def slot_bones(donor_ent_doc) -> dict:
    """vehicle_slots slot -> rig bone, as the donor entity's slot component maps them."""
    from .cp77.handles import materialize
    from .cp77.wkjson import index_handles
    index = index_handles(donor_ent_doc)
    for c in donor_ent_doc["Data"]["RootChunk"]["components"]:
        c = materialize(c, index)
        if c.get("$type") == "entSlotComponent" and value_of(c.get("name")) == "vehicle_slots":
            return {value_of(sl.get("slotName")): value_of(sl.get("boneName")) for sl in c.get("slots", [])}
    return {}


def rig_bone_names(rig_doc) -> list:
    return [value_of(n) for n in rig_doc["Data"]["RootChunk"]["boneNames"]]


def bind_parts(v, donor_rig_doc, donor_ent_doc) -> dict:
    """Check every part's slot against the donor: a part bound to a slot whose bone the donor's rig
    lacks would be left behind at the car's origin in game, so it is fixed to the body instead.
    Returns {"bones": part -> bone, "detached": [parts], "rig": [bone names], "slots": {slot: bone}}."""
    slots = slot_bones(donor_ent_doc)
    rig = set(rig_bone_names(donor_rig_doc))
    bones, detached = {}, []
    for pname, part in list(v.parts.items()):
        if not part.slot:
            continue
        bone = slots.get(part.slot, part.slot)
        bones[pname] = bone
        if bone not in rig:
            v.detach_to_body(pname)
            detached.append(pname)
    if detached:
        v.warnings.append("the donor's rig has no bone for " + ", ".join(
            f"{p} ({bones[p]})" for p in detached) + ": fixed to the body")
    return {"bones": bones, "detached": detached, "rig": sorted(rig), "slots": slots}


class Converter:
    """profile: a conversion profile (see profile.py); the old keyword options (name, display_name,
    donor, model, door_chime, chime_volume, chime_range) still work and are folded into it.
    cancel: optional threading.Event checked between steps."""

    def __init__(self, wk, cache: DonorCache, out_dir: str, log=print, profile=None, cancel=None, **opts):
        self.wk = wk
        self.cache = cache
        self.out_dir = os.path.abspath(out_dir)
        self.log = log
        self.cancel = cancel
        self.keep_work = bool(opts.pop("keep_work", False))
        self.version = str(opts.pop("version", None) or VERSION)
        patch = {}
        for k in ("name", "display_name", "model"):
            if opts.get(k):
                patch[k] = opts[k]
        if opts.get("donor"):
            patch["donor"] = opts["donor"]
        if opts.get("door_chime"):
            patch["chime"] = {"enabled": True, "file": opts["door_chime"]}
        for k, key in (("chime_volume", "volume"), ("chime_range", "range")):
            if opts.get(k) is not None:
                patch.setdefault("chime", {})[key] = opts[k]
        merged = profile_mod.merge(profile or {}, patch)
        self.profile = profile_mod.normalize(merged, donors=DONORS, sounds=sound_mod.CATALOGUE)

    def _step(self, msg=None):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("cancelled")
        if msg:
            self.log(msg)

    # ------------------------------------------------------------------ public
    def convert(self, resource_path: str) -> list:
        res = scan_resource(resource_path)
        results = []
        try:
            if not res.vehicles:
                raise ValueError("no vehicles found in this resource (vehicles.meta + .yft)")
            only = self.profile.get("model")
            for src in res.vehicles:
                if only and src.model.lower() != only.lower():
                    continue
                results.append(self.convert_vehicle(src))
        finally:
            res.cleanup()
        return results

    # ------------------------------------------------------------------ one car
    def build_vehicle(self, src):
        return build_ir(src, self.profile, self.log)

    def lid_plan(self, v, donor_rig) -> dict:
        mode = self.profile["parts"]["lids"]
        if mode == "semantic":
            return {}
        if mode == "swap":
            have = [n for n in ("hood_a", "trunk_a") if n in v.parts or n in v.bones]
            other = {"hood_a": "trunk_a", "trunk_a": "hood_a"}
            return {n: other[n] for n in have}
        return lid_mapping(v, donor_rig)

    def convert_vehicle(self, src, dry: bool = False) -> dict:
        """dry: stop after the rigs, the TweakDB record and the quality checks (no files written,
        no WolvenKit): what the Studio's quality sweep runs on every car."""
        t0 = time.time()
        log = self.log
        self._step()
        v = self.build_vehicle(src)
        name = v.name
        requested = None if self.profile["donor"] == "auto" else self.profile["donor"]
        donor = choose_donor(v, requested)
        prof = DONORS[donor]
        if requested and donor != requested:
            v.warnings.append(f"donor {requested} has no doors at {', '.join(missing_corners(v, requested))} "
                              f"(their parts would be left behind at the car's origin in game): {donor} is used")
        log(f"[{name}] {v.display_name}: {sum(p.vertex_count() for p in v.parts.values())} verts, "
            f"{len(v.parts)} parts, {len(v.textures)} textures; donor {prof['label']}")
        self.cache.ensure(donor)
        self._step()
        donor_rig = self.cache.load(prof["rig"])
        lids = self.lid_plan(v, donor_rig)
        if lids:
            v.rename_parts(lids)
            log(f"[{name}] lids ({self.profile['parts']['lids']}): " + ", ".join(
                f"{'front' if v.bones[new]['t'][1] > 0 else 'rear'} lid {old} -> {new}" for old, new in sorted(lids.items())))
        donor_ent = self.cache.load(prof["ent"])
        bindings = bind_parts(v, donor_rig, donor_ent)
        for pname in bindings["detached"]:
            log(f"[{name}] {pname}: the donor's rig has no {bindings['bones'][pname]} bone; "
                "the part is fixed to the body (it does not open)")
        sound = sound_mod.resolve(self.profile["sound"]["engine"], v, prof["sound"])
        log(f"[{name}] engine sound: {sound['label']} ({sound['player']}; {sound['source']})")

        work = os.path.join(self.out_dir, "_work", name)
        shutil.rmtree(work, ignore_errors=True)
        jdir, rdir, adir, pdir = (os.path.join(work, d) for d in ("json", "raw", "archive", "packed"))
        base = rf"{MOD_ROOT}\vehicles\{name}"
        paths = {
            "ent": rf"{base}\{name}.ent",
            "app": rf"{base}\{name}.app",
            "rig": rf"{base}\rig\{name}.rig",
            "vehicle_rig": rf"{base}\rig\{name}_wheels.rig",
            "anims": rf"{base}\anims\{name}.anims",
            "phys": rf"{base}\{name}.phys",
            "paint": rf"{base}\textures\{name}_paint.mlsetup",
            "loc": rf"{base}\localization\en-us.json",
        }

        def jpath(depot_path):
            return os.path.join(jdir, depot_path.replace("\\", os.sep) + ".json")

        # ---------------------------------------------------------- donor bbox (Root space)
        body_tpl = self.cache.load(prof["mesh_template"])
        donor_body = self.cache.load(prof.get("body_mesh") or prof["mesh_template"])
        bb = donor_body["Data"]["RootChunk"]["boundingBox"]
        dz = 0.44                    # the donor body hangs from Base, 0.44 m above Root in every donor rig
        donor_bbox = ((bb["Min"]["X"], bb["Min"]["Y"], bb["Min"]["Z"] + dz), (bb["Max"]["X"], bb["Max"]["Y"], bb["Max"]["Z"] + dz))

        # ---------------------------------------------------------- rigs + animations
        rig_doc, vrig_doc, frames, alignments = build_rigs(
            v, donor_rig, self.cache.load(prof["vehicle_rig"]), prof, donor_bbox)
        # ---------------------------------------------------------- TweakDB record + quality checks
        record = f"Vehicle.{name}"
        spawn_look = name if v.neon is None or v.neon.default == "off" else f"{name}_neon_{v.neon.default}"
        name_key = f"{MOD_ROOT}_{name}_name"
        tweak_text, hp = build_tweak(v, prof, record=record, ent_path=paths["ent"], appearance=spawn_look,
                                     name_key=name_key, handling=self.profile["handling"], sound=sound,
                                     destroyed_appearance=name, seating=v.seat_fit)
        from . import qc as qc_mod
        from .cp77.tweak import camera_class
        qc_facts = qc_mod.car_facts(v, donor, prof, bindings, frames, tweak_text, hp,
                                    camera=camera_class(v), profile=self.profile)
        qc_result = qc_mod.check_car({"qc": qc_facts, "version": VERSION})
        for chk in qc_result["checks"]:
            if not chk["ok"]:
                log(f"[{name}] QC {'FAIL' if chk['level'] == 'error' else 'warn'} {chk['id']}: {chk['message']}")
        if dry:
            return {"name": name, "record": record, "donor": donor, "qc": qc_facts, "qc_result": qc_result,
                    "warnings": list(v.warnings), "seconds": round(time.time() - t0, 1)}
        save(rig_doc, jpath(paths["rig"]))
        save(vrig_doc, jpath(paths["vehicle_rig"]))
        anims = copy.deepcopy(self.cache.load(prof["anims"]))
        anims["Data"]["RootChunk"]["rig"] = depot(paths["rig"])
        save(anims, jpath(paths["anims"]))

        # ---------------------------------------------------------- paint layer
        paint_doc, paint_note = self._paint_setup(v, prof)
        save(paint_doc, jpath(paths["paint"]))

        # ---------------------------------------------------------- textures
        tex_paths = {}
        tex_sizes = {}
        stems = {}

        def tex_path(tname, usage):
            key = (tname, usage)
            if key not in tex_paths:
                stem = _sanitize(tname) + ("_n" if usage == "normal" else "")
                if stems.get(stem, key) != key:
                    # two GTA names that sanitise alike ("Body-Detail", "body_detail") keep their own file
                    stem += "_" + hashlib.sha1(tname.encode()).hexdigest()[:6]
                stems[stem] = key
                p = rf"{base}\textures\{stem}.xbm"
                doc = xbm_document(v.textures[tname], usage)
                save(doc, jpath(p))
                tex_paths[key] = p
                tex_sizes[p] = len(v.textures[tname].data)
            return tex_paths[key]

        mats = {}
        for key, m in v.materials.items():
            mats[key] = material_json(m, tex_path, paths["paint"], prof["paint_mask"])

        # ---------------------------------------------------------- meshes
        part_bone = {}
        for part, pp in v.parts.items():
            if not pp.slot:
                continue                          # fixed to the body (or a parent part): no bone frame of its own
            if part.startswith("door_"):
                part_bone[part] = f"door_{SIDE_NAMES[part.split('_')[1]]}_a"
            elif part in ("hood_a", "trunk_a"):
                part_bone[part] = part
        mesh_paths = {}
        neon_meshes = {}
        os.makedirs(rdir, exist_ok=True)
        for pname, part in v.parts.items():
            if part.kind == "neon":
                # one small mesh per colour (each with its own glowing material)
                for key, _, rgb in v.neon.colors:
                    mp = rf"{base}\meshes\neon\neon_{key}.mesh"
                    neon_meshes[key] = mp
                    mname = f"neon_{key}"
                    doc = template_doc(body_tpl, {mname: neon_material(rgb)}, [mname])
                    save(doc, jpath(mp))
                    glb = os.path.join(rdir, mp.replace("\\", os.sep)[:-5] + ".glb")
                    os.makedirs(os.path.dirname(glb), exist_ok=True)
                    tube = copy.copy(part)
                    tube.submeshes = [copy.copy(sm) for sm in part.submeshes]
                    for sm in tube.submeshes:
                        sm.material = mname
                    write_glb(tube, glb)
                continue
            mp = rf"{base}\meshes\{pname}.mesh"
            mesh_paths[pname] = mp
            subs = ordered_submeshes(part)
            used = []
            for s in subs:
                if s.material not in used:
                    used.append(s.material)
            doc = template_doc(body_tpl, {k: mats[k] for k in used}, [s.material for s in subs])
            save(doc, jpath(mp))
            q = alignments.get(part_bone.get(pname))
            glb = os.path.join(rdir, mp.replace("\\", os.sep)[:-5] + ".glb")
            os.makedirs(os.path.dirname(glb), exist_ok=True)
            write_glb(part, glb, transform=q)

        # ---------------------------------------------------------- app / ent / phys / localisation
        save(app_mod.build_app(v, self.cache.load(prof["app"]), mesh_paths, app_name="default",
                               neon_meshes=neon_meshes), jpath(paths["app"]))
        if v.seat_fit is not None:
            sf = v.seat_fit
            low = [f"{k} {100 * i['lowered']:.0f} cm lower" for k, i in sf.seats.items() if i.get("lowered")]
            log(f"[{name}] seating: {sf.template} ({sf.template_reason})" + (f"; {', '.join(low)}" if low else ""))
            for note in sf.notes:
                log(f"[{name}] getting out: {note}")
        if v.neon is not None:
            hs = sorted({round(100 * l.height) for l in v.neon.lights})
            up = f"{hs[0]}-{hs[-1]}" if len(hs) > 1 else f"{hs[0]}"
            log(f"[{name}] neon kit: {', '.join(f'{t.side} {t.length:.2f} m' for t in v.neon.tubes)}; "
                f"{len(v.neon.colors)} colours, starts {v.neon.default}; {len(v.neon.lights)} soft lights "
                f"{up} cm up wash the road {v.neon.spread:.1f} m around the car")
        save(ent_mod.build_ent(v, donor_ent, paths, name, donor_bbox), jpath(paths["ent"]))
        save(build_phys(v, self.cache.load(prof["phys"])), jpath(paths["phys"]))
        save(package.localization_doc([(name_key, v.display_name)]), jpath(paths["loc"]))

        # ---------------------------------------------------------- WolvenKit: JSON -> CR2W, glb import
        self._step(f"[{name}] writing game files with WolvenKit...")
        self.wk.deserialize_tree(jdir, adir, work)
        self.wk.import_glb(rdir, adir)
        self._check_outputs(adir, list(paths.values()) + list(mesh_paths.values()) + list(neon_meshes.values())
                            + list(tex_sizes))

        # ---------------------------------------------------------- split textures into archives <= budget
        groups = [[]]
        size = 0
        for p in sorted(tex_sizes):
            sz = os.path.getsize(os.path.join(adir, p.replace("\\", os.sep)))
            if groups[-1] and size + sz > TEXTURE_PACKAGE_BUDGET:
                groups.append([])
                size = 0
            groups[-1].append(p)
            size += sz
        archive_name = f"{MOD_ROOT}_{name}"
        archives = {archive_name: [p for p in self._all_files(adir) if p not in tex_sizes]}
        # small cars: textures stay in the main archive
        total_tex = sum(os.path.getsize(os.path.join(adir, p.replace("\\", os.sep))) for p in tex_sizes)
        if total_tex <= TEXTURE_PACKAGE_BUDGET:
            archives[archive_name] += sorted(tex_sizes)
        else:
            for i, g in enumerate(groups, 1):
                archives[f"{archive_name}_tex{i}"] = g
        if len(archives) > package.MAX_PACKAGES:
            raise RuntimeError(f"{len(archives)} packages needed (Open77 allows {package.MAX_PACKAGES}); "
                               "textures are too large for one resource")
        packed = {}
        for aname, files in archives.items():
            src_root = os.path.join(work, "pack", aname)
            for p in files:
                s = os.path.join(adir, p.replace("\\", os.sep))
                d = os.path.join(src_root, p.replace("\\", os.sep))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copyfile(s, d)
            self._step()
            self.wk.pack(src_root, pdir)
            out = os.path.join(pdir, aname + ".archive")
            if not os.path.exists(out):
                raise RuntimeError(f"WolvenKit did not produce {aname}.archive")
            package.normalize_archive(out)   # same input -> same bytes -> same required-mod hash
            packed[aname] = out

        # ---------------------------------------------------------- tweaks, .xl, zips, resource
        tweak_file = os.path.join(work, f"{name}.yaml")
        with open(tweak_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(tweak_text)
        xl_file = os.path.join(work, f"{archive_name}.archive.xl")
        with open(xl_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(package.archive_xl(paths["loc"]))

        res_dir = os.path.join(self.out_dir, name)
        shutil.rmtree(res_dir, ignore_errors=True)
        zips = []
        for i, (aname, apath) in enumerate(packed.items()):
            files = {f"archive/pc/mod/{aname}.archive": apath}
            if i == 0:
                files[f"archive/pc/mod/{archive_name}.archive.xl"] = xl_file
                files[f"r6/tweaks/{MOD_ROOT}/{name}.yaml"] = tweak_file
            zname = f"{name}.zip" if i == 0 else f"{name}_{aname[len(archive_name) + 1:]}.zip"
            zsize = package.write_zip(os.path.join(res_dir, "dist", zname), files)
            if zsize > package.MAX_PACKAGE:
                raise RuntimeError(f"{zname} is {zsize / 1048576:.1f} MiB, over Open77's 64 MiB preload limit")
            zips.append((zname, zsize))

        report = {
            "tool": "fivem2open77", "version": VERSION, "resource_version": self.version,
            "seconds": round(time.time() - t0, 1),
            "source": {"model": src.model, "resource": os.path.basename(src.resource_dir or ""),
                       "yft": os.path.basename(src.yft)},
            "record": record, "appearance": name, "display_name": v.display_name, "donor": prof["record"],
            "packages": [{"file": z, "bytes": s} for z, s in zips],
            "handling": {k: (list(x) if isinstance(x, tuple) else x) for k, x in hp.items()},
            "paint": paint_note,
            "parts": v.report.get("parts"), "textures_used": len(v.textures),
            "textures_missing": v.report.get("textures_missing"),
            "lights": [l.name for l in v.lights],
            "sound": {k: sound[k] for k in ("player", "traffic", "label", "source")},
            "gta_sound": v.audio_name,
            "lids": lids,
            "seats": v.seat_fit.summary() if v.seat_fit is not None else None,
            "neon": None if v.neon is None else dict(
                v.neon.summary(), appearances={key: f"{name}_neon_{key}" for key, _, _ in v.neon.colors},
                labels={key: label for key, label, _ in v.neon.colors}, base_appearance=name),
            "profile": self.profile,
            "warnings": v.warnings,
            "qc": qc_facts,
            "qc_result": {"ok": qc_result["ok"], "failed": [c["id"] for c in qc_result["checks"]
                                                            if not c["ok"] and c["level"] == "error"]},
        }
        chime = self.profile["chime"]
        chime_file = chime["file"] if chime["enabled"] else None
        load = package.write_resource(res_dir, name=name, version=self.version, display=v.display_name, record=record,
                                      packages=[z for z, _ in zips], door_chime=chime_file,
                                      chime_volume=chime["volume"], chime_range=chime["range"], report=report,
                                      appearance=name, neon=report["neon"])
        if not self.keep_work:
            shutil.rmtree(work, ignore_errors=True)
            try:
                os.rmdir(os.path.dirname(work))      # _work, when no other car's files are in it
            except OSError:
                pass
        dirs = [res_dir] + ([package.chime_dir_for(res_dir)] if chime_file else [])
        log(f"[{name}] done in {report['seconds']}s -> {', '.join(dirs)}  (spawn with /car {record})")
        log(f"[{name}] resources.load needs: {', '.join(json.dumps(n) for n in load)}")
        return {"name": name, "record": record, "resource": res_dir, "resources": dirs, "load": load,
                "report": report}

    # ------------------------------------------------------------------ helpers
    def _paint_setup(self, v, prof):
        """One-layer multilayer setup from the donor's paint layer, its colour matched to the
        FiveM car's primary colour when the donor's material template is cached."""
        doc = copy.deepcopy(self.cache.load(prof["paint_setup"]))
        root = doc["Data"]["RootChunk"]
        root["layers"] = root["layers"][:1]
        note = {"layer_template": value_of(root["layers"][0].get("material"))}
        tpl_path = prof.get("paint_template")
        if tpl_path and self.cache.has(tpl_path):
            tpl = self.cache.load(tpl_path)["Data"]["RootChunk"]
            table = {value_of(c["n"]): c["v"]["Elements"] for c in tpl.get("overrides", {}).get("colorScale", [])}
            target = _srgb_to_linear(v.paint_colors[0])
            best = min(table, key=lambda k: float(np.sum((np.asarray(table[k][:3], float) - target) ** 2)))
            root["layers"][0]["colorScale"] = {"$type": "CName", "$storage": "string", "$value": best}
            note.update({"primary_srgb": [round(x, 3) for x in v.paint_colors[0]], "colorScale": best,
                         "matched_linear": [round(x, 3) for x in table[best][:3]]})
        else:
            note["colorScale"] = value_of(root["layers"][0].get("colorScale"))
            note["warning"] = "paint template not cached; kept the donor's paint colour"
        return doc, note

    @staticmethod
    def _all_files(root):
        out = []
        for dp, _, fns in os.walk(root):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), root)
                out.append(rel.replace(os.sep, "\\"))
        return sorted(out)

    @staticmethod
    def _check_outputs(adir, depot_paths):
        missing = [p for p in depot_paths if not os.path.exists(os.path.join(adir, p.replace("\\", os.sep)))]
        if missing:
            raise RuntimeError("WolvenKit did not write: " + ", ".join(missing[:10]))
