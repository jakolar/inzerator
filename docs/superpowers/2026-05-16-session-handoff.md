# Session handoff — Lokace UI + viewer polish (2026-05-15 → 2026-05-16)

Snapshot for a fresh Claude session picking up the inzerator project.

## What shipped (35 commits, baseline `aa56553` → HEAD `b8d27f0`)

End-to-end web UI for generating new v2 locations from RUIAN address search.
Plus a stack of v2.html viewer polish that emerged from real-world testing
on Kryry, Kamýk-nad-Vltavou, Děčín.

### Backend (new + modified)
- `locations.py` (~720 lines, new) — slug utils, disk scan, RUIAN search
  with diacritic tolerance + parcel layer, in-memory FIFO job queue,
  retry/cancel, background worker thread, **6-step pipeline**:
  1. `panorama` — `gen_panorama.py` (DMR5G 30 km terrain) — subprocess
  2. `sm5` — resolves MAPNOM codes via ČÚZK `KladyMapovychListu/25`,
     auto-downloads `dmpok_tiff_*` (SM5 DSM) + `ortofoto_*` (raw JPEGs)
     for outer-ring bbox. 3-attempt retry with backoff. **In-process.**
     Sentinel: `tiles_v2_<slug>/.sm5_ok`.
  3. `outer` — `gen_detail.py --half 2500 --step 2.5 --fade-to panorama`
  4. `closeup` — `--half 1500 --step 1.5 --fade-to outer`
  5. `inner` — `--half 500 --step 0.5 --fade-to closeup` (4 M verts)
  6. `compress` — Draco encodes each detail glb (~168 MB → ~30 MB at
     quant_pos=16, ≈15 mm error inner / 76 mm outer). Originals moved
     to `tiles_v2_<slug>/_orig_uncompressed/`. **In-process.**
     Sentinel: `tiles_v2_<slug>/.compress_ok`.
- `server.py` — new endpoints under ProxyHandler:
  - `GET /api/locations`, `GET /api/ruian/search?q=…`,
    `GET /api/jobs[?active=0|1]`, `GET /api/jobs/<id>`,
    `GET /api/sjtsk2wgs?cx=&cy=` (used by index.html verify map)
  - `POST /api/jobs` (with `resume_from_disk: true` support),
    `POST /api/jobs/<id>/retry`, `POST /api/jobs/<id>/cancel`
  - Process-wide `socket.getaddrinfo` monkey-patch filtering to IPv4
    (ČÚZK v6 path is dead from this LAN; v4 works)
  - `locations.start_worker()` called from `__main__`
- `download_ortofoto.py` — switched urllib → `requests` (urllib3 has
  proper IPv4 fallback when v6 fails)
- `draco_compress_glb.py` — fixed POSITION/TEXCOORD_0 unique_id mapping
  for DracoPy 2.0 (it sorts by attribute_type, not by encode() arg order)

### Frontend (new)
- `index.html` (~500 lines, new) — dashboard + create form:
  - Locations list scanned from `tiles_v2_*/manifest.json` with status
    badges (ready / partial / generating / fail)
  - "+ Nová lokace" form with **typeahead** RUIAN search (300 ms
    debounce), multi-token AND search, diacritic tolerance when input
    has a numeric token (server fetches broad, Python ASCII-folds)
  - **OSM verify map** (Leaflet 1.9.4 via CDN) shown after picking a
    hit — Potvrdit ✓ / Zpět buttons. Address hits zoom 18, parcel 17.
  - Slug auto-fill (`slugifyJS` mirrors Python `slugify`) with
    collision suffix (-2, -3, …)
  - Manual S-JTSK fallback when 0 hits (`<details>` collapsible)
  - Live polling `/api/jobs?active=1` every 2 s with step icons
    (⏳ pending, ⇣ N% downloading, ⚙ decoding, ✓ ok, ✗ fail, ↷ skipped,
    ⊘ cancelled), Retry + Cancel buttons per running/failed job
  - Resume-from-disk for `partial` locations (POST /api/jobs with
    `resume_from_disk: true`; backend reads cx/cy from manifest.json)
- `v2.html` — viewer fixes:
  - `?atmosphere=1` query flag (default off): sky HDR + fog + 15 km
    panorama mesh all become optional
  - `BUST = Date.now()` cache-bust suffix on `.glb` URLs
  - Loader `onProgress` hooks (⇣ X% during download, ⚙ during Draco)
  - DoF (`BokehPass`) commented out
  - Parcel outline: depthTest=true, single-pass HDR neon, **drapes
    over inner mesh DSM heightfield** (2001² Float32Array Y grid,
    bilinear sample matching gen_detail._sample_grid_y triangle
    convention). L-corner insertion at Y jumps ≥ 1.5 m for clean
    wall transitions over building roofs.
  - Camera retarget to inner-mesh centroid Y when region.ground_z
    mismatches village elevation (Kryry +50 m, Kamýk +80 m).
  - Parcel pick plane uses `controls.target.y` (post-retarget) instead
    of hard-coded 0.
  - Lazy `import { Muxer } from esm.sh` inside record handler (top-level
    fetch sometimes refused by network and breaks the whole module).
  - LOD shader-clip: each MeshBasicMaterial's `onBeforeCompile` injects
    fragment `discard` inside next-smaller LOD's world XZ bbox (works
    around gen_detail's child-hole carving requiring child-first order).
  - Record path retains `devicePixelRatio` (no `setPixelRatio(1)`),
    so canvas internal = fmt × pixelRatio → finer mipmap → crisp tex.

### Tests
- `tests/test_locations_unit.py` — 53 pytest unit tests (slug, disk scan,
  RUIAN search incl. diacritic + parcel, job state, retry/cancel, worker
  + subprocess pipeline mocked, partial glb cleanup, compress)
- `tests/test_locations_api.py` — 5 integration tests against live :8080
- All green at HEAD.

### Docs
- `docs/superpowers/specs/2026-05-15-lokace-ui-design.md` — design spec
  (frozen at brainstorming/spec phase; post-shipping deviations are
  documented in this handoff doc instead)
- `docs/superpowers/plans/2026-05-15-lokace-ui.md` — 13-task TDD plan
  (every task shipped)

## Current real-world state

- **3 generated locations:** `hnojice` (manually Draco-compressed in
  earlier session), `kamyk-nad-vltavou`, `kryry`
- All currently show `⚠ partial` because `.compress_ok` sentinel is
  missing. Clicking Retry on each runs only the compress step (other
  steps short-circuit via existing `.glb` files). For Hnojice the
  compress step is idempotent-skip on every detail (originals already
  in `_orig_uncompressed/`) → ~1 s. For Kryry/Kamýk it does real work
  (~1 min each). User intended to do this after the handoff.
- **6 SM5 sheets cached for Kryry** (`JESE43-55`) + **6 ortho JPEGs**
  for same. Likewise SM5 for Kamýk (`KRAH44-56`).
- Server running on `:8080`, accessible at
  `https://jans-mac-mini.tailfe475e.ts.net/` via Tailscale Serve

## Dirty working tree (user's in-progress work, do NOT commit on their behalf)

```
M gen_detail.py
M gen_multitile.py
M gen_panorama.py
M hnojice_lod_multi.html
M hnojice_multi.html
?? cache/                         (regenerated, gitignore-eligible)
?? jans-mac-mini.tailfe475e.*.crt (Tailscale cert — secret, never commit)
?? jans-mac-mini.tailfe475e.*.key
?? sky_cloudy.hdr                 (HDR asset, large)
?? tiles_hnojice_multi/           (older non-v2 layout)
```

User has explicitly never staged these across the entire 2-day session.
Continue this convention.

## Known issues / queued items

From the code review pass (commit `bcd1480` closed C1+C2+I2+I4+M7).
Remaining:

| ID | Severity | Area | Note |
|---|---|---|---|
| I1 | Important | server.py | Subprocess children (gen_*.py) don't inherit the IPv4-only `getaddrinfo` patch. Each ČÚZK call costs +1 s on the v6→v4 fall-through. Acceptable today; if it bites, see code review for fix options. |
| I3 | Important | locations.py | sm5/compress steps have no overall timeout. Cancel during sm5 only takes effect between tiles (max ~10 min stuck). |
| I5 | Minor | locations.py | Module reload would start 2 workers — YAGNI today. |
| M1 | Minor | locations.py | `_escape_like` `%`/`_` escapes are decorative without `ESCAPE '\'` clause; harmless. |
| M2 | Minor | locations.py | `parse_obec` regex assumes PSČ present. RUIAN currently always returns one. |
| M3 | Minor | index.html | Polling fixed 2 s; no exponential backoff. |

## Process-wide gotchas

- **IPv6 to ČÚZK is broken from this network.** server.py patches
  `socket.getaddrinfo` to filter to v4. Any new ČÚZK-touching Python
  code in server.py automatically benefits. Standalone scripts
  (download_*.py) explicitly use `requests` which has its own v4
  fallback.
- **`gen_detail.py` requires the uncompressed parent .glb** when
  building a child (reads parent's float32 POSITION for the fade band).
  The compress step moves originals to `_orig_uncompressed/` AFTER all
  details generated, so this is safe in the normal pipeline order.
  Manually re-running an old gen_detail invocation needs restoring
  from `_orig_uncompressed/` first.
- **Pipeline order matters for gen_detail.fade-to:** parent must exist
  before child. We generate panorama → outer (fade-to panorama) →
  closeup (fade-to outer) → inner (fade-to closeup). Reversing would
  break fade band Y sampling.
- **Pipeline order is WRONG for `gen_detail.children` hole carving:**
  outer would need closeup/inner in manifest to carve their bboxes
  out, but generation goes large→small. So outer/closeup ship SOLID
  and v2.html's fragment-shader `discard` papers over this at runtime.
  If/when this needs proper build-time carving, a two-pass generation
  (large→small for fade, then regenerate large with carve-children
  from already-existing manifest) would be the proper fix. See
  commit `9176f38` comment in v2.html for the runtime workaround.
- **`region.ground_z` from gen_panorama is the elevation at the 30 km
  panorama bbox center, NOT at the village center.** For non-flat
  regions (Kryry +50 m, Kamýk +80 m) the detail meshes end up at
  scene Y ≈ village_centroid, not 0. v2.html now retargets camera +
  parcel pick plane to inner mesh centroid Y. See commit `a030ddd`.

## Commit log (35 commits)

```
b8d27f0 feat: OSM verify-map step before generating new location
f55ed76 fix(v2): insert L-corner at parcel-outline wall transitions
f16f2e2 feat(locations): diacritic-tolerant address search + parcel search
345ec66 fix(locations): require .compress_ok sentinel for 'ready' status
eb5d50a feat(locations): Draco-compress detail GLBs as 6th pipeline step
f89ad67 feat(v2): drape parcel outline over inner mesh DSM heightfield
bcd1480 fix: code review follow-ups — worker resilience + deps + UX polish
9176f38 fix(v2,server): IPv4-only resolver + lazy muxer + shader-side LOD clip
a030ddd fix(v2): retarget camera to inner mesh centroid when ground_z mismatches
8a683a0 fix(locations): retry sm5/ortofoto downloads on transient ČÚZK errors
55d23cc fix(locations): sm5 step downloads BOTH DMPOK-TIFF + raw ortofoto
b626db4 feat(locations): auto-download SM5 sheets as 5th pipeline step
cb48c12 feat(locations,index): multi-token RUIAN search + typeahead
b39362a feat: resume-from-disk for partial lokace bez in-memory job
950bbc5 feat(index): diacritics tip + manual S-JTSK fallback
a966abc feat(index): polling + step status + retry/cancel buttons
d1b1696 feat(index): RUIAN search form + slug auto-fill
ecdd45e feat(index): dashboard skeleton (locations list, no form yet)
0dd9fac fix(server,locations): atomic slug-collision check + generic 500 error
7201641 feat(server): /api/locations + /api/ruian/search + /api/jobs endpoints
d30dba4 fix(locations): delete partial .glb on fail/cancel + remove dead flag
444ba4a feat(locations): worker thread + subprocess pipeline (inner step 0.5)
8b20d5c feat(locations): retry + cancel (state-only, worker bridge in Task 7)
af30610 feat(locations): job state + enqueue/get/list-active
6c36e76 feat(locations): ruian_search (AdresniMisto LIKE wrapper)
eb3096a feat(locations): disk scan (list_locations, status, expected_glb)
3a2aa16 feat(locations): slug utilities (slugify, validate, next-free, parse-obec)
8554971 feat(locations): module skeleton + state placeholders
eede6a8 fix(v2): drop ghost outline pass — buildings on top, glow underneath
eed0a21 fix(v2): dual-pass parcel outline + retain retina pixelRatio in rec
63b364f feat(v2): atmosphere flag + load progress + sky bg + inner 0.5m
968900c fix(draco): swap POSITION/TEXCOORD_0 unique_id mapping
f3e7093 docs(superpowers): implementační plán Lokace UI
dd9fabc docs(superpowers): spec revize po kritice + ČÚZK API spike
b333f43 docs(superpowers): lokace UI design spec
```

## Quick commands

```bash
# Restart server (kill existing on :8080 first)
lsof -ti:8080 | xargs -r kill
python3 server.py > /tmp/inzerator-server.log 2>&1 &

# Run all tests
python3 -m pytest tests/ -v

# Open dashboard (via Tailscale, from any device)
https://jans-mac-mini.tailfe475e.ts.net/

# Open viewer directly
https://jans-mac-mini.tailfe475e.ts.net/v2.html?region=<slug>
https://jans-mac-mini.tailfe475e.ts.net/v2.html?region=<slug>&atmosphere=1
```

## What the user is likely to ask next

- Click Retry on all 3 locations to compress them (will go ready→partial→ready
  with detail glbs shrinking 168 → ~30 MB each)
- Generate a new location through the dashboard with the OSM verify step
- Add more visual polish to v2.html (outline color/thickness, atmosphere
  defaults, video preset additions)
- Ship Draco compress with a higher `quant_pos` if 15 mm error in inner
  proves visible (default is 16 → 15 mm; 20 → 1 mm at +25 % size)
- Add a "force recompress" mode for partial → re-Draco with stronger
  quant if a location looks bad

The remaining queue from the post-review punch list (I1, I3, etc.) is
not blocking for normal use.
