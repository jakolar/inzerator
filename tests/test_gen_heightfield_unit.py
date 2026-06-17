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
