"""NCWE asset imports: storage under a map project, repacking into preload packages at build time,
the writer hook that ships them, and the QA rule that imported packages keep their own names."""
import io
import os
import struct
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77 import package as package_mod  # noqa: E402
from fivem2open77.mlo import resource as resource_mod  # noqa: E402
from fivem2open77.mlo import writer as writer_mod  # noqa: E402
from fivem2open77.studio import ncweassets, qa  # noqa: E402
from fivem2open77.studio.ncweassets import AssetError  # noqa: E402


# --------------------------------------------------------------------------- helpers
def _fake_archive(entries=2):
    """A tiny RDAR archive with a valid CRC'd index (same shape as tests/test_offline.py)."""
    table = bytearray(struct.pack("<III", entries, 0, 0))
    for i in range(entries):
        table += struct.pack("<Qq5I20s", 1000 + i, 123 + i, 0, 0, 0, 0, 0, b"\0" * 20)
    index_pos = 44
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, index_pos, len(table) + 16, 0, 0, 0) + b"\0\0\0\0"
    index = struct.pack("<IIQ", 8, len(table) + 8, package_mod._crc64_xz(bytes(table))) + table
    return head + index


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in files.items():
            z.writestr(arc, data)
    return buf.getvalue()


class _Project:
    """Just enough of a Studio project for the asset store."""

    def __init__(self, root):
        self.dir = str(root)

    def path(self, *parts):
        return os.path.join(self.dir, *parts)


def _quiet(_msg):
    pass


# --------------------------------------------------------------------------- storage
def test_safe_relpath_rules():
    assert ncweassets.safe_relpath("props.zip") == "props.zip"
    assert ncweassets.safe_relpath("pack\\archive\\pc\\mod\\x.archive") == "pack/archive/pc/mod/x.archive"
    assert ncweassets.safe_relpath("/abs/x") == "abs/x"      # a leading slash is made relative, not an escape
    for bad in ("", "/", "..", "../evil", "a/../b", ".hidden", "a/.git", "a/b/c/d/e/f/g/h/i"):
        with pytest.raises(AssetError):
            ncweassets.safe_relpath(bad)


def test_add_list_remove_import(tmp_path):
    p = _Project(tmp_path / "proj")
    data = _zip_bytes({"mymod/archive/pc/mod/props.archive": _fake_archive()})
    a = ncweassets.add_import(p, "mymod.zip", io.BytesIO(data), len(data))
    assert a["kind"] == "NCWE export (zip)" and a["archives"] == 1 and a["ready"]
    # a folder drop keeps its relative paths: one archive plus a note that does not ship
    small = _fake_archive()
    ncweassets.add_import(p, "pack/props2.archive", io.BytesIO(small), len(small))
    ncweassets.add_import(p, "pack/notes.txt", io.BytesIO(b"hi"), 2)
    listing = ncweassets.list_imports(p)
    assert [x["name"] for x in listing] == ["mymod.zip", "pack"]
    pack = listing[1]
    assert pack["kind"] == "folder" and pack["archives"] == 1 and pack["files"] == 2 and pack["ready"]
    assert ncweassets.remove_import(p, "mymod.zip") == "mymod.zip"
    assert [x["name"] for x in ncweassets.list_imports(p)] == ["pack"]
    with pytest.raises(AssetError):
        ncweassets.remove_import(p, "pack/props2.archive")   # remove the import, not a file inside it
    assert ncweassets.remove_import(p, "pack") == "pack"
    assert ncweassets.list_imports(p) == []


def test_add_import_rejects_bad_uploads(tmp_path):
    p = _Project(tmp_path / "proj")
    with pytest.raises(AssetError, match="empty"):
        ncweassets.add_import(p, "x.zip", io.BytesIO(b""), 0)
    with pytest.raises(AssetError, match="got 3 of 5"):
        ncweassets.add_import(p, "x.zip", io.BytesIO(b"abc"), 5)
    assert not os.path.exists(p.path("assets", "ncwe", "x.zip"))
    with pytest.raises(AssetError, match="import limit"):
        ncweassets.add_import(p, "big.zip", io.BytesIO(b"x"), ncweassets.MAX_IMPORT_BYTES + 1)
    small = _fake_archive()
    ncweassets.add_import(p, "pack/a.archive", io.BytesIO(small), len(small))
    with pytest.raises(AssetError, match="imported folder"):
        ncweassets.add_import(p, "pack", io.BytesIO(small), len(small))


# --------------------------------------------------------------------------- build-time repacking
def test_prepare_repacks_folder_and_loose_archive(tmp_path):
    assets = tmp_path / "assets"
    (assets / "pack1" / "archive" / "pc" / "mod").mkdir(parents=True)
    (assets / "pack1" / "archive" / "pc" / "mod" / "props.archive").write_bytes(_fake_archive())
    (assets / "pack1" / "notes.txt").write_text("hello")
    (assets / "solo.archive").write_bytes(_fake_archive())
    (assets / "notes2.txt").write_text("x")
    packages, warnings = ncweassets.prepare(str(assets), str(tmp_path / "staging"), log=_quiet)
    assert [p["source"] for p in packages] == ["pack1", "solo.archive"]
    assert [p["archives"] for p in packages] == [["props.archive"], ["solo.archive"]]
    assert len(warnings) == 2 and "notes2.txt" in warnings[0] and "pack1" in warnings[1]
    with zipfile.ZipFile(packages[0]["zip"]) as z:
        assert z.namelist() == ["archive/pc/mod/props.archive"]
    with zipfile.ZipFile(packages[1]["zip"]) as z:
        assert z.namelist() == ["archive/pc/mod/solo.archive"]


def test_prepare_zip_export_layout(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "mymod.zip").write_bytes(_zip_bytes({
        "MyMod/archive/pc/mod/props.archive": _fake_archive(),
        "MyMod/archive/pc/mod/props.archive.xl": b"xl",
        "MyMod/README.md": b"hi",
    }))
    (assets / "empty.zip").write_bytes(_zip_bytes({"a.txt": b"x"}))
    packages, warnings = ncweassets.prepare(str(assets), str(tmp_path / "staging"), log=_quiet)
    assert len(packages) == 1 and packages[0]["source"] == "mymod.zip"
    assert packages[0]["archives"] == ["props.archive"]
    with zipfile.ZipFile(packages[0]["zip"]) as z:
        assert set(z.namelist()) == {"archive/pc/mod/props.archive", "archive/pc/mod/props.archive.xl"}
    assert any("empty.zip" in w and "skipped" in w for w in warnings)


# --------------------------------------------------------------------------- the writer hook
def test_attach_extra_packages_ships_imports(tmp_path):
    res = tmp_path / "res"
    staging = tmp_path / "staging"
    staging.mkdir()
    imp = staging / "import-2.zip"
    imp.write_bytes(_zip_bytes({"archive/pc/mod/props.archive": _fake_archive()}))
    layout = {"packages": ["map.zip"], "archives": ["fivem2open77_map_map.archive"]}
    zips = [{"file": "map.zip", "bytes": 3, "archive": "fivem2open77_map_map.archive", "files": 1}]
    extra = [{"zip": str(imp), "source": "My Mod", "archives": ["props.archive"]},
             {"zip": str(imp), "source": "My Mod", "archives": ["props.archive"]}]
    log = []
    added = writer_mod.attach_extra_packages(str(res), "map", layout, zips, extra, log=log.append)
    assert [a["file"] for a in added] == ["map_ncwe_my_mod.zip", "map_ncwe_my_mod_2.zip"]
    assert layout["packages"] == ["map.zip", "map_ncwe_my_mod.zip", "map_ncwe_my_mod_2.zip"]
    assert layout["archives"] == ["fivem2open77_map_map.archive", "props.archive", "props.archive"]
    assert [z["file"] for z in zips] == ["map.zip", "map_ncwe_my_mod.zip", "map_ncwe_my_mod_2.zip"]
    for a in added:
        f = res / "dist" / a["file"]
        assert f.is_file() and f.read_bytes() == imp.read_bytes()
    assert any("imported NCWE asset My Mod" in m for m in log)


# --------------------------------------------------------------------------- QA scoping
def _map_out(tmp_path, own_archive="fivem2open77_map_cookiemap.archive"):
    """One map build out folder (map resource + manager), the shape MapWriter leaves behind."""
    out = tmp_path / "out"
    res = out / "cookiemap"
    dist = res / "dist"
    dist.mkdir(parents=True)
    (dist / "cookiemap.zip").write_bytes(_zip_bytes({f"archive/pc/mod/{own_archive}": _fake_archive()}))
    (dist / "cookiemap_ncwe_props.zip").write_bytes(_zip_bytes({"archive/pc/mod/myprops.archive": _fake_archive()}))
    packages = ["cookiemap.zip", "cookiemap_ncwe_props.zip"]
    layout = {"name": "cookiemap", "display": "Cookie", "version": "1.0.5",
              "parts": {}, "placements": [], "spawn": None,
              "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
              "packages": packages, "archives": [own_archive, "myprops.archive"], "default": None}
    resource_mod.write_map_resource(str(res), layout, packages, None, report={"warnings": []})
    (res / "ghost.txt").write_text("")
    mgr = out / "f2o77_maps"
    resource_mod.write_manager_resource(str(mgr))
    resource_mod.write_map_def(str(mgr), layout, None, "")
    return out


def test_check_map_build_accepts_imported_archive_names(tmp_path):
    r = qa.check_map_build(str(_map_out(tmp_path)))
    by = {c["id"]: c for c in r["checks"]}
    assert by["archive:myprops.archive:imported"]["ok"], by["archive:myprops.archive:imported"]
    assert by["archive:fivem2open77_map_cookiemap.archive:name"]["ok"]
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]


def test_check_map_build_still_names_own_archives(tmp_path):
    r = qa.check_map_build(str(_map_out(tmp_path, own_archive="wrongname.archive")))
    by = {c["id"]: c for c in r["checks"]}
    assert not by["archive:wrongname.archive:name"]["ok"]
    assert not r["ok"]
