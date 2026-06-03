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

## Jak se to celé servíruje (klient ↔ server flow)

Server stranou je to triviální — **statika přes HTTP**. Žádný runtime kompilátor, žádný GIS proces, žádný TiTiler. Inteligence je v klientovi.

### Server: prosté statické file serving

`server.py` (nebo nginx/Caddy/CloudFront) servíruje obsah `tiles/` adresáře jako klasickou statiku:

```python
elif self.path.startswith("/tiles/"):
    # files jsou immutable (versioning v cestě) → cache forever
    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
    self.send_header("Access-Control-Allow-Origin", "*")  # CORS pro CDN
    super().do_GET()
```

Když přijde GET `/tiles/heightmap/14/9123/5678.lerc`, server najde fyzický soubor na disku, vrátí jeho ~2 KB byte. **0 ms CPU**, jen disk I/O. Žádný GDAL, žádný Three.js — server nemusí být ani Python.

### URL struktura

Standard XYZ Slippy schema. Tři paralelní pyramidy:

```
/tiles/manifest.json                  # globální deskriptor (bounds, zoom range)
/tiles/heightmap/{z}/{x}/{y}.lerc     # výška (DMPOK)
/tiles/ortho/{z}/{x}/{y}.ktx2         # ortofoto barva
/tiles/bare/{z}/{x}/{y}.lerc          # bare-earth (DMR5G)
```

Multi-tier ortho per-zoom: `z=8..14` use `ortho_mid/`, `z=15..16` use `ortho_high/`, `z=17..18` use `ortho_ultra/`. Manifest řekne klientovi jakou variantu pro jaký zoom fetchovat.

### Per-frame klient flow (60×/s)

Co viewer dělá při každém pohybu kamery:

1. **Spočítat viditelné tiles** — z kamery + frustum spočítat WGS84 bbox, vyjmenovat XYZ tile coords v aktuálním zoomu. Typicky 4–16 tiles aktivních.
2. **Vyřadit už načtené** — LRU cache drží ~100 tiles v paměti, klíč `${z}/${x}/${y}/${layer}`.
3. **Fetchnout nové paralelně** — `fetch('/tiles/heightmap/...')` + `fetch('/tiles/ortho/...')`. Priority queue: visible center first.
4. **Dekódovat** — `LERC.decode(buffer)` → `Float32Array` → `THREE.DataTexture`. `KTX2Loader.parse(buffer)` → `CompressedTexture` (GPU-native ETC1S). ~5 ms per tile, lze Web Worker.
5. **Vytvořit tile mesh** — `new THREE.Mesh(sharedGeo, material)` s heightmap + ortho uniforms, position podle tile bbox center, scale = tile size v metrech.
6. **Render** — vertex shader displaceY pro každý tile, GPU < 5 ms na celkem ~16M vertexů.
7. **Cleanup** — tiles co opustily frustum → `dispose()` textur, `scene.remove()`.

Server v tomhle flow nic nedělá kromě posílání byte streamů. **Veškerá inteligence (které tiles? jak sestavit?) je v JS klientovi.**

### Per-tile bandwidth realita

Pro 16 současně viditelných tiles:

| Layer | Per tile | × 16 tiles |
|---|---|---|
| heightmap LERC | ~2 KB | 32 KB |
| ortho KTX2 ultra | ~10 KB | 160 KB |
| bare LERC (opt) | ~1 KB | 16 KB |
| **Initial load celé scény** | | **~210 KB** |

Pohybem kamery ~50 KB/s v stabilním tempu (1–2 nové tiles/s). Mobilní 4G to dá s rezervou. Edge cache (CloudFront / Bunny.net) zkrátí cestu — tiles jsou immutable + `max-age=1y` → 100 % cache hit po prvním fetchu.

### Srovnání s OGC 3D Tiles spec (Cesium)

„3D Tiles" jako formální spec (`tileset.json` + `.b3dm` / `.pnts` content) má jiný design:

| Aspekt | OGC 3D Tiles (Cesium) | Náš heightfield pyramid |
|---|---|---|
| Tile content | mesh přímo (binární glTF) | heightmap + ortho samostatně |
| Server | statický | statický |
| Tile hierarchy | quadtree v `tileset.json` | implicitní XYZ Slippy |
| Geometry | per-tile vertex data | reconstructed klient-side |
| Per-tile velikost | 1–10 MB (mesh) | 12 KB (heightmap+ortho) |
| Storage ČR @ 0,5 m (teoretická adaptive pipeline) | ~200 GB | ~370 GB |
| Storage ČR @ 0,5 m (naivně z existujícího `gen_detail`) | ~1–5 TB | — |
| Klient | Cesium / MapLibre 3D | THREE.js heightfield viewer |
| Vývoj | nový pipeline od nuly (2–4 týdny) | extension `heightfield/index.html` (1–2 týdny) |

Důležitý kontext: ten "~200 GB pro 3D Tiles" je **hypotetická** velikost pokud by se postavila adaptive mesh pipeline od nuly — to v inzeratoru neexistuje. Realistická mesh alternativa z existujícího `gen_detail.py` rozšířeného na ČR = **1–5 TB**. Heightmap pyramid je tedy **3–14× menší než realisticky dostupná mesh varianta**, ne větší. Větší vychází jen vs hypotetický adaptive mesh, který by stál 2–4 týdny vývoje navíc.

Náš heightfield pyramid je **30× menší per-tile fetch** (12 KB vs 1 MB) než mesh-based. Pro real-estate use-case: heightfield vyhrává bandwidth + reuse existujícího kódu.

### Co server musí přidat oproti dnešnímu stavu

V `server.py` jen:

1. **Static handler na `/tiles/`** s `Cache-Control: public, max-age=31536000, immutable` (~10 řádků kódu)
2. **CORS** pokud chcete budoucí CDN
3. **`Accept-Encoding: br`** dá marginální zisk — LERC/KTX2 jsou už interně komprimované

Žádný nový dependency, žádný runtime CPU, žádný state. **Tile pyramid je dataset, ne služba.**

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

## Nevýhody přístupu

Honest list — co se za výhodu menší velikosti a jednoduššího serveru platí.

### A) Storage a regeneration cost

| Problém | Detail |
|---|---|
| Plný rebuild při ČÚZK update | DMPOK má 2-letý cyklus. Když ČÚZK vydá novou generaci, znovu projet ~42 h compute + převrtat 370 GB. Mesh-based by zvládl per-tile diff. |
| Atomic per-tile load | Klient potřebuje celý tile před dekódováním. Mesh 3D Tiles umí progresivní LOD (low-quality base + refinement). LERC je atomic. |
| Větší disk než hypotetický adaptive mesh | 370 GB vs teoretických ~200 GB pro mesh-based 3D Tiles **s adaptivní edge-collapse decimací**. Heightmap nemůže "smáčknout" rovné pole na 4 vertexy. ALE: ten 200 GB mesh přístup vyžaduje novou pipeline od nuly (2–4 týdny vývoje); ve vašem **reálném rozhodovacím prostoru** byly mesh alternativy 1–5 TB (per-location `gen_detail` rozšířený na ČR). Heightmap je tedy 3–14× menší, ne větší, vs realistická konkurence. |

### B) Render fidelity strop

| Problém | Detail |
|---|---|
| Pouze Y-displacement | Heightmap = 1 výška per XY = jeden vertex nad každým bodem. **Žádné jeskyně, převisy, vertikální zdi, mosty, balkony.** Mesh zvládá protože může mít víc vertexů na stejné XY. Pro real-estate OK, pro urban architekturu (ulice pod podloubím) problém. |
| Tile boundary seams | LERC ztrátová s `max_z_error=0.10 m`. Hraniční pixel jednoho tilu může být reprezentován jinak než hraniční pixel souseda → ~10 cm rozdíl na hraně. Při z=18 viditelné jako jemná čára. Mesh formáty mohou explicitně sdílet hraniční vertexy. |
| Normály z fragment shader = noisy edges | 4-sample gradient na ostrých hranách (rohy budov, lomy terénu) vrátí prudké normály které z různých vzdáleností renderují různě. Mesh má baked per-vertex normály = stabilní. |
| Static texture lock-in | Ortho je zapečené do KTX2. Nelze přebarvit podle výšky / sklonu / use-typu bez kompletního rebuilds. Mesh 3D Tiles s per-vertex attributes umí runtime recolor. |

### C) Ecosystem / interoperability

| Problém | Detail |
|---|---|
| Custom formát, není OGC | Náš pyramid neumí Cesium, MapLibre 3D, QGIS, ArcGIS. Renderuje jen náš JS klient. Pro distribuci jako otevřená data všichni musí použít náš viewer. |
| Žádná vector layer support | Parcely, adresy, POI, road network — nic z toho na heightmap pyramid neexistuje. Buď samostatný vector tile pyramid (MVT), nebo cadastre jako rastr PNG (~126 GB), nebo runtime fetch z ČÚZK WMS. |
| Single-vendor data dep | Vstup je ČÚZK DMPOK. Pokud ČÚZK změní formát / endpoint / licenci, pyramid je mrtvý. 3D Tiles dataset jde generovat z více zdrojů. |
| Žádné feature picking | Klik na pixel vrátí `(x, z, y_height)`. **Ne "budova #1234"**, ne "parcela 350/2". Pro to potřebujeme runtime spatial query proti `KladyMapovychListu` nebo lokální parcel raster. |

### D) Operační / engineering overhead

| Problém | Detail |
|---|---|
| Per-frame vertex shader cost | GPU každý frame přepočítá Y displacement pro každý vertex každého tilu. 16 tiles × 1M vertexů = 16M sample-and-multiply per frame. Moderní GPU dá, ale mobil tier 1 pojede 30 FPS místo 60. Mesh čte hotový vertex buffer = 0 výpočet per frame. |
| Žádný "base layer" trik | Mapbox / Cesium mají world-wide low-res s domain-specific high-res přepisem. Heightmap-pyramid tuhle warstvu nemá — mimo ČR (Slovensko, Polsko) hranice = sráz na 0. |
| Vendor JS knihovny | LERC přes `lerc.wasm`, KTX2 přes `BasisU.wasm`. Závislost na verzích, breaking changes při upgradech. Mesh-based tahá jen `gltf-loader` ze standardních specs. |
| Tile generation single-machine | 42 h compute na jedné mašině. Pokud kdy chcete týdenní re-pull, potřebujete cluster nebo akceptovat 2-denní stagnaci. |

### E) UX / aplikační overhead

| Problém | Detail |
|---|---|
| Bandwidth není zdarma | 50 KB/s při panningu = 30 MB / 10 min session. Na mobilu (5 GB měsíční plán) reálná položka. CDN pomůže, ale prvním uživatelům každého tilu z origin pořád stojí. |
| Latence mimo cache | První fetch z origin = ~100 ms (RTT + decode). Pro user-facing 60 FPS pan viditelná díra. Mesh-based s `tileset.json` může prefetchovat agresivněji, heightmap-pyramid musí klient hádat. |
| Žádné offline mode | Pro mobilní viewer offline cache potřebuje stažení celé regionální pyramidy (~5 GB / kraj). Mesh by mohl streamovat menší celky. |

### Shrnutí kompromisů

| Preferuje heightmap-pyramid | Preferuje mesh-based 3D Tiles |
|---|---|
| Real-estate session 5–10 lokací | Globální exploration mapou |
| Krajinné terény, vesnice | Urban architektura s podloubími |
| Vlastní viewer stack (Three.js) | Standard tooling (Cesium, MapLibre, ArcGIS) |
| Per-tile bandwidth | Storage efficiency |
| Implementační jednoduchost | Render flexibilita |
| Single source of truth (ČÚZK only) | Multi-source layering |

### Realistický verdikt pro inzerator

Pro náš use-case (real-estate listings, krajinné scény, sessions 5–10 míst) jsou nevýhody přijatelné:

- Caves / overhangs neřešíme (uživatelé nebydlí v jeskyni)
- Custom viewer je už náš asset
- 370 GB se vejde na Elements 22×
- Single-vendor data je realita českého trhu
- Per-frame vertex cost je zanedbatelný na cílovém HW

Hlavní reálné slabiny:

1. **Tile boundary seams při z=18** — viditelné při high-zoom screenshot. Řešitelné přes overlap (1–2 pixel) v generator skriptu.
2. **2-letý re-bake cyklus** — řešitelné přes incremental diff od ČÚZK (potřeba extra tooling).
3. **Žádné feature picking** — řeší se runtime ArcGIS query nebo separátní RUIAN raster (přidat do pyramidy jako 4. layer).

Nic z toho není „nepřípustné", ale je dobré to mít vědomě.

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
