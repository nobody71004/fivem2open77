"""Preview model of a converted car: the IR assembled in Root space as one glTF binary, one node
per part (and per wheel corner), with flat or textured materials.  This is what the Studio's 3D
view shows before (and without) running WolvenKit: parts in their place, coloured as they will be.

Root space is X right, Y forward, Z up; glTF is X right, Y up, Z towards the viewer, so
(x, y, z) -> (x, z, -y).
"""
from __future__ import annotations

import json
import struct

import numpy as np

from .imaging import mean_color, png_bytes, texture_rgba

KIND_COLORS = {
    "body": (0.55, 0.57, 0.6), "interior": (0.25, 0.22, 0.2), "glass": (0.55, 0.7, 0.85),
    "door": (0.2, 0.55, 0.95), "window": (0.5, 0.75, 1.0), "hood": (0.95, 0.6, 0.15), "trunk": (0.95, 0.35, 0.2),
    "wheel": (0.75, 0.75, 0.78), "tire": (0.08, 0.08, 0.08), "hub": (0.5, 0.5, 0.5), "lights": (1.0, 0.95, 0.6),
    "steering": (0.6, 0.2, 0.8), "neon": (1.0, 0.05, 0.85),
    "wheel_static": (0.75, 0.75, 0.78), "tire_static": (0.08, 0.08, 0.08),
}
CORNERS = {"fl": "front", "fr": "front", "bl": "back", "br": "back"}


def _to_gltf(p):
    out = np.empty_like(p, dtype=np.float32)
    out[:, 0] = p[:, 0]
    out[:, 1] = p[:, 2]
    out[:, 2] = -p[:, 1]
    return out


class _Gltf:
    def __init__(self):
        self.bin = bytearray()
        self.doc = {"asset": {"version": "2.0", "generator": "fivem2open77 preview"}, "scene": 0,
                    "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "accessors": [], "bufferViews": [],
                    "materials": [], "textures": [], "images": [], "samplers": [{"magFilter": 9729, "minFilter": 9987}]}

    def view(self, data: bytes, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.doc["bufferViews"].append(v)
        return len(self.doc["bufferViews"]) - 1

    def accessor(self, arr, ctype, typ, target=None, minmax=False):
        arr = np.ascontiguousarray(arr)
        a = {"bufferView": self.view(arr.tobytes(), target), "componentType": ctype, "count": int(arr.shape[0]),
             "type": typ}
        if minmax:
            a["min"] = [float(x) for x in arr.min(0)]
            a["max"] = [float(x) for x in arr.max(0)]
        self.doc["accessors"].append(a)
        return len(self.doc["accessors"]) - 1

    def image(self, png: bytes):
        self.doc["images"].append({"bufferView": self.view(png), "mimeType": "image/png"})
        self.doc["textures"].append({"source": len(self.doc["images"]) - 1, "sampler": 0})
        return len(self.doc["textures"]) - 1

    def material(self, m: dict):
        self.doc["materials"].append(m)
        return len(self.doc["materials"]) - 1

    def save(self, path):
        while len(self.bin) % 4:
            self.bin.append(0)
        self.doc["buffers"] = [{"byteLength": len(self.bin)}]
        for k in ("textures", "images"):
            if not self.doc[k]:
                del self.doc[k]
        js = json.dumps(self.doc, separators=(",", ":")).encode()
        js += b" " * ((4 - len(js) % 4) % 4)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(self.bin)))
            f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            f.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))


def _part_to_root(v, part):
    """(R, t): local -> Root for a part (wheel parts are wheel-local and handled per corner)."""
    f = v.part_frames.get(part.name)
    if f is None:
        return np.eye(3), np.zeros(3)
    t, r = f
    return np.asarray(r, np.float64), np.asarray(t, np.float64)


def _paint_rgb(v, m):
    paint = [tuple(v.paint_colors[0]), tuple(v.paint_colors[1])]
    return list(paint[1 if m.paint_slot == 2 else 0])


def build_materials(v, g: _Gltf, textures=True, max_tex=128, tint_parts=False):
    """IR material key -> glTF material index; also returns {key: mean colour} for renders.
    Materials look the way the conversion lays them out: paint times its GTA texture (the decal
    overlays the game gets are not needed here), lamps in their own texture, glowing, glass in the
    tint its texture gives, the opaque parts of glass solid."""
    out, means, tex_cache = {}, {}, {}

    def image(name, tint=None):
        """(texture index, mean colour) of a texture, optionally multiplied by an RGB tint."""
        key = (name, tuple(tint) if tint is not None else None)
        if key not in tex_cache:
            rgba = texture_rgba(v.textures[name], max_tex) if name in v.textures else None
            if rgba is not None and tint is not None:
                rgba = rgba.copy()
                rgba[..., :3] = np.clip(rgba[..., :3].astype(np.float64) * np.asarray(tint)[None, None, :3], 0, 255).astype(np.uint8)
            tex_cache[key] = (g.image(png_bytes(rgba)), mean_color(rgba)) if rgba is not None else None
        return tex_cache[key]

    for key, m in v.materials.items():
        base = list(m.color[:3]) if m.color else [0.7, 0.7, 0.7]
        mat = {"name": key, "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.7},
               "doubleSided": False}
        alpha = 1.0
        tex = m.diffuse if textures and m.diffuse in v.textures else None
        tint = None
        if m.kind == "paint":
            base = _paint_rgb(v, m)
            mat["pbrMetallicRoughness"].update({"metallicFactor": 0.4, "roughnessFactor": 0.25})
            tint = base                      # GTA: paint x DiffuseSampler
        elif m.kind == "paint_detail":
            tex = None                       # the paint above already carries it
        elif m.kind in ("glass", "glass_tinted", "lightcover"):
            base = list(m.tint) if m.tint else [0.15, 0.2, 0.25]
            alpha = 0.35 if m.tint is None else float(max(0.12, min(0.92, 1.0 - float(np.mean(m.tint)) * 0.85)))
            mat["alphaMode"] = "BLEND"
            mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.05
            mat["doubleSided"] = True
            tex = None
        elif m.kind in ("glass_opaque", "glass_cutout"):
            mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.12
            if m.kind == "glass_cutout":
                mat["alphaMode"] = "MASK"
                mat["alphaCutoff"] = 0.95
        elif m.kind == "lights":
            base = [1.0, 0.95, 0.85]
            mat["emissiveFactor"] = [0.9, 0.85, 0.7]
        elif m.kind == "tire":
            base = [0.08, 0.08, 0.08]
        elif m.kind == "neon":
            base = list(m.color[:3])
            mat["emissiveFactor"] = [float(x) for x in m.color[:3]]
            mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.3
            tex = None
        elif m.kind == "decal":
            mat["alphaMode"] = "MASK"
            mat["alphaCutoff"] = 0.4
        means[key] = tuple(base)
        if m.kind in ("glass", "glass_tinted", "lightcover"):
            # flat renders cannot see through: glass reads as the dark cabin behind it, tinted
            means[key] = tuple(0.12 + 0.18 * np.asarray(base[:3], np.float64))
        if tex:
            t = image(tex, tint)
            if t is not None:
                mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": t[0]}
                if m.kind == "lights":
                    mat["emissiveTexture"] = {"index": t[0]}
                    mat["emissiveFactor"] = [0.8, 0.8, 0.8]
                means[key] = t[1]
                base = [1.0, 1.0, 1.0]
        mat["pbrMetallicRoughness"]["baseColorFactor"] = [float(base[0]), float(base[1]), float(base[2]), alpha]
        out[key] = g.material(mat)
    return out, means


def export_glb(v, path, textures=True, max_tex=128) -> dict:
    """Write the preview .glb; returns a small summary {parts, triangles, bytes}."""
    g = _Gltf()
    mats, _ = build_materials(v, g, textures=textures, max_tex=max_tex)
    roots = g.doc["scenes"][0]["nodes"]
    tris = 0

    def add_node(name, part, r, t, extras):
        nonlocal tris
        prims = []
        for sm in part.submeshes:
            if sm.lod != 1 or not len(sm.indices):
                continue
            if getattr(v.materials.get(sm.material), "kind", "") == "paint_detail":
                continue                         # drawn by the paint's own texture here
            p = sm.positions.astype(np.float64) @ r.T + t
            n = sm.normals.astype(np.float64) @ r.T
            attrs = {"POSITION": g.accessor(_to_gltf(p), 5126, "VEC3", 34962, minmax=True),
                     "NORMAL": g.accessor(_to_gltf(n), 5126, "VEC3", 34962)}
            if sm.uv0 is not None:
                attrs["TEXCOORD_0"] = g.accessor(sm.uv0.astype(np.float32), 5126, "VEC2", 34962)
            idx = sm.indices.astype(np.uint32)
            prims.append({"attributes": attrs, "indices": g.accessor(idx, 5125, "SCALAR", 34963),
                          "material": mats.get(sm.material, 0)})
            tris += len(idx) // 3
        if not prims:
            return
        g.doc["meshes"].append({"name": name, "primitives": prims})
        g.doc["nodes"].append({"name": name, "mesh": len(g.doc["meshes"]) - 1, "extras": extras})
        roots.append(len(g.doc["nodes"]) - 1)

    for name, part in v.parts.items():
        if part.kind in ("wheel", "tire"):
            continue
        r, t = _part_to_root(v, part)
        add_node(name, part, r, t, {"part": name, "kind": part.kind, "gta_bones": part.gta_bones})
    for corner, axle in CORNERS.items():
        ax = v.axles.get(axle)
        if ax is None or corner not in ax.positions:
            continue
        pos = np.array(ax.positions[corner], np.float64)       # Root space: wheel centres at Z = 0
        r = np.eye(3) if corner in ("fl", "bl") else np.diag([-1.0, -1.0, 1.0])   # right side: 180 deg about Z
        for kind in ("wheel", "tire"):
            part = v.parts.get(f"{kind}_{axle}")
            if part is not None:
                add_node(f"{kind}_{corner}", part, r, pos, {"part": f"{kind}_{axle}", "kind": kind, "corner": corner})
    g.save(path)
    import os
    return {"parts": len(roots), "triangles": int(tris), "bytes": os.path.getsize(path)}
