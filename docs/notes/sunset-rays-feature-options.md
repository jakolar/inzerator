# Sunset / golden-hour + paprsky — feature options

## Status

**Parked** 2026-05-08. Discussed during drone-video tool development.
User wants to come back to it later. Below are 3 complexity tiers + 2
deployment patterns evaluated, plus implementation hints for the
recommended tier so future-self can pick this up cleanly.

## Goal

Add a "sunset / golden-hour atmosphere" mode to the Hnojice 3D
viewer, optionally with visible god rays (volumetric sun shafts).
Real-estate marketing benefit: footage at golden-hour looks
dramatically better than midday flat lighting.

## Tension specific to this project

The ortofoto texture **already has baked lighting** from when ČÚZK
photographed the area (typically clear midday). Adding our own
directional "sun" light in three.js will not actually re-light the
texture — the photo already has its highlights and shadows baked in.

Practical implications:
- A "warm color overlay" on the texture (multiplicative tint) is the
  most honest cheat: visually shifts to golden tones without lying
  about geometry-vs-light direction.
- Custom shader could differentiate flat (terrain) vs vertical (DSM
  building edges) surfaces and apply tint only to verticals — those
  are most visually affected by horizontal sun rays. Mid-effort.
- Anything more ambitious (re-lighting the texture as if it were a
  diffuse albedo map) would require de-shading the input photo, which
  is research territory.

## 3 complexity tiers

### A — Rychlé (~2-3 hours, 1 commit)

**Visual elements:**
- `THREE.DirectionalLight` from sunset angle (azimuth ~270° = west,
  elevation ~15°), warm color `0xff8a3a`
- Multiplicative warm color overlay on ortofoto materials (custom
  fragment shader on existing `MeshBasicMaterial` or replace with a
  `ShaderMaterial`). Tint color `0xffaa66`.
- Sky background gradient — purple/orange near horizon, deep blue
  zenith. Use `THREE.Sky` from `examples/jsm/objects/Sky.js` for
  cheap-looking atmospheric gradient, OR plain large dome with
  vertex-color gradient.
- `THREE.Lensflare` from `examples/jsm/objects/Lensflare.js` — sun
  glare + lens flare when camera looks toward sun.

**Result:** Atmospheric, recognisably "sunset", but **no visible
paprsky / god rays in the air**.

**Where in code:**
- New helper `applySunsetMode(scene, on)` that toggles all the above.
- `THREE.Sky` requires `renderer.toneMappingExposure` tuning and
  `outputColorSpace = SRGBColorSpace` (already true).

### B — Realistic (~1-2 days, ~3 commits)

A) plus:

- **Volumetric god rays** via `EffectComposer` + custom
  `GodRaysShader` pass. Three.js `examples/jsm/postprocessing/` has
  building blocks. Pattern: render scene to texture → apply radial
  blur centered on sun screen-position → composite over original.
- **Cascaded shadow maps** for the 3 km × 3 km Hnojice scene. Default
  shadow map resolution gives blocky shadows at this scale; need 2-3
  cascades to keep edge sharpness near camera.
- **Atmospheric tint shader** on ortofoto — fragment shader picks
  tint amount from `length(worldPos - cameraPos)`, gives more
  orange-red to distant features (mimics atmospheric scattering).
- **Time-of-day slider** in the info panel that drives sun
  azimuth/elevation in real-time. Plus optional `SunCalc` integration
  to default to geographically-accurate Hnojice sunset (49.7°N
  17.2°E, late June ~20:30, late December ~16:00).

**Result:** Looks like a real drone shot at golden hour. Paprsky go
between buildings. Suitable for serious real-estate marketing.

### C — Production cinematic (~1 week+)

B) plus:

- Full Rayleigh + Mie atmospheric scattering shader (existing
  implementations in three.js examples and shadertoy)
- Volumetric clouds with light scattering (raymarched)
- Real weather/sun data via NMHU or OpenWeatherMap
- HDR pipeline with ACES Filmic tonemapping, bloom, glare
- Temporal anti-aliasing for stable god rays across animation frames

**Skip for hobby project.**

## 2 deployment patterns

### P1 — Toggle in left info panel

Checkbox "🌅 Sunset režim" pod "📹 Nahrát aktuální pohled" button.
Click → scene immediately switches to sunset mode (independent of
video). Stays on until toggled off. User can browse the scene in
sunset, take screenshots, etc.

### P2 — Auto preset for video tool

New radio button in video panel: `🌅 Sunset orbit (30s)`. Selecting
it during Preview/Export auto-enables sunset mode for the duration
of the recording, then reverts on completion.

### Recommended: P1 + P2 together

Single commit can do both. The video preset just programmatically
toggles the panel checkbox during recording. Clean factoring.

## Recommended next step

If/when picking this up:

**Start with A + (P1 + P2)** = 1 commit, ~3-4 hours.

Implementation skeleton:

```js
// Module-scope state
let _sunsetMode = false;
let _sunsetLight = null;
let _sunsetSky = null;
let _sunsetLensflare = null;
let _origMaterials = new Map();    // tile mesh uuid → original color/material

function applySunsetMode(on) {
  if (on === _sunsetMode) return;
  _sunsetMode = on;
  if (on) {
    // 1. Sun light
    _sunsetLight = new THREE.DirectionalLight(0xff8a3a, 1.5);
    _sunsetLight.position.set(-1000, 200, 0);  // west, low elevation
    scene.add(_sunsetLight);
    // 2. Sky gradient
    _sunsetSky = createSunsetSky();   // see below
    scene.add(_sunsetSky);
    // 3. Tint ortofoto materials
    for (const m of allMeshes) {
      _origMaterials.set(m.uuid, m.material.color.getHex());
      m.material.color.setHex(0xffaa66);  // warm multiplier
    }
    scene.background = null;            // sky dome covers it
  } else {
    if (_sunsetLight) { scene.remove(_sunsetLight); _sunsetLight = null; }
    if (_sunsetSky)   { scene.remove(_sunsetSky);   _sunsetSky = null;   }
    for (const m of allMeshes) {
      const orig = _origMaterials.get(m.uuid);
      if (orig != null) m.material.color.setHex(orig);
    }
    _origMaterials.clear();
    scene.background = new THREE.Color(0x87ceeb);
  }
}

function createSunsetSky() {
  // Simple radial gradient on a sphere. For better look, use THREE.Sky.
  const geo = new THREE.SphereGeometry(2500, 32, 16);
  const mat = new THREE.ShaderMaterial({
    side: THREE.BackSide,
    uniforms: {
      topColor:    { value: new THREE.Color(0x1a1a3a) },  // deep blue
      midColor:    { value: new THREE.Color(0x4a3a5a) },  // purple
      bottomColor: { value: new THREE.Color(0xff6a3a) },  // orange
    },
    vertexShader: `
      varying vec3 vWorldPosition;
      void main() {
        vWorldPosition = (modelMatrix * vec4(position, 1.0)).xyz;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 topColor;
      uniform vec3 midColor;
      uniform vec3 bottomColor;
      varying vec3 vWorldPosition;
      void main() {
        float h = normalize(vWorldPosition).y;          // -1 (down) .. 1 (up)
        vec3 col;
        if (h < 0.0) {
          col = mix(bottomColor, midColor, smoothstep(-0.3, 0.0, h));
        } else {
          col = mix(midColor, topColor, smoothstep(0.0, 0.4, h));
        }
        gl_FragColor = vec4(col, 1.0);
      }`,
  });
  return new THREE.Mesh(geo, mat);
}
```

Wire-up:
- P1: checkbox in `#info` panel, change handler → `applySunsetMode(checked)`
- P2: in vp-preview/vp-export click handlers, if preset is `'sunsetorbit'`, call `applySunsetMode(true)` before `startVideoTick`. In `_onVideoComplete` and `cancelVideoTick`, restore: if was on for preset reasons, call `applySunsetMode(false)`.

For the new `sunsetorbit` preset, reuse `highorbit` math but maybe slower duration. In `buildCameraPath`:
```js
if (preset === 'sunsetorbit') {
  // Same as highorbit but slower / smaller radius for cinematic feel.
  // Or just delegate: return buildCameraPath('highorbit', subject);
}
```

For Lensflare: use `examples/jsm/objects/Lensflare.js` with a small
texture from `examples/textures/lensflare/lensflare0.png`. CDN URL for
texture should be lazy-loaded only on sunset toggle.

## Out of scope for tier A

- Geographically accurate sun position (defer to B)
- God rays / volumetric scattering (defer to B)
- Time-of-day slider (defer to B)
- Per-surface tint differentiation (defer to B)
- Long shadows from buildings (would require switching from
  MeshBasicMaterial to MeshLambertMaterial; complicates A)

## Cross-references

- THREE.js color space note: `docs/notes/three-js-colorspace-srgb.md`
  — keep `texture.colorSpace = SRGBColorSpace` when adding any
  ShaderMaterial.
- Tile seam gaps note: `docs/notes/tile-seam-gaps-open3d-simplify.md`
  — relevant if sunset shadows expose seams more visibly than
  baseline lighting.
