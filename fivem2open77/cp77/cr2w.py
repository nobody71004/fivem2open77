"""A reader of the game's CR2W resource files, enough for what the Studio reads straight from the
game (streaming sectors and what they hold), without WolvenKit.

Layout (version 195): a 40-byte header (``CR2W``, version, flags, u64 time, build, objects end,
buffers end, crc, chunk count) and ten 12-byte table descriptors (offset, count, crc): strings,
names (u32 string offset, u32 hash), imports (u32 string offset, u16 class, u16 flags), properties,
exports (u16 class, u16 flags, u32 parent, u32 data size, u32 data offset, u32 template, u32 crc),
buffers (u32 flags, u32 index, u32 offset, u32 size on disk, u32 size, u32 crc) and embedded files.

An export's data is its tagged properties -- a zero byte, then for each property u16 name, u16 type
(both name indices), u32 size (with itself) and the value, ending with a 0 name -- and, for classes
the game cooks into a binary form of their own (``tail``), whatever follows.  Values: a CName or an
enum is a u16 name index; a resource reference (``raRef:``/``rRef:``) a u16 import index (from 1);
a handle an i32 export index (from 1); an array a u32 count and its elements; a struct the same
tagged form nested; a DataBuffer a u32 (``0x80000000`` | buffer index from 1).
"""
from __future__ import annotations

import re
import struct

PRIMS = {"Float": ("<f", 4), "Double": ("<d", 8), "Bool": ("<B", 1), "Uint8": ("<B", 1), "Int8": ("<b", 1),
         "Uint16": ("<H", 2), "Int16": ("<h", 2), "Uint32": ("<I", 4), "Int32": ("<i", 4), "Uint64": ("<Q", 8),
         "Int64": ("<q", 8), "TweakDBID": ("<Q", 8), "CRUID": ("<Q", 8)}
BUFFER_TYPES = ("DataBuffer", "serializationDeferredDataBuffer", "SharedDataBuffer")
STATIC_RX = re.compile(r"^(?:\[(\d+)\]|static:(?:\d+),)(.+)$")


class CR2WError(ValueError):
    pass


def vlq(b: bytes, o: int) -> tuple:
    """The game's packed int (bit 7 sign, bit 6 more, 6 bits; then 7 bits a byte, bit 7 more)."""
    b1 = b[o]
    o += 1
    sign, more, v, sh = b1 & 0x80, b1 & 0x40, b1 & 0x3F, 6
    while more:
        bb = b[o]
        o += 1
        v |= (bb & 0x7F) << sh
        more = bb & 0x80
        sh += 7
    return (-v if sign else v), o


class CR2W:
    def __init__(self, data: bytes, kraken=None):
        self.b = b = data
        self.kraken = kraken
        if b[:4] != b"CR2W":
            raise CR2WError("not a CR2W file")
        (_m, self.version, self.flags, _ts, self.build, self.objects_end, self.buffers_end, _crc,
         _n) = struct.unpack_from("<4sIIQIIIII", b, 0)
        t = [struct.unpack_from("<III", b, 40 + 12 * i) for i in range(10)]
        s = b[t[0][0]:t[0][0] + t[0][1]]

        def sat(o):
            e = s.find(b"\0", o)
            return s[o:e if e >= 0 else len(s)].decode("utf-8", "replace")
        self.names = [sat(struct.unpack_from("<II", b, t[1][0] + 8 * i)[0]) for i in range(t[1][1])]
        self.imports = []
        for i in range(t[2][1]):
            o, _c, fl = struct.unpack_from("<IHH", b, t[2][0] + 8 * i)
            self.imports.append((sat(o), fl))
        self.exports = []
        for i in range(t[4][1]):
            cls, of, parent, dsize, doff, _tmpl, _crc = struct.unpack_from("<HHIIIII", b, t[4][0] + 24 * i)
            self.exports.append({"cls": self.names[cls], "parent": parent, "size": dsize, "off": doff})
        self.buffers = [dict(zip(("flags", "index", "off", "disk", "mem", "crc"), struct.unpack_from("<IIIIII", b, t[5][0] + 24 * i)))
                        for i in range(t[5][1])]

    # -- buffers
    def buffer(self, i: int) -> bytes:
        """Buffer ``i`` (from 0), decompressed."""
        e = self.buffers[i]
        raw = self.b[e["off"]:e["off"] + e["disk"]]
        if e["disk"] == e["mem"]:
            return raw
        if self.kraken is None:
            raise CR2WError("a compressed buffer and no Kraken decoder")
        return self.kraken.unpack(raw, e["mem"])

    def data_buffer(self, ref: int) -> bytes | None:
        i = (ref & 0x7FFFFFFF) - 1
        return self.buffer(i) if 0 <= i < len(self.buffers) else None

    # -- exports
    def tagged(self, off: int, end: int) -> tuple:
        """[(name, type, value offset, value size)] and where the tagged part ends."""
        b = self.b
        if b[off] != 0:
            raise CR2WError(f"tagged data at {off} does not start with 0")
        o = off + 1
        out = []
        while o + 2 <= end:
            ni = struct.unpack_from("<H", b, o)[0]
            o += 2
            if ni == 0:
                break
            ti, size = struct.unpack_from("<HI", b, o)
            o += 6
            out.append((self.names[ni], self.names[ti], o, size - 4))
            o += size - 4
        return out, o

    def chunk(self, i: int) -> tuple:
        """(tagged properties, the cooked tail bytes) of export ``i`` (from 0)."""
        e = self.exports[i]
        props, tail = self.tagged(e["off"], e["off"] + e["size"])
        return props, self.b[tail:e["off"] + e["size"]]

    def props(self, i: int) -> dict:
        """Export ``i``'s tagged properties, decoded (see value)."""
        props, _ = self.chunk(i)
        return {n: self.value(t, o, sz) for n, t, o, sz in props}

    def tail_props(self, i: int) -> list:
        """The properties some classes cook after their tagged ones (a material instance's
        parameters): u32 count, then each u32 size (with itself), u16 name, u16 type and the value
        -> [(name, type, value offset, value size)]; [] when the tail is not in that form."""
        e = self.exports[i]
        _, tail = self.chunk(i)
        base = e["off"] + e["size"] - len(tail)
        if len(tail) < 4:
            return []
        n = struct.unpack_from("<I", tail, 0)[0]
        p, out = 4, []
        for _ in range(n):
            if p + 8 > len(tail):
                return []
            size, ni, ti = struct.unpack_from("<IHH", tail, p)
            if size < 8 or p + size > len(tail) or ni >= len(self.names) or ti >= len(self.names):
                return []
            out.append((self.names[ni], self.names[ti], base + p + 8, size - 8))
            p += size
        return out

    def embedded(self) -> dict:
        """{depot path: export index} of the files embedded in this one (table 6: u32 import from 1,
        u32 export, u64 hash)."""
        off, count, _crc = struct.unpack_from("<III", self.b, 40 + 12 * 6)
        out = {}
        for i in range(count):
            imp, chunk, _h = struct.unpack_from("<IIQ", self.b, off + 16 * i)
            if 0 < imp <= len(self.imports) and 0 <= chunk < len(self.exports):
                out[self.imports[imp - 1][0].lower()] = chunk
        return out

    def value(self, ty: str, o: int, size: int):
        b = self.b
        if ty in PRIMS:
            fmt, n = PRIMS[ty]
            return struct.unpack_from(fmt, b, o)[0]
        if ty == "CName":
            i = struct.unpack_from("<H", b, o)[0]
            return self.names[i] if i < len(self.names) else ""
        if ty.startswith(("raRef:", "rRef:")):
            i = struct.unpack_from("<H", b, o)[0]
            return self.imports[i - 1][0] if 0 < i <= len(self.imports) else ""
        if ty.startswith(("handle:", "whandle:")):
            return struct.unpack_from("<i", b, o)[0] - 1
        if ty in BUFFER_TYPES:
            return struct.unpack_from("<I", b, o)[0]
        static = STATIC_RX.match(ty)
        if ty.startswith("array:") or static:
            et = ty[6:] if not static else static.group(2)
            n = struct.unpack_from("<I", b, o)[0]
            p = o + 4
            out = []
            for _ in range(n):
                if et in PRIMS:
                    fmt, w = PRIMS[et]
                    out.append(struct.unpack_from(fmt, b, p)[0])
                    p += w
                elif et == "CName" or et.startswith(("raRef:", "rRef:")):
                    out.append(self.value(et, p, 2))
                    p += 2
                elif et.startswith(("handle:", "whandle:")):
                    out.append(struct.unpack_from("<i", b, p)[0] - 1)
                    p += 4
                else:
                    props, p = self.tagged(p, o + size)
                    out.append({pn: self.value(pt, po, ps) for pn, pt, po, ps in props})
            return out
        if size == 2:                                   # an enum: its value's name
            i = struct.unpack_from("<H", b, o)[0]
            return self.names[i] if i < len(self.names) else i
        if size > 0 and b[o] == 0:                      # a struct
            try:
                props, _ = self.tagged(o, o + size)
                return {pn: self.value(pt, po, ps) for pn, pt, po, ps in props}
            except (CR2WError, IndexError, struct.error):
                pass
        return b[o:o + size]
