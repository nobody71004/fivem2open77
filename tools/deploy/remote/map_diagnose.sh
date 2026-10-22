#!/usr/bin/env bash
# fivem2open77: everything about the converted maps on an Open77 server of this host, read only --
# what the map manager knows (spots, the last moves, the apply), what the map baker did (requests,
# answers, staged and installed builds), what the server loaded (log lines, required mods), the map
# definitions the manager reads.  Runs ON the server host.  Prints no tokens or keys.
#   map_diagnose.sh [SERVER_NAME] [LOG_LINES]      default open77-xbuniverse under $HOME, 200
set -u
NAME=${1:-open77-xbuniverse}
LINES=${2:-200}
ROOT=${SERVERS_ROOT:-$HOME}
D=$ROOT/$NAME
SYSTEMCTL=${SYSTEMCTL:-"systemctl --user"}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
echo "== $(hostname) $(date -u +%FT%TZ) server $D"
[ -d "$D" ] || { echo "no server folder $D"; exit 2; }
MGR=$(find "$D/resources" -mindepth 1 -maxdepth 4 -type d -name f2o77_maps 2>/dev/null | head -1)
section() { echo; echo "== $*"; }
json() { python3 -m json.tool "$1" 2>/dev/null || cat "$1"; }

section "server unit"
$SYSTEMCTL status "$NAME.service" --no-pager 2>&1 | head -14
section "server log: maps, required mods, connections, warnings and errors (last $LINES)"
grep -n -E 'f2o77|cedar|baked|preload|Required mods|required mod|Connection|connect|\[ERR|\[WRN|[Ee]rror|exception|failed' \
  "$D/server.log" 2>/dev/null | grep -v -i -E 'token|password|secret' | tail -"$LINES"
section "server log: the last 80 lines"
tail -80 "$D/server.log" 2>/dev/null | grep -v -i -E 'token|password|secret'

section "map manager"
if [ -z "$MGR" ]; then
  echo "no f2o77_maps under $D/resources"
else
  echo "folder: ${MGR#$D/}"
  grep -E '^(resource|version)' "$MGR/open77.lua"
  echo "-- files"
  (cd "$MGR" && find . -path ./data -prune -o -type f -printf '%TY-%Tm-%Td %TH:%TM  %8s  %p\n' | sort)
  echo "-- data/"
  (cd "$MGR/data" 2>/dev/null && find . -type f ! -name 'agent_token*' -printf '%TY-%Tm-%Td %TH:%TM  %8s  %p\n' | sort)
  for def in "$MGR"/shared/maps/*.lua; do
    [ -e "$def" ] || continue
    [ "$(basename "$def")" = "_index.lua" ] && continue
    section "map definition $(basename "$def") ($(wc -c < "$def") bytes)"
    grep -v -E '^[-0-9. ]+$' "$def" | head -60
    python3 - "$def" <<'PY'
import re, sys
t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"\.ghost = \[(=*)\[", t)
if not m:
    print("-- no ghost in this definition")
else:
    end = t.find("]" + m.group(1) + "]", m.end())
    body = t[m.end():end] if end > 0 else ""
    kinds = {}
    for line in body.splitlines():
        p = line.split()
        if len(p) == 7:
            kinds[p[6]] = kinds.get(p[6], 0) + 1
    print("-- ghost: %s; %d lines by kind %s; text after it: %r" % (
        "closed" if end > 0 else "NOT CLOSED", sum(kinds.values()), kinds, t[end:end + 40] if end > 0 else ""))
PY
  done
  for f in "$MGR"/data/maps/*.json; do
    [ -e "$f" ] || continue
    section "manager state $(basename "$f")"
    python3 - "$f" <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1]))
def t(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(v)))
    except (TypeError, ValueError):
        return v
for k in ("running", "runningId", "runningSource", "pending", "previous", "boot"):
    print("%-14s %s" % (k, s.get(k) if k != "boot" else t(s.get(k))))
a = s.get("apply")
print("apply          %s" % (json.dumps(dict(a, **{x: t(a[x]) for x in a if x.endswith("At")})) if isinstance(a, dict) else a))
print("history (newest first):")
for h in (s.get("history") or [])[:15]:
    print("   %s  %-22s %-28s %s" % (t(h.get("at")), str(h.get("by"))[:22], str(h.get("what"))[:28], h.get("anchor")))
print("presets: %s" % [p.get("name") for p in s.get("presets") or []])
PY
  done
  for sub in requests status; do
    for f in "$MGR"/data/bake/$sub/*.json; do
      [ -e "$f" ] || continue
      section "baker $sub/$(basename "$f")"
      json "$f" | head -40
    done
  done
  [ -f "$MGR/data/bake/baker.json" ] && { section "baker.json"; json "$MGR/data/bake/baker.json"; }
fi

for res in $(find "$D/resources" -mindepth 2 -maxdepth 4 -name layout.json -printf '%h\n' 2>/dev/null); do
  section "map resource ${res#$D/}"
  grep -E '^(resource|version|preload_mod)' "$res/open77.lua"
  [ -f "$res/baked.json" ] && json "$res/baked.json" | head -30
  ls -la --time-style=+%F_%T "$res" "$res/dist" 2>/dev/null
done

section "map baker work folder $D/f2o77-maps"
find "$D/f2o77-maps" -maxdepth 3 -printf '%TY-%Tm-%Td %TH:%TM  %8s  %p\n' 2>/dev/null | sort | tail -60
section "map baker service"
$SYSTEMCTL is-active "f2o77-mapbaker-$NAME.service" 2>&1
journalctl --user -u "f2o77-mapbaker-$NAME.service" -n 60 --no-pager 2>&1 | tail -60
section "server unit journal (the start hook installs staged builds)"
journalctl --user -u "$NAME.service" -n 40 --no-pager 2>&1 | grep -v -i -E 'token|password|secret' | tail -40
section "required mods the server serves (.open77/mods)"
ls -la --time-style=+%F_%T "$D/.open77/mods" 2>/dev/null | tail -20
echo
echo "MAP-DIAGNOSE-DONE"
