"""TweakDB records (TweakXL YAML) for the converted vehicle.

The vehicle record inherits from the donor (`$base`), so seating, cameras, audio, AI and UI keep
working, and overrides what is specific to the converted car: entity, appearance, name, wheel
dimensions, and a drive model / engine tuned from the FiveM handling.meta.

GTA handling values of FiveM add-on cars are often extreme (tuned for GTA's physics or simply
exaggerated), so every value is mapped *relative to the donor* and clamped to a sane band.
"""
from __future__ import annotations

import numpy as np

DONOR_REF = {
    # GTA handling values of a car comparable to the donor (sports/super class)
    "fInitialDriveForce": 0.33,
    "fInitialDriveMaxFlatVel": 155.0,
    "fBrakeForce": 1.0,
    "fSuspensionForce": 2.5,
    "fTractionCurveMax": 2.5,
    "fInitialDragCoeff": 7.0,
    "fSteeringLock": 40.0,
}

# Performance tunes, on top of the GTA handling mapped onto the donor.  GTA's numbers are made for
# GTA's physics: mapped one to one onto Cyberpunk's drive model, an SUV or a muscle car came out with
# less grip and weaker brakes than any vanilla car and the top speed of a city hatchback.
#   stock: the mapping alone
#   sport: (default) the converted cars against Cyberpunk's own fast cars -- the Caliburn, the
#          Outlaw, the Turbo-R: more torque (never under 0.40 N m per kg), a higher top speed, brakes
#          and tyres at least the donor's and then some, less body roll (stiffer anti-roll bars, less
#          weight thrown sideways) so the extra grip does not tip a tall car over
#   track: further still
# The profile's per-car multipliers apply after the tune.
TUNES = {
    "stock": {"torque": 1.0, "torque_per_kg": (0.0, 9.0), "top_speed": 1.0, "top_band": (15.0, 120.0),
              "drag": 1.0, "drag_max": 9.0, "brake": 1.0, "brake_min": 0.0, "grip": 1.0, "grip_min": 0.0,
              "chassis": {}, "axle": {}, "com_drop": 0.0},
    "sport": {"torque": 1.3, "torque_per_kg": (0.40, 0.70), "top_speed": 1.2, "top_band": (42.0, 62.0),
              "drag": 0.92, "drag_max": 1.0, "brake": 1.4, "brake_min": 1.0, "grip": 1.1, "grip_min": 1.0,
              "chassis": {"brakingFrictionFactor": 1.6, "turningRollFactor": 0.6, "sideWeightTransferFactor": 0.18,
                          "forwardWeightTransferFactor": 0.16, "wheelTurnMaxAddPerSecond": 110.0},
              "axle": {"swaybarStiffness": 1.5},
              "com_drop": 0.03},
    "track": {"torque": 1.5, "torque_per_kg": (0.50, 0.85), "top_speed": 1.3, "top_band": (46.0, 70.0),
              "drag": 0.85, "drag_max": 1.0, "brake": 1.7, "brake_min": 1.0, "grip": 1.2, "grip_min": 1.05,
              "chassis": {"brakingFrictionFactor": 1.8, "turningRollFactor": 0.4, "sideWeightTransferFactor": 0.14,
                          "forwardWeightTransferFactor": 0.12, "wheelTurnMaxAddPerSecond": 125.0},
              "axle": {"swaybarStiffness": 2.0},
              "com_drop": 0.05},
}
DEFAULT_TUNE = "sport"
DONOR_SWAYBAR = 16.0       # the 911's anti-roll bars (front and back)


# Third person camera presets by body size (the TweakDB records of a vanilla car of that size)
CAMERAS = {
    "sport": "Camera.VehicleTPP_4w_911",                                   # Porsche 911
    "sedan": "Camera.VehicleTPP_4w_Archer_Hella",                          # Archer Hella
    "suv": "Camera.VehicleTPP_v_standard25_mahir_supron_CameraPreset",     # Mahir Supron
    "truck": "Camera.VehicleTPP_4w_Truck_Preset",                          # Thorton Mackinaw
}
CAMERA_VIEWS = ("High_Close", "High_Medium", "High_Far", "High_DriverCombatClose", "High_DriverCombatMedium",
                "High_DriverCombatFar", "Low_Close", "Low_Medium", "Low_Far", "Low_DriverCombatClose",
                "Low_DriverCombatMedium", "Low_DriverCombatFar")


def camera_class(vehicle) -> str:
    """sport / sedan / suv / truck from the body's size: how high the roof is above the road (wheel
    centres are at Z 0) and how long the car is."""
    (x0, y0, z0), (x1, y1, z1) = vehicle.bbox
    radius = max((a.radius for a in getattr(vehicle, "axles", {}).values()), default=0.34)
    height, length = z1 + radius, y1 - y0
    if height >= 2.1 or length >= 5.7:
        return "truck"
    if height >= 1.55:
        return "suv"
    if height < 1.25 and length < 5.0:
        return "sport"
    return "sedan"


def camera_presets(kind: str) -> list:
    return [f"{CAMERAS[kind]}_{v}" for v in CAMERA_VIEWS]


def _clamp(v, lo, hi):
    return float(min(hi, max(lo, v)))


def _f(v):
    s = f"{float(v):.4f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _vec(v):
    return "{ x: %s, y: %s, z: %s }" % tuple(_f(x) for x in v)


class _Yaml:
    def __init__(self):
        self.lines = []

    def record(self, name, fields: dict, comment=None):
        if comment:
            self.lines.append(f"# {comment}")
        self.lines.append(f"{name}:")
        for k, v in fields.items():
            if isinstance(v, list):
                if not v:
                    self.lines.append(f"  {k}: []")
                else:
                    self.lines.append(f"  {k}:")
                    for item in v:
                        self.lines.append(f"    - {item}")
            else:
                self.lines.append(f"  {k}: {v}")
        self.lines.append("")

    def text(self):
        return "\n".join(self.lines) + "\n"


def handling_profile(vehicle, prof, overrides=None) -> dict:
    """Numbers for the drive model, engine and wheels (also written to the conversion report).

    overrides: the profile's "handling" section (drive, gears, mass, steer, multipliers)."""
    t = prof["tweak"]
    o = overrides or {}
    mult = {k: float(v) for k, v in (o.get("multipliers") or {}).items()}
    h = vehicle.handling
    hv = dict(h.values) if h else {}
    g = lambda k: hv.get(k, DONOR_REF.get(k))
    mass = _clamp(o["mass"] if o.get("mass") else hv.get("fMass", t["chassis_mass"]), 500, 4000)
    if not o.get("mass"):
        mass = _clamp(mass, 700, 3500)
    mass_ratio = mass / t["chassis_mass"]

    bias = hv.get("fDriveBiasFront", 0.0)
    layout = "rwd" if bias < 0.1 else ("fwd" if bias > 0.9 else "awd")
    if o.get("drive") in ("rwd", "fwd", "awd"):
        layout = o["drive"]

    tune_name = o.get("tune") or DEFAULT_TUNE
    tune = TUNES.get(tune_name, TUNES[DEFAULT_TUNE])

    accel = _clamp((g("fInitialDriveForce") / DONOR_REF["fInitialDriveForce"]) ** 0.5, 0.75, 1.35)
    torque = t["engine_max_torque"] * accel * mass_ratio * tune["torque"]
    lo, hi = tune["torque_per_kg"]
    torque = _clamp(torque, lo * mass, hi * mass) * mult.get("torque", 1.0)
    speed_f = _clamp(g("fInitialDriveMaxFlatVel") / DONOR_REF["fInitialDriveMaxFlatVel"], 0.75, 1.3)
    top = _clamp(t["top_gear_speed"] * speed_f * tune["top_speed"], *tune["top_band"])
    top = _clamp(top * mult.get("top_speed", 1.0), 15.0, 120.0)
    n_gears = int(_clamp(o["gears"] if o.get("gears") else (h.gears if h and h.gears else 6), 4, 7))

    brake_f = _clamp(g("fBrakeForce") / DONOR_REF["fBrakeForce"], 0.7, 1.4)
    brake_f = max(brake_f, tune["brake_min"]) * tune["brake"] * mult.get("brake", 1.0)
    spring_f = _clamp(g("fSuspensionForce") / DONOR_REF["fSuspensionForce"], 0.75, 1.3) * mult.get("spring", 1.0)
    grip_f = _clamp(g("fTractionCurveMax") / DONOR_REF["fTractionCurveMax"], 0.9, 1.15)
    grip_f = max(grip_f, tune["grip_min"]) * tune["grip"] * mult.get("grip", 1.0)
    drag_f = _clamp(g("fInitialDragCoeff") / DONOR_REF["fInitialDragCoeff"], 0.6, 1.3)
    drag_f = min(drag_f, tune["drag_max"]) * tune["drag"] * mult.get("drag", 1.0)
    steer = float(o["steer"]) if o.get("steer") else _clamp(g("fSteeringLock") * 1.25, 35, 60)

    # inertia: scale the donor's by mass and by the body's dimensions, then GTA's multiplier
    (x0, y0, z0), (x1, y1, z1) = vehicle.bbox
    W, L, H = x1 - x0, y1 - y0, z1 - z0
    dW, dL, dH = 1.94, 4.48, 1.33   # donor body extents (m)
    im = h.inertia_mult if h and h.inertia_mult else (1.0, 1.0, 1.0)
    moi = []
    for base, (a, b), (da, db), m in zip(t["moment_of_inertia"], ((L, H), (W, H), (L, W)),
                                         ((dL, dH), (dW, dH), (dL, dW)), im):
        moi.append(base * mass_ratio * (a * a + b * b) / (da * da + db * db) * _clamp(m, 0.8, 1.25))
    com_g = h.com_offset if h and h.com_offset else (0.0, 0.0, 0.0)
    cy = (y0 + y1) / 2
    com = (0.0, _clamp(cy + com_g[1], -0.5, 0.5),
           _clamp(t["com_offset"][2] + com_g[2] - tune["com_drop"], -0.1, 0.35))

    wheels = {}
    for axle in ("front", "back"):
        a = vehicle.axles.get(axle)
        if a is None:
            continue
        wheels[axle] = {
            "tireRadius": _clamp(a.radius, 0.25, 0.6),
            "rimRadius": _clamp(a.rim_radius, 0.12, a.radius - 0.03),
            "tireWidth": _clamp(a.width, 0.12, 0.45),
        }
    # a model without wheel geometry still needs wheel sizes for the simulation: the other axle's,
    # else a sports-car tyre (the donor's)
    fallback = wheels.get("front") or wheels.get("back") or {"tireRadius": 0.34, "rimRadius": 0.24, "tireWidth": 0.28}
    for axle in ("front", "back"):
        wheels.setdefault(axle, dict(fallback))
    # the profile may set each axle's lateral wheel offset (metres); else 0 (see build_tweak)
    for axle, off in (o.get("wheel_offset") or {}).items():
        if axle in wheels:
            wheels[axle]["offset"] = float(off)
    return {"mass": mass, "layout": layout, "torque": torque, "top_speed": top, "gears": n_gears,
            "brake": brake_f, "spring": spring_f, "grip": grip_f, "drag": drag_f, "steer": steer,
            "moi": tuple(moi), "com": com, "wheels": wheels, "tune": tune_name if tune_name in TUNES else DEFAULT_TUNE,
            "chassis": dict(tune["chassis"]), "axle": dict(tune["axle"])}


def _gear_table(prof, n, top):
    """n gears following the donor's speed/RPM/torque curve, ending at `top` m/s."""
    donor = [
        # minSpeed, maxSpeed, minRPM, maxRPM, torqueMul   (911 Turbo, game 2.31)
        (0, 10, 900, 6000, 0.87), (0, 15, 900, 5800, 0.87), (14, 23, 2800, 5900, 0.54),
        (22, 29, 2900, 5950, 0.39), (28, 34, 3000, 6000, 0.31), (33, 40.8, 3100, 7000, 0.25),
    ]
    d = np.array(donor, float)
    x_d = np.linspace(0, 1, len(d))
    x_n = np.linspace(0, 1, n)
    cols = [np.interp(x_n, x_d, d[:, i]) for i in range(5)]
    scale = top / d[-1, 1]
    gears = []
    for i in range(n):
        mn, mx = cols[0][i] * scale, cols[1][i] * scale
        if i == 0:
            mn = 0.0
        gears.append({"minSpeed": mn, "maxSpeed": mx, "minEngineRPM": cols[2][i],
                      "maxEngineRPM": cols[3][i], "torqueMultiplier": cols[4][i]})
    return gears


def build_tweak(vehicle, prof, *, record: str, ent_path: str, appearance: str, name_key: str,
                handling=None, sound=None, destroyed_appearance: str | None = None,
                seating=None) -> tuple[str, dict]:
    """Returns (yaml text, handling profile).

    handling: the profile's handling overrides; sound: cp77.sound.resolve() result (written when it
    differs from the donor's own sound)."""
    t = prof["tweak"]
    hp = handling_profile(vehicle, prof, handling)
    y = _Yaml()
    rid = record                     # e.g. Vehicle.i8rpncs
    base = prof["record"]

    wheel_dims = f"{rid}_wheel_dims"
    drive = f"{rid}_drive_model"
    wheel_setup = f"{rid}_wheel_setup"
    engine = f"{rid}_engine"
    destruction = f"{rid}_destruction"

    main = {
        "$base": base,
        "entityTemplatePath": ent_path,
        "appearanceName": appearance,
        "destroyedAppearance": destroyed_appearance or appearance,
        "displayName": f"LocKey#{name_key}",
        "vehWheelDimensionsSetup": wheel_dims,
        "vehDriveModelData": drive,
        "vehEngineData": engine,
        "destruction": destruction,
        "customizeLights": "false",
        "hasVisualCustomization": "false",
        # GTA wheels stand upright where the model has them: no donor camber (the 911 leans its
        # wheels 1.5 degrees and moves the rear ones 2.7 cm)
        "leftFrontCamber": "0", "rightFrontCamber": "0", "leftBackCamber": "0", "rightBackCamber": "0",
        "leftFrontCamberOffset": _vec((0, 0, 0)), "rightFrontCamberOffset": _vec((0, 0, 0)),
        "leftBackCamberOffset": _vec((0, 0, 0)), "rightBackCamberOffset": _vec((0, 0, 0)),
    }
    cam = camera_class(vehicle)
    if cam != prof.get("camera", "sport"):
        main["tppCameraPresets"] = camera_presets(cam)
    if sound and sound.get("override"):
        # engine/wheel/bump sounds: names of sound setups in the game's audio metadata
        main["player_audio_resource"] = sound["player"]
        main["traffic_audio_resource"] = sound["traffic"]
    if seating is not None and getattr(seating, "unmount", None):
        # where the game looks for room to let a player out (x: metres from the centre line).  The
        # donor's 1.65 m is inside a wide car's wheel fairings: every exit is refused and the game
        # slides the player from seat to seat instead
        main["unmountOffsetPosition"] = _vec(seating.unmount)
    package = None
    if seating is not None and t.get("data_package") and seating.template != prof.get("seating", "sport2"):
        # the sitting animation set: a low car gets the hypercars' lower, reclined posture
        package = f"{rid}_data_package"
        main["vehDataPackage"] = package
    y.record(rid, main, comment=f"{vehicle.display_name} converted from FiveM by fivem2open77 (donor: {base})")
    if package:
        tpl = seating.tweak_template
        y.record(package, {
            "$base": t["data_package"],
            "seatingTemplateOverride": tpl,
            "animVars": [f"{tpl}_seat_front_left", f"{tpl}_seat_front_right", f"{tpl}_seat_back_left",
                         f"{tpl}_seat_back_right"],
            # the donor's Porsche-specific corrective pose does not belong to another posture
            "additionalAnimFeatures": [],
        }, comment=f"seating: {seating.template} ({seating.template_reason})")

    # wheel dimensions (visual tyre size used by the simulation)
    y.record(wheel_dims, {"$type": "VehicleWheelDimensionsSetup",
                          "frontPreset": f"{wheel_dims}_front", "backPreset": f"{wheel_dims}_back"})
    for axle in ("front", "back"):
        w = hp["wheels"].get(axle) or hp["wheels"].get("front" if axle == "back" else "back")
        # wheelOffset moves the tyre out from its wheel bone along the axle.  A vanilla car's wheel
        # bones sit inboard of its tyres (the 911: 0.70 m out, tyres at 0.80), ours sit at the GTA
        # wheel centres already: 0, unless the profile asks (handling.wheel_offset)
        y.record(f"{wheel_dims}_{axle}", {"$type": "VehicleWheelDimensionsPreset",
                                          "rimRadius": _f(w["rimRadius"]), "tireRadius": _f(w["tireRadius"]),
                                          "tireWidth": _f(w["tireWidth"]),
                                          "wheelOffset": _f(w.get("offset", 0.0))})

    # drivetrain layout
    roles = {
        "rwd": ("Vehicle.VehicleWheelDrivingSetup_4_RWD_inline0", "Vehicle.VehicleWheelDrivingSetup_4_RWD_inline1",
                "Vehicle.VehicleWheelDrivingSetup_4_Default_inline3", "Vehicle.VehicleWheelDrivingSetup_4_Default_inline2"),
        "awd": ("Vehicle.VehicleWheelDrivingSetup_4_Default_inline1", "Vehicle.VehicleWheelDrivingSetup_4_Default_inline0",
                "Vehicle.VehicleWheelDrivingSetup_4_Default_inline3", "Vehicle.VehicleWheelDrivingSetup_4_Default_inline2"),
        "fwd": ("Vehicle.VehicleWheelDrivingSetup_4_FWD_inline0", "Vehicle.VehicleWheelDrivingSetup_4_FWD_inline1",
                "Vehicle.VehicleWheelDrivingSetup_4_FWD_inline3", "Vehicle.VehicleWheelDrivingSetup_4_FWD_inline2"),
    }[hp["layout"]]
    y.record(wheel_setup, {"$base": t["wheel_setup"], "LF": roles[0], "RF": roles[1], "LB": roles[2], "RB": roles[3],
                           "frontPreset": f"{wheel_setup}_front", "backPreset": f"{wheel_setup}_back"})
    donor_brake = {"front": 430.0, "back": 340.0}
    donor_spring = {"front": 14.0, "back": 21.0}
    for axle, src in (("front", t["front_preset"]), ("back", t["back_preset"])):
        y.record(f"{wheel_setup}_{axle}", {
            "$base": src,
            "maxBrakingTorque": _f(donor_brake[axle] * hp["brake"] * hp["mass"] / t["chassis_mass"]),
            "springStiffness": _f(donor_spring[axle] * hp["spring"] * hp["mass"] / t["chassis_mass"]),
            "frictionMulLateral": _f((0.92 if axle == "front" else 0.95) * hp["grip"]),
            "frictionMulLongitudinal": _f((0.89 if axle == "front" else 0.95) * hp["grip"]),
            **({"swaybarStiffness": _f(DONOR_SWAYBAR * hp["axle"]["swaybarStiffness"])}
               if "swaybarStiffness" in hp["axle"] else {}),
        })

    y.record(drive, {
        "$base": t["drive_model"],
        "chassis_mass": _f(hp["mass"]),
        "total_mass": _f(hp["mass"]),
        "center_of_mass_offset": _vec(hp["com"]),
        "momentOfInertia": _vec(hp["moi"]),
        "maxWheelTurnDeg": _f(hp["steer"]),
        "airResistanceFactor": _f(1.93 * hp["drag"]),
        "wheelSetup": wheel_setup,
        **{k: _f(v) for k, v in sorted(hp["chassis"].items())},
    })

    gears = _gear_table(prof, hp["gears"], hp["top_speed"])
    gear_ids = [f"{engine}_gear{i + 1}" for i in range(len(gears))]
    y.record(engine, {"$base": t["engine"], "engineMaxTorque": _f(hp["torque"]), "gears": gear_ids})
    for gid, gdata in zip(gear_ids, gears):
        y.record(gid, {"$type": "VehicleGear", **{k: _f(v) for k, v in gdata.items()}})

    # destruction: keep the donor's damage model but drop part lists that name donor components
    (x0, y0, z0), (x1, y1, z1) = vehicle.bbox
    y.record(destruction, {
        "$base": t.get("destruction") or f"{base}_inline8",
        "lights": [], "glass": [], "detachableParts": [], "deformableParts": [],
        "gridDimensions": _vec((x1 - x0, y1 - y0, 0.25)),
        "vehicleDimensions": _vec(((x1 - x0) / 2, (y1 - y0) / 2 * 1.33, 0.35)),
    })
    return y.text(), hp
