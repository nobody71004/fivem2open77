"""Helpers to read and write WolvenKit's CR2W JSON representation ("WKitJsonVersion" 0.0.9).

WolvenKit CLI turns these JSON documents into binary CR2W files (`convert deserialize`) and
back (`convert serialize`).  Values follow WolvenKit's conventions:

  CName            {"$type": "CName", "$storage": "string", "$value": "..."}
  resource refs    {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": "a\\b.mesh"}, "Flags": "Default"}
  handles          {"HandleId": "N", "Data": {...}} for the first occurrence, {"HandleRefId": "N"} afterwards
  FixedPoint       {"$type": "FixedPoint", "Bits": int(value * 2**17)}
"""
from __future__ import annotations

import copy
import datetime
import json
import os

WK_VERSION = "8.17.4"
GAME_VERSION = 2310


def header(archive_name: str = "") -> dict:
    return {
        "WolvenKitVersion": WK_VERSION,
        "WKitJsonVersion": "0.0.9",
        "GameVersion": GAME_VERSION,
        "ExportedDateTime": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z"),
        "DataType": "CR2W",
        "ArchiveFileName": archive_name,
    }


def document(root: dict, build_version: int = 0, embedded=None) -> dict:
    return {"Header": header(), "Data": {"Version": 195, "BuildVersion": build_version, "RootChunk": root,
                                         "EmbeddedFiles": embedded or []}}


def cname(value: str | None) -> dict:
    return {"$type": "CName", "$storage": "string", "$value": value if value else "None"}


def depot(path: str | None, flags: str = "Default") -> dict:
    if not path:
        return {"DepotPath": {"$type": "ResourcePath", "$storage": "uint64", "$value": "0"}, "Flags": flags}
    return {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": path}, "Flags": flags}


def fixed(v: float) -> dict:
    return {"$type": "FixedPoint", "Bits": int(round(v * 131072.0))}


def world_position(x, y, z) -> dict:
    return {"$type": "WorldPosition", "x": fixed(x), "y": fixed(y), "z": fixed(z)}


def quat(q) -> dict:
    x, y, z, w = q
    return {"$type": "Quaternion", "i": float(x), "j": float(y), "k": float(z), "r": float(w)}


def vec3(x, y, z, cap=True) -> dict:
    return {"$type": "Vector3", "X": float(x), "Y": float(y), "Z": float(z)}


def vec4(x, y, z, w) -> dict:
    return {"$type": "Vector4", "W": float(w), "X": float(x), "Y": float(y), "Z": float(z)}


def color(r, g, b, a=255) -> dict:
    return {"$type": "Color", "Alpha": int(a), "Blue": int(b), "Green": int(g), "Red": int(r)}


def world_transform(pos=(0, 0, 0), rot=(0, 0, 0, 1)) -> dict:
    return {"$type": "WorldTransform", "Orientation": quat(rot), "Position": world_position(*pos)}


def value_of(v):
    """Unwrap {"$value"} / DepotPath objects to plain Python values."""
    if isinstance(v, dict):
        if "$value" in v:
            return v["$value"]
        if "DepotPath" in v:
            return v["DepotPath"].get("$value")
    return v


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(doc: dict, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)


class Handles:
    """Tracks HandleId allocation inside one document."""

    def __init__(self, doc: dict | None = None):
        self.next = 0
        if doc is not None:
            self.next = max_handle_id(doc) + 1

    def new(self, data: dict) -> dict:
        h = {"HandleId": str(self.next), "Data": data}
        self.next += 1
        return h


def max_handle_id(o) -> int:
    best = -1
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "HandleId" in x:
                try:
                    best = max(best, int(x["HandleId"]))
                except ValueError:
                    pass
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return best


def index_handles(o) -> dict:
    """HandleId -> the {"HandleId","Data"} wrapper object, for the whole document."""
    out = {}
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "HandleId" in x and "Data" in x:
                out[str(x["HandleId"])] = x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


def deref(obj, handles: dict):
    """Return the Data of a handle wrapper or of a {"HandleRefId"} reference."""
    if isinstance(obj, dict):
        if "Data" in obj and "HandleId" in obj:
            return obj["Data"]
        if "HandleRefId" in obj:
            w = handles.get(str(obj["HandleRefId"]))
            return w["Data"] if w else None
    return obj


def renumber_handles(o, start: int) -> dict:
    """Deep-copy `o` giving every HandleId inside it a fresh number from `start`.
    References inside `o` to handles defined inside `o` are rewritten; references to
    handles defined elsewhere are left alone.  Returns (copy, next_free)."""
    c = copy.deepcopy(o)
    mapping = {}
    stack = [c]
    nxt = start
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "HandleId" in x and "Data" in x:
                mapping[str(x["HandleId"])] = str(nxt)
                x["HandleId"] = str(nxt)
                nxt += 1
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    stack = [c]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "HandleRefId" in x and str(x["HandleRefId"]) in mapping:
                x["HandleRefId"] = mapping[str(x["HandleRefId"])]
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return c, nxt
