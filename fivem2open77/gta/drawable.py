"""GTA V legacy drawables: shaders, skeleton, models, geometries and vertex data.

Structure offsets follow CodeWalker's ``Drawable.cs`` (legacy / gen8 path).
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field

import numpy as np

from .jenkhash import HashNames, joaat
from .reader import ResourceReader
from .texture import GtaTexture, read_texture, read_texture_dictionary, texture_name

_DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gta_names.json")
with open(_DATA, "r", encoding="utf-8") as _f:
    _names = json.load(_f)
SHADER_NAMES = HashNames(_names["shaders"])
PARAM_NAMES = HashNames(_names["params"])

# ---------------------------------------------------------------------------
# Shaders
# ---------------------------------------------------------------------------


@dataclass
class ShaderParam:
    name: str
    texture: str | None = None          # texture name for sampler params
    embedded: GtaTexture | None = None  # TextureBase with data (rare: usually only a name reference)
    vector: tuple | None = None         # first Vector4 for numeric params
    vectors: list | None = None         # all Vector4 rows for array params


@dataclass
class Shader:
    index: int
    name: str           # e.g. vehicle_paint1
    file_name: str      # e.g. vehicle_paint1.sps
    render_bucket: int
    params: dict = field(default_factory=dict)  # lower-case name -> ShaderParam

    def tex(self, *names: str) -> str | None:
        for n in names:
            p = self.params.get(n.lower())
            if p is not None and p.texture:
                return p.texture
        return None

    def vec(self, name: str, default=None):
        p = self.params.get(name.lower())
        return p.vector if p is not None and p.vector is not None else default


def _read_shader(rd: ResourceReader, ptr: int, index: int) -> Shader:
    c = rd.at(ptr)
    params_ptr = c.u64()
    name_hash = c.u32()
    c.u32()
    param_count = c.u8()
    render_bucket = c.u8()
    c.u16()
    c.u16()  # ParameterSize
    c.u16()  # ParameterDataSize
    file_hash = c.u32()
    name = SHADER_NAMES.get(name_hash)
    # FileName is the hash of "<name>.sps" (e.g. vehicle_paint1.sps)
    file_name = name + ".sps" if joaat(name + ".sps") == file_hash else f"0x{file_hash:08X}"
    shader = Shader(index=index, name=name, file_name=file_name, render_bucket=render_bucket)
    if not params_ptr or not param_count:
        return shader
    pc = rd.at(params_ptr)
    raw = []
    for _ in range(param_count):
        dtype = pc.u8()
        pc.u8()
        pc.u16()
        pc.u32()
        dptr = pc.u64()
        raw.append((dtype, dptr))
    # data block follows the parameter headers; hashes follow the data block
    offset = 0
    for dtype, _ in raw:
        if dtype == 1:
            offset += 16
        elif dtype > 1:
            offset += 16 * dtype
    pc.skip(offset)
    hashes = [pc.u32() for _ in range(param_count)]
    for (dtype, dptr), h in zip(raw, hashes):
        pname = PARAM_NAMES.get(h)
        sp = ShaderParam(name=pname)
        if dtype == 0:
            if dptr:
                # TextureBase: name at +0x28; may be a full Texture if embedded
                tc = rd.at(dptr)
                tc.skip(0x28)
                sp.texture = texture_name(rd.string_at(tc.u64())) or None
        elif dtype == 1:
            if dptr:
                sp.vector = struct.unpack_from("<4f", *_seg(rd, dptr))
        else:
            if dptr:
                buf, off = _seg(rd, dptr)
                sp.vectors = [struct.unpack_from("<4f", buf, off + 16 * i) for i in range(dtype)]
                sp.vector = sp.vectors[0]
        shader.params[pname.lower()] = sp
    return shader


def _seg(rd: ResourceReader, ptr: int):
    buf, off, _ = rd.segment(ptr)
    return buf, off


@dataclass
class ShaderGroup:
    shaders: list
    textures: dict  # embedded texture dictionary {lower name: GtaTexture}


def read_shader_group(rd: ResourceReader, ptr: int) -> ShaderGroup:
    c = rd.at(ptr)
    if c is None:
        return ShaderGroup([], {})
    c.skip(8)
    txd_ptr = c.u64()
    shaders_ptr = c.u64()
    count = c.u16()
    shaders = [_read_shader(rd, p, i) for i, p in enumerate(rd.pointer_array(shaders_ptr, count)) if p]
    textures = read_texture_dictionary(rd, txd_ptr) if txd_ptr else {}
    return ShaderGroup(shaders, textures)


# ---------------------------------------------------------------------------
# Skeleton
# ---------------------------------------------------------------------------


@dataclass
class Bone:
    index: int
    name: str
    tag: int
    parent: int
    flags: int
    rotation: tuple     # quaternion (x, y, z, w)
    translation: tuple
    scale: tuple


@dataclass
class RotationLimit:
    bone_tag: int
    min: tuple
    max: tuple


@dataclass
class Skeleton:
    bones: list
    bind: list          # world (model-space) 4x4 row-major matrices, SharpDX layout
    inverse_bind: list

    def by_name(self) -> dict:
        return {b.name.lower(): b for b in self.bones}

    def by_tag(self) -> dict:
        return {b.tag: b for b in self.bones}

    def world_matrix(self, index: int) -> np.ndarray:
        """Model-space transform of a bone, computed from the local TRS chain (4x4, column vectors)."""
        m = np.eye(4)
        i = index
        chain = []
        while i >= 0:
            chain.append(self.bones[i])
            i = self.bones[i].parent
        for b in reversed(chain):
            m = m @ trs_matrix(b.translation, b.rotation, b.scale)
        return m


def quat_to_mat3(q) -> np.ndarray:
    x, y, z, w = q
    n = x * x + y * y + z * z + w * w
    if n == 0:
        return np.eye(3)
    s = 2.0 / n
    return np.array([
        [1 - s * (y * y + z * z), s * (x * y - z * w), s * (x * z + y * w)],
        [s * (x * y + z * w), 1 - s * (x * x + z * z), s * (y * z - x * w)],
        [s * (x * z - y * w), s * (y * z + x * w), 1 - s * (x * x + y * y)],
    ])


def trs_matrix(t, q, s=(1, 1, 1)) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = quat_to_mat3(q) * np.array(s)[None, :]
    m[:3, 3] = t
    return m


def read_skeleton(rd: ResourceReader, ptr: int) -> Skeleton | None:
    c = rd.at(ptr)
    if c is None:
        return None
    c.skip(8 + 8)
    c.u64()  # bone tags ptr
    c.u16()
    c.u16()
    c.u32()
    bones_ptr = c.u64()
    inv_ptr = c.u64()
    tr_ptr = c.u64()
    parents_ptr = c.u64()
    c.u64()  # child indices
    c.u64()
    c.skip(12)
    c.u16()
    bones_count = c.u16()
    bones = []
    bc = rd.at(bones_ptr)
    for i in range(bones_count):
        rot = bc.vec4()
        trans = bc.vec3()
        bc.u32()
        scale = bc.vec3()
        bc.f32()
        bc.i16()  # next sibling
        parent = bc.i16()
        bc.u32()
        name_ptr = bc.u64()
        flags = bc.u16()
        index = bc.i16()
        tag = bc.u16()
        bc.i16()
        bc.u64()
        bones.append(Bone(index=i, name=rd.string_at(name_ptr) or f"bone_{i}", tag=tag, parent=parent,
                          flags=flags, rotation=rot, translation=trans, scale=scale))
    parents = rd.i16s_at(parents_ptr, bones_count)
    for b, p in zip(bones, parents):
        b.parent = p
    return Skeleton(bones=bones, bind=rd.matrices_at(tr_ptr, bones_count),
                    inverse_bind=rd.matrices_at(inv_ptr, bones_count))


def read_joints(rd: ResourceReader, ptr: int) -> list:
    c = rd.at(ptr)
    if c is None:
        return []
    c.skip(16)
    rot_ptr = c.u64()
    c.u64()
    c.skip(16)
    rot_count = c.u16()
    out = []
    buf, off = _seg(rd, rot_ptr) if rot_ptr else (None, 0)
    for i in range(rot_count):
        base = off + i * 0xC0
        tag = struct.unpack_from("<H", buf, base + 0x08)[0]
        mn = struct.unpack_from("<3f", buf, base + 0x5C)
        mx = struct.unpack_from("<3f", buf, base + 0x68)
        out.append(RotationLimit(bone_tag=tag, min=mn, max=mx))
    return out


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

SEMANTICS = ["position", "blendweights", "blendindices", "normal", "colour0", "colour1",
             "texcoord0", "texcoord1", "texcoord2", "texcoord3", "texcoord4", "texcoord5",
             "texcoord6", "texcoord7", "tangent", "binormal"]
# VertexComponentType -> (bytes, numpy dtype, components)
COMPONENT_TYPES = {
    0: (0, None, 0),
    1: (4, np.float16, 2),   # Half2
    2: (4, np.float32, 1),   # Float
    3: (8, np.float16, 4),   # Half4
    4: (0, None, 0),         # FloatUnk
    5: (8, np.float32, 2),   # Float2
    6: (12, np.float32, 3),  # Float3
    7: (16, np.float32, 4),  # Float4
    8: (4, np.uint8, 4),     # UByte4
    9: (4, np.uint8, 4),     # Colour (normalized RGBA8)
    10: (4, np.int8, 4),     # RGBA8 SNorm
}


@dataclass
class Geometry:
    shader_index: int
    vertex_flags: int
    vertex_types: int
    stride: int
    attrs: dict                     # semantic -> numpy array (N, C)
    indices: np.ndarray             # (M,) uint32
    bone_ids: list                  # palette: blendindices -> skeleton bone index
    aabb: tuple | None = None

    @property
    def vertex_count(self) -> int:
        return len(self.attrs["position"])

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


def decode_vertices(raw: bytes, count: int, stride: int, flags: int, types: int) -> dict:
    if count == 0:
        return {"position": np.zeros((0, 3), np.float32)}
    arr = np.frombuffer(raw[:count * stride], dtype=np.uint8).reshape(count, stride)
    off = 0
    out = {}
    for sem in range(16):
        if not (flags >> sem) & 1:
            continue
        ctype = (types >> (sem * 4)) & 0xF
        size, dt, comps = COMPONENT_TYPES.get(ctype, (0, None, 0))
        if size == 0:
            continue
        chunk = np.ascontiguousarray(arr[:, off:off + size])
        vals = chunk.view(dt).reshape(count, comps)
        if ctype == 9:
            if SEMANTICS[sem] == "blendindices":
                vals = vals.astype(np.int32)
            else:
                vals = vals.astype(np.float32) / 255.0
        elif ctype == 10:
            vals = np.maximum(vals.astype(np.float32) / 127.0, -1.0)
        elif dt == np.float16:
            vals = vals.astype(np.float32)
        out[SEMANTICS[sem]] = vals
        off += size
    return out


def _read_geometry(rd: ResourceReader, ptr: int) -> Geometry | None:
    c = rd.at(ptr)
    if c is None:
        return None
    c.skip(0x18)
    vb_ptr = c.u64()
    c.skip(0x18)
    ib_ptr = c.u64()
    c.skip(0x18)
    indices_count = c.u32()
    c.u32()  # triangles
    vertices_count = c.u16()
    c.u16()
    c.u32()
    bone_ids_ptr = c.u64()
    c.u16()  # vertex stride
    bone_ids_count = c.u16()
    # vertex buffer
    vc = rd.at(vb_ptr)
    vc.skip(8)
    vstride = vc.u16()
    vc.u16()
    vc.u32()
    data1 = vc.u64()
    vcount = vc.u32()
    vc.u32()
    data2 = vc.u64()
    vc.u64()
    info_ptr = vc.u64()
    ic = rd.at(info_ptr)
    vflags = ic.u32()
    ic.u16()
    ic.u8()
    ic.u8()
    vtypes = ic.u64()
    raw = rd.bytes_at(data1 or data2, vcount * vstride)
    attrs = decode_vertices(raw, vcount, vstride, vflags, vtypes)
    # index buffer
    ixc = rd.at(ib_ptr)
    ixc.skip(8)
    icount = ixc.u32()
    ixc.u32()
    iptr = ixc.u64()
    ibuf, ioff = _seg(rd, iptr)
    indices = np.frombuffer(ibuf, dtype=np.uint16, count=icount, offset=ioff).astype(np.uint32)
    return Geometry(shader_index=0, vertex_flags=vflags, vertex_types=vtypes, stride=vstride,
                    attrs=attrs, indices=indices, bone_ids=rd.u16s_at(bone_ids_ptr, bone_ids_count))


@dataclass
class Model:
    lod: str                 # high / med / low / vlow
    index: int
    bone_index: int          # rigid binding bone (when not skinned)
    has_skin: bool
    render_mask: int
    geometries: list


def _read_model(rd: ResourceReader, ptr: int, lod: str, index: int) -> Model:
    c = rd.at(ptr)
    c.skip(8)
    geoms_ptr = c.u64()
    gcount = c.u16()
    c.u16()
    c.u32()
    bounds_ptr = c.u64()
    shadermap_ptr = c.u64()
    binding = c.u32()
    render_mask = c.u16()
    shader_map = rd.u16s_at(shadermap_ptr, gcount)
    geoms = []
    bounds = []
    if bounds_ptr:
        nb = gcount + 1 if gcount > 1 else gcount
        fl = rd.floats_at(bounds_ptr, nb * 8)
        bounds = [(tuple(fl[i * 8:i * 8 + 3]), tuple(fl[i * 8 + 4:i * 8 + 7])) for i in range(nb)]
    for i, gp in enumerate(rd.pointer_array(geoms_ptr, gcount)):
        g = _read_geometry(rd, gp)
        if g is None:
            continue
        g.shader_index = shader_map[i] if i < len(shader_map) else 0
        if bounds:
            g.aabb = bounds[i + 1] if len(bounds) > 1 and i + 1 < len(bounds) else bounds[0]
        geoms.append(g)
    return Model(lod=lod, index=index, bone_index=(binding >> 24) & 0xFF, has_skin=bool((binding >> 8) & 0xFF),
                 render_mask=render_mask, geometries=geoms)


@dataclass
class Light:
    position: tuple
    color: tuple
    intensity: float
    bone_tag: int
    type: int            # 1 point, 2 spot, 4 capsule
    flags: int
    falloff: float
    falloff_exponent: float
    direction: tuple
    tangent: tuple
    cone_inner: float
    cone_outer: float
    corona_size: float
    corona_intensity: float
    extent: tuple


def read_lights(rd: ResourceReader, list_cursor) -> list:
    ptr, count, _ = rd.pointer_list_header(list_cursor)
    out = []
    if not ptr or not count:
        return out
    buf, off = _seg(rd, ptr)
    for i in range(count):
        b = off + i * 0xA8
        pos = struct.unpack_from("<3f", buf, b + 0x08)
        r, g, bl, _ = struct.unpack_from("<4B", buf, b + 0x18)
        intensity, flags = struct.unpack_from("<fI", buf, b + 0x1C)
        bone_id, ltype, _grp = struct.unpack_from("<HBB", buf, b + 0x24)
        falloff, fexp = struct.unpack_from("<2f", buf, b + 0x2C)
        corona_size = struct.unpack_from("<f", buf, b + 0x5C)[0]
        corona_int = struct.unpack_from("<f", buf, b + 0x6C)[0]
        direction = struct.unpack_from("<3f", buf, b + 0x74)
        tangent = struct.unpack_from("<3f", buf, b + 0x80)
        cone_in, cone_out = struct.unpack_from("<2f", buf, b + 0x8C)
        extent = struct.unpack_from("<3f", buf, b + 0x94)
        out.append(Light(position=pos, color=(r, g, bl), intensity=intensity, bone_tag=bone_id, type=ltype,
                         flags=flags, falloff=falloff, falloff_exponent=fexp, direction=direction,
                         tangent=tangent, cone_inner=cone_in, cone_outer=cone_out, corona_size=corona_size,
                         corona_intensity=corona_int, extent=extent))
    return out


@dataclass
class Drawable:
    name: str
    shader_group: ShaderGroup
    skeleton: Skeleton | None
    joints: list
    models: dict              # lod -> [Model]
    bbox: tuple
    lod_dist: tuple

    def all_models(self):
        for lod in ("high", "med", "low", "vlow"):
            for m in self.models.get(lod, []):
                yield m


def read_drawable_base(rd: ResourceReader, ptr: int) -> tuple:
    """Reads the DrawableBase part. Returns (Drawable, cursor positioned at 0xA8)."""
    c = rd.at(ptr)
    c.skip(0x10)
    sg_ptr = c.u64()
    skel_ptr = c.u64()
    c.vec3()
    c.f32()
    bbmin = c.vec3()
    c.u32()
    bbmax = c.vec3()
    c.u32()
    lod_ptrs = [c.u64() for _ in range(4)]
    lod_dist = tuple(c.f32() for _ in range(4))
    c.skip(16)  # render mask flags
    joints_ptr = c.u64()
    c.u16()
    c.u16()
    c.u32()
    c.u64()  # all models pointer
    models = {}
    for lod, lp in zip(("high", "med", "low", "vlow"), lod_ptrs):
        if not lp:
            continue
        hc = rd.at(lp)
        mp, mcount, mcap = rd.pointer_list_header(hc)
        models[lod] = [_read_model(rd, p, lod, i) for i, p in enumerate(rd.pointer_array(mp, mcount)) if p]
    d = Drawable(name="", shader_group=read_shader_group(rd, sg_ptr), skeleton=read_skeleton(rd, skel_ptr),
                 joints=read_joints(rd, joints_ptr), models=models, bbox=(bbmin, bbmax), lod_dist=lod_dist)
    return d, c
