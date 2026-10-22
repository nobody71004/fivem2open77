"""Thin wrapper around WolvenKit's command-line tool (WolvenKit.CLI / cp77tools).

Only managed-code commands are used (convert serialize/deserialize, import of glb, pack,
unbundle), so this works with WolvenKit.CLI on Windows and on Linux (.NET 8 runtime).
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading

# no console window per WolvenKit call when the app runs without a console (Windows)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WolvenKitError(Exception):
    pass


def _candidates():
    env = os.environ.get("WOLVENKIT_CLI")
    if env:
        yield env
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (os.getcwd(), os.path.join(here, "..", ".."), os.path.expanduser("~")):
        for pat in ("WolvenKit.Console*/WolvenKit.CLI.exe", "WolvenKit.Console*/WolvenKit.CLI.dll",
                    "tools/WolvenKit*/WolvenKit.CLI.dll", "tools/wkcli/WolvenKit.CLI.dll",
                    "WolvenKit*/WolvenKit.CLI.exe", "WolvenKit*/WolvenKit.CLI.dll"):
            yield from glob.glob(os.path.join(root, pat))
    for exe in ("WolvenKit.CLI", "cp77tools"):
        p = shutil.which(exe)
        if p:
            yield p


class WolvenKit:
    def __init__(self, cli: str | None = None, dotnet: str | None = None, log=print, verbose=False):
        self.log = log
        self.verbose = verbose
        path = cli
        if not path:
            for c in _candidates():
                if os.path.exists(c):
                    path = c
                    break
        if not path or not os.path.exists(path):
            raise WolvenKitError(
                "WolvenKit CLI not found. Download 'WolvenKit.Console' from "
                "https://github.com/WolvenKit/WolvenKit/releases (same version as your WolvenKit) and pass "
                "--wolvenkit <path to WolvenKit.CLI.exe or .dll> or set WOLVENKIT_CLI.")
        self.path = os.path.abspath(path)
        self._proc = None
        self._lock = threading.Lock()
        self.cancelled = False
        if self.path.lower().endswith(".dll"):
            dn = dotnet or os.environ.get("DOTNET") or shutil.which("dotnet")
            if not dn:
                root = os.environ.get("DOTNET_ROOT")
                if root and os.path.exists(os.path.join(root, "dotnet")):
                    dn = os.path.join(root, "dotnet")
            if not dn:
                raise WolvenKitError("a .NET 8 runtime ('dotnet') is required to run WolvenKit.CLI.dll")
            self.cmd = [dn, self.path]
        else:
            self.cmd = [self.path]

    def kill(self):
        """Stop the running WolvenKit call (the Studio's cancel button)."""
        with self._lock:
            self.cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def run(self, args, env=None, timeout=1800, check=True):
        e = dict(os.environ)
        if env:
            e.update(env)
        if os.path.basename(self.cmd[0]).lower() in ("dotnet", "dotnet.exe"):
            e["DOTNET_ROOT"] = os.path.dirname(self.cmd[0])
        e.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        with self._lock:
            if self.cancelled:
                raise WolvenKitError("cancelled")
            self._proc = subprocess.Popen(self.cmd + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, env=e, cwd=os.path.dirname(self.path),
                                          creationflags=NO_WINDOW)
        proc = self._proc
        try:
            raw, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise WolvenKitError(f"WolvenKit {' '.join(args[:2])} did not finish in {timeout} s")
        finally:
            with self._lock:
                self._proc = None
        if self.cancelled:
            raise WolvenKitError("cancelled")
        out = raw.decode("utf-8", "replace")
        if self.verbose:
            self.log(out)
        errors = [ln for ln in out.splitlines() if "Error" in ln[:40] or "Unhandled exception" in ln]
        if check and (proc.returncode != 0 or errors):
            tail = "\n".join((errors or out.splitlines())[-15:])
            raise WolvenKitError(f"WolvenKit {' '.join(args[:2])} failed (exit {proc.returncode}):\n{tail}")
        return out

    # -- commands -------------------------------------------------------------
    def deserialize(self, json_dir, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        return self.run(["convert", "deserialize", json_dir, "-o", out_dir])

    def deserialize_tree(self, json_root, out_root, scratch):
        """`convert deserialize` flattens folders, so each folder of the depot tree is converted on
        its own (through a flat scratch copy) into the matching output folder."""
        groups = {}
        for dp, _, fns in os.walk(json_root):
            js = [f for f in fns if f.endswith(".json")]
            if js:
                groups[os.path.relpath(dp, json_root)] = [os.path.join(dp, f) for f in js]
        for rel, files in sorted(groups.items()):
            tmp = os.path.join(scratch, "deser_" + str(abs(hash(rel))))
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            for f in files:
                shutil.copyfile(f, os.path.join(tmp, os.path.basename(f)))
            out = os.path.normpath(os.path.join(out_root, rel))
            self.deserialize(tmp, out)
            shutil.rmtree(tmp, ignore_errors=True)
            for f in files:
                produced = os.path.join(out, os.path.basename(f)[:-5])
                if not os.path.exists(produced):
                    raise WolvenKitError(f"WolvenKit did not write {produced}")

    def serialize(self, cr2w_path, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        return self.run(["convert", "serialize", cr2w_path, "-o", out_dir])

    def import_glb(self, raw_dir, archive_dir):
        """Import every glb below raw_dir over the .mesh with the same relative path in archive_dir."""
        env = {
            "GltfImportArgs__ImportGarmentSupport": "false",
            "GltfImportArgs__ImportMaterials": "false",
            "GltfImportArgs__FillEmpty": "false",
            "GltfImportArgs__OverrideMeshNameWithNodeName": "true",
        }
        out = self.run(["import", raw_dir, "-o", archive_dir, "-k"], env=env, check=False)
        errors = [ln for ln in out.splitlines() if "Error" in ln[:40] or "Unhandled exception" in ln]
        m = re.findall(r"Imported (\d+)/(\d+) file", out)
        if errors or not m or m[-1][0] != m[-1][1]:
            tail = "\n".join((errors or out.splitlines())[-15:])
            raise WolvenKitError(f"WolvenKit import failed:\n{tail}")
        return out

    def pack(self, archive_dir, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        return self.run(["pack", archive_dir, "-o", out_dir])

    def export_meshes(self, archives, out_dir, regex, game_dir=None):
        """The game's meshes matching ``regex`` as glTF (geometry only: ``<mesh>.glb``, its
        ``<mesh>.Material.json`` and the material repository: multilayer setups and templates)."""
        os.makedirs(out_dir, exist_ok=True)
        args = ["uncook"] + list(archives) + ["-o", out_dir, "-r", regex, "--mesh-export-type", "MeshOnly"]
        if game_dir:
            args += ["-gp", game_dir]
        # a mesh WolvenKit cannot export (an unusual material) must not stop the rest
        return self.run(args, timeout=3600, check=False)

    def unbundle(self, archives, out_dir, regex):
        os.makedirs(out_dir, exist_ok=True)
        args = ["unbundle"] + list(archives) + ["-o", out_dir, "-r", regex]
        return self.run(args, timeout=3600)

    def version(self):
        out = self.run(["--version"], check=False)
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        return m.group(1) if m else out.strip()
