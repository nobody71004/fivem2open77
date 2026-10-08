"""GTA V RPF7 archives (the dlc.rpf of a single-player add-on car).

Single-player car packs ship as ``dlc.rpf`` with the models in a nested ``x64/vehicles.rpf`` and the
metas under ``common/data``.  Archives saved by OpenIV use the "OPEN" format: the table of contents
is plain, so they can be read without any key.  The game's own archives (AES/NG) are encrypted with
keys that only the game executable has; those are refused with a clear message.

Layout::

    header   magic "RPF7" (LE 0x52504637), entry count, names length, encryption   (4 x uint32)
    entries  16 bytes each; entry 0 is the root directory
    names    null-terminated strings, addressed by each entry's name offset
    data     512-byte blocks; a nested archive's offsets are relative to its own start

Resource files (yft, ytd, ...) are stored with their 16-byte RSC7 header, so their bytes are exactly
a standalone resource file.  Binary files are raw deflate when compressed.
"""
from __future__ import annotations

import os
import struct
import zlib

MAGIC = 0x52504637
ENC_NONE = 0
ENC_OPEN = 0x4E45504F          # "OPEN"
ENC_NAMES = {0x0FFFFFF9: "AES", 0x0FEFFFFF: "NG"}
BLOCK = 512


class RpfError(Exception):
    pass


class RpfEntry:
    __slots__ = ("name", "path", "kind", "offset", "size", "usize", "encrypted", "children")

    def __init__(self, name, kind):
        self.name = name
        self.path = name
        self.kind = kind            # "dir" | "file" | "resource"
        self.offset = 0
        self.size = 0               # stored size (0 = stored uncompressed, usize bytes)
        self.usize = 0
        self.encrypted = False
        self.children = []


class RpfArchive:
    """One RPF7 archive held in memory (``data`` is the archive's own bytes)."""

    def __init__(self, data, label="archive.rpf"):
        self.data = memoryview(data)
        self.label = label
        if len(self.data) < 16:
            raise RpfError(f"{label}: too short for an RPF archive")
        magic, count, names_len, enc = struct.unpack_from("<IIII", self.data, 0)
        if magic != MAGIC:
            raise RpfError(f"{label}: not an RPF7 archive")
        if enc not in (ENC_NONE, ENC_OPEN):
            kind = ENC_NAMES.get(enc, f"0x{enc:08X}")
            raise RpfError(f"{label} is encrypted ({kind}). Only unencrypted (OpenIV 'OPEN') archives can be "
                           "read; open it in OpenIV or CodeWalker and extract the files instead.")
        if count == 0 or 16 + count * 16 + names_len > len(self.data):
            raise RpfError(f"{label}: damaged table of contents")
        self.encryption = enc
        names = bytes(self.data[16 + count * 16:16 + count * 16 + names_len])
        raw = []
        for i in range(count):
            o = 16 + i * 16
            y, x = struct.unpack_from("<II", self.data, o)
            if x == 0x7FFFFF00:
                name_off, _, first, n = struct.unpack_from("<IIII", self.data, o)
                e = RpfEntry(_name(names, name_off), "dir")
                e.offset, e.size = first, n            # children range
            elif x & 0x80000000 == 0:
                buf, usize, encflag = struct.unpack_from("<QII", self.data, o)
                e = RpfEntry(_name(names, buf & 0xFFFF), "file")
                e.size = (buf >> 16) & 0xFFFFFF
                e.offset = (buf >> 40) & 0xFFFFFF
                e.usize = usize
                e.encrypted = encflag == 1
            else:
                name_off = struct.unpack_from("<H", self.data, o)[0]
                b = bytes(self.data[o + 2:o + 8])
                e = RpfEntry(_name(names, name_off), "resource")
                e.size = b[0] | b[1] << 8 | b[2] << 16
                e.offset = (b[3] | b[4] << 8 | b[5] << 16) & 0x7FFFFF
                if e.size == 0xFFFFFF:          # large resource: the real size is in its header
                    h = bytes(self.data[e.offset * BLOCK:e.offset * BLOCK + 16])
                    if len(h) == 16:
                        e.size = h[7] | h[14] << 8 | h[5] << 16 | h[2] << 24
            raw.append(e)
        # resolve the directory tree from the root
        self.entries = []
        seen = set()

        def walk(i, prefix):
            if i in seen or i >= len(raw):
                raise RpfError(f"{label}: damaged directory tree")
            seen.add(i)
            d = raw[i]
            for j in range(d.offset, d.offset + d.size):
                if j >= len(raw):
                    raise RpfError(f"{label}: damaged directory tree")
                c = raw[j]
                c.path = f"{prefix}{c.name}"
                d.children.append(c)
                if c.kind == "dir":
                    walk(j, c.path + "/")
                else:
                    self.entries.append(c)

        if raw[0].kind != "dir":
            raise RpfError(f"{label}: no root directory")
        walk(0, "")

    def read(self, entry: RpfEntry) -> bytes:
        start = entry.offset * BLOCK
        if entry.kind == "resource":
            out = bytes(self.data[start:start + entry.size])
            if len(out) != entry.size:
                raise RpfError(f"{self.label}: {entry.path} runs past the end of the archive")
            return out
        if entry.encrypted:
            raise RpfError(f"{self.label}: {entry.path} is encrypted")
        if entry.size == 0:                      # stored
            out = bytes(self.data[start:start + entry.usize])
            if len(out) != entry.usize:
                raise RpfError(f"{self.label}: {entry.path} runs past the end of the archive")
            return out
        comp = bytes(self.data[start:start + entry.size])
        try:
            out = zlib.decompress(comp, -15)
        except zlib.error:
            try:
                out = zlib.decompress(comp)
            except zlib.error as e:
                raise RpfError(f"{self.label}: cannot inflate {entry.path}: {e}") from None
        return out

    def nested(self, entry: RpfEntry) -> "RpfArchive":
        start = entry.offset * BLOCK
        n = entry.usize or entry.size
        return RpfArchive(self.data[start:start + n], f"{self.label}/{entry.path}")


def _name(names: bytes, off: int) -> str:
    end = names.find(b"\0", off)
    if end < 0:
        end = len(names)
    return names[off:end].decode("latin-1")


def list_files(path_or_bytes, label=None, depth=0):
    """[(path, kind, size)] of every file, with nested archives expanded ("x64/vehicles.rpf/a.yft")."""
    arc = path_or_bytes if isinstance(path_or_bytes, RpfArchive) else RpfArchive(
        _load(path_or_bytes), label or (os.path.basename(path_or_bytes) if isinstance(path_or_bytes, str) else "archive.rpf"))
    out = []
    for e in arc.entries:
        if e.kind == "file" and e.name.lower().endswith(".rpf") and depth < 4:
            out.extend((f"{e.path}/{p}", k, s) for p, k, s in list_files(arc.nested(e), depth=depth + 1))
        else:
            out.append((e.path, e.kind, e.usize if e.kind == "file" and e.size == 0 else e.size))
    return out


def extract(path_or_bytes, dest: str, label=None, depth=0, max_bytes=2 << 30) -> list:
    """Write every file below ``dest`` (nested archives become folders named like the archive).

    Returns the written paths.  Names are sanitised: no absolute paths, no '..'.
    """
    arc = path_or_bytes if isinstance(path_or_bytes, RpfArchive) else RpfArchive(
        _load(path_or_bytes), label or (os.path.basename(path_or_bytes) if isinstance(path_or_bytes, str) else "archive.rpf"))
    written, total = [], 0
    root = os.path.realpath(dest)
    for e in arc.entries:
        parts = [p for p in e.path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        if not parts:
            continue
        target = os.path.realpath(os.path.join(dest, *parts))
        if not target.startswith(root + os.sep):
            raise RpfError(f"{arc.label}: refusing entry outside the target folder: {e.path}")
        if e.kind == "file" and e.name.lower().endswith(".rpf") and depth < 4:
            written.extend(extract(arc.nested(e), target, depth=depth + 1, max_bytes=max_bytes))
            continue
        data = arc.read(e)
        total += len(data)
        if total > max_bytes:
            raise RpfError(f"{arc.label}: more than {max_bytes >> 20} MB of files; refusing to extract")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        written.append(target)
    return written


def _load(p):
    if isinstance(p, (bytes, bytearray, memoryview)):
        return p
    with open(p, "rb") as f:
        return f.read()
