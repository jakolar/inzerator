# Pedestal redesign — premium presentation base

Date: 2026-06-11
Status: approved (Jan, 2026-06-11 — design sections approved in chat; spec written
on instruction "zapiš spec a pokračuj na plán")
Mockups: `docs/mockups/pedestal/01-wall-treatment.html` (chose A),
`docs/mockups/pedestal/02-material.html` (chose A)

## Problem

The current pedestal (anthracite block, commit 82b552e + uncommitted mitre fix)
reads as an extruded raw mesh block, not a presentation model:

- One grey wall runs from the jagged DSM terrain edge (tree canopies) all the
  way down — the noise reads as pedestal, not as terrain.
- Dark anthracite + bright rim-light is glossy/dramatic; the goal is a clean
  GIS / real-estate presentation look.
- The block is tall and dominant (`BASE_DEPTH` up to 25 m below `sceneYmin`).
- Box + skirt are two separate meshes whose mismatch already caused the
  see-through sliver bug (invisible box top exposed between skirt wall and box
  rim).

Terrain surface stays untouched — the jagged edge silhouette cannot and must
not change.

## Considered approaches (wall treatment)

- **A) Smoothed rim + ortho "section" band above it — CHOSEN.** Wall splits
  into a terrain-textured section band (jaggedness reads as terrain) and a
  clean matte pedestal below a light rim.
- B) Rim along the bare-earth (DMR5G) profile — truer line, but requires bare
  data at startup, wavier rim, bare-mode interaction. Rejected.
- C) Cosmetics only (material/height/bevels/AO on current geometry) — jagged
  grey walls remain. Rejected.

Material: **light warm grey** (chosen over medium graphite and off-white).

## Design

All in the pedestal block of `heightfield/index.html` (the `{ … }` block after
the ring build loop plus its async skirt IIFE). The perimeter sampling helpers
(`makeSceneYSampler`, `walkPerimeter`) and the async profile load are reused
unchanged.

### Geometry — two meshes replace skirt + box

The outer-ring perimeter walk (4×1024 columns) feeds both meshes. Per column
`k`: edge height `y(k)` (exact terrain edge, scene Y) and smoothed envelope
`E(k)`.

**Envelope `E(k)`:** cyclic rolling minimum of `y` with radius `SM_MIN_R = 7`
columns (≈ ±20 m at 2.93 m column spacing on the 3 km ring), then cyclic
moving average with radius `SM_AVG_R = 3`. Invariant: because
`SM_AVG_R ≤ SM_MIN_R`, every averaged sample is a mean of minima whose windows
all contain `k`, so `E(k) ≤ y(k)` always — band 1 can never invert. Keep that
inequality of the two radii.

**Mesh 1 — ortho section band** (per column, 2 rows):
- top row: `(x, y(k), z)` — exact terrain edge, watertight as today;
- bottom row: `(x, E(k), z)` — same XZ (vertical band on the footprint plane);
- both rows UV `(u, 1 − v)` — stretched edge texel, exactly the LOD seam-skirt
  technique;
- material: minimal ShaderMaterial sharing the **outer ring** material's
  `uOrtho` / `uOrthoFlipY` / `uOrthoFlipX` uniform objects
  (`ringMeshes[0].material.uniforms` — reference, never clone), own
  `uDarken = BAND_DARKEN = 0.8`, cloned fog uniforms, `fog: true`,
  `side: DoubleSide`, `renderOrder = 1`, `frustumCulled = false`.

**Mesh 2 — matte pedestal solid** (one watertight perimeter extrusion; the
separate `BoxGeometry` and the `invis` top-face hack are deleted). Per column,
vertex rows top→bottom (positions; colour notes in the AO section):
1. rim top `(x, E(k), z)` — shared line with band 1 bottom;
2. rim bottom `(x + ox·RIM_OUT, E(k) − RIM_H, z + oz·RIM_OUT)` — sloped light
   lip, `RIM_OUT = 2`, `RIM_H = 3`. **Duplicated vertex ring**: one copy ends
   the rim facet (flat rim colour), the second starts the wall (carries
   `AO_MIN`) — sharing one ring would bleed wall AO into the rim;
3. AO ring `(x + ox·RIM_OUT, max(bevelTopY, E(k) − RIM_H − AO_H), z + …)` —
   ends the 8 m contact-AO ramp; without it the ramp would interpolate over
   the whole wall height;
4. wall bottom `(x + ox·RIM_OUT, bevelTopY, z + oz·RIM_OUT)`;
5. bottom bevel `(x + ox·(RIM_OUT − BEV_W), bottomY, z + oz·(RIM_OUT − BEV_W))`,
   `BEV_W = BEV_H = 1.5`, `bevelTopY = bottomY + BEV_H`;
plus a bottom face closed by a triangle fan around one bottom-centre vertex
(a flat quad pair could not share the perimeter ring's vertices — cracks);
the fan interpolates bevel → bottom-face colour across the face.

`(ox, oz)` are the per-edge perpendicular offsets with mitred corners from the
2026-06-10 fix: `ox = (u === 0 || u === 1) ? sign(x) : 0`, likewise `oz` from
`v` — full offset on both axes at corners. The radial-from-centre push must
not come back.

Depth: `bottomY = topY − BASE_DEPTH` with
`BASE_DEPTH = min(12, max(6, range · 0.15))` (≈ half of today),
`topY = sceneYmin − 0.1` unchanged.

### Materials & colours

Matte only, scene stays unlit (`MeshBasicMaterial` + vertex colours + the
existing procedural grain `CanvasTexture`; metric UVs as today):

| element | colour |
|---|---|
| wall | `0xb5ada0` (light warm grey) |
| rim facet | `0xefe9dc` |
| bottom bevel | `0x9a9183` |
| bottom face | `0x867f74` |

Deleted: `STONE`, `DARK`, `EDGE_HL`, `BEVEL_W`, `BEVEL_H` constants and the
chamfer/rim-light logic.

### AO / shading (vertex colours, multiplicative)

- **Contact AO under the rim:** multiplier ramp from `AO_MIN = 0.78` directly
  under the rim (wall copy of row 2) easing to `1.0` at the AO ring (row 3,
  `AO_H = 8` m below the rim); rows 4–5 stay at `1.0` (times the global
  gradient).
- **Global wall gradient:** `shadeF` stays but `F_BOT` lightens `0.5 → 0.75`
  (light material must not go muddy).
- Rim facet: no AO (it is the light separator line; flat `1.0`).
- **Ground shadow plane:** kept, `SHADOW_PAD 1.7 → 1.5` (smaller halo for the
  lighter base). Drawing code otherwise unchanged.

### Deliberate simplifications

1. **Bare mode (🌳):** pedestal unchanged (today's behaviour). The DSM-built
   section band slightly overhangs bare terrain — bare is an analysis view.
   No wiring.
2. **LOD seam skirt** (inner ring) untouched.
3. Tunables (`SM_MIN_R`, `RIM_H`, `RIM_OUT`, `BAND_DARKEN`, AO values) are
   named constants at the top of the pedestal block — tuned during visual
   verification, no UI.

## Out of scope

- Terrain rendering, clipBox, seam skirt, data generation — unchanged.
- Cadastre/shadow overlays on the pedestal.

## Testing

- Syntax gate: `awk` module-script extraction + `node --check`.
- Visual on `slopne` (port 8081): clean rim around the full perimeter; mitred
  corners with no slivers/see-through; section band meets terrain edge with no
  micro-cracks; band follows tier upgrade + OSM swap (shared uniforms); lighter
  base proportions; ground shadow regression; orbit below model — bottom face
  closed.
