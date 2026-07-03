#!/usr/bin/env python3
"""Bulk ortho-pyramid builder — clone of dispatch_pyramid.py for colour.

Base level **z=16** built per tile from the bulk SM5 ortofoto sheets via
`build_ortho_tile.build_ortho_tile`; z=15 down to z=8 downsampled 2x2
from children JPEGs (reads 4 small files, no sheet re-read). z=17..18
populated-only is a later pass (spec D3/F3).

Resumability is filesystem-based: a tile whose `.jpg` exists is skipped
(atomic writes guarantee no torn files). SIGTERM drains in-flight tiles
and exits; re-run to continue. Failures are logged to ortho_build.log.

Output: `<out>/ortho/{z}/{x}/{y}.jpg` (default /Volumes/Elements/cuzk-pyramid).

Usage:
  python3 dispatch_ortho_pyramid.py                            # full ČR z=8..16
  python3 dispatch_ortho_pyramid.py --workers 3
  python3 dispatch_ortho_pyramid.py --center 49.62 17.29 --win 2 --zmin 15  # smoke
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from build_pyramid_tile import BULK_OUT_DIR, OUT_DIR, load_or_build_inventory
from build_ortho_tile import (
    TILE_PX, JPEG_QUALITY, FILL_RGB, build_ortho_tile, ortho_out_path,
)
from dispatch_pyramid import xtile, ytile, tile_range, CR_BBOX

BASE_Z = 16
ZMIN = 8

stop_event = threading.Event()
_counters_lock = threading.Lock()
_counters = {"ok": 0, "skip": 0, "empty": 0, "fail": 0}
_log_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _handle_signal(signum, _frame):
    print(f"\n[{_now()}] {signal.Signals(signum).name} — draining…", flush=True)
    stop_event.set()


def _bump(key: str) -> None:
    with _counters_lock:
        _counters[key] += 1


def _log(out_dir: Path, line: str) -> None:
    with _log_lock:
        with (out_dir / "ortho_build.log").open("a") as f:
            f.write(f"{_now()} {line}\n")


def downsample_children(children: dict) -> Image.Image | None:
    """4 optional child images {(qy, qx): Image|None} → 256^2 parent.
    Missing quadrants get the neutral fill. None when all children absent."""
    if all(v is None for v in children.values()):
        return None
    quad = Image.new("RGB", (2 * TILE_PX, 2 * TILE_PX), FILL_RGB)
    for (qy, qx), im in children.items():
        if im is not None:
            quad.paste(im, (qx * TILE_PX, qy * TILE_PX))
    return quad.resize((TILE_PX, TILE_PX), Image.LANCZOS)


def _open_child(out_dir: Path, z: int, x: int, y: int) -> Image.Image | None:
    p = ortho_out_path(out_dir, z, x, y)
    return Image.open(p).convert("RGB") if p.is_file() else None


def _build_agg(z: int, x: int, y: int, out_dir: Path) -> str:
    out = ortho_out_path(out_dir, z, x, y)
    if out.exists():
        return "skip"
    parent = downsample_children({
        (0, 0): _open_child(out_dir, z + 1, 2 * x, 2 * y),
        (0, 1): _open_child(out_dir, z + 1, 2 * x + 1, 2 * y),
        (1, 0): _open_child(out_dir, z + 1, 2 * x, 2 * y + 1),
        (1, 1): _open_child(out_dir, z + 1, 2 * x + 1, 2 * y + 1),
    })
    if parent is None:
        return "empty"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".jpg.tmp")
        parent.save(tmp, format="JPEG", quality=JPEG_QUALITY, progressive=True)
        tmp.replace(out)
        return "ok"
    except Exception as e:                                     # noqa: BLE001
        _log(out_dir, f"FAIL agg z={z} x={x} y={y} {e!r}")
        return "fail"


def _build_base(z: int, x: int, y: int, bulk_dir: Path, out_dir: Path,
                inv: dict) -> str:
    out = ortho_out_path(out_dir, z, x, y)
    if out.exists():
        return "skip"
    try:
        ok = build_ortho_tile(z, x, y, bulk_dir, out_dir, inv=inv)
        return "ok" if ok else "empty"
    except Exception as e:                                     # noqa: BLE001
        _log(out_dir, f"FAIL base z={z} x={x} y={y} {e!r}")
        return "fail"


def _run_level(z: int, bbox: tuple, bulk_dir: Path, out_dir: Path,
               workers: int, base_z: int, inv: dict) -> None:
    x0, x1, y0, y1 = tile_range(bbox, z)
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    is_base = (z == base_z)
    print(f"[{_now()}] level z={z}: {total:,} tiles "
          f"({'base←SM5 ortho' if is_base else 'agg←children'})", flush=True)
    coords = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def work(xy):
        if stop_event.is_set():
            return
        x, y = xy
        r = (_build_base(z, x, y, bulk_dir, out_dir, inv) if is_base
             else _build_agg(z, x, y, out_dir))
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zmin", type=int, default=ZMIN)
    ap.add_argument("--zmax", type=int, default=BASE_Z,
                    help="base level built from sheets (default 16)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--bulk-dir", default=str(BULK_OUT_DIR))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"),
                    help="smoke window centre (with --win)")
    ap.add_argument("--win", type=int, default=4,
                    help="smoke window half-size in base-level tiles")
    a = ap.parse_args()
    bulk_dir, out_dir = Path(a.bulk_dir), Path(a.out)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    bbox = CR_BBOX
    if a.center:
        lat, lon = a.center
        xc, yc = xtile(lon, a.zmax), ytile(lat, a.zmax)
        # window bbox from base-level tile range, reused for all levels
        from build_pyramid_tile import tile_bounds_3857
        import math as m
        l, b_, r, t = (tile_bounds_3857(a.zmax, xc - a.win, yc + a.win)[0],
                       tile_bounds_3857(a.zmax, xc, yc + a.win)[1],
                       tile_bounds_3857(a.zmax, xc + a.win, yc)[2],
                       tile_bounds_3857(a.zmax, xc, yc - a.win)[3])
        to_deg = lambda mx: mx / 20037508.342789244 * 180.0   # noqa: E731
        merc_lat = lambda my: m.degrees(                       # noqa: E731
            2 * m.atan(m.exp(my / 6378137.0)) - m.pi / 2)
        bbox = (to_deg(l), merc_lat(b_), to_deg(r), merc_lat(t))
        print(f"[{_now()}] smoke window around {lat},{lon}: bbox {bbox}")

    inv = load_or_build_inventory(bulk_dir)
    print(f"[{_now()}] inventory: {len(inv)} sheets")

    for z in range(a.zmax, a.zmin - 1, -1):
        if stop_event.is_set():
            break
        _run_level(z, bbox, bulk_dir, out_dir, a.workers, a.zmax, inv)

    with _counters_lock:
        c = dict(_counters)
    print(f"[{_now()}] END ok={c['ok']} skip={c['skip']} "
          f"empty={c['empty']} fail={c['fail']}", flush=True)
    return 1 if c["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
