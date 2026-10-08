"""Checks on a finished build, before it goes to a server: the resource manifests, every package
(size, zip integrity, archive index, the files the tweak points at), the TweakXL record, the
engine sound, the door chime and the conversion report."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import zipfile

from ..cp77 import package
from ..cp77 import sound as sound_mod

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def fnv1a64(s: str) -> int:
    h = 0xCBF29CE484222325
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def archive_hashes(data: bytes) -> set:
    magic, _ver, index_pos, _size = struct.unpack_from("<4sIQI", data, 0)
    if magic != b"RDAR":
        raise ValueError("not a Cyberpunk archive (no RDAR header)")
    _off, size, crc = struct.unpack_from("<IIQ", data, index_pos)
    table = data[index_pos + 16: index_pos + 8 + size]
    if package._crc64_xz(bytes(table)) != crc:
        raise ValueError("archive index checksum mismatch")
    n = struct.unpack_from("<I", table, 0)[0]
    return {struct.unpack_from("<Q", table, 12 + i * 56)[0] for i in range(n)}


def parse_manifest(text: str) -> dict:
    def one(key):
        m = re.search(rf'^{key}\s+"([^"]*)"', text, re.M)
        return m.group(1) if m else None
    return {
        "resource": one("resource"), "version": one("version"),
        "auto_start": bool(re.search(r"^auto_start\s+true", text, re.M)),
        "dependencies": re.findall(r'^dependency\s+"([^"]+)"', text, re.M),
        "preload_mod": re.findall(r'^preload_mod\s+"([^"]+)"', text, re.M),
        "server_scripts": re.findall(r'^server_script\s+"([^"]+)"', text, re.M),
        "client_scripts": re.findall(r'^client_script\s+"([^"]+)"', text, re.M),
        "files": re.findall(r'"([^"]+)"', (re.search(r"^files\s*\{([^}]*)\}", text, re.M) or [None, ""])[1]),
        "permissions": re.findall(r'"([^"]+)"', (re.search(r"^permissions\s*\{([^}]*)\}", text, re.M) or [None, ""])[1]),
    }


class _Checks:
    def __init__(self):
        self.items = []

    def add(self, cid, ok, msg, level="error"):
        self.items.append({"id": cid, "ok": bool(ok), "level": "ok" if ok else level, "message": msg})
        return ok


def _lua_syntax(paths) -> tuple[bool, str] | None:
    luac = shutil.which("luac5.4") or shutil.which("luac")
    if not luac:
        return None
    r = subprocess.run([luac, "-p"] + list(paths), capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def check_build(out_dir: str) -> dict:
    """out_dir holds <car>/ and maybe <car>_chime/ (one build)."""
    c = _Checks()
    cars = [d for d in sorted(os.listdir(out_dir)) if os.path.isfile(os.path.join(out_dir, d, "conversion-report.json"))]
    if not c.add("car", bool(cars), "build has a car resource with conversion-report.json"):
        return {"ok": False, "checks": c.items}
    name = cars[0]
    car = os.path.join(out_dir, name)
    with open(os.path.join(car, "conversion-report.json"), "r", encoding="utf-8") as f:
        report = json.load(f)
    record = report.get("record", f"Vehicle.{name}")
    c.add("name", NAME_RX.match(name), f"resource id '{name}' follows Open77's rules")
    man = parse_manifest(open(os.path.join(car, "open77.lua"), encoding="utf-8").read())
    c.add("manifest.resource", man["resource"] == name, f"open77.lua declares resource \"{name}\"")
    c.add("manifest.server_only", not man["client_scripts"],
          "the car resource is server-only (clients check dependency lines)")
    c.add("manifest.loaders", {d.split()[0] for d in man["dependencies"]} >= {"archivexl", "tweakxl"},
          "depends on archivexl and tweakxl")
    for s in man["server_scripts"]:
        c.add(f"script:{s}", os.path.isfile(os.path.join(car, s)), f"{s} exists")
    c.add("packages", 0 < len(man["preload_mod"]) <= package.MAX_PACKAGES,
          f"{len(man['preload_mod'])} package(s) (Open77 allows {package.MAX_PACKAGES})")
    yaml_text, hashes = None, set()
    sizes = {}
    for i, rel in enumerate(man["preload_mod"]):
        zpath = os.path.join(car, rel)
        if not c.add(f"zip:{rel}", os.path.isfile(zpath), f"{rel} exists"):
            continue
        sizes[rel] = os.path.getsize(zpath)
        c.add(f"zip:{rel}:size", sizes[rel] <= package.MAX_PACKAGE,
              f"{rel} is {sizes[rel] / 1048576:.1f} MiB (limit 64 MiB)")
        try:
            with zipfile.ZipFile(zpath) as z:
                bad = z.testzip()
                c.add(f"zip:{rel}:crc", bad is None, f"{rel} reads back intact" + (f" (bad entry {bad})" if bad else ""))
                names = z.namelist()
                archives = [n for n in names if n.startswith("archive/pc/mod/") and n.endswith(".archive")]
                c.add(f"zip:{rel}:archive", bool(archives), f"{rel} carries an archive under archive/pc/mod/")
                for a in archives:
                    try:
                        hashes |= archive_hashes(z.read(a))
                        c.add(f"archive:{a}", True, f"{os.path.basename(a)}: valid index")
                    except ValueError as e:
                        c.add(f"archive:{a}", False, f"{os.path.basename(a)}: {e}")
                if i == 0:
                    ys = [n for n in names if n.startswith("r6/tweaks/") and n.endswith((".yaml", ".yml", ".tweak"))]
                    c.add("tweak", bool(ys), "first package carries the TweakXL file")
                    if ys:
                        yaml_text = z.read(ys[0]).decode("utf-8", "replace")
                    c.add("archivexl", any(n.endswith(".archive.xl") for n in names), "first package carries the .archive.xl")
        except zipfile.BadZipFile as e:
            c.add(f"zip:{rel}:crc", False, f"{rel}: {e}")
    if yaml_text:
        c.add("tweak.record", re.search(rf"^{re.escape(record)}:\s*$", yaml_text, re.M) is not None,
              f"tweak defines {record}")
        m = re.search(r"^\s+entityTemplatePath:\s*(\S+)", yaml_text, re.M)
        ent = m.group(1) if m else None
        if c.add("tweak.ent", bool(ent), "record points at an entity file"):
            c.add("archive.ent", fnv1a64(ent.lower()) in hashes or fnv1a64(ent) in hashes,
                  f"{ent} is inside the packages")
        for key in ("player_audio_resource", "traffic_audio_resource"):
            m = re.search(rf"^\s+{key}:\s*(\S+)", yaml_text, re.M)
            if m:
                val = m.group(1)
                known = val in sound_mod.CATALOGUE or any(s["traffic"] == val for s in sound_mod.CATALOGUE.values())
                c.add(f"sound.{key}", known, f"{key} {val} is a sound the game has")
    # chime
    chime = os.path.join(out_dir, f"{name}_chime")
    if os.path.isdir(chime):
        cm = parse_manifest(open(os.path.join(chime, "open77.lua"), encoding="utf-8").read())
        c.add("chime.deps", not cm["dependencies"], "chime resource has no dependencies")
        c.add("chime.permissions", set(cm["permissions"]) >= {"world.vehicles", "audio.network"},
              "chime declares world.vehicles and audio.network")
        for f in cm["files"]:
            p = os.path.join(chime, f)
            c.add(f"chime.file:{f}", os.path.isfile(p) and os.path.getsize(p) > 0, f"chime clip {f} exists")
        script = os.path.join(chime, "server", "door_chime.lua")
        if c.add("chime.script", os.path.isfile(script), "chime script exists"):
            text = open(script, encoding="utf-8").read()
            m = re.search(r"^local DURATION = ([0-9.]+)", text, re.M)
            c.add("chime.duration", bool(m) and float(m.group(1)) > 0, "chime knows its clip length")
            c.add("chime.record", f'local RECORD = "{record}"' in text, "chime follows this car's record")
    # Lua syntax (when a Lua compiler is around)
    # (open77.lua is the manifest DSL, not plain Lua: only the scripts are compiled)
    luas = [os.path.join(dp, f) for root in (car, chime) if os.path.isdir(root)
            for dp, _, fs in os.walk(root) for f in fs if f.endswith(".lua") and f != "open77.lua"]
    res = _lua_syntax(luas)
    if res is not None:
        c.add("lua.syntax", res[0], "Lua scripts compile" + ("" if res[0] else f": {res[1][:300]}"))
    warnings = report.get("warnings", [])
    c.add("report.warnings", True, f"{len(warnings)} conversion warning(s)" if warnings else "no conversion warnings",
          level="warn")
    ok = all(i["ok"] for i in c.items if i["level"] != "warn")
    return {"ok": ok, "car": name, "record": record, "checks": c.items,
            "summary": {"packages": sizes, "warnings": warnings, "resources": report.get("resources"),
                        "sound": report.get("sound"), "version": man["version"]}}
