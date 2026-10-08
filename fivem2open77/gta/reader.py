"""Pointer-aware reader over the two segments of a decompressed RSC7 resource."""
from __future__ import annotations

import struct

from .rsc7 import GRAPHICS_BASE, SYSTEM_BASE, Rsc7, ResourceError


class Cursor:
    """Sequential reader positioned inside one segment (used to read a struct's fields)."""

    __slots__ = ("buf", "pos", "base")

    def __init__(self, buf: bytes, pos: int, base: int):
        self.buf = buf
        self.pos = pos
        self.base = base

    def _u(self, fmt: str, size: int):
        v = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += size
        return v

    def u8(self) -> int:
        return self._u("<B", 1)[0]

    def i16(self) -> int:
        return self._u("<h", 2)[0]

    def u16(self) -> int:
        return self._u("<H", 2)[0]

    def i32(self) -> int:
        return self._u("<i", 4)[0]

    def u32(self) -> int:
        return self._u("<I", 4)[0]

    def u64(self) -> int:
        return self._u("<Q", 8)[0]

    def f32(self) -> float:
        return self._u("<f", 4)[0]

    def vec3(self) -> tuple:
        return self._u("<3f", 12)

    def vec4(self) -> tuple:
        return self._u("<4f", 16)

    def skip(self, n: int) -> "Cursor":
        self.pos += n
        return self

    @property
    def address(self) -> int:
        return self.base | self.pos


class ResourceReader:
    def __init__(self, rsc: Rsc7, name: str = ""):
        self.rsc = rsc
        self.name = name
        self.system = rsc.system
        self.graphics = rsc.graphics

    # --- pointer resolution -------------------------------------------------
    def segment(self, ptr: int):
        if ptr == 0:
            return None, 0, 0
        if (ptr & GRAPHICS_BASE) == GRAPHICS_BASE:
            return self.graphics, ptr & ~GRAPHICS_BASE & 0xFFFFFFFF, GRAPHICS_BASE
        if (ptr & SYSTEM_BASE) == SYSTEM_BASE:
            return self.system, ptr & ~SYSTEM_BASE & 0xFFFFFFFF, SYSTEM_BASE
        raise ResourceError(f"{self.name}: invalid resource pointer 0x{ptr:X}")

    def at(self, ptr: int) -> Cursor | None:
        buf, off, base = self.segment(ptr)
        if buf is None:
            return None
        if off >= len(buf):
            raise ResourceError(f"{self.name}: pointer 0x{ptr:X} outside its segment")
        return Cursor(buf, off, base)

    def bytes_at(self, ptr: int, length: int) -> bytes:
        buf, off, _ = self.segment(ptr)
        if buf is None:
            return b""
        return bytes(buf[off:off + length])

    def string_at(self, ptr: int) -> str | None:
        buf, off, _ = self.segment(ptr)
        if buf is None:
            return None
        end = buf.find(b"\0", off)
        if end < 0:
            end = len(buf)
        return buf[off:end].decode("utf-8", "replace")

    def u16s_at(self, ptr: int, count: int) -> list:
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        return list(struct.unpack_from(f"<{count}H", buf, off))

    def i16s_at(self, ptr: int, count: int) -> list:
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        return list(struct.unpack_from(f"<{count}h", buf, off))

    def u32s_at(self, ptr: int, count: int) -> list:
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        return list(struct.unpack_from(f"<{count}I", buf, off))

    def u64s_at(self, ptr: int, count: int) -> list:
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        return list(struct.unpack_from(f"<{count}Q", buf, off))

    def floats_at(self, ptr: int, count: int) -> list:
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        return list(struct.unpack_from(f"<{count}f", buf, off))

    def matrices_at(self, ptr: int, count: int) -> list:
        """Read `count` row-major 4x4 float matrices (SharpDX Matrix layout)."""
        if not ptr or not count:
            return []
        buf, off, _ = self.segment(ptr)
        vals = struct.unpack_from(f"<{16 * count}f", buf, off)
        return [vals[i * 16:(i + 1) * 16] for i in range(count)]

    def pointer_array(self, ptr: int, count: int) -> list:
        """ResourcePointerArray64: `count` 64-bit pointers stored at ptr."""
        return self.u64s_at(ptr, count)

    def pointer_list_header(self, cur: Cursor):
        """ResourcePointerList64 / ResourceSimpleList64 header: ptr u64, count u16, cap u16, pad u32."""
        p = cur.u64()
        count = cur.u16()
        cap = cur.u16()
        cur.u32()
        return p, count, cap
