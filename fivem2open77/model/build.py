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
from ..gta.texture import load_ytd
from .colors import gta_color
from .ir import (LOD_MASKS, Bone, LightSpec, Material, Part, SubMesh, Vehicle,
                 WheelAxle)

MAX_VERTS = 65000  # Cyberpunk submeshes use 16-bit indices

WHEEL_BONES = {"wheel_lf": "fl", "wheel_rf": "fr", "wheel_lr": "bl", "wheel_rr": "br"}
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
    if pack and len(disp.split()) == 1 and disp.lower() in pack.lower() and len(pack) > len(disp) \
            and len(pack) <= 48:
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
        ex = self.opt.get("extras") or set()
        self.extras_on = {k.lower() for k, on in ex.items() if on} if isinstance(ex, dict) else {e.lower() for e in ex}
        self.bone_verts = defaultdict(int)     # GTA bone -> high-LOD vertices it carries

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

    def bone_role(self, i, overrides=True):
        """-> (role, key) for skeleton bone index i."""
        name = self.names[i]
        for a in self._ancestors(i):
            n = self.names[a]
            if overrides:
                ov = self._override_role(n)
                if ov:
                    return ov
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
        if re.match(r"^extra_", name):
            return ("extra", name)
        lk = _light_kind(name)
        if lk:
            return ("light", lk)
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
        role = bone_role[0]
        light = role == "light" and bone_role[1] not in ("interior", "neon")
        if sname.startswith("vehicle_lightsemissive") and light:
            key = "lights"
            if key not in self.vehicle.materials:
                self.vehicle.materials[key] = Material(key=key, kind="lights", gta_shader=sname)
            return key
        key = f"m{shader_index:03d}"
        if key in self.vehicle.materials:
            return key
        diffuse = self._tex(sh, "DiffuseSampler", "TextureSampler", "DiffuseTex", "DiffuseSampler2")
        normal = self._tex(sh, "BumpSampler", "BumpTex", "NormalSampler")
        spec = self._tex(sh, "SpecSampler", "SpecTex")
        mat = Material(key=key, kind="textured", diffuse=diffuse, normal=normal, spec=spec, gta_shader=sname)
        dc = sh.vec("matDiffuseColor") if sh else None
        if dc:
            mat.color = (float(dc[0]), float(dc[1]), float(dc[2]), 1.0)
        if sname.startswith("vehicle_paint"):
            num = re.search(r"vehicle_paint(\d)", sname)
            n = int(num.group(1)) if num else 1
            mat.kind = "paint"
            mat.paint_slot = 2 if n == 2 else 1
            mat.roughness = 0.15
            mat.metalness = 0.5
        elif "vehglass" in sname:
            mat.kind = "glass"
            mat.alpha = "blend"
        elif sname.startswith("vehicle_tire"):
            mat.kind = "tire"
            mat.roughness = 0.8
        elif sname.startswith(("vehicle_dash_emissive", "vehicle_emissive")) or sname.startswith("vehicle_lightsemissive"):
            mat.kind = "emissive"
            mat.emissive_ev = 2.0 if sname.startswith("vehicle_dash") else 0.0
        elif sname.startswith(("vehicle_badges", "vehicle_decal", "vehicle_cutout", "vehicle_detail", "vehicle_blurredrotor")):
            mat.kind = "decal"
            mat.alpha = "test"
        elif sname.startswith("vehicle_licenseplate"):
            mat.kind = "plate"
        if mat.diffuse is None and mat.kind not in ("paint", "glass", "lights"):
            self.missing_textures.add(self._texname(sh, "DiffuseSampler") or f"(none on {sname})")
        self.vehicle.materials[key] = mat
        return key

    def _texname(self, sh, *params):
        if sh is None:
            return None
        return sh.tex(*params)

    def _tex(self, sh, *params):
        n = self._texname(sh, *params)
        if not n:
            return None
        n = n.lower()
        if n in self.textures and self.textures[n].supported:
            self.used_textures.add(n)
            return n
        self.missing_textures.add(n)
        return None

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
        self.light_pieces = []                # (lod, bone index, material, attrs, tri)
        self.wheel_pieces = defaultdict(list)  # wheel key -> [(lod, bone index, shader, attrs, tri)]
        self.wheel_dx = {}                     # wheel key -> tyre centre minus bone, across the car (m)
        lods = self.opt.get("lods", ("high", "med", "low", "vlow"))
        for lod_name, models in self.drawable.models.items():
            if lod_name not in lods:
                continue
            for model in models:
                for g in model.geometries:
                    self._take_geometry(g, model, LOD_MASKS[lod_name])

    def _take_geometry(self, g, model, lod):
        attrs = g.attrs
        if "position" not in attrs or g.triangle_count == 0:
            return
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
        if not model_space:
            w = self.world[model.bone_index]
            pos = pos @ w[:3, :3].T + w[:3, 3]
        nrm = attrs.get("normal")
        if nrm is None:
            nrm = np.zeros((n, 3))
            nrm[:, 2] = 1
        else:
            nrm = nrm.astype(np.float64)
            if not model_space:
                nrm = nrm @ self.world[model.bone_index][:3, :3].T
        tan = attrs.get("tangent")
        if tan is not None and tan.shape[1] >= 4:
            tan = tan.astype(np.float64)
            if not model_space:
                tan = np.concatenate([tan[:, :3] @ self.world[model.bone_index][:3, :3].T, tan[:, 3:4]], 1)
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
        for b in np.unique(tbone):
            sel = tri[tbone == b]
            role = self.bone_role(int(b))
            if role[0] == "light":
                if lod == 1:
                    self.bone_verts[self.names[int(b)]] += int(len(np.unique(sel)))
                self.light_pieces.append((lod, int(b), g.shader_index, base, sel))
                continue
            if lod == 1:
                self.bone_verts[self.names[int(b)]] += int(len(np.unique(sel)))
            part = self._part_for(role[0], role[1], lod)
            if part is None:
                if role[0] == "wheel":
                    # wheel geometry modelled in the main drawable (no physics-child wheel drawable)
                    self.wheel_pieces[role[1]].append((lod, int(b), g.shader_index, base, sel))
                continue
            mat = self.material_for(g.shader_index, role)
            self.part_bones[part].add(self.names[int(b)])
            acc = self.acc[part].setdefault((mat, lod), _Accum(mat, lod))
            acc.add(base, sel)

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
        """Route vehicle_lightsemissive geometry into per-corner light meshes with zone colours."""
        zone_of = {"head": 0, "tail": 0, "indicator": 1, "extra": 2, "reverse": 2, "brake": 3}
        type_of = {"head": "Head", "extra": "Head", "tail": "Head", "reverse": "Reverse", "brake": "Brake"}
        used = defaultdict(set)
        for lod, b, shader_index, base, sel in self.light_pieces:
            kind = self.bone_role(b)[1]
            name = self.names[b]
            if kind in ("interior", "neon"):
                part, zone = None, None
            else:
                pos = self.cp_world(b)[:3, 3]
                end = "f" if pos[1] >= 0 else "b"
                side = "l" if pos[0] < -0.05 else ("r" if pos[0] > 0.05 else "m")
                if side == "m":
                    side = "l" if end == "f" else "m"
                part = f"lights_{end}{side}"
                zone = zone_of.get(kind, 0)
                if name.endswith("_hi"):
                    continue  # duplicate high-detail indicator meshes
            if part is None:
                # emissive interior details: keep them as plain textured interior geometry
                mat = self.material_for(shader_index, ("interior", name))
                acc = self.acc["interior_01"].setdefault((mat, lod), _Accum(mat, lod))
                acc.add(base, sel)
                continue
            mat = self.material_for(shader_index, ("light", kind))
            n = len(base["position"])
            color = np.zeros((n, 4))
            color[:, 0] = 0.2 * (zone + 1)
            color[:, 3] = 1.0
            attrs = dict(base)
            attrs["color"] = color
            acc = self.acc[part].setdefault((mat, lod), _Accum(mat, lod))
            acc.add(attrs, sel)
            self.part_bones[part].add(name)
            if lod == 1:
                used[(part, zone, kind)].add(b)
        # light components
        self.vehicle.lights = []
        for (part, zone, kind), bones in sorted(used.items()):
            pts = np.array([self.cp_world(b)[:3, 3] for b in bones])
            p = pts.mean(0)
            if kind == "indicator":
                ltype = "LeftBlinker" if p[0] < 0 else "RightBlinker"
            else:
                ltype = type_of.get(kind, "Head")
            facing_back = p[1] < 0
            self.vehicle.lights.append(LightSpec(
                name=f"{kind}_{part[7:]}_{zone}", light_type=ltype, zone=zone, part=part,
                position=(float(p[0]), float(p[1]), float(p[2] - self.base_z)),
                direction=(0.0, -1.0 if facing_back else 1.0, -0.05),
                color=(255, 40, 40) if kind in ("brake", "tail") else ((255, 150, 30) if kind == "indicator" else (255, 250, 220))))

    # ----------------------------------------------------------- assemble
    def part_frame(self, part):
        """Root-space 4x4 frame of the rig bone a part is bound to."""
        base = np.eye(4)
        base[2, 3] = self.base_z
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
        self.collect()
        self.collect_wheels()
        self.collect_lights()
        self.assemble()
        self.rig_overrides()
        # paint colours from carvariations
        var = self.src.variation
        if var and var.colors:
            c = var.colors[0]
            prim = gta_color(c[0]) if len(c) > 0 else None
            sec = gta_color(c[1]) if len(c) > 1 else None
            v.paint_colors = (prim or (0.2, 0.2, 0.2), sec or prim or (0.2, 0.2, 0.2))
        v.textures = {k: self.textures[k] for k in sorted(self.used_textures)}
        allp = np.concatenate([s.positions for p in v.parts.values() if p.kind in ("body",)
                               for s in p.submeshes if s.lod == 1]) if "body_01" in v.parts else np.zeros((1, 3))
        v.bbox = (tuple(allp.min(0) + [0, 0, self.base_z]), tuple(allp.max(0) + [0, 0, self.base_z]))
        if self.missing_textures:
            self.warnings.append(f"{len(self.missing_textures)} textures referenced but not in this resource "
                                 f"(GTA shared textures such as vehshare); flat colours are used instead: "
                                 + ", ".join(sorted(self.missing_textures)[:12])
                                 + (" ..." if len(self.missing_textures) > 12 else ""))
        v.warnings = self.src.warnings + self.warnings
        v.report = {
            "gta_bones": len(self.sk.bones),
            "parts": {k: {"verts": p.vertex_count(1), "submeshes": len([s for s in p.submeshes if s.lod == 1]),
                          "lods": sorted({s.lod for s in p.submeshes}), "gta_bones": p.gta_bones}
                      for k, p in v.parts.items()},
            "textures_used": len(v.textures),
            "textures_missing": sorted(self.missing_textures),
            "bones": self.bone_table(),
            "extras": sorted(n for n in self.names if re.match(r"^extra_", n)),
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


def _kind_of(part: str) -> str:
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
