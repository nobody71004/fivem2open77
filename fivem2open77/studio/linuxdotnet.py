""".NET 8 for Linux: the map baker's runtime.

The Studio rebuilds a map on the server when it is moved in game: the map baker runs there, next to
the Open77 server, and needs a linux-x64 .NET 8 runtime and WolvenKit CLI. The Studio packs both
into the baker's toolkit (mlo/toolkit.py) and uploads it with the map, so the server host needs
nothing installed. The Windows app carries the runtime in tools/baker/dotnet-linux-x64
(tools/package/build_windows.py unpacks it from the pinned download below); an install without it
-- a run from source, a folder an antivirus emptied -- downloads the same file into the workspace
from Settings > Tools.

Up to Studio 1.7.2 the Windows app did not carry it (tools.py said "bundled with the app", the
package build never put it in), so Settings > Tools showed it missing and a map installed from
Windows never got the baker: a map moved in game could not be rebuilt on the server.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
import urllib.request

# Microsoft's own file, the same runtime as the Windows app's (runtime/dotnet, 8.0.31). sha256 of
# the download; its sha512 is the one Microsoft publishes in release-metadata/8.0/releases.json.
VERSION = "8.0.31"
NAME = f"dotnet-runtime-{VERSION}-linux-x64.tar.gz"
URL = f"https://builds.dotnet.microsoft.com/dotnet/Runtime/{VERSION}/{NAME}"
SHA256 = "e2e392eedd49fd5a6eba078d508383fa9191059cb362c9d93cf533d5207b8db6"
SIZE = 31298876

_TOP = {"dotnet", "LICENSE.txt", "ThirdPartyNotices.txt", "host", "shared"}


def is_root(d: str | None) -> bool:
    """A linux-x64 .NET 8 runtime folder: the `dotnet` host (an ELF file), host/fxr/8.x and
    shared/Microsoft.NETCore.App/8.x -- what mlo/toolkit.py packs."""
    if not d or not os.path.isdir(os.path.join(d, "shared", "Microsoft.NETCore.App")):
        return False
    try:
        with open(os.path.join(d, "dotnet"), "rb") as f:
            if f.read(4) != b"\x7fELF":
                return False
    except OSError:
        return False
    fxr = os.path.join(d, "host", "fxr")
    shared = os.path.join(d, "shared", "Microsoft.NETCore.App")
    return (any(re.match(r"^8\.", v) for v in os.listdir(shared))
            and os.path.isdir(fxr) and any(re.match(r"^8\.", v) for v in os.listdir(fxr)))


def unpack(tgz: str, dest: str) -> dict:
    """Unpacks a dotnet-runtime-8.*-linux-x64.tar.gz into `dest` (made fresh): plain files and
    folders only, every one under dest, nothing but the runtime's own top-level names. Raises
    ValueError on anything else, before writing anything."""
    with tarfile.open(tgz, "r:gz") as tar:
        members = tar.getmembers()
        files = 0
        for m in members:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name in ("", "."):
                continue
            parts = name.split("/")
            if m.name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
                raise ValueError(f"{os.path.basename(tgz)}: unsafe path {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"{os.path.basename(tgz)}: {m.name!r} is not a plain file or folder")
            if parts[0] not in _TOP:
                raise ValueError(f"{os.path.basename(tgz)}: {m.name!r} is not part of a .NET runtime")
            files += m.isfile()
        part = dest + ".part"
        shutil.rmtree(part, ignore_errors=True)
        os.makedirs(part)
        for m in members:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name in ("", "."):
                continue
            out = os.path.join(part, *name.split("/"))
            if m.isdir():
                os.makedirs(out, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with tar.extractfile(m) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if os.name != "nt":
                os.chmod(out, 0o755 if (m.mode & 0o111) else 0o644)
    if not is_root(part):
        shutil.rmtree(part, ignore_errors=True)
        raise ValueError(f"{os.path.basename(tgz)} is not a linux-x64 .NET 8 runtime")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(part, dest)
    versions = sorted(os.listdir(os.path.join(dest, "shared", "Microsoft.NETCore.App")))
    return {"path": dest, "files": files, "runtime": versions[-1]}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(dest: str, cache_dir: str, *, url: str | None = None, sha256: str | None = None, size: int | None = None,
             log=print, progress=None, check=None) -> dict:
    """Downloads the pinned runtime (URL, SHA256) into `cache_dir` (kept: a second install reads it
    again), checks it is Microsoft's file byte for byte, and unpacks it into `dest`.
    `progress(done, total)` and `check()` (raises to stop) are called as it goes."""
    url, sha256, size = url or URL, sha256 or SHA256, SIZE if size is None else size
    os.makedirs(cache_dir, exist_ok=True)
    tgz = os.path.join(cache_dir, os.path.basename(url.split("?")[0]) or NAME)
    if os.path.isfile(tgz) and _sha256(tgz) == sha256:
        log(f"{os.path.basename(tgz)}: already downloaded")
    else:
        part = tgz + ".part"
        log(f"downloading {url} ({size / 1048576:.0f} MB)")
        req = urllib.request.Request(url, headers={"User-Agent": "fivem2open77-studio"})
        done = 0
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or size or 0)
                while True:
                    if check:
                        check()
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        except BaseException:
            if os.path.exists(part):
                os.remove(part)          # a stopped or broken download leaves nothing behind
            raise
        got = _sha256(part)
        if got != sha256:
            os.remove(part)
            raise ValueError(f"the download is not the pinned file (sha256 {got[:16]}..., expected {sha256[:16]}...): nothing was installed")
        os.replace(part, tgz)
        log(f"{os.path.basename(tgz)}: {done / 1048576:.1f} MB, sha256 verified")
    res = unpack(tgz, dest)
    log(f".NET {res['runtime']} for Linux unpacked into {dest} ({res['files']} files)")
    return res
