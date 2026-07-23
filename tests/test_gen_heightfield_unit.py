import gen_heightfield as gh


def test_derive_rings_default_geometry():
    """inner_half=500 reproduces today's halves/steps (the regression anchor);
    max_z_error follows the step formula."""
    rings = gh.derive_rings(500)
    assert [r["slug"] for r in rings] == ["closeup", "inner"]
    closeup, inner = rings
    assert (closeup["half"], closeup["step"]) == (1500, 1.5)
    assert (inner["half"], inner["step"]) == (500, 0.5)
    assert closeup["ortho_size"] == 4096 and inner["ortho_size"] == 4096
    assert closeup["max_z_error"] == gh.default_max_z_error_for_step(1.5)
    assert inner["max_z_error"] == gh.default_max_z_error_for_step(0.5)


def test_derive_rings_clamps():
    assert gh.derive_rings(100)[1]["half"] == 500      # below MIN → 500
    assert gh.derive_rings(9999)[1]["half"] == 2000    # above MAX → 2000
    assert gh.derive_rings(9999)[0]["half"] == 6000    # closeup = 3× inner


def test_derive_rings_step_scales_with_half():
    closeup, inner = gh.derive_rings(1000)
    assert (inner["half"], inner["step"]) == (1000, 1.0)
    assert (closeup["half"], closeup["step"]) == (3000, 3.0)


def test_derive_rings_single_ring_drops_closeup():
    """Polygon výřez: one tight inner ring, no closeup, 60 m floor."""
    rings = gh.derive_rings(800, single_ring=True)
    assert [r["slug"] for r in rings] == ["inner"]
    assert (rings[0]["half"], rings[0]["step"]) == (800, 0.8)
    # clamp floor 60 (not 500), cap 2000
    assert gh.derive_rings(30, single_ring=True)[0]["half"] == 60
    assert gh.derive_rings(9999, single_ring=True)[0]["half"] == 2000


def test_derive_rings_step_floors_at_dmpok_resolution():
    """A small ring must not over-sample below SM5's 0.5 m/px."""
    # inner_half 120 → half/1000 = 0.12, floored to 0.5
    assert gh.derive_rings(120, single_ring=True)[0]["step"] == 0.5
    # inner_half 800 → 0.8, above the floor, unchanged
    assert gh.derive_rings(800, single_ring=True)[0]["step"] == 0.8


def test_resolve_rings_single_ring_passes_through():
    rings = gh.resolve_rings(None, 800, single_ring=True)
    assert [r["slug"] for r in rings] == ["inner"]


def test_resolve_rings_default_when_nothing_set():
    assert gh.resolve_rings(None, None) is gh.DEFAULT_RINGS


def test_resolve_rings_inner_half():
    rings = gh.resolve_rings(None, 1000)
    assert rings[1]["half"] == 1000 and rings[1]["step"] == 1.0


def test_resolve_rings_file_wins(tmp_path):
    import json
    f = tmp_path / "rings.json"
    f.write_text(json.dumps([{"slug": "x", "half": 42, "step": 1,
                              "ortho_size": 4096, "max_z_error": 0.1}]))
    rings = gh.resolve_rings(str(f), 1000)   # file takes precedence over inner_half
    assert rings == [{"slug": "x", "half": 42, "step": 1,
                      "ortho_size": 4096, "max_z_error": 0.1}]
