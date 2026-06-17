# Ortho Grading Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a runtime, switchable colour-grading "look" picker to the heightfield viewer, applied to the ortho albedo in the terrain shaders, with four presets (Vyp / Teplý / Neutrální / Živý) and zero cost when off.

**Architecture:** A shared `gradeUniforms` object is referenced (by-reference, like `uOrtho`) into every ortho-sampling material. A pointwise `applyGrade()` GLSL function — shared between the ring and edge shaders via a single JS template string — grades the sampled albedo before lighting. A `🎨 Vzhled` select in the "Efekty" panel writes preset scalars into `gradeUniforms` (no shader recompile). Default `off` → `uGradeOn 0` → one branch, no work.

**Tech Stack:** Single-file `heightfield/index.html`, three.js 0.170.0 (CDN importmap), raw `ShaderMaterial`.

---

## Important context for every task

- **Only file touched:** `heightfield/index.html` (~4.9k lines). No servers to start, no other files.
- **Spec:** `docs/superpowers/specs/2026-06-17-ortho-grading-presets-design.md`.
- **Line numbers are approximate** (earlier edits shift them). Locate every edit by the quoted anchor code, not by line number.
- **Czech UI strings, English code/comments.**
- **Syntax gate** (the project's test for this file) — run after every code task, expect `GATE_OK`:
  ```bash
  cd /Users/jan/projekty/inzerator && awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin && echo GATE_OK
  ```
- **Commit trailer** on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- The ortho is sRGB and the shipped tiers are KTX2 (hardware sRGB→linear on sample), so the albedo `col` at the injection site is **linear**; `applyGrade` decodes to sRGB, grades, re-encodes (matches the spec).

---

### Task 1: Preset data + shared uniform object, wired into both ortho materials

**Files:**
- Modify: `heightfield/index.html` — insert a block just before `function makeEdgeOrthoMaterial(ringMat, darken) {`; add `...gradeUniforms,` to the edge material and the ring material uniform blocks.

- [ ] **Step 1: Add the preset table + shared uniforms.** Find the line:

```js
function makeEdgeOrthoMaterial(ringMat, darken) {
```

Insert directly ABOVE it:

```js
// ── Ortho grading presets (runtime "look", applied to the ortho albedo) ──
// Pointwise colour grade injected into the terrain shaders (ring + edge). One
// shared uniform object, referenced (not cloned) by every ortho-sampling
// material, so switching a preset writes scalars here once — no shader
// recompile. Default off (uGradeOn 0) costs one branch per terrain fragment.
// Starting values — tune on device. Spec: docs/superpowers/specs/2026-06-17-ortho-grading-presets-design.md
const GRADE_PRESETS = {
  off:     { on: 0, exposure: 0,    temp: 0,    tint: 0,    contrast: 1.00, shadows: 0,    highlights: 0,     whites: 0,    blacks: 0,     vibrance: 0,    saturation: 1.00 },
  warm:    { on: 1, exposure: 0.05, temp: 0.04, tint: 0.02, contrast: 1.14, shadows: 0.10, highlights: -0.12, whites: 0.06, blacks: -0.08, vibrance: 0.24, saturation: 1.05 },
  neutral: { on: 1, exposure: 0,    temp: 0,    tint: 0,    contrast: 1.08, shadows: 0.05, highlights: -0.06, whites: 0,    blacks: -0.04, vibrance: 0.12, saturation: 1.03 },
  vivid:   { on: 1, exposure: 0.05, temp: 0.02, tint: 0,    contrast: 1.18, shadows: 0.06, highlights: -0.10, whites: 0.06, blacks: -0.06, vibrance: 0.34, saturation: 1.10 },
};
const gradeUniforms = {
  uGradeOn:     { value: 0.0 },
  uGExposure:   { value: 0.0 },
  uGTemp:       { value: 0.0 },
  uGTint:       { value: 0.0 },
  uGContrast:   { value: 1.0 },
  uGShadows:    { value: 0.0 },
  uGHighlights: { value: 0.0 },
  uGWhites:     { value: 0.0 },
  uGBlacks:     { value: 0.0 },
  uGVibrance:   { value: 0.0 },
  uGSaturation: { value: 1.0 },
};
```

- [ ] **Step 2: Reference the shared uniforms in the edge material.** In `makeEdgeOrthoMaterial`, find:

```js
    uniforms: {
      ...fogUniforms,
      // SHARED with the ring material — reference, do NOT clone.
      uOrtho:      ringMat.uniforms.uOrtho,
```

Insert `...gradeUniforms,` so it reads:

```js
    uniforms: {
      ...fogUniforms,
      ...gradeUniforms,   // SHARED grade — same object as the ring material
      // SHARED with the ring material — reference, do NOT clone.
      uOrtho:      ringMat.uniforms.uOrtho,
```

- [ ] **Step 3: Reference the shared uniforms in the ring material.** Find the ring material's uniform block (the one with `uHeight`, `uHeightBare`, `uBareMode`):

```js
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      ...fogUniforms,
      uHeight:    { value: heightTex },
```

Insert `...gradeUniforms,` so it reads:

```js
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      ...fogUniforms,
      ...gradeUniforms,
      uHeight:    { value: heightTex },
```

- [ ] **Step 4: Syntax gate.** Run the gate command. Expected: `GATE_OK`. (The uniforms are not yet referenced in any GLSL — harmless; three ignores unbound uniform entries.)

- [ ] **Step 5: Confirm sharing is by-reference.** Run:

```bash
grep -n "gradeUniforms" heightfield/index.html
```

Expected: the definition + exactly two `...gradeUniforms` spreads (edge + ring). The spread copies the inner `{value}` object references, so one write to `gradeUniforms.uX.value` propagates to every material.

- [ ] **Step 6: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): ortho grading preset data + shared uniforms

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: applyGrade GLSL — shared function injected into both shaders

**Files:**
- Modify: `heightfield/index.html` — add `GRADE_GLSL` const after the `gradeUniforms` block; inject `${GRADE_GLSL}` into the ring + edge fragment shaders; call `applyGrade(...)` at both sample sites.

- [ ] **Step 1: Define the shared GLSL string.** Find (added in Task 1):

```js
const gradeUniforms = {
```

…and locate the closing `};` of that object. Directly AFTER that `};`, insert:

```js
// Pointwise grade in sRGB/gamma space (texture sample is linear → decode,
// grade, re-encode). Shared verbatim by the ring + edge shaders. uGradeOn 0
// short-circuits to zero work. shadows+blacks (and highlights+whites) share a
// luma mask — a deliberate simplification of Lightroom's separate tonal bands.
const GRADE_GLSL = `
  uniform float uGradeOn, uGExposure, uGTemp, uGTint, uGContrast,
                uGShadows, uGHighlights, uGWhites, uGBlacks, uGVibrance, uGSaturation;
  float _gLuma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
  vec3 applyGrade(vec3 lin) {
    if (uGradeOn < 0.5) return lin;
    vec3 c = pow(clamp(lin, 0.0, 1.0), vec3(1.0 / 2.2)); // linear → ~sRGB
    c.r += uGTemp; c.b -= uGTemp; c.g -= uGTint;         // white balance
    c *= exp2(uGExposure);                               // exposure
    c = (c - 0.5) * uGContrast + 0.5;                    // contrast
    float L = _gLuma(clamp(c, 0.0, 1.0));
    float darkM  = 1.0 - smoothstep(0.0, 0.5, L);
    float lightM = smoothstep(0.5, 1.0, L);
    c += uGShadows * darkM + uGBlacks * darkM;
    c += uGHighlights * lightM + uGWhites * lightM;
    c = clamp(c, 0.0, 1.0);
    float g = _gLuma(c);
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    float sat = mx - mn;                                 // cheap saturation
    c = mix(vec3(g), c, 1.0 + uGVibrance * (1.0 - sat)); // vibrance
    c = mix(vec3(g), c, uGSaturation);                   // saturation
    return pow(clamp(c, 0.0, 1.0), vec3(2.2));           // ~sRGB → linear
  }
`;
```

- [ ] **Step 2: Inject into the edge fragment shader + call it.** In `makeEdgeOrthoMaterial`, find:

```js
      uniform float uDarken;
      varying vec2 vUv;
      void main() {
```

Replace with (inject the GLSL between the varying and `main`):

```js
      uniform float uDarken;
      varying vec2 vUv;
      ${GRADE_GLSL}
      void main() {
```

Then in the same shader find:

```js
        gl_FragColor = vec4(texture2D(uOrtho, orthoUv).rgb * uDarken, 1.0);
```

Replace with:

```js
        gl_FragColor = vec4(applyGrade(texture2D(uOrtho, orthoUv).rgb) * uDarken, 1.0);
```

- [ ] **Step 3: Inject into the ring fragment shader.** Find (the end of the ring shader's varying declarations):

```js
      varying vec2 vUv;
      varying vec3 vWorldPos;
```

Insert `${GRADE_GLSL}` after them:

```js
      varying vec2 vUv;
      varying vec3 vWorldPos;
      ${GRADE_GLSL}
```

> Note: if `varying vec3 vWorldPos;` appears more than once, pick the occurrence inside the RING material's `fragmentShader` (the long shader with `uHillshade`, `viridis`, `uShadeMode`). The bake material / vertex shaders do not have this exact varying pair followed by the ortho branch.

- [ ] **Step 4: Call applyGrade at the ring sample site.** Find:

```js
          col = texture2D(uOrtho, orthoUv).rgb;
          if (uCadEnabled > 0.5) {
```

Replace with (grade the albedo before the white-line cadastre overlay, so cadastre lines stay pure white/ungraded):

```js
          col = applyGrade(texture2D(uOrtho, orthoUv).rgb);
          if (uCadEnabled > 0.5) {
```

- [ ] **Step 5: Syntax gate.** Run the gate command. Expected: `GATE_OK`. (This checks JS only — a GLSL compile error would surface on-device, hence the verification in Task 3 / device testing.)

- [ ] **Step 6: Confirm injection.** Run:

```bash
grep -n "GRADE_GLSL\|applyGrade(" heightfield/index.html
```

Expected: the `const GRADE_GLSL` definition, two `${GRADE_GLSL}` injections (ring + edge), and two `applyGrade(` calls (ring sample site + edge `gl_FragColor`).

- [ ] **Step 7: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): applyGrade GLSL in ring + edge ortho shaders

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: UI select + preset handler

**Files:**
- Modify: `heightfield/index.html` — add the `🎨 Vzhled` select to the "Efekty" panel; add the change handler after the tone-mapping handler.

- [ ] **Step 1: Add the select to the Efekty panel.** Find (the end of the tone-mapping UI, inside the Efekty `<details>`):

```html
    <label style="display:block;">
      Expozice: <input id="exp-slider" type="range" min="0.5" max="1.8" step="0.05" value="1" style="vertical-align:middle;">
      <span id="exp-val">1.00</span>
    </label>
  </details>
```

Insert the grade select before the closing `</details>`:

```html
    <label style="display:block;">
      Expozice: <input id="exp-slider" type="range" min="0.5" max="1.8" step="0.05" value="1" style="vertical-align:middle;">
      <span id="exp-val">1.00</span>
    </label>
    <label style="display:block;margin-top:4px;">
      🎨 Vzhled:
      <select id="grade-select">
        <option value="off" selected>Vyp</option>
        <option value="warm">Teplý</option>
        <option value="neutral">Neutrální</option>
        <option value="vivid">Živý</option>
      </select>
    </label>
  </details>
```

- [ ] **Step 2: Add the change handler.** Find (the end of the tone-mapping handler):

```js
toneSelect.addEventListener('change', applyTone);
expSlider.addEventListener('input', applyTone);
```

Insert AFTER it:

```js

// Ortho grading "look" — writes the chosen preset into the shared gradeUniforms
// (referenced by every terrain material), no shader recompile. Default Vyp.
const gradeSelect = document.getElementById('grade-select');
function applyGradePreset() {
  const p = GRADE_PRESETS[gradeSelect.value] || GRADE_PRESETS.off;
  gradeUniforms.uGradeOn.value     = p.on;
  gradeUniforms.uGExposure.value   = p.exposure;
  gradeUniforms.uGTemp.value       = p.temp;
  gradeUniforms.uGTint.value       = p.tint;
  gradeUniforms.uGContrast.value   = p.contrast;
  gradeUniforms.uGShadows.value    = p.shadows;
  gradeUniforms.uGHighlights.value = p.highlights;
  gradeUniforms.uGWhites.value     = p.whites;
  gradeUniforms.uGBlacks.value     = p.blacks;
  gradeUniforms.uGVibrance.value   = p.vibrance;
  gradeUniforms.uGSaturation.value = p.saturation;
}
gradeSelect.addEventListener('change', applyGradePreset);
```

(No init call needed — the select defaults to `Vyp` and `gradeUniforms` already holds the off values.)

- [ ] **Step 3: Syntax gate.** Run the gate command. Expected: `GATE_OK`.

- [ ] **Step 4: Confirm wiring.** Run:

```bash
grep -n "grade-select\|applyGradePreset\|gradeSelect" heightfield/index.html
```

Expected: the `<select id="grade-select">`, the `applyGradePreset` function, the `gradeSelect` lookup, and the `change` listener.

- [ ] **Step 5: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): Vzhled grading preset UI (select in Efekty panel)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: On-device visual verification + tuning (no new code unless tuning)

**Files:**
- Possibly modify: `GRADE_PRESETS` values only.

- [ ] **Step 1: Serve.** Check `lsof -ti:8082`; the inzerator server should already run there (gtaol holds 8080). View `http://<host>:8082/heightfield/?slug=slopne` from the device. If 8082 is free, start the server per the repo convention on `0.0.0.0`.

- [ ] **Step 2: Default + switching.** Confirm `Vyp` is identical to before this feature. Switch Vyp → Teplý → Neutrální → Živý: the ground re-grades live, instantly, with no shader-recompile stall after the first switch. The HDR sky, parcel lines, POI markers, and parcel bloom must NOT change colour. fps unchanged on `Vyp`.

- [ ] **Step 3: Correctness.** Verify the pedestal section band + visible terrain share the same look (edge material graded too). Enable viridis flat-shade mode — it must be unaffected by the grade. Toggle the cadastre overlay — white parcel lines stay pure white (ungraded). The minimap / a screenshot capture shows the grade (intended).

- [ ] **Step 4: Tune.** Adjust the `GRADE_PRESETS` numbers by eye for the real-estate look. If changed, commit:

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): tune ortho grading presets after on-device verification

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
