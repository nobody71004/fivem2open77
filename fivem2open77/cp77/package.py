"""Mod packaging and the Open77 resources.

Output layout of one converted car:

  <out>/<name>/                  the car: server-only resource
      open77.lua                 preload_mod + loader dependencies (archivexl, tweakxl)
      server/main.lua            marker printed on start
      dist/<name>.zip            game-root-relative: archive/pc/mod/*.archive(.xl), r6/tweaks/...
      dist/<name>_tex<N>.zip     extra texture packages when one zip would exceed Open77's 64 MiB cap
      conversion-report.json
  <out>/<name>_chime/            (door chime) sibling resource, no dependencies
      open77.lua
      server/door_chime.lua      server network audio while a cabin door is open (all players hear it)
      audio/door_chime.mp3|.wav  the clip clients download

Why two resources: the client resource host checks `dependency` lines too, and the loader
resources have no client side, so they are never published to clients.  A car resource that had
both the loader dependencies and a client script failed to activate on every client
(`i8rpncs:missing_dependency:archivexl`, session ended with resource_activation_failed).  The car
stays server-only (exactly the documented shape of an asset resource); client extras go in a
sibling that depends on nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile

from .wkjson import document

MAX_PACKAGE = 64 * 1024 * 1024
MAX_PACKAGES = 8


# ----------------------------------------------------------------------------- localization
def localization_doc(entries) -> dict:
    """ArchiveXL on-screen localisation resource (JsonResource / localizationPersistenceOnScreenEntries)."""
    return document({
        "$type": "JsonResource",
        "cookingPlatform": "PLATFORM_None",
        "root": {"HandleId": "0", "Data": {
            "$type": "localizationPersistenceOnScreenEntries",
            "entries": [{"$type": "localizationPersistenceOnScreenEntry", "femaleVariant": text, "maleVariant": "",
                         "primaryKey": "0", "secondaryKey": key} for key, text in entries],
        }},
    })


def archive_xl(loc_path: str) -> str:
    return ("localization:\n"
            "  onscreens:\n"
            f"    en-us: {loc_path}\n")


# ----------------------------------------------------------------------------- zips
def write_zip(path: str, files: dict):
    """files: archive name (forward slashes) -> local file.  Deterministic: fixed timestamps, sorted."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc in sorted(files):
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(files[arc], "rb") as f:
                z.writestr(info, f.read())
    return os.path.getsize(path)


# ----------------------------------------------------------------------------- Open77 resource
SERVER_MAIN = """-- {display} (converted from FiveM by fivem2open77).
-- The car itself is the preload package in dist/: the launcher installs it before the game boots.
-- It tells the car hub (f2o77_hub) what it offers -- the neon kit's colours are appearances the
-- hub switches with /neon -- when it starts and whenever the hub (re)starts.
local CAR = {info}

local function announce()
    TriggerEvent("f2o77:car", CAR)
end

AddEventHandler("onResourceStart", function(resource)
    if resource ~= GetCurrentResourceName() then return end
    print("[{name}] {record} available (spawn: /car {record}){neon_note}")
    announce()
end)
AddEventHandler("f2o77:hubReady", announce)
"""


def _lua(v, indent=0) -> str:
    """Python value -> Lua literal (strings, numbers, booleans, lists, string-keyed maps)."""
    pad = "    " * (indent + 1)
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, (list, tuple)):
        if not v:
            return "{}"
        return "{\n" + "".join(f"{pad}{_lua(x, indent + 1)},\n" for x in v) + "    " * indent + "}"
    if isinstance(v, dict):
        if not v:
            return "{}"
        out = []
        for k in sorted(v):
            key = k if isinstance(k, str) and k.isidentifier() else f"[{_lua(str(k))}]"
            out.append(f"{pad}{key} = {_lua(v[k], indent + 1)},\n")
        return "{\n" + "".join(out) + "    " * indent + "}"
    raise TypeError(f"cannot write {type(v).__name__} as Lua")


def car_info(name, record, display, appearance, version, neon=None) -> dict:
    """What a car resource announces to the hub."""
    info = {"resource": name, "record": record, "display": display, "appearance": appearance or name,
            "version": version}
    if neon:
        info["neon"] = {
            "colors": [{"key": k, "label": (neon.get("labels") or {}).get(k, k),
                        "appearance": (neon.get("appearances") or {})[k]} for k in neon.get("colors", [])],
            "default": neon.get("default", "off"),
            "tubes": sorted((neon.get("tubes") or {}).keys()),
        }
    return info

DOOR_CHIME = """-- Door chime for {display}, converted by fivem2open77.
--
-- Starts the moment a cabin door opens and stops the moment the last one closes.  The SERVER
-- decides and plays it as network audio at the car, so every player near the car hears the same
-- chime at the same time (and a player who streams in mid-chime joins the running loop).
--
-- A cabin door counts as open while the canonical door state says so -- a script, the world, or
-- a player getting IN: the entering client reports its door opening and shutting, so that bit
-- follows the door itself (measured: on ~0.1 s after the seat is taken, off 1.4-1.6 s later when
-- the door shuts) -- and while a player is getting OUT: no door report comes on the way out, so
-- the occupant's exiting window in the seat ledger stands in for it (~2.1 s, ends as the door
-- shuts).  The ledger's entering flag is NOT used: it only blips for a tick before the door
-- report arrives and would start, cut and restart the chime.  No hold-over tail.
local RECORD = "{record}"
local SOUND = "{sound}"
local DURATION = {duration}     -- clip length in seconds; the server needs it to loop a network sound
local VOLUME = {volume}
local RANGE = {range}
local CABIN_MASK = 15          -- door bits 0..3: frontLeft, frontRight, backLeft, backRight
local POLL_MS = 100            -- keeps a playing chime on the car and catches any missed event

local ours = {{}}               -- vehicle id -> true for every {record} on the server
local chimes = {{}}             -- vehicle id -> {{ handle, since, reason }}
local refusedAt = -1e9

local function now()
    return Open77.time.monotonic()
end

local function openReason(car)
    if ((tonumber(car.doors) or 0) & CABIN_MASK) ~= 0 then
        return "door"
    end
    for _, o in ipairs(car.occupants or {{}}) do
        if o.exiting then return "exiting" end
    end
    return nil
end

local function stop(id, why)
    local c = chimes[id]
    if not c then return end
    chimes[id] = nil
    StopSound(c.handle)
    DestroySound(c.handle)
    print(("[{name}] vehicle %s: chime off after %.2fs (%s)"):format(tostring(id), now() - c.since, why))
end

local function evaluate(id)
    local car = Open77.vehicles.get(id)
    if not car or car.record ~= RECORD then
        ours[id] = nil
        stop(id, car and "record changed" or "vehicle gone")
        return
    end
    ours[id] = true
    local reason = openReason(car)
    local c = chimes[id]
    if not reason then
        stop(id, "doors closed")
    elseif c then
        SetSoundPosition(c.handle, car.position)
    elseif now() - refusedAt > 5 then
        local handle, err = Play3DSound(SOUND, car.position, {{
            loop = true, duration = DURATION, bucket = car.bucket or 0,
            volume = VOLUME, minDistance = 1.5, maxDistance = RANGE,
        }})
        if handle then
            chimes[id] = {{ handle = handle, since = now(), reason = reason }}
            print(("[{name}] vehicle %s: chime on (%s)"):format(tostring(id), reason))
        else
            refusedAt = now()
            print("[{name}] door chime refused: " .. tostring(err))
        end
    end
end

local function onVehicle(id)
    id = tonumber(id)
    if id and (ours[id] or chimes[id]) then evaluate(id) end
end

AddEventHandler("onVehicleCreated", function(id, _, record)
    if record == RECORD then ours[tonumber(id)] = true end
end)
AddEventHandler("onVehicleRemoved", function(id)
    id = tonumber(id)
    ours[id] = nil
    stop(id, "vehicle removed")
end)
AddEventHandler("onVehicleUpdated", onVehicle)
AddEventHandler("onVehicleOccupancyChanged", onVehicle)
AddEventHandler("onPlayerEnteringVehicle", function(_, id) onVehicle(id) end)
AddEventHandler("onPlayerEnteredVehicle", function(_, id) onVehicle(id) end)
AddEventHandler("onPlayerLeftVehicle", function(_, id) onVehicle(id) end)

AddEventHandler("onResourceStop", function(resource)
    if resource ~= GetCurrentResourceName() then return end
    for id in pairs(chimes) do stop(id, "resource stopping") end
end)

CreateThread(function()
    if type(Play3DSound) ~= "function" then
        print("[{name}] door chime disabled: this server has no network package audio (needs 2.31.13+op77.63)")
        return
    end
    for _, car in ipairs(Open77.vehicles.all() or {{}}) do
        if car.record == RECORD then ours[car.id] = true end
    end
    print(("[{name}] door chime ready for {record} (%d on the server)"):format((function()
        local n = 0
        for _ in pairs(ours) do n = n + 1 end
        return n
    end)()))
    while true do
        for id in pairs(ours) do
            evaluate(id)
        end
        Wait(POLL_MS)
    end
end)
"""


def audio_duration(path: str) -> float:
    """Length in seconds of an MP3 (frame scan, CBR or VBR) or PCM WAV clip."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        pos, byte_rate = 12, None
        while pos + 8 <= len(data):
            cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
            if cid == b"fmt ":
                byte_rate = struct.unpack_from("<I", data, pos + 16)[0]
            elif cid == b"data" and byte_rate:
                return min(size, len(data) - pos - 8) / byte_rate
            pos += 8 + size + (size & 1)
        raise ValueError(f"{path}: WAV without fmt/data chunks")
    pos = 0
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size + (10 if data[5] & 0x10 else 0)
    bitrates = {  # (version_is_mpeg1) -> layer III kbit/s table
        True: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
        False: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    }
    rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
    samples = 0.0
    frames = 0
    while pos + 4 <= len(data):
        h = struct.unpack_from(">I", data, pos)[0]
        ver, layer = (h >> 19) & 3, (h >> 17) & 3
        bri, sri, pad = (h >> 12) & 15, (h >> 10) & 3, (h >> 9) & 1
        if (h >> 21) & 0x7FF != 0x7FF or ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
            pos += 1      # not a Layer III frame header: resync
            continue
        mpeg1 = ver == 3
        rate = rates[ver][sri]
        length = (144 if mpeg1 else 72) * bitrates[mpeg1][bri] * 1000 // rate + pad
        if length < 24:
            pos += 1
            continue
        # the first frame of most encoders is a Xing/Info/VBRI header frame, not audio
        if not (frames == 0 and any(tag in data[pos + 4:pos + 40] for tag in (b"Xing", b"Info", b"VBRI"))):
            samples += (1152 if mpeg1 else 576) / rate
        frames += 1
        pos += length
    if frames == 0:
        raise ValueError(f"{path}: not an MP3 or PCM WAV file")
    return samples


def chime_dir_for(res_dir: str) -> str:
    return res_dir.rstrip("/\\") + "_chime"


def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_resource(res_dir: str, *, name: str, version: str, display: str, record: str, packages: list,
                   loaders=(("archivexl", "1.27.1"), ("tweakxl", "1.11.4")), door_chime: str | None = None,
                   chime_volume: float = 0.8, chime_range: float = 12.0, report: dict | None = None,
                   appearance: str | None = None, neon: dict | None = None) -> list:
    """Write the car resource (and, with door_chime, its client add-on next to it).  Returns the
    resource names a server adds to resources.load, loaders first."""
    lines = [
        f"-- {display}: FiveM vehicle converted by fivem2open77.",
        f"-- Spawn with: /car {record}",
        "-- Server-only on purpose: clients check `dependency` lines too, and the loader resources",
        "-- have no client side, so a client script here would fail to activate on every client.",
    ]
    if door_chime:
        lines.append(f"-- The door chime is the separate client resource {name}_chime.")
    lines += [
        f'resource "{name}"',
        f'version "{version}"',
        "auto_start true",
        "",
    ]
    for loader, ver in loaders:
        lines.append(f'dependency "{loader} >={ver}"')
    lines.append("")
    for pkg in packages:
        lines.append(f'preload_mod "dist/{pkg}"')
    lines += ["", 'server_script "server/main.lua"']
    os.makedirs(os.path.join(res_dir, "dist"), exist_ok=True)
    _write(os.path.join(res_dir, "open77.lua"), "\n".join(lines) + "\n")
    info = car_info(name, record, display, appearance, version, neon)
    note = (f"; neon kit: /neon <colour> ({len(info['neon']['colors'])} colours)" if info.get("neon") else "")
    _write(os.path.join(res_dir, "server", "main.lua"),
           SERVER_MAIN.format(display=display, name=name, record=record, info=_lua(info), neon_note=note))
    # the same, as data: a car bundle (studio/carbundle.py) announces this car from its own script
    _write(os.path.join(res_dir, "server", "car.json"), json.dumps(info, indent=1, sort_keys=True) + "\n")
    names = [loader for loader, _ in loaders] + [name]

    chime_dir = chime_dir_for(res_dir)
    shutil.rmtree(chime_dir, ignore_errors=True)
    if door_chime:
        chime = f"{name}_chime"
        duration = audio_duration(door_chime)
        asset = "audio/door_chime" + (".wav" if door_chime.lower().endswith(".wav") else ".mp3")
        _write(os.path.join(chime_dir, "open77.lua"), "\n".join([
            f"-- Door chime for {display} ({record}), converted by fivem2open77.",
            "-- The server plays it as network audio at the car (everyone nearby hears it); clients",
            "-- only download the clip.  No dependencies: the car resource is server-only.",
            f'resource "{chime}"',
            f'version "{version}"',
            "auto_start true",
            "",
            'server_script "server/door_chime.lua"',
            f'files {{ "{asset}" }}',
            'permissions { "world.vehicles", "audio.network" }',
        ]) + "\n")
        _write(os.path.join(chime_dir, "server", "door_chime.lua"),
               DOOR_CHIME.format(display=display, record=record, name=chime, duration=f"{duration:.3f}",
                                 volume=f"{chime_volume:.2f}", range=f"{chime_range:.1f}", sound=asset))
        os.makedirs(os.path.join(chime_dir, "audio"), exist_ok=True)
        shutil.copyfile(door_chime, os.path.join(chime_dir, *asset.split("/")))
        names.append(chime)
    if report is not None:
        report = dict(report, resources=names)
        with open(os.path.join(res_dir, "conversion-report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
    return names


# ----------------------------------------------------------------------------- deterministic archives
_CRC64_TABLE = None


def _crc64_xz(data: bytes) -> int:
    """CRC-64/XZ (reflected ECMA-182), as WolvenKit computes the archive index checksum."""
    global _CRC64_TABLE
    if _CRC64_TABLE is None:
        poly = 0xC96C5795D7870F42
        tab = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ poly if c & 1 else c >> 1
            tab.append(c)
        _CRC64_TABLE = tab
    crc = 0xFFFFFFFFFFFFFFFF
    tab = _CRC64_TABLE
    for b in data:
        crc = (crc >> 8) ^ tab[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFFFFFFFFFF


FIXED_FILETIME = 133801920000000000   # 2025-01-01T00:00:00Z


def depot_hash(path: str) -> int:
    """The 64-bit FNV-1a hash an archive indexes a file by: its depot path, lower case, backslashes
    (what WolvenKit writes; checked against packed map archives)."""
    h = 0xCBF29CE484222325
    for b in str(path).replace("/", "\\").lower().encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def _read_index(f) -> tuple:
    """(file entries [(hash, first segment, end segment)], segments [(offset, packed, size)]) of an
    open archive.  Index layout: u32 table offset, u32 table size, u64 crc, then the table: u32
    files, u32 segments, u32 dependencies, 56-byte file entries, 16-byte segments."""
    import struct
    head = f.read(40)
    magic, _ver, index_pos, _index_size = struct.unpack_from("<4sIQI", head, 0)
    if magic != b"RDAR":
        raise ValueError("not a Cyberpunk archive")
    f.seek(index_pos)
    _table_off, table_size, _crc = struct.unpack("<IIQ", f.read(16))
    table = f.read(table_size - 8)
    n_files, n_segs, _n_deps = struct.unpack_from("<III", table, 0)
    files = []
    for i in range(n_files):
        off = 12 + i * 56          # u64 hash, i64 time, u32 inline buffers, u32 first segment, u32 end segment, ...
        files.append(struct.unpack_from("<Q", table, off) + struct.unpack_from("<II", table, off + 20))
    base = 12 + n_files * 56
    segs = [struct.unpack_from("<QII", table, base + i * 16) for i in range(n_segs)]
    return files, segs


def archive_costs(path: str) -> dict:
    """Bytes each file takes in a packed archive (its packed segments plus its index entries), by
    depot hash: what the file adds to the package wherever it is packed again."""
    with open(path, "rb") as f:
        files, segs = _read_index(f)
    out = {}
    for h, s0, s1 in files:
        out[h] = sum(segs[i][1] for i in range(s0, s1)) + 56 + 16 * (s1 - s0)
    return out


def normalize_archive(path: str, filetime: int = FIXED_FILETIME) -> int:
    """WolvenKit stamps every file entry with the packing time, so the same input packs to a
    different archive (and Open77 hands players a new required-mod hash) on every run.  Pin the
    timestamps and recompute the index checksum.  Returns the number of entries."""
    import struct
    with open(path, "r+b") as f:
        head = f.read(40)
        magic, _ver, index_pos, _index_size = struct.unpack_from("<4sIQI", head, 0)
        if magic != b"RDAR":
            raise ValueError(f"{path} is not a Cyberpunk archive")
        f.seek(index_pos)
        _table_off, table_size, stored_crc = struct.unpack("<IIQ", f.read(16))
        table = bytearray(f.read(table_size - 8))
        if _crc64_xz(bytes(table)) != stored_crc:
            raise ValueError(f"{path}: unexpected index layout (checksum mismatch)")
        n_entries = struct.unpack_from("<I", table, 0)[0]
        for i in range(n_entries):
            struct.pack_into("<q", table, 12 + i * 56 + 8, filetime)
        f.seek(index_pos + 8)
        f.write(struct.pack("<Q", _crc64_xz(bytes(table))))
        f.write(table)
    return n_entries
