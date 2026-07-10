#!/usr/bin/env python3
"""Force re-aggregation of ONE pyramid level from its children.

Needed after the full-CR z=18 fill: z=17 tiles built during the masked F3
run have baked-in fill quadrants (ortho FILL_RGB / heights NODATA) where
children outside the populated mask didn't exist yet. Once z=18 is
complete, re-aggregating z=17 heals them.

  python3 reagg_level.py --layer ortho --z 17
  python3 reagg_level.py --layer dmpok --z 17
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from build_pyramid_tile import OUT_DIR

WORKERS = 6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--layer", choices=("ortho", "dmpok"), required=True)
    ap.add_argument("--z", type=int, required=True)
    ap.add_argument("--out", default=str(OUT_DIR))
    a = ap.parse_args()
    out_dir = Path(a.out)

    if a.layer == "ortho":
        from dispatch_ortho_pyramid import _build_agg
        ext = "jpg"
        agg = lambda z, x, y: _build_agg(z, x, y, out_dir)          # noqa: E731
    else:
        from dispatch_pyramid import _build_agg
        ext = "lerc"
        agg = lambda z, x, y: _build_agg(z, x, y, out_dir, 0.10)    # noqa: E731

    child_dir = out_dir / a.layer / str(a.z + 1)
    parents = sorted({(int(p.parent.name) // 2, int(p.stem) // 2)
                      for p in child_dir.glob("*/*." + ext)})
    print(f"{a.layer} z={a.z}: {len(parents):,} parents from existing "
          f"z={a.z + 1} children", flush=True)
    t0 = time.time()

    def work(xy):
        x, y = xy
        p = out_dir / a.layer / str(a.z) / str(x) / f"{y}.{ext}"
        if p.exists():
            p.unlink()      # build artifact, regenerated right below
        return agg(a.z, x, y)

    counts = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as exe:
        for i, r in enumerate(exe.map(work, parents)):
            counts[r] = counts.get(r, 0) + 1
            if (i + 1) % 5000 == 0:
                rate = (i + 1) / max(time.time() - t0, 1e-6)
                print(f"  {i + 1:,}/{len(parents):,} ({rate:.0f}/s) {counts}",
                      flush=True)
    print(f"END {a.layer} z={a.z}: {counts} in {time.time() - t0:.0f}s",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
