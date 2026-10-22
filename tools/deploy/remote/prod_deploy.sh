#!/usr/bin/env bash
# fivem2open77: put converted maps and cars on Open77 servers of this host in one go.  Runs ON the
# server host, as the user that runs the servers.
#   prod_deploy.sh [NAME...]     default: every server named in SERVER_NAMES (space separated)
# Payload next to this script: server_deploy.sh, map_deploy.sh, baker_install.sh, baker-toolkit.tgz,
# bundle.tgz (maps + the map manager, mlo/deploy.py bundles merged), cars.tgz (studio make_bundle)
# and/or world.tgz (world edits, world/package.py bundle).  Per server: the cars and the world
# edits are installed first (no restart of their own), then the maps,
# the map manager and the map baker (map_deploy.sh: a map already there keeps the spot it was moved
# to in game); then the server restarts once, only when nobody is online -- otherwise everything
# loads at its next restart.  SERVERS_ROOT (default /opt) and SYSTEMCTL (default systemctl) as in
# server_deploy.sh.  The last line: "PROD-DEPLOY-RESULT {...}".  Prints no secrets.
set -uo pipefail
cd "$(dirname "$0")"
export SERVERS_ROOT=${SERVERS_ROOT:-/opt} SYSTEMCTL=${SYSTEMCTL:-systemctl}
NAMES=("$@")
[ ${#NAMES[@]} -gt 0 ] || read -r -a NAMES <<<"${SERVER_NAMES:-}"
[ ${#NAMES[@]} -gt 0 ] || { echo "!! no server names"; exit 2; }
RC=0
SUMMARY=()
for NAME in "${NAMES[@]}"; do
  echo "################################ $NAME  $(date -u +%FT%TZ)"
  if [ ! -f "$SERVERS_ROOT/$NAME/server.jsonc" ]; then
    echo "!! no Open77 server at $SERVERS_ROOT/$NAME"; RC=2; SUMMARY+=("\"$NAME\": \"missing\""); continue
  fi
  cars=skipped maps=skipped worlds=skipped
  if [ -f cars.tgz ]; then
    echo "== cars"
    if bash server_deploy.sh deploy cars.tgz "$NAME"; then cars=ok; else cars=failed; RC=1; echo "!! the cars did not install on $NAME"; fi
  fi
  if [ -f world.tgz ]; then
    echo "== world edits"
    if bash server_deploy.sh deploy world.tgz "$NAME"; then worlds=ok; else worlds=failed; RC=1; echo "!! the world edits did not install on $NAME"; fi
  fi
  if [ -f bundle.tgz ]; then
    echo "== maps"
    MAP_DEPLOY_KEEP_PLACEMENT=${MAP_DEPLOY_KEEP_PLACEMENT:-1} MAP_DEPLOY_RESTART=${MAP_DEPLOY_RESTART:-1} bash map_deploy.sh "$NAME"
    m=$?
    case $m in 0) maps=ok ;; 5|6) maps="partly ($m)"; RC=1 ;; *) maps="failed ($m)"; RC=1 ;; esac
  elif { [ "$cars" = ok ] || [ "$worlds" = ok ]; } && [ "${MAP_DEPLOY_RESTART:-1}" = 1 ]; then
    bash server_deploy.sh restart - "$NAME" || true
  fi
  if [ "$maps" = skipped ]; then                 # (map_deploy.sh prints the status itself)
    echo "== status"
    bash server_deploy.sh status - "$NAME" || true
  fi
  SUMMARY+=("\"$NAME\": {\"cars\": \"$cars\", \"maps\": \"$maps\", \"worlds\": \"$worlds\"}")
done
printf 'PROD-DEPLOY-RESULT {"rc": %s, %s}\n' "$RC" "$(IFS=,; echo "${SUMMARY[*]}")"
exit $RC
