"""The F2077 icon and logo where the app uses them (tools/brand/make_brand.py makes the files), the
setup's icon (tools/package/peicon.py), and the Terminal page's side panel."""
import os
import re
import struct
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools", "package"))
WEB = os.path.join(ROOT, "fivem2open77", "studio", "web")
HOST = os.path.join(ROOT, "host")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _ico(data: bytes) -> dict:
    reserved, kind, n = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    out = {}
    for i in range(n):
        w, h, colours, _r, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        out[w or 256] = (bits, data[offset:offset + size])
    return out


def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


def test_the_app_icon_has_every_size_windows_asks_for():
    with open(os.path.join(HOST, "app.ico"), "rb") as f:
        ico = _ico(f.read())
    assert sorted(ico) == [16, 20, 24, 32, 40, 48, 64, 128, 256]
    for size, (bits, blob) in ico.items():
        assert bits == 32, size
        if size >= 128:
            assert blob[:8] == b"\x89PNG\r\n\x1a\n", size              # the large ones as PNG
            continue
        assert struct.unpack_from("<IiiHH", blob, 0) == (40, size, 2 * size, 1, 32), size   # 32-bit DIB
        px = blob[40:40 + size * size * 4]                                 # bottom-up BGRA

        def alpha(x, y):
            return px[((size - 1 - y) * size + x) * 4 + 3]
        # a rounded tile: transparent corners, the emblem opaque in the middle
        assert alpha(0, 0) == alpha(size - 1, 0) == alpha(0, size - 1) == alpha(size - 1, size - 1) == 0, size
        assert alpha(size // 2, size // 2) == 255, size
        mask = blob[40 + size * size * 4:]
        assert len(mask) == ((size + 31) // 32) * 4 * size, size           # the AND mask is there
        assert mask[0] & 0x80, size                                       # ... and says the corner is clear


def test_the_app_and_its_windows_use_the_icon_and_the_logo():
    csproj = _read("host", "Fivem2Open77Studio.csproj")
    assert "<ApplicationIcon>app.ico</ApplicationIcon>" in csproj
    assert 'Include="app.ico" LogicalName="Fivem2Open77.Studio.app.ico"' in csproj
    assert 'Include="emblem.png" LogicalName="Fivem2Open77.Studio.emblem.png"' in csproj
    assert '"Fivem2Open77.Studio.app.ico"' in _read("host", "AppIcon.cs")
    assert '"Fivem2Open77.Studio.emblem.png"' in _read("host", "HudSplash.cs")
    for form in ("MainForm.cs", "Installer.cs"):
        assert "Icon = AppIcon.Load() ?? Icon;" in _read("host", form), form
        assert "ExtractAssociatedIcon" not in _read("host", form), form
    assert "AppIcon.RefreshShell();" in _read("host", "Installer.cs")
    w, h = _png_size(os.path.join(HOST, "emblem.png"))
    assert w == 320 and 250 < h < 320

    html = _read("fivem2open77", "studio", "web", "index.html")
    assert '<link rel="icon" href="/img/icon-32.png" type="image/png" sizes="32x32">' in html
    assert '<link rel="icon" href="/img/icon-192.png" type="image/png" sizes="192x192">' in html
    assert re.search(r'<a class="brand"[^>]*>\s*<img src="/img/emblem.png"', html)
    assert not os.path.exists(os.path.join(WEB, "img", "icon.svg"))
    assert _png_size(os.path.join(WEB, "img", "icon-32.png")) == (32, 32)
    assert _png_size(os.path.join(WEB, "img", "icon-192.png")) == (192, 192)
    assert _png_size(os.path.join(WEB, "img", "emblem.png"))[0] == 128
    # the Home page's banner is the logo as it is
    with open(os.path.join(WEB, "img", "logo.jpg"), "rb") as a, open(os.path.join(ROOT, "docs", "brand", "f2077-logo.jpg"), "rb") as b:
        assert a.read() == b.read()
    assert 'src: "/img/logo.jpg"' in _read("fivem2open77", "studio", "web", "js", "pages", "home.js")
    assert "url(/img/logo.jpg)" in _read("fivem2open77", "studio", "web", "css", "universe.css")
    assert "docs/brand/f2077-logo.jpg" in _read("README.md")


def _tiny_pe(rsrc_leaves, peicon) -> bytes:
    """A minimal 32-bit PE whose only section is .rsrc (enough for the resource writer and pefile)."""
    body = peicon._build_rsrc(rsrc_leaves, 0x1000)
    raw = (len(body) + 0x1FF) // 0x200 * 0x200
    dos = b"MZ" + b"\0" * 58 + struct.pack("<I", 0x80)
    dos += b"\0" * (0x80 - len(dos))
    coff = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x0102)
    opt = struct.pack("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", 0x10B, 14, 0, 0, raw, 0, 0, 0x1000, 0x1000, 0x400000,
                      0x1000, 0x200, 6, 0, 0, 0, 6, 0, 0, 0x1000 + (len(body) + 0xFFF) // 0x1000 * 0x1000, 0x200,
                      0, 2, 0x8140, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dirs = [(0, 0)] * 16
    dirs[2] = (0x1000, len(body))
    opt += b"".join(struct.pack("<II", *d) for d in dirs)
    sect = struct.pack("<8sIIIIIIHHI", b".rsrc", len(body), 0x1000, raw, 0x200, 0, 0, 0, 0, 0x40000040)
    head = dos + b"PE\0\0" + coff + opt + sect
    return head + b"\0" * (0x200 - len(head)) + body + b"\0" * (raw - len(body))


def test_peicon_gives_an_executable_the_icon_and_keeps_the_rest(tmp_path):
    pefile = pytest.importorskip("pefile", reason="the setup's icon needs pefile (a build tool)")
    import peicon
    old_icon = b"\x28" + b"\0" * 39 + b"\x11" * 64
    group = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHIH", 4, 4, 0, 0, 1, 32, len(old_icon), 1)
    leaves = [(3, 1, 1033, 0, old_icon), (14, 1, 1033, 0, group), (5, 97, 1033, 0, b"dialog" * 9),
              (6, 7, 1033, 0, b"strings!"), (16, 1, 1033, 0, b"version-info" * 5)]
    exe = tmp_path / "stub.exe"
    exe.write_bytes(_tiny_pe(leaves, peicon))
    res = peicon.set_icon(str(exe), os.path.join(HOST, "app.ico"))
    assert res["icons"] == 9 and res["rt_icon_entries"] == 9
    pe = pefile.PE(str(exe))
    got = {}
    for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        for n in t.directory.entries:
            for lang in n.directory.entries:
                d = lang.data.struct
                got[(t.id, n.id, lang.id)] = pe.get_data(d.OffsetToData, d.Size)
    for t, n, lang, _cp, data in leaves[2:]:
        assert got[(t, n, lang)] == data                                   # the other resources as they were
    with open(os.path.join(HOST, "app.ico"), "rb") as f:
        ico = _ico(f.read())
    g = got[(14, 1, 1033)]
    count = struct.unpack_from("<HHH", g, 0)[2]
    assert count == 9
    for i in range(count):
        w, h, _c, _r, planes, bits, size, nid = struct.unpack_from("<BBBBHHIH", g, 6 + 14 * i)
        assert got[(3, nid, 1033)] == ico[w or 256][1] and size == len(ico[w or 256][1])
    assert pe.OPTIONAL_HEADER.SizeOfImage >= pe.sections[-1].VirtualAddress + pe.sections[-1].Misc_VirtualSize
    assert len(exe.read_bytes()) == pe.sections[-1].PointerToRawData + pe.sections[-1].SizeOfRawData
    # a file with data after its sections (a finished setup) is refused: the stub is branded first
    exe.write_bytes(exe.read_bytes() + b"7z archive")
    with pytest.raises(ValueError, match="data after its sections"):
        peicon.set_icon(str(exe), os.path.join(HOST, "app.ico"))


def test_the_terminal_side_panel_hides_and_opens_over_a_narrow_console():
    """It used to drop under the console (the grid lost its column but kept the panel), and a narrow
    window hid it for good."""
    css = _read("fivem2open77", "studio", "web", "css", "term.css")
    assert re.search(r"\.term-grid\.no-side \.term-side \{ display: none; \}", css)
    assert ".term-grid.side-open .term-side { display: flex; position: absolute;" in css
    js = _read("fivem2open77", "studio", "web", "js", "pages", "terminal.js")
    assert 'matchMedia("(max-width: 980px)")' in js and '"studio-term-side"' in js
    assert 'grid.classList.toggle("no-side", !narrow.matches && !wanted)' in js
    assert 'grid.classList.toggle("side-open", narrow.matches && drawer)' in js
