"""IR materials -> Cyberpunk CMaterialInstance JSON."""
from __future__ import annotations

from .wkjson import cname, color, depot, vec4

WHITE = r"engine\textures\editor\white.xbm"
GREY = r"engine\textures\editor\grey.xbm"
BLACK = r"engine\textures\editor\black.xbm"
FLAT_NORMAL = r"engine\textures\small_flat_normal.xbm"

METAL_BASE = r"engine\materials\metal_base.remt"
MULTILAYER_VEHICLE = r"base\vehicles\common\materials\multilayer_vehicle_destruction.mi"
VEHICLE_LIGHTS = r"base\vehicles\common\materials\vehicle_lights.mi"
GLASS_WINDSHIELD = r"base\vehicles\common\materials\glass_windshield.mi"
GLASS_LIGHTCOVERS = r"base\vehicles\common\materials\glass_lightcovers.mi"
VEHICLE_MESH_DECAL = r"base\materials\vehicle_mesh_decal.mt"
SMALL_WHITE = r"engine\textures\small_white.xbm"


def _tex(key, path):
    return {"$type": "rRef:ITexture", key: depot(path)}


def _f(key, v):
    return {"$type": "Float", key: float(v)}


def _v4(key, x, y, z, w=1.0):
    return {"$type": "Vector4", key: vec4(x, y, z, w)}


def _col(key, r, g, b, a=255):
    return {"$type": "Color", key: color(r, g, b, a)}


def _rgb255(r, g, b):
    return tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in (r, g, b))


def instance(base: str, values: list, enable_mask: int = 0) -> dict:
    return {"$type": "CMaterialInstance", "audioTag": cname("None"), "baseMaterial": depot(base),
            "cookingPlatform": "PLATFORM_None", "enableMask": int(enable_mask), "metadata": None,
            "resourceVersion": 4, "values": values}


def neon_material(rgb, ev=6.0) -> dict:
    """A glowing LED tube: metal_base lit by its own emissive colour (always on)."""
    r, g, b = (max(0.0, min(1.0, c / 255.0)) for c in rgb)
    return instance(METAL_BASE, [
        _tex("BaseColor", WHITE), _v4("BaseColorScale", r, g, b, 1.0),
        _tex("Metalness", WHITE), _f("MetalnessScale", 0.0), _f("MetalnessBias", 0),
        _tex("Roughness", WHITE), _f("RoughnessScale", 0.3), _f("RoughnessBias", 0),
        _tex("Normal", FLAT_NORMAL), _f("NormalStrength", 0.0),
        _tex("Emissive", WHITE), _f("EmissiveEV", float(ev)),
        _col("EmissiveColor", int(round(r * 255)), int(round(g * 255)), int(round(b * 255))),
    ])


def material_json(mat, tex_path, paint_setup: str, paint_mask: str) -> dict:
    """mat: model.ir.Material; tex_path(name, usage) -> depot path of the converted xbm."""
    k = mat.kind
    if k == "paint":
        return instance(MULTILAYER_VEHICLE, [
            {"$type": "rRef:Multilayer_Mask", "MultilayerMask": depot(paint_mask)},
            {"$type": "rRef:Multilayer_Setup", "MultilayerSetup": depot(paint_setup)},
            _tex("CustomColorMask", GREY if mat.paint_slot != 2 else WHITE),
            _tex("BakedNormal", FLAT_NORMAL),
            _tex("ScratchMask", r"base\vehicles\common\textures\vehicles_destruction_01_mask.xbm"),
            _tex("DirtMap", r"base\surfaces\microblends\dust_fine.xbm"),
            _tex("GlobalNormal", r"base\vehicles\common\textures\vehicles_destruction_01_n.xbm"),
            _f("DamageInfluence", 0.5),
            _f("TextureTiling", 0.5),
            _f("ScratchResistance", 0),
            _f("DirtOpacity", 0),
            _col("CoatTintSide", 128, 128, 128),
            _col("CoatTintFwd", 223, 223, 223),
            _col("CoatSpecularColor", 255, 255, 255),
            _f("CoatTintFresnelBias", 2),
            _f("CoatFresnelBias", 0.66),
            _f("CoatLayerMin", -1),
            _f("CoatLayerMax", 1),
            _f("Opacity", 1),
        ])
    if k == "lights":
        vals = [
            _v4("BaseColorScale", 1, 1, 1, 1),
            _f("DamageInfluence", 0.5),
            _f("AlphaThreshold", 0),
            _f("Zone0EmissiveEV", 9 if mat.diffuse else 10), _f("Zone1EmissiveEV", 9 if mat.diffuse else 10),
            _f("Zone2EmissiveEV", 9 if mat.diffuse else 10), _f("Zone3EmissiveEV", 9 if mat.diffuse else 10),
        ]
        if mat.diffuse:
            # the GTA lamp texture is both the unlit look and the pattern that lights up (GTA lights
            # a lamp by brightening its own texture); mapped 1:1, no tiling or parallax
            t = tex_path(mat.diffuse, "color")
            vals += [_tex("BaseColor", t), _tex("Emissive", t), _f("EmissionTiling", 1), _f("EmissionParallax", 0),
                     _f("UvTilingX", 1), _f("UvTilingY", 1), _f("UvOffsetX", 0), _f("UvOffsetY", 0),
                     _tex("Normal", tex_path(mat.normal, "normal") if mat.normal else FLAT_NORMAL)]
        return instance(VEHICLE_LIGHTS, vals)
    if k == "lightcover":
        r, g, b = mat.tint or (1.0, 1.0, 1.0)
        return instance(GLASS_LIGHTCOVERS, [_col("TintColor", *_rgb255(r, g, b))])
    if k == "glass":
        # GTA's glass is clean: keep only a trace of the windshield's dirt mask
        if mat.tint is None:
            return instance(GLASS_WINDSHIELD, [_f("MaskOpacity", 0.1)])
        r, g, b = mat.tint
        return instance(GLASS_WINDSHIELD, [_col("TintColor", *_rgb255(r, g, b)), _f("MaskOpacity", 0.1)])
    if k in ("glass_opaque", "glass_cutout"):
        # the opaque parts of a GTA glass texture (frit, pillars, blacked-out bands): solid and glossy
        vals = [
            _tex("BaseColor", tex_path(mat.diffuse, "color") if mat.diffuse else BLACK), _v4("BaseColorScale", 1, 1, 1, 1),
            _tex("Metalness", WHITE), _f("MetalnessScale", 0.0), _f("MetalnessBias", 0),
            _tex("Roughness", WHITE), _f("RoughnessScale", mat.roughness), _f("RoughnessBias", 0),
            _tex("Normal", FLAT_NORMAL), _f("NormalStrength", 0.0), _f("VehicleDamageInfluence", 0.5),
        ]
        if k == "glass_cutout":
            vals.append(_f("AlphaThreshold", 0.95))
            return instance(METAL_BASE, vals, 1)
        return instance(METAL_BASE, vals)
    if k == "paint_detail":
        # a decal over the paint (the way the game's cars wear their stickers): colour and coverage
        # from the GTA texture, the paint's own gloss left alone
        return instance(VEHICLE_MESH_DECAL, [
            _tex("DiffuseTexture", tex_path(mat.diffuse, "color")), _f("DiffuseAlpha", 1),
            _col("DiffuseColor", 255, 255, 255),
            _tex("NormalTexture", FLAT_NORMAL), _f("NormalAlpha", 0), _f("NormalsBlendingMode", 1),
            _tex("RoughnessTexture", WHITE), _tex("MetalnessTexture", BLACK), _f("RoughnessMetalnessAlpha", 0),
            _tex("MaskTexture", SMALL_WHITE), _tex("GradientMap", SMALL_WHITE), _f("UseGradientMap", 0),
            _f("DepthThreshold", 0.2), _f("DamageInfluence", 0.5), _f("DirtOpacity", 0),
        ], 1)
    # textured family (metal_base)
    r, g, b, a = mat.color
    vals = [
        _tex("BaseColor", tex_path(mat.diffuse, "color") if mat.diffuse else WHITE),
        _v4("BaseColorScale", r, g, b, 1.0 if mat.diffuse else 1.0),
        _tex("Metalness", WHITE), _f("MetalnessScale", mat.metalness), _f("MetalnessBias", 0),
        _tex("Roughness", WHITE), _f("RoughnessScale", mat.roughness), _f("RoughnessBias", 0),
        _tex("Normal", tex_path(mat.normal, "normal") if mat.normal else FLAT_NORMAL),
        _f("NormalStrength", 1.0 if mat.normal else 0.0),
        _f("VehicleDamageInfluence", 0.5),
    ]
    enable_mask = 0
    if mat.alpha in ("test", "blend") or k == "decal":
        vals.append(_f("AlphaThreshold", 0.5))
        enable_mask = 1
    if k == "emissive":
        vals += [_tex("Emissive", tex_path(mat.diffuse, "color") if mat.diffuse else WHITE), _f("EmissiveEV", mat.emissive_ev or 2.0),
                 _col("EmissiveColor", 255, 255, 255)]
    return instance(METAL_BASE, vals, enable_mask)
