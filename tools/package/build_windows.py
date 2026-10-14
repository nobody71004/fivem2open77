"""Build the Windows app: a folder with everything the Studio needs, and a one-file setup.exe.

    python tools/package/build_windows.py --downloads DIR --wolvenkit DIR --host DIR --out DIR

DIR given by --downloads holds:
    python-3.12.*-embed-amd64.zip        python.org embeddable package
    dotnet-runtime-8.0.*-win-x64.zip     .NET 8 runtime (runs WolvenKit.CLI.dll)
    7z*-x64.exe                          7-Zip installer (7z.exe and 7z.dll are taken from it)
    lzma*.7z                             LZMA SDK (7zSD.sfx, the self-extracting setup stub)
    wheels/*.whl                         numpy + paramiko and their dependencies for win_amd64 / cp312
--wolvenkit: an extracted WolvenKit.Console release; --host: the built fivem2open77-studio.exe folder.

Layout (F2O77_HOME):
    fivem2open77-studio.exe (+ WebView2 dlls)   the window
    app/fivem2open77/                           the converter and the Studio (Python)
    tools/deploy/                               server_deploy.sh and the command-line deploy scripts
    tools/wolvenkit/  tools/7zip/               bundled tools
    runtime/python/  runtime/dotnet/            bundled runtimes
    LICENSES/                                   third-party licences
Needs 7z on PATH (p7zip / 7-Zip) to unpack and pack.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION = "1.5.1"

SFX_CONFIG = """;!@Install@!UTF-8!
Title="fivem2open77 Studio {version}"
RunProgram="fivem2open77-studio.exe --install"
GUIMode="1"
;!@InstallEnd@!
"""

README = """fivem2open77 Studio {version}
===========================

Converts FiveM (GTA V) vehicle resources into Open77 (Cyberpunk 2077 multiplayer) cars, previews
them in 3D, builds them with WolvenKit, checks them, and installs them on your Open77 servers --
by hand or with the built-in AI agent (Claude or a local Ollama model).

Start:   fivem2open77-studio.exe       (asks once to install itself to %LOCALAPPDATA%\\Programs)
         fivem2open77-studio.exe --browser    use your web browser instead of the app window
Remove:  Settings > Apps > fivem2open77 Studio, or fivem2open77-studio.exe --uninstall
Data:    %LOCALAPPDATA%\\fivem2open77\\studio   (projects, builds, settings, logs)

First run: Settings > Tools finds your Cyberpunk 2077 install and extracts the donor car's files
from it (once, about a minute).  The game's files are never shipped with the app.

Command line (same converter):
    runtime\\python\\python.exe -m fivem2open77 convert <car.zip|rar|7z|folder> --out <dir> --door-chime chime.mp3
    runtime\\python\\python.exe -m fivem2open77 --help

Bundled third-party software (licences in LICENSES\\): Python, numpy, paramiko, cryptography,
PyNaCl, bcrypt, cffi, pycparser, .NET runtime, WolvenKit CLI, 7-Zip, three.js, Microsoft Edge
WebView2 SDK.
"""


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(cmd)}\n{r.stdout[-2000:]}")
    return r.stdout


def one(pattern):
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise SystemExit(f"missing: {pattern}")
    return hits[-1]


def copytree(src, dst, ignore=None):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*(ignore or [])), dirs_exist_ok=True)


def build(a):
    # the launcher is the window the Studio opens: a bundle without it is no release (the 1.4.1 release
    # shipped with no launcher because nothing checked for it)
    if not os.path.isfile(os.path.join(a.host, "fivem2open77-studio.exe")):
        raise SystemExit(f"--host {a.host} has no fivem2open77-studio.exe: build the launcher first "
                         "(dotnet build host/Fivem2Open77Studio.csproj -c Release -p:OutputPath=...)")
    out = os.path.abspath(a.out)
    app = os.path.join(out, "fivem2open77-studio")
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(app)
    dl = os.path.abspath(a.downloads)
    lic = os.path.join(app, "LICENSES")
    os.makedirs(lic)

    # window
    for f in os.listdir(a.host):
        s = os.path.join(a.host, f)
        if f.endswith((".pdb", ".xml")) or f == "Microsoft.Web.WebView2.Wpf.dll":
            continue
        (copytree if os.path.isdir(s) else shutil.copy2)(s, os.path.join(app, f))

    if not os.path.isfile(os.path.join(app, "fivem2open77-studio.exe")):
        raise SystemExit("the launcher is missing from the app folder: refusing to package")

    # python
    py = os.path.join(app, "runtime", "python")
    os.makedirs(py)
    pyzip = one(os.path.join(dl, "python-3.12.*-embed-amd64.zip"))
    with zipfile.ZipFile(pyzip) as z:
        z.extractall(py)
    pth = [p for p in os.listdir(py) if p.endswith("._pth")]
    if len(pth) != 1:
        raise SystemExit("unexpected embeddable layout (no ._pth)")
    stdlib_zip = [p for p in os.listdir(py) if p.startswith("python3") and p.endswith(".zip")][0]
    with open(os.path.join(py, pth[0]), "w", encoding="ascii", newline="\r\n") as f:
        f.write(f"{stdlib_zip}\n.\nLib\\site-packages\n..\\..\\app\nimport site\n")
    sp = os.path.join(py, "Lib", "site-packages")
    os.makedirs(sp)
    wheels = sorted(glob.glob(os.path.join(dl, "wheels", "*.whl")))
    names = []
    for w in wheels:
        with zipfile.ZipFile(w) as z:
            z.extractall(sp)
        names.append(os.path.basename(w))
    for need in ("numpy", "paramiko", "cryptography", "nacl", "bcrypt", "cffi"):
        if not os.path.isdir(os.path.join(sp, need)):
            raise SystemExit(f"wheel for {need} missing in {dl}/wheels")
    shutil.copy2(os.path.join(py, "LICENSE.txt"), os.path.join(lic, "Python-LICENSE.txt"))
    for d in glob.glob(os.path.join(sp, "*.dist-info")):
        for lf in glob.glob(os.path.join(d, "LICEN[CS]E*")) + glob.glob(os.path.join(d, "licenses", "*")):
            if os.path.isfile(lf):
                shutil.copy2(lf, os.path.join(lic, os.path.basename(d).split("-")[0] + "-" + os.path.basename(lf)))

    # .NET runtime for WolvenKit.CLI.dll
    dn = os.path.join(app, "runtime", "dotnet")
    os.makedirs(dn)
    with zipfile.ZipFile(one(os.path.join(dl, "dotnet-runtime-8.0.*-win-x64.zip"))) as z:
        z.extractall(dn)
    if not os.path.isfile(os.path.join(dn, "dotnet.exe")):
        raise SystemExit("dotnet runtime zip has no dotnet.exe")
    for f in ("LICENSE.txt", "ThirdPartyNotices.txt"):
        if os.path.isfile(os.path.join(dn, f)):
            shutil.copy2(os.path.join(dn, f), os.path.join(lic, f".NET-{f}"))

    # 7-Zip
    sz = os.path.join(app, "tools", "7zip")
    os.makedirs(sz)
    sh("7z", "e", "-y", f"-o{sz}", one(os.path.join(dl, "7z*-x64.exe")), "7z.exe", "7z.dll", "License.txt")
    os.replace(os.path.join(sz, "License.txt"), os.path.join(lic, "7-Zip-License.txt"))

    # WolvenKit CLI
    wk = os.path.join(app, "tools", "wolvenkit")
    copytree(a.wolvenkit, wk)
    if not os.path.isfile(os.path.join(wk, "WolvenKit.CLI.dll")):
        raise SystemExit("WolvenKit.CLI.dll not found in --wolvenkit")
    for lf in glob.glob(os.path.join(wk, "**", "COPYING*"), recursive=True) + glob.glob(os.path.join(wk, "LICENSE*")):
        shutil.copy2(lf, os.path.join(lic, "WolvenKit-" + os.path.basename(lf)))
    with open(os.path.join(lic, "WolvenKit-NOTICE.txt"), "w") as f:
        f.write("WolvenKit CLI is free software by the WolvenKit team: https://github.com/WolvenKit/WolvenKit "
                "(source code there).\n")

    # the converter + Studio
    copytree(os.path.join(ROOT, "fivem2open77"), os.path.join(app, "app", "fivem2open77"),
             ignore=["__pycache__", "*.pyc", ".pytest_cache"])
    copytree(os.path.join(ROOT, "tools", "deploy"), os.path.join(app, "tools", "deploy"), ignore=["*.log"])
    shutil.copy2(os.path.join(ROOT, "tools", "sync_required_mods.py"), os.path.join(app, "tools", "sync_required_mods.py"))
    three = os.path.join(ROOT, "fivem2open77", "studio", "web", "vendor", "three", "LICENSE")
    shutil.copy2(three, os.path.join(lic, "three.js-LICENSE.txt"))
    with open(os.path.join(lic, "WebView2-NOTICE.txt"), "w") as f:
        f.write("Microsoft Edge WebView2 SDK (Microsoft.Web.WebView2 NuGet package), (c) Microsoft, BSD-style licence: "
                "https://www.nuget.org/packages/Microsoft.Web.WebView2/\n")
    with open(os.path.join(app, "README.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(README.format(version=VERSION))

    manifest = {"version": VERSION, "wheels": names, "python": os.path.basename(pyzip),
                "dotnet": os.path.basename(one(os.path.join(dl, "dotnet-runtime-8.0.*-win-x64.zip")))}
    with open(os.path.join(app, "app", "build-info.json"), "w") as f:
        json.dump(manifest, f, indent=1)

    # one-file setup: 7zSD.sfx + config + 7z archive of the folder
    sfx_dir = os.path.join(out, "_sfx")
    shutil.rmtree(sfx_dir, ignore_errors=True)
    os.makedirs(sfx_dir)
    sh("7z", "e", "-y", f"-o{sfx_dir}", one(os.path.join(dl, "lzma*.7z")), "bin/7zSD.sfx")
    archive = os.path.join(out, "_app.7z")
    if os.path.exists(archive):
        os.remove(archive)
    sh("7z", "a", "-t7z", "-mx=7", "-m0=lzma2", "-mmt=on", "-ms=on", archive, ".", cwd=app)
    setup = os.path.join(out, f"fivem2open77-studio-{VERSION}-setup.exe")
    with open(setup, "wb") as f:
        for part in (os.path.join(sfx_dir, "7zSD.sfx"),):
            f.write(open(part, "rb").read())
        f.write(SFX_CONFIG.format(version=VERSION).encode("utf-8"))
        with open(archive, "rb") as z:
            shutil.copyfileobj(z, f)
    shutil.rmtree(sfx_dir, ignore_errors=True)
    os.remove(archive)
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(app) for f in fs)
    h = hashlib.sha256(open(setup, "rb").read()).hexdigest()
    print(f"app folder: {app} ({size / 1048576:.0f} MiB)")
    print(f"setup:      {setup} ({os.path.getsize(setup) / 1048576:.1f} MiB) sha256 {h}")
    return setup


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--downloads", required=True)
    p.add_argument("--wolvenkit", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--out", required=True)
    build(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
