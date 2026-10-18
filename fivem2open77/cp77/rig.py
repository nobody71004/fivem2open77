"""Cyberpunk vehicle rigs for the converted model.

Two rigs are written, both cloned from the donor:
  * the deformation rig (all moving parts, seats, steering, pedals ...);
  * the vehicle (wheel physics) base rig, which places the suspension/wheels for the simulation.

Moving parts keep the donor's additive animations.  Those deltas are applied in the bone's local
frame, so each door/lid bone is oriented such that the donor's opening axis maps onto the GTA
model's hinge axis (from its joint limits).  Butterfly/scissor doors therefore open along the GTA
path with the stock Cyberpunk animation.
"""
from __future__ import annotations

import copy
import itertools

import numpy as np

from ..model.build import _rot_to_quat
from ..gta.drawable import quat_to_mat3
from .wkjson import value_of


def axis_rotations():
    """All 24 proper axis-aligned rotation matrices."""
    mats = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3))
            for r, (c, s) in enumerate(zip(perm, signs)):
                m[r, c] = s
            if np.linalg.det(m) > 0.5:
                mats.append(m)
    return mats


_AXIS_ROTS = axis_rotations()


def align_axis(d, a):
    """Rotation Q (3x3, axis aligned, closest to identity) with Q @ d == a."""
    d = np.asarray(d, float)
    a = np.asarray(a, float)
    best, score = np.eye(3), -1e9
    for m in _AXIS_ROTS:
        if np.allclose(m @ d, a, atol=1e-6):
            sc = np.trace(m) + 0.01 * m[2, 2]  # prefer keeping Z up
            if sc > score:
                best, score = m, sc
    return best


def _mat(t, r):
    m = np.eye(4)
    m[:3, :3] = r
    m[:3, 3] = t
    return m


def _read_rig(doc):
    r = doc["Data"]["RootChunk"]
    names = [value_of(n) for n in r["boneNames"]]
    parents = list(r["boneParentIndexes"])
    local = []
    for t in r["boneTransforms"]:
        tr = t["Translation"]
        q = t["Rotation"]
        local.append(_mat([tr["X"], tr["Y"], tr["Z"]], quat_to_mat3((q["i"], q["j"], q["k"], q["r"]))))
    return names, parents, local


def _abs_from_local(parents, local):
    out = []
    for i, p in enumerate(parents):
        out.append(local[i] if p < 0 else out[p] @ local[i])
    return out


def _write_rig(doc, names, parents, abs_m):
    d = copy.deepcopy(doc)
    r = d["Data"]["RootChunk"]
    for i, (p, m) in enumerate(zip(parents, abs_m)):
        loc = m if p < 0 else np.linalg.inv(abs_m[p]) @ m
        t = loc[:3, 3]
        q = _rot_to_quat(loc[:3, :3])
        bt = r["boneTransforms"][i]
        bt["Translation"]["X"], bt["Translation"]["Y"], bt["Translation"]["Z"] = (float(x) for x in t)
        bt["Rotation"]["i"], bt["Rotation"]["j"], bt["Rotation"]["k"], bt["Rotation"]["r"] = q
    return d


LIDS = ("hood_a", "trunk_a")


def lid_mapping(vehicle, donor_rig_doc) -> dict:
    """Which donor lid each converted lid has to become.

    The converter names the GTA bonnet hood_a and the boot trunk_a, by meaning.  But everything
    the donor attaches to a lid -- the interaction prompt, the player's workspot, the opening
    animation -- belongs to the donor lid at that END of the car, and the default donor (Porsche
    911) keeps its trunk at the front and its engine lid, hood_a, at the rear.  Left alone, the
    "trunk" prompt at the front opened the rear lid.  So each converted lid takes the name of the
    donor lid on the same end; on a front-engine donor this is the identity.

    Returns {current name: new name} for the lids that change, {} when nothing does."""
    names, parents, local = _read_rig(donor_rig_doc)
    dabs = _abs_from_local(parents, local)
    idx = {n: i for i, n in enumerate(names)}
    donor = {n: float(dabs[idx[n]][1, 3]) for n in LIDS if n in idx}
    wheels = [float(dabs[idx[n]][1, 3]) for n in names
              if n.startswith(("wheel_front_", "wheel_back_")) and n in idx]
    d_mid = (max(wheels) + min(wheels)) / 2 if wheels else 0.0
    ours = {n: float(vehicle.bones[n]["t"][1]) for n in LIDS if n in vehicle.bones}
    ys = [p[1] for a in vehicle.axles.values() for p in a.positions.values()]
    v_mid = (max(ys) + min(ys)) / 2 if ys else 0.0
    if len(donor) < 2 or not ours:
        return {}
    donor_at = {}
    for n, y in donor.items():
        donor_at.setdefault("front" if y > d_mid else "rear", n)
    if len(donor_at) < 2:
        return {}                 # donor has both lids on one end: nothing to match against
    mapping = {n: donor_at["front" if y > v_mid else "rear"] for n, y in ours.items()}
    if len(set(mapping.values())) < len(mapping):
        return {}                 # both converted lids on one end (odd model): keep their names
    return {k: v for k, v in mapping.items() if k != v}


def build_rigs(vehicle, donor_rig_doc, donor_vrig_doc, profile, donor_bbox):
    """Returns (rig_doc, vehicle_rig_doc, frames) where frames[bone] = (abs 4x4)."""
    names, parents, local = _read_rig(donor_rig_doc)
    dabs = _abs_from_local(parents, local)
    idx = {n: i for i, n in enumerate(names)}
    base_z = vehicle.base_z
    explicit = {}

    # Base and wheel geometry
    explicit["Base"] = _mat([0, 0, base_z], np.eye(3))
    corner = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}
    wheel_pos = {}
    for axle in vehicle.axles.values():
        for side, pos in axle.positions.items():
            wheel_pos[corner[side]] = np.array(pos, float)
    for c, pos in wheel_pos.items():
        explicit[f"suspension_{c}"] = _mat([pos[0], pos[1], pos[2] + base_z], np.eye(3))

    # articulated parts (frame alignment against the donor's animation axes)
    alignments = {}
    for name, b in vehicle.bones.items():
        if name not in idx or name.startswith(("wheel_", "seat_", "steering")):
            continue
        r = np.array(b["r"])
        d_axis = profile["open_axes"].get(name)
        if d_axis is not None and b.get("open_axis") is not None:
            q = align_axis(d_axis, b["open_axis"])
            r = r @ q
            alignments[name] = q
        explicit[name] = _mat(b["t"], r)
    # Seats.  GTA seat bones are not the hip point Cyberpunk's sitting animations expect, so the
    # driver keeps the donor's hip-to-steering-wheel ergonomics (arms reach the wheel, feet the
    # pedals) and the front passenger is level with the driver.  Lateral positions stay GTA's.
    ergo = None
    if ("steering_wheel_zro" in vehicle.bones and "seat_front_left" in idx and "steering_wheel_zro" in idx):
        off = dabs[idx["seat_front_left"]][:3, 3] - dabs[idx["steering_wheel_zro"]][:3, 3]
        wheel = np.array(vehicle.bones["steering_wheel_zro"]["t"], float)
        ergo = wheel + off
    for name in ("seat_front_left", "seat_front_right", "seat_back_left", "seat_back_right"):
        if name in vehicle.bones and name in idx:
            t = np.array(vehicle.bones[name]["t"], float)
            if ergo is not None and name in ("seat_front_left", "seat_front_right"):
                t = np.array([t[0], ergo[1], float(np.clip(ergo[2], t[2] - 0.08, t[2] + 0.08))])
            explicit[name] = _mat(t, np.eye(3))
    # seat backrests (four-door donors) keep their place on their seat
    for seat in ("seat_front_left", "seat_front_right", "seat_back_left", "seat_back_right"):
        rest = seat.replace("seat_", "backrest_", 1)
        if rest in idx and seat in explicit and seat in idx:
            off = dabs[idx[rest]][:3, 3] - dabs[idx[seat]][:3, 3]
            explicit[rest] = _mat(explicit[seat][:3, 3] + off, dabs[idx[rest]][:3, :3])
    if "steering_wheel_zro" in vehicle.bones and "steering_wheel_zro" in idx:
        b = vehicle.bones["steering_wheel_zro"]
        # keep the donor's column frame (its turn animation spins about the frame's local Y),
        # tilted so that axis follows the converted wheel's own column
        r = dabs[idx["steering_wheel_zro"]][:3, :3]
        col = b.get("column")
        if col is not None:
            r = _rotation_between(r @ np.array([0.0, 1.0, 0.0]), np.asarray(col, float)) @ r
        explicit["steering_wheel_zro"] = _mat(b["t"], r)
        if "pedals" in idx:
            off = dabs[idx["pedals"]][:3, 3] - dabs[idx["steering_wheel_zro"]][:3, 3]
            explicit["pedals"] = _mat(np.array(b["t"]) + off, np.eye(3))

    # remaining bones: follow their parent if it moved, else scale with the body size
    dmin, dmax = (np.array(x, float) for x in donor_bbox)
    nmin, nmax = (np.array(x, float) for x in vehicle.bbox)
    dsize = np.maximum(dmax - dmin, 1e-3)
    nsize = np.maximum(nmax - nmin, 1e-3)
    dctr, nctr = (dmin + dmax) / 2, (nmin + nmax) / 2
    new_abs = [None] * len(names)
    moved = set()
    for i, n in enumerate(names):
        p = parents[i]
        if n in explicit:
            new_abs[i] = explicit[n]
            moved.add(i)
        elif p >= 0 and p in moved and names[p] not in ("Root", "Base"):
            new_abs[i] = new_abs[p] @ local[i]
            moved.add(i)
        elif p < 0 or n in ("Trajectory",):
            new_abs[i] = dabs[i]
        else:
            t = nctr + (dabs[i][:3, 3] - dctr) * (nsize / dsize)
            new_abs[i] = _mat(t, dabs[i][:3, :3])
    # wheels sit base_z below their suspension; brake/swingarm carriers at the wheel centre
    for c in corner.values():
        for prefix in ("wheel_", "break_", "swingarm_"):
            n = prefix + c
            if n in idx and f"suspension_{c}" in idx:
                s = new_abs[idx[f"suspension_{c}"]]
                rot = dabs[idx[n]][:3, :3] if prefix == "wheel_" else np.eye(3)
                new_abs[idx[n]] = s @ _mat([0, 0, -base_z], np.linalg.inv(s[:3, :3]) @ rot)
    # steering wheel mesh frame stays axis aligned under the tilted column
    if "steering_wheel" in idx and "steering_wheel_zro" in explicit:
        zro = new_abs[idx["steering_wheel_zro"]]
        new_abs[idx["steering_wheel"]] = _mat(zro[:3, 3], np.eye(3))
        for child in ("steering_left", "steering_right"):
            if child in idx:
                turn = new_abs[idx["steering_wheel_turn"]] if "steering_wheel_turn" in idx else zro
                new_abs[idx[child]] = turn @ local[idx[child]]
    rig_doc = _write_rig(donor_rig_doc, names, parents, new_abs)
    _set_wheel_radius_tracks(rig_doc, vehicle)
    frames = {n: new_abs[i] for i, n in enumerate(names)}

    # wheel physics rig
    vnames, vparents, vlocal = _read_rig(donor_vrig_doc)
    vabs = []
    for i, n in enumerate(vnames):
        if n in frames:
            vabs.append(frames[n])
        else:
            p = vparents[i]
            vabs.append(vlocal[i] if p < 0 else vabs[p] @ vlocal[i])
    vrig_doc = _write_rig(donor_vrig_doc, vnames, vparents, vabs)
    _set_wheel_radius_tracks(vrig_doc, vehicle)
    return rig_doc, vrig_doc, frames, alignments


def _rotation_between(a, b):
    """Smallest rotation matrix taking direction a onto direction b."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    b = np.asarray(b, float) / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if np.linalg.norm(v) < 1e-9:
        if c > 0:
            return np.eye(3)
        # opposite: rotate 180 degrees about any axis perpendicular to a
        p = np.cross(a, [1.0, 0, 0])
        if np.linalg.norm(p) < 1e-6:
            p = np.cross(a, [0, 1.0, 0])
        p /= np.linalg.norm(p)
        return 2 * np.outer(p, p) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def _set_wheel_radius_tracks(doc, vehicle):
    """Reference values of the veh_rad_w_* float tracks follow the converted tyres."""
    r = doc["Data"]["RootChunk"]
    names = [value_of(n) for n in r.get("trackNames", [])]
    tracks = r.get("referenceTracks")
    if not tracks or len(tracks) != len(names):
        return
    rad = {"f": vehicle.axles.get("front"), "b": vehicle.axles.get("back")}
    for i, n in enumerate(names):
        if n and n.startswith("veh_rad_w_"):
            axle = rad.get(n.split("_")[3])
            if axle is not None:
                tracks[i] = float(axle.radius)
