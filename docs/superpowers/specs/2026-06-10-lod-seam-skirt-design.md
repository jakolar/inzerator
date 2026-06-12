# LOD seam skirt — design

Date: 2026-06-10
Status: approved (Jan, 2026-06-10)

## Problem

Black slivers ("škvíry") are visible between LOD rings in the heightfield
viewer. The outer ring (closeup, 1.5 m heightmap step) fragment-discards a
hole under the inner ring's footprint (`uClipBox`, `heightfield/index.html`
fragment shader). In plan view the cut lines up exactly, but the two rings
sample heightmaps of different resolution (1.5 m vs 0.5 m), so their
surfaces disagree vertically — by metres at steep features (building walls,
trees). Where the inner surface sits below the closeup cut edge, view rays
pass between the two surfaces into the void → black cracks along the
±500 m boundary square, most visible where the seam crosses buildings.

Cracks cannot occur inside a single ring (each is one watertight mesh).

## Considered approaches

- **A) Perimeter skirt on the inner ring** — vertical curtain hides the gap
  from every angle. Viewer-only, no data regen, no new artifacts. CHOSEN.
- B) Shader blend zone — morph inner heights toward closeup over the last
  ~30–50 m. Geometrically closes the gap but smears 0.5 m detail near the
  edge; more shader work in both materials. Possible later addition.
- C) Shrink clipBox (overlap band) — one-line change, but wherever closeup
  is *higher* than inner (roofs at the seam) it pokes through the inner
  surface. Rejected.

## Design

For every ring except the outermost (today: just `inner`), build a vertical
skirt around its perimeter during ring setup in `heightfield/index.html`.

**Geometry.** Top vertex row must coincide *exactly* with the rendered mesh
edge or it creates new micro-cracks. The mesh edge has vertices every
`side / GEO_SUBDIVS` (1024) metres, heights from bilinear heightmap
sampling — so the skirt top row uses the same positions (1024 segments per
side) and an identical bilinear CPU sample of `heightData` (available in
the build loop before the CPU copy is dropped). Bottom row is the top row
dropped by `SKIRT_DEPTH ≈ 30 m`. 4 sides × 1024 segments × 2 rows ≈ 8k
vertices — negligible.

**Texturing.** The skirt shares the ring's ortho texture. Each vertex pair
(top + bottom) gets the *same* UV — its position on the perimeter — so the
GPU stretches the edge pixel column downward: each vertical strip continues
the edge colour. No new texture, no CPU pixel reads (impossible anyway:
ortho is KTX2, GPU-compressed), and the skirt sharpens automatically on
tier upgrade because it samples the same texture object.

Material: a minimal `ShaderMaterial` (sample ortho with flips, darken
~×0.75, fog) rather than `MeshBasicMaterial` — KTX2 orthos are flipped
in-shader via the ring's `uOrthoFlipY` uniform, which `MeshBasicMaterial`
cannot replicate (it would sample the opposite edge). The skirt material
**shares the ring material's uniform objects** (`uOrtho`, `uOrthoFlipY`,
`uOrthoFlipX`), so tier upgrades, OSM basemap swaps and the visibility
unload/reload cycle all propagate to the skirt with zero extra wiring.
(Amended during planning, 2026-06-10 — original draft said
`MeshBasicMaterial` + manual `.map` swap in `switchOrthoTier`.)

**Deliberate simplifications.**

1. **Bare mode (🌳):** the skirt is built from DSM heights and would stick
   up above bare terrain. Hide the skirt while bare mode is active —
   without buildings the heightmaps nearly agree and cracks are invisible
   there. No geometry rebuild.
2. **Cadastre / baked shadows:** not applied to the skirt. It is a few
   pixels wide on screen; wiring it into the terrain ShaderMaterial isn't
   worth it.

## Out of scope

- `clipBox` mechanics unchanged.
- `gen_heightfield.py` / data regen unchanged (no per-location work).
- Outer perimeter of the closeup ring — already covered by the pedestal
  skirt.

## Testing

- Visual: load `slopne`, orbit low along the ±500 m boundary where it
  crosses buildings — no black slivers from any azimuth/elevation.
- Tier upgrade: skirt texture follows mid → ultra switch.
- Bare toggle: skirt disappears in bare mode, reappears in DSM mode.
- Syntax gate: `awk` module-script extraction + `node --check`.
