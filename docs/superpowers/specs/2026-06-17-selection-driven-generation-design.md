# Selection-driven generation (subsystem B) — parametric rings + parcel persistence

Date: 2026-06-17
Status: approved (Jan, 2026-06-17 — design + B params approved in chat:
"ano, sedí, napiš spec B")

Part of a 3-subsystem feature (build order **B → C → A**):
- **B (this spec):** inzerator pipeline accepts a selection extent → generates a
  model sized to it, and persists the chosen parcels.
- C: the 3D viewer pre-highlights the persisted parcels (separate spec).
- A: the standalone parcel-selector app (fork of `protoparcley`) that computes
  the extent and calls B (separate spec, new repo).

## Problem

Today `gen_heightfield.py` builds a fixed 2-ring pyramid (`DEFAULT_RINGS`:
closeup half 1500 m / step 1.5 m, inner half 500 m / step 0.5 m) centred on the
RÚIAN-searched `(cx, cy)`. The new operator flow picks specific parcels; the
model must be **centred on and sized to that selection**, with the selected
parcels always inside the inner (detail) ring, and the selection must be
**persisted** so the viewer can highlight it. The generator already supports a
`--rings <json-file>` override, so the core gen loop needs no change — only a
derivation rule, a CLI entry point, and job/persistence wiring.

## Design

### A) Ring derivation rule (pure, testable)

Add `derive_rings(inner_half: float) -> list[dict]` to `gen_heightfield.py`.
Given a desired inner-ring half-extent (metres), return a 2-ring config in the
same shape as `DEFAULT_RINGS`:

```
inner_half  = clamp(inner_half, 500, 2000)          # MIN 500 m (≈ today), MAX 2000 m
closeup_half = 3 * inner_half                        # keep today's 1500/500 ratio
# Constant ~2000² grid per ring → bounded data regardless of model size:
inner_step   = inner_half  / 1000                    # 500→0.5 m (today), 2000→2.0 m
closeup_step = closeup_half / 1000                   # 1500→1.5 m (today)
rings = [
  {slug:"closeup", half:closeup_half, step:closeup_step, ortho_size:4096,
   max_z_error: default_max_z_error_for_step(closeup_step)},
  {slug:"inner",   half:inner_half,   step:inner_step,   ortho_size:4096,
   max_z_error: default_max_z_error_for_step(inner_step)},
]
```

- `inner_half = 500` reproduces today's pyramid exactly (closeup 1500/1.5,
  inner 500/0.5) — the regression anchor for tests.
- The MAX 2000 m clamp bounds the model (inner 4 km / closeup 12 km). A selection
  whose bbox exceeds it is still generated at the clamp; the caller (subsystem A)
  is responsible for the +15 % margin and for warning the operator when the
  bbox already exceeds the clamp. B just clamps defensively.
- `ortho_size` stays 4096 (HIGH tier) as today; `max_z_error` derives from the
  (now variable) step via the existing `default_max_z_error_for_step`.

### B) gen_heightfield CLI entry point

Add `--inner-half <m>` to `gen_heightfield.py`. When present, the ring list is
`derive_rings(inner_half)` instead of `DEFAULT_RINGS`. Mutually exclusive with
`--rings <file>` (explicit file still wins / is its own path). When neither is
given, behaviour is unchanged (`DEFAULT_RINGS`) — the legacy RÚIAN flow is
untouched.

```
python3 gen_heightfield.py --slug X --cx=.. --cy=.. --inner-half 750
```

### C) Job + persistence wiring (`locations.py`, `server.py`)

- `enqueue_job(slug, label, cx, cy, …)` gains two optional params:
  `inner_half: float | None = None`, `parcel_ids: list[int] | None = None`.
  They are stored on the job dict so the worker can reach them.
- `cmd_for("heightfield", slug, cx, cy, inner_half=None)` appends
  `--inner-half <m>` to the `gen_heightfield.py` command when `inner_half` is
  set. The worker call site (currently `cmd_for(step["name"], job["slug"],
  job["cx"], job["cy"])`) passes `job.get("inner_half")`.
- `_persist_location_meta(slug, label, cx, cy, inner_half=None, parcel_ids=None)`
  writes the extra fields into `tiles_v2_<slug>/location.json` (atomic write
  pattern unchanged):
  ```json
  { "slug": "...", "label": "...", "cx": -508439.1, "cy": -1172677.8,
    "created_at": 0, "inner_half": 750, "subject_parcels": [123, 456] }
  ```
  Both new fields are omitted (or null) for the legacy RÚIAN flow. `subject_parcels`
  is the list of ČÚZK parcel IDs the viewer (subsystem C) will highlight.
- `server.py` `/api/jobs` POST handler accepts optional `inner_half` (number) and
  `parcel_ids` (array of ints) in the JSON body and forwards them to
  `enqueue_job`. Validation: `inner_half` a positive finite number or absent;
  `parcel_ids` a list of ints (cap length, e.g. ≤ 500) or absent.

### Backward compatibility

Every new parameter is optional. The dashboard "Nová lokace" RÚIAN flow sends
neither → `DEFAULT_RINGS`, no `subject_parcels` in `location.json`. Existing
locations and their `location.json` files are unaffected (readers must treat the
new fields as optional).

## Data contract (B's public surface)

`POST /api/jobs` body (superset of today's):
```
{ "slug": str, "label": str, "cx": number, "cy": number,
  "inner_half"?: number,        // metres; B clamps to [500, 2000]
  "parcel_ids"?: number[] }      // ČÚZK parcel IDs; persisted for the viewer
```

## Out of scope

- The selector app (subsystem A) — computes `cx/cy/inner_half/parcel_ids`.
- Viewer highlight of `subject_parcels` (subsystem C).
- Variable `ortho_size` / super-tier per model size (stays 4096).
- The in-process `sm5` pre-warm step (`locations._do_sm5_download`) uses a fixed
  `half=2500`. For large selections (closeup half up to 6000 m) it now derives
  `max(2500, 3 × clamp(inner_half))` so the pre-warm matches the model extent;
  even without that, the heightfield subprocess self-heals missing sheets via
  `ensure_sm5_cached(fetch_missing=True)`, so a large extent never yields empty
  ortho — only a slower first run.

## Testing

- **Unit (`derive_rings`)**: `inner_half=500` → exactly today's two rings
  (1500/1.5, 500/0.5); clamping below 500 and above 2000; step scaling
  (`inner_half=1000` → inner step 1.0, closeup 3000/3.0); `max_z_error` populated
  from step.
- **Unit (`cmd_for`)**: `inner_half` set → `--inner-half` in the argv; absent →
  unchanged command.
- **Unit (`_persist_location_meta`)**: writes `inner_half` + `subject_parcels`
  when given; omits them otherwise; atomic `.json.tmp` → replace preserved.
- **Integration**: enqueue with `inner_half=750` → generated
  `heightfield/manifest.json` rings reflect the derived halves/steps;
  `location.json` carries `subject_parcels`. (Heavy — gated/optional in CI;
  unit tests are the fast path.)
