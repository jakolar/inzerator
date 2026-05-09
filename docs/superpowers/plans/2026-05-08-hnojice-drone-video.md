# Hnojice Drone Video Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-service browser tool inside `hnojice_multi.html`: realtor right-clicks a parcel, picks one of 4 camera presets (flyover/orbit/approach/top-down), exports a 20–30 s 1080p webm of a drone-style flyover.

**Architecture:** Single-file change to `hnojice_multi.html`. Adds a Video panel (right-click → context menu), 4 camera-path builders (`THREE.CatmullRomCurve3` for position + lookAt), a "presentation mode" that swaps materials on subject parcel + subject building (highlight) and dims everything else, an optional 2D overlay blitted via `texSubImage2D` so it lands in the captured stream, and a `MediaRecorder`-driven export that downloads a webm. No server changes, no new dependencies.

**Tech Stack:** Three.js r170 (existing import), `MediaRecorder` + `HTMLCanvasElement.captureStream` (browser baseline). `THREE.CatmullRomCurve3`, `THREE.EdgesGeometry`, `THREE.LineSegments` already available.

**Spec:** `docs/superpowers/specs/2026-05-08-hnojice-drone-video-design.md`

**Test approach:** No automated tests — `hnojice_multi.html` is a single-file viewer with no test framework. Each task ends in a working page state; final manual verification follows the spec's test plan.

---

## File Structure

- **Modify only:** `/Users/jan/projekty/gtaol/hnojice_multi.html`

The single-file convention is established in this project. The plan stays inside it but groups new code in clearly-marked sections so a future split (`video-tool.js` import) is mechanical.

---

### Task 1: Module-scope state for the video subsystem

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

This task establishes the state plumbing only — no behavior change visible.

- [ ] **Step 1: Add module state near the existing parcel state**

Find the existing parcel state block (added in commit `1cfc9c7`, around line 162):

```js
let _parcels = null;          // raw array from server
let _parcelGroup = null;      // THREE.Group
const _parcelMeshes = [];     // flat list of clickable top-meshes for raycast
let _parcelHover = null;      // THREE.Line currently outlining hovered parcel
```

Add immediately after:

```js
// ─── Video tool state ────────────────────────────────────────────────
let _videoSubject = null;     // { parcel, label, ring_local, area_m2, use_label, building_idx }
let _videoState = 'idle';     // 'idle' | 'panel' | 'preview' | 'recording'
let _videoTickHandle = null;  // requestAnimationFrame handle for video tick
let _videoStartTs = 0;        // performance.now() at preview/record start
let _videoDurationMs = 25000; // current selection
let _videoOverlay = false;    // info-overlay toggle
let _videoMode = 'property';  // 'property' | 'land'
let _videoPreset = 'flyover'; // 'flyover' | 'orbit' | 'approach' | 'topdown'
let _videoCurves = null;      // { posCurve, targetCurve } from buildCameraPath
const _savedSceneState = new Map();  // material/opacity backup keyed by Object3D.uuid
```

- [ ] **Step 2: Verify parses**

Open `http://<remote>:8080/hnojice_multi.html` in browser. Page still loads, no console errors. Existing buttons (Parcely, etc.) all work.

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): module state for drone-video subsystem (no behavior yet)"
```

---

### Task 2: Camera-path builders (4 presets)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

Pure-function helpers. No DOM, no scene mutation. Just take a `subject` object and return `{ posCurve, targetCurve }`.

- [ ] **Step 1: Add subject helpers and path builders**

Right after the video state block from Task 1, add:

```js
// Compute centroid + bbox + diagonal + top-Y from a parcel ring_local.
function computeSubjectGeometry(ring_local) {
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity, maxY = -Infinity;
  let sumX = 0, sumZ = 0;
  for (const [x, z, y] of ring_local) {
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    if (y > maxY) maxY = y;
    sumX += x; sumZ += z;
  }
  const cx = sumX / ring_local.length;
  const cz = sumZ / ring_local.length;
  const diagonal = Math.hypot(maxX - minX, maxZ - minZ);
  const groundY = maxY;          // approximation: top of parcel = local terrain height
  return { centroid: [cx, cz], bbox: { minX, maxX, minZ, maxZ }, diagonal, groundY };
}

// Closest road-segment point in mesh-local frame, falling back to a point
// 200 m due west of the centroid at terrain level when /api/roads has no
// data nearby (or hasn't been loaded for this viewer yet).
function findNearestRoadPoint(centroid, fallbackY) {
  const FALLBACK = [centroid[0] - 200, fallbackY, centroid[1]];
  if (typeof window.roads === 'undefined' || !Array.isArray(window.roads) || window.roads.length === 0) {
    return FALLBACK;
  }
  let best = null, bestD2 = Infinity;
  for (const polyline of window.roads) {
    for (const [x, z, y] of polyline) {
      const dx = x - centroid[0], dz = z - centroid[1];
      const d2 = dx*dx + dz*dz;
      if (d2 < bestD2) { bestD2 = d2; best = [x, y || fallbackY, z]; }
    }
  }
  return best || FALLBACK;
}

// Build CatmullRomCurve3 paths for the 4 presets.
function buildCameraPath(preset, subject) {
  const THREE = window.THREE || THREE;  // resolve in either module/global form
  const [cx, cz] = subject.centroid;
  const gy = subject.groundY;
  const tgtAt = (y = gy) => new THREE.Vector3(cx, y, cz);

  if (preset === 'flyover') {
    const posPts = [
      new THREE.Vector3(cx - 300, gy + 200, cz - 300),
      new THREE.Vector3(cx - 100, gy + 120, cz - 100),
      new THREE.Vector3(cx +  80, gy +  50, cz +   0),
    ];
    const tgtPts = [tgtAt(), tgtAt(), tgtAt()];
    const posCurve = new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5);
    const targetCurve = new THREE.CatmullRomCurve3(tgtPts, false);
    return { posCurve, targetCurve };
  }

  if (preset === 'orbit') {
    const R = Math.max(40, subject.diagonal * 1.5);
    const H = Math.max(50, subject.diagonal * 0.8);
    const N = 16;
    const posPts = [];
    for (let i = 0; i < N; i++) {
      const theta = (2 * Math.PI * i) / N;
      posPts.push(new THREE.Vector3(
        cx + R * Math.cos(theta),
        gy + H,
        cz + R * Math.sin(theta),
      ));
    }
    const tgtPts = posPts.map(() => tgtAt());
    const posCurve = new THREE.CatmullRomCurve3(posPts, true /* closed */, 'catmullrom', 0.5);
    const targetCurve = new THREE.CatmullRomCurve3(tgtPts, true);
    return { posCurve, targetCurve };
  }

  if (preset === 'approach') {
    const road = findNearestRoadPoint([cx, cz], gy);
    const mid = [(road[0] + cx) / 2, gy + 45, (road[2] + cz) / 2];
    const posPts = [
      new THREE.Vector3(road[0], gy + 30, road[2]),
      new THREE.Vector3(mid[0],  mid[1],  mid[2]),
      new THREE.Vector3(cx,      gy + 60, cz),
    ];
    const tgtPts = posPts.map(() => tgtAt());
    const posCurve = new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5);
    const targetCurve = new THREE.CatmullRomCurve3(tgtPts, false);
    return { posCurve, targetCurve };
  }

  // 'topdown'
  const posPts = [
    new THREE.Vector3(cx, gy + 120, cz),
    new THREE.Vector3(cx, gy +  80, cz),
  ];
  const tgtPts = [tgtAt(), tgtAt()];
  return {
    posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
    targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
  };
}
```

⚠️ Note `findNearestRoadPoint` references `window.roads`. The hnojice viewer doesn't currently fetch `/api/roads` (we checked). The function is written defensively so the missing global means automatic fallback to "200 m W of centroid". Acceptable for v1.

- [ ] **Step 2: Smoke check from console**

Open the page, in DevTools console:
```js
const sub = computeSubjectGeometry([[0,0,250],[10,0,250],[10,10,250],[0,10,250]]);
console.log(sub);                                    // {centroid:[5,5], diagonal: ~14, groundY: 250 ...}
const path = buildCameraPath('flyover', sub);
console.log(path.posCurve.getPointAt(0.5));          // a Vector3 between start/end
```

If you see an undefined-symbol error for `THREE`, the helpers are placed before `import * as THREE from ...` — move them after the module-level THREE import. (They're defined inside the same `<script type="module">` so `THREE` is a local import binding; just place them after the import line.)

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): subject geometry + 4 preset path builders (flyover/orbit/approach/topdown)"
```

---

### Task 3: Subject building detection (point-in-polygon)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Add point-in-polygon helper + subject-building lookup**

Add right after the path builders from Task 2:

```js
// Standard ray-casting point-in-polygon (2D, X/Z plane). Polygon is a list
// of [x, z] pairs (or [x, z, y] — Y is ignored).
function pointInPolygon2D(px, pz, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, zi] = polygon[i];
    const [xj, zj] = polygon[j];
    const intersect = ((zi > pz) !== (zj > pz)) &&
      (px < (xj - xi) * (pz - zi) / (zj - zi) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

// Find the OSM building (from window-scope `ruianBuildings`) whose footprint
// centroid lies inside the parcel ring. Returns the index into ruianBuildings,
// or -1 if no match.
function findSubjectBuildingIdx(ring_local) {
  if (!Array.isArray(ruianBuildings) || ruianBuildings.length === 0) return -1;
  for (let i = 0; i < ruianBuildings.length; i++) {
    const b = ruianBuildings[i];
    // Each building has a footprint polygon in local coords. The shape of
    // the data must match what's already in ruianBuildings (look at the
    // shape on the existing click handler for reference). We assume
    // b.coords is the ring as [[x, z], ...].
    const ring = b.coords || b.footprint || b.local_ring;
    if (!Array.isArray(ring) || ring.length < 3) continue;
    let cx = 0, cz = 0;
    for (const [x, z] of ring) { cx += x; cz += z; }
    cx /= ring.length; cz /= ring.length;
    if (pointInPolygon2D(cx, cz, ring_local)) return i;
  }
  return -1;
}
```

⚠️ The `ruianBuildings[i]` shape: this code assumes the polygon is at `b.coords` (most likely) but probes `footprint` and `local_ring` as fallbacks. **Before completing this step, open the file and find an existing reference to `ruianBuildings[…]` — what property holds the ring?** Adjust the code above to use the actual property name. Do NOT guess; if you can't tell from the code, STOP and report NEEDS_CONTEXT with whatever surrounding code you found.

- [ ] **Step 2: Smoke check from console**

Open the page (after `dataLoaded`), in console:
```js
console.log(ruianBuildings[0]);   // shape check — what is the ring property called?
const idx = findSubjectBuildingIdx([[0,0,250],[10,0,250],[10,10,250],[0,10,250]]);
console.log(idx);                 // -1 (no real building at origin) — function works
```

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): subject building lookup via point-in-polygon"
```

---

### Task 4: Save / apply / restore presentation mode

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

This task adds the highlight-the-subject-and-dim-others state machine. Three functions: `applyPresentationMode(subject, mode)`, `restorePresentationMode()`. Idempotent — multiple `apply` calls without restore in between are no-ops after the first.

- [ ] **Step 1: Add the helpers**

Add right after the building-lookup helpers from Task 3:

```js
// Save the original visual state of `obj.material` so it can be restored later.
// Uses `_savedSceneState` Map keyed by uuid. Only saves once per uuid.
function saveMaterialState(obj) {
  if (!obj || !obj.material || _savedSceneState.has(obj.uuid)) return;
  const m = obj.material;
  _savedSceneState.set(obj.uuid, {
    color: m.color ? m.color.getHex() : null,
    opacity: m.opacity,
    transparent: m.transparent,
    depthWrite: m.depthWrite,
    emissive: m.emissive ? m.emissive.getHex() : null,
    emissiveIntensity: m.emissiveIntensity,
  });
}

// Restore one material from the saved state map (and remove the entry).
function restoreMaterialState(obj) {
  if (!obj || !obj.material) return;
  const saved = _savedSceneState.get(obj.uuid);
  if (!saved) return;
  const m = obj.material;
  if (saved.color !== null && m.color) m.color.setHex(saved.color);
  m.opacity = saved.opacity;
  m.transparent = saved.transparent;
  m.depthWrite = saved.depthWrite;
  if (saved.emissive !== null && m.emissive) m.emissive.setHex(saved.emissive);
  if ('emissiveIntensity' in m) m.emissiveIntensity = saved.emissiveIntensity;
  _savedSceneState.delete(obj.uuid);
}

const SUBJECT_YELLOW = 0xfde047;
const SUBJECT_CYAN = 0x67e8f9;

// Apply highlight to subject + dim everything else. Idempotent.
function applyPresentationMode(subject, mode) {
  if (!_parcelGroup || !subject) return;
  // 1. Highlight subject parcel.
  for (const pg of _parcelGroup.children) {
    const top = pg.children[0];
    const side = pg.children[1];
    if (!top || !side) continue;
    const isSubject = top.userData.parcel && top.userData.parcel.id === subject.parcel.id;
    saveMaterialState(top);
    saveMaterialState(side);
    if (isSubject) {
      top.material.color.setHex(SUBJECT_YELLOW);
      top.material.opacity = 1.0;
      side.material.color.setHex(SUBJECT_YELLOW);
      side.material.opacity = 1.0;
    } else {
      top.material.opacity = 0.30;
      side.material.opacity = 0.30;
    }
    top.material.transparent = true;
    side.material.transparent = true;
  }
  // 2. Highlight subject building (only in 'property' mode).
  if (mode === 'property' && subject.building_idx >= 0) {
    // OSM building meshes live in the existing scene under some parent —
    // find the mesh whose userData.idx === subject.building_idx, OR look
    // up via the existing buildings array. The implementer must pick the
    // canonical lookup based on how the existing click handler highlights
    // a building.
    const subjectBuildingMesh = lookupBuildingMesh(subject.building_idx);
    if (subjectBuildingMesh) {
      saveMaterialState(subjectBuildingMesh);
      const m = subjectBuildingMesh.material;
      if (m.emissive) {
        m.emissive.setHex(SUBJECT_CYAN);
        m.emissiveIntensity = 0.45;
      }
      // Add cyan edge outline (separate child object for easy removal).
      if (!subjectBuildingMesh.userData.videoEdges) {
        const edges = new THREE.EdgesGeometry(subjectBuildingMesh.geometry);
        const mat = new THREE.LineBasicMaterial({ color: SUBJECT_CYAN, transparent: true, opacity: 0.95 });
        const lines = new THREE.LineSegments(edges, mat);
        lines.userData.videoEdgesMarker = true;
        subjectBuildingMesh.add(lines);
        subjectBuildingMesh.userData.videoEdges = lines;
      }
    }
  }
  // 3. Dim other buildings.
  scene.traverse(obj => {
    if (!obj.isMesh) return;
    if (!obj.userData || obj.userData.kind !== 'osmBuilding') return;
    if (mode === 'property' && obj === lookupBuildingMesh(subject.building_idx)) return;
    saveMaterialState(obj);
    obj.material.transparent = true;
    obj.material.opacity = 0.65;
    obj.material.depthWrite = false;
  });
}

function restorePresentationMode() {
  // Remove any subject-building edge overlays.
  scene.traverse(obj => {
    if (obj.userData && obj.userData.videoEdges) {
      const e = obj.userData.videoEdges;
      obj.remove(e);
      if (e.geometry) e.geometry.dispose();
      if (e.material) e.material.dispose();
      delete obj.userData.videoEdges;
    }
  });
  // Restore every saved material.
  const uuids = Array.from(_savedSceneState.keys());
  for (const uuid of uuids) {
    let target = null;
    scene.traverse(obj => { if (obj.uuid === uuid) target = obj; });
    if (target) restoreMaterialState(target);
    else _savedSceneState.delete(uuid);  // orphan, drop
  }
}

// Resolve OSM building mesh by its index in the `ruianBuildings` array.
// IMPORTANT: depends on the convention used by the existing building-render
// code in this file — it tags meshes with userData.kind = 'osmBuilding' and
// userData.idx = <number>. If the existing code uses different conventions,
// this lookup must be adapted to match.
function lookupBuildingMesh(idx) {
  if (idx < 0) return null;
  let found = null;
  scene.traverse(obj => {
    if (found) return;
    if (obj.isMesh && obj.userData &&
        obj.userData.kind === 'osmBuilding' &&
        obj.userData.idx === idx) {
      found = obj;
    }
  });
  return found;
}
```

⚠️ The `userData.kind === 'osmBuilding'` and `userData.idx === idx` conventions are PRESCRIPTIVE: when the implementer reads the existing OSM-building-rendering code, they must verify that meshes actually carry these userData fields. **If the existing code doesn't set userData.kind or userData.idx, two things must change**:
1. Adjust the existing OSM building loader to set `mesh.userData.kind = 'osmBuilding'; mesh.userData.idx = <i>;` when it creates each mesh.
2. Keep the lookup function as-is.

This is part of this task's responsibility — making subject lookup work end-to-end. If unable to determine the right place to set userData, STOP and report NEEDS_CONTEXT with the relevant building-loader code path.

- [ ] **Step 2: Smoke check from console**

After page load with parcels visible:
```js
const subject = {
  parcel: _parcels[0],
  building_idx: findSubjectBuildingIdx(_parcels[0].ring_local),
};
applyPresentationMode(subject, 'property');
```
Visually confirm: parcel `_parcels[0]` turns yellow, others dim. Then:
```js
restorePresentationMode();
```
Scene returns to normal. (May need to manually trigger a render frame if the loop isn't running — `renderer.render(scene, camera);`.)

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): apply/restore presentation-mode highlight + dim"
```

---

### Task 5: Video tick (camera path animation)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

The existing render loop in `hnojice_multi.html` calls `controls.update(); renderer.render(scene, camera);` inside a `requestAnimationFrame`. We need a way to swap that with a path-driven update, then swap back.

- [ ] **Step 1: Find the existing render loop**

Locate the existing `function tick() { ... }` (or `const tick = () => { ... }`) and the `requestAnimationFrame(tick)` call that drives it. There is exactly one in `hnojice_multi.html`. Note the line number for reference.

- [ ] **Step 2: Refactor render loop to dispatch via `_currentTick`**

If the current render loop is something like:
```js
function tick() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}
tick();
```

Change it to:
```js
function interactiveTick() {
  controls.update();
  renderer.render(scene, camera);
}
let _currentTick = interactiveTick;
function tick() {
  _currentTick();
  requestAnimationFrame(tick);
}
tick();
```

This is a behavior-preserving refactor — `_currentTick` defaults to the existing interactive update.

- [ ] **Step 3: Add the video tick implementation**

Right after the new `interactiveTick` definition (or in the video helper section, your call), add:

```js
function smoothstep(t) {
  // Cubic Hermite, identical to GLSL smoothstep(0,1,t).
  const x = Math.max(0, Math.min(1, t));
  return x * x * (3 - 2 * x);
}

function videoTick() {
  if (!_videoCurves || !_videoStartTs) return;
  const elapsed = performance.now() - _videoStartTs;
  const tRaw = Math.min(1, elapsed / _videoDurationMs);
  const t = smoothstep(tRaw);
  const pos = _videoCurves.posCurve.getPointAt(t);
  const tgt = _videoCurves.targetCurve.getPointAt(t);
  camera.position.copy(pos);
  camera.lookAt(tgt);
  renderer.render(scene, camera);
  if (_videoOverlay) blitOverlay();         // defined in Task 6
  if (tRaw >= 1) _onVideoComplete();        // defined in Task 7/8
}

function startVideoTick(curves, durationMs) {
  _videoCurves = curves;
  _videoDurationMs = durationMs;
  _videoStartTs = performance.now();
  _currentTick = videoTick;
}

function stopVideoTick() {
  _currentTick = interactiveTick;
  _videoCurves = null;
  _videoStartTs = 0;
}

// Stub for Task 6 to fill in.
function blitOverlay() { /* implemented in Task 6 */ }

// Stub for Task 7/8 to fill in.
function _onVideoComplete() {
  stopVideoTick();
}
```

- [ ] **Step 4: Smoke check from console**

After page load, in console:
```js
const sub = computeSubjectGeometry(_parcels[0].ring_local);
const path = buildCameraPath('orbit', { ...sub, building_idx: -1 });
startVideoTick(path, 8000);
// Watch the camera animate over 8 seconds.
// After it completes, OrbitControls should work again.
```

If the camera doesn't animate, verify `_currentTick` actually swapped (`console.log(_currentTick === videoTick)`).

- [ ] **Step 5: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): swappable interactive/video tick + path-driven camera"
```

---

### Task 6: Info overlay via texSubImage2D blit

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

The overlay must be drawn ONTO the WebGL canvas (not as a sibling DOM `<canvas>`) so that `MediaRecorder.captureStream()` reads it. The cleanest way: render a tiny CSS-2D pill onto an `OffscreenCanvas`, copy as a texture, draw a fullscreen quad into the WebGL framebuffer over the existing render. This adds noticeable complexity. We use a simpler equivalent: render a transparent `THREE.Sprite` whose texture is updated each frame from a 2D canvas. Sprite is fixed in screen space via post-render camera math.

Actually simpler still: render the 2D-text canvas as a `THREE.PlaneGeometry` attached to the camera and rendered in an Orthographic overlay scene. Three.js's standard "HUD" pattern.

- [ ] **Step 1: Add the overlay scene + texture**

Add right after the video tick definitions:

```js
// HUD overlay rendered via a separate Orthographic scene drawn after the
// main scene. This gives MediaRecorder a baked-in info pill in the canvas.
let _hudCanvas = null, _hudTexture = null, _hudMesh = null, _hudScene = null, _hudCam = null;

function initHud() {
  if (_hudCanvas) return;
  _hudCanvas = document.createElement('canvas');
  _hudCanvas.width = 512;
  _hudCanvas.height = 96;
  _hudTexture = new THREE.CanvasTexture(_hudCanvas);
  _hudTexture.minFilter = THREE.LinearFilter;
  _hudTexture.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.MeshBasicMaterial({ map: _hudTexture, transparent: true, depthTest: false });
  const geo = new THREE.PlaneGeometry(1, 1);
  _hudMesh = new THREE.Mesh(geo, mat);
  _hudScene = new THREE.Scene();
  _hudScene.add(_hudMesh);
  _hudCam = new THREE.OrthographicCamera(0, 1, 1, 0, 0, 1);
}

function drawHudText(label, area, useLabel) {
  initHud();
  const c = _hudCanvas;
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  // Background pill.
  ctx.fillStyle = 'rgba(0,0,0,0.55)';
  const r = 12;
  ctx.beginPath();
  ctx.roundRect(0, 0, c.width, c.height, r);
  ctx.fill();
  // Text.
  ctx.fillStyle = 'white';
  ctx.font = '600 24px system-ui, sans-serif';
  ctx.textBaseline = 'top';
  ctx.fillText(label, 16, 14);
  ctx.font = '400 18px system-ui, sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.85)';
  const fmtArea = `${area.toLocaleString('cs-CZ')} m²`;
  ctx.fillText(`${fmtArea} · ${useLabel || '—'}`, 16, 50);
  _hudTexture.needsUpdate = true;
}

function blitOverlay() {
  if (!_hudMesh) return;
  // Position pill at lower-left, 16 px from edges, fixed pixel size.
  const W = renderer.domElement.width;
  const H = renderer.domElement.height;
  const pillW = 320, pillH = 60;
  const x = 16 / W;
  const y = (16) / H;
  const w = pillW / W;
  const h = pillH / H;
  _hudMesh.scale.set(w, h, 1);
  _hudMesh.position.set(x + w / 2, y + h / 2, 0);
  // We must not let the auto-clear wipe the main scene; render the HUD
  // with autoClear off.
  const wasAutoClear = renderer.autoClear;
  renderer.autoClear = false;
  renderer.render(_hudScene, _hudCam);
  renderer.autoClear = wasAutoClear;
}
```

- [ ] **Step 2: Smoke check from console**

```js
drawHudText('č.p. 47', 1245, 'zahrada');
_videoOverlay = true;
// next tick should draw the HUD; force one if needed:
_currentTick = () => { interactiveTick(); blitOverlay(); };
// ...visually verify pill in lower-left of the canvas
_videoOverlay = false;
_currentTick = interactiveTick;
```

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): HUD overlay (info pill) drawn into WebGL canvas"
```

---

### Task 7: Video panel UI (HTML + CSS + open/close wiring)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Add the video panel HTML**

Find the existing `<div id="building-popup">` block. Add a sibling block immediately after it:

```html
<div id="video-panel" style="display:none">
  <div class="vp-header">
    <span class="vp-title">🎬 Video pro parcelu</span>
    <span id="vp-close" class="vp-close">&times;</span>
  </div>
  <div class="vp-meta">
    <span id="vp-label"></span> · <span id="vp-area"></span> m² · <span id="vp-use"></span>
  </div>
  <fieldset class="vp-fieldset">
    <legend>Preset</legend>
    <label><input type="radio" name="vp-preset" value="flyover" checked> Flyover (25s)</label>
    <label><input type="radio" name="vp-preset" value="orbit"> Orbit (20s)</label>
    <label><input type="radio" name="vp-preset" value="approach"> Approach (15s)</label>
    <label><input type="radio" name="vp-preset" value="topdown"> Top-down (15s)</label>
  </fieldset>
  <fieldset class="vp-fieldset">
    <legend>Mód</legend>
    <label><input type="radio" name="vp-mode" value="property" checked> Property (parcel + budova)</label>
    <label><input type="radio" name="vp-mode" value="land"> Land only</label>
  </fieldset>
  <label class="vp-row"><input type="checkbox" id="vp-overlay"> Info overlay</label>
  <label class="vp-row">Délka <input type="range" id="vp-duration" min="15" max="45" step="1" value="25"> <span id="vp-duration-val">25</span>s</label>
  <div class="vp-actions">
    <button id="vp-preview">Preview</button>
    <button id="vp-export">Export</button>
  </div>
  <div id="vp-progress" class="vp-progress" style="display:none">
    <div id="vp-progress-bar"></div>
  </div>
</div>
```

- [ ] **Step 2: Add the panel CSS**

In the existing `<style>` block, add at the end:

```css
#video-panel {
  position: absolute; top: 70px; right: 10px; z-index: 30;
  background: rgba(255,255,255,0.97); padding: 14px 16px; border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.3); width: 280px; font-size: 13px;
}
#video-panel .vp-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
#video-panel .vp-title { font-weight: 600; color: #1a73e8; font-size: 14px; }
#video-panel .vp-close { cursor: pointer; color: #999; font-size: 18px; }
#video-panel .vp-meta { color: #555; font-size: 12px; margin-bottom: 10px; }
#video-panel fieldset.vp-fieldset { border: 1px solid #ddd; border-radius: 4px; padding: 6px 8px; margin: 0 0 8px 0; }
#video-panel fieldset.vp-fieldset legend { color: #888; font-size: 11px; padding: 0 4px; }
#video-panel fieldset.vp-fieldset label { display: block; margin: 3px 0; cursor: pointer; }
#video-panel .vp-row { display: block; margin: 6px 0; }
#video-panel input[type=range] { width: 60%; vertical-align: middle; }
#video-panel .vp-actions { display: flex; gap: 8px; margin-top: 10px; }
#video-panel .vp-actions button {
  flex: 1; padding: 7px 0; border-radius: 4px; border: 1px solid #ccc;
  background: #f6f6f6; cursor: pointer; font-size: 13px;
}
#video-panel .vp-actions button:hover { background: #e8e8e8; }
#video-panel .vp-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
#video-panel .vp-progress {
  height: 6px; background: #eee; border-radius: 3px; margin-top: 10px; overflow: hidden;
}
#video-panel .vp-progress-bar {
  /* applied by JS by id, not class */
}
#video-panel #vp-progress-bar { height: 100%; background: #1a73e8; width: 0%; transition: width 0.1s linear; }
```

- [ ] **Step 3: Add open/close + preset/mode/overlay/duration wiring**

In the `<script type="module">` body, after the video helpers, add:

```js
function openVideoPanel(parcel) {
  const ring = parcel.ring_local;
  if (!ring || ring.length < 3) return;
  const sg = computeSubjectGeometry(ring);
  const buildingIdx = findSubjectBuildingIdx(ring);
  _videoSubject = {
    parcel,
    label: parcel.label,
    area_m2: parcel.area_m2,
    use_label: parcel.use_label,
    ring_local: ring,
    centroid: sg.centroid,
    diagonal: sg.diagonal,
    groundY: sg.groundY,
    building_idx: buildingIdx,
  };
  document.getElementById('vp-label').textContent = `parcela ${parcel.label}`;
  document.getElementById('vp-area').textContent  = parcel.area_m2.toLocaleString('cs-CZ');
  document.getElementById('vp-use').textContent   = parcel.use_label || '—';
  document.getElementById('video-panel').style.display = 'block';
  // Prefer building č.p. when subject building is found (RÚIAN cislo on
  // the matched building). Fall back to "parcela <label>" otherwise.
  let hudTitle = `parcela ${parcel.label}`;
  if (buildingIdx >= 0) {
    const b = ruianBuildings[buildingIdx];
    if (b && (b.cislo || b.cislo_domovni)) {
      hudTitle = `č.p. ${b.cislo || b.cislo_domovni}`;
    }
  }
  drawHudText(hudTitle, parcel.area_m2, parcel.use_label);
  applyPresentationMode(_videoSubject, _videoMode);
  _videoState = 'panel';
}

function closeVideoPanel() {
  if (_videoState === 'preview' || _videoState === 'recording') {
    // refuse to close mid-record; user must wait
    return;
  }
  document.getElementById('video-panel').style.display = 'none';
  restorePresentationMode();
  _videoSubject = null;
  _videoState = 'idle';
}

document.getElementById('vp-close').addEventListener('click', closeVideoPanel);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _videoState === 'panel') closeVideoPanel();
});

document.querySelectorAll('input[name=vp-preset]').forEach(r => {
  r.addEventListener('change', () => {
    _videoPreset = r.value;
    const defaults = { flyover: 25, orbit: 20, approach: 15, topdown: 15 };
    const slider = document.getElementById('vp-duration');
    slider.value = defaults[_videoPreset] || 25;
    document.getElementById('vp-duration-val').textContent = slider.value;
    _videoDurationMs = parseInt(slider.value) * 1000;
  });
});
document.querySelectorAll('input[name=vp-mode]').forEach(r => {
  r.addEventListener('change', () => {
    _videoMode = r.value;
    if (_videoSubject) {
      restorePresentationMode();
      applyPresentationMode(_videoSubject, _videoMode);
    }
  });
});
document.getElementById('vp-overlay').addEventListener('change', e => {
  _videoOverlay = e.target.checked;
});
document.getElementById('vp-duration').addEventListener('input', e => {
  document.getElementById('vp-duration-val').textContent = e.target.value;
  _videoDurationMs = parseInt(e.target.value) * 1000;
});
```

- [ ] **Step 4: Hook the right-click on parcel to open the panel**

Find the existing parcel click handler (added in commit `1fdb164`). Right after the existing block that handles **left-click on a parcel** (the one that `popup.innerHTML = ...` for parcels), add a `contextmenu` handler:

```js
renderer.domElement.addEventListener('contextmenu', (e) => {
  if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) return;
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const phits = raycaster.intersectObjects(_parcelMeshes, false);
  if (!phits.length) return;
  e.preventDefault();
  const parcel = phits[0].object.userData.parcel;
  if (_videoState !== 'idle' && _videoState !== 'panel') return;  // refuse mid-record
  if (_videoState === 'panel') closeVideoPanel();  // re-target
  openVideoPanel(parcel);
});
```

- [ ] **Step 5: Smoke check**

Refresh page, toggle Parcely on, right-click any building parcel. Video panel appears at top-right with parcel info filled in. Subject parcel highlighted yellow, others dimmed, building (if any) glowing cyan. Switch Mode to Land — building glow disappears. Toggle overlay checkbox — no immediate visible change yet (overlay only blits during video tick; we test that in next task). Click X close — scene returns to normal.

- [ ] **Step 6: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): video panel UI + open/close + right-click wiring"
```

---

### Task 8: Preview button — animate without recording

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Wire the Preview button**

Add right after the panel control wiring from Task 7 Step 3:

```js
document.getElementById('vp-preview').addEventListener('click', () => {
  if (!_videoSubject || _videoState === 'preview' || _videoState === 'recording') return;
  const subjectForPath = {
    centroid: _videoSubject.centroid,
    groundY: _videoSubject.groundY,
    diagonal: _videoSubject.diagonal,
  };
  const curves = buildCameraPath(_videoPreset, subjectForPath);
  _videoState = 'preview';
  document.getElementById('vp-preview').disabled = true;
  document.getElementById('vp-export').disabled = true;
  // Show progress bar.
  const prog = document.getElementById('vp-progress');
  const bar = document.getElementById('vp-progress-bar');
  prog.style.display = 'block';
  bar.style.width = '0%';
  // Drive the bar from rAF since the duration is known.
  const t0 = performance.now();
  const updateBar = () => {
    if (_videoState !== 'preview') return;
    const t = Math.min(1, (performance.now() - t0) / _videoDurationMs);
    bar.style.width = `${(t * 100).toFixed(1)}%`;
    if (t < 1) requestAnimationFrame(updateBar);
  };
  requestAnimationFrame(updateBar);
  startVideoTick(curves, _videoDurationMs);
});

// Override the placeholder _onVideoComplete from Task 5.
_onVideoComplete = function () {
  stopVideoTick();
  document.getElementById('vp-progress').style.display = 'none';
  document.getElementById('vp-progress-bar').style.width = '0%';
  document.getElementById('vp-preview').disabled = false;
  document.getElementById('vp-export').disabled = false;
  if (_videoState === 'preview') {
    _videoState = 'panel';
  } else if (_videoState === 'recording') {
    // hand off to recording-specific finalization (Task 9 wires this in).
    _onRecordingFrameComplete && _onRecordingFrameComplete();
  }
};
```

⚠️ The `_onVideoComplete` was declared as a function in Task 5. To "override" we re-assign. Since `_onVideoComplete` was declared with `function`, it's a function declaration in module scope and CAN be reassigned (function declarations create `var`-like bindings). If the implementer used `function` strict-mode where this fails, change Task 5's declaration to `let _onVideoComplete = function() { stopVideoTick(); };` so the rebinding here works.

- [ ] **Step 2: Smoke check**

Right-click a parcel → panel opens. Click Preview. Camera animates over 25 s for Flyover. Buttons disabled, progress bar fills. After 25 s, controls re-enabled, scene stays in presentation mode (panel still open). Click Preview again with Orbit selected — camera makes a full loop. Etc.

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): Preview button — animate selected preset without recording"
```

---

### Task 9: Export button — record + download

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Wire the Export button**

Add right after the Preview wiring:

```js
function pickWebmMime() {
  const candidates = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
  for (const m of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) return m;
  }
  return 'video/webm';
}

let _onRecordingFrameComplete = null;     // assigned per recording session

document.getElementById('vp-export').addEventListener('click', () => {
  if (!_videoSubject || _videoState === 'preview' || _videoState === 'recording') return;
  const subjectForPath = {
    centroid: _videoSubject.centroid,
    groundY: _videoSubject.groundY,
    diagonal: _videoSubject.diagonal,
  };
  const curves = buildCameraPath(_videoPreset, subjectForPath);

  const stream = renderer.domElement.captureStream(30);
  const mime = pickWebmMime();
  let recorder;
  try {
    recorder = new MediaRecorder(stream, {
      mimeType: mime,
      videoBitsPerSecond: 8_000_000,
    });
  } catch (err) {
    alert(`MediaRecorder unsupported: ${err.message}`);
    return;
  }
  const chunks = [];
  recorder.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);

  _videoState = 'recording';
  document.getElementById('vp-preview').disabled = true;
  document.getElementById('vp-export').disabled = true;
  const prog = document.getElementById('vp-progress');
  const bar  = document.getElementById('vp-progress-bar');
  prog.style.display = 'block';
  bar.style.width = '0%';
  const t0 = performance.now();
  const updateBar = () => {
    if (_videoState !== 'recording') return;
    const t = Math.min(1, (performance.now() - t0) / _videoDurationMs);
    bar.style.width = `${(t * 100).toFixed(1)}%`;
    if (t < 1) requestAnimationFrame(updateBar);
  };
  requestAnimationFrame(updateBar);

  _onRecordingFrameComplete = () => {
    _onRecordingFrameComplete = null;
    recorder.stop();
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 16);
      a.href = url;
      a.download = `parcela-${_videoSubject.label.replace(/[\\/]/g, '-')}-${_videoPreset}-${ts}.webm`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      // Reset UI.
      document.getElementById('vp-progress').style.display = 'none';
      document.getElementById('vp-progress-bar').style.width = '0%';
      document.getElementById('vp-preview').disabled = false;
      document.getElementById('vp-export').disabled = false;
      _videoState = 'panel';
    };
  };

  recorder.start();
  startVideoTick(curves, _videoDurationMs);
});
```

- [ ] **Step 2: Smoke check**

Right-click parcel → Export with Flyover. Buttons disabled, progress bar fills, camera animates. After ~25 s the recorder stops, browser downloads `parcela-<label>-flyover-<ts>.webm`. Open the file locally — same animation plays. File size ~20–30 MB.

Try Export with each of the other 3 presets. Try toggling overlay on, then Export — pill should appear in the recorded video.

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice-video): Export — record canvas via MediaRecorder, download webm"
```

---

### Task 10: Final manual verification

**Files:** none (manual)

- [ ] **Step 1: Run the spec's full test plan**

Walk the 11 steps from `docs/superpowers/specs/2026-05-08-hnojice-drone-video-design.md` § "Test plan". Note any deviations.

- [ ] **Step 2: Verify file size budget**

`du -h /Users/jan/projekty/gtaol/hnojice_multi.html` — should still be under ~80 KB (the Task 0 refactor brought it to ~40 KB; this plan adds ~380 lines or roughly 10–15 KB of code).

- [ ] **Step 3: Note any deviations and fix in follow-up commits as needed.**

---

## Self-review notes

- **Spec coverage:**
  - User flow (panel + right-click + presets + mode + overlay + duration + Preview/Export): Tasks 7–9 ✓
  - 4 camera presets with concrete math (flyover, orbit, approach, topdown): Task 2 ✓
  - Subject viz (Property + Land modes, parcel highlight, building emissive + edges, others dimmed): Task 4 ✓
  - Subject building detection (point-in-polygon over `ruianBuildings`): Task 3 ✓
  - Info overlay baked into recording (HUD via Orthographic scene + texSubImage2D-equivalent through Three.js render pass): Task 6 ✓
  - Recording pipeline (MediaRecorder + captureStream + chunks + download): Task 9 ✓
  - State management (open/close persistence across exports, ESC, refusal mid-record): Tasks 7+8+9 ✓
- **Placeholder scan:** clean. Two notes flagged in Task 3 (`b.coords/footprint/local_ring`) and Task 4 (`userData.kind`) require the implementer to read existing code and adapt — not placeholders, but explicit "verify-this-shape" gates.
- **Type consistency:**
  - `_videoSubject` shape: defined in Task 7 (`{parcel, label, area_m2, use_label, ring_local, centroid, diagonal, groundY, building_idx}`); used in Tasks 4, 8, 9 — consistent.
  - `_videoCurves`: `{posCurve, targetCurve}` set in Task 5 (`startVideoTick`), produced by `buildCameraPath` in Task 2, used by `videoTick` — consistent.
  - `_videoState` values: `'idle' | 'panel' | 'preview' | 'recording'` declared in Task 1, used in Tasks 7, 8, 9 — consistent.
  - `_currentTick` swapping (Task 5): cleanly returns to `interactiveTick` on `_onVideoComplete` (Task 8 override).
- **Edge cases handled:**
  - No subject building (forest parcel): `building_idx === -1`, `applyPresentationMode` skips the cyan-glow branch.
  - User clicks Export while one is running: state guard rejects.
  - User presses Esc mid-recording: `closeVideoPanel` refuses to close in `'recording'` state.
  - Codec fallback: vp9 → vp8 → generic webm.
- **Known follow-ups (out of plan scope):**
  - mp4 export (would need ffmpeg.wasm)
  - Mobile / touch UX (no touch right-click)
  - Music / voiceover
  - Branded logo overlay (would extend Task 6)
