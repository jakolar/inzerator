# LOD Seam Skirt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide the black cracks at the inner/closeup LOD ring boundary with a vertical perimeter skirt on each inner ring.

**Architecture:** Viewer-only change in `heightfield/index.html`. A `BufferGeometry` curtain (2 vertex rows × 4×1024 perimeter columns) is built per inner ring during ring setup, top row matching the rendered mesh edge exactly (same vertex positions, same bilinear heightmap filter). It is textured by the ring's own ortho texture with UVs pinned to the perimeter (edge pixel stretched down), via a minimal ShaderMaterial that **shares the ring material's uniform objects** (`uOrtho`, `uOrthoFlipY`, `uOrthoFlipX`) — tier upgrades, OSM swaps and visibility-reload propagate with zero wiring. Spec: `docs/superpowers/specs/2026-06-10-lod-seam-skirt-design.md`.

**Tech Stack:** Three.js 0.170 (importmap), vanilla JS module inside `index.html`.

**Testing:** This repo has NO JS test harness (pytest covers `server.py` only — see `CLAUDE.md`). The gate for `index.html` changes, used by previous work in this session, is:

```bash
awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin
```

Expected output: nothing (exit 0). Plus a manual visual checklist (Task 4) on the dev server already running on port 8081 (`PORT=8081 python3 server.py`, viewer at `/heightfield/?slug=slopne`).

**Key context for an engineer with zero codebase knowledge:**

- The viewer renders 2+ concentric terrain "rings" (outer→inner in `manifest.rings`), each a shared `PlaneGeometry(1,1,1024,1024)` scaled to `r.side_m`, displaced in the vertex shader from a Float32 heightmap `DataTexture`. `GEO_SUBDIVS = 1024` (`index.html` ~line 694).
- The outer ring fragment-discards a hole under the inner ring's footprint (`uClipBox` test in the fragment shader, ~line 984). The cracks happen because the two rings' heightmaps (1.5 m vs 0.5 m step) disagree vertically at that cut line.
- The terrain vertex shader samples `uHeight` at `hmUv = (u, 1-v)` and sets `pos.y = absY - uYBase` where `uYBase = yMid` — all scene-space Y values below are "absolute metres minus `yMid`".
- The fragment shader samples `uOrtho` at `vUv` with conditional flips: `if (uOrthoFlipY > 0.5) orthoUv.y = 1.0 - orthoUv.y;` (~line 995). KTX2 textures need the flip, JPEG doesn't — which is why the skirt MUST share these uniforms rather than hardcode UV orientation.
- The pedestal ("block-base") section (~line 1097) already contains a perimeter-walking skirt for the OUTER ring edge. Task 1 extracts its sampler + walk into shared helpers.
- The build loop (~line 749) has `heightData` (decoded Float32Array) in scope before dropping it for heap savings — the seam skirt must be built there.

---

### Task 1: Extract shared perimeter helpers, refactor pedestal skirt to use them

Pure refactor — zero visual change. The pedestal skirt's bilinear sampler and perimeter walk become top-level functions reusable by the seam skirt.

**Files:**
- Modify: `heightfield/index.html` (two sites: insert helpers before the ring build loop ~line 722; replace inline code in the pedestal skirt IIFE ~lines 1284–1340)

- [ ] **Step 1: Insert helper functions**

Find the comment block starting `// Build outer→inner so smaller rings render LAST` (~line 723). Insert IMMEDIATELY BEFORE it:

```js
// ── Perimeter profile helpers (pedestal skirt + LOD seam skirts) ──
// Bilinear heightmap sampler matching the terrain vertex shader's
// LinearFilter + ClampToEdgeWrapping lookup (pixel CENTRES at
// (i+0.5)/n). Returns SCENE-space Y (absolute AMSL minus yMid). Any
// "i/(n-1)" texel-corner approximation leaves half-texel gaps against
// the rendered mesh — keep the 0.5 offset.
function makeSceneYSampler(f32, n) {
  return (hmU, hmV) => {
    const tu = hmU * n - 0.5;
    const tv = hmV * n - 0.5;
    let i0 = Math.floor(tu), i1 = i0 + 1;
    let j0 = Math.floor(tv), j1 = j0 + 1;
    let du = tu - i0, dv = tv - j0;
    if (i0 < 0)     { i0 = 0;     i1 = 0;     du = 0; }
    if (i1 > n - 1) { i0 = n - 1; i1 = n - 1; du = 0; }
    if (j0 < 0)     { j0 = 0;     j1 = 0;     dv = 0; }
    if (j1 > n - 1) { j0 = n - 1; j1 = n - 1; dv = 0; }
    const h00 = f32[j0 * n + i0];
    const h10 = f32[j0 * n + i1];
    const h01 = f32[j1 * n + i0];
    const h11 = f32[j1 * n + i1];
    return h00 * (1 - du) * (1 - dv) +
           h10 *      du  * (1 - dv) +
           h01 * (1 - du) *      dv  +
           h11 *      du  *      dv  - yMid;
  };
}

// Walk a ring's perimeter CW from NW at TERRAIN vertex resolution
// (GEO_SUBDIVS segments per edge). The terrain mesh interpolates
// linearly between adjacent edge vertices, so a profile sampled at
// every terrain vertex and connected with straight segments matches
// the rendered edge EXACTLY. Corners are shared between edges →
// 4 × GEO_SUBDIVS unique samples.
//
// PlaneGeometry vertex (u, v) maps (after rotateX(-π/2)) to scene
//   x = (u - 0.5) * sideM,  z = (0.5 - v) * sideM
// and the shader samples uHeight at hmUv = (u, 1 - v).
// Returns [{x, z, y, u, v}] — scene XZ, scene Y, plane UV.
function walkPerimeter(sampleSceneY, sideM) {
  const NSEG = GEO_SUBDIVS;
  const perim = [];
  const pushUV = (u, v) => {
    perim.push({
      x: (u - 0.5) * sideM,
      z: (0.5 - v) * sideM,
      y: sampleSceneY(u, 1.0 - v),
      u, v,
    });
  };
  // North edge: v=1, u from 0 to 1 (includes both corners)
  for (let i = 0; i <= NSEG; i++) pushUV(i / NSEG, 1);
  // East edge: u=1, v from <1 down to 0 (skip NE corner)
  for (let i = NSEG - 1; i >= 0; i--) pushUV(1, i / NSEG);
  // South edge: v=0, u from <1 to 0 (skip SE)
  for (let i = NSEG - 1; i >= 0; i--) pushUV(i / NSEG, 0);
  // West edge: u=0, v from >0 to <1 (skip SW and NW)
  for (let i = 1; i < NSEG; i++) pushUV(0, i / NSEG);
  return perim;
}
```

- [ ] **Step 2: Refactor pedestal skirt to use the helpers**

In the pedestal skirt async IIFE (search for `const sampleHmUv = (hmU, hmV) => {`, ~line 1284), DELETE everything from that line down to and including the four perimeter `for` loops ending with `for (let i = 1; i < NSEG; i++) pushUV(0, i / NSEG);` (~line 1340) — i.e. the `sampleHmUv` definition, the explanatory comment block above the walk, `const NSEG = 1024;`, `vertexXZ`, `sampleVertY`, `const perim = []`, `pushUV`, and the four loops. REPLACE with:

```js
      // Bilinear sampler + perimeter walk shared with the LOD seam
      // skirts — see makeSceneYSampler / walkPerimeter above the ring
      // build loop. Top row matches the rendered terrain edge exactly.
      const perim = walkPerimeter(makeSceneYSampler(f32, n), sideM);
```

The downstream code (`const P = perim.length;` and the 3-row geometry build) reads only `perim[k].x/.z/.y` — the extra `u`/`v` fields are ignored. Do NOT touch anything from `const P = perim.length;` onward.

- [ ] **Step 3: Syntax gate**

Run: `awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin`
Expected: exit 0, no output.

- [ ] **Step 4: Visual regression check (pedestal unchanged)**

Load `http://<host>:8081/heightfield/?slug=slopne`, orbit below the horizon: the anthracite pedestal skirt (chamfer lip + wall) must look exactly as before. Console must show `[block-base] skirt added (4096 perim samples, …)` — 4 × GEO_SUBDIVS columns.

- [ ] **Step 5: Commit**

```bash
git add heightfield/index.html
git commit -m "refactor(heightfield): extract perimeter profile helpers from pedestal skirt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Seam skirt builder + build-loop call site

**Files:**
- Modify: `heightfield/index.html` (insert builder right after the Task 1 helpers; add call in the ring build loop after `ringMeshes.push(mesh);` ~line 1054)

- [ ] **Step 1: Insert the seam-skirt builder**

IMMEDIATELY AFTER the `walkPerimeter` function from Task 1, insert:

```js
// ── LOD seam skirt ──
// Vertical curtain around an inner ring's perimeter. The outer ring
// fragment-discards a hole under this ring's footprint (uClipBox), but
// the rings sample heightmaps of different resolution (1.5 m vs 0.5 m
// step), so their surfaces disagree vertically at the cut line — by
// metres at building walls. Where the inner surface dips below the
// outer ring's cut edge, view rays escape into the void → black
// cracks. The skirt fills that gap from every angle.
//
// Top row coincides with the rendered mesh edge EXACTLY (same vertex
// positions, same bilinear filter — walkPerimeter). Both rows of each
// column share the edge UV, so the GPU stretches the ring's edge texel
// column downward — every strip continues the local edge colour, which
// camouflages the seam far better than any flat colour. The ortho +
// flip uniforms are THE SAME OBJECTS as the ring material's, so tier
// upgrades (switchOrthoTier), OSM basemap swaps and the visibility
// unload/reload cycle all propagate to the skirt with zero wiring.
const SEAM_SKIRT_DEPTH = 30;     // m below the terrain edge — covers metre-scale LOD disagreement with margin
const SEAM_SKIRT_DARKEN = 0.75;  // reads as shadow inside the seam, not as a lit wall
const seamSkirts = [];           // { mesh, ringIndex } — bare-mode toggle hides these
function buildSeamSkirt(ringIndex, r, heightData, ringMat) {
  const perim = walkPerimeter(
    makeSceneYSampler(heightData, r.heightmap_size), r.side_m);
  const P = perim.length;
  const positions = new Float32Array(P * 2 * 3);
  const uvs       = new Float32Array(P * 2 * 2);
  for (let k = 0; k < P; k++) {
    const p = perim[k];
    // Ring fragment shader samples uOrtho at vUv = (u, 1 - v) — the
    // vertex shader's hmUv flip. Replicate it so each skirt column
    // reads the exact edge texel the terrain edge above it shows.
    const tu = p.u, tv = 1.0 - p.v;
    for (let row = 0; row < 2; row++) {
      const vi = k * 2 + row;
      positions[vi * 3 + 0] = p.x;
      positions[vi * 3 + 1] = row === 0 ? p.y : p.y - SEAM_SKIRT_DEPTH;
      positions[vi * 3 + 2] = p.z;
      uvs[vi * 2 + 0] = tu;
      uvs[vi * 2 + 1] = tv;
    }
  }
  // One quad (2 tris) per perimeter segment, wrapping P-1 → 0.
  const indices = new Uint32Array(P * 6);
  for (let k = 0; k < P; k++) {
    const next = (k + 1) % P;
    const a = k * 2, b = next * 2;
    const off = k * 6;
    indices[off + 0] = a;     indices[off + 1] = a + 1; indices[off + 2] = b;
    indices[off + 3] = a + 1; indices[off + 4] = b + 1; indices[off + 5] = b;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geo.setIndex(new THREE.BufferAttribute(indices, 1));

  // Fog uniforms must be per-material clones (three.js refreshFogUniforms
  // writes .value into each material) — same pattern as the ring material.
  const fogUniforms = THREE.UniformsUtils.clone(THREE.UniformsLib.fog);
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      ...fogUniforms,
      // SHARED with the ring material — reference, do NOT clone.
      uOrtho:      ringMat.uniforms.uOrtho,
      uOrthoFlipY: ringMat.uniforms.uOrthoFlipY,
      uOrthoFlipX: ringMat.uniforms.uOrthoFlipX,
      uDarken:     { value: SEAM_SKIRT_DARKEN },
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
  const mesh = new THREE.Mesh(geo, mat);
  mesh.renderOrder = 1 + ringIndex;   // alongside its ring
  mesh.frustumCulled = false;
  scene.add(mesh);
  seamSkirts.push({ mesh, ringIndex });
  console.info(`[seam-skirt] ring ${r.slug}: ${P} columns, depth ${SEAM_SKIRT_DEPTH} m`);
}
```

- [ ] **Step 2: Call it from the ring build loop**

In the build loop, find `ringMeshes.push(mesh);` (~line 1054, after `scene.add(mesh);`). Insert IMMEDIATELY AFTER it:

```js
  // Seam skirt for every ring that sits inside another (i > 0) — the
  // LOD boundary leaks black cracks without it. Must run here while
  // `heightData` (the decoded Float32Array) is still in scope; it is
  // dropped right below for heap savings.
  if (i > 0) {
    try { buildSeamSkirt(i, r, heightData, mat); }
    catch (e) { console.warn(`[seam-skirt] ${r.slug} build failed:`, e); }
  }
```

(`mat` is the ring's ShaderMaterial defined earlier in the same loop iteration; `heightData` is assigned at the top of the loop from the LERC decode.)

- [ ] **Step 3: Syntax gate**

Run: `awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin`
Expected: exit 0, no output.

- [ ] **Step 4: Commit**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): perimeter seam skirt hides LOD ring boundary cracks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Hide seam skirts in bare-earth mode

The skirt is built from DSM heights (buildings/trees). In bare mode the terrain drops, the skirt would stick up above it. Without buildings the two heightmaps nearly agree, so the seam is invisible anyway — just hide the skirts.

**Files:**
- Modify: `heightfield/index.html` (bareToggle change handler, ~line 1618)

- [ ] **Step 1: Add visibility sync to the bare toggle handler**

In the `bareToggle.addEventListener('change', …)` handler, find:

```js
  const v = bareToggle.checked ? 1.0 : 0.0;
  for (const mesh of ringMeshes) {
    mesh.material.uniforms.uBareMode.value = v;
  }
```

Insert IMMEDIATELY AFTER that `for` loop:

```js
  // Seam skirts are built from DSM heights — in bare-earth mode they'd
  // stick up above the lowered terrain. Hide them; without buildings
  // the rings' heightmaps nearly agree and the seam is invisible.
  for (const s of seamSkirts) s.mesh.visible = v < 0.5;
```

(The visibility unload/reload cycle re-dispatches a synthetic `change` event on `bareToggle` — see `reloadAllAssets` — so restored sessions get the right visibility for free.)

- [ ] **Step 2: Syntax gate**

Run: `awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin`
Expected: exit 0, no output.

- [ ] **Step 3: Commit**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): hide seam skirts in bare-earth mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Manual visual verification (slopne, port 8081)

No code. Server already runs on `:8081` (background task; `PORT=8081 python3 server.py` if not). Viewer: `http://<host>:8081/heightfield/?slug=slopne`. Hard-reload (Cmd+Shift+R) to bypass cached HTML.

- [ ] **Step 1: Cracks gone.** Console shows `[seam-skirt] ring inner: 4096 columns, depth 30 m`. Orbit low along the inner-ring boundary (±500 m square from centre) where it crosses buildings — the previous black slivers must be filled with darkened stretched-ortho strips, from every azimuth and elevation.
- [ ] **Step 2: No new micro-cracks.** Zoom close to the seam on flat ground: the skirt's top edge must meet the terrain edge with no visible gap or z-fighting (it shares exact vertex heights, so any gap = bug in UV→position mapping).
- [ ] **Step 3: Correct texture orientation.** The skirt strip colour must continue the terrain colour directly above it (road → grey strip, field → green strip). A mismatched/mirrored edge means the flip-uniform sharing failed — check `uOrthoFlipY` propagation.
- [ ] **Step 4: Tier upgrade.** Watch the boot (mid) → ultra background upgrade (or click tier buttons): skirt sharpens together with the terrain, no stale texture.
- [ ] **Step 5: OSM toggle.** Toggle 🗺 OSM on/off: skirt swaps to the OSM basemap colours and back (shared uniform object).
- [ ] **Step 6: Bare mode.** Toggle 🌳: skirts disappear; toggle off: they return.
- [ ] **Step 7: Pedestal regression.** Pedestal chamfer + wall unchanged (Task 1 refactor touched its code path).

If any step fails, fix forward in this branch before reporting done.
