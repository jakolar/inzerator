#!/usr/bin/env python3
"""Populated-area mask for the high-zoom pre-bake (spec D3 / F3).

OSM place nodes (city/town/village/hamlet/suburb) for CZ — one Overpass
query — cached to populated.json next to the pyramid. A tile counts as
"populated" when its mercator bbox, expanded by the radius, contains any
place point (box test; circle precision is irrelevant for a bake filter).

Deviace od spec ("RÚIAN obce + buffer"): OSM place body pokrývají i
osady a jsou jeden ~2 MB dotaz; RÚIAN VFR polygony jsou stovky MB
stahování a parsování. Maska jen šetří pre-bake čas/disk — přesné
hranice obcí nejsou podstatné, on-demand endpoint kryje zbytek.

Usage:
  python3 populated_mask.py --fetch                # write populated.json
  python3 populated_mask.py --fetch --radius 1500
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

WMERC = 20037508.342789244
OVERPASS = "https://overpass-api.de/api/interpreter"
QUERY = """[out:json][timeout:180];
area["ISO3166-1"="CZ"][admin_level=2]->.cz;
node["place"~"^(city|town|village|hamlet|suburb)$"](area.cz);
out skel qt;"""
DEFAULT_RADIUS_M = 1200.0
DEFAULT_PATH = Path("/Volumes/Elements/cuzk-pyramid/populated.json")


def lonlat_to_merc(lon: float, lat: float) -> tuple[float, float]:
    return (lon / 180.0 * WMERC,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
            / math.pi * WMERC)


class PopulatedMask:
    """Grid-bucketed point set with an intersects-bbox test."""

    def __init__(self, points_merc: list, radius_m: float):
        self.r = radius_m
        self.cell = max(radius_m * 4, 5000.0)
        self.grid: dict = {}
        for x, y in points_merc:
            self.grid.setdefault(
                (int(x // self.cell), int(y // self.cell)), []).append((x, y))

    def intersects_bbox(self, left, bottom, right, top) -> bool:
        l, b = left - self.r, bottom - self.r
        r, t = right + self.r, top + self.r
        for ix in range(int(l // self.cell), int(r // self.cell) + 1):
            for iy in range(int(b // self.cell), int(t // self.cell) + 1):
                for x, y in self.grid.get((ix, iy), ()):
                    if l <= x <= r and b <= y <= t:
                        return True
        return False

    def intersects_tile(self, z: int, x: int, y: int) -> bool:
        from build_pyramid_tile import tile_bounds_3857
        return self.intersects_bbox(*tile_bounds_3857(z, x, y))


def load_mask(path: Path) -> PopulatedMask:
    data = json.loads(path.read_text())
    pts = [lonlat_to_merc(lon, lat) for lon, lat in data["points"]]
    return PopulatedMask(pts, float(data.get("radius_m", DEFAULT_RADIUS_M)))


def fetch(out_path: Path, radius_m: float) -> None:
    req = urllib.request.Request(
        OVERPASS, data=QUERY.encode(),
        headers={"User-Agent": "inzerator-pyramid/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    pts = [[e["lon"], e["lat"]] for e in data["elements"] if "lat" in e]
    if len(pts) < 5000:
        raise SystemExit(f"suspiciously few place points ({len(pts)}) — not saving")
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"radius_m": radius_m, "points": pts}))
    tmp.replace(out_path)
    print(f"{len(pts)} place points → {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    a = ap.parse_args()
    if a.fetch:
        fetch(Path(a.out), a.radius)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
