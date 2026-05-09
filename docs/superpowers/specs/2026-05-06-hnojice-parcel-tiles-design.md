# Area viewer — RÚIAN parcel tiles (3D extrusions)

**Date:** 2026-05-06
**Scope:** New `/api/parcels` endpoint in `server.py` + parcel layer in
`hnojice_multi.html`. Generic enough that other area viewers (Strážek,
Praha) can adopt it with one button + one fetch URL.
**Goal:** Render RÚIAN cadastral parcels as low extruded "tiles" (15 cm
tall) draped onto the terrain heightfield, color-coded by land-use type,
clickable for parcel info. Initially only Hnojice viewer wires it up.

## Motivation

The Hnojice 3D area viewer shows terrain + buildings extruded from RÚIAN
footprints. Land use beyond the buildings — orchards vs. arable, pond vs.
forest, garden vs. grass — is invisible. Adding parcel polygons as
slightly-elevated tiles gives that information at a glance and reveals the
ownership/use grid that defines a Czech village's character. 3D extrusion
(rather than 2D drape) makes the tiles read clearly even on slopes and
keeps the village's relief intact.

## Endpoint

```
GET /api/parcels?gcx=<sjtsk>&gcy=<sjtsk>&radius=<m, default 2000>
```

Same parameter contract as `/api/buildings`. Generic over location — the
client passes the centre that matches its own GLB tile pack's local frame.

**Server flow:**

1. Build SJTSK bbox `[gcx-r, gcy-r, gcx+r, gcy+r]`.
2. Query RÚIAN ArcGIS `MapServer/5` (Parcela layer) with `outFields =
   "kod,kmenovecislo,poddelenicisla,druhpozemkukod,vymera"`,
   `outSR=5514`, `returnGeometry=true`. Page in chunks of 1000 records
   using `resultOffset` until empty.
3. For each parcel polygon (outer ring only in v1), open the DSM TIFF
   covering that vertex (existing `cache/dmpok_tiff_*` scan, same
   logic as `/api/building-detail`) and sample elevation per vertex.
   Vertex outside any cached TIFF → fallback `terrain_y = 0` (parcel
   will sit at world Y=0 there, visually slightly off but tolerable).
4. Convert each vertex `(sx, sy)` to local frame: `lx = sx - gcx`,
   `lz = -(sy - gcy)`. Y stays in absolute metres above sea level
   (matches the GLB tile Y datum used by area viewers).
5. Cache the result to `cache/parcels_{gcx:.0f}_{gcy:.0f}_{radius:.0f}.json`.
   On subsequent requests, serve from disk — no RÚIAN call.

**Response (JSON array):**

```json
[
  {
    "id": "12345678",
    "label": "423/2",
    "use_code": 13,
    "use_label": "zastavěná plocha a nádvoří",
    "area_m2": 421,
    "ring_local": [[lx, lz, terrain_y_m], ...]
  },
  ...
]
```

`label` = `kmenovecislo` plus `/<poddelenicisla>` if present, else
just `kmenovecislo`. `use_label` = static lookup table from
`druhpozemkukod` (the dictionary lives server-side).

**Errors:**
- Missing `gcx`/`gcy` → 400.
- RÚIAN ArcGIS unreachable + no disk cache → 503.
- RÚIAN unreachable + disk cache present → serve cached JSON (logged).

**Cache invalidation:** manual — delete the JSON file. RÚIAN parcel data
changes rarely; staleness is acceptable in v1.

## Client (`hnojice_multi.html`)

### Toolbar toggle

Add a button next to the existing toolbar controls:

```html
<button id="parcelsBtn" class="toolbar-btn">Parcely</button>
```

State: **off by default**. First click triggers fetch + build; subsequent
clicks toggle `parcelGroup.visible`.

### Geometry build

For each parcel:

```js
const ring = parcel.ring_local;          // [[lx, lz, ty], ...]
const tileH = 0.15;                      // 15 cm
const lift  = 0.02;                      // tiny offset above terrain to win z-fight

// Top face: triangulated polygon at ty + lift + tileH per vertex
const topPositions = [];
for (const [x, z, y] of ring)
  topPositions.push(x, y + lift + tileH, z);
const topIndices = earcut(ring.flatMap(([x, z]) => [x, z]));   // 2D triangulation

// Side walls: per ring edge, two triangles (4 verts) connecting
// terrain top to extruded top
const sidePositions = [], sideIndices = [];
for (let i = 0; i < ring.length; i++) {
  const [ax, az, ay] = ring[i];
  const [bx, bz, by] = ring[(i + 1) % ring.length];
  const base = sidePositions.length / 3;
  sidePositions.push(
    ax, ay + lift,         az,    // a-bot
    bx, by + lift,         bz,    // b-bot
    bx, by + lift + tileH, bz,    // b-top
    ax, ay + lift + tileH, az,    // a-top
  );
  sideIndices.push(base, base+1, base+2,  base, base+2, base+3);
}
```

Bottom face omitted — it's flush with terrain and would only z-fight.

### Material per use code

Color palette keyed by `druhpozemkukod` (RÚIAN current codes):

```js
const PARCEL_COLORS = {
  2:  0xb9905a,   // orná půda
  3:  0x3a5f2a,   // chmelnice
  4:  0x5a3a55,   // vinice
  5:  0x6a8a3a,   // zahrada
  6:  0x4a7a30,   // ovocný sad
  7:  0x8db04a,   // trvalý travní porost
  10: 0x1f3a1c,   // lesní pozemek
  11: 0x2a4a6e,   // vodní plocha
  13: 0x555555,   // zastavěná plocha a nádvoří
  14: 0xa89878,   // ostatní plocha
};
const PARCEL_FALLBACK = 0x777777;
const TOP_OPACITY  = 0.55;
const SIDE_OPACITY = 0.85;
```

Each parcel gets its own top + side materials (cloned from a per-code
template at build time, so `MeshStandardMaterial` instance count stays
small — one base per code). Top: `transparent: true, opacity: 0.55,
depthWrite: false, roughness: 0.9, metalness: 0`. Side: same color,
`transparent: true, opacity: 0.85, depthWrite: true`. The `depthWrite:
false` on tops avoids rare punch-throughs when overlapping multipolygons
stack; sides keep depth so silhouettes remain solid.

### Render order

```
terrain (renderOrder 0)  →  parcels (renderOrder 1)  →  buildings (renderOrder 2)
```

Buildings opaque, drawn last for transparent layers — but since buildings
are opaque they can render in any order; the explicit `renderOrder` is for
the parcels-vs-terrain case.

### Mesh granularity

**One `Group` per parcel** containing two `Mesh`es (top, sides). Each
parcel `Group` carries `userData = { id, label, use_code, use_label,
area_m2, ring_local }` so the click handler reads the source record
straight off the raycast hit's `object.parent`. No per-triangle index
table needed.

Cost: ~1500 parcels × 2 meshes = ~3000 draw calls when the layer is
visible. THREE handles this for a static scene at 60 fps on Apple
silicon, and the user can toggle the layer off any time. If the iPad
stutters, the upgrade path is `THREE.BatchedMesh` with explicit ID
attribute — out of v1 scope.

### Click handling

Reuse the existing `#building-popup` element. Extend the global click
raycast to also test against `_parcelGroup.children` (when visible).

Click → populate popup with:
- Parcel number (`label`)
- Land use (`use_label`)
- Area (`area_m2`)
- External link: `https://nahlizenidokn.cuzk.cz/VyberParcelu/Parcela/InformaceO?id=${id}`
- Yellow outline (LineLoop) on the picked parcel's top edge

Hover state: on `mousemove` raycast, set a single hovered LineLoop with
weaker yellow. Cleared on mouseout.

### Lifecycle

```js
let _parcels = null;          // lazy-loaded array from server
let _parcelGroup = null;      // THREE.Group, parent of all parcel meshes
let _parcelHover = null;      // LineLoop, current hover outline

async function ensureParcels() {
  if (_parcelGroup) return _parcelGroup;
  const r = await fetch(`/api/parcels?gcx=${GCX}&gcy=${GCY}&radius=2000`);
  if (!r.ok) throw new Error(`parcels: HTTP ${r.status}`);
  _parcels = await r.json();
  _parcelGroup = buildParcelGroup(_parcels);
  scene.add(_parcelGroup);
  return _parcelGroup;
}

document.getElementById('parcelsBtn').onclick = async () => {
  try {
    const g = await ensureParcels();
    g.visible = !g.visible;
    parcelsBtn.classList.toggle('active', g.visible);
  } catch (e) {
    console.error(e);
    parcelsBtn.textContent = '✕ Parcely';
  }
};
```

## Performance budget

- Hnojice 4 km² ≈ ~1500 parcels, ~10 verts each → ~15k top verts + ~30k
  side verts = ~45k total verts, ~30k triangles.
- 1500 parcels × 2 meshes (top, sides) ≈ 3000 draw calls. Acceptable on
  desktop and iPad; revisit with `BatchedMesh` if frame time exceeds 16 ms
  after enabling.
- Initial fetch: ~500 KB JSON (gzipped ~150 KB). Disk cache after first
  request keeps subsequent loads near-instant.
- DSM sampling: ~15k vertex lookups against rasterio. Sub-second on first
  fetch; cached thereafter.

## What we are NOT doing (YAGNI)

- **No interior holes** in parcels (rare — ponds inside a single parcel).
  Earcut on outer ring only. Acceptable visual artifact.
- **No multi-polygon support** — RÚIAN occasionally returns parcels with
  multiple disjoint rings; first ring only in v1.
- **No owner / LV info** — Nahlížení do KN is one external click away.
- **No LOD / streaming** — village-sized data, single fetch is fine.
- **No edit / measure tools.**
- **No automatic re-fetch on data change** — manual cache delete.
- **No coloring fallback for unmapped use codes beyond grey** — visible
  in popup; user can read what it actually is.

## Test plan

Manual, with `server.py` running and DSM cache present for Hnojice:

1. Open `hnojice_multi.html`. Verify Parcely button is **off** initially —
   no parcels visible, scene loads same as before (regression check).
2. Click Parcely. Within 1–3 s: parcels appear as flat colored tiles
   covering the village. Buildings still visible on top. Terrain still
   visible through translucent tops.
3. Pan/zoom — tiles drape correctly on slopes (no floating, no clipping
   into terrain by more than the lift offset).
4. Click a residential tile — popup shows correct parcel number, "zastavěná
   plocha a nádvoří", area in m², link to Nahlížení that opens correct
   parcel page.
5. Click an orchard, a forest, a road parcel — colors match expectation.
6. Click Parcely again — tiles disappear. Click again — instant reappear
   (no fetch, just `.visible = true`).
7. Frame rate: stays ≥ 30 fps on iPad while panning. If not, switch to
   `BatchedMesh` (out of v1 scope).
8. Refresh page after first fetch — second load of parcels should hit
   server cache (`cache/parcels_*.json`) and complete in <100 ms.

## Files touched

- `server.py` —
  - new handler `/api/parcels` with disk cache
  - `_fetch_parcels_area(gcx, gcy, radius)` helper (paginated RÚIAN +
    DSM sample)
  - `DRUH_POZEMKU_LABELS` static lookup
- `hnojice_multi.html` —
  - Parcely toolbar button + active-state CSS
  - `ensureParcels`, `buildParcelGroup`, hover/click raycast extension
  - earcut import (already in three.js examples or via CDN)
- `cache/parcels_*.json` — auto-generated, add to `.gitignore` if not
  already covered by `cache/`.

No changes to `gen_multitile.py` (parcels are runtime, not pre-baked).
Inspector remains untouched.
