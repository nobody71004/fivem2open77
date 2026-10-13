#!/usr/bin/env bash
# Upgrade Open77 servers on this host to TARGET with the host's own release tooling
# (/opt/open77-releases/tools: history.py, plan2.py, apply.py, logcheck.py).  The release must be
# staged first (vps_upgrade_plan.sh).  Resources are not touched: plan2.py replaces the stock units
# and leaves custom resources (cars, maps) alone.  Servers are done in the order given; the run
# stops at the first server that does not come up clean, so list staging first.
#
#   vps_upgrade.sh TARGET [--kick] NAME [NAME...]      NAME = /opt/NAME run by NAME.service
#
# --kick upgrades even while players are connected (they are disconnected when the unit stops);
# without it a busy server is refused and the run stops there, like vps_push.sh.
#
# Per server (same flow as vps_push.sh / the host's upgrade.sh):
#   back up every open77 database and the whole directory; plan2.py decides (stock units ->
#   TARGET, custom units left alone); apply.py --dry-run, then stop the unit (SIGINT: the server
#   ignores SIGTERM), apply.py, start, wait for the listener, compare the start with the previous
#   one (logcheck.py) and roll back by itself if it is worse.  Prints no secrets.
set -euo pipefail
TARGET=${1:?target version, e.g. 2.31.21+op77.132}; shift
KICK=0
if [ "${1:-}" = "--kick" ]; then KICK=1; shift; fi
NAMES=("$@"); [ ${#NAMES[@]} -gt 0 ] || { echo "no servers given" >&2; exit 2; }
R=/opt/open77-releases; T=$R/tools; REL=$R/$TARGET; H=$R/history.json
say() { echo "[$(date -u +%H:%M:%S)] $*"; }
short() { echo "$1" | sed -E 's/.*op77\.([0-9]+).*/\1/'; }
version() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$1/package-manifest.json"; }

[ -f "$REL/package-manifest.json" ] || { say "release $TARGET is not staged at $REL (run the plan step first)"; exit 2; }
[ "$(version "$REL")" = "$TARGET" ] || { say "$REL does not hold $TARGET"; exit 2; }
for t in history.py plan2.py apply.py logcheck.py; do [ -f "$T/$t" ] || { say "missing $T/$t"; exit 2; }; done

# ---- history: index every release on the host, TARGET included (the next upgrade needs it)
srcs=()
for e in "$R"/*; do
  b=$(basename "$e")
  case "$b" in tools|history.json*|*.part) continue ;; esac
  if [ -d "$e" ] && [ -f "$e/package-manifest.json" ]; then srcs+=("$e"); fi
  case "$b" in *.tar.gz|*.tgz) srcs+=("$e") ;; esac
done
[ -f "$H" ] && cp -p "$H" "$H.bak-pre$(short "$TARGET")-$(date -u +%Y%m%d-%H%M%S)"
python3 "$T/history.py" "$H.new" "${srcs[@]}" > /dev/null
mv "$H.new" "$H"
say "history.json re-indexed: $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$H") paths from ${#srcs[@]} releases"

state() { systemctl is-active "$1" 2>/dev/null || true; }
stop_unit() {   # queue the stop first (no Restart=always comeback), then SIGINT for a clean ~2 s exit
  local unit=$1
  systemctl stop --no-block "$unit"
  sleep 1
  systemctl kill --signal=SIGINT "$unit" 2>/dev/null || true
  for _ in $(seq 1 60); do
    case "$(state "$unit")" in inactive|failed) return 0 ;; esac
    sleep 1
  done
  say "$unit still stopping after 60 s; waiting for systemd"
  systemctl stop "$unit"
}
online() {   # connections opened and not closed since the last start (empty when the unit is down)
  [ "$(state "$1.service")" = "active" ] || return 0
  python3 "$T/logcheck.py" --online "/opt/$1/server.log" || true
}
check_online() {   # 0 when it is fine to proceed; --kick disconnects whoever is on
  local who; who=$(online "$1")
  [ -z "$who" ] && return 0
  if [ "$KICK" = 1 ]; then say "$1: connection(s) $who open: --kick given, they are disconnected at the stop"; return 0; fi
  say "$1: connection(s) $who open: not upgrading (pass --kick to disconnect them)"
  return 1
}

came_up() {   # name start-time -> 0 once the start after START is listening, published and ready
  python3 - "/opt/$1/server.log" "$2" "$T/logcheck.py" <<'PY'
import json, subprocess, sys
r = json.loads(subprocess.run(['python3', sys.argv[3], sys.argv[1], '--since', sys.argv[2], '--window', '180'],
                              capture_output=True, text=True).stdout)
sys.exit(0 if r['found'] and r['listening'] and r['published'] and r['host_ready'] is not None else 1)
PY
}

upgrade_one() {
  local NAME=$1 SRV=/opt/$1 UNIT=$1.service
  grep -q "$SRV/Open77.Server" <<<"$(systemctl cat "$UNIT" 2>/dev/null)" || { say "unit $UNIT does not run $SRV"; return 2; }
  local cur; cur=$(version "$SRV")
  if [ "$cur" = "$TARGET" ]; then say "$NAME already carries $TARGET"; return 0; fi
  local active; active=$(state "$UNIT")
  check_online "$NAME" || return 1
  local BK=/opt/open77-backups/$NAME-to$(short "$TARGET")-$(date -u +%Y%m%d-%H%M%S)
  mkdir -p "$BK"; chmod 700 "$BK"; echo "$BK" > "$R/LAST-$NAME"
  say "$NAME: $cur -> $TARGET ($UNIT is $active); backup $BK"
  local dbs; dbs=$(mysql -N -e "show databases like 'open77%'" 2>/dev/null | tr '\n' ' ' || true)
  if [ -n "${dbs// /}" ]; then
    mysqldump --single-transaction --routines --events --databases $dbs 2>"$BK/mysqldump.err" | gzip > "$BK/databases.sql.gz"
    say "databases dumped ($dbs): $(du -h "$BK/databases.sql.gz" | cut -f1)"
  fi
  tar -czf "$BK/server-dir.tgz" --exclude="$(basename "$SRV")/server.log" -C "$(dirname "$SRV")" "$(basename "$SRV")" \
    || { say "$NAME: could not archive $SRV: nothing changed"; return 1; }
  chmod 600 "$BK"/*.gz "$BK"/*.tgz 2>/dev/null || true
  say "directory archived: $(du -h "$BK/server-dir.tgz" | cut -f1)"
  # (set -e does not apply inside a function called from `if`: every step is checked by hand)
  python3 "$T/logcheck.py" "$SRV/server.log" > "$BK/baseline.json" || { say "$NAME: logcheck failed: nothing changed"; return 1; }
  python3 "$T/plan2.py" "$SRV" "$REL" "$H" --json "$BK/plan.json" --brief | tee "$BK/plan.txt" | sed 's/^/    /' \
    || { say "$NAME: plan2.py failed: nothing changed"; return 1; }
  python3 "$T/apply.py" "$SRV" "$REL" "$BK/plan.json" "$BK" --dry-run | sed 's/^/    /' \
    || { say "$NAME: apply.py --dry-run failed: nothing changed"; return 1; }
  check_online "$NAME" || { say "$NAME: nothing changed"; return 1; }
  local OLDPID; OLDPID=$(systemctl show -p MainPID --value "$UNIT")
  if [ "$active" = "active" ]; then say "stopping $UNIT (pid $OLDPID)"; stop_unit "$UNIT"; fi
  local applied=1
  python3 "$T/apply.py" "$SRV" "$REL" "$BK/plan.json" "$BK" | sed 's/^/    /' || applied=0
  if [ $applied = 0 ]; then
    say "!! $NAME: apply.py failed; rolling back the files it wrote"
    python3 "$T/apply.py" --rollback "$SRV" "$BK" | sed 's/^/    /' || true
    [ "$active" = "active" ] && systemctl start "$UNIT"
    return 1
  fi
  if [ "$active" != "active" ]; then
    say "$NAME was not running: files upgraded, not started (rollback: python3 $T/apply.py --rollback $SRV $BK)"
    return 0
  fi
  local START; START=$(date -u '+%Y-%m-%d %H:%M:%S')
  say "starting $UNIT at $START UTC"
  systemctl start "$UNIT"
  local up=0
  for _ in $(seq 1 60); do
    sleep 3
    if came_up "$NAME" "$START"; then up=1; break; fi
    case "$(state "$UNIT")" in active|activating|reloading) ;; *) break ;; esac
  done
  if [ $up = 1 ]; then
    sleep 20     # resources finish starting; late start errors show up
    python3 "$T/logcheck.py" "$SRV/server.log" --since "$START" --window 180 > "$BK/after.json"
    if python3 "$T/logcheck.py" --compare "$BK/baseline.json" "$BK/after.json" | tee "$BK/compare.txt" | sed 's/^/    /' && \
       [ "$(systemctl show -p MainPID --value "$UNIT")" != "$OLDPID" ]; then
      say "$NAME now carries $(version "$SRV") (pid $(systemctl show -p MainPID --value "$UNIT")); backup $BK"
      return 0
    fi
    say "!! $NAME: the new start is worse than the last one"
  else
    say "!! $NAME: the new process did not come up (unit $(state "$UNIT"))"
  fi
  say "rolling $NAME back to $cur"
  stop_unit "$UNIT"
  python3 "$T/apply.py" --rollback "$SRV" "$BK" | sed 's/^/    /'
  local s; s=$(date -u '+%Y-%m-%d %H:%M:%S'); systemctl start "$UNIT"; sleep 25
  python3 "$T/logcheck.py" "$SRV/server.log" --since "$s" --window 60 > "$BK/after-rollback.json" || true
  python3 "$T/logcheck.py" --compare "$BK/baseline.json" "$BK/after-rollback.json" | sed 's/^/    /' || true
  say "$NAME rolled back to $(version "$SRV"); backup $BK kept"
  return 1
}

for NAME in "${NAMES[@]}"; do
  echo; say "===== $NAME"
  if ! upgrade_one "$NAME"; then say "stopping here: $NAME did not upgrade cleanly; later servers untouched"; exit 1; fi
done
echo; say "all done: ${NAMES[*]} on $TARGET"
