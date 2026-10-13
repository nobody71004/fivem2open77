"""What the converter sees in a FiveM resource, as JSON: every car with its parts, GTA bones and
their roles, lights, wheels, handling (GTA values and what the Cyberpunk drive model gets),
engine sound, textures and warnings.  The Studio and the AI agent both work from this."""
from __future__ import annotations

from collections import Counter

from . import profile as profile_mod
from .cp77 import sound as sound_mod
from .cp77.donor import DONORS, choose_donor
from .cp77.tweak import handling_profile
from .gta.resource import scan_resource
from .model.build import VehicleBuilder


def _r(x, n=3):
    if isinstance(x, dict):
        return {k: _r(y, n) for k, y in x.items()}
    if isinstance(x, (list, tuple)):
        return [_r(y, n) for y in x]
    if hasattr(x, "tolist"):
        return _r(x.tolist(), n)
    if isinstance(x, float):
        return round(x, n)
    return x


def build_vehicle(src, prof: dict, log=None):
    """GTA model -> IR with the profile's part, light and paint overrides (no donor files needed)."""
    from .pipeline import build_ir
    return build_ir(src, prof, log or (lambda *_: None))


def summarize(src, v, prof: dict) -> dict:
    donor = choose_donor(v, None if prof["donor"] == "auto" else prof["donor"])
    dprof = DONORS[donor]
    hp = handling_profile(v, dprof, prof["handling"])
    snd = sound_mod.resolve(prof["sound"]["engine"], v, dprof["sound"])
    h = v.handling
    info = src.info
    lid_pos = {}
    for b in ("hood_a", "trunk_a"):
        if b in v.bones:
            lid_pos[b] = "front" if v.bones[b]["t"][1] > 0 else "rear"
    # what the conversion will do with them (the donor's lids by end of the car)
    mode = prof["parts"]["lids"]
    ends = dprof.get("lid_ends", {})
    plan = {}
    if mode == "swap":
        plan = {b: {"hood_a": "trunk_a", "trunk_a": "hood_a"}[b] for b in lid_pos}
    elif mode == "auto" and ends and len(set(lid_pos.values())) == len(lid_pos):
        for b, end in lid_pos.items():
            target = next((d for d, e in ends.items() if e == end), b)
            if target != b:
                plan[b] = target
    return {
        "model": src.model,
        "name": v.name,
        "display_name": v.display_name,
        "make": v.make,
        "class": v.gta_class,
        "layout": v.gta_layout,
        "doors": v.door_count,
        "gta_sound": v.audio_name,
        "donor": {"id": donor, "label": dprof["label"], "record": dprof["record"], "doors": dprof["doors"]},
        "dimensions": {"bbox": _r(v.bbox), "length": round(v.bbox[1][1] - v.bbox[0][1], 3),
                       "width": round(v.bbox[1][0] - v.bbox[0][0], 3), "height": round(v.bbox[1][2] - v.bbox[0][2], 3)},
        "axles": {k: {"radius": round(a.radius, 3), "width": round(a.width, 3), "rim_radius": round(a.rim_radius, 3),
                      "positions": {s: _r(p) for s, p in a.positions.items()}} for k, a in v.axles.items()},
        "parts": {k: {"kind": p.kind, "verts": p.vertex_count(1), "slot": p.slot, "parent": p.parent,
                      "gta_bones": p.gta_bones,
                      "submeshes": len([s for s in p.submeshes if s.lod == 1])} for k, p in v.parts.items()},
        "lids": {"gta": lid_pos, "mode": mode, "plan": plan, "donor": ends},
        "bones": v.report.get("bones", []),
        "extras": v.report.get("extras", []),
        "extras_detail": v.report.get("extras_detail", []),
        "mods": v.report.get("mods", []),
        "lights": [{"name": l.name, "type": l.light_type, "zone": l.zone, "part": l.part, "position": _r(l.position),
                    "color": list(l.color)} for l in v.lights],
        "seats": {k: _r(p) for k, p in v.seats.items()},
        "paint": {"primary": profile_mod.to_hex(v.paint_colors[0]), "secondary": profile_mod.to_hex(v.paint_colors[1])},
        "handling": {
            "gta": ({k: _r(x) for k, x in h.values.items()} | {"nInitialDriveGears": h.gears,
                                                                "vecCentreOfMassOffset": _r(h.com_offset),
                                                                "vecInertiaMultiplier": _r(h.inertia_mult)})
            if h else None,
            "cyberpunk": {k: _r(x) for k, x in hp.items()},
        },
        "sound": {k: snd[k] for k in ("player", "traffic", "label", "kind", "source", "override")},
        "materials": {k: {"kind": m.kind, "shader": m.gta_shader, "diffuse": m.diffuse} for k, m in v.materials.items()},
        "textures": {"used": len(v.textures), "missing": v.report.get("textures_missing", []),
                     "unsupported": v.report.get("textures_unsupported", []),
                     "renamed": v.report.get("textures_renamed", {}),
                     "formats": sorted({t.format_name for t in v.textures.values()})},
        "looks": {"paint_overlays": v.report.get("overlays", []), "lamps_claimed": v.report.get("lamps_claimed", {}),
                  "lights_added": v.report.get("lights_added", []), "light_types": v.report.get("light_types", {}),
                  "kinds": dict(sorted(Counter(m.kind for m in v.materials.values()).items()))},
        "neon": _neon(v, prof),
        "seating": v.seat_fit.summary() if getattr(v, "seat_fit", None) is not None else None,
        "warnings": list(v.warnings),
    }


def _neon(v, prof):
    from .model.neon import GTA_NEON_COLORS
    kit = getattr(v, "neon", None)
    out = {"enabled": bool(prof.get("neon", {}).get("enabled", True)),
           "palette": [{"key": k, "label": label, "hex": "#%02x%02x%02x" % rgb} for k, label, rgb in GTA_NEON_COLORS],
           "kit": None, "gta_bones": sorted(getattr(v, "markers", {}) or {})}
    if kit is not None:
        out["kit"] = dict(kit.summary(), colors=[{"key": k, "label": label, "hex": "#%02x%02x%02x" % rgb}
                                                 for k, label, rgb in kit.colors],
                          warnings=list(kit.warnings))
    return out


def inspect_resource(path: str, prof: dict | None = None) -> dict:
    prof = profile_mod.normalize(prof or {}, donors=DONORS, sounds=sound_mod.CATALOGUE)
    res = scan_resource(path)
    try:
        cars = []
        for src in res.vehicles:
            if prof.get("model") and src.model.lower() != prof["model"].lower():
                continue
            v = build_vehicle(src, prof)
            cars.append(summarize(src, v, prof))
        return {"input": path, "manifests": len(res.manifests), "models": [s.model for s in res.vehicles],
                "warnings": list(res.warnings), "vehicles": cars}
    finally:
        res.cleanup()
