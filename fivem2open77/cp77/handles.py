"""Handle bookkeeping for WolvenKit JSON documents that are assembled from pieces of other documents.

WolvenKit writes a handle once as {"HandleId": "N", "Data": {...}} at its first occurrence (in
serialization order) and as {"HandleRefId": "N"} afterwards.  When components are copied out of a
donor file and re-arranged, definitions can end up after their references or be lost entirely
(they often live inside the donor's compiledData buffer).  `materialize` turns a copied subtree
into a self-contained one, `fresh_ids` gives it unused handle numbers, and `normalize` fixes the
definition/reference order of a finished document.
"""
from __future__ import annotations

import copy

from .wkjson import index_handles, max_handle_id


def materialize(obj, index: dict, _depth=0):
    """Deep copy of `obj` in which every {"HandleRefId"} is replaced by a full definition taken from
    `index` (HandleId -> wrapper).  Repeated references inside the copy stay references."""
    seen = set()

    def walk(o, depth):
        if depth > 200:
            raise ValueError("handle graph too deep")
        if isinstance(o, dict):
            if "HandleRefId" in o and len(o) == 1:
                hid = str(o["HandleRefId"])
                if hid in seen:
                    return {"HandleRefId": hid}
                w = index.get(hid)
                if w is None:
                    raise KeyError(f"dangling handle reference {hid}")
                seen.add(hid)
                return {"HandleId": hid, "Data": walk(w["Data"], depth + 1)}
            if "HandleId" in o and "Data" in o:
                hid = str(o["HandleId"])
                seen.add(hid)
                return {"HandleId": hid, "Data": walk(o["Data"], depth + 1)}
            return {k: walk(v, depth + 1) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v, depth + 1) for v in o]
        return o

    return walk(obj, _depth)


class IdAllocator:
    def __init__(self, *docs):
        self.next = max([max_handle_id(d) for d in docs] + [-1]) + 1

    def fresh_ids(self, obj):
        """Renumber every handle defined inside `obj` (in place) with unused numbers; references
        inside `obj` to those handles follow."""
        mapping = {}
        stack = [obj]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if "HandleId" in x and "Data" in x:
                    old = str(x["HandleId"])
                    if old not in mapping:
                        mapping[old] = str(self.next)
                        self.next += 1
                    x["HandleId"] = mapping[old]
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        stack = [obj]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if "HandleRefId" in x and str(x["HandleRefId"]) in mapping:
                    x["HandleRefId"] = mapping[str(x["HandleRefId"])]
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        return obj

    def handle(self, data: dict) -> dict:
        h = {"HandleId": str(self.next), "Data": data}
        self.next += 1
        return h


def normalize(root, extra_index: dict | None = None):
    """Make the first occurrence of every handle (in JSON order) its definition and every later
    one a reference.  Definitions may also come from `extra_index` (e.g. the donor document)."""
    index = dict(extra_index or {})
    index.update(index_handles(root))
    emitted = set()

    def walk(o):
        if isinstance(o, dict):
            if "HandleRefId" in o and len(o) == 1:
                hid = str(o["HandleRefId"])
                if hid in emitted:
                    return o
                w = index.get(hid)
                if w is None:
                    raise KeyError(f"dangling handle reference {hid}")
                emitted.add(hid)
                return {"HandleId": hid, "Data": walk(copy.deepcopy(w["Data"]))}
            if "HandleId" in o and "Data" in o:
                hid = str(o["HandleId"])
                if hid in emitted:
                    return {"HandleRefId": hid}
                emitted.add(hid)
                return {"HandleId": hid, "Data": walk(o["Data"])}
            for k in list(o.keys()):
                o[k] = walk(o[k])
            return o
        if isinstance(o, list):
            for i in range(len(o)):
                o[i] = walk(o[i])
            return o
        return o

    return walk(root)


def empty_package(flags: int = 0) -> dict:
    """An empty RedPackage buffer: WolvenKit rebuilds appearance/entity compiled data from the
    `components` (and `entity`) arrays when it writes the file."""
    return {"BufferId": "0", "Flags": flags,
            "Type": "WolvenKit.RED4.Archive.Buffer.RedPackage, WolvenKit.RED4, Version=8.17.4.0, Culture=neutral, PublicKeyToken=null",
            "Data": {"Version": 4, "Sections": 7, "CruidIndex": -1, "CruidDict": {}, "Chunks": []}}
