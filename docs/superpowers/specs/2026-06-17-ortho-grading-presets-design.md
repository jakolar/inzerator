# Ortho grading presets — runtime "look" picker in the viewer

Date: 2026-06-17
Status: approved (Jan, 2026-06-17 — design approved in chat: "napiš spec, hodnoty
ber jako startovní, jen UI select bez URL")

Scope answers (brainstorm 2026-06-17):
- Where computed: **runtime shader** in the viewer (not server-side bake) — live
  switchable, per-session.
- Operations: **pointwise only** (no clarity/texture/dehaze spatial filters).
- Presets: a fixed 4-entry set (Vyp / Teplý / Neutrální / Živý), **default Vyp**.
- Apply point: **approach A** — grade the ortho albedo inside the terrain
  shaders (not a full-frame post pass), so only the ground is graded.
- UI: a select in the "Efekty" panel. **No `?grade=` URL param.**

## Problem

The viewer draws raw ČÚZK ortho on the terrain. It reads flat and grey. We want
a small set of curated "looks" (real-estate friendly: warmer, more contrast,
lifted shadows, more vibrance) that the user can switch live from the UI, with
zero cost when off. The existing `?tone=`/🎨 tone-mapping control is a
whole-frame tone-mapping operator — a different thing; grading is a per-ortho
colour look applied to the ground albedo before lighting.

Builds on (all in `heightfield/index.html`, three pinned `0.170.0`):
- The ring `ShaderMaterial` samples the ortho at `col = texture2D(uOrtho, orthoUv).rgb`
  (frag shader ~line 1208), then applies hillshade. The viridis flat-shade
  branch (`uShadeMode > 0.5`) does NOT sample ortho albedo.
- `makeEdgeOrthoMaterial(ringMat, darken)` (~line 850) builds the seam-skirt +
  pedestal section-band material; it samples ortho at ~line 882 and **shares the
  ring material's `uOrtho` uniform by reference**.
- The "Efekty" `<details>` panel already hosts the tilt-shift / SSAO checkboxes
  and the 🎨 tone select.

## Design

### A) Where the grade is applied

A GLSL function `applyGrade(vec3)` is injected into both ortho-sampling fragment
shaders:
- **Ring material**: wrap the sampled albedo — `col = applyGrade(col);` —
  immediately after `col = texture2D(uOrtho, orthoUv).rgb;` and **before**
  hillshade, so the relief shading multiplies the graded albedo (natural). Only
  in the ortho branch; the viridis `uShadeMode > 0.5` branch is untouched.
- **Edge ortho material** (`makeEdgeOrthoMaterial`): wrap the albedo before the
  `* uDarken` multiply, so the pedestal section band / seam skirts stay
  colour-consistent with the top surface.

Sky, fog, parcel lines, POI markers, bloom, and the `?tone=` operator are all
unaffected — they are not ortho-sampling terrain materials.

### B) Shared uniforms (no recompile on switch)

A single shared uniform object `gradeUniforms`, referenced by every ring mesh's
material and by `makeEdgeOrthoMaterial` (the same by-reference sharing already
used for `uOrtho`). Switching a preset writes new scalar values into it — no
shader recompile, instant. Members:

```
uGradeOn     float   0 = bypass (default), 1 = apply
uGExposure   float   stops
uGTemp       float   R += t, B -= t   (warm > 0)
uGTint       float   G -= t           (magenta > 0)
uGContrast   float   multiplier around mid-grey (1 = none)
uGShadows    float   lift in dark regions
uGHighlights float   pull in bright regions
uGWhites     float   white-point shift
uGBlacks     float   black-point shift
uGVibrance   float   low-saturation-weighted sat boost
uGSaturation float   global sat multiplier (1 = none)
```

### C) Grade math (pointwise, in sRGB/gamma space)

The ortho is tagged `orthoTex.colorSpace = THREE.SRGBColorSpace`
(`heightfield/index.html:990`), and the ortho tiers are KTX2 (compressed sRGB),
which the GPU decodes on sample — so `texture2D(uOrtho, …)` returns **linear**
colour at the injection site. Lightroom slider intuition is defined in gamma
space, so `applyGrade` decodes to sRGB, grades, and re-encodes to linear, so the
preset numbers map to `docs/notes/2026-05-16-ortofoto-color-grading.md`.

> **Implementation check (for the plan):** the linear-sample assumption holds
> for the KTX2 (hardware-decoded) tiers, which is what ships. If a non-compressed
> ortho fallback is ever sampled without hardware sRGB decode, the sample is
> raw-sRGB and the internal decode/encode in `applyGrade` would double-apply —
> confirm the live texture path before relying on it.

Reference implementation (the plan finalises exact GLSL; ordering is fixed):

```glsl
float _luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
vec3 applyGrade(vec3 lin) {
  if (uGradeOn < 0.5) return lin;                    // default: zero work
  vec3 c = pow(clamp(lin, 0.0, 1.0), vec3(1.0 / 2.2)); // linear → ~sRGB
  // 1) white balance
  c.r += uGTemp; c.b -= uGTemp; c.g -= uGTint;
  // 2) exposure
  c *= exp2(uGExposure);
  // 3) contrast around mid-grey
  c = (c - 0.5) * uGContrast + 0.5;
  // 4) tonal regions via luma masks
  float L = _luma(clamp(c, 0.0, 1.0));
  float darkM  = 1.0 - smoothstep(0.0, 0.5, L);
  float lightM = smoothstep(0.5, 1.0, L);
  c += uGShadows * darkM + uGBlacks * darkM;
  c += uGHighlights * lightM + uGWhites * lightM;
  // 5) vibrance (weighted to low-sat pixels) then global saturation
  c = clamp(c, 0.0, 1.0);
  float g = _luma(c);
  float mx = max(c.r, max(c.g, c.b));
  float mn = min(c.r, min(c.g, c.b));
  float sat = mx - mn;                               // 0..1 cheap saturation
  c = mix(vec3(g), c, 1.0 + uGVibrance * (1.0 - sat));
  c = mix(vec3(g), c, uGSaturation);
  return pow(clamp(c, 0.0, 1.0), vec3(2.2));         // ~sRGB → linear
}
```

`uGShadows`/`uGBlacks` (and `uGHighlights`/`uGWhites`) share a luma mask here —
a deliberate simplification of Lightroom's separate tonal bands, adequate for a
fixed-preset look and easy to tune.

### D) Presets (starting values — tune on device)

```
const GRADE_PRESETS = {
  off:     { on: 0, exposure: 0,    temp: 0,    tint: 0,    contrast: 1.00, shadows: 0,    highlights: 0,     whites: 0,    blacks: 0,     vibrance: 0,    saturation: 1.00 },
  warm:    { on: 1, exposure: 0.05, temp: 0.04, tint: 0.02, contrast: 1.14, shadows: 0.10, highlights: -0.12, whites: 0.06, blacks: -0.08, vibrance: 0.24, saturation: 1.05 },
  neutral: { on: 1, exposure: 0,    temp: 0,    tint: 0,    contrast: 1.08, shadows: 0.05, highlights: -0.06, whites: 0,    blacks: -0.04, vibrance: 0.12, saturation: 1.03 },
  vivid:   { on: 1, exposure: 0.05, temp: 0.02, tint: 0,    contrast: 1.18, shadows: 0.06, highlights: -0.10, whites: 0.06, blacks: -0.06, vibrance: 0.34, saturation: 1.10 },
};
```

### E) UI + data flow

- A select `#grade-select` in the "Efekty" panel, label `🎨 Vzhled`, options:
  `Vyp` (value `off`, selected) / `Teplý` (`warm`) / `Neutrální` (`neutral`) /
  `Živý` (`vivid`).
- `change` handler: look up `GRADE_PRESETS[value]`, write each field into the
  shared `gradeUniforms` (`uGradeOn` from `.on`, etc.). No recompile.
- The shared `gradeUniforms` object is created once and referenced (not copied)
  into every ring material's `uniforms` and into `makeEdgeOrthoMaterial`, so LOD
  tier swaps, `disposeAllAssets`/reload, and minimap/ortho-capture paths all see
  the current grade with zero extra wiring.

### F) Interactions

- Independent of `?tone=` (tone is a whole-frame operator at OutputPass; grade is
  albedo before lighting) — they stack cleanly.
- Hillshade multiplies the graded albedo — intended.
- Viridis flat-shade mode: grade skipped (its branch never calls `applyGrade`).
- Minimap / ortho captures (plain `renderer.render`) show the grade — intended,
  consistent with the subtle-hillshade side effect.

## Out of scope

Clarity / texture / dehaze (spatial filters needing a blur prepass), per-tile
histogram auto-tune, server-side bake into `cache/ortofoto_*`, user-facing
sliders, `?grade=` URL param, per-location persisted preset.

## Performance

Default (Vyp): `uGradeOn = 0` → `applyGrade` returns immediately; one uniform
branch per terrain fragment, effectively free. When on: ~20–30 ALU ops per
terrain fragment, no prepass, no extra render target — mobile-safe. Sky / overlay
materials pay nothing (not touched).

## Testing

- Syntax gate: `awk` module-script extraction + `node --check`.
- On device (`slopne` + a varied location): switch Vyp → Teplý → Neutrální →
  Živý live; verify only the ground changes (sky / parcel lines / POI / bloom
  unchanged); verify Vyp is pixel-identical to pre-change; verify no perceptible
  fps change when off; verify viridis mode unaffected; tune the preset constants
  by eye and commit the tuned values.
