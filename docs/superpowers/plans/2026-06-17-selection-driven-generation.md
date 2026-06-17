# Selection-Driven Generation (Subsystem B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the inzerator pipeline generate a model sized to a selected-parcel extent and persist the chosen parcels, all behind optional parameters that leave the existing RÚIAN flow unchanged.

**Architecture:** A pure `derive_rings(inner_half)` builds a 2-ring config (clamped, step-scaled); a `--inner-half` CLI selects it; `locations.py` threads `inner_half`/`parcel_ids` through `enqueue_job` → job dict → `cmd_for` (`--inner-half`) and `_persist_location_meta` (`location.json`); `server.py /api/jobs` parses them via a testable helper.

**Tech Stack:** Python 3.9, pytest. `gen_heightfield.py`, `locations.py`, `server.py`.

---

## Important context for every task

- **Spec:** `docs/superpowers/specs/2026-06-17-selection-driven-generation-design.md`.
- **Files:** `gen_heightfield.py`, `locations.py`, `server.py`, tests under `tests/`.
- **Line numbers are approximate** — locate by quoted anchors.
- **Tests run without a server** (pure unit). Gate after each task:
  ```bash
  cd /Users/jan/projekty/inzerator && python3 -m pytest tests/test_gen_heightfield_unit.py tests/test_locations_unit.py -q
  ```
- Czech UI strings, English code/comments. Conventional commits + trailer:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- **Backward compat is a hard requirement:** every new param is optional; with none set the behaviour is byte-for-byte the legacy flow (`DEFAULT_RINGS`, no extra `location.json` keys).
- Reference existing code: `DEFAULT_RINGS` + `default_max_z_error_for_step` at `gen_heightfield.py:49–59`; ring resolution at `gen_heightfield.py:813` (`rings = DEFAULT_RINGS` / `if args.rings:`); `cmd_for` at `locations.py:854`; `_persist_location_meta` at `locations.py:92`; `enqueue_job` at `locations.py:734` (builds the job via `_new_job`).

---

### Task 1: `derive_rings()` — selection-sized ring config

**Files:**
- Create: `tests/test_gen_heightfield_unit.py`
- Modify: `gen_heightfield.py` (add `derive_rings` next to `default_max_z_error_for_step`, ~line 59)

- [ ] **Step 1: Write the failing test.** Create `tests/test_gen_heightfield_unit.py`:

```python
import gen_heightfield as gh


def test_derive_rings_default_geometry():
    """inner_half=500 reproduces today's halves/steps (the regression anchor);
    max_z_error follows the step formula."""
    rings = gh.derive_rings(500)
    assert [r["slug"] for r in rings] == ["closeup", "inner"]
    closeup, inner = rings
    assert (closeup["half"], closeup["step"]) == (1500, 1.5)
    assert (inner["half"], inner["step"]) == (500, 0.5)
    assert closeup["ortho_size"] == 4096 and inner["ortho_size"] == 4096
    assert closeup["max_z_error"] == gh.default_max_z_error_for_step(1.5)
    assert inner["max_z_error"] == gh.default_max_z_error_for_step(0.5)


def test_derive_rings_clamps():
    assert gh.derive_rings(100)[1]["half"] == 500      # below MIN → 500
    assert gh.derive_rings(9999)[1]["half"] == 2000    # above MAX → 2000
    assert gh.derive_rings(9999)[0]["half"] == 6000    # closeup = 3× inner


def test_derive_rings_step_scales_with_half():
    closeup, inner = gh.derive_rings(1000)
    assert (inner["half"], inner["step"]) == (1000, 1.0)
    assert (closeup["half"], closeup["step"]) == (3000, 3.0)
```

- [ ] **Step 2: Run it; verify it fails.**

Run: `python3 -m pytest tests/test_gen_heightfield_unit.py -q`
Expected: FAIL — `AttributeError: module 'gen_heightfield' has no attribute 'derive_rings'`.

- [ ] **Step 3: Implement `derive_rings`.** In `gen_heightfield.py`, directly after the `default_max_z_error_for_step` function (~line 59), add:

```python
def derive_rings(inner_half):
    """2-ring pyramid sized to a parcel selection (subsystem B).

    inner_half (m) is clamped to [500, 2000]; closeup = 3× inner (today's
    ratio); step = half/1000 keeps each ring a ~2000² grid so data stays
    bounded as the model grows (detail-per-metre degrades instead). At
    inner_half=500 the geometry equals DEFAULT_RINGS.
    """
    inner_half = max(500.0, min(2000.0, float(inner_half)))
    closeup_half = 3.0 * inner_half
    inner_step = inner_half / 1000.0
    closeup_step = closeup_half / 1000.0
    return [
        {"slug": "closeup", "half": closeup_half, "step": closeup_step,
         "ortho_size": 4096, "max_z_error": default_max_z_error_for_step(closeup_step)},
        {"slug": "inner", "half": inner_half, "step": inner_step,
         "ortho_size": 4096, "max_z_error": default_max_z_error_for_step(inner_step)},
    ]
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `python3 -m pytest tests/test_gen_heightfield_unit.py -q`
Expected: PASS (3 tests). Note: `1500.0 == 1500` is True in Python, so the float halves satisfy the integer asserts.

- [ ] **Step 5: Commit.**

```bash
git add gen_heightfield.py tests/test_gen_heightfield_unit.py
git commit -m "feat(gen): derive_rings — selection-sized 2-ring pyramid

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `resolve_rings()` + `--inner-half` CLI

**Files:**
- Modify: `gen_heightfield.py` (add `resolve_rings`, add `--inner-half` arg, use `resolve_rings` at the ring-resolution site ~line 813)
- Modify: `tests/test_gen_heightfield_unit.py` (add resolve_rings tests)

- [ ] **Step 1: Write the failing test.** Append to `tests/test_gen_heightfield_unit.py`:

```python
def test_resolve_rings_default_when_nothing_set():
    assert gh.resolve_rings(None, None) is gh.DEFAULT_RINGS


def test_resolve_rings_inner_half():
    rings = gh.resolve_rings(None, 1000)
    assert rings[1]["half"] == 1000 and rings[1]["step"] == 1.0


def test_resolve_rings_file_wins(tmp_path):
    import json
    f = tmp_path / "rings.json"
    f.write_text(json.dumps([{"slug": "x", "half": 42, "step": 1,
                              "ortho_size": 4096, "max_z_error": 0.1}]))
    rings = gh.resolve_rings(str(f), 1000)   # file takes precedence over inner_half
    assert rings == [{"slug": "x", "half": 42, "step": 1,
                      "ortho_size": 4096, "max_z_error": 0.1}]
```

- [ ] **Step 2: Run it; verify it fails.**

Run: `python3 -m pytest tests/test_gen_heightfield_unit.py -q`
Expected: FAIL — `resolve_rings` not defined.

- [ ] **Step 3: Implement `resolve_rings`.** In `gen_heightfield.py`, directly after `derive_rings`, add:

```python
def resolve_rings(rings_file, inner_half):
    """Pick the ring list: explicit --rings file wins, then --inner-half
    (selection-driven), else the fixed DEFAULT_RINGS (legacy RÚIAN flow)."""
    if rings_file:
        return json.loads(Path(rings_file).read_text())
    if inner_half is not None:
        return derive_rings(inner_half)
    return DEFAULT_RINGS
```

- [ ] **Step 4: Add the `--inner-half` argument.** In the argparse block, directly after the `--rings` argument (`gen_heightfield.py:739–741`), add:

```python
    p.add_argument("--inner-half", type=float, default=None,
                   help="inner-ring half-extent in m (selection-driven gen). "
                        "Builds a 2-ring pyramid via derive_rings (clamped "
                        "500–2000). Ignored if --rings is given. Default: "
                        "DEFAULT_RINGS fixed preset.")
```

- [ ] **Step 5: Use `resolve_rings` at the resolution site.** Replace the block at `gen_heightfield.py:813–825` that currently reads:

```python
    rings = DEFAULT_RINGS
    if args.rings:
        rings = json.loads(Path(args.rings).read_text())
```

(through its `print(f"Using custom rings from {args.rings}: …")` line) with:

```python
    rings = resolve_rings(args.rings, args.inner_half)
    if args.rings:
        print(f"Using custom rings from {args.rings}: "
              f"{[r['slug'] for r in rings]}")
    elif args.inner_half is not None:
        print(f"Using selection-driven rings (inner_half={args.inner_half}): "
              f"inner half={rings[1]['half']} step={rings[1]['step']}, "
              f"closeup half={rings[0]['half']} step={rings[0]['step']}")
```

(Preserve any code AFTER that print that uses `rings`; only the DEFAULT_RINGS/`if args.rings` selection + its print is replaced. Read the surrounding lines first to keep the rest intact.)

- [ ] **Step 6: Run tests; verify pass.**

Run: `python3 -m pytest tests/test_gen_heightfield_unit.py -q`
Expected: PASS (6 tests).

- [ ] **Step 7: Commit.**

```bash
git add gen_heightfield.py tests/test_gen_heightfield_unit.py
git commit -m "feat(gen): --inner-half CLI + resolve_rings selector

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Thread `inner_half` / `parcel_ids` through `locations.py`

**Files:**
- Modify: `locations.py` (`cmd_for`, `_persist_location_meta`, `enqueue_job`, the worker `cmd_for` call site ~line 959)
- Modify: `tests/test_locations_unit.py` (add tests)

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_locations_unit.py`:

```python
def test_cmd_for_heightfield_inner_half():
    cmd = locations.cmd_for("heightfield", "foo", -1.0, -2.0, inner_half=750)
    assert "--inner-half" in cmd
    assert cmd[cmd.index("--inner-half") + 1] == "750"


def test_cmd_for_heightfield_no_inner_half():
    cmd = locations.cmd_for("heightfield", "foo", -1.0, -2.0)
    assert "--inner-half" not in cmd


def test_persist_location_meta_selection_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    locations._persist_location_meta("foo", "Foo", -1.0, -2.0,
                                     inner_half=750, parcel_ids=[11, 22])
    import json as _json
    data = _json.loads((tmp_path / "tiles_v2_foo" / "location.json").read_text())
    assert data["inner_half"] == 750
    assert data["subject_parcels"] == [11, 22]


def test_persist_location_meta_omits_selection_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    locations._persist_location_meta("foo", "Foo", -1.0, -2.0)
    import json as _json
    data = _json.loads((tmp_path / "tiles_v2_foo" / "location.json").read_text())
    assert "inner_half" not in data and "subject_parcels" not in data
```

- [ ] **Step 2: Run; verify they fail.**

Run: `python3 -m pytest tests/test_locations_unit.py -q -k "inner_half or selection"`
Expected: FAIL — `cmd_for` takes no `inner_half`; `_persist_location_meta` takes no `parcel_ids`.

- [ ] **Step 3: Extend `cmd_for`.** Change its signature and the `heightfield` branch (`locations.py:854`, `:871–879`):

```python
def cmd_for(step: str, slug: str, cx: float, cy: float,
            inner_half: float | None = None) -> list[str]:
```

In the `heightfield` branch, replace:

```python
        return base + [
            "gen_heightfield.py", "--slug", slug,
            f"--cx={cx}", f"--cy={cy}",
        ]
```

with:

```python
        cmd = base + [
            "gen_heightfield.py", "--slug", slug,
            f"--cx={cx}", f"--cy={cy}",
        ]
        if inner_half is not None:
            cmd += ["--inner-half", str(inner_half)]
        return cmd
```

- [ ] **Step 4: Extend `_persist_location_meta`.** Change its signature and body (`locations.py:92–103`):

```python
def _persist_location_meta(slug: str, label: str, cx: float, cy: float,
                           inner_half: float | None = None,
                           parcel_ids: list | None = None) -> None:
```

Replace the `meta = {…}` dict construction with:

```python
    meta = {"slug": slug, "label": label, "cx": cx, "cy": cy,
            "created_at": time.time()}
    if inner_half is not None:
        meta["inner_half"] = inner_half
    if parcel_ids:
        meta["subject_parcels"] = list(parcel_ids)
```

(Keep the atomic `tmp.write_text(...)` → `replace` lines below it unchanged.)

- [ ] **Step 5: Extend `enqueue_job` + worker call site.** Change the `enqueue_job` signature (`locations.py:734`):

```python
def enqueue_job(slug: str, label: str, cx: float, cy: float,
                force_recompress: bool = False,
                inner_half: float | None = None,
                parcel_ids: list | None = None) -> str | None:
```

After `job = _new_job(slug, label, cx, cy, force_recompress=force_recompress)` add:

```python
        if inner_half is not None:
            job["inner_half"] = inner_half
```

Change the persist call near the end of `enqueue_job` from
`_persist_location_meta(slug, label, cx, cy)` to:

```python
        _persist_location_meta(slug, label, cx, cy, inner_half, parcel_ids)
```

Then at the worker subprocess site (`locations.py:959`), change:

```python
    cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"])
```

to:

```python
    cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"],
                  inner_half=job.get("inner_half"))
```

- [ ] **Step 6: Run tests; verify pass.**

Run: `python3 -m pytest tests/test_locations_unit.py -q`
Expected: PASS (all, including the 4 new + the pre-existing `test_persist_location_meta_writes_label`).

- [ ] **Step 7: Commit.**

```bash
git add locations.py tests/test_locations_unit.py
git commit -m "feat(locations): thread inner_half + parcel_ids to gen + location.json

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `/api/jobs` accepts `inner_half` + `parcel_ids`

**Files:**
- Modify: `locations.py` (add pure `parse_job_extent`; ensure `import math` present)
- Modify: `server.py` (wire `parse_job_extent` + pass to `enqueue_job` in the `/api/jobs` POST handler)
- Modify: `tests/test_locations_unit.py` (add `parse_job_extent` tests)

- [ ] **Step 1: Write the failing test.** Append to `tests/test_locations_unit.py`:

```python
def test_parse_job_extent_empty():
    assert locations.parse_job_extent({}) == (None, None)


def test_parse_job_extent_values():
    assert locations.parse_job_extent({"inner_half": 750}) == (750.0, None)
    assert locations.parse_job_extent({"parcel_ids": [1, 2, 3]}) == (None, [1, 2, 3])


def test_parse_job_extent_rejects_bad():
    import pytest
    for bad in ({"inner_half": -5}, {"inner_half": "x"}, {"inner_half": True},
                {"parcel_ids": "x"}, {"parcel_ids": [1, "a"]}):
        with pytest.raises(ValueError):
            locations.parse_job_extent(bad)
```

- [ ] **Step 2: Run; verify it fails.**

Run: `python3 -m pytest tests/test_locations_unit.py -q -k parse_job_extent`
Expected: FAIL — `parse_job_extent` not defined.

- [ ] **Step 3: Implement `parse_job_extent`.** Confirm `import math` is at the top of `locations.py` (it imports `subprocess` etc.; add `import math` if missing). Add the function near `enqueue_job`:

```python
def parse_job_extent(body: dict):
    """Extract optional (inner_half, parcel_ids) from a /api/jobs JSON body.
    Returns (inner_half|None, parcel_ids|None). Raises ValueError on bad types
    so the HTTP handler can map to 400."""
    inner_half = body.get("inner_half")
    if inner_half is not None:
        if (isinstance(inner_half, bool) or not isinstance(inner_half, (int, float))
                or not math.isfinite(inner_half) or inner_half <= 0):
            raise ValueError("inner_half must be a positive finite number")
        inner_half = float(inner_half)
    parcel_ids = body.get("parcel_ids")
    if parcel_ids is not None:
        if (not isinstance(parcel_ids, list) or len(parcel_ids) > 500
                or not all(isinstance(x, int) and not isinstance(x, bool)
                           for x in parcel_ids)):
            raise ValueError("parcel_ids must be a list of ≤500 ints")
    return inner_half, parcel_ids
```

- [ ] **Step 4: Run tests; verify pass.**

Run: `python3 -m pytest tests/test_locations_unit.py -q -k parse_job_extent`
Expected: PASS (3 tests).

- [ ] **Step 5: Wire `server.py`.** Find the `/api/jobs` POST handler (grep: `grep -n "api/jobs\|enqueue_job" server.py`). It parses the JSON body and calls `enqueue_job(slug, label, cx, cy, …)`. Immediately before that call, add:

```python
                try:
                    inner_half, parcel_ids = locations.parse_job_extent(body)
                except ValueError as e:
                    self.send_error(400, str(e)); return
```

(Match the handler's actual body variable name and error-response idiom — read the surrounding handler first; it already validates `slug`/`cx`/`cy`, so mirror that style.) Then pass the two through:

```python
                job_id = locations.enqueue_job(slug, label, cx, cy,
                                               inner_half=inner_half,
                                               parcel_ids=parcel_ids)
```

- [ ] **Step 6: Gate — full unit suite.**

Run: `python3 -m pytest tests/test_gen_heightfield_unit.py tests/test_locations_unit.py -q`
Expected: PASS (all). `server.py` change is exercised by the existing server-dependent tests when `:8080` is up; the pure `parse_job_extent` path is covered here.

- [ ] **Step 7: Commit.**

```bash
git add locations.py server.py tests/test_locations_unit.py
git commit -m "feat(api): /api/jobs accepts inner_half + parcel_ids

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
