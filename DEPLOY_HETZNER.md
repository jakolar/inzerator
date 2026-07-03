# Deploy produkce na Hetzner VPS (F4)

Topologie: **produkce plně online** (kompletně předpečená pyramida + viewer
na VPS), **archiv doma** (2 TB bulk na Elements — z něj se peče; na server
se nikdy nekopíruje). Mac po dopočtu F3 jen pushuje delty přes rsync.

## Volba serveru

**CX32** (4 vCPU, 8 GB, **160 GB NVMe**, ~€7,6/měs) — pyramida má dnes ~4 GB,
po F3 odhadem 10–40 GB; 160 GB nechává rezervu na KTX2 vedle JPEGů, případné
z=19/20 populated a další vrstvy. (CX22 s 80 GB by dnes stačil, ale rezerva
za €3,5 stojí za to. Object Storage netřeba — €6/měs paušál za statiku,
kterou VPS obslouží levněji a s vlastní doménou bez CDN mezikroku.)

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

- rsync = delta sync; pouštět opakovaně, jak F3 dopočítává (finálně po
  doběhnutí řetězu). První plný push ~4 GB dnes / desítky GB po F3 —
  na 100Mbit uploadu hodiny, ne dny.
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
