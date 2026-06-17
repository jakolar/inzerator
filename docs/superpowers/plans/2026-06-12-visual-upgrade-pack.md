# Visual Upgrade Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MSAA on the composer, subtle hillshade on by default, opt-in tilt-shift and SSAO passes, and a `?tone=` A/B switch — per spec `docs/superpowers/specs/2026-06-12-visual-upgrade-pack-design.md`.

**Architecture:** All changes live in `heightfield/index.html` (single-file viewer, three 0.170.0 from CDN importmap). The finalComposer pass chain becomes: RenderPass (MSAA 4×) → [SSAO] → bloom combine → [tilt-shift H] → [tilt-shift V] → OutputPass; bracketed passes ship `enabled = false` and are skipped at zero cost. Hillshade reuses existing ring-shader uniforms — only defaults and the toggle handler change.

**Tech Stack:** three.js 0.170.0 (`EffectComposer`, `ShaderPass`, `Pass`/`FullScreenQuad`, `DepthTexture`, `AgXToneMapping`), vanilla JS/GLSL, no new dependencies.

---

## Important context for every task

- **File under change:** `heightfield/index.html` (~4.6k lines). Line numbers below refer to the CURRENT working tree (pedestal redesign already in it). Verify with the quoted context before editing — numbers may drift a few lines between tasks.
- **Syntax gate (run before every commit):**
  ```bash
  awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin && echo GATE_OK
  ```
  Expected: `GATE_OK`, no output from node.
- **Bash-outage protocol:** if the Bash tool is rejected by the safety classifier ("temporarily unavailable"), retry 2–3×; if still blocked, SKIP the gate + commit steps, finish the edits, and report **DONE_WITH_CONCERNS** stating which gates/commits were skipped. Do not block on Bash.
- **Visual verification** happens once at the end (Task 5) on `slopne` via the dev server on port 8080 — do not start servers per task.
- **Czech UI labels, English code/comments/commits.** Commits end with:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- **`?nobloom=1` debug mode** bypasses the composer entirely (`renderer.render`), so none of the new passes apply there. That is accepted — it is a colour-debug mode.

---

### Task 1: MSAA composer target + tone-mapping URL params

**Files:**
- Modify: `heightfield/index.html:334-338` (URL param block)
- Modify: `heightfield/index.html:429-431` (after renderer creation)
- Modify: `heightfield/index.html:4131-4136` (finalComposer construction)

- [ ] **Step 1: Add URL param parsing.** Current code at ~334–337:

```js
const _CAD_FLIP = parseInt(URL_PARAMS.get('cadflip') ?? '0', 10);
// Bypass postprocess (no parcel glow). For comparing ortho colours
// against the pre-bloom direct-render baseline.
const _NO_BLOOM = URL_PARAMS.get('nobloom') === '1';
```

Append directly below `_NO_BLOOM`:

```js
// Tone-mapping A/B (?tone=agx|aces, ?exp=1.1). Default NoToneMapping — the
// ortho is already colour-graded (docs/notes/2026-05-16-ortofoto-color-grading.md);
// this is a verification tool, not a default. Applied by OutputPass only.
const _TONE = { agx: THREE.AgXToneMapping, aces: THREE.ACESFilmicToneMapping }[
  URL_PARAMS.get('tone')] ?? THREE.NoToneMapping;
const _TONE_EXP = parseFloat(URL_PARAMS.get('exp') ?? '1') || 1.0;
```

- [ ] **Step 2: Apply to renderer.** Current code at ~429–431:

```js
const _HIRES = new URLSearchParams(location.search).get('hires') === '1';
renderer.setPixelRatio(_HIRES ? devicePixelRatio : Math.min(devicePixelRatio, 1.5));
renderer.setSize(innerWidth, innerHeight);
```

Insert after `renderer.setSize(innerWidth, innerHeight);`:

```js
renderer.toneMapping = _TONE;            // OutputPass reads this at render time
renderer.toneMappingExposure = _TONE_EXP;
```

- [ ] **Step 3: MSAA target on finalComposer.** Current code at ~4131–4133:

```js
const finalComposer = new EffectComposer(renderer);
finalComposer.setPixelRatio(renderer.getPixelRatio());
finalComposer.setSize(innerWidth, innerHeight);
```

Replace the first line with:

```js
// Canvas `antialias: true` never applies through the composer (it renders
// into offscreen targets), so building edges alias. Explicit MSAA 4× target
// fixes that; HalfFloatType keeps the HDR sky's >1.0 values for the bloom
// combine + tone mapping. bloomComposer stays non-MSAA — its output is
// blurred anyway. setSize/setPixelRatio preserve `samples`.
const finalComposer = new EffectComposer(renderer, new THREE.WebGLRenderTarget(
  innerWidth, innerHeight, { samples: 4, type: THREE.HalfFloatType }));
```

- [ ] **Step 4: Syntax gate.** Run the awk + `node --check` gate. Expected: `GATE_OK`.

- [ ] **Step 5: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): MSAA composer target + tone-mapping A/B URL params

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Subtle hillshade on by default

**Files:**
- Modify: `heightfield/index.html:464-470` (sun config area — add preset constants)
- Modify: `heightfield/index.html:1003-1011` (ring material uniform defaults)
- Modify: `heightfield/index.html:1953-1969` (shadow-toggle handler)

- [ ] **Step 1: Add preset constants.** Current code at ~464–469:

```js
// Sun direction in scene coords. Y = up, +Z = south, +X = east in our
// SJTSK-aligned PlaneGeometry layout. South-elevated midday sun gives the
// most legible relief on Czech terrain (north-facing slopes shaded, south
// slopes lit — matches photo intuition).
const SUN_EL = 45, SUN_AZ = 215;   // 45° elevation, SSW (geographic bearing)
```

Insert directly below the `const SUN_EL …` line:

```js
// Hillshade presets. SUBTLE is on from startup — gentle relief legibility
// with zero startup cost (uShadows 0 → the baked shadow map is never read,
// so no bake happens until the "🌑" checkbox first turns FULL on).
const HILLSHADE_SUBTLE = { ambient: 0.78, diffuse: 0.22, shadows: 0.0 };
const HILLSHADE_FULL   = { ambient: 0.55, diffuse: 0.45, shadows: 1.0 };
```

- [ ] **Step 2: Change uniform defaults.** Current code at ~1003–1011:

```js
      uHillshade: { value: 0.0 },        // 0 = off (plain ortho), 1 = on
      uShadows:   { value: 0.0 },        // 0 = lambert only, 1 = + cast shadows
      // Precomputed shadow texture (baked GPU pass — see bakeShadowMaps).
      // Default: 1×1 white = "fully lit" so toggling shadows before bake
      // completes doesn't blacken everything. Replaced by render-target
      // texture once bake finishes.
      uShadowMap: { value: window._heightfieldDefaultShadowTex },
      uAmbient:   { value: 0.55 },        // base lighting on shadow side
      uDiffuse:   { value: 0.45 },        // sun-facing slope brightness boost
```

Replace with:

```js
      uHillshade: { value: 1.0 },        // always on; SUBTLE↔FULL via "🌑" checkbox
      uShadows:   { value: HILLSHADE_SUBTLE.shadows },
      // Precomputed shadow texture (baked GPU pass — see bakeShadowMaps).
      // Default: 1×1 white = "fully lit" so toggling shadows before bake
      // completes doesn't blacken everything. Replaced by render-target
      // texture once bake finishes.
      uShadowMap: { value: window._heightfieldDefaultShadowTex },
      uAmbient:   { value: HILLSHADE_SUBTLE.ambient },
      uDiffuse:   { value: HILLSHADE_SUBTLE.diffuse },
```

- [ ] **Step 3: Rewire the toggle.** Current code at ~1953–1969:

```js
shadowToggle.addEventListener('change', async () => {
  if (shadowToggle.checked) {
    shadowToggle.disabled = true;
    shadowStatus.textContent = 'počítám stíny…';
    await ensureShadowMaps();
    shadowStatus.textContent = '';
    shadowToggle.disabled = false;
    sunControls.style.display = '';
  } else {
    sunControls.style.display = 'none';
  }
  const on = shadowToggle.checked ? 1.0 : 0.0;
  for (const mesh of ringMeshes) {
    mesh.material.uniforms.uHillshade.value = on;
    mesh.material.uniforms.uShadows.value = on;
  }
});
```

Replace the lines from `const on = …` through the closing `}` of the for-loop with:

```js
  // Switch between presets — hillshade itself never turns off (uHillshade
  // stays 1.0; the viridis flat-shade mode bypasses it in-shader).
  const p = shadowToggle.checked ? HILLSHADE_FULL : HILLSHADE_SUBTLE;
  for (const mesh of ringMeshes) {
    const u = mesh.material.uniforms;
    u.uAmbient.value = p.ambient;
    u.uDiffuse.value = p.diffuse;
    u.uShadows.value = p.shadows;
  }
```

(Keep the surrounding `addEventListener` wrapper and the bake/`sunControls` logic untouched.)

- [ ] **Step 4: Check there are no other `uHillshade` writers.** Run:

```bash
grep -n "uHillshade.value\|uAmbient.value\|uDiffuse.value" heightfield/index.html
```

Expected: only the toggle-handler lines you just wrote (plus uniform declarations). If any other writer appears, stop and report — the spec assumes there is exactly one.

- [ ] **Step 5: Syntax gate.** Expected: `GATE_OK`.

- [ ] **Step 6: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): subtle hillshade on by default; checkbox switches to full sun+shadows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Tilt-shift ("📷 Miniatura")

**Files:**
- Modify: `heightfield/index.html:101` (UI — new details section after "Slunce a stíny")
- Modify: `heightfield/index.html:4131-4136` (pass chain — insert before OutputPass)
- Modify: `heightfield/index.html:530-539` (`setDpr`) and `:4260-4266` (resize listener)

- [ ] **Step 1: Add UI.** Current code at ~85–101 ends the sun section with `</details>`. Insert AFTER that closing `</details>` (before the `<details open class="info-section"><summary>Parcely a POI</summary>` block):

```html
  <details class="info-section">
    <summary>Efekty</summary>
    <label style="display:block;cursor:pointer;">
      <input id="tiltshift-toggle" type="checkbox"> 📷 Miniatura
    </label>
    <label style="display:block;cursor:pointer;">
      <input id="ssao-toggle" type="checkbox"> 🏘 Okluzní stíny (SSAO)
    </label>
  </details>
```

(The SSAO checkbox is wired in Task 4; an unwired checkbox in between commits is harmless.)

- [ ] **Step 2: Shader + passes.** Current code at ~4131–4136 (after Task 1 it reads):

```js
const finalComposer = new EffectComposer(renderer, new THREE.WebGLRenderTarget(
  innerWidth, innerHeight, { samples: 4, type: THREE.HalfFloatType }));
finalComposer.setPixelRatio(renderer.getPixelRatio());
finalComposer.setSize(innerWidth, innerHeight);
finalComposer.addPass(new RenderPass(scene, camera));
finalComposer.addPass(new ShaderPass(combineShader, 'baseTexture'));
finalComposer.addPass(new OutputPass());
```

Insert between the `combineShader` line and the `OutputPass` line:

```js
// ── Tilt-shift ("miniature" effect, opt-in) ──
// Separable Gaussian whose radius grows with vertical distance from a
// screen-space focus band — no depth input needed, so it is cheap and can't
// be confused by the displaced terrain. Placed after the bloom combine so
// selected-parcel glow blurs together with the scene.
const TILT_FOCUS_Y   = 0.5;    // focus band centre (UV y)
const TILT_BAND_HALF = 0.18;   // sharp half-height of the band
const TILT_FALLOFF   = 0.25;   // smoothstep ramp beyond the band
const TILT_MAX_BLUR  = 6.0;    // max blur radius in device px (at capped DPR)
const TiltShiftShader = {
  uniforms: {
    tDiffuse: { value: null },
    uDir:     { value: new THREE.Vector2(1, 0) },
    uTexel:   { value: new THREE.Vector2(1 / innerWidth, 1 / innerHeight) },
    uFocusY:  { value: TILT_FOCUS_Y },
    uBandHalf:{ value: TILT_BAND_HALF },
    uFalloff: { value: TILT_FALLOFF },
    uMaxBlur: { value: TILT_MAX_BLUR },
  },
  vertexShader: `
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
  `,
  fragmentShader: `
    uniform sampler2D tDiffuse;
    uniform vec2 uDir, uTexel;
    uniform float uFocusY, uBandHalf, uFalloff, uMaxBlur;
    varying vec2 vUv;
    void main() {
      float amt = uMaxBlur * smoothstep(uBandHalf, uBandHalf + uFalloff, abs(vUv.y - uFocusY));
      vec2 stp = uDir * uTexel * amt;
      // 5-tap linearly-sampled Gaussian (effective 9-tap).
      vec4 c = texture2D(tDiffuse, vUv) * 0.227027;
      c += (texture2D(tDiffuse, vUv + stp * 1.384615) + texture2D(tDiffuse, vUv - stp * 1.384615)) * 0.316216;
      c += (texture2D(tDiffuse, vUv + stp * 3.230769) + texture2D(tDiffuse, vUv - stp * 3.230769)) * 0.070270;
      gl_FragColor = c;
    }
  `,
};
const tiltShiftH = new ShaderPass(TiltShiftShader);
const tiltShiftV = new ShaderPass(TiltShiftShader);
tiltShiftV.material.uniforms.uDir.value.set(0, 1);
tiltShiftH.enabled = false;
tiltShiftV.enabled = false;
finalComposer.addPass(tiltShiftH);
finalComposer.addPass(tiltShiftV);
// uTexel must track the real drawing-buffer size (DPR changes during drag —
// see setDpr — and on window resize). ShaderPass.setSize is a no-op, so we
// update it explicitly from both call sites.
function updateTiltShiftTexel() {
  const s = renderer.getDrawingBufferSize(new THREE.Vector2());
  for (const p of [tiltShiftH, tiltShiftV])
    p.material.uniforms.uTexel.value.set(1 / s.x, 1 / s.y);
}
updateTiltShiftTexel();

const tiltShiftToggle = document.getElementById('tiltshift-toggle');
tiltShiftToggle.addEventListener('change', () => {
  tiltShiftH.enabled = tiltShiftToggle.checked;
  tiltShiftV.enabled = tiltShiftToggle.checked;
});
```

- [ ] **Step 3: Texel updates on DPR/resize.** In `setDpr` (~530–539) append after `finalComposer?.setSize(innerWidth, innerHeight);`:

```js
  updateTiltShiftTexel?.();
```

And in the resize listener at ~4260–4266, after `finalComposer.setSize(innerWidth, innerHeight);` add:

```js
  updateTiltShiftTexel();
```

(`setDpr` runs only on user interaction, long after module evaluation, so the hoisted function declaration is safely defined by then; the optional-call guard is belt-and-braces.)

- [ ] **Step 4: Syntax gate.** Expected: `GATE_OK`.

- [ ] **Step 5: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): tilt-shift miniature effect (opt-in checkbox)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: SSAO ("🏘 Okluzní stíny", opt-in, lazy init)

**Files:**
- Modify: `heightfield/index.html:246` (imports — add `Pass`, `FullScreenQuad`)
- Modify: `heightfield/index.html` (new block directly after the tilt-shift block from Task 3, before `renderer.info.autoReset = false;`)

**Why custom:** three's `GTAOPass`/`SSAOPass` re-render the scene with an override normal material that does not run our displacement vertex shader — terrain would be evaluated as flat planes. We instead read the depth buffer of the real render (which has the displaced geometry) and reconstruct positions/normals from it.

- [ ] **Step 1: Add import.** Current code at ~246:

```js
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
```

Add below it:

```js
import { Pass, FullScreenQuad } from 'three/addons/postprocessing/Pass.js';
```

- [ ] **Step 2: SSAO pass class + lazy init + checkbox.** Insert this whole block AFTER the `tiltShiftToggle.addEventListener(...)` block from Task 3 (i.e. after the `OutputPass` has been added — the SSAO pass's position in the chain is handled at runtime by `insertPass(pass, 1)` below, not by source order):

```js
// ── SSAO (opt-in, lazy) ──
// Depth-only AO: view-space position from the main render's depth buffer,
// normals via screen-space derivatives, 10-tap golden-angle spiral kernel,
// computed at half resolution + depth-aware blur, multiplied onto the colour
// buffer. Built on first enable so the default session pays nothing (no
// depth texture, no extra targets). iOS fallback if multisampled-depth
// resolve misbehaves: see the spec §E (separate non-MSAA depth prepass).
const SSAO_RADIUS    = 8.0;    // view-space metres (street / canopy scale)
const SSAO_INTENSITY = 1.0;
const SSAO_BIAS      = 0.02;

class DepthSSAOPass extends Pass {
  constructor(depthTex, cam) {
    super();
    this.needsSwap = true;
    this._cam = cam;
    const half = (w, h) => new THREE.WebGLRenderTarget(
      Math.max(1, Math.ceil(w / 2)), Math.max(1, Math.ceil(h / 2)));
    const s = renderer.getDrawingBufferSize(new THREE.Vector2());
    this._aoRT = half(s.x, s.y);
    this._blurRT = half(s.x, s.y);
    const quadVS = `
      varying vec2 vUv;
      void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
    `;
    this._aoMat = new THREE.ShaderMaterial({
      uniforms: {
        uDepth:     { value: depthTex },
        uProjInv:   { value: new THREE.Matrix4() },
        uProjScale: { value: new THREE.Vector2() },  // 0.5*(proj[0][0], proj[1][1])
        uRadius:    { value: SSAO_RADIUS },
        uIntensity: { value: SSAO_INTENSITY },
        uBias:      { value: SSAO_BIAS },
      },
      vertexShader: quadVS,
      fragmentShader: `
        varying vec2 vUv;
        uniform sampler2D uDepth;
        uniform mat4 uProjInv;
        uniform vec2 uProjScale;
        uniform float uRadius, uIntensity, uBias;
        vec3 viewPos(vec2 uv) {
          float d = texture2D(uDepth, uv).x;
          vec4 p = uProjInv * vec4(uv * 2.0 - 1.0, d * 2.0 - 1.0, 1.0);
          return p.xyz / p.w;
        }
        void main() {
          if (texture2D(uDepth, vUv).x >= 1.0) { gl_FragColor = vec4(1.0); return; }  // sky
          vec3 p = viewPos(vUv);
          vec3 n = normalize(cross(dFdx(p), dFdy(p)));
          // Interleaved gradient noise — per-pixel kernel rotation without a noise texture.
          float ign = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
          float occ = 0.0;
          const float N = 10.0;
          for (int i = 0; i < 10; i++) {
            float fi = float(i);
            float ang = (fi + ign) * 2.39996323;          // golden-angle spiral
            float rad = uRadius * sqrt((fi + 0.5) / N);
            vec2 duv = vec2(cos(ang), sin(ang)) * rad * uProjScale / -p.z;
            vec3 dv = viewPos(vUv + duv) - p;
            float dist = length(dv);
            occ += max(0.0, dot(n, dv) / max(dist, 0.01) - uBias)
                 * (1.0 - smoothstep(0.0, uRadius, dist));
          }
          gl_FragColor = vec4(vec3(clamp(1.0 - uIntensity * occ / N, 0.0, 1.0)), 1.0);
        }
      `,
    });
    this._blurMat = new THREE.ShaderMaterial({
      uniforms: {
        tAO:    { value: null },
        uDepth: { value: depthTex },
        uDir:   { value: new THREE.Vector2(1, 0) },
        uTexel: { value: new THREE.Vector2(1 / this._aoRT.width, 1 / this._aoRT.height) },
      },
      vertexShader: quadVS,
      fragmentShader: `
        varying vec2 vUv;
        uniform sampler2D tAO, uDepth;
        uniform vec2 uDir, uTexel;
        void main() {
          float d0 = texture2D(uDepth, vUv).x;
          float sum = texture2D(tAO, vUv).r;
          float wsum = 1.0;
          for (int i = 1; i <= 3; i++) {
            vec2 off = uDir * uTexel * float(i);
            // depth-aware: don't bleed AO across silhouettes
            float wA = exp(-abs(texture2D(uDepth, vUv + off).x - d0) * 4000.0);
            float wB = exp(-abs(texture2D(uDepth, vUv - off).x - d0) * 4000.0);
            sum += texture2D(tAO, vUv + off).r * wA + texture2D(tAO, vUv - off).r * wB;
            wsum += wA + wB;
          }
          gl_FragColor = vec4(vec3(sum / wsum), 1.0);
        }
      `,
    });
    this._compMat = new THREE.ShaderMaterial({
      uniforms: { tDiffuse: { value: null }, tAO: { value: null } },
      vertexShader: quadVS,
      fragmentShader: `
        varying vec2 vUv;
        uniform sampler2D tDiffuse, tAO;
        void main() {
          vec4 c = texture2D(tDiffuse, vUv);
          gl_FragColor = vec4(c.rgb * texture2D(tAO, vUv).r, c.a);
        }
      `,
    });
    this._quad = new FullScreenQuad(this._aoMat);
  }
  setSize(w, h) {   // called by composer.setSize (covers resize + setDpr)
    const hw = Math.max(1, Math.ceil(w / 2)), hh = Math.max(1, Math.ceil(h / 2));
    this._aoRT.setSize(hw, hh);
    this._blurRT.setSize(hw, hh);
    this._blurMat.uniforms.uTexel.value.set(1 / hw, 1 / hh);
  }
  render(renderer, writeBuffer, readBuffer) {
    const cam = this._cam;
    this._aoMat.uniforms.uProjInv.value.copy(cam.projectionMatrixInverse);
    this._aoMat.uniforms.uProjScale.value.set(
      cam.projectionMatrix.elements[0] * 0.5, cam.projectionMatrix.elements[5] * 0.5);
    // 1) raw AO at half res
    this._quad.material = this._aoMat;
    renderer.setRenderTarget(this._aoRT);
    this._quad.render(renderer);
    // 2) depth-aware blur H → blurRT, V → back to aoRT
    this._quad.material = this._blurMat;
    this._blurMat.uniforms.tAO.value = this._aoRT.texture;
    this._blurMat.uniforms.uDir.value.set(1, 0);
    renderer.setRenderTarget(this._blurRT);
    this._quad.render(renderer);
    this._blurMat.uniforms.tAO.value = this._blurRT.texture;
    this._blurMat.uniforms.uDir.value.set(0, 1);
    renderer.setRenderTarget(this._aoRT);
    this._quad.render(renderer);
    // 3) multiply onto colour
    this._quad.material = this._compMat;
    this._compMat.uniforms.tDiffuse.value = readBuffer.texture;
    this._compMat.uniforms.tAO.value = this._aoRT.texture;
    renderer.setRenderTarget(this.renderToScreen ? null : writeBuffer);
    this._quad.render(renderer);
  }
}

let ssaoPass = null;
function initSSAO() {
  // Rebuild the composer's ping-pong targets with a shared DepthTexture so
  // the depth of the real (displaced) render is samplable. BOTH targets get
  // the SAME instance — RenderPass writes into whichever buffer is current,
  // and the AO shader always reads this one texture. three 0.170 resolves
  // multisampled depth into the attached DepthTexture on blit.
  const s = renderer.getDrawingBufferSize(new THREE.Vector2());
  const depthTex = new THREE.DepthTexture(s.x, s.y);
  const rt = new THREE.WebGLRenderTarget(s.x, s.y, {
    samples: 4, type: THREE.HalfFloatType, depthTexture: depthTex,
  });
  finalComposer.reset(rt);
  finalComposer.renderTarget2.depthTexture = depthTex;
  const pass = new DepthSSAOPass(depthTex, camera);
  finalComposer.insertPass(pass, 1);   // directly after RenderPass, before bloom combine
  return pass;
}

const ssaoToggle = document.getElementById('ssao-toggle');
ssaoToggle.addEventListener('change', () => {
  if (ssaoToggle.checked && !ssaoPass) ssaoPass = initSSAO();
  if (ssaoPass) ssaoPass.enabled = ssaoToggle.checked;
});
```

- [ ] **Step 3: Syntax gate.** Expected: `GATE_OK`.

- [ ] **Step 4: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): opt-in depth-based SSAO pass (lazy init)

Custom pass instead of three's GTAO/SSAOPass: those re-render with an
override material that skips our displacement vertex shader. AO is
reconstructed from the main render's depth buffer (half-res spiral kernel
+ depth-aware blur), so the true displaced terrain geometry is used.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Visual verification + tuning (no new code unless tuning)

**Files:**
- Possibly modify: the named constants from Tasks 2–4.

- [ ] **Step 1: Serve.** Check the server: `lsof -ti:8080` — if empty, start `python3 server.py` in background from the repo root (binds 0.0.0.0). View `http://<host>:8080/heightfield/?slug=slopne` from the remote device.

- [ ] **Step 2: Default state.** Verify: building edges noticeably smoother (MSAA); terrain has gentle relief shading vs. yesterday's flat ortho; fps in the stats overlay within ~10 % of pre-change values on the iPad; pedestal + section band + parcel bloom unchanged; minimap / ortho-capture features still work (they use plain `renderer.render` and now show subtle shading — intended).

- [ ] **Step 3: Hillshade transition.** Toggle "🌑 Sluneční svit a stíny" on: status shows "počítám stíny…", then stronger shading + cast shadows. Toggle off: returns to the subtle look (NOT to flat). Viridis mode (flat-shade) unaffected. Bare-earth toggle still re-bakes when full mode is on.

- [ ] **Step 4: Tilt-shift.** Enable "📷 Miniatura": sharp band across the centre, blur ramping to top/bottom edges; works while orbiting; screenshot captures it; fps cost small. Tune `TILT_*` constants if the band reads wrong.

- [ ] **Step 5: SSAO.** Enable "🏘 Okluzní stíny": streets between houses and under-canopy areas darken; no halos around building silhouettes (depth-aware blur working); no banding on open fields (bias working). **Measure fps** — record before/after in the verification report. If iOS shows garbage AO (multisampled-depth resolve issue), STOP and report — the spec's §E fallback (separate depth prepass) becomes a follow-up task; do not improvise it.

- [ ] **Step 6: Tone A/B.** Load `?tone=agx`, `?tone=aces`, `?tone=agx&exp=1.15` — compare against default. No commit of a default change; just screenshots/notes for Jan.

- [ ] **Step 7: Tuning commit (only if constants changed):**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): tune visual-pack constants after on-device verification

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
