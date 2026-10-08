"""Minimal glTF 2.0 binary reader (for tests and analysis)."""
import json
import struct

import numpy as np

_CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(path):
    data = open(path, "rb").read()
    magic, ver, length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF"
    off = 12
    js, bin_ = None, b""
    while off < length:
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8:off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk)
        elif ctype == 0x004E4942:
            bin_ = chunk
        off += 8 + clen
    return js, bin_


def accessor(js, bin_, idx):
    a = js["accessors"][idx]
    bv = js["bufferViews"][a["bufferView"]]
    dt = _CT[a["componentType"]]
    n = _NC[a["type"]]
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride")
    itemsize = np.dtype(dt).itemsize * n
    if stride and stride != itemsize:
        raw = np.frombuffer(bin_, dtype=np.uint8, count=stride * a["count"], offset=start).reshape(a["count"], stride)
        arr = raw[:, :itemsize].copy().view(dt).reshape(a["count"], n)
    else:
        arr = np.frombuffer(bin_, dtype=dt, count=a["count"] * n, offset=start).reshape(a["count"], n)
    if a.get("normalized") and dt in (np.uint8, np.uint16):
        arr = arr.astype(np.float32) / np.iinfo(dt).max
    return arr
