"""Intermediate representation between the GTA readers and the Cyberpunk writers.

All geometry here is already in Cyberpunk "Root" space (metres, X right, Y forward, Z up,
wheel centres at Z = 0) and, for every part, expressed in the local frame of the rig bone
the part is attached to.  Triangles are counter-clockwise when seen from the outside.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LOD_MASKS = {"high": 1, "med": 2, "low": 4, "vlow": 8}


@dataclass
class SubMesh:
    material: str
    positions: np.ndarray          # (N,3) float32
    normals: np.ndarray            # (N,3) float32
    tangents: np.ndarray           # (N,4) float32, w = handedness
    uv0: np.ndarray                # (N,2)
    uv1: np.ndarray | None         # (N,2)
    colors: np.ndarray | None      # (N,4) float 0..1
    indices: np.ndarray            # (M,) uint32
    lod: int = 1                   # Cyberpunk LOD mask: 1, 2, 4, 8

    @property
    def vertex_count(self):
        return len(self.positions)


@dataclass
class Part:
    """One Cyberpunk mesh component (one .mesh file)."""
    name: str                      # component name in the .app (e.g. body_01, door_fl_a)
    mesh_name: str                 # file stem of the .mesh
    slot: str | None               # vehicle_slots slot / rig bone it is bound to (None = bound to `parent`)
    parent: str | None             # parent component name when not bound to a slot
    kind: str                      # body, interior, glass, lights, door, window, hood, trunk, wheel, tire, steering
    submeshes: list = field(default_factory=list)
    physical: bool = True          # entPhysicalMeshComponent vs entMeshComponent
    paintable: bool = False        # takes CrystalCoat / Open77 paint
    light_zones: dict = field(default_factory=dict)  # zone index -> light spec (for light meshes)
    gta_bones: list = field(default_factory=list)

    def bounds(self):
        pts = [s.positions for s in self.submeshes if s.lod == 1 and len(s.positions)]
        if not pts:
            return np.zeros(3), np.zeros(3)
        p = np.concatenate(pts)
        return p.min(0), p.max(0)

    def vertex_count(self, lod=1):
        return sum(s.vertex_count for s in self.submeshes if s.lod == lod)


@dataclass
class Material:
    key: str
    # paint, paint_detail (decal over paint), textured, glass, glass_opaque, glass_cutout, lightcover,
    # lights, emissive, tire, decal, plate
    kind: str
    diffuse: str | None = None
    normal: str | None = None
    spec: str | None = None
    emissive_tex: str | None = None
    color: tuple = (1.0, 1.0, 1.0, 1.0)
    paint_slot: int = 0          # 0 none, 1 primary, 2 secondary
    alpha: str = "opaque"        # opaque | test | blend
    roughness: float = 0.5
    metalness: float = 0.0
    emissive_ev: float = 0.0
    gta_shader: str = ""
    livery: str | None = None    # paint: DiffuseSampler2, the livery GTA lays over the paint (2nd UV set)
    tint: tuple | None = None    # glass: transmitted colour (0..1) worked out from the GTA texture


@dataclass
class Bone:
    name: str
    parent: str | None
    translation: tuple        # relative to parent
    rotation: tuple           # quaternion (x, y, z, w) relative to parent


@dataclass
class LightSpec:
    name: str
    light_type: str           # Head, Brake, LeftBlinker, RightBlinker, Reverse, Interior, Utility
    zone: int
    part: str                 # parent mesh component
    position: tuple           # in the parent component's frame
    direction: tuple          # unit vector the light points to (Root axes)
    color: tuple = (255, 250, 220)


@dataclass
class WheelAxle:
    radius: float
    width: float
    rim_radius: float
    positions: dict           # side -> (x, y, z) in Root space (z is the wheel centre, 0)


@dataclass
class Vehicle:
    name: str                 # sanitised id (lower-case, [a-z0-9_])
    display_name: str
    make: str
    gta_class: str
    gta_layout: str
    door_count: int
    audio_name: str = ""                              # GTA audioNameHash (the sound the car asks for)
    parts: dict = field(default_factory=dict)        # name -> Part
    materials: dict = field(default_factory=dict)    # key -> Material
    textures: dict = field(default_factory=dict)     # name -> GtaTexture (only the ones used)
    bones: dict = field(default_factory=dict)        # cyberpunk rig bone overrides: name -> Bone (absolute)
    axles: dict = field(default_factory=dict)        # 'front'/'back' -> WheelAxle
    lights: list = field(default_factory=list)
    seats: dict = field(default_factory=dict)        # seat_front_left -> abs position
    handling: object = None
    paint_colors: tuple = ((0.2, 0.2, 0.2), (0.2, 0.2, 0.2))
    base_z: float = 0.44
    gta_wheel_z: float = 0.0
    bbox: tuple = ((0, 0, 0), (0, 0, 0))
    exhausts: list = field(default_factory=list)     # exhaust tips (Root space)
    part_frames: dict = field(default_factory=dict)  # part -> (translation, 3x3) of the bone it is expressed in
    markers: dict = field(default_factory=dict)      # GTA helper bones (neon_l ...) -> Root-space position
    neon: object = None                              # model.neon.NeonKit when the car has an underglow kit
    seat_fit: object = None                          # model.seats.SeatFit: posture, head room, exits
    static_wheels: list = field(default_factory=list)  # GTA wheel bones of extra axles drawn fixed to the body
    warnings: list = field(default_factory=list)
    report: dict = field(default_factory=dict)

    def detach_to_body(self, name: str, host: str = "body_01"):
        """Make a part static: bound to the body instead of a rig bone of its own (a donor whose rig
        has no such bone -- rear quarter windows on a two-door donor).  Its geometry, and the lights
        that hang off it, are re-expressed in the body's frame; it no longer moves on its own."""
        part = self.parts[name]
        t, r = (np.asarray(x, np.float64) for x in self.part_frames.get(name, (np.zeros(3), np.eye(3))))
        tb, rb = (np.asarray(x, np.float64) for x in self.part_frames.get(host, (np.zeros(3), np.eye(3))))
        to_body = r.T @ rb                       # bone-local direction -> body-local direction
        for s in part.submeshes:
            if len(s.positions):
                root = s.positions.astype(np.float64) @ r.T + t
                s.positions = ((root - tb) @ rb).astype(s.positions.dtype)
            if s.normals is not None and len(s.normals):
                s.normals = (s.normals.astype(np.float64) @ to_body).astype(s.normals.dtype)
            if s.tangents is not None and len(s.tangents):
                tan = s.tangents.astype(np.float64).copy()
                tan[:, :3] = tan[:, :3] @ to_body
                s.tangents = tan.astype(s.tangents.dtype)
        for light in self.lights:
            if light.part == name:
                root = np.asarray(light.position, np.float64) @ r.T + t
                light.position = tuple(float(x) for x in (root - tb) @ rb)
                light.part = host
        part.slot = None
        part.parent = host
        self.part_frames[name] = (tb.copy(), rb.copy())
        return part

    def rename_parts(self, mapping: dict):
        """Rename parts and their rig bones in one go (a swap such as hood_a <-> trunk_a is fine):
        part keys, names, meshes, slots and parents, bone keys and parents, part frames, the light
        components that hang off them and the report."""
        if not mapping:
            return

        def ren(n):
            if not isinstance(n, str):
                return n
            if n in mapping:
                return mapping[n]
            if "_lights_" in n:                       # lamps carried by a renamed lid/door
                host, side = n.split("_lights_", 1)
                if host in mapping:
                    return f"{mapping[host]}_lights_{side}"
            return n

        self.parts = {ren(k): p for k, p in self.parts.items()}
        for p in self.parts.values():
            p.name, p.mesh_name, p.slot, p.parent = ren(p.name), ren(p.mesh_name), ren(p.slot), ren(p.parent)
            if p.name in ("hood_a", "trunk_a"):
                p.kind = p.name[:-2]
        bones = {}
        for k, b in self.bones.items():
            b = dict(b)
            if "parent" in b:
                b["parent"] = ren(b["parent"])
            bones[ren(k)] = b
        self.bones = bones
        self.part_frames = {ren(k): f for k, f in self.part_frames.items()}
        for light in self.lights:
            light.part = ren(light.part)
        parts = self.report.get("parts")
        if isinstance(parts, dict):
            self.report["parts"] = {ren(k): v for k, v in parts.items()}
        elif isinstance(parts, list):
            self.report["parts"] = [ren(x) if isinstance(x, str) else x for x in parts]
