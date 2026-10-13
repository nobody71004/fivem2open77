"""Seats: how the player sits, whether the head fits under the roof, and where they get out.

* Posture.  Cyberpunk picks the sitting animation set from the vehicle data package's
  ``seatingTemplateOverride``: ``sport2_vehicle`` (the Porsche donor, Shion, Type-66),
  ``sport1_vehicle`` (hypercars: Caliburn, Outlaw, Turbo-R -- lower and reclined),
  ``standard2_vehicle`` (ordinary cars) and so on.  The donor's posture is kept unless the head
  cannot fit under a closed roof even with the seat lowered; then sport1 (the hypercars' lower,
  reclined one) is used.
* Head room.  When a closed roof sits over the seat and the posture's head height would poke
  through it, the seat is lowered (at most MAX_LOWER in a low car, MAX_LOWER_UPRIGHT in others,
  never closer than SEAT_FLOOR to the road).
* Getting out.  The game lets a player out only where a player-sized space beside the car is free:
  on the vehicle record's ``unmountOffsetPosition`` (x: metres from the centre line; every
  Cyberpunk car has 1.65, i.e. about 0.65-0.7 m outside a 0.95-1.0 m half-width; the big trucks
  2.5, the bikes 1.0).  When neither side is free it slides the player to the other seat to get
  out there, and back, for ever.  A wide car (the Biome's wheel fairings are 1.26 m out) needs that
  distance raised to its own width plus the same clearance, measured over the stretch of the car a
  player standing beside the seat covers (the collision hull's slices reach that far too).  The
  standing spot of each seat (InteractionSlots, the donor's 1 m to the side and 0.5 m down) is
  moved out with it, and up to the road when it would be under it.

All positions are Cyberpunk Root space (X right, Y forward, Z up, wheel centres at Z = 0).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# template -> (TweakDB seatingTemplateOverride, animVars prefix, head-top height above the seat bone)
TEMPLATES = {
    "sport1": ("sport1_vehicle", 0.80),
    "sport2": ("sport2_vehicle", 0.90),
    "standard2": ("standard2_vehicle", 0.95),
    "standard25": ("standard25_vehicle", 0.97),
    "standard3": ("standard3_vehicle", 1.00),
}
SEAT_NAMES = ("seat_front_left", "seat_front_right", "seat_back_left", "seat_back_right")
SIDE_OF = {"seat_front_left": -1.0, "seat_back_left": -1.0, "seat_front_right": 1.0, "seat_back_right": 1.0}
MAX_LOWER = 0.10         # m a seat may be lowered for head room (low cars, which can recline instead)
MAX_LOWER_UPRIGHT = 0.15 # m for everything else: saloons and SUVs keep the upright posture
SEAT_FLOOR = 0.12        # m the seat bone stays above the road
ROOF_THICKNESS = 0.03    # m between the outer roof and the inside of the cabin
EXIT_CLEARANCE = 0.70    # m from the widest body beside a seat to where the player stands after getting
                         # out: what Cyberpunk's own cars leave (1.65 m from the centre, 0.95-1.0 m wide)
EXIT_WINDOW = 0.80       # m along the car, either side of a seat, whose width counts: a standing player
                         # (~0.4 m radius) and the collision hull's slices both reach that far
EXIT_MARGIN = 0.20       # m of clear air beside the body that a standing spot must keep to count as
                         # outside it (the donor's 1 m out on a 0.95-1.0 m half-width leaves that)
UNMOUNT_DEFAULT = (1.65, 5.0, 2.5)   # unmountOffsetPosition of every Cyberpunk car (the donors included)
MIN_CLEARANCE = 0.50     # m outside the body below which the default distance is not trusted: the
                         # BMW i8 (1.11 m out, 0.54 m left) gets out, the Biome (1.26 m, 0.39 m) does not


@dataclass
class SeatFit:
    template: str
    template_reason: str
    seats: dict = field(default_factory=dict)     # name -> {position, lowered, headroom, roof, exit}
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    unmount: tuple | None = None                  # unmountOffsetPosition for the record; None = the donor's
    exit_needed: float = 0.0                      # m from the centre line a player needs to get out

    @property
    def tweak_template(self):
        return TEMPLATES[self.template][0]

    def summary(self):
        return {"template": self.template, "reason": self.template_reason,
                "seats": {k: {kk: (round(vv, 3) if isinstance(vv, float) else
                                   [round(float(x), 3) for x in vv] if isinstance(vv, (tuple, list, np.ndarray)) else vv)
                              for kk, vv in s.items()} for k, s in self.seats.items()},
                "exit_distance": round(float(self.unmount[0] if self.unmount else UNMOUNT_DEFAULT[0]), 3),
                "exit_needed": round(float(self.exit_needed), 3),
                "unmount": [round(float(x), 3) for x in self.unmount] if self.unmount else None,
                "notes": list(self.notes),
                "warnings": list(self.warnings)}


class _TopMap:
    """Highest and lowest body surface over a ground-plan grid."""

    def __init__(self, tris, cell=0.03, seed=11):
        self.cell = cell
        self.ok = len(tris) > 0
        if not self.ok:
            return
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
        n = np.ceil(area / (cell * cell / 6.0)).astype(np.int64)
        if n.sum() > 3_000_000:
            n = np.maximum(1, (n * (3_000_000 / n.sum())).astype(np.int64))
        idx = np.repeat(np.arange(len(tris)), n)
        rng = np.random.default_rng(seed)
        u, v = rng.random(len(idx)), rng.random(len(idx))
        f = u + v > 1
        u[f], v[f] = 1 - u[f], 1 - v[f]
        p = a[idx] + (b[idx] - a[idx]) * u[:, None] + (c[idx] - a[idx]) * v[:, None]
        p = np.concatenate([p, a, b, c])
        self.p = p
        self.x0, self.y0 = float(p[:, 0].min()), float(p[:, 1].min())
        ix = ((p[:, 0] - self.x0) / cell).astype(np.int64)
        iy = ((p[:, 1] - self.y0) / cell).astype(np.int64)
        self.nx, self.ny = int(ix.max()) + 1, int(iy.max()) + 1
        top = np.full(self.nx * self.ny, -np.inf)
        np.maximum.at(top, ix * self.ny + iy, p[:, 2])
        self.top = top.reshape(self.nx, self.ny)

    def top_at(self, x, y):
        i, j = int((x - self.x0) / self.cell), int((y - self.y0) / self.cell)
        if not (0 <= i < self.nx and 0 <= j < self.ny):
            return None
        v = float(self.top[i, j])
        return None if np.isinf(v) else v


def _shell_triangles(vehicle):
    """Root-space triangles of the outer shell: body, doors, lids, windows, glass, lights."""
    out = []
    for name, part in vehicle.parts.items():
        if part.kind not in ("body", "door", "hood", "trunk", "window", "glass", "lights"):
            continue
        f = vehicle.part_frames.get(name)
        t, r = (np.asarray(f[0], float), np.asarray(f[1], float)) if f is not None else (np.zeros(3), np.eye(3))
        for s in part.submeshes:
            if s.lod == 1 and len(s.indices):
                p = s.positions.astype(np.float64) @ r.T + t
                out.append(p[s.indices.reshape(-1, 3)])
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def _ground(vehicle, y):
    f, b = vehicle.axles.get("front"), vehicle.axles.get("back") or vehicle.axles.get("front")
    if f is None:
        return -0.34
    yf = float(np.mean([p[1] for p in f.positions.values()])) if f.positions else 1.3
    yb = float(np.mean([p[1] for p in b.positions.values()])) if b.positions else -1.3
    if abs(yf - yb) < 1e-6:
        return -f.radius
    t = min(1.0, max(0.0, (yf - y) / (yf - yb)))
    return -(f.radius + (b.radius - f.radius) * t)


def _roof_over(shell, x, y, z):
    """Inside of a closed roof over a seated head (the head is a little behind the seat bone), or
    None for an open car (a windscreen header or roll hoop alone is not a roof)."""
    if not shell.ok:
        return None
    roof, samples = [], 0
    for dy in np.linspace(-0.35, -0.05, 7):
        for dx in np.linspace(-0.10, 0.10, 5):
            samples += 1
            h = shell.top_at(x + dx, y + dy)
            if h is not None and h > z + 0.45:
                roof.append(h)
    if len(roof) < 0.7 * samples:
        return None
    return float(np.percentile(roof, 20)) - ROOF_THICKNESS


def fit_seats(vehicle, conf, donor_template="sport2", occupant_seats=None) -> SeatFit:
    """Choose the posture, lower seats that would put the head through the roof, and place the
    standing spot each seat's exit is checked at.  Moves vehicle.bones[seat] / vehicle.seats.

    Posture "auto" keeps the donor's unless a GTA *low* car (LAYOUT_LOW...) cannot fit the head under
    its roof even with the seat lowered; then the hypercars' lower, reclined sport1 posture is used.
    Other cars keep the upright posture and lower the seat a little more.  Only the seats the donor
    can seat a player in (occupant_seats) decide: a GTA rear bench behind a two-seat donor is never
    sat on."""
    conf = conf or {}
    usable = set(occupant_seats or SEAT_NAMES)
    shell = _TopMap(_shell_triangles(vehicle))
    pts = shell.p if shell.ok else np.zeros((0, 3))
    seats = []
    for name in SEAT_NAMES:
        b = vehicle.bones.get(name)
        if b is None:
            continue
        pos = np.asarray(b["t"], float).copy()
        g = _ground(vehicle, pos[1])
        seats.append((name, b, pos, g, _roof_over(shell, pos[0], pos[1], pos[2])))

    layout = (vehicle.gta_layout or "").upper()
    low = "LOW" in layout

    def worst_deficit(head, max_lower):
        worst = 0.0
        for name, b, pos, g, roof in seats:
            if roof is None or name not in usable:
                continue
            room = max(0.0, pos[2] - (g + SEAT_FLOOR))
            worst = max(worst, (pos[2] + head + 0.01 - roof) - min(max_lower, room))
        return worst

    want = conf.get("template", "auto")
    if want and want != "auto":
        template, why = want, "profile"
    else:
        template, why = donor_template, "the donor's"
        if donor_template != "sport1" and worst_deficit(TEMPLATES[donor_template][1], MAX_LOWER) > 0.005:
            if low:
                template = "sport1"
                why = f"the head does not fit under this low roof with the donor's posture ({layout})"
            else:
                why = "the donor's, seats lowered for head room"
    fit = SeatFit(template, why)
    head = TEMPLATES[template][1]
    max_lower = MAX_LOWER if (template == "sport1" or low) else MAX_LOWER_UPRIGHT
    extra_lower = float(conf.get("lower", 0.0) or 0.0)

    # ---- how far out a player has to stand to get out: the widest body on that seat's side over
    # the stretch of car beside it, plus the clearance Cyberpunk's own cars have.  One distance for
    # the whole car (the record has one unmountOffsetPosition); the donor's when that is enough.
    def side_width(name, pos, g):
        side = SIDE_OF[name]
        if not len(pts):
            return 0.95
        near = pts[(np.abs(pts[:, 1] - pos[1]) < EXIT_WINDOW) & (pts[:, 2] > g + 0.10) & (pts[:, 2] < pos[2] + 0.8)]
        out = near[near[:, 0] * side > 0]
        return float((out[:, 0] * side).max()) if len(out) else 0.95

    widths = {name: side_width(name, pos, g) for name, b, pos, g, roof in seats}
    widest = max(widths.values(), default=0.95)
    fit.exit_needed = widest + EXIT_CLEARANCE
    if UNMOUNT_DEFAULT[0] - widest < MIN_CLEARANCE:
        x = float(np.ceil(fit.exit_needed / 0.05 - 1e-6) * 0.05)
        fit.unmount = (round(x, 2), UNMOUNT_DEFAULT[1], UNMOUNT_DEFAULT[2])
    exit_x = fit.unmount[0] if fit.unmount else None

    for name, b, pos, g, roof in seats:
        x, y, z = pos
        info = {"position": tuple(pos), "lowered": 0.0, "roof": roof, "headroom": None}
        lower = 0.0
        if roof is not None:
            deficit = (z + head + 0.01) - roof
            room = max(0.0, z - (g + SEAT_FLOOR))
            if deficit > 0:
                lower = min(deficit, max_lower, room)
                if deficit - lower > 0.005 and name in usable:
                    fit.warnings.append(f"{name}: the head may still touch the roof by {100 * (deficit - lower):.0f} cm "
                                        f"({template} seating); seats.lower moves the seat further down")
        lower += extra_lower
        if lower:
            pos = pos.copy()
            pos[2] -= lower
            b["t"] = pos
            if name in vehicle.seats:
                vehicle.seats[name] = tuple(pos)
            info["lowered"] = float(lower)
            info["position"] = tuple(pos)
        if roof is not None:
            info["headroom"] = float(roof - pos[2])
        # ---- where the player stands after getting out on this seat's side.  The donor's spot
        # (1 m out, 0.5 m down from the seat bone) is kept when it is clear of the body and not
        # under the road; otherwise it moves out to the car's exit distance, at road level.
        side = SIDE_OF[name]
        hw = widths[name]
        default = np.array([pos[0] + side * 1.0, y, pos[2] - 0.5])
        inside = default[0] * side < (exit_x if exit_x else hw + EXIT_MARGIN)
        buried = default[2] < g - 0.08
        if inside or buried:
            lateral = max(exit_x or (hw + EXIT_CLEARANCE), abs(default[0])) if inside else abs(default[0])
            info["exit"] = (side * lateral, float(y), float(g - 0.02))
            info["exit_moved"] = ("out of the body" if inside else "") + (" and " if inside and buried else "") + \
                                 ("up to the road" if buried else "")
        else:
            info["exit"] = tuple(float(v) for v in default)
            info["exit_moved"] = ""
        info["body_halfwidth"] = hw
        fit.seats[name] = info
    if fit.unmount:
        fit.notes = [f"exit {fit.unmount[0]:.2f} m from the centre line (the body is {max(widths.values()):.2f} m "
                     f"out beside the seats; Cyberpunk cars use {UNMOUNT_DEFAULT[0]:.2f})"]
    vehicle.warnings.extend(fit.warnings)
    return fit
