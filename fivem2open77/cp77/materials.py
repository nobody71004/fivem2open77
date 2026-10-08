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


def _tex(key, path):
    return {"$type": "rRef:ITexture", key: depot(path)}


def _f(key, v):
    return {"$type": "Float", key: float(v)}


def _v4(key, x, y, z, w=1.0):
    return {"$type": "Vector4", key: vec4(x, y, z, w)}


def _col(key, r, g, b, a=255):
    return {"$type": "Color", key: color(r, g, b, a)}


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
        return instance(VEHICLE_LIGHTS, [
            _v4("BaseColorScale", 1, 1, 1, 1),
            _f("DamageInfluence", 0.5),
            _f("Zone0EmissiveEV", 10), _f("Zone1EmissiveEV", 10),
            _f("Zone2EmissiveEV", 10), _f("Zone3EmissiveEV", 10),
        ])
    if k == "glass":
        return instance(GLASS_WINDSHIELD, [])
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
