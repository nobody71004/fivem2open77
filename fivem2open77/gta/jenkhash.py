"""Jenkins one-at-a-time hash ("joaat") as used by RAGE for names, shaders and params."""


def joaat(text: str, lower: bool = True) -> int:
    data = (text.lower() if lower else text).encode("utf-8")
    h = 0
    for b in data:
        h = (h + b) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    return h


class HashNames:
    """Reverse lookup table hash -> name, seeded with known strings."""

    def __init__(self, names=()):
        self._map = {}
        for n in names:
            self.add(n)

    def add(self, name: str):
        self._map.setdefault(joaat(name), name)

    def get(self, h: int, default=None):
        return self._map.get(h, default if default is not None else f"0x{h:08X}")

    def __contains__(self, h):
        return h in self._map
