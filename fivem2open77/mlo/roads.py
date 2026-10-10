"""Night City's roads, from the game's own traffic lanes -- the data NCWE (Open//77's world editor)
reads to know where the streets are.

The game's AI traffic and crowds run on a lane network compiled into
``base\\worlds\\03_night_city\\sectors\\_generated\\traffic\\``:

* ``all.traffic_persistent`` (``worldTrafficPersistentResource``): per lane its flags, width (m),
  max speed (m/s) and road material;
* ``all.lane_polygons`` (``worldTrafficPersistentLanePolygonResource``): per lane its centre line
  (``outline``, game metres, in the direction of travel), joined to the lanes by ``index``.

The flags say what a lane is: Road 0x10 (a carriageway), Highway 0x4000, Pavement 0x08 (a
footpath), Intersection 0x20 (inside a junction), CrossWalk 0x100, PatrolRoute 0x4.  With Phantom
Liberty the EP1 copy of these files is the whole network (Dogtown included) and wins.

``extract(game_dir, wk)`` reads them from the player's own game with WolvenKit; ``write_tiles``
writes them for the map manager -- ``index.json`` and one text file per 512 m tile (each well under
the 4 MiB ``Open77.io.read`` takes), one lane per line, simplified to 0.15 m:

    <id> <kind> <width> <speed> x y z x y z ...

kind: ``r`` road, ``h`` highway, ``i`` road inside a junction, ``c`` crosswalk, ``p`` pavement,
``j`` pavement crossing a junction, ``t`` patrol route, ``o`` other.  ``Roads`` reads the tiles back
and answers what is near a point (the agent's road tools, the Studio).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time

TRAFFIC_DIR = r"base\worlds\03_night_city\sectors\_generated\traffic"
FILES = ("all.traffic_persistent", "all.lane_polygons")
TILE = 512.0
FORMAT = 1
SIMPLIFY = 0.15

FLAG_ROAD, FLAG_PAVEMENT, FLAG_INTERSECTION = 0x10, 0x08, 0x20
FLAG_CROSSWALK, FLAG_PATROL, FLAG_HIGHWAY = 0x100, 0x04, 0x4000
KINDS = {"r": "road", "h": "highway", "i": "road in a junction", "c": "crosswalk", "p": "pavement",
         "j": "pavement crossing", "t": "patrol route", "o": "other"}
CAR_KINDS = "rhi"                       # where cars drive
WALK_KINDS = "pjc"                      # where people walk


def kind_of(flags: int) -> str:
    """One letter for a lane's flags (the order NCWE's LaneKind uses)."""
    if flags & FLAG_HIGHWAY:
        return "h"
    if flags & FLAG_INTERSECTION and flags & FLAG_ROAD:
        return "i"
    if flags & FLAG_CROSSWALK:
        return "c"
    if flags & FLAG_ROAD:
        return "r"
    if flags & FLAG_PAVEMENT:
        return "j" if flags & FLAG_INTERSECTION else "p"
    if flags & FLAG_PATROL:
        return "t"
    return "o"


# ----------------------------------------------------------------------------- reading the game's files
def _root(doc: dict) -> dict:
    """The root chunk of a WolvenKit JSON (``Data.RootChunk``), or the dict itself."""
    data = doc.get("Data") if isinstance(doc, dict) else None
    if isinstance(data, dict) and isinstance(data.get("RootChunk"), dict):
        return data["RootChunk"]
    return doc


def _vec(v) -> tuple:
    return (float(v.get("X", v.get("x", 0.0))), float(v.get("Y", v.get("y", 0.0))), float(v.get("Z", v.get("z", 0.0))))


def lanes_from_json(persistent: dict, polygons: dict) -> list:
    """Lanes as dicts {id, kind, flags, width, speed, material, points [(x, y, z), ...]} from the
    WolvenKit JSON of all.traffic_persistent and all.lane_polygons."""
    plist = (_root(persistent).get("data") or {}).get("lanes") or []
    outlines = {}
    for e in _root(polygons).get("data") or []:
        try:
            idx = int(e.get("index"))
        except (TypeError, ValueError):
            continue
        val = e.get("value") or {}
        outlines[idx] = [_vec(p) for p in val.get("outline") or []]
    out = []
    for i, lane in enumerate(plist):
        pts = outlines.get(i) or [_vec(p) for p in lane.get("outline") or []]
        if len(pts) < 2:
            continue
        flags = int(lane.get("flags") or 0)
        mats = lane.get("roadMaterials") or []
        out.append({
            "id": i, "kind": kind_of(flags), "flags": flags, "width": float(lane.get("width") or 0.0),
            "speed": float(lane.get("maxSpeed") or 0.0), "material": (mats[0].get("material") if mats else None),
            "points": pts,
        })
    return out


def simplify(points: list, tol: float = SIMPLIFY) -> list:
    """Douglas-Peucker in 3D: the polyline within `tol` metres of the original."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay, az = points[a]
        bx, by, bz = points[b]
        dx, dy, dz = bx - ax, by - ay, bz - az
        ll = dx * dx + dy * dy + dz * dz
        best, bi = -1.0, -1
        for k in range(a + 1, b):
            px, py, pz = points[k]
            if ll <= 1e-12:
                d = math.dist((px, py, pz), (ax, ay, az))
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy + (pz - az) * dz) / ll))
                d = math.dist((px, py, pz), (ax + t * dx, ay + t * dy, az + t * dz))
            if d > best:
                best, bi = d, k
        if best > tol:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(points, keep) if k]


# ----------------------------------------------------------------------------- tiles
def tile_of(x: float, y: float, size: float = TILE) -> tuple:
    return (math.floor(x / size), math.floor(y / size))


def tiles_of_lane(points: list, width: float, size: float = TILE) -> list:
    """Every tile the lane's 2D box (widened by half its width) touches."""
    h = max(0.0, width) / 2
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0 = tile_of(min(xs) - h, min(ys) - h, size)
    x1, y1 = tile_of(max(xs) + h, max(ys) + h, size)
    return [(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def lane_line(lane: dict) -> str:
    pts = " ".join(f"{_fmt(x)} {_fmt(y)} {_fmt(z)}" for x, y, z in lane["points"])
    return f"{lane['id']} {lane['kind']} {_fmt(lane['width'])} {_fmt(lane['speed'])} {pts}"


def write_tiles(lanes: list, out_dir: str, source: str = "", size: float = TILE, tol: float = SIMPLIFY) -> dict:
    """index.json + <cx>_<cy>.lanes per tile into `out_dir` (replaced as a whole)."""
    tmp = out_dir.rstrip("/\\") + ".f2o77-new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    per_tile: dict = {}
    kinds: dict = {}
    points = 0
    lo = [math.inf, math.inf]
    hi = [-math.inf, -math.inf]
    for lane in lanes:
        pts = simplify(lane["points"], tol)
        lane = dict(lane, points=pts)
        points += len(pts)
        kinds[lane["kind"]] = kinds.get(lane["kind"], 0) + 1
        for x, y, _ in pts:
            lo[0], lo[1], hi[0], hi[1] = min(lo[0], x), min(lo[1], y), max(hi[0], x), max(hi[1], y)
        line = lane_line(lane)
        for t in tiles_of_lane(pts, lane["width"], size):
            per_tile.setdefault(t, []).append(line)
    tiles = {}
    for (cx, cy), rows in sorted(per_tile.items()):
        name = f"{cx}_{cy}.lanes"
        text = "\n".join(rows) + "\n"
        with open(os.path.join(tmp, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        tiles[f"{cx}_{cy}"] = {"lanes": len(rows), "bytes": len(text.encode("utf-8"))}
    index = {
        "format": FORMAT, "what": "Night City traffic lanes (game data), for the fivem2open77 map manager",
        "source": source, "extractedAt": int(time.time()), "tile": size, "simplified": tol,
        "lanes": len(lanes), "points": points, "kinds": kinds, "kindNames": KINDS,
        "bounds": [lo[0], lo[1], hi[0], hi[1]] if lanes else None, "tiles": tiles,
        "line": "<id> <kind> <width m> <max speed m/s> x y z x y z ...",
    }
    with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.replace(tmp, out_dir)
    return index


# ----------------------------------------------------------------------------- reading tiles back
LINE_RE = re.compile(r"^(\d+) ([a-z]) (\S+) (\S+) (.*)$")


def parse_line(line: str) -> dict | None:
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    nums = [float(v) for v in m.group(5).split()]
    if len(nums) < 6 or len(nums) % 3:
        return None
    pts = [tuple(nums[i:i + 3]) for i in range(0, len(nums), 3)]
    return {"id": int(m.group(1)), "kind": m.group(2), "width": float(m.group(3)), "speed": float(m.group(4)),
            "points": pts}


def _closest(points: list, x: float, y: float) -> tuple:
    """(2D distance, point (x, y, z), segment index, unit direction (dx, dy)) of the closest point."""
    best = (math.inf, None, -1, (0.0, 1.0))
    for k in range(len(points) - 1):
        ax, ay, az = points[k]
        bx, by, bz = points[k + 1]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = 0.0 if ll <= 1e-12 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / ll))
        px, py, pz = ax + t * dx, ay + t * dy, az + t * (bz - az)
        d = math.hypot(x - px, y - py)
        if d < best[0]:
            n = math.sqrt(ll) or 1.0
            best = (d, (px, py, pz), k, (dx / n, dy / n))
    return best


def heading_of(dx: float, dy: float) -> float:
    """The game's yaw (degrees, 0 = +y north, counter-clockwise) of a direction."""
    return math.degrees(math.atan2(-dx, dy))


class Roads:
    """The lanes of a tile folder, read lazily per tile."""

    def __init__(self, folder: str):
        self.folder = folder
        with open(os.path.join(folder, "index.json"), encoding="utf-8") as f:
            self.index = json.load(f)
        self.size = float(self.index.get("tile") or TILE)
        self._tiles: dict = {}

    def tile(self, cx: int, cy: int) -> list:
        key = f"{cx}_{cy}"
        if key not in self._tiles:
            lanes = []
            if key in (self.index.get("tiles") or {}):
                with open(os.path.join(self.folder, key + ".lanes"), encoding="utf-8") as f:
                    for line in f:
                        lane = parse_line(line)
                        if lane:
                            lanes.append(lane)
            self._tiles[key] = lanes
        return self._tiles[key]

    def near(self, x: float, y: float, radius: float = 60.0, kinds: str | None = None, limit: int = 200) -> list:
        """Lanes within `radius` metres (2D, to their centre line) of x, y, nearest first, each with
        `distance`, `closest` (x, y, z), `heading` (the lane's direction there, game yaw) and `edge`
        (the distance from the point to the lane's nearer side, negative inside the lane)."""
        r = float(radius)
        x0, y0 = tile_of(x - r, y - r, self.size)
        x1, y1 = tile_of(x + r, y + r, self.size)
        seen, out = set(), []
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                for lane in self.tile(cx, cy):
                    if lane["id"] in seen or (kinds and lane["kind"] not in kinds):
                        continue
                    seen.add(lane["id"])
                    d, p, k, (dx, dy) = _closest(lane["points"], x, y)
                    if d <= r + lane["width"] / 2:
                        out.append(dict(lane, distance=round(d, 2), closest=p, segment=k,
                                        heading=round(heading_of(dx, dy), 1), edge=round(d - lane["width"] / 2, 2)))
        out.sort(key=lambda lane: lane["distance"])
        return out[:limit]

    def nearest(self, x: float, y: float, kinds: str = CAR_KINDS, radius: float = 400.0) -> dict | None:
        found = self.near(x, y, radius, kinds, limit=1)
        return found[0] if found else None


# ----------------------------------------------------------------------------- from the player's game
def extract(game_dir: str, wk, out_dir: str, log=print, keep_json: str | None = None) -> dict:
    """Read the traffic lanes from the game at `game_dir` with WolvenKit (`wk`, our WolvenKit wrapper)
    and write the tiles to `out_dir`."""
    pc = os.path.join(game_dir, "archive", "pc")
    archives = []
    # the EP1 copy (the whole network, Dogtown included) is unbundled last so it wins
    for sub, prefixes in (("content", ("basegame_",)), ("ep1", ("ep1_",))):
        d = os.path.join(pc, sub)
        if os.path.isdir(d):
            archives += sorted(os.path.join(d, f) for f in os.listdir(d)
                               if f.endswith(".archive") and f.startswith(prefixes))
    if not archives:
        raise RuntimeError(f"no game archives under {pc}")
    work = tempfile.mkdtemp(prefix="f2o77-roads-")
    try:
        raw = os.path.join(work, "raw")
        regex = "^(" + "|".join(re.escape(TRAFFIC_DIR + "\\" + n) for n in FILES) + ")$"
        log("roads: reading the traffic lanes from the game archives")
        wk.unbundle(archives, raw, regex)
        tdir = os.path.join(raw, *TRAFFIC_DIR.split("\\"))
        found = {n: os.path.join(tdir, n) for n in FILES}
        missing = [n for n, p in found.items() if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(f"not in the game archives: {', '.join(missing)}")
        js = os.path.join(work, "json")
        os.makedirs(js)
        for p in found.values():
            wk.serialize(p, js)
        docs = {}
        for n in FILES:
            p = os.path.join(js, n + ".json")
            with open(p, encoding="utf-8") as f:
                docs[n] = json.load(f)
            if keep_json:
                os.makedirs(keep_json, exist_ok=True)
                shutil.copy(p, os.path.join(keep_json, n + ".json"))
        version = (docs[FILES[0]].get("Header") or {}).get("GameVersion")
        lanes = lanes_from_json(docs[FILES[0]], docs[FILES[1]])
        log(f"roads: {len(lanes)} lanes")
        return write_tiles(lanes, out_dir, source=f"Cyberpunk 2077 game data {version}" if version else "Cyberpunk 2077")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def from_json(folder: str, out_dir: str) -> dict:
    """The tiles from already serialized JSON (all.traffic_persistent.json, all.lane_polygons.json)."""
    docs = {}
    for n in FILES:
        with open(os.path.join(folder, n + ".json"), encoding="utf-8") as f:
            docs[n] = json.load(f)
    version = (docs[FILES[0]].get("Header") or {}).get("GameVersion")
    lanes = lanes_from_json(docs[FILES[0]], docs[FILES[1]])
    return write_tiles(lanes, out_dir, source=f"Cyberpunk 2077 game data {version}" if version else "Cyberpunk 2077")
