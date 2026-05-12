"""Generate multi-tile 3D view — adjacent tiles on one page.

Usage: python gen_multitile.py --output hnojice_multi.html
       python gen_multitile.py --output hnojice_multi.html --glb
"""
import json
import struct
import math
import numpy as np
import rasterio
from pathlib import Path
from pyproj import Transformer

SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)


def save_tile_glb(tile, offset_x, offset_z, output_path):
    """Save tile mesh as .glb with uint16 indices + server-side gzip."""
    import pygltflib

    verts = np.array(tile["vertices"], dtype=np.float32)
    faces = np.array(tile["faces"], dtype=np.uint32)
    uvs = np.array(tile["uvs"], dtype=np.float32)

    # Apply offset
    verts[:, 0] += offset_x
    verts[:, 2] += offset_z

    # Use uint16 indices if possible (saves 50% on index buffer)
    if len(verts) < 65536:
        faces_packed = faces.astype(np.uint16)
        idx_component = pygltflib.UNSIGNED_SHORT
    else:
        faces_packed = faces
        idx_component = pygltflib.UNSIGNED_INT

    # Pack buffers
    verts_bytes = verts.tobytes()
    faces_bytes = faces_packed.tobytes()
    uvs_bytes = uvs.tobytes()
    all_bytes = verts_bytes + faces_bytes + uvs_bytes

    gltf = pygltflib.GLTF2(
        scene=0,
        scenes=[pygltflib.Scene(nodes=[0])],
        nodes=[pygltflib.Node(mesh=0)],
        meshes=[pygltflib.Mesh(primitives=[
            pygltflib.Primitive(
                attributes=pygltflib.Attributes(POSITION=0, TEXCOORD_0=2),
                indices=1,
            )
        ])],
        accessors=[
            pygltflib.Accessor(bufferView=0, componentType=pygltflib.FLOAT, count=len(verts), type="VEC3",
                               max=verts.max(axis=0).tolist(), min=verts.min(axis=0).tolist()),
            pygltflib.Accessor(bufferView=1, componentType=idx_component, count=faces_packed.size, type="SCALAR"),
            pygltflib.Accessor(bufferView=2, componentType=pygltflib.FLOAT, count=len(uvs), type="VEC2"),
        ],
        bufferViews=[
            pygltflib.BufferView(buffer=0, byteOffset=0, byteLength=len(verts_bytes), target=pygltflib.ARRAY_BUFFER),
            pygltflib.BufferView(buffer=0, byteOffset=len(verts_bytes), byteLength=len(faces_bytes), target=pygltflib.ELEMENT_ARRAY_BUFFER),
            pygltflib.BufferView(buffer=0, byteOffset=len(verts_bytes) + len(faces_bytes), byteLength=len(uvs_bytes), target=pygltflib.ARRAY_BUFFER),
        ],
        buffers=[pygltflib.Buffer(byteLength=len(all_bytes))],
    )
    gltf.set_binary_blob(all_bytes)
    gltf.save(output_path)
    return Path(output_path).stat().st_size


def _snap_vertices_to_footprints(vertices, footprints, cx, cy, snap_dist=1.2, frozen_indices=None):
    """Snap mesh vertices near building footprint edges to exact edge position.

    For each vertex within snap_dist of a footprint edge:
    - Project vertex XZ onto nearest edge segment
    - Move vertex XZ to projected position (keep Y/height unchanged)
    Result: sharp building outlines instead of staircase from grid.

    `frozen_indices` — set of vertex indices that must not be moved (used to
    keep seam vertices between adjacent tiles identical).
    """
    verts = [list(v) for v in vertices]
    frozen = frozen_indices or set()

    for fp in footprints:
        # Footprint coords are in local mesh space [lx, lz]
        coords = fp["coords"]
        if len(coords) < 3:
            continue

        # For each edge of footprint
        for ei in range(len(coords)):
            ej = (ei + 1) % len(coords)
            ex1, ez1 = coords[ei]
            ex2, ez2 = coords[ej]

            edge_dx = ex2 - ex1
            edge_dz = ez2 - ez1
            edge_len_sq = edge_dx * edge_dx + edge_dz * edge_dz
            if edge_len_sq < 0.01:
                continue

            # Check each vertex
            for vi in range(len(verts)):
                if vi in frozen:
                    continue
                vx = verts[vi][0]
                vz = verts[vi][2]

                # Project vertex onto edge line
                t = ((vx - ex1) * edge_dx + (vz - ez1) * edge_dz) / edge_len_sq
                if t < -0.1 or t > 1.1:
                    continue  # outside edge segment

                t = max(0, min(1, t))
                proj_x = ex1 + t * edge_dx
                proj_z = ez1 + t * edge_dz

                # Distance from vertex to projection
                dist = ((vx - proj_x) ** 2 + (vz - proj_z) ** 2) ** 0.5
                if dist < snap_dist and dist > 0.05:
                    # Snap XZ to edge, keep Y
                    verts[vi][0] = proj_x
                    verts[vi][2] = proj_z

    return verts


def extract_tile(dmp_paths, cx, cy, half=60, step=2, global_ground_z=None, ruian_footprints=None, simplify=True, flat=False, max_height=50, smooth_sigma=0):
    """Extract one tile mesh + UV data.

    `dmp_paths` may be a single path (legacy) or a list of paths covering the
    tile's bbox — when the tile straddles SM5 boundaries, multiple TIFs are
    mosaicked via rasterio.merge so seams are filled.
    """
    if isinstance(dmp_paths, (str, Path)):
        dmp_paths = [dmp_paths]

    half_m = half + 1.0  # 1m overlap each side to remove inter-tile cracks
    bounds = (cx - half_m, cy - half_m, cx + half_m, cy + half_m)
    srcs = [rasterio.open(p) for p in dmp_paths]
    try:
        from rasterio.merge import merge
        merged, transform = merge(srcs, bounds=bounds, res=(0.5, 0.5), nodata=-9999.0)
    finally:
        for s in srcs:
            s.close()

    patch = merged[0]  # band 1
    valid_mask = (patch > 0) & (patch != -9999.0)
    if not valid_mask.any():
        return None

    if global_ground_z is not None:
        ground_z = global_ground_z
    else:
        ground_z = float(np.percentile(patch[valid_mask], 5))
    # Lower clip removed: pits/riverbeds below ground_z appear as negative y.
    # Symmetric bound (-max_height) guards against raster outliers.
    patch_norm = (patch - ground_z).clip(-max_height, max_height)
    patch_norm[~valid_mask] = 0  # invalid filtered out via valid_ds on faces

    # Sample on a fixed world-space grid that is SHARED with neighbouring tiles
    # (seam meets along one single line, no 1m geometric overlap → no duplicate
    # triangles in the overlap zone that used to cause Z-fighting/holes).
    # The outer pixel margin stays only as a buffer for clean raster reading.
    pixel_size = 0.5
    buffer_px = int(round((half_m - half) / pixel_size))  # 2 px for 1m buffer
    n_total = patch_norm.shape[0]  # 244 for half=60, half_m=61
    start = buffer_px
    end = n_total - buffer_px + 1  # inclusive slice limit — see explanation below
    # Example with half=60, half_m=61, step=2:
    #   patch_norm.shape = (244, 244), start=2, end=243
    #   patch_ds has 121 samples at pixel indices 2, 4, ..., 242
    #   Sample centres in local coords: -59.75, -58.75, ..., +60.25
    #   Tile B (cx + 120) leftmost sample at local -59.75 → world cx + 60.25,
    #   Tile A rightmost sample at local +60.25 → world cx + 60.25 → shared line.
    # When step > 2, plain stride-sample drops tall thin features (towers,
    # narrow trees) that fall between sample centres. Apply a max filter at
    # the step radius first so any peak in the window survives the subsample.
    if step > 2:
        from scipy.ndimage import maximum_filter
        patch_norm = maximum_filter(patch_norm, size=step)
    patch_ds = patch_norm[start:end:step, start:end:step]
    valid_ds = valid_mask[start:end:step, start:end:step]

    # Smooth terrain to reduce jaggedness
    if smooth_sigma > 0:
        from scipy.ndimage import gaussian_filter
        smoothed = gaussian_filter(patch_ds.astype(np.float64), sigma=smooth_sigma)
        patch_ds = smoothed.astype(patch_ds.dtype)
    ds_rows, ds_cols = patch_ds.shape

    # Local coord of first sampled pixel centre (same for x and z axes).
    local0 = -half_m + (start + 0.5) * pixel_size  # e.g. -59.75
    local_step = step * pixel_size                  # 1.0 m for step=2

    vertices = []
    for r in range(ds_rows):
        for c in range(ds_cols):
            x = local0 + c * local_step
            y = 0.0 if flat else float(patch_ds[r, c])
            z = local0 + r * local_step
            vertices.append([x, y, z])

    faces = []
    for r in range(ds_rows - 1):
        for c in range(ds_cols - 1):
            if not (valid_ds[r, c] and valid_ds[r, c+1] and valid_ds[r+1, c] and valid_ds[r+1, c+1]):
                continue
            i = r * ds_cols + c
            faces.append([i, i + ds_cols, i + 1])
            faces.append([i + 1, i + ds_cols, i + ds_cols + 1])

    # Snap vertices near building edges to exact footprint lines.
    # Freeze the outermost 2 grid rows/cols so the seam vertices stay identical
    # between adjacent tiles (otherwise a footprint that straddles a seam would
    # pull the two tiles' edge vertices to different positions).
    if ruian_footprints:
        frozen = set()
        EDGE_BAND = 2  # number of outermost rows/cols to freeze
        for r in range(ds_rows):
            for c in range(ds_cols):
                if r < EDGE_BAND or r >= ds_rows - EDGE_BAND or \
                   c < EDGE_BAND or c >= ds_cols - EDGE_BAND:
                    frozen.add(r * ds_cols + c)
        vertices = _snap_vertices_to_footprints(
            vertices, ruian_footprints, cx, cy, snap_dist=1.2,
            frozen_indices=frozen,
        )

    # Per-vertex UV via WGS84
    all_sx = np.array([cx + v[0] for v in vertices])
    all_sy = np.array([cy - v[2] for v in vertices])
    all_lon, all_lat = SJTSK_TO_WGS.transform(all_sx, all_sy)

    lon_margin = (all_lon.max() - all_lon.min()) * 0.05
    lat_margin = (all_lat.max() - all_lat.min()) * 0.05
    lon_min = float(all_lon.min() - lon_margin)
    lon_max = float(all_lon.max() + lon_margin)
    lat_min = float(all_lat.min() - lat_margin)
    lat_max = float(all_lat.max() + lat_margin)

    uvs = []
    for i in range(len(vertices)):
        u = (all_lon[i] - lon_min) / (lon_max - lon_min)
        v = (all_lat[i] - lat_min) / (lat_max - lat_min)
        uvs.append([float(u), float(v)])

    center_lon, center_lat = SJTSK_TO_WGS.transform(cx, cy)

    # Mesh simplification via Open3D (skippable with simplify=False)
    import open3d as o3d
    simp_vertices = vertices
    simp_faces = faces
    simp_uvs = uvs
    if not simplify:
        return {
            "vertices": simp_vertices,
            "faces": simp_faces,
            "uvs": simp_uvs,
            "center_sjtsk": [float(cx), float(cy)],
            "center_wgs84": [float(center_lon), float(center_lat)],
            "ground_z": ground_z,
            "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
            "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
        }
    try:
        mesh_o3d = o3d.geometry.TriangleMesh()
        mesh_o3d.vertices = o3d.utility.Vector3dVector(np.array(vertices))
        mesh_o3d.triangles = o3d.utility.Vector3iVector(np.array(faces))
        mesh_o3d.compute_vertex_normals()

        # Adaptive simplification based on content complexity
        heights = np.array([v[1] for v in vertices])
        max_height = heights.max()
        height_std = heights.std()

        if height_std > 2.0:
            divisor = 8
        elif height_std > 0.5:
            divisor = 12
        else:
            divisor = 20
        target = max(len(faces) // divisor, 100)
        # boundary_weight=1000 asks Open3D to preserve topological boundary
        # (outer edge of tile grid) — prevents thin gaps on seams with adjacent
        # tiles after simplification.
        simplified = mesh_o3d.simplify_quadric_decimation(target, boundary_weight=1000.0)

        # Clean up artifacts from decimation that cause micro-holes in render:
        # degenerate triangles (zero area), duplicated/overlapping triangles,
        # duplicated vertices at same position, non-manifold edges.
        simplified.remove_degenerate_triangles()
        simplified.remove_duplicated_triangles()
        simplified.remove_duplicated_vertices()
        simplified.remove_non_manifold_edges()

        sv = np.asarray(simplified.vertices)
        sf = np.asarray(simplified.triangles)

        # Clip negative heights from simplification interpolation
        sv[:, 1] = np.clip(sv[:, 1], 0, 50)

        # Snap boundary vertices back to exact seam positions AND restore
        # height from source raster. Both adjacent tiles sample the same
        # world-space raster at the seam, so Y values match exactly → no
        # visible blue-line gap at tile edges.
        lx_min_edge = local0
        lx_max_edge = local0 + (ds_cols - 1) * local_step
        lz_min_edge = local0
        lz_max_edge = local0 + (ds_rows - 1) * local_step
        SNAP_TOL = 0.5
        for vi in range(len(sv)):
            x, _, z = sv[vi]
            on_x_edge = abs(x - lx_min_edge) < SNAP_TOL or abs(x - lx_max_edge) < SNAP_TOL
            on_z_edge = abs(z - lz_min_edge) < SNAP_TOL or abs(z - lz_max_edge) < SNAP_TOL
            if on_x_edge:
                sv[vi, 0] = lx_min_edge if abs(x - lx_min_edge) < abs(x - lx_max_edge) else lx_max_edge
            if on_z_edge:
                sv[vi, 2] = lz_min_edge if abs(z - lz_min_edge) < abs(z - lz_max_edge) else lz_max_edge
            # For any vertex on the tile boundary, restore its height from the
            # source raster at nearest grid cell. This guarantees that both
            # adjacent tiles assign the SAME Y to shared seam vertices.
            if on_x_edge or on_z_edge:
                c_i = int(round((sv[vi, 0] - local0) / local_step))
                r_i = int(round((sv[vi, 2] - local0) / local_step))
                c_i = max(0, min(ds_cols - 1, c_i))
                r_i = max(0, min(ds_rows - 1, r_i))
                # Seam vertices MUST match between adjacent tiles. Each tile
                # reads its own rasterio.merge output with different bounds, so
                # negative values (pits) can mismatch across the seam and create
                # vertical walls. Clip seam height to ≥ 0 — pit depth is lost
                # exactly on tile edges but is preserved in the tile interior.
                sv[vi, 1] = float(max(0, patch_ds[r_i, c_i]))

        simp_vertices = sv.tolist()
        simp_faces = sf.tolist()

        # Recompute UV for simplified vertices
        s_sx = np.array([cx + v[0] for v in simp_vertices])
        s_sy = np.array([cy - v[2] for v in simp_vertices])
        s_lon, s_lat = SJTSK_TO_WGS.transform(s_sx, s_sy)
        simp_uvs = []
        for i in range(len(simp_vertices)):
            u = (s_lon[i] - lon_min) / max(lon_max - lon_min, 1e-10)
            v = (s_lat[i] - lat_min) / max(lat_max - lat_min, 1e-10)
            simp_uvs.append([float(u), float(v)])
    except Exception as e:
        print(f"    Simplification failed: {e}")

    print(f"    Full: {len(vertices)} verts → Simplified: {len(simp_vertices)} verts ({100-len(simp_vertices)/len(vertices)*100:.0f}% reduction)")

    return {
        "vertices": simp_vertices,
        "faces": simp_faces,
        "uvs": simp_uvs,
        "center_sjtsk": [float(cx), float(cy)],
        "center_wgs84": [float(center_lon), float(center_lat)],
        "ground_z": ground_z,
        "wgs_bbox": [lon_min, lat_min, lon_max, lat_max],
        "sjtsk_bbox": [cx - half, cy - half, cx + half, cy + half],
    }


def extract_panorama_tile(dmp_paths, cx, cy, half, step, global_ground_z, max_height, skirt_depth):
    """Extract a coarse outer-ring panorama tile mesh + UV data.

    Samples DMP directly at `step` metre resolution via rasterio.merge, builds a
    regular n×n vertex grid, adds a 4-edge skirt wall (vertical drop of
    skirt_depth metres) along all tile borders, and returns the same
    {"vertices", "faces", "uvs"} dict that save_tile_glb() expects.
    """
    from rasterio.merge import merge

    bounds = (cx - half, cy - half, cx + half, cy + half)
    srcs = [rasterio.open(p) for p in dmp_paths]
    try:
        merged, transform = merge(srcs, bounds=bounds, res=(step, step), nodata=-9999.0)
    finally:
        for s in srcs:
            s.close()

    data = merged[0]  # shape (n, n)
    valid_mask = (data > 0) & (data != -9999.0)

    n = round(2 * half / step) + 1  # e.g. 61 for half=1500, step=50

    # Build terrain grid vertices: local_x = -half + c*step, local_z = -half + r*step
    vertices = []
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            # data row 0 = south (cy-half), row n-1 = north (cy+half);
            # but rasterio merge with S-JTSK (northing increases upward) flips rows,
            # so row 0 in the array = northern edge. Mirror to match local_z convention.
            dr = (n - 1) - r
            dc = c
            if dr < data.shape[0] and dc < data.shape[1] and valid_mask[dr, dc]:
                raw_y = float(data[dr, dc]) - global_ground_z
                y = float(max(0.0, min(raw_y, max_height)))
            else:
                y = 0.0
            vertices.append([local_x, y, local_z])

    # Terrain faces (two tris per quad)
    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            i = r * n + c
            # quad: (r,c), (r+1,c), (r,c+1), (r+1,c+1)
            faces.append([i,         i + n,     i + 1])
            faces.append([i + 1,     i + n,     i + n + 1])

    # Skirt rim — add n bottom vertices per edge (4 edges), then quad strips.
    # Edge order: top (r=0), bottom (r=n-1), left (c=0), right (c=n-1).
    # Each skirt bottom vertex sits directly below its terrain edge vertex at y - skirt_depth.
    base_vert_count = len(vertices)

    def skirt_bottom_y(vi):
        return vertices[vi][1] - skirt_depth

    # Top edge (r=0, c=0..n-1)  — outward normal faces -Z direction
    top_skirt_start = base_vert_count
    for c in range(n):
        vi = 0 * n + c
        vx, vy, vz = vertices[vi]
        vertices.append([vx, vy - skirt_depth, vz])
    for c in range(n - 1):
        ti = 0 * n + c          # terrain top-edge vertex at (r=0, c)
        si = top_skirt_start + c  # skirt bottom vertex below it
        # quad: terrain[c], terrain[c+1], skirt[c], skirt[c+1]
        # outward normal = -Z → wind CW from -Z side → CCW from outside
        faces.append([ti,     ti + 1,     si])
        faces.append([si,     ti + 1,     si + 1])

    # Bottom edge (r=n-1, c=0..n-1) — outward normal faces +Z
    bot_skirt_start = len(vertices)
    for c in range(n):
        vi = (n - 1) * n + c
        vx, vy, vz = vertices[vi]
        vertices.append([vx, vy - skirt_depth, vz])
    for c in range(n - 1):
        ti = (n - 1) * n + c
        si = bot_skirt_start + c
        # outward normal = +Z → wind opposite order
        faces.append([ti + 1, ti,     si + 1])
        faces.append([si + 1, ti,     si])

    # Left edge (c=0, r=0..n-1) — outward normal faces -X
    left_skirt_start = len(vertices)
    for r in range(n):
        vi = r * n + 0
        vx, vy, vz = vertices[vi]
        vertices.append([vx, vy - skirt_depth, vz])
    for r in range(n - 1):
        ti = r * n + 0
        si = left_skirt_start + r
        # outward normal = -X
        faces.append([ti + n, ti,     si + 1])
        faces.append([si + 1, ti,     si])

    # Right edge (c=n-1, r=0..n-1) — outward normal faces +X
    right_skirt_start = len(vertices)
    for r in range(n):
        vi = r * n + (n - 1)
        vx, vy, vz = vertices[vi]
        vertices.append([vx, vy - skirt_depth, vz])
    for r in range(n - 1):
        ti = r * n + (n - 1)
        si = right_skirt_start + r
        # outward normal = +X
        faces.append([ti,     ti + n,     si])
        faces.append([si,     ti + n,     si + 1])

    # UVs: uniform [0..1] for terrain grid; skirt verts copy their parent edge UV
    all_sx = np.array([cx + v[0] for v in vertices])
    # Note: in S-JTSK, northing = cy - local_z (Z increases southward in local space)
    all_sy = np.array([cy - v[2] for v in vertices])
    all_lon, all_lat = SJTSK_TO_WGS.transform(all_sx, all_sy)

    lon_min = float(all_lon[:base_vert_count].min())
    lon_max = float(all_lon[:base_vert_count].max())
    lat_min = float(all_lat[:base_vert_count].min())
    lat_max = float(all_lat[:base_vert_count].max())
    lon_range = max(lon_max - lon_min, 1e-10)
    lat_range = max(lat_max - lat_min, 1e-10)

    uvs = []
    for i in range(len(vertices)):
        u = float((all_lon[i] - lon_min) / lon_range)
        v_uv = float((all_lat[i] - lat_min) / lat_range)
        uvs.append([u, v_uv])

    return {"vertices": vertices, "faces": faces, "uvs": uvs}


LOCATIONS = {
    "hnojice":  {"cx": -547700,   "cy": -1107700,   "output": "hnojice_multi.html", "label": "Hnojice"},
    "santovka": {"cx": -546820.8, "cy": -1121852.6, "output": "santovka_multi.html", "label": "Šantovka"},
    "strazek":  {"cx": -625362.12, "cy": -1130203.92, "output": "strazek_multi.html", "label": "Strážek č.p. 52"},
}


def discover_tiffs():
    """Scan cache/dmpok_tiff_*/ and return list of (path, bbox) for each TIF."""
    out = []
    for d in sorted(Path("cache").glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            with rasterio.open(tif) as src:
                b = src.bounds
                out.append((str(tif), (b.left, b.bottom, b.right, b.top)))
    return out


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", choices=list(LOCATIONS.keys()), default="hnojice")
    parser.add_argument("--output", default=None)
    parser.add_argument("--center-sjtsk", default=None, help="Override: 'cx,cy' in S-JTSK")
    parser.add_argument("--half", type=int, default=60)
    parser.add_argument("--step", type=int, default=2, help="Mesh step in pixels (1=0.5m, 2=1.0m, 4=2.0m)")
    parser.add_argument("--no-snap", action="store_true", help="Disable vertex snapping to footprints")
    parser.add_argument("--no-simplify", action="store_true", help="Disable mesh simplification (precise edges, larger files)")
    parser.add_argument("--flat", action="store_true", help="Flatten mesh — all y=0 (debug: see how tiles align without heights)")
    parser.add_argument("--smooth", type=float, default=0, help="Gaussian sigma for terrain smoothing (0=off, 3=moderate, 6=very smooth)")
    parser.add_argument("--glb", action="store_true", help="Export tiles as .glb files")
    parser.add_argument("--grid", type=int, default=3, help="Grid size NxN (default 3)")
    parser.add_argument("--ground-z", type=float, default=None,
        help="Override ground reference (m). 0 = absolute heights above sea level. Default: auto (5th percentile)")
    parser.add_argument("--max-height", type=float, default=None,
        help="Override max height clip (m). Default: area_max - ground_z + 30. Use higher value for sharp peaks with tall structures.")
    parser.add_argument("--add-panorama", action="store_true",
        help="Generate 8 outer-ring panorama tiles (3000m, 50m step) around the existing 25x25 inner grid and APPEND them to the existing data.json. Skips inner tile generation.")
    parser.add_argument("--tiles-dir", default=None,
        help="Override tile output directory (default: tiles_<location>). Use tiles_hnojice_multi for the existing Hnojice viewer with symlinked tile data.")
    args = parser.parse_args()

    loc = LOCATIONS[args.location]
    grid_cx = loc["cx"]
    grid_cy = loc["cy"]
    location_label = loc.get("label", args.location.capitalize())
    output_path = args.output or loc["output"]
    if args.center_sjtsk:
        grid_cx, grid_cy = (float(v) for v in args.center_sjtsk.split(","))

    half = args.half
    tile_size = half * 2  # 120m per tile
    grid_cols = args.grid
    grid_rows = args.grid

    # Auto-discover TIFFs covering the requested area
    available_tiffs = discover_tiffs()
    print(f"  Discovered {len(available_tiffs)} cached TIFFs")

    def find_tiffs(cx, cy, half_m=0):
        """Return all TIFs whose bbox intersects the tile's bbox."""
        tx0, ty0, tx1, ty1 = cx - half_m, cy - half_m, cx + half_m, cy + half_m
        out = []
        for path, (x0, y0, x1, y1) in available_tiffs:
            if not (tx1 < x0 or tx0 > x1 or ty1 < y0 or ty0 > y1):
                out.append(path)
        return out

    def find_tiff(cx, cy):
        hits = find_tiffs(cx, cy)
        return hits[0] if hits else None

    # Global ground level from the ENTIRE grid area — using only the center
    # would place valley tiles below ground_z, and clip(0,50) then flattens
    # all valley buildings onto the floor.
    area_half = grid_cols * tile_size / 2 + 20
    area_bounds = (grid_cx - area_half, grid_cy - area_half,
                   grid_cx + area_half, grid_cy + area_half)
    area_paths = find_tiffs(grid_cx, grid_cy, half_m=area_half)
    if not area_paths:
        raise SystemExit(f"No cached TIFF covers area around ({grid_cx}, {grid_cy}). Run download_tiff.py first.")
    srcs = [rasterio.open(p) for p in area_paths]
    try:
        from rasterio.merge import merge
        area_merged, _ = merge(srcs, bounds=area_bounds, res=(2.0, 2.0), nodata=-9999.0)
    finally:
        for s in srcs:
            s.close()
    area_patch = area_merged[0]
    area_valid = (area_patch > 0) & (area_patch != -9999.0)
    if args.ground_z is not None:
        global_ground_z = float(args.ground_z)
        print(f"  Global ground_z: {global_ground_z:.1f}m (override)")
    else:
        global_ground_z = float(np.percentile(area_patch[area_valid], 5))
    # Headroom must clear the absolute area maximum, NOT the 99th percentile.
    # Earlier formula (p99 + 15) clipped peaks: e.g. on Sněžka it capped y at 1580m
    # while real peak + tower reaches ~1620m, so summit appeared as a flat plateau.
    area_max = float(area_patch[area_valid].max())
    max_height = max(50.0, (area_max - global_ground_z) + 30.0)
    if args.max_height is not None:
        max_height = float(args.max_height)
        print(f"  max_height: {max_height:.1f}m (override)")
    print(f"  Global ground_z: {global_ground_z:.1f}m, max_height: {max_height:.1f}m (area_max={area_max:.1f}m, {len(area_paths)} TIFs)")

    # --add-panorama: generate 8 outer-ring tiles and append to existing data.json
    if args.add_panorama:
        tiles_dir = args.tiles_dir or f"tiles_{args.location}"
        data_path = Path(tiles_dir) / f"{args.location}_data.json"
        if not data_path.exists():
            raise SystemExit(f"data.json not found at {data_path} — run full tile generation first.")
        existing_data = json.loads(data_path.read_text())
        if "tiles" not in existing_data or not isinstance(existing_data["tiles"], list):
            raise SystemExit(f"{data_path} does not contain a 'tiles' array.")

        PANORAMA_OFFSETS = [
            (-3000, +3000),  # NW
            (    0, +3000),  # N
            (+3000, +3000),  # NE
            (-3000,     0),  # W
            (+3000,     0),  # E
            (-3000, -3000),  # SW
            (    0, -3000),  # S
            (+3000, -3000),  # SE
        ]
        PANO_HALF = 1500   # 3000m tile, half = 1500m
        PANO_STEP = 50
        SKIRT_DEPTH = 40

        pano_count = 0
        for i, (offset_x, offset_z) in enumerate(PANORAMA_OFFSETS):
            pcx = grid_cx + offset_x
            pcy = grid_cy - offset_z  # Z-axis inversion

            dmp_paths = find_tiffs(pcx, pcy, half_m=PANO_HALF)
            if not dmp_paths:
                print(f"  Skipping panorama tile at offset ({offset_x}, {offset_z}) — no DMP coverage")
                continue

            print(f"  Generating panorama tile {i} at offset ({offset_x:+d}, {offset_z:+d}), center=({pcx:.0f}, {pcy:.0f}) [{len(dmp_paths)} TIFs]")
            tile = extract_panorama_tile(
                dmp_paths, pcx, pcy,
                half=PANO_HALF, step=PANO_STEP,
                global_ground_z=global_ground_z,
                max_height=max_height,
                skirt_depth=SKIRT_DEPTH,
            )

            glb_name = f"panorama_{i}.glb"
            glb_path = Path(tiles_dir) / glb_name
            size = save_tile_glb(tile, offset_x=offset_x, offset_z=offset_z, output_path=str(glb_path))
            print(f"    → {glb_path} ({size // 1024} KB, {len(tile['vertices'])} verts, {len(tile['faces'])} faces)")

            # WGS84 bbox via pyproj (same transformer used elsewhere)
            sjtsk_bbox = [pcx - PANO_HALF, pcy - PANO_HALF, pcx + PANO_HALF, pcy + PANO_HALF]
            corners_sx = np.array([sjtsk_bbox[0], sjtsk_bbox[2], sjtsk_bbox[0], sjtsk_bbox[2]])
            corners_sy = np.array([sjtsk_bbox[1], sjtsk_bbox[1], sjtsk_bbox[3], sjtsk_bbox[3]])
            corners_lon, corners_lat = SJTSK_TO_WGS.transform(corners_sx, corners_sy)
            wgs_bbox = [
                float(corners_lon.min()), float(corners_lat.min()),
                float(corners_lon.max()), float(corners_lat.max()),
            ]

            panorama_meta = {
                "grid_col": None,
                "grid_row": None,
                "offset_x": offset_x,
                "offset_z": offset_z,
                "center_sjtsk": [float(pcx), float(pcy)],
                "ground_z": global_ground_z,
                "wgs_bbox": wgs_bbox,
                "sjtsk_bbox": sjtsk_bbox,
                "glb_url": f"{tiles_dir}/{glb_name}",
                "is_panorama": True,
            }
            existing_data["tiles"].append(panorama_meta)
            pano_count += 1

        data_path.write_text(json.dumps(existing_data))
        print(f"Added {pano_count} panorama tiles to {data_path}")
        return

    # Pre-fetch RÚIAN footprints for vertex snapping
    import requests as req
    area_half = max(grid_cols, grid_rows) * tile_size / 2 + 20
    all_ruian_sjtsk = []
    try:
        ruian_url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/3/query"
        resp = req.get(ruian_url, params={
            "geometry": json.dumps({"xmin": grid_cx - area_half, "ymin": grid_cy - area_half,
                                    "xmax": grid_cx + area_half, "ymax": grid_cy + area_half,
                                    "spatialReference": {"wkid": 5514}}),
            "geometryType": "esriGeometryEnvelope", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "kod", "outSR": "5514", "f": "json", "returnGeometry": "true",
            "resultRecordCount": "500",
        }, timeout=60)
        for f in resp.json().get("features", []):
            rings = f["geometry"]["rings"][0]
            all_ruian_sjtsk.append(rings)  # S-JTSK coords
        print(f"  RÚIAN footprints for snapping: {len(all_ruian_sjtsk)}")
    except Exception as e:
        print(f"  RÚIAN fetch failed: {e}")

    tiles = []
    for gr in range(grid_rows):
        for gc in range(grid_cols):
            cx = grid_cx + (gc - grid_cols // 2) * tile_size
            cy = grid_cy + (gr - grid_rows // 2) * tile_size

            # Auto-resolve TIFFs that cover the tile bbox (may be 1–4)
            dmp_paths = find_tiffs(cx, cy, half + 1.0)
            dmp_path = dmp_paths[0] if dmp_paths else None

            if not dmp_paths:
                print(f"  Skip ({cx:.0f}, {cy:.0f}): outside available tiles")
                continue

            if not Path(dmp_path).exists():
                print(f"  Skip ({cx:.0f}, {cy:.0f}): no TIFF")
                continue

            tiff_names = ", ".join(Path(p).stem for p in dmp_paths)
            print(f"  Tile ({gc},{gr}): center=({cx:.0f}, {cy:.0f}) → [{tiff_names}]")

            # Convert RÚIAN footprints to this tile's local coords
            tile_fps = []
            for ring in all_ruian_sjtsk:
                local_coords = []
                in_tile = False
                for sx, sy in ring:
                    lx = sx - cx
                    lz = -(sy - cy)
                    local_coords.append([lx, lz])
                    if abs(lx) < half + 5 and abs(lz) < half + 5:
                        in_tile = True
                if in_tile:
                    tile_fps.append({"coords": local_coords})

            tile = extract_tile(dmp_paths, cx, cy, half, step=args.step,
                                global_ground_z=global_ground_z,
                                ruian_footprints=tile_fps if not args.no_snap else None,
                                simplify=not args.no_simplify,
                                flat=args.flat,
                                max_height=max_height,
                                smooth_sigma=args.smooth)
            if tile:
                tile["grid_col"] = gc
                tile["grid_row"] = gr
                tiles.append(tile)
            else:
                print(f"    No valid data")

    print(f"Generated {len(tiles)} tiles")

    # Global centroid for scene positioning
    # Use user-supplied grid center (not mean of tiles) — when tiles are missing
    # at edges (e.g. coverage gap at country border), mean shifts the origin
    # away from where the client expects (0,0) = grid_cx,grid_cy.
    gcx = grid_cx
    gcy = grid_cy

    # Fetch RÚIAN buildings for the area
    import requests as req
    area_half = grid_cols * tile_size / 2 + 20
    ruian_url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/3/query"
    try:
        resp = req.get(ruian_url, params={
            "geometry": json.dumps({
                "xmin": grid_cx - area_half, "ymin": grid_cy - area_half,
                "xmax": grid_cx + area_half, "ymax": grid_cy + area_half,
                "spatialReference": {"wkid": 5514},
            }),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "kod,cisladomovni,zpusobvyuzitikod,zastavenaplocha",
            "outSR": "5514", "f": "json", "returnGeometry": "true",
            "resultRecordCount": "500",
        }, timeout=60)

        ruian_buildings = []
        for f in resp.json().get("features", []):
            rings = f["geometry"]["rings"][0]
            from shapely.geometry import Polygon as SPoly
            poly = SPoly(rings)
            if not poly.is_valid or poly.area < 1:
                continue
            a = f["attributes"]
            bx, by = poly.centroid.x, poly.centroid.y
            blon, blat = SJTSK_TO_WGS.transform(bx, by)
            # Convert footprint coords to mesh local
            local_coords = []
            for sx, sy in rings:
                lx = sx - gcx
                lz = -(sy - gcy)
                local_coords.append([lx, lz])
            ruian_buildings.append({
                "kod": a["kod"],
                "cislo": a.get("cisladomovni", "") or "",
                "zpusob": a.get("zpusobvyuzitikod", 0),
                "plocha": round(poly.area, 0),
                "wgs84": [round(blon, 7), round(blat, 7)],
                "coords": local_coords,
            })
        print(f"  RÚIAN buildings: {len(ruian_buildings)}")
    except Exception as e:
        print(f"  RÚIAN failed: {e}")
        ruian_buildings = []

    ruian_json = json.dumps(ruian_buildings)

    # Add tile offsets relative to global centroid
    for t in tiles:
        t["offset_x"] = t["center_sjtsk"][0] - gcx
        t["offset_z"] = -(t["center_sjtsk"][1] - gcy)  # negate Y→Z

    # Export .glb files if requested
    if args.glb:
        glb_dir = Path(output_path).parent / f"tiles_{Path(output_path).stem}"
        glb_dir.mkdir(exist_ok=True)
        total_glb = 0
        for t in tiles:
            glb_path = glb_dir / f"tile_{t['grid_col']}_{t['grid_row']}.glb"
            size = save_tile_glb(t, t["offset_x"], t["offset_z"], str(glb_path))
            total_glb += size
        print(f"  GLB files: {len(tiles)} × avg {total_glb // len(tiles) // 1024} KB = {total_glb // 1024} KB total")

        # For GLB mode: tiles_json only has metadata (no vertices/faces/uvs)
        tiles_meta = []
        for t in tiles:
            tiles_meta.append({
                "grid_col": t["grid_col"],
                "grid_row": t["grid_row"],
                "offset_x": t["offset_x"],
                "offset_z": t["offset_z"],
                "center_sjtsk": t["center_sjtsk"],
                "ground_z": t["ground_z"],
                "wgs_bbox": t["wgs_bbox"],
                "sjtsk_bbox": t["sjtsk_bbox"],
                "glb_url": f"{glb_dir.name}/tile_{t['grid_col']}_{t['grid_row']}.glb",
            })
        tiles_json = json.dumps(tiles_meta)
        use_glb = True
    else:
        tiles_json = json.dumps(tiles)
        use_glb = False

    # Ortofoto URL template (proxy)

    html = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>{location_label} — Multi-tile 3D</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ overflow: hidden; background: #87ceeb; font-family: system-ui, sans-serif; }}
  #info {{
    position: absolute; top: 10px; left: 10px; z-index: 10;
    background: rgba(255,255,255,0.95); padding: 14px 18px; border-radius: 8px;
    font-size: 13px; box-shadow: 0 2px 10px rgba(0,0,0,0.2); max-width: 320px;
  }}
  #info h2 {{ margin: 0 0 6px; color: #1a73e8; font-size: 15px; }}
  #info p {{ margin: 3px 0; color: #555; font-size: 12px; }}
  #info a {{ color: #1a73e8; text-decoration: none; }}
  #info label {{ display: block; margin: 4px 0; }}
  #info input[type=range] {{ width: 100%; }}
  #info hr {{ border: none; border-top: 1px solid #ddd; margin: 8px 0; }}
  #click-info {{
    position: absolute; top: 10px; right: 10px; z-index: 10;
    background: rgba(255,255,255,0.95); padding: 14px 18px; border-radius: 8px;
    font-size: 13px; box-shadow: 0 2px 10px rgba(0,0,0,0.2); min-width: 220px;
    display: none;
  }}
  #click-info h3 {{ margin: 0 0 6px; color: #c0392b; font-size: 14px; }}
  #click-info .row {{ margin: 4px 0; }}
  #click-info .label {{ color: #888; }}
  #click-info a {{ color: #1a73e8; text-decoration: none; }}
  #building-popup {{
    position: absolute; z-index: 20;
    background: rgba(255,255,255,0.97); padding: 14px 18px; border-radius: 8px;
    font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,0.3); min-width: 200px;
    display: none; pointer-events: auto;
  }}
  #building-popup h3 {{ margin: 0 0 8px; color: #1a73e8; font-size: 15px; }}
  #building-popup .row {{ margin: 4px 0; }}
  #building-popup .label {{ color: #888; }}
  #building-popup a {{ color: #1a73e8; text-decoration: none; display: block; margin-top: 8px; }}
  #building-popup .close {{ position: absolute; top: 6px; right: 10px; cursor: pointer; color: #999; font-size: 18px; }}
  canvas {{ display: block; cursor: crosshair; }}
</style>
</head>
<body>
<div id="info">
  <h2>{location_label} — Multi-tile 3D</h2>
  <p><b>Tiles:</b> {len(tiles)} ({grid_cols}x{grid_rows} grid, {tile_size}m each)</p>
  <p><b>Mesh:</b> {args.step * 0.5:.2f} m{' (native DMP)' if args.step == 1 else ''}{', simplified' if not args.no_simplify else ', raw grid'}</p>
  <p><b>Ovládání:</b> levé tl. = posun, pravé = rotace, kolečko = zoom</p>
  <p>Klikni pro výšku + Mapy.cz odkaz. Double-click = recenter.</p>
  <hr>
  <label>Výškové zesílení: <span id="exag-val">1.0</span>x
    <input type="range" id="exag" min="0.5" max="5" step="0.1" value="1.0">
  </label>
  <label><input type="checkbox" id="wireframe"> Wireframe</label>
  <hr>
  <p><b>Podklad:</b></p>
  <label><b>Kvalita ortofota:</b></label>
  <select id="ortho-quality" style="width:100%;padding:4px;margin:4px 0;border-radius:4px;border:1px solid #ccc;">
    <option value="256">256px (rychlé, 15 KB)</option>
    <option value="512">512px (komprese, 49 KB)</option>
    <option value="640">640px (nativní, 65 KB)</option>
    <option value="768" selected>768px (nativní raw, ~120 KB)</option>
    <option value="1024">1024px (upscale, 200 KB)</option>
  </select>
  <label><b>Podklad:</b></label>
  <select id="basemap-select" style="width:100%;padding:4px;margin:4px 0;border-radius:4px;border:1px solid #ccc;">
    <optgroup label="Aktuální">
      <option value="ortofoto_cuzk" selected>ČÚZK Ortofoto (aktuální)</option>
      <option value="ortofoto_esri">Esri World Imagery</option>
    </optgroup>
    <optgroup label="Archiv ČÚZK ({location_label})">
      <option value="ortofoto_2022">2022</option>
      <option value="ortofoto_2020">2020</option>
      <option value="ortofoto_2018">2018</option>
      <option value="ortofoto_2016">2016</option>
      <option value="ortofoto_2014">2014</option>
      <option value="ortofoto_2012">2012</option>
      <option value="ortofoto_2009">2009</option>
      <option value="ortofoto_2006">2006</option>
      <option value="ortofoto_2003">2003</option>
      <option value="ortofoto_2000">2000</option>
    </optgroup>
    <optgroup label="Analýza">
      <option value="height">Výškové barvy</option>
      <option value="slope">Sklon terénu</option>
    </optgroup>
  </select>
  <label><input type="checkbox" id="toggle-cadastre"> Katastrální mapa (overlay)</label>
  <label><b>Linie katastru:</b></label>
  <select id="cadastre-style" style="width:100%;padding:4px;margin:4px 0;border-radius:4px;border:1px solid #ccc;">
    <option value="thin">Tenké (1px)</option>
    <option value="normal" selected>Normální (2px)</option>
    <option value="medium">Střední + AA (3px)</option>
    <option value="thick">Silné + AA (4px)</option>
  </select>
  </select>
  <hr>
  <p><a href="index.html">← Zpět</a></p>
</div>
<div id="click-info">
  <h3>Detail</h3>
  <div class="row"><span class="label">Výška:</span> <b id="ci-height">—</b></div>
  <div class="row"><span class="label">WGS84:</span> <span id="ci-wgs">—</span></div>
  <div class="row" style="margin-top:6px"><a id="ci-mapy" href="#" target="_blank">Mapy.cz 3D</a></div>
</div>
<!-- viewer-realtor-overlay.js depends on the #popup-kod span and the
     #building-popup id existing — the overlay's MutationObserver looks
     up containing parcels by querying #popup-kod's textContent. Don't
     rename without updating the overlay. -->
<div id="building-popup">
  <span class="close" id="popup-close">&times;</span>
  <h3 id="popup-title"></h3>
  <div class="row"><span class="label">Č.p.:</span> <span id="popup-cislo"></span></div>
  <div class="row"><span class="label">Plocha:</span> <span id="popup-plocha"></span> m²</div>
  <div class="row"><span class="label">Kód:</span> <span id="popup-kod"></span></div>
  <a id="popup-mapy" href="#" target="_blank">Mapy.cz 3D</a>
  <a id="popup-ruian" href="#" target="_blank">RÚIAN detail</a>
</div>
<script src="https://unpkg.com/proj4@2.9.2/dist/proj4.js"></script>
<script type="importmap">
{{
  "imports": {{
    "three": "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/"
  }}
}}
</script>
<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';

proj4.defs("EPSG:5514", "+proj=krovak +lat_0=49.5 +lon_0=24.83333333333333 +alpha=30.28813975277778 +k=0.9999 +x_0=0 +y_0=0 +ellps=bessel +towgs84=570.8,85.7,462.8,4.998,1.587,5.261,3.56 +units=m +no_defs");

const gcx = {gcx};
const gcy = {gcy};
let tiles = [];
let ruianBuildings = [];
const dataLoaded = fetch('tiles_{args.location}/{args.location}_data.json')
  .then(r => {{ if (!r.ok) throw new Error(`HTTP ${{r.status}}`); return r.json(); }})
  .then(d => {{ tiles = d.tiles; ruianBuildings = d.ruianBuildings; }});

const scene = new THREE.Scene();
// Haze blue — matched to fog so horizon blends seamlessly into background.
scene.background = new THREE.Color(0xb0c4d8);
// Exponential fog: 50% density at ~5.5km, full fade by ~10km. Masks the
// (future) LOD ring boundaries and gives atmospheric perspective.
scene.fog = new THREE.FogExp2(0xb0c4d8, 0.00012);

// near=1.0 (was 0.1) reclaims depth precision; far=15000 (was 5000) makes
// room for the future L3 panorama ring at ~7.2km. Linear depth buffer
// (logarithmicDepthBuffer was tried but broke cadastre + parcel polygonOffset
// overlays — see commit history). Parcels + cadastre live only in L0/L1
// (within 900m of subject) where 24-bit depth precision is still ~0.05m;
// distant L3 terrain accepts ~1m precision since fog masks z-fighting there.
const camera = new THREE.PerspectiveCamera(60, innerWidth / innerHeight, 1.0, 15000);
camera.position.set(200, 150, 200);

const renderer = new THREE.WebGLRenderer({{ antialias: true }});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
renderer.shadowMap.enabled = true;
document.body.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.15;
controls.mouseButtons = {{ LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE }};
controls.screenSpacePanning = true;
controls.minDistance = 1;
controls.maxDistance = 2000;
controls.maxPolarAngle = Math.PI / 2.05;

scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const dir = new THREE.DirectionalLight(0xffffff, 0.5);
dir.position.set(100, 200, 100);
dir.castShadow = true;
scene.add(dir);

const allMeshes = [];
let origPositions = [];
const texLoader = new THREE.TextureLoader();
const gltfLoader = new GLTFLoader();
const useGLB = {'true' if use_glb else 'false'};

dataLoaded.then(() => {{
for (const tile of tiles) {{
  if (useGLB && tile.glb_url) {{
    const setupMesh = (geometry) => {{
      const mat = new THREE.MeshBasicMaterial({{ vertexColors: false, side: THREE.DoubleSide }});
      const mesh = new THREE.Mesh(geometry, mat);
      mesh.userData = {{ tile, mat, geo: geometry, ortofotoTexture: null, ortofotoLoaded: false }};
      scene.add(mesh);
      allMeshes.push(mesh);

      // Load ortofoto
      const bbox = tile.sjtsk_bbox;
      const wbbox = tile.wgs_bbox;
      const ortSize = document.getElementById('ortho-quality').value;
      const ortUrl = `/proxy/ortofoto?BBOX=${{bbox[0]}},${{bbox[1]}},${{bbox[2]}},${{bbox[3]}}&WBBOX=${{wbbox[0]}},${{wbbox[1]}},${{wbbox[2]}},${{wbbox[3]}}&size=${{ortSize}}`;
      texLoader.load(ortUrl, (texture) => {{
        texture.colorSpace = THREE.SRGBColorSpace;
        mesh.material.map = texture;
        mesh.material.needsUpdate = true;
        mesh.userData.ortofotoLoaded = true;
        mesh.userData.ortofotoTexture = texture;
      }});
    }};

    gltfLoader.load(tile.glb_url, (gltf) => {{
      let m = gltf.scene.children[0];
      if (m && !m.isMesh && m.children[0]) m = m.children[0];
      if (m) setupMesh(m.geometry);
    }});
    continue;
  }}

  const geo = new THREE.BufferGeometry();
  const nFaceVerts = tile.faces.length * 3;
  const positions = new Float32Array(nFaceVerts * 3);
  const texUvs = new Float32Array(nFaceVerts * 2);

  let idx = 0, uvIdx = 0;
  for (const face of tile.faces) {{
    for (const vi of face) {{
      const v = tile.vertices[vi];
      positions[idx++] = v[0] + tile.offset_x;
      positions[idx++] = v[1];
      positions[idx++] = v[2] + tile.offset_z;
      const uv = tile.uvs[vi];
      texUvs[uvIdx++] = uv[0];
      texUvs[uvIdx++] = uv[1];
    }}
  }}

  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('uv', new THREE.BufferAttribute(texUvs, 2));
  geo.computeVertexNormals();

  // Height colors
  const colors = new Float32Array(nFaceVerts * 3);
  for (let i = 0; i < positions.length; i += 3) {{
    const h = positions[i + 1];
    let r, g, b;
    if (h < 0.3) {{ r=0.3; g=0.65; b=0.2; }}
    else if (h < 2) {{ r=0.5; g=0.7; b=0.3; }}
    else if (h < 4) {{ r=0.92; g=0.88; b=0.78; }}
    else if (h < 8) {{ r=0.85; g=0.35; b=0.25; }}
    else {{ r=0.55; g=0.15; b=0.15; }}
    colors[i]=r; colors[i+1]=g; colors[i+2]=b;
  }}
  geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));

  // MeshBasicMaterial = no lighting (true ortofoto colors)
  // MeshStandardMaterial = with lighting (adds 3D depth via shadows)
  const mat = new THREE.MeshBasicMaterial({{
    vertexColors: true, side: THREE.DoubleSide,
  }});
  const matLit = new THREE.MeshStandardMaterial({{
    roughness: 1.0, side: THREE.DoubleSide,
  }});
  const matDark = new THREE.MeshBasicMaterial({{
    side: THREE.DoubleSide, color: 0xbbbbbb,  // darken texture
  }});

  const mesh = new THREE.Mesh(geo, mat);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.userData = {{ tile, mat, matLit, matDark, geo, ortofotoTexture: null, ortofotoLoaded: false }};
  scene.add(mesh);
  allMeshes.push(mesh);

  // Load ortofoto for this tile
  const bbox = tile.sjtsk_bbox;
  const wbbox = tile.wgs_bbox;
  const ortUrl = `/proxy/ortofoto?BBOX=${{bbox[0]}},${{bbox[1]}},${{bbox[2]}},${{bbox[3]}}&WBBOX=${{wbbox[0]}},${{wbbox[1]}},${{wbbox[2]}},${{wbbox[3]}}`;
  texLoader.load(ortUrl, (texture) => {{
    texture.colorSpace = THREE.SRGBColorSpace;
    mesh.userData.ortofotoTexture = texture;
    mesh.userData.ortofotoLoaded = true;
    mat.map = texture;
    mat.vertexColors = false;
    mat.needsUpdate = true;
    matLit.map = texture;
    matLit.needsUpdate = true;
    matDark.map = texture;
    matDark.needsUpdate = true;
    // Also apply to simplified mesh
    if (mesh.userData.simpMesh) {{
      mesh.userData.simpMesh.material.map = texture;
      mesh.userData.simpMesh.material.vertexColors = false;
      mesh.userData.simpMesh.material.needsUpdate = true;
      mesh.userData.simpMesh.userData.ortofotoLoaded = true;
      mesh.userData.simpMesh.userData.ortofotoTexture = texture;
    }}
  }});

  // Build simplified mesh
  if (tile.simp_vertices && tile.simp_faces.length > 0) {{
    const sGeo = new THREE.BufferGeometry();
    const sNFV = tile.simp_faces.length * 3;
    const sPos = new Float32Array(sNFV * 3);
    const sUvs = new Float32Array(sNFV * 2);
    let si = 0, su = 0;
    for (const face of tile.simp_faces) {{
      for (const vi of face) {{
        const v = tile.simp_vertices[vi];
        sPos[si++] = v[0] + tile.offset_x;
        sPos[si++] = v[1];
        sPos[si++] = v[2] + tile.offset_z;
        const uv = tile.simp_uvs[vi];
        sUvs[su++] = uv[0];
        sUvs[su++] = uv[1];
      }}
    }}
    sGeo.setAttribute('position', new THREE.BufferAttribute(sPos, 3));
    sGeo.setAttribute('uv', new THREE.BufferAttribute(sUvs, 2));
    sGeo.computeVertexNormals();

    // Height colors
    const sCol = new Float32Array(sNFV * 3);
    for (let i = 0; i < sPos.length; i += 3) {{
      const h = sPos[i + 1];
      let r, g, b;
      if (h < 0.3) {{ r=0.3; g=0.65; b=0.2; }}
      else if (h < 2) {{ r=0.5; g=0.7; b=0.3; }}
      else if (h < 4) {{ r=0.92; g=0.88; b=0.78; }}
      else if (h < 8) {{ r=0.85; g=0.35; b=0.25; }}
      else {{ r=0.55; g=0.15; b=0.15; }}
      sCol[i]=r; sCol[i+1]=g; sCol[i+2]=b;
    }}
    sGeo.setAttribute('color', new THREE.BufferAttribute(sCol, 3));

    const sMat = new THREE.MeshBasicMaterial({{ vertexColors: true, side: THREE.DoubleSide }});
    const sMesh = new THREE.Mesh(sGeo, sMat);
    sMesh.visible = false;
    sMesh.userData = {{ mat: sMat, geo: sGeo, ortofotoTexture: null, ortofotoLoaded: false }};
    scene.add(sMesh);
    allSimpMeshes.push(sMesh);
    mesh.userData.simpMesh = sMesh;
  }}
}}

// Store original Y for exaggeration
origPositions = allMeshes.map(m => new Float32Array(m.geometry.attributes.position.array));
}});

document.getElementById('exag').addEventListener('input', (e) => {{
  const val = parseFloat(e.target.value);
  document.getElementById('exag-val').textContent = val.toFixed(1);
  allMeshes.forEach((mesh, mi) => {{
    const pos = mesh.geometry.attributes.position.array;
    const orig = origPositions[mi];
    for (let i = 1; i < pos.length; i += 3) pos[i] = orig[i] * val;
    mesh.geometry.attributes.position.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
  }});
}});

document.getElementById('wireframe').addEventListener('change', (e) => {{
  allMeshes.forEach(m => {{ m.userData.mat.wireframe = e.target.checked; }});
}});

// Basemap switching
function setBasemap(mode) {{
  allMeshes.forEach(m => {{
    const mat = m.userData.mat;
    const geo = m.userData.geo;

    // Toggle simplified vs full meshes
    const useSimp = (mode === 'ortofoto_simplified');
    m.visible = !useSimp;
    if (m.userData.simpMesh) m.userData.simpMesh.visible = useSimp;

    if (mode === 'ortofoto_simplified') {{
      if (m.userData.simpMesh && m.userData.simpMesh.userData.ortofotoLoaded) {{
        m.userData.simpMesh.material.map = m.userData.simpMesh.userData.ortofotoTexture;
        m.userData.simpMesh.material.vertexColors = false;
        m.userData.simpMesh.material.needsUpdate = true;
      }}
    }} else if (mode === 'ortofoto_basic' && m.userData.ortofotoLoaded) {{
      // MeshBasicMaterial — no lighting, true colors from aerial photo
      m.material = m.userData.mat;
      m.userData.mat.map = m.userData.ortofotoTexture;
      m.userData.mat.vertexColors = false;
      m.userData.mat.color.set(0xffffff);
      m.userData.mat.needsUpdate = true;
    }} else if (mode === 'ortofoto_lit' && m.userData.ortofotoLoaded) {{
      // MeshStandardMaterial — with lighting for 3D depth
      m.material = m.userData.matLit;
      m.userData.matLit.needsUpdate = true;
    }} else if (mode === 'ortofoto_dark' && m.userData.ortofotoLoaded) {{
      // MeshBasicMaterial — darkened for better contrast
      m.material = m.userData.matDark;
      m.userData.matDark.needsUpdate = true;
    }} else if (mode === 'slope') {{
      // Recolor vertices by slope
      m.material = m.userData.mat;
      m.userData.mat.map = null;
      m.userData.mat.vertexColors = true;
      m.userData.mat.color.set(0xffffff);
      const pos = geo.attributes.position.array;
      const colors = geo.attributes.color.array;
      for (let i = 0; i < pos.length; i += 9) {{
        // Triangle normal → slope
        const ax = pos[i+3]-pos[i], ay = pos[i+4]-pos[i+1], az = pos[i+5]-pos[i+2];
        const bx = pos[i+6]-pos[i], by = pos[i+7]-pos[i+1], bz = pos[i+8]-pos[i+2];
        const nx = ay*bz - az*by, ny = az*bx - ax*bz, nz = ax*by - ay*bx;
        const len = Math.sqrt(nx*nx + ny*ny + nz*nz) || 1;
        const slope = Math.acos(Math.abs(ny / len)) * 180 / Math.PI;

        // Color: green=flat, yellow=moderate, red=steep
        for (let j = 0; j < 3; j++) {{
          const ci = i + j * 3;
          if (slope < 10) {{ colors[ci]=0.2; colors[ci+1]=0.7; colors[ci+2]=0.2; }}
          else if (slope < 25) {{ colors[ci]=0.9; colors[ci+1]=0.9; colors[ci+2]=0.2; }}
          else if (slope < 45) {{ colors[ci]=0.9; colors[ci+1]=0.5; colors[ci+2]=0.1; }}
          else {{ colors[ci]=0.9; colors[ci+1]=0.2; colors[ci+2]=0.1; }}
        }}
      }}
      geo.attributes.color.needsUpdate = true;
    }} else {{
      // Height colors (default)
      m.material = m.userData.mat;
      m.userData.mat.map = null;
      m.userData.mat.vertexColors = true;
      m.userData.mat.color.set(0xffffff);
      const pos = geo.attributes.position.array;
      const colors = geo.attributes.color.array;
      const exag = parseFloat(document.getElementById('exag').value);
      for (let i = 0; i < pos.length; i += 3) {{
        const h = pos[i + 1] / exag;
        let r, g, b;
        if (h < 0.3) {{ r=0.3; g=0.65; b=0.2; }}
        else if (h < 2) {{ r=0.5; g=0.7; b=0.3; }}
        else if (h < 4) {{ r=0.92; g=0.88; b=0.78; }}
        else if (h < 8) {{ r=0.85; g=0.35; b=0.25; }}
        else {{ r=0.55; g=0.15; b=0.15; }}
        colors[i]=r; colors[i+1]=g; colors[i+2]=b;
      }}
      geo.attributes.color.needsUpdate = true;
    }}
    mat.needsUpdate = true;
  }});
}}

// Ortofoto quality reload with loading indicator
document.getElementById('ortho-quality').addEventListener('change', (e) => {{
  const size = e.target.value;
  const currentBasemap = document.getElementById('basemap-select').value;
  if (!currentBasemap.startsWith('ortofoto_')) return;
  const source = currentBasemap.replace('ortofoto_', '');

  let loaded = 0;
  const total = allMeshes.length;

  // Dim all tiles to show loading
  allMeshes.forEach(m => {{
    m.userData.mat.color.set(0x888888);
    m.userData.mat.needsUpdate = true;
  }});

  allMeshes.forEach(m => {{
    const tile = m.userData.tile;
    if (!tile) {{ loaded++; return; }}
    const bbox = tile.sjtsk_bbox;
    const wbbox = tile.wgs_bbox;
    const ortUrl = `/proxy/ortofoto?BBOX=${{bbox[0]}},${{bbox[1]}},${{bbox[2]}},${{bbox[3]}}&WBBOX=${{wbbox[0]}},${{wbbox[1]}},${{wbbox[2]}},${{wbbox[3]}}&source=${{source}}&size=${{size}}`;
    texLoader.load(ortUrl, (texture) => {{
      texture.colorSpace = THREE.SRGBColorSpace;
      m.userData.mat.map = texture;
      m.userData.mat.color.set(0xffffff);
      m.userData.mat.vertexColors = false;
      m.userData.mat.needsUpdate = true;
      m.userData.ortofotoTexture = texture;
      loaded++;
    }});
  }});
}});

document.getElementById('basemap-select').addEventListener('change', (e) => {{
  const mode = e.target.value;
  if (mode.startsWith('ortofoto_')) {{
    const source = mode.replace('ortofoto_', '');
    const size = document.getElementById('ortho-quality').value;

    // Dim tiles to indicate loading
    allMeshes.forEach(m => {{
      m.userData.mat.color.set(0x888888);
      m.userData.mat.needsUpdate = true;
    }});

    allMeshes.forEach(m => {{
      const tile = m.userData.tile;
      if (!tile) return;
      const bbox = tile.sjtsk_bbox;
      const wbbox = tile.wgs_bbox;
      const ortUrl = `/proxy/ortofoto?BBOX=${{bbox[0]}},${{bbox[1]}},${{bbox[2]}},${{bbox[3]}}&WBBOX=${{wbbox[0]}},${{wbbox[1]}},${{wbbox[2]}},${{wbbox[3]}}&source=${{source}}&size=${{size}}`;

      texLoader.load(ortUrl, (texture) => {{
        texture.colorSpace = THREE.SRGBColorSpace;
        m.userData.mat.map = texture;
        m.userData.mat.color.set(0xffffff);
        m.userData.mat.vertexColors = false;
        m.userData.mat.needsUpdate = true;
        m.userData.ortofotoTexture = texture;
        m.userData.ortofotoLoaded = true;
      }});
    }});
  }} else {{
    setBasemap(mode);
  }}
}});

// Cadastre overlay
let cadastreMeshes = [];

function reloadCadastre() {{
  // Remove old
  cadastreMeshes.forEach(m => scene.remove(m));
  cadastreMeshes = [];

  const checked = document.getElementById('toggle-cadastre').checked;
  console.log('[cadastre] reloadCadastre called, checked=', checked, 'meshes available=', allMeshes.length);
  if (!checked) return;

  const cadStyle = document.getElementById('cadastre-style').value;
  allMeshes.forEach(m => {{
    const tile = m.userData.tile;
    if (!tile) return;
    // Use ACTUAL vertex extent for cadastre bbox + UV (sampling fix shifted
    // vertices by 0.25m from nominal tile bounds, so sjtsk_bbox is off).
    const geo = m.geometry;
    const pos = geo.attributes.position.array;
    const origIdx = geo.index.array;

    const ox = tile.offset_x;
    const oz = tile.offset_z;

    // Local x/z extent (min/max across all vertices)
    let lxMin = Infinity, lxMax = -Infinity, lzMin = Infinity, lzMax = -Infinity;
    for (let i = 0; i < pos.length; i += 3) {{
      const lx = pos[i] - ox;
      const lz = pos[i+2] - oz;
      if (lx < lxMin) lxMin = lx; if (lx > lxMax) lxMax = lx;
      if (lz < lzMin) lzMin = lz; if (lz > lzMax) lzMax = lz;
    }}
    // World bbox matching the actual mesh extent
    const wxMin = tile.center_sjtsk[0] + lxMin;
    const wxMax = tile.center_sjtsk[0] + lxMax;
    const wyMin = tile.center_sjtsk[1] - lzMax;  // world Y = cy - local z
    const wyMax = tile.center_sjtsk[1] - lzMin;
    const cadastreUrl = `/proxy/cadastre?BBOX=${{wxMin}},${{wyMin}},${{wxMax}},${{wyMax}}&style=${{cadStyle}}`;
    console.log('[cadastre] tile', tile.grid_col, tile.grid_row, 'URL:', cadastreUrl);
    texLoader.load(cadastreUrl, (texture) => {{
      texture.colorSpace = THREE.SRGBColorSpace;
      console.log('[cadastre] texture loaded for tile', tile.grid_col, tile.grid_row);

      // Filter faces: keep only where |normal.y| > 0.5 (not a wall)
      const keepFaces = [];
      for (let f = 0; f < origIdx.length; f += 3) {{
        const a = origIdx[f], b = origIdx[f+1], c = origIdx[f+2];
        const ax = pos[a*3], ay = pos[a*3+1], az = pos[a*3+2];
        const bx = pos[b*3], by = pos[b*3+1], bz = pos[b*3+2];
        const cx = pos[c*3], cy = pos[c*3+1], cz = pos[c*3+2];
        // Face normal Y component
        const e1x = bx-ax, e1y = by-ay, e1z = bz-az;
        const e2x = cx-ax, e2y = cy-ay, e2z = cz-az;
        const nx = e1y*e2z - e1z*e2y;
        const ny = e1z*e2x - e1x*e2z;
        const nz = e1x*e2y - e1y*e2x;
        const nlen = Math.sqrt(nx*nx + ny*ny + nz*nz);
        if (nlen < 1e-6) continue;
        const nyNorm = Math.abs(ny) / nlen;
        if (nyNorm > 0.5) {{
          keepFaces.push(a, b, c);
        }}
      }}

      const cadUvs = new Float32Array(pos.length / 3 * 2);
      const lxRange = lxMax - lxMin, lzRange = lzMax - lzMin;
      for (let i = 0; i < pos.length; i += 3) {{
        const lx = pos[i] - ox;
        const lz = pos[i+2] - oz;
        cadUvs[(i/3)*2] = (lx - lxMin) / lxRange;
        cadUvs[(i/3)*2+1] = 1.0 - (lz - lzMin) / lzRange;
      }}
      const cadGeo = new THREE.BufferGeometry();
      cadGeo.setAttribute('position', geo.attributes.position.clone());
      cadGeo.setAttribute('uv', new THREE.BufferAttribute(cadUvs, 2));
      cadGeo.setIndex(new THREE.BufferAttribute(new Uint32Array(keepFaces), 1));

      const cadMat = new THREE.MeshBasicMaterial({{
        map: texture, transparent: true, side: THREE.DoubleSide,
        depthWrite: false, alphaTest: 0.3,
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -4,
      }});
      const cadMesh = new THREE.Mesh(cadGeo, cadMat);
      cadMesh.renderOrder = 100;
      scene.add(cadMesh);
      cadastreMeshes.push(cadMesh);
      console.log('[cadastre] mesh added for tile', tile.grid_col, tile.grid_row, 'faces:', keepFaces.length/3);
    }}, undefined, (err) => {{
      console.error('[cadastre] texLoader FAILED for tile', tile.grid_col, tile.grid_row, err);
    }});
  }});
}}

document.getElementById('cadastre-style').addEventListener('change', () => {{
  if (document.getElementById('toggle-cadastre').checked) reloadCadastre();
}});

document.getElementById('toggle-cadastre').addEventListener('change', () => reloadCadastre());

// Building highlight — outline only, simplified + Y-smoothed
const highlightObjects = [];
const heightRaycaster = new THREE.Raycaster();
const _downDir = new THREE.Vector3(0, -1, 0);
const _fromAbove = new THREE.Vector3();

function getTerrainHeightAt(x, z, fallback = 0) {{
  _fromAbove.set(x, 10000, z);
  heightRaycaster.set(_fromAbove, _downDir);
  const hits = heightRaycaster.intersectObjects(allMeshes, false);
  return hits.length ? hits[0].point.y : fallback;
}}

function clearHighlight() {{
  for (const o of highlightObjects) {{
    scene.remove(o);
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  }}
  highlightObjects.length = 0;
}}

function dpOpen(points, epsilon) {{
  if (points.length < 3) return points.slice();
  const a = points[0], b = points[points.length - 1];
  const dx = b[0] - a[0], dz = b[1] - a[1];
  const len2 = dx * dx + dz * dz;
  let maxDist = 0, idx = 0;
  for (let i = 1; i < points.length - 1; i++) {{
    const p = points[i];
    let d;
    if (len2 === 0) {{
      d = Math.hypot(p[0] - a[0], p[1] - a[1]);
    }} else {{
      const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / len2));
      d = Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dz));
    }}
    if (d > maxDist) {{ maxDist = d; idx = i; }}
  }}
  if (maxDist > epsilon) {{
    const left = dpOpen(points.slice(0, idx + 1), epsilon);
    const right = dpOpen(points.slice(idx), epsilon);
    return left.slice(0, -1).concat(right);
  }}
  return [a, b];
}}

function simplifyClosed(coords, epsilon) {{
  if (coords.length < 4) return coords.slice();
  let pts = coords.slice();
  const first = pts[0], last = pts[pts.length - 1];
  if (Math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-6) pts = pts.slice(0, -1);
  let far = 0, farDist = -1;
  for (let i = 1; i < pts.length; i++) {{
    const d = Math.hypot(pts[i][0] - pts[0][0], pts[i][1] - pts[0][1]);
    if (d > farDist) {{ farDist = d; far = i; }}
  }}
  const a = dpOpen(pts.slice(0, far + 1), epsilon);
  const b = dpOpen(pts.slice(far).concat([pts[0]]), epsilon);
  return a.slice(0, -1).concat(b.slice(0, -1));
}}

function smoothYClosed(points, window) {{
  const n = points.length;
  const half = Math.floor(window / 2);
  const ys = new Float32Array(n);
  for (let i = 0; i < n; i++) {{
    let sum = 0, cnt = 0;
    for (let k = -half; k <= half; k++) {{
      sum += points[(i + k + n) % n].y;
      cnt++;
    }}
    ys[i] = sum / cnt;
  }}
  for (let i = 0; i < n; i++) points[i].y = ys[i];
}}

function highlightBuilding(building) {{
  clearHighlight();
  if (!building || building.coords.length < 2) return;

  const simplified = simplifyClosed(building.coords, 0.15);

  const segLen = 1.0;
  const pts = [];
  for (let i = 0; i < simplified.length; i++) {{
    const a = simplified[i];
    const b = simplified[(i + 1) % simplified.length];
    const dx = b[0] - a[0], dz = b[1] - a[1];
    const len = Math.hypot(dx, dz);
    const steps = Math.max(1, Math.ceil(len / segLen));
    for (let s = 0; s < steps; s++) {{
      const t = s / steps;
      const x = a[0] + dx * t;
      const z = a[1] + dz * t;
      pts.push(new THREE.Vector3(x, getTerrainHeightAt(x, z), z));
    }}
  }}

  smoothYClosed(pts, 7);
  for (const p of pts) p.y += 0.25;

  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const mat = new THREE.LineBasicMaterial({{
    color: 0xff3300,
    depthTest: false,
    transparent: true,
  }});
  const line = new THREE.LineLoop(geo, mat);
  line.renderOrder = 51;
  scene.add(line);
  highlightObjects.push(line);
}}

const popup = document.getElementById('building-popup');
document.getElementById('popup-close').addEventListener('click', () => {{
  popup.style.display = 'none';
  restoreMeshColors();
}});

function isPointInPolygon(px, pz, coords, buffer) {{
  // Ray casting with optional buffer (expand polygon outward)
  const buf = buffer || 0;
  if (buf > 0) {{
    // Simple check: test against each edge, if distance < buffer → inside
    for (let i = 0, j = coords.length - 1; i < coords.length; j = i++) {{
      const x1 = coords[j][0], z1 = coords[j][1];
      const x2 = coords[i][0], z2 = coords[i][1];
      const dx = x2 - x1, dz = z2 - z1;
      const len = Math.sqrt(dx*dx + dz*dz) || 1;
      // Distance from point to line segment
      const t = Math.max(0, Math.min(1, ((px-x1)*dx + (pz-z1)*dz) / (len*len)));
      const cx = x1 + t*dx, cz = z1 + t*dz;
      const dist = Math.sqrt((px-cx)*(px-cx) + (pz-cz)*(pz-cz));
      if (dist < buf) return true;
    }}
  }}
  // Standard ray casting
  let inside = false;
  for (let i = 0, j = coords.length - 1; i < coords.length; j = i++) {{
    const xi = coords[i][0], zi = coords[i][1];
    const xj = coords[j][0], zj = coords[j][1];
    if (((zi > pz) !== (zj > pz)) && (px < (xj - xi) * (pz - zi) / (zj - zi) + xi)) {{
      inside = !inside;
    }}
  }}
  return inside;
}}


// Click
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();
const marker = new THREE.Mesh(new THREE.SphereGeometry(0.5), new THREE.MeshBasicMaterial({{color: 0xff0000}}));
marker.visible = false;
scene.add(marker);

renderer.domElement.addEventListener('click', (e) => {{
  mouse.x = (e.clientX / innerWidth) * 2 - 1;
  mouse.y = -(e.clientY / innerHeight) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);

  // Deselect previous
  highlightBuilding(null);
  popup.style.display = 'none';

  // Raycast on terrain mesh
  const hits = raycaster.intersectObjects(allMeshes);
  if (hits.length === 0) return;

  const pt = hits[0].point;
  marker.position.copy(pt);
  marker.visible = true;

  // Check if click point is inside any building footprint (with 1.5m buffer for walls)
  let clickedBuilding = null;
  for (const b of ruianBuildings) {{
    if (isPointInPolygon(pt.x, pt.z, b.coords, 1.5)) {{
      clickedBuilding = b;
      break;
    }}
  }}

  if (clickedBuilding) {{
    const b = clickedBuilding;

    // Highlight building on 3D surface
    highlightBuilding(b);

    // Popup
    popup.style.display = 'block';
    popup.style.left = Math.min(e.clientX, innerWidth - 250) + 'px';
    popup.style.top = Math.min(e.clientY, innerHeight - 200) + 'px';
    document.getElementById('popup-title').textContent = b.cislo ? ('{location_label} č.p. ' + b.cislo) : ('SO-' + b.kod);
    document.getElementById('popup-cislo').textContent = b.cislo || '—';
    document.getElementById('popup-plocha').textContent = b.plocha;
    document.getElementById('popup-kod').textContent = b.kod;
    document.getElementById('popup-mapy').href = 'https://mapy.com/en/letecka?x=' + b.wgs84[0].toFixed(7) + '&y=' + b.wgs84[1].toFixed(7) + '&z=20&m3d=1&height=92&yaw=0&pitch=-45';
    document.getElementById('popup-ruian').href = 'https://vdp.cuzk.cz/vdp/ruian/stavebniobjekty/' + b.kod;
  }}
}});

renderer.domElement.addEventListener('dblclick', (e) => {{
  mouse.x = (e.clientX / innerWidth) * 2 - 1;
  mouse.y = -(e.clientY / innerHeight) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(allMeshes);
  if (hits.length > 0) controls.target.copy(hits[0].point);
}});

window.addEventListener('resize', () => {{
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
}});

// Tick loop (single source of truth)
function interactiveTick() {{
  controls.update();
  renderer.render(scene, camera);
}}
let _mainTick = interactiveTick;
const _tickHooks = [];
function tick() {{
  _mainTick();
  for (const hook of _tickHooks) hook();
  requestAnimationFrame(tick);
}}
tick();

// Tick API exposed to the realtor overlay.
const addTickHook    = fn => {{ _tickHooks.push(fn); }};
const removeTickHook = fn => {{ const i = _tickHooks.indexOf(fn); if (i >= 0) _tickHooks.splice(i, 1); }};
const setMainTick    = fn => {{ _mainTick = fn; }};
const resetMainTick  = ()  => {{ _mainTick = interactiveTick; }};

// ─── Realtor overlay try-import ─────────────────────────────────────
// Optional: loads viewer-realtor-overlay.js if present, otherwise the
// base viewer continues as a plain 3D village viewer.
// Deferred to dataLoaded.then so ruianBuildings / allMeshes are populated
// before init destructures them by value.
dataLoaded.then(async () => {{
  try {{
    const overlay = await import('./viewer-realtor-overlay.js');
    overlay.init({{
      THREE, scene, camera, renderer, controls,
      allMeshes, ruianBuildings, gcx, gcy,
      addTickHook, removeTickHook, setMainTick, resetMainTick,
      getBuildingPopup: () => document.getElementById('building-popup'),
      getTerrainHeightAt,
    }});
  }} catch (e) {{
    console.warn('[viewer] realtor overlay not loaded:', e);
  }}
}});
</script>
</body>
</html>"""

    # Write sibling JSON data file (gitignored, lives next to GLB tiles)
    data_path = Path(f"tiles_{args.location}") / f"{args.location}_data.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps({
        "tiles": json.loads(tiles_json),
        "ruianBuildings": ruian_buildings,
    }), encoding="utf-8")
    print(f"Data JSON: {data_path} ({data_path.stat().st_size // 1024} KB)")

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"Output: {output_path} ({Path(output_path).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
