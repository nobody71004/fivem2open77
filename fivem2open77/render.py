"""Software renders of a converted car (numpy only): orthographic views for thumbnails, QA and
the AI agent's visual checks.

Triangles are point-sampled in proportion to their screen area and depth-sorted per pixel, which
needs no rasteriser loop and is fast enough for half a million triangles.  Colour modes:
  "material": each material's colour (texture average, paint colour, glass, lights);
  "parts":    one colour per part kind (doors blue, hood orange, trunk red, ...), to check mapping.
"""
from __future__ import annotations

import math

import numpy as np

from .imaging import png_bytes
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


def _triangles(v, color_mode="material", skip_kinds=()):
    """All LOD-1 triangles in Root space -> (tri (N,3,3), colour (N,3), kind list)."""
    if color_mode == "material":
        _, means = build_materials(v, _Gltf(), textures=True, max_tex=32)
    tris, cols = [], []

    def add(part, r, t, kind):
        for sm in part.submeshes:
            if sm.lod != 1 or not len(sm.indices):
                continue
            p = sm.positions.astype(np.float64) @ r.T + t
            tri = p[sm.indices.reshape(-1, 3)]
            if color_mode == "parts":
                c = KIND_COLORS.get(kind, (0.6, 0.6, 0.6))
            else:
                c = means.get(sm.material, (0.6, 0.6, 0.6))
            tris.append(tri)
            cols.append(np.tile(np.asarray(c, np.float64)[None, :3], (len(tri), 1)))

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


def render(tris, cols, view="front_3q", size=(640, 400), seed=1, light=None) -> np.ndarray:
    """-> uint8 (H, W, 3)."""
    W, H = size
    az, el = VIEWS[view] if isinstance(view, str) else view
    r, u, c = _basis(az, el)
    img = np.zeros((H, W, 3), np.float64)
    # background: soft vertical gradient
    grad = np.linspace(0.20, 0.11, H)[:, None]
    img[:] = np.stack([grad * 0.9, grad * 0.95, grad * 1.1], -1)
    if not len(tris):
        return (img * 255).astype(np.uint8)
    pts = tris.reshape(-1, 3)
    sx, sy, sd = pts @ r, pts @ u, pts @ c
    margin = 0.06
    span = max((sx.max() - sx.min()) / (W * (1 - 2 * margin)), (sy.max() - sy.min()) / (H * (1 - 2 * margin)))
    cx, cy = (sx.max() + sx.min()) / 2, (sy.max() + sy.min()) / 2
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
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


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
