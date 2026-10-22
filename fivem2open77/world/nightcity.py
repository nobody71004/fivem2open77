"""Night City off: the world with none of the city's own sectors streaming in.

The game streams the world from two blocks the world resource
(``base\\worlds\\03_night_city\\_compiled\\default\\03_night_city.streamingworld``) lists:

    base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock        Night City
    base\\worlds\\03_night_city\\_compiled\\default\\ep1\\blocks\\all.streamingblock    Dogtown (Phantom Liberty)

Each lists every sector with its streaming box, level and category.  Phantom Liberty ships its own
copy of both (in ``archive/pc/ep1``), so the copy read here is whichever the player's game uses.
A package that carries both blocks with only the sectors kept below replaces them (a file in
``archive/pc/mod`` wins over the game's own): no terrain, roads, buildings, interiors, props, water
or quest content of the city streams in any more.  The maps fivem2open77 converts are not touched:
ArchiveXL adds their blocks to the world on its own.

Kept, because game systems look for them and none of it is anything you see:

    AlwaysLoaded   map pins, NPC community registries, the traffic system's persistent data, the
                   distant GI of the old skyline (its lighting, no geometry)
    Navigation     the AI's navigation mesh (data only)

The distant lights (the lit windows of far-away buildings, drawn without any geometry) would stay
floating in the empty sky: their resource is replaced by one without lights.
"""
from __future__ import annotations

BLOCKS = (
    "base\\worlds\\03_night_city\\_compiled\\default\\blocks\\all.streamingblock",
    "base\\worlds\\03_night_city\\_compiled\\default\\ep1\\blocks\\all.streamingblock",
)
DISTANT_LIGHTS = "base\\worlds\\03_night_city\\sectors\\_generated\\distantlight.lights"
FILES = BLOCKS + (DISTANT_LIGHTS,)
KEEP = ("AlwaysLoaded", "Navigation")
CATEGORIES = ("Exterior", "Interior", "Quest", "AlwaysLoaded", "Navigation")
STATES = ("on", "off")


class CityError(ValueError):
    pass


def normalize_state(v) -> str:
    v = "on" if v in (None, "", True) else "off" if v is False else str(v).strip().lower()
    if v not in STATES:
        raise CityError(f"Night City must be on or off, not {v!r}")
    return v


def _root(doc: dict) -> dict:
    return (doc.get("Data") or {}).get("RootChunk") or {}


def _with_root(doc: dict, root: dict) -> dict:
    """The document with another root chunk (the rest shared, not copied: these are large)."""
    return dict(doc, Data=dict(doc.get("Data") or {}, RootChunk=root))


def filter_block(doc: dict, keep=KEEP) -> tuple:
    """A streaming block (WolvenKit JSON) with only the sectors of the ``keep`` categories:
    (new document, {category: [kept, removed]})."""
    root = _root(doc)
    if root.get("$type") != "worldStreamingBlock":
        raise CityError(f"not a streaming block: {root.get('$type')!r}")
    keep = set(keep)
    counts, kept = {}, []
    for d in root.get("descriptors") or []:
        cat = str(d.get("category") or "")
        c = counts.setdefault(cat, [0, 0])
        if cat in keep:
            kept.append(d)
            c[0] += 1
        else:
            c[1] += 1
    return _with_root(doc, dict(root, descriptors=kept)), counts


def dark_distant_lights(doc: dict) -> dict:
    """The distant lights resource with no lights: its one buffer (a flat array of light records)
    left empty."""
    root = _root(doc)
    if root.get("$type") != "CDistantLightsResource":
        raise CityError(f"not the distant lights resource: {root.get('$type')!r}")
    data = dict(root.get("data") or {})
    data.update({"BufferId": data.get("BufferId", "0"), "Flags": data.get("Flags", 0), "Bytes": ""})
    return _with_root(doc, dict(root, data=data))


def documents(read, keep=KEEP) -> tuple:
    """``read(depot path) -> WolvenKit JSON document`` (the player's game's own files) ->
    ([(document, depot path)] replacing the city's, {block: {category: [kept, removed]}})."""
    files, report = [], {}
    for path in BLOCKS:
        doc, counts = filter_block(read(path), keep)
        files.append((doc, path))
        report[path] = counts
    files.append((dark_distant_lights(read(DISTANT_LIGHTS)), DISTANT_LIGHTS))
    return files, report


def removed_sectors(report: dict) -> int:
    return sum(c[1] for counts in report.values() for c in counts.values())


def kept_sectors(report: dict) -> int:
    return sum(c[0] for counts in report.values() for c in counts.values())
