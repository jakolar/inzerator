# Hnojice Parcel Tiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/api/parcels` endpoint that returns RÚIAN parcels with per-vertex DSM-sampled elevations, plus a "Parcely" toggle in `hnojice_multi.html` that renders them as 15 cm extruded tiles color-coded by land-use type.

**Architecture:** Server queries RÚIAN ArcGIS layer 5 paginated, samples DSM rasters for elevation, caches the JSON to disk so subsequent requests are local. Frontend lazy-loads on first toggle, builds one `THREE.Group` per parcel (top + sides), reuses the existing `#building-popup` for click info.

**Tech Stack:** Python http.server (existing pattern), shapely, rasterio. Frontend: vanilla JS + Three.js r170, `THREE.ShapeUtils.triangulateShape` for top-face triangulation (no extra dep).

**Spec:** `docs/superpowers/specs/2026-05-06-hnojice-parcel-tiles-design.md`

**Test approach:** Server endpoint has a pytest integration test (running server, same pattern as `test_poi_endpoint.py`). Frontend is verified manually per the spec's test plan.

---

## File Structure

- **Modify:** `server.py` — new handler + helpers + dispatch line
- **Create:** `tests/test_parcels_endpoint.py` — integration test for the endpoint
- **Modify:** `hnojice_multi.html` — toolbar button + parcel layer
- **Auto-generated:** `cache/parcels_*.json` — first-fetch results (already covered by `cache/` in `.gitignore` if present; otherwise add)

---

### Task 1: Add DRUH_POZEMKU_LABELS constant

**Files:**
- Modify: `/Users/jan/projekty/gtaol/server.py`

- [ ] **Step 1: Add the lookup table near other top-level constants**

Locate (around line 470, after `_fetch_parcels_local` definition, before `PORT = ...`):

```python
PORT = int(os.environ.get("PORT", 8080))
```

Add **before** that line:

```python
DRUH_POZEMKU_LABELS = {
    2:  "orná půda",
    3:  "chmelnice",
    4:  "vinice",
    5:  "zahrada",
    6:  "ovocný sad",
    7:  "trvalý travní porost",
    10: "lesní pozemek",
    11: "vodní plocha",
    13: "zastavěná plocha a nádvoří",
    14: "ostatní plocha",
}
```

- [ ] **Step 2: Verify import works**

```bash
cd /Users/jan/projekty/gtaol && python -c "from server import DRUH_POZEMKU_LABELS; print(len(DRUH_POZEMKU_LABELS))"
```

Expected: `10`

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat(server): DRUH_POZEMKU_LABELS lookup for RÚIAN parcel use codes"
```

---

### Task 2: Add `_fetch_parcels_area` helper (paginated RÚIAN + DSM sample)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/server.py`

- [ ] **Step 1: Add the helper above `DRUH_POZEMKU_LABELS`**

```python
def _fetch_parcels_area(gcx, gcy, radius):
    """Query RÚIAN layer 5 for parcels in a square around (gcx, gcy);
    return rings in the local frame with per-vertex DSM-sampled Y.

    DSM Y is absolute m n.m. (matches GLB tile Y datum used by area
    viewers). Vertex outside any cached DSM TIFF → terrain_y = 0.
    """
    import json as _json
    import urllib.parse as _up
    out = []
    bx_min, by_min = gcx - radius, gcy - radius
    bx_max, by_max = gcx + radius, gcy + radius
    url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/5/query"

    # Page through results (RÚIAN max 1000 per response).
    offset = 0
    raw_features = []
    while True:
        params = {
            "geometry": _json.dumps({
                "xmin": bx_min, "ymin": by_min, "xmax": bx_max, "ymax": by_max,
                "spatialReference": {"wkid": 5514},
            }),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "kod,kmenovecislo,poddelenicisla,druhpozemkukod,vymera",
            "outSR": "5514", "f": "json", "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": "1000",
        }
        raw = _json.loads(_ruian_get(f"{url}?" + _up.urlencode(params), timeout=30))
        feats = raw.get("features", [])
        raw_features.extend(feats)
        if len(feats) < 1000:
            break
        offset += 1000

    # Open every cached DSM TIFF once; per-vertex sample picks the right one.
    tifs = []
    for d in sorted(Path("cache").glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            try:
                tifs.append(rasterio.open(tif))
            except Exception:
                pass

    def sample_y(sx, sy):
        for src in tifs:
            b = src.bounds
            if b.left <= sx <= b.right and b.bottom <= sy <= b.top:
                try:
                    val = next(src.sample([(sx, sy)]))[0]
                    if val and val > 0:
                        return round(float(val), 2)
                except Exception:
                    return 0.0
        return 0.0

    try:
        for f in raw_features:
            attrs = f.get("attributes", {})
            rings = f["geometry"].get("rings", []) if f.get("geometry") else []
            if not rings:
                continue
            outer = rings[0]   # outer ring only in v1
            ring_local = []
            for sx, sy in outer:
                ty = sample_y(sx, sy)
                ring_local.append([
                    round(sx - gcx, 2),
                    round(-(sy - gcy), 2),
                    ty,
                ])
            kmen = attrs.get("kmenovecislo")
            podd = attrs.get("poddelenicisla")
            label = f"{kmen}/{podd}" if podd else (str(kmen) if kmen else "—")
            use_code = attrs.get("druhpozemkukod")
            out.append({
                "id": str(attrs.get("kod", "")),
                "label": label,
                "use_code": use_code,
                "use_label": DRUH_POZEMKU_LABELS.get(use_code, "—"),
                "area_m2": int(attrs.get("vymera") or 0),
                "ring_local": ring_local,
            })
    finally:
        for src in tifs:
            try: src.close()
            except Exception: pass
    return out
```

- [ ] **Step 2: Sanity check the helper compiles**

```bash
cd /Users/jan/projekty/gtaol && python -c "from server import _fetch_parcels_area; print(_fetch_parcels_area.__doc__[:50])"
```

Expected: prints first 50 chars of the docstring.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat(server): _fetch_parcels_area helper (paginated RÚIAN + DSM sample)"
```

---

### Task 3: Add `/api/parcels` handler with disk cache

**Files:**
- Modify: `/Users/jan/projekty/gtaol/server.py`

- [ ] **Step 1: Add the handler method on the request handler class**

Find the existing `def _handle_buildings(self):` method (around line 1047) which begins with:

```python
def _handle_buildings(self):
    """Fetch RÚIAN building footprints, return in local coords relative to gcx,gcy."""
```

After that method's `return`, add a new method:

```python
def _handle_parcels(self):
    """Fetch RÚIAN parcels in a square around (gcx, gcy); cache to disk."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
    try:
        gcx = float(query["gcx"][0])
        gcy = float(query["gcy"][0])
        radius = float(query.get("radius", ["2000"])[0])
    except (KeyError, ValueError):
        self.send_error(400, "Required params: gcx, gcy (S-JTSK), optional radius")
        return

    cache_path = Path("cache") / f"parcels_{gcx:.0f}_{gcy:.0f}_{radius:.0f}.json"
    if cache_path.exists():
        body = cache_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return

    try:
        parcels = _fetch_parcels_area(gcx, gcy, radius)
    except Exception as e:
        # No fallback — disk cache miss + RÚIAN unreachable = 503.
        import sys as _sys
        print(f"[parcels] fetch failed: {e}", file=_sys.stderr)
        self.send_error(503, f"parcels: {e}")
        return

    body = json.dumps(parcels).encode("utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(body)
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
```

- [ ] **Step 2: Wire the dispatcher**

Find `do_GET` (around line 1026):

```python
def do_GET(self):
    if self.path.startswith("/proxy/ortofoto?"):
        self._proxy_ortofoto()
    elif self.path.startswith("/proxy/cadastre?"):
        ...
    elif self.path.startswith("/api/buildings?"):
        ...
```

Add a new branch after the `/api/buildings?` line:

```python
    elif self.path.startswith("/api/parcels?"):
        self._handle_parcels()
```

- [ ] **Step 3: Smoke test the endpoint manually**

Restart the server. Then:

```bash
curl -s "http://127.0.0.1:8080/api/parcels?gcx=-547700&gcy=-1107700&radius=2000" | python -c "import sys, json; d=json.load(sys.stdin); print(len(d), 'parcels'); print(d[0] if d else 'empty')"
```

Expected: prints something like `1234 parcels` and a sample dict with `id`, `label`, `use_code`, `use_label`, `area_m2`, `ring_local`.

Run twice — second call should be ~10× faster (disk cache hit). Verify
`cache/parcels_-547700_-1107700_2000.json` exists.

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(server): /api/parcels endpoint with disk cache"
```

---

### Task 4: Add pytest integration test for the endpoint

**Files:**
- Create: `/Users/jan/projekty/gtaol/tests/test_parcels_endpoint.py`

- [ ] **Step 1: Write the test (failing if endpoint missing — but it's already there from Task 3)**

```python
import json
import urllib.request


def test_parcels_returns_array_of_parcels():
    """Parcels endpoint vrací JSON pole {id, label, use_code, use_label, area_m2, ring_local}."""
    url = (
        "http://127.0.0.1:8080/api/parcels"
        "?gcx=-547700&gcy=-1107700&radius=2000"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read())
    assert isinstance(data, list)
    assert len(data) > 0, "Hnojice should have parcels"
    first = data[0]
    for key in ("id", "label", "use_code", "use_label", "area_m2", "ring_local"):
        assert key in first, f"missing key {key} in parcel response"
    assert isinstance(first["ring_local"], list)
    assert len(first["ring_local"]) >= 3, "ring should have ≥3 vertices"
    for vertex in first["ring_local"]:
        assert len(vertex) == 3, "each vertex is [lx, lz, terrain_y]"


def test_parcels_missing_params_returns_400():
    """Missing gcx/gcy → 400."""
    url = "http://127.0.0.1:8080/api/parcels?radius=2000"
    try:
        urllib.request.urlopen(url, timeout=10)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 400
```

- [ ] **Step 2: Run the test against the running server**

Make sure `server.py` is running on :8080 first. Then:

```bash
cd /Users/jan/projekty/gtaol && python -m pytest tests/test_parcels_endpoint.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_parcels_endpoint.py
git commit -m "test(server): integration tests for /api/parcels"
```

---

### Task 5: Add Parcely toolbar button + CSS

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Add the button to the existing #info panel**

Find the `<div id="info">` block (around line 46) — locate the bottom of the controls (e.g., after the `basemap-select` block). Add **before the closing `</div>`**:

```html
  <hr>
  <button id="parcelsBtn" style="width:100%;padding:6px;border-radius:4px;border:1px solid #ccc;background:#f6f6f6;cursor:pointer;font-size:12px">
    Parcely (RÚIAN) — vyp
  </button>
```

- [ ] **Step 2: Add CSS for the active state**

In the `<style>` block, add:

```css
#parcelsBtn.active { background: #1a73e8; color: white; border-color: #1456b8; }
#parcelsBtn:hover { background: #e8e8e8; }
#parcelsBtn.active:hover { background: #1456b8; }
```

- [ ] **Step 3: Verify the button appears**

Refresh `hnojice_multi.html`. Button is visible at the bottom of the left info panel, says "Parcely (RÚIAN) — vyp", clicking has no effect yet.

- [ ] **Step 4: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice): Parcely toolbar button (no behaviour yet)"
```

---

### Task 6: Implement ensureParcels + buildParcelGroup

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Add module state and helpers**

Find `const gcx = -547700;` (around line 133). After the existing module-level constants and right before the scene/camera setup, insert:

```js
// ─── Parcels layer (lazy) ────────────────────────────────────────────
const PARCEL_COLORS = {
  2:  0xb9905a,   // orná půda
  3:  0x3a5f2a,   // chmelnice
  4:  0x5a3a55,   // vinice
  5:  0x6a8a3a,   // zahrada
  6:  0x4a7a30,   // ovocný sad
  7:  0x8db04a,   // trvalý travní porost
  10: 0x1f3a1c,   // lesní pozemek
  11: 0x2a4a6e,   // vodní plocha
  13: 0x555555,   // zastavěná plocha
  14: 0xa89878,   // ostatní plocha
};
const PARCEL_FALLBACK = 0x777777;
const PARCEL_TILE_H = 0.15;
const PARCEL_LIFT   = 0.02;

let _parcels = null;          // raw array from server
let _parcelGroup = null;      // THREE.Group
const _parcelMeshes = [];     // flat list of clickable top-meshes for raycast

function buildParcelGroup(parcels) {
  const group = new THREE.Group();
  for (const p of parcels) {
    const ring = p.ring_local;
    if (!ring || ring.length < 3) continue;
    const color = PARCEL_COLORS[p.use_code] ?? PARCEL_FALLBACK;

    // Top face — triangulate the 2D outline; apply per-vertex Y from ring.
    const contour = ring.map(([x, z]) => new THREE.Vector2(x, z));
    const tris = THREE.ShapeUtils.triangulateShape(contour, []);
    const topPos = new Float32Array(ring.length * 3);
    for (let i = 0; i < ring.length; i++) {
      const [x, z, y] = ring[i];
      topPos[i*3]     = x;
      topPos[i*3 + 1] = y + PARCEL_LIFT + PARCEL_TILE_H;
      topPos[i*3 + 2] = z;
    }
    const topIdx = [];
    for (const t of tris) topIdx.push(t[0], t[1], t[2]);
    const topGeo = new THREE.BufferGeometry();
    topGeo.setAttribute('position', new THREE.BufferAttribute(topPos, 3));
    topGeo.setIndex(topIdx);
    topGeo.computeVertexNormals();
    const topMat = new THREE.MeshStandardMaterial({
      color, transparent: true, opacity: 0.55, depthWrite: false,
      roughness: 0.9, metalness: 0.0, side: THREE.DoubleSide,
    });
    const topMesh = new THREE.Mesh(topGeo, topMat);
    topMesh.renderOrder = 1;

    // Sides — quad per ring edge.
    const sidePos = [];
    const sideIdx = [];
    for (let i = 0; i < ring.length; i++) {
      const [ax, az, ay] = ring[i];
      const [bx, bz, by] = ring[(i + 1) % ring.length];
      const base = sidePos.length / 3;
      sidePos.push(
        ax, ay + PARCEL_LIFT,                  az,
        bx, by + PARCEL_LIFT,                  bz,
        bx, by + PARCEL_LIFT + PARCEL_TILE_H,  bz,
        ax, ay + PARCEL_LIFT + PARCEL_TILE_H,  az,
      );
      sideIdx.push(base, base+1, base+2,  base, base+2, base+3);
    }
    const sideGeo = new THREE.BufferGeometry();
    sideGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(sidePos), 3));
    sideGeo.setIndex(sideIdx);
    sideGeo.computeVertexNormals();
    const sideMat = new THREE.MeshStandardMaterial({
      color, transparent: true, opacity: 0.85,
      roughness: 0.9, metalness: 0.0,
    });
    const sideMesh = new THREE.Mesh(sideGeo, sideMat);
    sideMesh.renderOrder = 1;

    const parcelGroup = new THREE.Group();
    parcelGroup.add(topMesh);
    parcelGroup.add(sideMesh);
    topMesh.userData = { parcel: p };   // read by click + hover raycast
    group.add(parcelGroup);
    _parcelMeshes.push(topMesh);
  }
  return group;
}

async function ensureParcels() {
  if (_parcelGroup) return _parcelGroup;
  const r = await fetch(`/api/parcels?gcx=${gcx}&gcy=${gcy}&radius=2000`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  _parcels = await r.json();
  _parcelGroup = buildParcelGroup(_parcels);
  scene.add(_parcelGroup);
  return _parcelGroup;
}
```

- [ ] **Step 2: Wire the button onclick**

Find the existing button element handler section (or add a new one just before the click-handler region near line 748). Add:

```js
const parcelsBtn = document.getElementById('parcelsBtn');
parcelsBtn.addEventListener('click', async () => {
  parcelsBtn.disabled = true;
  parcelsBtn.textContent = 'Parcely — načítám…';
  try {
    const g = await ensureParcels();
    g.visible = !g.visible;
    parcelsBtn.classList.toggle('active', g.visible);
    parcelsBtn.textContent = g.visible
      ? 'Parcely (RÚIAN) — zap'
      : 'Parcely (RÚIAN) — vyp';
  } catch (e) {
    console.error('parcels', e);
    parcelsBtn.textContent = 'Parcely — chyba: ' + e.message;
  } finally {
    parcelsBtn.disabled = false;
  }
});
```

- [ ] **Step 3: Verify parcels appear and toggle**

Refresh `hnojice_multi.html`. Click "Parcely". After 1-3 s parcels appear as colored translucent tiles draped on terrain. Click again — disappear. Click third time — instant reappear (no fetch in DevTools Network).

- [ ] **Step 4: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice): parcel layer build + toggle"
```

---

### Task 7: Add click handling for parcels

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Extend the existing click handler to test parcels first**

Find the click handler (around line 748):

```js
renderer.domElement.addEventListener('click', (e) => {
  // ... existing code that builds raycaster, hits allMeshes, then iterates buildings ...
});
```

Locate where `popup.style.display = 'none';` clears the popup at the start of the handler. **After** that line and **before** the existing `const hits = raycaster.intersectObjects(allMeshes);` line, add:

```js
  // Parcels layer takes click priority when visible.
  if (_parcelGroup && _parcelGroup.visible && _parcelMeshes.length) {
    const phits = raycaster.intersectObjects(_parcelMeshes, false);
    if (phits.length) {
      const p = phits[0].object.userData.parcel;
      popup.style.display = 'block';
      popup.style.left = Math.min(e.clientX, innerWidth - 280) + 'px';
      popup.style.top = Math.min(e.clientY, innerHeight - 200) + 'px';
      popup.innerHTML = `
        <span class="close" id="popup-close">&times;</span>
        <h3>Parcela ${p.label}</h3>
        <div class="row"><span class="label">Druh:</span> ${p.use_label}</div>
        <div class="row"><span class="label">Výměra:</span> ${p.area_m2} m²</div>
        <div class="row"><span class="label">RÚIAN ID:</span> ${p.id}</div>
        <a href="https://nahlizenidokn.cuzk.cz/VyberParcelu/Parcela/InformaceO?id=${p.id}" target="_blank">Nahlížení do KN</a>
      `;
      // Close button works again because innerHTML wiped the listener.
      document.getElementById('popup-close').addEventListener('click', () => {
        popup.style.display = 'none';
      });
      return;   // skip the building hit-test
    }
  }
```

- [ ] **Step 2: Verify popup**

Refresh, toggle Parcely on, click on a residential parcel (one with code 13 = grey). Popup shows parcel number, "zastavěná plocha a nádvoří", area, RÚIAN ID, link. Click the link — opens correct Nahlížení page. Click X to close. Click on a tree-lined parcel — different color, "lesní pozemek" or "ostatní plocha".

Click on a building (where parcels are also visible underneath) — the building is opaque and renders in front, but the raycaster hits the parcel top first because of the order. **Expected:** clicking a building's roof falls through to the parcel below since the parcel layer takes priority. If this is undesirable in practice, swap the order (test buildings first, parcels second) — note in commit message.

- [ ] **Step 3: Decide click priority based on actual feel**

Test in the browser. If clicking buildings should still get the building popup, swap the order: keep the building hit-test before the parcel hit-test. Otherwise leave parcels-first (matches "see the cadastral grid" use case).

This plan picks **parcels-first** because the layer is opt-in via the toggle — when it's on, the user is asking for parcels.

- [ ] **Step 4: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice): click-to-inspect parcels (popup with RÚIAN link)"
```

---

### Task 8: Hover outline (optional polish)

**Files:**
- Modify: `/Users/jan/projekty/gtaol/hnojice_multi.html`

- [ ] **Step 1: Add hover state**

Add module state near the parcel state:

```js
let _parcelHover = null;      // THREE.LineLoop currently outlining hovered parcel
```

Add this listener (after the click handler block):

```js
renderer.domElement.addEventListener('mousemove', (e) => {
  if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) {
    if (_parcelHover) { scene.remove(_parcelHover); _parcelHover = null; }
    return;
  }
  const r = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
  mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const phits = raycaster.intersectObjects(_parcelMeshes, false);
  if (!phits.length) {
    if (_parcelHover) { scene.remove(_parcelHover); _parcelHover = null; }
    return;
  }
  const ring = phits[0].object.userData.parcel.ring_local;
  if (_parcelHover) {
    scene.remove(_parcelHover);
    _parcelHover.geometry.dispose();
    _parcelHover.material.dispose();
  }
  const pts = ring.map(([x, z, y]) =>
    new THREE.Vector3(x, y + PARCEL_LIFT + PARCEL_TILE_H + 0.01, z));
  pts.push(pts[0].clone());
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({ color: 0xfde047, transparent: true, opacity: 0.9 });
  _parcelHover = new THREE.Line(geo, mat);
  _parcelHover.renderOrder = 2;
  scene.add(_parcelHover);
});
```

- [ ] **Step 2: Verify hover outline**

Refresh, toggle Parcely on, move mouse over parcels. Yellow outline follows the parcel under the cursor. Outline removed when mouse leaves the parcel area or layer is toggled off.

- [ ] **Step 3: Commit**

```bash
git add hnojice_multi.html
git commit -m "feat(hnojice): hover outline on parcels"
```

---

### Task 9: Final verification per spec test plan

**Files:** none (manual verification)

- [ ] **Step 1: Run the spec's full test plan**

Follow steps 1–8 in `docs/superpowers/specs/2026-05-06-hnojice-parcel-tiles-design.md`
§ "Test plan". Verify frame rate on the iPad if possible.

- [ ] **Step 2: Run pytest**

```bash
cd /Users/jan/projekty/gtaol && python -m pytest tests/test_parcels_endpoint.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Confirm cache behavior**

```bash
ls -la cache/parcels_*.json
```

Expected: at least one file matching `parcels_-547700_-1107700_2000.json`,
non-empty.

- [ ] **Step 4: Note any deviations and fix in follow-up commits as needed.**

---

## Self-review notes

- Spec coverage:
  - Endpoint: Tasks 1-3 ✓
  - DSM sample drape: Task 2's `sample_y` ✓
  - Disk cache: Task 3 ✓
  - Color palette by use code: Task 6 ✓
  - 15 cm extrusion (top + sides, no bottom): Task 6 ✓
  - Toolbar toggle, default off, lazy fetch: Tasks 5-6 ✓
  - Click reuses building-popup: Task 7 ✓
  - Hover outline: Task 8 ✓
  - One Group per parcel: Task 6 ✓
  - YAGNI exclusions (holes, multipolygons, owners): Task 2 takes outer ring only, ignores holes ✓
- Placeholder scan: no TBDs, all code blocks complete.
- Type consistency: `_parcelMeshes` is a flat array of top-meshes used for
  raycasting; `_parcelGroup` is the parent for visibility toggle. Both
  defined in Task 6, used consistently in Tasks 7-8.
- Edge case in Task 2 (`sample_y` returning 0 for vertices outside any
  cached TIFF): documented in helper docstring; visible artifact would be
  parcel sitting at world Y=0 in that area. Hnojice has full DSM coverage,
  so this should not trigger in practice.
