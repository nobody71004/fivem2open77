"""The external tools a conversion needs, found automatically:

  Cyberpunk 2077   the player's own install (donor files come from it, once)  -- Steam, GOG, Epic
  WolvenKit CLI    writes the game files                                      -- bundled with the app
  .NET 8 runtime   runs WolvenKit.CLI.dll                                     -- bundled with the app
  7-Zip            opens .rar/.7z FiveM downloads                             -- bundled with the app
  .NET 8 (Linux)   the map baker's runtime, packed into its toolkit for Linux -- bundled with the app (from 1.7.3),
                   server hosts (linuxdotnet.py)                                  or downloaded in Settings > Tools
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil

from ..cp77.donor import DONORS, REQUIRED, _paths
from . import linuxdotnet
from .paths import IS_WINDOWS, app_home, bundled

EXE = ".exe" if IS_WINDOWS else ""


# ----------------------------------------------------------------------------- Cyberpunk 2077
def _valid_game(d: str | None) -> bool:
    return bool(d) and os.path.isdir(os.path.join(d, "archive", "pc", "content"))


def _reg(root, key, value):
    try:
        import winreg
        with winreg.OpenKey(root, key) as k:
            return winreg.QueryValueEx(k, value)[0]
    except OSError:
        return None


def _steam_libraries() -> list:
    roots = []
    if IS_WINDOWS:
        import winreg
        for root, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            v = _reg(root, key, val)
            if v:
                roots.append(os.path.normpath(v))
        roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    else:
        roots += [os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]
    libs = []
    for r in roots:
        if r not in libs and os.path.isdir(r):
            libs.append(r)
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            text = open(vdf, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = os.path.normpath(m.group(1).replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    return libs


def find_game_dirs() -> list:
    """Every Cyberpunk 2077 install found, as (path, where)."""
    found = []

    def add(p, where):
        if p and _valid_game(p) and os.path.normcase(os.path.abspath(p)) not in [os.path.normcase(x) for x, _ in found]:
            found.append((os.path.abspath(p), where))

    for env in ("CP77_GAME_DIR", "OP77_GAME_DIR"):
        add(os.environ.get(env), env)
    for lib in _steam_libraries():
        add(os.path.join(lib, "steamapps", "common", "Cyberpunk 2077"), "Steam")
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games") as games:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(games, i)
                    except OSError:
                        break
                    i += 1
                    name = _reg(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\GOG.com\Games\{sub}", "gameName") or ""
                    if "cyberpunk" in name.lower():
                        add(_reg(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\GOG.com\Games\{sub}", "path"), "GOG")
        except OSError:
            pass
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        for item in glob.glob(os.path.join(pd, "Epic", "EpicGamesLauncher", "Data", "Manifests", "*.item")):
            try:
                m = json.load(open(item, "r", encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if "cyberpunk" in str(m.get("DisplayName", "")).lower():
                add(m.get("InstallLocation"), "Epic")
        for drive in "CDEFGH":
            for rel in (r"GOG Games\Cyberpunk 2077", r"Program Files (x86)\GOG Galaxy\Games\Cyberpunk 2077",
                        r"SteamLibrary\steamapps\common\Cyberpunk 2077", r"Games\Cyberpunk 2077",
                        r"Program Files\Epic Games\Cyberpunk2077", r"Epic Games\Cyberpunk2077"):
                add(f"{drive}:\\{rel}", "folder")
    return found


# ----------------------------------------------------------------------------- binaries
def find_dotnet(configured: str = "") -> str | None:
    cands = [configured, bundled("runtime", "dotnet", "dotnet" + EXE), os.environ.get("DOTNET"),
             os.path.join(os.environ.get("DOTNET_ROOT", ""), "dotnet" + EXE) if os.environ.get("DOTNET_ROOT") else None,
             shutil.which("dotnet")]
    if IS_WINDOWS:
        cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet", "dotnet.exe"))
    else:
        cands += ["/opt/dotnet/dotnet", "/usr/share/dotnet/dotnet", "/usr/lib/dotnet/dotnet"]
    for c in cands:
        if c and os.path.isfile(c) and dotnet8(c):
            return os.path.abspath(c)
    return None


def dotnet8(dotnet: str) -> bool:
    """A Microsoft.NETCore.App 8.x runtime next to this dotnet host."""
    shared = os.path.join(os.path.dirname(os.path.abspath(dotnet)), "shared", "Microsoft.NETCore.App")
    return any(re.match(r"^8\.", d) for d in (os.listdir(shared) if os.path.isdir(shared) else []))


def find_wolvenkit(configured: str = "") -> str | None:
    cands = [configured, bundled("tools", "wolvenkit", "WolvenKit.CLI.dll"), bundled("tools", "wolvenkit", "WolvenKit.CLI.exe"),
             os.environ.get("WOLVENKIT_CLI")]
    if not IS_WINDOWS:
        cands.append("/opt/f2o77/wkcli/WolvenKit.CLI.dll")
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    try:
        from ..cp77.wolvenkit import _candidates
        for c in _candidates():
            if os.path.isfile(c):
                return os.path.abspath(c)
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_linux_dotnet_root(d: str | None) -> bool:
    return linuxdotnet.is_root(d)


def linux_dotnet_download_dir(workspace: str) -> str:
    """Where Settings > Tools puts the runtime when the app does not carry it."""
    return os.path.join(workspace, "tools", "dotnet-linux-x64")


def find_linux_dotnet(configured: str = "", workspace: str | None = None) -> str | None:
    """A linux-x64 .NET 8 runtime folder (dotnet, host/fxr, shared/Microsoft.NETCore.App/8.x) for the map
    baker on Linux server hosts: bundled with the Windows app (tools/baker/dotnet-linux-x64), else the
    one Settings > Tools downloaded into the workspace; on Linux the system's own will do."""
    cands = [configured, bundled("tools", "baker", "dotnet-linux-x64"),
             linux_dotnet_download_dir(workspace) if workspace else None, os.environ.get("F2O77_LINUX_DOTNET")]
    if not IS_WINDOWS:
        cands.append(os.environ.get("DOTNET_ROOT"))
        d = shutil.which("dotnet")
        if d:
            cands.append(os.path.dirname(os.path.realpath(d)))
        cands += ["/opt/dotnet", "/usr/share/dotnet", "/usr/lib/dotnet"]
    for c in cands:
        if c and _is_linux_dotnet_root(c):
            return os.path.abspath(c)
    return None


def find_7z(configured: str = "") -> str | None:
    cands = [configured, bundled("tools", "7zip", "7z" + EXE)]
    if IS_WINDOWS:
        for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            if pf:
                cands.append(os.path.join(pf, "7-Zip", "7z.exe"))
    for name in ("7z", "7zz", "7za"):
        cands.append(shutil.which(name))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


# ----------------------------------------------------------------------------- donor cache
def donor_status(cache_dir: str) -> dict:
    out = {}
    for name, prof in DONORS.items():
        req = [p for p in _paths(prof, optional=False)]
        have = [p for p in req if os.path.isfile(os.path.join(cache_dir, "json", p.replace("\\", os.sep) + ".json"))]
        out[name] = {"label": prof["label"], "ready": len(have) == len(req), "files": len(have), "needed": len(req)}
    return out


class Toolbox:
    """Resolved tool paths for the current settings; apply() exports them for the converter."""

    def __init__(self, settings: dict, workspace: str):
        p = settings.get("paths", {})
        self.dotnet = find_dotnet(p.get("dotnet", ""))
        self.wolvenkit = find_wolvenkit(p.get("wolvenkit", ""))
        self.sevenzip = find_7z(p.get("sevenzip", ""))
        self.linux_dotnet = find_linux_dotnet(p.get("linux_dotnet", ""), workspace)
        games = find_game_dirs() if not _valid_game(p.get("game_dir")) else [(p["game_dir"], "settings")]
        self.games = games
        self.game_dir = games[0][0] if games else None
        self.cache_dir = p.get("cache_dir") or os.path.join(workspace, "donor-cache")

    def apply(self):
        if self.sevenzip:
            d = os.path.dirname(self.sevenzip)
            if d not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            os.environ["FIVEM2OPEN77_7Z"] = self.sevenzip
        if self.dotnet:
            os.environ["DOTNET_ROOT"] = os.path.dirname(self.dotnet)
            os.environ["DOTNET"] = self.dotnet
            os.environ.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        if self.wolvenkit:
            os.environ["WOLVENKIT_CLI"] = self.wolvenkit

    def wolvenkit_obj(self, log=print):
        from ..cp77.wolvenkit import WolvenKit
        self.apply()
        return WolvenKit(self.wolvenkit, dotnet=self.dotnet, log=log)

    def status(self) -> dict:
        donors = donor_status(self.cache_dir)
        needs_dotnet = bool(self.wolvenkit and self.wolvenkit.lower().endswith(".dll"))
        items = {
            "game": {"ok": bool(self.game_dir), "path": self.game_dir, "found": [{"path": g, "where": w} for g, w in self.games],
                     "help": "Your Cyberpunk 2077 folder (the donor car's files are read from it once)."},
            "wolvenkit": {"ok": bool(self.wolvenkit), "path": self.wolvenkit,
                          "help": "WolvenKit CLI (WolvenKit.Console) writes the game files."},
            "dotnet": {"ok": bool(self.dotnet) or not needs_dotnet, "path": self.dotnet, "required": needs_dotnet,
                       "help": ".NET 8 runtime that runs WolvenKit.CLI.dll."},
            "sevenzip": {"ok": bool(self.sevenzip), "path": self.sevenzip,
                         "help": "7-Zip opens .rar/.7z downloads (zip and folders work without it)."},
            "donor": {"ok": all(d["ready"] for d in donors.values()), "path": self.cache_dir, "donors": donors,
                      "help": "Donor files extracted from your game (one time, about a minute)."},
            "linux_dotnet": {"ok": bool(self.linux_dotnet), "path": self.linux_dotnet, "optional": True,
                             "help": ".NET 8 for Linux: the map baker's runtime, installed on your server host so "
                                     "maps moved in game can be rebuilt there.",
                             "download": {"version": linuxdotnet.VERSION, "bytes": linuxdotnet.SIZE,
                                          "url": linuxdotnet.URL}},
        }
        items["ready"] = all(items[k]["ok"] for k in ("wolvenkit", "dotnet")) and (items["donor"]["ok"] or items["game"]["ok"])
        items["app_home"] = app_home()
        return items
