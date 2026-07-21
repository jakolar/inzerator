# Polygon cutout page — design

Date: 2026-07-21. Status: approved (prototype scope — "budeme na tom hodně
měnit věci").

## Purpose

Let the user draw an arbitrary polygon in the map3d viewer and turn it into a
standalone baked 3D page: a hard-clipped "island" of terrain (diorama) with
its own URL, shareable like today's per-slug heightfield pages.

Successor idea to the retired v2 detail-mesh flow (`gen_detail.py` → GLB in a
regional viewer), but: arbitrary drawn shape instead of an address point, and
a self-contained per-slug page instead of a detail inside a regional model.

## Decisions (user-approved)

1. **Polygon definition: drawing in map3d** — click vertices on terrain.
   Not parcel-select (rejected), not parcel+refine (deferred).
2. **Output: baked self-contained page** — existing slug machinery
   (`tiles_v2_<slug>/` + `/heightfield/?slug=`), not a live map3d deep-link,
   not a static export.
3. **Look: island (hard clip)** — terrain exists only inside the polygon,
   cut walls at the boundary, background instead of surrounding terrain.
4. **Clip happens viewer-side** (prototype): pipeline bakes the normal square
   rings; the heightfield viewer discards fragments outside the polygon.
   Look iteration = shader tweak, no regeneration. Baked asset clipping is a
   later phase, after the look settles.

## Flow

```
map3d: ✏️ výřez → click vertices (terrainPoint pick) → close by clicking the
first vertex → sheet: label + demand panel → Vygenerovat
  ↓ POST /api/jobs { slug, label, polygon: [[lon,lat],…] }
    (no cx/cy from the client — map3d has no projection library; the SERVER
     derives cx/cy = polygon bbox centre in S-JTSK via pyproj and inner_half
     from the bbox half-extent, existing clamp 500–2000 m)
  ↓ normal sm5 + heightfield job (pipeline unchanged)
  ↓ location.json gains "polygon": [[lon,lat],…]
dashboard (index.html): location appears as usual → open /heightfield/?slug=X
heightfield viewer: location.json has polygon → island mode
```

## Draw UI (map3d/index.html)

- Toolbar button `✏️ výřez` toggles draw mode; taps/clicks then place
  vertices at the picked terrain point (reuse `pickSurface`/`terrainPoint`).
- In-progress polygon rendered with the existing highlight helpers
  (`_hlLoop` line + translucent `_hlFill`); vertices as small markers.
- Backspace removes the last vertex, Escape cancels, clicking the first
  vertex closes the ring (min 3 vertices).
- On close: bottom sheet with label input (slug auto via `slugifyJS`,
  suffix `-vyrez` when colliding), demand panel (below), Vygenerovat button
  → POST `/api/jobs`, then link to the dashboard job.
- No vertex editing after close in the prototype — redraw instead.

## Demand panel (in the draw sheet, live while drawing)

- **Plocha + rozměry**: polygon area (ha) + bbox size (m × m), updated per
  vertex (shoelace formula on the WGS ring with cos(lat) scaling).
- **Ring fit + generation time**: `POST /api/estimate { polygon }` (new) —
  the server derives `{ cx, cy, inner_half, clamped, bbox_m }` (single source
  of truth, same code path as the job) and checks sheet cache presence via
  the existing MAPNOM resolution used by the sm5 step, returning
  `{ sheets_total, sheets_cached }`. (DMR5G is fetched per-location, not
  per-sheet — unknowable before the slug exists; the UI shows the time as a
  range instead.) Debounced per vertex.
  UI renders the clamp warning (island cut by ring edge when bbox > 4 km)
  and "~2–4 min (cache)" vs "~15–30 min (stahuje X listů)".
- **Page size**: static table by ring size + ortho tiers (mid/high/ultra
  ≈ 8–12 MB; +super ≈ +40 MB).
- **Viewer demand**: static GPU-memory table by tier (ultra ≈ 30 MB GPU,
  super ≈ 128 MB) — informs what can be sent to a phone.
- After generation, the Lokace dashboard lists the real on-disk size per
  location (summed in `/api/locations`).

## Server changes

- `locations.parse_job_extent`: accept optional `polygon` — list of 3–200
  `[lon, lat]` float pairs, ring not required to be closed; validate types
  and WGS bounds. When present and cx/cy are missing, derive cx/cy +
  inner_half from the S-JTSK bbox (pyproj) — shared helper also used by
  `/api/estimate`.
- `locations._persist_location_meta`: persist `polygon` into
  `location.json` (same pattern as `subject_parcels`).
- `server.py`: `POST /api/estimate` endpoint (derive ring + MAPNOM sheet
  resolution + cache presence check; no downloads). `/api/locations` adds
  `size_mb` per location.
- Pipeline (`gen_heightfield.py`) unchanged.

## Island rendering (heightfield/index.html)

On load, when `location.json` contains `polygon`:

1. Rasterise the polygon once into a mask texture (2D canvas, ring → fill,
   S-JTSK-aligned to the ring extent like the cadastre drape).
2. Terrain shader: `discard` fragments outside the mask (all rings — the
   closeup ring outside the polygon disappears too; background shows).
3. Curtain walls: polygon edge vertices sampled against the DSM
   (`sampleHeight`), extruded down to a base plane (min height − offset);
   single flat material — reads as a cross-section cut.
4. Background: plain gradient (existing clear color / fog), no terrain.
5. Ortho tiers, cadastre and parcel overlays keep working inside the island.

## Deferred (explicitly out of prototype)

- Baked asset clipping (phase 2, after the look settles).
- Vertex editing after close; touch-optimised drawing.
- Hiding viewer UI toggles for a clean "marketing" page.
- Parcel-snap or parcel+refine polygon definition.

## Success criteria

- Draw a ~1 ha polygon over a village in map3d, generate, open
  `/heightfield/?slug=…`: island shows clipped terrain with walls and
  background; page works after server restart (all data baked + polygon in
  location.json).
- Demand panel warns on a > 4 km polygon and its time estimate reflects
  cache state (cold vs warm village).
