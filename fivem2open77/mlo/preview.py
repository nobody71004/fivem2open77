"""Previews of a converted map without WolvenKit: software renders (a plan seen from above with a
metre grid, and 3/4 views) and a textured glTF for the Studio's 3D view.

Everything is in the map's own frame: anchor-local metres, x east, y north, z up at yaw 0 (the
frame of layout.json and bounds_local).  The glTF has one mesh per part and one node per
placement, so a part placed eight times (an MLO interior) is stored once.

Big maps are thinned for the renders only: each part keeps its largest triangles up to a budget
(the small ones are invisible at preview size anyway); the glTF keeps every triangle.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

from ..imaging import mean_color, png_bytes, texture_rgba
from ..preview import _Gltf
from ..render import render

KIND_COLORS = {
    "mlo": (0.78, 0.72, 0.62),        # interiors
    "object": (0.58, 0.66, 0.52),     # big standalone objects
    "chunk": (0.50, 0.55, 0.47),      # small objects and terrain, baked per cell
    "door": (0.20, 0.55, 0.95),       # Door devices
    "glass": (0.55, 0.75, 0.95),
}
MATERIAL_FALLBACK = {"glass": (0.45, 0.6, 0.72), "water": (0.2, 0.4, 0.6), "mirror": (0.75, 0.78, 0.8),
                     "emissive": (1.0, 0.92, 0.7)}
# (azimuth, elevation) for render._basis, named by where the camera stands: azimuth 0 puts it on
# the north side (+y) looking south, 90 on the west side, 180 south, 270 east
VIEWS = {"plan": (0, 89.9), "iso": (35, 38), "iso_back": (215, 38),
         "north": (0, 8), "west": (90, 8), "south": (180, 8), "east": (270, 8)}


def _part_lod(part):
    """The submeshes a preview shows: the most detailed LOD present."""
    subs = [s for s in part.submeshes if len(s.indices)]
    if not subs:
        return []
    best = min(s.lod for s in subs)
    return [s for s in subs if s.lod == best]


def material_colors(model, textures=True, max_tex=32) -> dict:
    """material key -> mean rgb (texture average when there is one, else its colour or kind)."""
    out, cache = {}, {}
    for key, m in model.materials.items():
        base = tuple(float(c) for c in (m.color[:3] if m.color else (0.7, 0.7, 0.7)))
        if m.kind in MATERIAL_FALLBACK:
            base = MATERIAL_FALLBACK[m.kind]
        elif textures and m.diffuse and m.diffuse in model.textures:
            if m.diffuse not in cache:
                try:
                    rgba = texture_rgba(model.textures[m.diffuse], max_tex)
                except Exception:  # noqa: BLE001 - an odd texture only loses its colour
                    rgba = None
                cache[m.diffuse] = mean_color(_shrink(rgba, max_tex)) if rgba is not None else None
            if cache[m.diffuse] is not None:
                c = cache[m.diffuse]
                base = (base[0] * c[0], base[1] * c[1], base[2] * c[2])
        out[key] = base
    return out


def _shrink(rgba, max_size):
    """Box-filter an image down to at most max_size on its longest side (textures without small
    mips decode at full size)."""
    h, w = rgba.shape[:2]
    f = 1
    while max(h, w) // f > max_size:
        f *= 2
    if f == 1:
        return rgba
    h2, w2 = max(1, h // f), max(1, w // f)
    a = rgba[:h2 * f, :w2 * f].astype(np.float32)
    if h2 * f > h or w2 * f > w:          # smaller than one block: plain striding
        return np.ascontiguousarray(rgba[::f, ::f][:max_size, :max_size])
    a = a.reshape(h2, f, w2, f, a.shape[2]).mean(axis=(1, 3))
    return np.clip(a + 0.5, 0, 255).astype(np.uint8)


def _rot(yaw_deg):
    a = math.radians(yaw_deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], np.float32)


def map_triangles(model, mode="material", max_tris=500_000, colors=None):
    """Placed triangles of the whole map -> (tris (N,3,3) float32, cols (N,3) float32).
    mode: "material" (texture colours) or "parts" (one colour per part kind)."""
    if mode not in ("material", "parts"):
        raise ValueError("mode: material or parts")
    if mode == "material" and colors is None:
        colors = material_colors(model)
    count = {}
    for pl in model.placements:
        count[pl.part] = count.get(pl.part, 0) + 1
    local = {}
    total = 0
    for name, part in model.parts.items():
        if part.collision_only or name not in count:
            continue
        tris, cols = [], []
        for sm in _part_lod(part):
            p = np.asarray(sm.positions, np.float32)
            t = p[np.asarray(sm.indices, np.int64).reshape(-1, 3)]
            if mode == "parts":
                kind = "glass" if model.materials.get(sm.material) is not None and \
                    model.materials[sm.material].kind == "glass" else part.kind
                c = KIND_COLORS.get(kind, (0.6, 0.6, 0.6))
            else:
                c = colors.get(sm.material, (0.6, 0.6, 0.6))
            tris.append(t)
            cols.append(np.tile(np.asarray(c, np.float32)[None, :3], (len(t), 1)))
        if not tris:
            continue
        t = np.concatenate(tris)
        local[name] = (t, np.concatenate(cols))
        total += len(t) * count[name]
    if total > max_tris:
        keep = max_tris / total
        for name, (t, c) in list(local.items()):
            n = max(1, int(len(t) * keep))
            if n < len(t):
                area = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)
                idx = np.argpartition(-area, n - 1)[:n]
                local[name] = (t[idx], c[idx])
    out_t, out_c = [], []
    for pl in model.placements:
        if pl.part not in local:
            continue
        t, c = local[pl.part]
        r = _rot(pl.yaw)
        out_t.append((t.reshape(-1, 3) @ r.T + np.asarray(pl.position, np.float32)).reshape(-1, 3, 3))
        out_c.append(c)
    if not out_t:
        return np.zeros((0, 3, 3), np.float32), np.zeros((0, 3), np.float32)
    return np.concatenate(out_t).astype(np.float32), np.concatenate(out_c).astype(np.float32)


def visual_bounds(model):
    """Anchor-local bounds of what can be seen (the collision can reach further: ground cells)."""
    box = {}
    for name, part in model.parts.items():
        if part.collision_only:
            continue
        pts = [np.asarray(s.positions, np.float64) for s in _part_lod(part)]
        if pts:
            p = np.concatenate(pts)
            box[name] = (p.min(0), p.max(0))
    corners = []
    for pl in model.placements:
        if pl.part not in box:
            continue
        mn, mx = box[pl.part]
        r = _rot(pl.yaw).astype(np.float64)
        c = np.array([[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])])
        corners.append(c @ r.T + np.asarray(pl.position, np.float64))
    if not corners:
        return model.bounds()
    c = np.concatenate(corners)
    return tuple(c.min(0)), tuple(c.max(0))


def plan_frame(bounds, size, margin=0.05) -> dict:
    """How render_plan maps map-local metres to pixels: x_px = (x - cx) / mpp + W/2,
    y_px = H/2 - (y - cy) / mpp."""
    (x0, y0, _), (x1, y1, _) = bounds
    w, h = size
    mpp = max((x1 - x0) / (w * (1 - 2 * margin)), (y1 - y0) / (h * (1 - 2 * margin)), 1e-3)
    return {"cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "mpp": mpp, "width": w, "height": h}


def _grid(img, fr, step, color, width=1):
    h, w = img.shape[:2]
    x_min = fr["cx"] - w / 2 * fr["mpp"]
    x_max = fr["cx"] + w / 2 * fr["mpp"]
    y_min = fr["cy"] - h / 2 * fr["mpp"]
    y_max = fr["cy"] + h / 2 * fr["mpp"]
    col = np.asarray(color, np.float64)
    for gx in np.arange(math.floor(x_min / step) * step, x_max + step, step):
        px = int(round((gx - fr["cx"]) / fr["mpp"] + w / 2))
        if 0 <= px < w:
            sl = img[:, max(0, px - width // 2): px + (width + 1) // 2]
            sl[:] = (sl * 0.55 + col * 0.45).astype(img.dtype)
    for gy in np.arange(math.floor(y_min / step) * step, y_max + step, step):
        py = int(round(h / 2 - (gy - fr["cy"]) / fr["mpp"]))
        if 0 <= py < h:
            sl = img[max(0, py - width // 2): py + (width + 1) // 2, :]
            sl[:] = (sl * 0.55 + col * 0.45).astype(img.dtype)


def _cross(img, fr, x, y, color, r=7):
    h, w = img.shape[:2]
    px = int(round((x - fr["cx"]) / fr["mpp"] + w / 2))
    py = int(round(h / 2 - (y - fr["cy"]) / fr["mpp"]))
    for d in range(-r, r + 1):
        for (a, b) in ((px + d, py), (px, py + d)):
            for o in (-1, 0, 1):
                xa, yb = (a, b + o) if a != px or d == 0 else (a + o, b)
                if 0 <= xa < w and 0 <= yb < h:
                    img[yb, xa] = color


def render_plan(tris, cols, bounds, size=(1024, 1024), spawn=None, grid=(10.0, 50.0), tile=False):
    """The map from above, north up, framed on `bounds`, with a grid every 10 m (and 50 m), the
    anchor (white cross) and the FiveM spawn point (cyan cross).  tile=True: no grid or marks and a
    transparent background (RGBA), for drawing the map at a spot.  -> (uint8 image, frame)."""
    fr = plan_frame(bounds, size)
    w, h = size
    span = fr["mpp"]
    # the frame is the bounds, not the triangles: pass it as an orthographic window
    img = render(tris, cols, VIEWS["plan"], (w, h), frame=(fr["cx"], fr["cy"], span), alpha=tile)
    img = img.astype(np.uint8)
    if tile:
        return img, fr
    if grid:
        fine, coarse = grid
        if fine and (w * span) / fine < 120:
            _grid(img, fr, fine, (90, 100, 120))
        if coarse:
            _grid(img, fr, coarse, (150, 165, 190), 2)
    _cross(img, fr, 0.0, 0.0, (255, 255, 255))
    if spawn is not None:
        _cross(img, fr, float(spawn[0]), float(spawn[1]), (60, 230, 255), 5)
    return img, fr


def render_view(tris, cols, view="iso", size=(960, 600)):
    if view not in VIEWS:
        raise ValueError(f"view: one of {', '.join(VIEWS)}")
    return render(tris, cols, VIEWS[view], size)


# ----------------------------------------------------------------------------- glTF
def _to_gltf(p):
    out = np.empty_like(p, dtype=np.float32)
    out[:, 0] = p[:, 0]
    out[:, 1] = p[:, 2]
    out[:, 2] = -p[:, 1]
    return out


def _welded(sm):
    """(positions float32, uv float32 or None, indices) with duplicate vertices merged: GTA map
    meshes often repeat a vertex per triangle."""
    p = np.ascontiguousarray(sm.positions, np.float32)
    uv = np.ascontiguousarray(sm.uv0, np.float32) if sm.uv0 is not None and len(sm.uv0) == len(p) else None
    idx = np.asarray(sm.indices, np.int64)
    key = p.view(np.uint32) if uv is None else np.concatenate([p.view(np.uint32), uv.view(np.uint32)], 1)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    return p[first], (uv[first] if uv is not None else None), inverse[idx]


def _yaw_quat_gltf(yaw_deg):
    """Rotation about the map's z (up) axis = glTF's y axis, as a glTF quaternion (x, y, z, w)."""
    a = math.radians(yaw_deg) / 2
    return [0.0, math.sin(a), 0.0, math.cos(a)]


def export_map_glb(model, path, textures=True, max_tex=128, max_tex_count=160) -> dict:
    """The map as a .glb: meshes per part (most detailed LOD), a node per placement with its
    translation and yaw, extras {part, kind, source} on each node.  Returns a summary."""
    g = _Gltf()
    g.doc["asset"]["generator"] = "fivem2open77 map preview"
    mats, tex_cache = {}, {}
    used = set()
    for part in model.parts.values():
        for sm in _part_lod(part):
            used.add(sm.material)
    tex_budget = max_tex_count
    for key in sorted(used):
        m = model.materials.get(key)
        base = list(m.color[:3]) if m is not None and m.color else [0.7, 0.7, 0.7]
        mat = {"name": key, "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.8}, "doubleSided": True}
        kind = m.kind if m is not None else "textured"
        if kind in ("glass", "water"):
            base = list(MATERIAL_FALLBACK[kind])
            mat["alphaMode"] = "BLEND"
            mat["pbrMetallicRoughness"]["roughnessFactor"] = 0.05
            alpha = 0.35
        else:
            alpha = 1.0
            if m is not None and m.alpha == "test":
                mat["alphaMode"] = "MASK"
                mat["alphaCutoff"] = 0.4
            if kind == "emissive":
                mat["emissiveFactor"] = [0.8, 0.75, 0.6]
            if textures and m is not None and m.diffuse and m.diffuse in model.textures:
                if m.diffuse not in tex_cache and tex_budget > 0:
                    try:
                        rgba = texture_rgba(model.textures[m.diffuse], max_tex)
                    except Exception:  # noqa: BLE001
                        rgba = None
                    if rgba is not None:
                        rgba = _shrink(rgba, max_tex)
                    tex_cache[m.diffuse] = g.image(png_bytes(rgba)) if rgba is not None else None
                    tex_budget -= 1
                t = tex_cache.get(m.diffuse)
                if t is not None:
                    mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": t}
                    base = [1.0, 1.0, 1.0]
        mat["pbrMetallicRoughness"]["baseColorFactor"] = [float(base[0]), float(base[1]), float(base[2]), alpha]
        mats[key] = g.material(mat)
    fallback = g.material({"name": "_none", "pbrMetallicRoughness": {"baseColorFactor": [0.6, 0.6, 0.6, 1.0]}})

    mesh_of, tris = {}, 0
    for name, part in model.parts.items():
        if part.collision_only:
            continue
        prims = []
        for sm in _part_lod(part):
            p, uv, idx = _welded(sm)
            # no normals: the viewer shades flat (GLTFLoader does when a mesh has none), which
            # suits buildings and halves the file
            attrs = {"POSITION": g.accessor(_to_gltf(p), 5126, "VEC3", 34962, minmax=True)}
            if uv is not None:
                attrs["TEXCOORD_0"] = g.accessor(uv, 5126, "VEC2", 34962)
            if len(p) < 65536:
                ia = g.accessor(idx.astype(np.uint16), 5123, "SCALAR", 34963)
            else:
                ia = g.accessor(idx.astype(np.uint32), 5125, "SCALAR", 34963)
            prims.append({"attributes": attrs, "indices": ia, "material": mats.get(sm.material, fallback)})
            tris += len(idx) // 3
        if prims:
            g.doc["meshes"].append({"name": name, "primitives": prims})
            mesh_of[name] = len(g.doc["meshes"]) - 1
    roots = g.doc["scenes"][0]["nodes"]
    placed = 0
    for i, pl in enumerate(model.placements):
        if pl.part not in mesh_of:
            continue
        part = model.parts[pl.part]
        x, y, z = (float(v) for v in pl.position)
        g.doc["nodes"].append({"name": f"{pl.part}#{i}", "mesh": mesh_of[pl.part],
                               "translation": [x, z, -y], "rotation": _yaw_quat_gltf(pl.yaw),
                               "extras": {"part": pl.part, "kind": part.kind, "source": pl.source,
                                          "position": [round(x, 3), round(y, 3), round(z, 3)], "yaw": round(float(pl.yaw), 2)}})
        roots.append(len(g.doc["nodes"]) - 1)
        placed += 1
    g.save(path)
    return {"parts": len(mesh_of), "placements": placed, "triangles": int(tris), "textures": sum(1 for v in tex_cache.values() if v is not None),
            "bytes": os.path.getsize(path)}


def summary(scene, model, coords, res_name) -> dict:
    """What the Studio shows about a map before any build (the first look)."""
    rep = model.report or {}
    mn, mx = model.bounds()
    vmn, vmx = visual_bounds(model)
    count = {}
    for pl in model.placements:
        count[pl.part] = count.get(pl.part, 0) + 1
    parts = []
    for name, p in model.parts.items():
        parts.append({"name": name, "kind": p.kind, "vertices": int(p.vertex_count), "triangles": int(p.triangle_count),
                      "colliders": len(p.colliders), "collision_only": bool(p.collision_only),
                      "instances": count.get(name, 0), "sources": dict(p.sources.most_common(5))})
    parts.sort(key=lambda x: (-x["triangles"] * max(1, x["instances"]), x["name"]))
    sc = scene.summary()
    sets = []
    for m in sc.get("mlo_instances", []):
        for es in m.get("entity_sets") or []:
            nm = es.get("name") if isinstance(es, dict) else es
            if nm and nm not in sets:
                sets.append(nm)
    missing = sc.get("missing_archetypes") or {}
    return {
        "map": model.name, "display_name": model.display_name, "resource": res_name,
        "coords": list(coords) if coords else None, "anchor_gta": [round(float(v), 3) for v in model.anchor],
        "spawn_local": [round(float(v), 3) for v in model.spawn] if model.spawn else None,
        "bounds_local": [[round(float(v), 2) for v in mn], [round(float(v), 2) for v in mx]],
        "visual_bounds": [[round(float(v), 2) for v in vmn], [round(float(v), 2) for v in vmx]],
        "size": [round(float(vmx[i] - vmn[i]), 1) for i in range(3)],
        "parts": parts, "placements": len(model.placements), "materials": len(model.materials),
        "textures": len(model.textures), "triangles_placed": int(sum(p["triangles"] * p["instances"] for p in parts)),
        "colliders": int(sum(p["colliders"] * max(1, p["instances"]) for p in parts)),
        "doors": rep.get("doors") or [], "glass": rep.get("glass") or {},
        "doorways": rep.get("doorways") or [], "door_candidates": rep.get("door_candidates") or [],
        "door_map": rep.get("door_map") or {"objects": {}, "doorways": {}},
        "door_objects": door_objects(scene, model),
        "kinds": {k: sum(1 for p in parts if p["kind"] == k) for k in sorted({p["kind"] for p in parts})},
        "scene": {"archetypes": sc.get("archetypes"), "mlo_instances": [
            {k: m.get(k) for k in ("name", "source", "position", "children", "rooms", "portals", "entity_sets")}
            for m in sc.get("mlo_instances", [])],
            "world_entities": sc.get("world_entities"), "skipped_lod_entities": sc.get("skipped_lod_entities"),
            "missing_total": sc.get("missing_total"), "missing_archetypes": dict(list(missing.items())[:40]),
            "ymaps": sc.get("ymaps"), "world_collisions": sc.get("world_collisions"),
            "skipped_files": sc.get("skipped_files"), "warnings": sc.get("warnings"),
            "area": sc.get("area"), "out_of_area": sc.get("out_of_area"), "districts": sc.get("districts")},
        "textures_missing": dict(list((rep.get("textures_missing") or {}).items())[:60]),
        "untextured_cutouts_dropped": rep.get("untextured_cutouts_dropped") or {},
        "entity_sets": sets,
        "warnings": list(model.warnings) + [w for w in (sc.get("warnings") or []) if w not in model.warnings],
    }


def door_objects(scene, model) -> list:
    """Every object of the map the Doors tab can map: [{name, count, kind}] -- door devices,
    candidates and the rest of the MLO children and big objects (name order)."""
    count = {}
    for m in getattr(scene, "mlos", []) or []:
        for ch in m.children:
            count[ch.name] = count.get(ch.name, 0) + 1
    for inst in getattr(scene, "instances", []) or []:
        count[inst.name] = count.get(inst.name, 0) + 1
    doors = {}
    for p in model.parts.values():
        if p.kind == "door":
            for n in p.sources:
                doors[n.lower()] = p.door.get("opening") if p.door else None
    return [{"name": n, "count": c, "door": doors.get(n.lower())} for n, c in sorted(count.items())][:3000]


def write_json(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    os.replace(tmp, path)
