# Multi-tile mesh seam gaps — Open3D simplification suspect

## Symptom

Při zoomu na Hnojice 3D viewer (a presumably i na ostatní `*_multi.html` lokality generované přes `gen_multitile.py`) jsou občas vidět **„prázdná místa" na švech mezi sousedními tile** — tenké svislé proužky/díry kde se setkávají dva GLB tile. Ne systematicky všude, jen občas.

Reportováno user-em 2026-05-08 v hnojice viewer.

## Co je správně v pipeline (kontrola podle gen_multitile.py)

**Geometrie tile** je navržená aby seamy seděly:

1. **Sdílená sample grid** — `extract_tile()` line 163-177: tile A's rightmost vertex column je v stejné world-space pozici jako tile B's leftmost. Šev je jedna sdílená čára, ne 1m overlap → no Z-fighting in overlap zone.

2. **Frozen edge band** — line 219-229: outermost 2 řady/sloupce vertices jsou freezované při `_snap_vertices_to_footprints` aby footprint snapping nepouložil oba sousedy do různých pozic.

3. **Post-simplify snap-back** — line 305-335: po `simplify_quadric_decimation` loop walk-uje all simplified vertices a každý co je do 0.5m od edge je snapnutý NA EXACT EDGE position. Y se restoruje z `patch_ds[r_i, c_i]` (zdrojový raster, sdílený mezi sousedy přes `rasterio.merge`). Plus clip ≥ 0 pro pit/negative-height handling.

4. **Sdílený `global_ground_z`** — line 524: všechny tiles používají stejnou ground_z, takže "height above ground" semantika je konzistentní. Ne per-tile percentile.

5. **GLB export bez komprese** — line 54: `pygltflib.FLOAT` pro POSITION, žádný Draco quantization. Vertex pozice jsou bit-exact.

## Co může selhat

**A) Open3D `simplify_quadric_decimation` občas drops boundary vertex** — přes `boundary_weight=1000` má vyšší cenu, ale algorithm STÁLE může collapsnout boundary vertex pokud je gradient cost-to-keep elsewhere víc strmý. Snap-back loop walk-uje **jen surviving** vertices, takže když celý vertex zmizí, snap nemá co opravit → topologická díra na šveu.

**B) `remove_non_manifold_edges()` (line 297)** — po simplifikaci může cleanup pass dropnout boundary triangle pokud je třeba součástí 3-share edge artefaktu. Méně časté než A, ale možné.

**C) JPEG ortofoto compression seam** — každý tile fetchuje vlastní `/proxy/ortofoto?BBOX=...&size=768`. JPEG 8×8 DCT bloky se nealignují přes adjacent tiles → tenká vizuální linie, ALE nikdy „empty místa" protože textura je tam, jen mírně discontinuous. Pravděpodobně NENÍ zdrojem reportu.

**D) Smoothing edge effect** (jen když `--smooth > 0`) — `gaussian_filter` aplikuje na `patch_ds` AŽ PO downsample. Edge pixely filteru používají reflected/extrapolated values → mismatching mezi adjacent tiles. **Pro hnojice irelevantní** protože default `--smooth=0` (line 395).

**Nejpravděpodobnější je A.**

## Diagnostické kroky (rychlé na nejpomalejší)

### 1. Visual confirmation

Otevři viewer, zoom na různá místa (use OrbitControls), porozhlédni se po tenkých svislých proužcích nebes prosvitajících mezi sousedy. Screenshot pro reference. Zaznamenat které tile pairs mají gaps (gridcol/gridrow) — z toho lze odvodit reproducibility.

### 2. Regenerate s `--no-simplify`

```bash
cd /Users/jan/projekty/gtaol
python3 gen_multitile.py --output hnojice_multi.html --glb --no-simplify
```

Vygeneruje tiles bez `simplify_quadric_decimation`. Tile pack bude **3–5× větší** (více triangles), download/render pomalejší. Ale pokud gaps zmizí → potvrzená příčina = Open3D simplify.

Trade-off: většina tiles tile zvedne z ~100 KB na ~400-500 KB. 625 tiles → z 80 MB na ~250-300 MB. Pro local dev OK, pro production deployment moc.

### 3. Vyšší `boundary_weight`

Změnit line 289 z `boundary_weight=1000.0` na `boundary_weight=10000.0` nebo `100000.0`. Open3D má numerické limity, vysoká hodnota je ekvivalentní „infinity → never collapse". Možná pomůže, možná ne — záleží na vnitřní implementaci Open3D.

### 4. Cílený fix — rekonstruovat boundary topologii po simplify

Komplikovaná oprava. Pseudocode:
```python
# Po simplify + cleanup, sebrat all surviving boundary vertex positions
# Porovnat s expected boundary positions z předkompenzace (lx_min_edge etc).
# Pro každý CHYBĚJÍCÍ expected boundary vertex:
#   1. Přidat vertex do sv s Y z patch_ds
#   2. Najít nejbližší surviving boundary vertex(y)
#   3. Přidat retro-aktivní triangle s těmito vertices
```

Risky — Open3D nemá easy hook pro tenhle workflow, museli bychom opustit Open3D mesh strukturu a pracovat s raw arrays.

### 5. Použít jinou simplify knihovnu

`pyacvd`, `meshlab`, vlastní quadric decimation s explicit boundary lock. Větší rewrite.

## Doporučený další krok

Spustit (2) — `--no-simplify` regeneration. Změřit:
- Gap visibility (zmizely / zůstaly)
- Total tile pack size (jak moc se nafoukl)
- Render frame rate v Chrome

Podle výsledků rozhodnout:
- Gaps zmizely + size únosný → změnit default na no-simplify
- Gaps zmizely + size moc velký → investovat do (4) custom boundary fix
- Gaps zůstaly → simplify NEBYL příčinou, hledat jinde (smoothing? rasterio.merge alignment?)

## Související

- THREE.js color space fix (commit `6957639`, doc `three-js-colorspace-srgb.md`) — řešil saturaci, NESOUVISÍ s seam gaps
- gen_multitile.py post-simplify snap-back (lines 305-335) — current best-effort fix, funguje pro většinu vertices ale ne pro úplně dropnutý ones

## Status

**Open** — nezablokované, není urgent (user reportoval „obcas", nezakazuje použití viewer-u). Vyřešit při příští iteraci na area-viewer pipelinu.
