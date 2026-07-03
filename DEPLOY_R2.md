# Deploy pyramidy na Cloudflare R2 (F4)

Operační manuál pro nasazení ČR 3D mapy — statické dlaždice + viewer na R2,
per spec `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md` (D5).

## Jednorázový setup

1. **rclone**: `brew install rclone`
2. **R2 API token**: Cloudflare dashboard → R2 → Manage R2 API Tokens →
   Create (Object Read & Write). Poznamenat Access Key ID + Secret +
   account ID.
3. **rclone config** — do `~/.config/rclone/rclone.conf` přidat:

   ```ini
   [r2]
   type = s3
   provider = Cloudflare
   access_key_id = <ACCESS_KEY_ID>
   secret_access_key = <SECRET>
   endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
   ```

4. **Bucket**: `rclone mkdir r2:inzerator-tiles`
5. **Public access**: dashboard → bucket → Settings → Custom Domains →
   připojit doménu (např. `tiles.example.com`). R2.dev subdoména jde taky,
   ale bez CF cache — custom doména dostane CDN zdarma.
6. **CORS** (jen pokud viewer poběží na jiném originu než dlaždice):
   bucket → Settings → CORS policy → `{"AllowedOrigins": ["*"],
   "AllowedMethods": ["GET"]}`.

## Upload / update

```bash
./deploy_r2.sh                      # sync dmpok + ortho + viewer do r2:inzerator-tiles/v1
```

- Dlaždice jsou immutable (verze v cestě) → `Cache-Control: max-age=1y,
  immutable`, sync `--size-only` (checksum listing milionů objektů je pomalý
  a platí se Class A ops).
- První plný sync: ~$4,5/milion PUT (Class A). Odhad pro plnou pyramidu
  (z=8..18 populated): ~5M objektů ≈ $22 jednorázově, poté jen delty.
- Storage: $0,015/GB/měs. Egress: $0.

## Ověření

```bash
curl -sI https://tiles.example.com/v1/dmpok/14/8978/5583.lerc | head -5
# → 200, cache-control: public, max-age=31536000, immutable
```

Viewer: `https://tiles.example.com/index.html?tiles=https://tiles.example.com/v1`

## Co na R2 NENÍ

- **On-demand build vysokých zoomů** — na CDN dlaždice buď je, nebo klient
  zobrazí rodiče (replacement refinement to řeší bez děr). Mac origin za
  CF Tunnel jde přidat později jako fallback, zatím YAGNI.
- **WMS ortho fallback** (`/proxy/ortofoto`) — vyžaduje lokální server;
  na CDN deployi request selže a viewer spadne do hypsometrie. Po dokončení
  F3 ortho pre-baku je to bezpředmětné.
- **Manifest** — viewer má konstanty inline; `manifest.json` přibude, až
  bude víc vrstev/tierů (spec kap. 7).
