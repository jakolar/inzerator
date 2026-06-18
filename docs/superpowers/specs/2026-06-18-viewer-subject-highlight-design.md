# Viewer subject-parcel highlight (subsystem C)

Date: 2026-06-18
Status: approved (Jan, 2026-06-18 — design approved in chat: "a, předvyplnit do
selectedParcels")

Part of the 3-subsystem selection feature (build order **B → C → A**):
- B (done, in `main`): generation persists `subject_parcels` (+ `inner_half`)
  into `tiles_v2_<slug>/location.json`.
- **C (this spec):** the 3D viewer reads `subject_parcels` on load and
  pre-highlights them, reusing the existing parcel-selection machinery.
- A: the standalone selector app that writes the selection (separate spec, new repo).

## Problem

`heightfield/index.html` already has a full parcel-selection overlay: a `🟦
Označit parcely` toggle loads `/api/parcels`, clicking a parcel toggles its id
in a `selectedParcels` Set, and `redrawOutlines()` draws draped outlines/fills
for the selected set. Today the set always starts empty. Subsystem B now
persists the listing's subject parcels in `location.json`; the viewer should
open with those already highlighted, so a generated marketing model shows its
property without any user action.

Relevant existing pieces (all in `heightfield/index.html`):
- `slug` and `asset(p)` (= `/tiles_v2_<slug>/heightfield/` + p); the viewer
  fetches `manifest.json` this way, so static files under `tiles_v2_<slug>/`
  are served.
- `selectedParcels` (Set), `parcelsById` (Map keyed by `p.id`), `ensureParcels()`,
  `redrawOutlines()`, and the `parcelsToggle` (`#parcels-toggle`) change handler
  which awaits `Promise.all([ensureParcels(), ensureHeightDataLoaded()])`, sets
  `outlineGroup.visible = true`, and calls `redrawOutlines()`.
- `/api/parcels` returns each parcel's `id` as a **string** (`str(attrs["id"])`,
  `server.py`).

## Design

### Data source

On viewer load, fetch the location metadata written by B:

```
GET /tiles_v2_<slug>/location.json
```

Build the URL explicitly from `slug` (don't rely on `..` normalisation):
`` `/tiles_v2_${slug}/location.json` ``. The file is optional — legacy / RÚIAN
locations have none, and pre-B `location.json` files lack the field. A 404 or a
missing/empty `subject_parcels` is a silent no-op (no highlight, no error).

### Highlight (reuse the existing overlay)

When `location.json.subject_parcels` is a non-empty array:

1. **Normalise ids to strings** and seed the selection — the viewer keys
   everything on the string `p.id` from `/api/parcels`, while B persists ints:
   ```js
   for (const id of subj) selectedParcels.add(String(id));
   ```
2. **Reflect + trigger the overlay** by reusing the toggle handler verbatim:
   ```js
   parcelsToggle.checked = true;
   parcelsToggle.dispatchEvent(new Event('change'));
   ```
   The handler then loads parcels + heightData, shows `outlineGroup`, and
   `redrawOutlines()` draws exactly the seeded subject set with the normal
   selection style. The checkbox shows checked, so the UI is consistent and the
   user can deselect/add as usual.

This runs once at startup, after the parcels machinery is defined (so
`parcelsToggle`, `selectedParcels`, `ensureParcels`, `redrawOutlines`, `asset`
all exist) — i.e. near the parcels-overlay setup / end of module init, inside a
`try/catch` that logs and continues on any failure.

### Edge cases

- **No `location.json` / no `subject_parcels`** → skip; viewer behaves exactly
  as today (empty initial selection).
- **A subject id not in the fetched parcels** (e.g. just outside the parcel
  radius) → `parcelsById.get(id)` is undefined; `redrawOutlines` already skips
  unknown ids (`if (!p || !p.ring_local …) continue`). Safe, no crash.
- **Hidden-tab unload/reload** (`_savedToggleState`): the subject ids live in
  `selectedParcels` like any selection, so the existing save/restore path
  preserves them across a reload cycle — no extra wiring.
- **Id type**: always compare/store as `String(id)` so int-persisted ids match
  the string `p.id`.

## Out of scope

- A distinct "subject" style/colour separate from the user selection (chosen:
  reuse the normal selection style).
- The selector app (A) that writes `subject_parcels`.
- Any change to B's persistence format or `/api/parcels`.
- Camera framing to the subject (auto fit-to-parcels) — possible later, not now.

## Testing

- Syntax gate: `awk` module-script extraction + `node --check`.
- On device, with a manually-seeded `location.json` (until subsystem A exists):
  add `"subject_parcels": [<id>, …]` (ids from `/api/parcels` for that slug) to
  e.g. `tiles_v2_slopne/location.json`, open
  `/heightfield/?slug=slopne` → the parcels overlay auto-enables and the subject
  outlines draw on load, checkbox checked, no click needed.
- Regression: a location whose `location.json` has no `subject_parcels` (or no
  file) opens with an empty selection and no console error, exactly as before.
