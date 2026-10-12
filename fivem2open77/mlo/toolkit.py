"""The map baker's toolkit: what a Linux server host needs to rebuild maps for the in-game manager.

    python -m fivem2open77 mlo-baker-toolkit --out baker-toolkit.tgz [--dotnet-root D] [--wolvenkit W]

    toolkit/VERSION        net<runtime>-wk<WolvenKit build>: the heavy part, unpacked again only when it changes
    toolkit/dotnet/        the .NET runtime (dotnet host, hostfxr, Microsoft.NETCore.App) for linux-x64
    toolkit/wolvenkit/     the WolvenKit CLI folder (WolvenKit.CLI.dll and its libraries)
    toolkit/lib/           the baker's Python package (standard library only)

tools/deploy/remote/baker_install.sh installs it into ~/.f2o77/baker on the host.
"""
from __future__ import annotations

import hashlib
import os
import re
import tarfile

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAKER_LIB = ("fivem2open77/__init__.py", "fivem2open77/argv.py", "fivem2open77/mlo/__init__.py", "fivem2open77/mlo/baker.py",
             "fivem2open77/mlo/bake.py", "fivem2open77/mlo/world.py", "fivem2open77/mlo/ambience.py", "fivem2open77/mlo/doors.py",
             "fivem2open77/mlo/resource.py", "fivem2open77/cp77/package.py", "fivem2open77/cp77/wkjson.py",
             "fivem2open77/cp77/wolvenkit.py")


def _version_key(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v))


def runtime_parts(dotnet_root: str) -> tuple:
    """(version, [(source path, arcname under dotnet/)]) of the newest Microsoft.NETCore.App 8.x."""
    shared = os.path.join(dotnet_root, "shared", "Microsoft.NETCore.App")
    if not os.path.isdir(shared):
        raise RuntimeError(f"{dotnet_root} has no shared/Microsoft.NETCore.App (a .NET 8 runtime for linux-x64)")
    versions = sorted((v for v in os.listdir(shared) if v.startswith("8.")), key=_version_key)
    if not versions:
        raise RuntimeError(f"no .NET 8 runtime in {shared}")
    ver = versions[-1]
    fxr = os.path.join(dotnet_root, "host", "fxr")
    fxrs = sorted((v for v in os.listdir(fxr) if v.startswith("8.")), key=_version_key) if os.path.isdir(fxr) else []
    if not fxrs or not os.path.isfile(os.path.join(dotnet_root, "dotnet")):
        raise RuntimeError(f"{dotnet_root} is not a linux .NET install (dotnet, host/fxr/8.x)")
    parts = [(os.path.join(dotnet_root, "dotnet"), "dotnet")]
    for f in ("LICENSE.txt", "ThirdPartyNotices.txt"):
        if os.path.isfile(os.path.join(dotnet_root, f)):
            parts.append((os.path.join(dotnet_root, f), f))
    for base, arc in ((os.path.join(fxr, fxrs[-1]), f"host/fxr/{fxrs[-1]}"),
                      (os.path.join(shared, ver), f"shared/Microsoft.NETCore.App/{ver}")):
        for dp, _, fns in os.walk(base):
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                parts.append((p, arc + "/" + os.path.relpath(p, base).replace(os.sep, "/")))
    return ver, parts


def wolvenkit_dir(path: str) -> str:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isfile(os.path.join(d, "WolvenKit.CLI.dll")):
        raise RuntimeError(f"{path}: no WolvenKit.CLI.dll (the WolvenKit.Console folder)")
    return d


def _wk_hash(wk: str) -> str:
    h = hashlib.sha256()
    with open(os.path.join(wk, "WolvenKit.CLI.dll"), "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:10]


def version_of(dotnet_root: str, wolvenkit: str) -> str:
    """The VERSION a toolkit built from these would carry (cheap: nothing is packed)."""
    ver, _ = runtime_parts(dotnet_root)
    return f"net{ver}-wk{_wk_hash(wolvenkit_dir(wolvenkit))}"


def _add(tar, src, arc, mode=0o644):
    info = tar.gettarinfo(src, arcname=arc)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = mode
    with open(src, "rb") as f:
        tar.addfile(info, f)


def build_lib(out_path: str, version: str, log=print) -> dict:
    """A light toolkit: VERSION + the baker's package only, for a host that already has this
    VERSION of the runtime and WolvenKit (baker_install.sh then only replaces the package)."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with tarfile.open(tmp, "w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        vf = out_path + ".VERSION"
        with open(vf, "w", encoding="utf-8", newline="\n") as f:
            f.write(version + "\n")
        _add(tar, vf, "toolkit/VERSION")
        os.remove(vf)
        for rel in BAKER_LIB:
            _add(tar, os.path.join(PACKAGE_ROOT, *rel.split("/")), "toolkit/lib/" + rel)
    os.replace(tmp, out_path)
    size = os.path.getsize(out_path)
    log(f"baker package for toolkit {version}: {len(BAKER_LIB)} files, {size / 1024:.0f} KiB -> {out_path}")
    return {"path": out_path, "version": version, "files": len(BAKER_LIB) + 1, "bytes": size, "lib_only": True}


def build(out_path: str, dotnet_root: str, wolvenkit: str, log=print) -> dict:
    ver, runtime = runtime_parts(dotnet_root)
    wk = wolvenkit_dir(wolvenkit)
    version = f"net{ver}-wk{_wk_hash(wk)}"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    add = _add

    tmp = out_path + ".tmp"
    files = 0
    with tarfile.open(tmp, "w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        vf = out_path + ".VERSION"
        with open(vf, "w", encoding="utf-8", newline="\n") as f:
            f.write(version + "\n")
        add(tar, vf, "toolkit/VERSION")
        os.remove(vf)
        for src, arc in runtime:
            add(tar, src, "toolkit/dotnet/" + arc, 0o755 if arc == "dotnet" or arc.endswith(".so") else 0o644)
            files += 1
        for dp, dns, fns in os.walk(wk):
            dns.sort()
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                if os.path.islink(p):
                    continue
                rel = os.path.relpath(p, wk).replace(os.sep, "/")
                add(tar, p, "toolkit/wolvenkit/" + rel, 0o755 if fn.endswith((".so", ".exe")) else 0o644)
                files += 1
        for rel in BAKER_LIB:
            add(tar, os.path.join(PACKAGE_ROOT, *rel.split("/")), "toolkit/lib/" + rel)
            files += 1
    os.replace(tmp, out_path)
    size = os.path.getsize(out_path)
    log(f"baker toolkit {version}: {files} files, {size / 1048576:.1f} MiB -> {out_path}")
    return {"path": out_path, "version": version, "files": files, "bytes": size, "runtime": ver, "lib_only": False}
