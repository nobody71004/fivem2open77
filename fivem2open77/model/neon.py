"""GTA V style neon underglow kits.

A kit is up to four LED tubes under the car -- left and right along the sills between the wheels,
front and back across the bumpers -- plus the lights that throw their colour on the road.  In GTA V
the tubes hang from the model's ``neon_l/neon_r/neon_f/neon_b`` bones; a model without them gets
the same layout measured from its own underside.  Like GTA V's neon the kit is light only: the
tubes place the lights and are not drawn, unless the profile asks for them (``neon.tubes``), then
they are glowing meshes too.

Mounting: the tube runs just under the body.  The underside is measured from the car's surfaces
(a height map of the lowest body surface over the ground plan), the tube sits ``gap`` below it,
never closer than ``min_clearance`` to the road, and is shortened where it would cut into a skirt
or a wheel.  Everything is in Cyberpunk Root space (X right, Y forward, Z up, wheel centres at
Z = 0, the road at -wheel radius).

The glow on the road: see ``plan_lights`` -- soft lights under the sills that wash the road around
the car in the kit's colour, the way the light of a real LED kit scatters on the road
(``ground_glow`` models them for previews and tests).

In the game every colour is its own appearance of the car (``<car>_neon_<colour>``), so a server
switches colours with ``Open77.vehicles.setAppearance`` and every player sees the same kit.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

# GTA V's neon palette (the LS Customs list), RGB 0..255
GTA_NEON_COLORS = [
    ("white", "White", (222, 222, 255)),
    ("blue", "Blue", (2, 21, 255)),
    ("electric_blue", "Electric Blue", (3, 83, 255)),
    ("mint_green", "Mint Green", (0, 255, 140)),
    ("lime_green", "Lime Green", (94, 255, 1)),
    ("yellow", "Yellow", (255, 255, 0)),
    ("golden_shower", "Golden Shower", (255, 150, 0)),
    ("orange", "Orange", (255, 62, 0)),
    ("red", "Red", (255, 1, 1)),
    ("pony_pink", "Pony Pink", (255, 50, 100)),
    ("hot_pink", "Hot Pink", (255, 5, 190)),
    ("purple", "Purple", (35, 1, 255)),
    ("blacklight", "Blacklight", (15, 3, 255)),
]
SIDES = ("left", "right", "front", "back")
BONE_OF = {"left": "neon_l", "right": "neon_r", "front": "neon_f", "back": "neon_b"}
MAX_COLORS = 24

TUBE_RADIUS = 0.011      # m: a 22 mm LED tube
GAP = 0.015              # m between the body and the top of the tube
MIN_CLEARANCE = 0.035    # m between the road and the bottom of the tube
WHEEL_MARGIN = 0.07      # m kept clear of the tyres (front ones steer)


@dataclass
class NeonTube:
    side: str
    start: np.ndarray        # Root space, metres
    end: np.ndarray
    source: str              # where the layout came from (GTA bone / measured)
    clearance: float = 0.0   # metres between the bottom of the tube and the road

    @property
    def length(self):
        return float(np.linalg.norm(self.end - self.start))

    @property
    def center(self):
        return (self.start + self.end) / 2


@dataclass
class NeonLight:
    """One of the soft lights that wash the road around the car in the kit's colour: a spot at
    `position` shining towards `direction` (see plan_lights)."""
    side: str
    position: np.ndarray     # Root space
    direction: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, -1.0]))
    along: np.ndarray = field(default_factory=lambda: np.array([0.0, 1.0, 0.0]))   # the tube's direction
    intensity: float = 0.0   # lumen at brightness 1, before the colour's gain
    radius: float = 2.0      # m: how far the light reaches
    source_radius: float = 0.2
    height: float = 0.0      # m above the road


@dataclass
class NeonKit:
    tubes: list
    lights: list
    colors: list             # [(key, label, (r, g, b))]
    default: str             # a colour key, or "off"
    brightness: float = 1.0
    tube_radius: float = TUBE_RADIUS
    spread: float = 2.0      # m: how far from the car the glow reaches
    visible_tubes: bool = False   # fit the LED tubes as glowing meshes (GTA's neon is light only)
    road: dict = field(default_factory=dict)   # axle data: the road height under the car (_ground)
    warnings: list = field(default_factory=list)

    def color(self, key):
        for c in self.colors:
            if c[0] == key:
                return c
        return None

    def summary(self):
        hs = [l.height for l in self.lights]
        return {
            "tubes": {t.side: {"length": round(t.length, 3), "center": [round(float(x), 3) for x in t.center],
                               "clearance": round(t.clearance, 3), "source": t.source} for t in self.tubes},
            "lights": len(self.lights),
            "light_height": [round(min(hs), 3), round(max(hs), 3)] if hs else None,
            "colors": [c[0] for c in self.colors],
            "default": self.default,
            "brightness": self.brightness,
            "spread": self.spread,
            "tubes_visible": self.visible_tubes,
        }


# ----------------------------------------------------------------------------- colours
def color_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")[:24] or "custom"


def parse_hex(s):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(s).strip())
    if not m:
        return None
    v = m.group(1)
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def resolve_colors(spec) -> list:
    """'gta' (default), or a list of GTA colour names / keys / {"name", "color": "#rrggbb"} / "#rrggbb"."""
    gta = {k: (k, label, rgb) for k, label, rgb in GTA_NEON_COLORS}
    by_label = {color_key(label): (k, label, rgb) for k, label, rgb in GTA_NEON_COLORS}
    if spec in (None, "gta", "all", ""):
        return list(GTA_NEON_COLORS)
    out, seen = [], set()
    for item in spec if isinstance(spec, (list, tuple)) else [spec]:
        c = None
        if isinstance(item, dict):
            rgb = parse_hex(item.get("color", ""))
            name = str(item.get("name") or item.get("label") or "").strip()
            if rgb and name:
                c = (color_key(name), name, rgb)
        else:
            k = color_key(item)
            c = gta.get(k) or by_label.get(k)
            if c is None:
                rgb = parse_hex(item)
                if rgb:
                    c = (f"c{''.join(f'{x:02x}' for x in rgb)}", "#" + "".join(f"{x:02X}" for x in rgb), rgb)
        if c is None:
            raise ValueError(f"neon colour {item!r}: use a GTA colour ({', '.join(k for k, _, _ in GTA_NEON_COLORS)}), "
                             "'#rrggbb' or {\"name\", \"color\"}")
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    if not out:
        raise ValueError("neon colours: the list is empty")
    if len(out) > MAX_COLORS:
        raise ValueError(f"neon colours: at most {MAX_COLORS}")
    return out


# ----------------------------------------------------------------------------- underside
class Underside:
    """Lowest body surface over a ground-plan grid (Root space)."""

    def __init__(self, tris: np.ndarray, cell=0.03, seed=7):
        self.cell = cell
        if len(tris) == 0:
            self.ok = False
            return
        self.ok = True
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
        per = cell * cell / 6.0
        n = np.ceil(area / per).astype(np.int64)
        budget = 3_000_000                  # samples: big flat panels need many, keep memory bounded
        if n.sum() > budget:
            n = np.maximum(1, (n * (budget / n.sum())).astype(np.int64))
        idx = np.repeat(np.arange(len(tris)), n)
        rng = np.random.default_rng(seed)
        u = rng.random(len(idx))
        v = rng.random(len(idx))
        flip = u + v > 1
        u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
        pts = a[idx] + (b[idx] - a[idx]) * u[:, None] + (c[idx] - a[idx]) * v[:, None]
        pts = np.concatenate([pts, a, b, c])
        self.x0, self.y0 = float(pts[:, 0].min()), float(pts[:, 1].min())
        ix = ((pts[:, 0] - self.x0) / cell).astype(np.int64)
        iy = ((pts[:, 1] - self.y0) / cell).astype(np.int64)
        self.nx, self.ny = int(ix.max()) + 1, int(iy.max()) + 1
        h = np.full(self.nx * self.ny, np.inf)
        np.minimum.at(h, ix * self.ny + iy, pts[:, 2])
        self.h = h.reshape(self.nx, self.ny)

    def at(self, x, y, reach=1):
        """Lowest surface within `reach` cells of (x, y); None when nothing is above that spot."""
        if not self.ok:
            return None
        i = int((x - self.x0) / self.cell)
        j = int((y - self.y0) / self.cell)
        i0, i1 = max(0, i - reach), min(self.nx, i + reach + 1)
        j0, j1 = max(0, j - reach), min(self.ny, j + reach + 1)
        if i0 >= i1 or j0 >= j1:
            return None
        v = float(self.h[i0:i1, j0:j1].min())
        return None if math.isinf(v) else v


def body_triangles(vehicle) -> np.ndarray:
    """Root-space triangles of the parts that make up the underside (not wheels, glass or lights)."""
    out = []
    for name, part in vehicle.parts.items():
        if part.kind in ("wheel", "tire", "hub", "glass", "lights", "steering", "window", "neon"):
            continue
        frame = vehicle.part_frames.get(name)
        if frame is None:
            t, r = np.zeros(3), np.eye(3)
        else:
            t, r = np.asarray(frame[0], np.float64), np.asarray(frame[1], np.float64)
        for s in part.submeshes:
            if s.lod != 1 or not len(s.indices):
                continue
            p = s.positions.astype(np.float64) @ r.T + t
            out.append(p[s.indices.reshape(-1, 3)])
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


# ----------------------------------------------------------------------------- layout
MAX_MOUNT = 0.50         # m: a tube never hangs higher than this above the road (it would float)
SIDE_INSET = 0.10        # m in from the bottom edge of the body for measured side tubes


def _axles(vehicle):
    f, b = vehicle.axles.get("front"), vehicle.axles.get("back") or vehicle.axles.get("front")
    if f is None:
        return None
    pf, pb = f.positions, b.positions
    yf = float(np.mean([p[1] for p in pf.values()])) if pf else 1.3
    yb = float(np.mean([p[1] for p in pb.values()])) if pb else -1.3
    inner_f = min(abs(p[0]) for p in pf.values()) - f.width / 2 if pf else 0.7
    inner_b = min(abs(p[0]) for p in pb.values()) - b.width / 2 if pb else 0.7
    return {"yf": yf, "yb": yb, "rf": f.radius, "rb": b.radius,
            "inner_f": max(0.15, inner_f), "inner_b": max(0.15, inner_b)}


def _ground(ax, y):
    """Road height under Root-space y (front and rear tyres can differ in size)."""
    if abs(ax["yf"] - ax["yb"]) < 1e-6:
        return -ax["rf"]
    t = min(1.0, max(0.0, (ax["yf"] - y) / (ax["yf"] - ax["yb"])))
    return -(ax["rf"] + (ax["rb"] - ax["rf"]) * t)


def _bottom_halfwidth(under, ax, y, xmax):
    """How far out (|x|) the low part of the body reaches at y: the outermost spot with body
    surface no higher than MAX_MOUNT above the road, on the narrower side."""
    g = _ground(ax, y)
    out = []
    for sign in (-1.0, 1.0):
        best = None
        x = 0.0
        while x <= xmax:
            h = under.at(sign * x, y, reach=0)
            if h is not None and h - g <= MAX_MOUNT:
                best = x
            x += under.cell
        out.append(best)
    if any(v is None for v in out):
        return None
    return min(out)


def _fit(under, ax, line, radius, gap, clearance, why, side):
    """Place a tube on `line(t)` (t in 0..1, x/y only), hanging just under the body; trim the ends
    where it cannot fit.  Returns (start, end) Root-space points, or None (reason appended to why)."""
    n = 41
    ts = np.linspace(0.0, 1.0, n)
    xy = np.array([line(t) for t in ts])
    hs = [under.at(x, y) for x, y in xy]
    ok = np.array([h is not None for h in hs])
    if ok.sum() < n * 0.6:
        why.append(f"neon {side}: no body above most of the tube")
        return None
    h = np.array([v if v is not None else np.nan for v in hs])
    g = max(_ground(ax, y) for _, y in xy)
    # the tube hangs from the floor: below most of it, ignoring isolated low bits (fins, pipes)
    top = float(np.nanpercentile(h, 15)) - gap
    if top - g > MAX_MOUNT:
        why.append(f"neon {side}: the body there is {top - g:.2f} m above the road; a tube would float")
        return None
    zc = top - radius
    floor = g + clearance + radius
    if zc < floor:
        if zc < floor - 0.015:
            why.append(f"neon {side}: the car sits very low; the tube is only {clearance * 1000:.0f} mm off the road")
        zc = floor
    # trim the ends where the body comes down lower than the tube (skirts, bumpers, wheel arches)
    fits = ok & ~(h < zc + radius + 0.002)
    if fits.sum() < 2:
        why.append(f"neon {side}: does not fit under the body")
        return None
    good = np.flatnonzero(fits)
    runs, s0 = [], good[0]
    for a, b in zip(good[:-1], good[1:]):
        if b != a + 1:
            runs.append((s0, a))
            s0 = b
    runs.append((s0, good[-1]))
    i0, i1 = max(runs, key=lambda r: r[1] - r[0])
    if (i1 - i0) / (n - 1) < 0.35:
        why.append(f"neon {side}: only {100 * (i1 - i0) / (n - 1):.0f}% of the tube fits under the body")
        return None
    return np.array([xy[i0][0], xy[i0][1], zc]), np.array([xy[i1][0], xy[i1][1], zc])


def plan_neon(vehicle, neon_bones: dict, conf: dict) -> NeonKit | None:
    """neon_bones: {'neon_l': (x, y, z) Root space, ...} from the GTA model (may be empty).
    conf: the profile's neon section."""
    if not conf.get("enabled", True):
        return None
    warnings = []
    ax = _axles(vehicle)
    if ax is None:
        vehicle.warnings.append("neon: the car has no wheels to measure from; no neon kit")
        return None
    under = Underside(body_triangles(vehicle))
    if not under.ok:
        vehicle.warnings.append("neon: no body geometry; no neon kit")
        return None
    radius = TUBE_RADIUS
    gap = float(conf.get("gap", GAP))
    clearance = MIN_CLEARANCE
    lift = float(conf.get("height_offset", 0.0))      # + raises the tubes, - lowers them
    sides = [s for s in SIDES if s in (conf.get("sides") or SIDES)]
    bb = vehicle.bbox
    xmax = max(abs(bb[0][0]), abs(bb[1][0])) + 0.05
    tubes = []

    # side tubes along the sills, between the wheels
    y_hi = ax["yf"] - ax["rf"] - WHEEL_MARGIN
    y_lo = ax["yb"] + ax["rb"] + WHEEL_MARGIN
    for side, sign in (("left", -1.0), ("right", 1.0)):
        if side not in sides:
            continue
        if y_hi - y_lo < 0.4:
            warnings.append(f"neon {side}: the wheelbase is too short for a side tube")
            continue
        bone = neon_bones.get(BONE_OF[side])
        if bone is not None and 0.2 < abs(bone[0]) < 1.6:
            x0 = abs(float(bone[0]))
            src = f"GTA bone {BONE_OF[side]}"
        else:
            hw = [_bottom_halfwidth(under, ax, y_lo + (y_hi - y_lo) * f, xmax) for f in np.linspace(0.1, 0.9, 7)]
            hw = [v for v in hw if v is not None]
            if not hw:
                warnings.append(f"neon {side}: no low body between the wheels")
                continue
            x0 = max(0.15, float(np.percentile(hw, 20)) - SIDE_INSET)
            src = "measured"
        placed, why = None, []
        for step in range(10):          # move inwards until the body covers the tube
            xi = sign * max(0.1, x0 - 0.04 * step)
            why = []
            placed = _fit(under, ax, lambda t, xi=xi: (xi, y_lo + (y_hi - y_lo) * t), radius, gap, clearance, why, side)
            if placed:
                if step:
                    src += f", moved {0.04 * step * 100:.0f} cm in"
                break
        if not placed:
            warnings.extend(why or [f"neon {side}: does not fit"])
            continue
        p0, p1 = placed                 # side tubes run front to back
        tubes.append(NeonTube(side, p1 if p1[1] > p0[1] else p0, p0 if p1[1] > p0[1] else p1, src))

    # front and back tubes across the car: under the bumper, else between the wheels
    for side, sign in (("front", 1.0), ("back", -1.0)):
        if side not in sides:
            continue
        bone = neon_bones.get(BONE_OF[side])
        y_axle = ax["yf"] if side == "front" else ax["yb"]
        r = ax["rf"] if side == "front" else ax["rb"]
        inner = ax["inner_f"] if side == "front" else ax["inner_b"]
        half_bumper = max(0.2, min(ax["inner_f"], ax["inner_b"], xmax - 0.15) - 0.10)
        tries = []
        if bone is not None and sign * (float(bone[1]) - y_axle) > 0.15:
            tries.append((float(bone[1]), half_bumper, f"GTA bone {BONE_OF[side]}", 10))
        tries.append((y_axle + sign * (r + 0.16), half_bumper, "under the " + ("nose" if side == "front" else "tail"), 10))
        # no overhang to speak of: across the car on the axle line, between the tyres
        # (a steered tyre does not move inwards on its own centre line)
        tries.append((y_axle, inner - WHEEL_MARGIN, "between the " + ("front" if side == "front" else "rear") + " wheels", 1))
        placed, why = None, []
        for y0, half, src0, steps in tries:
            if half < 0.2:
                why.append(f"neon {side}: no room between the wheels")
                continue
            for step in range(steps):   # towards the middle until the bumper covers it
                yi = y0 - sign * 0.04 * step
                if steps > 1 and sign * (yi - y_axle) < r + 0.04:
                    break
                w = []
                placed = _fit(under, ax, lambda t, yi=yi, half=half: (-half + 2 * half * t, yi),
                              radius, gap, clearance, w, side)
                if placed:
                    src = src0 + (f", moved {0.04 * step * 100:.0f} cm in" if step else "")
                    break
                why = w
            if placed:
                break
        if not placed:
            warnings.extend(why or [f"neon {side}: no room"])
            continue
        tubes.append(NeonTube(side, placed[0], placed[1], src))

    if lift:
        for t in tubes:
            t.start = t.start + np.array([0, 0, lift])
            t.end = t.end + np.array([0, 0, lift])
    for t in tubes:
        g = max(_ground(ax, float(t.start[1])), _ground(ax, float(t.end[1])))
        t.clearance = float(min(t.start[2], t.end[2]) - radius - g)
    if not tubes:
        vehicle.warnings.extend(warnings or ["neon: no tube fits under this car"])
        return None

    spread = float(conf.get("spread") or SPREAD)
    lights = plan_lights(tubes, under, ax, spread)

    colors = resolve_colors(conf.get("colors", "gta"))
    default = color_key(conf.get("default") or "off")
    if default == "on":                 # the kit lit in its first colour (White for the GTA palette)
        default = colors[0][0]
    if default != "off" and not any(c[0] == default for c in colors):
        warnings.append(f"neon: default colour '{conf.get('default')}' is not in the kit; the car starts without neon")
        default = "off"
    kit = NeonKit(tubes=tubes, lights=lights, colors=colors, default=default,
                  brightness=float(conf.get("brightness", 1.0)), tube_radius=radius, spread=spread, road=ax,
                  visible_tubes=bool(conf.get("tubes", False)), warnings=warnings)
    vehicle.warnings.extend(warnings)
    return kit


def fit_kit(vehicle, neon_bones: dict, conf: dict) -> NeonKit | None:
    """Plan the kit and fit it: vehicle.neon = the kit; its tubes become a glowing mesh part only
    when the profile wants them drawn (neon.tubes) -- otherwise the kit is light only, like GTA V's."""
    kit = plan_neon(vehicle, neon_bones, conf)
    if kit is not None:
        if kit.visible_tubes and add_neon_part(vehicle, kit) is None:
            kit.visible_tubes = False
        vehicle.neon = kit
    return kit


# ----------------------------------------------------------------------------- the glow on the road
# A real LED kit lights the road the way any light under a car does: the strips sit a hand's width
# off the road, their light bounces between the road and the floor pan and leaves from under the
# sills, so the road around the car is washed in colour -- brightest at the sills, fading smoothly
# a metre or two out -- and dry asphalt scatters it, nothing mirrors it.
#
# What does NOT look like that (both tried):
#   * a light close to the road (one or two points just under each tube; then one capsule per
#     tube like the game's own Tyger Claws underglow): light that low meets the road edge-on, so it
#     draws a hard bright band along each sill and leaves the road outside dark;
#   * a shiny light: on Cyberpunk's wet, glossy roads a tube-shaped light also shows up as a
#     mirrored streak -- a line.
# So the kit uses, per tube, a row of soft lights standing in for that bounced, scattered light:
#   * every LIGHT_SPACING metres along the tube, moved out to just inside the sill's edge (where
#     the light leaves from under the car) and LIGHT_HEIGHT up -- inside the body, which they never
#     light: they are on the world light channel only, like the game's underglow (road, kerbs and
#     walls, not the car or its cabin).  A source that high lights a wide, smooth pool instead of
#     a line, and suspension sag cannot sink it into the road;
#   * spots aimed at the road, TILT degrees out from under the car, cone a little short of a half
#     sphere (nothing above them is lit, and a wall beside the car only near the ground);
#   * the falloff of the game's headlights (linear over `spread` metres) and no specular at all,
#     the way the game's own fake bounce lights are set up: a soft glow, no streaks on wet roads.
# `brightness` scales them, `spread` is how far the glow reaches.
LIGHT_SPACING = 0.6        # m between the lights along a tube
LIGHT_FLUX = 400.0         # lumen per metre of tube at brightness 1
LIGHT_HEIGHT = 0.40        # m above the road ...
LIGHT_OVER_TUBE = 0.10     # ... and at least this far above the tube
EDGE_INSET = 0.12          # m in from the sill's edge
EDGE_MAX_SHIFT = 0.6       # m: never moved further than this out from the tube
EDGE_BODY = 0.50           # m: body this low over the road counts as the sill
SPREAD = 2.0               # m: the lights' reach (profile neon.spread)
TILT = 10.0                # degrees out from straight down
CONE_INNER = 140.0         # degrees, full cone: full strength inside
CONE_OUTER = 168.0         # nothing outside: the light stays 6 degrees under the horizon
SOURCE_RADIUS = 0.2        # m: a soft source
LIGHT_CHANNEL = "LC_ChannelWorld"
OUTWARD = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "front": (0.0, 1.0), "back": (0.0, -1.0)}
# dark colours get more light so every colour reads on the road: the eye and the game's tone
# mapping see a watt of blue as far dimmer than a watt of white (relative luminance 0.08 vs 1)
GAIN_REF = 0.30
GAIN_MAX = 2.5


def _shine(side):
    """Unit vector a light under `side`'s tube points along: down, tilted out from under the car."""
    ox, oy = OUTWARD[side]
    t = math.radians(TILT)
    return np.array([ox * math.sin(t), oy * math.sin(t), -math.cos(t)])


def _sill(under, ax, p, side):
    """How far out from `p` (Root space) towards `side` the low body reaches: the last spot of
    body no higher than EDGE_BODY over the road before it ends (a gap of more than 5 cm)."""
    if under is None:
        return 0.0
    ox, oy = OUTWARD[side]
    g = _ground(ax, float(p[1]))
    last, gap, step = 0.0, 0.0, 0.01
    s = 0.0
    while s <= EDGE_MAX_SHIFT + EDGE_INSET + 1e-9:
        h = under.at(float(p[0]) + ox * s, float(p[1]) + oy * s, reach=0)
        if h is not None and h - g <= EDGE_BODY:
            last, gap = s, 0.0
        else:
            gap += step
            if gap > 0.05:
                break
        s += step
    return last


def plan_lights(tubes, under, ax, spread=SPREAD) -> list:
    lights = []
    for t in tubes:
        n = max(1, int(math.ceil(t.length / LIGHT_SPACING - 1e-9)))
        flux = LIGHT_FLUX * t.length / n
        along = (t.end - t.start) / max(t.length, 1e-6)
        ox, oy = OUTWARD[t.side]
        for i in range(n):
            p = t.start + (t.end - t.start) * ((i + 0.5) / n)
            out = min(EDGE_MAX_SHIFT, max(0.0, _sill(under, ax, p, t.side) - EDGE_INSET))
            g = _ground(ax, float(p[1]))
            z = max(g + LIGHT_HEIGHT, float(p[2]) + LIGHT_OVER_TUBE)
            lights.append(NeonLight(t.side, np.array([float(p[0]) + ox * out, float(p[1]) + oy * out, z]),
                                    direction=_shine(t.side), along=np.asarray(along, np.float64),
                                    intensity=round(flux, 1), radius=float(spread),
                                    source_radius=SOURCE_RADIUS, height=float(z - g)))
    return lights


def _linear(c):
    c = float(c) / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def glow_gain(rgb) -> float:
    lum = 0.2126 * _linear(rgb[0]) + 0.7152 * _linear(rgb[1]) + 0.0722 * _linear(rgb[2])
    return float(min(GAIN_MAX, max(1.0, math.sqrt(GAIN_REF / max(lum, 1e-4)))))


def light_lumen(kit, light, rgb) -> float:
    """What a light component gets: its share of the tube, the profile's brightness, the colour's
    gain."""
    return round(light.intensity * kit.brightness * glow_gain(rgb), 1)


def road_height(kit, y):
    """Road height (Root space) under y, which may be an array."""
    y = np.asarray(y, np.float64)
    ax = kit.road
    if not ax:
        return np.full(y.shape, -0.35)
    if abs(ax["yf"] - ax["yb"]) < 1e-6:
        return np.full(y.shape, -ax["rf"])
    t = np.clip((ax["yf"] - y) / (ax["yf"] - ax["yb"]), 0.0, 1.0)
    return -(ax["rf"] + (ax["rb"] - ax["rf"]) * t)


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def ground_glow(kit, x, y, rgb=(255, 255, 255)):
    """Light the kit throws on the road at Root-space (x, y) (arrays), in the game's terms for these
    lights (spots, linear falloff): E = I/4pi * (1 - r/R) * cos(incidence) * cone(angle).  Lux,
    about: the donor's headlights (300 lm, the same falloff) put ~7 on the road 2 m ahead of the
    car and ~2 at 5 m."""
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    z = road_height(kit, y)
    e = np.zeros(x.shape)
    c_out, c_in = math.cos(math.radians(CONE_OUTER / 2)), math.cos(math.radians(CONE_INNER / 2))
    for l in kit.lights:
        dx, dy, dz = x - l.position[0], y - l.position[1], z - l.position[2]
        r = np.maximum(np.sqrt(dx * dx + dy * dy + dz * dz), 1e-6)
        cos_inc = np.clip(-dz / r, 0.0, 1.0)                       # the road faces up
        d = l.direction
        cone = _smoothstep(c_out, c_in, (dx * d[0] + dy * d[1] + dz * d[2]) / r)
        fall = np.clip(1.0 - r / l.radius, 0.0, 1.0)
        e += light_lumen(kit, l, rgb) / (4 * math.pi) * fall * cos_inc * cone
    return e


# ----------------------------------------------------------------------------- geometry
def tube_geometry(kit: NeonKit, offset=(0.0, 0.0, 0.0), segments=8):
    """One mesh for every tube: positions, normals, tangents, uv0, indices (in the frame whose
    origin is `offset` in Root space)."""
    P, N, T, UV, I = [], [], [], [], []
    base = 0
    off = np.asarray(offset, np.float64)
    r = kit.tube_radius
    for tube in kit.tubes:
        a, b = tube.start - off, tube.end - off
        axis = b - a
        L = float(np.linalg.norm(axis))
        if L < 1e-4:
            continue
        d = axis / L
        up = np.array([0.0, 0.0, 1.0])
        u = np.cross(d, up)
        if np.linalg.norm(u) < 1e-6:
            u = np.array([1.0, 0.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(u, d)
        ring = []
        for k in range(segments):
            ang = 2 * math.pi * k / segments
            ring.append(math.cos(ang) * u + math.sin(ang) * w)
        ring = np.array(ring)
        # side wall: two rings
        for end, v in ((a, 0.0), (b, 1.0)):
            for k in range(segments):
                P.append(end + r * ring[k])
                N.append(ring[k])
                T.append(np.append(d, 1.0))
                UV.append((k / segments, v * L))
        for k in range(segments):
            k2 = (k + 1) % segments
            i0, i1 = base + k, base + k2
            j0, j1 = base + segments + k, base + segments + k2
            I += [i0, j0, i1, i1, j0, j1]
        base += 2 * segments
        # caps
        for end, nrm in ((a, -d), (b, d)):
            c = base
            P.append(end)
            N.append(nrm)
            T.append(np.append(u, 1.0))
            UV.append((0.5, 0.5))
            for k in range(segments):
                P.append(end + r * ring[k])
                N.append(nrm)
                T.append(np.append(u, 1.0))
                UV.append((0.5 + 0.5 * math.cos(2 * math.pi * k / segments), 0.5 + 0.5 * math.sin(2 * math.pi * k / segments)))
            for k in range(segments):
                k2 = (k + 1) % segments
                if nrm @ d > 0:
                    I += [c, c + 1 + k, c + 1 + k2]
                else:
                    I += [c, c + 1 + k2, c + 1 + k]
            base += segments + 1
    if not P:
        return None
    P = np.asarray(P, np.float32)
    N = np.asarray(N, np.float32)
    T = np.asarray(T, np.float32)
    UV = np.asarray(UV, np.float32)
    I = np.asarray(I, np.uint32)
    # outward winding: counter-clockwise seen from outside (flip triangles whose normal points in)
    tri = I.reshape(-1, 3)
    fn = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
    vn = N[tri[:, 0]] + N[tri[:, 1]] + N[tri[:, 2]]
    bad = (fn * vn).sum(1) < 0
    tri[bad] = tri[bad][:, [0, 2, 1]]
    return P, N, T, UV, tri.reshape(-1).astype(np.uint32)


def add_neon_part(vehicle, kit: NeonKit, name="neon_01"):
    """Add the tubes to the IR as one mesh part hanging from body_01 (material key 'neon')."""
    from .ir import Material, Part, SubMesh
    frame = vehicle.part_frames.get("body_01")
    t = np.asarray(frame[0], np.float64) if frame is not None else np.zeros(3)
    r = np.asarray(frame[1], np.float64) if frame is not None else np.eye(3)
    geo = tube_geometry(kit)
    if geo is None:
        return None
    P, N, T, UV, I = geo
    P = ((P.astype(np.float64) - t) @ r).astype(np.float32)            # Root -> body frame
    N = (N.astype(np.float64) @ r).astype(np.float32)
    T = np.concatenate([T[:, :3].astype(np.float64) @ r, T[:, 3:4]], 1).astype(np.float32)
    c = kit.color(kit.default) or kit.colors[0]
    vehicle.materials["neon"] = Material(key="neon", kind="neon", color=tuple(x / 255.0 for x in c[2]) + (1.0,),
                                         emissive_ev=6.0, roughness=0.3)
    vehicle.parts[name] = Part(name=name, mesh_name=name, slot=None, parent="body_01", kind="neon",
                               submeshes=[SubMesh(material="neon", positions=P, normals=N, tangents=T, uv0=UV,
                                                  uv1=None, colors=None, indices=I, lod=1)],
                               physical=False)
    vehicle.part_frames[name] = (tuple(t), r)
    return vehicle.parts[name]
