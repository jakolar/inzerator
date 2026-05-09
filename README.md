# inzerator

Pipeline for generating 3D village viewers + drone-style real-estate
videos from Czech RÚIAN + ČÚZK ortofoto + DSM data.

Spin-off from the gtaol experimental project, focused on real-estate
listing/marketing tooling.

## What it does

1. **Pipeline** — given a location (lat/lon), download ČÚZK ortofoto +
   DSM (Digital Surface Model) tiles, combine into a multi-tile 3D
   viewer (`*_multi.html`).
2. **Viewer** — Three.js scene showing the village as a textured 3D
   heightfield. Click a building → RÚIAN attributes + DSM-derived
   height. Click a parcel → outline painted on the terrain. Right-click
   parcel → drone-style video panel.
3. **Drone-video tool** — 9 cinematographic camera presets (top-down,
   high orbit, half-orbit, reveal pull-up, diagonal push-in, locator
   zoom, context arc, lateral fly-by, sunset orbit). 6 toggleable
   highlights (pulse outline, vertical beam, floating label, marching
   ants, volumetric glow, drop-in pin). Records via MediaRecorder,
   transcodes webm→mp4 via ffmpeg.wasm in-browser.
4. **Server** — Python `http.server` proxying ČÚZK ortofoto + WMS +
   RÚIAN ArcGIS endpoints, exposing parcel/building APIs, serving
   static viewers.

## Status

**MVP / personal use.** Most code currently lives in a single
`hnojice_multi.html` (~2.5k lines). Refactor into modular
`viewer-realtor-overlay.js` is on the roadmap (see
`docs/superpowers/plans/`).

## Layout

```
inzerator/
├── server.py                    # HTTP API + static file server
├── gen_multitile.py             # multi-tile viewer generator
├── download_ortofoto.py         # ČÚZK SM5 raw JPEG fetcher
├── download_tiff.py             # ČÚZK DSM TIFF fetcher
├── hnojice_multi.html           # canonical viewer (Hnojice; reference for new locations)
├── inspector.html               # per-building DSM/RÚIAN inspector
├── tests/
│   └── test_parcels_endpoint.py # pytest integration test against running server
├── docs/
│   ├── notes/                   # design notes (colorspace, seam gaps, sunset, code review)
│   └── superpowers/
│       ├── specs/               # feature design docs
│       └── plans/               # implementation plans
└── (gitignored)
    ├── cache/                   # DSM TIFFs + ortofoto JPEGs + parcel JSON cache
    └── tiles_<location>/        # generated GLB tiles + per-location data.json
```

## Running

```bash
# Install Python deps (rasterio, shapely, pyproj, pillow, pygltflib, open3d)
pip install rasterio shapely pyproj pillow pygltflib open3d numpy scipy

# Start the server (default port 8080, binds 0.0.0.0)
python3 server.py

# Open in browser
open http://localhost:8080/hnojice_multi.html
open http://localhost:8080/inspector.html
```

## Generating a new village viewer

1. Download ortofoto for the SM5 map sheet covering the area:
   ```bash
   python3 download_ortofoto.py --code BYSP94
   ```
2. Download DSM TIFFs covering the area (script TBD or manual ČÚZK fetch).
3. Generate the viewer:
   ```bash
   python3 gen_multitile.py --location <name> --output <name>_multi.html --glb
   ```
   This writes:
   - `<name>_multi.html` — the viewer
   - `tiles_<name>/*.glb` — per-tile DSM meshes with per-vertex UV
   - `tiles_<name>/<name>_data.json` — tile metadata + RÚIAN building footprints

## Roadmap

See `docs/notes/2026-05-09-code-review-video-tool.md` for current code
quality items. Short-term:

1. **Viewer refactor** — extract realtor overlay (drone video tool,
   parcel highlights, presentation mode) from `hnojice_multi.html` into
   `viewer-realtor-overlay.js`. Lets `gen_multitile.py` regenerate the
   viewer without losing realtor features.
2. **Dashboard UI** — separate top-level UI for managing properties
   (list, search, generate viewer per address, video gallery).
3. **MP4 conversion progress** — ffmpeg.wasm libx264 progress events
   don't fire reliably; consider switching to vp8 transcode or an
   indeterminate spinner.
4. **Production deploy** — Hetzner/DO server hosting the pipeline +
   viewer; cache management for DSM TIFFs (large).

## License

TBD (personal project for now).
