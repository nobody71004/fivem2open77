#!/usr/bin/env bash
# Survey + DRY RUN of an Open77 upgrade on this host, using the host's own release tooling
# (/opt/open77-releases/tools: history.py, plan2.py, apply.py, logcheck.py).
#
#   vps_upgrade_plan.sh TARGET TARBALL [NAME...]
#
# Changes nothing on any server: it stages the release under /opt/open77-releases/TARGET (a new
# directory, only if absent), indexes every release into a SCRATCH history file, and prints, per
# server, what plan2.py would do and what apply.py --dry-run would touch.  No secrets printed.
set -uo pipefail
TARGET=${1:?target version, e.g. 2.31.21+op77.131}; TARBALL=${2:?release tarball}; shift 2
NAMES=("$@"); [ ${#NAMES[@]} -gt 0 ] || NAMES=(open77-xbuniverse-staging open77-xbuniverse)
R=/opt/open77-releases; T=$R/tools; REL=$R/$TARGET
say() { echo "[$(date -u +%H:%M:%S)] $*"; }
say "host $(hostname); /opt free $(df -h /opt | awk 'NR==2{print $4}'); memory available $(free -h | awk '/Mem:/{print $7}')"
for t in history.py plan2.py apply.py logcheck.py; do [ -f "$T/$t" ] || { say "missing $T/$t"; exit 2; }; done
say "releases: $(ls -1 $R | grep -v -E '^(tools|history.json)$' | tr '\n' ' ')"

if [ ! -f "$REL/package-manifest.json" ]; then
  say "staging release $TARGET from $(basename "$TARBALL")"
  mkdir -p "$REL.part" && tar -xzf "$TARBALL" -C "$REL.part" || { say "extract failed"; rm -rf "$REL.part"; exit 2; }
  got=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$REL.part/package-manifest.json")
  [ "$got" = "$TARGET" ] || { say "tarball is $got, not $TARGET"; rm -rf "$REL.part"; exit 2; }
  mv "$REL.part" "$REL"
  cp -n "$TARBALL" "$R/" 2>/dev/null || true
fi
say "release $TARGET staged: $(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["version"], "client", m["clientVersion"], len(m["files"]), "files")' "$REL/package-manifest.json")"

SCRATCH=$(mktemp -d); trap 'rm -rf "$SCRATCH"' EXIT
srcs=()
for e in "$R"/*; do
  b=$(basename "$e")
  case "$b" in tools|history.json|*.part) continue ;; esac
  if [ -d "$e" ] && [ -f "$e/package-manifest.json" ]; then srcs+=("$e"); fi
  case "$b" in *.tar.gz|*.tgz) srcs+=("$e") ;; esac
done
python3 "$T/history.py" "$SCRATCH/history.json" "${srcs[@]}" | tail -n 30 | sed 's/^/    /'

for n in "${NAMES[@]}"; do
  d=/opt/$n; u=$n.service
  echo; say "== $n: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$d/package-manifest.json"), unit $(systemctl is-active $u), online: [$(python3 "$T/logcheck.py" --online "$d/server.log" 2>/dev/null)]"
  systemctl cat "$u" 2>/dev/null | grep -E '^(ExecStart|WorkingDirectory|Restart|KillSignal|TimeoutStopSec)=' | sed 's/^/    /'
  python3 "$T/plan2.py" "$d" "$REL" "$SCRATCH/history.json" --json "$SCRATCH/$n-plan.json" | sed 's/^/    /' | head -120
  mkdir -p "$SCRATCH/bk-$n"
  python3 "$T/apply.py" "$d" "$REL" "$SCRATCH/$n-plan.json" "$SCRATCH/bk-$n" --dry-run 2>&1 | tail -n 15 | sed 's/^/    dry-run: /'
done
