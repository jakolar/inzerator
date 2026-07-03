#!/bin/zsh
# Sync the tile pyramid + map3d viewer to Cloudflare R2 (spec F4).
# Prereq: rclone configured per DEPLOY_R2.md. Idempotent, resumable.
#
#   ./deploy_r2.sh                     # remote r2:inzerator-tiles/v1
#   ./deploy_r2.sh r2:mybucket/v2      # custom remote/version
set -e
REMOTE=${1:-r2:inzerator-tiles/v1}
BUCKET=${REMOTE%%/*}
VERSION=/${REMOTE#*/}
SRC=/Volumes/Elements/cuzk-pyramid
IMMUTABLE='Cache-Control: public, max-age=31536000, immutable'

# Preflight: credentials filled in, remote has a version path, bucket exists.
if grep -q VYPLN ~/.config/rclone/rclone.conf; then
  echo "!! ~/.config/rclone/rclone.conf má stále placeholder hodnoty (VYPLN_*)"
  exit 1
fi
if [[ "$REMOTE" != */* ]]; then
  echo "!! REMOTE musí obsahovat verzi v cestě (r2:bucket/v1)"; exit 1
fi
rclone mkdir "${BUCKET}"   # idempotent

# Tiles are immutable (version in the path) → cache forever, size-only sync
# (checksum listing of millions of objects is slow and costs Class A ops).
for layer in dmpok ortho; do
  echo "== sync $layer =="
  rclone sync "$SRC/$layer" "$REMOTE/$layer" \
    --transfers 32 --checkers 32 --fast-list --size-only \
    --exclude '*.tmp' --exclude '*.log' \
    --header-upload "$IMMUTABLE" --stats 60s
done

# Viewer — bake the versioned tile base in (so no ?tiles= param is needed)
# and upload with a short cache so fixes propagate.
TMP=$(mktemp -t map3d-index)
sed "s|P.get('tiles') ?? '/cuzk-pyramid'|P.get('tiles') ?? '${VERSION}'|" \
  /Users/jan/projekty/inzerator/map3d/index.html > "$TMP"
grep -q "?? '${VERSION}'" "$TMP" || { echo "!! TILE_BASE bake failed"; exit 1; }
rclone copyto "$TMP" "${BUCKET}/index.html" \
  --header-upload 'Cache-Control: public, max-age=300'
rm -f "$TMP"

echo "done. Otevři: https://<custom-domain>/index.html"
