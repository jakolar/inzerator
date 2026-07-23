# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Inzerator generates 3D village viewers from Czech RÚIAN + ČÚZK ortofoto + DSM data, intended as listing/marketing tooling. MVP / personal use.

Single viewer stack as of 2026-06:

- **Heightfield viewer** (`heightfield/index.html`, ~3.9k lines): LERC heightmaps + KTX2 ortho tiles streamed from `tiles_v2_<slug>/heightfield/`. ~10 MB per region. Opened via `/heightfield/?slug=<slug>` from the dashboard.
- **Image edit UI** (`image-edit/index.html`): OpenAI gpt-image-1 proxy for orthophoto / mesh-texture repair.

Earlier viewers (v1 single-file `*_multi.html` and v2 GLB mesh `v2.html`) were retired in June 2026. Sources + per-location GLB data are quarantined under `TOBEDELETED/` pending manual rm — git log preserves the source history, but the code is no longer reachable.

## Server (`server.py`, ~3.5k lines)

Single threaded `http.server.ThreadingHTTPServer` on **port 8080**, bound to `0.0.0.0`. Plain HTTP — WebCodecs / MediaRecorder / clipboard need a secure context, so wrap with Tailscale Serve (or any TLS proxy) when accessing from outside `localhost`. Self-signed cert for `jans-mac-mini.tailfe475e.ts.net` is checked in.

Force-IPv4 monkey-patch on `socket.getaddrinfo` at module load — ČÚZK's IPv6 path is dead from this LAN; v4 works. If you see `Connection refused` on ČÚZK endpoints, that shim is the first thing to check.

Concurrency caps: `_CUZK_SEM = Semaphore(6)` (across all threads) and `_OSM_SEM = Semaphore(2)` (Overpass / OSM TOS). Don't raise without a reason — 9 HD tiles × 6 workers already saturates ČÚZK.

Endpoints (all under `ProxyHandler.do_GET` / `do_POST`):

- `/proxy/ortofoto`, `/proxy/cadastre`, `/proxy/osm` — ČÚZK / OSM tile/WMS proxies, in-process LRU caches (`_CUZK_CACHE`, `_BoundedCache`).
- `/api/locations`, `/api/ruian/search?q=`, `/api/sjtsk2wgs`, `/api/jobs[/<id>][/retry|/cancel]`, `/api/estimate` — Lokace UI; backed by `locations.py`. POST `/api/jobs` optionally takes `inner_half` (m, selection-driven ring sizing) + `parcel_ids` (subject parcels the viewer pre-highlights), or a `polygon` ring instead of `cx`/`cy` (see Pipeline section). POST `/api/estimate` is the pre-submit demand check for a drawn polygon.
- `/cuzk-pyramid/dmpok/<z>/<x>/<y>.lerc` — heightmap pyramid tiles, built **on demand** from bulk DMPOK for z=14..20 (`_PYRAMID_BUILD_SEM = Semaphore(3)`, per-tile locks) and served from disk once built. Lower zooms come pre-baked by `dispatch_pyramid.py`.
- `/api/buildings`, `/api/parcels`, `/api/parcel-at-point`, `/api/roads`, `/api/poi`, `/api/wiki`, `/api/building-detail` — viewer queries.
- `/api/image-edit` (POST) + `/api/image-edit/prompt`, `/api/image-edit/status` — OpenAI gpt-image-1 proxy. Refuses with 503 unless `INZERATOR_API_TOKEN` is set (intentional: shared LAN, OpenAI billing attaches to the key). System prompt is read server-side from `image_edit_prompt.txt` — single source of truth, clients cannot override.

`locations.start_worker()` is called from `__main__` — kills the background job worker if you skip it.

## Pipeline (`locations.py`)

`STEP_NAMES = ("sm5", "heightfield")` after the v2 retirement. Both steps run for every new location.

Per-step output lives under `tiles_v2_<slug>/` (the directory prefix is unchanged — kept as legacy convention until a separate rename pass):

| Step | Output | Notes |
|------|--------|-------|
| `sm5` | `.sm5_ok` sentinel | In-process. Resolves MAPNOM via ČÚZK `KladyMapovychListu/25`, then gets `dmpok_tiff_*` (SM5 DSM) + `ortofoto_*` JPEGs into cache. **Both are copied from the local bulk archive** (`/Volumes/Elements/cuzk-bulk`, override/disable via `BULK_DMPOK_DIR` / `BULK_ORTOFOTO_DIR`) when mounted — `download_tiff._copy_from_bulk` + `download_ortofoto._copy_from_bulk`, ~0.3–0.5 s/sheet vs. minutes of flaky ČÚZK ZIP/ATOM downloads; each falls back to its network path on a bulk miss. The full-ČR DMPOK **and** ortofoto pulls both live under `cuzk-bulk` (`dmpok_tiff_<CODE>/` and `ortofoto_<CODE>/`). |
| `heightfield` | `heightfield/manifest.json` + LERC + KTX2 tiers | Subprocess `gen_heightfield.py --slug X --cx X --cy Y` — writes LOD streaming assets that `heightfield/index.html` reads. |

`location_status()` reports `missing` (no slug dir) / `partial` (dir but no heightfield manifest) / `ready` (heightfield manifest present). `STEP_TIMEOUT_SECS = 3600` (cold DMR5G can take 10–30 min).

`tiles_v2_<slug>/location.json` is written by `enqueue_job` and persists `{slug, label, cx, cy, created_at}` (+ optional `inner_half`, `subject_parcels`) so the dashboard label survives across server restarts. `subject_parcels` drives the viewer's pre-highlight — match parcel ids **numerically**, ČÚZK returns them as floats. Legacy v2 manifest fallback is still wired in `_read_label()` and the resume-from-disk endpoint for lokace that pre-date the retirement.

Atomic manifest write pattern: `tmp = path.with_suffix('.json.tmp'); tmp.write_text(...); tmp.replace(path)`.

`POST /api/jobs` also accepts polygon mode — `{slug, label, polygon}` with a WGS `[lon, lat]` ring (3–200 vertices, drawn via map3d's ✏️ výřez button) instead of `cx`/`cy`. `locations.polygon_extent()` is the single source of truth for deriving `cx`/`cy` (S-JTSK bbox centre) and a clamped `inner_half` from the ring, shared with `POST /api/estimate` (sheet-cache-aware demand check — resolves MAPNOM codes and reports `sheets_total`/`sheets_cached` with no downloads, used by the draw sheet before submit). `location.json` for a polygon job additionally carries `polygon` (WGS ring) and `polygon_local` (viewer-space metres relative to `cx`/`cy`). The heightfield viewer reads these to activate island mode — fragment-shader discard outside the polygon mask plus curtain walls along the cut edges, in place of the square pedestal. Spec + plan: `docs/superpowers/specs/2026-07-21-polygon-cutout-page-design.md`, `docs/superpowers/plans/2026-07-21-polygon-cutout-page.md`.

## Cache layout

`cache/` is **a symlink** to `../gtaol/cache` — DSM TIFFs (`dmpok_tiff_*`) and ortofoto JPEGs (`ortofoto_*`) are shared with the gtaol experimental project. Override with `INZERATOR_CACHE` env when running from another worktree. The cache is large (regenerable from ČÚZK).

`cache/jobs/` holds per-job log JSON written by the Lokace worker.

`cuzk-pyramid/` is **a symlink** to `/Volumes/Elements/cuzk-pyramid` — the ČR-wide heightmap pyramid output (`dmpok/<z>/<x>/<y>.lerc`). Server endpoints 404 gracefully into on-demand builds when the external drive holds the data; if the symlink target is unmounted, pyramid features are dead, not broken code.

## Heightfield generation (`gen_heightfield.py`)

LOD ring assets for the streaming viewer. Defaults: ortho tiers = `mid,high,ultra`, format = `lerc` (50% smaller than PNG, WASM-decoded), bare-earth DMR5G **cached** to `<slug>_bare.lerc` and skipped on rerun unless `--refresh-bare`. SM5 ortofoto auto-fetched via ČÚZK ATOM feed unless `--no-fetch-missing`.

```
python3 gen_heightfield.py --slug hnojice --cx -547700 --cy -1107700   # new lokace path
python3 gen_heightfield.py --slug hnojice                              # re-gen existing (centre from heightfield manifest)
python3 gen_heightfield.py --slug hnojice --refresh-bare               # also re-fetch DMR5G
python3 gen_heightfield.py --slug hnojice --inner-half 1200            # selection-sized rings (clamped 500–2000 m, see derive_rings)
python3 dispatch_heightfield.py                                        # all ready locations missing heightfield/
python3 dispatch_heightfield.py --only foo,bar --force                 # subset, regenerate existing
python3 refresh_ortho.py --tiers super                                 # opt-in 16384² super tier (~40 MB KTX2, 128 MB GPU)
python3 refresh_cadastre.py --size 8192 --only foo                     # re-pull cadastre PNGs only (server :8080 must be up)
```

`resolve_slug_paths()` finds the centre in this order: explicit `--cx`/`--cy`, then `heightfield/manifest.json`, then the legacy v2 top-level manifest (for pre-retirement lokace whose data hasn't been moved out of TOBEDELETED yet).

`refresh_cadastre.py` and `refresh_ortho.py` both expect `tiles_v2_<slug>/heightfield/manifest.json` to exist (they update rings, not generate from scratch).

## Common commands

```bash
# Install pinned deps (Python 3.9.6 / macOS).
pip install -r requirements.txt

# Server (port 8080, 0.0.0.0). Loads .env first (OPENAI_API_KEY, INZERATOR_API_TOKEN).
set -a; source .env; set +a; python3 server.py

# Tests — pytest. Unit tests run standalone; *_api.py and test_parcels_endpoint.py
# need server.py running on :8080.
pytest                                            # all
pytest tests/test_locations_unit.py               # unit only (no server needed)
pytest tests/test_parcels_endpoint.py -k area     # single test
```

Generating one location end-to-end is driven from the Lokace UI (`index.html` → typeahead RÚIAN search → "+ Nová lokace"). Manual fallback:

```bash
python3 gen_heightfield.py --slug <slug> --cx <cx> --cy <cy>
```

Negative S-JTSK coords need the `=` form (`--cx=-547700`), otherwise argparse swallows the minus as a flag.

## Conventions specific to this repo

- **Negative ČÚZK coordinates** are normal — S-JTSK Krovak East-North has negative X and Y across all of Czechia. Use `--cx=<negative>` form on CLI scripts.
- **Atomic manifest writes**: `tmp = path.with_suffix('.json.tmp'); tmp.write_text(...); tmp.replace(path)`. Match it when adding new manifest writers.
- **Slug rules**: lowercase, ASCII, hyphen-separated. `locations.slugify` does NFD-fold; `slugifyJS` in `index.html` must stay byte-for-byte equivalent (the UI auto-fills slug from the chosen RÚIAN address; mismatch → wrong directory).
- **Sentinels for in-proc steps**: `.sm5_ok` is a zero-byte marker used by `expected_glb()` / `location_status()` so the resume-skip loop can `.exists()`-check uniformly. Don't replace with a different file-existence check.
- **`tiles_v2_` directory prefix** is unchanged after the v2 retirement — rename to `tiles_` is deferred until a separate sweep touches all hard-coded references (`gen_heightfield.py`, `dispatch_heightfield.py`, `refresh_*.py`, `locations.py`, `server.py`).
- **Czech UI, English code/commits** — matches the global rule in `~/.claude/CLAUDE.md`.

## Bulk downloads (DMPOK surface + DMR5G bare-earth + ortofoto)

ČÚZK DMPOK-TIFF mass pull lives in `bulk_dmpok.py` + `bulk_dmpok_inventory.py` + `bulk_dmpok_status.py` + `bulk_dmpok_profile.py`, writing to `/Volumes/Elements/cuzk-bulk/` by default (override `BULK_OUT_DIR`). 4 workers, ~10 sheets/min observed, ~3 nights for the full 16 299-sheet ČR pull. See `BULK_DMPOK.md` for runbook and `BULK_DMPOK_PROFILE.md` for the performance rationale. DMPOK is the **surface** model (0.5 m, includes buildings + vegetation).

`bulk_dmr5g.py` is the ground-only twin: the DMR 5G **bare-earth** product (LAZ point clouds, one `<MAPNOM>.laz` per SM5 sheet) from `openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/`. Same signal-safe/resumable/atomic-state engine as `bulk_dmpok.py`; writes a flat archive to `/Volumes/Elements/cuzk-dmr5g/` (override `BULK_DMR5G_OUT_DIR`). Full ČR = 16 299 sheets ≈ **45 GB** (measured 2026-07-20, all `done`, 0 missing/fail, ~2h44m). Seed its `sheets.json` once from the identical DMPOK MAPNOM grid — see the module docstring. It's a **point cloud**, not a raster: to use as terrain, rasterise to a grid (the per-location pipeline still fetches the DMR5G ImageServer raster directly; see `fetch_dmr5g` in `gen_heightfield.py`).

`bulk_ortofoto.py` + `bulk_ortofoto_inventory.py` + `bulk_ortofoto_status.py` are a clone of the same engine for the full ortofoto dataset (~1.1 TB JPEG, newest acquisition per sheet). Runbook in `BULK_ORTOFOTO.md`. Sanity check there: `missing` should stay ~0 — a climbing count means a broken ZIP url pattern, not a coverage gap. **This pull is complete** (16 299 `ortofoto_<CODE>/` dirs under `cuzk-bulk` as of 2026-07, `WRTO*.jpg` + `.jgw`), so the `sm5` step copies ortofoto from disk — see the Pipeline table.

## Heightmap pyramid (ČR-wide)

Web Mercator LERC pyramid built from the bulk DMPOK cache; design + tile-count rationale in `HEIGHTFIELD_PYRAMID.md`. z=8..14 is pre-baked bottom-up (z=14 from DMPOK mosaic, z=13..8 downsampled 2×2 from children); z=15..20 is built on demand by the server endpoint (a high-zoom tile touches only a few sheets).

```bash
python3 build_pyramid_tile.py --z 14 --x 8975 --y 5635    # single tile (writes inventory.json on first run)
python3 dispatch_pyramid.py --workers 4                    # full ČR z=8..14; filesystem-resumable, SIGTERM-safe
python3 dispatch_pyramid.py --center 50.736 15.74 --win 4 --zmin 12   # smoke test window
```

`pyramid-test.html` is the test viewer (heightmap + ortho drape served from local data by `server.py`).

## Docs worth reading before touching the relevant area

- `docs/notes/2026-05-16-pipeline-overview.md` — pre-retirement pipeline overview (mentions v2; read with retirement in mind).
- `docs/notes/three-js-colorspace-srgb.md` + `2026-05-16-ortofoto-color-grading.md` — colorspace pitfalls; matches the `feedback_composer_colorspace.md` memory.
- `docs/notes/2026-05-19-cuzk-data-resolution.md` — DMR5G vs SM5 vs DMP1G resolution ladder.
- `BULK_DMPOK.md` + `BULK_DMPOK_PROFILE.md` + `BULK_ORTOFOTO.md` — bulk download runbooks + profile.
- `HEIGHTFIELD_PYRAMID.md` + `HEIGHTFIELD_PYRAMID_RATIONALE.md` — design + decision log for the ČR-wide tile pyramid (now partially built — see Heightmap pyramid section).
- `cuzk-bulk-email.md` — draft access request to ČÚZK for bulk DMR5G/SM5.
