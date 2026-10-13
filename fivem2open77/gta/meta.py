"""Parsers for the GTA V data files a FiveM vehicle resource ships.

vehicles.meta      CVehicleModelInfo__InitDataList   model/txd/handling names, class, layout, flags
handling.meta      CHandlingDataMgr                   physics/handling numbers
carvariations.meta CVehicleModelInfoVariation         default colours, kits, lights
carcols.meta       CVehicleModelInfoVarGlobal         kits/lights/sirens (only light colours are used)

The files written by hand in the community are frequently malformed (BOM, stray text,
unclosed comments, duplicated declarations).  Parsing is therefore tolerant: we repair
the most common problems before handing the XML to ElementTree.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def _clean_xml(text: str) -> str:
    text = text.lstrip("﻿").strip()
    # drop anything before the first tag
    i = text.find("<")
    text = text[i:] if i >= 0 else text
    # remove duplicate xml declarations (concatenated metas)
    decl = re.findall(r"<\?xml[^>]*\?>", text)
    if len(decl) > 1:
        first = decl[0]
        text = first + re.sub(r"<\?xml[^>]*\?>", "", text)
    # stray ampersands
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    return text


def parse_xml(data: bytes | str, name: str = "") -> ET.Element:
    if isinstance(data, bytes):
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        text = data
    text = _clean_xml(text)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        # Try trimming garbage after the root element closes.
        m = re.match(r"\s*(?:<\?xml[^>]*\?>)?\s*<([A-Za-z_][\w.]*)", text)
        if m:
            root = m.group(1)
            end = text.rfind(f"</{root}>")
            if end > 0:
                return ET.fromstring(text[:end + len(root) + 3])
        raise


def _txt(el, tag, default=None):
    x = el.find(tag)
    if x is None:
        return default
    if x.text is not None and x.text.strip():
        return x.text.strip()
    return x.get("value", default)


def _val(el, tag, default=None, cast=float):
    x = el.find(tag)
    if x is None:
        return default
    v = x.get("value")
    if v is None:
        v = (x.text or "").strip()
    try:
        return cast(v)
    except (TypeError, ValueError):
        try:
            return cast(float(v))
        except (TypeError, ValueError):
            return default


def _vec(el, tag, default=(0.0, 0.0, 0.0)):
    x = el.find(tag)
    if x is None:
        return default
    try:
        return (float(x.get("x", 0)), float(x.get("y", 0)), float(x.get("z", 0)))
    except ValueError:
        return default


def extra_bones(text: str) -> list:
    """vehicles.meta extra names (EXTRA_1 EXTRA_10 ...) -> the skeleton's bone names (extra_1,
    extra_ten: GTA spells the tenth one out)."""
    out = []
    for tok in re.split(r"[\s,]+", text or ""):
        m = re.match(r"^extra_?(\d+)$", tok.strip(), re.I)
        if not m:
            continue
        n = int(m.group(1))
        name = "extra_ten" if n == 10 else f"extra_{n}"
        if name not in out:
            out.append(name)
    return out


@dataclass
class VehicleModelInfo:
    model_name: str
    txd_name: str
    handling_id: str
    game_name: str
    make_name: str
    vehicle_type: str
    vehicle_class: str
    layout: str
    wheel_type: str
    flags: list
    wheel_scale: float
    wheel_scale_rear: float
    extra: dict = field(default_factory=dict)


def parse_vehicles_meta(data, name="vehicles.meta"):
    root = parse_xml(data, name)
    out = []
    txd_parents = {}
    for item in root.iter("Item"):
        model = _txt(item, "modelName")
        if not model:
            continue
        flags = (_txt(item, "flags", "") or "").split()
        out.append(VehicleModelInfo(
            model_name=model,
            txd_name=_txt(item, "txdName", model) or model,
            handling_id=_txt(item, "handlingId", model) or model,
            game_name=_txt(item, "gameName", model) or model,
            make_name=_txt(item, "vehicleMakeName", "") or "",
            vehicle_type=_txt(item, "type", "VEHICLE_TYPE_CAR") or "VEHICLE_TYPE_CAR",
            vehicle_class=_txt(item, "vehicleClass", "VC_SPORT") or "VC_SPORT",
            layout=_txt(item, "layout", "LAYOUT_STANDARD") or "LAYOUT_STANDARD",
            wheel_type=_txt(item, "wheelType", "") or "",
            flags=flags,
            wheel_scale=_val(item, "wheelScale", 0.0),
            wheel_scale_rear=_val(item, "wheelScaleRear", 0.0),
            extra={
                "audioNameHash": _txt(item, "audioNameHash", ""),
                "plateType": _txt(item, "plateType", ""),
                "dashboardType": _txt(item, "dashboardType", ""),
                "requiredExtras": extra_bones(_txt(item, "requiredExtras", "") or ""),
            },
        ))
    rel = root.find("txdRelationships")
    if rel is not None:
        for it in rel.findall("Item"):
            p, c = _txt(it, "parent"), _txt(it, "child")
            if p and c:
                txd_parents[c.lower()] = p.lower()
    return out, txd_parents


HANDLING_FLOATS = [
    "fMass", "fInitialDragCoeff", "fPercentSubmerged", "fDriveBiasFront", "fInitialDriveForce",
    "fDriveInertia", "fClutchChangeRateScaleUpShift", "fClutchChangeRateScaleDownShift",
    "fInitialDriveMaxFlatVel", "fBrakeForce", "fBrakeBiasFront", "fHandBrakeForce", "fSteeringLock",
    "fTractionCurveMax", "fTractionCurveMin", "fTractionCurveLateral", "fTractionSpringDeltaMax",
    "fLowSpeedTractionLossMult", "fCamberStiffnesss", "fTractionBiasFront", "fTractionLossMult",
    "fSuspensionForce", "fSuspensionCompDamp", "fSuspensionReboundDamp", "fSuspensionUpperLimit",
    "fSuspensionLowerLimit", "fSuspensionRaise", "fSuspensionBiasFront", "fAntiRollBarForce",
    "fAntiRollBarBiasFront", "fRollCentreHeightFront", "fRollCentreHeightRear",
    "fCollisionDamageMult", "fWeaponDamageMult", "fDeformationDamageMult", "fEngineDamageMult",
    "fPetrolTankVolume", "fOilVolume", "fSeatOffsetDistX", "fSeatOffsetDistY", "fSeatOffsetDistZ",
]


@dataclass
class Handling:
    name: str
    values: dict
    com_offset: tuple
    inertia_mult: tuple
    gears: int
    model_flags: str
    handling_flags: str
    ai: str

    def get(self, key, default=None):
        return self.values.get(key, default)


def parse_handling_meta(data, name="handling.meta"):
    root = parse_xml(data, name)
    out = {}
    for item in root.iter("Item"):
        hname = _txt(item, "handlingName")
        if not hname:
            continue
        vals = {k: _val(item, k) for k in HANDLING_FLOATS}
        vals = {k: v for k, v in vals.items() if v is not None}
        out[hname.lower()] = Handling(
            name=hname, values=vals,
            com_offset=_vec(item, "vecCentreOfMassOffset"),
            inertia_mult=_vec(item, "vecInertiaMultiplier", (1.0, 1.0, 1.0)),
            gears=int(_val(item, "nInitialDriveGears", 5, cast=float) or 5),
            model_flags=_txt(item, "strModelFlags", "0") or "0",
            handling_flags=_txt(item, "strHandlingFlags", "0") or "0",
            ai=_txt(item, "AIHandling", "AVERAGE") or "AVERAGE",
        )
    return out


@dataclass
class Variation:
    model_name: str
    colors: list        # list of [primary, secondary, pearl, wheel, interior, dashboard] index lists
    lights_id: int | None
    plate_probabilities: dict
    kits: list = field(default_factory=list)      # mod kit names (carcols Kits) the car uses


@dataclass
class VisibleMod:
    """One visible part of a carcols mod kit: a .yft authored in the car's model space, fitted
    on `bone` (it moves with it) and hiding the car's `turn_off` bones while fitted."""
    model: str
    kind: str                 # VMT_BONNET, VMT_SPOILER, ...
    bone: str
    label: str = ""
    turn_off: list = field(default_factory=list)
    linked: list = field(default_factory=list)


def parse_carvariations(data, name="carvariations.meta"):
    root = parse_xml(data, name)
    out = {}
    for item in root.iter("Item"):
        model = _txt(item, "modelName")
        if not model:
            continue
        colors = []
        c = item.find("colors")
        if c is not None:
            for ci in c.findall("Item"):
                idx = ci.find("indices")
                if idx is not None and idx.text:
                    try:
                        colors.append([int(x) for x in idx.text.split()])
                    except ValueError:
                        pass
        lid = _val(item, "lightSettings", None, cast=int)
        kits = []
        k = item.find("kits")
        if k is not None:
            kits = [(x.text or "").strip() for x in k.findall("Item") if (x.text or "").strip()]
        out[model.lower()] = Variation(model_name=model, colors=colors, lights_id=lid, plate_probabilities={},
                                       kits=kits)
    return out


def parse_carcols(data, name="carcols.meta"):
    """Only light settings are used (headlight/taillight colours are not stored per car in
    GTA; carcols' ``Lights`` holds siren/indicator/headlight params).  Returns raw dict."""
    root = parse_xml(data, name)
    lights = {}
    for item in root.iter("Item"):
        lid = _val(item, "id", None, cast=int)
        if lid is None:
            continue
        hl = item.find("headLight")
        if hl is not None:
            col = _txt(hl, "color", None)
            lights[lid] = {"headlight_color": col}
    kits = {}
    kroot = root.find("Kits")
    for kit in (kroot.findall("Item") if kroot is not None else []):
        kname = (_txt(kit, "kitName", "") or "").strip()
        if not kname:
            continue
        mods = []
        vm = kit.find("visibleMods")
        for it in (vm.findall("Item") if vm is not None else []):
            model = (_txt(it, "modelName", "") or "").strip()
            if not model or model.lower() == "dummy_mod_frag_model":
                continue

            def names(tag):
                el = it.find(tag)
                return [(x.text or "").strip().lower() for x in el.findall("Item") if (x.text or "").strip()] \
                    if el is not None else []

            mods.append(VisibleMod(model=model, kind=(_txt(it, "type", "") or "").strip(),
                                   bone=(_txt(it, "bone", "chassis") or "chassis").strip().lower(),
                                   label=(_txt(it, "modShopLabel", "") or "").strip(),
                                   turn_off=names("turnOffBones"), linked=names("linkedModels")))
        kits[kname.lower()] = mods
    return {"lights": lights, "kits": kits}
