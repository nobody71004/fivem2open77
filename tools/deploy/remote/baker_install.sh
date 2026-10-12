#!/usr/bin/env bash
# fivem2open77: install or update the map baker next to an Open77 server on this host.  Runs ON the
# server host (map_deploy.sh calls it), as the user that runs the server.
#
#   baker_install.sh TOOLKIT.tgz SERVER_DIR [UNIT]
#
#   TOOLKIT.tgz  toolkit/{VERSION, dotnet/, wolvenkit/, lib/fivem2open77/}  (mlo-baker-toolkit); a
#                light one (VERSION + lib only) updates the baker's package when the host already
#                has that VERSION of the runtime and WolvenKit
#   SERVER_DIR   the Open77 server folder (server.jsonc, resources/)
#   UNIT         the server's systemd unit (default <basename SERVER_DIR>.service)
#
# Installs:
#   $BAKER_HOME (~/.f2o77/baker)    the .NET runtime, WolvenKit CLI and the baker's Python package
#   f2o77-mapbaker-<server>.service the baker: answers the in-game map manager's requests
#   <unit>.d/f2o77-maps.conf        the start hook: `baker promote` before every server start
#                                   (ExecStartPre), and a restart when Open77 exits with code 10
#                                   (its scheduled restart) whatever the unit's Restart= says
#   SERVER_DIR/f2o77-maps/hook.json where the hook is (the baker reports it to the manager)
# SYSTEMCTL (default "systemctl --user") picks user or system units.  Prints no secrets.
set -euo pipefail
TOOLKIT=${1:?toolkit.tgz}; SERVER_DIR=${2:?server dir}
SERVER_DIR=$(cd "$SERVER_DIR" && pwd)
NAME=$(basename "$SERVER_DIR")
UNIT=${3:-$NAME.service}
SYSTEMCTL=${SYSTEMCTL:-"systemctl --user"}
BAKER_HOME=${BAKER_HOME:-$HOME/.f2o77/baker}
say() { echo "[baker $(date -u +%H:%M:%S)] $*"; }

[ -d "$SERVER_DIR/resources" ] || { say "!! $SERVER_DIR is not an Open77 server folder (no resources/)"; exit 2; }
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { say "!! odd server folder name $NAME"; exit 2; }
ARCH=$(uname -m)
[ "$ARCH" = "x86_64" ] || { say "!! the toolkit is for x86_64 Linux, this host is $ARCH"; exit 2; }
PY=$(command -v python3 || true)
[ -n "$PY" ] || { say "!! python3 is missing (apt install python3)"; exit 2; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || { say "!! python3 >= 3.8 needed, found $("$PY" -V 2>&1)"; exit 2; }

# ------------------------------------------------------------------ the toolkit
# toolkit/VERSION names the heavy part (runtime + WolvenKit): unpacked only when it changes; the
# baker's Python package (toolkit/lib, small) is replaced every time.
WANT=$(tar -xzOf "$TOOLKIT" toolkit/VERSION 2>/dev/null | head -1)
[ -n "$WANT" ] || { say "!! $TOOLKIT has no toolkit/VERSION"; exit 2; }
HAVE=$(head -1 "$BAKER_HOME/VERSION" 2>/dev/null || true)
if [ "$WANT" != "$HAVE" ] && ! tar -tzf "$TOOLKIT" toolkit/dotnet/dotnet >/dev/null 2>&1; then
  say "!! this toolkit carries only the baker package (for $WANT) and this host has ${HAVE:-no toolkit}: send the full toolkit"
  exit 6
fi
SVC="f2o77-mapbaker-$NAME.service"
mkdir -p "$(dirname "$BAKER_HOME")"
# every server's baker on this host uses the toolkit: stop them for the swap
mapfile -t RUNNING < <($SYSTEMCTL list-units --type=service --state=active --no-legend 'f2o77-mapbaker-*' 2>/dev/null \
                        | awk '{print $1}' | grep '^f2o77-mapbaker-' || true)
for u in "${RUNNING[@]}"; do $SYSTEMCTL stop "$u" || true; done
if [ "$WANT" != "$HAVE" ]; then
  say "installing toolkit $WANT into $BAKER_HOME (had: ${HAVE:-none})"
  rm -rf "$BAKER_HOME.new"; mkdir -p "$BAKER_HOME.new"
  tar -xzf "$TOOLKIT" -C "$BAKER_HOME.new" --strip-components=1
  chmod +x "$BAKER_HOME.new/dotnet/dotnet"
  rm -rf "$BAKER_HOME.old"
  if [ -d "$BAKER_HOME" ]; then mv "$BAKER_HOME" "$BAKER_HOME.old"; fi
  mv "$BAKER_HOME.new" "$BAKER_HOME"
  rm -rf "$BAKER_HOME.old"
else
  say "toolkit $WANT already installed; updating the baker package"
  rm -rf "$BAKER_HOME/lib.new"; mkdir -p "$BAKER_HOME/lib.new"
  tar -xzf "$TOOLKIT" -C "$BAKER_HOME/lib.new" --strip-components=2 toolkit/lib
  rm -rf "$BAKER_HOME/lib.old"
  if [ -d "$BAKER_HOME/lib" ]; then mv "$BAKER_HOME/lib" "$BAKER_HOME/lib.old"; fi
  mv "$BAKER_HOME/lib.new" "$BAKER_HOME/lib"
  rm -rf "$BAKER_HOME/lib.old"
fi
for u in "${RUNNING[@]}"; do [ "$u" = "$SVC" ] || $SYSTEMCTL start "$u" || true; done

ENVS=(PYTHONPATH="$BAKER_HOME/lib" DOTNET_ROOT="$BAKER_HOME/dotnet" DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
      DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 PYTHONUNBUFFERED=1)
# the runtime and WolvenKit must run here (a missing system library shows up now, not at the first move)
if ! WKV=$(env "${ENVS[@]}" "$BAKER_HOME/dotnet/dotnet" "$BAKER_HOME/wolvenkit/WolvenKit.CLI.dll" --version 2>&1 | tail -3); then
  say "!! WolvenKit does not run on this host:"; echo "$WKV" | sed 's/^/    /'; exit 3
fi
say "WolvenKit: $(echo "$WKV" | tr '\n' ' ' | cut -c1-120)"
env "${ENVS[@]}" "$PY" -m fivem2open77.mlo.baker status --server-dir "$SERVER_DIR" > /dev/null \
  || { say "!! the baker's Python package does not load with $("$PY" -V 2>&1)"; exit 3; }

# ------------------------------------------------------------------ units
if [[ "$SYSTEMCTL" == *--user* ]]; then
  UNITDIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
else
  UNITDIR=/etc/systemd/system
fi
mkdir -p "$UNITDIR"
cat > "$UNITDIR/$SVC.f2o77-tmp" <<UNIT
# fivem2open77 map baker for $NAME: builds a map's sector package when the in-game map manager
# (f2o77_maps) asks for a move.  Installed by baker_install.sh; see fivem2open77/mlo/baker.py.
[Unit]
Description=fivem2open77 map baker ($NAME)
After=network.target

[Service]
Type=simple
Environment=PYTHONPATH=$BAKER_HOME/lib
Environment=DOTNET_ROOT=$BAKER_HOME/dotnet
Environment=DOTNET_CLI_TELEMETRY_OPTOUT=1
Environment=DOTNET_NOLOGO=1
Environment=DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m fivem2open77.mlo.baker run --server-dir $SERVER_DIR --wolvenkit $BAKER_HOME/wolvenkit/WolvenKit.CLI.dll --dotnet $BAKER_HOME/dotnet/dotnet
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
UNIT
mv "$UNITDIR/$SVC.f2o77-tmp" "$UNITDIR/$SVC"

HOOK_DIR="$UNITDIR/$UNIT.d"
HOOK="$HOOK_DIR/f2o77-maps.conf"
HOOK_OK=0
if mkdir -p "$HOOK_DIR" 2>/dev/null && cat > "$HOOK.f2o77-tmp" 2>/dev/null <<DROPIN
# fivem2open77 map baker: install the map builds the in-game map manager staged, before the server
# starts; and restart the server when Open77 exits for a scheduled restart (exit code 10) even if
# the unit's Restart= would not.  Installed by baker_install.sh.
[Service]
ExecStartPre=-/usr/bin/env PYTHONPATH=$BAKER_HOME/lib PYTHONUNBUFFERED=1 $PY -m fivem2open77.mlo.baker promote --server-dir $SERVER_DIR
RestartForceExitStatus=10
DROPIN
then
  mv "$HOOK.f2o77-tmp" "$HOOK" && HOOK_OK=1
fi
$SYSTEMCTL daemon-reload
mkdir -p "$SERVER_DIR/f2o77-maps"
if [ $HOOK_OK = 1 ]; then
  printf '{"unit": "%s", "dropIn": "%s", "installedAt": %s, "systemctl": "%s"}\n' "$UNIT" "$HOOK" "$(date +%s)" \
    "$SYSTEMCTL" > "$SERVER_DIR/f2o77-maps/hook.json"
  say "start hook: $HOOK"
else
  rm -f "$SERVER_DIR/f2o77-maps/hook.json"
  say "!! could not write $HOOK (no permission?): moves are installed just before the restart instead"
fi
RESTART_POLICY=$($SYSTEMCTL show -p Restart --value "$UNIT" 2>/dev/null || true)
say "server unit $UNIT: Restart=${RESTART_POLICY:-?} (+ RestartForceExitStatus=10 from the hook)"

$SYSTEMCTL enable "$SVC" >/dev/null 2>&1 || true
$SYSTEMCTL restart "$SVC"
for _ in $(seq 1 20); do
  sleep 1
  [ "$($SYSTEMCTL is-active "$SVC" 2>/dev/null || true)" = "active" ] && break
done
if [ "$($SYSTEMCTL is-active "$SVC" 2>/dev/null || true)" != "active" ]; then
  say "!! $SVC is not running:"; $SYSTEMCTL status "$SVC" --no-pager 2>&1 | tail -12 | sed 's/^/    /'; exit 4
fi
say "$SVC: active"
