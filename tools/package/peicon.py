"""Give a Windows executable a new icon: its RT_ICON / RT_GROUP_ICON resources become the images of
an .ico file.  Used on 7-Zip's setup stub (7zSD.sfx), so the downloaded setup carries the app's
icon instead of 7-Zip's.

The resource section is written again from scratch (every other resource is kept as it was), so
it may grow; that needs .rsrc to be the last section of the file, as it is in 7zSD.sfx.  Needs
pefile (a build-time tool, like 7-Zip and the .NET SDK).

    python tools/package/peicon.py <exe> <icon.ico>
"""
from __future__ import annotations

import struct
import sys

RT_ICON, RT_GROUP_ICON = 3, 14


def read_ico(data: bytes) -> list:
    """[(width, height, colours, planes, bit count, image bytes)] of an .ico file."""
    reserved, kind, n = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or not n:
        raise ValueError("not an .ico file")
    out = []
    for i in range(n):
        w, h, colours, _r, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        out.append((w, h, colours, planes, bits, data[offset:offset + size]))
    return out


def _leaves(pe) -> list:
    """Every resource as (type id, name id, language id, code page, data)."""
    out = []
    for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        for n in t.directory.entries:
            for lang in n.directory.entries:
                if t.name is not None or n.name is not None or lang.name is not None:
                    raise ValueError("named resources are not supported")
                d = lang.data.struct
                out.append((t.id, n.id, lang.id, d.CodePage, pe.get_data(d.OffsetToData, d.Size)))
    return out


def _build_rsrc(leaves: list, rva: int) -> bytes:
    """The resource section for these leaves, its first byte at `rva`: the directory tree (type ->
    name -> language), then the data entries, then the data (8-byte aligned)."""
    tree = {}
    for t, n, lang, cp, data in leaves:
        tree.setdefault(t, {}).setdefault(n, {})[lang] = (cp, data)
    # layout: all directories first, breadth first
    dirs = [("root", None, sorted(tree))]
    for t in sorted(tree):
        dirs.append(("type", t, sorted(tree[t])))
    for t in sorted(tree):
        for n in sorted(tree[t]):
            dirs.append(("name", (t, n), sorted(tree[t][n])))
    dir_off, pos = {}, 0
    for kind, key, kids in dirs:
        dir_off[(kind, key)] = pos
        pos += 16 + 8 * len(kids)
    leaf_off = {}
    for t in sorted(tree):
        for n in sorted(tree[t]):
            for lang in sorted(tree[t][n]):
                leaf_off[(t, n, lang)] = pos
                pos += 16
    data_off = {}
    for t in sorted(tree):
        for n in sorted(tree[t]):
            for lang in sorted(tree[t][n]):
                pos = (pos + 7) & ~7
                data_off[(t, n, lang)] = pos
                pos += len(tree[t][n][lang][1])
    buf = bytearray(pos)

    def directory(at, kids_entries):
        struct.pack_into("<IIHHHH", buf, at, 0, 0, 0, 0, 0, len(kids_entries))
        for i, (ident, target) in enumerate(kids_entries):
            struct.pack_into("<II", buf, at + 16 + 8 * i, ident, target)

    SUB = 0x80000000
    directory(dir_off[("root", None)], [(t, SUB | dir_off[("type", t)]) for t in sorted(tree)])
    for t in sorted(tree):
        directory(dir_off[("type", t)], [(n, SUB | dir_off[("name", (t, n))]) for n in sorted(tree[t])])
        for n in sorted(tree[t]):
            directory(dir_off[("name", (t, n))], [(lang, leaf_off[(t, n, lang)]) for lang in sorted(tree[t][n])])
            for lang in sorted(tree[t][n]):
                cp, data = tree[t][n][lang]
                struct.pack_into("<IIII", buf, leaf_off[(t, n, lang)], rva + data_off[(t, n, lang)], len(data), cp, 0)
                buf[data_off[(t, n, lang)]:data_off[(t, n, lang)] + len(data)] = data
    return bytes(buf)


def set_icon(exe_path: str, ico_path: str, group_id: int | None = None) -> dict:
    """Replace the icon group (the first one, or `group_id`) and its images; returns what was written."""
    import pefile
    raw = open(exe_path, "rb").read()
    pe = pefile.PE(data=raw)
    rsrc = next((s for s in pe.sections if s.VirtualAddress == pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].VirtualAddress), None)
    if rsrc is None or rsrc is not pe.sections[-1]:
        raise ValueError("the resource section is not the last section of the file")
    end = rsrc.PointerToRawData + rsrc.SizeOfRawData
    if len(raw) > end:
        raise ValueError("the file has data after its sections: give the bare executable")
    if pe.OPTIONAL_HEADER.DATA_DIRECTORY[4].Size:
        raise ValueError("the file is signed: a changed file would not verify")
    leaves = _leaves(pe)
    groups = sorted({n for t, n, *_ in leaves if t == RT_GROUP_ICON})
    gid = group_id if group_id is not None else (groups[0] if groups else 1)
    lang = next((lg for t, n, lg, *_ in leaves if t == RT_GROUP_ICON and n == gid), 1033)
    cp = next((c for t, n, lg, c, _ in leaves if t == RT_GROUP_ICON and n == gid), 0)
    old_group = next((d for t, n, lg, c, d in leaves if t == RT_GROUP_ICON and n == gid), None)
    old_ids = set()
    if old_group:
        count = struct.unpack_from("<HHH", old_group, 0)[2]
        old_ids = {struct.unpack_from("<H", old_group, 6 + 14 * i + 12)[0] for i in range(count)}
    images = read_ico(open(ico_path, "rb").read())
    keep = [x for x in leaves if not (x[0] == RT_ICON and x[1] in old_ids) and not (x[0] == RT_GROUP_ICON and x[1] == gid)]
    used = {n for t, n, *_ in keep if t == RT_ICON}
    ids, nxt = [], 1
    for _ in images:
        while nxt in used:
            nxt += 1
        ids.append(nxt)
        used.add(nxt)
    group = struct.pack("<HHH", 0, 1, len(images))
    for (w, h, colours, planes, bits, data), nid in zip(images, ids):
        group += struct.pack("<BBBBHHIH", w, h, colours, 0, planes, bits, len(data), nid)
        keep.append((RT_ICON, nid, lang, cp, data))
    keep.append((RT_GROUP_ICON, gid, lang, cp, group))

    rva = rsrc.VirtualAddress
    body = _build_rsrc(keep, rva)
    fa, sa = pe.OPTIONAL_HEADER.FileAlignment, pe.OPTIONAL_HEADER.SectionAlignment
    raw_size = (len(body) + fa - 1) // fa * fa
    old_raw, old_virt = rsrc.SizeOfRawData, (rsrc.Misc_VirtualSize + sa - 1) // sa * sa
    rsrc.SizeOfRawData = raw_size
    rsrc.Misc_VirtualSize = len(body)
    new_virt = (len(body) + sa - 1) // sa * sa
    pe.OPTIONAL_HEADER.SizeOfImage = rva + new_virt
    pe.OPTIONAL_HEADER.SizeOfInitializedData += raw_size - old_raw
    pe.OPTIONAL_HEADER.DATA_DIRECTORY[2].Size = len(body)
    head = pe.write()[:rsrc.PointerToRawData]           # headers and every section before .rsrc, updated
    out = head + body + b"\0" * (raw_size - len(body))
    pe.close()
    check = pefile.PE(data=out)                          # parses, and the icons are the ones asked for
    n = sum(1 for t in check.DIRECTORY_ENTRY_RESOURCE.entries if t.id == RT_ICON for _ in t.directory.entries)
    check.close()
    with open(exe_path, "wb") as f:
        f.write(out)
    return {"icons": len(images), "rt_icon_entries": n, "rsrc_bytes": len(body), "image_size_grew": new_virt - old_virt}


if __name__ == "__main__":
    print(set_icon(sys.argv[1], sys.argv[2]))
