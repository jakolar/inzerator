#!/usr/bin/env python3
"""MVP CR-wide heightmap pyramid tile builder.

Pipeline:
  Web Mercator tile (z, x, y)
    ─→ tile bbox in EPSG:3857
    ─→ corners reprojected to S-JTSK (EPSG:5514) for the SM5 sheet lookup
    ─→ mosaic of overlapping DMPOK TIFFs from bulk cache
    ─→ reproject + resample to 256×256 Web Mercator grid
    ─→ LERC encode → write to <out>/dmpok/<z>/<x>/<y>.lerc

Intentionally single-tile, single-thread, single-format. Scale-up
(multi-worker bulk run + LOD aggregation + state machine) lives in a
separate follow-up script once the per-tile output is validated.

Usage:
  python3 build_pyramid_tile.py --z 14 --x 8975 --y 5635
  python3 build_pyramid_tile.py --z 14 --x 8975 --y 5635 --max-z-error 0.05
  python3 build_pyramid_tile.py --inventory     # one-shot scan of bulk dir

The first run writes a lookup index (inventory.json) under BULK_OUT_DIR
so subsequent tile builds skip the per-TIFF rasterio.open scan that
costs ~16 s across the 16 299-sheet bulk cache.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling
from pyproj import Transformer

try:
    import lerc
    HAS_LERC = True
except ImportError:
    HAS_LERC = False


# Where bulk DMPOK lives. Override with --bulk-dir.
BULK_OUT_DIR = Path("/Volumes/Elements/cuzk-bulk")
# Where pyramid output goes. Override with --out.
OUT_DIR = Path("/Volumes/Elements/cuzk-pyramid")
TILE_SIZE = 256                # px per side
SJTSK_EPSG = "EPSG:5514"       # S-JTSK Krovák East-North
WMERC_EPSG = "EPSG:3857"       # Web Mercator (Spherical)
WMERC_EXTENT_M = 20037508.342789244
NODATA_FLOAT = -9999.0


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (left, bottom, right, top) of a Web Mercator tile in metres."""
    n = 2 ** z
    tile_size_m = (2 * WMERC_EXTENT_M) / n
    left = -WMERC_EXTENT_M + x * tile_size_m
    right = left + tile_size_m
    top = WMERC_EXTENT_M - y * tile_size_m
    bottom = top - tile_size_m
    return left, bottom, right, top


def latlon_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """Convert geographic coords to the Web Mercator tile (x, y) at zoom z.
    Useful for the operator: "give me the tile that covers Stříbrnice"."""
    n = 2 ** z
    lat_rad = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def load_or_build_inventory(bulk_dir: Path) -> dict:
    """Return {MAPNOM: {path, left, bottom, right, top}} for every SM5 TIFF
    in the bulk cache. Cached as inventory.json next to the data so the
    16 299-sheet scan only happens once.

    The cache is invalidated by hand (rm inventory.json) — ČÚZK
    republishing a single sheet is rare enough that a stale entry's
    cost (one wrong tile per affected sheet) is far cheaper than
    rescanning on every tile build.
    """
    cache_path = bulk_dir / "inventory.json"
    if cache_path.is_file():
        return json.loads(cache_path.read_text())

    print(f"[inventory] scanning {bulk_dir}/dmpok_tiff_* …")
    t0 = time.time()
    inv: dict = {}
    for d in sorted(bulk_dir.glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            mapnom = tif.stem
            try:
                with rasterio.open(tif) as src:
                    b = src.bounds
                    inv[mapnom] = {
                        "path": str(tif),
                        "left": b.left, "bottom": b.bottom,
                        "right": b.right, "top": b.top,
                    }
            except (rasterio.errors.RasterioIOError, OSError) as e:
                print(f"[inventory] WARN: skipping unreadable {tif}: {e}")
    dt = time.time() - t0
    print(f"[inventory] indexed {len(inv)} sheets in {dt:.1f} s → {cache_path}")
    cache_path.write_text(json.dumps(inv, indent=0))
    return inv


def discover_sm5_from_inventory(
    inv: dict, sjtsk_bbox: tuple[float, float, float, float],
) -> list[str]:
    """Return SM5 paths whose bbox intersects the given S-JTSK bbox.
    O(N) over the inventory — at 16 299 sheets that's microseconds vs
    rasterio.open per sheet which is 1 ms each.
    """
    left, bottom, right, top = sjtsk_bbox
    hits = []
    for mapnom, meta in inv.items():
        if (meta["left"] < right and meta["right"] > left
                and meta["bottom"] < top and meta["top"] > bottom):
            hits.append(meta["path"])
    return hits


def build_tile(
    z: int, x: int, y: int,
    bulk_dir: Path, out_dir: Path,
    max_z_error: float = 0.10,
    overwrite: bool = False,
) -> bool:
    """Build one Web Mercator heightmap tile. Returns True on success, False
    if the tile is outside any DMPOK coverage (= nothing to write)."""
    out_path = out_dir / "dmpok" / str(z) / str(x) / f"{y}.lerc"
    if out_path.exists() and not overwrite:
        print(f"[tile] skip existing {out_path}")
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Tile bounds in Web Mercator metres.
    left, bottom, right, top = tile_bounds_3857(z, x, y)
    print(f"[tile] z={z} x={x} y={y}")
    print(f"[tile] bbox 3857: left={left:.0f} bottom={bottom:.0f} "
          f"right={right:.0f} top={top:.0f}")

    # Compute an S-JTSK bbox that covers the same area. Web Mercator
    # rectangles are NOT rectangles in S-JTSK Krovák, so we expand the
    # tile's 4 corners + a 5 % perimeter margin into the S-JTSK bbox.
    # The margin guards against the small bow that S-JTSK introduces in
    # latitude-aligned rectangles — losing a row at the edge would be
    # ugly enough to warrant the cheap overscan.
    to_sjtsk = Transformer.from_crs(WMERC_EPSG, SJTSK_EPSG, always_xy=True)
    corners = [
        to_sjtsk.transform(left, bottom),
        to_sjtsk.transform(left, top),
        to_sjtsk.transform(right, bottom),
        to_sjtsk.transform(right, top),
        # Edge midpoints catch the bow.
        to_sjtsk.transform((left + right) / 2, bottom),
        to_sjtsk.transform((left + right) / 2, top),
        to_sjtsk.transform(left, (bottom + top) / 2),
        to_sjtsk.transform(right, (bottom + top) / 2),
    ]
    sxs = [p[0] for p in corners]
    sys_ = [p[1] for p in corners]
    sjtsk_left = min(sxs) - 100
    sjtsk_right = max(sxs) + 100
    sjtsk_bottom = min(sys_) - 100
    sjtsk_top = max(sys_) + 100
    print(f"[tile] bbox S-JTSK (expanded ±100 m): "
          f"left={sjtsk_left:.0f} bottom={sjtsk_bottom:.0f} "
          f"right={sjtsk_right:.0f} top={sjtsk_top:.0f}")

    # Inventory lookup.
    inv = load_or_build_inventory(bulk_dir)
    sm5_paths = discover_sm5_from_inventory(
        inv, (sjtsk_left, sjtsk_bottom, sjtsk_right, sjtsk_top))
    if not sm5_paths:
        print(f"[tile] no DMPOK coverage — outside CR or unstitched region")
        return False
    print(f"[tile] {len(sm5_paths)} SM5 sheets overlap")

    # Mosaic in S-JTSK. Use a source-native resolution so we don't lose
    # detail before reprojection; rasterio.merge picks max of source
    # resolutions when not explicitly set. DMPOK is 0.5 m natively.
    srcs = [rasterio.open(p) for p in sm5_paths]
    try:
        merged, src_transform = merge(
            srcs,
            bounds=(sjtsk_left, sjtsk_bottom, sjtsk_right, sjtsk_top),
            res=(0.5, 0.5),
            nodata=NODATA_FLOAT,
        )
    finally:
        for s in srcs:
            s.close()
    src_array = merged[0]
    print(f"[tile] mosaic shape={src_array.shape} dtype={src_array.dtype}")

    # Reproject to Web Mercator at TILE_SIZE × TILE_SIZE.
    dst_transform = from_bounds(left, bottom, right, top, TILE_SIZE, TILE_SIZE)
    dst_array = np.full((TILE_SIZE, TILE_SIZE), NODATA_FLOAT, dtype=np.float32)
    reproject(
        source=src_array,
        destination=dst_array,
        src_transform=src_transform,
        src_crs=SJTSK_EPSG,
        src_nodata=NODATA_FLOAT,
        dst_transform=dst_transform,
        dst_crs=WMERC_EPSG,
        dst_nodata=NODATA_FLOAT,
        resampling=Resampling.bilinear,
    )

    # Report coverage + range, useful for verifying the seam-test tile.
    valid = dst_array > NODATA_FLOAT + 1
    coverage = valid.mean() * 100
    if not valid.any():
        print(f"[tile] WARN: reproject produced 0 % coverage")
        return False
    y_min = float(dst_array[valid].min())
    y_max = float(dst_array[valid].max())
    print(f"[tile] coverage {coverage:.1f} %, y range {y_min:.1f}–{y_max:.1f} m")

    # LERC encode.
    if not HAS_LERC:
        raise SystemExit("lerc package not installed. pip3 install --user lerc")
    arr = np.ascontiguousarray(dst_array.astype(np.float32))
    hint = arr.nbytes + 1024
    code, nbytes, buf = lerc.encode(arr, 1, False, None, max_z_error, hint)
    if code != 0:
        raise SystemExit(f"LERC encode failed (code {code})")
    # Atomic write: dump to .tmp, then rename. Without this, a SIGTERM
    # in the middle of write leaves a partial .lerc on disk and the
    # resume-skip loop in bulk_pyramid.py would treat it as done.
    tmp_path = out_path.with_suffix(".lerc.tmp")
    tmp_path.write_bytes(bytes(buf[:nbytes]))
    tmp_path.replace(out_path)
    print(f"[tile] wrote {out_path} ({nbytes} bytes, max_z_error={max_z_error})")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--z", type=int, help="Web Mercator zoom level")
    p.add_argument("--x", type=int, help="Tile X (column)")
    p.add_argument("--y", type=int, help="Tile Y (row)")
    p.add_argument("--lat", type=float, default=None,
                   help="Pick the tile that covers this latitude (with --lon)")
    p.add_argument("--lon", type=float, default=None,
                   help="See --lat")
    p.add_argument("--bulk-dir", default=str(BULK_OUT_DIR),
                   help="Bulk DMPOK cache root (default: %(default)s)")
    p.add_argument("--out", default=str(OUT_DIR),
                   help="Pyramid output root (default: %(default)s)")
    p.add_argument("--max-z-error", type=float, default=0.10,
                   help="LERC lossy tolerance in metres (default: %(default).2f)")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-encode tile even if .lerc already exists")
    p.add_argument("--inventory", action="store_true",
                   help="Just (re)build the SM5 inventory cache and exit")
    args = p.parse_args()

    bulk_dir = Path(args.bulk_dir)
    if not bulk_dir.is_dir():
        raise SystemExit(f"--bulk-dir {bulk_dir} not found")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.inventory:
        # Force a re-scan by removing the cache first.
        cache = bulk_dir / "inventory.json"
        if cache.exists():
            cache.unlink()
        load_or_build_inventory(bulk_dir)
        return

    if args.lat is not None and args.lon is not None:
        if args.z is None:
            raise SystemExit("--lat/--lon needs --z")
        tx, ty = latlon_to_tile(args.lat, args.lon, args.z)
        print(f"[main] lat={args.lat} lon={args.lon} z={args.z} "
              f"→ tile ({tx}, {ty})")
        args.x, args.y = tx, ty
    if args.z is None or args.x is None or args.y is None:
        raise SystemExit("Need --z, --x, --y (or --lat, --lon, --z)")

    ok = build_tile(
        args.z, args.x, args.y,
        bulk_dir, out_dir,
        max_z_error=args.max_z_error,
        overwrite=args.overwrite,
    )
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
