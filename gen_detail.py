"""Generate one high-detail mesh for a single address inside a v2 region.

SM5 in the inner core, fade-ring blend to the panorama mesh at the outer 50 m.
Cardinal: scene +Z = world +Y.
"""
import argparse
import json
import struct
from pathlib import Path

import numpy as np
import rasterio
from rasterio.merge import merge
from pyproj import Transformer

from gen_panorama import save_glb, add_skirt

SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)


def discover_sm5(cx, cy, half):
    """Find SM5 TIFFs covering the tile's bbox in cache/dmpok_tiff_*/."""
    out = []
    for d in sorted(Path("cache").glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            with rasterio.open(tif) as src:
                b = src.bounds
                if not (cx + half < b.left or cx - half > b.right or
                        cy + half < b.bottom or cy - half > b.top):
                    out.append(str(tif))
    return out


def load_sm5_patch(sm5_paths, cx, cy, half, step):
    """rasterio.merge of SM5 .tifs cropped/resampled to the detail bbox + step.

    Returns (data, valid_mask). data is (h, w) float; rasterio fills missing
    with -9999. Caller must NOT merge this with DMR5G (CRS would mismatch).
    """
    srcs = [rasterio.open(p) for p in sm5_paths]
    try:
        merged, _ = merge(
            srcs,
            bounds=(cx - half, cy - half, cx + half, cy + half),
            res=(step, step),
            nodata=-9999.0,
        )
    finally:
        for s in srcs:
            s.close()
    data = merged[0]
    valid = (data > 0) & (data != -9999.0)
    return data, valid


def _load_mesh_grid(manifest_dir, glb_url, half, step):
    """Read a regular-grid mesh GLB and return (grid n×n×3, half, step).
    Skirt vertices stripped (they trail the regular grid).

    Refuses Draco- and meshopt-compressed parents: their POSITION buffer
    holds an encoded blob, not a float32 array. Auto-falls back to the
    `_orig_uncompressed/<filename>` copy if it exists; otherwise raises.
    """
    path = Path(manifest_dir) / glb_url

    def _read_glb(p):
        with open(p, "rb") as f:
            raw = f.read()
        jl = struct.unpack_from("<I", raw, 12)[0]
        g = json.loads(raw[20:20 + jl].decode())
        b = raw[20 + jl + 8:]
        return g, b

    gltf, bin_data = _read_glb(path)
    prim = gltf["meshes"][0]["primitives"][0]
    is_draco = "KHR_draco_mesh_compression" in prim.get("extensions", {})
    is_meshopt = "EXT_meshopt_compression" in gltf.get("extensionsUsed", [])
    if is_draco or is_meshopt:
        orig_path = path.parent.parent / "_orig_uncompressed" / path.name
        if not orig_path.exists():
            # Try sibling _orig_uncompressed (same dir as compressed file)
            orig_path = path.parent / "_orig_uncompressed" / path.name
        if orig_path.exists():
            gltf, bin_data = _read_glb(orig_path)
            prim = gltf["meshes"][0]["primitives"][0]
        else:
            kind = "Draco" if is_draco else "meshopt"
            raise SystemExit(
                f"{path} is {kind}-compressed and no uncompressed sibling at "
                f"{orig_path} exists. gen_detail needs the uncompressed parent "
                f"GLB to sample float32 Y values for the fade band."
            )
    acc = gltf["accessors"][prim["attributes"]["POSITION"]]
    bv = gltf["bufferViews"][acc["bufferView"]]
    off = bv.get("byteOffset", 0)
    count = acc["count"]
    floats = struct.unpack_from(f"<{count*3}f", bin_data, off)
    n = int(round(2 * half / step)) + 1
    flat = np.array(floats, dtype=np.float64).reshape(-1, 3)
    return flat[: n * n].reshape(n, n, 3), half, step


def _sample_grid_y(grid, half, step, mesh_cx, mesh_cy, world_x, world_y):
    """Triangle-linear Y sample of a regular-grid mesh at world (x, y).

    Must use the same triangle convention as the face emitter
    (T1=[i, i+n, i+1], T2=[i+1, i+n, i+n+1]) — bilinear interp gives
    different Y than the actual triangulated surface inside each quad
    when the four corners aren't coplanar (e.g. SM5 cliffs at building
    edges). Mismatch shows up as a visible vertical gap along the
    inter-LOD seam where child borrows parent_y but parent's rendered
    surface follows the diagonal."""
    plx = world_x - mesh_cx
    plz = world_y - mesh_cy
    n = grid.shape[0]
    fx = (plx + half) / step
    fz = (plz + half) / step
    ix = int(fx)
    iz = int(fz)
    tx = fx - ix
    tz = fz - iz
    ix = max(0, min(n - 2, ix))
    iz = max(0, min(n - 2, iz))
    y00 = grid[iz, ix, 1]
    y10 = grid[iz + 1, ix, 1]
    y01 = grid[iz, ix + 1, 1]
    y11 = grid[iz + 1, ix + 1, 1]
    if tx + tz <= 1.0:
        return (1 - tx - tz) * y00 + tz * y10 + tx * y01
    return (1 - tz) * y01 + (1 - tx) * y10 + (tx + tz - 1) * y11


def _sample_grid_y_array(grid, half, step, mesh_cx, mesh_cy, world_x, world_y):
    """Vectorized version of _sample_grid_y for same-shape world X/Y arrays."""
    plx = world_x - mesh_cx
    plz = world_y - mesh_cy
    n = grid.shape[0]
    fx = (plx + half) / step
    fz = (plz + half) / step
    ix = np.floor(fx).astype(np.int64)
    iz = np.floor(fz).astype(np.int64)
    tx = fx - ix
    tz = fz - iz
    ix = np.clip(ix, 0, n - 2)
    iz = np.clip(iz, 0, n - 2)

    y00 = grid[iz, ix, 1]
    y10 = grid[iz + 1, ix, 1]
    y01 = grid[iz, ix + 1, 1]
    y11 = grid[iz + 1, ix + 1, 1]
    lower = tx + tz <= 1.0
    return np.where(
        lower,
        (1 - tx - tz) * y00 + tz * y10 + tx * y01,
        (1 - tz) * y01 + (1 - tx) * y10 + (tx + tz - 1) * y11,
    )


def build_detail(cx, cy, half, step, fade_width, ground_z, manifest_dir, parent, child_holes=(), no_sm5=False, smooth_sigma=0, bare_earth=None, dmr_ceiling=50.0):
    """Build the detail mesh. Y = SM5 in the inner core, linear blend with
    `parent` mesh's Y across the fade band, exact parent Y match at the
    outer edge.

    `parent` — dict with center_sjtsk, half, step, and either panorama_glb
    (region) or glb_url (another detail). The fade target is parent's
    bilinear-sampled Y at the same world position, making the seam exact
    regardless of DMR5G/SM5 resolution mismatch.

    `child_holes` — iterable of (cx, cy, half) tuples for smaller detail
    meshes that nest inside this one. Quads whose centroid falls inside
    any child's L-∞ bbox are dropped (avoids panorama-over-detail style
    occlusion at the inter-LOD seam).

    ground_z — panorama's ground_z (from manifest.json); SM5 Y normalised
    against the same reference so seams match across LODs.

    `bare_earth` — region/panorama metadata used as DMR5G reference. SM5
    samples more than `dmr_ceiling` metres above this bare-earth surface are
    treated as LIDAR cliff-edge/backscatter artefacts and replaced before
    meshing.
    """
    # --- BARE-Y feature (toggle "no buildings/trees" in viewer) ---
    # Load DMR5G bare-earth grid once (also used by ceiling/floor filter
    # below). Reused per-vertex to emit a `_BARE_Y` attribute in the GLB so
    # the viewer can blend POSITION.y ↔ bare_y via a uniform. Remove the
    # whole bracketed block + the bare_ys references and the gen_panorama
    # save_glb branch to revert.
    _bare_grid = _bare_half = _bare_step = _bare_cx = _bare_cy = None
    if bare_earth is not None:
        _bare_glb = bare_earth.get("panorama_glb") or bare_earth.get("glb_url")
        if _bare_glb:
            _bare_grid, _bare_half, _bare_step = _load_mesh_grid(
                manifest_dir, _bare_glb, bare_earth["half"], bare_earth["step"]
            )
            _bare_cx, _bare_cy = bare_earth["center_sjtsk"]
    # --- end BARE-Y ---

    if no_sm5:
        sm5_data = sm5_valid = None
    else:
        sm5_paths = discover_sm5(cx, cy, half)
        if not sm5_paths:
            raise SystemExit(
                f"No SM5 cache for ({cx},{cy}); the SM5 cache in cache/dmpok_tiff_*/ "
                f"does not cover this address bbox. Add SM5 tiles or pick a different centre."
            )
        sm5_data, sm5_valid = load_sm5_patch(sm5_paths, cx, cy, half, step)
        sm5_h, sm5_w = sm5_data.shape
        # Despike: morphology-preserving anomaly cleanup.
        # SM5 error modes:
        #   (1) Isolated upward bird-spikes (LIDAR caught a bird/dust/cloud
        #       /sheet-edge artefact) — single cell sticking above all 8
        #       neighbours by 5-70 m.
        #   (2) Downward pits — physically impossible in a DSM. Cell sits
        #       below all 8 neighbours. Strong signal of SM5 processing
        #       glitch; often clustered with co-located upward spikes.
        #   (3) Cluster artefacts (2-3 cell wide) near a glitch zone — these
        #       slip past the "above all neighbours" test but coincide with
        #       detected pits.
        # Strategy: use (2) downward pits as unambiguous detector of "SM5 is
        # buggy here", dilate into anomaly zones, apply aggressive 3 m
        # |delta-from-median| threshold inside. Independently catch (1)
        # isolated upward spikes everywhere via "above all 8 neighbours by
        # >5 m" — this preserves cliff edges (whose cells touch at least one
        # equal-elevation neighbour). Real morphology (ridges, sandstone
        # formations, gorge walls) survives entirely.
        from scipy.ndimage import (
            median_filter, minimum_filter, maximum_filter, binary_dilation,
        )
        sm5_float = sm5_data.astype("float64")

        # 8-neighbour min/max (footprint excludes the centre cell)
        footprint = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]])
        nb_min = minimum_filter(sm5_float, footprint=footprint, mode="nearest")
        nb_max = maximum_filter(sm5_float, footprint=footprint, mode="nearest")

        # (1) Isolated upward spike: cell sits >5 m above ALL 8 neighbours.
        # A real cliff top has at least one neighbour at similar elevation
        # (adjacent plateau cell) so this test wouldn't fire there.
        isolated_up = sm5_float > (nb_max + 5.0)

        # (2) Downward pit: cell sits >3 m below ALL 8 neighbours.
        pit = sm5_float < (nb_min - 3.0)

        # (3) Anomaly zone — dilate pits by 6 cells (~3 m) to catch
        # co-located cluster artefacts. Inside, aggressive |delta| > 3 m.
        anomaly_zone = binary_dilation(pit, iterations=6)
        med = median_filter(sm5_float, size=3)
        delta = sm5_float - med
        cluster_in_zone = anomaly_zone & (np.abs(delta) > 3.0)

        needs_fix = isolated_up | pit | cluster_in_zone
        sm5_data = np.where(needs_fix, med, sm5_float).astype(sm5_data.dtype)
        sm5_float = sm5_data.astype("float64")

        if _bare_grid is not None and dmr_ceiling > 0:
            rows = np.arange(sm5_h, dtype=np.float64)
            cols = np.arange(sm5_w, dtype=np.float64)
            world_x = cx - half + cols[None, :] * step
            world_y = cy + half - rows[:, None] * step
            bare_y = _sample_grid_y_array(
                _bare_grid, _bare_half, _bare_step, _bare_cx, _bare_cy,
                world_x, world_y,
            ) + ground_z

            above_bare = sm5_valid & (sm5_float > bare_y + dmr_ceiling)
            if np.any(above_bare):
                med = median_filter(sm5_float, size=3)
                replacement = np.where(med <= bare_y + dmr_ceiling, med, bare_y)
                sm5_data = np.where(above_bare, replacement, sm5_float).astype(sm5_data.dtype)
                print(f"  DMR5G ceiling fixed {int(np.count_nonzero(above_bare))} SM5 spike cells "
                      f"(>{dmr_ceiling:g} m above bare earth)")
                sm5_float = sm5_data.astype("float64")

            # DMR5G floor — symmetric to ceiling. SM5 can have multi-cell
            # deep-pit artefacts (cluster nodata-fill glitches) up to
            # several hundred metres below bare earth. The single-cell
            # pit detector earlier misses cluster pits ≥2 cells wide. A
            # cell sitting more than `dmr_ceiling` metres below DMR5G is
            # physically impossible (LIDAR doesn't see below ground) —
            # replace with bare_y. Hřensko-mega 4 km mesh had 750 cells
            # in one cluster reaching -385 m relative.
            below_bare = sm5_valid & (sm5_float < bare_y - dmr_ceiling)
            if np.any(below_bare):
                sm5_data = np.where(below_bare, bare_y.astype(sm5_data.dtype), sm5_data)
                print(f"  DMR5G floor fixed {int(np.count_nonzero(below_bare))} SM5 deep-pit cells "
                      f"(>{dmr_ceiling:g} m below bare earth)")
        if smooth_sigma > 0:
            # Soften the DSM before meshing so building-edge cliffs don't
            # produce near-vertical mesh faces that smear the ortofoto
            # texture into long pixel stripes. Costs sharpness of building
            # outlines — use sparingly (0.5-1.5 sigma for moderate effect).
            from scipy.ndimage import gaussian_filter
            sm5_data = gaussian_filter(sm5_data.astype("float64"), sigma=smooth_sigma).astype(sm5_data.dtype)
    parent_glb = parent.get("panorama_glb") or parent["glb_url"]
    parent_grid, parent_half, parent_step = _load_mesh_grid(
        manifest_dir, parent_glb, parent["half"], parent["step"]
    )
    parent_cx, parent_cy = parent["center_sjtsk"]

    n = int(round(2 * half / step)) + 1
    if sm5_data is None:
        sm5_h, sm5_w = 0, 0

    inner_radius = half - fade_width

    vertices = []
    world_coords = []
    # --- BARE-Y feature ---
    bare_ys = [] if _bare_grid is not None else None
    # --- end BARE-Y ---
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            world_x = cx + local_x
            world_y = cy + local_z

            # SM5 sample; in no_sm5 mode every vertex stays None and the
            # branches below fall through to parent_y everywhere (the mesh
            # then exactly tracks the parent's Y — useful as a "texture-only"
            # LOD ring that just carries a sharper ortho).
            if sm5_data is None:
                sm5_y = None
            else:
                dr_sm = min((n - 1) - r, sm5_h - 1)
                dc_sm = min(c, sm5_w - 1)
                sm5_y = (float(sm5_data[dr_sm, dc_sm]) - ground_z
                         if sm5_valid[dr_sm, dc_sm] else None)

            # Fade target = parent mesh's actual Y at this world pos.
            parent_y = float(_sample_grid_y(parent_grid, parent_half, parent_step,
                                            parent_cx, parent_cy, world_x, world_y))

            d = max(abs(local_x), abs(local_z))  # L-infinity distance from centre
            if d <= inner_radius:
                y = sm5_y if sm5_y is not None else parent_y
            elif d >= half:
                y = parent_y
            else:
                t = (d - inner_radius) / fade_width
                if sm5_y is None:
                    y = parent_y
                else:
                    y = (1.0 - t) * sm5_y + t * parent_y

            vertices.append([local_x, y, local_z])
            world_coords.append((world_x, world_y))
            # --- BARE-Y feature: sample DMR5G at vertex position, fall back
            # to parent_y (= panorama, which is itself DMR5G) outside grid. ---
            if bare_ys is not None:
                bare_ys.append(float(_sample_grid_y(
                    _bare_grid, _bare_half, _bare_step,
                    _bare_cx, _bare_cy, world_x, world_y,
                )))
            # --- end BARE-Y ---

    # Faces — 2 triangles per quad, dropped only where the quad's full bbox
    # lies STRICTLY inside a child detail's L-∞ bbox (all 4 vertices inside).
    # Boundary quads stay → parent extends to/past child edge, no gap when
    # step doesn't divide child's half evenly; polygonOffset handles overlap.
    faces = []
    n_carved = 0
    for r in range(n - 1):
        for c in range(n - 1):
            qxmin = cx + (-half + c * step)
            qxmax = cx + (-half + (c + 1) * step)
            qymin = cy + (-half + r * step)
            qymax = cy + (-half + (r + 1) * step)
            if any(qxmin > hcx - hhalf and qxmax < hcx + hhalf and
                   qymin > hcy - hhalf and qymax < hcy + hhalf
                   for hcx, hcy, hhalf in child_holes):
                n_carved += 1
                continue
            i = r * n + c
            faces.append([i, i + n, i + 1])
            faces.append([i + 1, i + n, i + n + 1])
    if child_holes:
        print(f"  Carved {n_carved} quads ({n_carved * 2} faces) under {len(child_holes)} child hole(s)")

    # WGS84 per-vertex UVs over the actual lon/lat envelope
    sx = np.array([w[0] for w in world_coords])
    sy = np.array([w[1] for w in world_coords])
    lons, lats = SJTSK_TO_WGS.transform(sx, sy)
    lon_min, lon_max = float(lons.min()), float(lons.max())
    lat_min, lat_max = float(lats.min()), float(lats.max())
    uvs = [
        [(float(lons[i]) - lon_min) / (lon_max - lon_min),
         (float(lats[i]) - lat_min) / (lat_max - lat_min)]
        for i in range(len(vertices))
    ]

    return {
        "vertices": vertices,
        "faces": faces,
        "uvs": uvs,
        "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
        "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
        # --- BARE-Y feature: optional per-vertex bare-earth Y (None when
        # no DMR5G ref is available). gen_panorama.save_glb adds a _BARE_Y
        # accessor if present; viewer uses it as alternate vertex Y. ---
        "bare_ys": bare_ys,
        # --- end BARE-Y ---
        # Stash for optional post-build simplification.
        "_simplify_meta": {
            "cx": cx, "cy": cy,
            "lon_min": lon_min, "lon_max": lon_max,
            "lat_min": lat_min, "lat_max": lat_max,
        },
    }


def simplify_tile(tile, voxel_size):
    """Vertex-clustering decimation via Open3D. Merges vertices that fall
    into the same `voxel_size` (metres) cube. Used to wash out single-cell
    cliff micro-triangles between SM5 samples that otherwise project as
    pixel-thin texture-stretched needles at oblique angles."""
    import open3d as o3d
    meta = tile.pop("_simplify_meta")
    verts = np.array(tile["vertices"], dtype=np.float64)
    faces = np.array(tile["faces"], dtype=np.int32)
    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(verts)
    mesh.triangles = o3d.utility.Vector3iVector(faces)
    before = len(faces)
    simp = mesh.simplify_vertex_clustering(
        voxel_size=voxel_size,
        contraction=o3d.geometry.SimplificationContraction.Average,
    )
    new_verts = np.asarray(simp.vertices)
    new_faces = np.asarray(simp.triangles)
    print(f"  Simplify (voxel={voxel_size}m): {before} → {len(new_faces)} faces "
          f"({100 * len(new_faces) / before:.0f}%)")
    # Re-derive UVs from WGS lon/lat of each new vertex's world XZ.
    cx, cy = meta["cx"], meta["cy"]
    world_x = cx + new_verts[:, 0]
    world_y = cy + new_verts[:, 2]
    new_lons, new_lats = SJTSK_TO_WGS.transform(world_x, world_y)
    span_lon = meta["lon_max"] - meta["lon_min"]
    span_lat = meta["lat_max"] - meta["lat_min"]
    new_uvs = np.stack([
        (new_lons - meta["lon_min"]) / span_lon,
        (new_lats - meta["lat_min"]) / span_lat,
    ], axis=1)
    tile["vertices"] = new_verts.tolist()
    tile["faces"] = new_faces.tolist()
    tile["uvs"] = new_uvs.tolist()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--slug", required=True,
                   help="address slug (filename-safe identifier)")
    p.add_argument("--center-sjtsk", required=True,
                   help="cx,cy (use --center-sjtsk=cx,cy for negative ČÚZK coords)")
    p.add_argument("--half", type=int, default=300,
                   help="half side length in metres; 600m square default")
    p.add_argument("--step", type=float, default=1.0,
                   help="mesh sample step in metres (can be fractional, e.g. 0.5)")
    p.add_argument("--fade", type=int, default=50,
                   help="outer fade band width in metres")
    p.add_argument("--fade-to", default="panorama",
                   help="slug of parent mesh to fade against ('panorama' = region)")
    p.add_argument("--zoom", type=int, default=19,
                   help="orto zoom level stored in manifest for the viewer texture")
    p.add_argument("--size", type=int, default=4096,
                   help="orto texture output side in px (4096 default, up to 8192)")
    p.add_argument("--skirt", type=int, default=50,
                   help="skirt drop in metres")
    p.add_argument("--no-sm5", action="store_true",
                   help="skip SM5 sampling — Y tracks parent everywhere "
                        "(texture-only LOD ring for sharper ortho coverage)")
    p.add_argument("--smooth", type=float, default=0,
                   help="Gaussian sigma (pixels) applied to SM5 before "
                        "meshing — softens building-edge cliffs so the "
                        "ortofoto doesn't smear into vertical pixel stripes")
    p.add_argument("--dmr-ceiling", type=float, default=50.0,
                   help="Replace SM5 cells more than this many metres above "
                        "DMR5G/bare-earth terrain before meshing (0 = off). "
                        "Catches LIDAR cliff-edge/backscatter spikes.")
    p.add_argument("--simplify-voxel", type=float, default=0,
                   help="Run Open3D vertex-clustering decimation with this "
                        "voxel size in metres (0 = off). 1.0 is ~4× face "
                        "reduction at native 0.5 m sampling.")
    args = p.parse_args()

    cx, cy = (float(s) for s in args.center_sjtsk.split(","))
    out_dir = Path(f"tiles_v2_{args.region}")
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(
            f"Run gen_panorama.py --region {args.region} first to create the region manifest."
        )
    manifest = json.loads(manifest_path.read_text())
    region_ground_z = manifest["region"]["ground_z"]

    # Resolve parent: 'panorama' = the region, else a detail by slug.
    if args.fade_to == "panorama":
        parent = manifest["region"]
    else:
        parent = next((d for d in manifest.get("details", []) if d["slug"] == args.fade_to), None)
        if parent is None:
            raise SystemExit(
                f"--fade-to '{args.fade_to}' not found in manifest.details; "
                f"build that parent first."
            )

    # Auto-detect children: any other detail with smaller half whose bbox lies
    # entirely inside this one's bbox (so it carves a clean hole here).
    children = []
    for d in manifest.get("details", []):
        if d["slug"] == args.slug or d["half"] >= args.half:
            continue
        dcx, dcy = d["center_sjtsk"]
        if (abs(dcx - cx) + d["half"] <= args.half
                and abs(dcy - cy) + d["half"] <= args.half):
            children.append((dcx, dcy, d["half"]))

    print(f"Generating detail '{args.slug}' at ({cx}, {cy}) — {args.half*2}m square, "
          f"step {args.step}m, fade {args.fade}m, fade_to={args.fade_to}, "
          f"children={len(children)}{', NO-SM5' if args.no_sm5 else ''} …")
    tile = build_detail(cx, cy, args.half, args.step, args.fade,
                        region_ground_z, out_dir, parent,
                        child_holes=children, no_sm5=args.no_sm5,
                        smooth_sigma=args.smooth,
                        bare_earth=manifest["region"],
                        dmr_ceiling=args.dmr_ceiling)
    if args.simplify_voxel > 0:
        simplify_tile(tile, args.simplify_voxel)
    else:
        tile.pop("_simplify_meta", None)   # not needed downstream
    # `add_skirt` indexes by the original regular-grid layout, which the
    # simplifier shuffles. Skip it when skirting is disabled or the mesh has
    # been simplified.
    if args.skirt > 0 and args.simplify_voxel == 0:
        add_skirt(tile, args.skirt, args.half, args.step)

    details_dir = out_dir / "details"
    details_dir.mkdir(parents=True, exist_ok=True)
    glb_path = details_dir / f"{args.slug}.glb"
    save_glb(tile, str(glb_path))

    detail_meta = {
        "slug": args.slug,
        "center_sjtsk": [cx, cy],
        "half": args.half,
        "step": args.step,
        "fade": args.fade,
        "fade_to": args.fade_to,
        "zoom": args.zoom,
        "size": args.size,
        "no_sm5": args.no_sm5,
        "sjtsk_bbox": tile["sjtsk_bbox"],
        "wgs_bbox": tile["wgs_bbox"],
        "glb_url": f"details/{args.slug}.glb",
    }
    manifest.setdefault("details", [])
    manifest["details"] = [d for d in manifest["details"] if d["slug"] != args.slug]
    manifest["details"].append(detail_meta)

    # Atomic manifest write (same pattern as gen_panorama main)
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(manifest_path)
    print(f"Wrote {glb_path} ({glb_path.stat().st_size // 1024} KB) "
          f"+ manifest entry '{args.slug}'")


if __name__ == "__main__":
    main()
