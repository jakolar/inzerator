import locations
import pytest


def test_module_imports():
    assert locations.STEP_NAMES == ("panorama", "outer", "closeup", "inner")
    assert locations.TILES_DIR_PREFIX == "tiles_v2_"


@pytest.mark.parametrize("inp,expected", [
    ("Hnojice", "hnojice"),
    ("Strážek", "strazek"),
    ("Šternberk u Olomouce", "sternberk-u-olomouce"),
    ("  Trailing space  ", "trailing-space"),
    ("Praha 4 - Modřany", "praha-4-modrany"),
    ("ÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ", "acdeeinorstuuyz"),
])
def test_slugify(inp, expected):
    assert locations.slugify(inp) == expected


@pytest.mark.parametrize("s,ok", [
    ("hnojice", True),
    ("hnojice-2", True),
    ("hnojice-statek-47", True),
    ("Hnojice", False),       # uppercase
    ("hnojice_47", False),    # underscore
    ("hnojice 47", False),    # space
    ("-hnojice", False),      # leading dash
    ("hnojice-", False),      # trailing dash
    ("", False),
])
def test_is_valid_slug(s, ok):
    assert locations.is_valid_slug(s) is ok


def test_next_free_slug_no_conflict():
    assert locations.next_free_slug("hnojice", existing=set()) == "hnojice"


def test_next_free_slug_appends_number():
    existing = {"hnojice", "hnojice-2"}
    assert locations.next_free_slug("hnojice", existing=existing) == "hnojice-3"


def test_next_free_slug_skips_holes():
    """Pokud existuje 'hnojice' a 'hnojice-5', vrátí '-2' (první volný)."""
    existing = {"hnojice", "hnojice-5"}
    assert locations.next_free_slug("hnojice", existing=existing) == "hnojice-2"


@pytest.mark.parametrize("adresa,obec", [
    ("č.p. 136, 78501 Hnojice", "Hnojice"),
    ("Jemnice 8, 59253 Strážek", "Strážek"),
    ("Hlavní 47, 100 00 Praha 10", "Praha 10"),
    ("č.p. 5, 78501 Hnojice u Šternberka", "Hnojice u Šternberka"),
])
def test_parse_obec(adresa, obec):
    assert locations.parse_obec(adresa) == obec


def test_expected_glb_paths():
    from pathlib import Path
    assert locations.expected_glb("hnojice", "panorama") == Path("tiles_v2_hnojice/panorama.glb")
    assert locations.expected_glb("hnojice", "outer") == Path("tiles_v2_hnojice/details/outer.glb")
    assert locations.expected_glb("hnojice", "closeup") == Path("tiles_v2_hnojice/details/closeup.glb")
    assert locations.expected_glb("hnojice", "inner") == Path("tiles_v2_hnojice/details/inner.glb")


def test_location_status_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert locations.location_status("nonexistent") == "missing"


def test_location_status_partial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tiles_v2_foo").mkdir()
    (tmp_path / "tiles_v2_foo" / "panorama.glb").touch()
    (tmp_path / "tiles_v2_foo" / "details").mkdir()
    (tmp_path / "tiles_v2_foo" / "details" / "outer.glb").touch()
    # closeup + inner chybí
    assert locations.location_status("foo") == "partial"


def test_location_status_ready(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "tiles_v2_foo"
    (base / "details").mkdir(parents=True)
    (base / "panorama.glb").touch()
    for s in ("outer", "closeup", "inner"):
        (base / "details" / f"{s}.glb").touch()
    assert locations.location_status("foo") == "ready"


def test_list_locations_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Dvě lokace: jedna ready, jedna partial
    for slug in ("alpha", "beta"):
        (tmp_path / f"tiles_v2_{slug}" / "details").mkdir(parents=True)
        (tmp_path / f"tiles_v2_{slug}" / "panorama.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "outer.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "closeup.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "inner.glb").touch()
    # Manifest s label pro alpha
    import json as _json
    (tmp_path / "tiles_v2_alpha" / "manifest.json").write_text(
        _json.dumps({"region": {"slug": "alpha", "label": "Alpha Village"}}))

    result = locations.list_locations()
    by_slug = {r["slug"]: r for r in result}
    assert set(by_slug) == {"alpha", "beta"}
    assert by_slug["alpha"]["status"] == "ready"
    assert by_slug["alpha"]["label"] == "Alpha Village"
    assert by_slug["beta"]["status"] == "partial"
    assert by_slug["beta"]["label"] == "beta"   # fallback = slug
    assert by_slug["alpha"]["has_panorama"] is True
    assert by_slug["beta"]["has_outer"] is False
