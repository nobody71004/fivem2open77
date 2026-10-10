"""GTA map shaders -> Cyberpunk material instances.

    opaque / spec / normal / detail / parallax / terrain   engine\\materials\\metal_base.remt
    decal, cutout, alpha render buckets                     metal_base.remt with AlphaThreshold
    emissive*                                               metal_base.remt with Emissive
    glass*                                                  base\\materials\\glass.mt (tinted)
    water*                                                  base\\materials\\glass.mt (blue tint)
    mirror*                                                 metal_base.remt, metallic and smooth

GTA has no roughness map: roughness comes from the specular falloff (glossiness exponent) and
specular intensity, metalness is 0.  Parameter names were read from the 2.31 templates.
"""
from __future__ import annotations

import hashlib
import math

from ..cp77.materials import BLACK, FLAT_NORMAL, WHITE, instance
from ..cp77.wkjson import cname, color, depot, vec4
from ..model.ir import Material

METAL_BASE = r"engine\materials\metal_base.remt"
GLASS = r"base\materials\glass.mt"

RB_OPAQUE, RB_ALPHA, RB_DECAL, RB_CUTOUT = 0, 1, 2, 3

DIFFUSE_PARAMS = ("DiffuseSampler", "TextureSampler_layer0", "TextureSampler", "DiffuseSampler2",
                  "TextureSampler_layer1", "DiffuseExtraSampler", "TextureSamp")
NORMAL_PARAMS = ("BumpSampler", "BumpSampler_layer0", "NormalSampler", "BumpSampler_layer1")
SPEC_PARAMS = ("SpecSampler",)


def classify(shader_name: str, render_bucket: int) -> tuple:
    """-> (kind, alpha) for a GTA shader."""
    s = shader_name.lower()
    if s.startswith("glass"):
        if "emissive" in s:
            return "emissive", "opaque"
        return "glass", "blend"
    if s.startswith("mirror"):
        return "mirror", "opaque"
    if s.startswith("water"):
        return "water", "blend"
    if "emissive" in s:
        return "emissive", "test" if ("alpha" in s or "decal" in s or "clip" in s) else "opaque"
    if "decal" in s or s.startswith("cutout") or s.startswith("trees") or s.startswith("grass") or "_alpha" in s:
        return "textured", "test"
    if render_bucket in (RB_ALPHA, RB_DECAL, RB_CUTOUT):
        return "textured", "test"
    return "textured", "opaque"


def roughness_from(shader) -> float:
    fall = shader.vec("specularFalloffMult") if shader else None
    inten = shader.vec("specularIntensityMult") if shader else None
    r = 0.65
    if fall is not None and fall[0] > 0:
        # GTA falloff is a Phong exponent: 8 (matte) .. 1024 (mirror-like)
        r = 1.0 - (math.log2(max(2.0, min(2048.0, fall[0]))) - 1.0) / 11.0
        r = 0.08 + 0.85 * r
    if inten is not None and inten[0] < 0.05:
        r = max(r, 0.85)
    return float(max(0.05, min(0.95, r)))


def map_material(shader, tex_lookup) -> Material:
    """shader: gta.drawable.Shader; tex_lookup(name) -> canonical texture key or None."""
    sname = shader.name if shader else "default"
    kind, alpha = classify(sname, shader.render_bucket if shader else 0)
    diffuse = normal = spec = None
    if shader is not None:
        for p in DIFFUSE_PARAMS:
            t = shader.tex(p)
            if t:
                diffuse = tex_lookup(t)
                if diffuse:
                    break
        for p in NORMAL_PARAMS:
            t = shader.tex(p)
            if t:
                normal = tex_lookup(t)
                if normal:
                    break
        t = shader.tex(*SPEC_PARAMS)
        spec = tex_lookup(t) if t else None
    m = Material(key="", kind=kind, diffuse=diffuse, normal=normal, spec=spec, alpha=alpha, gta_shader=sname)
    m.roughness = roughness_from(shader)
    m.metalness = 0.0
    dc = shader.vec("matDiffuseColor") if shader else None
    if dc:
        m.color = (float(dc[0]), float(dc[1]), float(dc[2]), 1.0)
    if kind == "emissive":
        em = shader.vec("emissiveMultiplier") if shader else None
        mult = float(em[0]) if em else 1.0
        # GTA's multiplier is linear (1 = plain lit texture, 4-16 = neon); Cyberpunk wants EV
        m.emissive_ev = float(max(1.0, min(10.0, 2.0 + math.log2(max(0.25, mult)) * 1.5)))
    if kind == "mirror":
        m.roughness, m.metalness = 0.04, 1.0
    if normal:
        bump = shader.vec("bumpiness") if shader else None
        m.paint_slot = 0
        m.normal_strength = float(max(0.2, min(2.0, bump[0]))) if bump else 1.0
    m.key = "m_" + hashlib.sha1(repr((kind, alpha, diffuse, normal, spec, round(m.roughness, 3), m.color,
                                      round(m.emissive_ev, 2), sname)).encode()).hexdigest()[:10]
    return m


def material_json(mat: Material, tex_path) -> dict:
    """tex_path(texture key, usage) -> depot path of the converted .xbm."""
    k = mat.kind
    if k in ("glass", "water"):
        tint = (205, 228, 238) if k == "water" else (229, 229, 229)
        vals = [
            {"$type": "Float", "Opacity": 0.55 if k == "water" else 0.25},
            {"$type": "Float", "OpacityBackFace": 0.25 if k == "water" else 0.15},
            {"$type": "Color", "TintColor": color(*tint)},
            {"$type": "Float", "IOR": 1.33 if k == "water" else 1.0},
        ]
        if mat.normal:
            vals += [{"$type": "rRef:ITexture", "Normal": depot(tex_path(mat.normal, "normal"))},
                     {"$type": "Float", "NormalStrength": 0.5}]
        return instance(GLASS, vals)
    r, g, b, _ = mat.color
    vals = [
        {"$type": "rRef:ITexture", "BaseColor": depot(tex_path(mat.diffuse, "color") if mat.diffuse else WHITE)},
        {"$type": "Vector4", "BaseColorScale": vec4(r, g, b, 1.0)},
        {"$type": "rRef:ITexture", "Metalness": depot(WHITE if mat.metalness > 0 else BLACK)},
        {"$type": "Float", "MetalnessScale": float(mat.metalness)},
        {"$type": "Float", "MetalnessBias": 0.0},
        {"$type": "rRef:ITexture", "Roughness": depot(WHITE)},
        {"$type": "Float", "RoughnessScale": float(mat.roughness)},
        {"$type": "Float", "RoughnessBias": 0.0},
        {"$type": "rRef:ITexture", "Normal": depot(tex_path(mat.normal, "normal") if mat.normal else FLAT_NORMAL)},
        {"$type": "Float", "NormalStrength": float(getattr(mat, "normal_strength", 1.0)) if mat.normal else 0.0},
    ]
    enable_mask = 0
    if mat.alpha in ("test", "blend"):
        vals.append({"$type": "Float", "AlphaThreshold": 0.5})
        enable_mask = 1
    if k == "emissive":
        vals += [
            {"$type": "rRef:ITexture", "Emissive": depot(tex_path(mat.diffuse, "color") if mat.diffuse else WHITE)},
            {"$type": "Float", "EmissiveEV": float(mat.emissive_ev or 2.0)},
            {"$type": "Color", "EmissiveColor": color(255, 255, 255)},
        ]
    return instance(METAL_BASE, vals, enable_mask)


__all__ = ["classify", "map_material", "material_json", "cname"]
