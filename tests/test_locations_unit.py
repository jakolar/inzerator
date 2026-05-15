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
