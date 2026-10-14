"""Bringing a FiveM resource into the Studio, whatever its size.

A small resource is copied into its project, as always.  A city is not: Liberty V is 6.6 GB in
35,000 files (a 6.9 GB zip), so

* a big folder is *linked*: the project reads it where it is (nothing copied; keep it there),
* a big archive is *extracted once* (not on every look and build),
* and the work happens in a job with progress, not inside the HTTP request that asked for it.

What several projects read lives once in the workspace's library: a car pack (one zip, dozens of
cars: the FiveM Civ Car Pack has 68) becomes a project per car, and the districts of a city are
projects that share the city's files.

    <workspace>/library/<id>/entry.json   kind (cars | map), name, where it came from, how it is held,
                                          the cars in it
    <workspace>/library/<id>/src/         the files (copied or extracted; absent when linked)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile

LINK_BYTES = 1 << 30            # a folder this big is read in place
EXTRACT_BYTES = 512 << 20       # an archive this big is extracted once
ARCHIVES = (".zip", ".rar", ".7z")
SPARE_BYTES = 1 << 30           # free space left on the workspace's drive after an import
_ID = re.compile(r"^[a-f0-9]{12}$")


class SourceError(ValueError):
    pass


def measure(path: str) -> tuple:
    """(bytes, files) of a folder or a file."""
    if os.path.isfile(path):
        return os.path.getsize(path), 1
    total = files = 0
    for dp, dn, fn in os.walk(path):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                files += 1
            except OSError:
                pass
    return total, files


def check_source(path: str) -> str:
    """The absolute path of a resource folder or archive, or a SourceError saying what is wrong."""
    path = (path or "").strip().strip('"')
    if not path or not os.path.exists(path):
        raise SourceError(f"not found on this PC: {path}")
    path = os.path.abspath(path)
    if os.path.isfile(path) and not path.lower().endswith(ARCHIVES):
        raise SourceError("a FiveM resource folder, or a .zip / .rar / .7z of one")
    if os.path.isdir(path) and not any(True for _ in os.scandir(path)):
        raise SourceError(f"{path} is an empty folder")
    return path


def plan(path: str, uploaded: bool = False, size_files: tuple | None = None) -> tuple:
    """How to take a source in: (how, bytes, files) with how one of copy | move | link | extract."""
    size, files = size_files or measure(path)
    if os.path.isdir(path):
        return ("link" if size >= LINK_BYTES and not uploaded else "copy"), size, files
    if size >= EXTRACT_BYTES:
        return "extract", size, files
    return ("move" if uploaded else "copy"), size, files


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def unpacked_size(archive: str) -> int:
    """What an archive takes once extracted: exact for a zip, a fair guess for rar / 7z."""
    if archive.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(archive) as z:
                return sum(i.file_size for i in z.infolist())
        except (OSError, zipfile.BadZipFile):
            pass
    return int(os.path.getsize(archive) * 1.3)


def need_space(where: str, nbytes: int, what: str):
    """A SourceError when the drive of `where` cannot take nbytes more (and a spare GB)."""
    try:
        free = shutil.disk_usage(where).free
    except OSError:
        return
    if free < nbytes + SPARE_BYTES:
        drive = os.path.splitdrive(os.path.abspath(where))[0] or os.path.abspath(where)
        raise SourceError(f"not enough free space for {what}: it needs {fmt_bytes(nbytes)} and {drive} has "
                          f"{fmt_bytes(free)} free (keep {fmt_bytes(SPARE_BYTES)} spare). Free some space, or "
                          "extract it yourself and import the folder: a big folder is read where it is.")


_WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def _safe_member(dest: str, name: str) -> str | None:
    """Where an archive member goes, or None when it would land outside dest (zip-slip).  On
    Windows a name part with characters it does not allow is cleaned the way zipfile does."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if os.name == "nt":
        parts = [p if p == ".." else (_WIN_BAD.sub("_", p).rstrip(". ") or "_") for p in parts]
    if not parts:
        return None
    target = os.path.normpath(os.path.join(dest, *parts))
    if os.path.commonpath([os.path.abspath(dest), os.path.abspath(target)]) != os.path.abspath(dest):
        return None
    return target


def seven_zip_tools(sevenzip: str | None = None) -> list:
    out = []
    for t in (sevenzip, os.environ.get("FIVEM2OPEN77_7Z"), "7z", "7zz", "7za"):
        if t and (os.path.isfile(t) or shutil.which(t)) and t not in out:
            out.append(t)
    return out


def extract(archive: str, dest: str, log=print, progress=None, check=None, sevenzip: str | None = None):
    """Unpack an archive into dest (created).  .zip here, with progress; .rar / .7z with 7-Zip."""
    os.makedirs(dest, exist_ok=True)
    if archive.lower().endswith(".zip"):
        try:
            z = zipfile.ZipFile(archive)
        except zipfile.BadZipFile as e:
            raise SourceError(f"{os.path.basename(archive)} is not a readable zip: {e}") from e
        with z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos) or 1
            done = 0
            last = 0.0
            for i in infos:
                if check:
                    check()
                target = _safe_member(dest, i.filename)
                if target is None:
                    log(f"skipped {i.filename} (outside the archive's folder)")
                    continue
                if i.is_dir() or i.filename.endswith(("/", "\\")):
                    os.makedirs(target, exist_ok=True)
                    continue
                if i.flag_bits & 0x1:
                    raise SourceError(f"{os.path.basename(archive)} is password protected: extract it yourself "
                                      "and import the folder")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(i) as src, open(target, "wb") as out:
                    while True:
                        if check:
                            check()
                        chunk = src.read(4 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress and time.time() - last > 0.5:
                            last = time.time()
                            progress(done / total, f"extracting {fmt_bytes(done)} of {fmt_bytes(total)}")
        return dest
    tools = seven_zip_tools(sevenzip)
    if not tools:
        raise SourceError(f"cannot extract {os.path.basename(archive)}: 7-Zip was not found (install 7-Zip, or "
                          "extract it yourself and import the folder)")
    log(f"extracting {os.path.basename(archive)} with 7-Zip ...")
    if progress:
        progress(None, "extracting with 7-Zip")
    # -p<dummy>: a password-protected archive fails at once instead of waiting for a password
    p = subprocess.Popen([tools[0], "x", "-y", "-pfivem2open77", f"-o{dest}", archive], stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while p.poll() is None:
            if check:
                try:
                    check()
                except BaseException:
                    p.kill()
                    raise
            time.sleep(0.3)
    finally:
        err = (p.stderr.read() if p.stderr else b"") or b""
    if p.returncode != 0:
        raise SourceError(f"7-Zip could not extract {os.path.basename(archive)}: "
                          f"{err.decode('utf-8', 'replace').strip()[-300:] or f'exit {p.returncode}'}")
    return dest


def copy_tree(src: str, dest: str, log=print, progress=None, check=None):
    """shutil.copytree with progress (and a cancel check between files)."""
    size, files = measure(src)
    done = n = 0
    last = 0.0
    for dp, dn, fn in os.walk(src):
        dn[:] = [d for d in dn if not d.startswith(".")]
        rel = os.path.relpath(dp, src)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for f in fn:
            if check:
                check()
            s = os.path.join(dp, f)
            shutil.copy2(s, os.path.join(dest, rel, f))
            n += 1
            try:
                done += os.path.getsize(s)
            except OSError:
                pass
            if progress and time.time() - last > 0.5:
                last = time.time()
                progress(done / max(size, 1), f"copying {n} of {files} files ({fmt_bytes(done)})")
    return dest


def copy_file(src: str, dest: str, progress=None, check=None):
    size = os.path.getsize(src) or 1
    done = 0
    last = 0.0
    with open(src, "rb") as a, open(dest, "wb") as b:
        while True:
            if check:
                check()
            chunk = a.read(8 << 20)
            if not chunk:
                break
            b.write(chunk)
            done += len(chunk)
            if progress and time.time() - last > 0.5:
                last = time.time()
                progress(done / size, f"copying {fmt_bytes(done)} of {fmt_bytes(size)}")
    shutil.copystat(src, dest)
    return dest


MANIFESTS = ("fxmanifest.lua", "__resource.lua")


def resource_root(folder: str) -> str:
    """An extracted archive often holds one folder (libertyv.zip: libertyv/libertyv/...): the
    resource is the first folder that holds more than one thing, or a manifest (never a stream/ or
    data/ folder of its own)."""
    for _ in range(6):
        try:
            entries = [e for e in os.listdir(folder) if not e.startswith((".", "__MACOSX"))]
        except OSError:
            return folder
        if any(e.lower() in MANIFESTS for e in entries):
            return folder
        if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])) \
                and entries[0].lower() not in ("stream", "data"):
            folder = os.path.join(folder, entries[0])
            continue
        return folder
    return folder


def take_in(src: str, dest_dir: str, how: str, log=print, progress=None, check=None, sevenzip=None) -> str:
    """Bring src into dest_dir by copy | move | extract and return the path the resource is read at
    (for extract: the archive's single top folder when it has one).  link is not handled here."""
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(os.path.normpath(src))
    if how == "extract":
        need_space(dest_dir, unpacked_size(src), f"extracting {base}")
        d = os.path.join(dest_dir, os.path.splitext(base)[0])
        extract(src, d, log=log, progress=progress, check=check, sevenzip=sevenzip)
        return resource_root(d)
    dst = os.path.join(dest_dir, base)
    if how == "move":
        try:
            os.replace(src, dst)                 # same drive: instant
        except OSError:
            shutil.move(src, dst)
        return dst
    if os.path.isdir(src):
        need_space(dest_dir, measure(src)[0], f"copying {base}")
        return copy_tree(src, dst, log=log, progress=progress, check=check)
    need_space(dest_dir, os.path.getsize(src), f"copying {base}")
    return copy_file(src, dst, progress=progress, check=check)


# ====================================================================== the library
class Library:
    """FiveM sources held once for every project that reads them."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path(self, lid: str, *parts) -> str:
        if not _ID.match(lid or ""):
            raise SourceError("no such library entry")
        return os.path.join(self.root, lid, *parts)

    def get(self, lid: str) -> dict:
        try:
            with open(self.path(lid, "entry.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise SourceError("no such pack") from e

    def save(self, entry: dict):
        p = self.path(entry["id"], "entry.json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=1)
        os.replace(p + ".tmp", p)

    def list(self, kind: str | None = None) -> list:
        out = []
        for name in sorted(os.listdir(self.root)):
            if not _ID.match(name):
                continue
            try:
                e = self.get(name)
            except SourceError:
                continue
            if kind is None or e.get("kind") == kind:
                out.append(e)
        out.sort(key=lambda e: -(e.get("created") or 0))
        return out

    def folder(self, lid: str, entry: dict | None = None) -> str:
        """Where the entry's resource is read (the user's own folder when it is linked)."""
        e = entry or self.get(lid)
        if e.get("linked"):
            return e["linked"]
        return os.path.join(self.path(lid, "src"), *(e.get("root") or "").split("/")) if e.get("root") else self.path(lid, "src")

    def source(self, lid: str, sub: str = "") -> str:
        e = self.get(lid)
        if e.get("state") == "importing":
            raise SourceError(f"{e.get('name')} is still being imported")
        if e.get("state") == "failed":
            raise SourceError(f"the import of {e.get('name')} failed: {e.get('error') or 'see its job'}")
        base = self.folder(lid, e)
        path = os.path.join(base, *sub.split("/")) if sub else base
        if not os.path.exists(path):
            if e.get("linked"):
                raise SourceError(f"{e['linked']} is gone: the project reads that folder where it was imported from. "
                                  "Put it back (or import it again)")
            raise SourceError(f"the files of {e.get('name')} are missing from the Studio's library")
        return path

    def create(self, kind: str, name: str, original: str) -> dict:
        lid = uuid.uuid4().hex[:12]
        os.makedirs(self.path(lid))
        e = {"id": lid, "kind": kind, "name": name, "original": original, "created": time.time(),
             "state": "importing", "cars": []}
        self.save(e)
        return e

    def fill(self, entry: dict, src: str, how: str, log=print, progress=None, check=None, sevenzip=None) -> dict:
        """Take the source in and record where its resource folder is.  A car pack is always held
        unpacked (every car's look and build reads it), so a small archive is extracted too."""
        if how == "link":
            entry["linked"] = os.path.abspath(src)
        else:
            if os.path.isfile(src) and entry.get("kind") == "cars" and how in ("copy", "move"):
                how = "extract"
            d = self.path(entry["id"], "src")
            if how == "extract":
                need_space(self.root, unpacked_size(src), f"extracting {os.path.basename(src)}")
                extract(src, d, log=log, progress=progress, check=check, sevenzip=sevenzip)
                got = resource_root(d)
            else:
                got = take_in(src, d, how, log=log, progress=progress, check=check, sevenzip=sevenzip)
            entry["root"] = os.path.relpath(got, d).replace(os.sep, "/")
            if entry["root"] == ".":
                entry["root"] = ""
        entry["how"] = how
        self.save(entry)
        return entry

    def adopt(self, kind: str, name: str, original: str, path: str) -> dict:
        """Move a file or folder the Studio already holds (a project's input) into the library, so
        other projects can read it too."""
        e = self.create(kind, name, original)
        d = self.path(e["id"], "src")
        os.makedirs(d)
        dst = os.path.join(d, os.path.basename(os.path.normpath(path)))
        os.replace(path, dst)
        e.update(root=os.path.basename(dst), how="adopted", state="ready")
        self.save(e)
        return e

    def delete(self, lid: str):
        """The entry and its files; a linked folder is the user's own and is never touched."""
        d = self.path(lid)
        if not os.path.isdir(d):
            return
        trash = d + ".deleting"
        try:
            os.replace(d, trash)                 # gone from the list at once, removed below
        except OSError:
            trash = d
        shutil.rmtree(trash, ignore_errors=True)

    def prune(self, in_use: set, older_than: float = 86400.0) -> list:
        """Entries no project reads, left from imports nobody finished (a day old by default)."""
        now = time.time()
        gone = []
        for name in os.listdir(self.root):
            if name.endswith(".deleting"):
                shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
                continue
            if name in in_use or not _ID.match(name):
                continue
            try:
                created = float(self.get(name).get("created") or 0)
            except SourceError:
                created = os.path.getmtime(os.path.join(self.root, name))
            if now - created > older_than:
                self.delete(name)
                gone.append(name)
        return gone


# ====================================================================== the cars of a pack
def _manifest_dir(path: str, manifest_dirs: set, root: str) -> str | None:
    d = os.path.dirname(os.path.abspath(path))
    root = os.path.abspath(root)
    while True:
        if os.path.normcase(d) in manifest_dirs:
            return d
        parent = os.path.dirname(d)
        if parent == d or not os.path.normcase(d).startswith(os.path.normcase(root)):
            return None
        d = parent


def car_folder(res, v) -> str:
    """The folder of the pack a car's own files live in, relative to the pack (/ separated), or ""
    when the car needs the whole pack (its files spread over several resources)."""
    root = os.path.abspath(res.root)
    mans = {os.path.normcase(os.path.dirname(os.path.abspath(m))) for m in res.manifests}

    def original(p):
        # a file unpacked from a dlc.rpf: the rpf in the pack stands for it
        if res.tmpdir and os.path.abspath(p).startswith(os.path.join(os.path.abspath(res.tmpdir), "rpf") + os.sep):
            rel = os.path.relpath(p, os.path.join(res.tmpdir, "rpf")).split(os.sep)
            for i, part in enumerate(rel):
                if part.lower().endswith(".rpf"):
                    return os.path.join(root, *rel[: i + 1])
            return None
        return p

    yft = original(v.yft)
    if not yft:
        return ""
    d = _manifest_dir(yft, mans, root)
    if d is None:
        # no manifest above it: the folder of its dlc.rpf, or the folder its stream/ folder is in
        d = os.path.dirname(yft) if yft.lower().endswith(".rpf") else os.path.abspath(v.resource_dir or "")
        if not d or not os.path.normcase(d).startswith(os.path.normcase(root) + os.sep):
            return ""
    meta = original(getattr(v, "meta", None) or "") if getattr(v, "meta", None) else None
    if meta and not os.path.normcase(os.path.abspath(meta)).startswith(os.path.normcase(d) + os.sep):
        return ""          # its vehicles.meta is in another resource of the pack: read it all
    rel = os.path.relpath(d, root)
    return "" if rel == "." else rel.replace(os.sep, "/")


def scan_cars(folder: str, log=print) -> list:
    """The cars of a pack: [{model, display, make, class, sub, gen9, warnings}], one per model."""
    from ..gta.resource import scan_resource
    from ..model.build import display_name_of
    res = scan_resource(folder)
    try:
        out = []
        for v in res.vehicles:
            try:
                disp, make = display_name_of(v)
            except Exception:  # noqa: BLE001 - a name is not worth failing the pack over
                disp, make = v.model, ""
            cls = (v.info.vehicle_class if v.info else "") or ""
            out.append({"model": v.model, "display": disp, "make": make,
                        "class": cls.replace("VC_", "").lower() if cls else "",
                        "sub": car_folder(res, v), "warnings": len(res.warnings_for(v)) + len(v.warnings)})
        out.sort(key=lambda c: (c["make"].lower(), c["display"].lower(), c["model"].lower()))
        if not out:
            log("no cars found (vehicles.meta + .yft)")
        return out
    finally:
        res.cleanup()
