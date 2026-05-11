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

  // TODO in subsequent tasks: inject CSS, inject HTML, wire handlers,
  // register tick hooks, etc.
}
