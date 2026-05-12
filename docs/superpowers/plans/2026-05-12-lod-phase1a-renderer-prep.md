# LOD Phase 1A — Renderer prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable logarithmic depth buffer + far clip 15000 + exponential fog in the base viewer and `gen_multitile.py` template. No pipeline / no tile changes. Validates the three biggest renderer-side risks of the LOD spec.

**Architecture:** Three small synchronous edits in `hnojice_multi.html` (base scene setup) + matching edits in `gen_multitile.py` template (so future regen is consistent). After this lands, the existing tiles look identical except: (1) fog softens the horizon, (2) far horizon visible without z-fighting, (3) cadastre overlay + parcel highlights MUST still render correctly (this is the validation).

**Tech Stack:** THREE.js r170 — `WebGLRenderer({ logarithmicDepthBuffer: true })`, `PerspectiveCamera(fov, aspect, near=1.0, far=15000)`, `FogExp2(color, density)`.

**Spec:** `docs/superpowers/specs/2026-05-12-lod-terrain-rings.md`

**Test approach:** Browser smoke test via chrome-devtools MCP after each change. No automated frontend tests (project convention).

---

## File Structure

- **Modify:** `/Users/jan/projekty/inzerator/hnojice_multi.html` (renderer + camera + scene setup, lines ~141–146)
- **Modify:** `/Users/jan/projekty/inzerator/gen_multitile.py` (matching lines in the f-string template ~777–782)

---

## Pre-task setup

Confirm server is running and overlay is enabled:

```bash
curl -s -o /dev/null -w "viewer: %{http_code}\n" http://127.0.0.1:8080/hnojice_multi.html
curl -s -o /dev/null -w "overlay: %{http_code}\n" http://127.0.0.1:8080/viewer-realtor-overlay.js
```
Both must return 200.

---

### Task 1: Apply renderer changes to base viewer

**Files:**
- Modify: `/Users/jan/projekty/inzerator/hnojice_multi.html`

Modify the THREE.js scene setup to enable log depth buffer, push far clip to 15000, and add exponential fog. The cadastre + parcels + drone-video subsystems must continue to work — this is the validation target.

- [ ] **Step 1: Locate the renderer + camera + scene setup block**

Around lines 140–150 in `hnojice_multi.html`:

```js
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x87ceeb);

const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.1, 5000);
camera.position.set(200, 150, 200);

const renderer = new THREE.WebGLRenderer({ antialias: true });
```

- [ ] **Step 2: Replace with the new setup**

```js
const scene = new THREE.Scene();
// Haze blue — matched to fog so horizon blends seamlessly into background.
scene.background = new THREE.Color(0xb0c4d8);
// Exponential fog: 50% density at ~5.5km, full fade by ~10km. Masks the
// (future) LOD ring boundaries and gives atmospheric perspective.
scene.fog = new THREE.FogExp2(0xb0c4d8, 0.00012);

// near=1.0 (was 0.1) reclaims depth precision; far=15000 (was 5000) makes
// room for the future L3 panorama ring at ~7.2km. Both required by the
// logarithmicDepthBuffer renderer setting below.
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 1.0, 15000);
camera.position.set(200, 150, 200);

const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
```

⚠️ The order matters less than the *combination*: log depth + far/near values together resolve z-fighting at distance. Don't enable log depth without also pushing the camera planes.

- [ ] **Step 3: Verify static**

```bash
grep -n 'logarithmicDepthBuffer\|FogExp2\|0xb0c4d8\|near=\|far=' /Users/jan/projekty/inzerator/hnojice_multi.html
```
Expected:
- One match for `logarithmicDepthBuffer: true` in WebGLRenderer.
- One match for `new THREE.FogExp2(0xb0c4d8, 0.00012)`.
- One match for `new THREE.Color(0xb0c4d8)` (background).
- Camera signature `(60, innerWidth / innerHeight, 1.0, 15000)`.

```bash
curl -s -o /tmp/p.html -w "%{http_code} %{size_download}\n" http://127.0.0.1:8080/hnojice_multi.html
```
Expected: 200, file size shifts by ~150 bytes.

- [ ] **Step 4: Verify in browser via chrome-devtools MCP**

The controller will run these tests. The implementer needs to **commit the change first**, then ping the controller for verification.

Tests the controller runs:
1. Navigate to viewer, wait for `[realtor-overlay] init OK`.
2. Take screenshot — terrain visible, hazy horizon (background color 0xb0c4d8), no z-fighting on distant tiles.
3. Toggle "Katastrální mapa (overlay)" → cadastre raster overlay visible **without** z-fighting against terrain (polygonOffset must still work with logarithmicDepthBuffer).
4. Click parcels button → parcel tiles visible with colored top faces (no z-fighting against terrain underneath).
5. Click a parcel → painted yellow outline visible on terrain (this exercises renderOrder=101 + polygonOffset).
6. Click freeRecord → open video panel → click Preview → verify no visible rendering glitches during camera animation.
7. Toggle "Sunset orbit" preset → preview → fog color interaction with sunset tint should look reasonable (warm yellow fog OR retain blue — either acceptable for Phase 1A; we can tune in Phase 2 if needed).

Acceptance: NO console errors. Cadastre overlay must NOT z-fight terrain. Parcel highlights must NOT z-fight terrain.

If any of those fail → BLOCKED → spec needs revision (the log depth approach may not be compatible with the current polygonOffset values in cadastre and painted-mesh-outline code).

- [ ] **Step 5: Commit**

```bash
git add hnojice_multi.html
git commit -m "$(cat <<'EOF'
feat(viewer): log depth buffer + far clip 15000 + atmospheric fog

Renderer prep for the LOD terrain rings work (Phase 1A of the LOD
spec). Three coordinated changes:

- WebGLRenderer enables logarithmicDepthBuffer. Eliminates z-fighting
  at the 1km+ range that the future L3 panorama ring (7.2km radius)
  requires.
- Camera near plane lifts from 0.1 to 1.0, reclaiming depth precision
  that's now allocated across the much larger far range.
- Camera far plane lifts from 5000 to 15000, room for the future
  panorama ring without clipping the horizon.
- FogExp2 (density 0.00012, color 0xb0c4d8) provides atmospheric
  perspective and will mask LOD ring boundaries when those land.
  Background color matched so horizon blends seamlessly.

No pipeline / no tile changes. Existing 625 tiles render the same
content, just with deeper view distance and hazy horizon. Validates
that logarithmicDepthBuffer is compatible with cadastre overlay
polygonOffset and painted-mesh parcel highlight renderOrder — the
two highest-risk interactions called out in the LOD spec.
EOF
)"
```

---

### Task 2: Apply matching changes to gen_multitile.py template

**Files:**
- Modify: `/Users/jan/projekty/inzerator/gen_multitile.py`

Mirror Task 1 in the f-string template so future regenerations of any location pick up the same renderer settings. Standard brace-doubling rules apply.

- [ ] **Step 1: Locate the matching template block**

Around lines 777–782 in `gen_multitile.py` (inside the `TEMPLATE_HTML` f-string):

```python
scene.background = new THREE.Color(0x87ceeb);

const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 0.1, 5000);
camera.position.set(200, 150, 200);

const renderer = new THREE.WebGLRenderer({{ antialias: true }});
```

- [ ] **Step 2: Replace with the new setup**

```python
// Haze blue — matched to fog so horizon blends seamlessly into background.
scene.background = new THREE.Color(0xb0c4d8);
// Exponential fog: 50% density at ~5.5km, full fade by ~10km. Masks the
// (future) LOD ring boundaries and gives atmospheric perspective.
scene.fog = new THREE.FogExp2(0xb0c4d8, 0.00012);

// near=1.0 (was 0.1) reclaims depth precision; far=15000 (was 5000) makes
// room for the future L3 panorama ring at ~7.2km. Both required by the
// logarithmicDepthBuffer renderer setting below.
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 1.0, 15000);
camera.position.set(200, 150, 200);

const renderer = new THREE.WebGLRenderer({{ antialias: true, logarithmicDepthBuffer: true }});
```

⚠️ Brace-doubling: only the `WebGLRenderer({...})` and the future `LOD_PROFILES` interpolations need `{{` `}}`. Comments use plain `{` `}` because they're not actually code.

- [ ] **Step 3: Verify the template still parses**

```bash
python3 -c "import gen_multitile; print('OK')"
```
Expected: `OK`. Brace-doubling errors surface here at import time.

```bash
grep -n 'logarithmicDepthBuffer\|FogExp2\|1\.0, 15000' /Users/jan/projekty/inzerator/gen_multitile.py
```
Expected: matches in the template block, mirror of Task 1.

- [ ] **Step 4: Spot-check no drift vs base**

After this task lands, base and template should produce structurally identical renderer setup. The base has unbraced literals; the template has braced. Other than that, the JS should be byte-identical.

```bash
diff <(sed -n '140,150p' /Users/jan/projekty/inzerator/hnojice_multi.html) \
     <(sed -n '777,790p' /Users/jan/projekty/inzerator/gen_multitile.py | sed 's/{{/{/g; s/}}/}/g')
```

This is a fuzzy check — line numbers / indentation may differ. The output should show only minor formatting differences, not semantic ones. A grep for the key tokens (`logarithmicDepthBuffer`, `0xb0c4d8`, `1.0, 15000`, `0.00012`) appearing once in each file is the more reliable check.

- [ ] **Step 5: Commit**

```bash
git add gen_multitile.py
git commit -m "$(cat <<'EOF'
refactor(gen): mirror Phase 1A renderer changes in template

Future regenerations now emit a viewer with logarithmicDepthBuffer,
near=1.0, far=15000, and FogExp2(0xb0c4d8, 0.00012) by default.
Mirrors the hnojice_multi.html change in the previous commit so
that regenerating any location produces a viewer consistent with
the LOD spec's Phase 1A baseline.
EOF
)"
```

---

## Self-review notes

- **Spec coverage:**
  - Log depth buffer ✓ (Task 1 Step 2)
  - Far clip 15000 + near 1.0 ✓ (Task 1 Step 2)
  - Exponential fog with the village_flat default density ✓ (Task 1 Step 2)
  - Template mirroring ✓ (Task 2)
  - No tile pipeline changes ✓ (Phase 1A is intentionally renderer-only)

- **Placeholder scan:** No "TBD" or "implement later" in any step. Step 4 of Task 1 delegates browser verification to the controller — that's intentional because the implementer is a subagent without browser tooling.

- **Type consistency:** `logarithmicDepthBuffer: true`, `1.0, 15000`, `0xb0c4d8`, `0.00012`, `FogExp2` — same literals used everywhere they appear in both files.

- **Risks called out:**
  - logarithmicDepthBuffer vs polygonOffset (cadastre, parcel highlights) — explicit acceptance test in Task 1 Step 4. If it fails, BLOCKED.
  - Sunset preset color vs fog color — flagged as "acceptable either way for Phase 1A".

- **Estimated total:** 2 tasks, ~30 minutes. Task 1 = 15 min (edit + browser tests). Task 2 = 10 min (template mirror + smoke test). Plus controller verification.
