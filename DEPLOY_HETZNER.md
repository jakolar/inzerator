# Deploy produkce na Hetzner VPS (F4)

## Stav (2026-07-17)

**Deploy ZAHÁJEN** — pyramida je kompletní (full-ČR z=8..18, 888 GB),
čeká se na objednávku VPS uživatelem (krok 1 níže). Rozhodnuto: nasazuje se
i **backend** (`server.py` za Caddy reverse proxy) — search, katastr, OSM
vrstva a klik na parcely fungují i v produkci; server.py na VPS běží bez
bulk dat (jen ČÚZK REST + upstream proxy + disk cache).

## Krok 1: Objednávka VPS (uživatel, ~10 min)

1. Účet: <https://console.hetzner.com> — registrace (e-mail + karta;
   první objednávka může chtít ověření identity).
2. Nový projekt (např. „mapa") → **Add Server**:
   - Location: `Falkenstein` nebo `Nuremberg` (~20–30 ms do ČR)
   - Image: `Ubuntu 24.04`
   - Type: Shared vCPU x86 → **CX32** (4 vCPU, 8 GB) — ~€7,6/měs
   - Networking: IPv4 + IPv6 (default)
   - SSH keys → Add SSH key → vložit veřejný klíč Macu (celý řádek):
     ```
     ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFQBczqxwbMthspFCXbdXKr9MPannPVNzUyVDNTV0KAY alacremex@gmail.com
     ```
   - Volumes → Create Volume: **1 TB** (1024 GB), připojit k serveru — ~€48/měs
   - Celkem ≈ **€56/měs** → Create & Buy now
3. Z konzole opsat **IP adresu serveru** a předat Claudovi.

Doména není potřeba hned — start na IP, doména se doplní kdykoli později
(Caddy si pak sám vyřídí Let's Encrypt certifikát).

## Krok 2: Setup + upload (Claude, po obdržení IP)

1. Setup přes SSH: Caddy, deploy user, adresáře, mount 1TB Volume, firewall.
2. Backend: `server.py` jako systemd služba za Caddy reverse proxy
   (`/api/*`, `/proxy/*`).
3. Upload dat: 888 GB / ~9 M souborů po vrstvách a zoomech
   (`deploy_hetzner.sh`, resumable) — ~1–2 dny dle uploadu.
4. Ověření: dlaždice, viewer, API endpointy, cache hlavičky.

- [x] Rozhodnutí: produkce plně online na Hetzner VPS, archiv doma
  (amendment D5 ve spec `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md`)
- [x] Deploy tooling hotové: `deploy_hetzner.sh`, `deploy/Caddyfile`, tento runbook
- [x] Pyramida KOMPLETNÍ full-ČR (2026-07-16): výšky i ortho **z=8..18 celá
  ČR** (ne jen populated) — z=18 @2x (512^2, 0,20 m/px), z=17 re-agg z z=18.
  Poslední řetěz (`full_cr_chain.sh`, 4 stage, doběhl 2026-07-16 06:37, 0 chyb):
  ortho z=18 base 13 974 454 enumerováno / 4 537 022 zapsáno JPEG (zbytek
  skip + prázdný oceán/hranice), ortho z=17 re-agg 2 081 521; výšky z=18 base
  + z=17 re-agg. **888 GB celkem** (změřeno 2026-07-16).
- [ ] Objednat VPS (CX32 + **1TB** Volume, Ubuntu 24.04, Falkenstein/Norimberk)
  — viz Krok 1 výše
- [ ] Setup serveru vč. systemd `server.py` + Caddy reverse proxy (blok níže)
- [ ] První push: `./deploy_hetzner.sh deploy@<host>` (888 GB, resumable)
- [ ] Doména + DNS A záznam → IP (kdykoli později; do té doby IP)

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
- ~~WMS ortho fallback a `/api/*`~~ — **ZMĚNA 2026-07-17**: backend SE
  nasazuje. `server.py` běží na VPS jako systemd služba za Caddy reverse
  proxy (`/api/*`, `/proxy/*`) — search, klik na parcely/budovy, katastr
  a OSM vrstva fungují i v produkci. Bez bulk dat: jen ČÚZK REST /
  upstream WMS+XYZ proxy s diskovou cache na Volume.
- **R2 varianta** zůstává v `deploy_r2.sh` + `DEPLOY_R2.md` jako plan B
  (kdyby návštěvnost přerostla VPS, statika se přesune jedním rclone sync).
