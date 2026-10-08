"""glTF 2.0 binary writer producing files WolvenKit's mesh importer accepts.

Conventions required by WolvenKit (MeshImportTools.ImportMesh):
  * one node per submesh, each node owns a mesh with exactly one primitive;
  * submeshes are imported in node-name order, LOD from the "LOD_<n>" suffix (1, 2, 4, 8);
  * POSITION/NORMAL/TANGENT are required; WolvenKit maps glTF (x, y, z) -> RED (x, -z, y).
"""
from __future__ import annotations

import json
import struct

import numpy as np


def red_to_gltf(v: np.ndarray) -> np.ndarray:
    """RED (X right, Y fwd, Z up) -> glTF (X, Z, -Y)."""
    out = np.empty_like(v)
    out[:, 0] = v[:, 0]
    out[:, 1] = v[:, 2]
    out[:, 2] = -v[:, 1]
    return out


def submesh_node_name(i: int, lod: int) -> str:
    return f"submesh_{i:03d}_LOD_{lod}"


class GlbWriter:
    def __init__(self):
        self.bin = bytearray()
        self.views = []
        self.accessors = []
        self.meshes = []
        self.nodes = []
        self.materials = {}

    def _view(self, data: bytes, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin += data
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        self.views.append(v)
        return len(self.views) - 1

    def _acc(self, arr: np.ndarray, ctype: int, typ: str, target=None, minmax=False, normalized=False):
        view = self._view(np.ascontiguousarray(arr).tobytes(), target)
        a = {"bufferView": view, "componentType": ctype, "count": int(arr.shape[0]), "type": typ}
        if normalized:
            a["normalized"] = True
        if minmax:
            a["min"] = [float(x) for x in arr.min(0)]
            a["max"] = [float(x) for x in arr.max(0)]
        self.accessors.append(a)
        return len(self.accessors) - 1

    def _material(self, name):
        if name not in self.materials:
            self.materials[name] = len(self.materials)
        return self.materials[name]

    def add_submesh(self, name, positions, normals, tangents, uv0, uv1=None, colors=None, indices=None,
                    material="default"):
        p = red_to_gltf(positions.astype(np.float32)).astype(np.float32)
        n = red_to_gltf(normals.astype(np.float32)).astype(np.float32)
        t4 = tangents.astype(np.float32).copy()
        t4[:, :3] = red_to_gltf(t4[:, :3])
        attrs = {
            "POSITION": self._acc(p, 5126, "VEC3", 34962, minmax=True),
            "NORMAL": self._acc(n, 5126, "VEC3", 34962),
            "TANGENT": self._acc(t4, 5126, "VEC4", 34962),
            "TEXCOORD_0": self._acc(uv0.astype(np.float32), 5126, "VEC2", 34962),
        }
        if uv1 is not None:
            attrs["TEXCOORD_1"] = self._acc(uv1.astype(np.float32), 5126, "VEC2", 34962)
        if colors is not None:
            attrs["COLOR_0"] = self._acc(np.clip(colors, 0, 1).astype(np.float32), 5126, "VEC4", 34962)
        idx = indices.astype(np.uint32)
        if idx.max(initial=0) < 65535:
            ia = self._acc(idx.astype(np.uint16), 5123, "SCALAR", 34963)
        else:
            ia = self._acc(idx, 5125, "SCALAR", 34963)
        self.meshes.append({"name": name, "primitives": [{"attributes": attrs, "indices": ia, "mode": 4,
                                                          "material": self._material(material)}]})
        self.nodes.append({"name": name, "mesh": len(self.meshes) - 1})

    def tobytes(self) -> bytes:
        gltf = {
            "asset": {"version": "2.0", "generator": "fivem2open77"},
            "scene": 0,
            "scenes": [{"name": "Scene", "nodes": list(range(len(self.nodes)))}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": [{"name": n} for n, _ in sorted(self.materials.items(), key=lambda kv: kv[1])],
            "accessors": self.accessors,
            "bufferViews": self.views,
            "buffers": [{"byteLength": len(self.bin)}],
        }
        js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        js += b" " * ((4 - len(js) % 4) % 4)
        binb = bytes(self.bin) + b"\0" * ((4 - len(self.bin) % 4) % 4)
        total = 12 + 8 + len(js) + 8 + len(binb)
        return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
                + struct.pack("<I4s", len(binb), b"BIN\0") + binb)

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self.tobytes())
