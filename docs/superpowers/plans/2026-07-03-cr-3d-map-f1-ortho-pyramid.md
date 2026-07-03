# F1 — ortho pyramid builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.
> Spec: `docs/superpowers/specs/2026-07-02-cr-3d-map-design.md` kap. 5 (+ D2, D3, D6).

**Goal:** `ortho/{z}/{x}/{y}` dlaždice celé ČR z bulk SM5 ortofoto cache,
konzumovatelné viewerem z F2.

**Architecture:** zrcadlí ověřenou heightmap dvojici — `build_ortho_tile.py`
(1 dlaždice: draft-decode JPEG sheets → rasterio reproject 5514→3857 →
256² JPEG) + `dispatch_ortho_pyramid.py` (base z=16 celoplošně, z=15..8
downsample 2×2 z dětí, resumable, SIGTERM-safe).

**Tech stack:** PIL (draft mode = DCT downscale, čte 320 MP sheet v 1/8),
rasterio.warp, pyproj, sdílený DMPOK inventář (`build_pyramid_tile
.load_or_build_inventory` — ortho sdílí SM5 grid), `basisu` (opt-in).

## Global constraints

- Výstup: `/Volumes/Elements/cuzk-pyramid/ortho/{z}/{x}/{y}.jpg`,
  256², q=87 progressive, atomic write (`.tmp` → `replace`).
- Base level **z=16** (1,5 m/px na zemi); z=17..18 populated-only až F3.
- Georeference: sheet `.jgw` je autoritativní; inventář jen filtr kandidátů.
- Mozaika = reproject per sheet přímo do cílového 3857 gridu (žádný
  mezikanvas → žádná dvojitá převzorkovací chyba).

## Deviace od spec (vědomá): JPEG první, KTX2 jako pass

Spec D2 volí KTX2 ETC1S. Bulk encode je ale vícedenní commitment a riziko
kvality na vegetaci je v spec sekci 9 explicitně. Proto: primární výstup
`.jpg` (viewer ho konzumuje TextureLoaderem okamžitě, F2 fallback řetěz
zůstává), `--ktx2` flag přidá `.ktx2` vedle. KTX2 bulk pass se spustí až
po vizuální validaci vzorků — idempotentně nad hotovými JPEGy.
Trade-off: JPEG = 6× více GPU paměti per dlaždice (256 KB vs 43 KB);
při ~32 aktivních dlaždicích je to 8 MB vs 1,4 MB — pro MVP nepodstatné.

## Tasks

### Task 1: build_ortho_tile.py
- [x] `read_jgw(path)` — world file → (px_size, origin) — a
      `pick_draft_k(target_mpp, native_mpp)` → 1/2/4/8.
- [x] `sjtsk_envelope(z, x, y, margin_m)` — merc bbox rohy+středy hran
      přes pyproj do 5514 (Křovák rotace ~7,7°).
- [x] `build_ortho_tile(z, x, y, bulk_dir, out_dir, size, ktx2, overwrite)`
      — inventář → sheets → draft decode+crop → reproject lanczos →
      JPEG atomic; vrací ok/empty.
- [x] CLI `--z --x --y [--size 256] [--ktx2] [--overwrite]`.
- [x] Test: unit `pick_draft_k` + envelope obsahuje reprojektované rohy;
      ručně 1 dlaždice Šternberk z=16 a vizuální kontrola.

### Task 2: dispatch_ortho_pyramid.py
- [x] Klon dispatch_pyramid struktury: base z=16 přes ThreadPool
      (default 3 workers — heightmap dispatch může běžet souběžně),
      z=15..8 `_build_agg` (4 děti → 512² → LANCZOS 256²).
- [x] Resumabilita existencí `.jpg`, SIGTERM drain, countery, log
      `ortho_build.log`, `--center/--win` smoke okno.
- [x] Test: unit downsample (kvadrantové barvy), smoke okno Šternberk
      z=15..16.

### Task 3: viewer integrace
- [x] `map3d/index.html`: ortho load řetěz — pyramid `.jpg` → (z≥14) WMS
      fallback → hypsometrie. Žádný jiný zásah.
- [x] Ověření chrome-devtools: dlaždice ze smoke okna se servírují ze
      statiky (network: `/cuzk-pyramid/ortho/...` 200), WMS jen mimo okno.

### Task 4: commit
- [x] pytest unit, commit feat branch, merge.
