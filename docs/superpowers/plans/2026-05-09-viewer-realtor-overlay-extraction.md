# Viewer / realtor-overlay extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `hnojice_multi.html` into a generic generated viewer (terrain + ortho + cadastre + basic building popup) plus a separate `viewer-realtor-overlay.js` ES module that adds the realtor-specific stack (parcels layer, single-parcel highlight, drone video tool with 9 presets + 6 highlights + sunset + MP4 export, free flythrough). Update `gen_multitile.py` template so future regenerations preserve realtor features.

**Architecture:** Base viewer exposes a small API (scene/camera/renderer/controls + tick hooks + popup getter + terrain raycast helper) to the overlay via `init({...})`. Overlay injects its own CSS/HTML inline (template literals) and wires its own handlers. Try-import on the overlay file at the end of base's module — if missing or broken, base survives as a plain village viewer.

**Tech Stack:** Vanilla JS (ES modules + importmap, no build step), THREE.js r170, browser MediaRecorder + ffmpeg.wasm (carried over from existing implementation).

**Spec:** `docs/superpowers/specs/2026-05-09-viewer-realtor-overlay-extraction.md`

**Test approach:** No automated frontend tests (project convention). Each task ends with a manual browser check. Server must be running with cache + tile data symlinked from gtaol (one-time setup before Task 1).

---

## File Structure

- **Modify:** `/Users/jan/projekty/inzerator/hnojice_multi.html` (canonical viewer; serves as both the post-refactor viewer AND the template reference)
- **Create:** `/Users/jan/projekty/inzerator/viewer-realtor-overlay.js` (new ES module)
- **Modify:** `/Users/jan/projekty/inzerator/gen_multitile.py` (template emit)

---

## Pre-task setup: server + symlinks

Before starting Task 1, prepare the inzerator runtime:

```bash
cd /Users/jan/projekty/inzerator
# Symlink shared data from gtaol so we don't duplicate ~83MB of binaries
ln -s ../gtaol/cache cache
ln -s ../gtaol/tiles_hnojice_multi tiles_hnojice_multi
# Stop gtaol server, start inzerator server
kill $(lsof -ti:8080) 2>/dev/null; sleep 1
python3 server.py > /tmp/inzerator-server.log 2>&1 &
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/hnojice_multi.html  # expect 200
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/api/parcels?gcx=-547700&gcy=-1107700&radius=2000"  # expect 200 (disk cache)
```

After setup, the implementer should be able to open `http://<host>:8080/hnojice_multi.html` and see Hnojice render.

---

### Task 1: Add tick-hook API to base (no behavior change)

**Files:**
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`

Refactors the existing render loop so the overlay can register per-frame hooks and override the main tick. No visible change to the viewer.

- [ ] **Step 1: Locate and refactor the existing tick loop**

Find the existing render loop near the bottom of `<script type="module">`:

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

Replace with:

```js
function interactiveTick() {
  controls.update();
  renderer.render(scene, camera);
}
let _mainTick = interactiveTick;
const _tickHooks = [];
function tick() {
  _mainTick();
  for (const hook of _tickHooks) hook();
  requestAnimationFrame(tick);
}
tick();

// Tick API exposed to the realtor overlay.
const addTickHook    = fn => { _tickHooks.push(fn); };
const removeTickHook = fn => { const i = _tickHooks.indexOf(fn); if (i >= 0) _tickHooks.splice(i, 1); };
const setMainTick    = fn => { _mainTick = fn; };
const resetMainTick  = ()  => { _mainTick = interactiveTick; };
```

Note: the variable rename from `_currentTick` to `_mainTick` is intentional — it makes the role clearer (the swappable main tick, distinct from per-frame hooks).

- [ ] **Step 2: Update existing references**

Search the file for `_currentTick`. With the refactor in `_sceneOverlay` from gtaol's commit `d5bf116`, references are inside the migrated functions (videoTick, startVideoTick, stopVideoTick, etc. — currently still in this file). Update those references too:

- `startVideoTick`: change `_currentTick = videoTick;` to `setMainTick(videoTick);`
- `stopVideoTick`: change `_currentTick = interactiveTick;` to `resetMainTick();`
- `pauseVideoTick`: change `_currentTick = interactiveTick;` to `setMainTick(interactiveTick);` (preserves pause-as-noop behavior)
- `resumeVideoTick`: change `_currentTick = videoTick;` to `setMainTick(videoTick);`
- `_sceneOverlay.tick()` already calls `tickHighlights()` directly — don't change that. After this task, `_sceneOverlay.tick` will continue to work but won't be called from anywhere yet (we'll add `addTickHook(_sceneOverlay.tick)` in the overlay's init in a later task).

⚠️ Actually verify: search file for `_currentTick`. Should return ZERO matches after Step 2.

- [ ] **Step 3: Wire `_sceneOverlay.tick` into the new hooks system**

Currently `_sceneOverlay.tick` is called from `interactiveTick` and `videoTick` (per gtaol's d5bf116). After the refactor those calls would still be inline — but cleaner is to register `_sceneOverlay.tick` once via `addTickHook`. Find the current calls:

```js
function interactiveTick() {
  controls.update();
  _sceneOverlay.tick();          // ← remove this line
  renderer.render(scene, camera);
}
```

```js
function videoTick() {
  // ...
  _sceneOverlay.tick();          // ← remove this line
  renderer.render(scene, camera);
  // ...
}
```

Remove both inline calls. Then near where `_sceneOverlay` is defined, add a one-time registration AFTER `addTickHook` is in scope:

```js
addTickHook(() => _sceneOverlay.tick());
```

Now `_sceneOverlay.tick` runs once per frame regardless of which `_mainTick` is active.

- [ ] **Step 4: Verify in browser**

Reload viewer. Everything should still work — terrain, ortofoto, basemap toggles, building popup, parcels button, drone video preview, MP4 export. No console errors. **Functionally identical to pre-refactor.**

- [ ] **Step 5: Commit**

```bash
git add hnojice_multi.html
git commit -m "refactor(viewer): tick-hook API (preparation for overlay extraction)

$(cat <<'EOF'
Replaces the single _currentTick swappable ref with two separate
concerns:
- _mainTick: the swappable main render call (interactiveTick or
  videoTick during preview/recording). Swapped via setMainTick /
  resetMainTick.
- _tickHooks: array of per-frame side-effect callbacks (e.g.
  highlight animations). Hooks run after every main tick regardless
  of which mainTick is active. Registered via addTickHook /
  removeTickHook.

_sceneOverlay.tick is now registered once via addTickHook instead
of being inline in interactiveTick + videoTick.

No visible behavior change. This is the API the realtor overlay
will use after extraction (next task creates the overlay file).
EOF
)"
```

---

### Task 2: Create overlay skeleton + try-import in base

**Files:**
- Create: `/Users/jan/projekty/inzerator/viewer-realtor-overlay.js`
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`

Lays the `init({...})` contract. Overlay loads but does nothing visible yet — verifies the import + arg passing works.

- [ ] **Step 1: Create skeleton overlay file**

```js
// viewer-realtor-overlay.js
// Realtor-specific superpowers for the inzerator 3D village viewer.
// Loaded by the generated viewer (e.g. hnojice_multi.html) at the end
// of its module script via:
//   const overlay = await import('./viewer-realtor-overlay.js');
//   overlay.init({ ...args });
//
// This file owns: parcels layer, single-parcel click highlight, drone
// video panel + presets + highlights + sunset tint + MP4 export, free
// flythrough mode, building popup video link injection.
//
// State scoped to closure / module top-level — base viewer never reaches in.

export function init(args) {
  const { THREE, scene, camera, renderer, controls,
          allMeshes, ruianBuildings, gcx, gcy,
          addTickHook, removeTickHook, setMainTick, resetMainTick,
          getBuildingPopup, getTerrainHeightAt } = args;

  // Sanity: log to confirm overlay activated.
  console.info('[realtor-overlay] init OK', {
    location: { gcx, gcy },
    tiles: allMeshes.length,
    buildings: Array.isArray(ruianBuildings) ? ruianBuildings.length : 0,
  });

  // TODO in subsequent tasks: inject CSS, inject HTML, wire handlers,
  // register tick hooks, etc.
}
```

- [ ] **Step 2: Add `getTerrainHeightAt` helper to base if missing**

Search `hnojice_multi.html` for `function getTerrainHeightAt`. If it exists (it should — used by current single-parcel highlight + applyHighlights), no action. If not, add this helper before the realtor code:

```js
function getTerrainHeightAt(x, z) {
  const r = new THREE.Raycaster();
  r.set(new THREE.Vector3(x, 5000, z), new THREE.Vector3(0, -1, 0));
  const hits = r.intersectObjects(allMeshes, false);
  return hits.length ? hits[0].point.y : 0;
}
```

- [ ] **Step 3: Add try-import block at end of base's module script**

Find the very bottom of `<script type="module">` in `hnojice_multi.html` — should be right after `tick();`. Add:

```js
// ─── Realtor overlay try-import ─────────────────────────────────────
// Optional: loads viewer-realtor-overlay.js if present, otherwise the
// base viewer continues as a plain 3D village viewer.
try {
  const overlay = await import('./viewer-realtor-overlay.js');
  overlay.init({
    THREE, scene, camera, renderer, controls,
    allMeshes, ruianBuildings, gcx, gcy,
    addTickHook, removeTickHook, setMainTick, resetMainTick,
    getBuildingPopup: () => document.getElementById('building-popup'),
    getTerrainHeightAt,
  });
} catch (e) {
  console.warn('[viewer] realtor overlay not loaded:', e);
}
```

⚠️ The `await import(...)` requires the script to be in a module context (it is — `<script type="module">`). If you see `Uncaught SyntaxError: await is only valid in async functions`, the script tag is missing `type="module"`.

- [ ] **Step 4: Verify overlay loads**

Reload viewer. Open DevTools console. Should see:
- `[realtor-overlay] init OK { location: {...}, tiles: 625, buildings: ~700 }`

If you see `[viewer] realtor overlay not loaded: TypeError: ...`, the import or init failed — fix before proceeding.

- [ ] **Step 5: Commit**

```bash
git add hnojice_multi.html viewer-realtor-overlay.js
git commit -m "feat(viewer): overlay skeleton + try-import contract

$(cat <<'EOF'
Creates viewer-realtor-overlay.js as a skeleton ES module exporting
init({...}). Generated viewer attempts await-import at the end of
its module script and calls init with all the refs the overlay
will need (THREE, scene, camera, renderer, controls, allMeshes,
ruianBuildings, gcx/gcy, tick API, getBuildingPopup,
getTerrainHeightAt).

If the overlay file is missing or init throws, the page survives —
console.warn surfaces the failure but the base viewer continues.

No realtor features migrated yet; subsequent tasks move them
cluster by cluster.
EOF
)"
```

---

### Task 3: Migrate Parcels feature

**Files:**
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`
- Modify: `/Users/jan/projekty/inzerator/viewer-realtor-overlay.js`

Move the Parcely button + 1999-parcel layer + click + hover from base to overlay. Single commit at end leaves page functionally identical.

- [ ] **Step 1: In overlay, add Parcels module-scope state + helpers**

Inside the overlay file, BEFORE the `export function init(args)`, add module-scope state:

```js
// ── Parcels layer state (module-scope, lives across init/teardown) ──
const PARCEL_COLORS = {
  2:  0xb9905a,  3:  0x3a5f2a,  4:  0x5a3a55,  5:  0x6a8a3a,
  6:  0x4a7a30,  7:  0x8db04a,  10: 0x1f3a1c,  11: 0x2a4a6e,
  13: 0x555555,  14: 0xa89878,
};
const PARCEL_FALLBACK = 0x777777;
const PARCEL_TILE_H = 0.15;
const PARCEL_LIFT   = 0.02;

let _parcels = null;
let _parcelGroup = null;
const _parcelMeshes = [];
let _parcelHover = null;
```

These are the EXACT same declarations currently in `hnojice_multi.html` lines 243-262.

- [ ] **Step 2: In overlay, add Parcels helpers** (`buildParcelGroup`, `ensureParcelsData`, `ensureParcels`)

Inside the overlay file, after the state block, add these THREE functions VERBATIM from current `hnojice_multi.html`:
- `buildParcelGroup(parcels)` — currently around line 297 in the relevant section
- `ensureParcelsData()` — around line 1724 (split helper)
- `ensureParcels()` — uses `ensureParcelsData()`

Important: `THREE`, `scene`, `gcx`, `gcy` are needed by these. They're available in the overlay via `args`. Place these helpers INSIDE `init(args)` so they close over `THREE`, `scene`, `gcx`, `gcy` from `args`. Do not declare them at module scope.

- [ ] **Step 3: In overlay's `init(args)`, inject Parcely button + hint**

The base's `#info` panel HTML currently has the Parcely button + 🎬 hint + free record button hardcoded. These will be REMOVED from base in the next step. Overlay needs to inject them.

Inside `init(args)`, near the end:

```js
// Inject realtor controls into the existing #info panel.
const info = document.getElementById('info');
if (info) {
  info.insertAdjacentHTML('beforeend', `
    <hr>
    <button id="parcelsBtn" style="width:100%;padding:6px;border-radius:4px;border:1px solid #ccc;background:#f6f6f6;cursor:pointer;font-size:12px">
      Parcely (RÚIAN) — vyp
    </button>
  `);
  document.head.insertAdjacentHTML('beforeend', `
    <style>
      #parcelsBtn.active { background: #1a73e8; color: white; border-color: #1456b8; }
      #parcelsBtn:hover { background: #e8e8e8; }
      #parcelsBtn.active:hover { background: #1456b8; }
    </style>
  `);
}
```

- [ ] **Step 4: In overlay's `init(args)`, wire Parcely button click**

After the HTML injection, add the button handler (verbatim from base):

```js
const parcelsBtn = document.getElementById('parcelsBtn');
if (parcelsBtn) {
  parcelsBtn.addEventListener('click', async () => {
    parcelsBtn.disabled = true;
    parcelsBtn.textContent = 'Parcely — načítám…';
    try {
      const g = await ensureParcels();
      g.visible = !g.visible;
      parcelsBtn.classList.toggle('active', g.visible);
      parcelsBtn.textContent = g.visible
        ? 'Parcely (RÚIAN) — zap'
        : 'Parcely (RÚIAN) — vyp';
    } catch (e) {
      console.error('parcels', e);
      parcelsBtn.textContent = 'Parcely — chyba: ' + e.message;
    } finally {
      parcelsBtn.disabled = false;
    }
  });
}
```

- [ ] **Step 5: In overlay's `init(args)`, wire parcel hover (mousemove on canvas)**

```js
const mouse = new THREE.Vector2();
const raycaster = new THREE.Raycaster();
renderer.domElement.addEventListener('mousemove', (e) => {
  if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) {
    if (_parcelHover) {
      scene.remove(_parcelHover);
      _parcelHover.geometry.dispose();
      _parcelHover.material.dispose();
      _parcelHover = null;
    }
    return;
  }
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const phits = raycaster.intersectObjects(_parcelMeshes, false);
  if (!phits.length) {
    if (_parcelHover) {
      scene.remove(_parcelHover);
      _parcelHover.geometry.dispose();
      _parcelHover.material.dispose();
      _parcelHover = null;
    }
    return;
  }
  const ring = phits[0].object.userData.parcel.ring_local;
  if (_parcelHover) {
    scene.remove(_parcelHover);
    _parcelHover.geometry.dispose();
    _parcelHover.material.dispose();
  }
  const pts = ring.map(([x, z, y]) =>
    new THREE.Vector3(x, y + PARCEL_LIFT + PARCEL_TILE_H + 0.01, z));
  pts.push(pts[0].clone());
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({ color: 0xfde047, transparent: true, opacity: 0.9 });
  _parcelHover = new THREE.Line(geo, mat);
  _parcelHover.renderOrder = 2;
  scene.add(_parcelHover);
});
```

- [ ] **Step 6: In overlay's `init(args)`, wire parcel left-click popup**

The existing parcel-click popup (showing parcel info on left click on a visible parcel tile) lives in base's left-click handler. Move just the parcel-click branch to overlay:

```js
renderer.domElement.addEventListener('click', (e) => {
  if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) return;
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const phits = raycaster.intersectObjects(_parcelMeshes, false);
  if (!phits.length) return;
  const p = phits[0].object.userData.parcel;
  const popup = getBuildingPopup();
  if (!popup) return;
  popup.style.display = 'block';
  popup.style.left = Math.min(e.clientX, innerWidth - 280) + 'px';
  popup.style.top = Math.min(e.clientY, innerHeight - 200) + 'px';
  popup.innerHTML = `
    <span class="close" id="popup-close">&times;</span>
    <h3>Parcela ${p.label}</h3>
    <div class="row"><span class="label">Druh:</span> ${p.use_label}</div>
    <div class="row"><span class="label">Výměra:</span> ${p.area_m2} m²</div>
    <div class="row"><span class="label">RÚIAN ID:</span> ${p.id}</div>
    <a href="https://nahlizenidokn.cuzk.cz/VyberParcelu/Parcela/InformaceO?id=${p.id}" target="_blank">Nahlížení do KN</a>
  `;
  document.getElementById('popup-close').addEventListener('click', () => {
    popup.style.display = 'none';
  });
  e.stopPropagation();   // don't trigger base's building-popup
});
```

⚠️ The `e.stopPropagation()` is important — without it, after this handler fires, base's building-popup handler would also fire and OVERWRITE popup with building info. Test order: overlay's listener is added LATER than base's, so it fires later — but stopPropagation only works for bubbling. Use `{ capture: true }` if needed:

```js
renderer.domElement.addEventListener('click', (e) => { ... }, { capture: true });
```

- [ ] **Step 7: REMOVE Parcels code from base**

In `hnojice_multi.html`, delete:
- Lines ~243-262: PARCEL_COLORS / PARCEL_FALLBACK / PARCEL_TILE_H / PARCEL_LIFT / `_parcels` / `_parcelGroup` / `_parcelMeshes` / `_parcelHover`
- The `buildParcelGroup` function definition (around line 297)
- The `ensureParcelsData` function (around line 1724)
- The `ensureParcels` function
- The HTML for `<button id="parcelsBtn">` in `#info`
- The CSS for `#parcelsBtn.*`
- The parcelsBtn click handler
- The mousemove handler
- The parcel branch in the left-click handler — keep the building-popup branch in base, remove the parcel-popup branch

⚠️ The drone video subsystem still references `_parcels`, `_parcelGroup`, `_parcelMeshes` (e.g., `_sceneOverlay.apply` calls `applyPresentationMode` which iterates `_parcelGroup.children`). Those references will break temporarily after this task — they'll be FIXED in Task 5 when the drone video subsystem moves to overlay too. **Until Task 5 completes, the drone video panel will not work.**

Two options:
(a) Skip Tasks 3 and 4 — go straight to Task 5 (huge single move). Risky.
(b) Accept that drone video is broken between Tasks 3 and 5. Less risky overall — page still loads, basemap/wireframe/cadastre/building popup all work, and Parcely button works through overlay.

We pick (b). Note in the commit message that drone video is temporarily broken.

- [ ] **Step 8: Verify in browser**

Reload viewer. Test:
- ✅ Terrain renders, ortofoto applied
- ✅ Wireframe / basemap / cadastre toggles work
- ✅ Building left-click → popup shows č.p. / Mapy.cz / RÚIAN
- ✅ "Parcely (RÚIAN) — vyp" button visible in #info panel (injected by overlay)
- ✅ Click Parcely → loads + colored tiles render
- ✅ Hover parcel → yellow outline
- ✅ Left-click parcel → parcel popup shows label/area/RÚIAN link
- ⚠️ Right-click parcel → does NOT open video panel (drone video subsystem removed; will be back in Task 5)
- ⚠️ Building popup 🎬 link → NOT present (will be back in Task 5)
- Console: `[realtor-overlay] init OK` log

- [ ] **Step 9: Commit**

```bash
git add hnojice_multi.html viewer-realtor-overlay.js
git commit -m "refactor(viewer): migrate parcels layer to realtor overlay

$(cat <<'EOF'
First feature migration. Parcels layer (state, fetch helpers,
buildParcelGroup, button injection, click + hover handlers) moves
from hnojice_multi.html to viewer-realtor-overlay.js. Base viewer
no longer has any parcel-related code.

Drone video subsystem and single-parcel highlight are NOT migrated
yet — they remain in base for now. Drone video is temporarily broken
because it referenced _parcels / _parcelGroup which moved away;
reaching Task 5 of this plan restores it.

Single-parcel highlight (left-click on terrain → painted-on-mesh)
also still works because that code is in base too — Task 4 moves it.
EOF
)"
```

---

### Task 4: Migrate Single-parcel highlight feature

**Files:**
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`
- Modify: `/Users/jan/projekty/inzerator/viewer-realtor-overlay.js`

Move single-parcel click-to-highlight (painted-on-mesh outline + nearest-parcel fallback fetch) from base to overlay.

- [ ] **Step 1: In overlay, add module-scope state**

Add near the existing `_parcels` state at module top:

```js
let _selectedParcelMesh = null;   // THREE.Group with painted outline
let _selectClickSeq = 0;          // monotonic counter for in-flight debounce
```

- [ ] **Step 2: In overlay's `init(args)`, add functions**

Add these THREE functions VERBATIM from current `hnojice_multi.html` (around lines 1672-1820 — `buildSelectedParcelMesh`, `clearSelectedParcel`, `selectParcelAtClick`):

```js
function buildSelectedParcelMesh(parcel) {
  // ... full body as currently in base, ~120 lines ...
  // (uses THREE, allMeshes, gcx, gcy via closure from args)
}
function clearSelectedParcel() {
  // ... full body ...
}
async function selectParcelAtClick(localX, localZ) {
  // ... full body ...
}
```

- [ ] **Step 3: In overlay's `init(args)`, wire the left-click handler**

The base's left-click handler currently calls `selectParcelAtClick(point.x, point.z)` after a successful raycast against `allMeshes`. Move that branch to overlay. Add to the existing parcel-click handler from Task 3 step 6, OR add as a separate listener:

```js
renderer.domElement.addEventListener('click', (e) => {
  // ... if a parcel-tile click was already handled (from Task 3 step 6),
  // that handler called e.stopPropagation() and we won't reach here.
  // Otherwise raycast against terrain and trigger single-parcel selection.
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(allMeshes);
  if (hits.length > 0) {
    const point = hits[0].point;
    selectParcelAtClick(point.x, point.z);
  }
});
```

- [ ] **Step 4: REMOVE single-parcel code from base**

In `hnojice_multi.html`, delete:
- The state vars `_selectedParcelMesh`, `_selectClickSeq`
- `buildSelectedParcelMesh`, `clearSelectedParcel`, `selectParcelAtClick` function definitions
- The `selectParcelAtClick(point.x, point.z)` call in the base's left-click handler

- [ ] **Step 5: Verify in browser**

Reload. Test:
- ✅ All previous features still work (Parcely button, building popup)
- ✅ Left-click on terrain (NOT on a building, NOT on a parcel tile) → painted yellow outline appears on the parcel under the click
- ✅ Click again elsewhere → previous outline disappears, new one appears
- ✅ Click on road / outside any parcel → nearest parcel highlighted (via server's envelope fallback)

- [ ] **Step 6: Commit**

```bash
git add hnojice_multi.html viewer-realtor-overlay.js
git commit -m "refactor(viewer): migrate single-parcel click highlight to overlay

$(cat <<'EOF'
Moves selectParcelAtClick + clearSelectedParcel + buildSelectedParcelMesh
(painted-on-mesh outline using cadastre-style projection technique)
from hnojice_multi.html to viewer-realtor-overlay.js. Plus the in-flight
debounce token (_selectClickSeq) and the left-click hook that triggers
the fetch.

Base no longer has any parcel-related code. Drone video subsystem
still in base, still broken; Task 5 restores it.
EOF
)"
```

---

### Task 5: Migrate Drone video subsystem

**Files:**
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`
- Modify: `/Users/jan/projekty/inzerator/viewer-realtor-overlay.js`

The big one. Moves all drone-video infrastructure: presets, highlights, sunset, panel UI, recording, MP4 conversion, free flythrough, building popup video link injection. ~1000 lines.

This task is intentionally one large commit because the components are tightly coupled (`_sceneOverlay` orchestrates 6 functions; click handlers reference shared state). Splitting risks broken inter-references between commits.

- [ ] **Step 1: In overlay, add module-scope state**

Add to the overlay's module-top state block:

```js
let _videoSubject = null;
let _videoState = 'idle';     // 'idle' | 'panel' | 'preview' | 'recording' | 'paused' | 'converting'
let _videoStartTs = 0;
let _videoDurationMs = 25000;
let _videoOverlay = false;
let _videoMode = 'property';
let _videoHighlights = { pulse: false, beam: false, label: false, ants: false, glow: false, pin: false };
let _videoStartAngleDeg = 0;
let _videoStartDistanceMul = 1.0;
let _hlPulseObjs = [];
let _hlBeam = null, _hlLabel = null, _hlAnts = null, _hlGlow = null, _hlPin = null;
let _hlAntsOffset = 0;
let _videoPreset = 'topdown';
let _sunsetTintActive = false;
let _sunsetTintRestore = [];
let _videoCurves = null;
let _videoPauseElapsed = null;
let _videoCancelled = false;
let _currentRecorder = null;
let _ffmpegInstance = null;
let _ffmpegLoading = null;
let _videoPrevMode = null;
const _savedSceneState = new Map();
const SUBJECT_YELLOW = 0xfde047;
const SUBJECT_CYAN   = 0x67e8f9;
let _onRecordingFrameComplete = null;

let _hudCanvas = null, _hudTexture = null, _hudMesh = null,
    _hudScene = null, _hudCam = null;
```

These are exact copies of state currently in `hnojice_multi.html` lines 266-292.

- [ ] **Step 2: In overlay's `init(args)`, add ALL drone video functions**

Add these functions VERBATIM from `hnojice_multi.html`, INSIDE `init(args)` so they close over `THREE`, `scene`, `camera`, `renderer`, `controls`, `allMeshes`, `ruianBuildings`, `gcx`, `gcy`, `setMainTick`, `resetMainTick`, `getTerrainHeightAt`:

Pure helpers (around lines 297-560 in current base):
- `computeSubjectGeometry(ring_local)`
- `findNearestRoadPoint(centroid, fallbackY)`
- `buildCameraPath(preset, subject)`
- `applyStartTransform(curves, subject)`
- `pointInPolygon2D(px, pz, polygon)`
- `findSubjectBuildingIdx(ring_local)`

Material state + presentation (lines 563-660):
- `saveMaterialState(obj)`
- `restoreMaterialState(obj)`
- `applyPresentationMode(subject, mode)`
- `restorePresentationMode()`

Highlights (lines 668-870):
- `applyHighlights(subject)`
- `restoreHighlights()`

Sunset (lines 893-940):
- `applySunsetTint()`
- `restoreSunsetTint()`

`_sceneOverlay` object (lines 942-990):
- The `_sceneOverlay = { active, subject, mode, highlights, sunset, apply(), clear(), setSunset(), tick() }`

Tick + recording (lines 993-1320):
- `tickHighlights()`
- `smoothstep(t)`
- `videoTick()`
- `startVideoTick(curves, durationMs)`
- `stopVideoTick()`
- `let _onVideoComplete = function()` (initial declaration with `let`)
- HUD overlay: `initHud()`, `drawHudText(label, area, useLabel)`, `blitOverlay()`
- `pauseVideoTick()`, `resumeVideoTick()`, `cancelVideoTick()`
- `pickWebmMime()`
- `ensureFfmpeg()`, `transcodeWebmToMp4(webmBlob, onProgress)`
- `resetRecordingUI()`
- `_onVideoComplete = function()` (reassignment from Preview wiring)

Open/close panel (lines 1130-1240):
- `openVideoPanel(parcel)`
- `openVideoPanelFreeMode()`
- `closeVideoPanel()`

⚠️ Inside `videoTick`, the line `if (_videoOverlay) blitOverlay();` and the `if (tRaw >= 1) _onVideoComplete();` references are kept intact — they're internal to the overlay.

⚠️ `_sceneOverlay.tick` should be registered with `addTickHook` here. Add right after the function definitions:

```js
addTickHook(() => _sceneOverlay.tick());
```

(This replaces the current registration in base which we added in Task 1 — base will lose it in Step 3 below.)

- [ ] **Step 3: In overlay's `init(args)`, inject HTML/CSS**

Add the panel HTML + free-record button + hint to `#info`, and the CSS:

```js
// CSS
document.head.insertAdjacentHTML('beforeend', `
  <style>
    /* Free record button + hint */
    #freeRecordBtn:hover { background: #e8e8e8; }
    #freeRecordBtn.active { background: #1a73e8; color: white; border-color: #1456b8; }
    #freeRecordBtn.active:hover { background: #1456b8; }
    /* Video panel */
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
    #video-panel .vp-progress-row {
      display: flex; gap: 6px; align-items: center; margin-top: 10px;
    }
    #video-panel .vp-progress-bar-wrap {
      flex: 1; height: 6px; background: #eee; border-radius: 3px; overflow: hidden;
    }
    #video-panel #vp-progress-bar { height: 100%; background: #1a73e8; width: 0%; transition: width 0.1s linear; }
    #video-panel .vp-ctrl-btn {
      font-size: 11px; padding: 3px 6px; border-radius: 3px; border: 1px solid #ccc;
      background: #f6f6f6; cursor: pointer;
    }
    #video-panel .vp-ctrl-btn:hover { background: #e8e8e8; }
    #video-panel .vp-ctrl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    #video-panel .vp-ctrl-cancel { color: #c0392b; border-color: #e8b4ad; }
    #video-panel .vp-ctrl-cancel:hover { background: #fadbd8; }
  </style>
`);

// Hint + free record button into #info (after the parcels button from Task 3)
const info = document.getElementById('info');
if (info) {
  info.insertAdjacentHTML('beforeend', `
    <p style="font-size:11px;color:#888;margin:6px 0 4px;line-height:1.3">
      🎬 <b>Video režim:</b> pravým klikem na parcelu otevři panel pro export. Nebo:
    </p>
    <button id="freeRecordBtn" style="width:100%;padding:6px;border-radius:4px;border:1px solid #ccc;background:#f6f6f6;cursor:pointer;font-size:12px">
      📹 Nahrát aktuální pohled
    </button>
  `);
}

// Video panel into body
document.body.insertAdjacentHTML('beforeend', `
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
      <label><input type="radio" name="vp-preset" value="topdown" checked> 📐 Top-down zoom (15s)</label>
      <label><input type="radio" name="vp-preset" value="highorbit360"> 🛰️ High orbit 360° (30s)</label>
      <label><input type="radio" name="vp-preset" value="halforbit"> 🔄 Half-orbit (15s)</label>
      <label><input type="radio" name="vp-preset" value="revealpullup"> 📡 Reveal pull-up (25s)</label>
      <label><input type="radio" name="vp-preset" value="divepush"> 🎯 Diagonal push-in (20s)</label>
      <label><input type="radio" name="vp-preset" value="locator"> 🌍 Locator zoom (20s)</label>
      <label><input type="radio" name="vp-preset" value="contextarc"> 🌀 Context arc (25s)</label>
      <label><input type="radio" name="vp-preset" value="lateralflyby"> ✈️ Lateral fly-by (15s)</label>
      <label><input type="radio" name="vp-preset" value="sunsetorbit"> 🌅 Sunset orbit (15s)</label>
    </fieldset>
    <fieldset class="vp-fieldset">
      <legend>Mód</legend>
      <label><input type="radio" name="vp-mode" value="property" checked> Property (parcel + budova)</label>
      <label><input type="radio" name="vp-mode" value="land"> Land only</label>
    </fieldset>
    <fieldset class="vp-fieldset">
      <legend>Zvýraznění</legend>
      <label><input type="checkbox" name="vp-hl" value="pulse"> ✨ Pulsing outline</label>
      <label><input type="checkbox" name="vp-hl" value="beam"> 🔦 Beam z parcely</label>
      <label><input type="checkbox" name="vp-hl" value="label"> 🏷️ Floating label</label>
      <label><input type="checkbox" name="vp-hl" value="ants"> 🐜 Marching ants</label>
      <label><input type="checkbox" name="vp-hl" value="glow"> 💡 Volumetric glow</label>
      <label><input type="checkbox" name="vp-hl" value="pin"> 📍 Pin</label>
    </fieldset>
    <label class="vp-row"><input type="checkbox" id="vp-overlay"> Info overlay</label>
    <label class="vp-row">Délka <input type="range" id="vp-duration" min="15" max="45" step="1" value="25"> <span id="vp-duration-val">25</span>s</label>
    <fieldset class="vp-fieldset">
      <legend>Začátek dráhy</legend>
      <label class="vp-row">Úhel <input type="range" id="vp-start-angle" min="-180" max="180" step="5" value="0"> <span id="vp-start-angle-val">0</span>°</label>
      <label class="vp-row">Vzdálenost <input type="range" id="vp-start-dist" min="0.5" max="2.5" step="0.05" value="1.0"> <span id="vp-start-dist-val">1.0</span>×</label>
    </fieldset>
    <div class="vp-actions">
      <button id="vp-preview">Preview</button>
      <button id="vp-export">Export</button>
    </div>
    <div id="vp-progress" class="vp-progress-row" style="display:none">
      <div class="vp-progress-bar-wrap"><div id="vp-progress-bar"></div></div>
      <button id="vp-pause" class="vp-ctrl-btn">⏸ Pauza</button>
      <button id="vp-cancel" class="vp-ctrl-btn vp-ctrl-cancel">✕ Zrušit</button>
    </div>
  </div>
`);
```

- [ ] **Step 4: In overlay's `init(args)`, wire all panel handlers**

After HTML injection, add ALL panel handlers verbatim from base (around lines 1163-1480):
- `vp-close` close button + Escape key
- `vp-preset` radio change (with default duration map)
- `vp-mode` radio change
- `vp-overlay` checkbox change
- `vp-duration` slider input
- `vp-start-angle` + `vp-start-dist` slider input
- `vp-hl[]` checkbox change
- `vp-pause` click
- `vp-cancel` click
- `vp-preview` click
- `vp-export` click

Plus contextmenu on `renderer.domElement` for right-click → openVideoPanel.

Plus `freeRecordBtn` click handler:
```js
const freeBtn = document.getElementById('freeRecordBtn');
if (freeBtn) {
  freeBtn.addEventListener('click', () => {
    if (_videoState !== 'idle' && _videoState !== 'panel') return;
    if (_videoState === 'panel') closeVideoPanel();
    openVideoPanelFreeMode();
  });
}
```

- [ ] **Step 5: In overlay's `init(args)`, inject the 🎬 video link into the building popup**

The base's building-popup left-click handler builds `popup.innerHTML = `...` with č.p./Mapy.cz/RÚIAN. The overlay needs to add a 🎬 Vytvořit video link AFTER the popup is shown.

Approach: monkey-patch the popup show flow. Add a MutationObserver on the popup element OR override the popup-build:

Simpler: add a click handler on the canvas with `{ capture: false }` (default) so it fires AFTER base's. The base's handler builds the popup; overlay's handler runs next, finds the popup, and appends the link.

But the overlay's handler needs to know which building was clicked. Race-free pattern: in overlay's click handler, do the same raycast against `allMeshes`, find the same building (same point-in-polygon logic against `ruianBuildings`), then patch the popup.

Cleaner: overlay just observes the popup. When popup becomes visible, append the video link based on data already rendered:

```js
const popupEl = getBuildingPopup();
if (popupEl) {
  const observer = new MutationObserver(() => {
    if (popupEl.style.display !== 'block') return;
    if (popupEl.querySelector('#popup-video')) return;          // already added
    if (!popupEl.querySelector('#popup-kod')) return;            // not a building popup (parcel popup has no popup-kod)
    // Insert video link
    popupEl.insertAdjacentHTML('beforeend',
      `<a href="#" id="popup-video" style="color:#c0392b;font-weight:600">🎬 Vytvořit video</a>`);
    const link = document.getElementById('popup-video');
    link.addEventListener('click', async (ev) => {
      if (_videoState !== 'idle' && _videoState !== 'panel') {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      const kod = popupEl.querySelector('#popup-kod')?.textContent;
      if (!kod) return;
      // Find the building by kod, then look up its parcel.
      const b = ruianBuildings.find(rb => String(rb.kod) === kod);
      if (!b) return;
      link.textContent = '🎬 Načítám parcely…';
      try {
        await ensureParcelsData();
        let cx = 0, cz = 0;
        if (Array.isArray(b.coords) && b.coords.length >= 3) {
          for (const [x, z] of b.coords) { cx += x; cz += z; }
          cx /= b.coords.length; cz /= b.coords.length;
        }
        let containingParcel = null;
        for (const p of _parcels) {
          if (!p.ring_local || p.ring_local.length < 3) continue;
          if (pointInPolygon2D(cx, cz, p.ring_local)) {
            containingParcel = p;
            break;
          }
        }
        popupEl.style.display = 'none';
        if (containingParcel) openVideoPanel(containingParcel);
        else openVideoPanelFreeMode();
      } catch (err) {
        console.error('video link', err);
        link.textContent = '🎬 Chyba: ' + err.message;
      }
    });
  });
  observer.observe(popupEl, { childList: true, attributes: true, attributeFilter: ['style'] });
}
```

⚠️ This depends on the base's popup template still containing `<span id="popup-kod">${b.kod}</span>` — which it does (currently in base around line 916). Don't change base's popup template.

- [ ] **Step 6: REMOVE drone video code from base**

In `hnojice_multi.html`, delete:
- All state vars from Step 1 list (`_videoSubject`, `_videoState`, etc.)
- All functions from Step 2 list
- The `_sceneOverlay` object
- All HTML for `#video-panel`
- All CSS for `#video-panel`, `vp-*` rules, `#freeRecordBtn`
- The hint paragraph about Video režim
- The Free record button HTML
- All vp-* handlers
- The contextmenu listener on renderer.domElement
- The `selectParcelAtClick(point.x, point.z)` call from base's left-click handler (already moved in Task 4 — verify it's gone)
- The 🎬 popup-video link from base's building popup template + its click handler
- The `addTickHook(() => _sceneOverlay.tick())` line (overlay registers its own now)

Keep in base:
- The building-popup left-click handler with č.p./Mapy.cz/RÚIAN links (no video link)
- Cadastre overlay
- Basemap selector
- All non-realtor UI

- [ ] **Step 7: Verify in browser**

Reload. Test the FULL realtor flow:
- ✅ All previous features (basemap, building popup, parcels, hover, single-parcel highlight)
- ✅ Right-click parcel → video panel opens
- ✅ Free record button → video panel opens (free mode)
- ✅ Building popup contains 🎬 Vytvořit video link
- ✅ Click 🎬 → video panel opens with parcel containing the building
- ✅ All 9 video presets selectable
- ✅ All 6 highlight checkboxes work (visual change in scene)
- ✅ Sunset preset applies warm tint during preview
- ✅ Preview animates camera; Pause/Resume/Cancel work
- ✅ Export → record → MP4 download

- [ ] **Step 8: Commit**

```bash
git add hnojice_multi.html viewer-realtor-overlay.js
git commit -m "refactor(viewer): migrate drone video subsystem to realtor overlay

$(cat <<'EOF'
Final and largest migration. Moves the entire drone-video stack
from hnojice_multi.html to viewer-realtor-overlay.js:

- All _video*/_hl*/_sunset* state and the _sceneOverlay
  abstraction (presentation mode + highlights + sunset + tick).
- All 9 cinematography preset path builders, applyStartTransform,
  pointInPolygon2D, findSubjectBuildingIdx, computeSubjectGeometry,
  findNearestRoadPoint.
- All 6 highlight builders + tick (pulse / beam / label / ants /
  glow / pin).
- HUD overlay (initHud / drawHudText / blitOverlay).
- videoTick + Pause/Resume/Cancel + start/stop/setMainTick wiring.
- ensureFfmpeg + transcodeWebmToMp4 + MP4 download flow.
- resetRecordingUI helper.
- Full video panel HTML + CSS + 12+ event handlers.
- Free flythrough button.
- Building popup 🎬 Vytvořit video link injection (via MutationObserver
  on popup element — overlay observes base's popup, appends link).
- contextmenu right-click → openVideoPanel.

Base hnojice_multi.html no longer has any realtor-specific code.
The full realtor stack lives in viewer-realtor-overlay.js loaded
via try-import. Disabling the overlay (rename to .disabled.js)
gracefully degrades to a plain village viewer.
EOF
)"
```

---

### Task 6: Update `gen_multitile.py` template

**Files:**
- Modify: `/Users/jan/projekty/inzerator/gen_multitile.py`

Mirrors Tasks 1-5 in the template so future regenerations of any location produce a viewer that uses the overlay.

- [ ] **Step 1: Find the template literal**

In `gen_multitile.py`, find the `TEMPLATE_HTML = """..."""` (or however it's formatted). It's the multi-line f-string that produces the per-location HTML.

- [ ] **Step 2: Add tick-hook API to template**

Find the section in the template that contains:
```python
function animate() {{ ... }}
animate();
```
or whatever the current tick-loop structure is in the template.

Replace with the new structure from Task 1:
```python
function interactiveTick() {{
  controls.update();
  renderer.render(scene, camera);
}}
let _mainTick = interactiveTick;
const _tickHooks = [];
function tick() {{
  _mainTick();
  for (const hook of _tickHooks) hook();
  requestAnimationFrame(tick);
}}
tick();

const addTickHook    = fn => {{ _tickHooks.push(fn); }};
const removeTickHook = fn => {{ const i = _tickHooks.indexOf(fn); if (i >= 0) _tickHooks.splice(i, 1); }};
const setMainTick    = fn => {{ _mainTick = fn; }};
const resetMainTick  = ()  => {{ _mainTick = interactiveTick; }};
```

- [ ] **Step 3: Add `getTerrainHeightAt` helper to template**

If template doesn't have it (the current Hnojice viewer might have it added manually), add to the template:

```python
function getTerrainHeightAt(x, z) {{
  const r = new THREE.Raycaster();
  r.set(new THREE.Vector3(x, 5000, z), new THREE.Vector3(0, -1, 0));
  const hits = r.intersectObjects(allMeshes, false);
  return hits.length ? hits[0].point.y : 0;
}}
```

- [ ] **Step 4: Add try-import block at end of template**

Right before the `tick();` call (which should be the end of the template's module script), insert the try-import block:

```python
// Realtor overlay try-import.
try {{
  const overlay = await import('./viewer-realtor-overlay.js');
  overlay.init({{
    THREE, scene, camera, renderer, controls,
    allMeshes, ruianBuildings, gcx, gcy,
    addTickHook, removeTickHook, setMainTick, resetMainTick,
    getBuildingPopup: () => document.getElementById('building-popup'),
    getTerrainHeightAt,
  }});
}} catch (e) {{
  console.warn('[viewer] realtor overlay not loaded:', e);
}}
```

- [ ] **Step 5: REMOVE all realtor code from template**

Search the template for any of: `_video`, `_hl`, `_parcel`, `_selected`, `_sunset`, `_sceneOverlay`, `applyPresentation`, `applyHighlights`, `selectParcelAtClick`, `buildSelectedParcelMesh`, `buildCameraPath`, `videoTick`, `openVideoPanel`, `vp-`, `freeRecordBtn`, `parcelsBtn`, `popup-video`. Delete all matched blocks.

If the template already has any of these from when Hnojice was originally generated (commit `7ebf9fa`), the template needs deep cleanup — drone-video tool wasn't in the template before; it was all hand-added to `hnojice_multi.html`. So template only needs to lose the parcels button + parcel state + `buildParcelGroup` if those were in template. Use grep to verify what's actually there:

```bash
grep -E '_video|_hl|_parcel|_selected|_sunset|_sceneOverlay|applyPresentation|applyHighlights|selectParcelAtClick|buildSelectedParcelMesh|buildCameraPath|videoTick|openVideoPanel|vp-|freeRecordBtn|parcelsBtn|popup-video' gen_multitile.py | head -30
```

Each match should be removed.

- [ ] **Step 6: Smoke test — Python syntax**

```bash
python3 -c "import gen_multitile; print('OK')"
```
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add gen_multitile.py
git commit -m "refactor(gen): template emits viewer + try-import for realtor overlay

$(cat <<'EOF'
Mirrors the hnojice_multi.html refactor in gen_multitile.py
template so regenerations produce a viewer with:
- Tick-hook API (addTickHook / setMainTick / resetMainTick).
- getTerrainHeightAt helper.
- try-import viewer-realtor-overlay.js at the end of the module
  script with the full args contract.

Removed from template: any drone-video / parcels / single-parcel
realtor code that had crept in (none expected — those were all
hand-added post-generation — but verified via grep).

Future regenerations of any location now preserve realtor features
because the overlay file is shared. This is the value proposition of
the entire refactor.
EOF
)"
```

---

### Task 7: Regenerate Hnojice + final verification

**Files:** none (verification + commit only)

- [ ] **Step 1: Regenerate Hnojice viewer**

```bash
cd /Users/jan/projekty/inzerator
python3 gen_multitile.py --location hnojice --output hnojice_multi.html --glb
```

Expected: writes new `hnojice_multi.html` overwriting the current one + updates `tiles_hnojice_multi/hnojice_data.json`.

- [ ] **Step 2: Diff vs Task 5 output**

```bash
git diff hnojice_multi.html | head -100
```

The diff should be MINIMAL — at most cosmetic differences from the template emit. Major drift (e.g., realtor code reappearing) means Task 6 didn't fully clean the template.

If the diff is large or realtor code is back, fix the template (Task 6 step 5) and regenerate.

- [ ] **Step 3: Browser test — full flow**

Reload viewer. Test the spec's full test plan:
1. Terrain + ortho render
2. Wireframe / basemap / cadastre toggles
3. Building left-click popup with č.p. / Mapy.cz / RÚIAN
4. Building popup contains 🎬 Vytvořit video link (overlay-injected)
5. Parcely button → loads + colored tiles render
6. Hover parcel → yellow outline
7. Left-click parcel → parcel popup
8. Right-click parcel → video panel
9. Free record button → video panel (free mode)
10. All 9 presets selectable
11. All 6 highlights toggleable (visual change)
12. Sunset preset applies warm tint
13. Preview animates; Pause/Resume/Cancel
14. Export → record → MP4 download

- [ ] **Step 4: Negative test — disable overlay**

```bash
mv viewer-realtor-overlay.js viewer-realtor-overlay.disabled.js
```

Reload viewer. Verify:
- Page loads without errors (apart from `console.warn` about overlay not loaded)
- Terrain + ortho render
- Basemap / wireframe / cadastre work
- Building popup works (without 🎬 link)
- No Parcely button, no Free record button, no video panel — these were overlay-injected
- No left-click parcel highlight

Restore:
```bash
mv viewer-realtor-overlay.disabled.js viewer-realtor-overlay.js
```

- [ ] **Step 5: Commit (regenerated viewer if any drift)**

If `git status` shows changes to `hnojice_multi.html` (from regeneration), commit them:

```bash
git add hnojice_multi.html tiles_hnojice_multi/hnojice_data.json
git commit -m "chore(hnojice): regenerate viewer from updated template

$(cat <<'EOF'
Validates the Phase-2 refactor: gen_multitile.py emit produces a
viewer that loads viewer-realtor-overlay.js and gets full realtor
features back. Template-vs-hand-edited drift is now zero.

Note: tiles_hnojice_multi/hnojice_data.json regenerated alongside
the HTML — should be a no-op if RÚIAN data hasn't changed since
last gen.
EOF
)"
```

If no drift, no commit needed — the previous Task 5+6 commits already
captured the final state.

- [ ] **Step 6: Push everything**

```bash
git push origin main
```

---

## Self-review notes

- **Spec coverage:**
  - Mid-extract scope (parcels, single-parcel, drone video stay; rest in base) — Tasks 3, 4, 5 ✓
  - ES module + `init({...})` contract — Task 2 ✓
  - All-inline HTML/CSS in overlay — Task 5 step 3 ✓
  - Tick-hook API (`addTickHook`, `setMainTick`, `resetMainTick`) — Task 1 ✓
  - `getBuildingPopup` + `getTerrainHeightAt` passed through — Task 2 ✓
  - Building popup video link injection — Task 5 step 5 ✓
  - Try-import gracefully fails — Task 2 step 3 + Task 7 step 4 ✓
  - gen_multitile.py template update — Task 6 ✓
  - Regen-doesn't-break test — Task 7 ✓

- **Placeholder scan:** Two intentional placeholders flagged inline — Task 5 Step 2 says "verbatim from current base" with line refs. Implementer reads the source file directly. Acceptable because reproducing 1000+ lines of code in this plan would be wasteful and error-prone (any drift between plan and source would create a NEW bug). Plan tells implementer EXACTLY what to copy and where to put it.

- **Type consistency:**
  - `addTickHook` / `removeTickHook` / `setMainTick` / `resetMainTick` — same names in Task 1, Task 2, Task 5, Task 6 ✓
  - `_sceneOverlay` (with underscore) — referenced in Task 1 step 3 + Task 5 step 2 ✓
  - `getBuildingPopup` — Task 2 step 3 + Task 5 step 5 ✓
  - `getTerrainHeightAt` — Task 2 step 2 + Task 6 step 3 ✓

- **Edge cases handled:**
  - Overlay missing → try-catch → graceful degrade (Task 2)
  - Overlay init throws → same try-catch (Task 2)
  - Two click handlers on same canvas (base building-popup + overlay parcel-popup) → `e.stopPropagation` + `{ capture: true }` (Task 3 step 6)
  - Drone video temporarily broken between Tasks 3-5 → flagged in Task 3 commit message (Task 3 step 9)
  - Pre-Task-6 vs post-Task-6 viewer compatibility → Task 7 regen test catches drift

- **Risks called out in the plan body:**
  - Closure capture ordering (Task 5 step 2 says "INSIDE init(args)")
  - Template grep for leftover realtor code (Task 6 step 5)
  - MutationObserver for popup-video (Task 5 step 5) — alternative was "register own click handler" which would race with base's; observer is cleanest

- **Estimated total:** 7 tasks. Per spec ~3-4 hours. Tasks 1+2 are setup/skeleton (~30 min total). Tasks 3+4 are small migrations (~30 min each). Task 5 is the big one (~1.5-2 hours). Tasks 6+7 are template + verification (~30-60 min total).
