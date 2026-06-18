# Parcel-size-aware capture pitch

Date: 2026-06-18
Status: approved (Jan, 2026-06-18 — design approved in chat: "sedí, napiš spec
a spusť implementaci")

## Problem

The "📸 Generovat náhledy" feature (`captureParcelAngles` in
`heightfield/index.html`) orbits the camera around a selected parcel and snaps
20 PNGs at preset azimuth/pitch combos, for two uses: input to the AI image-edit
pipeline AND final marketing photos of the plot. The pitch angles are a fixed
constant (`CAPTURE_ANGLES`):

- 8× oblique `pitch 25°`, 4× medium `50°`, 4× high `70°`, 4× near-top `88°`.

Two problems:

1. **Too grazing.** The 8 oblique shots at 25° are near the horizon; on most
   parcels the plot is foreshortened and partly hidden behind terrain.
2. **Pitch ignores parcel size.** A large parcel viewed at 25° does not fit the
   frame and its shape is unreadable; a small parcel tolerates a low angle and
   benefits from the landscape look. Distance already scales with parcel size
   (`maxR × distMul`), but pitch is constant.

## Design

Make the four pitch tiers a continuous function of parcel size. Keep everything
else (20 shots, 8/4/4/4 azimuth structure, distance rule, orbit, capture/render
loop) unchanged.

### Size factor

`maxR` (max distance from the parcel centroid to any ring vertex, in metres) is
already computed in `captureParcelAngles` for the distance calc. Reuse it:

```
const R_SMALL = 20;    // m — small residential lot radius (≈ today's anchor)
const R_LARGE = 150;   // m — large field; at/above this, full top-shift
const sizeT = Math.min(1, Math.max(0, (maxR - R_SMALL) / (R_LARGE - R_SMALL)));
```

`sizeT` is 0 for small parcels, ramps to 1 at `maxR ≥ R_LARGE`, clamped both
ends. No threshold/cliff — fully continuous.

### Pitch tiers by size

Each of the four tiers lerps between a small-parcel preset (today's values, the
regression anchor) and a large-parcel preset (raised floor, top unchanged):

| tier    | shots | small (`sizeT=0`) | large (`sizeT=1`) |
|---------|-------|-------------------|-------------------|
| oblique | 8     | 25°               | 45°               |
| medium  | 4     | 50°               | 60°               |
| high    | 4     | 70°               | 75°               |
| top     | 4     | 88°               | 88°               |

`pitch = small + (large - small) * sizeT`, per tier.

Consequences:
- `maxR ≤ 20 m` → exactly today's angles (25/50/70/88) — regression anchor.
- `maxR ≥ 150 m` → 45/60/75/88: the oblique floor rises ~20°, shape fits and
  reads, top stays near-overhead.
- Always four distinct tiers → angular variety preserved at every size.
- The azimuth pattern, `tier` label strings, distance, and orbit are unchanged.

### Where

`CAPTURE_ANGLES` is currently a module-level `const` array. Replace it with a
pure builder `captureAngles(maxR)` that returns the same array shape
(`{ az, pitch, tier }[]`), called once inside `captureParcelAngles` after `maxR`
is computed. The capture loop already reads `az`/`pitch`/`tier` per entry — no
loop change.

### Meta label

The capture-session header already shows distance
(`· {distMul}× · vzdálenost {distance} m`). Append the derived oblique floor so
the operator sees what `maxR` produced, e.g. `· pitch od {obliqueFloor}°`
(rounded), where `obliqueFloor` is the oblique tier's resolved pitch.

## Edge cases

- **Tiny parcel** (`maxR < 20`): `sizeT` clamps to 0 → today's angles. Safe.
- **Huge parcel** (`maxR > 150`): `sizeT` clamps to 1 → max top-shift. Safe.
- **Degenerate `maxR = 0`** (single-point ring): already guarded upstream
  (`distance = max(100, …)`); `sizeT = 0`, angles default. No division issue
  (`R_LARGE - R_SMALL` is a nonzero constant).

## Out of scope

- Distance/`distMul` changes (already parcel-scaled and user-tunable).
- Per-shot fit guarantee from FOV (rejected approach B — overshoots to top-down,
  couples to FOV).
- Changing the number of shots, azimuth set, or tier count.
- A user-facing pitch slider (rejected — auto-by-size chosen).

## Testing

- Syntax gate: `awk` module-script extraction + `node --check` → `GATE_OK`.
- Unit-ish (pure builder, in-browser/console or by reasoning): `captureAngles(10)`
  → oblique tier 25°, top 88° (today's values); `captureAngles(150)` → oblique
  45°, medium 60°, high 75°, top 88°; `captureAngles(85)` (midpoint, `sizeT=0.5`)
  → oblique 35°.
- On device: capture a small lot → oblique shots near 25°, header shows
  `pitch od 25°`; capture a large parcel → oblique near 45°, whole parcel fits
  the frame, header shows `pitch od ~45°`.
