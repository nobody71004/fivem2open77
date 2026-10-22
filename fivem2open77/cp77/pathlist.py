"""Depot paths by their hash: the game names files by the FNV-1a hash of their path, and since 2.5
its sectors name most meshes only by that hash.  WolvenKit carries the list of the game's paths it
knows (an embedded resource of WolvenKit.Common.dll, Kraken-compressed: ``usedhashes.kark``); the
paths of the kinds the Studio reads (meshes, foliage, sectors) are hashed once and kept next to
the workspace's world cache.
"""
from __future__ import annotations

import os
import struct

import numpy as np

from .package import depot_hash

KEEP = (".mesh", ".cfoliage", ".streamingsector", ".streamingsector_inplace", ".streamingblock", ".ent")
VERSION = 1


class PathListError(RuntimeError):
    pass


def _embedded_paths(dll: bytes, kraken) -> list:
    """The path list among the DLL's Kraken-compressed resources: the one whose text starts with
    depot paths (``base\\``)."""
    o = 0
    best = None
    while True:
        i = dll.find(b"KARK", o)
        if i < 0:
            break
        o = i + 4
        if i < 4:
            continue
        size = struct.unpack_from("<I", dll, i + 4)[0]
        rlen = struct.unpack_from("<I", dll, i - 4)[0]           # the .NET resource's length, just before
        if not (16 < rlen <= len(dll) - i) or not (1000 < size < 1_000_000_000):
            continue
        try:
            data = kraken.unpack(dll[i:i + rlen])
        except Exception:                                       # noqa: BLE001 -- another resource
            continue
        if data[:5] == b"base\\" and (best is None or len(data) > len(best)):
            best = data
    if best is None:
        raise PathListError("no path list in WolvenKit.Common.dll")
    return best.decode("utf-8", "replace").split("\r\n")


class PathList:
    """``path(hash) -> depot path | None`` for the files WolvenKit knows."""

    def __init__(self, hashes: np.ndarray, paths: list):
        order = np.argsort(hashes)
        self.h = hashes[order]
        self.p = [paths[i] for i in order]

    def path(self, h: int):
        i = int(np.searchsorted(self.h, np.uint64(h)))
        if i < len(self.h) and int(self.h[i]) == int(h):
            return self.p[i]
        return None

    def __len__(self):
        return len(self.p)

    @classmethod
    def load(cls, wolvenkit_dir: str, cache_dir: str, kraken, log=lambda m: None) -> "PathList":
        dll = os.path.join(wolvenkit_dir if os.path.isdir(wolvenkit_dir) else os.path.dirname(wolvenkit_dir), "WolvenKit.Common.dll")
        if not os.path.isfile(dll):
            raise PathListError(f"{dll} not found")
        st = os.stat(dll)
        tag = f"v{VERSION}-{st.st_size}-{int(st.st_mtime)}"
        fp = os.path.join(cache_dir, f"paths-{tag}.npz")
        if os.path.isfile(fp):
            try:
                with np.load(fp, allow_pickle=False) as z:
                    return cls(z["h"], bytes(z["p"]).decode("utf-8").split("\n"))
            except (OSError, ValueError, KeyError):
                pass
        with open(dll, "rb") as f:
            raw = f.read()
        lines = [ln for ln in _embedded_paths(raw, kraken) if ln.lower().endswith(KEEP)]
        h = np.array([depot_hash(ln) for ln in lines], dtype=np.uint64)
        log(f"the game's paths: {len(lines):,} known to WolvenKit")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = fp + ".tmp.npz"
            np.savez(tmp, h=h, p=np.frombuffer("\n".join(lines).encode("utf-8"), np.uint8))
            os.replace(tmp, fp)
        except OSError:
            pass
        return cls(h, lines)
