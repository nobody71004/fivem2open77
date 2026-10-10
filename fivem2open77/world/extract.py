"""Depot files read out of the player's own game, as WolvenKit JSON, many at a time.

WolvenKit's start is the slow part of a call (seconds), so the files asked for together are
unbundled with one call and serialized with one more (``convert serialize`` of a folder writes
``<basename>.json`` side by side: files whose names collide go in separate rounds).
"""
from __future__ import annotations

import os
import re
import shutil
import uuid


def regex_for(paths) -> str:
    return "^(" + "|".join(re.escape(p) for p in paths) + ")$"


class GameExtractor:
    """``extract(paths) -> {depot path: json file}`` for the paths found in the game's archives.

    The JSON files are temporary (``temporary = True``): the caller reads them and they are removed
    with ``cleanup()`` (the world index keeps a compact copy of what it needs)."""

    temporary = True

    def __init__(self, wk, game_dir: str, scratch_dir: str, log=print):
        self.wk = wk
        self.game_dir = game_dir
        self.scratch = scratch_dir
        self.log = log
        self._dirs = []

    def archives(self) -> list:
        if not self.game_dir:
            raise RuntimeError("Cyberpunk 2077 was not found: set its folder in Settings")
        pc = os.path.join(self.game_dir, "archive", "pc")
        out = [os.path.join(pc, d) for d in ("content", "ep1") if os.path.isdir(os.path.join(pc, d))]
        if not out:
            raise RuntimeError(f"no game archives under {pc}")
        return out

    def __call__(self, paths) -> dict:
        paths = [p for p in dict.fromkeys(paths) if p]
        if not paths:
            return {}
        work = os.path.join(self.scratch, "x-" + uuid.uuid4().hex[:10])
        self._dirs.append(work)
        bins = os.path.join(work, "bin")
        os.makedirs(bins, exist_ok=True)
        found = {}
        for i in range(0, len(paths), 200):                  # a regex of a few hundred paths at most
            chunk = paths[i:i + 200]
            self.wk.unbundle(self.archives(), bins, regex_for(chunk))
        for p in paths:
            fp = os.path.join(bins, p.replace("\\", os.sep))
            if os.path.isfile(fp):
                found[p] = fp
        out = {}
        # rounds of files with distinct base names (serialize writes <basename>.json into one folder)
        left = dict(found)
        rnd = 0
        while left:
            rnd += 1
            batch, names = {}, set()
            for p, fp in list(left.items()):
                b = os.path.basename(fp).lower()
                if b in names:
                    continue
                names.add(b)
                batch[p] = fp
                del left[p]
            src = os.path.join(work, f"in{rnd}")
            dst = os.path.join(work, f"json{rnd}")
            os.makedirs(src, exist_ok=True)
            for p, fp in batch.items():
                shutil.move(fp, os.path.join(src, os.path.basename(fp)))
            self.wk.serialize(src, dst)
            for p, fp in batch.items():
                j = os.path.join(dst, os.path.basename(fp) + ".json")
                if os.path.isfile(j):
                    out[p] = j
            shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(bins, ignore_errors=True)
        return out

    def cleanup(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)
        self._dirs = []
