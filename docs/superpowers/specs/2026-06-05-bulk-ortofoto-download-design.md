# Bulk ortofoto download — design

**Date:** 2026-06-05
**Status:** approved
**Author:** Jan + Claude

## Goal

Pull the entire ČÚZK ortofoto dataset (whole ČR, newest acquisition per SM5
sheet) to `/Volumes/Elements/cuzk-bulk/` as the canonical raw source for the
ortho pyramid pre-bake (HEIGHTFIELD_PYRAMID.md Phase 4) and for the server's
raw-crop ortho path. Mirrors the proven `bulk_dmpok.py` family.

## Why raw bulk (vs live WMS per tile)

The viewer prototype (`pyramid-test.html`) fetches ortho live from the ČÚZK
WMS, which **re-encodes** ČÚZK's JPEG into a second JPEG (double loss). The
raw ATOM distribution is the *same* source the WMS renders from, taken once
and kept verbatim — strictly higher fidelity, and regenerable offline without
hammering the WMS rate limit.

## Quality — maximum available, verified

| | value |
|---|---|
| resolution | 20000×16000 px/sheet = **0.1239 m/px** (JGW measured) |
| format | RGB **JPEG** (lossy), S-JTSK + UTM world files |
| distribution | single `WRTO24.<year>.<MAPNOM>.zip` per sheet |

ČÚZK publishes **no** lossless TIFF/JP2 in the open data — the JPEG is the
ceiling. A lossless equivalent would be ~6–7 TB (320 Mpx × 3 B ÷ ~2.3:1);
ČÚZK does not distribute it.

## Size & time — from real data

Per sheet ~63–76 MB JPEG (measured: BENE09/2025 = 62.9 MB, BLAN14/2024 =
75.5 MB). 16 301 sheets × ~67 MB ≈ **~1.1 TB** (ZIP deleted after extract;
keeping ZIPs would double to ~2 TB). Elements has 7.9 TB free.

Throughput mirrors DMPOK (1.14 TB in 3–4 nights 24/7 at 4 workers) →
**~3–5 nights 24/7**, ~6–8 on the 22:00–06:00 window.

Why not the user's 5–7 TB intuition: ortho has 16× DMPOK's pixels (0.125 m
vs 0.5 m) but JPEG costs ~0.20 B/px vs DMPOK's lossless float 2.76 B/px — the
16× pixel increase is cancelled by ~14× cheaper bytes, netting ~equal size.

## Inventory source — the ortho ATOM index (not KladyMapovychListu)

`bulk_dmpok_inventory.py` enumerates `KladyMapovychListu/25`. For ortho the
**authoritative list is the ortho ATOM index itself**
(`https://atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml`, 23.7 MB):

- 16 301 unique MAPNOM (each listed twice = id + link → dedupe).
- dataset id pattern: `..._WRTO24.<YEAR>.<MAPNOM>.xml`; years 2024/2025 by
  flight area. **Newest = max(year) per MAPNOM.**
- ZIP URL is **deterministic** (HEAD-verified for both years):
  `https://openzu.cuzk.gov.cz/opendata/ORTOFOTO/WRTO24.<YEAR>.<MAPNOM>.zip`

→ inventory is **one GET + regex**, no per-sheet feed fetch.

## Components (clone of bulk_dmpok family)

- `bulk_ortofoto_inventory.py` — parse ATOM index → `ortofoto_sheets.json`
  keyed by MAPNOM, each `{status, year, zip_url}`. Idempotent: preserves
  terminal states (done/failed/missing) on re-run. Distinct filename so it
  coexists with DMPOK `sheets.json` in the same `BULK_OUT_DIR`.
- `bulk_ortofoto.py` — same engine as `bulk_dmpok.py` (ThreadPool, 4 workers,
  0.5–1.5 s jitter, SIGTERM-safe pause/resume, atomic state flush, mkdir
  lock). Per sheet: download ZIP → extract `*.jpg` + `*.jgw` into
  `ortofoto_<MAPNOM>/` → **delete ZIP** → mark done. 404 → `missing`.
- `bulk_ortofoto_status.py` — counts + `--retry-failed`.
- `BULK_ORTOFOTO.md` — runbook (mirrors BULK_DMPOK.md).

## Output layout

```
/Volumes/Elements/cuzk-bulk/
  ortofoto_sheets.json
  ortofoto_<MAPNOM>/
    WRTO24.<year>.<MAPNOM>.jpg     # ~63–76 MB, 0.1239 m/px, S-JTSK
    WRTO24.<year>.<MAPNOM>.jgw     # world file (+ .TM33N/.TM34N variants)
```

Dir prefix `ortofoto_` matches the server raw-crop glob (`cache/ortofoto_*`),
so the same crop logic can read the bulk via a configurable search root.

## Operational notes

- `missing` for ortho should be **≈0** (full-ČR coverage). A high `missing`
  count signals a feed-parse / URL-pattern bug, not real gaps — sanity-check
  in the runbook.
- Year is preserved in `ortofoto_sheets.json` and embedded in filenames
  (`WRTO24.2025.BENE00.jpg`) for free — usable later for cycle-seam handling.
- DMPOK pull is 100% done (16 299/16 299), so no contention.

## Out of scope (YAGNI)

KTX2 pyramid generation (Phase 4, separate plan); cycle-seam color-matching;
`bulk_core.py` refactor (revisit when DMR5G is queued — then 3 consumers
justify it); launchd plists (driven from shell like DMPOK); wiring the server
raw-crop to read the Elements bulk (separate `INZERATOR_ORTHO_BULK` env
change).
