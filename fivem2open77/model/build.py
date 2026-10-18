"""GTA V vehicle (YFT + YTDs + metas) -> intermediate Vehicle.

What happens here:
  * every vertex of the drawable is assigned to the bone that moves it (rigid skinning or the
    model's bone binding), wheel meshes come from the fragment's physics children;
  * GTA bones are classified into Cyberpunk roles (body, door, window, hood/trunk, wheel, tire,
    hub, steering wheel, lights, glass, interior);
  * geometry is re-expressed in the local frame of the Cyberpunk rig bone it will be bound to,
    in Root space where the wheel centres sit at Z = 0;
  * GTA shaders are mapped to material descriptions; textures are collected by name;
  * wheel dimensions, light placements and seat positions are measured.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

import numpy as np

from ..gta.drawable import quat_to_mat3
from ..gta.frag import load_yft
from ..gta.texture import NO_TEXTURE, load_ytd, ytd_names
from ..imaging import texture_rgba
from .colors import gta_color
from .ir import (LOD_MASKS, Bone, LightSpec, Material, Part, SubMesh, Vehicle,
                 WheelAxle)
from . import kit
from .overlays import add_paint_overlays

MAX_VERTS = 65000  # Cyberpunk submeshes use 16-bit indices
LAMP_REACH = 0.45     # metres: lamp-shader geometry this close to a light bone belongs to that light
OPAQUE_GLASS = 0.98   # GTA glass texels at least this opaque are solid (frit, pillars, black bands)
CUTOUT_OFFSET = 0.0006  # metres: the alpha-tested copy of a glass triangle sits this far outside it

WHEEL_BONES = {"wheel_lf": "fl", "wheel_rf": "fr", "wheel_lr": "bl", "wheel_rr": "br"}
EXTRA_WHEEL = re.compile(r"^wheel_([lr])m(\d+)$")     # middle axles of 6- and 8-wheelers
DOOR_BONES = {"door_dside_f": "fl", "door_pside_f": "fr", "door_dside_r": "bl", "door_pside_r": "br"}
WINDOW_BONES = {"window_lf": "fl", "window_rf": "fr", "window_lr": "bl", "window_rr": "br"}
HUB_BONES = {"hub_lf": "fl", "hub_rf": "fr", "hub_lr": "bl", "hub_rr": "br"}
SEAT_BONES = {"seat_dside_f": "seat_front_left", "seat_pside_f": "seat_front_right",
              "seat_dside_r": "seat_back_left", "seat_pside_r": "seat_back_right"}
SKIP_BONES = {"chassis_lowlod", "chassis_dummy"}

LIGHT_KINDS = [
    (re.compile(r"^headlight_[lr]"), "head"),
    (re.compile(r"^taillight_[lr]"), "tail"),
    (re.compile(r"^brakelight_[lrm]"), "brake"),
    (re.compile(r"^reversinglight_[lr]"), "reverse"),
    (re.compile(r"^indicator_[lr][fr]"), "indicator"),
    (re.compile(r"^extralight_\d+"), "extra"),
    (re.compile(r"^(interiorlight|dashglow|doorlight_.*|platelight)"), "interior"),
    (re.compile(r"^neon_[lrfb]"), "neon"),
]

SIDE_NAMES = {"fl": "front_left", "fr": "front_right", "bl": "back_left", "br": "back_right"}


def _light_kind(name: str):
    for rx, kind in LIGHT_KINDS:
        if rx.match(name):
            return kind
    return None


def _srgb_to_linear(c):
    c = np.asarray(c, np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _loose(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _sanitize(name: str) -> str:
    """Record and resource id.  Open77 resource names are 3-64 chars of [a-z0-9_] starting with a
    letter; the car's id also names its `<id>_chime` sibling, so keep 6 chars spare."""
    s = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", name.lower())).strip("_") or "car"
    if not s[0].isalpha():
        s = "v_" + s
    if len(s) < 3:
        s += "_car"
    return s[:58].rstrip("_")


def _rot_to_quat(m: np.ndarray):
    """3x3 rotation matrix -> quaternion (x, y, z, w)."""
    t = m[0, 0] + m[1, 1] + m[2, 2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    q = np.array([x, y, z, w])
    q /= np.linalg.norm(q)
    if q[3] < 0:
        q = -q
    return tuple(float(v) for v in q)


def display_name_of(src):
    """(display name, make) of a vehicle source: the texts its labels show, the pack name when that
    says more ("Biome" from a download called "Mercedes-Benz Biome Concept"), else the model name."""
    from ..gta.gxt2 import label
    info, model = src.info, src.model
    labels = getattr(src, "labels", None) or {}
    name_txt = label(labels, info.game_name) if info else None
    make_txt = label(labels, info.make_name) if info else None
    raw = (info.make_name or "").strip() if info else ""
    # a raw make is a text label; use it as a name only when it reads like one (not "FIVEMCAR.COM")
    raw_ok = bool(re.fullmatch(r"[A-Za-z][A-Za-z\- ]{1,24}", raw)) and raw.upper() not in ("NULL", "NONE")
    make = make_txt or (raw if raw_ok else "")
    if name_txt:
        disp = name_txt
    elif info and info.game_name and info.game_name.upper() not in (model.upper(), "NULL"):
        disp = info.game_name
    else:
        disp = model
    if make_txt and make.lower() not in disp.lower() and make.upper() != model.upper():
        disp = f"{make} {disp}"
    pack = (getattr(src, "pack_name", "") or "").strip()
    # (a car's own resource folder, "ikx3_c63se24", is its model id: that says nothing more)
    if pack and len(disp.split()) == 1 and disp.lower() in pack.lower() and len(pack) > len(disp) \
            and len(pack) <= 48 and re.sub(r"[^a-z0-9]", "", pack.lower()) != re.sub(r"[^a-z0-9]", "", model.lower()):
        disp = pack
    return disp, make


def _orthonormal(m3: np.ndarray) -> np.ndarray:
    """Remove scale/shear from a 3x3 matrix (bone scale is never meant for the rig)."""
    u, _, vt = np.linalg.svd(m3)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r


class _Accum:
    """Collects triangles for one (part, material, lod) until 65k vertices."""

    def __init__(self, material: str, lod: int):
        self.material = material
        self.lod = lod
        self.chunks = []
        self.count = 0

    def add(self, attrs: dict, tri: np.ndarray):
        used, inv = np.unique(tri.reshape(-1), return_inverse=True)
        if len(used) > MAX_VERTS:
            # split big inputs by triangles
            half = len(tri) // 2
            self.add(attrs, tri[:half])
            self.add(attrs, tri[half:])
            return
        sub = {k: v[used] for k, v in attrs.items() if v is not None}
        self.chunks.append((sub, inv.reshape(-1, 3).astype(np.uint32)))
        self.count += len(used)

    def build(self):
        out, cur, cur_n = [], [], 0
        for sub, tri in self.chunks:
            n = len(sub["position"])
            if cur and cur_n + n > MAX_VERTS:
                out.append(self._merge(cur))
                cur, cur_n = [], 0
            cur.append((sub, tri))
            cur_n += n
        if cur:
            out.append(self._merge(cur))
        return out

    def _merge(self, items):
        keys = items[0][0].keys()
        attrs = {}
        for k in keys:
            attrs[k] = np.concatenate([s[k] for s, _ in items]) if all(k in s for s, _ in items) else None
        tris, base = [], 0
        for s, t in items:
            tris.append(t + base)
            base += len(s["position"])
        return SubMesh(material=self.material, positions=attrs["position"].astype(np.float32),
                       normals=attrs["normal"].astype(np.float32), tangents=attrs["tangent"].astype(np.float32),
                       uv0=attrs["uv0"].astype(np.float32),
                       uv1=attrs["uv1"].astype(np.float32) if attrs.get("uv1") is not None else None,
                       colors=attrs["color"].astype(np.float32) if attrs.get("color") is not None else None,
                       indices=np.concatenate(tris).reshape(-1).astype(np.uint32), lod=self.lod)


def compute_tangents(pos, nrm, uv, tri):
    """Per-vertex tangents from UVs (Lengyel), w = handedness."""
    tan1 = np.zeros_like(pos)
    tan2 = np.zeros_like(pos)
    i0, i1, i2 = tri[:, 0], tri[:, 1], tri[:, 2]
    p0, p1, p2 = pos[i0], pos[i1], pos[i2]
    w0, w1, w2 = uv[i0], uv[i1], uv[i2]
    e1, e2 = p1 - p0, p2 - p0
    d1, d2 = w1 - w0, w2 - w0
    r = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.where(np.abs(r) < 1e-12, 1e-12, r)
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) / r[:, None]
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) / r[:, None]
    for idx in (i0, i1, i2):
        np.add.at(tan1, idx, sdir)
        np.add.at(tan2, idx, tdir)
    t = tan1 - nrm * (nrm * tan1).sum(1, keepdims=True)
    ln = np.linalg.norm(t, axis=1, keepdims=True)
    fallback = np.cross(nrm, np.array([0.0, 0.0, 1.0]))
    fallback[np.linalg.norm(fallback, axis=1) < 1e-6] = (1.0, 0.0, 0.0)
    t = np.where(ln > 1e-8, t / np.maximum(ln, 1e-8), fallback / np.linalg.norm(fallback, axis=1, keepdims=True))
    w = np.where((np.cross(nrm, t) * tan2).sum(1) < 0.0, -1.0, 1.0)
    return np.concatenate([t, w[:, None]], 1).astype(np.float32)


class VehicleBuilder:
    def __init__(self, src, options=None, log=print):
        self.src = src
        self.opt = options or {}
        self.log = log
        self.warnings = []
        self.role_overrides = {}
        ex = self.opt.get("extras") or {}
        # the profile's explicit choices; the model's own defaults (vehicles.meta requiredExtras,
        # FLAG_EXTRAS_ALL, what the car needs) fill in the rest once the skeleton is known (_plan_parts)
        self.extras_profile = ({str(k).lower(): bool(on) for k, on in ex.items()} if isinstance(ex, dict)
                               else {str(e).lower(): True for e in ex})
        self.extras_on = {k for k, on in self.extras_profile.items() if on}
        self.extras_default = {}
        mods = self.opt.get("mods") or {}
        self.mods_profile = ({str(k).lower(): bool(on) for k, on in mods.items()} if isinstance(mods, dict)
                             else {str(m).lower(): True for m in mods})
        self.mods_on = set()
        self.mod_info = {}
        self.mod_frags = {}
        self.hidden_bones = set()
        self.parts_plan = {}
        self.bone_verts = defaultdict(int)     # GTA bone -> high-LOD vertices it carries
        self.light_host = {}                   # light bone index -> part of the door/lid it hangs from

    # ------------------------------------------------------------------ loading
    def load(self):
        self.frag = load_yft(self.src.yft, name=self.src.yft)
        self.drawable = self.frag.drawable
        self.sk = self.drawable.skeleton
        if self.sk is None:
            raise ValueError(f"{self.src.model}: the fragment has no skeleton; not a vehicle model")
        self.textures = {}
        self.textures.update(self.drawable.shader_group.textures)
        for path in self.src.ytds:
            try:
                for k, v in load_ytd(path, name=path).items():
                    self.textures.setdefault(k, v)
            except Exception as e:  # noqa: BLE001
                self.warnings.append(f"cannot read texture dictionary {path}: {e}")
        # the resource's other dictionaries: looked up by name when a texture is not in the car's own
        self._extra = [p for p in (getattr(self.src, "ytd_extra", None) or []) if p not in self.src.ytds]
        self._extra_index = None
        self._extra_loose = None
        self._extra_loaded = set()
        # loose name index: packs misspell texture names in their shaders ("lightsBW" for the
        # dictionary's "lights BW"); a match that ignores case, spaces and punctuation is used when it
        # is unambiguous
        self.tex_loose = {}
        for k in self.textures:
            lk = _loose(k)
            self.tex_loose[lk] = None if lk in self.tex_loose and self.tex_loose[lk] != k else k
        self.shaders = self.drawable.shader_group.shaders
        self.world = [self.sk.world_matrix(i) for i in range(len(self.sk.bones))]
        self.names = [b.name.lower() for b in self.sk.bones]
        # opening axis of articulated bones (doors, bonnet, boot) from the joint rotation limits
        tag_index = {b.tag: i for i, b in enumerate(self.sk.bones)}
        self.open_axis = {}
        for lim in self.drawable.joints:
            i = tag_index.get(lim.bone_tag)
            if i is None:
                continue
            mn, mx = np.array(lim.min, float), np.array(lim.max, float)
            ext = np.where(np.abs(mn) > np.abs(mx), mn, mx)
            k = int(np.argmax(np.abs(ext)))
            if abs(ext[k]) < 1e-3:
                continue
            ax = np.zeros(3)
            ax[k] = 1.0 if ext[k] > 0 else -1.0
            self.open_axis[i] = ax

    # ------------------------------------------------------------- bone roles
    def _ancestors(self, i):
        out = []
        while i >= 0:
            out.append(i)
            i = self.sk.bones[i].parent
        return out

    def _override_role(self, n):
        """Role a profile gives GTA bone n (and the bones below it), or None."""
        v = self.role_overrides.get(n)
        if not v:
            return None
        if ":" in v:
            kind, key = v.split(":", 1)
            return (kind, key)
        if v == "steering":
            return ("steering", "sw")
        return (v, n)

    def _plan_parts(self):
        """Decide which GTA extras and mod-kit parts the car wears (see model/kit.py): the profile's
        choice, else GTA's rules (requiredExtras, FLAG_EXTRAS_ALL), else on when the part adds a
        substantial piece the model lacks.  Sets extras_on, mods_on and the stock bones fitted
        parts hide."""
        info = self.src.info
        required = set((info.extra.get("requiredExtras") or []) if info else [])
        every = bool(info and "FLAG_EXTRAS_ALL" in (info.flags or []))
        self.extras_required = required
        extras = [n for n in self.names if re.match(r"^extra_", n)]
        by_extra, base, verts_of = defaultdict(list), [], defaultdict(int)
        for b, p in kit.drawable_points(self.drawable):
            if b >= len(self.names):
                continue
            verts_of[b] += len(p)
            if self.names[b] in SKIP_BONES:
                continue
            ex = self._extra_of(b)
            (by_extra[ex] if ex else base).append(p)
        mods = {}
        for mod, path in getattr(self.src, "mods", None) or []:
            e = mods.setdefault(mod.model.lower(), {"mod": mod, "path": path, "slots": set()})
            e["slots"].add(mod.kind)
        self.mod_info = mods

        def candidates(use_profile):
            out = []
            for n in extras:
                forced = self.extras_profile.get(n) if use_profile else None
                why = None
                if forced is None and (every or n in required):
                    forced, why = True, ("vehicles.meta requires it" if n in required else "FLAG_EXTRAS_ALL")
                p = np.concatenate(by_extra[n]) if by_extra.get(n) else np.zeros((0, 3))
                out.append({"id": ("extra", n), "keys": keys[("extra", n)], "forced": forced, "why": why,
                            "verts": len(p), "replaced": 0})
            for name, e in mods.items():
                if name not in self.mod_frags:
                    continue
                off = {self.names.index(t) for t in e["mod"].turn_off if t in self.names}
                hidden = [j for j in range(len(self.names)) if any(a in off for a in self._ancestors(j))]
                out.append({"id": ("mod", name), "keys": keys[("mod", name)],
                            "forced": self.mods_profile.get(name) if use_profile else None,
                            "verts": e["verts"], "replaced": sum(verts_of.get(j, 0) for j in hidden),
                            "slots": set(e["slots"])})
            return out

        keys = {}
        for n in extras:
            keys[("extra", n)] = kit.voxel_keys(np.concatenate(by_extra[n])) if by_extra.get(n) else np.zeros(0, np.int64)
        for name, e in mods.items():
            try:
                frag = load_yft(e["path"], name=e["path"])
            except Exception as ex:  # noqa: BLE001
                self.warnings.append(f"mod kit part {name}: cannot read {e['path']}: {ex}")
                continue
            if frag.drawable is None:
                continue
            self.mod_frags[name] = frag
            pts = [q for _, q in kit.drawable_points(frag.drawable)]
            e["verts"] = int(sum(len(q) for q in pts))
            keys[("mod", name)] = kit.voxel_keys(np.concatenate(pts)) if pts else np.zeros(0, np.int64)
        base_keys = kit.voxel_keys(np.concatenate(base)) if base else np.zeros(0, np.int64)
        auto = kit.plan(base_keys, candidates(False)) if (extras or self.mod_frags) else {}
        actual = kit.plan(base_keys, candidates(True)) if (extras or self.mod_frags) else {}
        self.parts_plan = {"auto": auto, "actual": actual}
        self.extras_default = {n: auto.get(("extra", n), (False, ""))[0] for n in extras}
        self.extras_on = {n for (k, n), (on, _) in actual.items() if k == "extra" and on}
        self.extras_on |= {n for n, on in self.extras_profile.items() if on and n not in self.extras_default}
        self.mods_on = {n for (k, n), (on, _) in actual.items() if k == "mod" and on}
        self.hidden_bones = set()
        for name in self.mods_on:
            for t in mods[name]["mod"].turn_off:
                if t in self.names:
                    self.hidden_bones.add(self.names.index(t))
        unknown = sorted(set(self.mods_profile) - set(mods))
        if unknown:
            self.warnings.append("profile: mod kit parts not in this car's kit: " + ", ".join(unknown))

    def _extra_of(self, i):
        """The extra_N bone that bone i belongs to (itself or an ancestor), or None."""
        for a in self._ancestors(i):
            if re.match(r"^extra_", self.names[a]):
                return self.names[a]
        return None

    def bone_role(self, i, overrides=True):
        """-> (role, key) for skeleton bone index i."""
        name = self.names[i]
        if self.hidden_bones and any(a in self.hidden_bones for a in self._ancestors(i)):
            return ("skip", name)                     # a fitted mod-kit part replaces it
        # an extra that is off hides everything below it, wherever it hangs (extras below the boot
        # used to show whatever the profile said)
        ex = self._extra_of(i)
        if ex and ex not in self.extras_on:
            ov = None
            if overrides:
                for a in self._ancestors(i):
                    ov = self._override_role(self.names[a])
                    if ov or self.names[a] == ex:
                        break
            return ov or ("extra", ex)
        lk = _light_kind(name)
        for a in self._ancestors(i):
            n = self.names[a]
            if overrides:
                ov = self._override_role(n)
                if ov:
                    return ov
            if lk and lk not in ("interior", "neon") and a != i:
                # a lamp on a door or a lid (tail lights split between body and boot, mirror
                # indicators): still a light, carried by that door/lid
                host = None
                if n in DOOR_BONES:
                    host = f"door_{DOOR_BONES[n]}_a"
                elif n == "bonnet":
                    host = "hood_a"
                elif n.startswith("boot"):
                    host = "trunk_a"
                if host:
                    self.light_host[i] = host
                    return ("light", lk)
            if n in WINDOW_BONES:
                return ("window", WINDOW_BONES[n])
            if n in DOOR_BONES:
                return ("door", DOOR_BONES[n])
            if n == "bonnet":
                return ("lid", "bonnet")
            if n.startswith("boot"):
                return ("lid", "boot")
            if n in ("steeringwheel", "steering_wheel", "steering"):
                return ("steering", "sw")
            if n in WHEEL_BONES:
                return ("wheel", WHEEL_BONES[n])
            if n in HUB_BONES:
                return ("hub", HUB_BONES[n])
        if name in SKIP_BONES:
            return ("skip", name)
        if lk:
            return ("light", lk)
        if ex:
            return ("body", name)                     # an extra that is on is part of the body
        if name == "windscreen":
            return ("glass", "front")
        if name == "windscreen_r":
            return ("glass", "rear")
        if name.startswith("seat") or name in ("gauge", "dials"):
            return ("interior", name)
        return ("body", name)

    def _check_overrides(self):
        """Keep the profile's bone roles that this skeleton can honour (a door role needs the GTA
        door bone the door part hangs from, and so on); warn about the others."""
        wanted = {str(k).lower(): v for k, v in (self.opt.get("bone_roles") or {}).items()}
        have = set(self.names)
        targets = {f"door:{k}": n for n, k in DOOR_BONES.items()}
        targets.update({f"window:{k}": n for n, k in WINDOW_BONES.items()})
        ok = {}
        for bone, role in wanted.items():
            if bone not in have:
                self.warnings.append(f"profile: bone '{bone}' is not in this model; its role is ignored")
                continue
            need = targets.get(role)
            if role == "lid:bonnet":
                need = "bonnet"
            elif role == "lid:boot":
                need = next((n for n in self.names if n.startswith("boot")), "boot")
            elif role == "steering":
                need = next((n for n in self.names if n in ("steeringwheel", "steering_wheel", "steering")), "steering")
            if need and need not in have:
                self.warnings.append(f"profile: '{bone}' -> {role} needs the GTA bone '{need}', which this model "
                                     "does not have; ignored")
                continue
            ok[bone] = role
        self.role_overrides = ok

    # ---------------------------------------------------------------- frames
    def setup_frames(self):
        wheels = {}
        for i, n in enumerate(self.names):
            if n in WHEEL_BONES and WHEEL_BONES[n] not in wheels:
                wheels[WHEEL_BONES[n]] = i
        if len(wheels) < 4:
            raise ValueError(f"{self.src.model}: needs four wheel bones (wheel_lf/rf/lr/rr), found {sorted(wheels)}")
        self.wheel_bones = wheels
        self.z0 = float(np.mean([self.world[i][2, 3] for i in wheels.values()]))
        self.base_z = float(self.opt.get("base_z", 0.44))
        self.shift = np.eye(4)
        self.shift[2, 3] = -self.z0

    def cp_world(self, i) -> np.ndarray:
        """Cyberpunk Root-space transform of GTA bone i (rotation orthonormalised)."""
        m = self.shift @ self.world[i]
        r = _orthonormal(m[:3, :3])
        out = np.eye(4)
        out[:3, :3] = r
        out[:3, 3] = m[:3, 3]
        return out

    # ------------------------------------------------------------- materials
    def material_for(self, shader_index: int, bone_role) -> str:
        sh = self.shaders[shader_index] if shader_index < len(self.shaders) else None
        sname = sh.name if sh else "unknown"
        role, rkey = bone_role[0], bone_role[1]
        lamp = role == "light" and rkey not in ("interior", "neon")
        diffuse_params = ("DiffuseSampler", "TextureSampler", "DiffuseTex")
        if sname.startswith("vehicle_lightsemissive") and lamp:
            # the lamp itself: Cyberpunk's vehicle light material (lit per zone by the car's light
            # components) wearing the GTA lamp texture, one material per GTA shader (they used to
            # share one untextured material, so every lamp showed the game's stock LED pattern)
            key = f"lights{shader_index:03d}"
            if key not in self.vehicle.materials:
                self.vehicle.materials[key] = Material(
                    key=key, kind="lights", gta_shader=sname, diffuse=self._tex(sh, *diffuse_params),
                    normal=self._tex(sh, "BumpSampler", "BumpTex", "NormalSampler"))
            return key
        if "vehglass" in sname and role == "light":
            key = f"m{shader_index:03d}_lc"                  # a lamp's lens
            if key not in self.vehicle.materials:
                self.vehicle.materials[key] = Material(key=key, kind="lightcover", gta_shader=sname, alpha="blend",
                                                       diffuse=self._tex(sh, *diffuse_params))
            return key
        glow = sname.startswith("vehicle_lightsemissive") and role == "light"     # interior lights
        key = f"m{shader_index:03d}" + ("_e" if glow else "")
        if key in self.vehicle.materials:
            return key
        diffuse = self._tex(sh, *diffuse_params)
        normal = self._tex(sh, "BumpSampler", "BumpTex", "NormalSampler")
        spec = self._tex(sh, "SpecSampler", "SpecTex")
        mat = Material(key=key, kind="textured", diffuse=diffuse, normal=normal, spec=spec, gta_shader=sname)
        mat.color = self._tint(sh.vec("matDiffuseColor") if sh else None)
        if sname.startswith("vehicle_paint"):
            num = re.search(r"vehicle_paint(\d)", sname)
            n = int(num.group(1)) if num else 1
            mat.kind = "paint"
            mat.paint_slot = 2 if n == 2 else 1
            mat.roughness = 0.15
            mat.metalness = 0.5
            # GTA multiplies the paint by DiffuseSampler (baked logos, trims, shading) and lays the
            # livery (DiffuseSampler2, second UV set) over it; both become decal overlays
            mat.livery = self._tex(sh, "DiffuseSampler2")
        elif "vehglass" in sname:
            mat.kind = "glass"
            mat.alpha = "blend"
        elif sname.startswith("vehicle_tire"):
            mat.kind = "tire"
            mat.roughness = 0.8
        elif sname.startswith(("vehicle_dash_emissive", "vehicle_emissive")) or glow:
            mat.kind = "emissive"
            mat.emissive_ev = 2.0 if sname.startswith("vehicle_dash") else 0.0
        elif sname.startswith("vehicle_lightsemissive"):
            # a lamp texture on a bone that is no light (and no lamp bone near it): GTA never lights
            # it either, so it is plain textured geometry rather than a patch that always glows
            mat.kind = "textured"
        elif sname.startswith(("vehicle_badges", "vehicle_decal", "vehicle_cutout", "vehicle_detail", "vehicle_blurredrotor")):
            mat.kind = "decal"
            mat.alpha = "test"
        elif sname.startswith("vehicle_licenseplate"):
            mat.kind = "plate"
        if mat.diffuse is None and mat.kind not in ("paint", "glass", "lights", "plate"):
            self.missing_textures.add(self._texname(sh, "DiffuseSampler") or f"(none on {sname})")
        self.vehicle.materials[key] = mat
        return key

    def _slot_colors(self):
        """The car's six GTA colour slots (sRGB 0..1): primary, secondary, pearl, wheels, interior
        trim, dashboard -- carvariations' first colour combination, with sensible stand-ins."""
        if getattr(self, "_slots", None) is not None:
            return self._slots
        var = self.src.variation
        idx = var.colors[0] if var and var.colors else []
        stand_in = [(0.2, 0.2, 0.2), None, None, (0.55, 0.55, 0.57), (0.12, 0.12, 0.12), (0.12, 0.12, 0.12)]
        out = []
        for k in range(6):
            c = gta_color(idx[k]) if k < len(idx) else None
            out.append(c or stand_in[k] or out[0])
        self._slots = out
        return out

    def _tint(self, dc):
        """GTA's matDiffuseColor -> the RGB multiplier (linear) of a textured material.  Vehicle models
        mark colour slots with it: (2, n, n) tints the texture with the car's colour n (1 primary,
        2 secondary, 3 pearl, 4 wheels, 5 interior trim, 6 dashboard: what GTA's rim, trim and
        dashboard colours change).  A plain 0..1 colour is used as it is; anything else (some tools
        write odd values) is ignored rather than blowing the texture out."""
        if not dc:
            return (1.0, 1.0, 1.0, 1.0)
        x, y, z = (float(c) for c in dc[:3])
        n = int(round(y))
        if abs(x - 2.0) < 1e-3 and abs(y - n) < 1e-3 and abs(z - y) < 1e-3 and 1 <= n <= 6:
            r, g, b = (float(c) for c in _srgb_to_linear(self._slot_colors()[n - 1]))
            return (r, g, b, 1.0)
        if all(0.0 <= c <= 1.0 for c in (x, y, z)):
            return (x, y, z, 1.0)
        return (1.0, 1.0, 1.0, 1.0)

    def _texname(self, sh, *params):
        if sh is None:
            return None
        return sh.tex(*params)

    def _tex(self, sh, *params):
        n = self._texname(sh, *params)
        if not n:
            return None
        n = n.lower()
        if n in NO_TEXTURE:
            return None
        if n not in self.textures:
            self._extra_texture(n)
        if n not in self.textures:
            alt = self.tex_loose.get(_loose(n))
            if not alt and self._extra_texture(n, loose=True):
                alt = self.tex_loose.get(_loose(n))
            if alt:
                self.renamed_textures[n] = alt
                n = alt
        if n in self.textures and self.textures[n].supported:
            self.used_textures.add(n)
            return n
        if n in self.textures:
            self.unsupported_textures.add(f"{n} ({self.textures[n].format_name})")
        else:
            self.missing_textures.add(n)
        return None

    def _extra_texture(self, n, loose=False) -> bool:
        """Load the dictionary of the car's resource that has texture n (by its exact name, or with
        loose=True ignoring case, spaces and punctuation when only one texture matches).  The names
        are read once without the pixels; only a dictionary that has the texture is loaded."""
        if not getattr(self, "_extra", None):
            return False
        if self._extra_index is None:
            idx, li = {}, {}
            for path in self._extra:
                try:
                    names = ytd_names(path, name=path)
                except Exception as e:  # noqa: BLE001
                    self.warnings.append(f"cannot read texture dictionary {path}: {e}")
                    continue
                for k in names:
                    idx.setdefault(k, path)
                    lk = _loose(k)
                    li[lk] = None if lk in li and li[lk] != (k, path) else (k, path)
            self._extra_index, self._extra_loose = idx, li
        path = self._extra_index.get(n)
        if path is None and loose:
            hit = self._extra_loose.get(_loose(n))
            path = hit[1] if hit else None
        if path is None or path in self._extra_loaded:
            return False
        self._extra_loaded.add(path)
        try:
            got = load_ytd(path, name=path)
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"cannot read texture dictionary {path}: {e}")
            return False
        for k, v in got.items():
            if k not in self.textures:
                self.textures[k] = v
                lk = _loose(k)
                self.tex_loose[lk] = None if lk in self.tex_loose and self.tex_loose[lk] != k else k
        return True

    # ------------------------------------------------------- texture looks
    def _image(self, name, max_size=256):
        """Decoded texture (h, w, 4) float 0..1 (a mip no bigger than max_size), cached; None when
        the texture is missing or its format cannot be decoded here."""
        key = (name, max_size)
        if key not in self._images:
            tex = self.textures.get(name)
            rgba = texture_rgba(tex, max_size) if tex is not None else None
            self._images[key] = rgba.astype(np.float32) / 255.0 if rgba is not None else None
        return self._images[key]

    @staticmethod
    def _sample(img, uv):
        """Nearest texel of img at uv (N, 2), wrapping like the GPU (D3D: v grows downwards)."""
        h, w = img.shape[:2]
        x = np.clip((np.mod(uv[:, 0], 1.0) * w).astype(np.int64), 0, w - 1)
        y = np.clip((np.mod(uv[:, 1], 1.0) * h).astype(np.int64), 0, h - 1)
        return img[y, x]

    def _glass_split(self, mat_key, base, sel):
        """Split glass triangles by what their texture says (GTA glass textures are often palettes:
        a clear quadrant, a tinted one, an opaque black one for the frit and the B-pillars).
        -> [(material key, triangles)].  Opaque texels become solid textured geometry; clear ones
        glass tinted the way GTA blends them (1 - a * (1 - rgb)); a triangle that crosses from
        clear to opaque keeps the glass and gets an alpha-tested copy for its opaque part."""
        m = self.vehicle.materials[mat_key]
        if m.kind not in ("glass", "lightcover") or not m.diffuse:
            return [(mat_key, sel, 0.0)]
        img = self._image(m.diffuse)
        uv0 = base.get("uv0")
        if img is None or uv0 is None or not len(sel):
            return [(mat_key, sel, 0.0)]
        uv = np.asarray(uv0, np.float64)[sel]                                   # (T, 3, 2)
        bary = np.array([[1 / 3, 1 / 3, 1 / 3], [1, 0, 0], [0, 1, 0], [0, 0, 1],
                         [0.6, 0.2, 0.2], [0.2, 0.6, 0.2], [0.2, 0.2, 0.6],
                         [0.5, 0.5, 0], [0, 0.5, 0.5], [0.5, 0, 0.5]])
        pts = np.einsum("sk,tkd->tsd", bary, uv)                                 # (T, S, 2)
        px = self._sample(img, pts.reshape(-1, 2)).reshape(len(sel), len(bary), 4)
        a = px[..., 3]
        opaque = a >= OPAQUE_GLASS
        all_opaque = opaque.all(1)
        any_opaque = opaque.any(1)
        clear = ~opaque
        # tint of the clear samples of each triangle
        w = clear.astype(np.float64)
        tint_px = 1.0 - px[..., 3:4] * (1.0 - px[..., :3])
        tint = (tint_px * w[..., None]).sum(1) / np.maximum(w.sum(1, keepdims=True), 1e-6)
        out = []
        if all_opaque.any():
            ok = f"{mat_key}_o"
            if ok not in self.vehicle.materials:
                self.vehicle.materials[ok] = Material(key=ok, kind="glass_opaque", diffuse=m.diffuse,
                                                      gta_shader=m.gta_shader, roughness=0.12)
            out.append((ok, sel[all_opaque], 0.0))
        rest = ~all_opaque
        if rest.any():
            # one glass material per tint (eighths per channel: stable keys whatever the geometry)
            q = np.round(np.clip(tint[rest], 0, 1) * 8.0).astype(np.int64)
            keys, inv = np.unique(q, axis=0, return_inverse=True)
            idx = np.flatnonzero(rest)
            for ci, k in enumerate(keys):
                members = idx[inv.reshape(-1) == ci]
                gk = f"{mat_key}_t{k[0]}{k[1]}{k[2]}"
                if gk not in self.vehicle.materials:
                    self.vehicle.materials[gk] = Material(key=gk, kind=m.kind, diffuse=m.diffuse, alpha="blend",
                                                          gta_shader=m.gta_shader,
                                                          tint=tuple(float(c) / 8.0 for c in k))
                out.append((gk, sel[members], 0.0))
            mixed = rest & any_opaque
            if mixed.any():
                ck = f"{mat_key}_c"
                if ck not in self.vehicle.materials:
                    self.vehicle.materials[ck] = Material(key=ck, kind="glass_cutout", diffuse=m.diffuse,
                                                          gta_shader=m.gta_shader, alpha="test", roughness=0.12)
                out.append((ck, sel[mixed], CUTOUT_OFFSET))
        return out

    # -------------------------------------------------------------- geometry
    def _part_for(self, role, key, lod):
        """Return the IR part name a piece goes to (None to drop)."""
        r = role
        if r in ("skip", "extra") and key not in self.extras_on:
            return None
        if r == "extra":
            r = "body"
        if r == "door":
            return f"door_{key}_a"
        if r == "window":
            return f"window_{key}_a"
        if r == "lid":
            return "hood_a" if key == "bonnet" else "trunk_a"
        if r == "steering":
            return "steering_wheel"
        if r == "glass":
            return "window_f" if key == "front" else "window_b"
        if r == "wheel":
            return None  # wheels come from the wheel meshes, see collect_wheels
        if r == "hub":
            return f"hub_{key}"
        if r == "light":
            return None  # routed by collect_lights
        if r == "interior":
            return "interior_01"
        return "body_01"

    def collect(self):
        self.vehicle.parts = {}
        self.acc = defaultdict(dict)          # part -> {(material, lod): _Accum}
        self.part_bones = defaultdict(set)
        self.light_pieces = []                # (lod, bone index, shader, attrs, tri, host part or None)
        self.loose_lamps = []                 # lamp-shader pieces on other bones (see collect_lights)
        self.wheel_pieces = defaultdict(list)  # wheel key -> [(lod, bone index, shader, attrs, tri)]
        self.wheel_dx = {}                     # wheel key -> tyre centre minus bone, across the car (m)
        lods = self.opt.get("lods", ("high", "med", "low", "vlow"))
        for lod_name, models in self.drawable.models.items():
            if lod_name not in lods:
                continue
            for model in models:
                for g in model.geometries:
                    self._take_geometry(g, model, LOD_MASKS[lod_name])
        self._collect_mods(lods)

    def _collect_mods(self, lods):
        """Geometry of the fitted mod-kit parts: authored in the car's model space, moved by the
        car bone each is fitted on; their shaders and embedded textures join the car's."""
        self.shaders = list(self.shaders)
        for name in sorted(self.mods_on):
            frag = self.mod_frags.get(name)
            if frag is None or frag.drawable is None:
                continue
            d = frag.drawable
            mod = self.mod_info[name]["mod"]
            bone = self.names.index(mod.bone) if mod.bone in self.names else 0
            offset = len(self.shaders)
            self.shaders.extend(d.shader_group.shaders)
            for k, t in d.shader_group.textures.items():
                if k not in self.textures:
                    self.textures[k] = t
                    lk = _loose(k)
                    self.tex_loose[lk] = None if lk in self.tex_loose and self.tex_loose[lk] != k else k
            sk = d.skeleton
            world = [sk.world_matrix(i) for i in range(len(sk.bones))] if sk else []
            for lod_name, models in d.models.items():
                if lod_name not in lods:
                    continue
                for model in models:
                    for g in model.geometries:
                        self._take_geometry(g, model, LOD_MASKS[lod_name], shader_offset=offset, bone=bone, world=world)

    def _take_geometry(self, g, model, lod, shader_offset=0, bone=None, world=None):
        """One geometry of the car's drawable -- or of a mod-kit part (bone: the car bone it is fitted
        on, world: the part's own skeleton for its rigid models, shader_offset: where its shaders
        start in self.shaders)."""
        attrs = g.attrs
        if "position" not in attrs or g.triangle_count == 0:
            return
        world = world if world is not None else self.world
        pos = attrs["position"].astype(np.float64)
        n = len(pos)
        tri = g.indices.reshape(-1, 3)
        if model.has_skin and "blendweights" in attrs and "blendindices" in attrs:
            bw, bi = attrs["blendweights"], attrs["blendindices"]
            dom_slot = bi[np.arange(n), bw.argmax(1)]
            pal = np.array(g.bone_ids if g.bone_ids else range(256), dtype=np.int64)
            vbone = pal[np.clip(dom_slot, 0, len(pal) - 1)]
            model_space = True
        else:
            vbone = np.full(n, model.bone_index, dtype=np.int64)
            model_space = False
        # vertices of rigid (non-skinned) models are expressed in their bone's frame
        rot = None
        if not model_space and model.bone_index < len(world):
            w = world[model.bone_index]
            rot = w[:3, :3]
            pos = pos @ rot.T + w[:3, 3]
        if bone is not None:
            vbone = np.full(n, bone, dtype=np.int64)   # a kit part moves with the bone it is fitted on
        nrm = attrs.get("normal")
        if nrm is None:
            nrm = np.zeros((n, 3))
            nrm[:, 2] = 1
        else:
            nrm = nrm.astype(np.float64)
            if rot is not None:
                nrm = nrm @ rot.T
        tan = attrs.get("tangent")
        if tan is not None and tan.shape[1] >= 4:
            tan = tan.astype(np.float64)
            if rot is not None:
                tan = np.concatenate([tan[:, :3] @ rot.T, tan[:, 3:4]], 1)
        else:
            tan = None
        uv0 = attrs.get("texcoord0")
        if uv0 is None:
            uv0 = np.zeros((n, 2))
        uv1 = attrs.get("texcoord1")
        base = {"position": pos, "normal": nrm, "tangent": tan, "uv0": uv0, "uv1": uv1,
                "color": None}
        if base["tangent"] is None:
            base["tangent"] = compute_tangents(pos, _normalize(nrm), uv0.astype(np.float64), tri)
        # triangle owner = bone of its first vertex (vehicle parts are rigid, triangles don't straddle)
        tbone = vbone[tri[:, 0]]
        sidx = shader_offset + g.shader_index
        sh = self.shaders[sidx] if sidx < len(self.shaders) else None
        lamp_shader = bool(sh and sh.name.startswith("vehicle_lightsemissive"))
        for b in np.unique(tbone):
            if b < 0 or b >= len(self.names):
                continue
            sel = tri[tbone == b]
            role = self.bone_role(int(b))
            if role[0] == "light":
                if lod == 1:
                    self.bone_verts[self.names[int(b)]] += int(len(np.unique(sel)))
                self.light_pieces.append((lod, int(b), sidx, base, sel, self.light_host.get(int(b))))
                continue
            if lod == 1 and bone is None:
                self.bone_verts[self.names[int(b)]] += int(len(np.unique(sel)))
            part = self._part_for(role[0], role[1], lod)
            if part is None:
                if role[0] == "wheel":
                    # wheel geometry modelled in the main drawable (no physics-child wheel drawable)
                    self.wheel_pieces[role[1]].append((lod, int(b), sidx, base, sel))
                continue
            if lamp_shader and role[0] in ("body", "lid", "door"):
                # lamp geometry on a bone that is no light: the nearest light bone may claim it
                # (see collect_lights)
                self.loose_lamps.append((lod, int(b), sidx, base, sel, part, role))
                continue
            mat = self.material_for(sidx, role)
            self.part_bones[part].add(self.names[int(b)])
            self._add_tris(part, mat, lod, base, sel)

    def _add_tris(self, part, mat, lod, base, sel):
        """Queue triangles of one material for a part (glass is split by its texture first)."""
        for key, s, off in self._glass_split(mat, base, sel):
            attrs = _offset(base, off) if off else base
            acc = self.acc[part].setdefault((key, lod), _Accum(key, lod))
            acc.add(attrs, s)

    # ------------------------------------------------------------- wheels
    def collect_wheels(self):
        """Wheel meshes: physics children drawables (preferred) or main-model wheel geometry."""
        tag_to_bone = {}
        for i, b in enumerate(self.sk.bones):
            tag_to_bone.setdefault(b.tag, i)
        meshes = {}
        for child in self.frag.children:
            if child.drawable is None:
                continue
            bi = tag_to_bone.get(child.bone_tag)
            if bi is None or self.names[bi] not in WHEEL_BONES:
                continue
            # GTA draws every wheel with the front one when the others' drawables are empty (most
            # cars ship one wheel mesh): only a drawable with geometry counts
            pieces = self._drawable_pieces(child.drawable)
            if pieces:
                meshes[WHEEL_BONES[self.names[bi]]] = (pieces, bi)
        front = meshes.get("fl") or meshes.get("fr") or meshes.get("bl") or meshes.get("br")
        rear = meshes.get("bl") or meshes.get("br") or front
        self.vehicle.axles = {}
        if front is not None:
            for axle, (pieces, bi) in (("front", front), ("back", rear)):
                mirror = self.names[bi] in ("wheel_rf", "wheel_rr")
                self._wheel_meshes(axle, pieces, mirror)
            return
        # some models carry the wheels in the main drawable, bound to the wheel bones
        if self.wheel_pieces:
            # such a wheel spins about its bone's X axis, but the bone need not sit in the middle
            # of the tyre across the car (it does not matter in GTA, where each wheel keeps its own
            # geometry).  Here one mesh serves both sides, so centre it and move the corner instead.
            for key, pcs in self.wheel_pieces.items():
                xs = [base["position"][np.unique(sel.reshape(-1)), 0] for lod, b, sh, base, sel in pcs if lod == 1]
                if xs:
                    xs = np.concatenate(xs)
                    bx = self.world[pcs[0][1]][0, 3]
                    self.wheel_dx[key] = float((xs.min() + xs.max()) / 2 - bx)
            def pick(*keys):
                for k in keys:
                    if self.wheel_pieces.get(k):
                        return k
                return None
            fk = pick("fl", "fr")
            rk = pick("bl", "br") or fk
            if fk:
                for axle, key in (("front", fk), ("back", rk)):
                    self._wheel_meshes(axle, self._main_wheel_pieces(key), key in ("fr", "br"))
                self.warnings.append("wheels taken from the main model (it has no separate wheel drawables)")
                return
        self.warnings.append("no wheel geometry found (neither wheel drawables nor wheel-bound parts); "
                             "wheels will be missing")

    @staticmethod
    def _drawable_pieces(drawable):
        """[(lod, shader, position, normal, tri, tangent, uv0, uv1)] of a wheel drawable (wheel-local)."""
        out = []
        for lod_name, models in drawable.models.items():
            lod = LOD_MASKS[lod_name]
            for model in models:
                for g in model.geometries:
                    if g.triangle_count == 0:
                        continue
                    a = g.attrs
                    out.append((lod, g.shader_index, a["position"].astype(np.float64), a.get("normal"),
                                g.indices.reshape(-1, 3), a.get("tangent"), a.get("texcoord0"), a.get("texcoord1")))
        return out

    def _main_wheel_pieces(self, key):
        """Main-drawable geometry bound to wheel bone ``key``, moved into that bone's frame."""
        out = []
        for lod, b, shader_index, base, sel in self.wheel_pieces.get(key, []):
            used, inv = np.unique(sel.reshape(-1), return_inverse=True)
            tri = inv.reshape(-1, 3)
            w = self.world[b]
            r = _orthonormal(w[:3, :3])
            centre = w[:3, 3] + r[:, 0] * self.wheel_dx.get(key, 0.0)
            p = (base["position"][used] - centre) @ r        # model space -> wheel space (centred)
            nrm = base["normal"][used] @ r
            tan = base["tangent"][used] if base.get("tangent") is not None else None
            if tan is not None:
                tan = np.concatenate([tan[:, :3] @ r, tan[:, 3:4]], 1)
            uv0 = base["uv0"][used] if base.get("uv0") is not None else None
            uv1 = base["uv1"][used] if base.get("uv1") is not None else None
            out.append((lod, shader_index, p, nrm, tri, tan, uv0, uv1))
        return out

    def _wheel_meshes(self, axle, pieces_in, mirror):
        all_r = []
        pieces = []
        for lod, shader_index, p, nrm, tri, tan, uv0, uv1 in pieces_in:
            p = p.astype(np.float64)
            if mirror:
                p = p * np.array([-1.0, 1.0, 1.0])
            r = np.sqrt(p[:, 1] ** 2 + p[:, 2] ** 2)
            if lod == 1:
                all_r.append(r)
            pieces.append((lod, shader_index, p, r, nrm, tri, tan, uv0, uv1))
        if not pieces:
            return
        rmax = float(np.concatenate(all_r).max()) if all_r else 0.35
        tire_pts = []
        for lod, shader_index, p, r, nrm, tri, tan, uv0, uv1 in pieces:
            # tyre pieces sit in a ring near the outer radius; rims/brakes/caps reach in to the hub
            is_tire = float(np.percentile(r, 10)) > 0.6 * rmax
            nrm = np.zeros_like(p) if nrm is None else np.asarray(nrm, dtype=np.float64)[:, :3]
            tri = np.asarray(tri)
            if mirror:
                nrm = nrm * np.array([-1.0, 1.0, 1.0])
                tri = tri[:, [0, 2, 1]]
            uv0 = np.zeros((len(p), 2)) if uv0 is None else uv0
            if tan is None or mirror or np.asarray(tan).shape[1] < 4:
                tan = compute_tangents(p, _normalize(nrm), np.asarray(uv0, dtype=np.float64), tri)
            base = {"position": p, "normal": nrm, "tangent": tan, "uv0": uv0, "uv1": uv1, "color": None}
            part = f"tire_{axle}" if is_tire else f"wheel_{axle}"
            mat = self.material_for(shader_index, ("wheel", axle))
            if is_tire:
                self.vehicle.materials[mat].kind = "tire" if self.vehicle.materials[mat].kind == "textured" else self.vehicle.materials[mat].kind
            acc = self.acc[part].setdefault((mat, lod), _Accum(mat, lod))
            acc.add(base, tri)
            if is_tire and lod == 1:
                tire_pts.append(p)
        if tire_pts:
            tp = np.concatenate(tire_pts)
            tr = np.sqrt(tp[:, 1] ** 2 + tp[:, 2] ** 2)
            width = float(tp[:, 0].max() - tp[:, 0].min())
            ring = tr[tr > 0.5 * rmax]  # ignore inner backing discs modelled with the tyre
            rim = float(np.percentile(ring, 2)) if len(ring) else 0.7 * rmax
        else:
            allp = np.concatenate([pc[2] for pc in pieces if pc[0] == 1] or [pieces[0][2]])
            width = float(allp[:, 0].max() - allp[:, 0].min())
            rim = 0.7 * rmax
        self.vehicle.axles[axle] = WheelAxle(radius=rmax, width=width, rim_radius=min(rim, rmax * 0.95), positions={})

    # -------------------------------------------------------------- lights
    def collect_lights(self):
        """Route lamp geometry into light meshes with zone colours and lay out the light components.

        A lamp is geometry on a GTA light bone (headlight_l, brakelight_r, ...), and also lamp-shader
        geometry the modeller left on a body/door/lid bone near one: models whose lights live on
        misc_* bones (the light bones only mark where they are) still get working lights, and the
        rest of that geometry stays plain textured surface.  Lamps on a door or a lid get a light mesh
        of their own bound to that door/lid, so they move with it.  A light bone with no lamp geometry
        at all still casts its light (head and brake lights)."""
        zone_of = {"head": 0, "tail": 0, "indicator": 1, "extra": 2, "reverse": 2, "brake": 3}
        type_of = {"head": "Head", "extra": "Head", "tail": "Head", "reverse": "Reverse", "brake": "Brake"}
        # light bones that can claim lamp geometry: real lamps where lamps are -- at the ends of the
        # car or at its sides (mirror indicators) -- not one a modeller left at the origin
        wpos = np.array([self.cp_world(i)[:3, 3] for i in self.wheel_bones.values()])
        reach_y, reach_x = 0.5 * float(np.abs(wpos[:, 1]).max()), 0.85 * float(np.abs(wpos[:, 0]).mean())

        def outside(i):
            p = self.cp_world(i)[:3, 3]
            return abs(p[1]) >= reach_y or abs(p[0]) >= reach_x

        lamps = [i for i, n in enumerate(self.names)
                 if _light_kind(n) not in (None, "neon") and not n.endswith("_hi")
                 and self.bone_role(i)[0] == "light" and outside(i)]
        self.lamps_claimed = defaultdict(int)
        if self.loose_lamps:
            lp = np.array([self.world[i][:3, 3] for i in lamps]) if lamps else np.zeros((0, 3))
            for lod, b, shader_index, base, sel, part, role in self.loose_lamps:
                rest = sel
                cabin = re.match(r"^(interior|dash|dials|gauge|seat|steer|console|roof_in)", self.names[b])
                if len(lp) and not cabin:
                    c = np.asarray(base["position"], np.float64)[sel].mean(1)
                    d = np.linalg.norm(c[:, None, :] - lp[None, :, :], axis=2)
                    near = d.argmin(1)
                    claimed = d[np.arange(len(c)), near] <= LAMP_REACH
                    host = None
                    if role[0] == "door":
                        host = f"door_{role[1]}_a"
                    elif role[0] == "lid":
                        host = "hood_a" if role[1] == "bonnet" else "trunk_a"
                    for li in np.unique(near[claimed]):
                        s = sel[claimed & (near == li)]
                        if self.bone_role(lamps[li])[1] == "interior":
                            # next to a plate/door light: glows a little, where it is
                            mat = self.material_for(shader_index, ("light", "interior"))
                            self._add_tris(part, mat, lod, base, s)
                        else:
                            self.light_pieces.append((lod, lamps[li], shader_index, base, s, host))
                        if lod == 1:
                            self.lamps_claimed[self.names[b]] += int(len(s))
                    rest = sel[~claimed]
                if len(rest):
                    mat = self.material_for(shader_index, role)
                    self.part_bones[part].add(self.names[b])
                    self._add_tris(part, mat, lod, base, rest)
        lamps = [i for i in lamps if self.bone_role(i)[1] != "interior"]
        geo = defaultdict(lambda: [np.zeros(3), 0])              # light group -> (sum, count) Root space
        used = defaultdict(set)
        for lod, b, shader_index, base, sel, host in self.light_pieces:
            kind = self.bone_role(b)[1]
            name = self.names[b]
            if kind in ("interior", "neon"):
                # emissive interior details: keep them as interior geometry that glows a little
                mat = self.material_for(shader_index, ("light", "interior"))
                self._add_tris("interior_01", mat, lod, base, sel)
                continue
            if name.endswith("_hi"):
                continue  # duplicate high-detail indicator meshes
            pos = self.cp_world(b)[:3, 3]
            end = "f" if pos[1] >= 0 else "b"
            side = "l" if pos[0] < -0.05 else ("r" if pos[0] > 0.05 else "m")
            if side == "m":
                side = "l" if end == "f" else "m"
            part = f"lights_{end}{side}" if host is None else f"{host}_lights_{side}"
            zone = zone_of.get(kind, 0)
            mat = self.material_for(shader_index, ("light", kind))
            n = len(base["position"])
            color = np.zeros((n, 4))
            color[:, 0] = 0.2 * (zone + 1)
            color[:, 3] = 1.0
            attrs = dict(base)
            attrs["color"] = color
            self._add_tris(part, mat, lod, attrs, sel)
            self.part_bones[part].add(name)
            if lod == 1:
                used[(part, zone, kind)].add(b)
                pts = np.asarray(base["position"], np.float64)[np.unique(sel)] + self.shift[:3, 3]
                g = geo[(part, zone, kind)]
                g[0] = g[0] + pts.sum(0)
                g[1] += len(pts)
        # light components
        self.vehicle.lights = []

        def spec(part, zone, kind, bones):
            pts = np.array([self.cp_world(b)[:3, 3] for b in bones])
            p = pts.mean(0)
            gs, gn = geo.get((part, zone, kind), (None, 0))
            if gn and np.linalg.norm(gs / gn - p) > 0.3:
                p = gs / gn                       # the bone is far from its lamp: light from the lamp
            if kind == "indicator":
                ltype = "LeftBlinker" if p[0] < 0 else "RightBlinker"
            else:
                ltype = type_of.get(kind, "Head")
            facing_back = p[1] < 0
            f = self.part_frame(part)
            local = (p - f[:3, 3]) @ f[:3, :3]
            d = np.array([0.0, -1.0 if facing_back else 1.0, -0.05]) @ f[:3, :3]
            tag = part[7:] if part.startswith("lights_") else part.replace("_lights_", "_")
            return LightSpec(
                name=f"{kind}_{tag}_{zone}", light_type=ltype, zone=zone, part=part,
                position=tuple(float(x) for x in local), direction=tuple(float(x) for x in d),
                color=(255, 40, 40) if kind in ("brake", "tail") else ((255, 150, 30) if kind == "indicator" else (255, 250, 220)))

        for (part, zone, kind), bones in sorted(used.items()):
            self.vehicle.lights.append(spec(part, zone, kind, bones))
        # light bones with no lamp geometry anywhere near them still light the road (head) and show
        # the brake light; they hang from the body
        lit = {b for bones in used.values() for b in bones}
        for b in lamps:
            kind = self.bone_role(b)[1]
            if b in lit or kind not in ("head", "brake"):
                continue
            pos = self.cp_world(b)[:3, 3]
            if any(l.light_type == type_of[kind] and np.linalg.norm(
                    np.array(self._light_root(l)) - pos) < 0.3 for l in self.vehicle.lights):
                continue
            self.vehicle.lights.append(spec("body_01", zone_of[kind], kind, [b]))
            self.vehicle.lights[-1].name = f"{kind}_{self.names[b]}_bone"
        self._synth_lights()

    def _synth_lights(self):
        """A car with no headlights or brake lights at all (concept-car models without light bones)
        gets them at the corners of its nose and tail, so it can still be driven at night."""
        have = {l.light_type for l in self.vehicle.lights}
        want = [k for k, t in (("head", "Head"), ("brake", "Brake")) if t not in have]
        self.synth_lights = []
        if not want or "body_01" not in self.acc:
            return
        pts = [sub["position"] for (m, lod), acc in self.acc["body_01"].items() if lod == 1 for sub, _ in acc.chunks]
        if not pts:
            return
        p = np.concatenate(pts).astype(np.float64) + self.shift[:3, 3]           # Root space
        y0, y1 = float(p[:, 1].min()), float(p[:, 1].max())
        half_w = 0.5 * float(p[:, 0].max() - p[:, 0].min())
        for kind in want:
            front = kind == "head"
            end = p[p[:, 1] > y1 - 0.35] if front else p[p[:, 1] < y0 + 0.35]
            if not len(end):
                continue
            z = float(end[:, 2].min() + 0.55 * (end[:, 2].max() - end[:, 2].min()))
            y = (y1 - 0.12) if front else (y0 + 0.12)
            for side, sx in (("fl" if front else "bl", -1.0), ("fr" if front else "br", 1.0)):
                pos = (sx * 0.62 * half_w, y, z - self.base_z)
                self.vehicle.lights.append(LightSpec(
                    name=f"{kind}_{side}_synth", light_type="Head" if front else "Brake", zone=0 if front else 3,
                    part="body_01", position=pos, direction=(0.0, 1.0 if front else -1.0, -0.05),
                    color=(255, 250, 220) if front else (255, 40, 40)))
                self.synth_lights.append(f"{kind}_{side}_synth")

    def _light_root(self, light):
        """Root-space position of a LightSpec (positions are in the frame of the part it hangs from)."""
        f = self.part_frame(light.part)
        return tuple((np.asarray(light.position, np.float64) @ f[:3, :3].T) + f[:3, 3])

    # ----------------------------------------------------------- assemble
    def part_frame(self, part):
        """Root-space 4x4 frame of the rig bone a part is bound to."""
        base = np.eye(4)
        base[2, 3] = self.base_z
        if "_lights_" in part:
            return self.part_frame(part.split("_lights_")[0])   # lamps carried by a door or a lid
        if part in ("window_f", "window_b"):
            return base
        if part.startswith("door_") or (part.startswith("window_") and part.endswith("_a")):
            key = part.split("_")[1]
            src = DOOR_BONES if part.startswith("door_") else WINDOW_BONES
            for n, k in src.items():
                if k == key and n in self.names:
                    return self.cp_world(self.names.index(n))
            return base
        if part in ("hood_a", "trunk_a"):
            want = "bonnet" if part == "hood_a" else "boot"
            for i, n in enumerate(self.names):
                if (want == "bonnet" and n == "bonnet") or (want == "boot" and n.startswith("boot")):
                    return self.cp_world(i)
            return base
        if part == "steering_wheel":
            idx = self._steering_bone()
            m = np.eye(4)
            if idx is not None:
                m[:3, 3] = self.cp_world(idx)[:3, 3]
            return m
        if part.startswith(("wheel_", "tire_")):
            return np.eye(4)  # wheel meshes are already wheel-local
        if part.startswith("hub_"):
            key = part[4:]
            for n, k in WHEEL_BONES.items():
                if k == key and n in self.names:
                    m = np.eye(4)
                    m[:3, 3] = self.cp_world(self.names.index(n))[:3, 3]
                    return m
        return base

    def _steering_column(self, v):
        """Steering column direction (Root space, pointing forward) from the wheel mesh: the axis of
        least spread of a disc.  None when the mesh does not look like a wheel."""
        part = v.parts.get("steering_wheel")
        if part is None:
            return None
        pts = [s.positions for s in part.submeshes if s.lod == 1 and len(s.positions)]
        if not pts:
            return None
        p = np.concatenate(pts).astype(np.float64)
        p -= p.mean(0)
        w, vec = np.linalg.eigh(p.T @ p / len(p))
        axis = vec[:, 0]
        if w[0] > 0.35 * w[1]:
            return None  # not disc-like (column or dash pieces dominate)
        if axis[1] < 0:
            axis = -axis
        if axis[1] < 0.5:  # columns lean forward; anything else is a misfit
            return None
        return tuple(float(x) for x in axis)

    def _steering_bone(self):
        best, best_n = None, -1
        for i, n in enumerate(self.names):
            if n in ("steeringwheel", "steering_wheel", "steering"):
                score = np.linalg.norm(self.world[i][:3, 3])
                if score > best_n:
                    best, best_n = i, score
        return best

    def assemble(self):
        v = self.vehicle
        for part, accs in self.acc.items():
            if part.startswith(("wheel_", "tire_")):
                frame = np.eye(4)
                world_space = False
            else:
                frame = self.part_frame(part)
                world_space = True
            r = frame[:3, :3]
            t = frame[:3, 3]
            subs = []
            for (mat, lod), acc in sorted(accs.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                for sm in acc.build():
                    if world_space:
                        # GTA model space -> Cyberpunk Root space -> bone-local
                        p = sm.positions.astype(np.float64) + self.shift[:3, 3]
                        sm.positions = ((p - t) @ r).astype(np.float32)
                        sm.normals = _normalize(sm.normals.astype(np.float64) @ r).astype(np.float32)
                        tt = sm.tangents.astype(np.float64)
                        sm.tangents = np.concatenate([_normalize(tt[:, :3] @ r), tt[:, 3:4]], 1).astype(np.float32)
                    else:
                        sm.normals = _normalize(sm.normals.astype(np.float64)).astype(np.float32)
                        tt = sm.tangents.astype(np.float64)
                        sm.tangents = np.concatenate([_normalize(tt[:, :3]), tt[:, 3:4]], 1).astype(np.float32)
                    subs.append(sm)
            kind = _kind_of(part)
            slot, parent = _binding_of(part)
            p = Part(name=part, mesh_name=part, slot=slot, parent=parent, kind=kind, submeshes=subs,
                     gta_bones=sorted(self.part_bones.get(part, [])))
            p.paintable = any(v.materials[s.material].kind == "paint" for s in subs)
            p.physical = kind not in ("lights",)
            v.parts[part] = p

    # --------------------------------------------------------------- rig
    def rig_overrides(self):
        """Absolute Root-space bone placements measured from the GTA skeleton."""
        v = self.vehicle
        bones = {}

        def put(name, m, parent="Base", gidx=None, default_axis=None):
            ax = self.open_axis.get(gidx) if gidx is not None else None
            if ax is None and default_axis is not None:
                ax = np.array(default_axis, float)
            bones[name] = {"t": m[:3, 3].copy(), "r": m[:3, :3].copy(), "parent": parent, "open_axis": ax}

        for gname, key in DOOR_BONES.items():
            if gname in self.names:
                gi = self.names.index(gname)
                put(f"door_{SIDE_NAMES[key]}_a", self.cp_world(gi), gidx=gi,
                    default_axis=(0, 0, -1) if key in ("fl", "bl") else (0, 0, 1))
        for gname, key in WINDOW_BONES.items():
            if gname in self.names:
                put(f"window_{SIDE_NAMES[key]}_a", self.cp_world(self.names.index(gname)),
                    parent=f"door_{SIDE_NAMES[key]}_a")
        for i, n in enumerate(self.names):
            if n == "bonnet":
                put("hood_a", self.cp_world(i), gidx=i, default_axis=(-1, 0, 0))
            elif n.startswith("boot") and "trunk_a" not in bones:
                put("trunk_a", self.cp_world(i), gidx=i, default_axis=(-1, 0, 0))
        for gname, key in WHEEL_BONES.items():
            if gname in self.names:
                m = np.eye(4)
                m[:3, 3] = self.cp_world(self.names.index(gname))[:3, 3] + np.array([self.wheel_dx.get(key, 0.0), 0, 0])
                put(f"wheel_{SIDE_NAMES[key]}", m, parent=f"suspension_{SIDE_NAMES[key]}")
        for gname, cname in SEAT_BONES.items():
            if gname in self.names:
                m = np.eye(4)
                m[:3, 3] = self.cp_world(self.names.index(gname))[:3, 3]
                put(cname, m)
                v.seats[cname] = tuple(m[:3, 3])
        sw = self._steering_bone()
        if sw is not None:
            put("steering_wheel_zro", self.cp_world(sw))
            col = self._steering_column(v)
            if col is not None:
                bones["steering_wheel_zro"]["column"] = col
        v.bones = bones
        v.exhausts = []
        for i, n in enumerate(self.names):
            if n.startswith("exhaust") and n != "exhaust_end":
                pos = self.cp_world(i)[:3, 3]
                if np.hypot(pos[0], pos[1]) > 0.05:
                    v.exhausts.append(tuple(float(x) for x in pos))
        # frames used to express each part's geometry (must match the rig bones above)
        v.part_frames = {}
        for part in v.parts:
            f = self.part_frame(part)
            v.part_frames[part] = (f[:3, 3].copy(), f[:3, :3].copy())
        # axle placement
        for axle, (l, r) in (("front", ("fl", "fr")), ("back", ("bl", "br"))):
            if axle in v.axles:
                for side in (l, r):
                    gname = [n for n, k in WHEEL_BONES.items() if k == side][0]
                    v.axles[axle].positions[side] = tuple(self.cp_world(self.names.index(gname))[:3, 3]
                                                          + np.array([self.wheel_dx.get(side, 0.0), 0, 0]))

    # ---------------------------------------------------------- extra axles
    def add_static_wheels(self):
        """Wheels on axles beyond the front and the rear one (a 6x6's middle axle: wheel_lm1,
        wheel_rm1 ...).  Cyberpunk simulates four wheels, so these ride fixed to the body, drawn with
        the rear axle's wheel and tyre: without them the car would have no wheels there at all."""
        v = self.vehicle
        src = {kind: v.parts.get(f"{kind}_back") or v.parts.get(f"{kind}_front") for kind in ("wheel", "tire")}
        if not any(src.values()):
            return
        tb, rb = (np.asarray(x, np.float64) for x in v.part_frames.get("body_01", (np.array([0.0, 0.0, self.base_z]), np.eye(3))))
        added = []
        for i, n in enumerate(self.names):
            m = EXTRA_WHEEL.match(n)
            if not m:
                continue
            side = m.group(1)
            pos = self.cp_world(i)[:3, 3]
            # the right side is the left wheel turned half round (as the rig turns the right wheels)
            r = np.eye(3) if side == "l" else np.diag([-1.0, -1.0, 1.0])
            to_body = r.T @ rb
            for kind, part in src.items():
                if part is None:
                    continue
                name = f"{kind}_static_{n[6:]}"
                subs = []
                for sm in part.submeshes:
                    root = sm.positions.astype(np.float64) @ r.T + pos
                    nrm = None if sm.normals is None else (sm.normals.astype(np.float64) @ to_body).astype(np.float32)
                    tan = None
                    if sm.tangents is not None:
                        tan = sm.tangents.astype(np.float64).copy()
                        tan[:, :3] = tan[:, :3] @ to_body
                        tan = tan.astype(np.float32)
                    subs.append(SubMesh(material=sm.material, positions=((root - tb) @ rb).astype(np.float32),
                                        normals=nrm, tangents=tan, uv0=sm.uv0, uv1=sm.uv1, colors=sm.colors,
                                        indices=sm.indices, lod=sm.lod))
                v.parts[name] = Part(name=name, mesh_name=name, slot=None, parent="body_01", kind=f"{kind}_static",
                                     submeshes=subs, physical=False, gta_bones=[n])
                v.part_frames[name] = (tb.copy(), rb.copy())
            added.append(n)
        if added:
            v.static_wheels = added
            self.warnings.append(f"wheels on a third axle ({', '.join(added)}) ride fixed to the body: Cyberpunk "
                                 "simulates four wheels (they do not spin or follow the suspension)")

    # ------------------------------------------------------------ main
    def build(self) -> Vehicle:
        self.load()
        self._check_overrides()
        self.setup_frames()
        info = self.src.info
        model = self.src.model
        doors = sum(1 for n in DOOR_BONES if n in self.names)
        name = _sanitize(self.opt.get("name") or model)
        disp, make = display_name_of(self.src)
        v = Vehicle(name=name, display_name=self.opt.get("display_name") or disp, make=make,
                    gta_class=info.vehicle_class if info else "VC_SPORT",
                    gta_layout=info.layout if info else "LAYOUT_STANDARD", door_count=doors,
                    handling=self.src.handling, base_z=self.base_z, gta_wheel_z=self.z0,
                    audio_name=(info.extra.get("audioNameHash", "") if info else "") or "")
        self.vehicle = v
        v.markers = {n: tuple(float(x) for x in self.cp_world(i)[:3, 3])
                     for i, n in enumerate(self.names) if n in ("neon_l", "neon_r", "neon_f", "neon_b")}
        self.used_textures = set()
        self.missing_textures = set()
        self.unsupported_textures = set()
        self.renamed_textures = {}
        self._images = {}
        self._plan_parts()
        self.collect()
        self.collect_wheels()
        self.collect_lights()
        self.overlay_report = add_paint_overlays(self)
        self.assemble()
        # doors that open: door parts with geometry (a GTA skeleton often carries all four door
        # bones whether the body has four doors or two)
        v.door_count = sum(1 for p in v.parts.values() if p.kind == "door")
        self.rig_overrides()
        self.add_static_wheels()
        # paint colours from carvariations
        var = self.src.variation
        if var and var.colors:
            c = var.colors[0]
            prim = gta_color(c[0]) if len(c) > 0 else None
            sec = gta_color(c[1]) if len(c) > 1 else None
            v.paint_colors = (prim or (0.2, 0.2, 0.2), sec or prim or (0.2, 0.2, 0.2))
        # materials no submesh uses (a glass split into tints, a shader whose geometry went to an extra
        # that is off) and the textures only they needed are left out
        used_mats = {sm.material for p in v.parts.values() for sm in p.submeshes}
        v.materials = {k: m for k, m in v.materials.items() if k in used_mats}
        refs = {t for m in v.materials.values() for t in (m.diffuse, m.normal) if t}
        v.textures = {k: self.textures[k] for k in sorted(self.used_textures) if k in refs}
        allp = np.concatenate([s.positions for p in v.parts.values() if p.kind in ("body",)
                               for s in p.submeshes if s.lod == 1]) if "body_01" in v.parts else np.zeros((1, 3))
        v.bbox = (tuple(allp.min(0) + [0, 0, self.base_z]), tuple(allp.max(0) + [0, 0, self.base_z]))
        if self.missing_textures:
            self.warnings.append(f"{len(self.missing_textures)} textures referenced but not in this resource "
                                 f"(GTA shared textures such as vehshare); flat colours are used instead: "
                                 + ", ".join(sorted(self.missing_textures)[:12])
                                 + (" ..." if len(self.missing_textures) > 12 else ""))
        if self.unsupported_textures:
            self.warnings.append(f"{len(self.unsupported_textures)} textures are in a format the converter cannot "
                                 "read yet; flat colours are used instead: " + ", ".join(sorted(self.unsupported_textures)[:8]))
        if getattr(self, "synth_lights", None):
            self.warnings.append("the model has no head/brake light bones; lights were added at the corners of "
                                 "its nose and tail: " + ", ".join(self.synth_lights))
        if self.renamed_textures:
            self.warnings.append("textures matched by a near name (the model's shader misspells them): " + ", ".join(
                f"{a} -> {b}" for a, b in sorted(self.renamed_textures.items())[:8]))
        extras = []
        actual = self.parts_plan.get("actual", {})
        auto = self.parts_plan.get("auto", {})
        for n in sorted(self.extras_default):
            i = self.names.index(n)
            verts = sum(c for b, c in self.bone_verts.items() if self._extra_of(self.names.index(b)) == n)
            extras.append({"name": n, "on": n in self.extras_on, "default": self.extras_default[n],
                           "required": n in self.extras_required, "verts": int(verts),
                           "why": actual.get(("extra", n), (None, ""))[1],
                           "parent": self.names[self.sk.bones[i].parent] if self.sk.bones[i].parent >= 0 else None})
        mods_detail = []
        for name, e in sorted(self.mod_info.items()):
            mods_detail.append({"model": name, "slots": sorted(e["slots"]), "bone": e["mod"].bone,
                                "label": e["mod"].label, "verts": int(e.get("verts", 0)),
                                "replaces": list(e["mod"].turn_off), "on": name in self.mods_on,
                                "default": bool(auto.get(("mod", name), (False, ""))[0]),
                                "why": actual.get(("mod", name), (None, "cannot be read"))[1]})
        fitted = [m for m in mods_detail if m["on"]]
        if fitted:
            self.warnings.append("mod kit parts fitted (the car needs them to be whole): " + ", ".join(
                f"{m['model']} ({'/'.join(x.replace('VMT_', '').lower() for x in m['slots'])})" for m in fitted[:8]))
        v.warnings = self.src.warnings + self.warnings
        lamps = defaultdict(int)
        for l in v.lights:
            lamps[l.light_type] += 1
        v.report = {
            "gta_bones": len(self.sk.bones),
            "parts": {k: {"verts": p.vertex_count(1), "submeshes": len([s for s in p.submeshes if s.lod == 1]),
                          "lods": sorted({s.lod for s in p.submeshes}), "gta_bones": p.gta_bones}
                      for k, p in v.parts.items()},
            "textures_used": len(v.textures),
            "textures_missing": sorted(self.missing_textures),
            "textures_unsupported": sorted(self.unsupported_textures),
            "textures_renamed": dict(sorted(self.renamed_textures.items())),
            "bones": self.bone_table(),
            "extras": sorted(self.extras_default),
            "extras_detail": extras,
            "mods": mods_detail,
            "lamps_claimed": dict(sorted(self.lamps_claimed.items())),
            "lights_added": list(getattr(self, "synth_lights", [])),
            "light_types": dict(sorted(lamps.items())),
            "overlays": self.overlay_report,
        }
        return v

    def bone_table(self) -> list:
        """Every GTA bone with its automatic and effective role and the part it ends up in."""
        rows = []
        part_of = {}
        for part, bones in self.part_bones.items():
            for b in bones:
                part_of.setdefault(b, part)
        for i, n in enumerate(self.names):
            auto = self.bone_role(i, overrides=False)
            eff = self.bone_role(i)
            p = self.sk.bones[i].parent
            rows.append({"index": i, "name": n, "parent": self.names[p] if p >= 0 else None,
                         "auto": ":".join(auto) if auto[0] in ("door", "window", "glass", "lid", "wheel", "hub", "light")
                         else auto[0],
                         "role": ":".join(eff) if eff[0] in ("door", "window", "glass", "lid", "wheel", "hub", "light")
                         else eff[0],
                         "override": self.role_overrides.get(n),
                         "part": part_of.get(n), "verts": int(self.bone_verts.get(n, 0))})
        return rows


def _normalize(a):
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(n, 1e-12)


def _offset(attrs: dict, dist: float) -> dict:
    """attrs with the positions pushed `dist` metres out along the normals (an overlay that sits
    just above the surface it decorates, like the game's own decal meshes)."""
    out = dict(attrs)
    out["position"] = np.asarray(attrs["position"], np.float64) + _normalize(np.asarray(attrs["normal"], np.float64)) * dist
    return out


def _kind_of(part: str) -> str:
    if "_lights_" in part:
        return "lights"
    if part.startswith("door_"):
        return "door"
    if part.startswith("window_") and part.endswith("_a"):
        return "window"
    if part in ("window_f", "window_b"):
        return "glass"
    if part in ("hood_a", "trunk_a"):
        return part[:-2]
    if part.startswith("wheel_"):
        return "wheel"
    if part.startswith("tire_"):
        return "tire"
    if part.startswith("hub_"):
        return "hub"
    if part.startswith("lights_"):
        return "lights"
    if part == "steering_wheel":
        return "steering"
    if part == "interior_01":
        return "interior"
    return "body"


def _binding_of(part: str):
    """(slot, parent component) for a part."""
    if "_lights_" in part:
        return _binding_of(part.split("_lights_")[0])        # rides on the door/lid's rig bone
    if part.startswith("door_"):
        return SIDE_SLOT("door", part), None
    if part.startswith("window_") and part.endswith("_a"):
        return SIDE_SLOT("window", part), None
    if part in ("hood_a", "trunk_a", "steering_wheel"):
        return part, None
    if part.startswith(("wheel_", "tire_", "hub_")):
        return None, None  # handled per corner by the .app writer
    if part in ("body_01", "interior_01"):
        return "Base", None
    return None, "body_01"


def SIDE_SLOT(prefix, part):
    key = part.split("_")[1]
    return f"{prefix}_{SIDE_NAMES[key]}_a"
