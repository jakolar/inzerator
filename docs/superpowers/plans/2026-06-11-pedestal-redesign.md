# Pedestal Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the heightfield viewer's pedestal as a premium presentation base: light warm-grey matte solid with a pale rim lip, an ortho-textured "section" band absorbing the jagged DSM edge, half the depth, bottom bevel, contact AO.

**Architecture:** Viewer-only change inside the pedestal block of `heightfield/index.html`. The async perimeter-profile IIFE keeps its `walkPerimeter`/`makeSceneYSampler` sampling but now computes a smoothed envelope `E(k)` and builds TWO meshes: a watertight matte solid (rim → wall → bevel → bottom fan; replaces both the old `BoxGeometry` and the old 3-row skirt) and an ortho section band between the exact terrain edge and `E(k)` (same stretched-edge-texel + shared-uniform technique as the LOD seam skirt). Spec: `docs/superpowers/specs/2026-06-11-pedestal-redesign-design.md`.

**Tech Stack:** Three.js 0.170 (importmap), vanilla JS module inside `index.html`.

**Testing:** No JS test harness in this repo (pytest covers `server.py` only). Gate for `index.html`:

```bash
awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin
```

Expected: exit 0, no output. Plus manual visual checklist (Task 4) on the dev server on port 8081 (`http://<host>:8081/heightfield/?slug=slopne`).

**Bash outage protocol:** A classifier outage may block the Bash tool ("claude-fable-5 is temporarily unavailable"). Retry 2–3×; if still down, finish the edits, skip gate+commit, and report DONE_WITH_CONCERNS noting which steps were skipped — never BLOCKED for the outage alone. The controller batches pending commits later.

**Key context for an engineer with zero codebase knowledge:**

- The viewer renders concentric terrain rings; ring 0 (`ringMeshes[0]`, outer, 3 km) is the pedestal's host. Terrain vertex shader sets `pos.y = absY - uYBase` (`uYBase = yMid`), so all scene Y values are "metres AMSL minus `yMid`".
- Top-level helpers already exist (~line 723): `makeSceneYSampler(f32, n)` (bilinear CPU sampler matching the GPU exactly) and `walkPerimeter(sampleSceneY, sideM)` (CW walk, 4×1024 columns, returns `[{x, z, y, u, v}]`).
- The LOD seam skirt block (~line 784, `// ── LOD seam skirt ──`) contains `buildSeamSkirt` whose ShaderMaterial shares the ring material's `uOrtho`/`uOrthoFlipY`/`uOrthoFlipX` uniform OBJECTS (KTX2 orthos are flipped in-shader; sharing makes tier upgrades/OSM swaps/unload-reload propagate for free). Task 2 extracts that material into `makeEdgeOrthoMaterial` and reuses it for the section band.
- The pedestal block is the `{ … }` braced block starting `// ── Block diagram base + ground shadow ──` (~line 1253) and its async IIFE (~line 1444). Uncommitted state already contains the 2026-06-10 mitre fix (`ox = (p.u === 0 || p.u === 1) ? Math.sign(p.x) * BEVEL_W : 0;`) — that per-edge+miter offset principle must survive the rewrite.
- The working tree also carries the finished LOD-seam-skirt feature (uncommitted, approved). Task 0 commits it first if git works.

---

### Task 0: Flush pending commits (if Bash available)

**Files:** none (git only)

- [ ] **Step 1: Syntax gate** (command above; expected exit 0).
- [ ] **Step 2: Commit pending work** — three commits, in this order:

```bash
git add docs/superpowers/specs/2026-06-10-lod-seam-skirt-design.md docs/superpowers/plans/2026-06-10-lod-seam-skirt.md docs/superpowers/specs/2026-06-11-pedestal-redesign-design.md docs/superpowers/plans/2026-06-11-pedestal-redesign.md
git commit -m "docs: specs + plans for LOD seam skirt and pedestal redesign

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"

git add heightfield/index.html
git commit -m "feat(heightfield): perimeter seam skirt hides LOD ring boundary cracks

Vertical curtain around each inner ring (4x1024 perimeter columns, 30 m
deep), top row matching the rendered mesh edge exactly via the shared
bilinear sampler extracted from the pedestal skirt. UVs pinned to the
perimeter stretch the edge texel column downward; the skirt material
shares the ring material's ortho + flip uniform objects so tier
upgrades, OSM swaps and unload/reload propagate automatically. Hidden
in bare-earth mode. Includes the pedestal chamfer mitre fix (offset
perpendicular to the edge instead of radial, closing corner slivers).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(The mitre fix rides along in the feat commit — same file, cannot be split non-interactively.)

If Bash is blocked: skip, report DONE_WITH_CONCERNS, continue to Task 1.

---

### Task 1: Pedestal solid — constants, envelope, watertight extrusion (replaces box + skirt)

**Files:**
- Modify: `heightfield/index.html` (pedestal block ~1253–1539; five edit sites below)

- [ ] **Step 1: Replace the look constants**

Find (~line 1291):

```js
  // Premium "anthracite" look: dark slate/graphite walls that contrast the
  // colourful ortho on top and read as a machined plinth rather than a beige
  // cartographic block. A bright edge-highlight on the top chamfer lip is the
  // only light cue (scene is unlit) — it fakes a rim-light catching the bevel.
  const STONE   = 0x34383c;   // dark anthracite wall (was warm beige 0xa89986)
  const DARK    = 0x1b1d1f;   // near-black bottom face
  const BEVEL_W = 8;          // horizontal outward overhang of the top chamfer (m)
  const BEVEL_H = 6;          // vertical drop of the chamfer facet (m)
  const EDGE_HL = 2.8;        // edge-highlight multiplier on the chamfer lip (rim light)
```

Replace with:

```js
  // Premium presentation-model look: light warm-grey matte solid, pale rim
  // lip under an ortho-textured "section" band that absorbs the jagged DSM
  // edge. Spec: docs/superpowers/specs/2026-06-11-pedestal-redesign-design.md.
  // All tunables live here — adjust during visual verification, no UI.
  const WALL_C   = 0xb5ada0;  // matte light warm grey wall
  const RIM_C    = 0xefe9dc;  // pale rim lip — separates terrain from base
  const BEVEL_C  = 0x9a9183;  // bottom bevel facet
  const BOTTOM_C = 0x867f74;  // bottom face
  const RIM_OUT  = 2;         // rim horizontal protrusion (m)
  const RIM_H    = 3;         // rim facet height (m)
  const BEV_W    = 1.5;       // bottom bevel horizontal inset (m)
  const BEV_H    = 1.5;       // bottom bevel height (m)
  const SM_MIN_R = 7;         // envelope rolling-min radius in perimeter columns (≈ ±20 m)
  const SM_AVG_R = 3;         // envelope smoothing radius — MUST stay <= SM_MIN_R (keeps E(k) <= edge)
  const BAND_DARKEN = 0.8;    // ortho section band darkening (Task 2)
  const AO_MIN   = 0.78;      // contact-AO multiplier directly under the rim
  const AO_H     = 8;         // contact-AO ramp length down the wall (m)
  const BW = W + 2 * RIM_OUT; // full footprint incl. rim overhang (shadow plane sizing)
```

- [ ] **Step 2: Halve the base depth**

Find (~line 1282):

```js
  // Shallow base: deep block dips far below the sky horizon (Y=0) and
  // looks "wedged" against the background. 30 % of the terrain relief
  // range, floored at 12 m, capped at 25 m — enough to read as a 3D
  // diagram cross-section without becoming an iceberg.
  const range = sceneYmax - sceneYmin;
  const BASE_DEPTH = Math.min(25, Math.max(12, range * 0.3));
```

Replace with:

```js
  // Slim base: the pedestal should read as a light presentation slab, not a
  // block. 15 % of the terrain relief range, floored at 6 m, capped at 12 m.
  const range = sceneYmax - sceneYmin;
  const BASE_DEPTH = Math.min(12, Math.max(6, range * 0.15));
```

- [ ] **Step 3: Lighten the wall gradient + drop box materials and BoxGeometry**

(a) Find `const F_TOP = 1.0, F_BOT = 0.5;` (~line 1331) → replace with `const F_TOP = 1.0, F_BOT = 0.75;` (light material must not go muddy at the base).

(b) Find `grainBox.repeat.set(W / GRAIN_M, 1);` (~line 1324) and DELETE the line (the repeat was sized for the deleted box walls; the solid uses a metric-UV clone with repeat 1×1).

(c) DELETE everything from `const stone = new THREE.MeshBasicMaterial({ color: STONE, …` (~line 1338) down to and including:

```js
  const box = new THREE.Mesh(boxGeo, [stone, stone, invis, dark, stone, stone]);
  box.position.set(0, (topY + bottomY) / 2, 0);
  box.renderOrder = 0;     // before terrain (renderOrder 1+) so no z-fight surprises
  box.frustumCulled = false;
  scene.add(box);
```

i.e. the `stone`/`dark`/`invis` materials, the BoxGeometry comment block, `const BW = …` (now lives in Step 1 constants), `const boxGeo = …`, the per-vertex gradient `{ … }` block, and the box mesh add. The pedestal solid replaces the box entirely (Step 5). The ground-shadow section that follows stays.

(d) In the ground-shadow section find `const SHADOW_PAD = 1.7;` (~line 1379) → replace with `const SHADOW_PAD = 1.5;` (smaller halo for the lighter base). `SHADOW_SIZE = BW * SHADOW_PAD` keeps working via the new BW constant.

(e) Three comments still describe the deleted box/anthracite look — update them so they match the new design:

(e1) The block's leading comment. Find (~line 1253):

```js
// ── Block diagram base + ground shadow ──
// Cartographic "podstavec": extrudes the outermost ring extent downward
// into a solid block with stone-coloured vertical walls and a darker
// bottom. Removes the "floating coin in the sky" feel and reads as a
// 3D-printed relief model — classic block-diagram convention since the
// 19th century, used in atlas inserts and museum reliefs.
//
// Sized to the widest ring (rings[0].side_m). Block top sits 10 cm
// below globalYmin so the terrain edge meets the wall cleanly; the
// resulting drop reads as the natural cross-section. Depth scales with
// terrain relief (60 % of (Ymax-Ymin), floor 30 m) so flat regions
// don't get a paper-thin base.
//
// The radial shadow plane underneath grounds the model visually — when
// you orbit, the soft gradient suggests the block sits on a notional
// surface rather than floating. CSS radial-gradient baked into a
// CanvasTexture, no extra GPU cost.
//
// All MeshBasicMaterial — viewer's procedural shader stack doesn't
// install any lights, so PBR/Lambert would render black.
```

Replace with:

```js
// ── Block diagram base + ground shadow ──
// Cartographic "podstavec": light warm-grey matte presentation base — a
// pale rim lip following a smoothed envelope of the terrain edge, an ortho
// "section" band absorbing the jagged DSM edge above it, a clean wall with
// contact AO and a bottom bevel. Removes the "floating coin in the sky"
// feel and reads as a museum / architectural presentation model.
//
// Sized to the widest ring (rings[0].side_m). The rim follows the smoothed
// terrain-edge envelope; depth scales with terrain relief (15 % of
// (Ymax-Ymin), floor 6 m, cap 12 m).
//
// The rectangular shadow plane underneath grounds the model visually — when
// you orbit, the soft gradient suggests the block sits on a notional
// surface rather than floating. Gradient baked into a CanvasTexture, no
// extra GPU cost.
//
// MeshBasicMaterial + vertexColors for the solid, a minimal ortho
// ShaderMaterial for the section band — the viewer's shader stack installs
// no lights, so PBR/Lambert would render black.
```

(e2) The grain comment. Find (~line 1301):

```js
  // Fine procedural grain — kills the "default flat material" read. White-ish
  // hash noise (deterministic; no Math.random so renders are reproducible),
  // narrow 0.86–1.0 brightness so it modulates without speckling. Tiled via
  // RepeatWrapping; box uses 0–1 UVs (repeat sets scale), skirt sets metric
  // UVs on a clone so the grain size matches across both meshes.
```

Replace with:

```js
  // Fine procedural grain — kills the "default flat material" read. White-ish
  // hash noise (deterministic; no Math.random so renders are reproducible),
  // narrow 0.86–1.0 brightness so it modulates without speckling. Tiled via
  // RepeatWrapping; the pedestal solid samples it through metric UVs
  // (metres / GRAIN_M) on a clone so grain size is constant in metres.
```

(e3) The AO comment above `F_TOP`. Find (~line 1325):

```js
  // Fake vertical AO: scene has no lights, so the wall gets its depth from a
  // per-vertex grayscale multiplier baked into vertexColors. Brightest just
  // under the chamfer, darkest at the block floor — reads as ambient occlusion
  // sinking into the base. Multiplier (not absolute colour) so the anthracite
  // tone is preserved. The chamfer lip itself gets EDGE_HL (well above F_TOP)
  // for the bright rim-light read; the vertical wall fades F_TOP→F_BOT.
```

Replace with:

```js
  // Fake vertical AO: scene has no lights, so the wall gets its depth from a
  // per-vertex grayscale multiplier baked into vertexColors. The global
  // gradient fades F_TOP→F_BOT down the wall; a contact-AO ramp under the
  // rim (AO_MIN→1 over AO_H) multiplies on top in the IIFE build below.
```

- [ ] **Step 4: Syntax gate** — file must still parse (the IIFE still references the old skirt build; that is Step 5's target, do Step 5 before gating if you prefer a single gate — but gate MUST pass before commit).

- [ ] **Step 5: Rewrite the IIFE build (skirt → envelope + solid)**

In the async IIFE, KEEP everything through:

```js
      const perim = walkPerimeter(makeSceneYSampler(f32, n), sideM);
```

DELETE everything after that line down to and including:

```js
      console.info(`[block-base] skirt added (${P} perim samples, ` +
                   `outer ring ${sideM.toFixed(0)}×${sideM.toFixed(0)} m)`);
```

(i.e. the whole 3-row chamfer/wall geometry build, `grainSkirt`, `skirtMat`, `skirtMesh`, and the console line — the `} catch (e) {` block below stays). Replace with:

```js
      const P = perim.length;

      // Smoothed envelope E(k): cyclic rolling minimum (radius SM_MIN_R)
      // then cyclic moving average (radius SM_AVG_R). Because
      // SM_AVG_R <= SM_MIN_R, every averaged value is a mean of minima
      // whose windows all contain k, so E(k) <= edge height always — the
      // section band above the rim can never invert.
      const mins = new Float64Array(P);
      for (let k = 0; k < P; k++) {
        let m = Infinity;
        for (let d = -SM_MIN_R; d <= SM_MIN_R; d++)
          m = Math.min(m, perim[(k + d + P) % P].y);
        mins[k] = m;
      }
      const env = new Float64Array(P);
      for (let k = 0; k < P; k++) {
        let s = 0;
        for (let d = -SM_AVG_R; d <= SM_AVG_R; d++)
          s += mins[(k + d + P) % P];
        env[k] = s / (2 * SM_AVG_R + 1);
      }

      // Per-edge outward unit offset, mitred at the corners (full offset on
      // both axes there) — the wall must land exactly on the footprint plane
      // at half + RIM_OUT. Radial-from-centre offsets fall short everywhere
      // except edge midpoints (the 2026-06-10 corner-sliver bug).
      const out = (p) => [
        (p.u === 0 || p.u === 1) ? Math.sign(p.x) : 0,
        (p.v === 0 || p.v === 1) ? Math.sign(p.z) : 0,
      ];
      const rgb = (hex, f) => [
        ((hex >> 16 & 255) / 255) * f,
        ((hex >> 8 & 255) / 255) * f,
        ((hex & 255) / 255) * f,
      ];

      // ── Matte pedestal solid ──
      // One watertight perimeter extrusion replaces the old box + skirt duo
      // (whose mismatch caused the see-through sliver bug). 6 vertex rows
      // per column — rim top, rim bottom ×2 (duplicated so wall AO doesn't
      // bleed into the flat pale rim), AO ring, bevel top, bevel bottom —
      // plus one centre vertex fanning the bottom face closed. Material
      // colour is white: the actual colours (rim/wall/bevel/bottom) live in
      // vertexColors and the grain map multiplies on top.
      const bevelTopY = bottomY + BEV_H;
      const ROWS = 6;
      const positions = new Float32Array((P * ROWS + 1) * 3);
      const scol      = new Float32Array((P * ROWS + 1) * 3);
      const uvs       = new Float32Array((P * ROWS + 1) * 2);
      let cum = 0;
      for (let k = 0; k < P; k++) {
        const p = perim[k];
        const prev = perim[(k - 1 + P) % P];
        cum += Math.hypot(p.x - prev.x, p.z - prev.z);
        const [ou, ov] = out(p);
        const E = env[k];
        const aoY = Math.max(bevelTopY, E - RIM_H - AO_H);
        const wx = p.x + ou * RIM_OUT, wz = p.z + ov * RIM_OUT;
        const px = [p.x, wx, wx, wx, wx, p.x + ou * (RIM_OUT - BEV_W)];
        const pz = [p.z, wz, wz, wz, wz, p.z + ov * (RIM_OUT - BEV_W)];
        const ys = [E, E - RIM_H, E - RIM_H, aoY, bevelTopY, bottomY];
        const cs = [
          rgb(RIM_C, 1),                              // rim top
          rgb(RIM_C, 1),                              // rim bottom (rim copy)
          rgb(WALL_C, AO_MIN * shadeF(E - RIM_H)),    // rim bottom (wall copy)
          rgb(WALL_C, shadeF(aoY)),                   // AO ramp end
          rgb(WALL_C, shadeF(bevelTopY)),             // bevel top
          rgb(BEVEL_C, 1),                            // bevel bottom
        ];
        const u = cum / GRAIN_M;
        for (let row = 0; row < ROWS; row++) {
          const vi = k * ROWS + row;
          positions[vi * 3]     = px[row];
          positions[vi * 3 + 1] = ys[row];
          positions[vi * 3 + 2] = pz[row];
          scol[vi * 3]     = cs[row][0];
          scol[vi * 3 + 1] = cs[row][1];
          scol[vi * 3 + 2] = cs[row][2];
          uvs[vi * 2]     = u;
          uvs[vi * 2 + 1] = ys[row] / GRAIN_M;
        }
      }
      // Bottom-centre vertex closes the solid from below.
      const ci = P * ROWS;
      positions[ci * 3] = 0; positions[ci * 3 + 1] = bottomY; positions[ci * 3 + 2] = 0;
      const bc = rgb(BOTTOM_C, 1);
      scol[ci * 3] = bc[0]; scol[ci * 3 + 1] = bc[1]; scol[ci * 3 + 2] = bc[2];
      uvs[ci * 2] = 0; uvs[ci * 2 + 1] = bottomY / GRAIN_M;

      // 4 quad bands per segment (rim 0→1, AO wall 2→3, wall 3→4,
      // bevel 4→5; rows 1|2 share positions, only colours differ) + one
      // bottom-fan triangle = 27 indices per segment.
      const indices = new Uint32Array(P * 27);
      const bands = [[0, 1], [2, 3], [3, 4], [4, 5]];
      for (let k = 0; k < P; k++) {
        const next = (k + 1) % P;
        const a = k * ROWS, b = next * ROWS;
        let off = k * 27;
        for (const [t, m] of bands) {
          indices[off++] = a + t; indices[off++] = a + m; indices[off++] = b + t;
          indices[off++] = a + m; indices[off++] = b + m; indices[off++] = b + t;
        }
        indices[off++] = a + 5; indices[off++] = ci; indices[off++] = b + 5;
      }
      const solidGeo = new THREE.BufferGeometry();
      solidGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      solidGeo.setAttribute('color', new THREE.BufferAttribute(scol, 3));
      solidGeo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
      solidGeo.setIndex(new THREE.BufferAttribute(indices, 1));
      // Grain clone with metric UVs baked in → repeat 1×1. DoubleSide saves
      // worrying about winding across 4 wall orientations + bottom fan.
      const grainSolid = grainBox.clone();
      grainSolid.needsUpdate = true;
      grainSolid.repeat.set(1, 1);
      const solidMat = new THREE.MeshBasicMaterial({
        color: 0xffffff, fog: true, side: THREE.DoubleSide, vertexColors: true,
        map: grainSolid,
      });
      const solid = new THREE.Mesh(solidGeo, solidMat);
      solid.renderOrder = 0;
      solid.frustumCulled = false;
      scene.add(solid);
      console.info(`[block-base] pedestal solid (${P} columns, depth ${BASE_DEPTH.toFixed(0)} m, rim ${RIM_H} m @ envelope)`);
```

- [ ] **Step 6: Syntax gate** (command in header). Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): rebuild pedestal as light matte solid with smoothed rim

Watertight perimeter extrusion (rim lip, contact-AO wall, bottom bevel,
bottom fan) replaces the BoxGeometry + skirt duo. Rim follows a smoothed
envelope (cyclic rolling min + average) of the terrain edge; warm light
grey palette, half depth, lighter wall gradient, smaller shadow halo.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Bash blocked: skip gate+commit, report DONE_WITH_CONCERNS.)

NOTE: after this task the strip between the terrain edge and the rim is OPEN (you can see through it) — that is expected; Task 2 fills it with the ortho band. Do not "fix" it here.

---

### Task 2: Ortho section band + shared material factory

**Files:**
- Modify: `heightfield/index.html` (seam-skirt block ~784–886; pedestal IIFE end of Task 1 Step 5 code)

- [ ] **Step 1: Extract `makeEdgeOrthoMaterial` from `buildSeamSkirt`**

In the `// ── LOD seam skirt ──` block, find inside `buildSeamSkirt` the material construction (begins `// Fog uniforms must be per-material clones` and ends with the `});` closing `new THREE.ShaderMaterial({ … })`). DELETE it and insert ABOVE `function buildSeamSkirt(…)` this factory (shader strings copied verbatim from the deleted code):

```js
// Minimal ortho-sampling material for edge curtains (LOD seam skirts, the
// pedestal section band). SHARES the ring material's ortho + flip uniform
// OBJECTS — tier upgrades, OSM basemap swaps and the visibility
// unload/reload cycle all propagate with zero wiring. Fog uniforms must be
// per-material clones (three.js refreshFogUniforms writes .value into each).
function makeEdgeOrthoMaterial(ringMat, darken) {
  const fogUniforms = THREE.UniformsUtils.clone(THREE.UniformsLib.fog);
  return new THREE.ShaderMaterial({
    uniforms: {
      ...fogUniforms,
      // SHARED with the ring material — reference, do NOT clone.
      uOrtho:      ringMat.uniforms.uOrtho,
      uOrthoFlipY: ringMat.uniforms.uOrthoFlipY,
      uOrthoFlipX: ringMat.uniforms.uOrthoFlipX,
      uDarken:     { value: darken },
    },
    vertexShader: `
      #include <fog_pars_vertex>
      varying vec2 vUv;
      void main() {
        vUv = uv;
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mvPosition;
        #include <fog_vertex>
      }
    `,
    fragmentShader: `
      #include <fog_pars_fragment>
      uniform sampler2D uOrtho;
      uniform float uOrthoFlipY;
      uniform float uOrthoFlipX;
      uniform float uDarken;
      varying vec2 vUv;
      void main() {
        vec2 orthoUv = vUv;
        if (uOrthoFlipY > 0.5) orthoUv.y = 1.0 - orthoUv.y;
        if (uOrthoFlipX > 0.5) orthoUv.x = 1.0 - orthoUv.x;
        gl_FragColor = vec4(texture2D(uOrtho, orthoUv).rgb * uDarken, 1.0);
        #include <fog_fragment>
      }
    `,
    fog: true,
    side: THREE.DoubleSide,   // visible from outside AND from inside the seam
  });
}
```

Inside `buildSeamSkirt`, where the deleted construction sat, use instead:

```js
  const mat = makeEdgeOrthoMaterial(ringMat, SEAM_SKIRT_DARKEN);
```

(The rest of `buildSeamSkirt` — geometry, `new THREE.Mesh(geo, mat)`, renderOrder, `seamSkirts.push` — stays untouched.)

- [ ] **Step 2: Build the section band in the pedestal IIFE**

In the pedestal IIFE, IMMEDIATELY AFTER the `console.info(`[block-base] pedestal solid …`)` line from Task 1, insert:

```js
      // ── Ortho section band ──
      // Fills the strip between the exact (jagged) terrain edge and the
      // smoothed rim envelope. Top row is watertight against the terrain
      // edge; both rows share the perimeter UV so the GPU stretches the
      // ring's edge texel column downward — the jaggedness reads as a
      // terrain cross-section, not as pedestal. Same technique + shared
      // uniforms as the LOD seam skirt (ring 0 material here).
      const bpos = new Float32Array(P * 2 * 3);
      const buv  = new Float32Array(P * 2 * 2);
      for (let k = 0; k < P; k++) {
        const p = perim[k];
        const tu = p.u, tv = 1.0 - p.v;
        for (let row = 0; row < 2; row++) {
          const vi = k * 2 + row;
          bpos[vi * 3]     = p.x;
          bpos[vi * 3 + 1] = row === 0 ? p.y : env[k];
          bpos[vi * 3 + 2] = p.z;
          buv[vi * 2]     = tu;
          buv[vi * 2 + 1] = tv;
        }
      }
      const bidx = new Uint32Array(P * 6);
      for (let k = 0; k < P; k++) {
        const next = (k + 1) % P;
        const a = k * 2, b = next * 2, off = k * 6;
        bidx[off]     = a;     bidx[off + 1] = a + 1; bidx[off + 2] = b;
        bidx[off + 3] = a + 1; bidx[off + 4] = b + 1; bidx[off + 5] = b;
      }
      const bandGeo = new THREE.BufferGeometry();
      bandGeo.setAttribute('position', new THREE.BufferAttribute(bpos, 3));
      bandGeo.setAttribute('uv', new THREE.BufferAttribute(buv, 2));
      bandGeo.setIndex(new THREE.BufferAttribute(bidx, 1));
      const band = new THREE.Mesh(
        bandGeo, makeEdgeOrthoMaterial(ringMeshes[0].material, BAND_DARKEN));
      band.renderOrder = 1;        // alongside the outer ring
      band.frustumCulled = false;
      scene.add(band);
      console.info(`[block-base] section band added (${P} columns)`);
```

(`ringMeshes[0]` is the outer ring — rings build outer→inner. `env`, `P`, `perim` are in scope from Task 1's code.)

- [ ] **Step 3: Syntax gate** (command in header). Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): ortho section band between terrain edge and pedestal rim

Extracts makeEdgeOrthoMaterial from the seam skirt and reuses it for a
band of stretched edge texels filling the jagged DSM edge above the
smoothed rim — the noise reads as terrain cross-section, the pedestal
below stays clean.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(If Bash blocked: skip gate+commit, report DONE_WITH_CONCERNS.)

---

### Task 3: Manual visual verification (slopne, port 8081)

No code. Viewer: `http://<host>:8081/heightfield/?slug=slopne`, hard reload (Cmd+Shift+R). Console must show `[block-base] pedestal solid (4096 columns, depth … m, rim 3 m @ envelope)` and `[block-base] section band added (4096 columns)`.

- [ ] **Step 1: Rim.** Pale clean rim line runs the whole perimeter, following a smooth envelope under the jagged edge; no steps/spikes in the rim itself.
- [ ] **Step 2: Section band.** The strip between terrain edge and rim shows darkened stretched ortho; meets the terrain edge with no micro-cracks from any angle; trees read as terrain, not as grey wall.
- [ ] **Step 3: Corners.** Orbit all four corners low: no slivers, no see-through into the interior, rim mitres cleanly.
- [ ] **Step 4: Proportions + palette.** Base reads light (≈ half previous depth), warm light grey, matte, grain visible up close, no glossy/dark drama; bottom bevel visible from low angles; bottom face closed (orbit below the model).
- [ ] **Step 5: AO.** Subtle contact shadow directly under the rim fading over ~8 m; wall gradient gentle (not muddy at the bottom).
- [ ] **Step 6: Propagation.** Tier mid→ultra upgrade and 🗺 OSM toggle both update the section band (shared uniforms); seam skirt (inner ring) regression-free.
- [ ] **Step 7: Ground shadow.** Rectangular shadow still hugs the (now wider-by-RIM_OUT) footprint; halo slightly tighter than before.

If any step fails, fix forward before reporting done. Tunables (`RIM_H`, `RIM_OUT`, `SM_MIN_R`, `BAND_DARKEN`, `AO_*`, palette) are constants at the top of the pedestal block.
