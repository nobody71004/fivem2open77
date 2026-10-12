#!/usr/bin/env bash
# fivem2open77: install converted maps (bundle.tgz: <map> + the map manager f2o77_maps) on an Open77
# server of this host, with the map baker that lets the in-game manager move them, and restart the
# server when nobody is online.  Runs ON the server host.
#   map_deploy.sh [SERVER_NAME]      default open77-xbuniverse under $HOME (systemctl --user)
# Payload next to this script: server_deploy.sh, bundle.tgz, and (to install or update the baker)
# baker_install.sh + baker-toolkit.tgz.
# A map already on the server keeps the spot it was moved to in game: it is rebuilt there after the
# install (MAP_DEPLOY_KEEP_PLACEMENT=0 takes the bundle's spot instead).  MAP_DEPLOY_RESTART=0 never
# restarts (the maps load at the next server start); by default the server restarts when nobody is
# online.  The last line is a JSON summary for the Studio: "MAP-DEPLOY-RESULT {...}".
set -uo pipefail
cd "$(dirname "$0")"
NAME=${1:-open77-xbuniverse}
export SERVERS_ROOT=${SERVERS_ROOT:-$HOME} SYSTEMCTL=${SYSTEMCTL:-"systemctl --user"}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
KEEP=${MAP_DEPLOY_KEEP_PLACEMENT:-1}
RESTART=${MAP_DEPLOY_RESTART:-1}
BAKER=skipped RESTARTED=0 KEPT=()
result() {   # the summary line the Studio reads
  local rc=$1
  printf 'MAP-DEPLOY-RESULT {"rc": %s, "maps": [%s], "baker": "%s", "restarted": %s, "kept": [%s]}\n' "$rc" \
    "$(printf '"%s",' "${MAPS[@]:-}" | sed 's/,$//; s/^""$//')" "$BAKER" "$RESTARTED" \
    "$(printf '"%s",' "${KEPT[@]:-}" | sed 's/,$//; s/^""$//')"
}
MAPS=()
DIR=$SERVERS_ROOT/$NAME
BAKER_HOME=${BAKER_HOME:-$HOME/.f2o77/baker}
echo "== $(hostname) $(date -u +%FT%TZ) server $DIR"
if [ ! -f "$DIR/server.jsonc" ]; then
  echo "no server at $DIR; servers here:"
  ls -d "$SERVERS_ROOT"/open77* 2>/dev/null
  result 2; exit 2
fi
echo "== inspect"
INSPECT=$(bash server_deploy.sh inspect bundle.tgz "$NAME" 2>&1)
echo "$INSPECT"
if ! grep -q '"archivexl"' <<<"$INSPECT" && \
   [ -z "$(find "$DIR/resources" -maxdepth 4 -type d -name archivexl 2>/dev/null | head -1)" ] && \
   ! tar -tzf bundle.tgz | grep -q '^resources/assets/loaders/archivexl/'; then
  echo "!! this server has no archivexl loader resource and the bundle carries none: nothing deployed"
  result 3; exit 3
fi
mapfile -t MAPS < <(tar -tzf bundle.tgz | sed -nE 's#^resources/assets/[^/]+/([^/]+)/layout.json$#\1#p' | sort -u)
MUST=$(tar -tzf bundle.tgz | sed -nE 's#^resources/assets/[^/]+/([^/]+)/open77.lua$#\1#p' | sort -u | paste -sd' ' -)

# where each map stands on the server now (its baked.json), to keep it there
declare -A WAS=()
for m in "${MAPS[@]}"; do
  live=$(find "$DIR/resources" -mindepth 1 -maxdepth 4 -type d -name "$m" 2>/dev/null | head -1)
  [ -n "$live" ] && [ -f "$live/baked.json" ] || continue
  WAS[$m]=$(python3 -c 'import json,sys; a=json.load(open(sys.argv[1])).get("anchor") or {}; print(",".join(str(a[k]) for k in ("x","y","z","yaw")) if a else "")' "$live/baked.json" 2>/dev/null)
done

rc=0
# the baker must not read the manager's folder while the deploy swaps it
SVC="f2o77-mapbaker-$NAME.service"
BAKER_WAS=$($SYSTEMCTL is-active "$SVC" 2>/dev/null || true)
[ "$BAKER_WAS" = "active" ] && $SYSTEMCTL stop "$SVC" 2>/dev/null
echo "== deploy"
bash server_deploy.sh deploy bundle.tgz "$NAME"
drc=$?
if [ $drc != 0 ]; then
  [ "$BAKER_WAS" = "active" ] && $SYSTEMCTL start "$SVC" 2>/dev/null
  echo "!! deploy failed ($drc)"; result "$drc"; exit $drc
fi

# after the deploy, so the baker finds the map manager the moment it starts
if [ -f baker-toolkit.tgz ] && [ -f baker_install.sh ]; then
  echo "== map baker"
  if bash baker_install.sh baker-toolkit.tgz "$DIR" "$NAME.service"; then BAKER=installed
  else echo "!! the map baker did not install (moves in game will wait for it)"; rc=5; BAKER=failed; fi
elif [ "$BAKER_WAS" = "active" ]; then
  $SYSTEMCTL start "$SVC" 2>/dev/null || true
fi

if [ "$KEEP" = 1 ]; then
  for m in "${MAPS[@]}"; do
    old=${WAS[$m]:-}
    [ -n "$old" ] || continue
    live=$(find "$DIR/resources" -mindepth 1 -maxdepth 4 -type d -name "$m" 2>/dev/null | head -1)
    new=$(python3 -c 'import json,sys; a=json.load(open(sys.argv[1])).get("anchor") or {}; print(",".join(str(a[k]) for k in ("x","y","z","yaw")) if a else "")' "$live/baked.json" 2>/dev/null)
    if [ "$old" = "$new" ]; then echo "== $m: stays at $old"; KEPT+=("$m"); continue; fi
    echo "== $m: was moved in game to $old (the bundle says ${new:-not baked}): rebuilding it there"
    if [ -x "$BAKER_HOME/dotnet/dotnet" ]; then
      if out=$(env PYTHONPATH="$BAKER_HOME/lib" DOTNET_ROOT="$BAKER_HOME/dotnet" DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
               DOTNET_CLI_TELEMETRY_OPTOUT=1 python3 -m fivem2open77.mlo.baker bake "$m" --anchor="$old" --in-place \
               --server-dir "$DIR" --wolvenkit "$BAKER_HOME/wolvenkit/WolvenKit.CLI.dll" --dotnet "$BAKER_HOME/dotnet/dotnet" 2>&1); then
        grep -vE '^\s*[{}"\]]' <<<"$out" | tail -3 || true
        KEPT+=("$m")
      else
        tail -5 <<<"$out" | sed 's/^/    /'
        echo "!! could not rebuild $m at $old: it stands at the bundle's spot"; rc=6
      fi
    else
      echo "!! no map baker on this host: $m stands at the bundle's spot"; rc=6
    fi
  done
fi

echo "== restart"
WHO=$(bash server_deploy.sh status - "$NAME" --json 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("online") or []))' 2>/dev/null || echo "?")
if [ "$RESTART" != 1 ]; then
  echo "not restarting (asked not to): the maps load at the next server start"
elif [ "$WHO" = "0" ]; then
  if F2O77_MUST="$MUST" bash server_deploy.sh restart - "$NAME"; then RESTARTED=1; else rc=1; fi
else
  echo "players online ($WHO): not restarting; the maps load at the next server start"
fi
echo "== status"
bash server_deploy.sh status - "$NAME"
echo "== server.log (map lines)"
grep -n -E "$(echo "$MUST" | tr ' ' '|')|archivexl|Required mods|listening on UDP" "$DIR/server.log" | tail -30
if command -v journalctl >/dev/null 2>&1; then
  echo "== map baker log"
  JU=""; [[ "$SYSTEMCTL" == *--user* ]] && JU="--user"
  journalctl $JU -u "f2o77-mapbaker-$NAME.service" -n 8 --no-pager 2>/dev/null | sed 's/^/    /' || true
fi
result $rc
exit $rc
