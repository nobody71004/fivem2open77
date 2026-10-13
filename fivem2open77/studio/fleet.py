"""Open77 version drift across the Studio's servers, and the plan for a fleet upgrade.

Pure functions only: the SSH work lives in deploy.py (releases, status, upgrade).  A server folder's
installed version is the "version" of its package-manifest.json (server_deploy.sh status).  Its
target is the newest release staged on its host under RELEASE_ROOT, the same tree vps_upgrade.sh
installs from.  vps_upgrade.sh hard-codes /opt and system units, so only those servers are planned.
"""
from __future__ import annotations

import json
import re

RELEASE_ROOT = "/opt/open77-releases"            # the host's release tree (vps_upgrade.sh uses it too)
SERVERS_ROOT = "/opt"                            # the folder vps_upgrade.sh looks for each server in
VERSION_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,63}$")
_BUILD = re.compile(r"op77\.(\d+)")


def build_number(version) -> int:
    """2.31.21+op77.132 -> 132 (-1 when the version carries no build number)."""
    m = _BUILD.search(version or "")
    return int(m.group(1)) if m else -1


def newest(versions) -> str | None:
    """The newest of the given release versions by build number (None when there are none)."""
    vs = [v for v in versions or [] if isinstance(v, str) and VERSION_RX.match(v)]
    if not vs:
        return None
    return max(vs, key=lambda v: (build_number(v), v))


def parse_releases(text) -> list | None:
    """The JSON list the host check prints (staged release versions); None when it did not answer."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, list):
                return [x for x in found if isinstance(x, str)]
    return None


def drift(installed, target) -> str:
    """current: the target | behind: an older build than the target | differs: not older (a different
    or newer build, left alone) | unknown: no version read or nothing staged on the host."""
    if not installed or not target:
        return "unknown"
    if installed == target:
        return "current"
    if build_number(installed) < build_number(target):
        return "behind"
    return "differs"


def server_blockers(server: dict) -> str | None:
    """Why vps_upgrade.sh cannot run for this server entry at all (None when it can)."""
    if server.get("servers_root") != SERVERS_ROOT:
        return f"its folders are not under {SERVERS_ROOT}, the only place the host's upgrade tool looks"
    if server.get("systemctl") != "systemctl":
        return "its units are user units; the host's upgrade tool drives system units"
    return None


def describe(server: dict, rows, target) -> dict:
    """What the Studio shows for one server entry: each folder's version, drift and players online.
    rows are server_deploy.sh status rows (one per server folder)."""
    by = {r.get("name"): r for r in rows or []}
    folders = []
    for name in server.get("names") or []:
        r = by.get(name) or {}
        installed = r.get("version")
        folders.append({"name": name, "version": installed, "drift": drift(installed, target),
                        "online": len(r.get("online") or []) if r else None})
    return {"target": target, "blocked": server_blockers(server), "folders": folders}


def plan(server: dict, rows, target) -> tuple:
    """(names to upgrade, skipped) for one server entry.  skipped is [{"name", "reason"}].
    A folder is upgraded only when it is behind the target and nobody is connected to it."""
    why = server_blockers(server)
    if why:
        return [], [{"name": n, "reason": why} for n in server.get("names") or []]
    by = {r.get("name"): r for r in rows or []}
    names, skipped = [], []
    for name in server.get("names") or []:
        r = by.get(name)
        if r is None:
            skipped.append({"name": name, "reason": "no status for this folder"})
            continue
        d = drift(r.get("version"), target)
        online = len(r.get("online") or [])
        if d == "current":
            skipped.append({"name": name, "reason": "already on " + target})
        elif d == "unknown":
            skipped.append({"name": name, "reason": "version unknown (nothing staged, or no manifest)"})
        elif d == "differs":
            skipped.append({"name": name, "reason": f"runs {r.get('version')}, not older than {target}"})
        elif online:
            skipped.append({"name": name, "reason": f"{online} player(s) online: it waits for an empty server"})
        else:
            names.append(name)
    return names, skipped
