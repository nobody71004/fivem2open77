"""Paint detail overlays.

GTA multiplies a car's paint by the paint shader's DiffuseSampler texture -- the logos, black trims,
vents, panel shading and ambient occlusion modellers bake into it -- and lays the livery
(DiffuseSampler2, on the second UV set) over the result.  Cyberpunk's car paint is a multilayer
material with no colour texture, so those textures used to vanish: the Biome lost its whole
livery, the i8 its shading.

The paint stays the multilayer (the colour Open77 paint and CrystalCoat change) and each texture
becomes a decal mesh a millimetre above it, the way the game's own cars carry their stickers
(base\\materials\\vehicle_mesh_decal.mt).  For the detail texture D the decal is

    alpha = 1 - min(R, G, B),   colour = (D - min D) / alpha

which, blended over a paint colour P, gives  min(D)·P + (D - min D):  exactly GTA's D·P where the
texture is grey (shading, black trims, AO) and for white paint, and the colour it was drawn in for
coloured details.  A texture that is white everywhere needs no decal.  The livery is laid on as it
is (its own alpha) when it is not empty.
"""
from __future__ import annotations

import numpy as np

from ..imaging import bc3_texture, texture_rgba
from .ir import Material

OVERLAY_OFFSET = 0.001      # metres above the paint (the modding wiki's 0.5-2 mm for decal meshes)
LIVERY_OFFSET = 0.0015      # the livery sits above the detail decal
OVERLAY_LODS = (1, 2)       # far LODs go without (a decal there is invisible anyway)
MIN_COVER = 0.06            # a texture this close to white everywhere changes nothing
MAX_SIZE = 1024             # decal textures are kept at most this big (BC3)


def paint_detail_image(rgba: np.ndarray) -> np.ndarray:
    """uint8 RGBA of a GTA paint texture -> uint8 RGBA of the decal that reproduces paint x texture."""
    d = rgba[..., :3].astype(np.float32) / 255.0
    mn = d.min(-1)
    a = 1.0 - mn
    c = np.where(a[..., None] > 1.0 / 255.0, (d - mn[..., None]) / np.maximum(a[..., None], 1e-6), 0.0)
    out = np.empty(rgba.shape[:2] + (4,), np.uint8)
    out[..., :3] = np.clip(c * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(a * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out


def _overlay_texture(builder, tex_name: str, kind: str):
    """(texture name, coverage) of the decal texture made from tex_name, cached; None when the
    texture cannot be decoded or would change nothing."""
    cache = builder.__dict__.setdefault("_overlay_tex", {})
    key = (tex_name, kind)
    if key in cache:
        return cache[key]
    tex = builder.textures.get(tex_name)
    rgba = texture_rgba(tex, MAX_SIZE) if tex is not None else None
    result = None
    if rgba is not None:
        img = paint_detail_image(rgba) if kind == "detail" else np.ascontiguousarray(rgba, np.uint8)
        cover = float(img[..., 3].max()) / 255.0
        if cover >= (MIN_COVER if kind == "detail" else 0.02):
            name = f"{tex_name}#{'paint' if kind == 'detail' else 'livery'}"
            builder.textures[name] = bc3_texture(name, img)
            builder.used_textures.add(name)
            result = (name, round(float(img[..., 3].mean()) / 255.0, 3))
    cache[key] = result
    return result


def add_paint_overlays(builder) -> list:
    """Give every paint submesh (LODs OVERLAY_LODS) the decals of its textures.  Works on the
    builder's accumulators (GTA model space, before parts are assembled).  -> report rows."""
    from .build import _Accum   # noqa: PLC0415 (build imports this module)
    v = builder.vehicle
    report = {}
    for part, accs in list(builder.acc.items()):
        for (mkey, lod), acc in list(accs.items()):
            m = v.materials.get(mkey)
            if m is None or m.kind != "paint" or lod not in OVERLAY_LODS:
                continue
            for tex, kind, off, uv_from in ((m.diffuse, "detail", OVERLAY_OFFSET, "uv0"),
                                            (getattr(m, "livery", None), "livery", LIVERY_OFFSET, "uv1")):
                if not tex:
                    continue
                made = _overlay_texture(builder, tex, kind)
                if made is None:
                    continue
                tname, cover = made
                okey = f"{mkey}_{'dt' if kind == 'detail' else 'lv'}"
                if okey not in v.materials:
                    v.materials[okey] = Material(key=okey, kind="paint_detail", diffuse=tname, gta_shader=m.gta_shader,
                                                 alpha="blend", paint_slot=m.paint_slot)
                    report[okey] = {"paint": mkey, "texture": tex, "kind": kind, "coverage": cover}
                oacc = accs.setdefault((okey, lod), _Accum(okey, lod))
                for sub, tri in acc.chunks:
                    if uv_from == "uv1" and sub.get("uv1") is None:
                        continue
                    s2 = dict(sub)
                    nrm = np.asarray(sub["normal"], np.float64)
                    nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
                    s2["position"] = np.asarray(sub["position"], np.float64) + nrm * off
                    s2["uv0"] = sub[uv_from]
                    oacc.chunks.append((s2, tri))
                    oacc.count += len(s2["position"])
                if not oacc.chunks:
                    del accs[(okey, lod)]
    return [dict(material=k, **r) for k, r in sorted(report.items())]
