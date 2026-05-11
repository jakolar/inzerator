// viewer-realtor-overlay.js
// Realtor-specific superpowers for the inzerator 3D village viewer.
// Loaded by the generated viewer (e.g. hnojice_multi.html) at the end
// of its module script via:
//   const overlay = await import('./viewer-realtor-overlay.js');
//   overlay.init({ ...args });
//
// This file owns: parcels layer, single-parcel click highlight, drone
// video panel + presets + highlights + sunset tint + MP4 export, free
// flythrough mode, building popup video link injection.
//
// State scoped to closure / module top-level — base viewer never reaches in.

// ── Parcels layer state (module-scope, lives across init/teardown) ──
const PARCEL_COLORS = {
  2:  0xb9905a,  3:  0x3a5f2a,  4:  0x5a3a55,  5:  0x6a8a3a,
  6:  0x4a7a30,  7:  0x8db04a,  10: 0x1f3a1c,  11: 0x2a4a6e,
  13: 0x555555,  14: 0xa89878,
};
const PARCEL_FALLBACK = 0x777777;
const PARCEL_TILE_H = 0.15;
const PARCEL_LIFT   = 0.02;

let _parcels = null;
let _parcelGroup = null;
const _parcelMeshes = [];
let _parcelHover = null;
let _selectedParcelMesh = null;   // THREE.Group with painted yellow outline overlay
let _selectClickSeq = 0;          // monotonic in-flight selectParcelAtClick debounce

// ── Drone video subsystem state ──
let _videoSubject = null;     // { parcel, label, ring_local, area_m2, use_label, building_idx }
let _videoState = 'idle';     // 'idle' | 'panel' | 'preview' | 'recording' | 'paused' | 'converting'
let _videoStartTs = 0;
let _videoDurationMs = 25000;
let _videoOverlay = false;
let _videoMode = 'property';
let _videoHighlights = { pulse: false, beam: false, label: false, ants: false, glow: false, pin: false };
let _videoStartAngleDeg = 0;
let _videoStartDistanceMul = 1.0;
let _hlPulseObjs = [];
let _hlBeam = null, _hlLabel = null, _hlAnts = null, _hlGlow = null, _hlPin = null;
let _hlAntsOffset = 0;
let _videoPreset = 'topdown';
let _sunsetTintActive = false;
let _sunsetTintRestore = [];
let _videoCurves = null;
let _videoPauseElapsed = null;
let _videoCancelled = false;
let _currentRecorder = null;
let _ffmpegInstance = null;
let _ffmpegLoading = null;
let _videoPrevMode = null;
const _savedSceneState = new Map();
const SUBJECT_YELLOW = 0xfde047;
const SUBJECT_CYAN   = 0x67e8f9;
let _onRecordingFrameComplete = null;
let _hudCanvas = null, _hudTexture = null, _hudMesh = null, _hudScene = null, _hudCam = null;

export function init(args) {
  const { THREE, scene, camera, renderer, controls,
          allMeshes, ruianBuildings, gcx, gcy,
          addTickHook, removeTickHook, setMainTick, resetMainTick,
          getBuildingPopup, getTerrainHeightAt } = args;

  // Sanity: log to confirm overlay activated.
  console.info('[realtor-overlay] init OK', {
    location: { gcx, gcy },
    tiles: allMeshes.length,
    buildings: Array.isArray(ruianBuildings) ? ruianBuildings.length : 0,
  });

  // ── Parcels helpers (closure over THREE, scene, gcx, gcy) ─────────

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

  // ── Single-parcel click highlight (painted-on-mesh outline) ───────

  function buildSelectedParcelMesh(parcel) {
    const ring = parcel.ring_local;
    if (!ring || ring.length < 3) return null;

    // ── 1. Compute parcel bbox in LOCAL coords ─────────────────────────
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const [x, z] of ring) {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    }
    // Pad bbox so the outline stroke isn't clipped at edges of the texture.
    const PAD = 6;   // meters
    const bxMin = minX - PAD, bxMax = maxX + PAD;
    const bzMin = minZ - PAD, bzMax = maxZ + PAD;
    const bWidth = bxMax - bxMin, bHeight = bzMax - bzMin;

    // ── 2. Generate the outline texture on a Canvas ────────────────────
    // Resolution: ~10 px per meter, capped to keep memory reasonable.
    const PX_PER_M = 10;
    const TEX_W = Math.min(2048, Math.ceil(bWidth * PX_PER_M));
    const TEX_H = Math.min(2048, Math.ceil(bHeight * PX_PER_M));
    const canvas = document.createElement('canvas');
    canvas.width = TEX_W; canvas.height = TEX_H;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, TEX_W, TEX_H);
    // Map ring vertices from world coords to canvas pixel coords.
    const toCanvas = ([x, z]) => {
      const cx = ((x - bxMin) / bWidth) * TEX_W;
      const cy = ((z - bzMin) / bHeight) * TEX_H;
      return [cx, cy];
    };
    // Draw the outline (closed polygon stroke).
    ctx.lineWidth = 6;                    // pixels of canvas → ~0.6 m at 10 px/m
    ctx.strokeStyle = '#fde047';          // yellow
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();
    const [x0, y0] = toCanvas(ring[0]);
    ctx.moveTo(x0, y0);
    for (let i = 1; i < ring.length; i++) {
      const [px, py] = toCanvas(ring[i]);
      ctx.lineTo(px, py);
    }
    ctx.closePath();
    ctx.stroke();
    // Optional dark inner stroke for readability against bright ortho.
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(60,30,0,0.85)';
    ctx.stroke();

    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;

    // ── 3. Find terrain tiles that overlap the parcel bbox ─────────────
    const overlapTiles = [];
    for (const m of allMeshes) {
      if (!m.geometry) continue;
      if (!m.geometry.boundingBox) m.geometry.computeBoundingBox();
      const bb = m.geometry.boundingBox;
      if (!bb) continue;
      if (bb.max.x < bxMin || bb.min.x > bxMax) continue;
      if (bb.max.z < bzMin || bb.min.z > bzMax) continue;
      overlapTiles.push(m);
    }
    if (overlapTiles.length === 0) return null;

    // ── 4. For each overlap tile, build a face-filtered, UV-mapped mesh
    //      that paints the outline texture onto the terrain surface. ──
    const group = new THREE.Group();
    for (const tileMesh of overlapTiles) {
      const tileGeo = tileMesh.geometry;
      const pos = tileGeo.attributes.position.array;
      const idx = tileGeo.index ? tileGeo.index.array : null;
      if (!idx) continue;   // expecting indexed geometry (GLB tiles are)

      // Filter faces: keep only those with |normal.y| > 0.5 AND that have
      // at least one vertex inside the parcel bbox (to keep mesh small).
      const keepFaces = [];
      for (let f = 0; f < idx.length; f += 3) {
        const a = idx[f], b = idx[f+1], c = idx[f+2];
        const ax = pos[a*3], ay = pos[a*3+1], az = pos[a*3+2];
        const bx = pos[b*3], by = pos[b*3+1], bz = pos[b*3+2];
        const cx = pos[c*3], cy = pos[c*3+1], cz = pos[c*3+2];
        // Bbox quick reject (any vertex outside expanded bbox by PAD)
        const bxMinTri = Math.min(ax, bx, cx), bxMaxTri = Math.max(ax, bx, cx);
        const bzMinTri = Math.min(az, bz, cz), bzMaxTri = Math.max(az, bz, cz);
        if (bxMaxTri < bxMin || bxMinTri > bxMax) continue;
        if (bzMaxTri < bzMin || bzMinTri > bzMax) continue;
        // Normal Y filter
        const e1x = bx-ax, e1y = by-ay, e1z = bz-az;
        const e2x = cx-ax, e2y = cy-ay, e2z = cz-az;
        const nx = e1y*e2z - e1z*e2y;
        const ny = e1z*e2x - e1x*e2z;
        const nz = e1x*e2y - e1y*e2x;
        const nlen = Math.sqrt(nx*nx + ny*ny + nz*nz);
        if (nlen < 1e-6) continue;
        const nyNorm = Math.abs(ny) / nlen;
        if (nyNorm <= 0.5) continue;     // skip walls
        keepFaces.push(a, b, c);
      }
      if (keepFaces.length === 0) continue;

      // Compute UVs for ALL vertices (we share the position buffer; unused
      // vertices have arbitrary UV — they're not referenced via index).
      const uvs = new Float32Array((pos.length / 3) * 2);
      for (let i = 0; i < pos.length / 3; i++) {
        const vx = pos[i*3];
        const vz = pos[i*3+2];
        uvs[i*2]     = (vx - bxMin) / bWidth;
        uvs[i*2 + 1] = 1.0 - (vz - bzMin) / bHeight;
      }

      const overlayGeo = new THREE.BufferGeometry();
      overlayGeo.setAttribute('position', tileGeo.attributes.position.clone());
      overlayGeo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
      overlayGeo.setIndex(new THREE.BufferAttribute(new Uint32Array(keepFaces), 1));

      const overlayMat = new THREE.MeshBasicMaterial({
        map: tex,
        transparent: true,
        side: THREE.DoubleSide,
        depthWrite: false,
        alphaTest: 0.05,
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -4,
      });
      const overlayMesh = new THREE.Mesh(overlayGeo, overlayMat);
      overlayMesh.renderOrder = 101;     // above cadastre overlay (100)
      overlayMesh.userData = { selectedParcel: parcel };
      group.add(overlayMesh);
    }
    if (group.children.length === 0) return null;
    return group;
  }

  function clearSelectedParcel() {
    if (_selectedParcelMesh) {
      scene.remove(_selectedParcelMesh);
      _selectedParcelMesh.traverse(node => {
        if (node.geometry) node.geometry.dispose();
        if (node.material) node.material.dispose();
      });
      _selectedParcelMesh = null;
    }
  }

  async function selectParcelAtClick(localX, localZ) {
    const seq = ++_selectClickSeq;
    const sx = localX + gcx;
    const sy = -localZ + gcy;
    try {
      const r = await fetch(`/api/parcel-at-point?gcx=${gcx}&gcy=${gcy}&sx=${sx}&sy=${sy}`);
      if (seq !== _selectClickSeq) return null;   // stale, drop
      if (r.status === 404) {
        clearSelectedParcel();
        return null;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const parcel = await r.json();
      if (seq !== _selectClickSeq) return null;   // also stale
      clearSelectedParcel();
      const mesh = buildSelectedParcelMesh(parcel);
      if (mesh) {
        _selectedParcelMesh = mesh;
        scene.add(mesh);
      }
      return parcel;
    } catch (err) {
      console.error('selectParcelAtClick', err);
      return null;
    }
  }

  // ── Inject Parcely button into base #info panel + its CSS ─────────
  const info = document.getElementById('info');
  if (info) {
    info.insertAdjacentHTML('beforeend', `
      <hr>
      <button id="parcelsBtn" style="width:100%;padding:6px;border-radius:4px;border:1px solid #ccc;background:#f6f6f6;cursor:pointer;font-size:12px">
        Parcely (RÚIAN) — vyp
      </button>
    `);
    document.head.insertAdjacentHTML('beforeend', `
      <style>
        #parcelsBtn.active { background: #1a73e8; color: white; border-color: #1456b8; }
        #parcelsBtn:hover { background: #e8e8e8; }
        #parcelsBtn.active:hover { background: #1456b8; }
      </style>
    `);
  }

  // ── Wire Parcely button click ─────────────────────────────────────
  const parcelsBtn = document.getElementById('parcelsBtn');
  if (parcelsBtn) {
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
  }

  // ── Parcel hover outline + left-click popup ───────────────────────
  const mouse = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();

  renderer.domElement.addEventListener('mousemove', (e) => {
    if (_videoState !== 'idle' && _videoState !== 'panel') {
      if (_parcelHover) { scene.remove(_parcelHover); _parcelHover.geometry.dispose(); _parcelHover.material.dispose(); _parcelHover = null; }
      return;
    }
    if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) {
      if (_parcelHover) {
        scene.remove(_parcelHover);
        _parcelHover.geometry.dispose();
        _parcelHover.material.dispose();
        _parcelHover = null;
      }
      return;
    }
    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const phits = raycaster.intersectObjects(_parcelMeshes, false);
    if (!phits.length) {
      if (_parcelHover) {
        scene.remove(_parcelHover);
        _parcelHover.geometry.dispose();
        _parcelHover.material.dispose();
        _parcelHover = null;
      }
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

  // Inject a static parcel-popup template (separate from base's static
  // #building-popup so neither handler clobbers the other's DOM).
  document.body.insertAdjacentHTML('beforeend', `
    <div id="parcel-popup" style="
      position: absolute; z-index: 20;
      background: rgba(255,255,255,0.97); padding: 14px 18px; border-radius: 8px;
      font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,0.3); min-width: 200px;
      display: none; pointer-events: auto;
    ">
      <span class="close" id="parcel-popup-close" style="position:absolute;top:6px;right:10px;cursor:pointer;color:#999;font-size:18px">&times;</span>
      <h3 id="parcel-popup-title" style="margin:0 0 8px;color:#1a73e8;font-size:15px"></h3>
      <div class="row" style="margin:4px 0"><span class="label" style="color:#888">Druh:</span> <span id="parcel-popup-druh"></span></div>
      <div class="row" style="margin:4px 0"><span class="label" style="color:#888">Výměra:</span> <span id="parcel-popup-area"></span> m²</div>
      <div class="row" style="margin:4px 0"><span class="label" style="color:#888">RÚIAN ID:</span> <span id="parcel-popup-id"></span></div>
      <a id="parcel-popup-link" href="#" target="_blank" style="color:#1a73e8;text-decoration:none;display:block;margin-top:8px">Nahlížení do KN</a>
    </div>
  `);
  const parcelPopup = document.getElementById('parcel-popup');
  document.getElementById('parcel-popup-close').addEventListener('click', () => {
    parcelPopup.style.display = 'none';
  });

  renderer.domElement.addEventListener('click', (e) => {
    // Don't react to clicks while a video is in flight — would contaminate
    // the recording with parcel highlights / popup state changes.
    if (_videoState !== 'idle' && _videoState !== 'panel') return;

    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);

    // Parcels visible? Try parcel-tile hit first — if hit, show popup and stop.
    if (_parcelGroup && _parcelGroup.visible && _parcelMeshes.length) {
      const phits = raycaster.intersectObjects(_parcelMeshes, false);
      if (phits.length) {
        const p = phits[0].object.userData.parcel;
        parcelPopup.style.display = 'block';
        parcelPopup.style.left = Math.min(e.clientX, innerWidth - 280) + 'px';
        parcelPopup.style.top = Math.min(e.clientY, innerHeight - 200) + 'px';
        document.getElementById('parcel-popup-title').textContent = `Parcela ${p.label}`;
        document.getElementById('parcel-popup-druh').textContent = p.use_label;
        document.getElementById('parcel-popup-area').textContent = p.area_m2;
        document.getElementById('parcel-popup-id').textContent = p.id;
        document.getElementById('parcel-popup-link').href = `https://nahlizenidokn.cuzk.cz/VyberParcelu/Parcela/InformaceO?id=${p.id}`;
        e.stopPropagation();   // don't trigger base's building-popup
        return;
      }
    }

    // No parcel hit — try terrain raycast for single-parcel highlight.
    // (Base's bubble-phase building-popup handler still runs after this for buildings.)
    const hits = raycaster.intersectObjects(allMeshes);
    if (hits.length > 0) {
      selectParcelAtClick(hits[0].point.x, hits[0].point.z);
    }
  }, { capture: true });

  // ── Drone video subsystem ────────────────────────────────────────────

  // Compute centroid + bbox + diagonal + top-Y from a parcel ring_local.
  function computeSubjectGeometry(ring_local) {
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity, maxY = -Infinity;
    let sumX = 0, sumZ = 0;
    for (const [x, z, y] of ring_local) {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
      if (y > maxY) maxY = y;
      sumX += x; sumZ += z;
    }
    const cx = sumX / ring_local.length;
    const cz = sumZ / ring_local.length;
    const diagonal = Math.hypot(maxX - minX, maxZ - minZ);
    const groundY = maxY;
    return { centroid: [cx, cz], bbox: { minX, maxX, minZ, maxZ }, diagonal, groundY };
  }

  // Closest road-segment point in mesh-local frame, falling back to a point
  // 200 m due west of the centroid at terrain level when /api/roads has no
  // data nearby (or hasn't been loaded for this viewer yet).
  function findNearestRoadPoint(centroid, fallbackY) {
    const FALLBACK = [centroid[0] - 200, fallbackY, centroid[1]];
    if (typeof window.roads === 'undefined' || !Array.isArray(window.roads) || window.roads.length === 0) {
      return FALLBACK;
    }
    let best = null, bestD2 = Infinity;
    for (const polyline of window.roads) {
      for (const [x, z, y] of polyline) {
        const dx = x - centroid[0], dz = z - centroid[1];
        const d2 = dx*dx + dz*dz;
        if (d2 < bestD2) { bestD2 = d2; best = [x, y || fallbackY, z]; }
      }
    }
    return best || FALLBACK;
  }

  // Build CatmullRomCurve3 paths for the presets.
  function buildCameraPath(preset, subject) {
    const [cx, cz] = subject.centroid;
    const gy = subject.groundY;
    const tgtAt = (y = gy) => new THREE.Vector3(cx, y, cz);

    if (preset === 'topdown') {
      const posPts = [
        new THREE.Vector3(cx, gy + 150, cz),
        new THREE.Vector3(cx, gy + 115, cz),
        new THREE.Vector3(cx, gy +  80, cz),
      ];
      const tgtPts = [tgtAt(), tgtAt(), tgtAt()];
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'highorbit360') {
      const R = 250, H = 200;
      const N = 16;
      const posPts = [];
      for (let i = 0; i < N; i++) {
        const theta = (2 * Math.PI * i) / N;
        posPts.push(new THREE.Vector3(cx + R * Math.cos(theta), gy + H, cz + R * Math.sin(theta)));
      }
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, true, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, true),
      };
    }

    if (preset === 'halforbit') {
      const R = 220, H = 180;
      const N = 8;
      const startAng = Math.PI;
      const endAng = startAng - Math.PI / 2;
      const posPts = [];
      for (let i = 0; i <= N; i++) {
        const t = i / N;
        const theta = startAng + (endAng - startAng) * t;
        posPts.push(new THREE.Vector3(cx + R * Math.cos(theta), gy + H, cz + R * Math.sin(theta)));
      }
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'revealpullup') {
      const posPts = [
        new THREE.Vector3(cx - 400,  gy + 30,  cz - 400),
        new THREE.Vector3(cx - 250,  gy + 90,  cz - 250),
        new THREE.Vector3(cx - 100,  gy + 160, cz - 100),
        new THREE.Vector3(cx,        gy + 220, cz),
      ];
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'divepush') {
      const posPts = [
        new THREE.Vector3(cx - 250, gy + 300, cz - 250),
        new THREE.Vector3(cx - 100, gy + 180, cz - 100),
        new THREE.Vector3(cx +  80, gy + 100, cz +  20),
        new THREE.Vector3(cx +  80, gy +  80, cz +  20),
      ];
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'locator') {
      const posPts = [
        new THREE.Vector3(cx, gy + 500, cz),
        new THREE.Vector3(cx, gy + 350, cz),
        new THREE.Vector3(cx, gy + 220, cz),
        new THREE.Vector3(cx, gy + 150, cz),
      ];
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'contextarc') {
      const R = 220, H = 180;
      const N = 8;
      const startAng = Math.PI;
      const endAng = startAng - Math.PI / 2;
      const posPts = [];
      for (let i = 0; i <= N; i++) {
        const t = i / N;
        const theta = startAng + (endAng - startAng) * t;
        const radiusFalloff = t < 0.75 ? 1.0 : (1.0 - (t - 0.75) / 0.25);
        const r = R * radiusFalloff;
        const h = H + (1 - radiusFalloff) * 40;
        posPts.push(new THREE.Vector3(cx + r * Math.cos(theta), gy + h, cz + r * Math.sin(theta)));
      }
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'lateralflyby') {
      const ZOFF = 80;
      const posPts = [
        new THREE.Vector3(cx - 350, gy + 220, cz + ZOFF),
        new THREE.Vector3(cx - 100, gy + 220, cz + ZOFF),
        new THREE.Vector3(cx + 100, gy + 220, cz + ZOFF),
        new THREE.Vector3(cx + 350, gy + 220, cz + ZOFF),
      ];
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    if (preset === 'sunsetorbit') {
      const R = 220, H = 180;
      const N = 8;
      const startAng = Math.PI;
      const endAng = startAng - Math.PI / 2;
      const posPts = [];
      for (let i = 0; i <= N; i++) {
        const t = i / N;
        const theta = startAng + (endAng - startAng) * t;
        posPts.push(new THREE.Vector3(cx + R * Math.cos(theta), gy + H, cz + R * Math.sin(theta)));
      }
      const tgtPts = posPts.map(() => tgtAt());
      return {
        posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
        targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
      };
    }

    // Fallback (unknown preset): vertical top-down zoom.
    const posPts = [
      new THREE.Vector3(cx, gy + 150, cz),
      new THREE.Vector3(cx, gy +  80, cz),
    ];
    const tgtPts = [tgtAt(), tgtAt()];
    return {
      posCurve: new THREE.CatmullRomCurve3(posPts, false, 'catmullrom', 0.5),
      targetCurve: new THREE.CatmullRomCurve3(tgtPts, false),
    };
  }

  function applyStartTransform(curves, subject) {
    const angleDeg = _videoStartAngleDeg || 0;
    const distMul = _videoStartDistanceMul || 1.0;
    if (angleDeg === 0 && distMul === 1.0) return curves;
    const [cx, cz] = subject.centroid;
    const rad = angleDeg * Math.PI / 180;
    const cosA = Math.cos(rad), sinA = Math.sin(rad);
    for (const pt of curves.posCurve.points) {
      const dx = pt.x - cx, dz = pt.z - cz;
      const rx = dx * cosA - dz * sinA;
      const rz = dx * sinA + dz * cosA;
      pt.x = cx + rx * distMul;
      pt.z = cz + rz * distMul;
    }
    for (const pt of curves.targetCurve.points) {
      const dx = pt.x - cx, dz = pt.z - cz;
      pt.x = cx + dx * cosA - dz * sinA;
      pt.z = cz + dx * sinA + dz * cosA;
    }
    curves.posCurve.updateArcLengths();
    curves.targetCurve.updateArcLengths();
    return curves;
  }

  // Standard ray-casting point-in-polygon (2D, X/Z plane). Polygon is a list
  // of [x, z] pairs (or [x, z, y] — Y is ignored).
  function pointInPolygon2D(px, pz, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const [xi, zi] = polygon[i];
      const [xj, zj] = polygon[j];
      const intersect = ((zi > pz) !== (zj > pz)) &&
        (px < (xj - xi) * (pz - zi) / (zj - zi) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  // Find the OSM/RÚIAN building whose footprint centroid lies inside the
  // parcel ring. Returns the index into ruianBuildings, or -1 if no match.
  function findSubjectBuildingIdx(ring_local) {
    if (!Array.isArray(ruianBuildings) || ruianBuildings.length === 0) return -1;
    for (let i = 0; i < ruianBuildings.length; i++) {
      const b = ruianBuildings[i];
      const ring = b.coords;
      if (!Array.isArray(ring) || ring.length < 3) continue;
      let cx = 0, cz = 0;
      for (const [x, z] of ring) { cx += x; cz += z; }
      cx /= ring.length; cz /= ring.length;
      if (pointInPolygon2D(cx, cz, ring_local)) return i;
    }
    return -1;
  }

  // ── Presentation-mode helpers ─────────────────────────────────────────

  function saveMaterialState(obj) {
    if (!obj || !obj.material || _savedSceneState.has(obj.uuid)) return;
    const m = obj.material;
    _savedSceneState.set(obj.uuid, {
      color: m.color ? m.color.getHex() : null,
      opacity: m.opacity,
      transparent: m.transparent,
      depthWrite: m.depthWrite,
      emissive: m.emissive ? m.emissive.getHex() : null,
      emissiveIntensity: m.emissiveIntensity,
    });
  }

  function restoreMaterialState(obj) {
    if (!obj || !obj.material) return;
    const saved = _savedSceneState.get(obj.uuid);
    if (!saved) return;
    const m = obj.material;
    if (saved.color !== null && m.color) m.color.setHex(saved.color);
    m.opacity = saved.opacity;
    m.transparent = saved.transparent;
    m.depthWrite = saved.depthWrite;
    if (saved.emissive !== null && m.emissive) m.emissive.setHex(saved.emissive);
    if ('emissiveIntensity' in m) m.emissiveIntensity = saved.emissiveIntensity;
    _savedSceneState.delete(obj.uuid);
  }

  function applyPresentationMode(subject, mode) {
    if (!subject || subject.free) return;
    if (!_parcelGroup || !subject) return;
    for (const pg of _parcelGroup.children) {
      const top = pg.children[0];
      const side = pg.children[1];
      if (!top || !side) continue;
      const isSubject = top.userData.parcel && top.userData.parcel.id === subject.parcel.id;
      saveMaterialState(top);
      saveMaterialState(side);
      if (isSubject) {
        top.material.color.setHex(SUBJECT_YELLOW);
        top.material.opacity = 1.0;
        side.material.color.setHex(SUBJECT_YELLOW);
        side.material.opacity = 1.0;
      } else {
        top.material.opacity = 0.30;
        side.material.opacity = 0.30;
      }
      top.material.transparent = true;
      side.material.transparent = true;
    }
    if (mode === 'property' && subject.building_idx >= 0) {
      const b = ruianBuildings[subject.building_idx];
      if (b && Array.isArray(b.coords) && b.coords.length >= 3) {
        const pts = [];
        for (const [x, z] of b.coords) {
          const y = getTerrainHeightAt(x, z) + 0.3;
          pts.push(new THREE.Vector3(x, y, z));
        }
        const geo = new THREE.BufferGeometry().setFromPoints(pts);
        const mat = new THREE.LineBasicMaterial({
          color: SUBJECT_CYAN, transparent: true, opacity: 0.95, depthTest: false,
        });
        const loop = new THREE.LineLoop(geo, mat);
        loop.renderOrder = 5;
        loop.userData.videoSubjectOutline = true;
        scene.add(loop);
      }
    }
  }

  function restorePresentationMode() {
    const toRemove = [];
    scene.traverse(obj => {
      if (obj.userData && obj.userData.videoSubjectOutline) toRemove.push(obj);
    });
    for (const obj of toRemove) {
      scene.remove(obj);
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) obj.material.dispose();
    }
    const byUuid = new Map();
    scene.traverse(o => {
      if (_savedSceneState.has(o.uuid)) byUuid.set(o.uuid, o);
    });
    const uuids = Array.from(_savedSceneState.keys());
    for (const uuid of uuids) {
      const target = byUuid.get(uuid);
      if (target) restoreMaterialState(target);
      else _savedSceneState.delete(uuid);
    }
  }

  function applyHighlights(subject) {
    if (!subject || subject.free || !subject.ring_local) return;
    const ring = subject.ring_local;
    const [cx, cz] = subject.centroid;
    const gy = subject.groundY;

    // PULSE
    if (_videoHighlights.pulse) {
      if (_parcelGroup) {
        for (const pg of _parcelGroup.children) {
          const top = pg.children[0];
          if (top && top.userData.parcel && top.userData.parcel.id === subject.parcel.id) {
            _hlPulseObjs.push({ obj: top.material, baseOpacity: top.material.opacity });
            if (pg.children[1]) _hlPulseObjs.push({ obj: pg.children[1].material, baseOpacity: pg.children[1].material.opacity });
            break;
          }
        }
      }
      scene.traverse(obj => {
        if (obj.userData && obj.userData.videoSubjectOutline) {
          _hlPulseObjs.push({ obj: obj.material, baseOpacity: obj.material.opacity });
        }
      });
    }

    // BEAM
    if (_videoHighlights.beam) {
      const beamHeight = 200;
      const beamGeo = new THREE.ConeGeometry(8, beamHeight, 16, 1, true);
      const beamMat = new THREE.MeshBasicMaterial({
        color: 0xfde047, transparent: true, opacity: 0.18,
        side: THREE.DoubleSide, depthWrite: false,
      });
      _hlBeam = new THREE.Mesh(beamGeo, beamMat);
      _hlBeam.position.set(cx, gy + beamHeight / 2, cz);
      scene.add(_hlBeam);
    }

    // LABEL
    if (_videoHighlights.label) {
      const c = document.createElement('canvas');
      c.width = 1024; c.height = 256;
      const ctx = c.getContext('2d');
      ctx.fillStyle = 'rgba(0,0,0,0.75)';
      ctx.beginPath();
      ctx.roundRect(0, 0, c.width, c.height, 24);
      ctx.fill();
      ctx.fillStyle = '#fde047';
      ctx.font = '700 80px system-ui, sans-serif';
      ctx.textBaseline = 'top';
      ctx.fillText(subject.label || '—', 32, 28);
      ctx.fillStyle = 'rgba(255,255,255,0.92)';
      ctx.font = '500 56px system-ui, sans-serif';
      const sub = `${(subject.area_m2 || 0).toLocaleString('cs-CZ')} m² · ${subject.use_label || '—'}`;
      ctx.fillText(sub, 32, 130);
      const tex = new THREE.CanvasTexture(c);
      tex.colorSpace = THREE.SRGBColorSpace;
      const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
      _hlLabel = new THREE.Sprite(mat);
      _hlLabel.scale.set(60, 15, 1);
      _hlLabel.position.set(cx, gy + 25, cz);
      _hlLabel.renderOrder = 10;
      scene.add(_hlLabel);
    }

    // ANTS
    if (_videoHighlights.ants) {
      const pts = ring.map(([x, z, y]) => new THREE.Vector3(x, y + 0.5, z));
      pts.push(pts[0].clone());
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const mat = new THREE.LineDashedMaterial({
        color: 0xfde047, dashSize: 1.5, gapSize: 1.0,
        transparent: true, opacity: 0.95, depthTest: false,
      });
      _hlAnts = new THREE.Line(geo, mat);
      _hlAnts.computeLineDistances();
      _hlAnts.renderOrder = 11;
      scene.add(_hlAnts);
    }

    // GLOW
    if (_videoHighlights.glow) {
      let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
      for (const [x, z] of ring) {
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
      }
      const w = maxX - minX, d = maxZ - minZ;
      const h = 60;
      const glowGeo = new THREE.BoxGeometry(w, h, d);
      const colors = new Float32Array(glowGeo.attributes.position.count * 3);
      const alphas = new Float32Array(glowGeo.attributes.position.count);
      for (let i = 0; i < glowGeo.attributes.position.count; i++) {
        const y = glowGeo.attributes.position.getY(i);
        const t = (y + h/2) / h;
        colors[i*3]   = 1.0;
        colors[i*3+1] = 0.88;
        colors[i*3+2] = 0.28;
        alphas[i] = 0.35 * (1.0 - t);
      }
      glowGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
      glowGeo.setAttribute('alpha', new THREE.BufferAttribute(alphas, 1));
      const glowMat = new THREE.ShaderMaterial({
        transparent: true, depthWrite: false, side: THREE.DoubleSide,
        vertexShader: `
          attribute float alpha;
          varying float vAlpha;
          varying vec3 vColor;
          void main() {
            vAlpha = alpha;
            vColor = color;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }`,
        fragmentShader: `
          varying float vAlpha;
          varying vec3 vColor;
          void main() { gl_FragColor = vec4(vColor, vAlpha); }`,
        vertexColors: true,
      });
      _hlGlow = new THREE.Mesh(glowGeo, glowMat);
      _hlGlow.position.set((minX + maxX) / 2, gy + h/2, (minZ + maxZ) / 2);
      scene.add(_hlGlow);
    }

    // PIN
    if (_videoHighlights.pin) {
      let px = cx, pz = cz;
      if (subject.building_idx >= 0 && Array.isArray(ruianBuildings) && ruianBuildings[subject.building_idx]) {
        const b = ruianBuildings[subject.building_idx];
        if (Array.isArray(b.coords) && b.coords.length >= 3) {
          let sx = 0, sz = 0;
          for (const [x, z] of b.coords) { sx += x; sz += z; }
          px = sx / b.coords.length;
          pz = sz / b.coords.length;
        }
      }
      const group = new THREE.Group();
      const stemGeo = new THREE.ConeGeometry(2.5, 7, 16);
      stemGeo.translate(0, 3.5, 0);
      stemGeo.rotateX(Math.PI);
      stemGeo.translate(0, 7, 0);
      const pinMat = new THREE.MeshStandardMaterial({
        color: 0xc0392b, roughness: 0.5, metalness: 0.1,
        depthTest: false, transparent: true, opacity: 0.95,
      });
      const stem = new THREE.Mesh(stemGeo, pinMat);
      stem.renderOrder = 12;
      group.add(stem);

      const ballGeo = new THREE.SphereGeometry(3, 24, 18);
      const ball = new THREE.Mesh(ballGeo, pinMat.clone());
      ball.position.y = 10;
      ball.renderOrder = 12;
      group.add(ball);

      const dotCanvas = document.createElement('canvas');
      dotCanvas.width = 128; dotCanvas.height = 128;
      const dctx = dotCanvas.getContext('2d');
      dctx.fillStyle = 'rgba(255,255,255,0)';
      dctx.fillRect(0, 0, 128, 128);
      dctx.fillStyle = '#ffffff';
      dctx.beginPath();
      dctx.arc(64, 64, 50, 0, Math.PI * 2);
      dctx.fill();
      dctx.strokeStyle = 'rgba(60,30,30,0.4)';
      dctx.lineWidth = 4;
      dctx.stroke();
      const dotTex = new THREE.CanvasTexture(dotCanvas);
      dotTex.colorSpace = THREE.SRGBColorSpace;
      const dotMat = new THREE.SpriteMaterial({
        map: dotTex, transparent: true, depthTest: false,
      });
      const dot = new THREE.Sprite(dotMat);
      dot.scale.set(2.6, 2.6, 1);
      dot.position.set(0, 10, 0);
      dot.renderOrder = 13;
      group.add(dot);

      const pinRay = new THREE.Raycaster();
      pinRay.set(new THREE.Vector3(px, 5000, pz), new THREE.Vector3(0, -1, 0));
      const pinHits = pinRay.intersectObjects(allMeshes, false);
      const surfaceY = pinHits.length ? pinHits[0].point.y : gy;
      const PIN_HOVER = 5;
      const finalY = surfaceY + PIN_HOVER;

      group.position.set(px, finalY + 80, pz);
      group.userData.pinAppliedAt = performance.now();
      group.userData.pinDropDurMs = 600;
      group.userData.pinFinalY = finalY;
      _hlPin = group;
      scene.add(group);
    }
  }

  function restoreHighlights() {
    for (const { obj, baseOpacity } of _hlPulseObjs) {
      obj.opacity = baseOpacity;
    }
    _hlPulseObjs = [];
    for (const ref of [_hlBeam, _hlLabel, _hlAnts, _hlGlow, _hlPin]) {
      if (!ref) continue;
      scene.remove(ref);
      ref.traverse(node => {
        if (node.geometry) node.geometry.dispose();
        if (node.material) {
          if (node.material.map) node.material.map.dispose();
          node.material.dispose();
        }
      });
    }
    _hlBeam = _hlLabel = _hlAnts = _hlGlow = _hlPin = null;
  }

  function applySunsetTint() {
    if (_sunsetTintActive) return;
    _sunsetTintActive = true;
    _sunsetTintRestore = [];
    const TINT = 0xffaa66;
    for (const m of allMeshes) {
      if (m.material && m.material.color) {
        _sunsetTintRestore.push({ material: m.material, hex: m.material.color.getHex() });
        m.material.color.setHex(TINT);
      }
    }
    if (_parcelGroup) {
      _parcelGroup.traverse(obj => {
        if (obj.isMesh && obj.material && obj.material.color) {
          _sunsetTintRestore.push({ material: obj.material, hex: obj.material.color.getHex() });
          const orig = new THREE.Color(obj.material.color.getHex());
          const tint = new THREE.Color(TINT);
          obj.material.color.setRGB(orig.r * tint.r, orig.g * tint.g, orig.b * tint.b);
        }
      });
    }
    if (_selectedParcelMesh) {
      _selectedParcelMesh.traverse(obj => {
        if (obj.isMesh && obj.material && obj.material.color) {
          _sunsetTintRestore.push({ material: obj.material, hex: obj.material.color.getHex() });
          const orig = new THREE.Color(obj.material.color.getHex());
          const tint = new THREE.Color(TINT);
          obj.material.color.setRGB(orig.r * tint.r, orig.g * tint.g, orig.b * tint.b);
        }
      });
    }
  }

  function restoreSunsetTint() {
    if (!_sunsetTintActive) return;
    for (const { material, hex } of _sunsetTintRestore) {
      if (material && material.color) material.color.setHex(hex);
    }
    _sunsetTintRestore = [];
    _sunsetTintActive = false;
  }

  // Single object that orchestrates presentation mode + highlights + sunset tint.
  const _sceneOverlay = {
    active: false,
    subject: null,
    mode: null,
    highlights: null,
    sunset: false,

    apply(subject, mode, highlights = null) {
      this.clear({ keepSunset: true });
      if (!subject) return;
      applyPresentationMode(subject, mode);
      if (highlights) {
        Object.assign(_videoHighlights, highlights);
      }
      applyHighlights(subject);
      this.subject = subject;
      this.mode = mode;
      this.highlights = highlights;
      this.active = true;
    },

    clear(opts = {}) {
      if (!this.active && !opts.force) {
        if (!opts.keepSunset && _sunsetTintActive) restoreSunsetTint();
        return;
      }
      restoreHighlights();
      restorePresentationMode();
      this.subject = null;
      this.mode = null;
      this.highlights = null;
      this.active = false;
      if (!opts.keepSunset && _sunsetTintActive) restoreSunsetTint();
    },

    setSunset(on) {
      if (on && !_sunsetTintActive) applySunsetTint();
      else if (!on && _sunsetTintActive) restoreSunsetTint();
      this.sunset = on;
    },

    tick() {
      tickHighlights();
    },
  };

  function tickHighlights() {
    if (_hlPulseObjs.length) {
      const t = (performance.now() % 1200) / 1200;
      const phase = 0.5 + 0.5 * Math.sin(t * Math.PI * 2);
      const alpha = 0.45 + 0.55 * phase;
      for (const { obj, baseOpacity } of _hlPulseObjs) {
        obj.opacity = baseOpacity * alpha + (1 - baseOpacity) * 0;
      }
    }
    if (_hlAnts) {
      _hlAntsOffset += 0.04;
      if (_hlAnts.material) _hlAnts.material.gapSize = 0.7 + 0.6 * (0.5 + 0.5 * Math.sin(_hlAntsOffset * 2));
      if (_hlAnts.material) _hlAnts.material.needsUpdate = true;
    }
    if (_hlPin) {
      const elapsed = performance.now() - _hlPin.userData.pinAppliedAt;
      const dropDur = _hlPin.userData.pinDropDurMs;
      const finalY = _hlPin.userData.pinFinalY;
      if (elapsed < dropDur) {
        const t = elapsed / dropDur;
        const ease = 1 - Math.pow(1 - t, 3);
        _hlPin.position.y = finalY + 80 * (1 - ease);
      } else {
        const hovT = (performance.now() % 2000) / 2000;
        _hlPin.position.y = finalY + 0.3 * Math.sin(hovT * Math.PI * 2);
      }
    }
  }

  function smoothstep(t) {
    const x = Math.max(0, Math.min(1, t));
    return x * x * (3 - 2 * x);
  }

  function videoTick() {
    if (!_videoCurves || !_videoStartTs) return;
    const elapsed = performance.now() - _videoStartTs;
    const tRaw = Math.min(1, elapsed / _videoDurationMs);
    const t = smoothstep(tRaw);
    const pos = _videoCurves.posCurve.getPointAt(t);
    const tgt = _videoCurves.targetCurve.getPointAt(t);
    camera.position.copy(pos);
    camera.lookAt(tgt);
    renderer.render(scene, camera);
    if (_videoOverlay) blitOverlay();
    if (tRaw >= 1) _onVideoComplete();
  }

  function startVideoTick(curves, durationMs) {
    if (typeof controls !== 'undefined' && controls) controls.enabled = false;
    _videoCurves = curves;
    _videoDurationMs = durationMs;
    _videoStartTs = performance.now();
    setMainTick(videoTick);
  }

  function stopVideoTick() {
    resetMainTick();
    if (typeof controls !== 'undefined' && controls && _videoCurves) {
      const endTgt = _videoCurves.targetCurve.getPointAt(1);
      controls.target.copy(endTgt);
      controls.enabled = true;
    }
    _videoCurves = null;
    _videoStartTs = 0;
  }

  function pauseVideoTick() {
    if (_videoState !== 'preview' && _videoState !== 'recording') return;
    _videoPrevMode = _videoState;
    _videoPauseElapsed = performance.now() - _videoStartTs;
    resetMainTick();
    if (_videoState === 'recording' && _currentRecorder && _currentRecorder.state === 'recording') {
      try { _currentRecorder.pause(); } catch (e) { console.warn('pause failed', e); }
    }
    _videoState = 'paused';
  }

  function resumeVideoTick() {
    if (_videoPauseElapsed === null || !_videoPrevMode) return;
    _videoStartTs = performance.now() - _videoPauseElapsed;
    _videoPauseElapsed = null;
    setMainTick(videoTick);
    const wasMode = _videoPrevMode;
    _videoState = wasMode;
    _videoPrevMode = null;
    if (wasMode === 'recording' && _currentRecorder && _currentRecorder.state === 'paused') {
      try { _currentRecorder.resume(); } catch (e) { console.warn('resume failed', e); }
    }
  }

  function resetRecordingUI() {
    _currentRecorder = null;
    _videoCancelled = false;
    _sceneOverlay.setSunset(false);
    document.getElementById('vp-progress').style.display = 'none';
    document.getElementById('vp-progress-bar').style.width = '0%';
    const status = document.getElementById('vp-progress-status');
    if (status && status.parentNode) status.parentNode.removeChild(status);
    const pauseBtn = document.getElementById('vp-pause');
    const cancelBtn = document.getElementById('vp-cancel');
    if (pauseBtn) {
      pauseBtn.disabled = false;
      pauseBtn.style.opacity = '';
      pauseBtn.textContent = '⏸ Pauza';
    }
    if (cancelBtn) {
      cancelBtn.disabled = false;
      cancelBtn.style.opacity = '';
    }
    document.getElementById('vp-preview').disabled = false;
    document.getElementById('vp-export').disabled = false;
    const closeBtn = document.getElementById('vp-close');
    if (closeBtn) {
      closeBtn.style.opacity = '';
      closeBtn.style.pointerEvents = '';
    }
    const bmsel = document.getElementById('basemap-select');
    if (bmsel) bmsel.disabled = false;
    const oqsel = document.getElementById('ortho-quality');
    if (oqsel) oqsel.disabled = false;
    _videoState = 'panel';
  }

  function cancelVideoTick() {
    if (_videoState !== 'preview' && _videoState !== 'recording' && _videoState !== 'paused') return;
    _videoCancelled = true;
    if (_currentRecorder) {
      try {
        if (_currentRecorder.state !== 'inactive') _currentRecorder.stop();
      } catch (e) { console.warn('cancel stop failed', e); }
    }
    stopVideoTick();
    _videoPauseElapsed = null;
    _videoPrevMode = null;
    resetRecordingUI();
  }

  // HUD overlay rendered via a separate Orthographic scene drawn after the
  // main scene. This gives MediaRecorder a baked-in info pill in the canvas.
  function initHud() {
    if (_hudCanvas) return;
    _hudCanvas = document.createElement('canvas');
    _hudCanvas.width = 512;
    _hudCanvas.height = 96;
    _hudTexture = new THREE.CanvasTexture(_hudCanvas);
    _hudTexture.minFilter = THREE.LinearFilter;
    _hudTexture.colorSpace = THREE.SRGBColorSpace;
    const mat = new THREE.MeshBasicMaterial({ map: _hudTexture, transparent: true, depthTest: false });
    const geo = new THREE.PlaneGeometry(1, 1);
    _hudMesh = new THREE.Mesh(geo, mat);
    _hudScene = new THREE.Scene();
    _hudScene.add(_hudMesh);
    _hudCam = new THREE.OrthographicCamera(0, 1, 1, 0, 0, 1);
  }

  function drawHudText(label, area, useLabel) {
    initHud();
    const c = _hudCanvas;
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.fillStyle = 'rgba(0,0,0,0.55)';
    const r = 12;
    ctx.beginPath();
    ctx.roundRect(0, 0, c.width, c.height, r);
    ctx.fill();
    ctx.fillStyle = 'white';
    ctx.font = '600 24px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    ctx.fillText(label, 16, 14);
    ctx.font = '400 18px system-ui, sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    const fmtArea = `${area.toLocaleString('cs-CZ')} m²`;
    ctx.fillText(`${fmtArea} · ${useLabel || '—'}`, 16, 50);
    _hudTexture.needsUpdate = true;
  }

  function blitOverlay() {
    if (!_hudMesh) return;
    const W = renderer.domElement.width;
    const H = renderer.domElement.height;
    const pillW = 320, pillH = 60;
    const x = 16 / W;
    const y = 16 / H;
    const w = pillW / W;
    const h = pillH / H;
    _hudMesh.scale.set(w, h, 1);
    _hudMesh.position.set(x + w / 2, y + h / 2, 0);
    const wasAutoClear = renderer.autoClear;
    renderer.autoClear = false;
    renderer.render(_hudScene, _hudCam);
    renderer.autoClear = wasAutoClear;
  }

  // Stub; overridden below after Preview button wiring.
  let _onVideoComplete = function () {
    stopVideoTick();
  };

  function openVideoPanel(parcel) {
    const ring = parcel.ring_local;
    if (!ring || ring.length < 3) return;
    const sg = computeSubjectGeometry(ring);
    const buildingIdx = findSubjectBuildingIdx(ring);
    _videoSubject = {
      parcel,
      label: parcel.label,
      area_m2: parcel.area_m2,
      use_label: parcel.use_label,
      ring_local: ring,
      centroid: sg.centroid,
      diagonal: sg.diagonal,
      groundY: sg.groundY,
      building_idx: buildingIdx,
    };
    document.getElementById('vp-label').textContent = `parcela ${parcel.label}`;
    document.getElementById('vp-area').textContent  = parcel.area_m2.toLocaleString('cs-CZ');
    document.getElementById('vp-use').textContent   = parcel.use_label || '—';
    document.getElementById('video-panel').style.display = 'block';
    _videoStartAngleDeg = 0;
    _videoStartDistanceMul = 1.0;
    document.getElementById('vp-start-angle').value = 0;
    document.getElementById('vp-start-angle-val').textContent = '0';
    document.getElementById('vp-start-dist').value = '1.0';
    document.getElementById('vp-start-dist-val').textContent = '1.00';
    let hudTitle = `parcela ${parcel.label}`;
    if (buildingIdx >= 0) {
      const b = ruianBuildings[buildingIdx];
      if (b && b.cislo) {
        hudTitle = `č.p. ${b.cislo}`;
      }
    }
    drawHudText(hudTitle, parcel.area_m2, parcel.use_label);
    _sceneOverlay.apply(_videoSubject, _videoMode);
    _videoState = 'panel';
  }

  function openVideoPanelFreeMode() {
    const tgt = controls.target;
    const dx = camera.position.x - tgt.x;
    const dz = camera.position.z - tgt.z;
    const dist = Math.hypot(dx, dz);
    _videoSubject = {
      parcel: { id: null, label: 'aktuální pohled', area_m2: 0, use_label: '—' },
      label: 'aktuální pohled',
      area_m2: 0,
      use_label: '—',
      ring_local: null,
      centroid: [tgt.x, tgt.z],
      diagonal: Math.max(80, dist * 0.8),
      groundY: tgt.y,
      building_idx: -1,
      free: true,
    };
    document.getElementById('vp-label').textContent = 'aktuální pohled';
    document.getElementById('vp-area').textContent  = '—';
    document.getElementById('vp-use').textContent   = 'volný přílet';
    document.getElementById('video-panel').style.display = 'block';
    _videoStartAngleDeg = 0;
    _videoStartDistanceMul = 1.0;
    document.getElementById('vp-start-angle').value = 0;
    document.getElementById('vp-start-angle-val').textContent = '0';
    document.getElementById('vp-start-dist').value = '1.0';
    document.getElementById('vp-start-dist-val').textContent = '1.00';
    drawHudText('Hnojice — volný přílet', 0, '');
    _sceneOverlay.apply(_videoSubject, _videoMode);
    _videoState = 'panel';
  }

  function closeVideoPanel() {
    if (_videoState === 'preview' || _videoState === 'recording' ||
        _videoState === 'paused' || _videoState === 'converting') {
      return;
    }
    document.getElementById('video-panel').style.display = 'none';
    _sceneOverlay.clear();
    _videoSubject = null;
    _videoState = 'idle';
  }

  function pickWebmMime() {
    const candidates = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
    for (const m of candidates) {
      if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) return m;
    }
    return 'video/webm';
  }

  async function ensureFfmpeg() {
    if (_ffmpegInstance) return _ffmpegInstance;
    if (_ffmpegLoading) return _ffmpegLoading;
    _ffmpegLoading = (async () => {
      const FFmpegMod = await import('https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/dist/esm/index.js');
      const ffmpeg = new FFmpegMod.FFmpeg();
      const fetchAsBlobURL = async (url, mimeType) => {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`fetch ${url}: HTTP ${res.status}`);
        const buf = await res.arrayBuffer();
        return URL.createObjectURL(new Blob([buf], { type: mimeType }));
      };
      const [classWorkerURL, coreURL, wasmURL] = await Promise.all([
        fetchAsBlobURL('https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/dist/esm/worker.js', 'text/javascript'),
        fetchAsBlobURL('https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/esm/ffmpeg-core.js', 'text/javascript'),
        fetchAsBlobURL('https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/esm/ffmpeg-core.wasm', 'application/wasm'),
      ]);
      await ffmpeg.load({ classWorkerURL, coreURL, wasmURL });
      _ffmpegInstance = ffmpeg;
      return ffmpeg;
    })();
    try {
      return await _ffmpegLoading;
    } catch (err) {
      _ffmpegLoading = null;
      throw err;
    }
  }

  async function transcodeWebmToMp4(webmBlob, onProgress) {
    const ffmpeg = await ensureFfmpeg();
    let progressHandler = null;
    if (onProgress) {
      progressHandler = ({ progress }) => onProgress(Math.max(0, Math.min(1, progress)));
      ffmpeg.on('progress', progressHandler);
    }
    try {
      const inputBytes = new Uint8Array(await webmBlob.arrayBuffer());
      await ffmpeg.writeFile('input.webm', inputBytes);
      await ffmpeg.exec([
        '-i', 'input.webm',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        'output.mp4',
      ]);
      const outBytes = await ffmpeg.readFile('output.mp4');
      try { await ffmpeg.deleteFile('input.webm'); } catch (e) {}
      try { await ffmpeg.deleteFile('output.mp4'); } catch (e) {}
      const buffer = outBytes instanceof Uint8Array ? outBytes.buffer : outBytes;
      return new Blob([buffer], { type: 'video/mp4' });
    } finally {
      if (progressHandler) ffmpeg.off('progress', progressHandler);
    }
  }

  // ── Inject CSS + HTML ─────────────────────────────────────────────────

  document.head.insertAdjacentHTML('beforeend', `
    <style>
      #freeRecordBtn:hover { background: #e8e8e8; }
      #freeRecordBtn.active { background: #1a73e8; color: white; border-color: #1456b8; }
      #freeRecordBtn.active:hover { background: #1456b8; }
      #video-panel {
        position: absolute; top: 70px; right: 10px; z-index: 30;
        background: rgba(255,255,255,0.97); padding: 14px 16px; border-radius: 8px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.3); width: 280px; font-size: 13px;
      }
      #video-panel .vp-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
      #video-panel .vp-title { font-weight: 600; color: #1a73e8; font-size: 14px; }
      #video-panel .vp-close { cursor: pointer; color: #999; font-size: 18px; }
      #video-panel .vp-meta { color: #555; font-size: 12px; margin-bottom: 10px; }
      #video-panel fieldset.vp-fieldset { border: 1px solid #ddd; border-radius: 4px; padding: 6px 8px; margin: 0 0 8px 0; }
      #video-panel fieldset.vp-fieldset legend { color: #888; font-size: 11px; padding: 0 4px; }
      #video-panel fieldset.vp-fieldset label { display: block; margin: 3px 0; cursor: pointer; }
      #video-panel .vp-row { display: block; margin: 6px 0; }
      #video-panel input[type=range] { width: 60%; vertical-align: middle; }
      #video-panel .vp-actions { display: flex; gap: 8px; margin-top: 10px; }
      #video-panel .vp-actions button {
        flex: 1; padding: 7px 0; border-radius: 4px; border: 1px solid #ccc;
        background: #f6f6f6; cursor: pointer; font-size: 13px;
      }
      #video-panel .vp-actions button:hover { background: #e8e8e8; }
      #video-panel .vp-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
      #video-panel .vp-progress-row {
        display: flex; gap: 6px; align-items: center; margin-top: 10px;
      }
      #video-panel .vp-progress-bar-wrap {
        flex: 1; height: 6px; background: #eee; border-radius: 3px; overflow: hidden;
      }
      #video-panel #vp-progress-bar { height: 100%; background: #1a73e8; width: 0%; transition: width 0.1s linear; }
      #video-panel .vp-ctrl-btn {
        font-size: 11px; padding: 3px 6px; border-radius: 3px; border: 1px solid #ccc;
        background: #f6f6f6; cursor: pointer;
      }
      #video-panel .vp-ctrl-btn:hover { background: #e8e8e8; }
      #video-panel .vp-ctrl-btn:disabled { opacity: 0.4; cursor: not-allowed; }
      #video-panel .vp-ctrl-cancel { color: #c0392b; border-color: #e8b4ad; }
      #video-panel .vp-ctrl-cancel:hover { background: #fadbd8; }
    </style>
  `);

  // Inject freeRecordBtn + hint into #info (before the "← Zpět" link)
  const infoEl = document.getElementById('info');
  if (infoEl) {
    // Find the <p><a href="index.html"> node and insert before it
    const backLink = infoEl.querySelector('a[href="index.html"]');
    if (backLink && backLink.parentElement) {
      backLink.parentElement.insertAdjacentHTML('beforebegin', `
        <hr>
        <p style="font-size:11px;color:#888;margin:6px 0 4px;line-height:1.3">
          🎬 <b>Video režim:</b> pravým klikem na parcelu otevři panel pro export. Nebo:
        </p>
        <button id="freeRecordBtn" style="width:100%;padding:6px;border-radius:4px;border:1px solid #ccc;background:#f6f6f6;cursor:pointer;font-size:12px">
          📹 Nahrát aktuální pohled
        </button>
      `);
    }
  }

  // Inject video panel into <body>
  document.body.insertAdjacentHTML('beforeend', `
    <div id="video-panel" style="display:none">
      <div class="vp-header">
        <span class="vp-title">🎬 Video pro parcelu</span>
        <span id="vp-close" class="vp-close">&times;</span>
      </div>
      <div class="vp-meta">
        <span id="vp-label"></span> · <span id="vp-area"></span> m² · <span id="vp-use"></span>
      </div>
      <fieldset class="vp-fieldset">
        <legend>Preset</legend>
        <label><input type="radio" name="vp-preset" value="topdown" checked> 📐 Top-down zoom (15s)</label>
        <label><input type="radio" name="vp-preset" value="highorbit360"> 🛰️ High orbit 360° (30s)</label>
        <label><input type="radio" name="vp-preset" value="halforbit"> 🔄 Half-orbit (15s)</label>
        <label><input type="radio" name="vp-preset" value="revealpullup"> 📡 Reveal pull-up (25s)</label>
        <label><input type="radio" name="vp-preset" value="divepush"> 🎯 Diagonal push-in (20s)</label>
        <label><input type="radio" name="vp-preset" value="locator"> 🌍 Locator zoom (20s)</label>
        <label><input type="radio" name="vp-preset" value="contextarc"> 🌀 Context arc (25s)</label>
        <label><input type="radio" name="vp-preset" value="lateralflyby"> ✈️ Lateral fly-by (15s)</label>
        <label><input type="radio" name="vp-preset" value="sunsetorbit"> 🌅 Sunset orbit (15s)</label>
      </fieldset>
      <fieldset class="vp-fieldset">
        <legend>Mód</legend>
        <label><input type="radio" name="vp-mode" value="property" checked> Property (parcel + budova)</label>
        <label><input type="radio" name="vp-mode" value="land"> Land only</label>
      </fieldset>
      <fieldset class="vp-fieldset">
        <legend>Zvýraznění</legend>
        <label><input type="checkbox" name="vp-hl" value="pulse"> ✨ Pulsing outline</label>
        <label><input type="checkbox" name="vp-hl" value="beam"> 🔦 Beam z parcely</label>
        <label><input type="checkbox" name="vp-hl" value="label"> 🏷️ Floating label</label>
        <label><input type="checkbox" name="vp-hl" value="ants"> 🐜 Marching ants</label>
        <label><input type="checkbox" name="vp-hl" value="glow"> 💡 Volumetric glow</label>
        <label><input type="checkbox" name="vp-hl" value="pin"> 📍 Pin</label>
      </fieldset>
      <label class="vp-row"><input type="checkbox" id="vp-overlay"> Info overlay</label>
      <label class="vp-row">Délka <input type="range" id="vp-duration" min="15" max="45" step="1" value="25"> <span id="vp-duration-val">25</span>s</label>
      <fieldset class="vp-fieldset">
        <legend>Začátek dráhy</legend>
        <label class="vp-row">Úhel <input type="range" id="vp-start-angle" min="-180" max="180" step="5" value="0"> <span id="vp-start-angle-val">0</span>°</label>
        <label class="vp-row">Vzdálenost <input type="range" id="vp-start-dist" min="0.5" max="2.5" step="0.05" value="1.0"> <span id="vp-start-dist-val">1.0</span>×</label>
      </fieldset>
      <div class="vp-actions">
        <button id="vp-preview">Preview</button>
        <button id="vp-export">Export</button>
      </div>
      <div id="vp-progress" class="vp-progress-row" style="display:none">
        <div class="vp-progress-bar-wrap"><div id="vp-progress-bar"></div></div>
        <button id="vp-pause" class="vp-ctrl-btn">⏸ Pauza</button>
        <button id="vp-cancel" class="vp-ctrl-btn vp-ctrl-cancel">✕ Zrušit</button>
      </div>
    </div>
  `);

  // ── Wire all panel handlers ───────────────────────────────────────────

  document.getElementById('vp-close').addEventListener('click', closeVideoPanel);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _videoState === 'panel') closeVideoPanel();
  });

  document.querySelectorAll('input[name=vp-preset]').forEach(r => {
    r.addEventListener('change', () => {
      if (_videoState !== 'panel') return;
      _videoPreset = r.value;
      const defaults = {
        topdown: 15, highorbit360: 30, halforbit: 15,
        revealpullup: 25, divepush: 20, locator: 20,
        contextarc: 25, lateralflyby: 15, sunsetorbit: 15,
      };
      const slider = document.getElementById('vp-duration');
      slider.value = defaults[_videoPreset] || 25;
      document.getElementById('vp-duration-val').textContent = slider.value;
      _videoDurationMs = parseInt(slider.value) * 1000;
    });
  });
  document.querySelectorAll('input[name=vp-mode]').forEach(r => {
    r.addEventListener('change', () => {
      if (_videoState !== 'panel') return;
      _videoMode = r.value;
      if (_videoSubject) {
        _sceneOverlay.apply(_videoSubject, _videoMode);
      }
    });
  });
  document.getElementById('vp-overlay').addEventListener('change', e => {
    if (_videoState !== 'panel') return;
    _videoOverlay = e.target.checked;
  });
  document.querySelectorAll('input[name=vp-hl]').forEach(cb => {
    cb.addEventListener('change', () => {
      if (_videoState !== 'panel') return;
      _videoHighlights[cb.value] = cb.checked;
      if (_videoSubject) {
        _sceneOverlay.apply(_videoSubject, _videoMode);
      }
    });
  });
  document.getElementById('vp-duration').addEventListener('input', e => {
    if (_videoState !== 'panel') return;
    document.getElementById('vp-duration-val').textContent = e.target.value;
    _videoDurationMs = parseInt(e.target.value) * 1000;
  });
  document.getElementById('vp-start-angle').addEventListener('input', e => {
    if (_videoState !== 'panel') return;
    _videoStartAngleDeg = parseInt(e.target.value);
    document.getElementById('vp-start-angle-val').textContent = _videoStartAngleDeg;
  });
  document.getElementById('vp-start-dist').addEventListener('input', e => {
    if (_videoState !== 'panel') return;
    _videoStartDistanceMul = parseFloat(e.target.value);
    document.getElementById('vp-start-dist-val').textContent = _videoStartDistanceMul.toFixed(2);
  });

  document.getElementById('vp-preview').addEventListener('click', () => {
    if (!_videoSubject || _videoState === 'preview' || _videoState === 'recording' || _videoState === 'paused' || _videoState === 'converting') return;
    const subjectForPath = {
      centroid: _videoSubject.centroid,
      groundY: _videoSubject.groundY,
      diagonal: _videoSubject.diagonal,
    };
    const curves = applyStartTransform(buildCameraPath(_videoPreset, subjectForPath), subjectForPath);
    _videoState = 'preview';
    document.getElementById('vp-preview').disabled = true;
    document.getElementById('vp-export').disabled = true;
    const prog = document.getElementById('vp-progress');
    const bar = document.getElementById('vp-progress-bar');
    prog.style.display = 'block';
    bar.style.width = '0%';
    const updateBar = () => {
      if (_videoState !== 'preview' && _videoState !== 'paused') return;
      const elapsed = _videoState === 'paused' ? _videoPauseElapsed : (performance.now() - _videoStartTs);
      const t = Math.min(1, elapsed / _videoDurationMs);
      bar.style.width = `${(t * 100).toFixed(1)}%`;
      if (t < 1) requestAnimationFrame(updateBar);
    };
    requestAnimationFrame(updateBar);
    _sceneOverlay.setSunset(_videoPreset === 'sunsetorbit');
    startVideoTick(curves, _videoDurationMs);
  });

  // Override the stub _onVideoComplete with the full Preview + Recording callback.
  _onVideoComplete = function () {
    stopVideoTick();
    _sceneOverlay.setSunset(false);
    if (_videoState === 'preview') {
      document.getElementById('vp-progress').style.display = 'none';
      document.getElementById('vp-progress-bar').style.width = '0%';
      document.getElementById('vp-preview').disabled = false;
      document.getElementById('vp-export').disabled = false;
      _videoState = 'panel';
    } else if (_videoState === 'recording') {
      if (typeof _onRecordingFrameComplete === 'function') _onRecordingFrameComplete();
    }
  };

  document.getElementById('vp-export').addEventListener('click', () => {
    if (!_videoSubject || _videoState === 'preview' || _videoState === 'recording' || _videoState === 'paused' || _videoState === 'converting') return;
    const subjectForPath = {
      centroid: _videoSubject.centroid,
      groundY: _videoSubject.groundY,
      diagonal: _videoSubject.diagonal,
    };
    const curves = applyStartTransform(buildCameraPath(_videoPreset, subjectForPath), subjectForPath);

    const stream = renderer.domElement.captureStream(30);
    const mime = pickWebmMime();
    let recorder;
    try {
      recorder = new MediaRecorder(stream, {
        mimeType: mime,
        videoBitsPerSecond: 8_000_000,
      });
    } catch (err) {
      alert(`MediaRecorder unsupported: ${err.message}`);
      return;
    }
    _currentRecorder = recorder;
    _videoCancelled = false;
    const chunks = [];
    recorder.ondataavailable = e => e.data && e.data.size && chunks.push(e.data);

    _videoState = 'recording';
    document.getElementById('vp-preview').disabled = true;
    document.getElementById('vp-export').disabled = true;
    document.getElementById('vp-close').style.opacity = '0.4';
    document.getElementById('vp-close').style.pointerEvents = 'none';
    const bmsel = document.getElementById('basemap-select');
    if (bmsel) bmsel.disabled = true;
    const oqsel = document.getElementById('ortho-quality');
    if (oqsel) oqsel.disabled = true;
    const prog = document.getElementById('vp-progress');
    const bar  = document.getElementById('vp-progress-bar');
    prog.style.display = 'block';
    bar.style.width = '0%';
    const updateBarRec = () => {
      if (_videoState !== 'recording' && _videoState !== 'paused') return;
      const elapsed = _videoState === 'paused' ? _videoPauseElapsed : (performance.now() - _videoStartTs);
      const t = Math.min(1, elapsed / _videoDurationMs);
      bar.style.width = `${(t * 100).toFixed(1)}%`;
      if (t < 1) requestAnimationFrame(updateBarRec);
    };
    requestAnimationFrame(updateBarRec);

    recorder.onerror = (ev) => {
      console.error('MediaRecorder error', ev);
      alert('Chyba nahrávání: ' + (ev?.error?.message || 'unknown'));
      _onRecordingFrameComplete = null;
      stopVideoTick();
      resetRecordingUI();
    };

    _onRecordingFrameComplete = () => {
      _onRecordingFrameComplete = null;
      requestAnimationFrame(() => requestAnimationFrame(() => recorder.stop()));
      recorder.onstop = async () => {
        _currentRecorder = null;
        if (_videoCancelled) {
          _videoCancelled = false;
          return;
        }
        const webmBlob = new Blob(chunks, { type: mime });
        _videoState = 'converting';
        document.getElementById('vp-progress').style.display = 'block';
        const bar = document.getElementById('vp-progress-bar');
        const pauseBtn = document.getElementById('vp-pause');
        const cancelBtn = document.getElementById('vp-cancel');
        pauseBtn.disabled = true;
        cancelBtn.disabled = true;
        pauseBtn.style.opacity = '0.4';
        cancelBtn.style.opacity = '0.4';
        let statusEl = document.getElementById('vp-progress-status');
        if (!statusEl) {
          statusEl = document.createElement('div');
          statusEl.id = 'vp-progress-status';
          statusEl.style.cssText = 'font-size:11px;color:#666;margin-top:4px';
          document.getElementById('vp-progress').appendChild(statusEl);
        }
        statusEl.textContent = 'Převádím na MP4…';
        bar.style.width = '0%';
        let outBlob, ext;
        try {
          outBlob = await transcodeWebmToMp4(webmBlob, (p) => {
            bar.style.width = `${(p * 100).toFixed(1)}%`;
          });
          ext = 'mp4';
        } catch (err) {
          console.error('mp4 conversion failed', err);
          statusEl.textContent = 'MP4 konverze selhala — stahuji webm';
          outBlob = webmBlob;
          ext = 'webm';
        }
        const url = URL.createObjectURL(outBlob);
        const a = document.createElement('a');
        const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
        a.href = url;
        a.download = `parcela-${_videoSubject.label.replace(/[\\/]/g, '-')}-${_videoPreset}-${ts}.${ext}`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        resetRecordingUI();
      };
    };

    try {
      recorder.start();
    } catch (err) {
      alert(`MediaRecorder.start() failed: ${err.message}`);
      _onRecordingFrameComplete = null;
      resetRecordingUI();
      return;
    }
    _sceneOverlay.setSunset(_videoPreset === 'sunsetorbit');
    startVideoTick(curves, _videoDurationMs);
  });

  document.getElementById('vp-pause').addEventListener('click', () => {
    const pb = document.getElementById('vp-pause');
    if (_videoState === 'preview' || _videoState === 'recording') {
      pauseVideoTick();
      pb.textContent = '▶ Pokračovat';
    } else if (_videoState === 'paused') {
      resumeVideoTick();
      pb.textContent = '⏸ Pauza';
    }
  });
  document.getElementById('vp-cancel').addEventListener('click', () => {
    cancelVideoTick();
  });

  // ── contextmenu on canvas → openVideoPanel ────────────────────────────

  renderer.domElement.addEventListener('contextmenu', (e) => {
    if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) return;
    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const phits = raycaster.intersectObjects(_parcelMeshes, false);
    if (!phits.length) return;
    e.preventDefault();
    const parcel = phits[0].object.userData.parcel;
    if (_videoState !== 'idle' && _videoState !== 'panel') return;
    if (_videoState === 'panel') closeVideoPanel();
    openVideoPanel(parcel);
  });

  // ── freeRecordBtn click ───────────────────────────────────────────────

  const freeRecordBtnEl = document.getElementById('freeRecordBtn');
  if (freeRecordBtnEl) {
    freeRecordBtnEl.addEventListener('click', () => {
      if (_videoState !== 'idle' && _videoState !== 'panel') return;
      if (_videoState === 'panel') closeVideoPanel();
      openVideoPanelFreeMode();
    });
  }

  // ── Register _sceneOverlay.tick with the base tick API ────────────────

  addTickHook(() => _sceneOverlay.tick());

  // ── MutationObserver: inject 🎬 video link into building popup ────────

  const popupEl = getBuildingPopup();
  if (popupEl) {
    const observer = new MutationObserver(() => {
      if (popupEl.style.display !== 'block') return;
      if (!popupEl.querySelector('#popup-kod')) return;            // not a building popup
      // Reset stale link text from a prior session ("Načítám parcely…" / "Chyba: …").
      const existing = popupEl.querySelector('#popup-video');
      if (existing) { existing.textContent = '🎬 Vytvořit video'; return; }
      popupEl.insertAdjacentHTML('beforeend',
        `<a href="#" id="popup-video" style="color:#c0392b;font-weight:600">🎬 Vytvořit video</a>`);
      const link = document.getElementById('popup-video');
      link.addEventListener('click', async (ev) => {
        if (_videoState !== 'idle' && _videoState !== 'panel') {
          ev.preventDefault();
          return;
        }
        ev.preventDefault();
        const kod = popupEl.querySelector('#popup-kod')?.textContent;
        if (!kod) return;
        const b = ruianBuildings.find(rb => String(rb.kod) === kod);
        if (!b) return;
        link.textContent = '🎬 Načítám parcely…';
        try {
          await ensureParcelsData();
          let cx = 0, cz = 0;
          if (Array.isArray(b.coords) && b.coords.length >= 3) {
            for (const [x, z] of b.coords) { cx += x; cz += z; }
            cx /= b.coords.length; cz /= b.coords.length;
          }
          let containingParcel = null;
          for (const p of _parcels) {
            if (!p.ring_local || p.ring_local.length < 3) continue;
            if (pointInPolygon2D(cx, cz, p.ring_local)) {
              containingParcel = p;
              break;
            }
          }
          popupEl.style.display = 'none';
          if (containingParcel) openVideoPanel(containingParcel);
          else openVideoPanelFreeMode();
        } catch (err) {
          console.error('video link', err);
          link.textContent = '🎬 Chyba: ' + err.message;
        }
      });
    });
    observer.observe(popupEl, { childList: true, attributes: true, attributeFilter: ['style'] });
  }
}
