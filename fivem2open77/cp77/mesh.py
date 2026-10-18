"""Mesh (.mesh) generation: a template CMesh JSON carrying our materials, plus a glb that
WolvenKit imports over it (geometry, LODs, quantisation, vertex streams)."""
from __future__ import annotations

import copy

import numpy as np

from .glb import GlbWriter, submesh_node_name
from .wkjson import cname

# render chunks (submeshes) one mesh may have: the game keeps the count in 8 bits, and a mesh with
# more crashes it as soon as it streams in (271 wrapped to 15).  Builders stay well under it.
MAX_RENDER_CHUNKS = 255


def ordered_submeshes(part):
    """Submeshes in the order they become render chunks: by LOD then material."""
    return sorted(part.submeshes, key=lambda s: (s.lod, s.material))


def clean_frames(n, t):
    """Unit normals and unit tangents with a handedness of +1 or -1, as glTF wants.  GTA models carry
    zero-length normals and tangents (degenerate or unmapped triangles); WolvenKit refuses a mesh
    with one ("Operation is not valid due to the current state of the object"): the whole part, often
    the body, was then missing from the car."""
    n = np.nan_to_num(np.asarray(n, np.float64)[:, :3])
    ln = np.linalg.norm(n, axis=1)
    bad = ln < 1e-6
    n[bad] = (0.0, 0.0, 1.0)
    n[~bad] /= ln[~bad, None]
    t = np.nan_to_num(np.asarray(t, np.float64))
    t3, w = t[:, :3].copy(), t[:, 3] if t.shape[1] > 3 else np.ones(len(t))
    lt = np.linalg.norm(t3, axis=1)
    badt = lt < 1e-6
    if badt.any():
        nb = n[badt]
        ref = np.where(np.abs(nb[:, :1]) < 0.9, np.array([[1.0, 0.0, 0.0]]), np.array([[0.0, 1.0, 0.0]]))
        alt = np.cross(nb, ref)
        t3[badt] = alt / np.linalg.norm(alt, axis=1, keepdims=True)
        lt[badt] = 1.0
    t3 /= lt[:, None]
    w = np.where(w < 0, -1.0, 1.0)
    return n.astype(np.float32), np.concatenate([t3, w[:, None]], 1).astype(np.float32)


def write_glb(part, path, transform=None):
    """transform: optional 3x3 applied to positions/normals/tangents (row vectors: v @ transform)."""
    w = GlbWriter()
    for i, sm in enumerate(ordered_submeshes(part)):
        p, n, t = sm.positions, sm.normals, sm.tangents
        if transform is not None:
            q = np.asarray(transform, np.float64)
            p = (p.astype(np.float64) @ q).astype(np.float32)
            n = (n.astype(np.float64) @ q).astype(np.float32)
            t = np.concatenate([(t[:, :3].astype(np.float64) @ q), t[:, 3:4]], 1).astype(np.float32)
        n, t = clean_frames(n, t)
        colors = sm.colors
        if colors is None:
            colors = np.ones((len(p), 4), np.float32)
        w.add_submesh(submesh_node_name(i, sm.lod), p, n, t, sm.uv0, sm.uv1, colors, sm.indices, sm.material)
    w.save(path)


def template_doc(donor_mesh_doc, materials: dict, chunk_materials: list, shadow=False) -> dict:
    """materials: ordered {name: CMaterialInstance json}; chunk_materials: material name per chunk."""
    if len(chunk_materials) > MAX_RENDER_CHUNKS:
        raise ValueError(f"{len(chunk_materials)} render chunks in one mesh: the game takes at most "
                         f"{MAX_RENDER_CHUNKS} (more crashes it when the mesh streams in)")
    doc = copy.deepcopy(donor_mesh_doc)
    r = doc["Data"]["RootChunk"]
    names = list(materials)
    r["materialEntries"] = [{"$type": "CMeshMaterialEntry", "index": i, "isLocalInstance": 1, "name": cname(n)}
                            for i, n in enumerate(names)]
    mats = [materials[n] for n in names]
    lmb = r["localMaterialBuffer"]
    lmb["materials"] = mats
    raw = lmb.get("rawData") or {}
    raw.setdefault("BufferId", "0")
    raw.setdefault("Flags", 4063232)
    raw["Type"] = "WolvenKit.RED4.Archive.Buffer.CR2WList, WolvenKit.RED4, Version=8.17.4.0, Culture=neutral, PublicKeyToken=null"
    raw["Data"] = {"Files": [{"Version": 195, "BuildVersion": 0, "RootChunk": m, "EmbeddedFiles": []} for m in mats]}
    lmb["rawData"] = raw
    lmb["rawDataHeaders"] = []
    for key in ("localMaterialInstances", "preloadLocalMaterialInstances", "externalMaterials", "preloadExternalMaterials"):
        if key in r:
            r[key] = []
    app = copy.deepcopy(r["appearances"][0])
    app["Data"]["chunkMaterials"] = [cname(m) for m in chunk_materials]
    app["Data"]["name"] = cname("default")
    r["appearances"] = [app]
    r["parameters"] = []
    r["boneNames"] = []
    r["boneRigMatrices"] = []
    r["boneVertexEpsilons"] = []
    r["lodBoneMask"] = []
    r["floatTrackNames"] = []
    if "isShadowMesh" in r:
        r["isShadowMesh"] = 1 if shadow else 0
    return doc
