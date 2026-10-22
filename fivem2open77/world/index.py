"""Which sectors cover a spot, and the sectors themselves, read once from the game and kept.

The world's streaming block (``base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock``)
lists every sector with its streaming box, level and category.  Its JSON is large (tens of MB), so
the index keeps one compact row per sector: path, box, level, category.  A sector is extracted
from the game archives the first time something near it is looked at (WolvenKit, through the
caller's extractor), read (sector.py) and kept as compact JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import sector as S

BLOCK_PATH = "base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock"
SECTOR_RX = re.compile(r"^[a-z0-9_\\.\-]+\.streamingsector$", re.I)
INDEX_VERSION = 1
SECTOR_VERSION = 3              # 2: every element of a node, with its rotation; 3: foliage spans


def build_block_index(block_doc: dict) -> dict:
    """all.streamingblock (WolvenKit JSON) -> {version, sectors: [[path, minx, miny, minz, maxx, maxy, maxz,
    level, category], ...]}."""
    root = (block_doc.get("Data") or {}).get("RootChunk") or block_doc
    rows = []
    for d in root.get("descriptors") or []:
        path = S._v(d.get("data"))
        box = d.get("streamingBox") or {}
        mn, mx = S._vec(box.get("Min")), S._vec(box.get("Max"))
        if not path or not mn or not mx:
            continue
        rows.append([str(path)] + [round(v, 2) for v in mn] + [round(v, 2) for v in mx]
                    + [int(d.get("level") or 0), str(d.get("category") or "")])
    return {"version": INDEX_VERSION, "sectors": rows}


class WorldIndex:
    """``cache_dir``/world/{blocks.json, sectors/*.json}.  ``extract(paths) -> {path: json file}`` reads
    depot files out of the game (the Studio passes a DonorCache-backed one); without it only what
    is already cached can be read."""

    def __init__(self, cache_dir: str, extract=None, log=print):
        self.dir = os.path.join(cache_dir, "world")
        self.extract = extract
        self.log = log
        self._rows = None
        os.makedirs(os.path.join(self.dir, "sectors"), exist_ok=True)

    # ------------------------------------------------------------------ the block index
    @property
    def index_file(self) -> str:
        return os.path.join(self.dir, "blocks.json")

    def has_index(self) -> bool:
        return os.path.isfile(self.index_file)

    def rows(self) -> list:
        if self._rows is None:
            if not self.has_index():
                self.build_index()
            with open(self.index_file, encoding="utf-8") as f:
                data = json.load(f)
            self._rows = data.get("sectors") or []
        return self._rows

    def build_index(self, block_json: str | None = None) -> int:
        """From a given all.streamingblock JSON file, else extracted from the game."""
        if block_json is None:
            if self.extract is None:
                raise RuntimeError("no world index yet and no game to read it from (set the Cyberpunk 2077 folder)")
            self.log("reading the world's sector list from the game (one time)...")
            got = self.extract([BLOCK_PATH])
            if not got.get(BLOCK_PATH):
                self._done_with(got)
                raise RuntimeError("the world's sector list (all.streamingblock) is not in the game archives")
            block_json = got[BLOCK_PATH]
        else:
            got = None
        try:
            with open(block_json, encoding="utf-8") as f:
                idx = build_block_index(json.load(f))
        finally:
            if got is not None:
                self._done_with(got)
        tmp = self.index_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, separators=(",", ":"))
        os.replace(tmp, self.index_file)
        self._rows = idx["sectors"]
        self.log(f"world index: {len(self._rows)} sectors")
        return len(self._rows)

    def sectors_at(self, x: float, y: float, radius: float = 50.0, z: float | None = None, levels=None,
                   categories=None) -> list:
        """Sector rows that may hold objects within ``radius`` of the point, nearest first.

        A grid sector (exterior_X_Y_Z_L...) holds the objects of its cell (64 m at level 0, twice that
        each level up), a little over its edges; other sectors (quest_<hash>...) are taken by their
        streaming box, which is far larger than what they hold."""
        out = []
        for r in self.rows():
            path, x0, y0, z0, x1, y1, z1, level, cat = r
            if levels is not None and level not in levels:
                continue
            if categories and cat not in categories:
                continue
            cb = S.cell_box(path)
            if cb:
                cx0, cy0, cx1, cy1, size = cb
                m = 0.15 * size + 4.0
                bx0, by0, bx1, by1 = cx0 - m, cy0 - m, cx1 + m, cy1 + m
            else:
                bx0, by0, bx1, by1 = x0, y0, x1, y1
            dx = max(bx0 - x, 0.0, x - bx1)
            dy = max(by0 - y, 0.0, y - by1)
            d = (dx * dx + dy * dy) ** 0.5
            if d > radius:
                continue
            if z is not None and (z < z0 - radius or z > z1 + radius):
                continue
            out.append((d, level, r))
        out.sort(key=lambda t: (t[0] > 0, t[1], t[0], t[2][0]))
        return [r for _d, _l, r in out]

    # ------------------------------------------------------------------ sectors
    def _sector_file(self, path: str) -> str:
        h = hashlib.sha1(path.lower().encode()).hexdigest()[:16]
        base = re.sub(r"[^a-z0-9_.-]+", "_", path.lower().split("\\")[-1])[:80]
        return os.path.join(self.dir, "sectors", f"{base}-{h}.json")

    def has_sector(self, path: str) -> bool:
        return os.path.isfile(self._sector_file(path))

    def put_sector_json(self, path: str, json_file: str) -> dict:
        """Read a sector's WolvenKit JSON (a file) into the cache."""
        with open(json_file, encoding="utf-8") as f:
            sec = S.read_sector(json.load(f), path)
        sec["version"] = SECTOR_VERSION
        tmp = self._sector_file(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sec, f, separators=(",", ":"))
        os.replace(tmp, self._sector_file(path))
        return sec

    def _cached(self, path: str):
        fp = self._sector_file(path)
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, encoding="utf-8") as f:
                sec = json.load(f)
        except (OSError, ValueError):
            return None
        return sec if sec.get("version") == SECTOR_VERSION else None

    def sectors(self, paths) -> dict:
        """{path: sector} for the given sector paths, extracting the ones not cached yet."""
        paths = [p for p in dict.fromkeys(paths) if SECTOR_RX.match(p)]
        have = {p: self._cached(p) for p in paths}
        missing = [p for p in paths if have[p] is None]
        if missing:
            if self.extract is None:
                raise RuntimeError(f"{len(missing)} sector(s) not read yet and no game to read them from")
            self.log(f"reading {len(missing)} sector(s) from the game...")
            files = self.extract(missing)
            try:
                for p in missing:
                    if p in files and files[p] and os.path.isfile(files[p]):
                        have[p] = self.put_sector_json(p, files[p])
                    else:
                        self.log(f"  {p}: not in the game archives")
            finally:
                self._done_with(files)
        return {p: sec for p, sec in have.items() if sec}

    def _done_with(self, files):
        """Remove the extractor's temporary JSON once read (the answer's own cleanup, or the extractor's)."""
        if hasattr(files, "cleanup"):
            files.cleanup()
        elif self.extract is not None and getattr(self.extract, "temporary", False) and hasattr(self.extract, "cleanup"):
            self.extract.cleanup()

    def sector(self, path: str) -> dict | None:
        return self.sectors([path]).get(path)

    # ------------------------------------------------------------------ queries
    def entry(self, sector: str, i: int) -> tuple:
        """(sector, entry) of node instance ``i`` of a sector, with every element."""
        sec = self.sector(sector)
        if not sec:
            raise KeyError(f"sector {sector} is not in the game")
        if not 0 <= int(i) < len(sec["entries"]):
            raise KeyError(f"{sector} has no node instance {i} (it has {len(sec['entries'])})")
        return sec, sec["entries"][int(i)]

    def query(self, x: float, y: float, radius: float = 40.0, z: float | None = None, dz: float | None = None,
              kinds=None, text: str | None = None, levels=None, categories=("Exterior", "Interior"),
              limit: int = 600, max_sectors: int = 60, element_limit: int = 200) -> dict:
        """Node instances around a point, nearest first: {sectors: [{path, level, category, nodes}],
        entries: [entry + sector, expectedNodes, level, distance]}.  An entry with elements lists only
        the ones in the area (``elements.items``, each with its index ``k``; ``count`` is all of them)."""
        rows = self.sectors_at(x, y, radius, z, levels=set(levels) if levels is not None else None,
                               categories=set(categories) if categories else None)[:max_sectors]
        secs = self.sectors([r[0] for r in rows])
        txt = (text or "").lower().strip()
        r2 = radius * radius
        out = []
        for r in rows:
            sec = secs.get(r[0])
            if not sec:
                continue
            for e in sec["entries"]:
                if kinds and e["kind"] not in kinds and e["type"] not in kinds:
                    continue
                if txt and txt not in e["name"].lower() and txt not in e["res"].lower() and txt not in e["type"].lower():
                    continue
                el = e.get("elements")
                if el and el.get("items"):
                    near = []
                    for k, it in enumerate(el["items"]):
                        p = it["pos"]
                        d2 = (p[0] - x) ** 2 + (p[1] - y) ** 2
                        if d2 <= r2 and (z is None or dz is None or abs(p[2] - z) <= dz):
                            near.append((d2, k, it))
                    if not near:
                        continue
                    near.sort(key=lambda t: t[0])
                    d = near[0][0] ** 0.5
                    items = [dict(it, k=k) for _d, k, it in sorted(near[:element_limit], key=lambda t: t[1])]
                    o = dict(e, elements=dict(el, items=items, inArea=len(near)))
                else:
                    p = e["pos"]
                    d2 = (p[0] - x) ** 2 + (p[1] - y) ** 2
                    if d2 > r2 or (z is not None and dz is not None and abs(p[2] - z) > dz):
                        continue
                    d = d2 ** 0.5
                    o = dict(e)
                o.update(sector=sec["path"], expectedNodes=sec["nodes"], level=sec.get("level"), distance=round(d, 2))
                out.append(o)
        out.sort(key=lambda e: (e["distance"], e["sector"], e["i"]))
        return {"center": {"x": x, "y": y, "z": z}, "radius": radius,
                "sectors": [{"path": r[0], "level": r[7], "category": r[8], "nodes": (secs.get(r[0]) or {}).get("nodes")}
                            for r in rows],
                "entries": out[:limit], "total": len(out)}

    # ------------------------------------------------------------------ meshes (what an added object brings)
    def mesh_info(self, res: str) -> dict | None:
        """{res, bounds: [[x,y,z],[x,y,z]] (local, from the pivot), physics: bool, appearances} of a
        game .mesh, read once; None when the game has no such mesh (or nothing can read it)."""
        res = res.lower()
        h = hashlib.sha1(res.encode()).hexdigest()[:16]
        fp = os.path.join(self.dir, "meshes", h + ".json")
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        if self.extract is None:
            return None
        files = self.extract([res])
        try:
            if not files.get(res):
                return None
            with open(files[res], encoding="utf-8") as f:
                doc = json.load(f)
        finally:
            self._done_with(files)
        info = read_mesh_info(doc, res)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        return info


def read_mesh_info(doc: dict, res: str) -> dict:
    root = (doc.get("Data") or {}).get("RootChunk") or doc
    bb = root.get("boundingBox") or {}
    mn, mx = S._vec(bb.get("Min")), S._vec(bb.get("Max"))
    physics = False
    for prm in root.get("parameters") or []:
        d = prm.get("Data") if isinstance(prm, dict) else None
        if isinstance(d, dict) and d.get("$type") == "meshMeshParamPhysics":
            physics = True
    apps = []
    for a in root.get("appearances") or []:
        d = a.get("Data") if isinstance(a, dict) else None
        if isinstance(d, dict):
            n = S._v(d.get("name"))
            if n:
                apps.append(str(n))
    return {"res": res, "bounds": [mn, mx] if mn and mx else None, "physics": physics, "appearances": apps[:64]}
