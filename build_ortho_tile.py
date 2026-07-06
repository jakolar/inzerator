#!/usr/bin/env python3
"""Ortho pyramid tile builder — JPEG tiles cut from bulk SM5 ortofoto.

Single-tile counterpart of build_pyramid_tile.py for the colour layer.
Output is a plain 256^2 JPEG per XYZ tile (`<out>/ortho/{z}/{x}/{y}.jpg`).
GPU-friendly KTX2 (spec D2) is an opt-in extra (`--ktx2`) so ETC1S
quality can be validated on samples before a multi-day bulk encode.

Source sheets: `ortofoto_<MAPNOM>/*.jpg` + `.jgw` under the bulk dir
(S-JTSK EPSG:5514, 0.125 m/px, 20000x16000 px = 2500x2000 m). Sheet
bboxes come from the shared DMPOK inventory (ortho shares the SM5
grid); the JGW is the authoritative georef. Sheets are decoded via PIL
draft mode (DCT downscale — a z=16 tile reads each 320 MP sheet at 1/8
resolution) and reprojected straight into the target Web Mercator grid,
one rasterio.warp call per sheet — no intermediate mosaic canvas, so no
double-resampling error.

Usage:
  python3 build_ortho_tile.py --z 16 --x 35913 --y 22333
  python3 build_ortho_tile.py --z 16 --x 35913 --y 22333 --ktx2
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import reproject, Resampling

from build_pyramid_tile import (
    BULK_OUT_DIR, OUT_DIR, tile_bounds_3857, load_or_build_inventory,
)

Image.MAX_IMAGE_PIXELS = None

TILE_PX = 256
JPEG_QUALITY = 87
NATIVE_MPP = 0.125            # bulk ortofoto ground resolution (JGW pixel size)
SJTSK = "EPSG:5514"
WMERC = "EPSG:3857"
# Neutral fill for target pixels no sheet covers (outside ČR). Matches the
# viewer's hypsometric fallback tone so border tiles don't flash black.
FILL_RGB = (107, 112, 95)

_to_sjtsk = Transformer.from_crs(WMERC, SJTSK, always_xy=True)

# Drafted-sheet cache: one z=18 tile shares its source sheet with ~270
# neighbours, and PIL's convert() decodes the WHOLE 80 MP drafted image —
# uncached this capped the 2026-07-06 bulk run at 6 tiles/s. Key
# (jpg_path, k) → (np RGB array, actual per-axis pixel sizes). ~230 MB per
# draft-2 sheet, so keep the cache tiny; the dispatcher iterates in sheet-
# sized blocks to make a small cache effective. Per-key locks stop two
# workers decoding the same sheet twice.
import collections
import threading
_SHEET_CACHE: "collections.OrderedDict" = collections.OrderedDict()
_SHEET_CACHE_MAX = 6
_SHEET_CACHE_GUARD = threading.Lock()
_SHEET_KEY_LOCKS: dict = {}


def _load_sheet(jpg: Path, k: int):
    key = (str(jpg), k)
    with _SHEET_CACHE_GUARD:
        if key in _SHEET_CACHE:
            _SHEET_CACHE.move_to_end(key)
            return _SHEET_CACHE[key]
        lock = _SHEET_KEY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        with _SHEET_CACHE_GUARD:
            if key in _SHEET_CACHE:
                _SHEET_CACHE.move_to_end(key)
                return _SHEET_CACHE[key]
        im = Image.open(jpg)
        ow, oh = im.size
        im.draft("RGB", (ow // k, oh // k))
        dw, dh = im.size
        arr = np.asarray(im.convert("RGB"))
        entry = (arr, ow / dw, oh / dh)
        with _SHEET_CACHE_GUARD:
            _SHEET_CACHE[key] = entry
            while len(_SHEET_CACHE) > _SHEET_CACHE_MAX:
                _SHEET_CACHE.popitem(last=False)
            _SHEET_KEY_LOCKS.pop(key, None)
        return entry


def ortho_out_path(out_dir: Path, z: int, x: int, y: int) -> Path:
    return out_dir / "ortho" / str(z) / str(x) / f"{y}.jpg"


def read_jgw(path: Path) -> tuple[float, float, float]:
    """World file → (pixel_size_m, left_edge, top_edge) in S-JTSK.

    JGW stores the CENTRE of the top-left pixel; edges are +-half a pixel.
    Rotation terms are always 0 for ČÚZK sheets (asserted).
    A handful of ČÚZK world files use Czech decimal commas ("0,125").
    """
    vals = [float(v.replace(",", ".")) for v in path.read_text().split()]
    a, d, b, e, c, f = vals[:6]
    if d != 0.0 or b != 0.0:
        raise ValueError(f"rotated world file not supported: {path}")
    return a, c - a / 2.0, f - e / 2.0     # e is negative → top = f + |e|/2


def pick_draft_k(target_mpp: float, native_mpp: float = NATIVE_MPP) -> int:
    """Largest power-of-two JPEG draft divisor (<=8) that keeps the drafted
    resolution at least as fine as the target."""
    k = 1
    while k * 2 <= 8 and native_mpp * k * 2 <= target_mpp:
        k *= 2
    return k


def sjtsk_envelope(z: int, x: int, y: int, margin_m: float = 12.0):
    """S-JTSK envelope of a mercator tile: corners + edge midpoints
    transformed (Krovak is rotated ~7.7 deg vs WGS/mercator axes), plus a
    margin for the resampling kernel."""
    left, bottom, right, top = tile_bounds_3857(z, x, y)
    xs, ys = [], []
    for mx, my in [(left, bottom), (left, top), (right, bottom), (right, top),
                   ((left + right) / 2, bottom), ((left + right) / 2, top),
                   (left, (bottom + top) / 2), (right, (bottom + top) / 2)]:
        sx, sy = _to_sjtsk.transform(mx, my)
        xs.append(sx); ys.append(sy)
    return (min(xs) - margin_m, min(ys) - margin_m,
            max(xs) + margin_m, max(ys) + margin_m)


def find_sheets(inv: dict, env: tuple, bulk_dir: Path) -> list[tuple[Path, Path]]:
    """(jpg, jgw) pairs for sheets intersecting the S-JTSK envelope."""
    exmin, eymin, exmax, eymax = env
    out = []
    for mapnom, b in inv.items():
        if (b["left"] < exmax and b["right"] > exmin
                and b["bottom"] < eymax and b["top"] > eymin):
            d = bulk_dir / f"ortofoto_{mapnom}"
            if not d.is_dir():
                continue
            for jpg in sorted(d.glob("*.jpg")):
                jgw = jpg.with_suffix(".jgw")
                if jgw.exists():
                    out.append((jpg, jgw))
                    break
    return out


def _ground_mpp(z: int, x: int, y: int) -> float:
    left, bottom, right, top = tile_bounds_3857(z, x, y)
    merc_mpp = (right - left) / TILE_PX
    lat_mid = math.degrees(2 * math.atan(
        math.exp((top + bottom) / 2 / 6378137.0)) - math.pi / 2)
    return merc_mpp * math.cos(math.radians(lat_mid))


def build_ortho_tile(z: int, x: int, y: int,
                     bulk_dir: Path = BULK_OUT_DIR,
                     out_dir: Path = OUT_DIR,
                     size: int = TILE_PX,
                     ktx2: bool = False,
                     overwrite: bool = False,
                     inv: dict | None = None) -> bool:
    """Build one ortho tile. Returns False when no sheet covers it."""
    out = ortho_out_path(out_dir, z, x, y)
    if out.exists() and not overwrite:
        return True
    env = sjtsk_envelope(z, x, y)
    if inv is None:
        inv = load_or_build_inventory(bulk_dir)
    sheets = find_sheets(inv, env, bulk_dir)
    if not sheets:
        return False

    k = pick_draft_k(_ground_mpp(z, x, y))
    dst = np.empty((3, size, size), dtype=np.uint8)
    dst[0], dst[1], dst[2] = FILL_RGB[0], FILL_RGB[1], FILL_RGB[2]
    dst_transform = from_bounds(*tile_bounds_3857(z, x, y), size, size)

    exmin, eymin, exmax, eymax = env
    for jpg, jgw in sheets:
        px, left, top = read_jgw(jgw)
        sheet, sx, sy = _load_sheet(jpg, k)
        dh, dw = sheet.shape[:2]
        pxx, pxy = px * sx, px * sy
        # Crop the drafted sheet to the envelope intersection (+1 px pad).
        col0 = max(0, int((exmin - left) / pxx) - 1)
        col1 = min(dw, int(math.ceil((exmax - left) / pxx)) + 1)
        row0 = max(0, int((top - eymax) / pxy) - 1)
        row1 = min(dh, int(math.ceil((top - eymin) / pxy)) + 1)
        if col0 >= col1 or row0 >= row1:
            continue
        arr = sheet[row0:row1, col0:col1]
        src = np.ascontiguousarray(arr.transpose(2, 0, 1))
        src_transform = from_origin(left + col0 * pxx, top - row0 * pxy,
                                    pxx, pxy)
        # init_dest_nodata=False: pixels this sheet doesn't cover keep what
        # earlier sheets (or the fill) wrote — that's the whole mosaic.
        reproject(src, dst,
                  src_transform=src_transform, src_crs=SJTSK,
                  dst_transform=dst_transform, dst_crs=WMERC,
                  resampling=Resampling.lanczos, init_dest_nodata=False)

    img = Image.fromarray(dst.transpose(1, 2, 0))

    # Clip to the CZ state border: source sheets carry white no-data fill
    # beyond it. Skipped silently when cz_border.geojson is absent.
    from cz_border import load_border
    border = load_border()
    if border is not None:
        lbrt = tile_bounds_3857(z, x, y)     # (left, bottom, right, top)
        cls = border.classify_bbox(*lbrt)
        if cls == "outside":
            return False
        if cls == "crossing":
            fill = Image.new("RGB", img.size, FILL_RGB)
            img = Image.composite(img, fill, border.tile_mask(*lbrt, size))

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jpg.tmp")
    img.save(tmp, format="JPEG", quality=JPEG_QUALITY, progressive=True)
    tmp.replace(out)

    if ktx2:
        from gen_heightfield import encode_ortho_ktx2
        kout = out.with_suffix(".ktx2")
        ktmp = out.with_suffix(".ktx2.tmp")
        encode_ortho_ktx2(out, ktmp)
        ktmp.replace(kout)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--z", type=int, required=True)
    ap.add_argument("--x", type=int, required=True)
    ap.add_argument("--y", type=int, required=True)
    ap.add_argument("--size", type=int, default=TILE_PX)
    ap.add_argument("--ktx2", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--bulk-dir", default=str(BULK_OUT_DIR))
    ap.add_argument("--out", default=str(OUT_DIR))
    a = ap.parse_args()
    ok = build_ortho_tile(a.z, a.x, a.y, Path(a.bulk_dir), Path(a.out),
                          size=a.size, ktx2=a.ktx2, overwrite=a.overwrite)
    print("ok" if ok else "empty (no sheet coverage)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
