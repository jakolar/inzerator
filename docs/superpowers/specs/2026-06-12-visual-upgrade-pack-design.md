# Visual upgrade pack — hillshade default, tilt-shift, SSAO, MSAA, tone-mapping A/B

Date: 2026-06-12
Status: approved (Jan, 2026-06-12 — design sections A–F approved in chat,
"pokracuj v implementaci")
Scope answers: hillshade = subtle default-on (a), tilt-shift = checkbox off (a),
SSAO = checkbox off (a), AA = MSAA decided inline, tone mapping = URL A/B (a),
horizon haze dropped (already exists: HDR sky + FogExp2).

## Problem

The viewer renders plain unlit ortho on displaced terrain. Relief reads only
from the silhouette; building edges alias (the canvas has `antialias: true`
but everything draws through a non-multisampled EffectComposer target, so
canvas MSAA never applies); there is no depth-of-field or AO option for
presentation shots; tone mapping is `NoToneMapping` with no way to compare
alternatives against the already-graded ortho.

Existing pieces this builds on (all in `heightfield/index.html`, three pinned
at `0.170.0` via importmap):

- Ring ShaderMaterial already implements hillshade: `sampleNormal()` from the
  height texture + Lambert vs `uSunDir`, plus ray-marched cast shadows
  (`castShadow()`, baked `uShadowMap`). Gated on `uHillshade` (default `0.0`),
  driven by the "🌑 Sluneční svit a stíny" checkbox (`#shadow-toggle`).
  Current strength when on: `uAmbient 0.55 / uDiffuse 0.45 / uShadows 1`.
- Post chain: `bloomComposer` (selective parcel bloom, renderToScreen=false) →
  `finalComposer` = RenderPass → combineShader ShaderPass → OutputPass.
  finalComposer always runs (OutputPass does the sRGB encode).
- HDR sky background (`sky_cloudy.hdr`) + `FogExp2(SKY_HAZE, 0.00012)`.

## Design

### A) Pass order in finalComposer

```
RenderPass                       (MSAA 4×, HalfFloatType target)
  → SSAO ShaderPass              (pass.enabled = checkbox, default false)
  → combineShader ShaderPass     (bloom composite — unchanged)
  → tilt-shift H ShaderPass      (pass.enabled = checkbox, default false)
  → tilt-shift V ShaderPass      (same flag)
  → OutputPass                   (tone mapping per ?tone=, default off; sRGB)
```

Disabled passes are skipped by EffectComposer — zero per-frame cost. The
default state therefore adds only the MSAA resolve. `bloomComposer` stays
non-MSAA (its output is blurred anyway).

### B) MSAA on the final composer

Construct `finalComposer` with an explicit target:

```js
new EffectComposer(renderer, new THREE.WebGLRenderTarget(innerWidth, innerHeight, {
  samples: 4, type: THREE.HalfFloatType,
}))
```

`HalfFloatType` preserves the HDR sky values (>1.0) that the current default
composer target already carries; `setSize`/`setPixelRatio` preserve `samples`.
4 samples is universally supported on WebGL2 incl. iOS.

### C) Hillshade — subtle preset on by default

Two named presets on the existing uniforms; no shader changes:

| preset | uAmbient | uDiffuse | uShadows | when |
|---|---|---|---|---|
| SUBTLE | 0.78 | 0.22 | 0 | default from startup |
| FULL   | 0.55 | 0.45 | 1 (+ `bakeShadowMaps()`) | "🌑" checkbox on |

- Material creation defaults change to `uHillshade: 1.0` + SUBTLE values.
- SUBTLE never touches the shadow map (`uShadows 0`), so startup cost is
  unchanged — no bake until the checkbox is first enabled (existing lazy-bake
  + disabled-while-baking logic stays).
- Unchecking the checkbox returns to SUBTLE, not to unshaded.
- Flat-shade (viridis) mode is unaffected (`uShadeMode` branch bypasses
  hillshade). Pedestal walls / section band / seam skirts are separate
  materials — unaffected.
- Accepted side effect: minimap / ortho captures (plain `renderer.render`
  paths) now show subtle shading — intended, reads better.

### D) Tilt-shift ("📷 Miniatura" checkbox, default off)

Two custom ShaderPasses (separable 9-tap Gaussian, horizontal then vertical).
Blur radius scales with vertical distance from a screen-space focus band — no
depth input, cheap and robust:

```
amount = uMaxBlur * smoothstep(uBandHalf, uBandHalf + uFalloff, abs(vUv.y - uFocusY))
```

Named constants (tuned at visual verification): `uFocusY 0.5`,
`uBandHalf 0.18`, `uFalloff 0.25`, `uMaxBlur 6.0` (px at DPR 1.5). Both passes
share one uniforms-shape; the checkbox flips `enabled` on both. Screenshots
capture the effect (desired). Placed after bloom composite so the glow blurs
with the scene.

### E) SSAO ("🏘 Okluzní stíny" checkbox, default off)

three's `GTAOPass`/`SSAOPass` cannot be used: they re-render the scene with an
override normal material that does not run our displacement vertex shader —
the terrain would be evaluated as flat planes. Instead, a custom depth-based
pass:

- **Depth source:** a `DepthTexture` attached to the finalComposer MSAA target
  (three 0.170 resolves multisampled depth on blit). The main render already
  uses our custom shaders, so the depth has the true displaced geometry.
- **Lazy init:** the depth texture + SSAO pass infrastructure is built on the
  first checkbox enable, swapping the composer target via
  `finalComposer.reset(rtWithDepth)` — the default session pays no GPU memory
  for it.
- **Shader:** reconstruct view-space position from depth (inverse projection),
  normal via `cross(dFdx(pos), dFdy(pos))`, 10-tap spiral hemisphere kernel,
  view-space radius ~8 m (street/canopy scale), distance falloff.
- **Perf:** AO computed at half resolution into its own target, depth-aware
  separable blur, then composited multiplicatively onto the colour buffer by
  the SSAO ShaderPass in the main chain.
- **iOS fallback (only if multisampled-depth resolve misbehaves there):**
  render a half-res depth prepass into a separate non-MSAA target reusing the
  existing scene materials (no override), and feed that instead. Decide during
  visual verification; do not build both up front.

### F) Tone mapping A/B via URL

`?tone=agx|aces|off` (default `off`) maps to
`AgXToneMapping | ACESFilmicToneMapping | NoToneMapping` on the renderer,
set at startup before OutputPass creation; optional `?exp=<float>` sets
`toneMappingExposure` (default 1.0). No UI — a verification tool. Rationale:
the ortho is already colour-graded (see
`docs/notes/2026-05-16-ortofoto-color-grading.md`); a default change happens
only in a follow-up commit if a variant clearly wins by eye. Only OutputPass
output is affected (terrain materials write linear into the buffer as today).

### UI

Two new checkboxes next to the existing "🌑 Sluneční svit a stíny" control:
"📷 Miniatura" and "🏘 Okluzní stíny (SSAO)". Czech UI, English code —
matches repo convention.

## Out of scope

- Horizon haze / sky (already present), bloom tuning, ortho regeneration,
  cadastre/parcel rendering, pedestal (just landed), water flattening,
  default tone-mapping change (follow-up after A/B).

## Performance budget

Default state (subtle hillshade + MSAA 4×) must stay within ~10 % of today's
fps on the iPad (stats overlay readout on `slopne`). Each optional effect is
measured individually at verification; SSAO is the only one allowed to be
"heavy" since it is opt-in.

## Testing

- Syntax gate: `awk` module-script extraction + `node --check`.
- Visual on `slopne`: building-edge AA before/after; subtle vs full hillshade
  transition (checkbox on → bake → cast shadows; off → back to subtle);
  tilt-shift band placement + screenshot capture; SSAO in streets/under
  canopy + fps cost; `?tone=agx` / `?tone=aces` / `?exp=` comparison shots;
  viridis mode unaffected; parcel bloom still composites; minimap capture
  regression.
