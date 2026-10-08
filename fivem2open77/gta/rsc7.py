"""RSC7 resource container used by GTA V (legacy/gen8) for .yft/.ytd/.ydr/... files.

Layout of a standalone resource file (as streamed by FiveM):

    0x00  char[4]  magic 'RSC7'
    0x04  u32      resource version (yft = 162, ytd = 13, ydr = 165, ...)
    0x08  u32      system (virtual) page flags
    0x0C  u32      graphics (physical) page flags
    0x10  ...      raw-deflate compressed data: system segment followed by graphics segment

Pointers inside the resource are 64-bit values tagged with 0x50000000 (system
segment) or 0x60000000 (graphics segment).  The page-flag decoding follows
CodeWalker's ``RpfResourceFileEntry.GetSizeFromFlags``.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

RSC7_MAGIC = b"RSC7"
SYSTEM_BASE = 0x50000000
GRAPHICS_BASE = 0x60000000

# FiveM asset-escrow encrypted stream files do not carry the RSC7 magic in the clear.
# We never try to decrypt them; we only detect and refuse.


class ResourceError(Exception):
    pass


class EscrowProtectedError(ResourceError):
    """Raised for Cfx.re asset-escrow (encrypted) resources, which we do not convert."""


def size_from_flags(flags: int) -> int:
    s0 = ((flags >> 27) & 0x1) << 0
    s1 = ((flags >> 26) & 0x1) << 1
    s2 = ((flags >> 25) & 0x1) << 2
    s3 = ((flags >> 24) & 0x1) << 3
    s4 = ((flags >> 17) & 0x7F) << 4
    s5 = ((flags >> 11) & 0x3F) << 5
    s6 = ((flags >> 7) & 0xF) << 6
    s7 = ((flags >> 5) & 0x3) << 7
    s8 = ((flags >> 4) & 0x1) << 8
    ss = flags & 0xF
    base = 0x200 << ss
    return base * (s0 + s1 + s2 + s3 + s4 + s5 + s6 + s7 + s8)


@dataclass
class Rsc7:
    version: int
    system_flags: int
    graphics_flags: int
    system: bytes
    graphics: bytes

    @property
    def is_gen9(self) -> bool:
        # Gen9 (GTA V Enhanced) resources use different versions (yft 171, ytd 5);
        # FiveM streams legacy (gen8) resources only (yft 162, ytd 13).
        return self.version in GEN9_VERSIONS


GEN9_VERSIONS = {171, 5}
LEGACY_VERSIONS = {"yft": 162, "ytd": 13, "ydr": 165, "ydd": 165}


def read_rsc7(data: bytes, expect_version: int | None = None, name: str = "") -> Rsc7:
    if len(data) < 16:
        raise ResourceError(f"{name}: file too small to be a resource ({len(data)} bytes)")
    magic, version, sflags, gflags = struct.unpack_from("<4sIII", data, 0)
    if magic != RSC7_MAGIC:
        if magic[:3] == b"RSC":
            raise ResourceError(f"{name}: unsupported resource container {magic!r}")
        raise EscrowProtectedError(
            f"{name}: not a plain RSC7 resource (magic {magic!r}). This looks like an "
            "asset-escrow (encrypted) or otherwise protected file; it cannot be converted."
        )
    if version in GEN9_VERSIONS:
        raise ResourceError(
            f"{name}: this is a GTA V Enhanced (gen9) resource (version {version}). FiveM uses "
            "legacy resources; convert it back to legacy with CodeWalker/Sollumz first."
        )
    if expect_version is not None and version != expect_version:
        raise ResourceError(f"{name}: unexpected resource version {version} (expected {expect_version})")
    ssize = size_from_flags(sflags)
    gsize = size_from_flags(gflags)
    try:
        raw = zlib.decompress(data[16:], -15)
    except zlib.error as e:
        raise ResourceError(f"{name}: deflate stream is corrupt ({e})") from e
    if len(raw) < ssize + gsize:
        # Some exporters store a slightly short graphics segment; pad defensively.
        raw = raw + b"\0" * (ssize + gsize - len(raw))
    return Rsc7(version, sflags, gflags, raw[:ssize], raw[ssize:ssize + gsize])
