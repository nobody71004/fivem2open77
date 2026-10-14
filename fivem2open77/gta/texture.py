"""GTA V legacy texture dictionaries (.ytd and embedded YFT/YDR dictionaries)."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .jenkhash import joaat
from .reader import ResourceReader
from .rsc7 import read_rsc7

# D3DFORMAT values used by legacy GTA V textures
FMT_A8R8G8B8 = 21
FMT_X8R8G8B8 = 22
FMT_A1R5G5B5 = 25
FMT_A8 = 28
FMT_A8B8G8R8 = 32
FMT_L8 = 50
FMT_DXT1 = 0x31545844
FMT_DXT3 = 0x33545844
FMT_DXT5 = 0x35545844
FMT_ATI1 = 0x31495441
FMT_ATI2 = 0x32495441
FMT_BC7 = 0x20374342

# format -> (dxgi format, block compressed?, bytes per block or per pixel, name)
FORMATS = {
    FMT_DXT1: (71, True, 8, "BC1"),
    FMT_DXT3: (74, True, 16, "BC2"),
    FMT_DXT5: (77, True, 16, "BC3"),
    FMT_ATI1: (80, True, 8, "BC4"),
    FMT_ATI2: (83, True, 16, "BC5"),
    FMT_BC7: (98, True, 16, "BC7"),
    FMT_A8R8G8B8: (87, False, 4, "B8G8R8A8"),
    FMT_X8R8G8B8: (88, False, 4, "B8G8R8X8"),
    FMT_A8B8G8R8: (28, False, 4, "R8G8B8A8"),
    FMT_A1R5G5B5: (86, False, 2, "B5G5R5A1"),
    FMT_A8: (65, False, 1, "A8"),
    FMT_L8: (61, False, 1, "R8"),
}


def mip_size(fmt: int, w: int, h: int) -> int:
    dxgi, bc, bpb, _ = FORMATS[fmt]
    w = max(1, w)
    h = max(1, h)
    if bc:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bpb
    return w * h * bpb


_IMAGE_EXT = (".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")

# names a shader gives a sampler it does not use: no texture, and nothing missing
NO_TEXTURE = frozenset({"none", "null", "givemechecker"})


def texture_name(raw) -> str:
    """A texture's name the way GTA finds it: without a `pack:/` (or any folder) prefix and without
    a file extension -- 'pack:/tl_church_marble.dds' is found as 'tl_church_marble'.  A dictionary's
    hash list holds joaat of exactly that (lower case), and shaders name their textures that way.
    Dictionaries converted with OpenIV (Liberty V's are almost all like this) store the long form."""
    n = (raw or "").strip().replace("\\", "/")
    if "/" in n:
        n = n.rsplit("/", 1)[1]
    elif ":" in n:
        n = n.rsplit(":", 1)[1]
    low = n.lower()
    for ext in _IMAGE_EXT:
        if low.endswith(ext) and len(n) > len(ext):
            return n[:-len(ext)]
    return n


@dataclass
class GtaTexture:
    name: str
    width: int
    height: int
    levels: int
    format: int
    stride: int
    usage: int
    data: bytes = field(repr=False, default=b"")

    @property
    def format_name(self) -> str:
        return FORMATS.get(self.format, (0, False, 0, f"0x{self.format:X}"))[3]

    @property
    def supported(self) -> bool:
        return self.format in FORMATS

    def mip_chain(self):
        """Yield (level, width, height, bytes) for every mip present in the data."""
        off = 0
        w, h = self.width, self.height
        for lvl in range(self.levels):
            sz = mip_size(self.format, w, h)
            if off + sz > len(self.data):
                break
            yield lvl, w, h, self.data[off:off + sz]
            off += sz
            w = max(1, w // 2)
            h = max(1, h // 2)

    def to_dds(self) -> bytes:
        """Encode as a DDS file with a DX10 header (readable by DirectXTex/WolvenKit)."""
        if not self.supported:
            raise ValueError(f"texture {self.name}: unsupported format 0x{self.format:X}")
        mips = list(self.mip_chain())
        if not mips:
            raise ValueError(f"texture {self.name}: no pixel data")
        dxgi, bc, bpb, _ = FORMATS[self.format]
        DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT = 0x1, 0x2, 0x4, 0x1000
        DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE, DDSD_PITCH = 0x20000, 0x80000, 0x8
        flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT
        if bc:
            flags |= DDSD_LINEARSIZE
            pitch = mip_size(self.format, self.width, self.height)
        else:
            flags |= DDSD_PITCH
            pitch = self.width * bpb
        caps = 0x1000 | (0x400008 if len(mips) > 1 else 0)  # TEXTURE | MIPMAP|COMPLEX
        pf = struct.pack("<II4sIIIII", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
        hdr = struct.pack("<IIIIIII", 124, flags, self.height, self.width, pitch, 0, len(mips))
        hdr += b"\0" * 44 + pf + struct.pack("<IIIII", caps, 0, 0, 0, 0)
        dx10 = struct.pack("<IIIII", dxgi, 3, 0, 1, 0)
        return b"DDS " + hdr + dx10 + b"".join(m[3] for m in mips)


def read_texture(rd: ResourceReader, ptr: int) -> GtaTexture | None:
    c = rd.at(ptr)
    if c is None:
        return None
    # TextureBase (legacy) 0x50 bytes
    c.skip(0x28)
    name_ptr = c.u64()
    c.skip(2 + 2 + 4 + 4 + 4)
    usage_data = c.u32()
    c.skip(4 + 4 + 4)
    # Texture (legacy) fields from 0x50
    width = c.u16()
    height = c.u16()
    depth = c.u16()
    stride = c.u16()
    fmt = c.u32()
    c.u8()
    levels = c.u8()
    c.skip(2 + 4 * 4)
    data_ptr = c.u64()
    name = texture_name(rd.string_at(name_ptr))
    # Read generously: the exact mip chain is computed from the format.
    total = 0
    w, h = width, height
    if fmt in FORMATS:
        for _ in range(max(1, levels)):
            total += mip_size(fmt, w, h)
            w, h = max(1, w // 2), max(1, h // 2)
    else:
        total = stride * height * 2
    buf, off, _ = rd.segment(data_ptr) if data_ptr else (None, 0, 0)
    data = bytes(buf[off:off + total]) if buf is not None else b""
    return GtaTexture(name=name, width=width, height=height, levels=max(1, levels), format=fmt,
                      stride=stride, usage=usage_data & 0x1F, data=data)


def read_texture_dictionary(rd: ResourceReader, ptr: int) -> dict:
    """Returns {lowercase name: GtaTexture}."""
    out = {}
    c = rd.at(ptr)
    if c is None:
        return out
    c.skip(0x10)  # ResourceFileBase
    c.skip(16)
    hashes_ptr, hcount, _ = rd.pointer_list_header(c)
    tex_ptr, tcount, tcap = rd.pointer_list_header(c)
    for p in rd.pointer_array(tex_ptr, tcount):
        t = read_texture(rd, p)
        if t is not None:
            out[t.name.lower()] = t
    return out


def ytd_names(path_or_bytes, name: str = "") -> list:
    """The lower-case texture names of a .ytd without copying any pixel data."""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), expect_version=13, name=name or str(path_or_bytes))
    rd = ResourceReader(rsc, name=name)
    c = rd.at(0x50000000)
    if c is None:
        return []
    c.skip(0x10)
    c.skip(16)
    rd.pointer_list_header(c)
    tex_ptr, tcount, _ = rd.pointer_list_header(c)
    out = []
    for p in rd.pointer_array(tex_ptr, tcount):
        t = rd.at(p)
        if t is None:
            continue
        t.skip(0x28)
        n = texture_name(rd.string_at(t.u64()))
        if n:
            out.append(n.lower())
    return out


def load_ytd(path_or_bytes, name: str = "") -> dict:
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), expect_version=13, name=name or str(path_or_bytes))
    rd = ResourceReader(rsc, name=name)
    return read_texture_dictionary(rd, 0x50000000)


__all__ = ["GtaTexture", "load_ytd", "ytd_names", "read_texture_dictionary", "read_texture", "texture_name", "NO_TEXTURE", "joaat"]
