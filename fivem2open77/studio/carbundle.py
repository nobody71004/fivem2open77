"""Every converted car on a server in a handful of resources: car bundles.

An Open77 server may require at most 64 mods, and every resource that preloads a package is one
required mod, so eighty cars as eighty resources do not fit.  A car bundle (``f2o77_cars_N``)
carries up to BUNDLE_SIZE cars in one resource: one preload package holding each car's archive and
TweakXL record, and a server script that announces every car to the car hub (``f2o77:car``, as a
single car resource does).

A car keeps its bundle from one deploy to the next -- the assignment is read back from the bundles
the server has -- so adding a car re-packs one bundle, not all of them, and players download only
what changed.  New cars fill bundles that have room before a new bundle is started.  A car that was
installed on its own (its own resource) stays on its own and is updated in place: carrying it in a
bundle as well would define its record twice.
"""
from __future__ import annotations

import json
import os
import re
import zipfile

from ..cp77 import package as pkg

PREFIX = "f2o77_cars_"
BUNDLE_SIZE = 12
BUNDLE_RX = re.compile(r"^f2o77_cars_(\d+)$")


class BundleError(ValueError):
    pass


# ----------------------------------------------------------------------------- reading built cars
def parse_lua_table(text: str, start: int = 0):
    """The Lua literal at text[start:] as Python (the subset cp77.package._lua writes: strings,
    numbers, booleans, nil, lists and string-keyed tables).  -> (value, end index)."""
    i = start
    n = len(text)

    def ws(i):
        while i < n:
            if text[i] in " \t\r\n,;":
                i += 1
            elif text.startswith("--", i):
                j = text.find("\n", i)
                i = n if j < 0 else j
            else:
                break
        return i

    def value(i):
        i = ws(i)
        c = text[i]
        if c == '"':
            out, i = [], i + 1
            while text[i] != '"':
                if text[i] == "\\":
                    nxt = text[i + 1]
                    out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            return "".join(out), i + 1
        if c == "{":
            i += 1
            items, keyed = [], {}
            while True:
                i = ws(i)
                if text[i] == "}":
                    return (keyed if keyed else items), i + 1
                m = re.compile(r"([A-Za-z_]\w*)\s*=").match(text, i)
                if m and not text.startswith("==", m.end() - 1):
                    v, i = value(m.end())
                    keyed[m.group(1)] = v
                    continue
                if text[i] == "[":
                    k, i = value(i + 1)
                    i = ws(i)
                    if text[i] != "]":
                        raise BundleError("bad table key")
                    i = ws(i + 1)
                    if text[i] != "=":
                        raise BundleError("bad table key")
                    v, i = value(i + 1)
                    keyed[str(k)] = v
                    continue
                v, i = value(i)
                items.append(v)
        m = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?").match(text, i)
        if m:
            s = m.group(0)
            return (float(s) if any(x in s for x in ".eE") else int(s)), m.end()
        for word, v in (("true", True), ("false", False), ("nil", None)):
            if text.startswith(word, i):
                return v, i + len(word)
        raise BundleError(f"cannot read the Lua value at {text[i:i + 30]!r}")

    return value(i)


def car_info_of(res_dir: str) -> dict:
    """What a built car resource announces to the hub: server/car.json, or (builds before it was
    written) the CAR table of its server/main.lua."""
    j = os.path.join(res_dir, "server", "car.json")
    if os.path.isfile(j):
        with open(j, encoding="utf-8") as f:
            return json.load(f)
    with open(os.path.join(res_dir, "server", "main.lua"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^local CAR = ", text, re.M)
    if not m:
        raise BundleError(f"{os.path.basename(res_dir)}: no CAR table in server/main.lua")
    info, _ = parse_lua_table(text, m.end())
    if not isinstance(info, dict) or not info.get("record"):
        raise BundleError(f"{os.path.basename(res_dir)}: the CAR table has no record")
    return info


def car_resources(build_out: str) -> list:
    """The car resources of one build's out/ folder (a resource with several models gives several):
    [{resource, record, dir, zips, info}].  Client add-ons (a door chime) are not cars."""
    cars = []
    for name in sorted(os.listdir(build_out)):
        d = os.path.join(build_out, name)
        lua = os.path.join(d, "open77.lua")
        if not os.path.isfile(lua):
            continue
        with open(lua, encoding="utf-8") as f:
            text = f.read()
        zips = re.findall(r'^preload_mod "([^"]+)"', text, re.M)
        if "converted by fivem2open77" not in text or not zips:
            continue
        info = car_info_of(d)
        cars.append({"resource": name, "record": info["record"], "dir": d,
                     "zips": [os.path.join(d, *z.split("/")) for z in zips], "info": info})
    return cars


def addons_of(build_out: str) -> list:
    """Client add-ons of a build (e.g. <car>_chime): resources with no preload package."""
    out = []
    for name in sorted(os.listdir(build_out)):
        lua = os.path.join(build_out, name, "open77.lua")
        if os.path.isfile(lua):
            with open(lua, encoding="utf-8") as f:
                if "preload_mod" not in f.read():
                    out.append(name)
    return out


# ----------------------------------------------------------------------------- what goes where
def parse_bundle_lua(text: str) -> list:
    """Records a bundle's open77.lua says it carries ("-- Records:", or the older "-- Cars:")."""
    m = re.search(r"^-- Records: (.*)$", text, re.M)
    if m:
        return [r.strip() for r in m.group(1).split(",") if r.strip()]
    m = re.search(r"^-- Cars: (.*)$", text, re.M)
    if m:
        return ["Vehicle." + r.strip() for r in m.group(1).split(",") if r.strip()]
    return []


def plan(records: list, existing: dict, size: int = BUNDLE_SIZE) -> dict:
    """records: the cars to carry; existing: {bundle name: [records]} as installed.
    -> {bundle name: [records]}: every car stays in the bundle it is in, the rest fill bundles with
    room (lowest number first), then new bundles.  Bundles left empty are not in the result."""
    want = list(dict.fromkeys(records))
    wanted = set(want)
    out, placed = {}, set()
    for name in sorted(existing, key=_bundle_no):
        keep = [r for r in existing[name] if r in wanted and r not in placed][:size]
        if keep:
            out[name] = keep
            placed.update(keep)
    todo = sorted(r for r in want if r not in placed)
    for name in sorted(out, key=_bundle_no):
        while todo and len(out[name]) < size:
            out[name].append(todo.pop(0))
    used = {_bundle_no(n) for n in out} | {_bundle_no(n) for n in existing}
    k = 1
    while todo:
        while k in used:
            k += 1
        name = f"{PREFIX}{k}"
        out[name], todo = todo[:size], todo[size:]
        used.add(k)
    for name in out:
        out[name] = sorted(out[name])
    return out


def _bundle_no(name: str) -> int:
    m = BUNDLE_RX.match(name)
    return int(m.group(1)) if m else 10 ** 6


# ----------------------------------------------------------------------------- writing a bundle
BUNDLE_MAIN = """-- {name}: the cars this bundle carries, announced to the car hub f2o77_hub
-- (fivem2open77 car bundle; each car announces itself as its own resource would).
local CARS = {cars}

local function announce()
    for _, car in ipairs(CARS) do
        TriggerEvent("f2o77:car", car)
    end
end

AddEventHandler("onResourceStart", function(resource)
    if resource ~= GetCurrentResourceName() then return end
    print("[{name}] " .. #CARS .. " cars available (/car Vehicle.<name>)")
    announce()
end)
AddEventHandler("f2o77:hubReady", announce)
"""


def write_bundle(res_dir: str, name: str, version: str, cars: list,
                 loaders=(("archivexl", "1.27.1"), ("tweakxl", "1.11.4"))) -> dict:
    """One bundle resource: cars = car_resources() entries.  -> {name, cars, bytes}."""
    if not BUNDLE_RX.match(name):
        raise BundleError(f"bad bundle name {name}")
    cars = sorted(cars, key=lambda c: c["record"])
    short = [c["record"].split(".", 1)[-1] for c in cars]
    os.makedirs(os.path.join(res_dir, "dist"), exist_ok=True)
    os.makedirs(os.path.join(res_dir, "server"), exist_ok=True)
    zpath = os.path.join(res_dir, "dist", f"{name}.zip")
    seen = {}
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for c in cars:
            for zp in c["zips"]:
                with zipfile.ZipFile(zp) as z:
                    for e in z.infolist():
                        if e.is_dir():
                            continue
                        if e.filename in seen:
                            raise BundleError(f"{e.filename} is in both {seen[e.filename]} and {c['record']}")
                        seen[e.filename] = c["record"]
                        info = zipfile.ZipInfo(e.filename, date_time=e.date_time)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o644 << 16
                        with z.open(e) as src:
                            out.writestr(info, src.read())
    lines = [
        f"-- {name}: {len(cars)} FiveM vehicles converted by fivem2open77, carried as one",
        "-- resource because every resource that preloads a package is one required mod and an",
        "-- Open77 server may require at most 64.",
        f"-- Cars: {', '.join(short)}",
        f"-- Records: {', '.join(c['record'] for c in cars)}",
        f"-- Spawn with: {', '.join('/car ' + c['record'] for c in cars)}",
        f'resource "{name}"',
        f'version "{version}"',
        "auto_start true",
        "",
    ]
    lines += [f'dependency "{loader} >={ver}"' for loader, ver in loaders]
    lines += ["", f'preload_mod "dist/{name}.zip"', "", 'server_script "server/main.lua"']
    with open(os.path.join(res_dir, "open77.lua"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    infos = [dict(c["info"], resource=name) for c in cars]
    with open(os.path.join(res_dir, "server", "main.lua"), "w", encoding="utf-8", newline="\n") as f:
        f.write(BUNDLE_MAIN.format(name=name, cars=pkg._lua(infos)))
    with open(os.path.join(res_dir, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": version, "cars": [{"record": c["record"], "resource": c["resource"],
                                                               "version": c["info"].get("version")} for c in cars]},
                  f, indent=1)
    return {"name": name, "cars": [c["record"] for c in cars], "bytes": os.path.getsize(zpath)}


# ----------------------------------------------------------------------------- what a server has
# run on the server host (python3, stdlib): the converted car resources of each server folder
INVENTORY_PY = r'''
import json, os, re, sys
root = sys.argv[1]
for name in sys.argv[2:]:
    res = os.path.join(root, name, "resources")
    out = {"name": name, "bundles": {}, "standalone": {}, "versions": {}}
    for dp, dn, fn in os.walk(res):
        if dp.count(os.sep) - res.count(os.sep) > 4:
            dn[:] = []
            continue
        if "open77.lua" not in fn or os.path.basename(os.path.dirname(dp)) != "vehicles":
            continue
        rn = os.path.basename(dp)
        try:
            text = open(os.path.join(dp, "open77.lua"), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "converted by fivem2open77" not in text or "preload_mod" not in text:
            continue
        m = re.search(r'^version "([^"]+)"', text, re.M)
        out["versions"][rn] = m.group(1) if m else None
        if re.match(r"^f2o77_cars_\d+$", rn):
            recs = []
            m = re.search(r"^-- Records: (.*)$", text, re.M)
            if m:
                recs = [r.strip() for r in m.group(1).split(",") if r.strip()]
            else:
                m = re.search(r"^-- Cars: (.*)$", text, re.M)
                if m:
                    recs = ["Vehicle." + r.strip() for r in m.group(1).split(",") if r.strip()]
            cars = {}
            try:
                b = json.load(open(os.path.join(dp, "bundle.json"), encoding="utf-8"))
                cars = {c["record"]: c.get("version") for c in b.get("cars", [])}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            out["bundles"][rn] = {"records": recs, "cars": cars}
        else:
            m = re.search(r"^-- Spawn with: /car (\S+)", text, re.M)
            if m:
                out["standalone"][rn] = m.group(1)
    print("F2O77-INVENTORY " + json.dumps(out))
'''


def parse_inventory(text: str) -> dict:
    """{server folder: {bundles: {name: {records, cars}}, standalone: {resource: record}, versions}}"""
    out = {}
    for line in (text or "").splitlines():
        if line.startswith("F2O77-INVENTORY "):
            try:
                d = json.loads(line[len("F2O77-INVENTORY "):])
            except ValueError:
                continue
            out[d["name"]] = d
    return out


def next_version(old: str | None) -> str:
    """1.<minor + 1>.0 after the version installed (1.1.0 the first time)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", old or "")
    if not m:
        return "1.1.0"
    return f"{int(m.group(1))}.{int(m.group(2)) + 1}.0"
