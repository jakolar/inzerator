# Deploy produkce na Hetzner VPS (F4)

## Stav (2026-07-03)

**Deploy ODLOŽEN** — rozhodnutí uživatele: nejdřív doběhne F3 chain
(pre-bake vysokých zoomů) a doladí se kvalita mapy; VPS se objedná až pak.
Kroky níže zůstávají jako fronta práce, nic z nich teď nespouštět.

- [x] Rozhodnutí: produkce plně online na Hetzner VPS, archiv doma
  (amendment D5 ve spec `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md`)
- [x] Deploy tooling hotové: `deploy_hetzner.sh`, `deploy/Caddyfile`, tento runbook
- [x] Pyramida KOMPLETNÍ (2026-07-07): výšky i ortho z=8..16 celá ČR
  + z=17/18 populated, border backfill hotový. **265 GB / 11,0 M dlaždic**
  (dmpok 172 GB / 5 489 281 LERC; ortho 93 GB / 5 503 455 JPEG).
- [ ] Objednat VPS (CX32 + 300GB Volume, Ubuntu 24.04, Falkenstein/Norimberk)
- [ ] DNS A záznam domény → IP serveru
- [ ] Setup serveru (blok níže)
- [ ] První push: `./deploy_hetzner.sh deploy@<host>`
- [ ] Finální sync po doběhnutí F3 řetězu

Topologie: **produkce plně online** (kompletně předpečená pyramida + viewer
na VPS), **archiv doma** (2 TB bulk na Elements — z něj se peče; na server
se nikdy nekopíruje). Mac po dopočtu F3 jen pushuje delty přes rsync.

## Volba serveru

Finální pyramida měří **265 GB** (změřeno 2026-07-07 po doběhnutí F3 +
border backfillu) — původní odhad 10–40 GB a CX32/160 GB NVMe nestačí.

**CX32 (4 vCPU, 8 GB, ~€7,6/měs) + 300 GB Volume (~€14/měs) ≈ €22/měs** —
doporučeno. Volume lze zvětšovat za běhu (z=19/20 populated, KTX2 vedle
JPEGů); statika sype z disku, výkon Volume (SSD, síťový) na tile serving
bohatě stačí. Alternativa CX52 (360 GB NVMe, ~€32/měs) je dražší a strop
360 GB je blízko — další vrstva by stejně vynutila Volume. Object Storage
netřeba — VPS obslouží statiku levněji a s vlastní doménou bez CDN
mezikroku.

Latence Falkenstein/Norimberk → ČR ~20–30 ms; CDN pro českou audienci
neřešíme.

## Jednorázový setup serveru (Ubuntu 24.04)

```bash
# 1. DNS: A záznam mapa.example.com → IP serveru

# 2. Na serveru:
apt update && apt install -y caddy rsync
adduser --disabled-password deploy
mkdir -p /srv/tiles/v1/{dmpok,ortho} /srv/map3d
chown -R deploy:deploy /srv/tiles /srv/map3d

# 3. SSH klíč Macu do /home/deploy/.ssh/authorized_keys

# 4. Caddyfile: zkopírovat deploy/Caddyfile do /etc/caddy/Caddyfile,
#    nahradit doménu, pak:
systemctl reload caddy
```

## Upload / update (z Macu)

```bash
./deploy_hetzner.sh deploy@mapa.example.com
```

- rsync = delta sync. První plný push je **265 GB v 11 M souborech** — na
  100Mbit uploadu ~6–8 h čistého přenosu, ale metadata scan 11 M souborů
  je znát: pouštět po vrstvách/úrovních (`rsync .../dmpok/`, pak
  `.../ortho/16/`, …), ať jde přerušit a navázat. Následné delta syncy
  jsou levné.
- Viewer se nahrává se zapečenou tile base `/v1` — produkce nepotřebuje
  žádné URL parametry.

## Ověření

```bash
curl -sI https://mapa.example.com/v1/dmpok/14/8978/5583.lerc | head -3
# → 200 + cache-control: immutable
curl -sI https://mapa.example.com/ | head -3
# → 200, max-age=300
```

## Co v produkci NENÍ (záměrně)

- **On-demand build** — vyžaduje 2 TB bulk; ten je doma. Nepředpečená
  dlaždice = klient zobrazí rodiče (replacement refinement, žádná díra).
  Po F3 se to týká jen neobydlených z=17..18.
- **WMS ortho fallback a `/api/*`** — viewer je na CDN/VPS čistá statika;
  fallback řetěz končí hypsometrií. Až bude potřeba vyhledávání adres,
  přidá se malý backend (server.py umí běžet i bez bulk dat) — VPS na to
  má výkon; zatím YAGNI.
- **R2 varianta** zůstává v `deploy_r2.sh` + `DEPLOY_R2.md` jako plan B
  (kdyby návštěvnost přerostla VPS, statika se přesune jedním rclone sync).
