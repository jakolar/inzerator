#!/usr/bin/env python3
"""CZ state-border clip for ortho tiles — masks the white no-data fill
that ČÚZK sheets carry beyond the border (spec: bile-okraje backlog).

Data: OSM relation 51684 GeoJSON (polygons.openstreetmap.fr), fetched once
to <pyramid>/cz_border.geojson. Precision is metres — good enough to clip
0.4 m/px tiles (the border white in source sheets bleeds tens of metres).

Classification is O(1) per tile via two prebuilt structures:
  * a segment grid (5 km cells) — tile bbox near a border segment → 'crossing'
  * a coarse inside-bitmap (~400 m/px PIL rasterization) — else in/out lookup
'crossing' tiles get a per-tile pixel mask (ImageDraw polygon in tile pixel
space); PIL clips the huge polygon to the 256^2 canvas cheaply.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

WMERC = 20037508.342789244
DEFAULT_PATH = Path("/Volumes/Elements/cuzk-pyramid/cz_border.geojson")
_COARSE_W = 4096


def _lonlat_to_merc(lon: float, lat: float) -> tuple[float, float]:
    return (lon / 180.0 * WMERC,
            math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
            / math.pi * WMERC)


class CzBorder:
    def __init__(self, rings_merc: list):
        self.rings = rings_merc                      # [[(mx,my),...], ...]
        xs = [p[0] for r in rings_merc for p in r]
        ys = [p[1] for r in rings_merc for p in r]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        # Segment grid: cell → True (any border segment touches it).
        self.cell = 5000.0
        self.seg_cells: set = set()
        for ring in rings_merc:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                # conservative: mark every cell the segment's bbox spans
                for ix in range(int(min(x1, x2) // self.cell),
                                int(max(x1, x2) // self.cell) + 1):
                    for iy in range(int(min(y1, y2) // self.cell),
                                    int(max(y1, y2) // self.cell) + 1):
                        self.seg_cells.add((ix, iy))
        # Coarse inside-bitmap for cells with no segments.
        l, b, r, t = self.bbox
        self._cw = (r - l) / _COARSE_W
        h = max(1, int((t - b) / self._cw))
        img = Image.new("1", (_COARSE_W, h), 0)
        d = ImageDraw.Draw(img)
        for ring in rings_merc:
            d.polygon([((x - l) / self._cw, (t - y) / self._cw)
                       for x, y in ring], fill=1)
        self._coarse = img.load()
        self._ch = h

    def _inside_coarse(self, mx: float, my: float) -> bool:
        l, _, _, t = self.bbox
        px = int((mx - l) / self._cw)
        py = int((t - my) / self._cw)
        if not (0 <= px < _COARSE_W and 0 <= py < self._ch):
            return False
        return bool(self._coarse[px, py])

    def classify_bbox(self, left, bottom, right, top) -> str:
        """'inside' | 'outside' | 'crossing' (conservative)."""
        for ix in range(int(left // self.cell), int(right // self.cell) + 1):
            for iy in range(int(bottom // self.cell),
                            int(top // self.cell) + 1):
                if (ix, iy) in self.seg_cells:
                    return "crossing"
        return ("inside"
                if self._inside_coarse((left + right) / 2, (bottom + top) / 2)
                else "outside")

    def tile_mask(self, left, bottom, right, top, size: int) -> Image.Image:
        """PIL 'L' mask for a tile bbox: 255 inside CZ, 0 outside.

        Rings are rectangle-clipped to the tile bbox first —
        ImageDraw.polygon silently produces an empty fill when fed the
        full 125k-vertex ring whose coordinates lie far outside the
        canvas (found on the 2026-07-05 Aš samples)."""
        sx = size / (right - left)
        sy = size / (top - bottom)
        img = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(img)
        any_clip = False
        for ring in self.rings:
            clipped = _clip_ring(ring, left, bottom, right, top)
            if len(clipped) >= 3:
                any_clip = True
                d.polygon([((x - left) * sx, (top - y) * sy)
                           for x, y in clipped], fill=255)
        if not any_clip:
            # No ring edge crosses this tile (conservative 5 km cells) —
            # it is wholly inside or wholly outside: exact ray cast.
            cx, cy = (left + right) / 2, (bottom + top) / 2
            if any(_point_in_ring(cx, cy, r) for r in self.rings):
                img.paste(255, (0, 0, size, size))
        return img


def _point_in_ring(px: float, py: float, ring: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > py) != (y2 > py) and \
                px < x1 + (py - y1) / (y2 - y1) * (x2 - x1):
            inside = not inside
    return inside


def _clip_ring(ring: list, left, bottom, right, top) -> list:
    """Sutherland–Hodgman polygon clip against an axis-aligned rectangle."""
    def clip_edge(pts, inside, intersect):
        out = []
        for p, q in zip(pts, pts[1:] + pts[:1]):
            pin, qin = inside(p), inside(q)
            if pin:
                out.append(p)
                if not qin:
                    out.append(intersect(p, q))
            elif qin:
                out.append(intersect(p, q))
        return out

    def ix(p, q, x):   # intersection with vertical line x
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))

    def iy(p, q, y):   # intersection with horizontal line y
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)

    pts = ring
    for inside, intersect in [
        (lambda p: p[0] >= left,   lambda p, q: ix(p, q, left)),
        (lambda p: p[0] <= right,  lambda p, q: ix(p, q, right)),
        (lambda p: p[1] >= bottom, lambda p, q: iy(p, q, bottom)),
        (lambda p: p[1] <= top,    lambda p, q: iy(p, q, top)),
    ]:
        pts = clip_edge(pts, inside, intersect)
        if not pts:
            return []
    return pts


_BORDER = None


def load_border(path: Path = DEFAULT_PATH) -> CzBorder | None:
    """Memoized loader; None when the geojson is absent (masking is then
    skipped — builder degrades to the old behaviour)."""
    global _BORDER
    if _BORDER is not None:
        return _BORDER
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    geom = data["geometries"][0] if "geometries" in data else data
    rings = []
    for poly in geom["coordinates"]:
        outer = poly[0]                      # holes: CZ has none
        rings.append([_lonlat_to_merc(lon, lat) for lon, lat in outer])
    _BORDER = CzBorder(rings)
    return _BORDER
