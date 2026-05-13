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


def _load_panorama_grid(manifest_dir, region):
    """Read the region's panorama GLB and return the regular vertex grid
    (n x n x 3) for bilinear Y sampling. Skirt verts are stripped."""
    path = Path(manifest_dir) / region["panorama_glb"]
    with open(path, "rb") as f:
        data = f.read()
    json_len = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + json_len].decode())
    bin_data = data[20 + json_len + 8:]
    prim = gltf["meshes"][0]["primitives"][0]
    acc = gltf["accessors"][prim["attributes"]["POSITION"]]
    bv = gltf["bufferViews"][acc["bufferView"]]
    off = bv.get("byteOffset", 0)
    count = acc["count"]
    floats = struct.unpack_from(f"<{count*3}f", bin_data, off)
    half = region["half"]
    step = region["step"]
    n = int(round(2 * half / step)) + 1
    flat = np.array(floats, dtype=np.float64).reshape(-1, 3)
    return flat[: n * n].reshape(n, n, 3), half, step


def _panorama_y_at(grid, half, step, region_cx, region_cy, world_x, world_y):
    """Bilinear panorama Y at a given world position (assumes within bbox)."""
    plx = world_x - region_cx
    plz = world_y - region_cy
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
    return (1 - tx) * (1 - tz) * y00 + tx * (1 - tz) * y01 + (1 - tx) * tz * y10 + tx * tz * y11


def build_detail(cx, cy, half, step, fade_width, ground_z, manifest_dir, region):
    """Build the detail mesh. Y = SM5 in the inner core, linear blend with
    the panorama mesh's Y across the fade band, exact panorama Y match at
    the outer edge.

    The fade target is the panorama mesh's bilinear-sampled Y at the same
    world (x, y) — this makes the seam exact by construction, regardless of
    DMR5G fetch-resolution mismatch.

    ground_z is the panorama's ground_z (from manifest.json) — SM5 Y values
    are normalized against the SAME ground reference; panorama Y is already
    normalized at build time.
    """
    sm5_paths = discover_sm5(cx, cy, half)
    if not sm5_paths:
        raise SystemExit(
            f"No SM5 cache for ({cx},{cy}); the SM5 cache in cache/dmpok_tiff_*/ "
            f"does not cover this address bbox. Add SM5 tiles or pick a different centre."
        )
    sm5_data, sm5_valid = load_sm5_patch(sm5_paths, cx, cy, half, step)
    pano_grid, pano_half, pano_step = _load_panorama_grid(manifest_dir, region)
    region_cx, region_cy = region["center_sjtsk"]

    n = int(round(2 * half / step)) + 1
    sm5_h, sm5_w = sm5_data.shape

    inner_radius = half - fade_width

    vertices = []
    world_coords = []
    for r in range(n):
        for c in range(n):
            local_x = -half + c * step
            local_z = -half + r * step
            world_x = cx + local_x
            world_y = cy + local_z

            # Sample SM5 (clamped rasterio rows). data row 0 = top of bbox = max world_y.
            dr_sm = min((n - 1) - r, sm5_h - 1)
            dc_sm = min(c, sm5_w - 1)
            sm5_y = (float(sm5_data[dr_sm, dc_sm]) - ground_z
                     if sm5_valid[dr_sm, dc_sm] else None)

            # Fade target = panorama mesh's actual Y at this world pos.
            # By construction the outer edge of the detail matches the
            # panorama exactly (no fetch-resolution mismatch).
            pano_y = float(_panorama_y_at(pano_grid, pano_half, pano_step,
                                          region_cx, region_cy, world_x, world_y))

            d = max(abs(local_x), abs(local_z))  # L-infinity distance from centre
            if d <= inner_radius:
                y = sm5_y if sm5_y is not None else pano_y
            elif d >= half:
                y = pano_y
            else:
                t = (d - inner_radius) / fade_width
                if sm5_y is None:
                    y = pano_y
                else:
                    y = (1.0 - t) * sm5_y + t * pano_y

            vertices.append([local_x, y, local_z])
            world_coords.append((world_x, world_y))

    # Faces — 2 triangles per quad, no grid_valid mask
    faces = []
    for r in range(n - 1):
        for c in range(n - 1):
            i = r * n + c
            faces.append([i, i + n, i + 1])
            faces.append([i + 1, i + n, i + n + 1])

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
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True)
    p.add_argument("--slug", required=True,
                   help="address slug (filename-safe identifier)")
    p.add_argument("--center-sjtsk", required=True,
                   help="cx,cy (use --center-sjtsk=cx,cy for negative ČÚZK coords)")
    p.add_argument("--half", type=int, default=300,
                   help="half side length in metres; 600m square default")
    p.add_argument("--step", type=int, default=1)
    p.add_argument("--fade", type=int, default=50,
                   help="outer fade band width in metres")
    p.add_argument("--skirt", type=int, default=50,
                   help="skirt drop in metres")
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

    print(f"Generating detail '{args.slug}' at ({cx}, {cy}) — {args.half*2}m square, "
          f"step {args.step}m, fade {args.fade}m …")
    tile = build_detail(cx, cy, args.half, args.step, args.fade,
                        region_ground_z, out_dir, manifest["region"])
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
