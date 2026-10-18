"""Conversion profile: everything a user (or the AI agent) can override about one car.

A profile is plain JSON.  Missing keys mean "automatic", so ``{}`` is a valid profile and gives
the same result as the bare command line.  ``normalize`` fills defaults and checks every value;
``merge`` applies a partial profile (a patch) on top of another, ``null`` removing an entry.

    {
      "name": "i8rpncs",                  record / resource id (default: the model name)
      "display_name": "BMW i8 Roadster",  shown in game
      "model": null,                      which car of a multi-car resource
      "donor": "auto",                    Cyberpunk donor (see cp77/donor.py)
      "parts": {
        "lids": "auto",                   auto: GTA bonnet/boot follow the donor's front/rear lid
                                          semantic: bonnet->hood_a, boot->trunk_a whatever the layout
                                          swap: always exchange them
        "bone_roles": {"handle_dside_f": "door:fl", "misc_w": "skip"},
        "extras": {"extra_1": true},      GTA extras on/off (default: required ones, and those the car
                                          needs to be whole -- a roof or a body only an extra carries)
        "mods": {"mycar_bodykit": true}   carcols mod-kit parts on/off (default: the ones the car needs to be
                                          whole, one per slot; alternatives and add-ons stay off)
      },
      "lights": {"head_fl_0": {"color": [255, 250, 220], "type": "Head", "skip": false}},
      "handling": {
        "tune": "sport",                  stock (GTA's handling mapped onto the donor) | sport | track (cp77/tweak.py TUNES)
        "drive": "auto",                  auto | rwd | fwd | awd
        "gears": null,                    4..7
        "mass": null,                     kg
        "steer": null,                    max wheel angle, degrees
        "multipliers": {"torque": 1, "top_speed": 1, "brake": 1, "grip": 1, "spring": 1, "drag": 1}
      },
      "sound": {"engine": "auto"},        auto (from GTA's sound) | donor | a sound name (cp77/sound.py)
      "paint": {"primary": "auto"},       auto (carvariations) | "#rrggbb"
      "chime": {"enabled": false, "file": null, "volume": 0.8, "range": 12.0},
      "neon": {                           GTA V style underglow kit (one appearance per colour)
        "enabled": true,
        "sides": ["left", "right", "front", "back"],
        "colors": "gta",                  the 13 LS Customs colours, or a list of names / "#rrggbb"
        "default": "on",                  colour the car spawns with: "on" = the kit's first colour,
                                          "off" = kit fitted, lights off (switch in game with /neon)
        "brightness": 1.0,                0.2..4: how strongly the tubes light the road
        "spread": 2.0,                    metres the glow reaches from the car (0.5..5)
        "tubes": false,                   true = the LED tubes show as glowing strips (GTA's neon: light only)
        "height_offset": 0.0              metres: + raises the tubes, - lowers them (-0.1..0.1)
      },
      "seats": {                          how the player sits (see model/seats.py)
        "template": "auto",               auto (the donor's; GTA LOW layouts too low for it: sport1) | sport1 | sport2 | standard2 | standard25 | standard3
        "lower": 0.0                      extra metres to lower every seat (0..0.2)
      }
    }
"""
from __future__ import annotations

import copy
import json
import os
import re

SCHEMA = 1

DEFAULTS = {
    "schema": SCHEMA,
    "name": None,
    "display_name": None,
    "model": None,
    "donor": "auto",
    "parts": {"lids": "auto", "bone_roles": {}, "extras": {}, "mods": {}},
    "lights": {},
    "handling": {
        "tune": "sport", "drive": "auto", "gears": None, "mass": None, "steer": None, "wheel_offset": None,
        "multipliers": {"torque": 1.0, "top_speed": 1.0, "brake": 1.0, "grip": 1.0, "spring": 1.0, "drag": 1.0},
    },
    "sound": {"engine": "auto"},
    "paint": {"primary": "auto"},
    "chime": {"enabled": False, "file": None, "volume": 0.8, "range": 12.0},
    "neon": {"enabled": True, "sides": ["left", "right", "front", "back"], "colors": "gta", "default": "on",
             "brightness": 1.0, "spread": 2.0, "tubes": False, "height_offset": 0.0},
    "seats": {"template": "auto", "lower": 0.0},
    "notes": "",
}
NEON_SIDES = ("left", "right", "front", "back")

SIDES = ("fl", "fr", "bl", "br")
ROLE_VALUES = (["auto", "body", "interior", "skip", "glass:front", "glass:rear", "lid:bonnet", "lid:boot", "steering"]
               + [f"door:{s}" for s in SIDES] + [f"window:{s}" for s in SIDES])
LIGHT_TYPES = ("Head", "Brake", "LeftBlinker", "RightBlinker", "Reverse", "Interior", "Utility")
MULT_RANGE = (0.25, 4.0)
NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,57}$")


class ProfileError(ValueError):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def default_profile() -> dict:
    return copy.deepcopy(DEFAULTS)


def merge(base: dict, patch: dict) -> dict:
    """Deep merge: dicts merge key by key, anything else replaces; None deletes a key of a
    free-form map (bone_roles, extras, lights) and resets a fixed setting to its default."""
    out = copy.deepcopy(base)

    def rec(dst, src, defaults):
        for k, v in src.items():
            d = defaults.get(k) if isinstance(defaults, dict) else None
            if v is None:
                if isinstance(defaults, dict) and k in defaults:
                    dst[k] = copy.deepcopy(defaults[k])
                else:
                    dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                rec(dst[k], v, d if isinstance(d, dict) else {})
            else:
                dst[k] = copy.deepcopy(v)

    rec(out, patch or {}, DEFAULTS)
    return out


def _num(errors, where, v, lo, hi, allow_none=True):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errors.append(f"{where}: expected a number, got {v!r}")
        return None
    if not lo <= v <= hi:
        errors.append(f"{where}: {v} is outside {lo}..{hi}")
        return None
    return float(v)


def parse_color(v):
    """'#rrggbb' or [r, g, b] (0..255 or 0..1) -> (r, g, b) floats 0..1, or None."""
    if isinstance(v, str):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", v.strip())
        if not m:
            return None
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        # 0..255, unless every value is within 0..1 and one has a fraction (then 0..1)
        if all(0 <= x <= 1 for x in v) and any(not float(x).is_integer() for x in v):
            return tuple(float(x) for x in v)
        if all(0 <= x <= 255 for x in v):
            return tuple(float(x) / 255.0 for x in v)
    return None


def to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def normalize(p: dict, *, donors=None, sounds=None) -> dict:
    """Defaults filled in and every value checked; raises ProfileError listing all problems."""
    errors = []
    p = merge(DEFAULTS, p or {})
    unknown = set(p) - set(DEFAULTS)
    if unknown:
        errors.append("unknown settings: " + ", ".join(sorted(unknown)))
    if p.get("schema") != SCHEMA:
        errors.append(f"schema {p.get('schema')!r} is not supported (expected {SCHEMA})")
    if p["name"] is not None and (not isinstance(p["name"], str) or not NAME_RX.match(p["name"])):
        errors.append("name: 3-58 characters of a-z, 0-9 and _, starting with a letter")
    if p["display_name"] is not None and (not isinstance(p["display_name"], str) or not p["display_name"].strip()
                                          or len(p["display_name"]) > 80):
        errors.append("display_name: 1-80 characters")
    if p["model"] is not None and not isinstance(p["model"], str):
        errors.append("model: a model name")
    if donors is not None and p["donor"] != "auto" and p["donor"] not in donors:
        errors.append(f"donor: '{p['donor']}' is not one of auto, {', '.join(sorted(donors))}")

    parts = p["parts"]
    if parts["lids"] not in ("auto", "semantic", "swap"):
        errors.append("parts.lids: auto, semantic or swap")
    roles = parts["bone_roles"]
    if not isinstance(roles, dict):
        errors.append("parts.bone_roles: a map of GTA bone name -> role")
        parts["bone_roles"] = {}
    else:
        clean = {}
        for k, v in roles.items():
            if v not in ROLE_VALUES:
                errors.append(f"parts.bone_roles.{k}: '{v}' is not one of {', '.join(ROLE_VALUES)}")
            elif v != "auto":
                clean[str(k).lower()] = v
        parts["bone_roles"] = clean
    extras = parts["extras"]
    if not isinstance(extras, dict) or not all(isinstance(x, bool) for x in extras.values()):
        errors.append("parts.extras: a map of extra name -> true/false")
        parts["extras"] = {}
    else:
        parts["extras"] = {str(k).lower(): bool(v) for k, v in extras.items()}
    mods = parts.get("mods", {})
    if not isinstance(mods, dict) or not all(isinstance(x, bool) for x in mods.values()):
        errors.append("parts.mods: a map of mod kit part (model name) -> true/false")
        parts["mods"] = {}
    else:
        parts["mods"] = {str(k).lower(): bool(v) for k, v in mods.items()}

    lights = p["lights"]
    if not isinstance(lights, dict):
        errors.append("lights: a map of light name -> settings")
        p["lights"] = {}
    else:
        for name, s in lights.items():
            if not isinstance(s, dict):
                errors.append(f"lights.{name}: settings object expected")
                continue
            extra = set(s) - {"color", "type", "skip"}
            if extra:
                errors.append(f"lights.{name}: unknown settings {', '.join(sorted(extra))}")
            if "color" in s and parse_color(s["color"]) is None:
                errors.append(f"lights.{name}.color: '#rrggbb' or [r, g, b]")
            if "type" in s and s["type"] not in LIGHT_TYPES:
                errors.append(f"lights.{name}.type: one of {', '.join(LIGHT_TYPES)}")
            if "skip" in s and not isinstance(s["skip"], bool):
                errors.append(f"lights.{name}.skip: true or false")

    h = p["handling"]
    extra = set(h) - set(DEFAULTS["handling"])
    if extra:
        errors.append("handling: unknown settings " + ", ".join(sorted(extra)))
    if h["drive"] not in ("auto", "rwd", "fwd", "awd"):
        errors.append("handling.drive: auto, rwd, fwd or awd")
    if h.get("tune") not in ("stock", "sport", "track"):
        errors.append("handling.tune: stock, sport or track")
        h["tune"] = "sport"
    g = _num(errors, "handling.gears", h["gears"], 4, 7)
    h["gears"] = int(g) if g is not None else None
    h["mass"] = _num(errors, "handling.mass", h["mass"], 500, 4000)
    h["steer"] = _num(errors, "handling.steer", h["steer"], 25, 70)
    wo = h.get("wheel_offset")
    if wo is not None:
        if not isinstance(wo, dict) or set(wo) - {"front", "back"}:
            errors.append('handling.wheel_offset: {"front": metres, "back": metres}')
            h["wheel_offset"] = None
        else:
            h["wheel_offset"] = {k: _num(errors, f"handling.wheel_offset.{k}", v, -0.2, 0.2, allow_none=False)
                                 for k, v in wo.items()}
    mult = h["multipliers"]
    if not isinstance(mult, dict):
        errors.append("handling.multipliers: a map")
        h["multipliers"] = copy.deepcopy(DEFAULTS["handling"]["multipliers"])
    else:
        bad = set(mult) - set(DEFAULTS["handling"]["multipliers"])
        if bad:
            errors.append("handling.multipliers: unknown " + ", ".join(sorted(bad)))
        for k in DEFAULTS["handling"]["multipliers"]:
            v = _num(errors, f"handling.multipliers.{k}", mult.get(k, 1.0), *MULT_RANGE, allow_none=False)
            mult[k] = 1.0 if v is None else v

    eng = p["sound"].get("engine", "auto")
    if not isinstance(eng, str) or (eng not in ("auto", "donor") and sounds is not None and eng not in sounds):
        errors.append(f"sound.engine: auto, donor or a Cyberpunk sound name (got {eng!r})")
    extra = set(p["sound"]) - {"engine"}
    if extra:
        errors.append("sound: unknown settings " + ", ".join(sorted(extra)))

    prim = p["paint"].get("primary", "auto")
    if prim != "auto":
        rgb = parse_color(prim)
        if rgb is None:
            errors.append("paint.primary: auto or '#rrggbb'")
        else:
            p["paint"]["primary"] = to_hex(rgb)

    c = p["chime"]
    if not isinstance(c.get("enabled"), bool):
        errors.append("chime.enabled: true or false")
    if c.get("file") is not None and not isinstance(c["file"], str):
        errors.append("chime.file: a path to an .mp3 or .wav")
    if c.get("enabled") and not c.get("file"):
        errors.append("chime.enabled needs chime.file")
    if c.get("file") and not c["file"].lower().endswith((".mp3", ".wav")):
        errors.append("chime.file: .mp3 or .wav")
    c["volume"] = _num(errors, "chime.volume", c.get("volume"), 0.0, 1.0, allow_none=False) or 0.0
    c["range"] = _num(errors, "chime.range", c.get("range"), 2.0, 60.0, allow_none=False) or 12.0
    n = p["neon"]
    extra = set(n) - set(DEFAULTS["neon"])
    if extra:
        errors.append("neon: unknown settings " + ", ".join(sorted(extra)))
    if not isinstance(n.get("enabled"), bool):
        errors.append("neon.enabled: true or false")
    sides = n.get("sides")
    if not isinstance(sides, list) or not sides or any(x not in NEON_SIDES for x in sides):
        errors.append("neon.sides: a non-empty list of left, right, front, back")
    else:
        n["sides"] = [x for x in NEON_SIDES if x in sides]
    from .model.neon import color_key, resolve_colors
    colors = None
    try:
        colors = resolve_colors(n.get("colors"))
    except ValueError as e:
        errors.append(str(e))
    d = n.get("default")
    if not isinstance(d, str) or not d.strip():
        errors.append("neon.default: on, off or one of the kit's colours")
    elif colors is not None:
        k = color_key(d)
        if k not in ("off", "on") and not any(c[0] == k for c in colors):
            errors.append(f"neon.default: '{d}' is not in the kit (on, off, {', '.join(c[0] for c in colors)})")
        else:
            n["default"] = k
    n["brightness"] = _num(errors, "neon.brightness", n.get("brightness"), 0.2, 4.0, allow_none=False) or 1.0
    n["spread"] = _num(errors, "neon.spread", n.get("spread"), 0.5, 5.0, allow_none=False) or 2.0
    if not isinstance(n.get("tubes"), bool):
        errors.append("neon.tubes: true or false")
    n["height_offset"] = _num(errors, "neon.height_offset", n.get("height_offset"), -0.1, 0.1, allow_none=False) or 0.0
    st = p["seats"]
    extra = set(st) - set(DEFAULTS["seats"])
    if extra:
        errors.append("seats: unknown settings " + ", ".join(sorted(extra)))
    from .model.seats import TEMPLATES
    if st.get("template") not in ("auto", *TEMPLATES):
        errors.append(f"seats.template: auto or one of {', '.join(TEMPLATES)}")
    st["lower"] = _num(errors, "seats.lower", st.get("lower"), 0.0, 0.2, allow_none=False) or 0.0
    if not isinstance(p.get("notes", ""), str):
        errors.append("notes: text")
    if errors:
        raise ProfileError(errors)
    return p


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ProfileError([f"{path}: a JSON object is expected"])
    return data


def save(profile: dict, path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(profile, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def from_cli(args) -> dict:
    """Profile patch from the convert command's flags (only what was given)."""
    patch = {}
    for k in ("name", "display_name", "model"):
        v = getattr(args, k, None)
        if v:
            patch[k] = v
    if getattr(args, "donor", None):
        patch["donor"] = args.donor
    if getattr(args, "door_chime", None):
        patch["chime"] = {"enabled": True, "file": args.door_chime}
    for k, key in (("chime_volume", "volume"), ("chime_range", "range")):
        v = getattr(args, k, None)
        if v is not None:
            patch.setdefault("chime", {})[key] = v
    if getattr(args, "engine_sound", None):
        patch["sound"] = {"engine": args.engine_sound}
    if getattr(args, "lids", None):
        patch.setdefault("parts", {})["lids"] = args.lids
    neon = getattr(args, "neon", None)
    if neon:
        if neon == "none":
            patch["neon"] = {"enabled": False}
        else:
            patch["neon"] = {"enabled": True, "default": neon}
    return patch
