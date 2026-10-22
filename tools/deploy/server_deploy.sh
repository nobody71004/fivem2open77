#!/usr/bin/env bash
# fivem2open77: install converted cars (and the ArchiveXL/TweakXL loader resources) into Open77
# servers on this host.  Runs ON the server host; Deploy-Car.ps1 / deploy_car.sh copy it there.
#
#   server_deploy.sh inspect  BUNDLE.tgz NAME [NAME...]
#   server_deploy.sh deploy   BUNDLE.tgz NAME [NAME...] [--restart | --stage] [--allow-unsecured]
#   server_deploy.sh status   -          NAME [NAME...] [--json]
#   server_deploy.sh logs     -          NAME [LINES]
#   server_deploy.sh restart  -          NAME [NAME...]
#   server_deploy.sh agent-api -         NAME [NAME...] [--off] [--restart]
#   server_deploy.sh acl-grant PERMISSION NAME [NAME...] [--restart]
#   server_deploy.sh manager-update UPDATE.tgz NAME [NAME...]
#   server_deploy.sh remove   RESOURCE   NAME [NAME...] [--restart]    (a world edit)
#
# NAME is a server directory under $SERVERS_ROOT (default /opt) whose systemd unit is NAME.service,
# e.g. open77-xbuniverse.  BUNDLE.tgz holds resources/assets/{loaders,vehicles}/<resource>/...
#
# deploy, per server:
#   * refuses a server whose Open77 build cannot run the bundle (no network package audio);
#   * backs up server.jsonc and every resource directory it replaces (/opt/open77-backups/...);
#   * installs the bundle's resources (loaders only when missing or older), adds them to
#     resources.load (text edit: comments and layout of server.jsonc are kept);
#   * requiredMods must be enabled and, because the loaders are executable, unsecured: an
#     unsecured=false server is left untouched unless --allow-unsecured;
#   * validates with Open77.Server --check-config (restores the backup on failure);
#   * with --restart and nobody online: restarts the unit (SIGINT stop) and checks the log for the
#     listener, the declared required mods and every bundle resource starting.  Without --restart,
#     or with players online, it says what is pending.  The car's Lua hot-reloads by itself; its
#     PACKAGE (the archive players install) is only declared again at a server start.
#   * with --stage: no restart here; when the f2o77_hub resource runs on the server, the car is
#     written to the hub's data/pending.json and the hub restarts the server when nobody is online
#     (or at its daily time).  A rebuild whose packages did not change goes live at once (Lua).
#   * a resource's data/ folder (the hub's state and settings) is kept across updates.
#   * the map manager (f2o77_maps) is shared by every converted map: its code is replaced when the
#     bundle's version is the same or newer, the bundle's shared/maps/<map>.lua files are added or
#     replaced, the other maps' files and its data/ stay.
# status prints what is installed (cars, maps, hub, manager, map baker, agent API, pending, players
# online); --json for the Studio.
# agent-api switches the server's HTTP handler listener (httpHandlers, loopback 127.0.0.1:11781)
# on (or --off): the map manager's agent API, which the Studio reaches through its SSH connection.
# server.jsonc is backed up and checked like a deploy; it takes effect at the next server start
# (--restart: now, when nobody is online).
# acl-grant adds an ACL right (e.g. command.maps, which the in-game map manager's F9 needs) to a
# principal of the server's ACL file (server.jsonc accessControl.file): the one whose name starts
# with $F2O77_ACL_PRINCIPAL, or the only one.  Text edit (comments kept), backed up and checked like a
# deploy.  The server reads its ACL at start: --restart restarts it when nobody is online, also for a
# grant an earlier run could not restart for.  Prints no keys.
# manager-update (UPDATE.tgz from `mlo-manager-bundle`: resources/assets/tools/f2o77_maps/ and
# ghosts/<map>.txt) replaces the map manager's code when the update is the same version or newer
# (its shared/maps/ and data/ stay), and gives each map installed here its new wireframe: the map's
# ghost.txt (later bakes use it) and the ghost in the manager's shared/maps/<map>.lua and in a build
# staged for the next restart, keeping where the map is baked.  Backed up first; the map baker's lock
# is held while a wireframe changes.  Scripts and the page reload by themselves: no restart.
# Prints no secrets.
set -euo pipefail
MODE=${1:?mode}; BUNDLE=${2:?bundle}; shift 2
# a Windows bundle path (C:\..., handed over by Git Bash) would make tar look for a remote host
# named "C": convert it to a POSIX path where cygpath is around
BUNDLE=$(cygpath -u "$BUNDLE" 2>/dev/null || echo "$BUNDLE")
SERVERS_ROOT=${SERVERS_ROOT:-/opt}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
RESTART=0; STAGE=0; ALLOW_UNSECURED=0; JSON=0; HTTP_OFF=0; NAMES=()
for a in "$@"; do
  case "$a" in
    --restart) RESTART=1 ;;
    --stage) STAGE=1 ;;
    --allow-unsecured) ALLOW_UNSECURED=1 ;;
    --json) JSON=1 ;;
    --off) HTTP_OFF=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) NAMES+=("$a") ;;
  esac
done
[ ${#NAMES[@]} -gt 0 ] || { echo "no server names given" >&2; exit 2; }
for nm in "${NAMES[@]}"; do
  [[ "$nm" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "bad server name: $nm" >&2; exit 2; }
done
PERM=""
if [ "$MODE" = acl-grant ]; then
  PERM=$BUNDLE
  [[ "$PERM" =~ ^[a-z0-9][a-z0-9._*-]{0,127}$ ]] || { echo "bad permission: $PERM" >&2; exit 2; }
fi
[ $RESTART = 1 ] && [ $STAGE = 1 ] && { echo "--restart and --stage exclude each other" >&2; exit 2; }
say() { echo "[$(date -u +%H:%M:%S)] $*"; }
TS=$(date -u +%Y%m%d-%H%M%S)
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
BRES=()
if [ "$MODE" = inspect ] || [ "$MODE" = deploy ]; then
  tar -xzf "$BUNDLE" -C "$WORK"
  mapfile -t BRES < <(cd "$WORK" && find resources -mindepth 3 -maxdepth 3 -type d | sort)   # resources/assets/<cat>/<name>
  [ ${#BRES[@]} -gt 0 ] || { echo "bundle has no resources/assets/<category>/<resource> folders" >&2; exit 2; }
fi
if [ "$MODE" = manager-update ]; then
  tar -xzf "$BUNDLE" -C "$WORK" --no-same-owner
  [ -f "$WORK/resources/assets/tools/f2o77_maps/open77.lua" ] || ls "$WORK"/ghosts/*.txt >/dev/null 2>&1 \
    || { echo "the update has neither resources/assets/tools/f2o77_maps nor ghosts/<map>.txt" >&2; exit 2; }
fi

# ------------------------------------------------------------------ helpers (python, stdlib)
cat > "$WORK/cfg.py" <<'PY'
import json, re, sys

def strip_jsonc(t):
    out, i, n, s = [], 0, len(t), False
    while i < n:
        c = t[i]
        if s:
            out.append(c)
            if c == "\\": out.append(t[i + 1]); i += 1
            elif c == '"': s = False
        elif c == '"': s = True; out.append(c)
        elif t.startswith("//", i):
            while i < n and t[i] != "\n": i += 1
            continue
        elif t.startswith("/*", i): i = t.index("*/", i) + 2; continue
        else: out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))

def load(path):
    with open(path, encoding="utf-8") as f:
        return json.loads(strip_jsonc(f.read()))

def find_key(text, key, start=0, end=None):
    """Position just after `"key":` at nesting depth 1 of the object starting at `start`."""
    end = len(text) if end is None else end
    depth, i, s = 0, start, False
    while i < end:
        c = text[i]
        if s:
            if c == "\\": i += 2; continue
            if c == '"': s = False
            i += 1; continue
        if text.startswith("//", i):
            i = text.find("\n", i); i = end if i < 0 else i; continue
        if text.startswith("/*", i): i = text.index("*/", i) + 2; continue
        if c == '"':
            if depth == 1:
                m = re.compile(r'"%s"\s*:\s*' % re.escape(key)).match(text, i)
                if m: return m.end()
            s = True
        elif c in "{[": depth += 1
        elif c in "}]": depth -= 1
        i += 1
    return -1

def block_end(text, i):
    """Index of the bracket closing the {...} or [...] starting at i."""
    depth, s = 0, False
    while i < len(text):
        c = text[i]
        if s:
            if c == "\\": i += 2; continue
            if c == '"': s = False
        elif text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0: break
            i = j; continue
        elif text.startswith("/*", i): i = text.index("*/", i) + 2; continue
        elif c == '"': s = True
        elif c in "{[": depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0: return i
        i += 1
    raise ValueError("unbalanced brackets")

def remove_from_load(text, names):
    """Take names out of resources.load (text edit: the rest of the file stays as it is)."""
    root = text.index("{")
    r = find_key(text, "resources", root)
    if r < 0: return text, []
    r_end = block_end(text, r)
    l = find_key(text, "load", r, r_end)
    if l < 0: return text, []
    gone = []
    for n in names:
        l_end = block_end(text, l)
        want = json.dumps(n)
        i, s, start, found = l + 1, False, None, None
        while i < l_end:
            c = text[i]
            if s:
                if c == "\\": i += 2; continue
                if c == '"':
                    s = False
                    if text[start:i + 1] == want: found = (start, i + 1); break
            elif text.startswith("//", i):
                j = text.find("\n", i); i = l_end if j < 0 else j; continue
            elif text.startswith("/*", i): i = text.index("*/", i) + 2; continue
            elif c == '"': s, start = True, i
            i += 1
        if not found: continue
        a, b = found
        j = a - 1
        while j > l and text[j] in " \t\r\n": j -= 1
        if text[j] == ",":
            text = text[:j] + text[b:]
        else:
            k = b
            while k < l_end and text[k] in " \t\r\n": k += 1
            if text[k] == ",":
                k += 1
                while k < l_end and text[k] in " \t": k += 1
                text = text[:a] + text[k:]
            else:
                text = text[:a] + text[b:]
        gone.append(n)
    return text, gone


def add_to_load(text, names):
    root = text.index("{")
    r = find_key(text, "resources", root)
    if r < 0: raise ValueError("server.jsonc has no resources section")
    r_end = block_end(text, r)
    l = find_key(text, "load", r, r_end)
    if l < 0: raise ValueError("resources.load is absent (every resource folder is loaded): nothing to add")
    l_end = block_end(text, l)
    have = set(json.loads(strip_jsonc(text[l:l_end + 1])))
    new = [n for n in names if n not in have]
    if not new: return text, []
    # insert right after the last string of the array: comments, trailing commas, layout untouched
    i, s, last_end = l + 1, False, None
    while i < l_end:
        c = text[i]
        if s:
            if c == "\\": i += 2; continue
            if c == '"': s = False; last_end = i + 1
        elif text.startswith("//", i):
            j = text.find("\n", i); i = l_end if j < 0 else j; continue
        elif text.startswith("/*", i): i = text.index("*/", i) + 2; continue
        elif c == '"': s = True
        i += 1
    pads = re.findall(r"\n([ \t]+)\"", text[l:l_end])
    pad = pads[-1] if pads else " " * 6
    if last_end is None:
        line = text.rfind("\n", 0, l) + 1
        outer = re.match(r"[ \t]*", text[line:]).group(0)
        ins = ",".join("\n" + outer + "  " + json.dumps(n) for n in new) + "\n" + outer
        return text[:l + 1] + ins + text[l_end:], new
    ins = "".join(",\n" + pad + json.dumps(n) for n in new)
    return text[:last_end] + ins + text[last_end:], new

def set_bool(text, section, key, value):
    root = text.index("{")
    s = find_key(text, section, root)
    if s < 0: raise ValueError(f"no {section} section")
    s_end = block_end(text, s)
    k = find_key(text, key, s, s_end)
    lit = "true" if value else "false"
    if k >= 0:
        m = re.compile(r"true|false").match(text, k)
        return text[:k] + lit + text[m.end():]
    body_start = s + 1
    return text[:body_start] + f'\n    "{key}": {lit},' + text[body_start:]

def set_http(text, on, listen=None):
    """httpHandlers.enabled (and listenUrl when absent or given); the section is added when missing."""
    root = text.index("{")
    s = find_key(text, "httpHandlers", root)
    url = listen or "http://127.0.0.1:11781"
    if s < 0:
        sec = '\n  "httpHandlers": { "enabled": %s, "listenUrl": %s },' % ("true" if on else "false", json.dumps(url))
        return text[:root + 1] + sec + text[root + 1:]
    text = set_bool(text, "httpHandlers", "enabled", on)
    s = find_key(text, "httpHandlers", root)
    s_end = block_end(text, s)
    k = find_key(text, "listenUrl", s, s_end)
    if k >= 0:
        if listen:
            m = re.compile(r'"(?:[^"\\]|\\.)*"').match(text, k)
            text = text[:k] + json.dumps(listen) + text[m.end():]
        return text
    return text[:s + 1] + '\n    "listenUrl": %s,' % json.dumps(url) + text[s + 1:]

def acl_covers(perm, have):
    """Whether a granted string covers perm: exact, "*", or a trailing wildcard ("command.*")."""
    return have == "*" or have == perm or (have.endswith(".*") and perm.startswith(have[:-1]))

def acl_allowed_by(acl, principal, perm):
    """What already gives the principal perm (a permission, a role), or None."""
    for h in principal.get("permissions") or []:
        if isinstance(h, str) and acl_covers(perm, h):
            return "permission " + h
    roles = {r.get("name"): r for r in acl.get("roles") or [] if isinstance(r, dict)}
    for name in principal.get("roles") or []:
        r = roles.get(name)
        if r is None:
            if name == "owner" or (name == "admin" and perm.startswith("command.")):
                return "built-in role " + name
            continue
        for h in r.get("permissions") or []:
            if isinstance(h, str) and acl_covers(perm, h):
                return "role %s (%s)" % (name, h)
    return None

def principal_spans(text):
    """(start, end) of each object in the root "principals" array, in order."""
    root = text.index("{")
    p = find_key(text, "principals", root)
    if p < 0:
        return []
    if text[p] != "[":
        raise ValueError("principals is not a list")
    end = block_end(text, p)
    spans, i, s = [], p + 1, False
    while i < end:
        c = text[i]
        if s:
            if c == "\\": i += 2; continue
            if c == '"': s = False
            i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = end if j < 0 else j; continue
        if text.startswith("/*", i): i = text.index("*/", i) + 2; continue
        if c == '"': s = True
        elif c == "{":
            e = block_end(text, i); spans.append((i, e)); i = e + 1; continue
        i += 1
    return spans

def acl_grant(text, perm, prefix=""):
    """perm added to the principal(s) whose name starts with prefix (any case); without a match, to the
    only principal.  Returns (text, changed names, {name: what already allowed it}, chosen names)."""
    acl = json.loads(strip_jsonc(text))
    prs = [p for p in acl.get("principals") or []]
    idx = [i for i, p in enumerate(prs) if prefix and str(p.get("name", "")).lower().startswith(prefix.lower())]
    if not idx and len(prs) == 1:
        idx = [0]
    if not idx:
        raise LookupError("no principal named %s* (and not exactly one principal)" % prefix if prefix
                          else "several principals and no name given (F2O77_ACL_PRINCIPAL)")
    spans = principal_spans(text)
    if len(spans) != len(prs):
        raise ValueError("could not find every principal in the file")
    changed, already = [], {}
    for i in sorted(idx, reverse=True):          # from the end: earlier spans stay where they are
        p, (o0, o1) = prs[i], spans[i]
        via = acl_allowed_by(acl, p, perm)
        if via:
            already[p.get("name")] = via
            continue
        k = find_key(text, "permissions", o0, o1 + 1)
        if k >= 0:
            if text[k] != "[":
                raise ValueError("permissions of %s is not a list" % p.get("name"))
            a1 = block_end(text, k)
            if not strip_jsonc(text[k + 1:a1]).strip():
                text = text[:k + 1] + json.dumps(perm) + text[k + 1:]
            else:
                m = re.match(r"[ \t]*\r?\n([ \t]*)", text[k + 1:])
                ins = ("\n" + m.group(1) + json.dumps(perm) + ",") if m else (json.dumps(perm) + ", ")
                text = text[:k + 1] + ins + text[k + 1:]
        else:
            m = re.search(r"\n([ \t]*)\"", text[o0:o1])
            ins = ("\n" + m.group(1) if m else " ") + '"permissions": [%s],' % json.dumps(perm)
            text = text[:o0 + 1] + ins + text[o0 + 1:]
        changed.append(p.get("name"))
    after = json.loads(strip_jsonc(text))
    for i, (a, b) in enumerate(zip(prs, after.get("principals") or [])):
        a2 = dict(a, permissions=None); b2 = dict(b, permissions=None)
        if a2 != b2 or (a.get("name") not in changed and a.get("permissions") != b.get("permissions")):
            raise ValueError("the edit touched more than the permissions it was meant to")
        if a.get("name") in changed and perm not in (b.get("permissions") or []):
            raise ValueError("the edit did not land")
    if {k: v for k, v in after.items() if k != "principals"} != {k: v for k, v in acl.items() if k != "principals"}:
        raise ValueError("the edit touched more than the principals")
    return text, changed, already, [prs[i].get("name") for i in idx]

GHOST_COMMENT = ("-- wireframe: x1 y1 z1 x2 y2 z2 kind per line (4 outline at ground level, 5 gates, 0 box, "
                 "1 buildings, 2 walls, 3 ground)")

def ghost_check(text):
    """(lines, {kind: count}); ValueError unless every line is 'x1 y1 z1 x2 y2 z2 kind' (numbers only:
    the text goes into a Lua long string)."""
    n, kinds = 0, {}
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        p = line.split()
        ok = len(p) == 7
        if ok:
            try:
                v = [float(x) for x in p[:6]]
                k = int(p[6])
                ok = all(x == x and abs(x) < 1e6 for x in v) and 0 <= k <= 9
            except ValueError:
                ok = False
        if not ok:
            raise ValueError("line %d is not 'x1 y1 z1 x2 y2 z2 kind'" % i)
        n += 1
        kinds[k] = kinds.get(k, 0) + 1
    return n, kinds

def ghost_of_def(text, name):
    key = "F2O77_MAPS[%s].ghost = " % json.dumps(name)
    i = text.find(key)
    if i < 0:
        return None
    m = re.compile(r"\[(=*)\[\n?").match(text, i + len(key))
    if not m:
        return None
    end = text.find("]" + m.group(1) + "]", m.end())
    return text[m.end():end] if end >= 0 else None

def splice_ghost(path, name, ghost):
    """The map def at `path` with `ghost` as its wireframe; everything else (where it is baked, its
    bake id, bounds...) as it was.  False when it already had that wireframe."""
    import os
    text = open(path, encoding="utf-8").read()
    if not re.search(r"^F2O77_MAPS\[%s\] = \{" % re.escape(json.dumps(name)), text, re.M):
        raise ValueError("%s does not define the map %s" % (path, name))
    body = ghost.strip("\n")
    if (ghost_of_def(text, name) or "").strip("\n") == body:
        return False
    key = "F2O77_MAPS[%s].ghost = " % json.dumps(name)
    i = text.find(key)
    head = (text if i < 0 else text[:i]).rstrip("\n").split("\n")
    while head and head[-1].startswith("-- wireframe:"):
        head.pop()
    eq = ""
    while ("]" + eq + "]") in body:
        eq += "="
    out = "\n".join(head) + "\n" + GHOST_COMMENT + "\n" + key + "[" + eq + "[\n" + body + "]" + eq + "]\n"
    tmp = path + ".f2o77-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    os.replace(tmp, path)
    return True

def ghost_update(server_dir, manager, map_dir, name, ghost_file):
    """The new wireframe of one map: its ghost.txt, the manager's def and a staged build's def, under the
    map baker's lock (a bake reads ghost.txt and writes the staged def)."""
    import os, time, fcntl
    ghost = open(ghost_file, encoding="utf-8").read().strip("\n") + "\n"
    lines, kinds = ghost_check(ghost)
    fm = os.path.join(server_dir, "f2o77-maps")
    fd = None
    if os.path.isdir(fm):
        fd = os.open(os.path.join(fm, "baker.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.time() + 300
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("the map baker has been busy for 5 minutes (baker.lock): try again later")
                time.sleep(0.5)
    try:
        changed = []
        if map_dir:
            gp = os.path.join(map_dir, "ghost.txt")
            old = open(gp, encoding="utf-8").read() if os.path.isfile(gp) else None
            if old is None or old.strip("\n") != ghost.strip("\n"):
                with open(gp + ".f2o77-tmp", "w", encoding="utf-8", newline="\n") as f:
                    f.write(ghost)
                os.replace(gp + ".f2o77-tmp", gp)
                changed.append(os.path.relpath(gp, server_dir))
        defs = [os.path.join(manager, "shared", "maps", name + ".lua")]
        sd = os.path.join(fm, "staged", name)
        if os.path.isdir(sd):
            try:
                rel = json.load(open(os.path.join(sd, "build.json"), encoding="utf-8")).get("managerDef")
            except (OSError, ValueError):
                rel = None
            rel = rel or "manager/%s.lua" % name
            if ".." not in rel.replace("\\", "/").split("/") and not os.path.isabs(rel):
                defs.append(os.path.join(sd, rel))
        for d in defs:
            if os.path.isfile(d) and splice_ghost(d, name, ghost):
                changed.append(os.path.relpath(d, server_dir))
        return {"map": name, "lines": lines, "kinds": {str(k): v for k, v in sorted(kinds.items())},
                "changed": changed, "staged": len(defs) > 1}
    finally:
        if fd is not None:
            os.close(fd)

if __name__ == "__main__":
    cmd, path = sys.argv[1], sys.argv[2]
    if cmd == "summary":
        c = load(path)
        res, rm = c.get("resources") or {}, c.get("requiredMods") or {}
        print(json.dumps({"load": res.get("load"), "requiredMods.enabled": rm.get("enabled"),
                          "requiredMods.unsecured": rm.get("unsecured"),
                          "watchIntervalMilliseconds": res.get("watchIntervalMilliseconds"),
                          "download": {k: (res.get("download") or {}).get(k) for k in ("listenUrl", "publicBaseUrl")},
                          "network.port": (c.get("network") or {}).get("port"),
                          "visibility": (c.get("identity") or {}).get("visibility")}))
    elif cmd == "remove-load":
        text = open(path, encoding="utf-8").read()
        text, gone = remove_from_load(text, sys.argv[3:])
        if gone:
            json.loads(strip_jsonc(text))          # still parses
            open(path, "w", encoding="utf-8").write(text)
        print(" ".join(gone))
    elif cmd == "add-load":
        text = open(path, encoding="utf-8").read()
        text, new = add_to_load(text, sys.argv[3:])
        if new:
            json.loads(strip_jsonc(text))          # still parses
            open(path, "w", encoding="utf-8").write(text)
        print(" ".join(new))
    elif cmd == "set-bool":
        text = open(path, encoding="utf-8").read()
        text = set_bool(text, sys.argv[3], sys.argv[4], sys.argv[5] == "true")
        json.loads(strip_jsonc(text))
        open(path, "w", encoding="utf-8").write(text)
    elif cmd == "set-http":
        text = open(path, encoding="utf-8").read()
        text = set_http(text, sys.argv[3] == "on", sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None)
        c = json.loads(strip_jsonc(text))
        open(path, "w", encoding="utf-8").write(text)
        h = c.get("httpHandlers") or {}
        print(json.dumps({"enabled": h.get("enabled"), "listenUrl": h.get("listenUrl")}))
    elif cmd == "pending-add":
        # path = the hub's data folder; argv: name version record display
        import os, time
        name, version, record, display = sys.argv[3:7]
        pf, inf = os.path.join(path, "pending.json"), os.path.join(path, "installed.json")
        def rd(p, d):
            try:
                v = json.load(open(p, encoding="utf-8"))
                return v if isinstance(v, type(d)) else d
            except (OSError, ValueError):
                return d
        pending, installed = rd(pf, []), rd(inf, {})
        live = {k: v.get("version") for k, v in installed.items() if isinstance(v, dict)}
        keep = [p for p in pending if isinstance(p, dict) and p.get("name") != name
                and live.get(p.get("name")) != p.get("version")]
        keep.append({"name": name, "version": version, "record": record, "display": display,
                     "staged_at": int(time.time())})
        os.makedirs(path, exist_ok=True)
        tmp = pf + ".f2o77-tmp"
        json.dump(keep, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, pf)
        print(len(keep))
    elif cmd == "pending-names":
        # cars still waiting: queued and not yet reported live by the hub at that version
        def rd(p, d):
            try:
                v = json.load(open(p, encoding="utf-8"))
                return v if isinstance(v, type(d)) else d
            except (OSError, ValueError):
                return d
        inst = rd(path + "/installed.json", {})
        names = []
        for p in rd(path + "/pending.json", []):
            if not isinstance(p, dict):
                continue
            i = inst.get(p.get("name")) if isinstance(inst.get(p.get("name")), dict) else {}
            if not (i.get("state") == "live" and i.get("version") == p.get("version")):
                names.append(p.get("name", ""))
        print(" ".join(names))
    elif cmd == "acl-file":
        print((load(path).get("accessControl") or {}).get("file") or "")
    elif cmd == "ghost-update":
        # path = the server dir; argv: manager dir, map resource dir (may be empty), map name, ghost file
        try:
            print(json.dumps(ghost_update(path, sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])))
        except (ValueError, RuntimeError, OSError) as e:
            print(str(e))
            sys.exit(3)
    elif cmd == "acl-grant":
        # argv: permission, principal name prefix (may be empty); prints the principals (no keys)
        perm, prefix = sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else ""
        text = open(path, encoding="utf-8").read()
        acl = json.loads(strip_jsonc(text))
        for p in acl.get("principals") or []:
            print("principal %s  user %s  roles %s  permissions %d  %s: %s" % (
                p.get("name"), (str(p.get("userId") or "-"))[:8] + "...", p.get("roles") or [],
                len(p.get("permissions") or []), perm, acl_allowed_by(acl, p, perm) or "no"))
        try:
            text, changed, already, chosen = acl_grant(text, perm, prefix)
        except LookupError as e:
            print("ACL-GRANT " + json.dumps({"error": str(e)}))
            sys.exit(3)
        if changed:
            open(path, "w", encoding="utf-8").write(text)
        print("ACL-GRANT " + json.dumps({"permission": perm, "changed": changed, "already": already, "principals": chosen}))
    elif cmd == "status":
        # path = server dir; argv[3] = unit state, argv[4] = pid, argv[5] = since, argv[6] = online ids,
        # argv[7] = the map baker service state, argv[8] = the baker toolkit installed on this host
        import os, hashlib
        d = path
        out = {"name": os.path.basename(d), "dir": d, "unit": sys.argv[3], "pid": sys.argv[4], "since": sys.argv[5],
               "online": sys.argv[6].split(), "cars": [], "hub": None, "maps": [], "mapManager": None,
               "http": None, "mapBaker": None, "worlds": []}
        if len(sys.argv) > 8:
            fm = os.path.join(d, "f2o77-maps")
            staged = sorted(x for x in os.listdir(os.path.join(fm, "staged")) if not x.startswith(".")) \
                if os.path.isdir(os.path.join(fm, "staged")) else []
            out["mapBaker"] = {"service": sys.argv[7] or "unknown", "toolkit": sys.argv[8] or None,
                               "hook": os.path.isfile(os.path.join(fm, "hook.json")), "staged": staged}
        try:
            out["version"] = json.load(open(os.path.join(d, "package-manifest.json"))).get("version")
        except (OSError, ValueError):
            out["version"] = None
        try:
            c = load(os.path.join(d, "server.jsonc"))
            out["load"] = (c.get("resources") or {}).get("load")
            rm = c.get("requiredMods") or {}
            out["requiredMods"] = {"enabled": rm.get("enabled"), "unsecured": rm.get("unsecured")}
            out["port"] = (c.get("network") or {}).get("port")
            hh = c.get("httpHandlers") or {}
            out["http"] = {"enabled": bool(hh.get("enabled")), "listenUrl": hh.get("listenUrl") or "http://127.0.0.1:11781"}
        except (OSError, ValueError) as e:
            out["config_error"] = str(e)
        def manifest(p):
            t = open(p, encoding="utf-8", errors="replace").read()
            g = lambda k: (re.search(r'^%s\s+"([^"]*)"' % k, t, re.M) or [None, None])[1]
            return t, g("resource"), g("version")
        for root, dirs, files in os.walk(os.path.join(d, "resources")):
            if "open77.lua" not in files:
                continue
            dirs[:] = []
            t, rn, ver = manifest(os.path.join(root, "open77.lua"))
            rel = os.path.relpath(root, d)
            if "converted by fivem2open77" in t and "preload_mod" in t and "FiveM map converted" not in t:
                m = re.search(r"Spawn with: /car (\S+)", t)
                disp = (re.match(r"-- (.*): FiveM vehicle converted", t) or [None, None])[1]
                pk = {}
                for z in re.findall(r'^preload_mod\s+"([^"]+)"', t, re.M):
                    zp = os.path.join(root, z)
                    if os.path.isfile(zp):
                        h = hashlib.sha256()
                        with open(zp, "rb") as f:
                            for ch in iter(lambda: f.read(1 << 20), b""):
                                h.update(ch)
                        pk[os.path.basename(z)] = {"bytes": os.path.getsize(zp), "sha256": h.hexdigest()}
                out["cars"].append({"name": rn, "version": ver, "dir": rel, "record": m.group(1) if m else None,
                                    "display": disp, "packages": pk,
                                    "chime": os.path.isdir(root + "_chime") or os.path.isdir(os.path.join(os.path.dirname(root), (rn or "") + "_chime")),
                                    "in_load": rn in (out.get("load") or [rn])})
            elif "FiveM map converted by fivem2open77" in t and os.path.isfile(os.path.join(root, "layout.json")):
                def rj(p):
                    try:
                        return json.load(open(p, encoding="utf-8"))
                    except (OSError, ValueError):
                        return None
                baked = rj(os.path.join(root, "baked.json")) or {}
                lay = rj(os.path.join(root, "layout.json")) or {}
                out["maps"].append({"name": rn, "version": ver, "dir": rel, "display": lay.get("display"),
                                    "anchor": baked.get("anchor"), "bakeId": baked.get("id"),
                                    "package": baked.get("package"), "bakedAt": baked.get("bakedAt"),
                                    "in_load": rn in (out.get("load") or [rn])})
            elif "world edit made with fivem2open77" in t:
                try:
                    ed = json.load(open(os.path.join(root, "edits.json"), encoding="utf-8"))
                except (OSError, ValueError):
                    ed = {}
                out["worlds"].append({"name": rn, "version": ver, "dir": rel, "display": ed.get("display"),
                                      "summary": ed.get("summary"), "package": ed.get("package"),
                                      "in_load": rn in (out.get("load") or [rn])})
            elif rn == "f2o77_maps":
                mm = {"dir": rel, "version": ver, "in_load": rn in (out.get("load") or [rn]), "maps": [],
                      "baker": None, "status": {}, "token": os.path.isfile(os.path.join(root, "data", "agent_token.txt"))}
                sm = os.path.join(root, "shared", "maps")
                if os.path.isdir(sm):
                    mm["maps"] = sorted(f[:-4] for f in os.listdir(sm) if f.endswith(".lua") and not f.startswith("_"))
                bd = os.path.join(root, "data", "bake")
                try:
                    mm["baker"] = json.load(open(os.path.join(bd, "baker.json"), encoding="utf-8"))
                except (OSError, ValueError):
                    pass
                if os.path.isdir(os.path.join(bd, "status")):
                    for f in sorted(os.listdir(os.path.join(bd, "status"))):
                        if f.endswith(".json"):
                            try:
                                mm["status"][f[:-5]] = json.load(open(os.path.join(bd, "status", f), encoding="utf-8"))
                            except (OSError, ValueError):
                                pass
                out["mapManager"] = mm
            elif rn == "f2o77_hub":
                hub = {"dir": rel, "version": ver, "in_load": rn in (out.get("load") or [rn])}
                for k in ("status", "pending", "installed", "config"):
                    try:
                        hub[k] = json.load(open(os.path.join(root, "data", k + ".json"), encoding="utf-8"))
                    except (OSError, ValueError):
                        hub[k] = None
                out["hub"] = hub
        print(json.dumps(out))
PY

version_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version","?"))' "$1/package-manifest.json" 2>/dev/null || echo "?"; }
op77_build() { echo "$1" | sed -nE 's/.*op77\.([0-9]+).*/\1/p'; }
online() {   # connections opened and not closed since the last start
  local log=$1/server.log
  [ -f "$log" ] || return 0
  python3 - "$log" <<'PY'
import re, sys
lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
start = max([i for i, l in enumerate(lines) if "Server configuration:" in l] or [0])
live = {}
for l in lines[start:]:
    m = re.search(r"Connection (\d+) (opened|closed)", l)
    if m: live[m.group(1)] = m.group(2) == "opened"
print(" ".join(k for k, v in live.items() if v))
PY
}
# --check-config exactly as the unit would start the server: its ExecStart command line, working
# directory and environment (Environment= and EnvironmentFile=, e.g. the database connection the
# config needs).  Nothing from that environment is printed.
check_config() {
  local unit=$1 dir=$2
  python3 - "$unit" "$dir" <<'PY'
import os, re, shlex, subprocess, sys
unit, d = sys.argv[1], sys.argv[2]
sysctl = os.environ.get("SYSTEMCTL", "systemctl").split()
def spawn(cmd, **kw):
    # the unit's command is a Linux binary and SYSTEMCTL a systemctl.  When the test harness runs
    # this script on Windows they are shell scripts only bash (Git Bash) can start
    return subprocess.run((["bash"] + list(cmd)) if os.name == "nt" else cmd, **kw)
def show(prop):
    r = spawn(sysctl + ["show", "-p", prop, "--value", unit], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""
env = dict(os.environ)
for f in re.findall(r"(/[^\s()]+)", show("EnvironmentFiles")):
    try:
        for line in open(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k.strip()] = v
    except OSError:
        pass
try:
    for item in shlex.split(show("Environment")):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
except ValueError:
    pass
m = re.search(r"argv\[\]=([^;]+);", show("ExecStart"))
cmd = m.group(1) if m else ""
if os.name == "nt":
    cmd = cmd.replace("\\", "/")     # shlex would read Windows path backslashes as escapes
argv = shlex.split(cmd) if cmd else []
wd = show("WorkingDirectory") or d
if not argv or not any("Open77.Server" in a for a in argv):
    argv = ["./Open77.Server", "--config", "server.jsonc"]
    wd = d
argv = [a for a in argv if a not in ("--no-setup",)] + ["--no-setup", "--check-config"]
if "--config" not in argv and not any(a.startswith("--config=") for a in argv):
    argv += ["--config", "server.jsonc"]
try:
    r = spawn(argv, cwd=wd, env=env, capture_output=True, text=True, timeout=90)
except (OSError, subprocess.TimeoutExpired) as e:
    print(f"check could not run: {e}")
    sys.exit(2)
out = (r.stdout + r.stderr).strip().splitlines()
print("\n".join(out[-6:]))
sys.exit(r.returncode)
PY
}

res_version() { sed -nE 's/^version "([^"]+)".*/\1/p' "$1/open77.lua" 2>/dev/null | head -1; }
res_record() { sed -nE 's/^-- Spawn with: \/car (Vehicle\.[A-Za-z0-9_]+).*/\1/p' "$1/open77.lua" 2>/dev/null | head -1; }
res_display() { sed -nE 's/^-- (.*): FiveM vehicle converted by fivem2open77\..*/\1/p' "$1/open77.lua" 2>/dev/null | head -1; }
packages_sum() {   # sha256 of a resource's preload packages (empty when it has none)
  local d=$1
  [ -f "$d/open77.lua" ] || return 0
  sed -nE 's/^preload_mod "([^"]+)".*/\1/p' "$d/open77.lua" | while read -r z; do
    [ -f "$d/$z" ] && sha256sum "$d/$z" | cut -c1-64
  done | sha256sum | cut -c1-16
}
find_res() { find "$1/resources" -mindepth 1 -maxdepth 4 -type d -name "$2" 2>/dev/null | head -1; }

# restart one unit cleanly and check what came up; $2.. = resources that must start
restart_unit() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service; shift
  local must=("$@")
  local n; n=$(wc -l < "$dir/server.log" 2>/dev/null || echo 0)
  local oldpid; oldpid=$($SYSTEMCTL show -p MainPID --value "$unit" 2>/dev/null || echo 0)
  say "$name: restarting $unit"
  # SIGINT is the server's clean-exit path (~2 s; it ignores SIGTERM).  Restart=always units come
  # back by themselves; the others are started again here.
  $SYSTEMCTL kill --signal=SIGINT "$unit" 2>/dev/null || true
  local st pid
  for _ in $(seq 1 45); do
    sleep 1
    st=$($SYSTEMCTL is-active "$unit" 2>/dev/null || true)
    pid=$($SYSTEMCTL show -p MainPID --value "$unit" 2>/dev/null || echo 0)
    if [ "$st" = "active" ] && [ "$pid" != "$oldpid" ] && [ "$pid" != "0" ]; then break; fi
    if [ "$st" = "inactive" ] || [ "$st" = "failed" ]; then $SYSTEMCTL start "$unit"; break; fi
  done
  local ok=0
  for _ in $(seq 1 60); do
    sleep 2
    # here-strings, not `... | grep -q`: under pipefail, grep -q's early exit SIGPIPEs the writer
    # and a long log then reads as "no match"
    if grep -q "listening on UDP" <<<"$(tail -n +$((n + 1)) "$dir/server.log")"; then ok=1; break; fi
  done
  sleep 6
  local log; log=$(tail -n +$((n + 1)) "$dir/server.log")
  [ $ok = 1 ] || { say "$name: !! the server did not report listening; last lines:"; tail -15 <<<"$log"; return 1; }
  grep -E "Required mods:|listening on UDP" <<<"$log" | sed 's/^/    /'
  local bad=0
  for rn in "${must[@]}"; do
    if grep -q "\[resource:$rn\] started" <<<"$log"; then
      echo "    $rn: started"
    else
      echo "    $rn: NOT started"; bad=1
      grep -F "$rn" <<<"$log" | tail -3 | cut -c1-220 | sed 's/^/      /' || true
    fi
  done
  if [ ${#must[@]} -gt 0 ]; then
    grep -E "\[(ERR|FTL)\]" <<<"$log" | grep -E "$(IFS='|'; echo "${must[*]}")" | head -5 | sed 's/^/    /' || true
  fi
  [ $bad = 0 ] && say "$name: done" || { say "$name: some resources did not start (see above)"; return 1; }
}

status_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service
  if [ ! -d "$dir" ]; then
    if [ $JSON = 1 ]; then echo "{\"name\": \"$name\", \"exists\": false}"; else say "$name: no $dir"; fi
    return 1
  fi
  local st pid since
  st=$($SYSTEMCTL is-active "$unit" 2>/dev/null || true); st=${st:-unknown}
  pid=$($SYSTEMCTL show -p MainPID --value "$unit" 2>/dev/null || echo 0)
  since=$($SYSTEMCTL show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null || true)
  local bst bver
  bst=$($SYSTEMCTL is-active "f2o77-mapbaker-$name.service" 2>/dev/null || true)
  bver=$(head -1 "${BAKER_HOME:-$HOME/.f2o77/baker}/VERSION" 2>/dev/null || true)
  local js; js=$(python3 "$WORK/cfg.py" status "$dir" "$st" "$pid" "$since" "$(online "$dir")" "${bst:-unknown}" "$bver")
  if [ $JSON = 1 ]; then echo "$js"; return 0; fi
  python3 - "$js" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
print(f"[{s['name']}] Open77 {s.get('version')}  unit {s['unit']} (pid {s['pid']})  online: {len(s['online'])}")
for c in s["cars"]:
    print(f"    car {c['name']} {c['version']}  {c['record']}  {'' if c['in_load'] else '(not in resources.load) '}{', '.join(c['packages'])}")
for mp in s.get("maps", []):
    a = mp.get("anchor") or {}
    where = f"{a.get('x')} {a.get('y')} {a.get('z')} yaw {a.get('yaw')}" if a else "not baked"
    print(f"    map {mp['name']} {mp['version']}  at {where}  {'' if mp['in_load'] else '(not in resources.load)'}")
for wd in s.get("worlds", []):
    sm = wd.get("summary") or {}
    print(f"    world edit {wd['name']} {wd['version']}  {'NIGHT CITY OFF, ' if sm.get('city') == 'off' else ''}"
          f"{sm.get('deleted', 0)} removed, {sm.get('elementsDeleted', 0)} parts, "
          f"{(sm.get('changed') or 0) + (sm.get('elementsMoved') or 0)} moved/changed, {sm.get('added', 0)} added  "
          f"{'' if wd['in_load'] else '(not in resources.load)'}")
mm = s.get("mapManager")
if mm:
    b = mm.get("baker") or {}
    print(f"    map manager {mm['version']}  maps: {', '.join(mm['maps']) or '-'}  baker: "
          f"{('v' + str(b.get('version')) + (' start hook' if b.get('hook') else ' NO start hook')) if b else 'not reported'}")
    mb = s.get("mapBaker") or {}
    print(f"    map baker service: {mb.get('service')}  toolkit: {mb.get('toolkit') or 'not installed'}  "
          f"staged: {', '.join(mb.get('staged') or []) or '-'}")
hh = s.get("http") or {}
print(f"    agent API (httpHandlers): {'on ' + str(hh.get('listenUrl')) if hh.get('enabled') else 'off'}")
h = s.get("hub")
if h:
    st = h.get("status") or {}
    print(f"    hub {h['version']}  waiting: {len(st.get('waiting') or [])}  restart: {(st.get('restart') or {}).get('scheduled')}")
else:
    print("    hub: not installed")
PY
}

# the map manager's agent API: Open77's HTTP handler listener (httpHandlers) on or off
agent_api_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service
  [ -f "$dir/server.jsonc" ] || { say "$name: no $dir/server.jsonc"; return 1; }
  local bk=${BACKUP_ROOT:-$SERVERS_ROOT/open77-backups}/$name-f2o77-$TS
  mkdir -p "$bk"; chmod 700 "$bk"
  cp -p "$dir/server.jsonc" "$bk/server.jsonc"
  local want=on; [ $HTTP_OFF = 1 ] && want=off
  local res
  res=$(python3 "$WORK/cfg.py" set-http "$dir/server.jsonc" "$want" "${F2O77_HTTP_LISTEN:-}") \
    || { say "$name: could not edit httpHandlers: server.jsonc restored"; cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1; }
  if cmp -s "$bk/server.jsonc" "$dir/server.jsonc"; then
    say "$name: AGENT API already $want ($res)"; return 0
  fi
  if [ -x "$dir/Open77.Server" ] || [ -f "$dir/Open77.Server.dll" ]; then
    local chk
    if chk=$(check_config "$unit" "$dir" 2>&1); then
      say "$name: $(echo "$chk" | grep -m1 -i 'valid' || echo 'config valid')"
    else
      say "$name: --check-config FAILED, restoring server.jsonc:"; echo "$chk" | tail -5 | sed 's/^/    /'
      cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1
    fi
  fi
  say "$name: AGENT API $want ($res); backup $bk"
  if [ $RESTART = 1 ]; then
    local who; who=$(online "$dir")
    if [ -n "$who" ]; then say "$name: PENDING restart: connection(s) $who online, not restarting"; return 0; fi
    restart_unit "$name"
  else
    say "$name: PENDING restart: the listener starts with the next server start"
  fi
}

# an ACL right for a principal of the server's ACL file; the server reads the file at start
acl_grant_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service
  [ -f "$dir/server.jsonc" ] || { say "$name: no $dir/server.jsonc"; return 1; }
  local rel; rel=$(python3 "$WORK/cfg.py" acl-file "$dir/server.jsonc") || { say "$name: server.jsonc does not parse"; return 1; }
  if [ -z "$rel" ]; then
    say "$name: server.jsonc has no accessControl.file: this server has no ACL file (nothing changed)"; return 3
  fi
  local acl; case "$rel" in /*) acl=$rel ;; *) acl=$dir/$rel ;; esac
  [ -f "$acl" ] || { say "$name: the ACL file $acl is missing (nothing changed)"; return 3; }
  local owed=$dir/.f2o77-acl-restart     # a grant written but not loaded yet (nobody restarted since)
  local bk=${BACKUP_ROOT:-$SERVERS_ROOT/open77-backups}/$name-f2o77-acl-$TS
  mkdir -p "$bk"; chmod 700 "$bk"
  cp -p "$acl" "$bk/$(basename "$acl")"
  local out rc
  out=$(python3 "$WORK/cfg.py" acl-grant "$acl" "$PERM" "${F2O77_ACL_PRINCIPAL:-}" 2>&1); rc=$?
  grep -v '^ACL-GRANT ' <<<"$out" | sed 's/^/    /'
  local res; res=$(grep '^ACL-GRANT ' <<<"$out" | tail -1 | cut -c11-)
  if [ $rc != 0 ] || [ -z "$res" ]; then
    cp -p "$bk/$(basename "$acl")" "$acl"
    say "$name: no change: ${res:-$(tail -3 <<<"$out")}"; return 3
  fi
  local changed; changed=$(python3 -c 'import json,sys; print(" ".join(json.loads(sys.argv[1])["changed"]))' "$res")
  if [ -n "$changed" ]; then
    if [ -x "$dir/Open77.Server" ] || [ -f "$dir/Open77.Server.dll" ]; then
      local chk
      if chk=$(check_config "$unit" "$dir" 2>&1); then
        say "$name: $(echo "$chk" | grep -m1 -i 'valid' || echo 'config valid')"
      else
        say "$name: --check-config FAILED, restoring $(basename "$acl"):"; echo "$chk" | tail -5 | sed 's/^/    /'
        cp -p "$bk/$(basename "$acl")" "$acl"; return 1
      fi
    fi
    say "$name: $PERM added for $changed in $(basename "$acl"); backup $bk"
    touch "$owed"
  else
    say "$name: $PERM: already there ($(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["already"])' "$res"))"
  fi
  if [ ! -f "$owed" ]; then return 0; fi
  if [ $RESTART = 1 ]; then
    local who; who=$(online "$dir")
    if [ -n "$who" ]; then
      say "$name: PENDING restart: connection(s) $who online; leave the server and run this again"; return 0
    fi
    restart_unit "$name" && rm -f "$owed"
  else
    say "$name: PENDING restart: the server reads the ACL at its next start"
  fi
}

logs_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 lines=${2:-200}
  [[ "$lines" =~ ^[0-9]+$ ]] || lines=200
  tail -n "$lines" "$dir/server.log" 2>/dev/null || say "$name: no server.log"
}

inspect_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service
  [ -d "$dir" ] || { say "$name: no $dir"; return 1; }
  local ver; ver=$(version_of "$dir")
  say "$name: Open77 $ver, unit $($SYSTEMCTL is-active "$unit" 2>/dev/null || true), online: [$(online "$dir")]"
  python3 "$WORK/cfg.py" summary "$dir/server.jsonc" | sed "s/^/    config: /"
  for r in "${BRES[@]}"; do
    local rn; rn=$(basename "$r")
    local found; found=$(find "$dir/resources" -mindepth 1 -maxdepth 4 -type d -name "$rn" 2>/dev/null | head -1)
    local have=absent
    [ -n "$found" ] && have="$(res_version "$found") at ${found#"$dir"/}"
    say "    $rn: bundle $(res_version "$WORK/$r") / server $have"
  done
}

# the map manager: code from a same-or-newer bundle, the bundle's map files merged, the rest kept
merge_manager() {
  local src=$1 target=$2 bk=$3
  { mkdir -p "$bk/resources" && rm -rf "$bk/resources/f2o77_maps" && cp -a "$target" "$bk/resources/f2o77_maps"; } || return 1
  local have want f b
  have=$(res_version "$target"); want=$(res_version "$src")
  if [ "$(printf '%s\n%s\n' "$want" "$have" | sort -V | tail -1)" = "$want" ]; then
    { rm -rf "$target.f2o77-new" && cp -a "$src" "$target.f2o77-new"; } || return 1
    mkdir -p "$target.f2o77-new/shared/maps" || return 1
    for f in "$target"/shared/maps/*.lua; do
      [ -e "$f" ] || continue
      b=$(basename "$f")
      [ -e "$target.f2o77-new/shared/maps/$b" ] || cp -p "$f" "$target.f2o77-new/shared/maps/$b" || return 1
    done
    if [ -d "$target/data" ]; then
      { mkdir -p "$target.f2o77-new/data" && cp -a "$target/data/." "$target.f2o77-new/data/"; } || return 1
    fi
    { rm -rf "$target" && mv "$target.f2o77-new" "$target"; } || return 1
    echo "code $have -> $want"
  else
    mkdir -p "$target/shared/maps" || return 1
    for f in "$src"/shared/maps/*.lua; do
      [ -e "$f" ] || continue
      [ "$(basename "$f")" = "_index.lua" ] && continue
      cp -p "$f" "$target/shared/maps/$(basename "$f").f2o77-tmp" && mv "$target/shared/maps/$(basename "$f").f2o77-tmp" "$target/shared/maps/$(basename "$f")" || return 1
    done
    echo "code $have kept (bundle $want)"
  fi
}

manager_update_one() {
  local name=$1 dir=$SERVERS_ROOT/$1
  [ -d "$dir" ] || { say "$name: no $dir"; return 1; }
  local mgr; mgr=$(find_res "$dir" f2o77_maps)
  if [ -z "$mgr" ] || [ ! -f "$mgr/open77.lua" ]; then
    say "$name: no map manager (f2o77_maps) here: deploy a map first"; return 1
  fi
  local bk=${BACKUP_ROOT:-$SERVERS_ROOT/open77-backups}/$name-f2o77-manager-$TS
  { mkdir -p "$bk/resources" && chmod 700 "$bk" && cp -a "$mgr" "$bk/resources/f2o77_maps"; } \
    || { say "$name: could not back up f2o77_maps: stopped"; return 1; }
  local src=$WORK/resources/assets/tools/f2o77_maps changed=0 bad=0
  if [ -f "$src/open77.lua" ]; then
    local have want; have=$(res_version "$mgr"); want=$(res_version "$src")
    if [ "$(printf '%s\n%s\n' "$want" "$have" | sort -V | tail -1)" = "$want" ]; then
      local rel n=0
      # each file is written beside its place and renamed over it, with a fresh time, so the server's
      # resource watcher sees whole new files; shared/maps/ (but its index) and data/ are the server's
      while IFS= read -r rel; do
        case "$rel" in
          data/*) continue ;;
          shared/maps/_index.lua) ;;
          shared/maps/*) continue ;;
        esac
        cmp -s "$src/$rel" "$mgr/$rel" && continue
        { mkdir -p "$(dirname "$mgr/$rel")" && cp "$src/$rel" "$mgr/$rel.f2o77-tmp" && chmod 644 "$mgr/$rel.f2o77-tmp" \
            && mv -f "$mgr/$rel.f2o77-tmp" "$mgr/$rel"; } \
          || { say "$name: could not write f2o77_maps/$rel: stopped (backup $bk)"; return 1; }
        n=$((n + 1))
      done < <(cd "$src" && find . -type f | sed 's|^\./||' | sort)
      if [ $n -gt 0 ]; then
        say "$name: f2o77_maps code $have -> $want ($n file(s))"; changed=1
      else
        say "$name: f2o77_maps code $want already in place"
      fi
    else
      say "$name: f2o77_maps $have is newer than this update's $want: code kept"
    fi
  fi
  local g map res out
  for g in "$WORK"/ghosts/*.txt; do
    [ -e "$g" ] || continue
    map=$(basename "$g" .txt)
    if ! [[ "$map" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$ ]]; then say "$name: bad map name $map: skipped"; bad=1; continue; fi
    if [ ! -f "$mgr/shared/maps/$map.lua" ]; then say "$name: $map is not installed here: its wireframe skipped"; continue; fi
    res=$(find_res "$dir" "$map")
    { [ -n "$res" ] && [ -f "$res/layout.json" ]; } || res=""
    mkdir -p "$bk/ghosts/$map" && cp -p "$mgr/shared/maps/$map.lua" "$bk/ghosts/$map/" \
      && { [ -z "$res" ] || [ ! -f "$res/ghost.txt" ] || cp -p "$res/ghost.txt" "$bk/ghosts/$map/"; } \
      || { say "$name: could not back up $map's wireframe: skipped"; bad=1; continue; }
    if ! out=$(python3 "$WORK/cfg.py" ghost-update "$dir" "$mgr" "$res" "$map" "$g"); then
      say "$name: $map wireframe refused: $out"; bad=1; continue
    fi
    say "$name: $map wireframe: $(python3 -c '
import json, sys
r = json.loads(sys.argv[1])
names = {"4": "outline", "5": "gates", "0": "box", "1": "buildings", "2": "walls", "3": "ground"}
kinds = ", ".join("%s %s" % (v, names.get(k, "kind " + k)) for k, v in r["kinds"].items())
what = ("updated: " + ", ".join(r["changed"])) if r["changed"] else "already this one"
print("%d lines (%s); %s; where it is baked kept%s" % (r["lines"], kinds, what, "; the staged build too" if r["staged"] else ""))
' "$out")"
    [ "$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["changed"]))' "$out")" = 0 ] || changed=1
  done
  # Night City's road lanes for the manager's road overlay (data/roads, server side only): replaced as
  # a whole -- a new folder beside it, renamed in; the old one goes to the backup
  if [ -f "$WORK/roads/index.json" ]; then
    local rdst=$mgr/data/roads rnew=$mgr/data/roads.f2o77-new rn
    rn=$(find "$WORK/roads" -maxdepth 1 -name '*.lanes' | wc -l)
    if [ -d "$rdst" ] && diff -rq "$WORK/roads" "$rdst" >/dev/null 2>&1; then
      say "$name: road data already in place ($rn tiles)"
    elif rm -rf "$rnew" && mkdir -p "$rnew" && cp "$WORK"/roads/* "$rnew"/ && chmod 644 "$rnew"/*; then
      { [ ! -d "$rdst" ] || mv "$rdst" "$bk/data-roads"; } && mv "$rnew" "$rdst" \
        && { say "$name: road data installed ($rn tiles of Night City's lanes)"; changed=1; } \
        || { say "$name: could not put the road data in place (backup $bk)"; bad=1; }
    else
      say "$name: could not copy the road data: skipped"; bad=1
    fi
  fi
  say "$name: backup $bk"
  if [ $changed = 0 ]; then
    say "$name: nothing changed"
  else
    local watch; watch=$(python3 "$WORK/cfg.py" summary "$dir/server.jsonc" | python3 -c 'import json,sys; print(json.load(sys.stdin)["watchIntervalMilliseconds"])')
    if [ "$watch" = "0" ]; then
      say "$name: PENDING restart: this server does not watch its resources (watchIntervalMilliseconds 0)"
    else
      say "$name: LIVE: f2o77_maps reloads by itself (scripts and its page, no restart); players get it within seconds"
    fi
  fi
  return $bad
}

deploy_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 unit=$1.service
  [ -d "$dir" ] || { say "$name: no $dir"; return 1; }
  local ver b; ver=$(version_of "$dir"); b=$(op77_build "$ver")
  if [ -n "$b" ] && [ "$b" -lt 63 ]; then say "$name: Open77 $ver has no network package audio (needs op77.63+): skipped"; return 1; fi
  local sum; sum=$(python3 "$WORK/cfg.py" summary "$dir/server.jsonc")
  local enabled unsec
  enabled=$(echo "$sum" | python3 -c 'import json,sys; print(json.load(sys.stdin)["requiredMods.enabled"])')
  unsec=$(echo "$sum" | python3 -c 'import json,sys; print(json.load(sys.stdin)["requiredMods.unsecured"])')
  if [ "$unsec" != "True" ] && [ $ALLOW_UNSECURED = 0 ]; then
    say "$name: requiredMods.unsecured is $unsec; ArchiveXL/TweakXL are executable packages, so the car needs an unsecured world. Not changed (rerun with --allow-unsecured to switch it on)."
    return 1
  fi
  # without resources.load every IMMEDIATE resource folder under the root loads: install there
  local hasload; hasload=$(echo "$sum" | python3 -c 'import json,sys; print(json.load(sys.stdin)["load"] is not None)')
  local bk=${BACKUP_ROOT:-$SERVERS_ROOT/open77-backups}/$name-f2o77-$TS
  mkdir -p "$bk"; chmod 700 "$bk"
  cp -p "$dir/server.jsonc" "$bk/server.jsonc"
  local loadnames=() restartfor=() newres=()
  for r in "${BRES[@]}"; do
    local rn cat target found
    rn=$(basename "$r"); cat=$(basename "$(dirname "$r")")
    found=$(find "$dir/resources" -mindepth 1 -maxdepth 4 -type d -name "$rn" 2>/dev/null | head -1)
    if [ "$hasload" = "True" ]; then target=${found:-$dir/resources/assets/$cat/$rn}; else target=${found:-$dir/resources/$rn}; fi
    loadnames+=("$rn")
    if [ "$rn" = "f2o77_maps" ] && [ -n "$found" ]; then
      local how
      how=$(merge_manager "$WORK/$r" "$found" "$bk") || { say "$name: could not update f2o77_maps: stopped"; return 1; }
      say "$name: f2o77_maps at ${found#"$dir"/}: $how; maps: $(cd "$found/shared/maps" && ls *.lua 2>/dev/null | grep -v '^_' | sed 's/\.lua$//' | paste -sd' ' -)"
      continue
    fi
    # shared resources (loaders, the car hub) are never downgraded by an older Studio's bundle
    if { [ "$cat" = "loaders" ] || [ "$cat" = "tools" ]; } && [ -n "$found" ]; then
      local have want
      have=$(res_version "$found"); want=$(res_version "$WORK/$r")
      if [ "$(printf '%s\n%s\n' "$want" "$have" | sort -V | tail -1)" = "$have" ]; then
        say "$name: ${cat%s} $rn $have already present at ${found#"$dir"/}"
        continue
      fi
    fi
    # (set -e does not apply inside a function called with ||: every step is checked by hand)
    if [ -d "$target" ]; then
      { mkdir -p "$bk/resources" && cp -a "$target" "$bk/resources/$rn"; } || { say "$name: backup of $rn failed: stopped"; return 1; }
    fi
    local oldpk=""
    [ -d "$target" ] && oldpk=$(packages_sum "$target")
    { mkdir -p "$(dirname "$target")" && rm -rf "$target.f2o77-new" && cp -a "$WORK/$r" "$target.f2o77-new"; } \
      || { say "$name: could not stage $rn: stopped"; return 1; }
    # a resource's data/ folder is its state and settings (the hub's queue, config): keep it
    if [ -d "$target/data" ]; then
      mkdir -p "$target.f2o77-new/data" && cp -a "$target/data/." "$target.f2o77-new/data/" \
        || { say "$name: could not carry over $rn/data: stopped"; return 1; }
    fi
    { rm -rf "$target" && mv "$target.f2o77-new" "$target"; } || { say "$name: could not install $rn: stopped"; return 1; }
    say "$name: installed $rn $(res_version "$target") at ${target#"$dir"/}"
    if grep -q '^dependency "archivexl' "$target/open77.lua" 2>/dev/null && [ -z "$(find_res "$dir" archivexl)" ]; then
      say "$name: WARNING: $rn needs the archivexl loader resource, which this server does not have"
    fi
    local newpk; newpk=$(packages_sum "$target")
    if [ "$cat" != "loaders" ] && grep -q '^preload_mod' "$target/open77.lua" 2>/dev/null; then
      if [ -n "$oldpk" ] && [ "$oldpk" = "$newpk" ]; then
        say "$name: $rn packages unchanged: its scripts reload live, no restart needed"
      else
        restartfor+=("$rn")
      fi
    elif [ "$cat" != "loaders" ] && [ -z "$oldpk" ] && [ ! -d "$bk/resources/$rn" ]; then
      newres+=("$rn")
    fi
  done
  if [ "$hasload" = "True" ]; then
    local added
    added=$(python3 "$WORK/cfg.py" add-load "$dir/server.jsonc" "${loadnames[@]}") \
      || { say "$name: could not edit resources.load: server.jsonc restored"; cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1; }
    [ -n "$added" ] && say "$name: resources.load += $added"
  fi
  if [ "$enabled" != "True" ]; then
    python3 "$WORK/cfg.py" set-bool "$dir/server.jsonc" requiredMods enabled true \
      || { say "$name: could not enable requiredMods: server.jsonc restored"; cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1; }
    say "$name: requiredMods.enabled -> true"
  fi
  if [ "$unsec" != "True" ]; then
    python3 "$WORK/cfg.py" set-bool "$dir/server.jsonc" requiredMods unsecured true \
      || { say "$name: could not set unsecured: server.jsonc restored"; cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1; }
    say "$name: requiredMods.unsecured -> true (--allow-unsecured)"
  fi
  if [ -x "$dir/Open77.Server" ] || [ -f "$dir/Open77.Server.dll" ]; then
    local chk
    if chk=$(check_config "$unit" "$dir" 2>&1); then
      say "$name: $(echo "$chk" | grep -m1 -i 'valid' || echo 'config valid')"
    else
      say "$name: --check-config FAILED, restoring server.jsonc:"; echo "$chk" | tail -5 | sed 's/^/    /'
      cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1
    fi
  fi
  say "$name: backup $bk"
  local who; who=$(online "$dir")
  if [ $STAGE = 1 ]; then
    local waitfor=("${restartfor[@]}" "${newres[@]}")
    local hub; hub=$(find_res "$dir" f2o77_hub)
    local hubnew=0; [[ " ${newres[*]} " == *" f2o77_hub "* ]] && hubnew=1
    if [ -n "$hub" ] && [ $hubnew = 0 ]; then
      # resources of this bundle still queued in the hub get their entry refreshed (new version)
      local queued; queued=" $(python3 "$WORK/cfg.py" pending-names "$hub/data") "
      local ln
      for ln in "${loadnames[@]}"; do
        if [[ "$queued" == *" $ln "* ]] && [[ " ${waitfor[*]} " != *" $ln "* ]]; then waitfor+=("$ln"); fi
      done
    fi
    if [ ${#waitfor[@]} = 0 ]; then
      say "$name: LIVE: nothing waits for a restart"; return 0
    fi
    if [ -z "$hub" ]; then
      say "$name: PENDING restart: ${waitfor[*]} load at the next server start (no f2o77_hub here to do it for you)"
      return 0
    fi
    if [ $hubnew = 1 ]; then
      say "$name: PENDING restart: ${waitfor[*]} load at the next server start; f2o77_hub runs from then on"
      return 0
    fi
    local rn2
    for rn2 in "${waitfor[@]}"; do
      local t2; t2=$(find_res "$dir" "$rn2")
      python3 "$WORK/cfg.py" pending-add "$hub/data" "$rn2" "$(res_version "$t2")" "$(res_record "$t2")" "$(res_display "$t2")" >/dev/null \
        || { say "$name: could not queue $rn2 in the hub"; return 1; }
      say "$name: STAGED: $rn2 $(res_version "$t2") queued for f2o77_hub (restart when nobody is online)"
    done
    return 0
  fi
  if [ $RESTART = 0 ]; then say "$name: PENDING restart: the new package is declared at the next server start"; return 0; fi
  if [ -n "$who" ]; then say "$name: PENDING restart: connection(s) $who online, not restarting"; return 0; fi
  restart_unit "$name" "${loadnames[@]}"
}

# remove RESOURCE NAME...: take a world edit off a server (resources/assets/world/<resource> and its
# resources.load entry), backed up and checked like a deploy; --restart restarts when nobody is online.
remove_one() {
  local name=$1 dir=$SERVERS_ROOT/$1 rn=$BUNDLE
  [ -d "$dir" ] || { say "$name: no $dir"; return 1; }
  [[ "$rn" =~ ^[a-z][a-z0-9_]{2,47}$ ]] || { say "$name: '$rn' is not a resource name"; return 1; }
  local target; target=$(find "$dir/resources/assets/world" -mindepth 1 -maxdepth 1 -type d -name "$rn" 2>/dev/null | head -1)
  [ -n "$target" ] || { say "$name: no world edit $rn here (resources/assets/world/$rn)"; return 1; }
  local bk=${BACKUP_ROOT:-$SERVERS_ROOT/open77-backups}/$name-f2o77-$TS
  mkdir -p "$bk/resources"; chmod 700 "$bk"
  cp -p "$dir/server.jsonc" "$bk/server.jsonc" && cp -a "$target" "$bk/resources/$rn" || { say "$name: backup failed: stopped"; return 1; }
  local gone; gone=$(python3 "$WORK/cfg.py" remove-load "$dir/server.jsonc" "$rn") \
    || { say "$name: could not edit resources.load: server.jsonc restored"; cp -p "$bk/server.jsonc" "$dir/server.jsonc"; return 1; }
  [ -n "$gone" ] && say "$name: resources.load -= $gone"
  rm -rf "$target" || { say "$name: could not remove $rn"; return 1; }
  say "$name: removed $rn (backup $bk)"
  if [ -x "$dir/Open77.Server" ] || [ -f "$dir/Open77.Server.dll" ]; then
    local chk
    if chk=$(check_config "$name.service" "$dir" 2>&1); then
      say "$name: $(echo "$chk" | grep -m1 -i 'valid' || echo 'config valid')"
    else
      say "$name: --check-config FAILED, putting it back:"; echo "$chk" | tail -5 | sed 's/^/    /'
      cp -p "$bk/server.jsonc" "$dir/server.jsonc"; cp -a "$bk/resources/$rn" "$target"; return 1
    fi
  fi
  if [ $RESTART = 0 ]; then say "$name: PENDING restart: players keep the edit until the next server start"; return 0; fi
  local who; who=$(online "$dir")
  if [ -n "$who" ]; then say "$name: PENDING restart: connection(s) $who online, not restarting"; return 0; fi
  restart_unit "$name"
}

rc=0
for nm in "${NAMES[@]}"; do
  case "$MODE" in
    remove) remove_one "$nm" || rc=1 ;;
    inspect) inspect_one "$nm" || rc=1 ;;
    deploy) deploy_one "$nm" || rc=1 ;;
    status) status_one "$nm" || rc=1 ;;
    logs) logs_one "$nm" "${NAMES[1]:-200}"; break ;;
    agent-api) agent_api_one "$nm" || rc=1 ;;
    acl-grant) acl_grant_one "$nm" || rc=$? ;;
    manager-update) manager_update_one "$nm" || rc=1 ;;
    restart)
      who=$(online "$SERVERS_ROOT/$nm")
      if [ -n "$who" ] && [ "${F2O77_FORCE:-0}" != 1 ]; then say "$nm: connection(s) $who online: not restarting (F2O77_FORCE=1 to insist)"; rc=1
      else restart_unit "$nm" ${F2O77_MUST:-} || rc=1; fi ;;
    *) echo "mode must be inspect, deploy, status, logs, restart, agent-api, acl-grant, manager-update or remove" >&2; exit 2 ;;
  esac
done
exit $rc
