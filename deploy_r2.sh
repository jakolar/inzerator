#!/bin/zsh
# Sync the tile pyramid + map3d viewer to Cloudflare R2 (spec F4).
# Prereq: rclone configured per DEPLOY_R2.md. Idempotent, resumable.
#
#   ./deploy_r2.sh                     # remote r2:inzerator-tiles/v1
#   ./deploy_r2.sh r2:mybucket/v2      # custom remote/version
set -e
REMOTE=${1:-r2:inzerator-tiles/v1}
SRC=/Volumes/Elements/cuzk-pyramid
IMMUTABLE='Cache-Control: public, max-age=31536000, immutable'

# Tiles are immutable (version in the path) → cache forever, size-only sync
# (checksum listing of millions of objects is slow and costs Class A ops).
for layer in dmpok ortho; do
  echo "== sync $layer =="
  rclone sync "$SRC/$layer" "$REMOTE/$layer" \
    --transfers 32 --checkers 32 --fast-list --size-only \
    --exclude '*.tmp' --exclude '*.log' \
    --header-upload "$IMMUTABLE" --stats 60s
done

# Viewer — short cache so fixes propagate.
rclone copyto /Users/jan/projekty/inzerator/map3d/index.html \
  "${REMOTE%/*}/index.html" \
  --header-upload 'Cache-Control: public, max-age=300'

echo "done. Viewer: https://<custom-domain>/index.html?tiles=https://<custom-domain>/${REMOTE#*/}"
