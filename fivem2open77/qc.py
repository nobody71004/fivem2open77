"""Quality control of converted cars.

While a car converts, `car_facts` writes down what was built: every part and the rig bone it hangs
from, the donor's doors and seats, where the wheels went in the rig and what the TweakDB record
tells the game about them, what geometry was left out and why.  They go into the build's
conversion-report.json ("qc").  `check_car` turns them into pass / warn / fail checks; the Studio
runs it after every build and on its quality sweeps (studio/qc.py), so a car that would be missing
a piece in game -- a door hanging from a bone the donor does not have, a wheel pushed out of its
arch, a body part left out -- is caught before it reaches a server.

QC_VERSION goes up whenever the converter fixes something the checks can see: builds made before
are reported stale (and rebuilt by the Studio's sweep).
"""
from __future__ import annotations

import re

import numpy as np

QC_VERSION = 3          # 3: the performance tune (cp77/tweak.py TUNES) and engine sounds by engine

CORNER_OF_SLOT = {"front_left": "fl", "front_right": "fr", "back_left": "bl", "back_right": "br"}
WHEEL_KINDS = ("wheel", "tire", "wheel_static", "tire_static")


def _r(x, n=3):
    if isinstance(x, dict):
        return {k: _r(y, n) for k, y in x.items()}
    if isinstance(x, (list, tuple)):
        return [_r(y, n) for y in x]
    if hasattr(x, "tolist"):
        return _r(x.tolist(), n)
    if isinstance(x, float):
        return round(x, n)
    return x


def _corner(slot):
    if not slot:
        return None
    for side, key in CORNER_OF_SLOT.items():
        if f"_{side}_" in f"_{slot}_":
            return key
    return None


def _tweak_values(text: str) -> dict:
    """The numbers of the record that matter to the wheels, read back from the written YAML."""
    out = {"wheelOffset": [], "camber": {}}
    for m in re.finditer(r"^\s+wheelOffset:\s*([-0-9.]+)", text or "", re.M):
        out["wheelOffset"].append(float(m.group(1)))
    for m in re.finditer(r"^\s+((?:left|right)(?:Front|Back)Camber):\s*([-0-9.]+)", text or "", re.M):
        out["camber"][m.group(1)] = float(m.group(2))
    for m in re.finditer(r"^\s+((?:left|right)(?:Front|Back)CamberOffset):\s*\{\s*x:\s*([-0-9.]+)", text or "", re.M):
        out["camber"][m.group(1)] = float(m.group(2))
    for key in ("tireRadius", "tireWidth", "rimRadius"):
        out[key] = [float(m.group(1)) for m in re.finditer(rf"^\s+{key}:\s*([-0-9.]+)", text or "", re.M)]
    m = re.search(r"^\s+\$base:\s*(\S+)", text or "", re.M)
    out["base"] = m.group(1) if m else None
    return out


def _body_halfwidth(v, y, radius):
    """Widest body point (|x|) beside a wheel: body shell within the wheel's radius along the car,
    from the wheel centre's height up (the arch over the tyre, not a sill below it)."""
    out = 0.0
    for name, part in v.parts.items():
        if part.kind not in ("body", "door"):
            continue
        f = v.part_frames.get(name)
        if f is None:
            continue
        t, r = (np.asarray(x, np.float64) for x in f)
        for s in part.submeshes:
            if s.lod != 1 or not len(s.positions):
                continue
            p = s.positions.astype(np.float64) @ r.T + t
            sel = p[(np.abs(p[:, 1] - y) < radius) & (p[:, 2] > -0.05)]
            if len(sel):
                out = max(out, float(np.abs(sel[:, 0]).max()))
    return out


def car_facts(v, donor_id: str, donor: dict, bindings: dict, frames: dict, tweak_text: str,
              hp: dict, camera: str | None = None, profile: dict | None = None) -> dict:
    """What was built, for the checks (all positions Root space, metres)."""
    from .cp77.donor import is_side_door
    parts = {}
    for name, p in v.parts.items():
        parts[name] = {"kind": p.kind, "slot": p.slot, "parent": p.parent, "verts": int(p.vertex_count(1)),
                       "bone": bindings.get("bones", {}).get(name), "corner": _corner(p.slot) or _corner(name)}
    rig = set(bindings.get("rig") or [])
    for name, info in parts.items():
        info["bone_in_rig"] = info["bone"] in rig if info["bone"] else None
    # wheels: where GTA has them, where the rig we wrote has them, how big
    wheels = {}
    names = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}
    for axle_name, axle in v.axles.items():
        for corner, pos in axle.positions.items():
            rig_pos = frames.get(f"wheel_{names[corner]}")
            susp = frames.get(f"suspension_{names[corner]}")
            wheels[corner] = {
                "axle": axle_name, "gta": _r(pos), "radius": round(float(axle.radius), 3),
                "width": round(float(axle.width), 3),
                "rig": _r(np.asarray(rig_pos)[:3, 3]) if rig_pos is not None else None,
                "suspension": _r(np.asarray(susp)[:3, 3]) if susp is not None else None,
                "body_halfwidth": round(_body_halfwidth(v, pos[1], axle.radius), 3),
            }
    bones = v.report.get("bones") or []
    # lamp-shader geometry on a plain bone goes into the light mesh of the light bone that claims it
    claimed = v.report.get("lamps_claimed") or {}
    dropped = [{"bone": b["name"], "role": b["role"], "verts": int(b["verts"])} for b in bones
               if b.get("verts") and b.get("part") is None and not claimed.get(b["name"])
               and b.get("role", "").split(":")[0] not in ("skip", "extra", "wheel", "light")]
    total = sum(int(b.get("verts") or 0) for b in bones)
    seats = sorted(v.seats)
    fit = getattr(v, "seat_fit", None)
    return {
        "version": QC_VERSION,
        "donor": {"id": donor_id, "record": donor["record"], "corners": list(donor.get("corners", ())),
                  "doors": donor.get("doors"), "occupant_seats": list(donor.get("occupant_seats") or ()),
                  "open_axes": sorted(donor.get("open_axes", {})), "camera": donor.get("camera")},
        "parts": parts,
        "detached": list(bindings.get("detached") or []),
        "doors": sorted({parts[n]["corner"] for n, p in v.parts.items() if p.kind == "door" and parts[n]["corner"]
                         and p.slot and is_side_door(v, n)}),
        "windows": sorted({i["corner"] for i in parts.values() if i["kind"] == "window" and i["corner"]}),
        "lids": {k: parts[k]["bone"] for k in ("hood_a", "trunk_a") if k in parts},
        "seats": seats,
        "seat_template": fit.template if fit is not None else None,
        "wheels": wheels,
        "static_wheels": list(getattr(v, "static_wheels", []) or []),
        "tweak": _tweak_values(tweak_text),
        "wheel_offset_profile": ((profile or {}).get("handling") or {}).get("wheel_offset"),
        "handling_wheels": _r(hp.get("wheels", {})),
        "performance": {"tune": hp.get("tune"), "torque_per_kg": round(float(hp["torque"]) / float(hp["mass"]), 3),
                        "top_speed": round(float(hp["top_speed"]), 2), "brake": round(float(hp["brake"]), 3),
                        "grip": round(float(hp["grip"]), 3), "mass": round(float(hp["mass"]), 1)}
        if hp.get("torque") and hp.get("mass") else None,
        "geometry": {"bones_verts": total, "dropped": dropped,
                     "dropped_verts": sum(d["verts"] for d in dropped)},
        "fitted": {"extras": [e["name"] for e in v.report.get("extras_detail", []) if e.get("on")],
                   "mods": [m["model"] for m in v.report.get("mods", []) if m.get("on")]},
        "lights": sorted({l.light_type for l in v.lights}),
        "camera": camera,
        "bbox": _r(v.bbox),
        "textures_missing": len(v.report.get("textures_missing") or []),
    }


class _Checks:
    def __init__(self):
        self.items = []

    def add(self, cid, ok, msg, level="error"):
        self.items.append({"id": cid, "ok": bool(ok), "level": "ok" if ok else level, "message": msg})
        return ok


def check_car(report: dict) -> dict:
    """pass / warn / fail checks on a build's conversion report."""
    c = _Checks()
    q = (report or {}).get("qc")
    if not q:
        c.add("qc.facts", False, f"built by converter {report.get('version', '?') if report else '?'}, before the "
              "quality checks and the fixes they watch: rebuild it", level="error")
        return {"ok": False, "stale": True, "checks": c.items}
    if q.get("version", 0) < QC_VERSION:
        c.add("qc.version", False, f"built with quality checks v{q.get('version')} (now v{QC_VERSION}): the "
              "converter has fixes this build does not have; rebuild it", level="error")
    d = q["donor"]
    parts = q["parts"]
    # ---- every part hangs from a bone the donor's rig has
    loose = [n for n, i in parts.items() if i.get("slot") and i.get("bone_in_rig") is False]
    c.add("parts.attached", not loose, "every part hangs from a bone of the rig" if not loose else
          "parts bound to bones the rig does not have (left at the car's origin in game): " + ", ".join(loose))
    if q.get("detached"):
        c.add("parts.detached", False, "fixed to the body (the donor has no bone for them, they do not open): "
              + ", ".join(q["detached"]), level="warn")
    # ---- doors the donor can open
    missing = [k for k in q["doors"] if k not in d["corners"]]
    c.add("doors.donor", not missing, f"{len(q['doors'])} doors, the donor ({d['id']}) opens "
          f"{len(d['corners'])}" if not missing else f"doors at {', '.join(missing)} but the donor {d['id']} "
          f"has doors at {', '.join(d['corners'])} only")
    for corner in q["windows"]:
        if corner not in d["corners"] and not any(p.get("corner") == corner and p["kind"] == "window" and p.get("slot")
                                                  for p in parts.values()):
            pass
    # ---- seats a player can use
    rear = [s for s in q["seats"] if s.startswith("seat_back")]
    usable = set(d["occupant_seats"])
    if rear and not usable >= set(rear) and q["doors"] and any(k in ("bl", "br") for k in q["doors"]):
        c.add("seats.rear", False, "the car has rear doors and seats but the donor seats two", level="error")
    elif rear and not usable >= set(rear):
        c.add("seats.rear", False, f"{len(rear)} rear seats nobody can sit in (two-door donor)", level="warn")
    else:
        c.add("seats.rear", True, f"{len(usable & set(q['seats'])) or len(usable)} seats a player can use")
    # ---- lids open with a donor animation
    for lid, bone in (q.get("lids") or {}).items():
        c.add(f"lid.{lid}", bone in d["open_axes"], f"{lid} opens with the donor's {bone} animation"
              if bone in d["open_axes"] else f"{lid} hangs from {bone}, which the donor does not animate", level="warn")
    # ---- wheels
    tw = q.get("tweak") or {}
    want = q.get("wheel_offset_profile") or {}
    offsets = tw.get("wheelOffset") or []
    expect = [float(want.get("front", 0.0)), float(want.get("back", 0.0))] if want else [0.0, 0.0]
    c.add("wheels.offset", len(offsets) == 2 and all(abs(a - b) < 1e-4 for a, b in zip(offsets, expect)),
          f"wheel offset {offsets} (the tyres sit on the GTA wheel centres)" if offsets else "no wheel offset written",
          level="error")
    camber = tw.get("camber") or {}
    c.add("wheels.camber", camber and all(abs(x) < 1e-4 for x in camber.values()),
          "no camber (wheels upright, as the model has them)" if camber else "camber not written: the donor's applies",
          level="error")
    wh = q.get("wheels") or {}
    off_rig = []
    for corner, w in wh.items():
        if w.get("rig") is None:
            off_rig.append(f"{corner}: no wheel bone")
            continue
        gx, gy, gz = w["gta"]
        rx, ry, rz = w["rig"]
        if max(abs(gx - rx), abs(gy - ry), abs(gz - rz)) > 0.01:
            off_rig.append(f"{corner}: rig ({rx:.2f}, {ry:.2f}, {rz:.2f}) vs GTA ({gx:.2f}, {gy:.2f}, {gz:.2f})")
    c.add("wheels.rig", len(wh) == 4 and not off_rig, "the rig's four wheel bones sit on the GTA wheel centres"
          if len(wh) == 4 and not off_rig else ("; ".join(off_rig) or f"{len(wh)} wheels"))
    asym = []
    for front, back in (("fl", "fr"), ("bl", "br")):
        if front in wh and back in wh:
            a, b = wh[front]["gta"], wh[back]["gta"]
            if abs(a[0] + b[0]) > 0.03 or abs(a[1] - b[1]) > 0.03:
                asym.append(f"{front}/{back} x {a[0]:.2f}/{b[0]:.2f} y {a[1]:.2f}/{b[1]:.2f}")
    c.add("wheels.symmetry", not asym, "left and right wheels mirror each other" if not asym else
          "the model's wheels are not symmetric: " + "; ".join(asym), level="warn")
    out = []
    for corner, w in wh.items():
        outer = abs(w["gta"][0]) + w["width"] / 2
        if w["body_halfwidth"] and outer > w["body_halfwidth"] + 0.04:
            out.append(f"{corner} {100 * (outer - w['body_halfwidth']):.0f} cm")
    c.add("wheels.arches", not out, "every tyre sits inside its wheel arch" if not out else
          "tyres stand out of the body (as in the GTA model): " + ", ".join(out), level="warn")
    hw = q.get("handling_wheels") or {}
    bad_r = []
    for corner, w in wh.items():
        r = (hw.get(w["axle"]) or {}).get("tireRadius")
        if r is not None and abs(r - w["radius"]) > 0.02:
            bad_r.append(f"{w['axle']} tyre {r:.2f} m vs model {w['radius']:.2f} m")
    c.add("wheels.radius", not bad_r, "the simulated tyres are the model's size" if not bad_r else
          "; ".join(sorted(set(bad_r))) + " (clamped)", level="warn")
    if q.get("static_wheels"):
        c.add("wheels.extra_axles", False, f"{len(q['static_wheels'])} wheels on a third axle ride fixed to the body",
              level="warn")
    # ---- geometry left out
    g = q.get("geometry") or {}
    total = max(1, g.get("bones_verts") or 1)
    lost = g.get("dropped_verts") or 0
    share = lost / total
    c.add("geometry.dropped", share <= 0.01, f"all the model's geometry is in the car" if not lost else
          f"{lost} vertices ({100 * share:.1f}%) of the model are not in any part: "
          + ", ".join(f"{d['bone']} ({d['verts']})" for d in sorted(g.get('dropped') or [], key=lambda x: -x['verts'])[:6]),
          level="error" if share > 0.05 else "warn")
    # ---- lights
    have = set(q.get("lights") or [])
    c.add("lights", {"Head", "Brake"} <= have, "head and brake lights" if {"Head", "Brake"} <= have else
          f"lights: {', '.join(sorted(have)) or 'none'}", level="warn")
    if q.get("fitted", {}).get("mods") or q.get("fitted", {}).get("extras"):
        f = q["fitted"]
        c.add("parts.fitted", True, "fitted: " + ", ".join(f["mods"] + f["extras"]), level="warn")
    perf = q.get("performance")
    if perf:
        tuned = perf.get("tune") not in (None, "stock")
        weak = []
        if tuned:
            if perf["torque_per_kg"] < 0.39:
                weak.append(f"torque {perf['torque_per_kg']:.2f} N m/kg")
            if perf["brake"] < 1.0:
                weak.append(f"brakes {perf['brake']:.2f}x")
            if perf["grip"] < 1.0:
                weak.append(f"grip {perf['grip']:.2f}x")
        c.add("handling.performance", not weak,
              f"{perf.get('tune')} tune: {perf['torque_per_kg']:.2f} N m/kg, top gear {perf['top_speed'] * 3.6:.0f} km/h, "
              f"brakes {perf['brake']:.2f}x, grip {perf['grip']:.2f}x" if not weak else
              f"{perf.get('tune')} tune, but the profile takes it below the tune's floor: " + ", ".join(weak), level="warn")
    ok = all(i["ok"] or i["level"] == "warn" for i in c.items)
    return {"ok": ok, "stale": any(i["id"] in ("qc.version",) and not i["ok"] for i in c.items), "checks": c.items}
