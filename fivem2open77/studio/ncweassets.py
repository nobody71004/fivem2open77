"""NCWE asset imports in map projects: drag-and-drop exports of the NCWE world editor.

An import is one top-level entry in the project's ``assets/ncwe/`` folder: an NCWE export
(`.zip`/`.7z`/`.rar` ArchiveXL mod) or a folder of files dropped on the Studio.  At build time
every import that carries game archives (``archive/pc/mod/*.archive``, or loose ``*.archive`` files
wrapped into that layout) is repacked into a clean preload package and shipped with the map, so
props, lights and meshes made in NCWE load wherever the map is installed.  Imports without game
archives are kept but skipped at build with a note.  Open77 allows 8 preload packages per resource,
the map's own included (the writer enforces the total).

NCWE is a separate editor with its own project: this module only stores and repacks what the user
exports from it.  Nothing here reads or writes NCWE's own project files or account state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import zipfile

from ..cp77 import package as package_mod
from ..gta.resource import _extract_archive
from .qa import fnv1a64

SEGMENT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
ARCHIVE_SUFFIXES = (".archive", ".archive.xl")
MAX_ZIP_ENTRIES = 20000


class AssetError(ValueError):
    pass


def safe_relpath(name: str) -> str:
    """A relative import path (a dropped file keeps its folder's relative path)."""
    name = str(name or "").replace("\\", "/").strip("/")
    parts = [p for p in name.split("/") if p]
    if not parts or len(parts) > 8:
        raise AssetError("name: a relative path like 'myprops.zip' or 'myprops/archive/pc/mod/x.archive'")
    for p in parts:
        if p in (".", "..") or not SEGMENT_RX.match(p):
            raise AssetError(f"name: '{p}' is not allowed (letters, digits, . _ - , no hidden files)")
    return "/".join(parts)


def _root(project) -> str:
    root = project.path("assets", "ncwe")
    os.makedirs(root, exist_ok=True)
    return root


def _zip_archives(path: str) -> tuple:
    """(game archive names, total entries) of a zip import, or (None, 0) when it is not a zip."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return None, 0
    return [n.split("/")[-1] for n in names if n.replace("\\", "/").endswith(".archive")
            and "/archive/pc/mod/" in "/" + n.replace("\\", "/")], len(names)


def describe(root: str, rel: str) -> dict:
    path = os.path.join(root, *rel.split("/"))
    if os.path.isdir(path):
        files, bytes_, archives = 0, 0, []
        for dp, _, fns in os.walk(path):
            for fn in fns:
                files += 1
                fp = os.path.join(dp, fn)
                bytes_ += os.path.getsize(fp)
                if fn.endswith(ARCHIVE_SUFFIXES):
                    archives.append(fn)
        return {"name": rel, "kind": "folder", "bytes": bytes_, "files": files,
                "archives": len(archives), "archive_names": sorted(archives), "ready": bool(archives)}
    size = os.path.getsize(path)
    low = rel.lower()
    if low.endswith(".zip"):
        archives, entries = _zip_archives(path)
        if archives is not None:
            return {"name": rel, "kind": "NCWE export (zip)", "bytes": size, "files": entries,
                    "archives": len(archives), "archive_names": sorted(archives), "ready": bool(archives)}
    if low.endswith((".7z", ".rar")):
        return {"name": rel, "kind": "NCWE export (archive)", "bytes": size, "files": None,
                "archives": None, "archive_names": None, "ready": None}
    if low.endswith(ARCHIVE_SUFFIXES):
        return {"name": rel, "kind": "game archive", "bytes": size, "files": 1,
                "archives": 1, "archive_names": [rel.split("/")[-1]], "ready": True}
    return {"name": rel, "kind": "file", "bytes": size, "files": 1, "archives": 0, "archive_names": [], "ready": False}


def list_imports(project) -> list:
    root = _root(project)
    out = []
    for name in sorted(os.listdir(root)):
        rel = safe_relpath(name)          # names on disk were validated on the way in
        out.append(describe(root, rel))
    return out


def add_import(project, name: str, stream, size: int) -> dict:
    """Store one uploaded file at its drop-relative path under assets/ncwe/."""
    rel = safe_relpath(name)
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise AssetError("upload: missing size") from None
    if size <= 0:
        raise AssetError("upload: empty file")
    if size > MAX_IMPORT_BYTES:
        raise AssetError(f"upload: {size / 1048576:.0f} MiB is over the {MAX_IMPORT_BYTES // 1048576} MiB import limit")
    root = _root(project)
    dst = os.path.join(root, *rel.split("/"))
    if os.path.isdir(dst):
        raise AssetError(f"'{rel}' is an imported folder; drop the file under another name")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    written = 0
    with open(dst, "wb") as f:
        while written < size:
            chunk = stream.read(min(1 << 20, size - written))
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    if written != size:
        os.remove(dst)
        raise AssetError(f"upload: got {written} of {size} bytes")
    return describe(root, rel)


def remove_import(project, name: str) -> str:
    rel = safe_relpath(name)
    if "/" in rel:
        raise AssetError("remove the import it belongs to, not a file inside it")
    path = os.path.join(_root(project), rel)
    if not os.path.exists(path):
        raise AssetError(f"no import named '{rel}'")
    (shutil.rmtree if os.path.isdir(path) else os.remove)(path)
    return rel


def import_path(project, src: str, name: str = "") -> dict:
    """Import a file or a folder from this PC (a folder keeps its paths under one top-level entry)."""
    if os.path.isdir(src):
        top = safe_relpath(name or os.path.basename(src.rstrip("\\/")))
        for dp, _, fns in os.walk(src):
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, src).replace("\\", "/")
                with open(full, "rb") as f:
                    add_import(project, f"{top}/{rel}", f, os.path.getsize(full))
        if not os.path.isdir(os.path.join(_root(project), *top.split("/"))):
            raise AssetError(f"'{src}' holds no files to import")
        return describe(_root(project), top)
    if not os.path.isfile(src):
        raise AssetError(f"no such file or folder: {src}")
    with open(src, "rb") as f:
        return add_import(project, name or os.path.basename(src), f, os.path.getsize(src))


# ------------------------------------------------------------------ what an import holds
def _res_kind(path: str) -> str:
    low = str(path or "").lower().replace("\\", "/")
    if low.endswith(".mesh"):
        return "mesh"
    if low.endswith((".ent", ".app")):
        return "prop"
    if low.endswith(".xbm"):
        return "texture"
    if low.endswith((".wem", ".opus", ".audio")):
        return "sound"
    return "other"


_KIND_TOTAL = {"mesh": "meshes", "prop": "props", "texture": "textures", "sound": "sounds", "other": "other"}


def _archive_info(fp) -> dict:
    """{entries, hashes} from a Cyberpunk archive's index; only the index region is read (cf. qa.archive_hashes)."""
    magic, _ver, index_pos, _size = struct.unpack_from("<4sIQI", fp.read(20))
    if magic != b"RDAR":
        raise ValueError("not a Cyberpunk archive (no RDAR header)")
    fp.seek(index_pos)
    _off, size, crc = struct.unpack_from("<IIQ", fp.read(16))
    table = fp.read(size - 8)
    if package_mod._crc64_xz(table) != crc:
        raise ValueError("archive index checksum mismatch")
    n = struct.unpack_from("<I", table, 0)[0]
    return {"entries": n,
            "hashes": {struct.unpack_from("<Q", table, 12 + i * 56)[0] for i in range(n)}}


def _xl_resources(data: bytes) -> dict:
    """archive name -> [resource path] from an ArchiveXL descriptor (best effort)."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return {}
    out = {}
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, list):
                paths = v
            elif isinstance(v, dict):
                paths = v.get("resources") or v.get("entries") or []
            else:
                paths = []
            key = str(k).replace("\\", "/").split("/")[-1].lower()
            out[key] = [str(x) for x in paths if isinstance(x, str)]
    return out


MAX_CONTENT_ITEMS = 300


def _breakdown(entries) -> tuple:
    """[(relative path, open())] -> (game archives with entry counts and .xl resources, other files)."""
    xl, archs, files = {}, [], []
    for rel, opener in entries:
        rel = rel.replace("\\", "/")
        low = rel.lower()
        if low.endswith(".archive.xl"):
            with opener() as f:
                for a, paths in _xl_resources(f.read(1 << 20)).items():
                    xl.setdefault(a, []).extend(paths)
        elif low.endswith(ARCHIVE_SUFFIXES):
            info = {"name": rel.split("/")[-1], "path": rel, "entries": None, "resources": [], "error": None}
            try:
                with opener() as f:
                    idx = _archive_info(f)
                info["entries"] = idx["entries"]
                info["_hashes"] = idx["hashes"]
            except (OSError, ValueError, EOFError, struct.error) as e:
                info["error"] = str(e)
            archs.append(info)
        else:
            files.append({"path": rel, "kind": _res_kind(rel)})
    for a in archs:
        hashes = a.pop("_hashes", None)
        paths = xl.pop(a["name"].lower(), [])
        a["resources"] = [{"path": p, "kind": _res_kind(p),
                           "present": (fnv1a64(p.lower()) in hashes or fnv1a64(p) in hashes) if hashes is not None else None}
                          for p in paths[:MAX_CONTENT_ITEMS]]
    for key in sorted(xl):                    # a descriptor naming an archive the import does not carry
        archs.append({"name": key, "path": None, "entries": None,
                      "error": "listed by an .archive.xl but not in this import",
                      "resources": [{"path": p, "kind": _res_kind(p), "present": None}
                                    for p in xl[key][:MAX_CONTENT_ITEMS]]})
    return archs[:MAX_CONTENT_ITEMS], files[:MAX_CONTENT_ITEMS]


def contents(project, name: str) -> dict:
    """What one import holds: game archives (names, entry counts, the resources its .archive.xl lists)
    and its other files.  Archive indices hash their entry names, so mesh/prop names come from the
    mod's own .archive.xl descriptors (verified against the index when possible)."""
    rel = safe_relpath(name)
    root = _root(project)
    path = os.path.join(root, *rel.split("/"))
    if not os.path.exists(path):
        raise AssetError(f"no import named '{rel}'")
    notes = []
    if os.path.isdir(path):
        archs, files = _breakdown((r, lambda f=f: open(f, "rb")) for r, f in _iter_files(path))
    elif rel.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            members = [n for n in z.namelist()[:MAX_ZIP_ENTRIES] if not n.endswith("/")]
            archs, files = _breakdown((n.replace("\\", "/"), lambda n=n: z.open(n)) for n in members)
        if not _zip_archives(path)[0]:
            notes.append("no game archives under archive/pc/mod/; skipped at build")
    elif rel.lower().endswith((".7z", ".rar")):
        tmp = _extract_archive(path)
        try:
            archs, files = _breakdown((r, lambda f=f: open(f, "rb")) for r, f in _iter_files(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        archs, files = _breakdown([(rel, lambda: open(path, "rb"))])
        if not rel.lower().endswith(ARCHIVE_SUFFIXES):
            notes.append("not an NCWE export or game archive; skipped at build")
    totals = {}
    for a in archs:
        for r in a["resources"]:
            totals[_KIND_TOTAL[r["kind"]]] = totals.get(_KIND_TOTAL[r["kind"]], 0) + 1
        if not a["resources"]:
            totals["entries"] = totals.get("entries", 0) + (a["entries"] or 0)
    for f in files:
        totals[_KIND_TOTAL[f["kind"]]] = totals.get(_KIND_TOTAL[f["kind"]], 0) + 1
    return {"name": rel, "archives": archs, "files": files, "totals": totals, "notes": notes}


def _raw_archive_names(src: str) -> list:
    """Archive names one import would ship at build, duplicates preserved (mirrors prepare's layout rules)."""
    low = src.replace("\\", "/").split("/")[-1].lower()
    names = []
    if os.path.isdir(src):
        for rel, _ in _iter_files(src):
            arc = _norm_entry(rel)
            if arc and arc.endswith(ARCHIVE_SUFFIXES):
                names.append(arc.split("/")[-1])
    elif low.endswith(".zip"):
        if _zip_archives(src)[0]:             # prepare skips zips with no archive/pc/mod game archives
            with zipfile.ZipFile(src) as z:
                for n in z.namelist()[:MAX_ZIP_ENTRIES]:
                    if n.endswith("/"):
                        continue
                    arc = _norm_entry(n)
                    if arc and arc.endswith(ARCHIVE_SUFFIXES):
                        names.append(arc.split("/")[-1])
    elif low.endswith(ARCHIVE_SUFFIXES):
        names.append(low)
    # .7z/.rar: names are known when prepare unpacks them (build / ncwe_assets_verify)
    return names


def collisions(project) -> list:
    """Archive name clashes to fix before building: one name shipped by several imports (the game
    loads only one of them) or a name starting with fivem2open77_map_ (map resources' own archives)."""
    root = _root(project)
    seen = {}
    for name in sorted(os.listdir(root)):
        rel = safe_relpath(name)
        for arc in _raw_archive_names(os.path.join(root, *rel.split("/"))):
            seen.setdefault(arc.lower(), []).append(rel)
    out = []
    for arc, srcs in sorted(seen.items()):
        where = ", ".join(sorted(set(srcs)))
        if len(srcs) > 1:
            more = " (twice in the same import)" if len(set(srcs)) < len(srcs) else ""
            out.append({"archive": arc, "imports": sorted(set(srcs)),
                        "problem": f"{arc}: ships with {where}{more} — the game loads only one of them; "
                                   "rename one before building"})
        if arc.startswith("fivem2open77_map_"):
            out.append({"archive": arc, "imports": sorted(set(srcs)),
                        "problem": f"{arc}: names starting with fivem2open77_map_ belong to map resources; "
                                   f"rename the archive in {where} before building"})
    return out


def fingerprint(project) -> str:
    """Cheap stable fingerprint of the imports (names, sizes, mtimes): a build records it so the
    Studio can say the build is stale when the imports change afterwards."""
    root = _root(project)
    h = hashlib.sha256()

    def add(rel, st):
        h.update(f"{rel}\0{st.st_size}\0{int(st.st_mtime_ns)}\n".encode("utf-8"))

    for name in sorted(os.listdir(root)):
        rel = safe_relpath(name)
        path = os.path.join(root, *rel.split("/"))
        if os.path.isdir(path):
            for r, f in _iter_files(path):
                add(rel + "/" + r, os.stat(f))
        else:
            add(rel, os.stat(path))
    return h.hexdigest()[:16]


def build_stale(project, build: dict) -> str | None:
    """Why a build record no longer matches the project's imports (None: it still does)."""
    if build is None:
        return None          # nothing built yet: nothing to be stale
    if (build or {}).get("imports") == fingerprint(project):
        return None
    if (build or {}).get("imports") is None and not os.listdir(_root(project)):
        return None          # a build from before import tracking, and nothing was ever imported
    return "NCWE imports changed since this build"


# ------------------------------------------------------------------ build-time packaging
def _norm_entry(path: str):
    """Map an import's file onto the package layout: archive/pc/mod/<game archive>, else None."""
    p = path.replace("\\", "/").lstrip("/")
    marker = "archive/pc/mod/"
    i = p.find(marker)
    if i >= 0:
        return p[i:]
    if p.endswith(ARCHIVE_SUFFIXES) and "/" not in p:
        return "archive/pc/mod/" + p
    return None


def _iter_files(root: str):
    for dp, _, fns in sorted(os.walk(root)):
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            yield os.path.relpath(full, root).replace("\\", "/"), full


def _make_package(entries, out_zip: str):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in sorted(entries):
            z.write(src, arc)


def _collect_dir(root: str, out_zip: str):
    """Repack a folder import into a clean package; -> (game archives, skipped count)."""
    entries, skipped = [], 0
    for rel, full in _iter_files(root):
        arc = _norm_entry(rel)
        if arc:
            entries.append((arc, full))
        else:
            skipped += 1
    if not entries:
        return [], skipped
    _make_package(entries, out_zip)
    return sorted({arc.split("/")[-1] for arc, _ in entries if arc.endswith(".archive")}), skipped


def prepare(assets_dir: str, staging_dir: str, log=print) -> tuple:
    """Repack every import that carries game archives -> ([{zip, source, archives}], warnings)."""
    packages, warnings = [], []
    if not os.path.isdir(assets_dir):
        return packages, warnings
    os.makedirs(staging_dir, exist_ok=True)
    for i, name in enumerate(sorted(os.listdir(assets_dir)), 1):
        src = os.path.join(assets_dir, name)
        out_zip = os.path.join(staging_dir, f"import-{i}.zip")
        archives = []
        try:
            if os.path.isdir(src):
                archives, skipped = _collect_dir(src, out_zip)
                if skipped:
                    warnings.append(f"{name}: {skipped} file(s) are not game archives and were left out")
            elif name.lower().endswith(".zip"):
                archives, entries = _zip_archives(src)
                if archives:
                    with zipfile.ZipFile(src) as z, zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
                        for n in z.namelist()[:MAX_ZIP_ENTRIES]:
                            arc = _norm_entry(n)
                            if arc and not n.endswith("/"):
                                out.writestr(arc, z.read(n))
                    archives = sorted({a.split("/")[-1] for a in archives})
                else:
                    warnings.append(f"{name}: no game archives (archive/pc/mod/*.archive); skipped")
            elif name.lower().endswith((".7z", ".rar")):
                tmp = _extract_archive(src)
                try:
                    archives, skipped = _collect_dir(tmp, out_zip)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                if not archives:
                    warnings.append(f"{name}: no game archives (archive/pc/mod/*.archive); skipped")
            elif name.lower().endswith(ARCHIVE_SUFFIXES):
                with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as out:
                    out.write(src, "archive/pc/mod/" + name)
                archives = [name]
            else:
                warnings.append(f"{name}: not an NCWE export (.zip/.7z/.rar) or game archive; skipped")
        except (OSError, zipfile.BadZipFile) as e:
            warnings.append(f"{name}: could not be read ({e}); skipped")
            archives = []
        if not archives:
            continue
        packages.append({"zip": out_zip, "source": name, "archives": archives})
        log(f"[ncwe assets] {name} -> {len(archives)} game archive(s)")
    seen = {}
    for pkg in packages:
        for a in pkg["archives"]:
            seen.setdefault(a.lower(), []).append(pkg["source"])
    for a, srcs in sorted(seen.items()):
        if len(srcs) > 1:
            warnings.append(f"{a}: ships with {', '.join(srcs)} — the game loads only one of them; "
                            "rename one before building")
        if a.startswith("fivem2open77_map_"):
            warnings.append(f"{a}: names starting with fivem2open77_map_ belong to map resources; "
                            "rename the imported archive before building")
    return packages, warnings
