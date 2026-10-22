#!/usr/bin/env bash
# fivem2open77: update the map manager (f2o77_maps) of an Open77 server on this host -- its code and
# the maps' wireframes (`mlo-manager-bundle`) -- without a restart: scripts and the page reload by
# themselves.  Runs ON the server host; server_deploy.sh and update.tgz come with it.
#   manager_update.sh [SERVER_NAME]       default open77-xbuniverse under $HOME
set -uo pipefail
cd "$(dirname "$0")"
NAME=${1:-open77-xbuniverse}
export SERVERS_ROOT=${SERVERS_ROOT:-$HOME} SYSTEMCTL=${SYSTEMCTL:-"systemctl --user"}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
echo "== $(hostname) $(date -u +%FT%TZ) server $SERVERS_ROOT/$NAME: map manager update"
if [ -f update.sha256 ]; then
  sha256sum -c update.sha256 >/dev/null 2>&1 || { echo "update.tgz is damaged (sha256)"; echo 'MANAGER-UPDATE-RESULT {"rc": 4}'; exit 4; }
fi
bash server_deploy.sh manager-update update.tgz "$NAME"
rc=$?
echo "MANAGER-UPDATE-RESULT {\"rc\": $rc}"
exit $rc
