# LOD Phase 5 — Atmosphere Polish (sun-tinted fog + Mie + time-of-day presets)

**Goal:** Improve perceived realism of sky / horizon by (a) tinting fog with sun direction, (b) tuning Mie scattering on `THREE.Sky` for a believable sun halo, and (c) shipping 4 time-of-day presets that bundle sky + fog + lights into named moods.

**Architecture:** Single new module `viewer-atmosphere.js` (sibling of `viewer-realtor-overlay.js`) that the base viewer try-imports the same way it does the overlay. The module owns: a `sunDirection` vec3, a custom `onBeforeCompile` hook that injects sun-aware fog into every terrain `MeshBasicMaterial`, a `setPreset(name)` API, and the preset table. UI is one `<select>` in the drone video panel (existing) + an "Atmosféra" button next to the existing right-bar buttons in inspector mode (toggle through presets on click).

**Tech stack:** Three.js r170 (existing), no new deps.

**Why this design:**
- Module pattern matches Phase 2 viewer-realtor-overlay extraction (well-rehearsed in this codebase).
- `onBeforeCompile` is the standard Three.js way to extend a built-in material — survives `mesh.material.map = tex` reassignments because we patch the shader, not the JS material instance.
- Discrete presets > continuous time-of-day slider for this product: realtor wants 1-click "good light", not a 24h cycle. Continuous slider can come later if asked.

---

### Task 1: Atmosphere module skeleton + sun direction

**Files:**
- Create: `viewer-atmosphere.js`
- Modify: `hnojice_multi.html`, `hnojice_lod_multi.html`, `gen_multitile.py` (template)

- [ ] **Step 1: Create module with sun state + try-import wiring**

```javascript
// viewer-atmosphere.js
import * as THREE from 'three';

const state = {
  sunDirection: new THREE.Vector3(0, 1, 0),   // pointing FROM ground TO sun
  fogWarm: new THREE.Color(0xd8b890),
  fogCool: new THREE.Color(0xb0c4d8),
  presetName: 'noon',
};

export function init({ scene, sky, dirLight, fog }) {
  state.scene = scene;
  state.sky = sky;
  state.dirLight = dirLight;
  state.fog = fog;
  applyPreset('noon');
}

export function getSunDirection() { return state.sunDirection; }
export function getState() { return state; }
```

- [ ] **Step 2: Wire try-import in both viewers + template**

In `hnojice_multi.html` (and `_lod_multi.html`, and the template in `gen_multitile.py`), after the THREE.Sky setup add:

```javascript
let atmosphereModule = null;
try {
  atmosphereModule = await import('./viewer-atmosphere.js');
  atmosphereModule.init({ scene, sky, dirLight: dir, fog: scene.fog });
} catch (e) {
  console.info('viewer-atmosphere.js not present, using default sky');
}
```

- [ ] **Step 3: Commit**

```bash
git add viewer-atmosphere.js hnojice_multi.html hnojice_lod_multi.html gen_multitile.py
git commit -m "feat(atmosphere): module skeleton with sun direction state"
```

---

### Task 2: Sun-aware fog via onBeforeCompile

**Files:**
- Modify: `viewer-atmosphere.js`, `hnojice_multi.html`, `hnojice_lod_multi.html`, `gen_multitile.py` (template)

- [ ] **Step 1: Add fog shader patcher to module**

```javascript
// viewer-atmosphere.js — append
const FOG_PATCH = {
  uniforms: `
    uniform vec3 uSunDir;
    uniform vec3 uFogWarm;
    uniform vec3 uFogCool;
  `,
  fragMain: `
    // Replace standard fog mix: blend warm (toward sun) vs cool (away) by view-sun dot.
    vec3 viewDirWorld = normalize(vWorldPosition - cameraPosition);
    float sunBlend = clamp(dot(viewDirWorld, normalize(uSunDir)) * 0.5 + 0.5, 0.0, 1.0);
    vec3 fogColor = mix(uFogCool, uFogWarm, pow(sunBlend, 4.0));
    gl_FragColor.rgb = mix(gl_FragColor.rgb, fogColor, fogFactor);
  `,
};

export function patchMaterial(material) {
  material.onBeforeCompile = (shader) => {
    shader.uniforms.uSunDir = { value: state.sunDirection };
    shader.uniforms.uFogWarm = { value: state.fogWarm };
    shader.uniforms.uFogCool = { value: state.fogCool };
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vWorldPosition;')
      .replace('#include <fog_vertex>',
        '#include <fog_vertex>\nvWorldPosition = (modelMatrix * vec4(position, 1.0)).xyz;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vWorldPosition;\n' + FOG_PATCH.uniforms)
      .replace('#include <fog_fragment>', FOG_PATCH.fragMain);
    material.userData.atmosphereShader = shader;
  };
}
```

- [ ] **Step 2: Call patchMaterial on every terrain mesh material**

In both viewers, inside `setupMesh(geometry)`:

```javascript
const mat = new THREE.MeshBasicMaterial({ vertexColors: false, side: THREE.DoubleSide });
if (atmosphereModule) atmosphereModule.patchMaterial(mat);
```

- [ ] **Step 3: Manual verify in browser**

Run server, open `hnojice_lod_multi.html`, orbit to look toward sun and away from sun. The far horizon should now be visibly warmer in the sun direction and cooler away.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(atmosphere): sun-direction-tinted fog via onBeforeCompile"
```

---

### Task 3: Time-of-day presets + UI

**Files:**
- Modify: `viewer-atmosphere.js`, `hnojice_multi.html`, `hnojice_lod_multi.html`, `gen_multitile.py` (template)

- [ ] **Step 1: Define presets**

```javascript
// viewer-atmosphere.js — append
const PRESETS = {
  noon: {
    label: 'Poledne',
    sunElevation: 55, sunAzimuth: 180,
    turbidity: 8, rayleigh: 1.5, mieG: 0.7, mieCoeff: 0.005,
    fogDensity: 0.00010, fogWarm: 0xc8d4dc, fogCool: 0xb0c4d8,
    dirIntensity: 0.6, ambIntensity: 0.7,
  },
  afternoon: {
    label: 'Odpoledne',
    sunElevation: 25, sunAzimuth: 220,
    turbidity: 10, rayleigh: 2.0, mieG: 0.8, mieCoeff: 0.005,
    fogDensity: 0.00012, fogWarm: 0xd8c098, fogCool: 0xa8b8d0,
    dirIntensity: 0.7, ambIntensity: 0.6,
  },
  golden: {
    label: 'Zlatá hodina',
    sunElevation: 8, sunAzimuth: 245,
    turbidity: 14, rayleigh: 3.0, mieG: 0.86, mieCoeff: 0.008,
    fogDensity: 0.00014, fogWarm: 0xf0c080, fogCool: 0x90a8c8,
    dirIntensity: 0.8, ambIntensity: 0.45,
  },
  dusk: {
    label: 'Soumrak',
    sunElevation: -2, sunAzimuth: 260,
    turbidity: 18, rayleigh: 4.0, mieG: 0.9, mieCoeff: 0.012,
    fogDensity: 0.00016, fogWarm: 0xc06868, fogCool: 0x506890,
    dirIntensity: 0.4, ambIntensity: 0.3,
  },
};

export function applyPreset(name) {
  const p = PRESETS[name];
  if (!p) return;
  state.presetName = name;
  // Sun position
  const phi = THREE.MathUtils.degToRad(90 - p.sunElevation);
  const theta = THREE.MathUtils.degToRad(p.sunAzimuth);
  state.sunDirection.setFromSphericalCoords(1, phi, theta);
  // Sky uniforms
  const u = state.sky.material.uniforms;
  u.turbidity.value = p.turbidity;
  u.rayleigh.value = p.rayleigh;
  u.mieCoefficient.value = p.mieCoeff;
  u.mieDirectionalG.value = p.mieG;
  u.sunPosition.value.copy(state.sunDirection);
  // Fog
  state.fog.density = p.fogDensity;
  state.fogWarm.setHex(p.fogWarm);
  state.fogCool.setHex(p.fogCool);
  // Lights
  state.dirLight.position.copy(state.sunDirection).multiplyScalar(500);
  state.dirLight.intensity = p.dirIntensity;
  // Ambient: find scene's AmbientLight and set intensity
  state.scene.traverse(o => { if (o.isAmbientLight) o.intensity = p.ambIntensity; });
}

export function listPresets() {
  return Object.entries(PRESETS).map(([k, v]) => ({ key: k, label: v.label }));
}
```

- [ ] **Step 2: Inspector UI button — toolbar entry**

In both viewers, in the right-side toolbar (next to Parcely button), add:

```html
<button id="atmosphere-btn" class="toolbar-btn" title="Atmosféra (klik prochází presety)">☀️ Poledne</button>
```

```javascript
document.getElementById('atmosphere-btn').addEventListener('click', () => {
  if (!atmosphereModule) return;
  const presets = atmosphereModule.listPresets();
  const cur = atmosphereModule.getState().presetName;
  const idx = presets.findIndex(p => p.key === cur);
  const next = presets[(idx + 1) % presets.length];
  atmosphereModule.applyPreset(next.key);
  document.getElementById('atmosphere-btn').textContent = '☀️ ' + next.label;
});
```

- [ ] **Step 3: Drone video panel selector**

In the video panel HTML (in `viewer-realtor-overlay.js`'s `buildVideoPanel`), add a `<select>` for atmosphere, defaulting to current preset. When changed, call `applyPreset` and snapshot in the recorded video config.

- [ ] **Step 4: Manual verify all 4 presets**

Run server, cycle Poledne → Odpoledne → Zlatá → Soumrak via toolbar button. Confirm:
- Sun position visibly shifts in sky
- Fog warmth changes (Poledne neutral, Zlatá orange, Soumrak red-blue split)
- Directional light intensity drops at dusk (shadows softer)
- Sky color believable in each (no green casts or fluorescent yellows)

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(atmosphere): 4 time-of-day presets + inspector toolbar + video selector"
```

---

## Out of scope (defer to Phase 6 if asked)

- Full custom atmosphere shader from Maxime Heckel article (LUTs, Hillaire's paper). Diminishing returns vs effort for inspection use case.
- Continuous time-of-day slider with hour/minute input.
- Per-location sun azimuth from latitude/longitude + date.
- Star field / moon for dusk preset.
- Volumetric clouds.
