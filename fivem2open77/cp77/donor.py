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

DONORS = {
    "porsche_911": {
        "label": "Porsche 911 Turbo (sport, 2 doors)",
        "record": f"Vehicle.{PORSCHE}",
        "ent": rf"base\vehicles\sport\{PORSCHE}__basic_01.ent",
        "app": r"base\vehicles\appearances\sport\porsche_911turbo__basic.app",
        "app_appearance": "default",
        "rig": rf"base\vehicles\sport\{PORSCHE}\rig\{PORSCHE}_01__basic_01.rig",
        "vehicle_rig": r"base\vehicles\base_rig\v_base_x4_248_146_44\v_base_x4_248_146_44.rig",
        "anims": rf"base\animations\vehicle\sport\vehicle\{PORSCHE}.anims",
        "mesh_template": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\{PORSCHE}__ext01_body_01.mesh",
        "paint_setup": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\textures\ml_{PORSCHE}_exterior.mlsetup",
        "paint_mask": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\textures\ml_{PORSCHE}_exterior_masksset.mlmask",
        "phys": rf"base\vehicles\sport\{PORSCHE}\entities\meshes\{PORSCHE}__ext01_interaction_collider_01.phys",
        # multilayer template of the paint layer: its colour table lets us match the GTA paint colour
        "paint_template": r"base\surfaces\materials\plastic\plastic_hq\plastic_tech_hq_01_30.mltemplate",
        "doors": 2,
        # which end of the car each lid bone sits at in the donor rig (911: frunk in front)
        "lid_ends": {"trunk_a": "front", "hood_a": "rear"},
        # engine sound (TweakDB player_audio_resource the record inherits)
        "sound": "v_car_porsche_911_turbo",
        # how the player sits (vehDataPackage.seatingTemplateOverride, see model/seats.py)
        "seating": "sport2",
        # additive animation delta axes (bone-local, positive angle opens) decoded from the donor .anims
        "open_axes": {
            "door_front_left_a": (0, 0, -1),
            "door_front_right_a": (0, 0, 1),
            "hood_a": (-1, 0, 0),
            "trunk_a": (1, 0, 0),
        },
        # TweakDB records of the donor (dumped from tweakdb_ep1.bin, game 2.31)
        "tweak": {
            "drive_model": "Vehicle.VehicleDriveModelData_911Turbo",
            "wheel_setup": "Vehicle.VehicleDriveModelData_911Turbo_inline0",
            "front_preset": "Vehicle.VehicleDriveModelData_911Turbo_inline1",
            "back_preset": "Vehicle.VehicleDriveModelData_911Turbo_inline2",
            "wheel_dims": f"Vehicle.{PORSCHE}_inline53",
            "wheel_dims_front": f"Vehicle.{PORSCHE}_inline54",
            "wheel_dims_back": f"Vehicle.{PORSCHE}_inline55",
            "engine": f"Vehicle.{PORSCHE}_inline0",
            "gears": [f"Vehicle.{PORSCHE}_inline{i}" for i in range(1, 7)],
            "data_package": f"Vehicle.{PORSCHE}_inline7",
            "chassis_mass": 1290.0,
            "com_offset": (0.0, -0.16, 0.14),
            "moment_of_inertia": (1890.0, 600.0, 2010.0),
            "top_gear_speed": 40.8,
            "engine_max_torque": 431.0,
            "wheel_roles": {
                "rwd": ("Vehicle.VehicleWheelDrivingSetup_4_RWD_inline0", "Vehicle.VehicleWheelDrivingSetup_4_RWD_inline1",
                        "Vehicle.VehicleWheelDrivingSetup_4_Default_inline3", "Vehicle.VehicleWheelDrivingSetup_4_Default_inline2"),
            },
        },
        "gta_classes": ["VC_SUPER", "VC_SPORT", "VC_SPORT_CLASSIC", "VC_COUPE", "VC_COMPACT", "VC_MUSCLE"],
    },
}

DEFAULT_DONOR = "porsche_911"


def choose_donor(vehicle, requested: str | None = None) -> str:
    if requested:
        if requested not in DONORS:
            raise ValueError(f"unknown donor '{requested}', available: {', '.join(DONORS)}")
        return requested
    for name, prof in DONORS.items():
        if vehicle.gta_class in prof["gta_classes"] and vehicle.door_count <= prof["doors"]:
            return name
    return DEFAULT_DONOR


REQUIRED = ("ent", "app", "rig", "vehicle_rig", "anims", "mesh_template", "paint_setup", "phys")
OPTIONAL = ("paint_template",)


def _paths(prof, optional=True):
    keys = REQUIRED + (OPTIONAL if optional else ())
    return [prof[k] for k in keys if prof.get(k)]


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
        paths = _paths(prof)
        missing = [p for p in paths if not os.path.exists(self._json_path(p))]
        if not missing:
            return
        required_missing = [p for p in _paths(prof, optional=False) if p in missing]
        if not self.game_dir:
            if not required_missing:
                self.log("note: optional donor files not cached and no --game given: " + ", ".join(missing))
                return
            raise RuntimeError("Cyberpunk 2077 install folder is required the first time a donor is used "
                               "(pass --game <folder containing bin/x64/Cyberpunk2077.exe>)")
        content = os.path.join(self.game_dir, "archive", "pc", "content")
        ep1 = os.path.join(self.game_dir, "archive", "pc", "ep1")
        archives = [content] + ([ep1] if os.path.isdir(ep1) else [])
        self.log(f"extracting donor '{name}' from the game archives ({len(missing)} files)...")
        bin_root = os.path.join(self.cache_dir, "bin")
        self.wk.unbundle(archives, bin_root, _regex_for(missing))
        still = [p for p in missing if not os.path.exists(self._bin_path(p))]
        if [p for p in still if p in required_missing]:
            raise RuntimeError("could not extract donor files: " + ", ".join(still))
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
