"""Entity template (.ent) for the converted vehicle, cloned from the donor.

Everything that defines how the car *behaves* (controller, seats, audio, AI, interactions, camera
targets, workspots...) is the donor's.  We swap in our rigs, animations, collision and appearance,
and re-fit the few components that carry absolute positions (FX slots, shadow decal, env probe,
interaction points) to the converted body's size.
"""
from __future__ import annotations

import copy

import numpy as np

from .handles import empty_package, materialize, normalize
from .wkjson import cname, depot, index_handles, value_of


def _scale_point(p, dctr, dsize, nctr, nsize):
    p = np.asarray(p, float)
    return nctr + (p - dctr) * (nsize / dsize)


def build_ent(vehicle, donor_ent_doc, paths: dict, appearance_name: str, donor_bbox) -> dict:
    """paths: depot paths for 'app', 'rig', 'vehicle_rig', 'anims', 'phys'."""
    doc = copy.deepcopy(donor_ent_doc)
    index = index_handles(donor_ent_doc)
    root = doc["Data"]["RootChunk"]

    # ---- appearance list: ours (and one per neon colour), all in our .app
    base = copy.deepcopy(root["appearances"][0]) if root["appearances"] else {"$type": "entTemplateAppearance"}
    apps = [(appearance_name, "default")]
    kit = getattr(vehicle, "neon", None)
    if kit is not None:
        apps += [(f"{appearance_name}_neon_{key}", f"neon_{key}") for key, _, _ in kit.colors]
    root["appearances"] = []
    for ent_name, app_name in apps:
        tmpl = copy.deepcopy(base)
        tmpl["name"] = cname(ent_name)
        tmpl["appearanceName"] = cname(app_name)
        tmpl["appearanceResource"] = depot(paths["app"])
        root["appearances"].append(tmpl)
    root["defaultAppearance"] = cname(appearance_name)

    dmin, dmax = (np.asarray(x, float) for x in donor_bbox)
    nmin, nmax = (np.asarray(x, float) for x in vehicle.bbox)
    dctr, nctr = (dmin + dmax) / 2, (nmin + nmax) / 2
    dsize, nsize = np.maximum(dmax - dmin, 1e-3), np.maximum(nmax - nmin, 1e-3)
    base_z = float(vehicle.base_z)

    comps = []
    for c in root["components"]:
        c = materialize(c, index)
        comps.append(c)
        t = c["$type"]
        name = value_of(c.get("name"))
        if t == "entAnimatedComponent" and name in ("deformation_rig", "vehicle_rig"):
            c["rig"] = depot(paths["rig"] if name == "deformation_rig" else paths["vehicle_rig"])
            for entry in c.get("animations", {}).get("gameplay", []):
                entry["animSet"] = depot(paths["anims"])
        elif t == "vehicleChassisComponent":
            c["collisionResource"] = depot(paths["phys"])
        elif t == "entDecalComponent" and name == "decal_shadow":
            vs = c.get("visualScale")
            if vs:
                # decal is turned 90 degrees about Z: its X runs along the car
                vs["X"] = float(nsize[1] * 1.1)
                vs["Y"] = float(nsize[0] * 1.25)
            pos = c["localTransform"]["Position"]
            pos["x"]["Bits"] = int(round(nctr[0] * 131072))
            pos["y"]["Bits"] = int(round(nctr[1] * 131072))
        elif t == "entEnvProbeComponent":
            s = c.get("size")
            if s:
                s["X"] *= float(nsize[0] / dsize[0])
                s["Y"] *= float(nsize[1] / dsize[1])
                s["Z"] *= float(nsize[2] / dsize[2])
        elif t == "entSlotComponent" and name in ("FX_slots", "fx_slots_holo", "InteractionSlots", "UI_slots"):
            _fit_slots(c, name, vehicle, base_z, dctr, dsize, nctr, nsize)

    root["components"] = comps
    root["compiledData"] = empty_package(root.get("compiledData", {}).get("Flags", 0))
    root["resolvedDependencies"] = [depot(paths["anims"])]
    root["inplaceResources"] = []
    normalize(doc, index)
    return doc


def _fit_slots(c, comp_name, vehicle, base_z, dctr, dsize, nctr, nsize):
    exhausts = list(getattr(vehicle, "exhausts", []) or [])
    left = sorted([e for e in exhausts if e[0] < -0.05], key=lambda e: e[0])
    right = sorted([e for e in exhausts if e[0] > 0.05], key=lambda e: -e[0])
    mid = [e for e in exhausts if -0.05 <= e[0] <= 0.05]
    pick = {
        "fx_exhaust_l1": (left or mid or right or [None])[0],
        "fx_exhaust_l2": (left[1:] or left or mid or right or [None])[0],
        "fx_exhaust_r1": (right or mid or left or [None])[0],
        "fx_exhaust_r2": (right[1:] or right or mid or left or [None])[0],
        "fx_exhaust": tuple(np.mean(np.array(exhausts), 0)) if exhausts else None,
    }
    fit = getattr(vehicle, "seat_fit", None)
    for s in c.get("slots", []):
        sname = value_of(s.get("slotName"))
        bone = value_of(s.get("boneName"))
        rp = s["relativePosition"]
        p = np.array([rp["X"], rp["Y"], rp["Z"]], float)
        # the spot a seat's exit is checked at: outside the body, at road level (seat bones have
        # no rotation, so the offset is a plain difference)
        if comp_name == "InteractionSlots" and fit is not None and sname == f"{bone}_interaction" and bone in fit.seats:
            info = fit.seats[bone]
            q = np.asarray(info["exit"], float) - np.asarray(info["position"], float)
            rp["X"], rp["Y"], rp["Z"] = (float(v) for v in q)
            continue
        # bone-relative slots on bones that already moved with the rig stay as they are
        if bone not in (None, "None", "Base", "Root", "Trajectory"):
            continue
        off = base_z if bone == "Base" else 0.0
        if comp_name == "FX_slots" and sname in pick and pick[sname] is not None:
            q = np.array(pick[sname], float) - [0, 0, off]
        else:
            q = _scale_point(p + [0, 0, off], dctr, dsize, nctr, nsize) - [0, 0, off]
        rp["X"], rp["Y"], rp["Z"] = (float(v) for v in q)
