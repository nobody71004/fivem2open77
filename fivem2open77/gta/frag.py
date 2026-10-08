"""GTA V legacy fragments (.yft): the container used by vehicles.

A vehicle .yft holds:
  * FragType            root block
  * FragDrawable        main drawable (shaders, skeleton, LOD models)
  * PhysicsLODGroup     physics LODs with children; each child may carry its own drawable
                        (vehicle wheels live here on many cars) and a damaged variant
  * LightAttributes     light definitions (head/tail/brake/indicator positions, colours)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .drawable import Drawable, Light, read_drawable_base, read_lights
from .reader import ResourceReader
from .rsc7 import read_rsc7


@dataclass
class FragChild:
    index: int
    bone_tag: int
    group_index: int
    group_name: str
    pristine_mass: float
    drawable: Drawable | None
    damaged: Drawable | None


@dataclass
class Fragment:
    name: str
    drawable: Drawable
    extra_drawables: list            # DrawableArray (rare on vehicles)
    children: list = field(default_factory=list)
    lights: list = field(default_factory=list)
    gravity_factor: float = 1.0
    buoyancy_factor: float = 1.0

    @property
    def skeleton(self):
        return self.drawable.skeleton


def _read_frag_drawable(rd: ResourceReader, ptr: int) -> Drawable | None:
    if not ptr:
        return None
    d, c = read_drawable_base(rd, ptr)
    # FragDrawable extension starts at 0xA8
    c.u64()
    c.skip(64)              # FragMatrix (Matrix4F_s)
    c.u64()                 # bound
    c.u64()
    c.u16()
    c.u16()
    c.u32()
    c.u64()
    c.u16()
    c.u16()
    c.u32()
    c.skip(24)
    name_ptr = c.u64()
    d.name = rd.string_at(name_ptr) or ""
    return d


def _read_group_names(rd: ResourceReader, ptr: int, count: int) -> list:
    """FragPhysGroupNamesBlock: `count` FragPhysTypeGroups each own a 0x20-char name; but the
    names block is a pointer array to fixed 32-byte name structs inside each group (read from group)."""
    return []


def _read_physics(rd: ResourceReader, ptr: int, frag: Fragment):
    c = rd.at(ptr)
    if c is None:
        return
    c.skip(0x10)
    lod1 = c.u64()
    if not lod1:
        return
    lc = rd.at(lod1)
    lc.skip(0xC0)
    lc.u64()                 # group names
    groups_ptr = lc.u64()
    children_ptr = lc.u64()
    lc.skip(8 * 8)
    lc.u8()
    lc.u8()
    groups_count = lc.u8()
    lc.u8()
    lc.u8()
    children_count = lc.u8()
    group_names = []
    for gp in rd.pointer_array(groups_ptr, groups_count):
        if not gp:
            group_names.append("")
            continue
        # FragPhysTypeGroup: name struct (32 chars) at 0x80
        buf, off, _ = rd.segment(gp)
        raw = buf[off + 0x80:off + 0xA8]
        group_names.append(raw.split(b"\0", 1)[0].decode("ascii", "replace"))
    for i, cp in enumerate(rd.pointer_array(children_ptr, children_count)):
        if not cp:
            continue
        cc = rd.at(cp)
        cc.skip(8)
        pristine = cc.f32()
        cc.f32()
        group_index = cc.u16()
        bone_tag = cc.u16()
        cc.skip(4 + 8 * 17)
        d1 = cc.u64()
        d2 = cc.u64()
        frag.children.append(FragChild(
            index=i, bone_tag=bone_tag, group_index=group_index,
            group_name=group_names[group_index] if group_index < len(group_names) else "",
            pristine_mass=pristine, drawable=_read_frag_drawable(rd, d1), damaged=_read_frag_drawable(rd, d2)))


def read_fragment(rd: ResourceReader) -> Fragment:
    c = rd.at(0x50000000)
    c.skip(0x10)
    c.u64()
    c.u64()
    c.vec3()
    c.f32()
    drawable_ptr = c.u64()
    array_ptr = c.u64()
    c.u64()                    # array names
    array_count = c.u32()
    c.i32()
    c.u64()
    name_ptr = c.u64()
    c.skip(16)                 # cloths list
    c.skip(7 * 8)
    c.u64()                    # bone transforms
    c.skip(7 * 4)
    c.f32()                    # Unknown_CCh
    gravity = c.f32()
    buoyancy = c.f32()
    c.u8()
    c.u8()                     # glass windows count
    c.u16()
    c.u32()
    c.u64()                    # glass windows
    c.u64()
    physics_ptr = c.u64()
    c.u64()                    # drawable cloth
    c.u64()
    c.u64()
    lights = read_lights(rd, c)
    drawable = _read_frag_drawable(rd, drawable_ptr)
    extra = [_read_frag_drawable(rd, p) for p in rd.pointer_array(array_ptr, array_count) if p]
    frag = Fragment(name=rd.string_at(name_ptr) or "", drawable=drawable, extra_drawables=extra,
                    lights=lights, gravity_factor=gravity, buoyancy_factor=buoyancy)
    _read_physics(rd, physics_ptr, frag)
    return frag


def load_yft(path_or_bytes, name: str = "") -> Fragment:
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    rsc = read_rsc7(bytes(data), expect_version=162, name=name or str(path_or_bytes))
    return read_fragment(ResourceReader(rsc, name=name))
