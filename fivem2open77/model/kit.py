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

A mod-kit part is fitted only when it is a big piece of the car (MOD_NEEDED: a body, a chassis, a
bed); GTA fits no mod by default, so smaller ones (spoilers, cages, running boards) stay options.
A part the model lacks is fitted only when it shows: most of its new surface can be seen from above,
the sides, the front or the back past the model (a missing roof, body or bed does; a skid plate seen
only from below, or a cage inside the cabin, does not), it stays inside the car's own box (a roof
box, a rack or a light bar standing above the roof is an accessory) and it is not mostly another
fitted part over again (alternatives in two slots).  A mod's new surface is measured against the model
itself, so two mods that complete the car together (a truck shipped as a "chassis" and a "body"
mod) are both fitted; an extra's also against the parts already fitted.
"""
from __future__ import annotations

import numpy as np

VOXEL = 0.05            # metres
MIN_NEW = 0.5           # at least this share of a part's surface must be where nothing is yet
MIN_GAIN = 0.06         # ... and that new surface must be this big next to the model's own
REPLACES = 0.2          # a part that hides stock geometry this big (vs its own) replaces it
MIN_SEEN = 0.5          # share of its new surface that must be visible from outside (not from below)
MAX_OUTSIDE = 0.5       # share of its new surface allowed outside the car's box (+ BOX_MARGIN)
BOX_MARGIN = 2          # voxels (10 cm)
WHOLE_BODY = 0.3        # a part this big next to the model is the body itself: no box test
MAX_DUP = 0.6           # share of its new surface over a part already fitted: an alternative to it
MOD_NEEDED = 0.2        # a mod-kit part is fitted only when it is a piece this big of the car (a body, a
                        # chassis, a bed): GTA fits no mod by default, a smaller one is an option
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


def seen_share(occupied_keys, part_keys) -> float:
    """Share of part_keys visible past the occupied voxels from above, the sides, the front or
    the back (not from below): a voxel is seen from a direction when nothing occupied lies beyond
    it that way."""
    if not len(part_keys):
        return 0.0
    occupied_keys = dilate(occupied_keys)        # the model's vertices are sparse: close the gaps
    allv = [_unpack(k) for k in (occupied_keys, part_keys) if len(k)]
    lo = np.concatenate(allv).min(0)
    hi = np.concatenate(allv).max(0)
    shape = tuple(hi - lo + 1)
    occ = np.zeros(shape, bool)
    if len(occupied_keys):
        o = _unpack(occupied_keys) - lo
        occ[o[:, 0], o[:, 1], o[:, 2]] = True
    p = _unpack(part_keys) - lo
    seen = np.zeros(len(p), bool)
    idx = np.arange(shape[2])
    # +Z (above): the highest occupied z in the column
    top = np.where(occ.any(2), np.max(np.where(occ, idx[None, None, :], -1), axis=2), -1)
    seen |= p[:, 2] >= top[p[:, 0], p[:, 1]]
    for axis in (0, 1):                      # -X/+X (sides), -Y/+Y (back, front)
        ix = np.arange(shape[axis])
        sh = [1, 1, 1]
        sh[axis] = -1
        coords = ix.reshape(sh)
        any_ = occ.any(axis)
        mx = np.where(any_, np.max(np.where(occ, coords, -1), axis=axis), -1)
        mn = np.where(any_, np.min(np.where(occ, coords, shape[axis]), axis=axis), shape[axis])
        rest = [a for a in range(3) if a != axis]
        a, b = p[:, rest[0]], p[:, rest[1]]
        seen |= p[:, axis] >= mx[a, b]
        seen |= p[:, axis] <= mn[a, b]
    return float(seen.mean())


def plan(base_keys, candidates) -> dict:
    """candidates: [{"id", "keys", "forced" (True/False/None), "replaced" (stock verts it hides),
    "verts", "slots" (set, mods only)}]  ->  {id: (on, reason)}."""
    occupied = dilate(base_keys)                 # the model and what GTA/the profile fits
    model_keys = base_keys
    fitted = np.zeros(0, np.int64)               # parts fitted here because the car needs them (dilated)
    fitted_raw = np.zeros(0, np.int64)
    base_n = max(1, len(base_keys))
    out = {}
    taken_slots = set()

    def fit(c, forced=False):
        nonlocal occupied, fitted, fitted_raw, model_keys
        d = dilate(c["keys"])
        if forced:
            occupied = np.union1d(occupied, d)
            model_keys = np.union1d(model_keys, c["keys"])
        else:
            fitted = np.union1d(fitted, d)
            fitted_raw = np.union1d(fitted_raw, c["keys"])
        taken_slots.update(c.get("slots") or ())

    for c in candidates:                          # the profile's and GTA's own choices first
        if c.get("forced") is True:
            out[c["id"]] = (True, c.get("why") or "on in the profile")
            fit(c, forced=True)
        elif c.get("forced") is False:
            out[c["id"]] = (False, "off in the profile")
    if len(model_keys):
        mv = _unpack(model_keys)
        box_lo, box_hi = mv.min(0) - BOX_MARGIN, mv.max(0) + BOX_MARGIN
    else:
        box_lo = box_hi = None
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
        # a mod-kit part is measured against the model alone (two mods can make up the car between
        # them), an extra also against what is fitted already (extras overlap as alternatives)
        ref = occupied if c["id"][0] == "mod" or not len(fitted) else np.union1d(occupied, fitted)
        new = ~np.isin(keys, ref)
        share, gain = float(new.mean()), float(new.sum()) / base_n
        if share < MIN_NEW:
            out[c["id"]] = (False, f"an alternative to what the car has ({100 * (1 - share):.0f}% overlaps)")
            continue
        if gain < MIN_GAIN:
            out[c["id"]] = (False, f"an optional add-on ({100 * gain:.1f}% of the car)")
            continue
        nk = keys[new]
        dup = float(np.isin(nk, fitted).mean()) if len(fitted) else 0.0
        if dup > MAX_DUP:
            out[c["id"]] = (False, f"an alternative to a part already fitted ({100 * dup:.0f}% overlaps it)")
            continue
        if box_lo is not None and gain < WHOLE_BODY:
            v = _unpack(nk)
            outside = float(((v < box_lo) | (v > box_hi)).any(1).mean())
            if outside > MAX_OUTSIDE:
                out[c["id"]] = (False, f"an accessory: {100 * outside:.0f}% of it stands outside the car "
                                       "(a roof box, rack or light bar)")
                continue
        seen = seen_share(np.union1d(model_keys, fitted_raw), nk) if gain < WHOLE_BODY else 1.0
        if seen < MIN_SEEN:
            out[c["id"]] = (False, f"an optional add-on: {100 * (1 - seen):.0f}% of it is hidden under or "
                                   "inside the car (a skid plate, a cage)")
            continue
        if c["id"][0] == "mod" and gain < MOD_NEEDED:
            out[c["id"]] = (False, f"an optional mod-kit part ({100 * gain:.0f}% of the car; GTA fits none by default)")
            continue
        out[c["id"]] = (True, f"adds {100 * gain:.0f}% to the car where it has nothing (the model needs it)")
        fit(c)
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
