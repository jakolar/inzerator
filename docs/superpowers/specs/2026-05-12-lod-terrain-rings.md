# LOD terrain rings — Design Spec

**Status:** approved (brainstorming-by-discussion 2026-05-11, 2 iterations)
**Goal:** Render dramatically larger surrounding area than current 3km × 3km (e.g. 14.4km × 14.4km for flat villages, up to 30km × 30km for mountain locations) while keeping native-resolution detail concentrated around the subject property.

## Motivation

Real-estate viewer's drone-video presets (locator zoom, context arc, sunset orbit, reveal pull-up) start at altitudes/distances that today reach the **edge of the rendered area** — beyond ~1.5km the world ends and the camera sees blue sky. This kills the cinematic feel. The fix: render much more surrounding terrain, but at progressively lower mesh resolution as distance from the subject grows.

The user always knows what the subject is (it's a specific parcel/building referenced from the realtor backend), so the LOD anchor is well-defined.

## Approach: concentric LOD rings

Four LOD levels at concentric radii from the subject anchor. Each ring uses **larger tiles at coarser mesh step** as distance grows, keeping per-tile vertex count roughly bounded.

### Reference profile — `village_flat` (Hnojice, Šantovka)

| Ring | Radius (m) | Tile size (m) | Mesh step (m) | Skirt depth (m) | Buildings min m² |
|------|------------|---------------|---------------|-----------------|------------------|
| L0 (detail) | 0–360 | 120 | 0.5 | 8 | 0 (all) |
| L1 (vesnice) | 360–900 | 240 | 1.0 | 12 | 30 |
| L2 (okolí) | 900–2400 | 480 | 4.0 | 20 | none |
| L3 (panorama) | 2400–7200 | 1200 | 16.0 | 40 | none |

**Coverage:** 14.4km × 14.4km. **Approximate tile count:** 36 + 24 + 24 + 16 = ~100 tiles. **GLB total:** ~4 MB (vs. dnešních ~80 MB pro 9 km²).

### Reference profile — `mountain` (Sněžka)

| Ring | Radius (m) | Tile size (m) | Mesh step (m) | Skirt depth (m) | Buildings min m² |
|------|------------|---------------|---------------|-----------------|------------------|
| L0 | 0–500 | 100 | 0.5 | 30 | 0 |
| L1 | 500–1500 | 200 | 4.0 | 60 | 50 |
| L2 | 1500–4500 | 600 | 16.0 | 120 | none |
| L3 | 4500–15000 | 2500 | 64.0 | 250 | none |

**Coverage:** 30km × 30km. **Skirt depths scaled** for mountainous elevation range.

## Seam handling — snap-vertex stitching

The hardest technical challenge. Where two rings meet (L0↔L1, L1↔L2, L2↔L3), tile edges have **different vertex densities**. Without intervention, sampling DMP at different positions produces **different Y values for the same X-Z point**, creating visible "step" artifacts at ring boundaries — especially objectionable at the shallow drone camera angles this viewer specifically supports.

**Mechanism:** During `extract_tile`, when generating a tile on a ring boundary:
1. For each of the 4 edges, identify the neighboring ring (if any).
2. If neighbor has **coarser** mesh: this tile must include T-vertices at the coarser ring's vertex positions on the shared edge. The Y at each such T-vertex is sampled from the DMP at the coarser ring's stride.
3. If neighbor has **finer** mesh: no action — this tile's edge already has fewer vertices, but the neighbor will insert T-vertices matching this tile.
4. Result: at every shared X-Z position on a boundary, **both tiles have the same vertex Y** → no step.

**Fallback:** vertical skirt rims (this tile's edges extended downward by `skirt` meters) on the outermost ring (no neighbor) and as defense-in-depth if snap-vertex misses an edge case.

## Renderer changes (base + template)

1. **Logarithmic depth buffer.** `new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true })`. Eliminates z-fighting at the 1km+ range required for L3 horizon.
2. **Camera far clip.** Increase from 5000 to **15000** for village profile, **30000** for mountain profile. Set via profile.
3. **Camera near clip.** Increase from 0.1 to 1.0 — reclaims depth precision; 1m near is fine because the user never zooms closer than ~10m to terrain.
4. **Exponential fog.** `scene.fog = new THREE.FogExp2(profile.fog_color, profile.fog_density);` Default village color `0xb0c4d8` (haze blue), density `0.00012`. Match `scene.background` to the fog color so horizon blends seamlessly.
5. **Cadastre overlay** (already exists as raster textured layer) — limit to L0 + L1 rings only (skip generation/load for L2+ since the level of detail is meaningless at that scale and the WMS would rate-limit).

## Configuration: per-location LOD profile

Extend `LOCATIONS` dict in `gen_multitile.py`:

```python
LOCATIONS = {
    "hnojice": {
        "cx": -547700, "cy": -1107700,
        "output": "hnojice_multi.html",
        "label": "Hnojice",
        "lod_profile": "village_flat",   # NEW
    },
    "snezka": {
        ...
        "lod_profile": "mountain",
    },
}
```

Profiles are defined as module-level constants in `gen_multitile.py`:

```python
LOD_PROFILES = {
    "village_flat": {
        "rings": [
            {"r_max": 360,  "tile_size": 120,  "step_m": 0.5, "skirt": 8,  "buildings_min_m2": 0},
            {"r_max": 900,  "tile_size": 240,  "step_m": 1.0, "skirt": 12, "buildings_min_m2": 30},
            {"r_max": 2400, "tile_size": 480,  "step_m": 4.0, "skirt": 20, "buildings_min_m2": None},
            {"r_max": 7200, "tile_size": 1200, "step_m": 16.0,"skirt": 40, "buildings_min_m2": None},
        ],
        "cadastre_max_radius": 900,
        "fog_density": 0.00012,
        "fog_color": "0xb0c4d8",
        "far_clip": 15000,
    },
    "mountain": { ... },
}
```

Anchor for ring radii: parcel center (the LOCATIONS `cx`/`cy`). Phase 4 (optional) extends this to dynamic `--parcel-id` lookup via RÚIAN centroid.

## Implementation phases

The technical risks (log depth + polygon offset, fog + cadastre interaction, large-tile ortho fetch, snap-vertex correctness, iPad performance) warrant staged validation rather than a single big-bang change.

### Phase 1A — renderer prep (no pipeline changes)
Enable log depth + far clip + fog in **base viewer and template**. Tests:
- Cadastre overlay (renderOrder=100, polygonOffset) still renders correctly with log depth.
- Parcel hover outlines, single-parcel painted highlights still visible.
- All 9 drone-video presets work; sunset preset's color tint interacts with fog as expected.
- iPad framerate acceptable.

If log depth breaks cadastre/polygonOffset, the spec needs revision before continuing.

### Phase 1B — L3 panorama ring (proof of concept)
Add a single outer ring (L3) around the existing 25×25 L0 grid. Smallest meaningful test of the full LOD pipeline: large tile generation, ortho with large bbox, snap-vertex stitching at the L0↔L3 boundary, performance with the panoramic view.

If snap-vertex stitching is too clunky / artifacts visible, fall back to **skirt rims only** and accept the slight quality hit.

### Phase 2 — full 4-ring
After Phase 1 succeeds, add L1 + L2 in between. Per-ring building thresholds. Cadastre limited to L0+L1.

### Phase 3 — profiles
Move ring config into `LOD_PROFILES`. Add `mountain` profile and test on Sněžka.

### Phase 4 (optional) — ad-hoc parcel viewer
`gen_multitile.py --parcel-id 12345` → RÚIAN centroid lookup → use as anchor. Enables the realtor backend to one-shot a viewer for any parcel.

## Open risks (to validate in Phase 1A first)

1. **Log depth + polygonOffset on cadastre overlay.** Three.js logarithmicDepthBuffer changes how `polygonOffset` interacts with depth; needs empirical testing.
2. **iPad performance** with log depth + fog + larger total scene. Existing 625-tile 0.5m grid is borderline on iPad today (per user feedback during P2 work). Post-LOD: fewer total vertices but log depth has fragment-shader overhead.
3. **ČÚZK ortho WMS** rate-limit and tile-size cap for L3's 1024² @ 1200m bbox requests. May need request batching or fall back to smaller tile sizes for L3.
4. **DMP TIFF coverage** for outer rings. For 7.2km L3 radius, need DMP TIFFs in 14.4km × 14.4km area. Current Hnojice cache has 6km × 6km coverage. `download_tiff.py` will need `--area-radius` extension.
5. **Snap-vertex correctness** at ring corners (where 4 tiles meet). 4-way snap is more complex than 2-way edge snap.

## Out of scope

- Quadtree / fully adaptive LOD. Concentric rings + known subject anchor is sufficient.
- Runtime LOD switching (changing detail as camera moves). LOD is baked at generation time.
- Geomorphing / LOD transitions. Hard ring boundaries with snap-vertex are acceptable.
- Tile streaming. All tiles loaded upfront from local server (existing pattern).

## Acceptance criteria (cumulative across phases)

- Phase 1A: viewer functionally equivalent to pre-change, just with deeper view distance and atmospheric fog. No cadastre/parcel/highlight regressions.
- Phase 1B: L3 panoramic horizon visible in viewer. Drone-video `sunset_orbit` and `context_arc` presets show real distant terrain, not blue sky. No visible seam between L0 inner grid and L3 outer ring (or only acceptable minor artifacts).
- Phase 2: Full 4-ring LOD generates for Hnojice in < 10 min. Total GLB < 10 MB. Viewer loads in < 5 sec on desktop, < 10 sec on iPad. All P2 realtor features still work.
- Phase 3: Switching `lod_profile` between `village_flat` and `mountain` produces correctly-scaled output for the respective terrain type.
