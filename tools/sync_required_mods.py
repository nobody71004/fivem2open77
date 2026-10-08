#!/usr/bin/env python3
"""Install an Open77 server's required mods into a game folder, for direct test boots.

The Open77 launcher normally installs a world's required mods (resource preload_mod packages such
as a converted car, ArchiveXL, TweakXL) before it starts the game.  A direct boot (OP77_CONNECT,
no launcher) skips that, so after re-converting a car the game would still load the old one.
This brings the game folder in line with what the server declares at /mods/v1/manifest:

    python sync_required_mods.py --manifest http://<server>:11822/mods/v1/manifest \
        --game "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Cyberpunk 2077"

* every package is downloaded once into a cache and checked against the manifest's SHA-256 and size;
* a file is written only when its content differs, through a temp file and an atomic replace, so a
  file the Open77 launcher hard-linked from its store is replaced, never written through;
* files this tool installed for an earlier version of a package and that the new version no
  longer has are removed (only if still unchanged since we wrote them);
* packages with executable content (.dll, .exe, .asi, .reds, .lua) are only installed with
  --allow-executable; without it they are checked and reported.

Standard library only (Windows python.exe from the launcher bat, or Linux for tests).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

ROOTS = ("archive/pc/mod/", "r6/tweaks/", "r6/scripts/", "r6/config/", "red4ext/plugins/",
         "bin/x64/plugins/", "engine/config/")
EXECUTABLE = (".dll", ".exe", ".asi", ".reds", ".lua")
STATE_NAME = "direct-sync-state.json"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_cache() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "fivem2open77", "mods")


def _safe_entry(name: str) -> str | None:
    """Game-root relative path of a zip entry, or None when it must not be installed."""
    n = name.replace("\\", "/")
    if n.endswith("/") or n.startswith("/") or ":" in n:
        return None
    n = posixpath.normpath(n)
    if n.startswith("../") or n == ".." or "/../" in n:
        return None
    return n if n.lower().startswith(ROOTS) else None


def _fetch(url: str, dest: str, sha256: str, size: int | None, timeout: float):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    h = hashlib.sha256()
    n = 0
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=timeout) as resp:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                out.write(chunk)
                h.update(chunk)
                n += len(chunk)
        if h.hexdigest() != sha256.lower() or (size is not None and n != size):
            raise ValueError(f"{url}: got {n} bytes sha256={h.hexdigest()}, manifest says {size} / {sha256}")
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _write_atomic(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".f2o77-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)          # new directory entry: a hard link elsewhere keeps its bytes
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def sync(manifest_url: str, game: str, cache: str, allow_executable: bool = False, dry_run: bool = False,
         timeout: float = 30.0, log=print) -> dict:
    game = os.path.abspath(game)
    if not os.path.isdir(os.path.join(game, "archive", "pc")):
        raise SystemExit(f"error: {game} does not look like a Cyberpunk 2077 folder (no archive/pc)")
    os.makedirs(cache, exist_ok=True)
    with urllib.request.urlopen(manifest_url, timeout=timeout) as resp:
        manifest = json.load(resp)
    mods = manifest.get("mods") or []
    log(f"server requires {len(mods)} mod(s), digest {str(manifest.get('digest'))[:12]}")

    state_path = os.path.join(cache, STATE_NAME)
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    games = state.setdefault("games", {})
    gstate = games.setdefault(os.path.normcase(game), {})

    summary = {"written": [], "unchanged": 0, "removed": [], "blocked": [], "digest": manifest.get("digest")}
    for mod in mods:
        mid, sha = mod["id"], mod["sha256"].lower()
        blob = os.path.join(cache, sha + ".zip")
        if not (os.path.isfile(blob) and _sha256_file(blob) == sha):
            url = urllib.parse.urljoin(manifest_url, mod.get("url") or f"blobs/{sha}")
            log(f"  {mid}: downloading {mod.get('sizeBytes', '?')} bytes")
            _fetch(url, blob, sha, mod.get("sizeBytes"), timeout)
        with zipfile.ZipFile(blob) as z:
            entries = {}
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = _safe_entry(info.filename)
                if rel is None:
                    raise ValueError(f"{mid}: refusing entry outside the game's mod folders: {info.filename!r}")
                entries[rel] = info
            plan = []
            for rel, info in sorted(entries.items()):
                data = z.read(info)
                want = hashlib.sha256(data).hexdigest()
                target = os.path.join(game, *rel.split("/"))
                have = _sha256_file(target) if os.path.isfile(target) else None
                plan.append((rel, target, data, want, have))
        changes = [p for p in plan if p[4] != p[3]]
        executable = [p[0] for p in changes if p[0].lower().endswith(EXECUTABLE)]
        if executable and not allow_executable:
            log(f"  {mid}: NOT installed - executable content differs or is missing ({', '.join(executable)}); "
                f"rerun with --allow-executable to install it")
            summary["blocked"].append(mid)
            continue
        installed = gstate.get(mid, {})
        for rel, target, data, want, have in plan:
            if have == want:
                summary["unchanged"] += 1
                continue
            tag = "exec " if rel.lower().endswith(EXECUTABLE) else ""
            log(f"  {mid}: {'would write' if dry_run else 'write'} {tag}{rel}")
            if not dry_run:
                _write_atomic(target, data)
            summary["written"].append(rel)
        # files an earlier version of this package installed and this version no longer has
        for rel, sha_written in sorted((installed.get("files") or {}).items()):
            if rel in entries:
                continue
            target = os.path.join(game, *rel.split("/"))
            if os.path.isfile(target) and _sha256_file(target) == sha_written:
                log(f"  {mid}: {'would remove' if dry_run else 'remove'} stale {rel}")
                if not dry_run:
                    os.remove(target)
                summary["removed"].append(rel)
        if not dry_run:
            gstate[mid] = {"sha256": sha, "files": {rel: want for rel, _t, _d, want, _h in plan}}
    for mid in sorted(set(gstate) - {m["id"] for m in mods}):
        log(f"  {mid}: no longer required by this server (its files are left in place)")
    if not dry_run:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, state_path)
    log(f"done: {len(summary['written'])} written, {summary['unchanged']} unchanged, "
        f"{len(summary['removed'])} removed, {len(summary['blocked'])} blocked")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, help="server's http://host:port/mods/v1/manifest")
    ap.add_argument("--game", required=True, help="Cyberpunk 2077 folder")
    ap.add_argument("--cache", default=_default_cache(), help="package cache (default %%LOCALAPPDATA%%\\fivem2open77\\mods)")
    ap.add_argument("--allow-executable", action="store_true", help="also install .dll/.reds/... packages")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args(argv)
    try:
        s = sync(a.manifest, a.game, a.cache, a.allow_executable, a.dry_run, a.timeout)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 3 if s["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
