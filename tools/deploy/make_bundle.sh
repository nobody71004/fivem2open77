#!/usr/bin/env bash
# Pack converted car resources (+ the loader resources they depend on) into a deploy bundle:
#   make_bundle.sh OUT.tgz LOADERS_DIR CAR_DIR [CAR_DIR...]
# LOADERS_DIR holds archivexl/ and tweakxl/ resource folders; each CAR_DIR is a converter output
# folder (<name>/ or <name>_chime/).  Layout inside: resources/assets/{loaders,vehicles}/<name>/.
set -euo pipefail
OUT=$1; LOADERS=$2; shift 2
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/resources/assets/loaders" "$T/resources/assets/vehicles"
for l in archivexl tweakxl; do
  [ -f "$LOADERS/$l/open77.lua" ] || { echo "missing loader resource $LOADERS/$l" >&2; exit 2; }
  cp -a "$LOADERS/$l" "$T/resources/assets/loaders/$l"
done
for c in "$@"; do
  [ -f "$c/open77.lua" ] || { echo "$c is not a resource folder" >&2; exit 2; }
  cp -a "$c" "$T/resources/assets/vehicles/$(basename "$c")"
done
find "$T" -name '__pycache__' -prune -exec rm -rf {} +
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01 00:00:00' -czf "$OUT" -C "$T" resources
echo "$OUT: $(du -h "$OUT" | cut -f1), sha256 $(sha256sum "$OUT" | cut -c1-16)"
(cd "$T" && find resources -mindepth 3 -maxdepth 3 -type d | sort | sed 's/^/  /')
