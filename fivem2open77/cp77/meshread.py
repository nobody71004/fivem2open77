"""The game's meshes read straight from their files, without WolvenKit: the geometry of the best
level of detail, the appearances (a material name per render chunk) and the materials (their
parameters, through the chain of material instances they derive from), and the textures they
name (a small mip, decoded).

A ``.mesh`` (CR2W, see cr2w.py) holds a ``CMesh`` and its ``rendRenderMeshBlob``:

    header          quantizationScale / quantizationOffset (positions are 16-bit, scaled into the
                    mesh's box), indexBufferOffset, and renderChunkInfos: per render chunk its
                    vertex layout (elements: type, usage, stream; slotStrides: each stream's stride),
                    byteOffsets (where each stream of the chunk starts), chunkIndices (teOffset:
                    where its indices start, after indexBufferOffset), numVertices, numIndices and
                    lodMask (bit n: the chunk belongs to level of detail n)
    renderBuffer    the vertex streams, then the indices (16-bit, relative to the chunk)

Materials: ``materialEntries`` name them; a local one is a CMaterialInstance in the mesh (its own
small CR2W in ``localMaterialBuffer``, or an export listed in ``preloadLocalMaterialInstances``),
an external one a ``.mi`` file.  An instance's parameters are its tagged properties and the ones
cooked after them (tail_props); what it does not set comes from its ``baseMaterial`` (another
instance, up to a template: ``.mt`` / ``.remt``).
"""
from __future__ import annotations

import struct

import numpy as np

from .cr2w import CR2W, CR2WError

ELEMENT_SIZE = {
    "PT_Float1": 4, "PT_Float2": 8, "PT_Float3": 12, "PT_Float4": 16, "PT_Float16_2": 4, "PT_Float16_4": 8,
    "PT_UByte1": 1, "PT_UByte1F": 1, "PT_UByte4": 4, "PT_UByte4N": 4, "PT_Byte4N": 4,
    "PT_UShort1": 2, "PT_UShort2": 4, "PT_UShort4": 8, "PT_UShort4N": 8,
    "PT_Short1": 2, "PT_Short2": 4, "PT_Short4": 8, "PT_Short4N": 8,
    "PT_UInt1": 4, "PT_UInt2": 8, "PT_UInt3": 12, "PT_UInt4": 16,
    "PT_Int1": 4, "PT_Int2": 8, "PT_Int3": 12, "PT_Int4": 16,
    "PT_Color": 4, "PT_Dec4": 4, "PT_Index16": 2, "PT_Index32": 4,
}
SKIP_PARAMS = ("baseMaterial", "resourceVersion", "cookingPlatform", "enableMask", "audioTag", "metadata",
               "userData", "parameters")
MAX_CHAIN = 8


class MeshReadError(CR2WError):
    pass


def _strided(buf: bytes, start: int, stride: int, n: int, size: int) -> np.ndarray:
    """(n, size) bytes: ``size`` bytes every ``stride`` from ``start``."""
    if stride <= 0 or start < 0 or start + stride * (n - 1) + size > len(buf):
        raise MeshReadError("a vertex stream runs past the mesh's buffer")
    a = np.frombuffer(buf, np.uint8, count=stride * (n - 1) + size, offset=start)
    return np.lib.stride_tricks.as_strided(a, shape=(n, size), strides=(stride, 1)).copy()


def _positions(t: str, raw: np.ndarray, scale, offset) -> np.ndarray:
    if t == "PT_Short4N":
        q = raw.view("<i2").reshape(-1, 4)[:, :3].astype(np.float32) / 32767.0
        return q * scale + offset
    if t in ("PT_Float3", "PT_Float4"):
        return raw.view("<f4").reshape(len(raw), -1)[:, :3].astype(np.float32)
    if t == "PT_Float16_4":
        return raw.view("<f2").reshape(-1, 4)[:, :3].astype(np.float32)
    if t == "PT_UShort4N":
        q = raw.view("<u2").reshape(-1, 4)[:, :3].astype(np.float32) / 65535.0 * 2.0 - 1.0
        return q * scale + offset
    raise MeshReadError(f"positions as {t}")


def _normals(t: str, raw: np.ndarray) -> np.ndarray | None:
    if t == "PT_Dec4":
        v = raw.view("<u4").reshape(-1).astype(np.int64)
        xyz = np.stack([v & 1023, (v >> 10) & 1023, (v >> 20) & 1023], 1).astype(np.float32)
        return (xyz - 511.5) / 511.5
    if t in ("PT_Byte4N",):
        return raw.view(np.int8).reshape(-1, 4)[:, :3].astype(np.float32) / 127.0
    if t in ("PT_UByte4N", "PT_Color"):
        return raw.reshape(-1, 4)[:, :3].astype(np.float32) / 127.5 - 1.0
    if t in ("PT_Float3", "PT_Float4"):
        return raw.view("<f4").reshape(len(raw), -1)[:, :3].astype(np.float32)
    if t == "PT_Float16_4":
        return raw.view("<f2").reshape(-1, 4)[:, :3].astype(np.float32)
    return None


def _uvs(t: str, raw: np.ndarray) -> np.ndarray | None:
    if t == "PT_Float16_2":
        return raw.view("<f2").reshape(-1, 2).astype(np.float32)
    if t == "PT_Float2":
        return raw.view("<f4").reshape(-1, 2).astype(np.float32)
    if t == "PT_Float16_4":
        return raw.view("<f2").reshape(-1, 4)[:, :2].astype(np.float32)
    return None


def _vec3(d, default):
    d = d or {}
    return np.array([float(d.get("X", default)), float(d.get("Y", default)), float(d.get("Z", default))], np.float32)


def blob_of(c: CR2W) -> int:
    """The export index of the mesh's render blob."""
    if not c.exports or c.exports[0]["cls"] not in ("CMesh", "CPhysicalDestructionResource", "CGradientMesh"):
        raise MeshReadError(f"not a mesh ({c.exports[0]['cls'] if c.exports else 'empty'})")
    h = c.props(0).get("renderResourceBlob")
    if isinstance(h, int) and 0 <= h < len(c.exports) and c.exports[h]["cls"] == "rendRenderMeshBlob":
        return h
    for i, e in enumerate(c.exports):
        if e["cls"] == "rendRenderMeshBlob":
            return i
    raise MeshReadError("the mesh has no render blob")


def read_geometry(c: CR2W, lod: int | None = None, uv: bool = False) -> list:
    """[{chunk, lod, positions (n, 3), normals (n, 3), indices (m,) uint32[, uv (n, 2)]}] of the
    render chunks of one level of detail: ``lod`` (0 the best), or the best there is."""
    blob = c.props(blob_of(c))
    h = blob.get("header") or {}
    buf = c.data_buffer(blob.get("renderBuffer") or 0)
    if not buf:
        raise MeshReadError("the mesh has no geometry buffer")
    scale = _vec3(h.get("quantizationScale"), 1.0)
    offset = _vec3(h.get("quantizationOffset"), 0.0)
    ibo = int(h.get("indexBufferOffset") or 0)
    infos = h.get("renderChunkInfos") or []
    masks = [int(i.get("lodMask") or 1) for i in infos]
    if not infos:
        return []
    if lod is None:
        lowest = [(m & -m).bit_length() - 1 for m in masks if m]
        lod = min(lowest) if lowest else 0
    want = 1 << lod
    out = []
    for ci, info in enumerate(infos):
        if not masks[ci] & want:
            continue
        nv, ni = int(info.get("numVertices") or 0), int(info.get("numIndices") or 0)
        if nv <= 0 or ni < 3:
            continue
        cv = info.get("chunkVertices") or {}
        lay = cv.get("vertexLayout") or {}
        strides = list(lay.get("slotStrides") or [])
        offs = list(cv.get("byteOffsets") or [])
        acc = {}
        pos = nrm = tex = None
        for el in lay.get("elements") or []:
            t = el.get("type")
            st = el.get("streamType", "ST_PerVertex")
            if t in (None, "PT_Invalid") or st not in ("ST_PerVertex",):
                continue
            size = ELEMENT_SIZE.get(t)
            if size is None:
                raise MeshReadError(f"vertex element type {t}")
            s = int(el.get("streamIndex") or 0)
            eo = acc.get(s, 0)
            acc[s] = eo + size
            if s >= len(offs) or s >= len(strides) or not strides[s]:
                continue
            usage, ui = el.get("usage"), int(el.get("usageIndex") or 0)
            if usage == "PS_Position" and ui == 0 and pos is None:
                pos = _positions(t, _strided(buf, int(offs[s]) + eo, int(strides[s]), nv, size), scale, offset)
            elif usage == "PS_Normal" and ui == 0 and nrm is None:
                nrm = _normals(t, _strided(buf, int(offs[s]) + eo, int(strides[s]), nv, size))
            elif uv and usage == "PS_TexCoord" and ui == 0 and tex is None:
                tex = _uvs(t, _strided(buf, int(offs[s]) + eo, int(strides[s]), nv, size))
        if pos is None:
            raise MeshReadError(f"render chunk {ci} has no positions")
        ck = info.get("chunkIndices") or {}
        wide = ck.get("pe") == "IBCT_IndexUInt"
        start = ibo + int(ck.get("teOffset") or 0)
        width = 4 if wide else 2
        if start + ni * width > len(buf):
            raise MeshReadError(f"render chunk {ci}'s indices run past the mesh's buffer")
        idx = np.frombuffer(buf, "<u4" if wide else "<u2", ni, start).astype(np.uint32)
        if len(idx) and int(idx.max()) >= nv:
            raise MeshReadError(f"render chunk {ci}'s indices point past its {nv} vertices")
        if nrm is None:
            nrm = np.zeros((nv, 3), np.float32)
        ch = {"chunk": ci, "lod": lod, "positions": pos.astype(np.float32), "normals": nrm.astype(np.float32),
              "indices": idx[:len(idx) - len(idx) % 3]}
        if uv:
            ch["uv"] = tex
        out.append(ch)
    return out


def appearances(c: CR2W) -> list:
    """[(name, [material name per render chunk])], the default (first) one first; an unnamed one is
    called ``default``."""
    out = []
    for h in c.props(0).get("appearances") or []:
        if not isinstance(h, int) or not 0 <= h < len(c.exports):
            continue
        p = c.props(h)
        out.append((str(p.get("name") or "") or "default", [str(n or "") for n in p.get("chunkMaterials") or []]))
    return out


def instance_params(c: CR2W, i: int) -> tuple:
    """(base material path, {parameter: value}) of a CMaterialInstance export."""
    base, vals = "", {}
    props, _ = c.chunk(i)
    for n, t, o, sz in props + c.tail_props(i):
        if n == "baseMaterial":
            base = str(c.value(t, o, sz) or "")
        elif n not in SKIP_PARAMS:
            try:
                vals[n] = c.value(t, o, sz)
            except (CR2WError, IndexError, struct.error):
                pass
    return base, vals


class Materials:
    """The materials of one mesh: ``get(name) -> {Name, BaseMaterial, MaterialTemplate, Data}`` (the
    shape of WolvenKit's Material.json entries), parameters merged up the instance chain.
    ``fetch(path) -> CR2W | None`` reads the other files (``.mi``)."""

    def __init__(self, c: CR2W, fetch, kraken=None):
        self.c = c
        self.fetch = fetch
        cm = c.props(0)
        self.entries = {}
        for e in cm.get("materialEntries") or []:
            self.entries.setdefault(str(e.get("name") or ""), (int(e.get("index") or 0), bool(e.get("isLocalInstance"))))
        self.local = []                     # (CR2W, export index) per local index
        lmb = cm.get("localMaterialBuffer") or {}
        raw = c.data_buffer(lmb.get("rawData") or 0) if lmb.get("rawData") else None
        if raw:
            for hd in lmb.get("rawDataHeaders") or []:
                o, n = int(hd.get("offset") or 0), int(hd.get("size") or 0)
                try:
                    self.local.append((CR2W(raw[o:o + n], kraken), 0))
                except (CR2WError, struct.error, IndexError):
                    self.local.append(None)
        else:
            for h in cm.get("preloadLocalMaterialInstances") or []:
                ok = isinstance(h, int) and 0 <= h < len(c.exports)
                self.local.append((c, h) if ok else None)
        self.external = [str(p or "") for p in (cm.get("externalMaterials") or cm.get("preloadExternalMaterials") or [])]
        self._memo = {}

    def get(self, name: str) -> dict | None:
        if name in self._memo:
            return self._memo[name]
        got = None
        ent = self.entries.get(name)
        if ent is not None:
            idx, local = ent
            first = None
            if local:
                if 0 <= idx < len(self.local) and self.local[idx] is not None:
                    first = self.local[idx]
            elif 0 <= idx < len(self.external) and self.external[idx]:
                fc = self.fetch(self.external[idx])
                if fc is not None and fc.exports and fc.exports[0]["cls"] == "CMaterialInstance":
                    first = (fc, 0)
                elif self.external[idx].lower().endswith((".mt", ".remt")):
                    got = {"Name": name, "BaseMaterial": self.external[idx], "MaterialTemplate": self.external[idx], "Data": {}}
            if first is not None:
                got = self._chain(name, first)
        self._memo[name] = got
        return got

    def _chain(self, name, first) -> dict:
        data = {}
        c, i = first
        base, vals = instance_params(c, i)
        data.update(vals)
        first_base, template = base, ""
        seen = set()
        for _ in range(MAX_CHAIN):
            low = base.lower()
            if not base or low in seen:
                break
            seen.add(low)
            if low.endswith((".mt", ".remt")):
                template = base
                break
            fc = self.fetch(base)
            if fc is None or not fc.exports or fc.exports[0]["cls"] != "CMaterialInstance":
                template = base
                break
            base, vals = instance_params(fc, 0)
            for k, v in vals.items():
                data.setdefault(k, v)
        # what nothing sets: the template's own defaults (a stand-in's grey base colour...)
        if template:
            for k, v in template_defaults(self.fetch, template).items():
                data.setdefault(k, v)
        return {"Name": name, "BaseMaterial": first_base, "MaterialTemplate": template, "Data": data}


_TEMPLATES: dict = {}
PARAM_VALUE = ("texture", "vector", "scalar", "color", "value")


def template_defaults(fetch, path: str) -> dict:
    """{parameter: default value} of a material template (.mt / .remt: its CMaterialParameter*)."""
    key = path.lower()
    got = _TEMPLATES.get(key)
    if got is not None:
        return got
    got = {}
    c = fetch(path)
    if c is not None:
        for i, e in enumerate(c.exports):
            if not e["cls"].startswith("CMaterialParameter"):
                continue
            try:
                p = c.props(i)
            except (CR2WError, IndexError, struct.error):
                continue
            n = p.get("parameterName")
            if not n:
                continue
            for k in PARAM_VALUE:
                if k in p:
                    got[str(n)] = p[k]
                    break
    if len(_TEMPLATES) > 2000:
        _TEMPLATES.clear()
    _TEMPLATES[key] = got
    return got


# ----------------------------------------------------------------------------- textures
TCM_FORMAT = {"TCM_DXTNoAlpha": 0x31545844, "TCM_DXTAlpha": 0x35545844, "TCM_DXTAlphaLinear": 0x35545844,
              "TCM_QualityR": 0x31495441, "TCM_QualityRG": 0x32495441, "TCM_Normalmap": 0x32495441,
              "TCM_QualityColor": 0x20374342}
RAW_FORMAT = {"TRF_TrueColor": 32, "TRF_Grayscale": 50}         # A8B8G8R8, L8


def texture_rgba(c: CR2W, i: int, max_size: int = 64) -> tuple:
    """(uint8 (h, w, 4) RGBA of the largest mip not over ``max_size``, sRGB?) of a CBitmapTexture
    export, or (None, False) when its format is not one read here (BC7, HDR)."""
    from ..gta import texture as gt
    from .. import imaging
    p = c.props(i)
    setup = p.get("setup") or {}
    comp = setup.get("compression") or "TCM_None"
    gamma = bool(setup.get("isGamma"))
    if comp == "TCM_None":
        fmt = {32: gt.FMT_A8B8G8R8, 50: gt.FMT_L8}.get(RAW_FORMAT.get(setup.get("rawFormat") or "TRF_TrueColor"))
    else:
        fmt = {0x31545844: gt.FMT_DXT1, 0x35545844: gt.FMT_DXT5, 0x31495441: gt.FMT_ATI1,
               0x32495441: gt.FMT_ATI2, 0x20374342: gt.FMT_BC7}.get(TCM_FORMAT.get(comp))
    if fmt is None:
        return None, gamma
    rtr = p.get("renderTextureResource") or {}
    bh = rtr.get("renderResourceBlobPC")
    if not isinstance(bh, int) or not 0 <= bh < len(c.exports):
        return None, gamma
    blob = c.props(bh)
    hdr = blob.get("header") or {}
    data = c.data_buffer(blob.get("textureData") or 0)
    if not data:
        return None, gamma
    size = hdr.get("sizeInfo") or {}
    w0, h0 = int(size.get("width") or p.get("width") or 0), int(size.get("height") or p.get("height") or 0)
    mips = hdr.get("mipMapInfo") or []
    if not w0 or not h0 or not mips:
        return None, gamma
    pick = len(mips) - 1
    for m in range(len(mips)):
        if max(max(1, w0 >> m), max(1, h0 >> m)) <= max_size:
            pick = m
            break
    w, h = max(1, w0 >> pick), max(1, h0 >> pick)
    pl = mips[pick].get("placement") or {}
    o = int(pl.get("offset") or 0)
    # the mip's size from its format (a texture's layout pitches are not always filled in)
    block = {gt.FMT_DXT1: 8, gt.FMT_ATI1: 8, gt.FMT_DXT5: 16, gt.FMT_ATI2: 16, gt.FMT_BC7: 16}.get(fmt)
    n = ((w + 3) // 4) * ((h + 3) // 4) * block if block else w * h * (4 if fmt == gt.FMT_A8B8G8R8 else 1)
    if o + n > len(data):
        return None, gamma
    try:
        img = imaging.decode(fmt, w, h, data[o:o + n])
    except (ValueError, IndexError):
        img = None
    return img, gamma
