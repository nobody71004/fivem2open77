"""Where things live.

  app home (F2O77_HOME)   the installed app: runtime/python, runtime/dotnet, tools/wolvenkit,
                          tools/7zip, app/fivem2open77 (this code).  Unset when run from source.
  workspace               projects, settings, logs, donor cache.  Default:
                          Windows %LOCALAPPDATA%\\fivem2open77\\studio, else ~/.local/share/fivem2open77/studio
"""
from __future__ import annotations

import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../fivem2open77
SOURCE_ROOT = os.path.dirname(PACKAGE_DIR)                                   # repo root or <home>/app
IS_WINDOWS = os.name == "nt"


def app_home() -> str | None:
    h = os.environ.get("F2O77_HOME")
    if h and os.path.isdir(h):
        return os.path.abspath(h)
    # packaged layout: <home>/app/fivem2open77
    cand = os.path.dirname(SOURCE_ROOT)
    if os.path.isdir(os.path.join(cand, "runtime")) and os.path.basename(SOURCE_ROOT) == "app":
        return cand
    return None


def default_workspace() -> str:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "fivem2open77", "studio")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "fivem2open77", "studio")


def web_dir() -> str:
    return os.path.join(PACKAGE_DIR, "studio", "web")


def hub_dir() -> str:
    return os.path.join(PACKAGE_DIR, "hub", "f2o77_hub")


def deploy_script() -> str:
    """server_deploy.sh: in the source tree under tools/deploy, in the app under tools/deploy too."""
    for root in filter(None, (app_home(), SOURCE_ROOT)):
        p = os.path.join(root, "tools", "deploy", "server_deploy.sh")
        if os.path.isfile(p):
            return p
    p = os.path.join(PACKAGE_DIR, "data", "server_deploy.sh")
    return p


def remote_script(name: str) -> str:
    """A script of tools/deploy/remote (map_deploy.sh, baker_install.sh) in the source tree or the app."""
    for root in filter(None, (app_home(), SOURCE_ROOT)):
        p = os.path.join(root, "tools", "deploy", "remote", name)
        if os.path.isfile(p):
            return p
    return os.path.join(PACKAGE_DIR, "data", name)


def bundled(*parts) -> str | None:
    h = app_home()
    if not h:
        return None
    p = os.path.join(h, *parts)
    return p if os.path.exists(p) else None


def python_exe() -> str:
    return sys.executable
