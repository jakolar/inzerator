# Viewer Subject-Parcel Highlight (Subsystem C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox (`- [ ]`) syntax.

**Goal:** On viewer load, read `subject_parcels` from `location.json` and pre-highlight them by seeding the existing parcel-selection overlay.

**Architecture:** A startup async IIFE fetches `/tiles_v2_<slug>/location.json`; if it carries a non-empty `subject_parcels`, it adds them (string-normalised) to `selectedParcels` and triggers the existing `parcelsToggle` change handler. Absent file / empty / unknown ids = silent no-op.

**Tech Stack:** Single-file `heightfield/index.html`, three.js 0.170. No JS test infra — gate = syntax check + manual on-device verification.

---

## Important context

- Spec: `docs/superpowers/specs/2026-06-18-viewer-subject-highlight-design.md`.
- Only file touched: `heightfield/index.html`.
- Czech UI, English code. Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Syntax gate: `cd /Users/jan/projekty/inzerator && awk '/<script type="module">/{f=1;next} /<\/script>/{f=0} f' heightfield/index.html | node --check /dev/stdin && echo GATE_OK`
- Insertion point is locked by anchor, not line number. All referenced symbols (`slug`, `selectedParcels`, `parcelsToggle`, `ensureParcels`, `redrawOutlines`, `asset`) are defined ABOVE the parcels-toggle handler, which runs after the top-level ring-load await — so the init block (placed right after the handler) executes with everything defined and rings built.

---

### Task 1: subject-parcel highlight on load

**Files:**
- Modify: `heightfield/index.html` — insert one block between the `parcelsToggle` change handler and the `parcelsClear` click handler.

- [ ] **Step 1: Insert the init block.** Find the END of the parcels-toggle change handler and the start of the clear handler:

```js
  outlineGroup.visible = true;
  redrawOutlines();
});

parcelsClear.addEventListener('click', () => {
```

Insert the block between them so it reads:

```js
  outlineGroup.visible = true;
  redrawOutlines();
});

// ── Subject-parcel highlight (subsystem C) ──
// Generation (subsystem B) persists the listing's subject parcels in
// tiles_v2_<slug>/location.json. Load the parcels overlay, then highlight the
// matches — compared NUMERICALLY: /api/parcels returns ids like "959535737.0"
// (ČÚZK id is a float) while B persists the int 959535737, so String() compare
// would miss. We add each loaded parcel's own id so redrawOutlines' parcelsById
// lookup hits. Optional file / missing field / unknown ids → silent no-op.
(async () => {
  try {
    const loc = await fetch(`/tiles_v2_${slug}/location.json?t=${Date.now()}`)
      .then(r => r.ok ? r.json() : null);
    const subj = loc?.subject_parcels;
    if (!Array.isArray(subj) || !subj.length) return;
    const want = new Set(subj.map(Number));
    parcelsToggle.checked = true;
    parcelsToggle.disabled = true;
    await Promise.all([ensureParcels(), ensureHeightDataLoaded()]);
    parcelsToggle.disabled = false;
    for (const p of (parcels || [])) {
      if (p.id && want.has(Number(p.id))) selectedParcels.add(p.id);
    }
    outlineGroup.visible = true;
    redrawOutlines();
  } catch (e) {
    console.warn('[subject] location.json highlight skipped:', e);
  }
})();

parcelsClear.addEventListener('click', () => {
```

- [ ] **Step 2: Syntax gate.** Run the gate command. Expected: `GATE_OK`.

- [ ] **Step 3: Confirm wiring.** Run:
  ```bash
  grep -n "subject_parcels\|Subject-parcel highlight" heightfield/index.html
  ```
  Expected: the comment + the `loc?.subject_parcels` read, exactly once, between the toggle handler and `parcelsClear`.

- [ ] **Step 4: Commit.**
  ```bash
  git add heightfield/index.html
  git commit -m "feat(heightfield): pre-highlight subject parcels from location.json

  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 2: manual on-device verification (no new code)

- [ ] **Step 1: Seed a test location.** Pick a slug whose parcels load (e.g. `slopne`). Get a couple of parcel ids from `/api/parcels` for it (or from the viewer by selecting parcels and reading them), then add to `tiles_v2_slopne/location.json` a `"subject_parcels": [<id1>, <id2>]` field (ids as they appear in `/api/parcels`, i.e. the ČÚZK parcel id integers). Use the atomic-write convention if editing programmatically; a manual edit is fine for the test.

- [ ] **Step 2: Verify auto-highlight.** Open `http://<host>:8082/heightfield/?slug=slopne` → the parcels overlay auto-enables (checkbox checked) and the subject outlines draw on load with no click.

- [ ] **Step 3: Regression.** Open a slug whose `location.json` has no `subject_parcels` (or none) → empty initial selection, overlay off, no console error — exactly as before.

- [ ] **Step 4: Cleanup.** Remove the test `subject_parcels` from the seeded `location.json` (it was only for verification; real values come from subsystem A).
