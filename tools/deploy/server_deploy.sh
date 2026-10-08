#!/usr/bin/env bash
# fivem2open77: install converted cars (and the ArchiveXL/TweakXL loader resources) into Open77
# servers on this host.  Runs ON the server host; Deploy-Car.ps1 / deploy_car.sh copy it there.
#
#   server_deploy.sh inspect  BUNDLE.tgz NAME [NAME...]
#   server_deploy.sh deploy   BUNDLE.tgz NAME [NAME...] [--restart | --stage] [--allow-unsecured]
#   server_deploy.sh status   -          NAME [NAME...] [--json]
#   server_deploy.sh logs     -          NAME [LINES]
#   server_deploy.sh restart  -          NAME [NAME...]
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
# status prints what is installed (cars, hub, pending, players online); --json for the Studio.
# Prints no secrets.
set -euo pipefail
MODE=${1:?mode}; BUNDLE=${2:?bundle}; shift 2
SERVERS_ROOT=${SERVERS_ROOT:-/opt}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
RESTART=0; STAGE=0; ALLOW_UNSECURED=0; JSON=0; NAMES=()
for a in "$@"; do
  case "$a" in
    --restart) RESTART=1 ;;
    --stage) STAGE=1 ;;
    --allow-unsecured) ALLOW_UNSECURED=1 ;;
    --json) JSON=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) NAMES+=("$a") ;;
  esac
done
[ ${#NAMES[@]} -gt 0 ] || { echo "no server names given" >&2; exit 2; }
for nm in "${NAMES[@]}"; do
  [[ "$nm" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "bad server name: $nm" >&2; exit 2; }
done
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
    elif cmd == "status":
        # path = server dir; argv[3] = unit state, argv[4] = pid, argv[5] = since, argv[6] = online ids
        import os, hashlib
        d = path
        out = {"name": os.path.basename(d), "dir": d, "unit": sys.argv[3], "pid": sys.argv[4], "since": sys.argv[5],
               "online": sys.argv[6].split(), "cars": [], "hub": None}
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
            if "converted by fivem2open77" in t and "preload_mod" in t:
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
def show(prop):
    r = subprocess.run(sysctl + ["show", "-p", prop, "--value", unit], capture_output=True, text=True)
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
argv = shlex.split(m.group(1)) if m else []
wd = show("WorkingDirectory") or d
if not argv or not any("Open77.Server" in a for a in argv):
    argv = ["./Open77.Server", "--config", "server.jsonc"]
    wd = d
argv = [a for a in argv if a not in ("--no-setup",)] + ["--no-setup", "--check-config"]
if "--config" not in argv and not any(a.startswith("--config=") for a in argv):
    argv += ["--config", "server.jsonc"]
try:
    r = subprocess.run(argv, cwd=wd, env=env, capture_output=True, text=True, timeout=90)
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
  local js; js=$(python3 "$WORK/cfg.py" status "$dir" "$st" "$pid" "$since" "$(online "$dir")")
  if [ $JSON = 1 ]; then echo "$js"; return 0; fi
  python3 - "$js" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
print(f"[{s['name']}] Open77 {s.get('version')}  unit {s['unit']} (pid {s['pid']})  online: {len(s['online'])}")
for c in s["cars"]:
    print(f"    car {c['name']} {c['version']}  {c['record']}  {'' if c['in_load'] else '(not in resources.load) '}{', '.join(c['packages'])}")
h = s.get("hub")
if h:
    st = h.get("status") or {}
    print(f"    hub {h['version']}  waiting: {len(st.get('waiting') or [])}  restart: {(st.get('restart') or {}).get('scheduled')}")
else:
    print("    hub: not installed")
PY
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
    say "    $rn: bundle $(res_version "$WORK/$r") / server ${found:+$(res_version "$found") at ${found#$dir/}}${found:-absent}"
  done
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
    # shared resources (loaders, the car hub) are never downgraded by an older Studio's bundle
    if { [ "$cat" = "loaders" ] || [ "$cat" = "tools" ]; } && [ -n "$found" ]; then
      local have want
      have=$(res_version "$found"); want=$(res_version "$WORK/$r")
      if [ "$(printf '%s\n%s\n' "$want" "$have" | sort -V | tail -1)" = "$have" ]; then
        say "$name: ${cat%s} $rn $have already present at ${found#$dir/}"
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
    say "$name: installed $rn $(res_version "$target") at ${target#$dir/}"
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

rc=0
for nm in "${NAMES[@]}"; do
  case "$MODE" in
    inspect) inspect_one "$nm" || rc=1 ;;
    deploy) deploy_one "$nm" || rc=1 ;;
    status) status_one "$nm" || rc=1 ;;
    logs) logs_one "$nm" "${NAMES[1]:-200}"; break ;;
    restart)
      who=$(online "$SERVERS_ROOT/$nm")
      if [ -n "$who" ] && [ "${F2O77_FORCE:-0}" != 1 ]; then say "$nm: connection(s) $who online: not restarting (F2O77_FORCE=1 to insist)"; rc=1
      else restart_unit "$nm" || rc=1; fi ;;
    *) echo "mode must be inspect, deploy, status, logs or restart" >&2; exit 2 ;;
  esac
done
exit $rc
