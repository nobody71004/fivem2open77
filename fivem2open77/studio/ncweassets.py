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

import os
import re
import shutil
import zipfile

from ..gta.resource import _extract_archive

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
                "archives": len(archives), "ready": bool(archives)}
    size = os.path.getsize(path)
    low = rel.lower()
    if low.endswith(".zip"):
        archives, entries = _zip_archives(path)
        if archives is not None:
            return {"name": rel, "kind": "NCWE export (zip)", "bytes": size, "files": entries,
                    "archives": len(archives), "ready": bool(archives)}
    if low.endswith((".7z", ".rar")):
        return {"name": rel, "kind": "NCWE export (archive)", "bytes": size, "files": None,
                "archives": None, "ready": None}
    if low.endswith(ARCHIVE_SUFFIXES):
        return {"name": rel, "kind": "game archive", "bytes": size, "files": 1, "archives": 1, "ready": True}
    return {"name": rel, "kind": "file", "bytes": size, "files": 1, "archives": 0, "ready": False}


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
    return packages, warnings
