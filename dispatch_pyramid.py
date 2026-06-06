#!/usr/bin/env python3
"""Bulk heightmap-pyramid builder — pre-bakes the low/mid zoom base.

Strategy (see HEIGHTFIELD_PYRAMID.md + the 2026-06-06 tile-count finding):
a full z=8..18 ČR pyramid is 18.6 M tiles — infeasible to pre-bake. Instead
we pre-bake **z=8..14** statically and leave z=15..18 to an on-demand server
endpoint (a high-zoom tile merges only a few DMPOK sheets, so it's cheap to
build live + cache).

Low zooms can't be built directly from native DMPOK: a z=8 tile spans ~156 km,
which merged at 0.5 m would be a ~400 GB array. So the pyramid is built
**bottom-up**:

  * base level (z=14): each tile reprojected from the DMPOK mosaic via the
    proven `build_pyramid_tile.build_tile` (a z=14 tile merges ~100 MB).
  * z=13 → z=8: each tile downsampled 2×2 from its 4 children one level down.
    Reads 4 small LERC files, no DMPOK re-read.

Resumability is filesystem-based: a tile whose `.lerc` already exists is
skipped (atomic writes in build_tile guarantee no torn files). SIGTERM drains
in-flight tiles and exits; re-run to continue. Failures are logged, not
retried inline.

Output: `<out>/dmpok/<z>/<x>/<y>.lerc` (default /Volumes/Elements/cuzk-pyramid).

Usage:
  python3 dispatch_pyramid.py                              # full ČR, z=8..14
  python3 dispatch_pyramid.py --workers 4
  python3 dispatch_pyramid.py --center 50.736 15.74 --win 4 --zmin 12   # smoke
"""
from __future__ import annotations

import argparse
import math
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import build_pyramid_tile as bpt
from build_pyramid_tile import (
    BULK_OUT_DIR, OUT_DIR, TILE_SIZE, NODATA_FLOAT, build_tile,
)

try:
    import lerc
except ImportError:
    raise SystemExit("lerc package not installed. pip3 install --user lerc")

# ČR WGS84 bounding box (generous — tiles outside real DMPOK coverage just
# return 'empty' and are skipped).
CR_BBOX = (12.09, 48.55, 18.86, 51.06)   # W, S, E, N
BASE_Z = 14
ZMIN = 8

stop_event = threading.Event()
_counters_lock = threading.Lock()
_counters = {"ok": 0, "skip": 0, "empty": 0, "fail": 0}


def _handle_signal(signum, _frame):
    print(f"\n[{_now()}] {signal.Signals(signum).name} — draining…", flush=True)
    stop_event.set()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _bump(key: str, n: int = 1) -> None:
    with _counters_lock:
        _counters[key] += n


def xtile(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * 2 ** z)


def ytile(lat: float, z: int) -> int:
    r = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * 2 ** z)


def tile_range(bbox: tuple, z: int) -> tuple[int, int, int, int]:
    """(x0, x1, y0, y1) inclusive tile range covering bbox at zoom z."""
    w, s, e, n = bbox
    x0, x1 = xtile(w, z), xtile(e, z)
    y0, y1 = ytile(n, z), ytile(s, z)          # north → smaller y
    return x0, x1, y0, y1


def _out_path(out_dir: Path, z: int, x: int, y: int) -> Path:
    return out_dir / "dmpok" / str(z) / str(x) / f"{y}.lerc"


def _decode_lerc(path: Path) -> np.ndarray | None:
    """Decode a LERC tile to a 256×256 float32 array, or None if absent."""
    if not path.is_file():
        return None
    res = lerc.decode(path.read_bytes())
    arr = res[1] if isinstance(res, tuple) else res
    return np.asarray(arr, dtype=np.float32)


def _encode_lerc(arr: np.ndarray, path: Path, max_z_error: float) -> int:
    arr = np.ascontiguousarray(arr.astype(np.float32))
    code, nbytes, buf = lerc.encode(arr, 1, False, None, max_z_error,
                                    arr.nbytes + 1024)
    if code != 0:
        raise RuntimeError(f"LERC encode failed (code {code})")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".lerc.tmp")
    tmp.write_bytes(bytes(buf[:nbytes]))
    tmp.replace(path)
    return nbytes


def _downsample_2x2(quad: np.ndarray) -> np.ndarray:
    """512×512 → 256×256, mean over non-nodata pixels in each 2×2 block."""
    block = quad.reshape(TILE_SIZE, 2, TILE_SIZE, 2)
    valid = block > (NODATA_FLOAT + 1)
    cnt = valid.sum(axis=(1, 3))
    summed = np.where(valid, block, 0.0).sum(axis=(1, 3))
    with np.errstate(invalid="ignore"):
        out = np.where(cnt > 0, summed / np.maximum(cnt, 1), NODATA_FLOAT)
    return out.astype(np.float32)


def _build_base(z: int, x: int, y: int, bulk_dir: Path, out_dir: Path,
                max_z_error: float) -> str:
    out = _out_path(out_dir, z, x, y)
    if out.exists():
        return "skip"
    try:
        # build_tile is chatty — its output is silenced process-wide via the
        # module-level `print` override installed in main() (thread-safe,
        # unlike redirect_stdout which swaps the shared sys.stdout).
        ok = build_tile(z, x, y, bulk_dir, out_dir,
                        max_z_error=max_z_error, overwrite=False)
        return "ok" if ok else "empty"
    except Exception as e:                                    # noqa: BLE001
        _log(out_dir, f"FAIL base z={z} x={x} y={y} {e!r}")
        return "fail"


def _build_agg(z: int, x: int, y: int, out_dir: Path, max_z_error: float) -> str:
    out = _out_path(out_dir, z, x, y)
    if out.exists():
        return "skip"
    # 4 children one level down.
    children = {
        (0, 0): _decode_lerc(_out_path(out_dir, z + 1, 2 * x, 2 * y)),
        (0, 1): _decode_lerc(_out_path(out_dir, z + 1, 2 * x + 1, 2 * y)),
        (1, 0): _decode_lerc(_out_path(out_dir, z + 1, 2 * x, 2 * y + 1)),
        (1, 1): _decode_lerc(_out_path(out_dir, z + 1, 2 * x + 1, 2 * y + 1)),
    }
    if all(c is None for c in children.values()):
        return "empty"
    quad = np.full((2 * TILE_SIZE, 2 * TILE_SIZE), NODATA_FLOAT, dtype=np.float32)
    for (qy, qx), arr in children.items():
        if arr is not None:
            quad[qy * TILE_SIZE:(qy + 1) * TILE_SIZE,
                 qx * TILE_SIZE:(qx + 1) * TILE_SIZE] = arr
    try:
        _encode_lerc(_downsample_2x2(quad), out, max_z_error)
        return "ok"
    except Exception as e:                                    # noqa: BLE001
        _log(out_dir, f"FAIL agg z={z} x={x} y={y} {e!r}")
        return "fail"


_log_lock = threading.Lock()


def _log(out_dir: Path, line: str) -> None:
    with _log_lock:
        with (out_dir / "pyramid_build.log").open("a") as f:
            f.write(f"{_now()} {line}\n")


def _run_level(z: int, bbox: tuple, bulk_dir: Path, out_dir: Path,
               max_z_error: float, workers: int, base_z: int) -> None:
    x0, x1, y0, y1 = tile_range(bbox, z)
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    is_base = (z == base_z)
    print(f"[{_now()}] level z={z}: {total:,} tiles "
          f"({'base←DMPOK' if is_base else 'agg←children'})", flush=True)

    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def work(xy):
        if stop_event.is_set():
            return
        x, y = xy
        if is_base:
            r = _build_base(z, x, y, bulk_dir, out_dir, max_z_error)
        else:
            r = _build_agg(z, x, y, out_dir, max_z_error)
        _bump(r)

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as exe:
        for _ in exe.map(work, coords):
            done += 1
            if stop_event.is_set():
                break
            if done % 500 == 0:
                rate = done / max(time.time() - t0, 1e-6)
                with _counters_lock:
                    c = dict(_counters)
                print(f"  z={z} {done:,}/{total:,} ({rate:.0f}/s) "
                      f"ok={c['ok']} skip={c['skip']} empty={c['empty']} "
                      f"fail={c['fail']}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zmin", type=int, default=ZMIN)
    ap.add_argument("--zmax", type=int, default=BASE_Z,
                    help="highest pre-baked zoom = base level (default 14)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--bulk-dir", default=str(BULK_OUT_DIR))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-z-error", type=float, default=0.10)
    ap.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"),
                    help="smoke test: restrict to a window around this point")
    ap.add_argument("--win", type=int, default=4,
                    help="window half-size in base-zoom tiles (with --center)")
    args = ap.parse_args()

    base_z = args.zmax
    bulk_dir = Path(args.bulk_dir)
    out_dir = Path(args.out)
    if not bulk_dir.is_dir():
        raise SystemExit(f"--bulk-dir {bulk_dir} not found")
    out_dir.mkdir(parents=True, exist_ok=True)

    bbox = CR_BBOX
    if args.center:
        lat, lon = args.center
        cx, cy = xtile(lon, base_z), ytile(lat, base_z)
        w = args.win
        # geographic bbox of the [cx-w..cx+w]×[cy-w..cy+w] window at base_z
        n2 = 2 ** base_z
        def lon_of(xt): return xt / n2 * 360.0 - 180.0
        def lat_of(yt):
            t = math.pi * (1 - 2 * yt / n2)
            return math.degrees(math.atan(math.sinh(t)))
        bbox = (lon_of(cx - w), lat_of(cy + w + 1),
                lon_of(cx + w + 1), lat_of(cy - w))
        print(f"[{_now()}] smoke window around {lat},{lon}: bbox {bbox}")

    # Load DMPOK inventory ONCE and memoize, else build_tile re-parses the
    # 2.6 MB inventory.json on every one of ~55 k base tiles.
    inv = bpt.load_or_build_inventory(bulk_dir)
    bpt.load_or_build_inventory = lambda _bulk: inv
    # Silence build_tile's per-tile prints process-wide. Name resolution in
    # build_pyramid_tile finds this module global before the builtin.
    bpt.print = lambda *a, **k: None
    print(f"[{_now()}] inventory: {len(inv)} DMPOK sheets")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Base first, then aggregate downward (each level needs the one above it).
    levels = [base_z] + list(range(base_z - 1, args.zmin - 1, -1))
    for z in levels:
        if stop_event.is_set():
            break
        _run_level(z, bbox, bulk_dir, out_dir, args.max_z_error,
                   args.workers, base_z)

    with _counters_lock:
        c = dict(_counters)
    print(f"[{_now()}] END ok={c['ok']} skip={c['skip']} "
          f"empty={c['empty']} fail={c['fail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
