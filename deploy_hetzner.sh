#!/bin/zsh
# Push the tile pyramid + map3d viewer to the production VPS (spec F4,
# Hetzner variant — fully online production, bulk archive stays home).
# Prereq: DEPLOY_HETZNER.md (server, SSH key, Caddy). Idempotent, resumable.
#
#   ./deploy_hetzner.sh deploy@mapa.example.com
set -e
HOST=${1:?usage: ./deploy_hetzner.sh user@host}
SRC=/Volumes/Elements/cuzk-pyramid
RSYNC=(rsync -a --info=progress2 --exclude '*.tmp' --exclude '*.log')

for layer in dmpok ortho; do
  echo "== sync $layer =="
  "${RSYNC[@]}" "$SRC/$layer/" "$HOST:/srv/tiles/v1/$layer/"
done

# Viewer — bake the versioned tile base in (no ?tiles= param needed in prod).
TMP=$(mktemp -t map3d-index)
sed "s|P.get('tiles') ?? '/cuzk-pyramid'|P.get('tiles') ?? '/v1'|" \
  /Users/jan/projekty/inzerator/map3d/index.html > "$TMP"
grep -q "?? '/v1'" "$TMP" || { echo "!! TILE_BASE bake failed"; exit 1; }
"${RSYNC[@]}" "$TMP" "$HOST:/srv/map3d/index.html"
rm -f "$TMP"

echo "done. https://<domain>/  (Caddy serves /srv/map3d + /srv/tiles)"
