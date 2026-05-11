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

  renderer.domElement.addEventListener('click', (e) => {
    // TODO(task-5): re-introduce _videoState gate (idle|panel) once the
    // drone video subsystem migrates here — clicks during preview/recording
    // must not open the parcel popup or trigger single-parcel highlight.

    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);

    // Parcels visible? Try parcel-tile hit first — if hit, show popup and stop.
    if (_parcelGroup && _parcelGroup.visible && _parcelMeshes.length) {
      const phits = raycaster.intersectObjects(_parcelMeshes, false);
      if (phits.length) {
        const p = phits[0].object.userData.parcel;
        const popup = getBuildingPopup();
        if (popup) {
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
          document.getElementById('popup-close').addEventListener('click', () => {
            popup.style.display = 'none';
          });
        }
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

  // TODO in subsequent tasks: single-parcel highlight, drone video panel, etc.
}
