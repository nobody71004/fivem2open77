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
    # the car itself: every part on a bone, doors, seats, wheels, geometry (fivem2open77/qc.py)
    from .. import qc as car_qc
    cq = car_qc.check_car(report)
    for item in cq["checks"]:
        c.items.append(dict(item, id="car." + item["id"]))
    ok = all(i["ok"] for i in c.items if i["level"] != "warn")
    facts = report.get("qc") or {}
    return {"ok": ok, "car": name, "record": record, "checks": c.items, "stale": bool(cq.get("stale")),
            "converter": report.get("version"),
            "car_qc": {"ok": cq["ok"], "donor": (facts.get("donor") or {}).get("id"),
                       "doors": facts.get("doors"), "version": facts.get("version")},
            "summary": {"packages": sizes, "warnings": warnings, "resources": report.get("resources"),
                        "sound": report.get("sound"), "version": man["version"]}}


# ----------------------------------------------------------------------------- maps
MAP_COVERAGE_WARN = {"walkable": 95.0, "walls": 90.0}
MAP_COVERAGE_FAIL = {"walkable": 80.0, "walls": 60.0}


def check_map_build(out_dir: str) -> dict:
    """out_dir holds <map>/ and f2o77_maps/ (one map build)."""
    c = _Checks()
    maps = [d for d in sorted(os.listdir(out_dir)) if os.path.isfile(os.path.join(out_dir, d, "layout.json"))] \
        if os.path.isdir(out_dir) else []
    if not c.add("map", bool(maps), "build has a map resource with layout.json"):
        return {"ok": False, "checks": c.items}
    name = maps[0]
    root = os.path.join(out_dir, name)
    try:
        with open(os.path.join(root, "conversion-report.json"), "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        report = {}
    c.add("report", bool(report), "conversion-report.json reads")
    c.add("name", NAME_RX.match(name), f"resource id '{name}' follows Open77's rules")
    man = parse_manifest(open(os.path.join(root, "open77.lua"), encoding="utf-8").read())
    c.add("manifest.resource", man["resource"] == name, f"open77.lua declares resource \"{name}\"")
    c.add("manifest.server_only", not man["client_scripts"], "the map resource is server-only (the manager has the client side)")
    c.add("manifest.archivexl", any(d.split()[0] == "archivexl" for d in man["dependencies"]), "depends on archivexl")
    for s in man["server_scripts"]:
        c.add(f"script:{s}", os.path.isfile(os.path.join(root, s)), f"{s} exists")
    c.add("packages", 0 < len(man["preload_mod"]) <= package.MAX_PACKAGES,
          f"{len(man['preload_mod'])} package(s) (Open77 allows {package.MAX_PACKAGES})")
    sizes, prefix = {}, f"fivem2open77_map_{name}"
    for rel in man["preload_mod"]:
        # <map>_ncwe_* packages are imported NCWE assets: their archives keep the names the editor
        # exported, so the "named after the map" rule (which keeps two maps' geometry from clashing)
        # applies only to the map's own packages.
        own = not os.path.basename(rel).startswith(f"{name}_ncwe_")
        zpath = os.path.join(root, rel)
        if not c.add(f"zip:{rel}", os.path.isfile(zpath), f"{rel} exists"):
            continue
        sizes[rel] = os.path.getsize(zpath)
        c.add(f"zip:{rel}:size", sizes[rel] <= package.MAX_PACKAGE, f"{rel} is {sizes[rel] / 1048576:.1f} MiB (limit 64 MiB)")
        try:
            with zipfile.ZipFile(zpath) as z:
                bad = z.testzip()
                c.add(f"zip:{rel}:crc", bad is None, f"{rel} reads back intact" + (f" (bad entry {bad})" if bad else ""))
                archives = [n for n in z.namelist() if n.startswith("archive/pc/mod/") and n.endswith(".archive")]
                c.add(f"zip:{rel}:archive", bool(archives), f"{rel} carries an archive under archive/pc/mod/")
                for a in archives:
                    base = a.split("/")[-1]
                    if own:
                        c.add(f"archive:{base}:name", base.startswith(prefix), f"{base} is named after the map")
                    else:
                        c.add(f"archive:{base}:imported", True, f"{base} keeps its imported name (NCWE asset package)")
                    try:
                        n = len(archive_hashes(z.read(a)))
                        c.add(f"archive:{base}", n > 0, f"{base}: valid index, {n} files")
                    except ValueError as e:
                        c.add(f"archive:{base}", False, f"{base}: {e}")
        except zipfile.BadZipFile as e:
            c.add(f"zip:{rel}:crc", False, f"{rel}: {e}")
    baked = None
    if os.path.isfile(os.path.join(root, "baked.json")):
        try:
            with open(os.path.join(root, "baked.json"), "r", encoding="utf-8") as f:
                baked = json.load(f)
        except (OSError, ValueError):
            baked = None
    if baked:
        c.add("baked.package", f"dist/{baked.get('package')}" in man["preload_mod"],
              f"the sector package {baked.get('package')} is declared")
        a = baked.get("anchor") or {}
        c.add("baked.anchor", all(isinstance(a.get(k), (int, float)) for k in ("x", "y", "z", "yaw")),
              "baked at " + ", ".join(f"{k} {a.get(k)}" for k in ("x", "y", "z", "yaw")))
        c.add("doors.json", baked.get("doors") in (None, 0) or os.path.isfile(os.path.join(root, "doors.json")),
              "doors.json lists the Door devices")
    else:
        c.add("baked", False, "not baked yet: players see nothing until it is placed (set a spot in Options, or place "
              "it in game with F9 and Apply)", level="warn")
        n = len(man["preload_mod"])
        c.add("placeable", n + 1 <= package.MAX_PACKAGES,
              f"room for the sector package placing it adds ({n} + 1 of {package.MAX_PACKAGES} packages)" if n + 1 <=
              package.MAX_PACKAGES else f"it can never be placed: {n} packages and placing adds the sector package, but "
              f"Open77 allows {package.MAX_PACKAGES} per resource (rebuild it with Studio 1.7.4 or later, or drop an "
              "NCWE import)")
    for f in ("ghost.txt", "layout.json"):
        c.add(f"file:{f}", os.path.isfile(os.path.join(root, f)), f"{f} is there (bakes on the server need it)")
    mgr = os.path.join(out_dir, "f2o77_maps")
    if c.add("manager", os.path.isfile(os.path.join(mgr, "open77.lua")), "the map manager f2o77_maps is in the build"):
        mm = parse_manifest(open(os.path.join(mgr, "open77.lua"), encoding="utf-8").read())
        c.add("manager.resource", mm["resource"] == "f2o77_maps", "the manager's manifest names f2o77_maps")
        c.add("manager.map", os.path.isfile(os.path.join(mgr, "shared", "maps", f"{name}.lua")),
              f"the manager knows {name} (shared/maps/{name}.lua)")
        c.add("manager.http", "http.serve" in mm["permissions"], "the manager may serve its agent API (http.serve)")
    luas = [os.path.join(dp, f) for top in (root, mgr) if os.path.isdir(top)
            for dp, _, fs in os.walk(top) for f in fs if f.endswith(".lua") and f != "open77.lua"]
    res = _lua_syntax(luas)
    if res is not None:
        c.add("lua.syntax", res[0], "Lua scripts compile" + ("" if res[0] else f": {res[1][:300]}"))
    cov = ((report.get("build") or {}).get("coverage") or {})
    col, vis = cov.get("collision") or {}, cov.get("visual_floors") or {}
    if cov:
        got = {"walkable": (col.get("walkable") or {}).get("covered_pct"), "walls": (col.get("walls") or {}).get("covered_pct")}
        for k, lim in MAP_COVERAGE_WARN.items():
            v = got.get(k)
            if v is None:
                continue
            # a player falls through (or walks through) what is not covered: far below is a broken map
            c.add(f"coverage.{k}", v >= lim, f"{k} collision covers {v}% (want {lim}%+)",
                  level="error" if v < MAP_COVERAGE_FAIL.get(k, 0) else "warn")
        if vis.get("m2"):
            # floors the FiveM map has no surface for either are the map's own (open sky, a roof
            # walked over in GTA too); what counts is a floor the conversion lost
            lost = float(vis.get("converter_gaps_m2") or 0.0)
            share = 100.0 * lost / max(1.0, float(vis["m2"]))
            c.add("coverage.floors", share <= 1.0 or lost <= 4.0,
                  f"the conversion keeps the visible floors ({lost:.0f} m2 of {vis['m2']:.0f} m2 lost)" if share <= 1.0 or lost <= 4.0
                  else f"the conversion lost {lost:.0f} m2 of visible floor ({share:.1f}%)",
                  level="error" if share > 5.0 else "warn")
    skipped = (report.get("build") or {}).get("skipped") or {}
    if skipped:
        n = sum(int(v) if isinstance(v, (int, float)) else len(v) if isinstance(v, (list, dict)) else 1
                for v in skipped.values())
        c.add("parts.skipped", False, f"{n} objects left out: " + ", ".join(sorted(skipped)[:8]), level="warn")
    warnings = report.get("warnings", [])
    c.add("report.warnings", True, f"{len(warnings)} conversion warning(s)" if warnings else "no conversion warnings",
          level="warn")
    ok = all(i["ok"] for i in c.items if i["level"] != "warn")
    return {"ok": ok, "map": name, "checks": c.items,
            "summary": {"packages": sizes, "warnings": warnings, "baked": (baked or {}).get("anchor"),
                        "version": man["version"]}}
