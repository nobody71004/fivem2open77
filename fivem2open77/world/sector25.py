"""Night City's streaming sectors as the game stores them since update 2.5, read without WolvenKit.

2.5 cooks most of a sector: ``worldStreamingSector`` keeps a few tagged properties (cooking platform,
the resources embedded in it, the ``.streamingsector_inplace`` file next to it, level, category) and
then a cooked tail -- an unknown u32, its size, the node-data buffer (``DataBuffer``), the node
list (a packed count and that many export handles), then node references and the rest.  Static
meshes, generic far-view stand-ins, bended meshes (roads, rails, cables), decals, collisions and
acoustics became one cooked node each per sector, listing what used to be separate nodes;
instanced meshes became one ``worldInstancedMeshNode`` with a cooked transform buffer.  WolvenKit
(up to 9.1.0) knows none of the cooked classes and refuses the whole sector.

What the Studio needs from them, as found by matching sectors read before the update:

    node data      144 bytes a node instance, in the old order: position (4 floats), orientation
                   (4: i j k r), scale (3), pivot (3), bounds (6), at 80 its id (u64), at 88 a far-view
                   stand-in's own id and at 96 the id of the stand-in the instance is under (u64s: a
                   stand-in shows from its nearAutoHideDistance on, and the instances under it then
                   hide), at 112 the node index (u16), at 124 the streaming distance (float), at 128
                   the entry of a cooked node (u16)
    cooked static  a packed byte size, then 33-byte entries: mesh (u64 depot-path hash),
    mesh node      appearance (u64 hash of its name), ...
    cooked generic a packed byte size, then 41-byte entries starting the same way
    proxy node
    cooked bended  a packed byte size, then 30-byte entries: mesh, appearance, u32 first and u32
    mesh node      count of its deformation matrices, u16 index, 4 flag bytes; then a packed count
                   and the 4x4 float matrices (the mesh bent along its length, y)
    instanced      a zero byte, a packed count, 64-byte entries (bounds 8 floats, mesh, appearance,
    mesh node      ...), a packed count and u16 pairs (first instance, count); its cooked buffer
                   holds 48 bytes an instance: position as 3 int32 (1/131072 m), 4 bytes, the
                   orientation (4 floats), the scale (3 floats), 4 bytes
    foliage        (an embedded or separate ``.cfoliage``) its buffer: the populations' positions
                   (3 floats each), scales (1 float each), orientations (4 half floats each)

``decode`` returns what ``sector.read_sector`` does for a sector read through WolvenKit -- entries
with type (cooked classes under their old names), resource, appearance, transform, elements and
foliage -- or None for a sector of the older format; and per entry its streaming distance
(``dist``), a stand-in's ``hide`` distance and ids (``pid``, ``par``: 16 hex digits).  A cooked
entry names its mesh by hash: the sector's own imports and the game's path list (``paths``) turn
it back into the depot path (a mesh no list knows stays ``#<hash>.mesh``: the archives find it by
hash all the same); its appearance stays a hash (``#`` and 16 hex digits: FNV-1a 64 of the name),
which the 3D city matches against the mesh's appearance names.
"""
from __future__ import annotations

import math
import struct

import numpy as np

from ..cp77.cr2w import CR2W, CR2WError, vlq
from ..cp77.package import depot_hash
from . import sector as S

FORMAT = "2.5"
REC = 144
FIXED = 131072.0
OLD_NAME = {"worldCookedStaticMeshNode": "worldStaticMeshNode", "worldCookedGenericProxyMeshNode": "worldGenericProxyMeshNode",
            "worldCookedBendedMeshNode": "worldBendedMeshNode", "worldCookedStaticDecalNode": "worldStaticDecalNode",
            "worldCookedCollisionNode": "worldCollisionNode", "worldCookedAcousticSectorNode": "worldAcousticSectorNode"}
ENTRY = {"worldCookedStaticMeshNode": 33, "worldCookedGenericProxyMeshNode": 41, "worldCookedBendedMeshNode": 30}
RESOURCE_KEYS = ("mesh", "meshRef") + tuple(k for k in S.RESOURCE_KEYS if k != "mesh")
HIDE_AT = {"worldCookedGenericProxyMeshNode": 37}     # its entries' nearAutoHideDistance (float)
PROXY_TYPES = {"worldGenericProxyMeshNode", "worldBuildingProxyMeshNode", "worldTerrainProxyMeshNode", "worldRoadProxyMeshNode",
               "worldEntityProxyMeshNode", "worldDestructibleEntityProxyMeshNode", "worldInvalidProxyMeshNode",
               "worldDestructibleProxyMeshNode"}


class SectorFormatError(CR2WError):
    pass


def is_cooked(c: CR2W) -> bool:
    """A 2.5 sector: no tagged node list (its nodes are in the cooked tail)."""
    if not c.exports or c.exports[0]["cls"] != "worldStreamingSector":
        raise SectorFormatError("not a streaming sector")
    props, _ = c.chunk(0)
    return not any(n == "nodes" for n, _t, _o, _s in props)


def _app_hash(h: int) -> str:
    return "#%016x" % h


def hash_name(h: int) -> str:
    """A mesh no path list knows, by its hash (the archives find it all the same)."""
    return "#%016x.mesh" % h


def hash_of(name: str):
    """hash_name's hash, else None."""
    if isinstance(name, str) and name.startswith("#") and name.lower().endswith(".mesh") and len(name) == 22:
        try:
            return int(name[1:17], 16)
        except ValueError:
            return None
    return None


def _cooked_entries(tail: bytes, size: int) -> list:
    """[(mesh hash, appearance hash, raw entry)] of a cooked static / proxy / bended node."""
    n, o = vlq(tail, 0)
    out = []
    end = min(len(tail), o + n)
    while o + size <= end:
        mh, ah = struct.unpack_from("<QQ", tail, o)
        out.append((mh, ah, tail[o:o + size]))
        o += size
    return out, end


def _bend_matrices(tail: bytes, at: int) -> np.ndarray:
    n, o = vlq(tail, at)
    m = np.frombuffer(tail, "<f4", n * 16, o).reshape(n, 4, 4)
    return m


def _instanced(c: CR2W, chunk: int):
    """[(mesh hash, appearance hash, [(pos, rot, scale)])] of the cooked instanced node."""
    props = c.props(chunk)
    _, tail = c.chunk(chunk)
    o = 1 if tail[:1] == b"\0" else 0
    n, o = vlq(tail, o)
    ents = []
    for _ in range(n):
        mh, ah = struct.unpack_from("<QQ", tail, o + 32)
        ents.append([mh, ah])
        o += 64
    m, o = vlq(tail, o)
    ranges = [struct.unpack_from("<HH", tail, o + 4 * j) for j in range(m)]
    buf = c.data_buffer(props.get("cookedWorldTransformsBuffer") or 0) or b""
    k = len(buf) // 48
    rec = np.frombuffer(buf, np.uint8, k * 48).reshape(k, 48) if k else np.zeros((0, 48), np.uint8)
    pos = rec[:, 0:12].copy().view("<i4").astype(np.float64) / FIXED
    rot = rec[:, 16:32].copy().view("<f4").astype(np.float64)
    scl = rec[:, 32:44].copy().view("<f4").astype(np.float64)
    out = []
    for j, e in enumerate(ents):
        first, count = ranges[j] if j < len(ranges) else (0, 0)
        items = [(pos[i].tolist(), rot[i].tolist(), scl[i].tolist()) for i in range(first, min(first + count, k))]
        out.append((e[0], e[1], items))
    return out


def foliage_from(c: CR2W, chunk: int) -> dict | None:
    """A compiled foliage resource (export ``chunk``) -> {pos, rot, scale} lists."""
    p = c.props(chunk)
    n = int(p.get("populationCount") or 0)
    data = c.data_buffer(p.get("dataBuffer") or 0) or b""
    if len(data) < n * 24:
        return None
    pos = np.frombuffer(data, "<f4", n * 3, 0).reshape(n, 3)
    sc = np.frombuffer(data, "<f4", n, n * 12)
    rot = np.frombuffer(data, "<f2", n * 4, n * 16).reshape(n, 4).astype(np.float32)
    return {"pos": np.round(pos, 4).tolist(), "rot": np.round(rot, 6).tolist(), "scale": np.round(sc, 4).tolist()}


def embedded_foliage(c: CR2W) -> dict:
    """{depot path: populations} of the foliage resources embedded in the sector."""
    b = c.b
    off, count, _crc = struct.unpack_from("<III", b, 40 + 12 * 6)
    out = {}
    for i in range(count):
        imp, chunk, _h = struct.unpack_from("<IIQ", b, off + 16 * i)
        if 0 < imp <= len(c.imports) and 0 <= chunk < len(c.exports) and c.exports[chunk]["cls"] == "worldFoliageCompiledResource":
            pops = foliage_from(c, chunk)
            if pops is not None:
                out[c.imports[imp - 1][0]] = pops
    return out


def decode(data: bytes, path: str, kraken, paths=None) -> dict | None:
    """A 2.5 sector -> sector.read_sector's form (+ ``foliage_data``: its embedded foliage), or
    None when the sector is in the older format (WolvenKit reads those)."""
    c = CR2W(data, kraken)
    if not is_cooked(c):
        return None
    head = c.props(0)
    _, tail = c.chunk(0)
    if len(tail) < 16:
        raise SectorFormatError(f"{path}: no cooked node list")
    nd_ref = struct.unpack_from("<I", tail, 8)[0]
    n, o = vlq(tail, 12)
    handles = struct.unpack_from(f"<{n}I", tail, o)
    nodes = [h - 1 for h in handles]
    nd = c.data_buffer(nd_ref) if nd_ref else b""
    if nd is None or len(nd) % REC or (n and not nd):
        raise SectorFormatError(f"{path}: node data of {len(nd or b'')} bytes")
    if not nd:                                          # (an empty sector)
        return {"path": path, "level": head.get("level"), "category": head.get("category"), "nodes": 0,
                "entries": [], "format": FORMAT, "unresolved": 0}
    recs = np.frombuffer(nd, np.uint8).reshape(-1, REC)
    pos = recs[:, 0:12].copy().view("<f4").astype(np.float64)
    rot = recs[:, 16:32].copy().view("<f4").astype(np.float64)
    scl = recs[:, 32:44].copy().view("<f4").astype(np.float64)
    ids = recs[:, 80:104].copy().view("<u8")              # the record's id, its proxy id, its proxy's
    nidx = recs[:, 112:114].copy().view("<u2")[:, 0]
    dist = recs[:, 124:128].copy().view("<f4")[:, 0]
    sub = recs[:, 128:130].copy().view("<u2")[:, 0]
    # a mesh named by hash: the sector's own imports name most (and the ones only it has, like the
    # roads' bent meshes), the game's path list the rest
    local = {}
    for imp, _fl in c.imports:
        if imp:
            local[depot_hash(imp)] = imp

    def resolve(h):
        got = local.get(h)
        if got is None and paths is not None:
            got = paths.path(h)
        return got

    cache = {}

    def node(k):
        if k in cache:
            return cache[k]
        if not 0 <= k < len(nodes):
            cache[k] = None
            return None
        ch = nodes[k]
        cls = c.exports[ch]["cls"]
        info = {"cls": cls}
        if cls in ENTRY:
            _, t = c.chunk(ch)
            ents, end = _cooked_entries(t, ENTRY[cls])
            info["entries"] = ents
            if cls == "worldCookedBendedMeshNode":
                info["matrices"] = _bend_matrices(t, end)
        elif cls == "worldInstancedMeshNode":
            info["instanced"] = _instanced(c, ch)
        else:
            try:
                info["props"] = c.props(ch)
            except (CR2WError, IndexError, struct.error):
                info["props"] = {}
        cache[k] = info
        return info

    entries = []
    unresolved = 0
    for i in range(len(recs)):
        k = int(nidx[i])
        info = node(k)
        if info is None:
            continue
        cls = info["cls"]
        t = OLD_NAME.get(cls, cls)
        p3, q, s3 = pos[i].tolist(), rot[i].tolist(), scl[i].tolist()
        ent = {"i": i, "n": k, "type": t, "kind": S.KINDS.get(t, "other"), "name": "", "res": "", "app": "",
               "pos": [round(v, 4) for v in p3], "rot": [round(v, 6) for v in q], "yaw": round(S.yaw_of(q), 2),
               "scale": [round(v, 4) for v in s3], "dist": round(float(dist[i]), 1)}
        if t in PROXY_TYPES and ids[i, 1]:
            ent["pid"] = "%016x" % int(ids[i, 1])           # the far-view stand-in's own id...
        if ids[i, 2]:
            ent["par"] = "%016x" % int(ids[i, 2])           # ...which the nodes it stands in for name
        center = p3
        if "entries" in info:
            j = int(sub[i])
            if j < len(info["entries"]):
                mh, ah, raw = info["entries"][j]
                res = resolve(mh)
                if res is None:
                    unresolved += 1
                    res = hash_name(mh)
                ent["res"], ent["app"] = res, _app_hash(ah)
                at = HIDE_AT.get(cls)
                if at is not None and len(raw) >= at + 4:
                    ent["hide"] = round(float(struct.unpack_from("<f", raw, at)[0]), 1)
                if cls == "worldCookedBendedMeshNode":
                    first, count = struct.unpack_from("<II", raw, 16)
                    m = info["matrices"][first:first + count]
                    ent["bend"] = [[round(float(v), 5) for v in mm.reshape(-1)] for mm in m]
        elif "instanced" in info:
            j = int(sub[i])
            if j < len(info["instanced"]):
                mh, ah, items = info["instanced"][j]
                res = resolve(mh)
                if res is None:
                    unresolved += 1
                    res = hash_name(mh)
                ent["res"], ent["app"] = res, _app_hash(ah)
                pts = []
                for ip, iq, isc in items:
                    wp = [a + b for a, b in zip(p3, S.quat_rotate(q, ip))]
                    wq = S.quat_mul(q, iq)
                    pts.append({"pos": [round(v, 4) for v in wp], "rot": [round(v, 6) for v in wq],
                                "yaw": round(S.yaw_of(wq), 2), "scale": [round(v, 4) for v in isc]})
                ent["elements"] = {"kind": "instances", "count": len(pts), "items": pts}
                if pts:
                    center = [sum(pt["pos"][a] for pt in pts) / len(pts) for a in range(3)]
        else:
            p = info.get("props") or {}
            for key in RESOURCE_KEYS:
                if p.get(key):
                    ent["res"] = str(p[key])
                    break
            for key in S.APPEARANCE_KEYS:
                if p.get(key):
                    ent["app"] = str(p[key])
                    break
            if p.get("debugName"):
                ent["name"] = str(p["debugName"])
            if "nearAutoHideDistance" in p:
                try:
                    ent["hide"] = round(float(p["nearAutoHideDistance"]), 1)
                except (TypeError, ValueError):
                    pass
            if cls == "worldInstancedDestructibleMeshNode" and isinstance(p.get("instanceTransforms"), list):
                pts = []
                for tr in p["instanceTransforms"]:
                    tp = tr.get("position") or {}
                    tq = tr.get("orientation") or {}
                    ip = [float(tp.get("X", 0.0)), float(tp.get("Y", 0.0)), float(tp.get("Z", 0.0))]
                    iq = [float(tq.get("i", 0.0)), float(tq.get("j", 0.0)), float(tq.get("k", 0.0)), float(tq.get("r", 1.0))]
                    wp = [a + b for a, b in zip(p3, S.quat_rotate(q, ip))]
                    wq = S.quat_mul(q, iq)
                    pts.append({"pos": [round(v, 4) for v in wp], "rot": [round(v, 6) for v in wq],
                                "yaw": round(S.yaw_of(wq), 2), "scale": [1.0, 1.0, 1.0]})
                ent["elements"] = {"kind": "instances", "count": len(pts), "items": pts}
                if pts:
                    center = [sum(pt["pos"][a] for pt in pts) / len(pts) for a in range(3)]
            if cls == "worldFoliageNode":
                span = p.get("populationSpanInfo") or {}
                ent["foliage"] = {"res": str(p.get("foliageResource") or ""),
                                  "begin": int(span.get("stancesBegin") or 0), "count": int(span.get("stancesCount") or 0)}
        ent["center"] = [round(v, 3) for v in center]
        entries.append(ent)
    out = {"path": path, "level": head.get("level"), "category": head.get("category"), "nodes": len(recs),
           "entries": entries, "format": FORMAT, "unresolved": unresolved}
    fol = embedded_foliage(c)
    if fol:
        out["foliage_data"] = fol
    return out


def foliage_file(data: bytes, kraken) -> dict | None:
    """A separate ``.cfoliage`` file -> {pos, rot, scale}."""
    c = CR2W(data, kraken)
    for i, e in enumerate(c.exports):
        if e["cls"] == "worldFoliageCompiledResource":
            return foliage_from(c, i)
    return None


def quat_ok(q) -> bool:
    n = math.sqrt(sum(v * v for v in q))
    return 0.9 < n < 1.1
