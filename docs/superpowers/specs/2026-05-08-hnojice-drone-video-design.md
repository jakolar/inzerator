# Hnojice — Drone-style video tool for realtors

**Date:** 2026-05-08
**Scope:** `hnojice_multi.html` only. No server changes (point-in-polygon
runs client-side; existing `/api/parcels` + `/api/buildings` provide all
the data needed).
**Goal:** Self-service browser tool that lets a realtor click a parcel,
pick a camera preset, and download a 20–30s 1080p video of a drone-style
flyover of that property. Browser-only — no server-side rendering, no
new dependencies, runs on the same Three.js scene the area viewer
already builds.

## Motivation

Hnojice 3D viewer renders the village's terrain + ortofoto + building
extrusions + (now) parcel tiles. With the parcel layer in place, the
scene already expresses everything a realtor wants to communicate about
a property: location in village, parcel boundaries, surrounding land
use, access roads, building footprint. Today there is no way to *export*
that as marketing material — the realtor has to screen-record manually
and crop the result. A first-class export pipeline turns the existing
viewer into a video generator that produces consistent, branded-able
outputs in under a minute per property.

## User flow

1. Realtor opens `hnojice_multi.html`, toggles **Parcely** on
   (existing button from prior plan).
2. Right-click on the target parcel → context menu with one item:
   "🎬 Video pro tuto parcelu". (Alternative entry point: a "Video"
   button added to the parcel popup, which already opens on left
   click.)
3. A floating **Video panel** opens at the right edge of the viewport:
   - Mini preview area (uses the main canvas — the panel just
     overlays controls; no separate WebGL context)
   - Preset radio: ⚪ Flyover (25s) ⚪ Orbit (20s) ⚪ Approach (15s)
     ⚪ Top-down (15s) — Flyover selected by default
   - Subject mode radio: ⚪ Property (parcel + budova) ⚪ Land only
   - Toggle ☐ Info overlay (default off)
   - Slider "Délka" 15–45 s (default = preset's nominal length)
   - Buttons: **Preview** (animate camera, no recording), **Export**
     (animate + record + download), **Zavřít**
4. Preview: camera jumps to preset's first frame, animates over the
   selected duration, then returns to interactive (OrbitControls).
   Subject highlight is applied during preview; reverts after.
5. Export: same animation, but wrapped in `MediaRecorder.start()` /
   `stop()`. On stop, browser triggers download of
   `parcela-{label}-{preset}-{YYYYMMDD-HHMM}.webm`.
6. During Export: panel shows a progress bar driven by elapsed/total
   time (the recording is real-time). All UI buttons disabled until
   stop completes.

## 4 camera presets

Each preset receives a `subject` object with:
- `centroid_local` — `[x, z]` derived from `mean(ring_local[*][0,1])`
- `bbox_local` — axis-aligned `{ minX, maxX, minZ, maxZ }` from `ring_local`
- `diagonal_m` — `sqrt((maxX-minX)^2 + (maxZ-minZ)^2)`
- `top_y_m` — `max(ring_local[*][2]) + 5` (5 m above terrain → safe altitude reference)
- `nearest_road_local` — closest point on any road polyline (server already
  exposes `/api/roads`; if response empty, fall back to a point 200 m due
  west of centroid at terrain level)

All paths use `THREE.CatmullRomCurve3` for the camera position track and
a parallel `THREE.CatmullRomCurve3` for the lookAt target. Sample at
`t = smoothstep(0, 1, elapsed / duration)` to get ease-in-out motion.
LookAt vector is recomputed per frame (`camera.position.copy(curve_pos.getPointAt(t)); camera.lookAt(curve_target.getPointAt(t))`).

### Flyover (default 25 s)
- `pos[0]` = centroid + `(-300, 200, -300)` (200 m up, ~424 m NW)
- `pos[0.4]` = centroid + `(-100, 120, -100)`
- `pos[1]` = centroid + `(80, 50, 0)` (50 m up, 80 m E)
- `target[*]` = centroid_local at terrain Y
- "Where it is, how it fits in the village"

### Orbit (default 20 s)
- Radius `R = max(40, diagonal_m * 1.5)`
- Altitude `H = max(50, diagonal_m * 0.8)`
- 360° around centroid: 16 control points
  `pos[i] = centroid + R*(cos θ, 0, sin θ) + (0, H, 0)`, `θ = 2π * i/16`
- `posCurve.closed = true` so the start/end seam is C¹-smooth
  (otherwise the orbit "jumps" at t=1 because Catmull-Rom open
  splines extrapolate from the last segment)
- `target[*]` = centroid at terrain Y
- "How the parcel looks from every side"

### Approach (default 15 s)
- `pos[0]` = `nearest_road_local` + `(0, 30, 0)`
- `pos[0.5]` = midpoint between road and centroid + `(0, 45, 0)`
- `pos[1]` = centroid + `(0, 60, 0)`
- `target[*]` = centroid at terrain Y
- "Approach from the road, just like driving up"

### Top-down (default 15 s)
- Static XZ position = centroid_local
- `pos[0]` = `(0, 120, 0)` above centroid
- `pos[1]` = `(0, 80, 0)` above centroid (mild dolly zoom)
- `target[*]` = centroid_local at terrain Y (camera looking straight
  down, slight zoom in)
- "How big it is, how it sits relative to neighbours"

## Subject visualization

Triggered when the Video panel opens — the realtor sees the
presentation-mode scene while picking the preset, so the Preview
button matches what Export will record. Reverted only when the
panel closes (Zavřít or Esc). Export completion does NOT revert —
the panel stays open so the realtor can record another preset for
the same parcel without re-applying highlights.

State changes captured before showing, restored after:
- For each parcel `Group` in `_parcelGroup.children`: original
  `topMaterial.color`, `.opacity`, `.userData.savedColor`.
- For each building mesh in the OSM building layer (existing in
  `hnojice_multi.html`): original material + opacity.

### Property mode (default)

- **Subject parcel:** top opacity → 1.0, color → `0xfde047` (yellow);
  side material color → same yellow, opacity 1.0
- **Subject building** (if any — see detection below): material
  `emissive = 0x67e8f9`, `emissiveIntensity = 0.45` (cyan glow);
  add a `THREE.LineSegments` with `EdgesGeometry` of the building
  mesh, color `0x67e8f9`
- **Other parcels:** top opacity 0.30, side opacity 0.30 (color
  unchanged — context tones stay readable)
- **Other buildings:** `material.transparent = true; material.opacity =
  0.65; material.depthWrite = false`. The depthWrite=false avoids the
  half-transparent buildings punching holes through each other.
  Restore-state must remember the original `transparent` and
  `depthWrite` values too, not just `opacity`.

### Land mode

- Subject parcel highlighted as in Property mode
- No building highlighting at all (subject building stays in original
  state along with all others)
- Other parcels + buildings ditched to 0.30 / 0.65 same as Property
  mode

### Subject building detection

For the clicked parcel's `ring_local`, run point-in-polygon test
against the centroid of every loaded OSM building. The OSM building
data is already loaded by the existing `hnojice_multi.html` flow
(via `/api/buildings`); each building has a footprint polygon in
local coords.

Use ray-casting algorithm against parcel ring (outer ring only,
matching server's v1 behaviour). First hit = subject building. Store
on subject object as `subject_building_idx` (index into the existing
buildings array) for state save/restore.

### Info overlay (toggle, default off)

When enabled and during preview/recording, draw on the canvas via
`CanvasRenderingContext2D` — overlay rendered in the same WebGL
canvas via a final pass before `MediaRecorder.captureStream` reads
the frame. Specifically:

1. After each `renderer.render(scene, camera)` in the recording
   tick, draw via the renderer's underlying `gl` context using a
   small 2D overlay canvas composited onto the main canvas. The
   simplest reliable path: keep a hidden `<canvas>` of size
   matching the WebGL canvas, draw the overlay text via 2D context,
   then `texSubImage2D` it as a final blit. Overhead per frame:
   sub-millisecond.

   *Alternative if texSubImage2D plumbing is too heavy:* render to
   an offscreen `OffscreenCanvas` 2D, then use a transparent HTML
   `<canvas>` overlay on top of the WebGL canvas. The MediaRecorder
   `captureStream` is taken from the **WebGL canvas only**, so the
   HTML overlay would NOT be in the recording. Bad.

   The texSubImage2D approach is mandatory; use that.

2. Overlay content (left-bottom corner, 16 px from edges):
   ```
   ┌──────────────────────────────┐
   │ č.p. 47                      │ (or "parcela 423/2" if no č.p.)
   │ 1 245 m² · zahrada           │
   └──────────────────────────────┘
   ```
   Background: `rgba(0,0,0,0.55)` rounded rect
   Text: white, 18 px (1080p), font matches existing UI
   (`system-ui`)

3. When overlay toggle is off, no canvas blit — pure WebGL output.

## Recording pipeline

Built on `MediaRecorder` + `HTMLCanvasElement.captureStream`.

```js
async function exportVideo(preset, subject, durationMs, includeOverlay) {
  saveSceneState(subject);
  applySubjectHighlight(subject, /* mode */ 'property' or 'land');
  const stream = renderer.domElement.captureStream(30);
  const supportedMime =
    MediaRecorder.isTypeSupported('video/webm;codecs=vp9')
      ? 'video/webm;codecs=vp9'
      : 'video/webm';
  const recorder = new MediaRecorder(stream, {
    mimeType: supportedMime,
    videoBitsPerSecond: 8_000_000,        // 8 Mbps for 1080p — fine for 30s
  });
  const chunks = [];
  recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
  const path = buildCameraPath(preset, subject);
  const startTime = performance.now();
  recorder.start();
  // Replace the existing tick loop's camera update with our path-driven update
  startVideoTick(path, durationMs, includeOverlay);
  await new Promise(r => setTimeout(r, durationMs + 100));
  recorder.stop();
  await new Promise(r => recorder.onstop = r);
  stopVideoTick();
  restoreSceneState();
  const blob = new Blob(chunks, { type: supportedMime });
  triggerDownload(blob, `parcela-${subject.label}-${preset}-${ts()}.webm`);
}
```

`startVideoTick` swaps the existing `tick` function reference. The
existing render loop calls `_currentTick()` (a module-scope ref). In
interactive mode it's the existing `interactiveTick`. During video
preview/export it's `videoTick(path, durationMs, t0, includeOverlay)`
which:
- computes `t = smoothstep(0, 1, (now - t0) / durationMs)`
- sets `camera.position` and `camera.lookAt(...)` from the curves at `t`
- calls `renderer.render(scene, camera)`
- if overlay: blits 2D overlay canvas via `texSubImage2D` (see
  Info overlay section)
- if `t >= 1`, signals completion and `stopVideoTick()` swaps back to
  `interactiveTick`

Frame rate: matches whatever `requestAnimationFrame` delivers on the
client (target 30 fps, may dip to ~24 fps with full Hnojice load).
Real-time recording captures whatever is rendered; the resulting
video plays smoothly on any decoder, just with fewer unique frames if
the browser slowed down. Acceptable for v1.

Container/codec: `video/webm;codecs=vp9` first, fallback
`video/webm`. No mp4/h264 — would need ffmpeg.wasm (~5 MB extra dep).
WebM plays natively on all modern browsers including iOS Safari
14.1+ (Sept 2021). Realtors with very old devices can convert via
HandBrake / VLC.

Bitrate 8 Mbps × 25 s ≈ 25 MB. Fine for download/email/Slack/Drive.

## Files touched

Single file: `hnojice_multi.html`.

Estimated additions:
- ~30 lines: video panel HTML/CSS
- ~80 lines: video panel JS (open/close, preset selection, slider,
  Preview/Export wire-up)
- ~120 lines: 4 preset path-builder functions
- ~50 lines: subject highlight save/apply/restore
- ~40 lines: point-in-polygon + subject building detection
- ~30 lines: info overlay 2D canvas + texSubImage2D blit
- ~30 lines: MediaRecorder pipeline (`exportVideo`, `videoTick`)

Total: ~380 lines net add to `hnojice_multi.html`.

No server changes. No new endpoints. No new dependencies (Three.js
ShapeUtils/CatmullRomCurve3 already in r170 examples; WebGL2 +
MediaRecorder + captureStream are baseline browser APIs from 2018+).

## What we are NOT doing (YAGNI)

- **No mp4 / h264 export** in v1 — webm is sufficient for 99 % of
  realtor distribution channels (Instagram, Facebook, WhatsApp,
  email, Drive). Add ffmpeg.wasm later if a specific client demands
  mp4.
- **No server-side render** — everything runs in the realtor's
  browser. No headless Chrome instance to operate.
- **No 4K** — 1080p is plenty for property marketing on social /
  mobile.
- **No music / voiceover** — silent video, realtor adds audio in
  their editing tool of choice (Premiere, CapCut, free options).
- **No logo / branded watermark** — different realtors, different
  brands. Postpone until at least one customer asks.
- **No multi-property "tour"** — one parcel per video. Stitching is
  an editing-tool job.
- **No upload to YouTube / Drive** — realtor downloads the file and
  uploads from their own account.
- **No scheduling / queue** — synchronous recording, one at a time.
- **No iOS / mobile UX optimization** — realtor uses desktop /
  laptop; touch UI later if demand.

## Test plan

Manual, with `server.py` running and Hnojice viewer loaded:

1. Toggle Parcely on. Right-click any building parcel (use_code 13).
   Context menu shows "🎬 Video pro tuto parcelu". Open it.
2. Video panel appears with Flyover preset selected. Click **Preview**.
   Camera animates over 25 s; subject parcel turns yellow, subject
   building (the house) glows cyan, surrounding parcels dim. After
   25 s, scene returns to interactive (OrbitControls works again).
3. Switch preset to **Orbit**. Click Preview. Camera makes one full
   loop around the parcel at the right altitude. No drift.
4. Switch to **Approach**. Verify the start point is along the
   nearest road, end point above the parcel. (If `/api/roads`
   returns empty, the fallback 200 m W of centroid kicks in — log
   in console.)
5. Switch to **Top-down**. Camera holds steady above the parcel
   while slowly zooming in.
6. Toggle **Info overlay**. Run Preview again — info pill appears
   in lower-left corner with č.p. + area + use type.
7. Switch subject mode to **Land only**. Run Preview. The subject
   parcel still highlights yellow, but the building on it is no
   longer cyan-glowing.
8. Click **Export** with Property mode + Flyover. Progress bar
   advances, scene animates. After 25 s, browser downloads
   `parcela-{label}-flyover-{ts}.webm`. Open it locally —
   playback shows the same animation, with subject highlight and
   (if toggled) overlay baked in. File size ~20–30 MB.
9. Repeat Export for Orbit, Approach, Top-down. Confirm each
   preset produces a coherent, watchable video.
10. Negative: pick a forest parcel (no building). Run Preview in
    Property mode — subject parcel highlights but no building glow
    (no subject_building found, code path skipped cleanly).
11. Negative: try Export while another Export is in progress —
    Export button disabled, second click does nothing.
