"""GTA texture -> Cyberpunk CBitmapTexture (.xbm) as WolvenKit JSON.

Block-compressed GTA data (BC1/BC3/BC4/BC5/BC7) is passed through untouched (no quality loss,
no re-encoding).  BC2 and 16-bit/8-bit formats are decoded to RGBA8.  BGRA8 is swizzled to RGBA8.
The blob layout mirrors what WolvenKit's own importer writes (mips back to back, DirectXTex pitches).
"""
from __future__ import annotations

import base64

import numpy as np

from ..gta import texture as gt
from .wkjson import document

# GTA format -> (cyberpunk compression, raw format, block bytes or bpp, block compressed)
_PASS = {
    gt.FMT_DXT1: ("TCM_DXTNoAlpha", 8, True),
    gt.FMT_DXT5: ("TCM_DXTAlpha", 16, True),
    gt.FMT_ATI1: ("TCM_QualityR", 8, True),
    gt.FMT_ATI2: ("TCM_Normalmap", 16, True),
    gt.FMT_BC7: ("TCM_QualityColor", 16, True),
}


def _mip_layout(w, h, block_bytes, bc):
    if bc:
        row = max(1, (w + 3) // 4) * block_bytes
        slice_ = row * max(1, (h + 3) // 4)
    else:
        row = w * block_bytes
        slice_ = row * h
    return row, slice_


def _decode_bc2(data, w, h):
    """BC2 (DXT3) -> RGBA8 (colour like BC1 4-colour mode, explicit 4-bit alpha)."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    blocks = np.frombuffer(data[:bw * bh * 16], dtype=np.uint8).reshape(bh, bw, 16)
    out = np.zeros((bh * 4, bw * 4, 4), np.uint8)
    a = blocks[:, :, 0:8].astype(np.uint16)
    alpha = np.zeros((bh, bw, 16), np.uint8)
    for i in range(8):
        alpha[:, :, 2 * i] = (a[:, :, i] & 0xF) * 17
        alpha[:, :, 2 * i + 1] = (a[:, :, i] >> 4) * 17
    rgb = _decode_bc1_colors(blocks[:, :, 8:16], four_color=True)
    out[..., :3] = rgb
    out[..., 3] = alpha.reshape(bh, bw, 4, 4).transpose(0, 2, 1, 3).reshape(bh * 4, bw * 4)
    return out[:h, :w]


def _decode_bc1_colors(cb, four_color):
    bh, bw = cb.shape[:2]
    c0 = cb[:, :, 0].astype(np.uint32) | (cb[:, :, 1].astype(np.uint32) << 8)
    c1 = cb[:, :, 2].astype(np.uint32) | (cb[:, :, 3].astype(np.uint32) << 8)

    def unpack(c):
        r = ((c >> 11) & 31) * 255 // 31
        g = ((c >> 5) & 63) * 255 // 63
        b = (c & 31) * 255 // 31
        return np.stack([r, g, b], -1).astype(np.int32)

    p0, p1 = unpack(c0), unpack(c1)
    pal = np.stack([p0, p1, (2 * p0 + p1) // 3, (p0 + 2 * p1) // 3], 2)  # bh,bw,4,3
    bits = cb[:, :, 4:8].astype(np.uint32)
    idx = bits[:, :, 0] | (bits[:, :, 1] << 8) | (bits[:, :, 2] << 16) | (bits[:, :, 3] << 24)
    sel = np.stack([(idx >> (2 * i)) & 3 for i in range(16)], -1)  # bh,bw,16
    rgb = np.take_along_axis(pal, sel[..., None].repeat(3, -1).astype(np.int64), axis=2)  # bh,bw,16,3
    return rgb.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3).astype(np.uint8)


def _to_rgba8(tex: gt.GtaTexture):
    """Decode non pass-through formats to a list of RGBA8 mip images."""
    mips = []
    for lvl, w, h, data in tex.mip_chain():
        if tex.format in (gt.FMT_A8R8G8B8, gt.FMT_X8R8G8B8):
            a = np.frombuffer(data, np.uint8).reshape(h, w, 4)[..., [2, 1, 0, 3]].copy()
            if tex.format == gt.FMT_X8R8G8B8:
                a[..., 3] = 255
        elif tex.format == gt.FMT_A8B8G8R8:
            a = np.frombuffer(data, np.uint8).reshape(h, w, 4).copy()
        elif tex.format == gt.FMT_A1R5G5B5:
            v = np.frombuffer(data, np.uint16).reshape(h, w).astype(np.uint32)
            a = np.stack([((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31,
                          ((v >> 15) & 1) * 255], -1).astype(np.uint8)
        elif tex.format in (gt.FMT_A8, gt.FMT_L8):
            v = np.frombuffer(data, np.uint8).reshape(h, w)
            if tex.format == gt.FMT_A8:
                a = np.stack([np.full_like(v, 255)] * 3 + [v], -1)
            else:
                a = np.stack([v, v, v, np.full_like(v, 255)], -1)
        elif tex.format == gt.FMT_DXT3:
            a = _decode_bc2(data, w, h)
        else:
            raise ValueError(f"unsupported texture format {tex.format_name}")
        mips.append((w, h, np.ascontiguousarray(a).tobytes()))
    return mips


def xbm_document(tex: gt.GtaTexture, usage: str = "color") -> dict:
    """usage: color | normal | data (linear) | mask"""
    if tex.format in _PASS:
        compression, bpb, bc = _PASS[tex.format]
        mips = [(w, h, d) for _, w, h, d in tex.mip_chain()]
        raw_format = "TRF_TrueColor"
        if usage == "normal" and tex.format != gt.FMT_ATI2:
            # keep the original compression; the engine samples RG(B) either way
            pass
        if usage != "normal" and tex.format == gt.FMT_ATI2:
            compression = "TCM_QualityRG"
    else:
        compression, bpb, bc = "TCM_None", 4, False
        mips = _to_rgba8(tex)
        raw_format = "TRF_TrueColor"
    if not mips:
        raise ValueError(f"texture {tex.name} has no usable pixel data")
    is_gamma = 1 if usage == "color" else 0
    if compression in ("TCM_Normalmap", "TCM_QualityR", "TCM_QualityRG"):
        is_gamma = 0
    group = {"color": "TEXG_Generic_Color", "normal": "TEXG_Generic_Normal", "data": "TEXG_Generic_Data",
             "mask": "TEXG_Generic_Grayscale"}.get(usage, "TEXG_Generic_Color")
    infos, chunks, off = [], [], 0
    for w, h, d in mips:
        row, slc = _mip_layout(w, h, bpb, bc)
        d = d[:slc].ljust(slc, b"\0")
        infos.append({"$type": "rendRenderTextureBlobMipMapInfo",
                      "layout": {"$type": "rendRenderTextureBlobMemoryLayout", "rowPitch": row, "slicePitch": slc},
                      "placement": {"$type": "rendRenderTextureBlobPlacement", "offset": off, "size": slc}})
        chunks.append(d)
        off += slc
    data = b"".join(chunks)
    w0, h0 = mips[0][0], mips[0][1]
    blob = {
        "$type": "rendRenderTextureBlobPC",
        "header": {
            "$type": "rendRenderTextureBlobHeader", "flags": 1, "histogramData": [], "mipMapInfo": infos,
            "sizeInfo": {"$type": "rendRenderTextureBlobSizeInfo", "depth": 1, "height": h0, "width": w0},
            "textureInfo": {"$type": "rendRenderTextureBlobTextureInfo", "dataAlignment": 8, "mipCount": len(mips),
                            "sliceCount": 1, "sliceSize": len(data), "textureDataSize": len(data),
                            "type": "TEXTYPE_2D"},
            "version": 2,
        },
        "textureData": {"BufferId": "0", "Flags": 131072, "Bytes": base64.b64encode(data).decode("ascii")},
    }
    root = {
        "$type": "CBitmapTexture", "cookingPlatform": "PLATFORM_PC", "depth": 1, "height": h0,
        "histBiasAddCoef": {"$type": "Vector3", "X": 0, "Y": 0, "Z": 0},
        "histBiasMulCoef": {"$type": "Vector3", "X": 1, "Y": 1, "Z": 1},
        "renderResourceBlob": None,
        "renderTextureResource": {"$type": "rendRenderTextureResource",
                                  "renderResourceBlobPC": {"HandleId": "0", "Data": blob}},
        "setup": {"$type": "STextureGroupSetup", "allowTextureDowngrade": 1, "alphaToCoverageThreshold": 0,
                  "compression": compression, "group": group, "hasMipchain": 1 if len(mips) > 1 else 0,
                  "isGamma": is_gamma, "isStreamable": 1, "platformMipBiasConsole": 0, "platformMipBiasPC": 0,
                  "rawFormat": raw_format},
        "width": w0,
    }
    return document(root, build_version=184)
