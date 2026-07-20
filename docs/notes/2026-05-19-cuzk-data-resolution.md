# ČÚZK — rozlišení datasetů, endpointy, dostupnost

Pracovní reference (2026-05-19) pro inzerator/PZMK. Co lze fakticky stáhnout z
ČÚZK zdarma, v jakém rozlišení, přes který endpoint.

## Ortofoto (RGB letecké snímky)

| Produkt | Rozlišení | Endpoint | Distribuce | Auth | Cena |
|---|---|---|---|---|---|
| **Ortofoto ČR (free, ATOM)** | **0.25 m/px** | `atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml` | ZIP s JPEG + JGW per SM5 list (20000×16000 = 5×4 km) | ne | free |
| **Ortofoto ČR (free, WMS)** | **0.125 m/px native** | `ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer` | WMS GetMap, max 4000×4000 px / request | ne | free, CC BY 4.0 |
| Ortofoto VHR města | ~0.05-0.10 m/px | E-shop ČÚZK / placený WMS | per-objednávka | ano | paid |
| Archivní ortofoto | různé (1953→) | `geoportal.cuzk.gov.cz/.../ORTOARCHIV` | WMS | ne | free |

**Aktualizace:** od 2021 ČÚZK pořizuje letecké snímky v 0.125 m/px rozlišení.
Free ATOM stream je sub-sampled na 0.25 m kvůli velikosti distribuce. Native
0.125 m je dostupný přes public WMS.

Pipeline inzerator dnes:
- Cached source (`cache/ortofoto_<MAPNOM>/*.jpg`): 0.25 m z ATOM
- HD toggle (`?hires=1`): 0.125 m z WMS, 4× sub-tile fetch + stitch → KTX2

Viz commit `f439533` a `server.py:_proxy_ortofoto_vhr`.

## DSM / DTM (digitální modely terénu a povrchu)

| Produkt | Rozlišení | Co obsahuje | Endpoint | Cena |
|---|---|---|---|---|
| **SM5 / DMPOK** | **0.5 m horizontal**, ~10 cm vertical | terén + budovy + vegetace ("surface", DSM) | ATOM (`cache/dmpok_tiff_*`), WCS/WMS | free |
| DMP1G | 1 m horizontal | terén + budovy | WCS, ATOM | free |
| DMR5G | 5 m horizontal | jen terén (ground-only) | WCS `ags.cuzk.gov.cz/.../dmr5g/ImageServer` | free |
| DMR4G | 5 m | terén derived | WMS/WCS | free |
| LIDAR raw pointcloud | varies (1-5 bodů/m²) | bare points (las/laz) | E-shop | paid |

Pipeline použití:
- `gen_detail.py`: SM5 0.5 m → inner/closeup/outer detail meshes
- `gen_panorama.py`: DMR5G 5 m → 30 km horizon panorama

## Katastrální data

| Layer | Rozlišení | Endpoint | Co |
|---|---|---|---|
| `KN` (full katastrální mapa) | scale-dependent raster | WMS `services.cuzk.cz/wms/wms.asp` | budovy + parcel boundaries + parcel numbers |
| `DKM` | scale-dependent | WMS | jen digital cadastral map (boundaries, no numbers) |
| `hranice_parcel` | vector → raster | WMS | parcel boundary lines |
| `parcelni_cisla` | vector → raster | WMS | parcel number labels |
| `ParcelaDefinicniBod` (point) | vector | `ags.cuzk.cz/.../RUIAN/MapServer/0` ArcGIS REST | parcel centroids with attributes |
| `Parcela` (polygon) | vector | `ags.cuzk.cz/.../RUIAN/MapServer/5` ArcGIS REST | parcel polygons with rings |
| `KatastralniUzemi` | vector | `MapServer/7` | KÚ polygons + kod/nazev |

Pipeline:
- `/api/parcels` server-side fetchuje `MapServer/5` (polygony) s pagination
- `/api/ruian/search` používá `MapServer/0` (parcel def. points) + `MapServer/1` (adresy)
- `/proxy/cadastre` renderuje `KN` layer přes WMS

## RUIAN (adresní + administrativní)

| Layer | Endpoint | Co |
|---|---|---|
| `AdresniMisto` | `MapServer/1` | adresní bod (ulice + č.p.) |
| `StavebniObjekt` | `MapServer/3` | budovy s atributy |
| `Obec` | `MapServer/12` | obce |
| ostatní | viz GetCapabilities | ulice, okrsky, atd. |

## Endpointy (souhrn)

```
# Public ortofoto + maps:
https://services.cuzk.cz/wms/wms.asp                          # WMS, GetCapabilities pro layer list
https://geoportal.cuzk.gov.cz/...                              # info, paid eshop
https://atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml                # ATOM feed pro 0.25 m JPEGs
https://atom.cuzk.gov.cz/ORTOFOTO/datasetFeeds/<id>.xml       # per-MAPNOM tile feed

# ArcGIS REST (RÚIAN + ortofoto):
https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/N/query    # vector data
https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer  # 0.125 m WMS
https://ags.cuzk.gov.cz/arcgis/rest/services/3D/dmr5g/ImageServer    # DMR5G WCS-like

# KladyMapovychListu (MAPNOM lookup for ATOM):
https://ags.cuzk.cz/arcgis/rest/services/KladyMapovychListu/MapServer/24  # SM5
https://ags.cuzk.cz/arcgis/rest/services/KladyMapovychListu/MapServer/25  # SM5 alt
```

## Sítí gotchas

- **IPv6 path do ČÚZK z některých sítí neexistuje** (Tailscale, některé ISP).
  `server.py` má monkey-patch `socket.getaddrinfo` na `AF_INET` only.
- ČÚZK občas vrací `Connection refused` na první request, druhý prochází.
  `_ruian_get` retry-uje 5× s exponential backoff (0.5+1+2+4+8s = 15.5s).
- Public WMS na `geoportal.cuzk.gov.cz` občas redirect-uje na PDF `Podminky.pdf`
  místo XML capabilities — používat `services.cuzk.cz` nebo `ags.cuzk.gov.cz/arcgis1`.

## Co ČÚZK nedělá (alternativy)

| Pokud potřebuješ | ČÚZK | Alternativa |
|---|---|---|
| Ortofoto 0.05 m | jen paid | drone photogrammetry |
| Building footprints | RUIAN body | OSM, Mapy.cz |
| Street-level photos | ne | Mapy.cz Panorama, Google StreetView |
| Real-time data | ne | OSM, paid feeds |
| Stereo pairs | neveřejné | E-shop ČÚZK paid |
| Sub-meter LiDAR | jen paid | drone LiDAR, komerční |

## Reference

- Geoportál info: <https://geoportal.cuzk.gov.cz/>
- Otevřená data: <https://geoportal.cuzk.gov.cz/...> (CC BY 4.0 od 1.7.2023)
- INSPIRE OI (Orthoimagery): <https://geoportal.cuzk.gov.cz/WMS_INSPIRE_ORTOFOTO/>
