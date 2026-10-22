#!/usr/bin/env bash
# fivem2open77: give a player an ACL right on an Open77 server of this host -- by default command.maps,
# which the in-game map manager (F9) needs -- and restart the server when nobody is online (it reads
# its ACL file at start).  Runs ON the server host; server_deploy.sh comes with it.
#   acl_grant.sh [SERVER_NAME] [PERMISSION]     default open77-xbuniverse under $HOME, command.maps
# F2O77_ACL_PRINCIPAL: the start of the principal's name in the ACL file (any case); without it, or
# without a match, the file's only principal.  Prints the principals (names, roles, counts; no keys).
set -uo pipefail
cd "$(dirname "$0")"
NAME=${1:-open77-xbuniverse}
PERM=${2:-command.maps}
export SERVERS_ROOT=${SERVERS_ROOT:-$HOME} SYSTEMCTL=${SYSTEMCTL:-"systemctl --user"}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
echo "== $(hostname) $(date -u +%FT%TZ) server $SERVERS_ROOT/$NAME: $PERM for ${F2O77_ACL_PRINCIPAL:-the only principal}"
bash server_deploy.sh acl-grant "$PERM" "$NAME" --restart
rc=$?
echo "ACL-GRANT-RESULT {\"rc\": $rc}"
exit $rc
