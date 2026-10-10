"""fivem2open77 command line.

  python -m fivem2open77 convert <fivem resource folder|zip|rar|7z> --out <dir> [options]
  python -m fivem2open77 inspect <fivem resource> [--profile p.json]
  python -m fivem2open77 donor --game <Cyberpunk 2077 folder> [--cache <dir>] [--donor porsche_911]
  python -m fivem2open77 profile [--check p.json]      default profile / validate one
  python -m fivem2open77 sounds                         Cyberpunk engine sounds a car can use
  python -m fivem2open77 studio [--port N]              the converter app (web UI + API)
  python -m fivem2open77 mcp                            MCP server (stdio) for agents, relaying to the Studio
  python -m fivem2open77 mlo <fivem map/MLO> --out <dir> [--place x,y,z[,yaw]]   baked map + placer
  python -m fivem2open77 mlo-inspect | mlo-bake | mlo-install | mlo-bundle | mlo-manager-bundle (see mlo/cli.py)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import pipeline
from . import profile as profile_mod
from .cp77 import sound as sound_mod
from .cp77.donor import DEFAULT_DONOR, DONORS, DonorCache
from .cp77.wolvenkit import WolvenKit, WolvenKitError
from .gta.rsc7 import EscrowProtectedError, ResourceError


def _default_cache():
    base = os.environ.get("FIVEM2OPEN77_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "fivem2open77")
    return os.path.join(base, "donor")


def _game_dir(arg):
    g = arg or os.environ.get("CP77_GAME_DIR") or os.environ.get("OP77_GAME_DIR")
    if g and not os.path.isdir(os.path.join(g, "archive", "pc", "content")):
        raise SystemExit(f"error: {g} does not look like a Cyberpunk 2077 install (no archive/pc/content)")
    return g


def _profile(a) -> dict:
    base = profile_mod.load(a.profile) if getattr(a, "profile", None) else {}
    p = profile_mod.merge(base, profile_mod.from_cli(a))
    p = profile_mod.normalize(p, donors=DONORS, sounds=sound_mod.CATALOGUE)
    if p["chime"]["enabled"] and not os.path.isfile(p["chime"]["file"]):
        raise SystemExit(f"error: door chime file not found: {p['chime']['file']}")
    return p


def cmd_convert(a):
    prof = _profile(a)
    wk = WolvenKit(a.wolvenkit, verbose=a.verbose)
    cache = DonorCache(wk, _game_dir(a.game), a.cache)
    conv = pipeline.Converter(wk, cache, a.out, profile=prof, keep_work=a.keep_work, version=a.resource_version)
    results = conv.convert(a.resource)
    if not results:
        raise SystemExit("error: nothing converted (check --model)")
    for r in results:
        rep = r["report"]
        print(f"\n{r['name']}: {', '.join(r['resources'])}")
        print(f"  record   {r['record']}   (Open77 freeroam: /car {r['record']})")
        for p in rep["packages"]:
            print(f"  package  dist/{p['file']}  {p['bytes'] / 1048576:.1f} MiB")
        print("  resources.load: " + ", ".join(f'"{n}"' for n in r["load"]))
        for w in rep["warnings"]:
            print(f"  warning  {w}")
    return 0


def cmd_inspect(a):
    from .inspect import inspect_resource
    prof = profile_mod.normalize(profile_mod.load(a.profile) if a.profile else {}, donors=DONORS,
                                 sounds=sound_mod.CATALOGUE)
    print(json.dumps(inspect_resource(a.resource, prof), indent=2))
    return 0


def cmd_profile(a):
    if a.check:
        try:
            p = profile_mod.normalize(profile_mod.load(a.check), donors=DONORS, sounds=sound_mod.CATALOGUE)
        except profile_mod.ProfileError as e:
            for err in e.errors:
                print(f"  {err}", file=sys.stderr)
            return 2
        print(json.dumps(p, indent=2))
        return 0
    print(json.dumps(profile_mod.default_profile(), indent=2))
    return 0


def cmd_sounds(a):
    for s in sound_mod.choices():
        print(f"{s['name']:42s} {s['kind']:9s} {s['label']}")
    return 0


def cmd_mcp(a):
    from .studio.bridge import main as bridge_main
    return bridge_main(a)


def cmd_studio(a):
    from .studio.server import main as studio_main
    return studio_main(a)


def cmd_donor(a):
    wk = WolvenKit(a.wolvenkit, verbose=a.verbose)
    game = _game_dir(a.game)
    if not game:
        raise SystemExit("error: --game is required")
    cache = DonorCache(wk, game, a.cache)
    for name in (a.donor or list(DONORS)):
        cache.ensure(name)
        print(f"donor {name}: cached in {a.cache}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="fivem2open77", description="Convert FiveM vehicles into Open77 (Cyberpunk 2077 multiplayer) resources.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--wolvenkit", help="WolvenKit.CLI(.exe|.dll) path (or WOLVENKIT_CLI)")
        sp.add_argument("--cache", default=_default_cache(), help="donor cache folder (default: %(default)s)")
        sp.add_argument("--game", help="Cyberpunk 2077 install folder (needed once, to extract donor files)")
        sp.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("convert", help="convert a FiveM vehicle resource")
    c.add_argument("resource", help="FiveM resource folder or archive (.zip/.rar/.7z)")
    c.add_argument("--out", required=True, help="output folder (one Open77 resource per vehicle)")
    c.add_argument("--name", help="record/resource id (default: the model name)")
    c.add_argument("--display-name", help="name shown in game")
    c.add_argument("--model", help="only this model when the resource has several")
    c.add_argument("--donor", choices=sorted(DONORS), help=f"Cyberpunk donor vehicle (default: by class, {DEFAULT_DONOR})")
    c.add_argument("--door-chime", help="mp3/wav played while a cabin door is open")
    c.add_argument("--chime-volume", type=float, help="0..1 (default 0.8)")
    c.add_argument("--chime-range", type=float, help="metres (default 12)")
    c.add_argument("--keep-work", action="store_true", help="keep intermediate JSON/glb files")
    c.add_argument("--profile", help="conversion profile (JSON, see `profile`); flags given here override it")
    c.add_argument("--engine-sound", help="auto (from GTA's sound), donor, or a name from `sounds`")
    c.add_argument("--lids", choices=("auto", "semantic", "swap"), help="hood/trunk mapping (default auto)")
    c.add_argument("--resource-version", help="version of the Open77 resources (default 1.0.0); bump it per build so "
                                              "servers and the car hub can tell builds apart")
    common(c)
    c.set_defaults(fn=cmd_convert)

    i = sub.add_parser("inspect", help="print what the converter sees in a FiveM resource")
    i.add_argument("resource")
    i.add_argument("--profile")
    i.set_defaults(fn=cmd_inspect)

    pr = sub.add_parser("profile", help="print the default conversion profile, or check one")
    pr.add_argument("--check", metavar="FILE")
    pr.set_defaults(fn=cmd_profile)

    so = sub.add_parser("sounds", help="list the Cyberpunk engine sounds a car can use")
    so.set_defaults(fn=cmd_sounds)

    st = sub.add_parser("studio", help="run the converter app (local web UI + API)")
    st.add_argument("--host", default="127.0.0.1")
    st.add_argument("--port", type=int, default=0, help="0 = any free port")
    st.add_argument("--workspace", help="projects/settings folder")
    st.add_argument("--token", help="API token (default: random, printed)")
    st.add_argument("--ready-file", help="write {port, token, url} here once listening")
    st.add_argument("--open", action="store_true", help="open the UI in the default browser")
    st.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                    help="stop after this long with no agent call, open page or job (a Studio started for agents)")
    st.set_defaults(fn=cmd_studio)

    mc = sub.add_parser("mcp", help="MCP server (stdio) for agents: relays to the running Studio, starting it if needed")
    mc.add_argument("--workspace", help="the Studio's projects/settings folder (default: the usual one)")
    mc.add_argument("--attach-only", action="store_true", help="never start the Studio: fail when it is not running")
    mc.add_argument("--headless", action="store_true", help="start the Studio without its window (stops when idle)")
    mc.add_argument("--log", help="write the bridge's log here (default: <workspace>/logs/mcp-bridge.log)")
    mc.set_defaults(fn=cmd_mcp)

    d = sub.add_parser("donor", help="extract and cache donor files from the game")
    d.add_argument("--donor", action="append", choices=sorted(DONORS))
    common(d)
    d.set_defaults(fn=cmd_donor)

    from .mlo import cli as mlo_cli
    mlo_cli.add_parsers(sub, common, _game_dir, DonorCache)

    from .argv import glue_vectors
    a = p.parse_args(glue_vectors(argv))
    try:
        return a.fn(a)
    except EscrowProtectedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except profile_mod.ProfileError as e:
        print("error: invalid profile:", file=sys.stderr)
        for err in e.errors:
            print(f"  {err}", file=sys.stderr)
        return 2
    except (ResourceError, WolvenKitError, RuntimeError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        if getattr(a, "json", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
