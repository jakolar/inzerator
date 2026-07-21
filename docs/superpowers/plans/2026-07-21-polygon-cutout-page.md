# Polygon Cutout Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw an arbitrary polygon in map3d and generate a baked standalone "island" page (hard-clipped terrain) served as a normal heightfield slug.

**Architecture:** Pipeline unchanged — the polygon rides along the existing job flow (`POST /api/jobs` → sm5 + heightfield steps → `tiles_v2_<slug>/`). The server derives the S-JTSK centre + `inner_half` from the polygon bbox (map3d has no projection library) and persists both the WGS ring and a viewer-ready local-metres ring into `location.json`. The heightfield viewer clips viewer-side: a canvas-rasterised mask texture + fragment `discard` (same pattern as the existing `uClipBox` hole punch at `heightfield/index.html:1200-1203`), plus a curtain-wall mesh along the boundary. A demand panel in the draw sheet estimates cost via a new `POST /api/estimate`.

**Tech Stack:** Python 3.9 stdlib http.server + pyproj + shapely (server), vanilla ES-module three.js viewers, pytest.

**Spec:** `docs/superpowers/specs/2026-07-21-polygon-cutout-page-design.md`

## Global Constraints

- Czech UI strings, English code/comments/commits (repo convention).
- Polygon: 3–200 `[lon, lat]` pairs, WGS84, inside ČR bounds (lon 12–19, lat 48.2–51.3); ring stored UNCLOSED (no repeated last vertex).
- `inner_half` clamp stays 500–2000 m (existing `derive_rings` behaviour) — never re-implement the clamp, only report `clamped: true`.
- Atomic manifest writes: `tmp = path.with_suffix('.json.tmp'); tmp.write_text(...); tmp.replace(path)` (already inside `_persist_location_meta` — don't change).
- Commit after each task; messages conventional-commit style ending with the session trailer used in this repo.
- Server tests assume `server.py` running on :8080 (repo convention — see `tests/test_locations_api.py`); pure-logic tests must not need it.

---

### Task 1: `locations.polygon_extent()` — validate polygon + derive centre/ring

**Files:**
- Modify: `locations.py` (add after `_sjtsk_to_wgs`, ~line 197)
- Test: `tests/test_locations_unit.py` (append)

**Interfaces:**
- Produces: `polygon_extent(polygon) -> dict` raising `ValueError` on invalid input. Return keys: `cx, cy` (float, S-JTSK bbox centre), `inner_half_raw` (float, metres, unclamped bbox half-extent = max(bbox_w, bbox_h)/2), `inner_half` (float, clamped 500–2000), `clamped` (bool), `bbox_w_m`, `bbox_h_m` (float), `polygon` (normalized list of `[lon, lat]` floats, unclosed), `polygon_local` (list of `[dx, dz]` metres relative to cx/cy, dz sign-flipped: `dz = -(sy - cy)` — matches the viewer/`ring_local` convention in `server.py::_api_parcel_at_point`).
- Consumed by: Task 2 (`parse_job_extent`), Task 4 (`_handle_post_jobs`), Task 5 (`/api/estimate`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_locations_unit.py`:

```python
class TestPolygonExtent:
    # Hnojice-ish triangle, ~600 m across
    POLY = [[17.2215, 49.7175], [17.2300, 49.7175], [17.2258, 49.7230]]

    def test_derives_centre_and_ring(self):
        ext = locations.polygon_extent(self.POLY)
        # S-JTSK for this area: cx ≈ -547-548k, cy ≈ -1107-1108k
        assert -549000 < ext["cx"] < -546000
        assert -1109000 < ext["cy"] < -1106000
        assert ext["inner_half"] == 500.0          # small polygon → clamped up
        assert ext["clamped"] is False             # raw < 500 is a floor, not a cut
        assert 400 < ext["bbox_w_m"] < 800
        assert len(ext["polygon_local"]) == 3
        # local ring is centred on (cx, cy): bbox of dx spans ~±bbox_w/2
        dxs = [p[0] for p in ext["polygon_local"]]
        assert abs(max(dxs) + min(dxs)) < 1.0

    def test_clamp_flag_when_too_big(self):
        big = [[17.20, 49.70], [17.26, 49.70], [17.26, 49.745], [17.20, 49.745]]
        ext = locations.polygon_extent(big)        # ~4.3 x 5 km bbox
        assert ext["inner_half"] == 2000.0
        assert ext["clamped"] is True

    def test_drops_closing_vertex(self):
        closed = self.POLY + [self.POLY[0]]
        ext = locations.polygon_extent(closed)
        assert len(ext["polygon"]) == 3

    def test_rejects_bad_input(self):
        import pytest
        for bad in (
            None, [], self.POLY[:2],                       # too few
            [[17.2, 49.7]] * 201,                          # too many
            [[17.2, 49.7], [17.3, "x"], [17.25, 49.75]],   # non-number
            [[3.0, 49.7], [3.1, 49.7], [3.05, 49.75]],     # outside CR
        ):
            with pytest.raises(ValueError):
                locations.polygon_extent(bad)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_locations_unit.py::TestPolygonExtent -q`
Expected: FAIL — `AttributeError: module 'locations' has no attribute 'polygon_extent'`

- [ ] **Step 3: Implement** — add to `locations.py` after `_sjtsk_to_wgs` (~line 197):

```python
_WGS_TO_SJTSK = None

def _wgs_to_sjtsk(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 → EPSG:5514. Lazy transformer, mirror of _sjtsk_to_wgs."""
    global _WGS_TO_SJTSK
    if _WGS_TO_SJTSK is None:
        from pyproj import Transformer
        _WGS_TO_SJTSK = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
    return _WGS_TO_SJTSK.transform(lon, lat)


def polygon_extent(polygon) -> dict:
    """Validate a drawn WGS polygon and derive the job extent from its bbox.

    Single source of truth for both POST /api/jobs (polygon mode) and
    POST /api/estimate. Returns cx/cy (S-JTSK bbox centre), inner_half
    (clamped to derive_rings' 500–2000 m), the normalized unclosed WGS ring
    and a viewer-ready local ring ([dx, -(sy-cy)] metres, the ring_local
    axis convention). Raises ValueError with a client-safe message.
    """
    if not isinstance(polygon, list) or not (3 <= len(polygon) <= 201):
        raise ValueError("polygon must be a list of 3-200 [lon, lat] pairs")
    pts = []
    for p in polygon:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or isinstance(p[0], bool) or isinstance(p[1], bool)
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in p)):
            raise ValueError("polygon vertices must be [lon, lat] numbers")
        lon, lat = float(p[0]), float(p[1])
        if not (12.0 <= lon <= 19.0 and 48.2 <= lat <= 51.3):
            raise ValueError("polygon vertex outside CR bounds")
        pts.append([lon, lat])
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]                       # store unclosed
    if len(pts) < 3 or len(pts) > 200:
        raise ValueError("polygon must have 3-200 distinct vertices")

    sj = [_wgs_to_sjtsk(lon, lat) for lon, lat in pts]
    xs = [s[0] for s in sj]; ys = [s[1] for s in sj]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    bbox_w = max(xs) - min(xs); bbox_h = max(ys) - min(ys)
    raw = max(bbox_w, bbox_h) / 2.0
    inner_half = max(500.0, min(2000.0, raw))   # mirrors derive_rings clamp
    return {
        "cx": round(cx, 2), "cy": round(cy, 2),
        "inner_half_raw": round(raw, 1), "inner_half": inner_half,
        "clamped": raw > 2000.0,
        "bbox_w_m": round(bbox_w, 1), "bbox_h_m": round(bbox_h, 1),
        "polygon": pts,
        "polygon_local": [[round(sx - cx, 2), round(-(sy - cy), 2)]
                          for sx, sy in sj],
    }
```

Check `import math` exists at the top of `locations.py` (it does — used by `parse_job_extent`).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_locations_unit.py::TestPolygonExtent -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add locations.py tests/test_locations_unit.py
git commit -m "feat(locations): polygon_extent - validate drawn polygon, derive job extent"
```

---

### Task 2: polygon through `parse_job_extent` + persistence into `location.json`

**Files:**
- Modify: `locations.py::parse_job_extent` (~line 747), `locations.py::_persist_location_meta` (~line 93), `locations.py::enqueue_job` (~line 766)
- Modify: `server.py:3963` (the only `parse_job_extent` caller — unpack 3-tuple)
- Test: `tests/test_locations_unit.py` (append)

**Interfaces:**
- `parse_job_extent(body) -> (inner_half, parcel_ids, polygon_ext)` where `polygon_ext` is `None` or the full `polygon_extent()` dict (already validated/derived).
- `_persist_location_meta(slug, label, cx, cy, inner_half=None, parcel_ids=None, polygon=None, polygon_local=None)` — writes optional `"polygon"` + `"polygon_local"` keys.
- `enqueue_job(..., polygon_ext=None)` — passes ring data through to `_persist_location_meta`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_locations_unit.py`:

```python
class TestParseJobExtentPolygon:
    POLY = [[17.2215, 49.7175], [17.2300, 49.7175], [17.2258, 49.7230]]

    def test_polygon_parsed_and_derived(self):
        ih, pids, ext = locations.parse_job_extent({"polygon": self.POLY})
        assert ext is not None and ext["inner_half"] == 500.0
        assert ih == 500.0                    # polygon fills inner_half
        assert pids is None

    def test_explicit_inner_half_wins(self):
        ih, _, ext = locations.parse_job_extent(
            {"polygon": self.POLY, "inner_half": 900})
        assert ih == 900.0 and ext is not None

    def test_no_polygon_is_backcompat(self):
        ih, pids, ext = locations.parse_job_extent({"inner_half": 700})
        assert (ih, pids, ext) == (700.0, None, None)

    def test_invalid_polygon_raises(self):
        import pytest
        with pytest.raises(ValueError):
            locations.parse_job_extent({"polygon": [[1, 2]]})


class TestPersistPolygon(TmpCwd):          # reuse the existing tmp-cwd base
    def test_location_json_gains_polygon(self):
        ext = locations.polygon_extent(TestParseJobExtentPolygon.POLY)
        locations._persist_location_meta(
            "poly-test", "Poly", ext["cx"], ext["cy"],
            inner_half=ext["inner_half"],
            polygon=ext["polygon"], polygon_local=ext["polygon_local"])
        meta = json.loads(Path("tiles_v2_poly-test/location.json").read_text())
        assert meta["polygon"] == ext["polygon"]
        assert meta["polygon_local"] == ext["polygon_local"]
```

Note: check the real name of the tmp-cwd fixture/base class in `tests/test_locations_unit.py` before writing (`grep -n "tmp" tests/test_locations_unit.py | head`) — reuse whatever pattern existing `_persist_location_meta` tests use; if none exists, use pytest's `tmp_path` + `monkeypatch.chdir(tmp_path)` in the test body instead of a base class.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_locations_unit.py -k "PolygonExtent or ParseJobExtentPolygon or PersistPolygon" -q`
Expected: new tests FAIL (`too many values to unpack` / `unexpected keyword argument 'polygon'`)

- [ ] **Step 3: Implement**

In `parse_job_extent` (keep existing inner_half/parcel_ids logic, extend return):

```python
def parse_job_extent(body: dict):
    """Extract optional (inner_half, parcel_ids, polygon_ext) from a
    /api/jobs JSON body. polygon_ext is the polygon_extent() dict (already
    validated) or None. An explicit inner_half beats the polygon-derived
    one; without it the polygon's clamped bbox half-extent is used."""
    ...existing inner_half validation...
    ...existing parcel_ids validation...
    polygon_ext = None
    if body.get("polygon") is not None:
        polygon_ext = polygon_extent(body["polygon"])   # raises ValueError
        if inner_half is None:
            inner_half = polygon_ext["inner_half"]
    return inner_half, parcel_ids, polygon_ext
```

In `_persist_location_meta`: add `polygon=None, polygon_local=None` params; after the `subject_parcels` block add:

```python
    if polygon:
        meta["polygon"] = polygon
        meta["polygon_local"] = polygon_local
```

In `enqueue_job`: add `polygon_ext: dict | None = None` param; pass to persistence:

```python
        _persist_location_meta(slug, label, cx, cy, inner_half, parcel_ids,
                               polygon=(polygon_ext or {}).get("polygon"),
                               polygon_local=(polygon_ext or {}).get("polygon_local"))
```

In `server.py:3963` update the unpack + hand-through (full wiring of the cx/cy-less path is Task 4 — here just keep the API compiling):

```python
            inner_half, parcel_ids, polygon_ext = locations.parse_job_extent(body)
            ...
        job_id = locations.enqueue_job(slug, label, cx, cy,
                                       force_recompress=force_recompress,
                                       inner_half=inner_half,
                                       parcel_ids=parcel_ids,
                                       polygon_ext=polygon_ext)
```

- [ ] **Step 4: Run full unit file** — `python3 -m pytest tests/test_locations_unit.py -q` — all pass (old tests confirm backcompat).

- [ ] **Step 5: Commit** — `git add locations.py server.py tests/test_locations_unit.py && git commit -m "feat(locations): polygon rides the job flow into location.json"`

---

### Task 3: `POST /api/jobs` without cx/cy (polygon mode) + `POST /api/estimate`

**Files:**
- Modify: `server.py::_handle_post_jobs` (~line 3927-3941) and `server.py::do_POST` dispatch (~line 3858)
- Create: `tests/test_estimate_endpoint.py` (live-server test, repo pattern)

**Interfaces:**
- `POST /api/jobs {slug, label, polygon}` — server derives cx/cy from the polygon when they're absent.
- `POST /api/estimate {polygon}` → 200 `{cx, cy, inner_half, clamped, bbox_w_m, bbox_h_m, sheets_total, sheets_cached}`; 400 `{error}` on invalid polygon; 503 when ČÚZK sheet lookup fails. NOTE: the spec's `dmr5g_cached` is unknowable before a slug exists (DMR5G is fetched per-location, not per-sheet) — the UI presents the time as a range instead (Task 5 table).

- [ ] **Step 1: Write the failing test** — `tests/test_estimate_endpoint.py`:

```python
"""POST /api/estimate — needs server.py running on :8080 (repo convention)."""
import json
import urllib.request

BASE = "http://localhost:8080"

def _post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def test_estimate_valid_polygon():
    poly = [[17.2215, 49.7175], [17.2300, 49.7175], [17.2258, 49.7230]]
    status, body = _post("/api/estimate", {"polygon": poly})
    assert status == 200
    assert body["inner_half"] == 500.0
    assert body["sheets_total"] >= 1
    assert 0 <= body["sheets_cached"] <= body["sheets_total"]

def test_estimate_rejects_garbage():
    status, body = _post("/api/estimate", {"polygon": [[1, 2]]})
    assert status == 400 and "polygon" in body["error"]
```

- [ ] **Step 2: Run to verify failure** — `python3 -m pytest tests/test_estimate_endpoint.py -q` → FAIL (404 from the running server; note: the running server needs a restart AFTER implementation for the test to pass — coordinate with the user, the port is shared).

- [ ] **Step 3: Implement** — in `server.py`:

do_POST dispatch (after the `/api/jobs` branch):

```python
            elif path == "/api/estimate":
                self._handle_post_estimate()
```

Handler (place next to `_handle_post_jobs`):

```python
    def _handle_post_estimate(self):
        """Demand estimate for a drawn polygon: derived ring + SM5 sheet
        cache state. No downloads — resolves MAPNOM codes via ČÚZK and
        checks cache/dmpok_tiff_<code>/<code>.tif presence."""
        body = self._read_json_body()
        try:
            ext = locations.polygon_extent(body.get("polygon"))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        # Same envelope the sm5 step will use (see _do_sm5_download).
        sm5_half = max(2500.0, 3.0 * ext["inner_half"])
        try:
            codes = locations._resolve_sm5_codes(ext["cx"], ext["cy"], half=sm5_half)
        except locations.RuianUnavailable as e:
            self._send_json(503, {"error": str(e)})
            return
        cached = sum(
            1 for c in codes
            if (Path("cache") / f"dmpok_tiff_{c}" / f"{c}.tif").exists())
        self._send_json(200, {
            "cx": ext["cx"], "cy": ext["cy"],
            "inner_half": ext["inner_half"], "clamped": ext["clamped"],
            "bbox_w_m": ext["bbox_w_m"], "bbox_h_m": ext["bbox_h_m"],
            "sheets_total": len(codes), "sheets_cached": cached,
        })
```

In `_handle_post_jobs`, replace the `else: cx = body.get("cx"); cy = body.get("cy")` branch (~line 3927-3929) with:

```python
        else:
            cx = body.get("cx")
            cy = body.get("cy")
            if (cx is None or cy is None) and body.get("polygon") is not None:
                # Polygon mode: the client (map3d) has no projection library;
                # derive the centre server-side. parse_job_extent re-derives
                # below — polygon_extent is cheap and idempotent.
                try:
                    _ext = locations.polygon_extent(body["polygon"])
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                cx, cy = _ext["cx"], _ext["cy"]
```

- [ ] **Step 4: Restart the shared server (ask the user or use the session's established flow), then run** — `python3 -m pytest tests/test_estimate_endpoint.py tests/test_locations_api.py -q` → all pass.

- [ ] **Step 5: Commit** — `git add server.py tests/test_estimate_endpoint.py && git commit -m "feat(server): POST /api/estimate + polygon-mode /api/jobs (no cx/cy)"`

---

### Task 4: `list_locations` on-disk size + dashboard display

**Files:**
- Modify: `locations.py::list_locations` (~line 136), `index.html` (dashboard row rendering — locate with `grep -n "has_heightfield\|status" index.html`)
- Test: `tests/test_locations_unit.py` (append)

**Interfaces:**
- Each `list_locations()` entry gains `"size_mb": float` (sum of file sizes under `tiles_v2_<slug>/`, rounded to 1 decimal; 0.0 for empty/missing).

- [ ] **Step 1: Failing test** (same tmp-cwd pattern as Task 2):

```python
    def test_list_locations_reports_size(self):
        base = Path("tiles_v2_size-test"); base.mkdir(parents=True)
        (base / "blob.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        entry = [l for l in locations.list_locations() if l["slug"] == "size-test"][0]
        assert 1.9 < entry["size_mb"] < 2.1
```

- [ ] **Step 2: Verify failure** — KeyError `size_mb`.

- [ ] **Step 3: Implement** — in the `list_locations` loop, before `out.append`:

```python
        try:
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            size = 0
```

and add `"size_mb": round(size / 1048576, 1),` to the dict. In `index.html`, append the size to the location row label (find the row template; add `· ${loc.size_mb} MB` when `size_mb > 0`).

- [ ] **Step 4: Verify** — unit test passes; dashboard row shows e.g. `hnojice · ready · 10.3 MB` (open `http://localhost:8080/` after the Task 3 server restart).

- [ ] **Step 5: Commit** — `git commit -am "feat(dashboard): per-location on-disk size"`

---

### Task 5: map3d draw mode + demand panel + generate

**Files:**
- Modify: `map3d/index.html` — toolbar (~line 112 area), styles, new module-level draw-mode block, pointer hook at lines 916-929, touch tap hook at ~line 556 (`tapTimer = setTimeout(() => showInfo(tx, ty), 320);`)

No pytest here — verify with chrome-devtools (steps below). Key implementation elements (all in `map3d/index.html`):

- [ ] **Step 1: UI skeleton.** Add `<label id="ctl5"><button id="drawBtn">✏️ výřez</button></label>` under the `#ctl4` block, and a bottom sheet:

```html
<div id="drawSheet" class="hidden">
  <div id="drawStats">klikáním umísťuj vrcholy, uzavři kliknutím na první bod</div>
  <input id="drawLabel" placeholder="název výřezu">
  <button id="drawGo" disabled>Vygenerovat</button>
  <button id="drawCancel">×</button>
</div>
```

Style like `#bsheet` (copy its CSS block, fixed bottom, z above canvas).

- [ ] **Step 2: Draw state + rendering.** Module-level:

```js
let drawMode = false, drawPts = [];          // [{lon, lat, sx, sz}]
let drawGhost = null;
function redrawDraw() {
  if (drawGhost) { scene.remove(drawGhost);
    drawGhost.children.forEach(o => { o.geometry.dispose(); o.material.dispose(); });
    drawGhost = null; }
  if (!drawPts.length) return;
  drawGhost = new THREE.Group();
  const pts = drawPts.map(p => new THREE.Vector2(p.sx, p.sz));
  const y = Math.max(...drawPts.map(p => (terrainHiRes(p.sx, p.sz) ?? 0))) * Y_SCALE + 4;
  if (pts.length >= 3) drawGhost.add(_hlFill(pts, y, 0x38e0ff, 0.15));
  if (pts.length >= 2) drawGhost.add(_hlLoop(pts, y, 0x38e0ff, 0.9));
  for (const p of drawPts) {                  // vertex markers
    const m = new THREE.Mesh(new THREE.SphereGeometry(3, 8, 8),
      new THREE.MeshBasicMaterial({ color: 0x38e0ff }));
    m.position.set(p.sx, y, p.sz); drawGhost.add(m);
  }
  drawGhost.children.forEach(o => o.renderOrder = 11);
  scene.add(drawGhost);
}
```

(`_hlFill`/`_hlLoop` signatures: `(pts: Vector2[] scene XZ, y, color, opacity)` — see lines 732/753. `terrainPoint(px, py)` at line 321 returns the picked scene point or null.)

- [ ] **Step 3: Input wiring.** In the mouse click block (lines 916-929) change the tap branch:

```js
    if (moved < 5) drawMode ? addDrawVertex(e.clientX, e.clientY)
                            : showInfo(e.clientX, e.clientY);
```

and equivalently the touch tap at ~line 556 (`tapTimer = setTimeout(() => drawMode ? addDrawVertex(tx, ty) : showInfo(tx, ty), 320);`).

```js
function addDrawVertex(px, py) {
  // close the ring when clicking near the FIRST vertex (screen space)
  if (drawPts.length >= 3) {
    const v = new THREE.Vector3(drawPts[0].sx,
      (terrainHiRes(drawPts[0].sx, drawPts[0].sz) ?? 0) * Y_SCALE, drawPts[0].sz)
      .project(camera);
    const sx0 = (v.x * 0.5 + 0.5) * innerWidth, sy0 = (-v.y * 0.5 + 0.5) * innerHeight;
    if (Math.hypot(px - sx0, py - sy0) < 14) { finishDraw(); return; }
  }
  const pt = terrainPoint(px, py);
  if (!pt) return;
  const mx = pt.x + ORIGIN.x, my = ORIGIN.y - pt.z;
  drawPts.push({ lon: mx / WMERC * 180,
                 lat: mercYToLat(my), sx: pt.x, sz: pt.z });
  redrawDraw(); updateDrawStats();
}
```

(Check the exact merc→lon formula against `showInfo`'s existing conversion around line 860 and reuse it verbatim.) Escape cancels (`exitDraw()`), Backspace pops a vertex — add to the existing keydown listener near line 674.

- [ ] **Step 4: Demand panel.** `updateDrawStats()`: area via shoelace on `[lon*cos(lat0), lat]` scaled by 111320 m/deg; bbox from the same projected coords; then debounced (600 ms) `fetch('/api/estimate', {method:'POST', body: JSON.stringify({polygon: drawPts.map(p=>[p.lon,p.lat])})})` once ≥3 vertices. Render into `#drawStats`:

```
plocha 1.2 ha · bbox 610 × 480 m · ring 500 m
⚠ polygon přesahuje max. ring 4 km — ostrov bude oříznut   (only when clamped)
generování ~2–4 min (9/9 listů v cache) | ~15–30 min (chybí 4 listy)
stránka ≈ 10 MB · GPU ≈ 30 MB (ultra) / 128 MB (super)
```

Size/GPU from static tables: `inner_half ≤ 700 → ≈8 MB`, `≤ 1400 → ≈10 MB`, else `≈12 MB`; GPU text constant. Time: `sheets_cached === sheets_total ? '~2–4 min (vše v cache)' : `~${5+missing}–30 min (stahuje ${missing} listů)``.

- [ ] **Step 5: Generate.** `finishDraw()` enables `#drawGo` + fills `#drawLabel` placeholder. `#drawGo` click:

```js
const slug = slugify(label) + '-vyrez';   // inline copy of dashboard slugifyJS (NFD fold)
const r = await fetch('/api/jobs', { method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ slug, label, polygon: drawPts.map(p => [p.lon, p.lat]) }) });
const j = await r.json();
drawStats.innerHTML = r.ok
  ? `job běží — <a href="/" target="_blank">dashboard</a>, po dokončení
     <a href="/heightfield/?slug=${slug}" target="_blank">${slug}</a>`
  : `chyba: ${j.error}`;
```

On 409 (slug taken) show the server's `suggestion`. `exitDraw()` on success keeps the sheet visible with the links.

- [ ] **Step 6: Verify in browser (chrome-devtools).** Load `/map3d/?lat=49.7189&lon=17.2248&z=16` (Hnojice — cache-warm). Script: click `#drawBtn`; dispatch 3 canvas clicks (triangle over the village); assert `__map3d`-side `drawPts.length === 3` (expose `drawPts` on the debug handle); click near first vertex → `#drawGo` enabled; `#drawStats` contains `plocha` and a time estimate (network tab shows one `/api/estimate` POST). Screenshot the sheet. Do NOT click Vygenerovat yet (that's Task 7 E2E).

- [ ] **Step 7: Commit** — `git commit -am "feat(map3d): polygon draw mode with demand panel"`

---

### Task 6: heightfield island mode (mask discard + curtain + background)

**Files:**
- Modify: `heightfield/index.html` — ring fragment shader (~line 1087-1246), location.json block (~line 2814-2831), new island module block

**Interfaces:**
- Consumes `location.json` keys `polygon_local` ([[dx, dz] m]) and manifest `rings[i].half`, `y_min` (Task 2 wrote them).
- Shader contract: new uniforms on every ring material — `uMask` (sampler2D), `uHasMask` (float 0/1), `uMaskHalf` (float m). All rings share ONE mask texture rasterised over `±maskHalf` = the CLOSEUP ring half (rings[0]).

- [ ] **Step 1: Shader patch.** In the ring fragmentShader (the block containing the `uClipBox` discard at lines 1200-1203 — mirror that pattern), add uniforms + at the very top of `main()` after `vWorldPos` is available:

```glsl
uniform sampler2D uMask;
uniform float uHasMask;
uniform float uMaskHalf;
...
  if (uHasMask > 0.5) {
    vec2 muv = vWorldPos.xz / (2.0 * uMaskHalf) + 0.5;
    if (muv.x < 0.0 || muv.x > 1.0 || muv.y < 0.0 || muv.y > 1.0) discard;
    if (texture2D(uMask, muv).a < 0.5) discard;
  }
```

Add the three uniforms to the ring material uniforms object (~line 1012): `uMask: { value: null }, uHasMask: { value: 0.0 }, uMaskHalf: { value: 1.0 }`. Apply the same trio + guard to the LOD seam-skirt / section-band materials (line ~1320 block) so cut geometry outside the island disappears too.

- [ ] **Step 2: Mask rasterisation + activation.** In the location.json block (~2814), after subject_parcels:

```js
if (Array.isArray(loc?.polygon_local) && loc.polygon_local.length >= 3) {
  const maskHalf = manifest.rings[0].half;          // closeup extent
  const c = document.createElement('canvas'); c.width = c.height = 1024;
  const g = c.getContext('2d');
  g.clearRect(0, 0, 1024, 1024);
  g.fillStyle = '#fff'; g.beginPath();
  for (let i = 0; i < loc.polygon_local.length; i++) {
    const [dx, dz] = loc.polygon_local[i];
    const u = (dx / (2 * maskHalf) + 0.5) * 1024;
    const v = (dz / (2 * maskHalf) + 0.5) * 1024;   // dz axis == world +Z
    i ? g.lineTo(u, v) : g.moveTo(u, v);
  }
  g.closePath(); g.fill();
  const maskTex = new THREE.CanvasTexture(c);
  maskTex.flipY = false;                             // verify vs cadastre drape flip
  for (const mesh of ringMeshes) {
    const u = mesh.material.uniforms;
    if (!u.uMask) continue;
    u.uMask.value = maskTex; u.uHasMask.value = 1.0; u.uMaskHalf.value = maskHalf;
  }
  buildCurtain(loc.polygon_local, maskHalf);
}
```

The `v`/`flipY` orientation MUST be verified visually (memory `feedback_ktx2_orientation`: default flip=0; check against the cadastre drape code path in this file and flip `v = 1024 - v` if the island comes out mirrored).

- [ ] **Step 3: Curtain walls.**

```js
function buildCurtain(ring, maskHalf) {
  const yBase = (manifest.y_min ?? 0) - 15;
  const pos = [];
  for (let i = 0; i < ring.length; i++) {
    const [ax, az] = ring[i], [bx, bz] = ring[(i + 1) % ring.length];
    const ay = sampleHeight(ax, az) ?? yBase, by = sampleHeight(bx, bz) ?? yBase;
    pos.push(ax, ay, az,  bx, by, bz,  ax, yBase, az,
             bx, by, bz,  bx, yBase, bz,  ax, yBase, az);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.computeVertexNormals();
  const mat = new THREE.MeshBasicMaterial({ color: 0xcfc5b4, side: THREE.DoubleSide });
  scene.add(new THREE.Mesh(geo, mat));
}
```

Check `sampleHeight`'s real name/signature first (`grep -n "function sampleHeight" heightfield/index.html`) — it may expect local metres in exactly this axis convention (used by `redrawOutlines` ~line 2408); if it can return null before heights load, rebuild the curtain after ring 0's height texture is ready (hook where `redrawOutlines` gets triggered — memory `feedback_lazy_heightdata` describes the rehydrate path).

- [ ] **Step 4: Verify in browser.** Manually write `polygon` + `polygon_local` into an existing location's `location.json` (e.g. hnojice — compute local ring with a 5-line python snippet using `locations.polygon_extent`), open `/heightfield/?slug=hnojice`, screenshot: terrain only inside the polygon, walls at the cut, fog-colour background, ortho/cadastre still working inside. Iterate the mask flip here if mirrored. Remove the manual edit after (or keep — it is the feature).

- [ ] **Step 5: Commit** — `git commit -am "feat(heightfield): island mode - polygon mask discard + curtain walls"`

---

### Task 7: E2E + docs

**Files:**
- Modify: `CLAUDE.md` (Pipeline section — one paragraph on polygon jobs + island mode)

- [ ] **Step 1: E2E.** In map3d draw a small polygon over Hnojice (cache-warm → fast job), Vygenerovat, watch `/api/jobs/<id>` until done (or dashboard), open `/heightfield/?slug=<slug>`, screenshot the island. Confirm `tiles_v2_<slug>/location.json` contains `polygon` + `polygon_local`.
- [ ] **Step 2: Restart persistence check.** Restart the server (shared :8080 — coordinate), reload the island page — everything still renders (all data baked).
- [ ] **Step 3: Docs.** CLAUDE.md: add to the Pipeline section: polygon jobs (`POST /api/jobs {slug,label,polygon}`), `location.json` keys `polygon`/`polygon_local`, `/api/estimate`, island mode in the heightfield viewer, spec+plan paths.
- [ ] **Step 4: Full test run** — `python3 -m pytest -q` → all green.
- [ ] **Step 5: Commit** — `git commit -am "feat: polygon cutout page E2E + docs"` and push.
