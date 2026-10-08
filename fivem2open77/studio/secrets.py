"""Secret settings (API keys, server passwords): stored apart from settings.json.

Windows: encrypted with DPAPI for the current Windows user (CryptProtectData), so the file is
useless to other accounts and other machines.  Elsewhere: kept in a file only the user can read
(mode 600); not encrypted.  Secrets never leave the Studio through the API -- the UI only learns
whether one is set.
"""
from __future__ import annotations

import base64
import json
import os
import threading

from .paths import IS_WINDOWS

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _ENTROPY = b"fivem2open77-studio"

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _protect(data: bytes) -> bytes:
        src, _k1 = _blob(data)
        ent, _k2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(src), "fivem2open77", ctypes.byref(ent), None, None,
                                         0x1, ctypes.byref(out)):   # CRYPTPROTECT_UI_FORBIDDEN
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def _unprotect(data: bytes) -> bytes:
        src, _k1 = _blob(data)
        ent, _k2 = _blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                           ctypes.byref(out)):
            raise OSError("CryptUnprotectData failed (secret saved by another Windows user?)")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)
else:
    def _protect(data: bytes) -> bytes:
        return data

    def _unprotect(data: bytes) -> bytes:
        return data


class SecretStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
        if not IS_WINDOWS:
            os.chmod(self.path, 0o600)

    def get(self, key: str) -> str | None:
        with self.lock:
            v = self._load().get(key)
        if not v:
            return None
        try:
            return _unprotect(base64.b64decode(v)).decode("utf-8")
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: str | None):
        with self.lock:
            data = self._load()
            if value:
                data[key] = base64.b64encode(_protect(value.encode("utf-8"))).decode("ascii")
            else:
                data.pop(key, None)
            self._save(data)

    def has(self, key: str) -> bool:
        with self.lock:
            return key in self._load()

    def keys(self) -> list:
        with self.lock:
            return sorted(self._load())

    @property
    def encrypted(self) -> bool:
        return IS_WINDOWS
