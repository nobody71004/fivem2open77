"""Small CR2W files and archives for the tests: what cp77/cr2w.py, cp77/archive.py, world/sector25.py
and cp77/meshread.py read, written by hand (no game file in the tests).  Buffers are stored as
they are (no Kraken): a file whose buffers' two sizes are equal needs no decoder."""
from __future__ import annotations

import struct

import numpy as np


def depot_hash(path: str) -> int:
    h = 0xCBF29CE484222325
    for b in str(path).replace("/", "\\").lower().encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def fnv(name: str) -> int:
    h = 0xCBF29CE484222325
    for b in str(name).encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class V:
    """A typed value for a tagged property: (type name, encoder)."""

    def __init__(self, ty, enc):
        self.ty, self.enc = ty, enc


class Writer:
    def __init__(self):
        self.names, self._nix = [""], {"": 0}
        self.imports = []                    # (path, flags)
        self.exports = []                    # (class, data)
        self.buffers = []                    # bytes
        self.embedded = []                   # (import index from 1, export index)

    # ---------------------------------------------------------------- names, imports, buffers
    def n(self, s: str) -> int:
        i = self._nix.get(s)
        if i is None:
            i = self._nix[s] = len(self.names)
            self.names.append(s)
        return i

    def imp(self, path: str, flags: int = 0) -> int:
        for i, (p, _f) in enumerate(self.imports):
            if p == path:
                return i + 1
        self.imports.append((path, flags))
        return len(self.imports)

    def buffer(self, data: bytes) -> int:
        self.buffers.append(bytes(data))
        return 0x80000000 | len(self.buffers)

    def embed(self, path: str, export: int):
        self.embedded.append((self.imp(path, 8), export))

    # ---------------------------------------------------------------- values
    def f32(self, v):
        return V("Float", lambda: struct.pack("<f", v))

    def u8(self, v):
        return V("Uint8", lambda: struct.pack("<B", v))

    def u16(self, v):
        return V("Uint16", lambda: struct.pack("<H", v))

    def u32(self, v):
        return V("Uint32", lambda: struct.pack("<I", v))

    def u64(self, v):
        return V("Uint64", lambda: struct.pack("<Q", v))

    def cname(self, s):
        return V("CName", lambda: struct.pack("<H", self.n(s)))

    def enum(self, ty, s):
        return V(ty, lambda: struct.pack("<H", self.n(s)))

    def rref(self, path, cls="CResource", flags=0, kind="rRef"):
        return V(f"{kind}:{cls}", lambda: struct.pack("<H", self.imp(path, flags)))

    def handle(self, export_index, cls="ISerializable"):
        return V(f"handle:{cls}", lambda: struct.pack("<i", export_index + 1))

    def databuffer(self, ref, ty="DataBuffer"):
        return V(ty, lambda: struct.pack("<I", ref))

    def struct(self, ty, props: dict):
        return V(ty, lambda: self.tagged(props))

    def array(self, elem_ty, items, static: int | None = None):
        ty = f"[{static}]{elem_ty}" if static else f"array:{elem_ty}"

        def enc():
            out = struct.pack("<I", len(items))
            for it in items:
                out += it.enc() if isinstance(it, V) else it
            return out
        return V(ty, enc)

    def vec(self, ty="Vector4", **kw):
        return self.struct(ty, {k: self.f32(v) for k, v in kw.items()})

    def tagged(self, props: dict) -> bytes:
        out = b"\0"
        for name, v in props.items():
            data = v.enc()
            out += struct.pack("<HHI", self.n(name), self.n(v.ty), len(data) + 4) + data
        return out + struct.pack("<H", 0)

    def cooked_params(self, props: dict) -> bytes:
        """A material instance's parameters as cooked after its tagged properties."""
        out = struct.pack("<I", len(props))
        for name, v in props.items():
            data = v.enc()
            out += struct.pack("<IHH", len(data) + 8, self.n(name), self.n(v.ty)) + data
        return out

    # ---------------------------------------------------------------- exports
    def export(self, cls: str, props: dict | None = None, tail: bytes = b"") -> int:
        self.exports.append((cls, props or {}, tail))
        return len(self.exports) - 1

    # ---------------------------------------------------------------- the file
    def build(self) -> bytes:
        # (names first: the exports' data names more of them)
        datas = [self.tagged(p) + t for _c, p, t in self.exports]
        cls_ix = [self.n(c) for c, _p, _t in self.exports]
        strings = b""
        str_off = {}

        def s_at(s):
            nonlocal strings
            if s not in str_off:
                str_off[s] = len(strings)
                strings += s.encode("utf-8") + b"\0"
            return str_off[s]
        name_rows = [struct.pack("<II", s_at(nm), 0) for nm in self.names]
        imp_rows = [struct.pack("<IHH", s_at(p), 0, fl) for p, fl in self.imports]
        head = 40 + 120
        t_str = head
        t_names = t_str + len(strings)
        t_imps = t_names + 8 * len(name_rows)
        t_props = t_imps + 8 * len(imp_rows)
        t_exps = t_props
        t_bufs = t_exps + 24 * len(datas)
        t_emb = t_bufs + 24 * len(self.buffers)
        data_off = t_emb + 16 * len(self.embedded)
        exp_rows, off = [], data_off
        for ci, d in zip(cls_ix, datas):
            exp_rows.append(struct.pack("<HHIIIII", ci, 0, 0, len(d), off, 0, 0))
            off += len(d)
        objects_end = off
        buf_rows = []
        for i, b in enumerate(self.buffers):
            buf_rows.append(struct.pack("<IIIIII", 0, i, off, len(b), len(b), 0))
            off += len(b)
        emb_rows = [struct.pack("<IIQ", imp, ex, 0) for imp, ex in self.embedded]
        tables = [(t_str, len(strings)), (t_names, len(name_rows)), (t_imps, len(imp_rows)), (t_props, 0),
                  (t_exps, len(exp_rows)), (t_bufs, len(buf_rows)), (t_emb, len(emb_rows)), (0, 0), (0, 0), (0, 0)]
        hdr = struct.pack("<4sIIQIIIII", b"CR2W", 195, 0, 0, 0, objects_end, off, 0, len(datas))
        hdr += b"".join(struct.pack("<III", o, n, 0) for o, n in tables)
        out = hdr + strings + b"".join(name_rows) + b"".join(imp_rows) + b"".join(exp_rows) + b"".join(buf_rows)
        out += b"".join(emb_rows)
        assert len(out) == data_off, (len(out), data_off)
        return out + b"".join(datas) + b"".join(self.buffers)


def vlq(n: int) -> bytes:
    """The game's packed int (positive)."""
    first = n & 0x3F
    n >>= 6
    out = bytearray([first | (0x40 if n else 0)])
    while n:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
    return bytes(out)


def record(pos, rot=(0, 0, 0, 1), scale=(1, 1, 1), node=0, sub=0, dist=100.0, rid=0, proxy_id=0, parent=0) -> bytes:
    """One 144-byte node-data record of a 2.5 sector."""
    r = bytearray(144)
    struct.pack_into("<4f", r, 0, *pos, 0.0)
    struct.pack_into("<4f", r, 16, *rot)
    struct.pack_into("<3f", r, 32, *scale)
    struct.pack_into("<3Q", r, 80, rid, proxy_id, parent)
    struct.pack_into("<H", r, 112, node)
    struct.pack_into("<f", r, 124, dist)
    struct.pack_into("<H", r, 128, sub)
    return bytes(r)


def archive(files: dict) -> bytes:
    """An RDAR archive of {depot path or hash: bytes} (stored, one segment each)."""
    body = bytearray(b"\0" * 40)
    entries, segs = [], []
    for key, data in files.items():
        h = key if isinstance(key, int) else depot_hash(key)
        off = len(body)
        body += data
        entries.append((h, len(segs)))
        segs.append((off, len(data), len(data)))
    index_pos = len(body)
    table = struct.pack("<III", len(entries), len(segs), 0)
    for h, si in entries:
        table += struct.pack("<QqIIIII", h, 0, 0, si, si + 1, 0, 0) + b"\0" * 20
    for off, z, size in segs:
        table += struct.pack("<QII", off, z, size)
    index = struct.pack("<IIQ", 8, len(table) + 8, 0) + table
    struct.pack_into("<4sIQI", body, 0, b"RDAR", 12, index_pos, len(index))
    return bytes(body) + index


def short4n(p, scale, offset) -> np.ndarray:
    q = np.round((np.asarray(p, np.float64) - offset) / scale * 32767.0).astype(np.int16)
    return np.concatenate([q, np.zeros((len(q), 1), np.int16)], axis=1)


def dec4(n) -> np.ndarray:
    n = np.asarray(n, np.float64)
    q = np.clip(np.round(n * 511.5 + 511.5), 0, 1023).astype(np.uint32)
    return q[:, 0] | (q[:, 1] << 10) | (q[:, 2] << 20)
