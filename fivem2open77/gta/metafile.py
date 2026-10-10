"""RSC7 "Meta" resources (.ymap / .ytyp, resource version 2).

A Meta file carries its own type information: a table of structure infos (field name hash, data
offset, data type) and a list of typed data blocks.  This module decodes any Meta file generically
into plain Python values (dicts, lists, numbers, tuples) using those embedded infos, so it needs no
hard-coded layouts.  Field and type names are resolved through RAGE's case-sensitive joaat with the
name table in ``data/gta_meta_names.txt.gz``; unknown hashes stay as ``"0x1234ABCD"`` strings.

Layout notes follow CodeWalker's ``Meta.cs`` / ``MetaXml.cs`` (MIT):

    Meta header (system segment 0x50000000)
      0x10 i32 Unknown_10h   0x14 i16   0x16 u8 HasEncryptedStrings   0x17 u8   0x18 i32
      0x1C i32 RootBlockIndex (1-based)
      0x20 ptr StructureInfos 0x28 ptr EnumInfos 0x30 ptr DataBlocks 0x38 ptr Name 0x40 ptr EncStrings
      0x48 i16 StructureInfosCount 0x4A i16 EnumInfosCount 0x4C i16 DataBlocksCount

    Data-block pointers inside the data are (blockId & 0xFFF) | (offset << 12), blockId 1-based.
"""
from __future__ import annotations

import gzip
import os
import struct

from .jenkhash import joaat
from .rsc7 import ResourceError, read_rsc7

_NAMES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gta_meta_names.txt.gz")

# MetaStructureEntryDataType
BOOLEAN = 0x01
SBYTE, UBYTE, SSHORT, USHORT, SINT, UINT = 0x10, 0x11, 0x12, 0x13, 0x14, 0x15
FLOAT = 0x21
FLOAT_XYZ, FLOAT_XYZW = 0x33, 0x34
BYTE_ENUM, INT_ENUM, SHORT_FLAGS, INT_FLAGS1, INT_FLAGS2 = 0x60, 0x62, 0x64, 0x63, 0x65
HASH = 0x4A
ARRAY = 0x52
ARRAY_OF_CHARS = 0x40
ARRAY_OF_BYTES = 0x50
DATA_BLOCK_POINTER = 0x59
CHAR_POINTER = 0x44
STRUCTURE_POINTER = 0x07
STRUCTURE = 0x05

# MetaTypeName: structure name hashes of primitive data blocks
T_VECTOR4, T_HASH, T_STRING, T_POINTER, T_USHORT, T_UINT, T_ARRAYINFO, T_BYTE, T_FLOAT = (
    0x33, 0x4A, 0x10, 0x7, 0x13, 0x15, 0x100, 17, 33)

_SCALAR = {
    BOOLEAN: ("<?", 1), SBYTE: ("<b", 1), UBYTE: ("<B", 1), SSHORT: ("<h", 2), USHORT: ("<H", 2),
    SINT: ("<i", 4), UINT: ("<I", 4), FLOAT: ("<f", 4), BYTE_ENUM: ("<B", 1), INT_ENUM: ("<i", 4),
    SHORT_FLAGS: ("<h", 2), INT_FLAGS1: ("<i", 4), INT_FLAGS2: ("<i", 4),
}
# primitive array element: (struct name of the block holding them, format, item size)
_ARRAY_ITEMS = {
    UINT: (T_UINT, "<I", 4), USHORT: (T_USHORT, "<H", 2), UBYTE: (T_BYTE, "<B", 1),
    FLOAT: (T_FLOAT, "<f", 4), HASH: (T_HASH, "<I", 4), FLOAT_XYZ: (T_VECTOR4, "<4f", 16),
}


class _Names:
    _map = None

    @classmethod
    def table(cls) -> dict:
        if cls._map is None:
            m = {}
            try:
                with gzip.open(_NAMES_FILE, "rt", encoding="utf-8") as f:
                    for line in f:
                        n = line.strip()
                        if n and not n.startswith("#"):
                            m.setdefault(joaat(n, lower=False), n)
            except OSError:
                pass
            cls._map = m
        return cls._map


def meta_name(h: int) -> str:
    return _Names.table().get(h & 0xFFFFFFFF) or f"0x{h & 0xFFFFFFFF:08X}"


def mhash(name: str) -> int:
    """Hash of a parser name (case-sensitive), e.g. mhash('CEntityDef')."""
    return joaat(name, lower=False)


class MetaHash(int):
    """A hash value that prints its name when known (archetype names, txd names...)."""

    names = {}

    def __repr__(self):
        return self.names.get(int(self)) or f"0x{int(self):08X}"

    __str__ = __repr__


class StructInfo:
    __slots__ = ("name", "size", "entries")

    def __init__(self, name, size, entries):
        self.name = name
        self.size = size
        self.entries = entries   # [(name_hash, offset, dtype, ref_index, ref_key)]


class MetaFile:
    def __init__(self, data: bytes, name: str = ""):
        self.name = name
        rsc = read_rsc7(bytes(data), name=name)
        if rsc.version != 2:
            raise ResourceError(f"{name}: not a Meta resource (version {rsc.version}, expected 2)")
        sysb = rsc.system
        self._sys = sysb
        (root_index,) = struct.unpack_from("<i", sysb, 0x1C)
        si_ptr, ei_ptr, db_ptr = struct.unpack_from("<QQQ", sysb, 0x20)
        si_count, ei_count, db_count = struct.unpack_from("<hhh", sysb, 0x48)
        self.root_index = root_index
        self.structs = {}
        for i in range(si_count):
            off = self._off(si_ptr) + i * 32
            nh, _key, _u8, _uc, eptr, ssize, _u1c, ecount = struct.unpack_from("<IIIIQihh", sysb, off)
            entries = []
            eoff = self._off(eptr)
            for j in range(ecount):
                enh, doff, dtype, _u9, ref_idx, ref_key = struct.unpack_from("<IiBBhI", sysb, eoff + j * 16)
                entries.append((enh, doff, dtype, ref_idx, ref_key))
            self.structs[nh] = StructInfo(nh, ssize, entries)
        self.enums = {}
        for i in range(ei_count):
            off = self._off(ei_ptr) + i * 24
            nh, _key, eptr, ecount, _u14 = struct.unpack_from("<IIQii", sysb, off)
            vals = {}
            eoff = self._off(eptr)
            for j in range(ecount):
                enh, val = struct.unpack_from("<Ii", sysb, eoff + j * 8)
                vals[val] = enh
            self.enums[nh] = vals
        self.blocks = []   # [(struct name hash, bytes)]
        for i in range(db_count):
            off = self._off(db_ptr) + i * 16
            nh, dlen, dptr = struct.unpack_from("<IiQ", sysb, off)
            buf, boff = self._seg(rsc, dptr)
            self.blocks.append((nh, bytes(buf[boff:boff + dlen])))

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _off(ptr: int) -> int:
        return ptr & 0x0FFFFFFF

    @staticmethod
    def _seg(rsc, ptr):
        if (ptr & 0x60000000) == 0x60000000:
            return rsc.graphics, ptr & 0x0FFFFFFF
        return rsc.system, ptr & 0x0FFFFFFF

    def block(self, block_id: int):
        i = block_id - 1
        if 0 <= i < len(self.blocks):
            return self.blocks[i]
        return None

    # ------------------------------------------------------------------ decoding
    def root(self) -> dict:
        b = self.block(self.root_index)
        if b is None:
            raise ResourceError(f"{self.name}: no root block")
        return self.decode(self.root_index, 0, b[0])

    def decode(self, block_id: int, offset: int, struct_hash: int | None = None) -> dict:
        blk = self.block(block_id)
        if blk is None:
            return {}
        if struct_hash is None:
            struct_hash = blk[0]
        info = self.structs.get(struct_hash)
        if info is None:
            return {"_type": meta_name(struct_hash), "_error": "no structure info"}
        data = blk[1]
        out = {"_type": meta_name(struct_hash)}
        arr_info = None
        for idx, (enh, doff, dtype, ref_idx, ref_key) in enumerate(info.entries):
            if enh == T_ARRAYINFO:
                arr_info = (enh, doff, dtype, ref_idx, ref_key)
                continue
            name = meta_name(enh)
            eo = offset + doff
            try:
                out[name] = self._value(data, eo, block_id, dtype, ref_idx, ref_key, info, arr_info)
            except (struct.error, IndexError):
                out[name] = None
        return out

    def _value(self, data, eo, block_id, dtype, ref_idx, ref_key, info, arr_info):
        if dtype in _SCALAR:
            fmt, _ = _SCALAR[dtype]
            return struct.unpack_from(fmt, data, eo)[0]
        if dtype == HASH:
            return MetaHash(struct.unpack_from("<I", data, eo)[0])
        if dtype == FLOAT_XYZ:
            return struct.unpack_from("<3f", data, eo)
        if dtype == FLOAT_XYZW:
            return struct.unpack_from("<4f", data, eo)
        if dtype == STRUCTURE:
            return self.decode(block_id, eo, ref_key)
        if dtype == ARRAY_OF_CHARS:
            raw = data[eo:eo + ref_key]
            return raw.split(b"\0", 1)[0].decode("ascii", "replace")
        if dtype == CHAR_POINTER:
            ptr, count = struct.unpack_from("<QH", data, eo)
            blk = self.block(ptr & 0xFFF)
            if blk is None or not count:
                return ""
            o = (ptr >> 12) & 0xFFFFF
            return blk[1][o:o + count].split(b"\0", 1)[0].decode("ascii", "replace")
        if dtype == DATA_BLOCK_POINTER:
            (ptr,) = struct.unpack_from("<Q", data, eo)
            blk = self.block(ptr & 0xFFF)
            return blk[1] if blk else b""
        if dtype == ARRAY_OF_BYTES:
            et = arr_info[2] if arr_info else UBYTE
            fmt, size = _SCALAR.get(et, ("<B", 1))
            return [struct.unpack_from(fmt, data, eo + i * size)[0] for i in range(ref_key)]
        if dtype == ARRAY:
            ainfo = info.entries[ref_idx] if 0 <= ref_idx < len(info.entries) else arr_info
            return self._array(data, eo, ainfo)
        if dtype == STRUCTURE_POINTER:
            return None
        return None

    def _array(self, data, eo, ainfo):
        if ainfo is None:
            return []
        ptr, count = struct.unpack_from("<QH", data, eo)
        if not count:
            return []
        bid, off = ptr & 0xFFF, (ptr >> 12) & 0xFFFFF
        et, ekey = ainfo[2], ainfo[4]
        if et == STRUCTURE:
            sinfo = self.structs.get(ekey)
            if sinfo is None:
                return []
            items = []
            for _ in range(count):
                blk = self.block(bid)
                if blk is None:
                    break
                if off >= len(blk[1]):
                    bid, off = bid + 1, 0
                    if self.block(bid) is None:
                        break
                items.append(self.decode(bid, off, ekey))
                off += sinfo.size
            return items
        if et == STRUCTURE_POINTER:
            blk = self.block(bid)
            if blk is None or blk[0] != T_POINTER:
                return []
            items = []
            for i in range(count):
                o = off + i * 8
                if o + 8 > len(blk[1]):
                    break
                (p,) = struct.unpack_from("<Q", blk[1], o)
                items.append(self.decode(p & 0xFFF, (p >> 12) & 0xFFFFF))
            return items
        if et in _ARRAY_ITEMS:
            want, fmt, size = _ARRAY_ITEMS[et]
            items = []
            idx = bid - 1
            o = off
            while len(items) < count and 0 <= idx < len(self.blocks):
                nh, buf = self.blocks[idx]
                if nh != want:
                    break
                while len(items) < count and o + size <= len(buf):
                    v = struct.unpack_from(fmt, buf, o)
                    items.append(v if len(v) > 1 else v[0])
                    o += size
                idx += 1
                o = 0
            if et == HASH:
                items = [MetaHash(v) for v in items]
            elif et == FLOAT_XYZ:
                items = [tuple(v[:3]) for v in items]
            return items
        return []


def load_meta(path_or_bytes, name: str = "") -> dict:
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    return MetaFile(bytes(data), name=name or str(path_or_bytes)).root()
