# Parcel-Size-Aware Capture Pitch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the 📸 capture pitch angles scale with parcel size so large parcels are framed more top-down (shape fits/reads) while small parcels keep today's landscape angles.

**Architecture:** Replace the module-level `CAPTURE_ANGLES` constant with a pure builder `captureAngles(maxR)` that lerps each pitch tier between a small-parcel preset (today's values) and a large-parcel preset by a clamped size factor derived from `maxR`. Call it once inside `captureParcelAngles` after `maxR` is computed; update the four references and the session header label.

**Tech Stack:** Single-file `heightfield/index.html`, three.js 0.170. No JS test infra — gate = `node --check` syntax check + manual on-device verification.

---

## Important context

- Only file touched: `heightfield/index.html`.
- Spec: `docs/superpowers/specs/2026-06-18-capture-parcel-size-pitch-design.md`.
- Czech UI, English code. Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Syntax gate: `cd /Users/jan/projekty/inzerator && awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin && echo GATE_OK`
- `CAPTURE_ANGLES` references (all to be updated): declaration ~3644; `.length` at ~3709, ~3738, ~3778; `[i]` at ~3739.
- `maxR` and `distance` are computed in `captureParcelAngles` (~3679-3684) BEFORE the references; the builder call goes right after `distance`.

---

### Task 1: Replace the constant with a size-aware builder

**Files:**
- Modify: `heightfield/index.html` — replace the `CAPTURE_ANGLES` const block (~3644-3653).

- [ ] **Step 1: Replace the const.** Find this block:

```js
const CAPTURE_ANGLES = [
  // 8× outer ring, oblique (25° from horizontal — landscape feel)
  ...[0, 45, 90, 135, 180, 225, 270, 315].map(az => ({ az, pitch: 25, tier: 'oblique' })),
  // 4× medium pitch (50° — drone-like)
  ...[0, 90, 180, 270].map(az => ({ az, pitch: 50, tier: 'medium' })),
  // 4× high pitch (70° — near-overhead)
  ...[0, 90, 180, 270].map(az => ({ az, pitch: 70, tier: 'high' })),
  // 4× near-top-down (88°)
  ...[0, 90, 180, 270].map(az => ({ az, pitch: 88, tier: 'top' })),
];
```

Replace it with:

```js
// ── Parcel-size-aware capture angles ──
// Pitch scales with parcel size: a large parcel viewed at a grazing 25° is
// foreshortened and doesn't fit the frame, so the floor rises toward top-down;
// a small parcel keeps today's landscape angles. maxR = max parcel radius (m).
// sizeT ramps 0→1 over [R_SMALL, R_LARGE], clamped. Each tier lerps between a
// small-parcel preset (today's values — regression anchor at maxR≤20) and a
// large-parcel preset (raised floor, top unchanged). 8/4/4/4 = 20 shots, same
// azimuths. Returns { az, pitch, tier }[] (pitch = degrees above horizontal).
const CAPTURE_R_SMALL = 20;    // m — small residential lot radius
const CAPTURE_R_LARGE = 150;   // m — large field; full top-shift at/above
// [small°, large°] pitch band per tier.
const CAPTURE_TIERS = [
  { azs: [0, 45, 90, 135, 180, 225, 270, 315], tier: 'oblique', small: 25, large: 45 },
  { azs: [0, 90, 180, 270],                     tier: 'medium',  small: 50, large: 60 },
  { azs: [0, 90, 180, 270],                     tier: 'high',    small: 70, large: 75 },
  { azs: [0, 90, 180, 270],                     tier: 'top',     small: 88, large: 88 },
];
function captureAngles(maxR) {
  const sizeT = Math.min(1, Math.max(0,
    (maxR - CAPTURE_R_SMALL) / (CAPTURE_R_LARGE - CAPTURE_R_SMALL)));
  const out = [];
  for (const t of CAPTURE_TIERS) {
    const pitch = t.small + (t.large - t.small) * sizeT;
    for (const az of t.azs) out.push({ az, pitch, tier: t.tier });
  }
  return out;
}
```

- [ ] **Step 2: Syntax gate.** Run the gate command. Expected: `GATE_OK`.

- [ ] **Step 3: Verify the builder by hand-reasoning** (no test infra). Confirm:
  - `captureAngles(10)` → oblique entries `pitch === 25`, top `=== 88` (today's values; `sizeT` clamps to 0).
  - `captureAngles(150)` → oblique `45`, medium `60`, high `75`, top `88` (`sizeT === 1`).
  - `captureAngles(85)` → `sizeT === 0.5` → oblique `35`, medium `55`.
  - Length is 20 in all cases (8+4+4+4).

- [ ] **Step 4: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): size-aware capture pitch builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Wire the builder into capture + label the floor

**Files:**
- Modify: `heightfield/index.html` — `captureParcelAngles` body: call site, 4 references, session header.

- [ ] **Step 1: Build the angles after `distance`.** Find (~3684):

```js
  const distance = Math.max(100, maxR * _captureDistMul);
```

Insert immediately after it:

```js
  const distance = Math.max(100, maxR * _captureDistMul);
  // Size-aware angle set (pitch rises toward top-down for large parcels).
  const angles = captureAngles(maxR);
  const obliqueFloor = angles[0].pitch;   // resolved oblique-tier pitch, for the label
```

- [ ] **Step 2: Update the four references.** Replace each `CAPTURE_ANGLES` with `angles`:
  - `načítám 0/${CAPTURE_ANGLES.length}…` → `načítám 0/${angles.length}…`
  - `for (let i = 0; i < CAPTURE_ANGLES.length; i++) {` → `for (let i = 0; i < angles.length; i++) {`
  - `const { az, pitch, tier } = CAPTURE_ANGLES[i];` → `const { az, pitch, tier } = angles[i];`
  - `${i + 1}/${CAPTURE_ANGLES.length}…` → `${i + 1}/${angles.length}…`

- [ ] **Step 3: Show the pitch floor in the session header.** Find (~3722):

```js
  sessionHeader.innerHTML =
    `<span><b style="color:#e7e9ee;">Náhledy ${sessionTs.toLocaleTimeString('cs-CZ')}</b> ` +
    `· ${_captureDistMul}× · vzdálenost ${distance.toFixed(0)} m</span>`;
```

Replace the second line with:

```js
  sessionHeader.innerHTML =
    `<span><b style="color:#e7e9ee;">Náhledy ${sessionTs.toLocaleTimeString('cs-CZ')}</b> ` +
    `· ${_captureDistMul}× · vzdálenost ${distance.toFixed(0)} m · pitch od ${obliqueFloor.toFixed(0)}°</span>`;
```

- [ ] **Step 4: Confirm no stale references.** Run:
  ```bash
  grep -n "CAPTURE_ANGLES" heightfield/index.html
  ```
  Expected: only the declaration lines inside the `CAPTURE_TIERS`/`captureAngles` block from Task 1 (the comment may say "CAPTURE_ANGLES"); NO `CAPTURE_ANGLES.length` / `CAPTURE_ANGLES[i]` left. If the comment still references the old name it's fine; the live identifier `CAPTURE_ANGLES` must not be read anywhere.

- [ ] **Step 5: Syntax gate.** Run the gate command. Expected: `GATE_OK`.

- [ ] **Step 6: Commit.**

```bash
git add heightfield/index.html
git commit -m "feat(heightfield): use size-aware angles in capture + show pitch floor

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Manual on-device verification (no new code)

- [ ] **Step 1: Small parcel.** Open `http://jans-mac-mini:8082/heightfield/?slug=slopne`, select a small lot, click 📸 Generovat náhledy. Expect: session header shows `pitch od 25°`; the 8 oblique shots look like today's landscape angle.

- [ ] **Step 2: Large parcel.** Select a large parcel (or a big field), capture. Expect: header shows `pitch od ~45°` (between 25 and 45 depending on size); the whole parcel fits the frame and reads better than a 25° grazing shot.

- [ ] **Step 3: Regression.** Distance label, orbit, thumbnail grid, AI/ZIP buttons all behave exactly as before — only pitch changed.
