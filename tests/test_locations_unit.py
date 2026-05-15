import locations


def test_module_imports():
    assert locations.STEP_NAMES == ("panorama", "outer", "closeup", "inner")
    assert locations.TILES_DIR_PREFIX == "tiles_v2_"
