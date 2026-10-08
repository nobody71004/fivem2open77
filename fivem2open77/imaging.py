"""Small image helpers without third-party libraries: PNG encoding and decoding of the GTA texture
formats (BC1-BC5 and the uncompressed ones) into RGBA, for previews and renders."""
from __future__ import annotations

import struct
import zlib

import numpy as np

from .gta.texture import (FMT_A1R5G5B5, FMT_A8, FMT_A8B8G8R8, FMT_A8R8G8B8, FMT_ATI1,
                          FMT_ATI2, FMT_DXT1, FMT_DXT3, FMT_DXT5, FMT_L8, FMT_X8R8G8B8)


def png_bytes(rgba: np.ndarray) -> bytes:
    """uint8 (H, W, 3|4) -> PNG file bytes."""
    a = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w, c = a.shape
    color_type = {3: 2, 4: 6}[c]
    raw = np.empty((h, 1 + w * c), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = a.reshape(h, w * c)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def _rgb565(c):
    c = c.astype(np.uint32)
    r = ((c >> 11) & 31) * 255 // 31
    g = ((c >> 5) & 63) * 255 // 63
    b = (c & 31) * 255 // 31
    return np.stack([r, g, b], -1).astype(np.int32)


def _blocks_to_image(px, bw, bh, w, h):
    """(nblocks, 16, C) block pixels -> (h, w, C)."""
    c = px.shape[-1]
    img = px.reshape(bh, bw, 4, 4, c).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, c)
    return img[:h, :w]


def _bc1_color(blocks, punchthrough=True):
    c0 = blocks[:, 0].astype(np.uint32) | (blocks[:, 1].astype(np.uint32) << 8)
    c1 = blocks[:, 2].astype(np.uint32) | (blocks[:, 3].astype(np.uint32) << 8)
    idx = (blocks[:, 4].astype(np.uint32) | (blocks[:, 5].astype(np.uint32) << 8)
           | (blocks[:, 6].astype(np.uint32) << 16) | (blocks[:, 7].astype(np.uint32) << 24))
    p0, p1 = _rgb565(c0), _rgb565(c1)
    four = (c0 > c1) | (not punchthrough)
    p2 = np.where(four[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    p3 = np.where(four[:, None], (p0 + 2 * p1) // 3, 0)
    pal = np.stack([p0, p1, p2, p3], 1)                         # (n, 4, 3)
    alpha_pal = np.stack([np.full(len(c0), 255), np.full(len(c0), 255), np.full(len(c0), 255),
                          np.where(four, 255, 0)], 1)
    sel = (idx[:, None] >> (2 * np.arange(16, dtype=np.uint32))[None, :]) & 3   # (n, 16)
    rows = np.arange(len(c0))[:, None]
    rgb = pal[rows, sel]
    a = alpha_pal[rows, sel]
    return np.concatenate([rgb, a[..., None]], -1)


def _bc4_channel(blocks):
    a0 = blocks[:, 0].astype(np.int32)
    a1 = blocks[:, 1].astype(np.int32)
    bits = np.zeros(len(a0), dtype=np.uint64)
    for i in range(6):
        bits |= blocks[:, 2 + i].astype(np.uint64) << np.uint64(8 * i)
    sel = ((bits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))[None, :]) & np.uint64(7)).astype(np.int64)
    eight = a0 > a1
    pal = np.zeros((len(a0), 8), dtype=np.int32)
    pal[:, 0], pal[:, 1] = a0, a1
    for i in range(1, 7):
        pal[:, i + 1] = np.where(eight, ((7 - i) * a0 + i * a1) // 7, 0)
    for i in range(1, 5):
        pal[:, i + 1] = np.where(eight, pal[:, i + 1], ((5 - i) * a0 + i * a1) // 5)
    pal[:, 6] = np.where(eight, pal[:, 6], 0)
    pal[:, 7] = np.where(eight, pal[:, 7], 255)
    return pal[np.arange(len(a0))[:, None], sel]


def decode(fmt: int, w: int, h: int, data: bytes) -> np.ndarray | None:
    """One mip level -> uint8 (h, w, 4) RGBA, or None for formats not handled (BC7)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    buf = np.frombuffer(data, dtype=np.uint8)
    if fmt == FMT_DXT1:
        px = _bc1_color(buf[:bw * bh * 8].reshape(-1, 8))
    elif fmt in (FMT_DXT3, FMT_DXT5):
        b = buf[:bw * bh * 16].reshape(-1, 16)
        px = _bc1_color(b[:, 8:], punchthrough=False)
        if fmt == FMT_DXT5:
            px[..., 3] = _bc4_channel(b[:, :8])
        else:
            nib = np.stack([b[:, :8] & 15, b[:, :8] >> 4], -1).reshape(-1, 16)
            px[..., 3] = nib.astype(np.int32) * 17
    elif fmt == FMT_ATI1:
        g = _bc4_channel(buf[:bw * bh * 8].reshape(-1, 8))
        px = np.stack([g, g, g, np.full_like(g, 255)], -1)
    elif fmt == FMT_ATI2:
        b = buf[:bw * bh * 16].reshape(-1, 16)
        r, g = _bc4_channel(b[:, :8]), _bc4_channel(b[:, 8:])
        px = np.stack([r, g, np.full_like(r, 255), np.full_like(r, 255)], -1)
    elif fmt in (FMT_A8R8G8B8, FMT_X8R8G8B8):
        a = buf[:w * h * 4].reshape(h, w, 4)[..., [2, 1, 0, 3]].copy()
        if fmt == FMT_X8R8G8B8:
            a[..., 3] = 255
        return a
    elif fmt == FMT_A8B8G8R8:
        return buf[:w * h * 4].reshape(h, w, 4).copy()
    elif fmt == FMT_A1R5G5B5:
        c = buf[:w * h * 2].view("<u2").reshape(h, w).astype(np.uint32)
        return np.stack([((c >> 10) & 31) * 255 // 31, ((c >> 5) & 31) * 255 // 31, (c & 31) * 255 // 31,
                         ((c >> 15) & 1) * 255], -1).astype(np.uint8)
    elif fmt in (FMT_L8, FMT_A8):
        g = buf[:w * h].reshape(h, w)
        if fmt == FMT_A8:
            return np.stack([np.full_like(g, 255)] * 3 + [g], -1)
        return np.stack([g, g, g, np.full_like(g, 255)], -1)
    else:
        return None
    return np.clip(_blocks_to_image(px, bw, bh, w, h), 0, 255).astype(np.uint8)


def texture_rgba(tex, max_size: int = 512) -> np.ndarray | None:
    """Largest mip of a GtaTexture not bigger than max_size, decoded; None if not decodable."""
    best = None
    for lvl, w, h, data in tex.mip_chain():
        best = (w, h, data)
        if max(w, h) <= max_size:
            break
    if best is None:
        return None
    try:
        return decode(tex.format, *best)
    except (ValueError, IndexError):
        return None


def mean_color(rgba: np.ndarray) -> tuple:
    """Average colour of the opaque pixels (0..1)."""
    a = rgba.reshape(-1, 4).astype(np.float64)
    m = a[:, 3] > 127
    if m.sum() < 8:
        m = np.ones(len(a), bool)
    return tuple((a[m, :3].mean(0) / 255.0).tolist())
