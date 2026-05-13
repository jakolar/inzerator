# Greenfield Two-Layer Architecture — Panorama Region + Per-Address Detail (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Goal

Rebuild the property-inspection viewer from scratch with two cleanly separated terrain layers:

1. **Region panorama** — one large DMR5G mesh (~30 × 30 km). Loaded once per region. Shared across every property/address inside that region.
2. **Per-address detail** — one small high-detail SM5 mesh (~1 × 1 km) centred on the property. Cheap to regenerate when the user switches addresses.

The two layers never share a vertex. They never sample two data sources into one mesh. The detail mesh **fades its outer ring to match DMR5G** exactly, so the visible boundary with the panorama beneath it is flush (no vertical step, no z-fighting, no stitching code).

All v2 files (`gen_panorama.py`, `gen_detail.py`, `v2.html`, `verify_v2.py`) live alongside v1 (`gen_multitile.py`, `hnojice_*.html`). v1 stays untouched as fossil reference until v2 is verified.

## Decisions locked before this plan executes

These were the painful unresolved ambiguities in earlier work. Locking them up front:

### D1 — Detail/panorama Y boundary handling: **fade-to-DMR5G outer ring**

The detail mesh has an inner core (pure SM5 with building heights) and an outer fade band. In the fade band, Y is a linear blend `(1 − t) · SM5 + t · DMR5G_panorama`, where `t` ramps from 0 at the inner radius to 1 at the outer radius. At the outer edge the detail mesh's Y values are **exactly equal** to the panorama's DMR5G values at the same world positions.

Why this works where polygonOffset doesn't:
- `polygonOffset` is a depth-buffer hack against z-fighting between coplanar faces. It does not move geometry.
- A real ~5 m Y jump at the building-village edge is geometry, not z-fight. polygonOffset would leave the cliff visible.
- The fade ring makes the two layers share Y at the seam by construction.

Parameters:
- Inner core radius (full SM5): `detail_half − fade_width`.
- Fade width: 50 m default (configurable).
- For `detail_half = 500` (1 × 1 km mesh): inner core 450 m × 450 m, fade band the surrounding 50 m strip.

### D2 — Cardinal convention: **scene +Z = world +Y (north)** in all v2 code

v1 used the opposite (`scene +Z = world −Y` via `t["offset_z"] = -(cy - gcy)`). v2 inverts this and is internally consistent. Specifically:

| Where | v1 (kept as fossil) | v2 (new) |
|---|---|---|
| Inner tile `offset_z` formula | `-(cy - gcy)` | `cy - gcy` |
| Panorama `pcy` formula | `grid_cy - offset_z` | `grid_cy + offset_z` |
| UV per-vertex math | `cy - v[2]` | `cy + v[2]` |
| `world_to_native_px` row | `(cy + half) - (cy - local_z)` | `(cy + half) - (cy + local_z)` |
| Data sampling row in `extract_tile` | `patch_ds[r, c]` | `patch_ds[ds_rows-1-r, c]` |
| Footprint snap | `lz = -(sy - cy)` | `lz = sy - cy` |
| Viewer cadastre URL | `wyMin = cy - lzMax` | `wyMin = cy + lzMin` |
| Overlay parcel click | `sy = -localZ + gcy` | `sy = localZ + gcy` |
| Server `_fetch_parcels_area` local Z | `-(sy - gcy)` | `sy - gcy` |
| Server `_api_buildings` local Z | `-(sy - gcy)` | `sy - gcy` |

v2 has its own viewer (`v2.html`), its own copy of the overlay (`viewer-realtor-overlay-v2.js`), and the existing `server.py` gets v2-specific endpoints with the new convention. v1 endpoints stay as they are.

### D3 — Detail mesh is **one GLB**, not 625 inner tiles

The existing 25 × 25 inner-grid GLBs in `tiles_hnojice_lod/` are v1 fossils. v2 generates a single detail GLB per address. Reasons:
- Single mesh = no inter-tile seams inside the detail layer.
- Smaller working set; easier to swap when the user navigates between addresses.
- Fade ring works cleanly on one mesh; would need 4 special-cased edge tiles in the 625-tile layout.

### D4 — Verification is **programmatic**, not visual

After each generator run, `verify_v2.py` checks:
- Mesh face count matches `n² × 2 + skirt_faces` (no `grid_valid`-induced holes).
- Outer edge Y values match DMR5G interpolation within 0.05 m (fade ring did its job).
- Detail mesh outer edge Y matches panorama Y at the same world positions within 0.5 m.
- All vertices have valid (finite, non-zero) Y unless explicitly skirt.
- Cardinal sanity: vertex with the largest world Y has the largest scene Z.

Run `python3 verify_v2.py --region hnojice --detail main` after every regen. The viewer is only opened when verify passes.

### D5 — Per-address workflow lives **in the generator from day one**

`gen_detail.py` takes `--region <name> --slug <name> --center-sjtsk <cx>,<cy>` and writes:
- `tiles_v2_<region>/details/<slug>.glb`
- `tiles_v2_<region>/details/<slug>_meta.json`
- An entry appended to `tiles_v2_<region>/manifest.json` with `{slug, label, glb_url, sjtsk_bbox, wgs_bbox, half, step}`.

Viewer URL: `v2.html?region=hnojice&detail=main`. Reads `tiles_v2_hnojice/manifest.json`, picks the matching detail entry. Future address geocoding lands in a separate plan; for v2 the slug + S-JTSK are passed by hand.

### D6 — Texture mapping uses **WGS84 UVs**, atlas math uses **WGS84 bbox**

The server (`/proxy/ortofoto`) crops the composite XYZ tiles to the requested WGS84 bbox. So the texture image is axis-aligned in WGS84 (lat × lon), not in S-JTSK. UVs per vertex must therefore be normalized over each tile's wgs_bbox:

```python
u = (lon - wgs_lon_min) / (wgs_lon_max - wgs_lon_min)
v = (lat - wgs_lat_min) / (wgs_lat_max - wgs_lat_min)  # V=0 at south
```

This matches what the v1 viewer ended up using after we fixed the S-JTSK-vs-WGS atlas mismatch.

### D7 — rasterio.merge edge handling: **clamp, don't drop**

When `merge(bounds=..., res=step)` returns shape `(n-1, n-1)` for a vertex grid `(n, n)`, vertex sampling clamps `dr, dc` to `data.shape - 1` so the outer row/col reads the last valid pixel instead of falling back to `y = 0`. Combined with D1's fade ring, there is no `grid_valid` mask needed and no faces are dropped.

### D8 — DMR5G fetch strips CRS metadata so it can mix with SM5

For panorama (DMR5G only) this is irrelevant. For detail (SM5 + DMR5G in the same generator process when computing the fade ring), DMR5G is opened separately and only its Y values are sampled — never merged with SM5 in rasterio. So CRS mismatch never arises in v2. The strip-CRS hack from v1 is not needed.

---

## Architecture

```
v2.html?region=hnojice&detail=main
  │
  ├── fetch tiles_v2_hnojice/manifest.json
  │       { region: {panorama_glb, ground_z, sjtsk_bbox, wgs_bbox, half, step},
  │         details: [{slug, glb_url, center_sjtsk, half, step, ground_z, sjtsk_bbox, wgs_bbox}] }
  │
  ├── load region.panorama_glb  → mesh at scene origin, renderOrder = 0
  │   load ortho for region.wgs_bbox at z=12, size=4096 → texture
  │
  ├── load details[detail].glb  → mesh at scene = (cx-cx_region, 0, cy-cy_region), renderOrder = 1
  │   load ortho for detail.wgs_bbox at z=19, size=2048 → texture
  │
  ├── try import viewer-realtor-overlay-v2.js (new-convention port)
  │
  └── THREE.Sky + FogExp2

gen_panorama.py --region hnojice --center-sjtsk -547700,-1107700
gen_detail.py   --region hnojice --slug main --center-sjtsk -547700,-1107700
verify_v2.py    --region hnojice --detail main
```

**Tech stack:** Python 3 (rasterio, numpy, pygltflib, pyproj), Three.js r170 ES modules. No new dependencies.

**Region anchor for Hnojice:** S-JTSK `(-547700, -1107700)`. Panorama half = 15 000 m. Reaches Praděd just barely (28 km from Hnojice, panorama outer at 15 km — Praděd at 28 km is OUTSIDE this panorama). If the user wants Praděd visible, set panorama half to 20 000 m or larger; the trade-off is more verts (~ `(2 · half / step + 1)²`).

Confirmed panorama spec: half = 15 000 m, step = 50 m → 601 × 601 = 361 k verts, ~16 MB GLB. Outer edge at scene ±15 000 m. **Note:** Praděd (Jeseníky) is at 28 km from Hnojice — beyond this 15 km panorama. If the user later wants it visible, bump `--half` to 30 000.

Confirmed detail spec: half = 300 m (= 600 m square), step = 1 m, fade = 50 m → 601 × 601 = 361 k verts, ~6 MB GLB. Inner core (pure SM5) = 250 m × 250 m, fade ring = the outer 50 m annulus.

**Cache symlink note:** `cache/` is a symlink to `../gtaol/cache`. Both `cache/dmr5g_v2/` (panorama-scale DMR5G) and the existing `cache/dmpok_tiff_*/` (SM5) live there. Side-effect: gtaol gains a `dmr5g_v2` subdir. Acceptable.

---

### Task 1: `gen_panorama.py` — region panorama generator

**Files:**
- Create: `gen_panorama.py`

- [ ] **Step 1: Module scaffold + DMR5G fetch (with CRS=EPSG:5514)**

```python
"""Generate one continuous panorama mesh for a v2 region.

DMR5G only; one GLB per region. Cardinal: scene +Z = world +Y (north).
"""
import argparse, json, struct, urllib.request
from pathlib import Path
import numpy as np
import pygltflib
import rasterio
from pyproj import Transformer

SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)

def fetch_dmr5g(cx, cy, half, step, cache_dir="cache/dmr5g_v2"):
    cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"dmr5g_{int(cx)}_{int(cy)}_{int(half)}_{step}.tif"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return cache_path
    n = int(round(2 * half / step)) + 1
    n = max(50, min(2048, n))
    bbox = f"{cx-half},{cy-half},{cx+half},{cy+half}"
    url = (
        f"https://ags.cuzk.gov.cz/arcgis/rest/services/3D/dmr5g/ImageServer/exportImage"
        f"?bbox={bbox}&bboxSR=5514&imageSR=5514&size={n},{n}"
        f"&format=tiff&pixelType=F32&f=image"
    )
    with urllib.request.urlopen(url, timeout=120) as r:
        cache_path.write_bytes(r.read())
    return cache_path
```

- [ ] **Step 2: Panorama mesh builder (cardinal = new, clamp dr/dc, WGS84 UVs)**

```python
def build_panorama(dmr5g_path, cx, cy, half, step):
    """Return {vertices, faces, uvs, sjtsk_bbox, wgs_bbox, ground_z}.

    Convention: scene +Z = world +Y. world_y(local_z) = cy + local_z.
    """
    with rasterio.open(dmr5g_path) as ds:
        data = ds.read(1)
    valid = (data > 0) & (data != -9999.0)
    ground_z = float(np.percentile(data[valid], 5))

    n = int(round(2 * half / step)) + 1
    data_h, data_w = data.shape

    vertices, world_coords = [], []
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            world_x = cx + local_x
            world_y = cy + local_z
            # rasterio row 0 = top of bbox = max world_y; with scene +Z = world +Y,
            # the vertex at the largest local_z reads data row 0. Clamp to last
            # valid pixel for the outer row/col when merge returns (n-1, n-1).
            dr = min((n - 1) - r, data_h - 1)
            dc = min(c, data_w - 1)
            if valid[dr, dc]:
                y = float(data[dr, dc]) - ground_z
            else:
                y = 0.0
            vertices.append([local_x, y, local_z])
            world_coords.append((world_x, world_y))

    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            i = r * n + c
            faces.append([i, i + n, i + 1])
            faces.append([i + 1, i + n, i + n + 1])

    # WGS84 UVs over the actual wgs_bbox envelope.
    all_lon, all_lat = SJTSK_TO_WGS.transform(
        np.array([w[0] for w in world_coords]),
        np.array([w[1] for w in world_coords]),
    )
    lon_min, lon_max = float(all_lon.min()), float(all_lon.max())
    lat_min, lat_max = float(all_lat.min()), float(all_lat.max())
    uvs = []
    for i in range(len(vertices)):
        u = (float(all_lon[i]) - lon_min) / (lon_max - lon_min)
        v_uv = (float(all_lat[i]) - lat_min) / (lat_max - lat_min)
        uvs.append([u, v_uv])

    return {
        "vertices": vertices,
        "faces": faces,
        "uvs": uvs,
        "ground_z": ground_z,
        "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
        "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
    }
```

- [ ] **Step 3: Skirt rim (all 4 sides, drop 200 m)**

Same pattern as `extract_panorama_tile` skirt code in v1 (`gen_multitile.py:592-670`). Copy and simplify — no conditional skirt_edges, always emit all 4.

- [ ] **Step 4: GLB writer (uint32 indices for > 65k verts)**

```python
def save_glb(tile, output_path):
    verts = np.array(tile["vertices"], dtype=np.float32)
    faces = np.array(tile["faces"], dtype=np.uint32)
    uvs = np.array(tile["uvs"], dtype=np.float32)
    faces_packed = faces  # 601² > 65 536, must use uint32
    idx_component = pygltflib.UNSIGNED_INT
    # ... same gltf assembly as gen_multitile.save_tile_glb but no offset bake
```

- [ ] **Step 5: `main()` wiring + `manifest.json` write**

```python
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--center-sjtsk", required=True, help="cx,cy")
    p.add_argument("--half", type=int, default=15000)
    p.add_argument("--step", type=int, default=50)
    args = p.parse_args()

    cx, cy = (float(s) for s in args.center_sjtsk.split(","))
    out_dir = Path(f"tiles_v2_{args.region}")
    out_dir.mkdir(parents=True, exist_ok=True)

    dmr = fetch_dmr5g(cx, cy, args.half, args.step)
    tile = build_panorama(dmr, cx, cy, args.half, args.step)
    add_skirt(tile, depth=200)
    glb_path = out_dir / "panorama.glb"
    save_glb(tile, str(glb_path))

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"details": []}
    manifest["region"] = {
        "slug": args.region,
        "center_sjtsk": [cx, cy],
        "half": args.half, "step": args.step,
        "ground_z": tile["ground_z"],
        "sjtsk_bbox": tile["sjtsk_bbox"],
        "wgs_bbox": tile["wgs_bbox"],
        "panorama_glb": "panorama.glb",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {glb_path} ({glb_path.stat().st_size // 1024} KB) and {manifest_path}")
```

- [ ] **Step 6: Smoke test (run + measure output)**

```bash
python3 gen_panorama.py --region hnojice --center-sjtsk -547700,-1107700
python3 -c "
import struct, json
data = open('tiles_v2_hnojice/panorama.glb', 'rb').read()
print('GLB size:', len(data) // 1024, 'KB')
manifest = json.load(open('tiles_v2_hnojice/manifest.json'))
print('region:', manifest['region'])
"
```
Expected: ~11 MB GLB, manifest with `wgs_bbox` covering roughly Hnojice ± 30 km.

- [ ] **Step 7: Commit**

```bash
git add gen_panorama.py
git commit -m "feat(v2): gen_panorama.py — single-mesh region generator"
```

---

### Task 2: `gen_detail.py` — per-address detail generator (with fade ring)

**Files:**
- Create: `gen_detail.py`

- [ ] **Step 1: Module scaffold; discover SM5 tiffs covering the bbox**

```python
"""Generate one high-detail mesh for a single address inside a v2 region.

SM5 in the inner core, fade-ring blend to DMR5G at the outer 50 m.
Cardinal: scene +Z = world +Y.
"""
import argparse, json, struct, urllib.request
from pathlib import Path
import numpy as np
import pygltflib
import rasterio
from rasterio.merge import merge
from pyproj import Transformer

SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)

def discover_sm5(cx, cy, half):
    out = []
    for d in sorted(Path("cache").glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            with rasterio.open(tif) as src:
                b = src.bounds
                if not (cx + half < b.left or cx - half > b.right or
                        cy + half < b.bottom or cy - half > b.top):
                    out.append(str(tif))
    return out
```

- [ ] **Step 2: Load SM5 patch + DMR5G patch covering the same bbox**

```python
def load_sm5_patch(sm5_paths, cx, cy, half, step):
    """Return (data, valid_mask) at the requested step resolution."""
    srcs = [rasterio.open(p) for p in sm5_paths]
    try:
        merged, _ = merge(srcs, bounds=(cx-half, cy-half, cx+half, cy+half),
                          res=(step, step), nodata=-9999.0)
    finally:
        for s in srcs: s.close()
    data = merged[0]
    valid = (data > 0) & (data != -9999.0)
    return data, valid

def load_dmr5g_patch(cx, cy, half, step):
    """Use the same fetch + cache helper as gen_panorama."""
    from gen_panorama import fetch_dmr5g
    path = fetch_dmr5g(cx, cy, half, step)
    with rasterio.open(path) as ds:
        data = ds.read(1)
    valid = (data > 0) & (data != -9999.0)
    return data, valid
```

- [ ] **Step 3: Build mesh with fade-ring Y blending**

```python
def build_detail(cx, cy, half, step, fade_width, ground_z):
    """Build the detail mesh. Y = SM5 in the inner core, linear blend with
    DMR5G across the fade band, exact DMR5G match at the outer edge.
    """
    sm5_paths = discover_sm5(cx, cy, half)
    if not sm5_paths:
        raise SystemExit(f"No SM5 cache for ({cx},{cy})")
    sm5_data, sm5_valid = load_sm5_patch(sm5_paths, cx, cy, half, step)
    dmr_data, dmr_valid = load_dmr5g_patch(cx, cy, half, step)

    n = int(round(2 * half / step)) + 1
    sm5_h, sm5_w = sm5_data.shape
    dmr_h, dmr_w = dmr_data.shape

    inner_radius = half - fade_width

    vertices = []
    world_coords = []
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            dr = min((n - 1) - r, sm5_h - 1)
            dc = min(c, sm5_w - 1)
            sm5_y = float(sm5_data[dr, dc]) - ground_z if sm5_valid[dr, dc] else None
            dr2 = min((n - 1) - r, dmr_h - 1)
            dc2 = min(c, dmr_w - 1)
            dmr_y = float(dmr_data[dr2, dc2]) - ground_z if dmr_valid[dr2, dc2] else 0.0

            # Distance from mesh centre (L∞ — square fade ring instead of circular)
            d = max(abs(local_x), abs(local_z))
            if d <= inner_radius and sm5_y is not None:
                y = sm5_y
            elif d >= half:
                y = dmr_y
            else:
                t = (d - inner_radius) / fade_width
                if sm5_y is None:
                    y = dmr_y
                else:
                    y = (1.0 - t) * sm5_y + t * dmr_y
            vertices.append([local_x, y, local_z])
            world_coords.append((cx + local_x, cy + local_z))

    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            i = r * n + c
            faces.append([i, i + n, i + 1])
            faces.append([i + 1, i + n, i + n + 1])

    # WGS84 UVs over the wgs_bbox envelope
    sx = np.array([w[0] for w in world_coords])
    sy = np.array([w[1] for w in world_coords])
    lons, lats = SJTSK_TO_WGS.transform(sx, sy)
    lon_min, lon_max = float(lons.min()), float(lons.max())
    lat_min, lat_max = float(lats.min()), float(lats.max())
    uvs = [[(float(lons[i]) - lon_min) / (lon_max - lon_min),
            (float(lats[i]) - lat_min) / (lat_max - lat_min)]
           for i in range(len(vertices))]

    return {
        "vertices": vertices, "faces": faces, "uvs": uvs,
        "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
        "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
    }
```

- [ ] **Step 4: GLB writer (reuse `save_glb` from `gen_panorama`)**

```python
from gen_panorama import save_glb
```

- [ ] **Step 5: `main()` wiring + manifest append**

```python
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--center-sjtsk", required=True)
    p.add_argument("--half", type=int, default=300)   # 600 × 600 m default; bumpable per address
    p.add_argument("--step", type=int, default=1)
    p.add_argument("--fade", type=int, default=50)
    args = p.parse_args()

    cx, cy = (float(s) for s in args.center_sjtsk.split(","))
    out_dir = Path(f"tiles_v2_{args.region}")
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Run gen_panorama.py --region {args.region} first")
    manifest = json.loads(manifest_path.read_text())
    region_ground_z = manifest["region"]["ground_z"]

    tile = build_detail(cx, cy, args.half, args.step, args.fade, region_ground_z)

    details_dir = out_dir / "details"
    details_dir.mkdir(parents=True, exist_ok=True)
    glb_path = details_dir / f"{args.slug}.glb"
    save_glb(tile, str(glb_path))

    detail_meta = {
        "slug": args.slug,
        "center_sjtsk": [cx, cy],
        "half": args.half, "step": args.step, "fade": args.fade,
        "sjtsk_bbox": tile["sjtsk_bbox"],
        "wgs_bbox": tile["wgs_bbox"],
        "glb_url": f"details/{args.slug}.glb",
    }
    manifest.setdefault("details", [])
    manifest["details"] = [d for d in manifest["details"] if d["slug"] != args.slug]
    manifest["details"].append(detail_meta)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {glb_path}")
```

- [ ] **Step 6: Run + smoke test**

```bash
python3 gen_detail.py --region hnojice --slug main --center-sjtsk -547700,-1107700
```
Expected: ~36 MB GLB at `tiles_v2_hnojice/details/main.glb`, manifest entry appended.

- [ ] **Step 7: Commit**

```bash
git add gen_detail.py
git commit -m "feat(v2): gen_detail.py — per-address mesh with SM5→DMR5G fade ring"
```

---

### Task 3: `verify_v2.py` — programmatic verification

**Files:**
- Create: `verify_v2.py`

- [ ] **Step 1: GLB reader helper**

```python
import struct, json, sys
from pathlib import Path
import numpy as np

def parse_glb(path):
    with open(path, 'rb') as f: data = f.read()
    json_len = struct.unpack_from('<I', data, 12)[0]
    gltf = json.loads(data[20:20+json_len].decode())
    bin_data = data[20+json_len+8:]
    prim = gltf['meshes'][0]['primitives'][0]
    pos_acc = gltf['accessors'][prim['attributes']['POSITION']]
    bv = gltf['bufferViews'][pos_acc['bufferView']]
    off = bv.get('byteOffset', 0); count = pos_acc['count']
    f = struct.unpack_from(f'<{count*3}f', bin_data, off)
    verts = np.array([(f[i], f[i+1], f[i+2]) for i in range(0, len(f), 3)])
    idx_acc = gltf['accessors'][prim['indices']]
    return verts, idx_acc['count'] // 3
```

- [ ] **Step 2: Per-mesh sanity checks**

```python
def check_mesh(verts, face_count, expected_faces, label):
    fail = False
    if face_count < expected_faces * 0.99:
        print(f"[FAIL] {label}: face count {face_count} < expected {expected_faces}")
        fail = True
    ys = verts[:, 1]
    if not np.all(np.isfinite(ys)):
        print(f"[FAIL] {label}: non-finite Y values")
        fail = True
    # Cardinal sanity: highest world_y should be highest scene_z (since v2 uses scene+z = world+y)
    return not fail
```

- [ ] **Step 3: Cross-mesh boundary check (detail outer ↔ panorama)**

```python
def check_boundary(detail_verts, detail_meta, panorama_verts, panorama_meta):
    """For each detail outer-edge vertex, find the panorama Y at the same
    world position and assert ΔY < 0.5 m."""
    cx_d, cy_d = detail_meta["center_sjtsk"]
    cx_p, cy_p = panorama_meta["center_sjtsk"]
    half_d = detail_meta["half"]
    half_p = panorama_meta["half"]
    step_p = panorama_meta["step"]

    # Find outer edge verts: |local_x| == half_d or |local_z| == half_d
    edge_mask = (np.abs(detail_verts[:, 0]) >= half_d - 0.1) | (np.abs(detail_verts[:, 2]) >= half_d - 0.1)
    edge = detail_verts[edge_mask]

    # For each edge vertex, world position = (cx_d + lx, cy_d + lz). Panorama
    # local position = (world - panorama_center). Bilinear sample at that local.
    pano_n = int(round(2 * half_p / step_p)) + 1
    pano_grid = panorama_verts.reshape(pano_n, pano_n, 3)

    diffs = []
    for v in edge:
        wx, wz = cx_d + v[0], cy_d + v[2]
        plx, plz = wx - cx_p, wz - cy_p
        if abs(plx) > half_p or abs(plz) > half_p:
            continue
        fx = (plx + half_p) / step_p
        fz = (plz + half_p) / step_p
        ix, iz = int(fx), int(fz)
        tx, tz = fx - ix, fz - iz
        # Bilinear: panorama grid is (z, x), z=row, x=col
        y00 = pano_grid[iz, ix, 1]
        y10 = pano_grid[iz+1, ix, 1] if iz+1 < pano_n else y00
        y01 = pano_grid[iz, ix+1, 1] if ix+1 < pano_n else y00
        y11 = pano_grid[iz+1, ix+1, 1] if iz+1 < pano_n and ix+1 < pano_n else y00
        pano_y = (1-tx)*(1-tz)*y00 + tx*(1-tz)*y01 + (1-tx)*tz*y10 + tx*tz*y11
        diffs.append(abs(v[1] - pano_y))

    if not diffs:
        print("[WARN] No outer-edge verts found inside panorama bbox")
        return True
    max_d = max(diffs); mean_d = sum(diffs) / len(diffs)
    print(f"detail→panorama outer-edge ΔY: max={max_d:.3f}m mean={mean_d:.3f}m")
    if max_d > 0.5:
        print(f"[FAIL] outer-edge ΔY exceeds 0.5 m threshold")
        return False
    return True
```

- [ ] **Step 4: `main()` glues it together**

```python
def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--detail", required=True)
    args = p.parse_args()

    out_dir = Path(f"tiles_v2_{args.region}")
    manifest = json.loads((out_dir / "manifest.json").read_text())
    region = manifest["region"]
    detail = next(d for d in manifest["details"] if d["slug"] == args.detail)

    pano_verts, pano_faces = parse_glb(out_dir / region["panorama_glb"])
    pano_n = int(round(2 * region["half"] / region["step"])) + 1
    pano_expected = (pano_n - 1) ** 2 * 2

    detail_verts, detail_faces = parse_glb(out_dir / detail["glb_url"])
    det_n = int(round(2 * detail["half"] / detail["step"])) + 1
    det_expected = (det_n - 1) ** 2 * 2

    ok = True
    ok &= check_mesh(pano_verts, pano_faces, pano_expected, "panorama")
    ok &= check_mesh(detail_verts, detail_faces, det_expected, "detail")
    ok &= check_boundary(detail_verts, detail, pano_verts, region)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run + ensure it passes**

```bash
python3 verify_v2.py --region hnojice --detail main
```
Expected output: `detail→panorama outer-edge ΔY: max=0.0xx mean=0.0xx`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add verify_v2.py
git commit -m "feat(v2): verify_v2.py — programmatic seam + sanity check"
```

---

### Task 4: `v2.html` viewer — load panorama + detail + sky

**Files:**
- Create: `v2.html`

- [ ] **Step 1: HTML scaffold (camera, sky, controls)**

Copy the head + body skeleton from `hnojice_lod_multi.html:1-200` but rip out everything v1-specific. Keep the importmap, `THREE.Sky`, OrbitControls, fog. Initial camera `(0, 200, 200)`, far plane 60 000.

- [ ] **Step 2: Parse URL query**

```javascript
const params = new URLSearchParams(location.search);
const REGION = params.get('region') || 'hnojice';
const DETAIL_SLUG = params.get('detail') || 'main';
```

- [ ] **Step 3: Load manifest + panorama + detail**

```javascript
const manifest = await fetch(`tiles_v2_${REGION}/manifest.json`, { cache: 'no-store' }).then(r => r.json());
const region = manifest.region;
const detail = manifest.details.find(d => d.slug === DETAIL_SLUG);

const gltfLoader = new GLTFLoader();
const texLoader = new THREE.TextureLoader();

// Panorama at scene origin
gltfLoader.load(`tiles_v2_${REGION}/${region.panorama_glb}?v=v2-1`, (gltf) => {
  let m = gltf.scene.children[0]; if (m && !m.isMesh && m.children[0]) m = m.children[0];
  const mat = new THREE.MeshBasicMaterial({ side: THREE.DoubleSide });
  const mesh = new THREE.Mesh(m.geometry, mat);
  mesh.renderOrder = 0;
  scene.add(mesh);
  const bb = region.sjtsk_bbox, wb = region.wgs_bbox;
  const url = `/proxy/ortofoto?BBOX=${bb.join(',')}&WBBOX=${wb.join(',')}&size=4096&zoom=12`;
  texLoader.load(url, (tex) => { tex.colorSpace = THREE.SRGBColorSpace; mat.map = tex; mat.needsUpdate = true; });
});

// Detail at scene = world delta to region centre. Cardinal: scene +Z = world +Y.
const off_x = detail.center_sjtsk[0] - region.center_sjtsk[0];
const off_z = detail.center_sjtsk[1] - region.center_sjtsk[1];
gltfLoader.load(`tiles_v2_${REGION}/${detail.glb_url}?v=v2-1`, (gltf) => {
  let m = gltf.scene.children[0]; if (m && !m.isMesh && m.children[0]) m = m.children[0];
  const mat = new THREE.MeshBasicMaterial({
    side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
  });
  const mesh = new THREE.Mesh(m.geometry, mat);
  mesh.position.set(off_x, 0, off_z);
  mesh.renderOrder = 1;
  scene.add(mesh);
  const bb = detail.sjtsk_bbox, wb = detail.wgs_bbox;
  const url = `/proxy/ortofoto?BBOX=${bb.join(',')}&WBBOX=${wb.join(',')}&size=2048&zoom=19`;
  texLoader.load(url, (tex) => { tex.colorSpace = THREE.SRGBColorSpace; mat.map = tex; mat.needsUpdate = true; });
});
```

- [ ] **Step 4: Manual verification in browser**

Start `python3 server.py`. Open `v2.html?region=hnojice&detail=main`. Expected:
- Panorama plate (~30 km wide) covers the horizon.
- Hnojice village mesh sits crisply on top of panorama.
- No vertical step at the village outer edge (fade ring did its job).
- Mountains visible toward Jeseníky (north-east) at the panorama edge.
- Sky overhead, OrbitControls works.

- [ ] **Step 5: Commit**

```bash
git add v2.html
git commit -m "feat(v2): v2.html — region + detail viewer"
```

---

### Task 5: `viewer-realtor-overlay-v2.js` — overlay port with new cardinal

**Files:**
- Create: `viewer-realtor-overlay-v2.js` (copy of v1 file, then patch)
- Create: `server.py` v2 endpoints `/api/v2/parcel-at-point`, `/api/v2/parcels`, `/api/v2/buildings`
- Modify: `v2.html` (try-import the overlay)

- [ ] **Step 1: Copy + patch the overlay**

```bash
cp viewer-realtor-overlay.js viewer-realtor-overlay-v2.js
```

Then in `viewer-realtor-overlay-v2.js`:
- Change every `sy = -localZ + gcy` → `sy = localZ + gcy`.
- Change every fetch URL `/api/parcel-at-point` → `/api/v2/parcel-at-point`. Same for `/api/parcels`, `/api/buildings`.
- Update any `tile.center_sjtsk[1] - lzMax` cadastre-bbox math to `tile.center_sjtsk[1] + lzMin`, etc.

- [ ] **Step 2: Add v2 server endpoints**

In `server.py`, add (paralleling the v1 handlers):
```python
elif self.path.startswith("/api/v2/parcel-at-point?"):
    self._api_v2_parcel_at_point()
elif self.path.startswith("/api/v2/parcels?"):
    self._api_v2_parcels()
elif self.path.startswith("/api/v2/buildings?"):
    self._api_v2_buildings()
```

In `_fetch_parcels_area` make a v2 variant (or parameterize the existing one): change `round(-(sy - gcy), 2)` → `round(sy - gcy, 2)`. Same for `_api_buildings`. Keep v1 versions unchanged.

- [ ] **Step 3: Fetch RUIAN buildings + wire overlay into `v2.html`**

**Important — `gcx`/`gcy` must be the REGION anchor, not the detail centre.** Both panorama and detail share the world coord system; a raycast against either mesh yields `world_x = scene_x + region.center_sjtsk[0]`, `world_y = scene_z + region.center_sjtsk[1]`. Passing the detail centre would offset every parcel lookup by `(detail_cx - region_cx, detail_cy - region_cy)`, which can be hundreds of metres → wrong parcel returned for every click.

```javascript
const gcx = region.center_sjtsk[0];
const gcy = region.center_sjtsk[1];

// Fetch RUIAN building footprints around the region anchor. v1 baked these
// into data.json; v2 fetches them at load. Use a radius that covers the
// detail mesh wholly — detail.half + 100 m buffer is plenty.
const buildingsRadius = Math.max(detail.half + 100, 800);
const ruianBuildings = await fetch(
  `/api/v2/buildings?gcx=${gcx}&gcy=${gcy}&radius=${buildingsRadius}`,
  { cache: 'no-store' }
).then(r => r.json()).catch(() => []);

try {
  const mod = await import('./viewer-realtor-overlay-v2.js');
  mod.attach({
    scene, camera, controls, renderer,
    allMeshes,                          // both panorama + detail meshes for raycasting
    ruianBuildings,
    gcx, gcy,                            // REGION anchor — NOT detail centre
    location: { label: REGION },
  });
} catch (e) { console.info('overlay not present', e); }
```

- [ ] **Step 4: Port building popup click handler**

The popup HTML element (`#building-popup`) is copied with the body skeleton in Task 1. The CLICK handler that raycasts and fills the popup lives in v1's main script (not in the overlay). Port it into `v2.html`:

```javascript
// Raycast on canvas click. If hit hits a vertex inside a RÚIAN building
// footprint, populate #popup-* spans and show the popup at the cursor.
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const popup = document.getElementById('building-popup');

renderer.domElement.addEventListener('click', (event) => {
  pointer.x = (event.clientX / innerWidth) * 2 - 1;
  pointer.y = -(event.clientY / innerHeight) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(allMeshes, true);
  if (!hits.length) { popup.style.display = 'none'; return; }
  const p = hits[0].point;
  const world_x = p.x + gcx;
  const world_y = p.z + gcy;   // v2 convention: scene +Z = world +Y
  // Walk ruianBuildings, point-in-polygon test in S-JTSK
  const hitBldg = findBuildingAt(world_x, world_y, ruianBuildings);
  if (!hitBldg) { popup.style.display = 'none'; return; }
  document.getElementById('popup-title').textContent = hitBldg.label || 'Budova';
  document.getElementById('popup-cislo').textContent = hitBldg.cislo || '—';
  document.getElementById('popup-plocha').textContent = hitBldg.plocha || '—';
  document.getElementById('popup-kod').textContent = hitBldg.kod || '—';
  document.getElementById('popup-mapy').href = mapyUrl(world_x, world_y);
  document.getElementById('popup-ruian').href = ruianUrl(hitBldg.kod);
  popup.style.left = event.clientX + 'px';
  popup.style.top = event.clientY + 'px';
  popup.style.display = 'block';
});
```

`findBuildingAt`, `mapyUrl`, `ruianUrl` are 10-line helpers. Look them up in `hnojice_lod_multi.html` and copy. RÚIAN footprints are in local coords relative to gcx/gcy with v2 convention `lz = sy - gcy`, so the point-in-polygon test uses `world_x - gcx, world_y - gcy` against polygon coords.

- [ ] **Step 5: Manual verify in browser**

- Click on a Hnojice house → popup with č.p. opens.
- Click `Parcely` toolbar button → parcel boundaries draw on the detail mesh.
- Right-click → drone video panel opens.

- [ ] **Step 6: Commit**

```bash
git add viewer-realtor-overlay-v2.js server.py v2.html
git commit -m "feat(v2): port realtor overlay to new cardinal convention"
```

---

### Task 6: Mark v1 deprecated + final smoke test

**Files:**
- Modify: `hnojice_multi.html`, `hnojice_lod_multi.html` (banner)

- [ ] **Step 1: Banner in v1 viewers**

Prepend each v1 viewer's `<body>` with:
```html
<div style="background:#fee;padding:8px;font:14px system-ui;border-bottom:2px solid #c33;">
  ⚠️ This is the v1 viewer kept for reference. The current viewer is
  <a href="v2.html?region=hnojice&detail=main">v2.html</a>.
</div>
```

- [ ] **Step 2: Final round trip**

```bash
python3 gen_panorama.py --region hnojice --center-sjtsk -547700,-1107700
python3 gen_detail.py --region hnojice --slug main --center-sjtsk -547700,-1107700
python3 verify_v2.py --region hnojice --detail main
# (open v2.html?region=hnojice&detail=main in browser, verify visually)
```

- [ ] **Step 3: Commit**

```bash
git commit -am "docs: deprecate v1 viewers in favour of v2"
```

---

## Out of scope (deferred)

- **Atmosphere polish** (sun-tinted fog, time-of-day presets) — separate plan `2026-05-13-lod-phase5-atmosphere-polish.md`. Land after v2 is verified.
- **Address geocoding** — `--address "Hnojice 23"` resolving to S-JTSK lookup. Separate plan; for now manual `--slug` + `--center-sjtsk`.
- **Multi-region UX** — drop-down to switch regions in v2.html. For now URL query only.
- **Auto-deletion of v1** — keep at least one week after v2 ships.
- **Other locations** (Šantovka, Strážek, Praha) — once Hnojice is verified, regen for each region by hand: `gen_panorama.py --region <name> --center-sjtsk <cx>,<cy>`.
- **Cache busting strategy** — static `?v=v2-1` query, bump manually.
- **Off-center detail test** (B8) — second detail with `--slug satellite --center-sjtsk -546700,-1107700` to confirm overlay click still works when detail ≠ region centre. Land before declaring v2 production-ready for multi-address use.
- **Cadastre overlay** (Katastrální mapa toggle) and **height exaggeration slider** — v1 features not yet ported. Land separately once v2 core is stable.
- **Praděd / Jeseníky panorama** — bump `--half` to 30 000 (or split into separate "far horizon" optional layer). Currently 15 km panorama doesn't reach.

## Self-review against the 6 hours of v1 pain

| Past failure | This plan's mitigation |
|---|---|
| Cardinal sign drift between layers | D2 locks scene +Z = world +Y everywhere in v2; explicit conversion table; v1 endpoints untouched. |
| SM5 + DMR5G mixed in one mesh = CRS / source step | D1: SM5 in core, DMR5G in fade; same mesh but data sources interpolate cleanly. Never `rasterio.merge` them together. |
| Partial SM5 + `grid_valid` = levitating holes | D1 fade ring handles missing SM5: when `sm5_valid` is False, the vertex uses DMR5G alone. Always solid mesh, no `grid_valid` mask. |
| rasterio.merge off-by-one (n vs n-1) | D7: clamp `dr, dc` to `data.shape - 1`. Tested in `verify_v2.py`. |
| Edge stitching corner ↔ side ↔ ring | No rings in v2. One panorama mesh, one detail mesh. Zero stitching code. |
| UV vs atlas bbox mismatch | D6: WGS84-based UVs across the whole tile + WGS84 bbox for the ortho fetch. Atlas math unnecessary (one texture per mesh). |
| Visual-only verification cycle = slow | D4: `verify_v2.py` runs after every gen, fails fast with explicit thresholds. |
| `polygonOffset` masquerading as Y fix | D1: the fade ring is a real geometric solution; polygonOffset stays only as a final z-fight insurance, not load-bearing. |

## Second review pass (B-findings)

| Finding | Status |
|---|---|
| **B1**: Overlay `gcx/gcy` must be region anchor, not detail centre — both meshes share one world coord system. | Fixed in Task 5 step 3 with explicit warning + correct code. |
| **B2**: Detail default `--half` was 500 (1 km); user wants 600 m (= half 300). | Defaults updated. |
| **B3**: `ruianBuildings` was baked into v1 `data.json` but absent from v2 manifest. | Task 5 step 3 fetches `/api/v2/buildings` at startup. |
| **B4**: Building popup click handler lives in viewer (not overlay) — easy to miss. | Task 5 step 4 explicitly ports it with code outline. |
| **B5**: Panorama half 15 km does NOT reach Praděd (28 km). | Documented in Architecture section. Bump to 30 000 if needed. |
| **B6**: `cache/` is a symlink to `../gtaol/cache`; v2's `cache/dmr5g_v2/` will appear inside gtaol. | Documented in Architecture section. Acceptable. |
| **B7**: WGS84 UVs across a Krovák-rotated tile leave a thin axis-aligned-WGS band of "extra" texture pixels around the diagonal mesh footprint. Effective resolution ~5 % worse than ideal. | Acceptable; cost is tiny vs implementation complexity of S-JTSK-rotated server crop. |
| **B8**: First detail (slug = `main`) has `center == region.center`, so the offset edge case isn't tested. | Recommended: after Hnojice main is working, generate a second `--slug satellite --center-sjtsk -546700,-1107700` (1 km east) and verify overlay click still resolves to the correct parcel via region-anchored gcx/gcy. Out of scope for this plan but listed below. |

## Execution handoff

Two paths:

1. **Subagent-driven (recommended)** — fresh subagent per task, two-stage review. Fast iteration in this same session.
2. **Inline** — execute task-by-task in this session with `superpowers:executing-plans`.

Pick one and the implementation starts.
