# ČR-wide heightmap pyramid (návrh)

Cílem je rozšířit existující `heightfield/index.html` viewer z per-location režimu (jedna lokace = 3 LOD ringy) na **bezešvý globální tile pyramid**, kde libovolný bod v ČR má dostupný LERC heightmap + KTX2 ortho v Web Mercator zoom levelech `z=8..18`. Pyramid generuje se jednou z bulk DMPOK cache (`/Volumes/Elements/cuzk-bulk/`), servíruje se jako statické soubory, klient skládá mesh přes vertex shader.

Odhad: **~400 GB pro celou ČR** se všemi vrstvami (heightmap + ortho + cadastre + bare-earth), z toho dataset se vejde 22× na Elements.

## Proč heightmap pyramid, ne mesh pyramid

| Přístup | Velikost ČR @ 0,5 m | Render | Změna pipeline |
|---|---|---|---|
| Per-location GLB (současný stav) | ~5 TB pokud rozšířeno na ČR | server pre-bake | žádná, ale stoplánuje |
| Mesh pyramid (3D Tiles / Cesium) | ~200 GB s adaptivní simplifikací | streamed | nová pipeline, 2–4 týdny |
| **Heightmap pyramid (tento dokument)** | **~400 GB** | klient vertex-shader | rozšíření stávajícího `heightfield/` |
| ČÚZK live fetch (současný `gen_heightfield`) | 0 GB lokálně | per-request 30 min | žádná |

Heightmap přístup vyhrává protože:

- **Existující viewer už LERC + KTX2 dekódovat umí** (`heightfield/index.html` to dělá pro 3 ringy)
- **Mesh se generuje na GPU za běhu** přes `displaceY` vertex shader z LERC výškových dat
- **Storage roste přes 4 byte / px (LERC) místo ~32 byte / vertex (GLB)** → ~15× menší (viz níže)
- **Adaptace na zoom level je triviální** — vyšší zoom = stáhne tiles s vyšším rozlišením, žádné per-vertex rozhodnutí
- Server zůstává statický (`server.py` nebo nginx), zero runtime CPU

### Proč heightmap zabírá ~15× méně místa než mesh

Mesh ukládá *všechno o každém bodu*, heightmap ukládá *jen výšku*. Zbytek se dopočte.

**Co mesh obsahuje per vertex:**

| Atribut | Velikost | Proč to musí být uložené |
|---|---|---|
| `position (x, y, z)` | 3 × float32 = 12 B | GPU potřebuje vědět kde vertex je v 3D prostoru |
| `uv coords` | 2 × float32 = 8 B | Kam v textuře sahá → odkud vzít barvu |
| `normal (nx, ny, nz)` | 3 × float32 = 12 B | Směr „nahoru" pro osvětlení (sklon plochy) |
| **Celkem per vertex** | **32 B** | |

Plus pro každý trojúhelník 3 × uint32 indexy = 12 B (sděluje které vertexy ho tvoří). Na 1 km² @ 0,5 m = 4M vertexů, ~8M trojúhelníků → raw mesh **224 MB / km²**, po Draco kompresi **~30 MB / km²**.

**Co heightmap obsahuje per pixel:**

| Atribut | Velikost |
|---|---|
| výška (jedno číslo) | float32 = 4 B, LERC efektivně ~0,5 B |

A to je všechno. Žádné UV, žádné normály, žádné indexy. Na 1 km² @ 0,5 m = 4M pixelů → raw **16 MB / km²**, po LERC kompresi **~2 MB / km²**.

**Kde se těch chybějících 28 MB vzalo / proč to není potřeba ukládat:**

1. **UV souřadnice se odvodí z pozice pixelu** — pixel `[i, j]` jednoznačně mapuje na UV `[i/width, j/height]`.
2. **Normály se počítají v shader z výškových rozdílů** — `normal = normalize(cross(dx, dz))` kde `dx`, `dz` jsou rozdíly výšky mezi sousedy. GPU to dělá za jeden tick.
3. **Topologie je implicitní** — heightmap je regular grid, sousední vertexy `[i,j]`, `[i+1,j]`, `[i,j+1]`, `[i+1,j+1]` vždy tvoří dva trojúhelníky. Indexy nepotřebné.
4. **Pozice v 3D je půl odvozená, půl uložená** — `x` a `z` plynou z gridu (`x = i × pixel_size`), jen `y` se skladuje.

Mesh ukládá 8 čísel per vertex (position + uv + normal). Heightmap ukládá 1 (výšku). To je 8× méně dat ještě před kompresí.

**A komprese je taky lepší:**

LERC komprimuje pole skalárních hodnot, kde sousední pixely mají hodně podobnou výšku (terén je hladký). Ukládá rozdíly mezi sousedy, ne absolutní hodnoty → komprese typicky 30–50×.

Draco komprimuje 3D vertex pozice, což jsou víc-méně náhodné souřadnice → komprese 10–15×.

224 MB raw mesh → 30 MB Draco (7×). 16 MB raw heightmap → 2 MB LERC (8×). Heightmap startuje z mnohem menšího raw, takže výsledek je **15× menší než mesh**.

**Cena je GPU práce navíc — kterou GPU má zadarmo:**

Při mesh renderingu GPU jen čte hotové vertex data ze souboru a vykreslí. Žádná matematika.

Při heightmap renderingu GPU dostane plochý grid (jako papír), vertex shader pro každý vertex přečte výšku z heightmap textury, posune vertex nahoru o `displaceY = sampledHeight`, pak vyrobí normálu ze sousedních samples. Dvě texture fetche + pár aritmetických operací per vertex. Na moderním GPU je vertex shader stage dlouhodobě nedotížený (čeká se na fragment shader) — tradeoff je v tomto případě výhodný.

Vaše inzerator pipeline to už dělá: `heightfield/index.html` načte `closeup_heightmap.lerc` (2,5 MB) + `inner_heightmap.lerc` (2,0 MB), v vertex shader `displaceY`, normály computed inline. Proto heightfield viewer ~10 MB / location vs v2 viewer ~150 MB / location — stejná lokace, jiné storage strategie.

## Tile struktura

Standard Web Mercator XYZ schema, tile size 256×256 px (ekvivalentně 512×512 podle volby tile builder skriptu).

Tile pyramid `/tiles/<layer>/{z}/{x}/{y}.<ext>`:

| Layer | Formát | Soubor | Velikost / tile (avg) |
|---|---|---|---|
| heightmap (DMPOK) | LERC s `max_z_error=0.10 m` | `.lerc` | ~2 KB při z=18, větší při nízkých z |
| bare-earth (DMR5G) | LERC s `max_z_error=0.15 m` | `.lerc` | ~1 KB při z=18 |
| ortho (ČÚZK) | KTX2 ETC1S | `.ktx2` | ~3–10 KB podle quality tier |
| cadastre | PNG alpha-cut | `.png` | ~1–3 KB |

Zoom range: **`z=8..18`** kryje pohled „celá ČR" až „jednotlivá parcela". Native 0,5 m DMPOK ≈ z=18 na 50° s.š. (z=18 dává 0,597 m/px, z=19 už oversample). Nižší zoom levely se generují downsamplingem z native rastru.

## Storage odhad

Z měřených dat (`tiles_v2_*` per-location):

| Layer | MB/km² @ native res | ČR celkem (78 866 km²) |
|---|---|---|
| heightmap LERC inner (0,5 m) | 2,0 | 158 GB |
| heightmap LERC closeup (1,5 m) | 0,28 | 22 GB |
| heightmap LERC outer (2,5 m) | 0,1 | 8 GB |
| bare-earth LERC (DMR5G 1 m) | 1,2 | 95 GB |
| ortho KTX2 ultra (inner) | 10,0 | 790 GB |
| ortho KTX2 high (closeup) | 1,1 | 87 GB |
| ortho KTX2 mid (overview) | 0,07 | 6 GB |
| cadastre PNG | 1,6 | 126 GB |

Naivní suma: **~1,3 TB**. Realisticky:

- **Ortho ultra (790 GB) je drahá luxusní položka** — high tier (87 GB) pokrývá z=14-16, ultra jen z=17-18 a jen kde uživatel zoomne dovnitř. Multi-tier strategie sníží ortho total na ~150 GB.
- **Cadastre tiles** se dají streamovat z ČÚZK WMS on-demand místo pre-bake — ušetří 126 GB.
- **Bare-earth** stačí v nižším rozlišení (z=8..15) protože je primárně pro shadow + osvětlení, ne pro click-precision. ~30 GB.

**Realistická konfigurace:**

| Layer | Velikost |
|---|---|
| heightmap LERC z=8..18 (multi-res) | **~190 GB** |
| ortho KTX2 z=8..14 mid + z=15-16 high + z=17-18 ultra (jen populated) | **~150 GB** |
| bare-earth LERC z=8..15 | **~30 GB** |
| cadastre — streamuje se z ČÚZK WMS on-demand | **0 GB lokálně** |
| **Total na disku** | **~370 GB** |

To je v rámci Elements 10× rezerva.

## Layout na disku

```
/Volumes/Elements/cuzk-bulk/        # zdroj dat (existující)
  dmpok_tiff_<MAPNOM>/                # 16 299 SM5 listů DMPOK
    <MAPNOM>.tif + .tfw

/Volumes/Elements/cr-pyramid/       # výstup, který stavíme
  heightmap/                          # LERC heightmap pyramid
    {z}/{x}/{y}.lerc
  ortho/                              # KTX2 ortho pyramid
    {z}/{x}/{y}.ktx2
  bare/                               # LERC bare-earth pyramid
    {z}/{x}/{y}.lerc
  manifest.json                       # zoom range, tile size, attribution
  build.log                           # generation log

~/Library/Caches/inzerator/         # locks (jako u bulk_dmpok)
  cr_pyramid-<hash>.lock/
```

## Generation pipeline

Pět fází, každá nezávisle restartovatelná (state v `manifest.json` per layer).

### Fáze 0 — Prerekvizity

- `bulk_dmpok.py` dokončené (16 299 DMPOK listů na disku) — currently in progress
- DMR5G bulk pulldown (~600 GB, podobný script jako `bulk_dmpok.py` ale `openzu.cuzk.gov.cz/opendata/DMR5G-TIFF/` endpoint — needs to be built)
- Ortofoto bulk pulldown (1.8 TB, využije existující `download_ortofoto.py` ATOM feed traversal) — needed for ortho pyramid

### Fáze 1 — VRT virtuální raster z DMPOK

```bash
gdalbuildvrt -a_srs EPSG:5514 -srcnodata 0 \
  /Volumes/Elements/cr-pyramid/dmpok.vrt \
  /Volumes/Elements/cuzk-bulk/dmpok_tiff_*/*.tif
```

Instant operace, jen XML pointer na všechny TIFFy. Stejně se udělá `dmr5g.vrt` a `ortho.vrt` ze svých zdrojů.

### Fáze 2 — Reprojekce + base level (z=18)

Web Mercator je `EPSG:3857`. Reprojekce přes celou ČR z `EPSG:5514`:

```bash
gdalwarp -t_srs EPSG:3857 -r bilinear -of COG \
  -co COMPRESS=LERC -co MAX_Z_ERROR=0.10 \
  -multi -wo NUM_THREADS=8 \
  /Volumes/Elements/cr-pyramid/dmpok.vrt \
  /Volumes/Elements/cr-pyramid/dmpok_3857.tif
```

Trvá 6–12 h podle CPU. Mezikrok ~250 GB COG. Lze smazat po dokončení tile cuts.

### Fáze 3 — Tile pyramid generation

Pro každý zoom level z=18 dolů k z=8:

```bash
# z=18 (native, ~0.6 m/px)
python3 build_pyramid.py \
  --src /Volumes/Elements/cr-pyramid/dmpok_3857.tif \
  --layer heightmap --zoom 18 \
  --tile-size 256 --format lerc --max-z-error 0.10 \
  --bbox-czechia \
  --out /Volumes/Elements/cr-pyramid/heightmap/
```

Pak `--zoom 17, 16, …, 8`, každý nižší level z předchozího (downsampling). Per-zoom rate odhadem 10 000 tiles/h na jednom jádře, paralelně 4 jádra → ~6 h pro celou heightmap pyramidu.

`build_pyramid.py` neexistuje — bude potřeba napsat. ~150 řádků Pythonu:
- iteruje XYZ tiles v ČR bbox per zoom
- pro každý tile `rasterio.windows.from_bounds` ze zdrojového COG
- LERC encode přes `rasterio.io.MemoryFile` s `LERC_MAX_Z_ERROR`
- atomic write `.lerc` na disk

### Fáze 4 — Ortho pyramid (vyšší zoom, jen populated)

Stejný algoritmus, ale:
- Zdroj: `ortho.vrt` z ortofoto bulk
- Encoder: KTX2 ETC1S přes `basisu` nebo `toktx`
- Multi-tier strategie:
  - z=8..14: ortho mid (KTX2 low quality)
  - z=15..16: ortho high
  - z=17..18: ortho ultra, jen tiles uvnitř RUIAN-defined populated areas (úspora ~85 % storage)

### Fáze 5 — Manifest + serving

`manifest.json` deskriptor pro klienta:

```json
{
  "bounds_wgs84": [12.09, 48.55, 18.85, 51.06],
  "bounds_3857": [...],
  "zoom_min": 8,
  "zoom_max": 18,
  "layers": {
    "heightmap": { "format": "lerc", "max_z_error_m": 0.10, "ext": "lerc" },
    "ortho":     { "format": "ktx2", "tier_by_zoom": {"8-14": "mid", "15-16": "high", "17-18": "ultra"} },
    "bare":      { "format": "lerc", "max_z_error_m": 0.15, "zoom_max": 15 }
  },
  "attribution": "Mapová data © ČÚZK (DMPOK, DMR5G, Ortofoto, RÚIAN) — CC BY 4.0"
}
```

Server (`server.py`) přidá:

```python
elif self.path.startswith("/tiles/"):
    self.send_response(200)
    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
    super().do_GET()  # static file serve
```

URL pattern: `/tiles/heightmap/{z}/{x}/{y}.lerc`, etc.

## Jak se z heightmapy vyrobí mesh na zobrazení

Trik: geometrie zůstává jedna a ta samá, mění se jen textury. Celý proces je v existujícím `heightfield/index.html`, jen aplikovaný na 3 LOD ringy — pro ČR pyramid se rozšíří na dynamický tile pool.

### Krok 1 — Jednorázově vyrobit "prázdný" plane

Při startu aplikace na CPU jednou za celou session:

```js
const sharedGeo = new THREE.PlaneGeometry(1, 1, 1024, 1024);
sharedGeo.rotateX(-Math.PI / 2);           // postavit naležato (XZ rovina)
sharedGeo.deleteAttribute('normal');       // normály nepotřebujeme (počítat v shaderu)
```

Čtverec 1×1 jednotka rozdělený na 1024×1024 segmentů → **~1 milion vertexů**, všechny v Y=0 (rovná plocha). Ve `heightfield/index.html:668` přesně takhle. Plane je **sdílená přes všechny tiles** — 24 MB GPU paměti použitých tisíckrát.

### Krok 2 — Per-tile material s heightmap texturou

Pro každý viditelný tile vytvoříme `THREE.Mesh(sharedGeo, material)`:

```js
const tileMaterial = new THREE.ShaderMaterial({
  uniforms: {
    uHeight: { value: tileHeightmap },        // LERC dekódovaný do DataTexture
    uOrtho:  { value: tileOrtho },            // KTX2 ortho fotka
    uYBase:  { value: tile.elevation_base },  // posun pro absolutní výšky
    uHmStep: { value: 0.5 },                  // m / pixel pro normály
  },
  vertexShader: VERT,
  fragmentShader: FRAG,
});
const tileMesh = new THREE.Mesh(sharedGeo, tileMaterial);
tileMesh.position.set(tile.worldX, 0, tile.worldZ);
tileMesh.scale.set(tile.sizeMeters, 1, tile.sizeMeters);
scene.add(tileMesh);
```

Geometrie není deep-clone, jen reference. Když mesh `dispose()`ujete, GPU buffer pro geometrii zůstává (sdílený), jen material + textures se uvolní.

### Krok 3 — Vertex shader: posunout Y nahoru o vzorek heightmapu

Existující shader z `heightfield/index.html:806`:

```glsl
uniform sampler2D uHeight;
uniform float uYBase;
varying vec2 vUv;

void main() {
  vec2 hmUv = vec2(uv.x, 1.0 - uv.y);          // Y-flip kvůli konvenci rasteru
  float absY = texture2D(uHeight, hmUv).r;     // klíčový řádek
  vec3 pos = position;
  pos.y = absY - uYBase;                       // přesun vertexu nahoru
  vUv = vec2(uv.x, 1.0 - uv.y);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(pos, 1.0);
}
```

Pro každý vertex z 1M vertexů: GPU si v UV `[i/1024, j/1024]` přečte výšku z heightmap textury a posune vertex nahoru. Paralelně přes vertex shader unity, typicky < 0,1 ms na celý milion vertexů.

### Krok 4 — Fragment shader: dopočet normály z gradientů

Pro osvětlení normály per-pixel z výškových rozdílů mezi sousedy:

```glsl
vec3 sampleNormal() {
  float t = uHmTexel;
  float hL = texture2D(uHeight, vUv + vec2(-t, 0)).r;
  float hR = texture2D(uHeight, vUv + vec2( t, 0)).r;
  float hU = texture2D(uHeight, vUv + vec2(0, -t)).r;
  float hD = texture2D(uHeight, vUv + vec2(0,  t)).r;
  float dxMeters = 2.0 * uHmStep;
  return normalize(vec3(hL - hR, dxMeters, -(hU - hD)));
}
```

4 texture fetche + cross product per pixel. Mesh by měl normály per-vertex (zubaté), heightmap per-pixel (hladké) — to je další bonus.

### Krok 5 — Skládání tiles do bezešvé scény

Per camera move:

1. `THREE.Frustum` spočítá viditelné XYZ tiles
2. Priority queue fetchuje `heightmap/{z}/{x}/{y}.lerc` + `ortho/{z}/{x}/{y}.ktx2` pro nové tiles
3. Každý tile dostane `new THREE.Mesh(sharedGeo, tileMaterial)` s vlastním heightmap + ortho, uloží se do `scene`
4. Position + scale podle Web Mercator world coords
5. LOD: zoom-in → 1 tile se rozdělí na 4 menší (quadtree split), staré se vyfade
6. Tiles co opustily viewport → `dispose()`, GPU paměť se vrátí

V kterémkoliv momentě 8–32 tiles aktivních, **~160 MB GPU paměti** (LERC heightmap ~4 MB + KTX2 ortho ~1 MB per tile). To je laptop-tier GPU triviální.

### Co je sdíleno a co per-tile

| Resource | Sdíleno | Per-tile |
|---|---|---|
| Vertex buffer (positions + UV) | ✓ | |
| Index buffer | ✓ | |
| Vertex + fragment shader code | ✓ | |
| Heightmap texture | | ✓ |
| Ortho texture | | ✓ |
| Material uniforms | | ✓ |
| THREE.Mesh objekt | | ✓ |

### Mapování na existující kód

| `heightfield/index.html` linka | Co dělá |
|---|---|
| `:668` | Vytvoří sdílenou `sharedGeo` plane (1024×1024 subdivisions) |
| `:707–737` | Per-ring: loadne heightmap LERC + ortho KTX2 |
| `:806–834` | Vertex shader = `displaceY` z heightmap |
| `:871–887` | Fragment shader = normála z gradientů |
| `:947` | `scene.add(ringMesh)` umístění do scény |

Pro ČR pyramid se ten kód **rozšíří, ne přepíše**:

1. 3 fixní ringy (outer/closeup/inner) → dynamický pool XYZ tiles
2. Per-slug manifest → globální `/tiles/manifest.json`
3. Streaming queue (tile add/remove na základě kamery)
4. LOD switching mezi zoom levely

Vertex + fragment shadery jsou identické. To je hlavní design win — renderování se nevymýšlí znovu, jen pivot tile addressing.

## Klient-side úpravy

`heightfield/index.html` momentálně načte 3 LOD ringy z `tiles_v2_<slug>/heightfield/manifest.json`. Refactor na pyramid:

1. **Init**: načte `/tiles/manifest.json` namísto per-slug manifestu
2. **Bbox→tiles**: pro aktuální camera view spočítá které XYZ tiles potřebuje (standard slippy-tile math)
3. **Streaming**: priority queue stahování — viditelné tiles první, sousední na pozadí
4. **Mesh assembly**: každý tile = jeden THREE.PlaneGeometry rozšířený podle LERC heightmap přes vertex shader (`displaceY` jak to děláte teď, ale per-tile)
5. **LOD blending**: pokud uživatel zoomuje, nahradí 4 tiles z=N čtyřmi tiles z=N+1 (quadtree split) s krátkou animací

Pro každý tile bude `THREE.Group` s heightmapem + ortho + bare-earth-toggle. Renderuje se ~4-16 tiles současně v zorném poli, zbytek se necachuje.

URL parameter změna: `/heightfield/?lat=50.08&lon=14.42&zoom=15` místo `?slug=hnojice`. Slug-based režim může zůstat jako per-location override.

## Sjezd z map portálu

V mapovém portálu (Leaflet/MapLibre 2D) bude link „3D pohled" co předá `lat,lon,zoom` do `/heightfield/`. 2D map a 3D viewer se nesynchronizují — to je separátní problém pokud chcete pan/zoom synced.

## Phasing — co stavět v jakém pořadí

1. **Fáze A — heightmap pyramid only** (cca 2 týdny vývoje):
   - `bulk_dmpok` dokončí
   - VRT + reprojekce → 250 GB COG
   - `build_pyramid.py` napsat + spustit
   - Klient-side z `tiles_v2_<slug>` → `/tiles/manifest.json` adresace
   - Test: 3D pohled na libovolný bod ČR s bezešvým height + monochrome rendering
2. **Fáze B — ortho pyramid** (1 týden):
   - Ortofoto bulk download (~1,8 TB) — nový script `bulk_ortofoto.py` postavený nad `download_ortofoto.py` (ATOM feed má URL pattern, jen scale + parallel)
   - VRT + reprojekce
   - KTX2 multi-tier pyramid generování
3. **Fáze C — bare-earth pyramid** (3 dny):
   - DMR5G bulk download (~600 GB) — nový `bulk_dmr5g.py`
   - VRT + LERC pyramid
4. **Fáze D — cadastre + parcel overlay**:
   - Buď live z ČÚZK WMS (cheap, ale online dependency), nebo pre-bake jako PNG pyramidu (~126 GB)
5. **Fáze E — POI + adresy z RÚIAN**:
   - Vector tiles (MVT) s populated areas, POI, adresami pro klick-to-info funkci

## Open questions

1. **Web Mercator vs S-JTSK pyramid?** Většina map portálů jede 3857. Tile pyramid v 5514 by se vyhnul reprojekci ale klient by potřeboval pyproj-equivalent v JS (proj4js).
2. **Tile size 256 vs 512 vs adaptive?** 256 = více HTTP requestů ale rychlejší LOD blending. 512 = méně requestů ale větší per-tile bandwidth.
3. **Pre-bake all populated z=18 nebo on-demand?** Pre-bake = ~150 GB navíc storage, on-demand = potřeba runtime tile cutter (vrátíme se k TiTiler / MapServer).
4. **Building heights jako samostatná vrstva?** DMPOK - DMR5G = výška budov + vegetace. Vector layer s building footprints (z RÚIAN) + height by umožnil 2.5D budovy bez full DSM mesh.
5. **Versioning / update strategy?** DMPOK má 2-letý update cyklus. Pyramidu re-bake přes víkend? Nebo per-tile diff podle staré vs nové DMPOK?

## Compute budget odhad

| Fáze | Wall-clock | CPU | Disk write |
|---|---|---|---|
| A — heightmap pyramid | ~12 h | 8 cores | 190 GB |
| B — ortho pyramid | ~24 h | 8 cores + GPU pro basisu | 150 GB |
| C — bare-earth pyramid | ~6 h | 4 cores | 30 GB |
| **Total** | **~42 h compute, 2–4 týdny vývoje** | | **~370 GB** |

Plus 3 nová bulk download skripty (DMR5G, ortofoto, případně cadastre) — každý ~5–7 nocí stahování paralelně s tím compute.

## Co nevyplývá z tohoto návrhu

- **Per-location parcel overlay** — současný `tiles_v2_<slug>` workflow s drone video tool a realtor presentation **zůstává jak je**. Pyramid je samostatný kanál pro „prohlédnout libovolnou adresu na ČR". Per-listing detailní vizualizace se generuje per-request jak dosud.
- **3D budovy z RÚIAN** — extrúze z polygonů (LOD 1) je separátní feature, ne součást heightmap pyramidy.
- **Globální tree/canopy heightmap** — DMPOK obsahuje vegetaci, ale samostatný „canopy height model" = DMPOK − DMR5G je odvozený dataset, ne primární vrstva.
