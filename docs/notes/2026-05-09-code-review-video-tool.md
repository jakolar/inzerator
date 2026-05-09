# Code review — drone-video tool ad-hoc additions (2026-05-09)

## Status

**Parked** — to be addressed in a future cleanup pass before adding more
highlights/presets/modes. None of the issues block current usage; all are
latent or maintainability concerns.

## Scope reviewed

18 commits between `88a0c06` (last formal-plan polish) and `5434f1f` (latest
outline-only parcel highlight). Covers: server fixes for `/api/parcels`,
sRGB colorSpace fix, MP4 conversion via ffmpeg.wasm, Pauza/Zrušit controls,
6 toggleable highlights, start angle+distance controls, discoverability
(free-flythrough button + popup-video link), 3 rounds of preset rework
(culminating in the 9 cinematography-briefing presets), single-parcel
click-to-highlight (+nearest fallback +z-occlusion fix +conform-to-terrain
+outline-only), 3 design notes.

Reviewer: superpowers:code-reviewer agent, full diff `88a0c06..5434f1f`.

---

## Critical (1-line fixes, real latent bugs)

### C1. `rasterio` not explicitly imported in two helpers

**Location:** `server.py:526` (`_fetch_parcels_area`) and `server.py:1357`
(`_api_parcel_at_point`).

**Bug:** Both reference `rasterio.open(...)` but never `import rasterio`
locally or at module top. Currently works because some other endpoint
(`_api_building_detail`, etc.) does `import rasterio.mask` first and
leaves `rasterio` in module globals.

**Failure mode:** Cold-start a fresh server process and make `/api/parcels`
or `/api/parcel-at-point` the very first request → `NameError: name
'rasterio' is not defined`.

**Fix:** Add `import rasterio` at the top of each helper, right after
`from pathlib import Path`. (Or hoist `import rasterio` to module top —
see M4.)

### C2. Click handler runs during preview/recording → contaminates MP4

**Location:** `hnojice_multi.html:2311` —
`renderer.domElement.addEventListener('click', ...)`.

**Bug:** No `_videoState` guard. During recording, a click on the canvas:
- Toggles the building popup (DOM, OK)
- Calls `selectParcelAtClick(point.x, point.z)` → adds yellow `TubeGeometry`
  outline mesh to the scene mid-recording → **shows up in the captured MP4**.

**Fix:** Mirror the `contextmenu` handler's gate. At top of click handler:
```js
if (_videoState !== 'idle' && _videoState !== 'panel') return;
```

### C3. `popup-video` link no state guard → state machine corruption

**Location:** `hnojice_multi.html:2387-2419`.

**Bug:** Calls `openVideoPanel(parcel)` unconditionally. If popup is still
on screen (e.g., user opened a building popup before Export, didn't close,
then clicked Export), and then clicks 🎬 Vytvořit video mid-recording →
`openVideoPanel` resets `_videoState = 'panel'` while a recording's
`recorder.onstop` is still pending. Cleanup paths fail.

**Fix:** At top of the link handler:
```js
if (_videoState !== 'idle' && _videoState !== 'panel') {
  ev.preventDefault();
  return;
}
```

---

## Important (worth fixing in a cleanup pass)

### I1. `restorePresentationMode` is O(N×M) on scene size

**Location:** `hnojice_multi.html:653-659`.

**Bug:** One full `scene.traverse` per saved uuid. With ~50 saved parcel
materials × scene of a few hundred objects = thousands of traversal nodes
per close. Cheap today; degrades.

**Fix:** Build `uuid → object` index once, then look up:
```js
const byUuid = new Map();
scene.traverse(o => { if (_savedSceneState.has(o.uuid)) byUuid.set(o.uuid, o); });
for (const uuid of _savedSceneState.keys()) {
  const t = byUuid.get(uuid);
  if (t) restoreMaterialState(t); else _savedSceneState.delete(uuid);
}
```

### I2. `recorder.onerror` cleanup path incomplete

**Location:** `hnojice_multi.html:1396-1408`.

**Missing on error:**
- Clear `_currentRecorder` (set on line 1373; only success-path nulls)
- Reset `_videoCancelled` to false
- Restore sunset tint if active
- Reset `vp-pause` button text/disabled state (only matters if error fires
  after converting transition started)

**Fix:** Factor a single `resetRecordingUI()` helper that all
error/cancel/success paths call.

### I3. `_selectedParcelOutline` is dead state

**Location:** `hnojice_multi.html:265` (declaration), `1647-1651`
(cleanup branch).

**Bug:** Declared, never assigned. Cleanup branch never executes. Dead
since `5434f1f` switched to outline-only TubeGeometry. Same for
`_selectedParcel` — stored at 1674 but never read elsewhere.

**Fix:** Delete `_selectedParcelOutline` declaration + cleanup branch.
Verify `_selectedParcel` truly unread (check `clearSelectedParcel`,
`selectParcelAtClick`); if so, delete that too.

### I4. `userData.videoHighlight` marker is dead

**Location:** Set on `_hlBeam`, `_hlLabel`, `_hlAnts`, `_hlGlow`, pin
group. Read: nowhere.

**Bug:** `restoreHighlights` cleans up via explicit `_hl*` references,
never reads the marker.

**Fix:** Drop the marker, or use it for a sanity-cleanup pass after
`restoreHighlights` to catch leftover orphans (defensive).

### I5. `selectParcelAtClick` has no debounce / in-flight cancel

**Location:** `hnojice_multi.html:2428-2431`.

**Bug:** Rapid clicks fire concurrent `/api/parcel-at-point` requests.
Responses can interleave; last to arrive wins, not necessarily most
recent click → flicker. Each one tears down + rebuilds TubeGeometry +
raycasts the entire ring.

**Fix:**
```js
let _selectClickSeq = 0;
async function selectParcelAtClick(localX, localZ) {
  const seq = ++_selectClickSeq;
  // ...await fetch...
  if (seq !== _selectClickSeq) return null;  // stale, drop
  // ...build mesh...
}
```

### I6. Parcels lazy-load duplicated

**Location:** `popup-video` handler (`hnojice_multi.html:2392-2397`)
re-implements the `/api/parcels` fetch inline rather than using
`ensureParcels`. Reason: "data only, don't make them visible." Solvable
by extracting `ensureParcelsData()`.

**Fix:** Split into two:
```js
async function ensureParcelsData() {
  if (_parcels) return _parcels;
  const r = await fetch(`/api/parcels?gcx=${gcx}&gcy=${gcy}&radius=2000`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  _parcels = await r.json();
  return _parcels;
}
async function ensureParcels() {
  if (_parcelGroup) return _parcelGroup;
  await ensureParcelsData();
  _parcelGroup = buildParcelGroup(_parcels);
  scene.add(_parcelGroup);
  return _parcelGroup;
}
```

### I7. Highlight-management functions = emerging God-cluster

**Affected:** `applyHighlights` / `restoreHighlights` / `tickHighlights`
/ `applySunsetTint` / `restoreSunsetTint` / `applyPresentationMode` /
`restorePresentationMode` — 7 functions touching scene state, sharing
8+ module vars.

**Bug:** Mode-change handler at `1195-1206` manually orchestrates
`restoreHighlights → restorePresentationMode → applyPresentationMode →
applyHighlights` in correct order; same in `closeVideoPanel`, in cancel
paths. One missed call → materials don't restore or memory leaks.

**Refactor proposal:** Single `VideoOverlay` object with `apply(subject,
mode, options)` and `clear()` methods that internally manages
presentation, highlights, and tint. Caller never needs to know the order.

**Worth doing BEFORE adding a 7th highlight, second tint mode, or another
presentation mode.**

---

## Minor (polish / cleanup)

| ID | Location | Issue |
|----|----------|-------|
| M1 | `1226-1233` | `vp-start-angle` / `vp-start-dist` sliders don't gate on `_videoState !== 'panel'` (others do). Harmless — values read at preview/export start. |
| M2 | `1143` | Magic string `parcel.id === '__free__'` vs `subject.free === true`. Pick one. |
| M3 | `864-898` | Sunset tint fragile to scene mutations between apply and restore (e.g. basemap change overwrites color, restore writes wrong baseline). Consider blocking basemap dropdown while in preview/record/converting. |
| M4 | `server.py` | Inconsistent lazy imports: `import json` vs `import json as _json`. Hoist `from pathlib import Path` and `import json` to module top. Removes ~10 redundant lines. |
| M5 | n/a | `_videoStartAngleDeg` / `_videoStartDistanceMul` not reset on new panel open. Sliders' DOM `value` may or may not reset across `display:none` toggles — verify in browser. |
| M6 | `269` | Stale comment: `_videoState = 'idle' \| 'panel' \| 'preview' \| 'recording'` (4 states). Real set is 6: `idle / panel / preview / recording / paused / converting`. |
| M7 | `2063, 2093, 2096, 2141, 2143` | `console.log('[cadastre]...')` debug logging left in. Pre-existing but spammy. |

---

## Concurrent-operation analysis

| Scenario | Behavior | Status |
|----------|----------|--------|
| Click "Parcely" while exporting | Mutates scene mid-recording → MP4 corruption | **Hazard (Critical-adjacent)** |
| Open video panel for different parcel mid-conversion | Right-click contextmenu guard rejects 'converting' | Safe |
| Click handler fires during preview | Parcel highlight added to scene → visible in MP4 | **Critical C2** |
| Sunset-tint apply during pause | Pause doesn't touch tint | Safe |
| Pause during 'converting' | Pause/Cancel disabled in converting | Safe |
| Close panel during preview | Refused (gate check) | Safe |
| Rapid clicks → racing `/api/parcel-at-point` | Last response wins, not most recent | **Important I5** |

---

## Quick-win priority list

1. **C1** — `import rasterio` at top of two helpers (1 line each)
2. **C2** — `_videoState` guard at top of canvas click handler (2 lines)
3. **C3** — `_videoState` guard at top of popup-video link handler (3 lines)
4. **I3** — Delete `_selectedParcelOutline` declaration + dead cleanup branch
5. **M6** — Update stale `_videoState` comment to list all 6 states
6. **M4** — Hoist `import json` and `from pathlib import Path` to top of `server.py`
7. **I4** — Delete `userData.videoHighlight` marker if not adopting for sanity-cleanup

Total: ~15 minutes, zero functional risk, eliminates all C-bugs + dead code.

## Bigger refactors (do before next feature wave)

1. **I7** — `VideoOverlay` abstraction (~1-2 hours). Required before adding a
   7th highlight, 2nd tint mode, or 3rd presentation mode.
2. **I6** — Split `ensureParcels` → `ensureParcelsData` + `ensureParcels`.
   Aligns popup-video flow with main parcel layer flow.
3. **Server endpoints** — `_api_parcels` and `_api_parcel_at_point` share
   near-identical inner DSM-sampling loop. Extract `_with_dsm_tifs(rings,
   gcx, gcy)` helper that yields `ring_local`. ~20 lines saved.

---

## Overall assessment

**File health.** `hnojice_multi.html` at ~2530 lines is at the upper end of
"still navigable in one file." Video tool occupies ~1450 of those (260-1510).
Functions reasonably-sized and named. **Highlight cluster is the hot spot
(I7).**

**State machine.** 6 states with ~12 transition gates inlined as `if`
checks. No central transition table. Mostly works because every entry
point gates carefully, but C2/C3 show how easy it is to forget a guard.

**Resource management.** Texture/geometry/material disposal is mostly
correct. One leak vector: if `applyHighlights` is called twice without
intervening `restoreHighlights`, previous `_hl*` references are
overwritten and orphan THREE objects stay in scene. Mode-change handler
calls `restoreHighlights` first, but if any future caller forgets, leak.

**Plan-deviation review.** All changes user-driven, internally consistent,
no architectural drift. Cinematography preset rework was the biggest
single change and stayed within existing preset-radio pattern.

**Recommended next action.** Fix the 3 Critical items (C1-C3) + 4 quick
wins (15 minutes total) before next feature work. Then schedule I7
refactor before next round of highlight/preset additions.

---

## Cross-references

- `docs/notes/three-js-colorspace-srgb.md` — colorSpace gotcha
- `docs/notes/tile-seam-gaps-open3d-simplify.md` — terrain mesh seams
- `docs/notes/sunset-rays-feature-options.md` — parked sunset feature
- Original drone-video plan: `docs/superpowers/plans/2026-05-08-hnojice-drone-video.md`
- Original drone-video spec: `docs/superpowers/specs/2026-05-08-hnojice-drone-video-design.md`
