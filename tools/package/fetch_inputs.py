"""Download and verify the pinned inputs for the Windows installer build (build_windows.py).

    python tools/package/fetch_inputs.py [--dir out/package-downloads] [--check]

Every input is pinned below by URL and sha256, taken from the verified Studio 1.4.1 release build (unchanged for 1.5.0 to 1.6.0):
the embeddable CPython, the .NET runtime for WolvenKit.CLI, the 7-Zip installer (7z.exe/7z.dll) and
LZMA SDK (7zSD.sfx stub), the WolvenKit.Console release, and the win_amd64 / cp312 wheels.  A file
that is present but hashes wrong is re-downloaded; everything is verified before use.  --check only
verifies (no network), for CI and tests.

Notes on the pins:
  * python 3.12.10 is the newest embeddable package python.org serves reliably (from some networks
    the 3.12.11+ Windows binaries 404 while the source tarballs do not); any 3.12.x matches
    build_windows.py's python-3.12.*-embed-amd64.zip glob.
  * wheel URLs are PyPI's content-addressed files.pythonhosted.org paths (blake2b-256 of the file),
    so they are immutable; the sha256 is checked anyway.

Then package in one command (7z on PATH):

    python tools/package/build_windows.py --downloads out/package-downloads \\
        --wolvenkit out/package-downloads/wolvenkit --host out/studio-1.6.0-host --out out/studio_1_6_0

(--host is the built window: dotnet build host/Fivem2Open77Studio.csproj -c Release -p:OutputPath=...)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WOLVENKIT = "WolvenKit.Console-8.17.4.zip"

# (path under --dir, URL, sha256)  -- the exact inputs of the verified 1.4.1 release build
PINNED = [
    ("python-3.12.10-embed-amd64.zip",
     "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
     "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"),
    ("dotnet-runtime-8.0.31-win-x64.zip",
     "https://builds.dotnet.microsoft.com/dotnet/Runtime/8.0.31/dotnet-runtime-8.0.31-win-x64.zip",
     "7deabbefdc83e378b343c551fe447b86002680eea91355b1f6f81d0c9ab0dcaa"),
    ("7z2604-x64.exe",
     "https://www.7-zip.org/a/7z2604-x64.exe",
     "d54bf805f9f3704d1e8db2fa3498ae7ef2df0312b40b558e7c71c734430a665d"),
    ("lzma2604.7z",
     "https://www.7-zip.org/a/lzma2604.7z",
     "bd98058d0e12c5da970dd4e5e1d8fa24f2d704914d96e69c28f92ed45421a439"),
    (WOLVENKIT,
     "https://github.com/WolvenKit/WolvenKit/releases/download/8.17.4/WolvenKit.Console-8.17.4.zip",
     "fb513158899e50893e5dfc296762dc90cbdc777e3dba3d3e0b231faefd29dd9c"),
    ("wheels/numpy-2.5.4-cp312-cp312-win_amd64.whl",
     "https://files.pythonhosted.org/packages/12/33/fefe62073dc8acfd0f2b9ed7c003af2f50aa61555e113e6db02b8f79f145/numpy-2.5.4-cp312-cp312-win_amd64.whl",
     "a7b1b6353e36a7e50de2973a38d705c88ee93adcf120673cee7f45a4a3fa223a"),
    ("wheels/paramiko-5.0.0-py3-none-any.whl",
     "https://files.pythonhosted.org/packages/82/5b/eadf6d45de38d30ab603f49393b6cd2cbe7e233af8cf90197e32782b68a9/paramiko-5.0.0-py3-none-any.whl",
     "b7044611c30140d9a75261653210e2002977b71a0497ff3ba0d98d7edbf62f7c"),
    ("wheels/cryptography-50.0.2-cp311-abi3-win_amd64.whl",
     "https://files.pythonhosted.org/packages/9a/4f/adfc442765721292fff86d314ce385d3249d22db42295c0dd057727b60f3/cryptography-50.0.2-cp311-abi3-win_amd64.whl",
     "7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c"),
    ("wheels/pynacl-1.6.2-cp38-abi3-win_amd64.whl",
     "https://files.pythonhosted.org/packages/41/ad/334600e8cacc7d86587fe5f565480fde569dfb487389c8e1be56ac21d8ac/pynacl-1.6.2-cp38-abi3-win_amd64.whl",
     "62985f233210dee6548c223301b6c25440852e13d59a8b81490203c3227c5ba0"),
    ("wheels/bcrypt-5.0.0-cp39-abi3-win_amd64.whl",
     "https://files.pythonhosted.org/packages/9f/b9/9d9a641194a730bda138b3dfe53f584d61c58cd5230e37566e83ec2ffa0d/bcrypt-5.0.0-cp39-abi3-win_amd64.whl",
     "64ee8434b0da054d830fa8e89e1c8bf30061d539044a39524ff7dec90481e5c2"),
    ("wheels/cffi-2.1.1-cp312-cp312-win_amd64.whl",
     "https://files.pythonhosted.org/packages/d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/cffi-2.1.1-cp312-cp312-win_amd64.whl",
     "f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a"),
    ("wheels/pycparser-3.11-py3-none-any.whl",
     "https://files.pythonhosted.org/packages/90/11/0e6f11117525ff0eec40ebac3d313376f102df93ca44ad9e893ee85e4f89/pycparser-3.11-py3-none-any.whl",
     "51d5a8ba2be0bbe440b99d2112604c95bbbc3c2748a64260186c541e1729cd80"),
    ("wheels/invoke-3.0.3-py3-none-any.whl",
     "https://files.pythonhosted.org/packages/5a/de/bbc12563bbf979618d17625a4e753ff7a078523e28d870d3626daa97261a/invoke-3.0.3-py3-none-any.whl",
     "f11327165e5cbb89b2ad1d88d3292b5113332c43b8553b494da435d6ec6f5053"),
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: str, tries: int = 3):
    """url -> dst (a .part file first), three attempts; the network is allowed to hiccup."""
    part = dst + ".part"
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fivem2open77-fetch-inputs"})
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
            return part
        except OSError as e:
            last = e
    raise SystemExit(f"failed to download {url} ({last})")


def ensure(name: str, url: str, want: str, root: str, check_only: bool = False) -> str:
    """One pinned input: present and matching -> 'ok'; wrong or missing -> (re)downloaded."""
    path = os.path.join(root, *name.split("/"))
    if os.path.isfile(path):
        if sha256(path) == want:
            return "ok"
        if check_only:
            return "HASH MISMATCH"
        os.remove(path)
    elif check_only:
        return "missing"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = download(url, path)
    got = sha256(part)
    if got != want:
        os.remove(part)
        raise SystemExit(f"{name}: sha256 {got} does not match the pin {want} (from {url})")
    os.replace(part, path)
    return "downloaded"


def extract_wolvenkit(root: str) -> str:
    """The Console release next to the downloads, as build_windows.py's --wolvenkit folder."""
    dst = os.path.join(root, "wolvenkit")
    if os.path.isfile(os.path.join(dst, "WolvenKit.CLI.dll")):
        return dst
    with zipfile.ZipFile(os.path.join(root, WOLVENKIT)) as z:
        z.extractall(dst)
    if not os.path.isfile(os.path.join(dst, "WolvenKit.CLI.dll")):
        raise SystemExit(f"{WOLVENKIT} has no WolvenKit.CLI.dll at its root")
    return dst


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=os.path.join("out", "package-downloads"),
                   help="input folder for build_windows.py --downloads (default out/package-downloads)")
    p.add_argument("--check", action="store_true", help="verify only: nothing is downloaded")
    a = p.parse_args(argv)
    root = os.path.abspath(os.path.join(ROOT, a.dir) if not os.path.isabs(a.dir) else a.dir)
    bad = 0
    for name, url, want in PINNED:
        state = ensure(name, url, want, root, check_only=a.check)
        bad += state not in ("ok", "downloaded")
        print(f"  {state:11} {name}")
    if not a.check:
        wk = extract_wolvenkit(root)
        print(f"  wolvenkit   {wk}")
    if bad:
        raise SystemExit(f"{bad} input(s) are missing or wrong; run without --check to fetch them")
    if not a.check:
        print(f"\ninputs ready.  package with:\n"
              f"  python tools/package/build_windows.py --downloads {a.dir} \\\n"
              f"      --wolvenkit {os.path.join(a.dir, 'wolvenkit')} --host out/studio-1.6.0-host --out out/studio_1_6_0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
