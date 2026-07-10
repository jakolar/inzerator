#!/usr/bin/env python3
"""One-shot: force re-aggregation of ortho z=15..8 from the (clean) z=16
base. Early windowed smoke runs baked FILL_RGB quadrants into ancestor
tiles at their window edges (children outside the window didn't exist
yet); the full ČR run then skipped those tiles as already built
(resume-by-existence). Seen as a grey square ring around Olomouc/Šternberk
at z=12-13 (2026-07-10 phone report).

z=16/17/18 are base-baked (window-independent) — never poisoned.
NOTE: aggregating FROM z=17/18 is not safe with this script since the @2x
pass (children are 512^2; downsample_children assumes TILE_PX).

  python3 reagg_ortho.py            # rebuild z=15..8
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from build_pyramid_tile import OUT_DIR
from dispatch_ortho_pyramid import _build_agg

WORKERS = 6


def main() -> int:
    out_dir = Path(OUT_DIR)
    level = {tuple(map(int, (p.parent.name, p.stem)))
             for p in (out_dir / "ortho" / "16").glob("*/*.jpg")}
    print(f"z=16: {len(level):,} base tiles", flush=True)
    for z in range(15, 7, -1):
        parents = sorted({(x // 2, y // 2) for x, y in level})
        t0 = time.time()

        def work(xy):
            x, y = xy
            p = out_dir / "ortho" / str(z) / str(x) / f"{y}.jpg"
            if p.exists():
                p.unlink()      # build artifact, regenerated right below
            return _build_agg(z, x, y, out_dir)

        counts = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as exe:
            for i, r in enumerate(exe.map(work, parents)):
                counts[r] = counts.get(r, 0) + 1
                if (i + 1) % 5000 == 0:
                    rate = (i + 1) / max(time.time() - t0, 1e-6)
                    print(f"  z={z} {i + 1:,}/{len(parents):,} "
                          f"({rate:.0f}/s) {counts}", flush=True)
        print(f"z={z}: {len(parents):,} re-aggregated {counts} "
              f"in {time.time() - t0:.0f}s", flush=True)
        level = set(parents)
    print("END", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
