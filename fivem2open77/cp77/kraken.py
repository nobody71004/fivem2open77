"""Kraken (Oodle) decompression, through a library already on the machine.

The game packs its files with Oodle's Kraken.  Two decoders can be at hand:

    WolvenKit's   an open decoder next to WolvenKit.CLI.dll: kraken.dll (Windows), libkraken.so
                  (Linux), libkraken.dylib (macOS), exporting
                  ``int Kraken_Decompress(const byte *src, size_t src_len, byte *dst, size_t dst_len)``
    the game's    Oodle itself, ``bin\\x64\\oo2ext_7_win64.dll`` (Windows), exporting
                  ``OodleLZ_Decompress`` (what WolvenKit calls on Windows too)

A buffer the game stores compressed starts with ``KARK`` and the size it decompresses to.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading

NAMES = ("kraken.dll",) if os.name == "nt" else (("libkraken.dylib",) if sys.platform == "darwin" else ("libkraken.so",))
OODLE = os.path.join("bin", "x64", "oo2ext_7_win64.dll")
_lock = threading.Lock()
_libs: dict = {}


class KrakenError(RuntimeError):
    pass


def find(wolvenkit_dir: str | None) -> str | None:
    """The decoder next to WolvenKit (``wolvenkit_dir``: its folder, or WolvenKit.CLI.dll's path)."""
    if not wolvenkit_dir:
        return None
    d = wolvenkit_dir if os.path.isdir(wolvenkit_dir) else os.path.dirname(wolvenkit_dir)
    for n in NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    return None


def find_oodle(game_dir: str | None) -> str | None:
    """The game's own Oodle (Windows only: it is a Windows DLL)."""
    if os.name != "nt" or not game_dir:
        return None
    p = os.path.join(game_dir, OODLE)
    return p if os.path.isfile(p) else None


def load(path: str):
    with _lock:
        lib = _libs.get(path)
        if lib is None:
            lib = ctypes.CDLL(path)
            if hasattr(lib, "Kraken_Decompress"):
                lib.Kraken_Decompress.restype = ctypes.c_int
                lib.Kraken_Decompress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
            else:
                f = lib.OodleLZ_Decompress
                f.restype = ctypes.c_int64
                f.argtypes = [ctypes.c_char_p, ctypes.c_int64, ctypes.c_char_p, ctypes.c_int64, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]
            _libs[path] = lib
        return lib


class Kraken:
    """``Kraken(path).unpack(data, size)``: ``data`` with or without its KARK header."""

    def __init__(self, path: str):
        if not path or not os.path.isfile(path):
            raise KrakenError("no Kraken decoder: WolvenKit's (kraken.dll / libkraken.so next to WolvenKit) "
                              "and the game's (bin\\x64\\oo2ext_7_win64.dll) were not found")
        self.path = path
        self.lib = load(path)
        self.oodle = not hasattr(self.lib, "Kraken_Decompress")

    @classmethod
    def any(cls, wolvenkit_dir: str | None = None, game_dir: str | None = None) -> "Kraken":
        """WolvenKit's decoder, else the game's Oodle."""
        p = find(wolvenkit_dir) or find_oodle(game_dir)
        return cls(p)

    def unpack(self, data: bytes, size: int | None = None) -> bytes:
        if data[:4] == b"KARK":
            size = struct.unpack_from("<I", data, 4)[0]
            data = data[8:]
        if size is None:
            raise KrakenError("no size for a headerless Kraken buffer")
        if size == 0:
            return b""
        dst = ctypes.create_string_buffer(size + 64)        # (the decoder may write a little past the end)
        if self.oodle:
            # fuzz safe, no CRC check, quiet, no dictionary, no callback, no scratch, unthreaded
            n = self.lib.OodleLZ_Decompress(data, len(data), dst, size, 1, 0, 0, None, 0, None, None, None, 0, 3)
        else:
            n = self.lib.Kraken_Decompress(data, len(data), dst, size)
        if n != size:
            raise KrakenError(f"Kraken: {n} bytes out of {size}")
        return dst.raw[:size]
