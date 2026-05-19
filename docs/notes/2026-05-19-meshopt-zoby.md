# Meshopt renderuje zubaté lines, Draco ne — root-cause hledání

**RESOLVED 2026-05-19** — fix `-noq` flag do gltfpack. Default output
`gltfpack -cc -vpf -vtf -kv` přidával `KHR_mesh_quantization` + EXPONENTIAL
attribute filter i přes `-vpf/-vtf` (Float32 atributy). Ten filter
renderoval visible zubatost na high-contrast ortofoto líníh.

Finální command: `gltfpack -i snapped.glb -o out.glb -cc -vpf -vtf -kv -noq`

Output `extensionsUsed = ['EXT_meshopt_compression']` only (no
KHR_mesh_quantization). Cena: raw GLB 19 → 40 MB (2×), brotli wire 2.7 →
6.7 MB (~2.5×). **Stále 3× menší než Draco (20 MB brotli)** + meshopt
decode 5-10× rychlejší.

Hypothesis o GPU mipmap LOD selection (níže) byla **mimo** — viník byla
metadata na úrovni gltfpack output formátu.

---

## Original problem statement (kept for reference)

**Stav 2026-05-19.** Pipeline `inzerator/v2.html` přepnul z Draco na meshopt
(gltfpack 0.24.0) pro 10× menší wire size. Vizuálně se objevily **zubaté /
zigzag pattern na pravidelných featur** (ploty, hrany střech, dokonce trace
lodi na hladině jezera). Stejný source mesh, stejná precision, ale Draco
vykresluje čistě a meshopt zubatě. Hledá se důvod a fix.

## Pipeline

### Source mesh

`gen_detail.py` produkuje **uncompressed GLB** s:

- `POSITION`: float32 vec3, range `[-half, +half]` v X a Z (1000 m square),
  Y je elevace v metrech (range ~170 m pro inner)
- `TEXCOORD_0`: float32 vec2, range `[0, 1]` (WGS84 lon/lat normalized
  přes mesh bbox)
- `indices`: uint32 (4 M faces × 3)
- Žádný `NORMAL`, žádný `material` binding na primitive
- Step `0.5 m` → 4 M verts pro 1×1 km inner mesh

DSM zdroj je ČÚZK SM5 (0.5 m horizontal sampling). Y values mají
~5-10 mm sensor noise per cell (verified — source mesh má 580 546
unikátních Y values v rozsahu 173 m).

### Komprese

**Draco varianta** (funguje vizuálně):

```python
DracoPy.encode(positions, faces=indices, tex_coord=uvs,
               quantization_bits=14, compression_level=7)
```

- `quantization_bits=14` na pozice → 2 781 unique Y values po decode
- Wire size per inner: ~30 MB raw glb, ~20 MB brotli q=11

**Meshopt varianta** (fixovaná po 2026-05-19 A/B):

```bash
# Step 1: snap positions to 14-bit uniform-cube grid in Python (bit-exact
# equivalent of Draco's quant_pos=14 dequantize)
levels = (1 << 14) - 1
bbox_min = pos.min(axis=0); bbox_max = pos.max(axis=0)
span = float((bbox_max - bbox_min).max())  # uniform cube
snapped = round((pos - bbox_min) / span * levels) * span / levels + bbox_min

# Step 2: meshopt encode of float32 snapped positions, with gltfpack
# quantization/attribute filters disabled
gltfpack -i snapped.glb -o out.glb -cc -vpf -vtf -kv -noq
```

- `-vpf` = float32 positions (no quantization at this step; lossless
  re-encode of already-snapped values)
- `-vtf` = float32 UVs
- `-kv` = keep vertex attributes even if not material-referenced
- `-cc` = compact encoder
- `-noq` = disables `KHR_mesh_quantization` and gltfpack's EXPONENTIAL
  attribute filters; this removed the Kamyk inner zubatost in A/B testing
- Wire size per inner: ~40 MB raw glb with `-noq` (vs ~19 MB for zubaté
  default meshopt), brotli still much smaller than Draco
  than Draco)

### Equivalence verification

Decoded both Draco and snap-meshopt outputs, compared position values:

| | Draco | snap+meshopt |
|---|---|---|
| Vertex count | 4,012,001 | 4,012,005 (Draco dedupes 4 dupes) |
| Unique Y values | **2,781** | **2,781** |
| Y std within ±1 m of median | 572.2 mm | 572.2 mm |

**Position values are bit-exact equivalent.** Same 14-bit grid, same
quantized floats. Yet rendering differs.

## Viewer

`v2.html` uses Three.js r170 with:

```js
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.toneMapping = THREE.LinearToneMapping;
renderer.toneMappingExposure = 1.0;

const composer = new EffectComposer(
  renderer,
  new THREE.WebGLRenderTarget(innerWidth, innerHeight, {
    type: THREE.HalfFloatType,
    samples: 4,    // MSAA 4× on composer FBO
  }),
);
composer.addPass(new RenderPass(scene, camera));
composer.addPass(new UnrealBloomPass(/*...*/));
composer.addPass(new OutputPass());

const gltfLoader = new GLTFLoader();
gltfLoader.setMeshoptDecoder(MeshoptDecoder);
gltfLoader.setDRACOLoader(dracoLoader);   // also registered

// Per detail:
gltfLoader.load(`tiles_v2_${REGION}/${detail.glb_url}`, (gltf) => {
  let m = gltf.scene.children[0];
  // m.geometry: Float32 positions (-500..+500, snapped), Float32 UVs (0..1)
  const mat = new THREE.MeshBasicMaterial({
    side: THREE.DoubleSide,
    polygonOffset: true,
    polygonOffsetFactor: 0,
    polygonOffsetUnits: -(i + 1) * 32,
    transparent: true,
    opacity: 0,
  });
  const mesh = new THREE.Mesh(m.geometry, mat);
  mesh.position.set(offX, 0, offZ);
  mesh.scale.z = -1;       // scene +Z = world -Y convention
  mesh.renderOrder = 1 + i;
  scene.add(mesh);

  // Texture (KTX2 UASTC for closeup, PNG for inner):
  ktx2Loader.load(textureUrl, (tex) => {
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = renderer.capabilities.getMaxAnisotropy();   // typically 16
    mat.map = tex;
    mat.needsUpdate = true;
  });
});
```

Detail meshes (outer, closeup, inner) stack via:
- `renderOrder` 1, 2, 3 (largest LOD drawn first)
- `polygonOffsetUnits` -32, -64, -96 (closer to camera per LOD step)
- Fragment-shader `discard` in next-smaller LOD's XZ bbox (via `onBeforeCompile`)

## Symptom

Identical mesh positions, identical UVs, identical texture. Yet:

- **Draco-encoded inner**: smooth ortho rendering, fence lines straight
- **Meshopt-encoded inner**: visible **zigzag/staircase** on:
  - Long straight fences (5-10 cm wide in ortho, ~10 m apart)
  - Roof edges of buildings
  - Asphalt road edges
  - **Boat wake trail on a lake surface** (mesh geometrically flat after
    Y-snap; ortho texture has straight wake line; yet renders zigzag)
  - General: "anything periodic / regular" per user

User-attached screenshot of boat wake clearly shows zigzag aliasing of
what's a straight high-contrast line in the orthophoto, projected onto a
mesh region where Y is uniformly snapped.

## What's been ruled out

- ❌ Position precision (Draco quant=14 ≡ snap-then-vpf, both 2 781
  unique Y values, same std)
- ❌ DSM source noise (snap removes 5-10 mm noise to uniform 6 cm grid,
  same as Draco)
- ❌ Camera angle (same camera, same orbit, both encoders compared
  side-by-side)
- ❌ Texture (same ortho fetched from `/proxy/ortofoto`, KTX2 UASTC for
  closeup at 4096², PNG for inner at 8192²)
- ❌ MSAA (composer has `samples: 4`)
- ❌ Anisotropy (max GPU anisotropy applied)
- ❌ LOD overlap (polygonOffset + shader discard verified clean both
  encoders)
- ✅ Root trigger found: gltfpack default output with `KHR_mesh_quantization`
  / EXPONENTIAL attribute filters produces zuby; adding `-noq` removes the
  artifact while keeping `EXT_meshopt_compression`.

## Superseded hypothesis: GPU mipmap LOD selection sensitivity to triangle order

Both Draco and meshopt reorder triangles for vertex-cache efficiency
post-encode. The **specific reorder algorithm differs**:

- Draco: uses Tipsify (Sander et al.) for vertex cache optimization
- Meshopt: uses meshoptimizer's own algorithm

For a regular grid mesh (gen_detail emits row-major 2001×2001 with
T1=`[i, i+n, i+1]` / T2=`[i+1, i+n, i+n+1]` per quad), the post-encode
triangle order is **different per encoder** even though the resulting
geometry is bit-exact identical.

WebGL/GLSL fragment shader computes mipmap LOD per **2×2 quad of
fragments** using `dFdx(uv)` and `dFdy(uv)`. When the quad straddles a
triangle edge, the UV derivatives are computed from adjacent fragments
that may belong to different triangles. With identical triangle topology
but **different triangle drawing order**, the GPU's per-quad
discontinuity pattern can shift — different fragments end up at the
"edge crossing" position → different mipmap level selected per pixel →
visible texture pattern.

This explains why the artefact:

- Appears on regular/periodic features (high-contrast texel content
  where mipmap level affects sampling)
- Doesn't depend on mesh precision (positions are identical, just
  rendered in different order)
- Disappears with Draco (different reorder happens to keep adjacent
  fragments more correlated in mipmap LOD)

## What we tried that didn't help

1. **meshopt `-c` instead of `-cc`** (lighter encoder): identical visual
   result
2. **meshopt `-vpf` (no position quant)** vs **snap-then-vpf**: snap
   helps with fence-step DSM noise; doesn't fix the boat-wake-on-water
   case (mesh is geometrically uniform there but still zubatě)
3. **meshopt `-vp 14` (KHR_mesh_quantization, ushort positions, node TRS
   dequantize)**: requires viewer-side bake (12 M-iter loop on main
   thread × 3 detail meshes); **bricked Chrome** during load on the
   user's Mac, never visible
4. **`gen_detail --smooth 1.5` and `--smooth 3.0`** to filter DSM Y
   noise: helps slightly on fences but creates other artefacts (walls
   become fuzzy ridges)
5. **Per-axis vs uniform-cube snap**: uniform-cube (matching Draco)
   produces correct equivalent values; doesn't fix render

## Open questions for the next debugger

### 2026-05-19 result: `-noq` fixes Kamyk inner

Confirmed on the public/default Kamyk URL after swapping only
`details/inner.glb`:

- `inner.meshopt.glb` (`gltfpack -cc -vpf -vtf -kv`) → zubaté
- `inner.meshopt-noq.glb` (`gltfpack -cc -vpf -vtf -kv -noq`) → zuby nejsou

So the artifact is not Draco-vs-meshopt geometry precision and not meshopt
index compression alone. The bad path is gltfpack's default quantization /
EXPONENTIAL attribute filter metadata even when `-vpf -vtf` asks for Float32
attributes. Pipeline fix: keep pre-snap, add `-noq`.

`v2.html` also has a viewer-side A/B switch for detail textures:

```text
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&textureFilter=base
```

`textureFilter=base` sets detail ortofoto textures to:

```js
tex.generateMipmaps = false;
tex.minFilter = THREE.LinearFilter;
tex.magFilter = THREE.LinearFilter;
tex.anisotropy = 1;
```

Default remains normal mipmapped rendering (`textureFilter=mip` / omitted).
This is now secondary; `-noq` fixed the visible inner artifact without needing
to disable mipmaps.

There is also a layer isolator:

```text
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&onlyDetail=closeup
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&onlyDetail=outer
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&onlyDetail=inner
```

Kamyk test state after the confirmed A/B: `details/inner.glb` has been swapped
to the clean `inner.meshopt-noq.glb`; `details/inner.draco.glb` is the saved
Draco backup.

For inner-only meshopt A/B without overwriting the current Draco inner:

```text
# gltfpack -cc -vpf -vtf -kv (confirmed zubaté)
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&innerGlb=details/inner.meshopt.glb

# gltfpack -cc -vpf -vtf -kv -noq (confirmed clean)
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&innerGlb=details/inner.meshopt-noq.glb

# gltfpack -vpf -vtf -kv -noq without EXT_meshopt compression
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&innerGlb=details/inner.gltfpack-no-compress.glb

# Combine with base texture sampling if needed
http://localhost:8080/v2.html?region=kamyk-nad-vltavou&innerGlb=details/inner.meshopt-noq.glb&textureFilter=base
```

Files generated for this A/B:

- `tiles_v2_kamyk-nad-vltavou/details/inner.meshopt.glb` — 19 MB
- `tiles_v2_kamyk-nad-vltavou/details/inner.meshopt-noq.glb` — 40 MB
- `tiles_v2_kamyk-nad-vltavou/details/inner.gltfpack-no-compress.glb` — 168 MB

`inner.meshopt-noq.glb` still uses `EXT_meshopt_compression`, but its
accessor bufferViews have no `filter: EXPONENTIAL` and the GLB no longer
uses `KHR_mesh_quantization`.

`inner.gltfpack-no-compress.glb` keeps gltfpack's mesh rewrite/reorder but
removes `EXT_meshopt_compression`; it is no longer the critical test because
the compressed `-noq` variant is already clean.

Important caveat: triangle draw order alone should not normally affect the
raster result for a non-overlapping triangle mesh; GPU derivatives at triangle
edges use helper fragments for the same primitive. So the "triangle reorder →
different 2×2 quad LOD" story is plausible only if some other discontinuity
is present (overlap, degenerates, UV/topology change, precision issue, or
implementation-specific sampling behaviour).

1. **Can the mipmap-LOD theory be confirmed?** Try
   `tex.minFilter = THREE.LinearFilter; tex.generateMipmaps = false;` on
   the detail textures with meshopt-encoded meshes. If zubatě disappears,
   confirms the hypothesis. (Loses far-distance anti-aliasing as a
   trade.)

2. **If LOD selection is the cause, what's a render-side mitigation?**
   - Force a specific mipmap level (uniform `lod` in fragment shader)?
   - Use `texture2DLodEXT` / `textureLod` with computed LOD?
   - Adjust `tex.anisotropy` further (we're already at GPU max)?

3. **Can meshopt's triangle reorder be controlled / disabled?**
   - `gltfpack -noq` is confirmed to disable `KHR_mesh_quantization` and
     EXPONENTIAL attribute filters; it fixes Kamyk inner while keeping
     `EXT_meshopt_compression`.
   - Alternative: encode via `meshopt_encodeIndexBuffer` directly,
     skipping `meshopt_optimizeVertexCache` — but this leaves only the
     raw meshopt codec without gltfpack's wrapping

4. **Does the same artefact appear with a vanilla three.js example?**
   Take a regular grid mesh with a high-contrast texture (e.g. checker
   pattern), encode via gltfpack -cc -vpf -vtf, render in r170. Compare
   to same mesh raw or Draco-encoded.

5. **Is it a known meshoptimizer issue?** zeux/meshoptimizer GitHub may
   have prior reports for "texture aliasing" / "mipmap" / "regular grid
   mesh" + meshopt.

## Reference files

- `meshopt_compress_glb.py` — current snap-then-vpf wrapper
- `draco_compress_glb.py` — reference (known-good) Draco encoder
- `v2.html` — viewer (search `gltfLoader.setMeshoptDecoder` /
  `setDRACOLoader`)
- `tiles_v2_kamyk-nad-vltavou/_orig_uncompressed/inner.glb` — source GLB
  for reproduction
- `tiles_v2_kamyk-nad-vltavou/details/inner.glb` — currently Draco-
  encoded after user requested rollback; replace with meshopt to repro
- `/tmp/kamyk-draco14.glb` — known-good Draco for A/B testing
- User screenshots showing the artefact: see git annex / commit history
  near commits `bcf298c`..HEAD

## How to reproduce

```bash
# Generate Draco reference
python3 draco_compress_glb.py \
  tiles_v2_kamyk-nad-vltavou/_orig_uncompressed/inner.glb \
  /tmp/draco-inner.glb 14

# Generate meshopt suspect
python3 meshopt_compress_glb.py \
  tiles_v2_kamyk-nad-vltavou/_orig_uncompressed/inner.glb \
  /tmp/meshopt-inner.glb

# Swap into details/ alternately, hard-refresh
# https://localhost:8080/v2.html?region=kamyk-nad-vltavou
# Compare same camera angle on:
# - boat wake on the Vltava
# - fence outlines around fenced gardens
# - roof edges of buildings
```

Server `server.py` serves brotli-compressed GLBs (q=11) on `Accept-Encoding: br`.
