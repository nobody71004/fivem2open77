#!/usr/bin/env bash
# Read-only survey before upgrading the XBUNIVERSE servers on this host to a new Open77 build.
# Prints no secrets: environment files are listed by name and key only.
set -uo pipefail
say() { echo "[$(date -u +%H:%M:%S)] $*"; }
REL=/opt/open77-releases
say "host $(hostname), $(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"), disk free on /opt: $(df -h /opt | awk 'NR==2{print $4}')"
say "releases in $REL:"; ls -1 "$REL" 2>/dev/null | sed 's/^/    /'
say "release tools:"; ls -la "$REL/tools" 2>/dev/null | awk 'NR>1{print "    "$6" "$7" "$8" "$5" "$9}'
[ -f "$REL/history.json" ] && { say "history.json (tail):"; python3 -c 'import json,sys; h=json.load(open(sys.argv[1])); print("    " + json.dumps(h if not isinstance(h, dict) else {k: (v if not isinstance(v, (list, dict)) else str(v)[:300]) for k, v in h.items()})[:1500])' "$REL/history.json"; }
for n in open77-xbuniverse open77-xbuniverse-staging; do
  d=/opt/$n; u=$n.service
  echo; say "== $n ($d)"
  python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("    version", m.get("version"), "client", m.get("clientVersion"), "files", len(m.get("files", {})), "resources", len(m.get("resources", {})))' "$d/package-manifest.json" 2>/dev/null || echo "    no package-manifest.json"
  say "unit $u: $(systemctl is-active $u) since $(systemctl show -p ActiveEnterTimestamp --value $u)"
  systemctl cat "$u" 2>/dev/null | grep -E '^(ExecStart|WorkingDirectory|Restart|User|KillSignal|TimeoutStopSec|EnvironmentFile|Environment)=' | sed -E 's/(Environment=)(.*)/\1<'"$(systemctl show -p Environment --value $u | tr ' ' '\n' | sed -E 's/=.*//' | tr '\n' ' ')"'>/; s/^/    /'
  for ef in $(systemctl show -p EnvironmentFiles --value "$u" | grep -oE '/[^ ]+'); do
    echo "    envfile $ef keys: $(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ef" 2>/dev/null | sed -E 's/=.*//' | tr '\n' ' ')"
  done
  echo "    top-level files not in its package manifest:"
  python3 - "$d" <<'PY'
import json, os, sys
d = sys.argv[1]
try:
    files = set(json.load(open(os.path.join(d, "package-manifest.json"))).get("files", {}))
except Exception:
    files = set()
extra = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and f not in files)
print("      " + " ".join(extra[:80]) + (" ..." if len(extra) > 80 else ""))
PY
  echo "    resources not in its package manifest (custom):"
  python3 - "$d" <<'PY'
import json, os, sys
d = sys.argv[1]
try:
    stock = set(json.load(open(os.path.join(d, "package-manifest.json"))).get("resources", {}))
except Exception:
    stock = set()
found = []
for root, dirs, files in os.walk(os.path.join(d, "resources")):
    if "open77.lua" in files:
        found.append(os.path.basename(root)); dirs[:] = []
custom = sorted(set(found) - stock)
print("      " + " ".join(custom))
PY
  say "    last start lines:"
  grep -n -E "Server configuration:|listening on UDP|Required mods:" "$d/server.log" 2>/dev/null | tail -3 | cut -c1-200 | sed 's/^/      /'
done
echo
say "mysql: $(command -v mysql >/dev/null && echo present || echo absent)"
say "free memory: $(free -h | awk '/Mem:/{print $7" available of "$2}')"
