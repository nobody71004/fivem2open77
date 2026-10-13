"""Which optional parts a converted car wears: GTA extras and mod-kit parts.

GTA cars carry optional geometry two ways: extras (extra_N bones of the model, toggled per car)
and visible mods (carcols.meta mod kits: separate .yft parts fitted on a bone, one per slot).
Many FiveM cars are complete as they are and use both for options (spoilers, bumpers, light bars);
some are not: their body, roof or seats only exist as an extra or a kit part (a drag build whose
whole bodyshell is a "grille" mod, a truck whose cab roof is a "roof" mod), and the game shows
them complete only because the server fits those parts.

The default here is what makes the car look like the car: GTA's own rules first (vehicles.meta
requiredExtras, FLAG_EXTRAS_ALL), then every optional part that adds a substantial piece of the
car the model does not already have.  "Has" is measured on a voxel grid: a part whose surfaces
mostly sit where the model (or a part already fitted) has none is new geometry; one that sits
where the model already has a surface is an alternative to it (another bonnet, another spoiler)
and stays off, like a part that hides stock bones to replace them.  Only one part per mod slot.
The profile's own choices always win (parts.extras, parts.mods).
"""
from __future__ import annotations

import numpy as np

VOXEL = 0.05            # metres
MIN_NEW = 0.5           # at least this share of a part's surface must be where nothing is yet
MIN_GAIN = 0.06         # ... and that new surface must be this big next to the model's own
REPLACES = 0.2          # a part that hides stock geometry this big (vs its own) replaces it
_BIAS = 1 << 20
_OFFS = np.array([[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], np.int64)


def voxel_keys(points) -> np.ndarray:
    """(N, 3) metres -> sorted unique int64 voxel keys."""
    p = np.asarray(points, np.float64)
    if not len(p):
        return np.zeros(0, np.int64)
    v = np.floor(p / VOXEL).astype(np.int64) + _BIAS
    return np.unique((v[:, 0] << 42) | (v[:, 1] << 21) | v[:, 2])


def _unpack(keys):
    return np.stack([(keys >> 42) & 0x1FFFFF, (keys >> 21) & 0x1FFFFF, keys & 0x1FFFFF], 1)


def dilate(keys) -> np.ndarray:
    """Keys plus their 6 face neighbours (a surface a few cm off another counts as the same)."""
    if not len(keys):
        return keys
    v = _unpack(keys)
    out = (v[None, :, :] + _OFFS[:, None, :]).reshape(-1, 3)
    return np.unique((out[:, 0] << 42) | (out[:, 1] << 21) | out[:, 2])


def plan(base_keys, candidates) -> dict:
    """candidates: [{"id", "keys", "forced" (True/False/None), "replaced" (stock verts it hides),
    "verts", "slots" (set, mods only)}]  ->  {id: (on, reason)}."""
    occupied = dilate(base_keys)
    base_n = max(1, len(base_keys))
    out = {}
    taken_slots = set()

    def fit(c):
        nonlocal occupied
        occupied = np.union1d(occupied, dilate(c["keys"]))
        taken_slots.update(c.get("slots") or ())

    for c in candidates:                          # the profile's and GTA's own choices first
        if c.get("forced") is True:
            out[c["id"]] = (True, c.get("why") or "on in the profile")
            fit(c)
        elif c.get("forced") is False:
            out[c["id"]] = (False, "off in the profile")
    rest = sorted((c for c in candidates if c["id"] not in out), key=lambda c: -len(c["keys"]))
    for c in rest:
        keys = c["keys"]
        if not len(keys):
            out[c["id"]] = (False, "no geometry")
            continue
        if c.get("slots") and c["slots"] & taken_slots:
            out[c["id"]] = (False, "another part is fitted in its slot")
            continue
        if c.get("replaced", 0) > REPLACES * max(1, c.get("verts", 0)):
            out[c["id"]] = (False, "replaces a part the car already has")
            continue
        new = ~np.isin(keys, occupied)
        share, gain = float(new.mean()), float(new.sum()) / base_n
        if share >= MIN_NEW and gain >= MIN_GAIN:
            out[c["id"]] = (True, f"adds {100 * gain:.0f}% to the car where it has nothing (the model needs it)")
            fit(c)
        elif share < MIN_NEW:
            out[c["id"]] = (False, f"an alternative to what the car has ({100 * (1 - share):.0f}% overlaps)")
        else:
            out[c["id"]] = (False, f"an optional add-on ({100 * gain:.1f}% of the car)")
    return out


def drawable_points(drawable, lods=("high", "med", "low", "vlow")):
    """[(bone index or None, (N, 3) model-space points)] of a drawable's most detailed LOD.  Skinned
    geometry is in model space already; rigid models are moved by their bone."""
    sk = drawable.skeleton
    world = [sk.world_matrix(i) for i in range(len(sk.bones))] if sk else []
    models = []
    for lod in lods:
        if drawable.models.get(lod):
            models = drawable.models[lod]
            break
    out = []
    for m in models:
        for g in m.geometries:
            a = g.attrs
            if "position" not in a or g.triangle_count == 0:
                continue
            p = np.asarray(a["position"], np.float64)
            n = len(p)
            if m.has_skin and "blendweights" in a and "blendindices" in a:
                bw, bi = a["blendweights"], a["blendindices"]
                pal = np.array(g.bone_ids if g.bone_ids else range(256), dtype=np.int64)
                vb = pal[np.clip(bi[np.arange(n), bw.argmax(1)], 0, len(pal) - 1)]
                for b in np.unique(vb):
                    out.append((int(b), p[vb == b]))
            else:
                if world and m.bone_index < len(world):
                    w = world[m.bone_index]
                    p = p @ w[:3, :3].T + w[:3, 3]
                out.append((int(m.bone_index), p))
    return out
