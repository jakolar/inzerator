# F2 — map3d streaming viewer (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.
> Spec: `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md` kap. 6 (+ D1, D6).

**Goal:** `map3d/index.html` — bezešvý 3D viewer celé ČR nad dlaždicovou
pyramidou, streaming podle kamery.

**Architecture:** single-file HTML (konvence repa), three.js 0.170 z CDN
importmap (stejné verze jako `heightfield/index.html`). Quadtree
s replacement-refinement (rodič se vymění za děti, až jsou všechny 4 ready),
sdílená skirted grid geometrie, výšky přes vertex-shader displacement.

**Tech stack:** three.js 0.170, MapControls, lerc@4 WASM (CDN ESM),
`/cuzk-pyramid/dmpok/{z}/{x}/{y}.lerc` (on-demand server), ortho zatím
`/proxy/ortofoto` WMS JPEG per dlaždice.

## Global constraints

- Heightmap dlaždice: 256×256 LERC, hodnoty = absolutní metry n. m.,
  NODATA < -9000 → nahradit minimem dlaždice při dekódu.
- Zoom range klienta: z=8..18 (server umí do 20; UI strop 18 dle D6-A).
- Mercator korekce: svět v mercator metrech, výšky × `1/cos(lat₀)` (≈1,556)
  v shaderu (`uYScale`), volitelná exaggerace navrch (URL `?ex=`).
- Scene origin = střed startovního pohledu (fp32 přesnost — světové
  souřadnice ČR v EPSG:3857 jsou ~10⁶ m).
- Colorspace: ortho textury sample-ovat raw (NoColorSpace), passthrough do
  gl_FragColor, žádný composer (ponytail; composer + OutputPass až při
  vizuálním ladění — viz memory feedback_composer_colorspace).
- Czech UI, English code/comments.

## Deviace od spec kap. 6 (vědomé, pro MVP)

| Spec | MVP | Kdy povýšit |
|---|---|---|
| ortho KTX2 z `/tiles/ortho/` | `/proxy/ortofoto` WMS JPEG per dlaždice (F1 ještě neexistuje) | po F1 — přepnout URL builder, přidat KTX2Loader |
| LERC decode ve Web Workeru | main thread (256² ≈ 1 ms) | až profiling ukáže jank |
| LRU cache ~100 dekódovaných dlaždic | cache = mapa všech kdy načtených, evikce až nad 300 | až paměť reálně poroste |
| 404 → rodičovská dlaždice s UV offsetem | replacement refinement drží rodiče, dokud děti nejsou ready (řeší totéž bez UV offsetu) | pokud bude chybět coverage z=17..18 |

## Tasks

### Task 1: Tile math + skeleton
- [x] `map3d/index.html`: importmap, error overlay (kopie z heightfield),
      THREE scéna, MapControls, hemisféra + směrové světlo, resize handler.
- [x] Tile math: `lonLatToTile`, `tileToBBoxMerc` (EPSG:3857),
      `tileToBBoxWgs` (pro WMS), ČR bbox konstanty, URL params
      `?lat=&lon=&z=&ex=`.
- [x] Ověření: stránka se načte bez console errorů, prázdná scéna + grid helper.

### Task 2: Tile mesh + shader
- [x] Sdílená BufferGeometry: grid 128×128 segmentů + skirt ring
      (atribut `aSkirt`, shader spouští skirt verty o `uSkirtDrop` dolů,
      UV clamped na hranu). 128 stačí pro 256px texturu při šikmém pohledu;
      povýšit na 256 až podle vizuálu (ponytail).
- [x] ShaderMaterial: vertex displace z `uHeight` (LinearFilter,
      FloatType DataTexture), `uYBase`/`uYScale`/`uExag`; fragment: normála
      ze 4 tapů, hillshade × ortho passthrough.
- [x] Ověření: jedna hardcoded dlaždice (Stříbrnice z=14) renderuje jako
      v pyramid-test.

### Task 3: Streaming + quadtree
- [x] TileManager: `desired(camera)` — rekurzivní SSE selekce od ČR rootů
      (z=8), split když `screenPx(tile) > 384` && z < zmax && ve frustumu.
- [x] Fetch queue: max 6 souběžných, priorita = vzdálenost od look-at,
      LERC decode + WMS ortho load, stav per tile
      (empty/loading/ready/failed), 404 → trvale empty.
- [x] Replacement refinement: rodič v scéně, dokud všechny ne-empty děti
      nejsou ready; pak swap. Zpětný merge při oddálení.
- [x] Dispose: mesh + textury dlaždic mimo desired set (mimo cache limit).
- [x] Ověření: přelet ČR — dlaždice se doostřují, žádné díry, žádný
      memory leak (heap plateau).

### Task 4: Browser verification
- [x] chrome-devtools MCP: load `http://localhost:8080/map3d/`, konzole
      bez errorů, screenshot terénu, pohyb kamery → nové fetche.
