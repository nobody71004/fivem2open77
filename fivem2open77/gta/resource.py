"""Discovery of vehicles inside a FiveM resource (folder or archive).

A FiveM add-on vehicle resource normally looks like::

    mycar/
      fxmanifest.lua | __resource.lua
      stream/ mycar.yft  mycar_hi.yft  mycar.ytd  [extra .ytd / mod-kit .yft]
      data/   vehicles.meta handling.meta carvariations.meta carcols.meta ...

Packs may contain several cars and several resources.  We do not require the manifest:
metas are found by declaration first and by name second.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field

from . import rpf
from .gxt2 import parse_gxt2, parse_lua_text_entries
from .meta import (parse_carcols, parse_carvariations, parse_handling_meta,
                   parse_vehicles_meta)
from .rsc7 import GEN9_VERSIONS, EscrowProtectedError

STREAM_EXTS = (".yft", ".ytd", ".ydr", ".ydd")
META_KINDS = {
    "VEHICLE_METADATA_FILE": "vehicles",
    "HANDLING_FILE": "handling",
    "VEHICLE_VARIATION_FILE": "carvariations",
    "CARCOLS_FILE": "carcols",
    "VEHICLE_LAYOUTS_FILE": "layouts",
}
# declared files the conversion does not read: a pack that forgot one is not worth a warning
UNUSED_METAS = {"layouts", "carcols"}


class ResourceInputError(Exception):
    pass


@dataclass
class VehicleSource:
    model: str
    yft: str                    # path to the drawable fragment used for conversion (_hi preferred)
    yft_lod: str | None         # the non-hi yft when both exist and differ
    ytds: list                  # candidate texture dictionaries, primary first
    info: object | None         # VehicleModelInfo
    handling: object | None
    variation: object | None
    resource_dir: str
    warnings: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)   # text label hash -> display text
    pack_name: str = ""                          # name of the download (folder or archive)
    mods: list = field(default_factory=list)     # [(meta.VisibleMod, yft path)] of the car's mod kits
    meta: str | None = None                      # the vehicles.meta that lists the car
    # the other texture dictionaries of the car's resource: read only for a texture the car's own
    # dictionaries lack (a pack has dozens of cars' worth of them)
    ytd_extra: list = field(default_factory=list)


@dataclass
class ScannedResource:
    root: str
    manifests: list
    vehicles: list
    meta_files: dict
    stream_files: list
    txd_parents: dict
    warnings: list = field(default_factory=list)
    tmpdir: str | None = None
    # (resource folder or None for the whole input, message): what each warning is about
    scoped_warnings: list = field(default_factory=list)

    def resource_of(self, path: str) -> str | None:
        """The resource folder (the nearest one with a manifest) a file belongs to."""
        dirs = {os.path.normcase(os.path.dirname(m)) for m in self.manifests}
        d = os.path.normcase(os.path.dirname(os.path.abspath(path)))
        while True:
            if d in dirs:
                return d
            parent = os.path.dirname(d)
            if parent == d or not d.startswith(os.path.normcase(os.path.abspath(self.root))):
                return None
            d = parent

    def warnings_for(self, src) -> list:
        """The warnings about one car of a pack: its own resource's and the input's, with the others
        counted rather than listed (a 70-car pack is 300 lines about cars nobody picked)."""
        own = self.resource_of(src.yft) if src is not None else None
        mine = [m for d, m in self.scoped_warnings if d is None or d == own]
        others = len(self.scoped_warnings) - len(mine)
        if others:
            mine.append(f"{others} more warning(s) about other cars in this pack (not this one)")
        return mine

    def cleanup(self):
        if self.tmpdir and os.path.isdir(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)


def _extract_archive(path: str) -> str:
    tmp = tempfile.mkdtemp(prefix="fivem2open77_")
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for m in z.infolist():
                target = os.path.realpath(os.path.join(tmp, m.filename))
                if not target.startswith(os.path.realpath(tmp) + os.sep) and target != os.path.realpath(tmp):
                    raise ResourceInputError(f"refusing archive entry outside the target folder: {m.filename}")
            z.extractall(tmp)
        return tmp
    # rar / 7z: use whatever extractor the system has
    tools = []
    # -p<dummy>: a password-protected archive fails at once instead of waiting for a password
    custom = os.environ.get("FIVEM2OPEN77_7Z")
    if custom:
        tools.append([custom, "x", "-y", "-pfivem2open77", f"-o{tmp}", path])
    tools += [
        ["7z", "x", "-y", "-pfivem2open77", f"-o{tmp}", path],
        ["7zz", "x", "-y", "-pfivem2open77", f"-o{tmp}", path],     # 7-Zip for Linux (standalone binary)
        ["7za", "x", "-y", "-pfivem2open77", f"-o{tmp}", path],
        ["bsdtar", "-xf", path, "-C", tmp],
        ["unrar", "x", "-o+", path, tmp + os.sep],
        ["tar", "-xf", path, "-C", tmp],  # Windows 10+ tar.exe is libarchive and reads rar/7z
    ]
    for cmd in tools:
        if shutil.which(cmd[0]) is None and not os.path.isfile(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if any(os.scandir(tmp)):
                return tmp
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue
    shutil.rmtree(tmp, ignore_errors=True)
    raise ResourceInputError(
        f"cannot extract {os.path.basename(path)}: install 7-Zip (7z) or extract it yourself and pass the folder")


_DATA_FILE_RE = re.compile(r"data_file\s*\(?\s*['\"]([A-Z_]+)['\"]\s*\)?\s*\(?\s*['\"]([^'\"]+)['\"]")


def _parse_manifest(path: str) -> list:
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    text = re.sub(r"--\[\[.*?\]\]", "", text, flags=re.S)
    text = re.sub(r"--[^\n]*", "", text)
    return _DATA_FILE_RE.findall(text)


def _find_files(root: str):
    for dp, dn, fn in os.walk(root):
        # skip version-control and tool folders
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            yield os.path.join(dp, f)


_PACK_ID = re.compile(r"^(?:[a-z]?\d{3,}[-_ ]+)", re.I)


def _pack_name(path: str, root: str) -> str:
    """A readable name for the download: its single top folder, else the file/folder name."""
    name = os.path.basename(os.path.normpath(path))
    if os.path.isfile(path):
        name = os.path.splitext(name)[0]
        try:
            tops = [e for e in os.listdir(root) if not e.startswith(".")]
        except OSError:
            tops = []
        if len(tops) == 1 and os.path.isdir(os.path.join(root, tops[0])):
            name = tops[0]
    name = re.sub(r"\s*\(\d+\)$", "", name)          # "car (1)" from a repeated download
    name = _PACK_ID.sub("", name).replace("_", " ").strip()
    return name


def _expand_rpfs(files, root, tmpdir, warnings):
    """Unpack single-player dlc.rpf archives next to the other files (into tmpdir)."""
    out = []
    for f in files:
        if not f.lower().endswith(".rpf"):
            continue
        rel = os.path.relpath(f, root)
        dest = os.path.join(tmpdir, "rpf", rel)
        try:
            out.extend(rpf.extract(f, dest))
        except rpf.RpfError as e:
            warnings.append(str(e))
    return out


def scan_resource(path: str) -> ScannedResource:
    tmpdir = None
    if os.path.isfile(path):
        tmpdir = _extract_archive(path)
        root = tmpdir
    elif os.path.isdir(path):
        root = path
    else:
        raise ResourceInputError(f"input not found: {path}")

    files = list(_find_files(root))
    warnings = []
    scoped = []                                  # (file the warning is about, message)
    pack_name = _pack_name(path, root)
    if any(f.lower().endswith(".rpf") for f in files):
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp(prefix="fivem2open77_")
        files += _expand_rpfs(files, root, tmpdir, warnings)
        scoped += [(None, w) for w in warnings]
    lower = {f.lower(): f for f in files}

    def warn(about, message):
        warnings.append(message)
        scoped.append((about, message))

    if any(f.lower().endswith(".fxap") for f in files):
        raise EscrowProtectedError(
            "this resource is protected by Cfx.re asset escrow (.fxap found). Escrowed assets are "
            "encrypted for one server licence and cannot be converted.")

    manifests = [f for f in files if os.path.basename(f).lower() in ("fxmanifest.lua", "__resource.lua")]
    meta_files = {"vehicles": [], "handling": [], "carvariations": [], "carcols": [], "layouts": []}
    declared = set()
    for man in manifests:
        base = os.path.dirname(man)
        for kind, rel in _parse_manifest(man):
            k = META_KINDS.get(kind)
            if not k:
                continue
            # the folder part is literal: packs name folders "[CARS]", which a glob reads as a
            # character class, so every declared file of such a pack looked missing
            exact = os.path.normpath(os.path.join(base, rel)).lower()
            if exact in lower:
                matched = [lower[exact]]
            elif any(c in rel for c in "*?["):
                pattern = os.path.normpath(os.path.join(glob.escape(base), rel)).lower()
                matched = [lower[p] for p in lower if fnmatch.fnmatchcase(p, pattern)]
            else:
                matched = []
            if not matched and k not in UNUSED_METAS:
                warn(man, f"{os.path.relpath(man, root)} declares {kind} '{rel}' but the file is missing")
            for m in matched:
                if m not in meta_files[k]:
                    meta_files[k].append(m)
                    declared.add(m)
    # undeclared metas found by name
    for f in files:
        b = os.path.basename(f).lower()
        if not b.endswith(".meta") or f in declared:
            continue
        for key in ("carvariations", "carcols", "handling", "vehicles"):
            if key in b:
                if key == "vehicles" and ("layout" in b or "variation" in b):
                    continue
                if f not in meta_files[key]:
                    meta_files[key].append(f)
                break

    labels = {}
    gxts = sorted((f for f in files if f.lower().endswith(".gxt2")),
                  key=lambda f: (0 if "american" in f.lower() else 1, f))
    for f in reversed(gxts):                 # American English wins
        try:
            labels.update(parse_gxt2(open(f, "rb").read()))
        except OSError:
            pass
    for f in files:
        if f.lower().endswith(".lua"):
            try:
                labels.update(parse_lua_text_entries(open(f, "r", encoding="utf-8", errors="replace").read()))
            except OSError:
                pass

    stream = [f for f in files if f.lower().endswith(STREAM_EXTS)]
    by_name = {}
    for f in stream:
        by_name.setdefault(os.path.basename(f).lower(), []).append(f)

    infos, txd_parents, info_src = [], {}, {}
    for mf in meta_files["vehicles"]:
        try:
            items, rel = parse_vehicles_meta(open(mf, "rb").read(), mf)
            infos.extend(items)
            for it in items:
                info_src.setdefault(it.model_name.lower(), mf)
            txd_parents.update(rel)
        except Exception as e:  # noqa: BLE001 - report and continue
            warn(mf, f"cannot parse {os.path.relpath(mf, root)}: {e}")
    handlings = {}
    for mf in meta_files["handling"]:
        try:
            handlings.update(parse_handling_meta(open(mf, "rb").read(), mf))
        except Exception as e:  # noqa: BLE001
            warn(mf, f"cannot parse {os.path.relpath(mf, root)}: {e}")
    variations = {}
    for mf in meta_files["carvariations"]:
        try:
            variations.update(parse_carvariations(open(mf, "rb").read(), mf))
        except Exception as e:  # noqa: BLE001
            warn(mf, f"cannot parse {os.path.relpath(mf, root)}: {e}")
    kits = {}
    for mf in meta_files["carcols"]:
        try:
            for k, mods in parse_carcols(open(mf, "rb").read(), mf).get("kits", {}).items():
                kits.setdefault(k, []).extend(mods)
        except Exception as e:  # noqa: BLE001
            warn(mf, f"cannot parse {os.path.relpath(mf, root)}: {e}")

    vehicles = []
    seen = set()
    versions = {}

    def gen9(f):
        """A GTA V Enhanced (gen9) resource: FiveM streams legacy ones, and this converter reads
        those only.  Packs ship both ("Legacy\\..." and "Enhanced\\..."): the legacy copy is used."""
        if f not in versions:
            try:
                with open(f, "rb") as fh:
                    head = fh.read(8)
                versions[f] = int.from_bytes(head[4:8], "little") if head[:4] == b"RSC7" else None
            except OSError:
                versions[f] = None
        return versions[f] in GEN9_VERSIONS

    def legacy(cands):
        """The legacy files of a list, or the list itself when there are none (its error then says
        what the file is)."""
        if not cands:
            return cands
        keep = [c for c in cands if not gen9(c)]
        return keep or cands

    legacy_names = {os.path.basename(f).lower() for f in stream if f.lower().endswith(".ytd") and not gen9(f)}
    all_ytds = [f for f in stream if f.lower().endswith(".ytd")
                and not (gen9(f) and os.path.basename(f).lower() in legacy_names)]

    man_dirs = {os.path.normcase(os.path.dirname(os.path.abspath(m))) for m in manifests}
    res_cache = {}

    def resource_dir(f):
        """The nearest folder with a manifest above f (None outside them, or when the input is one
        resource: then every file is the car's)."""
        if len(man_dirs) < 2:
            return None
        d = os.path.normcase(os.path.dirname(os.path.abspath(f)))
        if d not in res_cache:
            walk, found = d, None
            while True:
                if walk in man_dirs:
                    found = walk
                    break
                up = os.path.dirname(walk)
                if up == walk:
                    break
                walk = up
            res_cache[d] = found
        return res_cache[d]

    def near(cands, anchor):
        """The candidate that shares the longest path with anchor (the meta that names the car):
        a pack with both a replace folder and an add-on dlc.rpf uses the add-on's own files.  Legacy
        files win over gen9 ones whatever the path."""
        cands = legacy(cands)
        if not cands:
            return None
        if not anchor or len(cands) == 1:
            return cands[0]
        a = os.path.normcase(os.path.abspath(anchor))
        return max(cands, key=lambda c: len(os.path.commonpath([a, os.path.normcase(os.path.abspath(c))])))

    def make(model, info):
        m = model.lower()
        anchor = info_src.get(m)
        hi = near(by_name.get(m + "_hi.yft"), anchor)
        base = near(by_name.get(m + ".yft"), anchor)
        if not hi and not base:
            return None
        yft = hi or base
        yft_lod = base if hi and base and os.path.getsize(hi) != os.path.getsize(base) else None
        txd = (info.txd_name if info else model).lower()
        ytds = []
        chain = [txd]
        # follow txd relationship chain upwards (children inherit parents)
        p = txd_parents.get(txd)
        while p and p not in chain and len(chain) < 8:
            chain.append(p)
            p = txd_parents.get(p)
        for t in chain:
            group = legacy(by_name.get(t + ".ytd", [])) + legacy(by_name.get(t + "+hi.ytd", []))
            if anchor:
                group = sorted(group, key=lambda c: -len(os.path.commonpath(
                    [os.path.normcase(os.path.abspath(anchor)), os.path.normcase(os.path.abspath(c))])))
            for f in group:
                if f not in ytds:
                    ytds.append(f)
        # any other ytd of the car's resource can hold textures the model references: kept aside
        # and read only for a texture the car's own dictionaries lack
        own = resource_dir(yft)
        extra = [f for f in all_ytds if f not in ytds and resource_dir(f) == own]
        handling = handlings.get((info.handling_id if info else model).lower())
        res_dir = os.path.dirname(os.path.dirname(yft)) if os.path.basename(os.path.dirname(yft)).lower() == "stream" else os.path.dirname(yft)
        variation = variations.get(m)
        mods, seen_mods = [], set()
        for kit in (variation.kits if variation else []):
            for mod in kits.get(kit.lower(), []):
                key = (mod.model.lower(), mod.kind)
                path = near(by_name.get(mod.model.lower() + ".yft"), anchor)
                if key in seen_mods or not path:
                    continue
                seen_mods.add(key)
                mods.append((mod, path))
        v = VehicleSource(model=model, yft=yft, yft_lod=yft_lod, ytds=ytds, info=info, handling=handling,
                          variation=variation, resource_dir=res_dir, labels=labels, pack_name=pack_name, mods=mods,
                          meta=anchor, ytd_extra=extra)
        if info is None:
            v.warnings.append("no vehicles.meta entry; using defaults")
        if handling is None:
            v.warnings.append("no handling.meta entry; using the donor car's handling")
        return v

    for info in infos:
        if info.model_name.lower() in seen:
            continue
        v = make(info.model_name, info)
        if v is None:
            warn(info_src.get(info.model_name.lower()),
                 f"vehicles.meta lists '{info.model_name}' but no {info.model_name}.yft is streamed")
            continue
        seen.add(info.model_name.lower())
        vehicles.append(v)
    if not infos:
        # no metas: every top-level yft (not _hi, not mod-kit parts) is a vehicle
        models = [n[:-4] for n in sorted(by_name) if n.endswith(".yft") and not n.endswith("_hi.yft")]

        def kit_part(model):
            # mycar_bumf_1 next to mycar.yft, at any depth of underscores
            parts = model.split("_")
            return any("_".join(parts[:i]) + ".yft" in by_name for i in range(1, len(parts)))

        models = [m for m in models if not kit_part(m)]
        # a car has its own texture dictionary or a _hi model; mod-kit parts borrowed from another
        # car's kit (sturbo_bumf_1 in a Camaro) have neither
        owned = [m for m in models if m + ".ytd" in by_name or m + "_hi.yft" in by_name]
        for model in owned or models:
            v = make(model, None)
            if v:
                vehicles.append(v)

    res = ScannedResource(root=root, manifests=manifests, vehicles=vehicles, meta_files=meta_files,
                          stream_files=stream, txd_parents=txd_parents, warnings=warnings, tmpdir=tmpdir)
    res.scoped_warnings = [(res.resource_of(f) if f else None, m) for f, m in scoped]
    return res
