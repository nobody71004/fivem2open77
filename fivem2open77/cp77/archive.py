"""The game's own files, read straight out of its archives (no WolvenKit call).

A ``.archive`` (``RDAR``) ends in an index: a table of 56-byte file entries (u64 depot-path hash,
i64 time, u32 inline buffer count, u32 first segment, u32 end segment, u32 first dependency,
u32 end dependency, 20-byte SHA-1) and 16-byte segments (u64 offset, u32 size on disk, u32 size).
A file is its first segment -- Kraken-compressed (``KARK`` + size) when its two sizes differ --
followed by its buffers as stored (a CR2W file's buffer table describes them compressed, so they
stay as they are): what WolvenKit's ``unbundle`` writes, byte for byte.

``GameArchives`` indexes every archive of the game once (numpy arrays, a few tens of MB for the
game's 1.7 million files) and reads files by depot path or hash.  Phantom Liberty's archives
(``archive/pc/ep1``) come after the base game's and win where both have a file, as in the game.
"""
from __future__ import annotations

import os
import struct
import threading

import numpy as np

from .kraken import Kraken
from .package import depot_hash

FILE_DT = np.dtype([("hash", "<u8"), ("time", "<i8"), ("inline", "<u4"), ("s0", "<u4"), ("s1", "<u4"),
                    ("d0", "<u4"), ("d1", "<u4"), ("sha1", "V20")])
SEG_DT = np.dtype([("off", "<u8"), ("z", "<u4"), ("size", "<u4")])
FOLDERS = ("content", "ep1")


class ArchiveError(RuntimeError):
    pass


def read_index(path: str) -> tuple:
    """(file entries, segments) of one archive as numpy structured arrays."""
    with open(path, "rb") as f:
        head = f.read(40)
        magic, _ver, index_pos, _index_size = struct.unpack_from("<4sIQI", head, 0)
        if magic != b"RDAR":
            raise ArchiveError(f"{path} is not a Cyberpunk archive")
        f.seek(index_pos)
        _table_off, table_size, _crc = struct.unpack("<IIQ", f.read(16))
        table = f.read(table_size - 8)
    n_files, n_segs, _n_deps = struct.unpack_from("<III", table, 0)
    files = np.frombuffer(table, FILE_DT, n_files, 12)
    segs = np.frombuffer(table, SEG_DT, n_segs, 12 + n_files * FILE_DT.itemsize)
    return files, segs


class GameArchives:
    def __init__(self, game_dir: str, kraken: Kraken, folders=FOLDERS):
        self.game_dir = game_dir
        self.kraken = kraken
        pc = os.path.join(game_dir, "archive", "pc")
        self.paths = []
        for d in folders:
            fd = os.path.join(pc, d)
            if os.path.isdir(fd):
                self.paths += [os.path.join(fd, n) for n in sorted(os.listdir(fd)) if n.lower().endswith(".archive")]
        if not self.paths:
            raise ArchiveError(f"no game archives under {pc}")
        self._lock = threading.Lock()
        self._index = None

    def stamp(self) -> str:
        """Changes when the game's archives do (an update): names, sizes and times."""
        parts = []
        for p in self.paths:
            try:
                st = os.stat(p)
                parts.append(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                parts.append(os.path.basename(p))
        import hashlib
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    def _build(self):
        hashes, where = [], []
        self._tables = []
        for ai, p in enumerate(self.paths):
            files, segs = read_index(p)
            self._tables.append((files, segs))
            hashes.append(files["hash"])
            where.append(np.stack([np.full(len(files), ai, np.int32), np.arange(len(files), dtype=np.int32)], axis=1))
        h = np.concatenate(hashes)
        w = np.concatenate(where)
        # the last archive with a file wins (Phantom Liberty after the base game): a stable sort by
        # hash keeps archive order, and the last of each run of equal hashes is kept
        order = np.argsort(h, kind="stable")
        h, w = h[order], w[order]
        last = np.ones(len(h), bool)
        last[:-1] = h[1:] != h[:-1]
        self._h, self._w = h[last], w[last]

    def _locate(self, key):
        with self._lock:
            if self._index is None:
                self._build()
                self._index = True
        hv = np.uint64(key if isinstance(key, int) else depot_hash(key))
        i = int(np.searchsorted(self._h, hv))
        if i >= len(self._h) or self._h[i] != hv:
            return None
        ai, fi = (int(v) for v in self._w[i])
        return ai, fi

    def has(self, key) -> bool:
        return self._locate(key) is not None

    def read(self, key) -> bytes | None:
        """The file (depot path or hash), as WolvenKit's unbundle writes it; None if the game has none."""
        loc = self._locate(key)
        if loc is None:
            return None
        ai, fi = loc
        files, segs = self._tables[ai]
        e = files[fi]
        s0, s1 = int(e["s0"]), int(e["s1"])
        out = []
        with open(self.paths[ai], "rb") as f:
            for si in range(s0, s1):
                off, z, size = int(segs[si]["off"]), int(segs[si]["z"]), int(segs[si]["size"])
                f.seek(off)
                data = f.read(z)
                if si == s0 and z != size:
                    data = self.kraken.unpack(data, size)
                out.append(data)
        return b"".join(out)
