"""The Windows packaging: the release versions agree, and a bundle without the Studio launcher is refused."""
import argparse
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "package"))

import build_windows  # noqa: E402
from fivem2open77.studio.app import APP_VERSION  # noqa: E402


def test_release_versions_agree():
    with open(os.path.join(ROOT, "host", "Fivem2Open77Studio.csproj"), encoding="utf-8") as f:
        csproj = re.search(r"<Version>([^<]+)</Version>", f.read()).group(1)
    assert build_windows.VERSION == APP_VERSION == csproj


def test_build_refuses_a_host_without_the_launcher(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "WebView2Loader.dll").write_bytes(b"x")
    args = argparse.Namespace(downloads=str(tmp_path / "dl"), wolvenkit=str(tmp_path / "wk"),
                              host=str(host), out=str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="no fivem2open77-studio.exe"):
        build_windows.build(args)
    assert not (tmp_path / "out").exists()
