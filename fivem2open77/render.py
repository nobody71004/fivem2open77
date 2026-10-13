"""Software renders of a converted car (numpy only): orthographic views for thumbnails, QA and
the AI agent's visual checks.

Triangles are point-sampled in proportion to their screen area and depth-sorted per pixel, which
needs no rasteriser loop and is fast enough for half a million triangles.  Colour modes:
  "material": each triangle in its texture's colour at its centre (paint times its GTA texture,
              lamps in their texture, glass in its tint);
  "parts":    one colour per part kind (doors blue, hood orange, trunk red, ...), to check mapping.
"""
from __future__ import annotations

import math

import numpy as np

from .imaging import png_bytes, texture_rgba
from .preview import CORNERS, KIND_COLORS, _Gltf, build_materials

DENSITY = 6.0      # samples per pixel of triangle area (holes ~ e^-DENSITY)

VIEWS = {
    # name: (azimuth, elevation) in degrees; azimuth 0 = seen from the front, 90 = from the left
    "front_3q": (35, 18), "rear_3q": (145, 18), "left": (90, 2), "right": (270, 2),
    "front": (0, 4), "rear": (180, 4), "top": (0, 89.9),
    "bottom": (0, -89.9), "under_3q": (35, -12), "under_rear_3q": (215, -12),
}


def neon_triangles(v, rgb):
    """The neon tubes alone, in one colour (rgb 0..255): a colour preview without a rebuild."""
    part = v.parts.get("neon_01")
    if part is None:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    f = v.part_frames.get("neon_01")
    r, t = (np.asarray(f[1], np.float64), np.asarray(f[0], np.float64)) if f else (np.eye(3), np.zeros(3))
    tris = []
    for sm in part.submeshes:
        if sm.lod == 1 and len(sm.indices):
            p = sm.positions.astype(np.float64) @ r.T + t
            tris.append(p[sm.indices.reshape(-1, 3)])
    if not tris:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    tris = np.concatenate(tris)
    c = np.asarray(rgb, np.float64)[:3] / 255.0
    return tris, np.tile(c[None, :], (len(tris), 1))


def light_markers(v, rgb, size=0.035):
    """Where a light-only kit's light comes from (its tubes are not drawn in game): a small diamond
    on each tube line at each light's place along it, in one colour (rgb 0..255)."""
    kit = getattr(v, "neon", None)
    if kit is None or not kit.lights:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    r = float(size)
    xp, yp, xn, yn = np.array([r, 0, 0]), np.array([0, r, 0]), np.array([-r, 0, 0]), np.array([0, -r, 0])
    zp, zn = np.array([0, 0, r]), np.array([0, 0, -r])
    faces = [(xp, yp, zp), (yp, xn, zp), (xn, yn, zp), (yn, xp, zp),          # outward, counter-clockwise
             (yp, xp, zn), (xn, yp, zn), (yn, xn, zn), (xp, yn, zn)]
    tris = []
    for l in kit.lights:
        t = next((t for t in kit.tubes if t.side == l.side), None)
        if t is None:
            continue
        d = t.end - t.start
        f = float(np.clip(((l.position - t.start)[:2] @ d[:2]) / max(float(d[:2] @ d[:2]), 1e-9), 0.0, 1.0))
        c = t.start + d * f
        tris += [np.stack([c + a, c + b, c + e]) for a, b, e in faces]
    if not tris:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    col = np.asarray(rgb, np.float64)[:3] / 255.0
    return np.asarray(tris, np.float64), np.tile(col[None, :], (len(tris), 1))


ROAD = 0.035        # asphalt at night
GLOW_REF = 6.0      # ground_glow value drawn at 63 % of the colour: the donor's headlights ~2 m ahead


def glow_triangles(v, rgb, cell=0.05, margin=2.2):
    """The road around the car as coloured triangles, lit at night by its neon kit in `rgb`
    (0..255) with the light model of model/neon.ground_glow, tone-mapped.  An approximation of
    the game: its exposure, the surface and the weather decide the final look."""
    from .model.neon import ground_glow, road_height
    kit = getattr(v, "neon", None)
    if kit is None or not kit.lights:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    (x0, y0, _), (x1, y1, _) = v.bbox
    xs = np.arange(x0 - margin, x1 + margin + 1e-9, cell)
    ys = np.arange(y0 - margin, y1 + margin + 1e-9, cell)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    P = np.stack([X, Y, road_height(kit, Y) - 0.003], -1)
    xc, yc = (X[:-1, :-1] + X[1:, 1:]) / 2, (Y[:-1, :-1] + Y[1:, 1:]) / 2
    e = ground_glow(kit, xc, yc, rgb)
    tone = 1.0 - np.exp(-e / GLOW_REF)
    col = np.clip(ROAD + tone[..., None] * (np.asarray(rgb, np.float64)[:3] / 255.0), 0, 1).reshape(-1, 3)
    a, b, c, d = P[:-1, :-1], P[1:, :-1], P[1:, 1:], P[:-1, 1:]      # counter-clockwise seen from above
    tris = np.concatenate([np.stack([a, b, c], -2).reshape(-1, 3, 3), np.stack([a, c, d], -2).reshape(-1, 3, 3)])
    return tris, np.concatenate([col, col])


def glow_views(v, rgb, size=(480, 300), views=((0, 89.9), (35, 28)), body=None):
    """The car at night over its lit road: [image per view].  body: (tris, cols) of the car without
    its tubes (from _triangles(..., skip_kinds=("neon",))), to reuse a cached set."""
    body = body if body is not None else _triangles(v, "material", skip_kinds=("neon",))
    tube = neon_triangles(v, rgb)
    road = glow_triangles(v, rgb)
    tris = np.concatenate([body[0], tube[0], road[0]])
    cols = np.concatenate([body[1] * 0.35, tube[1], road[1]])
    unlit = np.concatenate([np.zeros(len(body[0]), bool), np.ones(len(tube[0]) + len(road[0]), bool)])
    return [render(tris, cols, vw, size, light=(0.0, 0.0, 1.0), unlit=unlit) for vw in views]


# material kinds whose triangles take their colour from their texture (sampled per triangle)
_TEXTURED = ("textured", "tire", "emissive", "decal", "lights", "glass_opaque", "glass_cutout", "plate", "paint")
_CUTOFF = {"decal": 0.4, "glass_cutout": 0.95}


def _triangles(v, color_mode="material", skip_kinds=()):
    """All LOD-1 triangles in Root space -> (tri (N,3,3), colour (N,3), kind list).  In material
    mode each triangle takes its texture's colour at its centre (paint times its GTA texture, lamps
    in their own texture), so renders show logos, lamps and trims, not just flat averages."""
    if color_mode == "material":
        _, means = build_materials(v, _Gltf(), textures=True, max_tex=32)
    images = {}

    def image(name):
        if name not in images:
            tex = v.textures.get(name)
            rgba = texture_rgba(tex, 128) if tex is not None else None
            images[name] = rgba.astype(np.float64) / 255.0 if rgba is not None else None
        return images[name]

    tris, cols = [], []
    paint = [np.asarray(v.paint_colors[0], np.float64), np.asarray(v.paint_colors[1], np.float64)]

    def add(part, r, t, kind):
        for sm in part.submeshes:
            if sm.lod != 1 or not len(sm.indices):
                continue
            m = v.materials.get(sm.material)
            if color_mode == "material" and m is not None and m.kind == "paint_detail":
                continue                     # the paint triangles below carry that texture
            p = sm.positions.astype(np.float64) @ r.T + t
            idx = sm.indices.reshape(-1, 3)
            tri = p[idx]
            if color_mode == "parts":
                c = np.tile(np.asarray(KIND_COLORS.get(kind, (0.6, 0.6, 0.6)), np.float64)[None, :3], (len(tri), 1))
            else:
                c = np.tile(np.asarray(means.get(sm.material, (0.6, 0.6, 0.6)), np.float64)[None, :3], (len(tri), 1))
                img = image(m.diffuse) if m is not None and m.diffuse and m.kind in _TEXTURED else None
                if img is not None and sm.uv0 is not None:
                    uv = sm.uv0.astype(np.float64)[idx].mean(1)
                    h, w = img.shape[:2]
                    x = np.clip((np.mod(uv[:, 0], 1.0) * w).astype(np.int64), 0, w - 1)
                    y = np.clip((np.mod(uv[:, 1], 1.0) * h).astype(np.int64), 0, h - 1)
                    px = img[y, x]
                    if m.kind == "paint":
                        c = px[:, :3] * paint[1 if m.paint_slot == 2 else 0][None, :3]
                    else:
                        c = px[:, :3] * np.asarray(m.color[:3], np.float64)[None, :]
                    cut = _CUTOFF.get(m.kind)
                    if cut is not None:
                        keep = px[:, 3] >= cut
                        tri, c = tri[keep], c[keep]
            tris.append(tri)
            cols.append(c)

    for name, part in v.parts.items():
        if part.kind in ("wheel", "tire") or part.kind in skip_kinds:
            continue
        f = v.part_frames.get(name)
        r, t = (np.asarray(f[1], np.float64), np.asarray(f[0], np.float64)) if f else (np.eye(3), np.zeros(3))
        add(part, r, t, part.kind)
    for corner, axle in CORNERS.items():
        ax = v.axles.get(axle)
        if ax is None or corner not in ax.positions:
            continue
        r = np.eye(3) if corner in ("fl", "bl") else np.diag([-1.0, -1.0, 1.0])
        for kind in ("wheel", "tire"):
            part = v.parts.get(f"{kind}_{axle}")
            if part is not None:
                add(part, r, np.asarray(ax.positions[corner], np.float64), kind)
    if not tris:
        return np.zeros((0, 3, 3)), np.zeros((0, 3))
    return np.concatenate(tris), np.concatenate(cols)


def _basis(az, el):
    a, e = math.radians(az), math.radians(el)
    c = np.array([-math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)])   # target -> camera
    f = -c
    up = np.array([0.0, 0.0, 1.0]) if abs(el) < 89 else np.array([0.0, 1.0, 0.0])
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    return r, u, c


def render(tris, cols, view="front_3q", size=(640, 400), seed=1, light=None, frame=None, alpha=False,
           unlit=None) -> np.ndarray:
    """-> uint8 (H, W, 3), or (H, W, 4) with alpha=True (transparent where nothing was drawn).
    frame=(cx, cy, metres per pixel) in screen axes fixes the window instead of fitting the
    triangles (map plans framed on their bounds).  unlit: per-triangle flags for triangles drawn in
    their own colour (lights, a lit road), not shaded."""
    W, H = size
    az, el = VIEWS[view] if isinstance(view, str) else view
    r, u, c = _basis(az, el)
    img = np.zeros((H, W, 3), np.float64)
    # background: the Studio's viewport colour (flat graphite)
    img[:] = np.array([0.071, 0.075, 0.086])
    if not len(tris):
        out = (img * 255).astype(np.uint8)
        return np.concatenate([out, np.zeros((H, W, 1), np.uint8)], 2) if alpha else out
    tris = np.asarray(tris, np.float64)
    cols = np.asarray(cols, np.float64)
    pts = tris.reshape(-1, 3)
    sx, sy, sd = pts @ r, pts @ u, pts @ c
    margin = 0.06
    if frame is not None:
        cx, cy, span = (float(v) for v in frame)
    else:
        span = max((sx.max() - sx.min()) / (W * (1 - 2 * margin)), (sy.max() - sy.min()) / (H * (1 - 2 * margin)))
        cx, cy = (sx.max() + sx.min()) / 2, (sy.max() + sy.min()) / 2
    span = max(span, 1e-9)
    X = ((sx - cx) / span + W / 2).reshape(-1, 3)
    Y = (H / 2 - (sy - cy) / span).reshape(-1, 3)
    D = sd.reshape(-1, 3)
    # flat shading from the face normal (double sided)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    nl = np.linalg.norm(n, axis=1)
    # drop degenerate and back-facing triangles (outward CCW faces turned away from the camera)
    ok = (nl > 1e-12) & ((n @ c) > -0.02 * np.maximum(nl, 1e-12))
    n = n[ok] / nl[ok, None]
    X, Y, D, col = X[ok], Y[ok], D[ok], cols[ok]
    L = np.asarray(light if light is not None else (c + r * 0.4 + u * 0.8), np.float64)
    L /= np.linalg.norm(L)
    shade = 0.32 + 0.68 * np.abs(n @ L)
    facing = np.abs(n @ c)
    shade *= 0.85 + 0.15 * facing
    if unlit is not None:
        shade[np.asarray(unlit, bool)[ok]] = 1.0
    # samples per triangle ~ screen area
    area = 0.5 * np.abs((X[:, 1] - X[:, 0]) * (Y[:, 2] - Y[:, 0]) - (X[:, 2] - X[:, 0]) * (Y[:, 1] - Y[:, 0]))
    k = np.clip(np.ceil(area * DENSITY), 1, 20000).astype(np.int64)
    rng = np.random.default_rng(seed)
    tri_id = np.repeat(np.arange(len(k)), k)
    a = rng.random(len(tri_id))
    b = rng.random(len(tri_id))
    flip = a + b > 1
    a[flip], b[flip] = 1 - a[flip], 1 - b[flip]
    first = np.cumsum(k) - k
    centroid = (tri_id - first[tri_id]) == 0          # first sample of each triangle: its centroid
    a[centroid], b[centroid] = 1 / 3, 1 / 3
    w0 = 1 - a - b
    px = X[tri_id, 0] * w0 + X[tri_id, 1] * a + X[tri_id, 2] * b
    py = Y[tri_id, 0] * w0 + Y[tri_id, 1] * a + Y[tri_id, 2] * b
    pd = D[tri_id, 0] * w0 + D[tri_id, 1] * a + D[tri_id, 2] * b
    ix, iy = px.astype(np.int64), py.astype(np.int64)
    inside = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
    ix, iy, pd, tri_id = ix[inside], iy[inside], pd[inside], tri_id[inside]
    pix = iy * W + ix
    order = np.argsort(pd, kind="stable")             # far to near: the nearest sample is written last
    flat = img.reshape(-1, 3)
    t = tri_id[order]
    flat[pix[order]] = np.clip(col[t] * shade[t, None], 0, 1)
    out = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    if alpha:
        a = np.zeros(H * W, np.uint8)
        a[pix] = 255
        return np.concatenate([out, a.reshape(H, W, 1)], 2)
    return out


def render_views(v, views=("front_3q", "rear_3q", "left", "top"), size=(640, 400), color_mode="material") -> dict:
    """-> {view: PNG bytes}."""
    tris, cols = _triangles(v, color_mode)
    return {name: png_bytes(render(tris, cols, name, size)) for name in views}


def contact_sheet(v, size=(480, 300), color_mode="material", views=("front_3q", "rear_3q", "left", "top")) -> bytes:
    """Four views in a 2x2 sheet (one PNG)."""
    tris, cols = _triangles(v, color_mode)
    ims = [render(tris, cols, name, size) for name in views]
    while len(ims) < 4:
        ims.append(np.zeros_like(ims[0]))
    top = np.concatenate(ims[:2], 1)
    bottom = np.concatenate(ims[2:4], 1)
    sheet = np.concatenate([top, bottom], 0)
    sheet[size[1] - 1:size[1] + 1, :] = 60
    sheet[:, size[0] - 1:size[0] + 1] = 60
    return png_bytes(sheet)
