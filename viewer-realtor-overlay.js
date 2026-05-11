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
    // must not open the parcel popup.
    if (!_parcelGroup || !_parcelGroup.visible || !_parcelMeshes.length) return;
    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const phits = raycaster.intersectObjects(_parcelMeshes, false);
    if (!phits.length) return;
    const p = phits[0].object.userData.parcel;
    const popup = getBuildingPopup();
    if (!popup) return;
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
    e.stopPropagation();   // don't trigger base's building-popup
  }, { capture: true });

  // TODO in subsequent tasks: single-parcel highlight, drone video panel, etc.
}
