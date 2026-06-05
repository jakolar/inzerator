# Session handoff — 2026-06-05

Snapshot pro pokračování po `/compact` + `/exit`. Hlavní téma dne: pivotovat z v2 GLB pipeline na heightfield-only + začít stavět CR-wide heightmap pyramid.

## Co je hotové dnes

### 1. v2 viewer + pipeline retirement
- `v2.html`, `gen_panorama.py`, `gen_detail.py`, `draco_compress_glb.py`, `verify_v2.py`, `viewer-realtor-overlay.js` → `TOBEDELETED/v2-viewer/`
- v1 viewery (`hnojice_*_multi.html`, `gen_multitile.py`, `inspector.html`, `tiles_hnojice_multi/`) → `TOBEDELETED/v1-viewer/`
- Per-slug v2 data (`panorama.glb`, `details/`, `_orig_uncompressed/`, top-level `manifest.json`) → `TOBEDELETED/v2-data/<slug>/` (~22 GB)
- `locations.py` STEP_NAMES `("panorama","sm5","outer","closeup","inner","compress","heightfield")` → **`("sm5","heightfield")`**
- `gen_heightfield.resolve_slug_paths()` decoupled — bere `--cx --cy` z CLI místo čtení v2 manifestu
- `server.py` resume-from-disk: 3-úrovňový fallback (location.json → heightfield manifest → legacy v2 manifest)
- `index.html` UI: HF promote na primární „Otevřít", smazaný v2 link + Recompress button
- Testy přepsané pro 2-step pipeline (59 passed, 6 skipped jako v2-dead-code)
- `CLAUDE.md` přepsán pro heightfield-only pipeline
- **Commits:** `75b541b` (refactor), `135ecc8` (test fix), `99ee8b8` (quarantine), `131976f` (docs)

### 2. Bulk DMPOK pull dokončen
- **944,8 GB, 16 299 SM5 listů, 0 fail / 0 missing** ✓
- Lokace: `/Volumes/Elements/cuzk-bulk/dmpok_tiff_<MAPNOM>/<MAPNOM>.tif`
- Bulk_dmpok.py PID gracefully ukončen po 100% completion
- ČÚZK kompletně zaindexována pro celou ČR

### 3. CR-wide heightmap pyramid MVP
- **`build_pyramid_tile.py`** — single tile builder ([`b5171c0`])
  - Web Mercator (z, x, y) input
  - Inventory cache `/Volumes/Elements/cuzk-bulk/inventory.json` (5 MB, jednorázová ~3 min cena)
  - Mosaic z SM5 → reproject S-JTSK → 3857 → 256² LERC
  - Atomic write (`.lerc.tmp` → rename)
- **MVP results** ([commit `1a3978e`]):
  - 4 test tiles @ z=14 + 3 @ z=18 (diverse Czech terrain)
  - LERC velikost @ z=14: 38–66 KB (avg 57 KB)
  - LERC velikost @ z=18: 12,5–29,4 KB (avg 19 KB)
  - Sheet seam analýza: **žádné viditelné seams** → no feathering needed
- **Test viewer** `pyramid-test.html` ([commit `78f608f`])
  - WASM lerc decode + Three.js displaced PlaneGeometry
  - Hypsometric color ramp
  - URL `/pyramid-test.html?z=N&x=X&y=Y`
  - Link v `index.html` header
- **Symlink** `/Users/jan/projekty/inzerator/cuzk-pyramid` → `/Volumes/Elements/cuzk-pyramid` (out-of-tree, machine-local)

### 4. Doc updates
- `HEIGHTFIELD_PYRAMID.md` rozšířen o:
  - „Revize plánu — 2026-06-05" sekci s důvody (Web Mercator potvrzen, bare-earth odložen, MVP first, …)
  - „MVP results 2026-06-05" sekci s benchmark tabulkami a revidovaný storage odhad **~210-220 GB** (vs původní 280 GB) pro DMPOK pyramid

## Otevřené vlákna

### 1. Block diagram skirt (heightfield/index.html)
- Status: **napsané, čeká na vizuální test**
- Iterace: BoxGeometry → scene-Y fix → texelXZ Z-flip fix → terrain-vertex resolution + bilinear interp
- Code v `heightfield/index.html` kolem řádku 1040-1240
- Uncommitted: ANO, ještě nebylo testováno
- Co dál: user mrkne v prohlížeči, podle výsledku buď commit nebo další iterace

### 2. Bulk pyramid build (přerušeno tu)
- **`bulk_pyramid.py` NEEXISTUJE — měli jsme začít**
- Architektura schválena:
  - ThreadPoolExecutor, 4 workers default
  - Per-tile file existence jako state (`.lerc` = done)
  - Pre-compute CR mask z `inventory.json` (skip outside-CR tiles)
  - Lock dir `~/Library/Caches/inzerator/bulk_pyramid-<hash>.lock/`
  - SIGTERM/SIGINT handler s stop event
  - Mode 1: `--z 14` build z source (mosaic + reproject + LERC)
  - Mode 2: `--z 13` aggregate z `z+1` (downsample 4 children 2×2 → 256²)
- **První bulk run = z=14** (~30k tiles, est ~12 h na 4 workers)
- Potom aggregation z=13 → z=8 (rychlé, ~hodina)
- Higher zooms (z=15..18) přidat inkrementálně poté

## Stav infrastruktury

### Servery a procesy
- `server.py` PID `53411` na port 8080, restartován během refactoru ✓
- `bulk_dmpok.py` proces gracefully skončil ✓
- Žádné aktivní jobs v paměti

### Disky
- `/Volumes/Elements/` 10 TB external HDD, 1,2 TB used (944 GB DMPOK + ~5 MB inventory + pyramid test tiles)
- Free ~8 TB, dost na pyramid (~220 GB DMPOK) + ortho (~150 GB) + bare-earth (~95 GB)
- ⚠️ Elements občas unmountovaná (USB sleep) — pre-bulk-run **zkontrolovat `ls /Volumes/Elements/`**

### Lokální dependencies
- `lerc` Python package: ✓ instalováno
- `rasterio`, `pyproj`, `numpy`: ✓
- WASM lerc v browseru: ✓ z `https://cdn.jsdelivr.net/npm/lerc@4/+esm`

## Konkrétní příští kroky

### Hned po pickupu:
1. **Vyřešit otevřené vlákno block diagram skirt** — buď commit (pokud funguje), nebo další iterace
2. **Napsat `bulk_pyramid.py`** — viz architektura výše. Cíl ~300-400 řádků.
3. **Test bulk_pyramid.py na malé oblasti** — např. jen `--tiles-bbox 49.5,17.3,49.7,17.5` (Stříbrnice + okolí, ~10 tiles). Validace state management + atomic writes + resume.
4. **Full bulk run z=14** — `python3 bulk_pyramid.py --z 14` (~12 h, nohup + caffeinate jako u bulk_dmpok)
5. **Aggregation z=13..8** — `python3 bulk_pyramid.py --z 13`, `--z 12`, ...
6. **Viewer integration** — modify `pyramid-test.html` na multi-tile pan/zoom, později nový `heightfield-cr/index.html`

### Otevřené otázky pro session 2:
- **Higher zooms strategie** — z=15..18 jako další bulk runs, nebo on-demand?
- **Bare-earth DMR5G** — kdy spustit bulk pull?
- **Ortho** — odložené, separátní bulk pull
- **CR-wide viewer** — refactor `heightfield/index.html` nebo nový from-scratch viewer?

## Klíčové soubory pro orientaci

```
HEIGHTFIELD_PYRAMID.md            — design + revize + MVP results (READ FIRST)
HEIGHTFIELD_PYRAMID_RATIONALE.md  — decision log + self-critique starší
BULK_DMPOK.md                     — runbook pro bulk DMPOK pull (referenční pattern)
BULK_DMPOK_PROFILE.md             — profile-driven optimizations
build_pyramid_tile.py             — single tile MVP, ~300 řádků
bulk_pyramid.py                   — TO BE WRITTEN
pyramid-test.html                 — E2E test viewer
heightfield/index.html            — main viewer, blocks diagram skirt code uncommitted!
locations.py                      — pipeline orchestration, STEP_NAMES = ("sm5","heightfield")
gen_heightfield.py                — per-location heightfield gen (zachován)
TOBEDELETED/                      — pending manual `rm -rf` review
```

## Co rozhodně NE-dělat

- ❌ Smazat `TOBEDELETED/` automaticky (user musí review)
- ❌ Reanimovat v2 / v1 pipeline (final retirement potvrzen)
- ❌ Editovat `gen_heightfield.py` per-location pipeline (per-listing zůstává netknutá)
- ❌ Rename `tiles_v2_` prefix (deferred, touch 8+ souborů)
- ❌ Zvýšit `bulk_pyramid.py` workers nad 6 bez profiling (USB I/O ceiling)
