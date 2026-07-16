# Deploy produkce na Hetzner VPS (F4)

## Stav (2026-07-03)

**Deploy ODLOŽEN** — rozhodnutí uživatele: nejdřív doběhne F3 chain
(pre-bake vysokých zoomů) a doladí se kvalita mapy; VPS se objedná až pak.
Kroky níže zůstávají jako fronta práce, nic z nich teď nespouštět.

- [x] Rozhodnutí: produkce plně online na Hetzner VPS, archiv doma
  (amendment D5 ve spec `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md`)
- [x] Deploy tooling hotové: `deploy_hetzner.sh`, `deploy/Caddyfile`, tento runbook
- [x] Pyramida KOMPLETNÍ full-ČR (2026-07-16): výšky i ortho **z=8..18 celá
  ČR** (ne jen populated) — z=18 @2x (512^2, 0,20 m/px), z=17 re-agg z z=18.
  Poslední řetěz (`full_cr_chain.sh`, 4 stage, doběhl 2026-07-16 06:37, 0 chyb):
  ortho z=18 base 13 974 454 enumerováno / 4 537 022 zapsáno JPEG (zbytek
  skip + prázdný oceán/hranice), ortho z=17 re-agg 2 081 521; výšky z=18 base
  + z=17 re-agg. **888 GB celkem** (změřeno 2026-07-16).
- [ ] Objednat VPS (CX32 + 500GB Volume, Ubuntu 24.04, Falkenstein/Norimberk)
- [ ] DNS A záznam domény → IP serveru
- [ ] Setup serveru (blok níže)
- [ ] První push: `./deploy_hetzner.sh deploy@<host>`
- [ ] Finální sync po doběhnutí F3 řetězu

Topologie: **produkce plně online** (kompletně předpečená pyramida + viewer
na VPS), **archiv doma** (2 TB bulk na Elements — z něj se peče; na server
se nikdy nekopíruje). Mac po dopočtu F3 jen pushuje delty přes rsync.

## Volba serveru

Finální pyramida měří **888 GB** (změřeno 2026-07-16 po full-ČR z=18/z=17
passu — celá ČR, ne jen populated) — původní odhad 10–40 GB a CX32/160 GB
NVMe nestačí; i 500 GB Volume je teď málo, viz níže.

**CX32 (4 vCPU, 8 GB, ~€7,6/měs) + 1 TB Volume (~€48/měs) ≈ €56/měs** —
doporučeno. Volume lze zvětšovat za běhu (z=19/20 populated, KTX2 vedle
JPEGů); statika sype z disku, výkon Volume (SSD, síťový) na tile serving
bohatě stačí. Alternativa CX52 (360 GB NVMe) už kapacitně nestačí vůbec.
Object Storage netřeba — VPS obslouží statiku levněji a s vlastní doménou
bez CDN mezikroku.

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

- rsync = delta sync. První plný push je **888 GB (full ČR z=8..18)** — na
  100Mbit uploadu ~20–24 h čistého přenosu, a metadata scan desítek milionů
  souborů je znát: pouštět po vrstvách/úrovních (`rsync .../dmpok/`, pak
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
