"""Night City straight out of the installed game, without WolvenKit: the sector list, the sectors
(update 2.5's cooked form, world/sector25.py), foliage populations and meshes (geometry and
colours, as .geo: world/geocache.py), read from the archives (cp77/archive.py) with the Kraken
decoder WolvenKit brings, or the game's own Oodle.

What it cannot read (a sector in the older form) the caller reads through WolvenKit as before.
"""
from __future__ import annotations

import collections
import hashlib
import os
import threading

from ..cp77.archive import GameArchives
from ..cp77.cr2w import CR2W, CR2WError
from ..cp77.kraken import Kraken
from . import geocache as GC
from . import sector25 as S25

BLOCK_PATH = "base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock"
FILE_CACHE = 400                    # files (materials, setups, templates) kept parsed between meshes
FILE_KEEP_MAX = 1 << 20             # (bigger ones -- textures -- are not kept: their colour is, by the caller)


class GameWorldError(RuntimeError):
    pass


def game_stamp(game_dir: str) -> str | None:
    """Changes when the game's archives do (an update): their names, sizes and times."""
    pc = os.path.join(game_dir or "", "archive", "pc")
    parts = []
    for d in ("content", "ep1"):
        fd = os.path.join(pc, d)
        if not os.path.isdir(fd):
            continue
        for n in sorted(os.listdir(fd)):
            if n.lower().endswith(".archive"):
                try:
                    st = os.stat(os.path.join(fd, n))
                    parts.append(f"{d}/{n}:{st.st_size}:{int(st.st_mtime)}")
                except OSError:
                    parts.append(f"{d}/{n}")
    if not parts:
        return None
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


class GameWorld:
    def __init__(self, game_dir: str, wolvenkit_dir: str | None = None, cache_dir: str | None = None, log=print):
        self.game_dir = game_dir
        self.wolvenkit_dir = wolvenkit_dir
        self.cache_dir = cache_dir
        self.log = log
        self.kraken = Kraken.any(wolvenkit_dir, game_dir)
        self.archives = GameArchives(game_dir, self.kraken)
        self._paths = None
        self._paths_tried = False
        self._lock = threading.RLock()
        self._files = collections.OrderedDict()
        self.memo = {}                                      # geocache.NativeSource's: setups, texture colours
        self._stamp = None

    # ------------------------------------------------------------------ files
    def stamp(self) -> str | None:
        if self._stamp is None:
            self._stamp = game_stamp(self.game_dir)
        return self._stamp

    def read(self, key) -> bytes | None:
        """A file by depot path, hash or hash name (``#<16 hex>.mesh``)."""
        h = S25.hash_of(key) if isinstance(key, str) else None
        return self.archives.read(h if h is not None else key)

    def fetch(self, path: str):
        """A file parsed (CR2W), kept a while; None when the game has none (or it is not CR2W)."""
        key = str(path or "").lower()
        if not key:
            return None
        with self._lock:
            if key in self._files:
                self._files.move_to_end(key)
                return self._files[key]
        data = self.read(path)
        c = None
        if data:
            try:
                c = CR2W(data, self.kraken)
            except (CR2WError, ValueError, IndexError):
                c = None
        if data is None or len(data) <= FILE_KEEP_MAX:
            with self._lock:
                self._files[key] = c
                while len(self._files) > FILE_CACHE:
                    self._files.popitem(last=False)
        return c

    def paths(self):
        """The game's path list (WolvenKit's), for meshes the sectors name by hash only; None
        without WolvenKit (its own imports still name most)."""
        with self._lock:
            if not self._paths_tried:
                self._paths_tried = True
                try:
                    from ..cp77.pathlist import PathList
                    if self.wolvenkit_dir and self.cache_dir:
                        self._paths = PathList.load(self.wolvenkit_dir, self.cache_dir, self.kraken, log=self.log)
                except Exception as e:                      # noqa: BLE001 -- the hash names do
                    self.log(f"the game's path list could not be read ({e}); meshes no sector names keep their hash")
                    self._paths = None
            return self._paths

    # ------------------------------------------------------------------ the world
    def block_rows(self) -> list:
        """The world's sector list: [[path, minx, miny, minz, maxx, maxy, maxz, level, category]]."""
        c = self.fetch(BLOCK_PATH)
        if c is None:
            raise GameWorldError("the world's sector list (all.streamingblock) is not in the game archives")
        rows = []
        for d in c.props(0).get("descriptors") or []:
            path = d.get("data")
            box = d.get("streamingBox") or {}
            mn, mx = box.get("Min") or {}, box.get("Max") or {}
            if not path:
                continue
            rows.append([str(path)] + [round(float(mn.get(k, 0.0)), 2) for k in "XYZ"]
                        + [round(float(mx.get(k, 0.0)), 2) for k in "XYZ"]
                        + [int(d.get("level") or 0), str(d.get("category") or "Exterior")])
        with self._lock:
            self._files.pop(BLOCK_PATH.lower(), None)        # (10 MB: not worth keeping)
        return rows

    def sector(self, path: str):
        """A sector read (sector.read_sector's form, + ``foliage_data``); None when it is in the
        older form (WolvenKit reads those); KeyError when the game has no such sector."""
        data = self.read(path)
        if data is None:
            raise KeyError(path)
        return S25.decode(data, path, self.kraken, self.paths())

    def foliage(self, res: str):
        """A compiled foliage resource's populations ({pos, rot, scale}), None if unreadable."""
        data = self.read(res)
        if data is None:
            return None
        try:
            return S25.foliage_file(data, self.kraken)
        except (CR2WError, ValueError, IndexError):
            return None

    def mesh_geo(self, res: str, templates) -> bytes:
        """A mesh as a .geo file (geometry and colours), read from the game."""
        data = self.read(res)
        if data is None:
            raise GC.GeoError("not in the game archives")
        if len(self.memo) > 20000:
            self.memo.clear()
        return GC.build_native(res, CR2W(data, self.kraken), self.fetch, templates, self.memo, self.kraken)
