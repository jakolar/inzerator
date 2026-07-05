#!/usr/bin/env python3
"""One-shot backfill: re-cut existing ortho tiles crossed by the CZ border
(white no-data beyond the border → neutral fill via cz_border clip), then
rebuild their ancestor aggregates bottom-up.

Run AFTER the F3 chain finishes (shares the Elements disk):
  python3 backfill_border.py            # z=16 base + z=15..8 ancestors
  python3 backfill_border.py --dry-run  # count only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from build_pyramid_tile import OUT_DIR, BULK_OUT_DIR, tile_bounds_3857
from build_ortho_tile import build_ortho_tile, ortho_out_path
from cz_border import load_border, WMERC
from dispatch_ortho_pyramid import _build_agg

BASE_Z = 16


def crossing_tiles(border, z: int) -> set:
    """Tiles at zoom z whose bbox a border segment actually intersects
    (walk segments, mark tiles under each — exact, no 5 km smear)."""
    n = 2 ** z
    side = 2 * WMERC / n
    tiles = set()
    for ring in border.rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            tx0 = int((min(x1, x2) + WMERC) / side)
            tx1 = int((max(x1, x2) + WMERC) / side)
            ty0 = int((WMERC - max(y1, y2)) / side)
            ty1 = int((WMERC - min(y1, y2)) / side)
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    tiles.add((tx, ty))
    return tiles


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(OUT_DIR))
    a = ap.parse_args()
    out_dir = Path(a.out)
    border = load_border()
    if border is None:
        raise SystemExit("cz_border.geojson missing — run the fetch first")

    base = {t for t in crossing_tiles(border, BASE_Z)
            if ortho_out_path(out_dir, BASE_Z, *t).exists()}
    print(f"z={BASE_Z}: {len(base):,} existing border tiles to re-cut")
    if a.dry_run:
        return 0

    for i, (x, y) in enumerate(sorted(base)):
        build_ortho_tile(BASE_Z, x, y, BULK_OUT_DIR, out_dir, overwrite=True)
        if (i + 1) % 200 == 0:
            print(f"  {i + 1:,}/{len(base):,}", flush=True)

    level = base
    for z in range(BASE_Z - 1, 7, -1):
        parents = {(x // 2, y // 2) for x, y in level}
        rebuilt = 0
        for x, y in sorted(parents):
            p = ortho_out_path(out_dir, z, x, y)
            if p.exists():
                p.unlink()          # build artifact, regenerated right below
                _build_agg(z, x, y, out_dir)
                rebuilt += 1
        print(f"z={z}: {rebuilt:,} ancestors re-aggregated", flush=True)
        level = parents
    return 0


if __name__ == "__main__":
    sys.exit(main())
