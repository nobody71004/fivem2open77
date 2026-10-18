"""Donor vehicles: a vanilla Cyberpunk car whose entity, rig, animations, audio, seating and
gameplay setup are reused, with the converted model's geometry and dimensions put on top.

The donor files are extracted from the player's own game install (never shipped by us) and
cached as WolvenKit JSON.
"""
from __future__ import annotations

import json
import os
import re
import shutil

PORSCHE = "v_sport2_porsche_911turbo"
HELLA = "v_standard2_archer_hella"

# Files every donor shares.  The converter uses them only as templates -- the component layout of
# the .app (meshes, lights, the visual controller), the mesh/material layout, the paint layer, a
# convex collider -- so the set proven on the Porsche serves every donor.
TEMPLATES = {
    "app": r"base\vehicles\appearances\sport\porsche_911turbo__basic.app",
    "app_appearance": "default",
    "mesh_template": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\{PORSCHE}__ext01_body_01.mesh",
    "paint_setup": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\textures\ml_{PORSCHE}_exterior.mlsetup",
    "paint_mask": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\textures\ml_{PORSCHE}_exterior_masksset.mlmask",
    "phys": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\{PORSCHE}__ext01_interaction_collider_01.phys",
    # multilayer template of the paint layer: its colour table lets us match the GTA paint colour
    "paint_template": r"base\surfaces\materials\plastic\plastic_hq\plastic_tech_hq_01_30.mltemplate",
}

# The drive model, engine, gears and wheel setup every converted car starts from (TweakDB records of
# the Porsche, dumped from tweakdb_ep1.bin, game 2.31).  The FiveM handling is mapped onto these
# numbers (cp77/tweak.py works relative to them), whichever donor lends the body.
PHYSICS = {
    "drive_model": "Vehicle.VehicleDriveModelData_911Turbo",
    "wheel_setup": "Vehicle.VehicleDriveModelData_911Turbo_inline0",
    "front_preset": "Vehicle.VehicleDriveModelData_911Turbo_inline1",
    "back_preset": "Vehicle.VehicleDriveModelData_911Turbo_inline2",
    "wheel_dims": f"Vehicle.{PORSCHE}_inline53",
    "wheel_dims_front": f"Vehicle.{PORSCHE}_inline54",
    "wheel_dims_back": f"Vehicle.{PORSCHE}_inline55",
    "engine": f"Vehicle.{PORSCHE}_inline0",
    "gears": [f"Vehicle.{PORSCHE}_inline{i}" for i in range(1, 7)],
    "chassis_mass": 1290.0,
    "com_offset": (0.0, -0.16, 0.14),
    "moment_of_inertia": (1890.0, 600.0, 2010.0),
    "top_gear_speed": 40.8,
    "engine_max_torque": 431.0,
    "wheel_roles": {
        "rwd": ("Vehicle.VehicleWheelDrivingSetup_4_RWD_inline0", "Vehicle.VehicleWheelDrivingSetup_4_RWD_inline1",
                "Vehicle.VehicleWheelDrivingSetup_4_Default_inline3", "Vehicle.VehicleWheelDrivingSetup_4_Default_inline2"),
    },
}

ALL_SEATS = ("seat_front_left", "seat_front_right", "seat_back_left", "seat_back_right")

DONORS = {
    "porsche_911": {
        **TEMPLATES,
        "label": "Porsche 911 Turbo (sport, 2 doors)",
        "record": f"Vehicle.{PORSCHE}",
        "ent": rf"base\vehicles\sport\{PORSCHE}__basic_01.ent",
        "rig": rf"base\vehicles\sport\{PORSCHE}\rig\{PORSCHE}_01__basic_01.rig",
        "vehicle_rig": r"base\vehicles\base_rig\v_base_x4_248_146_44\v_base_x4_248_146_44.rig",
        "anims": rf"base\animations\vehicle\sport\vehicle\{PORSCHE}.anims",
        # the donor's own body: its bounding box is what the donor's slots and bones are scaled from
        "body_mesh": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\{PORSCHE}__ext01_body_01.mesh",
        "doors": 2,
        # door/window corners the rig and its animations have (fl front left ... br back right)
        "corners": ("fl", "fr"),
        # which end of the car each lid bone sits at in the donor rig (911: frunk in front)
        "lid_ends": {"trunk_a": "front", "hood_a": "rear"},
        # engine sound (TweakDB player_audio_resource the record inherits)
        "sound": "v_car_porsche_911_turbo",
        # how the player sits (vehDataPackage.seatingTemplateOverride, see model/seats.py)
        "seating": "sport2",
        # the seats its OccupantSlots (Vehicle2SeatSetDefault) let a player sit in
        "occupant_seats": ("seat_front_left", "seat_front_right"),
        # third person camera of the record (cp77/tweak.py camera_class)
        "camera": "sport",
        # additive animation delta axes (bone-local, positive angle opens) decoded from the donor .anims
        "open_axes": {
            "door_front_left_a": (0, 0, -1),
            "door_front_right_a": (0, 0, 1),
            "hood_a": (-1, 0, 0),
            "trunk_a": (1, 0, 0),
        },
        "tweak": {
            **PHYSICS,
            "data_package": f"Vehicle.{PORSCHE}_inline7",
            "destruction": f"Vehicle.{PORSCHE}_inline8",
        },
        "gta_classes": ["VC_SUPER", "VC_SPORT", "VC_SPORT_CLASSIC", "VC_COUPE", "VC_COMPACT", "VC_MUSCLE"],
    },
    "archer_hella": {
        **TEMPLATES,
        "label": "Archer Hella (saloon, 4 doors)",
        "record": f"Vehicle.{HELLA}",
        "ent": rf"base\vehicles\standard\{HELLA}__basic_01.ent",
        "rig": rf"base\vehicles\standard\{HELLA}\rig\{HELLA}_01__basic_01.rig",
        "vehicle_rig": r"base\vehicles\base_rig\v_base_x4_337_165_44\v_base_x4_337_165_44.rig",
        "anims": rf"base\animations\vehicle\standard\vehicle\{HELLA}.anims",
        "body_mesh": rf"base\vehicles\standard\{HELLA}\entities\meshes\{HELLA}__ext01_body_01.mesh",
        "doors": 4,
        "corners": ("fl", "fr", "bl", "br"),
        "lid_ends": {"hood_a": "front", "trunk_a": "rear"},
        "sound": "v_car_archer_hella",
        "seating": "standard2",
        # Vehicle4SeatSetDefault: all four seats (OccupantSlots of the entity has all four too)
        "occupant_seats": ALL_SEATS,
        "camera": "sedan",
        # every door swings 70 degrees about its vertical hinge, the bonnet 50 about its rear edge,
        # the boot lid 60 about its front edge (v_standard2_archer_hella.anims, game 2.31)
        "open_axes": {
            "door_front_left_a": (0, 0, -1),
            "door_front_right_a": (0, 0, 1),
            "door_back_left_a": (0, 0, -1),
            "door_back_right_a": (0, 0, 1),
            "hood_a": (1, 0, 0),
            "trunk_a": (-1, 0, 0),
        },
        "tweak": {
            **PHYSICS,
            # vehSeatSet Vehicle4SeatSetDefault, standard2_vehicle seating
            "data_package": f"Vehicle.{HELLA}_inline1",
            "destruction": "Vehicle.VehicleDestructionParamsHella",
        },
        "gta_classes": ["VC_SEDAN", "VC_SUV", "VC_OFF_ROAD", "VC_VAN", "VC_EMERGENCY", "VC_SERVICE",
                        "VC_COMMERCIAL", "VC_INDUSTRIAL", "VC_UTILITY", "VC_SPORT", "VC_SUPER", "VC_COUPE",
                        "VC_COMPACT", "VC_MUSCLE", "VC_SPORT_CLASSIC"],
    },
}

DEFAULT_DONOR = "porsche_911"
CORNER_SLOTS = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}


def is_side_door(vehicle, name: str) -> bool:
    """A door part that is a door: a panel at the side of the car, door-sized.  GTA modellers also
    hang other things on the rear door bones of two-door cars (a Lamborghini's engine-cover flaps,
    a hatchback's roof panel); those are not doors and do not ask for a four-door donor."""
    import numpy as np
    part = vehicle.parts.get(name)
    if part is None or part.kind != "door":
        return False
    pts = [s.positions for s in part.submeshes if s.lod == 1 and len(s.positions)]
    if not pts:
        return False
    t, r = (np.asarray(x, np.float64) for x in vehicle.part_frames.get(name, (np.zeros(3), np.eye(3))))
    p = np.concatenate(pts).astype(np.float64) @ r.T + t
    lo, hi = p.min(0), p.max(0)
    ext, ctr = hi - lo, (lo + hi) / 2
    return bool(ext[1] >= 0.4 and ext[2] >= 0.4 and abs(ctr[0]) >= 0.35)


def corners_needed(vehicle) -> set:
    """Corners of the car with a door that opens (a door-sized door part).  Windows do not count:
    a two-door's rear quarter windows stay fixed in the body when the donor has no bones for them
    (pipeline.bind_parts)."""
    out = set()
    for name, part in getattr(vehicle, "parts", {}).items():
        if part.kind != "door" or not part.slot or not is_side_door(vehicle, name):
            continue
        for key, side in CORNER_SLOTS.items():
            if part.slot == f"door_{side}_a":
                out.add(key)
    return out


def missing_corners(vehicle, name: str) -> list:
    """Corners the car opens that donor `name` has no doors for."""
    return sorted(corners_needed(vehicle) - set(DONORS[name].get("corners", ())))


def choose_donor(vehicle, requested: str | None = None) -> str:
    """The donor whose rig, animations and seats fit the car: the four-door saloon when the car has
    rear doors (their bones, opening animations, rear seats and interaction points), the two-door
    Porsche otherwise.  A requested donor wins unless it lacks doors the car has: their parts would
    hang from bones the donor's rig does not have (left behind at the car's origin in game)."""
    if requested:
        if requested not in DONORS:
            raise ValueError(f"unknown donor '{requested}', available: {', '.join(DONORS)}")
        if not missing_corners(vehicle, requested):
            return requested
    need = corners_needed(vehicle)
    for name in (DEFAULT_DONOR,) + tuple(n for n in DONORS if n != DEFAULT_DONOR):
        if need <= set(DONORS[name]["corners"]):
            return name
    return max(DONORS, key=lambda n: len(DONORS[n]["corners"]))


REQUIRED = ("ent", "app", "rig", "vehicle_rig", "anims", "body_mesh", "mesh_template", "paint_setup", "phys")
OPTIONAL = ("paint_template",)


def _paths(prof, optional=True):
    keys = REQUIRED + (OPTIONAL if optional else ())
    out = []
    for k in keys:
        if prof.get(k) and prof[k] not in out:
            out.append(prof[k])
    return out


def _regex_for(paths):
    alts = "|".join(re.escape(p) for p in paths)
    return f"^({alts})$"


class DonorCache:
    """Extracts donor files from the game install into a cache and serves them as JSON."""

    def __init__(self, wk, game_dir: str | None, cache_dir: str, log=print):
        self.wk = wk
        self.game_dir = game_dir
        self.cache_dir = cache_dir
        self.log = log

    def _json_path(self, depot_path: str) -> str:
        return os.path.join(self.cache_dir, "json", depot_path.replace("\\", os.sep) + ".json")

    def _bin_path(self, depot_path: str) -> str:
        return os.path.join(self.cache_dir, "bin", depot_path.replace("\\", os.sep))

    def has(self, depot_path: str) -> bool:
        return os.path.exists(self._json_path(depot_path))

    def ensure(self, name: str):
        prof = DONORS[name]
        required = _paths(prof, optional=False)
        self.ensure_paths(required, [p for p in _paths(prof) if p not in required], what=f"donor '{name}'")

    def ensure_paths(self, required, optional=(), what="template files"):
        """Make sure the given depot files are cached as JSON, extracting them from the game the
        first time.  Files in `optional` may be missing from the game without failing."""
        required, optional = list(required), list(optional)
        paths = required + [p for p in optional if p not in required]
        missing = [p for p in paths if not os.path.exists(self._json_path(p))]
        if not missing:
            return
        required_missing = [p for p in required if p in missing]
        if not self.game_dir:
            if not required_missing:
                self.log(f"note: optional {what} files not cached and no --game given: " + ", ".join(missing))
                return
            raise RuntimeError(f"Cyberpunk 2077 install folder is required the first time ({what} files are "
                               "extracted from your own game): pass --game <folder containing bin/x64/Cyberpunk2077.exe>")
        content = os.path.join(self.game_dir, "archive", "pc", "content")
        ep1 = os.path.join(self.game_dir, "archive", "pc", "ep1")
        archives = [content] + ([ep1] if os.path.isdir(ep1) else [])
        self.log(f"extracting {what} from the game archives ({len(missing)} files)...")
        bin_root = os.path.join(self.cache_dir, "bin")
        self.wk.unbundle(archives, bin_root, _regex_for(missing))
        still = [p for p in missing if not os.path.exists(self._bin_path(p))]
        if [p for p in still if p in required_missing]:
            raise RuntimeError(f"could not extract {what} files: " + ", ".join(still))
        missing = [p for p in missing if p not in still]
        tmp_json = os.path.join(self.cache_dir, "json_tmp")
        shutil.rmtree(tmp_json, ignore_errors=True)
        for p in missing:
            self.wk.serialize(self._bin_path(p), tmp_json)
            produced = os.path.join(tmp_json, p.split("\\")[-1] + ".json")
            dst = self._json_path(p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(produced, dst)
        shutil.rmtree(tmp_json, ignore_errors=True)

    def load(self, depot_path: str) -> dict:
        with open(self._json_path(depot_path), "r", encoding="utf-8") as f:
            return json.load(f)

    def binary(self, depot_path: str) -> str:
        return self._bin_path(depot_path)
