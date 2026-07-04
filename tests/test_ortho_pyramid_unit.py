"""Unit tests for the ortho pyramid builder (no bulk data / server needed)."""
import sys
from pathlib import Path

import pytest
from PIL import Image
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_ortho_tile import (          # noqa: E402
    pick_draft_k, read_jgw, sjtsk_envelope, TILE_PX, FILL_RGB,
)
from build_pyramid_tile import tile_bounds_3857   # noqa: E402
from dispatch_ortho_pyramid import downsample_children   # noqa: E402


def test_pick_draft_k():
    assert pick_draft_k(1.53) == 8      # z=16 base
    assert pick_draft_k(0.77) == 4      # z=17
    assert pick_draft_k(0.384) == 2     # z=18
    assert pick_draft_k(0.19) == 1      # z=19
    assert pick_draft_k(0.1) == 1       # finer than native → no draft


def test_read_jgw(tmp_path):
    jgw = tmp_path / "s.jgw"
    jgw.write_text("0.125000\n0.000000\n0.000000\n-0.125000\n"
                   "-527499.937500\n-1100000.062500\n")
    px, left, top = read_jgw(jgw)
    assert px == 0.125
    assert left == pytest.approx(-527500.0)
    assert top == pytest.approx(-1100000.0)


def test_sjtsk_envelope_contains_tile_corners():
    z, x, y = 16, 35913, 22333          # Šternberk area
    env = sjtsk_envelope(z, x, y, margin_m=0.0)
    to_sjtsk = Transformer.from_crs("EPSG:3857", "EPSG:5514", always_xy=True)
    left, bottom, right, top = tile_bounds_3857(z, x, y)
    for mx, my in [(left, bottom), (left, top), (right, bottom), (right, top)]:
        sx, sy = to_sjtsk.transform(mx, my)
        assert env[0] <= sx <= env[2]
        assert env[1] <= sy <= env[3]


def test_downsample_children_quadrants():
    kids = {
        (0, 0): Image.new("RGB", (TILE_PX, TILE_PX), (200, 0, 0)),
        (0, 1): Image.new("RGB", (TILE_PX, TILE_PX), (0, 200, 0)),
        (1, 0): Image.new("RGB", (TILE_PX, TILE_PX), (0, 0, 200)),
        (1, 1): None,                    # missing → neutral fill
    }
    parent = downsample_children(kids)
    assert parent.size == (TILE_PX, TILE_PX)
    q = TILE_PX // 4
    assert parent.getpixel((q, q)) == (200, 0, 0)          # NW
    assert parent.getpixel((3 * q, q)) == (0, 200, 0)      # NE
    assert parent.getpixel((q, 3 * q)) == (0, 0, 200)      # SW
    assert parent.getpixel((3 * q, 3 * q)) == FILL_RGB     # missing SE


def test_downsample_children_all_missing():
    assert downsample_children({(0, 0): None, (0, 1): None,
                                (1, 0): None, (1, 1): None}) is None


def test_populated_mask():
    from populated_mask import PopulatedMask, lonlat_to_merc
    from build_pyramid_tile import latlon_to_tile
    pts = [lonlat_to_merc(17.29, 49.62)]        # Šternberk
    mask = PopulatedMask(pts, radius_m=1200.0)
    x, y = latlon_to_tile(49.62, 17.29, 18)
    assert mask.intersects_tile(18, x, y)
    assert mask.intersects_tile(18, x + 4, y)    # within 1.2 km buffer
    assert not mask.intersects_tile(18, x + 200, y)   # ~30 km away
    assert not mask.intersects_tile(18, x, y + 200)


def test_read_jgw_czech_decimal_comma(tmp_path):
    jgw = tmp_path / "s.jgw"
    jgw.write_text(",125\n0\n0\n-,125\n-527499,9375\n-1100000,0625\n")
    px, left, top = read_jgw(jgw)
    assert px == 0.125
    assert left == pytest.approx(-527500.0)
    assert top == pytest.approx(-1100000.0)
