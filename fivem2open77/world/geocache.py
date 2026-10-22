"""The game's own meshes for the Studio's 3D city: geometry and colours, read once and kept small.

WolvenKit exports a ``.mesh`` from the game (``uncook --mesh-export-type MeshOnly``) as a glTF binary
(``<mesh>.glb``: one glTF mesh per render chunk of the best LOD, named ``submesh_<chunk>_LOD_<lod>``,
Y up) and a ``<mesh>.Material.json`` (every material the mesh's appearances use, each with its
parameters, and per appearance the material of each chunk), plus a material repository next to it:
the multilayer setups (``.mlsetup.json``: the layers, each a surface template and a colour name)
and the surface templates (``.mltemplate.json``: the colour names a template knows, as linear RGB).
Proxy meshes (the far-away stand-ins of whole buildings) carry their own baked colour texture.

``build`` turns that into one compact file per mesh (``.geo``, read by the browser straight into
typed arrays):

    "F2GE" u32 version u32 header bytes  header (JSON)  then 4-aligned arrays:
        positions float32 (n, 3)   mesh space: metres, x right, y forward, z up (the game's own)
        normals   int8 (n, 3)      x127
        indices   uint16 | uint32
    header: {res, bounds, vertices, triangles, index: "u16"|"u32", chunks: [{chunk, v: [first, count],
             i: [first, count]}], apps: {appearance: [[r, g, b, kind] per chunk]}, default: name}

    kind: 0 solid, 1 glass, 2 light (emissive), 3 hidden (decals, shadow casters: not drawn)

The colours are what a map needs, not a renderer's: a multilayered material is the blend of its
layers' colours (the template's colour times a typical albedo of its surface), any other material
its colour parameter, its diffuse texture's average or, failing everything, a colour its surface
name suggests (concrete grey, asphalt dark, vegetation green...).
"""
from __future__ import annotations

import json
import os
import re
import struct
import zlib

import numpy as np

from ..cp77.glbread import accessor, read_glb

GEO_VERSION = 1
MAGIC = b"F2GE"
KIND_SOLID, KIND_GLASS, KIND_LIGHT, KIND_HIDDEN = 0, 1, 2, 3
SUBMESH_RX = re.compile(r"submesh_(\d+)_LOD_(\d+)", re.I)

# a surface's typical albedo (linear) by words in its template / texture / material path
PALETTE = (
    (("asphalt", "road", "tarmac"), (0.07, 0.07, 0.075)),
    (("concrete", "cement", "plaster", "stucco"), (0.33, 0.32, 0.30)),
    (("brick",), (0.32, 0.14, 0.09)),
    (("tile", "ceramic", "porcelain"), (0.55, 0.55, 0.53)),
    (("marble", "granite", "stone", "rock", "cliff"), (0.38, 0.36, 0.33)),
    (("sand", "dune"), (0.55, 0.45, 0.30)),
    (("dirt", "soil", "mud", "ground"), (0.20, 0.15, 0.10)),
    (("grass", "lawn", "vegetation", "foliage", "leaf", "leaves", "tree", "palm", "bush", "shrub", "plant", "ivy"),
     (0.10, 0.20, 0.06)),
    (("wood", "plank", "timber"), (0.30, 0.19, 0.10)),
    (("glass", "window", "windshield"), (0.30, 0.42, 0.50)),
    (("chrome", "steel", "aluminium", "aluminum", "metal", "iron", "tin"), (0.42, 0.42, 0.43)),
    (("rust",), (0.30, 0.13, 0.06)),
    (("copper", "bronze", "brass"), (0.45, 0.25, 0.12)),
    (("gold",), (0.65, 0.48, 0.15)),
    (("rubber", "tyre", "tire", "carbon"), (0.03, 0.03, 0.03)),
    (("plastic", "foam", "vinyl"), (0.30, 0.30, 0.30)),
    (("fabric", "cloth", "textile", "carpet", "canvas", "leather"), (0.25, 0.22, 0.20)),
    (("paint", "enamel", "lacquer"), (0.55, 0.55, 0.55)),
    (("water", "ocean"), (0.04, 0.10, 0.14)),
    (("neon", "emissive", "light", "screen", "hologram", "advert", "billboard"), (0.70, 0.40, 0.90)),
)
DEFAULT_ALBEDO = (0.36, 0.36, 0.37)
HIDDEN_WORDS = ("decal", "shadow", "proxy_collision", "invisible", "occluder", "_null", "editor\\")
GLASS_WORDS = ("glass", "window")
LIGHT_WORDS = ("emissive", "neon", "hologram", "_screen", "screen_", "glow", "billboard", "advert")


class GeoError(Exception):
    pass


def albedo_of(*texts) -> tuple | None:
    """The palette colour the first matching word in these paths suggests (None: nothing matched)."""
    for t in texts:
        low = str(t or "").lower()
        if not low:
            continue
        for words, rgb in PALETTE:
            if any(w in low for w in words):
                return rgb
    return None


# ----------------------------------------------------------------------------- the glb
def gltf_to_game(v: np.ndarray) -> np.ndarray:
    """glTF (Y up: x, y, z) -> the game's mesh space (Z up): (x, -z, y)."""
    out = np.empty_like(v)
    out[:, 0], out[:, 1], out[:, 2] = v[:, 0], -v[:, 2], v[:, 1]
    return out


def read_mesh_glb(path: str) -> list:
    """The render chunks of a WolvenKit glb: [{chunk, lod, positions, normals, indices}] (game space),
    the best LOD only."""
    js, bin_ = read_glb(path)
    chunks = []
    names = {}
    for ni, node in enumerate(js.get("nodes") or []):
        if "mesh" in node:
            names[node["mesh"]] = node.get("name") or ""
    for mi, mesh in enumerate(js.get("meshes") or []):
        name = mesh.get("name") or names.get(mi, "")
        m = SUBMESH_RX.search(name)
        chunk, lod = (int(m.group(1)), int(m.group(2))) if m else (mi, 1)
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            if "POSITION" not in attrs or prim.get("mode", 4) != 4:
                continue
            pos = gltf_to_game(accessor(js, bin_, attrs["POSITION"]).astype(np.float32))
            nrm = (gltf_to_game(accessor(js, bin_, attrs["NORMAL"]).astype(np.float32)) if "NORMAL" in attrs
                   else np.zeros_like(pos))
            if "indices" in prim:
                idx = accessor(js, bin_, prim["indices"]).reshape(-1).astype(np.uint32)
            else:
                idx = np.arange(len(pos), dtype=np.uint32)
            if len(idx) < 3 or len(pos) == 0:
                continue
            chunks.append({"chunk": chunk, "lod": lod, "positions": pos, "normals": nrm, "indices": idx})
    if not chunks:
        return []
    best = min(c["lod"] for c in chunks)
    return [c for c in chunks if c["lod"] == best]


# ----------------------------------------------------------------------------- materials -> colours
def _repo_file(repos, depot: str, suffix: str = ".json") -> str | None:
    """A file of WolvenKit's material repository: nested folders (Windows), or one flat name with
    backslashes in it (WolvenKit on Linux)."""
    if not depot:
        return None
    clean = str(depot).replace("\\\\", "\\").replace("/", "\\").strip("\\")
    for repo in repos:
        if not repo:
            continue
        for cand in (os.path.join(repo, *clean.split("\\")) + suffix, os.path.join(repo, clean + suffix)):
            if os.path.isfile(cand):
                return cand
    return None


def _cname(v):
    if isinstance(v, dict):
        return v.get("$value") if "$value" in v else _cname(v.get("DepotPath"))
    return v


def _rgb(v):
    if isinstance(v, dict):
        if "Elements" in v:
            v = v["Elements"]
        elif all(k in v for k in ("X", "Y", "Z")):
            v = [v["X"], v["Y"], v["Z"]]
        elif all(k in v for k in ("Red", "Green", "Blue")):
            return [v["Red"] / 255.0, v["Green"] / 255.0, v["Blue"] / 255.0]
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return [float(v[0]), float(v[1]), float(v[2])]
        except (TypeError, ValueError):
            return None
    return None


class TemplateColors:
    """The colour names of surface templates (``.mltemplate``), read from the export's repository
    and kept (a template is shared by thousands of meshes and is big: ~1000 colours)."""

    def __init__(self, cache_file: str | None = None):
        self.cache_file = cache_file
        self.data = {}
        if cache_file and os.path.isfile(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}
        self.dirty = False

    def get(self, template: str, repos) -> dict | None:
        key = str(template or "").lower().replace("/", "\\")
        if not key:
            return None
        if key in self.data:
            return self.data[key]
        fp = _repo_file(repos, template)
        if not fp:
            return None
        try:
            with open(fp, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        root = (doc.get("Data") or {}).get("RootChunk") or doc
        colors = {}
        for c in ((root.get("overrides") or {}).get("colorScale") or []):
            n, rgb = _cname(c.get("n")), _rgb(c.get("v"))
            if n and rgb:
                colors[str(n)] = [round(x, 4) for x in rgb]
        tex = _cname(root.get("colorTexture")) or ""
        entry = {"colors": colors, "texture": tex}
        self.data[key] = entry
        self.dirty = True
        return entry

    def save(self):
        if self.cache_file and self.dirty:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, separators=(",", ":"))
            os.replace(tmp, self.cache_file)
            self.dirty = False


LAYER_WEIGHTS = (1.0, 0.8, 0.5)
TEXTURE_LUMA = 0.8


def multilayer_color(setup_doc: dict, templates: TemplateColors, repos) -> tuple | None:
    """A multilayer setup's colour: its layers' colours (template colour x the surface's albedo),
    the base layer counting most."""
    root = (setup_doc.get("Data") or {}).get("RootChunk") or setup_doc
    acc, wsum = np.zeros(3), 0.0
    for i, layer in enumerate(root.get("layers") or []):
        tpl = _cname(layer.get("material")) or ""
        info = templates.get(tpl, repos) or {"colors": {}, "texture": ""}
        name = str(_cname(layer.get("colorScale")) or "")
        tint = info["colors"].get(name)
        base = albedo_of(tpl, info.get("texture")) or DEFAULT_ALBEDO
        # the colour tints the template's surface texture (its detail, mostly light grey); without
        # one the surface keeps the texture's own colour, which its name tells best
        col = np.array(base) if tint is None else np.array(tint) * TEXTURE_LUMA
        try:
            opacity = float(layer.get("opacity", 1.0))
        except (TypeError, ValueError):
            opacity = 1.0
        w = (LAYER_WEIGHTS[i] if i < len(LAYER_WEIGHTS) else 0.3) * max(0.0, min(1.0, opacity))
        if w <= 0:
            continue
        acc += np.clip(col, 0.0, 1.0) * w
        wsum += w
    if wsum <= 0:
        return None
    return tuple((acc / wsum).tolist())


def _kind_of(name: str, base_material: str, template: str) -> int:
    low = " ".join((name or "", base_material or "", template or "")).lower()
    if any(w in low for w in HIDDEN_WORDS):
        return KIND_HIDDEN
    if any(w in low for w in GLASS_WORDS):
        return KIND_GLASS
    if any(w in low for w in LIGHT_WORDS):
        return KIND_LIGHT
    return KIND_SOLID


def material_color(mat: dict, templates: TemplateColors, repos, textures=None) -> tuple:
    """(rgb linear, kind) of one entry of a Material.json."""
    name, base, tpl = mat.get("Name") or "", mat.get("BaseMaterial") or "", mat.get("MaterialTemplate") or ""
    data = mat.get("Data") or {}
    kind = _kind_of(name, base, tpl)
    setup = data.get("MultilayerSetup")
    if setup:
        fp = _repo_file(repos, setup)
        if fp:
            try:
                with open(fp, encoding="utf-8") as f:
                    c = multilayer_color(json.load(f), templates, repos)
                if c is not None:
                    return c, kind
            except (OSError, ValueError):
                pass
    for key in ("BaseColorScale", "DiffuseColor", "BaseColor", "Color", "AlbedoColor", "TintColor", "ColorTint"):
        rgb = _rgb(data.get(key))
        if rgb and max(rgb) > 0.002:
            if max(rgb) > 1.5:          # some are 0..255
                rgb = [v / 255.0 for v in rgb]
            return tuple(float(min(1.0, max(0.0, v))) for v in rgb[:3]), kind
    for key in ("DiffuseTexture", "BaseColor", "Albedo", "DiffuseMap", "MainTexture", "ColorTexture"):
        tex = data.get(key)
        if isinstance(tex, str) and tex.lower().endswith(".xbm") and textures is not None:
            c = textures(tex)
            if c is not None:
                return c, kind
    # a proxy (or any material) whose diffuse texture came out of the export with it
    if textures is not None:
        for v in data.values():
            if isinstance(v, str) and v.lower().endswith("_d.xbm"):
                c = textures(v)
                if c is not None:
                    return c, kind
    pick = albedo_of(name, setup, tpl, base, *(v for v in data.values() if isinstance(v, str)))
    if kind == KIND_GLASS:
        pick = pick or (0.30, 0.42, 0.50)
    return (pick or DEFAULT_ALBEDO), kind


def appearance_colors(material_json: dict, templates: TemplateColors, repos, textures=None) -> tuple:
    """({appearance: [(rgb, kind) per chunk]}, default appearance name) of a WolvenKit Material.json.
    Its appearance keys are the appearance name followed by its index (``default0``, ``red1``)."""
    by_name = {}
    for m in material_json.get("Materials") or []:
        if m.get("Name"):
            by_name[m["Name"]] = m
            by_name.setdefault(m["Name"].lower(), m)
    cache = {}

    def color(name):
        if name not in cache:
            m = by_name.get(name) or by_name.get(str(name).lower())
            cache[name] = (material_color(m, templates, repos, textures) if m
                           else (albedo_of(name) or DEFAULT_ALBEDO, _kind_of(name, "", "")))
        return cache[name]

    apps = {}
    default = None
    for key, mats in (material_json.get("Appearances") or {}).items():
        m = re.match(r"^(.*?)(\d+)$", key)
        app = m.group(1) if m and m.group(1) else key
        if default is None:
            default = app
        apps[app] = [color(n) for n in mats or []]
    return apps, default


# ----------------------------------------------------------------------------- textures
DXGI = {71: 0x31545844, 72: 0x31545844, 74: 0x33545844, 75: 0x33545844, 77: 0x35545844, 78: 0x35545844,
        80: 0x31495441, 83: 0x32495441, 87: 21, 28: 32, 29: 32}


def _png_rgba(data: bytes) -> np.ndarray | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, w = 8, [], None
    while pos + 8 <= len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.append(body)
        pos += 12 + n
    if w is None or depth != 8 or interlace or ctype not in (0, 2, 4, 6):
        return None
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), dtype=np.uint8)
    stride = w * ch
    out = np.zeros((h, stride), dtype=np.int32)
    prev = np.zeros(stride, dtype=np.int32)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)].astype(np.int32)
        if f == 0:
            cur = line
        elif f == 2:
            cur = (line + prev) & 255
        else:                       # sub / average / paeth need the pixel to the left: per pixel
            cur = np.zeros(stride, dtype=np.int32)
            for x in range(stride):
                a = cur[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                if f == 1:
                    p = a
                elif f == 3:
                    p = (a + b) // 2
                else:
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                cur[x] = (line[x] + p) & 255
        out[y] = cur
        prev = cur
    img = out.reshape(h, w, ch).astype(np.uint8)
    if ch == 1:
        img = np.repeat(img, 3, axis=2)
    elif ch == 2:
        img = np.concatenate([np.repeat(img[..., :1], 3, axis=2), img[..., 1:]], axis=2)
    if img.shape[2] == 3:
        img = np.concatenate([img, np.full(img.shape[:2] + (1,), 255, np.uint8)], axis=2)
    return img


def _tga_rgba(data: bytes) -> np.ndarray | None:
    if len(data) < 18:
        return None
    idlen, cmap, itype = data[0], data[1], data[2]
    w, h, bpp, desc = struct.unpack_from("<HHBB", data, 12)
    if cmap or itype not in (2, 3, 10, 11) or bpp not in (8, 24, 32):
        return None
    ch = bpp // 8
    pos = 18 + idlen
    n = w * h
    if itype in (2, 3):
        px = np.frombuffer(data, dtype=np.uint8, count=n * ch, offset=pos).reshape(n, ch)
    else:                           # run-length encoded
        out = np.empty((n, ch), dtype=np.uint8)
        i = 0
        while i < n and pos < len(data):
            hdr = data[pos]
            pos += 1
            cnt = (hdr & 127) + 1
            if hdr & 128:
                out[i:i + cnt] = np.frombuffer(data, np.uint8, ch, pos)
                pos += ch
            else:
                out[i:i + cnt] = np.frombuffer(data, np.uint8, cnt * ch, pos).reshape(cnt, ch)
                pos += cnt * ch
            i += cnt
        px = out
    if ch == 1:
        rgba = np.concatenate([np.repeat(px, 3, axis=1), np.full((n, 1), 255, np.uint8)], axis=1)
    else:
        rgba = np.concatenate([px[:, 2::-1], px[:, 3:4] if ch == 4 else np.full((n, 1), 255, np.uint8)], axis=1)
    return rgba.reshape(h, w, 4)


def _dds_rgba(data: bytes) -> np.ndarray | None:
    from .. import imaging
    if data[:4] != b"DDS " or len(data) < 128:
        return None
    h, w = struct.unpack_from("<II", data, 12)
    mips = max(1, struct.unpack_from("<I", data, 28)[0])
    flags, four = struct.unpack_from("<I4s", data, 80)
    off = 128
    if four == b"DX10":
        dxgi = struct.unpack_from("<I", data, 128)[0]
        fmt = DXGI.get(dxgi)
        off = 148
    elif flags & 4:
        fmt = struct.unpack("<I", four)[0]
    else:
        bits = struct.unpack_from("<I", data, 88)[0]
        fmt = 21 if bits == 32 else None
    if fmt is None:
        return None
    block = fmt in (0x31545844, 0x31495441) and 8 or (16 if fmt in (0x33545844, 0x35545844, 0x32495441) else 0)
    # the smallest mip not under 64 px: plenty for an average
    for _ in range(mips - 1):
        if max(w, h) <= 64:
            break
        size = (max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block) if block else w * h * 4
        off += size
        w, h = max(1, w // 2), max(1, h // 2)
    try:
        return imaging.decode(fmt, w, h, data[off:])
    except (ValueError, IndexError):
        return None


def mean_color_file(path: str) -> tuple | None:
    """Average linear colour of an exported texture (.png, .tga, .dds), None when unreadable."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        img = (_png_rgba(data) if ext == ".png" else _tga_rgba(data) if ext == ".tga" else
               _dds_rgba(data) if ext == ".dds" else None)
    except (ValueError, struct.error, zlib.error, IndexError):
        img = None
    if img is None or img.size == 0:
        return None
    if max(img.shape[:2]) > 128:
        step = max(1, max(img.shape[:2]) // 128)
        img = img[::step, ::step]
    a = img.reshape(-1, 4).astype(np.float64)
    m = a[:, 3] > 127
    if m.sum() < 4:
        m = np.ones(len(a), bool)
    srgb = a[m, :3].mean(0) / 255.0
    return tuple(float(v) for v in np.power(srgb, 2.2))           # sRGB -> linear


def texture_finder(repos):
    """texture depot path -> its average linear colour, from the files the export wrote."""
    memo = {}

    def find(depot: str):
        if depot in memo:
            return memo[depot]
        got = None
        for ext in (".png", ".tga", ".dds"):
            stem = str(depot)[:-4] if str(depot).lower().endswith(".xbm") else str(depot)
            fp = _repo_file(repos, stem, ext)
            if fp:
                got = mean_color_file(fp)
                if got is not None:
                    break
        memo[depot] = got
        return got
    return find


# ----------------------------------------------------------------------------- the .geo file
def encode(res: str, chunks: list, apps: dict, default: str | None) -> bytes:
    """Chunks (read_mesh_glb) and appearance colours -> a .geo file's bytes."""
    if not chunks:
        raise GeoError(f"{res}: no geometry")
    chunks = sorted(chunks, key=lambda c: c["chunk"])
    pos = np.concatenate([c["positions"] for c in chunks]).astype(np.float32)
    nrm = np.concatenate([c["normals"] for c in chunks]).astype(np.float32)
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.where(ln > 1e-6, nrm / np.maximum(ln, 1e-6), np.array([0.0, 0.0, 1.0], np.float32))
    nrm8 = np.clip(np.round(nrm * 127.0), -127, 127).astype(np.int8)
    table, idx_parts, vbase, ibase = [], [], 0, 0
    for c in chunks:
        n, m = len(c["positions"]), len(c["indices"])
        idx_parts.append(c["indices"].astype(np.uint32) + vbase)
        table.append({"chunk": int(c["chunk"]), "v": [vbase, n], "i": [ibase, m]})
        vbase += n
        ibase += m
    idx = np.concatenate(idx_parts)
    wide = vbase > 65535
    idx = idx.astype(np.uint32 if wide else np.uint16)
    lo, hi = pos.min(0), pos.max(0)
    app_out = {}
    for app, cols in (apps or {}).items():
        app_out[app] = [[round(float(c[0][0]), 4), round(float(c[0][1]), 4), round(float(c[0][2]), 4), int(c[1])]
                        if i < len(cols) else None for i, c in enumerate(cols)]
    header = {"v": GEO_VERSION, "res": res, "bounds": [[round(float(v), 3) for v in lo], [round(float(v), 3) for v in hi]],
              "vertices": int(vbase), "triangles": int(len(idx) // 3), "index": "u32" if wide else "u16",
              "chunks": table, "apps": app_out, "default": default}
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    hb += b" " * ((-len(hb) - 12) % 4)
    parts = [MAGIC, struct.pack("<II", GEO_VERSION, len(hb)), hb, pos.tobytes(), nrm8.tobytes()]
    pad = (-len(nrm8.tobytes())) % 4
    parts.append(b"\0" * pad)
    parts.append(idx.tobytes())
    return b"".join(parts)


def decode(data: bytes) -> tuple:
    """A .geo file -> (header, positions, normals, indices)."""
    if data[:4] != MAGIC:
        raise GeoError("not a .geo file")
    ver, hlen = struct.unpack_from("<II", data, 4)
    if ver != GEO_VERSION:
        raise GeoError(f".geo version {ver}")
    header = json.loads(data[12:12 + hlen].decode("utf-8"))
    off = 12 + hlen
    n = header["vertices"]
    pos = np.frombuffer(data, np.float32, n * 3, off).reshape(n, 3)
    off += n * 12
    nrm = np.frombuffer(data, np.int8, n * 3, off).reshape(n, 3)
    off += n * 3
    off += (-n * 3) % 4
    dt = np.uint32 if header["index"] == "u32" else np.uint16
    idx = np.frombuffer(data, dt, header["triangles"] * 3, off)
    return header, pos, nrm, idx


def build(res: str, glb_path: str, material_json_path: str | None, repos, templates: TemplateColors) -> bytes:
    """One exported mesh -> its .geo bytes."""
    chunks = read_mesh_glb(glb_path)
    if not chunks:
        raise GeoError(f"{res}: the export has no geometry")
    apps, default = {}, None
    if material_json_path and os.path.isfile(material_json_path):
        with open(material_json_path, encoding="utf-8") as f:
            mj = json.load(f)
        repos = list(repos) + [mj.get("MaterialRepo") or ""]
        apps, default = appearance_colors(mj, templates, repos, texture_finder(repos))
    if not apps:
        guess = albedo_of(res) or DEFAULT_ALBEDO
        apps = {"default": [(guess, _kind_of(res, "", ""))] * (max(c["chunk"] for c in chunks) + 1)}
        default = "default"
    return encode(res, chunks, apps, default)
