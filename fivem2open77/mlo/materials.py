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


# what a texture GTA ships itself (and a map only names) most likely shows, by its name: the
# surface keeps that colour instead of turning flat white.  First match wins.
_GUESSES = [
    (r"glass|window|wndw|mirror", (0.30, 0.36, 0.42)),
    (r"pool_?tile", (0.42, 0.66, 0.76)),
    (r"water|pool|pond|sea", (0.18, 0.32, 0.40)),
    (r"terr?acott?a", (0.64, 0.36, 0.24)),
    (r"roof", (0.36, 0.30, 0.28)),
    (r"step|stair|ledge|kerb|curb", (0.60, 0.59, 0.56)),
    (r"hedge|bush|shrub|leaf|leaves|foliage|ivy|tree|fern|palm|plant|veg|fur_grass", (0.22, 0.31, 0.14)),
    (r"grass|lawn|turf|meadow|wetland", (0.31, 0.40, 0.19)),
    (r"woodchip|bark|mulch", (0.40, 0.29, 0.19)),
    (r"mud|dirt|soil|earth|ground", (0.40, 0.33, 0.24)),
    (r"sand|beach|dune", (0.74, 0.66, 0.49)),
    (r"gravel|pebble", (0.50, 0.47, 0.42)),
    (r"rock|stone|cliff|boulder|slate", (0.48, 0.46, 0.43)),
    (r"road|tarmac|asphalt|street|lane|parking|blacktop", (0.19, 0.19, 0.20)),
    (r"pavement|sidewalk|paving|pave|concrete|cement|drain", (0.56, 0.55, 0.53)),
    (r"marble", (0.85, 0.83, 0.79)),
    (r"granite|terrazzo", (0.54, 0.52, 0.51)),
    (r"tile|tiling|ceramic", (0.78, 0.78, 0.76)),
    (r"brick", (0.54, 0.30, 0.22)),
    (r"wood|plank|parquet|timber|oak|pine|walnut|deck|floorboard|worktop", (0.50, 0.35, 0.22)),
    (r"carpet|rug|fabric|cloth|curtain|sofa|couch|cushion", (0.45, 0.40, 0.38)),
    (r"leather", (0.28, 0.19, 0.14)),
    (r"rust", (0.44, 0.25, 0.15)),
    (r"chrome|steel|metal|alu|iron|silver|tin|grate|grill", (0.58, 0.59, 0.61)),
    (r"gold|brass", (0.72, 0.58, 0.30)),
    (r"shingle", (0.32, 0.30, 0.30)),
    (r"plaster|stucco|wall|ceiling|paint", (0.80, 0.78, 0.74)),
    (r"black|dark|shadow", (0.06, 0.06, 0.06)),
    (r"white", (0.90, 0.90, 0.90)),
]


def guess_color(*names) -> tuple | None:
    """(r, g, b) sRGB 0..1 for a missing GTA texture, from its (and its shader's) name; None when
    nothing in the names says what it is."""
    import re
    text = " ".join(str(n).lower() for n in names if n)
    for rx, rgb in _GUESSES:
        if re.search(rx, text):
            return rgb
    return None


def map_material(shader, tex_lookup) -> Material:
    """shader: gta.drawable.Shader; tex_lookup(name) -> canonical texture key or None."""
    sname = shader.name if shader else "default"
    kind, alpha = classify(sname, shader.render_bucket if shader else 0)
    diffuse = normal = spec = None
    wanted = None
    if shader is not None:
        for p in DIFFUSE_PARAMS:
            t = shader.tex(p)
            if t:
                wanted = wanted or t
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
    if dc and all(0.0 <= float(c) <= 4.0 for c in dc[:3]):
        m.color = (float(dc[0]), float(dc[1]), float(dc[2]), 1.0)
    m.missing_diffuse = wanted if (wanted and not diffuse) else None
    if m.missing_diffuse:
        # a texture of GTA's own the map only names: keep the surface in the colour it most likely
        # has (mid grey when its name says nothing) rather than flat white
        g = guess_color(wanted, sname) or (0.55, 0.55, 0.55)
        m.color = (m.color[0] * g[0], m.color[1] * g[1], m.color[2] * g[2], 1.0)
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
    m.key = "m_" + hashlib.sha1(repr((kind, alpha, diffuse, normal, spec, round(m.roughness, 3), tuple(round(c, 4) for c in m.color),
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
