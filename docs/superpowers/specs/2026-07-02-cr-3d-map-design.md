# ČR-wide 3D mapa — technický design + plán nasazení

> Navazuje na `HEIGHTFIELD_PYRAMID.md` (design pyramidy, odhady velikostí) a
> `HEIGHTFIELD_PYRAMID_RATIONALE.md`. Tento dokument řeší **deltu k nasazení**:
> co přesně chybí, jak to technicky funguje end-to-end, a rozhodnutí D1–D5
> s výhodami/nevýhodami alternativ. Stav dat k 2026-07-02:
>
> - bulk DMPOK: **16 299/16 299** listů, 945 GB (`/Volumes/Elements/cuzk-bulk/`)
> - bulk ortofoto: **16 300/16 301**, 973 GB (1 list `missing` — prověřit URL pattern)
> - heightmap pyramida: 61 dlaždic (smoke test), builder ověřený
>   (`build_pyramid_tile.py`, `dispatch_pyramid.py`), on-demand endpoint
>   v `server.py` funkční pro z=14..20

**Cíl:** bezešvá 3D mapa celé ČR (heightmap + ortofoto drape) v prohlížeči,
nasazená veřejně, bez runtime backendu.

**Architektura ve větě:** statická XYZ Web Mercator pyramida
(LERC heightmapy + KTX2 ortho) na object storage + CDN; veškerá inteligence
(quadtree LOD, streaming, mesh skládání) v three.js klientovi — rozšíření
existujícího `heightfield/` renderovacího kódu.

---

## 1. End-to-end datový tok

```
/Volumes/Elements/cuzk-bulk/            (suroviny, 1.9 TB, hotovo)
  dmpok_tiff_*  ortofoto_*
        │
        ├─ dispatch_pyramid.py          (existuje) ──► cuzk-pyramid/dmpok/{z}/{x}/{y}.lerc
        │                                              z=8..14 předpečené, ~5 GB
        │                                              z=15..18 viz D3
        └─ dispatch_ortho_pyramid.py    (NOVÉ, F1) ──► cuzk-pyramid/ortho/{z}/{x}/{y}.ktx2
                                                       ~25–40 GB (multi-tier, viz D2/D3/D6)
        │
        ▼
  rclone sync ──► object storage + CDN (D5)           immutable, max-age=1y
        │
        ▼
  map3d/ viewer (NOVÉ, F2)                             quadtree LOD, sdílená geometrie,
                                                       vertex-shader displacement
```

Server přestává být součástí runtime — v produkci servíruje CDN statiku.
`server.py` zůstává jako dev origin + on-demand builder vysokých zoomů.

## 2. Dlaždicové schéma a geodézie

- **Grid:** standard XYZ slippy (EPSG:3857), 256×256 px na dlaždici
  (`TILE_SIZE = 256` v `build_pyramid_tile.py` — už rozhodnuto, viz D4).
- **Zoom range:** z=8 (celá ČR na obrazovce) až z=18. **Konvence: m/px
  v tomto dokumentu = na zemi** (mercator hodnota × cos 50° ≈ ×0,64) —
  HEIGHTFIELD_PYRAMID.md obojí směšuje. z=18 → 0,38 m/px na zemi, tj. první
  zoom, který plně vzorkuje 0,5m DMPOK (z=17 s 0,77 m/px undersampluje);
  z=19+ pro heightmap nemá smysl. Pro **ortho** je strop jiný — viz D6.
- **Heightmap:** LERC, `max_z_error = 0.05 m`, NODATA pro okraje ČR.
  ~2 KB/dlaždice po kompresi.
- **Mercator zkreslení:** na 50° s.š. je scale factor 1/cos(50°) ≈ 1,556.
  Výšky jsou pravé metry, ale horizontála v mercator metrech je natažená —
  bez korekce vypadá terén 1,56× placatější. Viewer musí buď škálovat
  horizontálu `cos(lat₀)`, nebo výšky násobit 1,556 (konstanta pro celou ČR
  je přijatelná — rozptyl cos(48,5°..51,1°) je ±2 %). `pyramid-test.html`
  tohle zatím ignoruje a maskuje to 2× exaggerací.
- **Počty dlaždic (z HEIGHTFIELD_PYRAMID.md):** z=8..14 ≈ 73 k, z=15 ≈ 219 k,
  z=16 ≈ 875 k, z=17 ≈ 3,5 M, z=18 ≈ 14 M. Plná pyramida 18,6 M dlaždic —
  proto multi-tier strategie (D3).

## 3. Co existuje vs. co chybí

| Komponenta | Stav | Fáze |
|---|---|---|
| Heightmap builder (1 dlaždice, mozaika+reprojekce+LERC) | ✅ `build_pyramid_tile.py` | — |
| Heightmap bulk dispatch z=8..14 (bottom-up, resumable) | ✅ kód, ❌ nespuštěno | F0 |
| Heightmap on-demand z=14..20 | ✅ `server.py:_serve_pyramid_tile` | — |
| Ortho pyramid builder | ❌ neexistuje (test viewer tahá živě z ČÚZK WMS) | F1 |
| Multi-tile streaming viewer | ❌ (`pyramid-test.html` = 1 dlaždice) | F2 |
| Vysoké zoomy z=15..18 pre-bake | ❌ | F3 |
| Hosting/CDN + upload pipeline | ❌ | F4 |

## 4. Rozhodnutí s alternativami

### D1 — Rendering stack ve vieweru

| Varianta | Výhody | Nevýhody |
|---|---|---|
| **A. Vlastní three.js tile pool (rozšíření `heightfield/`)** ✅ | Shadery, LERC/KTX2 loading, skirt řešení a colorspace pipeline už existují a jsou odladěné (viz `docs/notes/three-js-colorspace-srgb.md`, seam-skirt plan 2026-06-10). Plná kontrola nad vzhledem (pedestal, tone mapping, SSAO se dají recyklovat). Žádná nová dependency. | Quadtree LOD + fetch queue se píše ručně (~1–2 týdny). Edge-casy (crack mezi zoomy, cache eviction) na nás. |
| B. CesiumJS | Hotový globe streaming, quantized-mesh, battle-tested LOD. | Vyžaduje **mesh** pipeline (quantized-mesh/3D Tiles), ne heightmap textury — zahodí se existující LERC pyramida i shadery; ~1 MB runtime; vizuální styl těžko ohnout na náš „premium" look. |
| C. MapLibre GL (raster-dem terén) | Nejméně kódu, hotové ovládání mapy. | Terrain-RGB PNG formát (přepis pipeline), omezená 3D kamera (pitch cap), žádný přístup k vlastním shaderům/efektům — konec pedestalu, tilt-shiftu, SSAO. |
| D. deck.gl TerrainLayer | Deklarativní, slušný LOD. | Další velký framework vedle three.js; TerrainLayer je nejslabší část deck.gl (známé seam problémy); LERC musíme stejně dekódovat sami. |

**Doporučení: A.** Jediná varianta, která zhodnotí existující kód; B–D
znamenají zahodit funkční shader stack a část pyramidy.

### D2 — Formát ortho dlaždic

| Varianta | Velikost (256², foto) | GPU paměť | Výhody | Nevýhody |
|---|---|---|---|---|
| **KTX2 ETC1S** ✅ | ~5–10 KB | ~43 KB (komprimovaná zůstává na GPU) | GPU-native, `KTX2Loader` už ve vieweru, 4× méně GPU paměti než RGBA | Encode pomalý (basisu), mírné artefakty na vegetaci |
| KTX2 UASTC | ~30 KB | ~85 KB | Vyšší kvalita | 3–6× větší soubory — na 3,7 M dlaždic neúnosné |
| WebP q80 | ~8 KB | 256 KB (dekóduje do RGBA) | Menší soubory, rychlý encode | 6× víc GPU paměti per tile, dekódování na CPU (jank při streamu) |
| JPEG q85 | ~12 KB | 256 KB | Univerzální, encode zdarma (zdroj je JPEG) | Totéž co WebP + žádná alfa; dvojitá ztrátová komprese |

**Doporučení: KTX2 ETC1S**, stejně jako per-location pipeline — konzistence
s existujícím loaderem a GPU rozpočtem. Kvalitu ladit `q`/`comp_level` na
vzorku (viz commit `b65ffb4`, q=220 už vyzkoušeno).

### D3 — Strategie vysokých zoomů (z=15..18)

| Varianta | Disk | Výhody | Nevýhody |
|---|---|---|---|
| Plný pre-bake z=8..18 | ~200–400 GB height+ortho | Čistá statika, žádný origin server, funguje kdekoli | z=17..18 je 17,5 M dlaždic — dny až týden výpočtu; 85 % dlaždic (lesy, pole) skoro nikdo nezobrazí |
| **Hybrid: pre-bake z=8..16 celoplošně + z=17..18 jen "populated" (RÚIAN maska); zbytek on-demand** ✅ | ~60–120 GB | 95 % reálného trafficu ze statiky; výpočet dnů, ne týdnů; on-demand už existuje pro heightmap | Vyžaduje běžící Mac jako fallback origin pro neobydlené z=17..18; ortho on-demand builder je nový kód |
| Vše on-demand od z=15 | ~10 GB | Minimální pre-bake | První návštěvník každé dlaždice čeká sekundy; Mac + Elements disk se stávají hard dependency produkce; studený start po rebootu |

> **Pozor na odhady velikostí:** HEIGHTFIELD_PYRAMID.md počítá ~2 KB/heightmap
> dlaždici, ale smoke test měří **51 KB průměr na z=14** (61 dlaždic, 3,1 MB).
> Velikost LERC klesá s zoomem (menší území = menší relief per dlaždice),
> takže obě čísla mohou platit každé jinde — rozptyl odhadu je ale ±3×.
> **F0 přinese tvrdá čísla pro z=8..14; před F4 nekupovat storage podle
> odhadů z papíru.**

**Doporučení: hybrid.** Fallback origin může v prvních týdnech chybět úplně —
klient při 404 prostě zobrazí rodičovskou dlaždici (z=16 = 1,2 m/px, pořád
slušné) a nic se nerozbije. On-demand fallback je optimalizace, ne blocker.

### D4 — Velikost dlaždice (potvrzení 256)

512² by znamenalo 4× méně requestů, ale 4× větší minimální fetch, horší
granularita LOD přechodů a **re-build existující heightmap pyramidy**.
256 je standard, builder na něm stojí → zůstává 256. (Zapsáno jen proto,
aby se otázka znovu neotvírala.)

### D5 — Hosting

| Varianta | Cena (~60–120 GB, ~50 GB egress/měs) | Výhody | Nevýhody |
|---|---|---|---|
| **Cloudflare R2 + CF CDN** ✅ | ~$1–2/měs storage, **egress $0**; jednorázově ~$17 Class A ops (3,7 M PUT à $4,5/M) | Nulový egress = žádné překvapení na faktuře; CDN, TLS, custom doména v ceně; S3-kompatibilní API (rclone) | Vendor lock-in mírný (S3 API → přenositelné) |
| Backblaze B2 + Cloudflare | <$1/měs (Bandwidth Alliance egress $0) | Nejlevnější storage | Dva účty/dvě konfigurace; latence B2→CF o něco horší na cache-miss |
| AWS S3 + CloudFront | ~$2–3 + $4,5 egress | Nejrobustnější tooling | Nejdražší; egress roste s trafficem |
| Mac mini origin + Cloudflare Tunnel | $0 | Okamžitě k dispozici, on-demand build funguje nativně | Dostupnost = uptime Macu + připojený Elements disk; upload rychlost domácí linky limituje cache-miss latenci |
| Firebase Hosting | — | (projektový default pro weby) | **Nevhodné**: 10 GB limit storage na free/Blaze hosting tier, deploy 3,7 M souborů netriviální |

**Doporučení: R2** pro pyramidu + viewer statiku. Mac mini za CF Tunnel jako
volitelný on-demand fallback origin (D3). Poznámka: globální pravidlo
„deploy na Firebase" tady vědomě porušujeme — desítky GB / miliony statických
dlaždic jsou jiná kategorie než web app; viewer HTML *může* žít na Firebase,
dlaždice ne.

> **AMENDMENT 2026-07-03 — rozhodnuto jinak: Hetzner VPS (CX32).**
> Požadavek: produkce musí být kompletně online a nezávislá na Macu;
> archiv (2 TB bulk) zůstává doma jako build vstup. Produkce je tedy čistá
> statika (plně předpečená pyramida + viewer) za Caddy na malém VPS
> (~€7,6/měs, 160 GB NVMe, latence do ČR ~20–30 ms — CDN pro českou
> audienci netřeba). Nepředpečená dlaždice = klient zobrazí rodiče; žádný
> on-demand builder v produkci. R2 (`deploy_r2.sh` + `DEPLOY_R2.md`)
> zůstává plán B pro případ růstu návštěvnosti — přechod je jeden sync.
> Runbook: `DEPLOY_HETZNER.md`, deploy: `deploy_hetzner.sh`,
> Caddy: `deploy/Caddyfile`.

### D6 — Strop ortho detailu (z=18 nestačí na zdroj)

Zdroj má 0,124 m/px, ale z=18 dává na zemi jen 0,38 m/px — pyramida zahodí
~3× lineárního rozlišení ortofota. Per-location viewery (ultra tier
0,18 m/px) budou znatelně ostřejší než celorepubliková mapa.

| Varianta | Výhody | Nevýhody |
|---|---|---|
| **A. Přijmout z=18 strop pro v1** ✅ | Nulová práce navíc; dlaždice jsou immutable — z=19..20 lze přidat kdykoli později bez přegenerování nižších zoomů | Zoom „na parcelu" je měkčí než per-location viewer |
| B. Populated-only ortho z=19..20 | Plný detail zdroje tam, kde se kouká | z=19 populated ≈ 8 M, z=20 ≈ 34 M dlaždic — encode týdny, ops náklady rostou 10× |
| C. 512² ortho dlaždice od z=17 | Detail z=18/19 při 4× menším počtu souborů | Dvojí tile size v pipeline i vieweru (výjimka v addressingu, gridu i cache) |

**Doporučení: A pro v1**; B jako follow-up po nasazení, až bude vidět reálné
usage (které oblasti lidi zoomují). Server on-demand už umí do z=20, takže
„ostrý detail na vyžádání" může mezitím suplovat Mac origin.

## 5. Ortho pyramid builder (F1) — jak přesně

Zrcadlí ověřený heightmap přístup (žádná nová architektura):

1. **Inventář:** SM5 grid je sdílený s DMPOK → `inventory.json` mechanismus
   z `build_pyramid_tile.py` se použije beze změny, jen nad
   `ortofoto_*` JPEGy (bbox listu je týž).
2. **Base level z=16** (ne 14 jako heightmap): z=16 je 1,5 m/px na zemi —
   nejvyšší zoom, který jde celoplošně; z=17..18 (0,77/0,38 m/px) se generuje
   přímo ze zdroje jen do populated masky (D3), strop detailu řeší D6.
   Dlaždice = mozaika překrývajících se JPEGů → reprojekce 5514→3857
   (rasterio, bilinear) → 256² RGB.
3. **Encode:** `basisu` ETC1S (subprocess, `-q` dle vzorku). CPU-bound —
   paralelizovat přes `ThreadPoolExecutor` jako `dispatch_pyramid.py`,
   ale worker count řídit podle encode throughput, ne I/O.
   **Bez mipmap** — minifikaci řeší LOD přechod na nižší zoom; −33 %
   velikosti i encode času. Pokud šikmé pohledy ve F2 shimmerují,
   přehodnotit (mips má smysl jen pro ortho, ne heightmap).
4. **z=15..8 downsample** 2×2 z dětí (PIL/numpy average před encode) —
   bottom-up jako heightmap, žádné opakované čtení JPEGů.
5. **z=17..18 populated-only:** maska z RÚIAN (obce + buffer 500 m);
   dlaždice mimo masku se negenerují. Stejný filtr použije F3 pro heightmap.
6. **Resumabilita:** existence `.ktx2` = hotovo, atomic write (`.tmp` →
   `replace`), SIGTERM drain — kopie vzoru z `dispatch_pyramid.py`.

Odhad běhu: ~1,2 M dlaždic (z=8..16) + ~2,6 M populated (z=17..18);
při ~80 encode/s (8 jader) ≈ 2–4 dny. Disk ~25–40 GB (ETC1S ~5–10 KB/dlaždici;
ověřit na vzorku první den — viz riziko kvality ETC1S).

## 6. Viewer (F2) — jak přesně

Nový `map3d/index.html`, renderovací jádro převzaté z `heightfield/index.html`:

- **Sdílená geometrie:** jedna `PlaneGeometry(1,1,256,256)` (65 k vertexů,
  vertex ≙ texel heightmapy — víc nemá smysl, detail nese textura; viz
  `feedback_geo_subdivs` memory). Mesh per dlaždice = reference na geometrii
  + vlastní material.
- **Shadery:** identické s heightfield (displaceY ve vertex, normály
  z gradientů ve fragment) + per-tile `uYBase` (fp32 přesnost) a skirt
  na okraji dlaždice (recyklace 2026-06-10 seam-skirt řešení — skirty
  zakryjí i crack mezi sousedy různých zoomů).
- **Quadtree LOD:** screen-space error metrika — dlaždice se rozdělí na 4
  děti, když `(velikost_px_na_obrazovce / 256) > práh (~1,5)`. Merge při
  oddálení. Hysterezí zabránit flip-flop na hranici.
- **Streaming:** priority queue (střed frustumu první), max ~6 souběžných
  fetchů, LRU cache ~100 dekódovaných dlaždic. LERC decode ve Web Workeru
  (hlavní vlákno jen vytvoří DataTexture), KTX2 transcode dělá
  `KTX2Loader` worker pool sám.
- **404 = neexistující dlaždice** (mimo ČR / negenerovaný z=17..18):
  zobrazit rodiče (ortho UV offset ¼, z=16 ≈ 1,5 m/px na zemi — pořád
  použitelné), nikdy díru.
- **Vstup:** URL params `?lat=&lon=&z=` (deep-linkovatelné pohledy) — pro v1
  stačí. Vyhledávání adres přes existující `/api/ruian/search` funguje jen
  s běžícím serverem (dev/vlastní použití); na čistě statickém CDN deployi
  vyhledávání ve v1 není — doplnit později (statický index obcí ~1 MB JSON).
- **Rozpočet:** 8–32 aktivních dlaždic; heightmap 256 KB + ortho ~43 KB GPU
  per tile → < 10 MB GPU textur + 1 sdílený vertex buffer. Mobil zvládne.
- **Vzhled:** hillshade default, tone-mapping presety, pedestal vypnout
  (nekonečný terén ho nepotřebuje) — vše už parametrizované v heightfield kódu.

## 7. Serving, verze, atribuce (F4)

- **URL:** `/tiles/v1/dmpok/{z}/{x}/{y}.lerc`, `/tiles/v1/ortho/{z}/{x}/{y}.ktx2`
  + `/tiles/v1/manifest.json` (bounds, zoom range, tier mapping, attribution).
  Verze v cestě → soubory immutable → `Cache-Control: public, max-age=31536000, immutable`.
- **Upload:** `rclone sync cuzk-pyramid/ r2:tiles/v1/ --transfers 32 --fast-list`
  — resumable, idempotentní. První sync ~60–120 GB ≈ noc na domácí lince.
  Pozor na **miliony malých souborů**: listing bucketu je pomalý a stojí
  Class A ops — inkrementální syncy řídit lokálním seznamem změn
  (`--files-from`), ne porovnáním proti bucketu.
- **CORS:** `Access-Control-Allow-Origin: *` na bucketu (viewer může žít jinde).
- **Atribuce:** „Mapová data © ČÚZK (DMPOK, Ortofoto, RÚIAN)" viditelně ve
  vieweru — podmínka open dat ČÚZK.
- **Katastr/parcely:** v1 vynechat. Později vektorově z RÚIAN (ne WMS proxy
  z klientů na ČÚZK).

## 8. Fáze a pracnost

| Fáze | Co | Nový kód | Trvání |
|---|---|---|---|
| **F0** | Spustit `dispatch_pyramid.py` z=8..14, zvalidovat vzorky + **změřit reálné velikosti per zoom** (kalibrace odhadů v D3/D5) | žádný | 1 noc běhu |
| **F1** | Ortho pyramid builder + dispatch (kap. 5) | ~2 skripty po vzoru existujících | 2–3 dny kódu + 2–4 dny běhu |
| **F2** | `map3d/` streaming viewer (kap. 6) | 1 HTML + worker | 1–2 týdny |
| **F3** | Populated maska (RÚIAN) + heightmap z=15..16 pre-bake | maska + parametr dispatche | 2 dny + běh |
| **F4** | R2 bucket, rclone sync, manifest, atribuce, doména | konfigurace + manifest writer | 1–2 dny |

F0 ∥ F1 ∥ F2 jsou nezávislé — F2 se vyvíjí nad lokálním serverem
(on-demand endpoint) i bez dokončených pre-baků. Kritická cesta = F2.

## 9. Rizika

- **Elements disk je single copy** surovin i pyramidy. Regenerovatelné
  (bulk re-pull ~6 nocí), ale výpadek uprostřed práce bolí. Levná pojistka:
  po F1 syncnout pyramidu na R2 hned — tím je záloha zadarmo součástí deploye.
- **ETC1S kvalita na ortofotu** (vegetace, střechy) — ověřit na vzorku
  *před* 4denním encode během F1; fallback UASTC jen pro z=17..18 populated
  by zvedl disk o ~60 GB, pořád únosné.
- **Mercator/S-JTSK reprojekční šev** mezi sousedními dlaždicemi z různých
  SM5 listů — heightmap builder to už řeší mozaikou před reprojekcí; ortho
  builder musí převzít týž postup (mozaika → reprojekce, ne reprojekce →
  mozaika).
- **1 chybějící ortofoto list** — vyřešit před F1 (runbook `BULK_ORTOFOTO.md`:
  zkontrolovat ZIP URL pattern, případně `--retry-failed`).
