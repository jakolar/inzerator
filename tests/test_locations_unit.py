import json
from pathlib import Path

import locations
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _reset_ku_cache():
    """Module-level KÚ cache leaks between tests; reset around every test."""
    locations._KU_CACHE = None
    yield
    locations._KU_CACHE = None


def test_module_imports():
    # After v2 viewer retirement (2026-06), pipeline trimmed to two steps.
    # Heightfield viewer is the only consumer; sm5 stays separate so a flaky
    # ČÚZK download can be retried without re-encoding the heightfield.
    assert locations.STEP_NAMES == ("sm5", "heightfield")
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
    assert locations.expected_glb("hnojice", "sm5") == Path("tiles_v2_hnojice/.sm5_ok")
    assert locations.expected_glb("hnojice", "heightfield") == \
        Path("tiles_v2_hnojice/heightfield/manifest.json")
    # Steps from the retired v2 pipeline raise — guard against accidental
    # re-introduction without updating STEP_NAMES.
    import pytest as _pytest
    with _pytest.raises(ValueError):
        locations.expected_glb("hnojice", "panorama")


def test_location_status_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert locations.location_status("nonexistent") == "missing"


def test_location_status_partial(tmp_path, monkeypatch):
    """Directory exists but heightfield manifest doesn't → partial
    (in-flight or interrupted gen)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tiles_v2_foo").mkdir()
    (tmp_path / "tiles_v2_foo" / ".sm5_ok").touch()
    assert locations.location_status("foo") == "partial"


def test_location_status_ready(tmp_path, monkeypatch):
    """heightfield/manifest.json present → ready, regardless of sm5 sentinel."""
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "tiles_v2_foo"
    (base / "heightfield").mkdir(parents=True)
    (base / "heightfield" / "manifest.json").write_text("{}")
    assert locations.location_status("foo") == "ready"


def test_list_locations_scan(tmp_path, monkeypatch):
    """Two locations: alpha ready (heightfield manifest exists), beta partial.
    label resolved from location.json (current) or legacy v2 manifest."""
    monkeypatch.chdir(tmp_path)
    import json as _json
    for slug in ("alpha", "beta"):
        (tmp_path / f"tiles_v2_{slug}").mkdir(parents=True)
    # alpha: ready + label via location.json (current persistence path)
    (tmp_path / "tiles_v2_alpha" / "heightfield").mkdir()
    (tmp_path / "tiles_v2_alpha" / "heightfield" / "manifest.json").write_text("{}")
    (tmp_path / "tiles_v2_alpha" / "location.json").write_text(
        _json.dumps({"slug": "alpha", "label": "Alpha Village",
                     "cx": -547700, "cy": -1107700}))
    # beta: partial + label via legacy v2 manifest fallback
    (tmp_path / "tiles_v2_beta" / "manifest.json").write_text(
        _json.dumps({"region": {"slug": "beta", "label": "Beta Hamlet"}}))

    result = locations.list_locations()
    by_slug = {r["slug"]: r for r in result}
    assert set(by_slug) == {"alpha", "beta"}
    assert by_slug["alpha"]["status"] == "ready"
    assert by_slug["alpha"]["label"] == "Alpha Village"
    assert by_slug["alpha"]["has_heightfield"] is True
    assert by_slug["beta"]["status"] == "partial"
    assert by_slug["beta"]["label"] == "Beta Hamlet"
    assert by_slug["beta"]["has_heightfield"] is False


def test_persist_location_meta_writes_label(tmp_path, monkeypatch):
    """enqueue path: location.json with {slug, label, cx, cy, created_at}
    after _persist_location_meta. Atomic via .tmp + replace."""
    monkeypatch.chdir(tmp_path)
    locations._persist_location_meta("foo", "Foo Village", -100.0, -200.0)
    meta_path = tmp_path / "tiles_v2_foo" / "location.json"
    assert meta_path.is_file()
    import json as _json
    data = _json.loads(meta_path.read_text())
    assert data["slug"] == "foo"
    assert data["label"] == "Foo Village"
    assert data["cx"] == -100.0
    assert data["cy"] == -200.0
    assert "created_at" in data


def _mock_ruian_response(features):
    """Vyrobí MagicMock context manager s .read() vracejícím JSON."""
    payload = json.dumps({"features": features}).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: payload))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_ruian_search_maps_features():
    fake_features = [
        {"attributes": {"adresa": "č.p. 136, 78501 Hnojice", "psc": 78501},
         "geometry": {"x": -547980.76, "y": -1107944.18}},
        {"attributes": {"adresa": "Jemnice 8, 59253 Strážek", "psc": 59253},
         "geometry": {"x": -626541.82, "y": -1130200.0}},
    ]
    with patch("locations.urlopen", return_value=_mock_ruian_response(fake_features)):
        result = locations.ruian_search("Hnojice")
    # "Hnojice" has no numeric token → no parcel path, no fold filter.
    # Both fake features pass through.
    assert len(result) == 2
    r0 = result[0]
    assert r0["kind"] == "address"
    assert r0["label"] == "č.p. 136, 78501 Hnojice"
    assert r0["sjtsk_cx"] == -547980.76
    assert r0["sjtsk_cy"] == -1107944.18
    assert r0["obec"] == "Hnojice"
    # wgs_lat/lon come from pyproj — Hnojice center ~ (49.72, 17.22)
    assert 49.6 < r0["wgs_lat"] < 49.8
    assert 17.1 < r0["wgs_lon"] < 17.3
    assert result[1]["obec"] == "Strážek"


def test_ruian_search_empty():
    with patch("locations.urlopen", return_value=_mock_ruian_response([])):
        result = locations.ruian_search("nonsense")
    assert result == []


def test_ruian_search_escapes_quotes_in_literal_path():
    """When input has no numeric token, address search hits the literal-LIKE
    path. Single-quote in input must be escaped to '' or RUIAN errors."""
    captured_url = []

    def fake_urlopen(req, timeout=None):
        captured_url.append(req.get_full_url() if hasattr(req, "get_full_url") else req)
        return _mock_ruian_response([])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        # No digit anywhere → literal LIKE path, no parcel path.
        locations.ruian_search("O'Hara")

    assert len(captured_url) == 1
    sent = captured_url[0]
    # urlencode turns `''` into `%27%27`
    assert "O%27%27Hara" in sent


def test_ruian_search_diacritic_tolerance():
    """User types 'Fugnerova 355/16 Decin' (no diacritics) — RUIAN has
    'Fügnerova/Děčín' with diacritics. Phase 1 (literal LIKE on all
    tokens) returns 0 (LIKE is diacritic-sensitive). Phase 2 (broad LIKE
    on numeric token only) returns candidates which Python fold-filters."""
    fake_features = [
        {"attributes": {"adresa": "Fügnerova 355/16, 40502 Děčín I-Děčín, Děčín"},
         "geometry": {"x": -750000.0, "y": -960000.0}},
        {"attributes": {"adresa": "Riegrova 355/16, 25001 Brandýs nad Labem"},
         "geometry": {"x": -740000.0, "y": -1040000.0}},
    ]
    addr_calls = [0]

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, "get_full_url") else req
        # Parcel/KÚ layers: empty (no parcel candidates / no KÚ match).
        if "MapServer/0" in url or "MapServer/7" in url:
            return _mock_ruian_response([])
        # Address layer (MapServer/1): phase 1 (multi-token literal LIKE)
        # returns empty (server LIKE is diacritic-sensitive); phase 2 (broad
        # numeric LIKE) returns the candidates for Python fold-filter.
        if "MapServer/1" in url:
            addr_calls[0] += 1
            if addr_calls[0] == 1:
                return _mock_ruian_response([])
            return _mock_ruian_response(fake_features)
        return _mock_ruian_response([])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        result = locations.ruian_search("Fugnerova 355/16 Decin")

    addresses = [h for h in result if h["kind"] == "address"]
    assert len(addresses) == 1
    assert addresses[0]["label"] == "Fügnerova 355/16, 40502 Děčín I-Děčín, Děčín"


def test_ruian_search_parcel_hits_emitted():
    """When input has a parcel-format token (N/N), the parcel layer is
    queried in addition to addresses and matching features are returned
    with kind='parcel'. Mock returns the SAME features for both calls;
    we just verify the parcel branch produces a kind='parcel' entry."""
    fake_features = [
        {"attributes": {"cisloparcely": "355/16",
                        "katastralniuzemicisloparcely": "Libhošť 355/16",
                        "vymeraparcely": 485.0},
         "geometry": {"x": -540000.0, "y": -1100000.0}},
    ]
    with patch("locations.urlopen", return_value=_mock_ruian_response(fake_features)):
        result = locations.ruian_search("355/16")
    parcels = [h for h in result if h["kind"] == "parcel"]
    assert len(parcels) == 1
    assert "Libhošť 355/16" in parcels[0]["label"]
    assert parcels[0]["obec"] == "Libhošť"


def test_ruian_search_network_error_raises():
    from urllib.error import URLError
    with patch("locations.urlopen", side_effect=URLError("connection refused")):
        with pytest.raises(locations.RuianUnavailable):
            locations.ruian_search("Hnojice")


def test_resolve_ku_codes_diacritic_fold():
    """KÚ cache lookup is diacritic-tolerant: user types 'Stribrnice', cache
    holds 'Stříbrnice' with diacritics, fold match returns kod 756091."""
    locations._KU_CACHE = [
        (756091, "Stříbrnice", "stribrnice"),
        (719790, "Petrov nad Desnou", "petrov nad desnou"),
        (777001, "Stříbrnice u Uherského Hradiště", "stribrnice u uherskeho hradiste"),
    ]
    assert sorted(locations._resolve_ku_codes(["Stribrnice"])) == [756091, 777001]
    assert locations._resolve_ku_codes(["Stribrnice", "Uherskeho"]) == [777001]
    assert locations._resolve_ku_codes(["Nonexistent"]) == []
    assert locations._resolve_ku_codes([]) == []


def test_fetch_all_ku_paginates():
    """_fetch_all_ku must paginate when ČÚZK returns full pages of size 2000."""
    page1 = [{"attributes": {"kod": i, "nazev": f"KU{i}"}} for i in range(2000)]
    page2 = [{"attributes": {"kod": i, "nazev": f"KU{i}"}} for i in range(2000, 2500)]
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url()
        calls.append(url)
        if "resultOffset=2000" in url:
            return _mock_ruian_response(page2)
        return _mock_ruian_response(page1)

    with patch("locations.urlopen", side_effect=fake_urlopen):
        entries = locations._fetch_all_ku()

    assert len(entries) == 2500
    assert entries[0] == (0, "KU0")
    assert entries[-1] == (2499, "KU2499")
    assert len(calls) == 2


def test_search_parcels_uses_ku_filter_in_server_query():
    """User types '350/2 Stribrnice' — server-side query must include
    katastralniuzemi IN (756091) so ČÚZK's 200-row cap returns the
    Stříbrnice parcel directly (was: lost in the cross-ČR haystack)."""
    locations._KU_CACHE = [(756091, "Stříbrnice", "stribrnice")]
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url()
        captured_urls.append(url)
        return _mock_ruian_response([
            {"attributes": {"cisloparcely": "350/2",
                            "katastralniuzemicisloparcely": "Stříbrnice 350/2",
                            "vymeraparcely": 1910.0},
             "geometry": {"x": -550000.0, "y": -1100000.0}},
        ])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        result = locations.ruian_search("350/2 Stribrnice")

    parcels = [h for h in result if h["kind"] == "parcel"]
    assert len(parcels) == 1
    assert "Stříbrnice 350/2" in parcels[0]["label"]
    parcel_urls = [u for u in captured_urls if "MapServer/0" in u]
    assert len(parcel_urls) == 1
    # urlencode of 'katastralniuzemi IN (756091)' → escaped form
    assert "katastralniuzemi+IN+%28756091%29" in parcel_urls[0]


def test_enqueue_job_propagates_force_recompress():
    """force_recompress flag flows into job dict so the worker can branch."""
    locations.JOBS.clear(); locations.JOB_QUEUE.clear()
    job_id = locations.enqueue_job("slug-a", "Slug A", -100.0, -200.0, force_recompress=True)
    assert job_id is not None
    assert locations.JOBS[job_id]["force_recompress"] is True
    locations.JOBS.clear(); locations.JOB_QUEUE.clear()


def test_enqueue_job_default_force_recompress_false():
    """Backwards-compat: callers that don't pass force_recompress get False."""
    locations.JOBS.clear(); locations.JOB_QUEUE.clear()
    job_id = locations.enqueue_job("slug-b", "Slug B", -100.0, -200.0)
    assert locations.JOBS[job_id]["force_recompress"] is False
    locations.JOBS.clear(); locations.JOB_QUEUE.clear()


@pytest.mark.skip(reason="v2 compress step retired 2026-06; _do_compress dead")
def test_do_compress_force_re_dracos_from_orig(tmp_path, monkeypatch):
    """force_recompress=True ignores `orig_path already exists` skip;
    re-Dracos from _orig_uncompressed/ and overwrites details/."""
    monkeypatch.chdir(tmp_path)
    slug = "force-x"
    region = tmp_path / f"tiles_v2_{slug}"
    (region / "details").mkdir(parents=True)
    (region / "_orig_uncompressed").mkdir()
    # Both orig and compressed exist (= location previously compressed).
    for name in ("outer", "closeup", "inner"):
        (region / "_orig_uncompressed" / f"{name}.glb").write_bytes(b"ORIG-" + name.encode())
        (region / "details" / f"{name}.glb").write_bytes(b"OLD-DRACO-" + name.encode())

    calls = []
    def fake_compress(src, dst):
        # meshopt has no per-LOD knob — single signature, no quant_pos
        calls.append((Path(src).name, Path(dst).name))
        Path(dst).write_bytes(b"NEW-MESHOPT-" + Path(src).read_bytes())

    fake_mod = type("M", (), {"compress": staticmethod(fake_compress)})
    monkeypatch.setitem(__import__("sys").modules, "meshopt_compress_glb", fake_mod)

    job = {"slug": slug, "force_recompress": True, "cancelled": False}
    ok = locations._do_compress(job, region / "log.txt")
    assert ok
    # All three details re-compressed (not skipped).
    assert {c[0] for c in calls} == {"outer.glb", "closeup.glb", "inner.glb"}
    # New Meshopt payload written into details/.
    for name in ("outer", "closeup", "inner"):
        assert (region / "details" / f"{name}.glb").read_bytes() == b"NEW-MESHOPT-ORIG-" + name.encode()


@pytest.mark.skip(reason="v2 compress step retired 2026-06")
def test_do_compress_force_encode_crash_keeps_orig(tmp_path, monkeypatch):
    """force_recompress: meshopt encode raises. The except path must NOT
    rename `_orig_uncompressed/<slug>.glb` away (it's the only lossless
    copy), so a future Recompress can still re-compress from it."""
    monkeypatch.chdir(tmp_path)
    slug = "force-crash"
    region = tmp_path / f"tiles_v2_{slug}"
    (region / "details").mkdir(parents=True)
    (region / "_orig_uncompressed").mkdir()
    (region / "_orig_uncompressed" / "outer.glb").write_bytes(b"LOSSLESS-ORIG")
    (region / "details" / "outer.glb").write_bytes(b"OLD-MESHOPT")

    def crashing_compress(src, dst):
        raise RuntimeError("simulated meshopt encode crash")
    fake_mod = type("M", (), {"compress": staticmethod(crashing_compress)})
    monkeypatch.setitem(__import__("sys").modules, "meshopt_compress_glb", fake_mod)

    job = {"slug": slug, "force_recompress": True, "cancelled": False}
    ok = locations._do_compress(job, region / "log.txt")
    assert ok is False
    # The lossless original MUST still be in _orig_uncompressed/ — otherwise
    # the next Recompress hits the "no orig to re-Draco from" guard and the
    # data is permanently unrecoverable without manual file rescue.
    assert (region / "_orig_uncompressed" / "outer.glb").exists()
    assert (region / "_orig_uncompressed" / "outer.glb").read_bytes() == b"LOSSLESS-ORIG"


@pytest.mark.skip(reason="v2 compress step retired 2026-06")
def test_do_compress_force_fails_without_orig(tmp_path, monkeypatch):
    """force_recompress with no _orig_uncompressed file: deployed glb stays
    intact (no data destruction). Step soft-skips that target rather than
    failing the whole compress (legacy regions may have a deployed but no
    backup glb, e.g. panorama from a manual Draco run before per-LOD
    compress landed)."""
    monkeypatch.chdir(tmp_path)
    slug = "force-y"
    region = tmp_path / f"tiles_v2_{slug}"
    (region / "details").mkdir(parents=True)
    # No _orig_uncompressed/ dir at all.
    (region / "details" / "outer.glb").write_bytes(b"EXISTING-DRACO")

    fake_mod = type("M", (), {"compress": staticmethod(lambda *a, **k: None)})
    monkeypatch.setitem(__import__("sys").modules, "meshopt_compress_glb", fake_mod)

    job = {"slug": slug, "force_recompress": True, "cancelled": False}
    ok = locations._do_compress(job, region / "log.txt")
    # Soft-skip: missing orig is non-fatal, step succeeds, deployed glb
    # untouched.
    assert ok is True
    assert (region / "details" / "outer.glb").read_bytes() == b"EXISTING-DRACO"


def test_ruian_search_kind_parcel_skips_addresses():
    """kind='parcel' calls only the parcel layer (MapServer/0), never
    MapServer/1. Even if input has tokens that would match an address."""
    locations._KU_CACHE = [(756091, "Stříbrnice", "stribrnice")]
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.get_full_url())
        return _mock_ruian_response([
            {"attributes": {"cisloparcely": "350/2",
                            "katastralniuzemicisloparcely": "Stříbrnice 350/2",
                            "vymeraparcely": 1910.0},
             "geometry": {"x": -550000.0, "y": -1100000.0}},
        ])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        result = locations.ruian_search("350/2 Stribrnice", kind="parcel")

    assert all(h["kind"] == "parcel" for h in result)
    assert any("MapServer/0" in u for u in captured_urls)
    assert not any("MapServer/1" in u for u in captured_urls)


def test_ruian_search_kind_address_skips_parcels():
    """kind='address' calls only MapServer/1, never MapServer/0 or KÚ layer 7."""
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.get_full_url())
        return _mock_ruian_response([
            {"attributes": {"adresa": "č.p. 47, 78501 Hnojice"},
             "geometry": {"x": -547980.0, "y": -1107944.0}},
        ])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        result = locations.ruian_search("350/2 Hnojice", kind="address")

    assert all(h["kind"] == "address" for h in result)
    assert any("MapServer/1" in u for u in captured_urls)
    assert not any("MapServer/0" in u for u in captured_urls)
    assert not any("MapServer/7" in u for u in captured_urls)


def test_ruian_search_kind_all_default():
    """Default kind='all' preserves the historical behaviour: both layers fire."""
    locations._KU_CACHE = [(756091, "Stříbrnice", "stribrnice")]
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.get_full_url())
        return _mock_ruian_response([])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        locations.ruian_search("350/2 Stribrnice")  # no kind arg

    assert any("MapServer/0" in u for u in captured_urls)
    assert any("MapServer/1" in u for u in captured_urls)


def test_search_parcels_empty_when_obec_unknown():
    """User types '350/2 Zizala' — no KÚ matches, return [] without
    hitting the parcel layer (better UX than ČR-wide fan-out)."""
    locations._KU_CACHE = [(756091, "Stříbrnice", "stribrnice")]
    captured_urls = []

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.get_full_url())
        return _mock_ruian_response([])

    with patch("locations.urlopen", side_effect=fake_urlopen):
        result = locations.ruian_search("350/2 Zizala")

    assert [h for h in result if h["kind"] == "parcel"] == []
    # No parcel-layer call was made (only addresses path).
    assert not any("MapServer/0" in u for u in captured_urls)


def test_resolve_sm5_codes_envelope_query():
    """Mock ČÚZK envelope query → return 2 MAPNOM codes."""
    payload = json.dumps({"features": [
        {"attributes": {"MAPNOM": "JESE44"}},
        {"attributes": {"MAPNOM": "JESE45"}},
    ]}).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: payload))
    cm.__exit__ = MagicMock(return_value=False)
    with patch("locations.urlopen", return_value=cm):
        codes = locations._resolve_sm5_codes(-813194.0, -1029466.0, half=2500)
    assert codes == ["JESE44", "JESE45"]


def test_resolve_sm5_codes_network_error():
    from urllib.error import URLError
    with patch("locations.urlopen", side_effect=URLError("nope")):
        with pytest.raises(locations.RuianUnavailable):
            locations._resolve_sm5_codes(0.0, 0.0)


@pytest.fixture
def clean_jobs():
    """Reset job state mezi testy."""
    with locations.JOB_LOCK:
        locations.JOBS.clear()
        locations.JOB_QUEUE.clear()
        locations.CURRENT_JOB = None
    yield
    with locations.JOB_LOCK:
        locations.JOBS.clear()
        locations.JOB_QUEUE.clear()
        locations.CURRENT_JOB = None


def test_enqueue_creates_job_with_step_set(clean_jobs):
    job_id = locations.enqueue_job("test1", "Test Village", -500000.0, -1100000.0)
    assert job_id in locations.JOBS
    job = locations.JOBS[job_id]
    assert job["slug"] == "test1"
    assert job["label"] == "Test Village"
    assert job["cx"] == -500000.0
    assert job["cy"] == -1100000.0
    assert [s["name"] for s in job["steps"]] == list(locations.STEP_NAMES)
    assert all(s["state"] == "pending" for s in job["steps"])
    assert locations.JOB_QUEUE == [job_id]


def test_enqueue_two_jobs_fifo(clean_jobs):
    a = locations.enqueue_job("a", "A", -500000.0, -1100000.0)
    b = locations.enqueue_job("b", "B", -500000.0, -1100000.0)
    assert locations.JOB_QUEUE == [a, b]


def test_enqueue_duplicate_slug_returns_none(clean_jobs):
    """Second enqueue with same slug while first is still queued → None."""
    first = locations.enqueue_job("dup-test", "first", 0.0, 0.0)
    second = locations.enqueue_job("dup-test", "second", 0.0, 0.0)
    assert first is not None
    assert second is None
    assert len(locations.JOB_QUEUE) == 1


def test_get_job_returns_none_for_unknown(clean_jobs):
    assert locations.get_job("nonexistent-uuid") is None


def test_list_active_jobs_queued(clean_jobs):
    a = locations.enqueue_job("a", "A", -500000.0, -1100000.0)
    b = locations.enqueue_job("b", "B", -500000.0, -1100000.0)
    active = locations.list_active_jobs()
    assert len(active) == 2
    assert active[0]["job_id"] == a
    assert active[0]["queue_position"] == 0
    assert active[1]["job_id"] == b
    assert active[1]["queue_position"] == 1


def test_retry_resets_failed_steps(clean_jobs):
    """retry_job flips `fail` steps back to `pending` and re-enqueues the job.
    `ok` steps stay `ok` (the worker will skip them via the on-disk
    sentinel / manifest existence check)."""
    job_id = locations.enqueue_job("t", "T", 0.0, 0.0)
    locations.JOB_QUEUE.clear()
    job = locations.JOBS[job_id]
    # STEP_NAMES = ("sm5", "heightfield") after the v2 retirement.
    job["steps"][0]["state"] = "ok"        # sm5
    job["steps"][1]["state"] = "fail"      # heightfield
    job["steps"][1]["error"] = "test error"
    assert locations.retry_job(job_id) is True
    assert locations.JOB_QUEUE == [job_id]
    assert job["steps"][1]["state"] == "pending"
    assert job["steps"][1]["error"] is None
    assert job["steps"][0]["state"] == "ok"


def test_retry_unknown_job_returns_false(clean_jobs):
    assert locations.retry_job("nonexistent") is False


def test_cancel_queued_removes_from_queue(clean_jobs):
    a = locations.enqueue_job("a", "A", 0.0, 0.0)
    b = locations.enqueue_job("b", "B", 0.0, 0.0)
    assert locations.cancel_job(a) is True
    assert locations.JOB_QUEUE == [b]
    assert locations.JOBS[a]["cancelled"] is True


def test_cancel_already_done_returns_false(clean_jobs):
    job_id = locations.enqueue_job("t", "T", 0.0, 0.0)
    locations.JOB_QUEUE.clear()
    for s in locations.JOBS[job_id]["steps"]:
        s["state"] = "ok"
    assert locations.cancel_job(job_id) is False


import os


@pytest.mark.skip(reason="v2 pipeline retired 2026-06; tests live until "
                         "cmd_for branches + gen_panorama.py move to TOBEDELETED")
def test_cmd_for_panorama():
    cmd = locations.cmd_for("panorama", "test", -547700.0, -1107700.0)
    assert cmd[0] == "python3"
    assert "gen_panorama.py" in cmd[1]
    assert "--region" in cmd
    assert "test" in cmd
    # --center-sjtsk používá `=` syntax kvůli negativním číslům + argparse
    assert any(arg.startswith("--center-sjtsk=") for arg in cmd)


@pytest.mark.skip(reason="v2 pipeline retired 2026-06")
def test_cmd_for_inner_has_step_0_5():
    """Inner detail = --step 0.5 (jemnější než stávající Hnojice manifest)."""
    cmd = locations.cmd_for("inner", "test", -547700.0, -1107700.0)
    cmd_str = " ".join(cmd)
    assert "--slug inner" in cmd_str
    assert "--step 0.5" in cmd_str
    assert "--half 500" in cmd_str
    assert "--fade-to closeup" in cmd_str


@pytest.mark.skip(reason="v2 pipeline retired 2026-06")
def test_cmd_for_outer_closeup_steps():
    outer = " ".join(locations.cmd_for("outer", "x", 0.0, 0.0))
    closeup = " ".join(locations.cmd_for("closeup", "x", 0.0, 0.0))
    assert "--half 2500" in outer and "--step 2.5" in outer and "--fade-to panorama" in outer
    assert "--half 1500" in closeup and "--step 1.5" in closeup and "--fade-to outer" in closeup


def _patch_sm5_sentinel(monkeypatch):
    """Helper: stub the in-proc SM5 step so it just writes .sm5_ok."""
    def fake_sm5(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "sm5")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_sm5_download", fake_sm5)


def _fake_heightfield_proc(success: bool, write_partial: bool = False):
    """Build a fake subprocess.Popen class that, when invoked with
    gen_heightfield.py args (--slug X), writes heightfield/manifest.json
    (or not) and returns the configured exit code."""
    class Proc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 0 if success else 1
        def communicate(self, timeout=None):
            args = self.cmd
            if "--slug" in args:
                slug = args[args.index("--slug") + 1]
                if success or write_partial:
                    out = locations.expected_glb(slug, "heightfield")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.touch()
            if success:
                return "fake stdout", ""
            return "", "Traceback (most recent call last):\nValueError: bad"
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): pass
    return Proc


def test_worker_runs_job_to_completion(tmp_path, monkeypatch, clean_jobs):
    """Worker walks all STEP_NAMES → ok. After v2 retirement the chain is
    sm5 (in-proc) → heightfield (subprocess gen_heightfield.py)."""
    monkeypatch.chdir(tmp_path)
    _patch_sm5_sentinel(monkeypatch)
    monkeypatch.setattr(locations.subprocess, "Popen",
                        _fake_heightfield_proc(success=True))

    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert all(s["state"] == "ok" for s in job["steps"]), \
        f"states: {[(s['name'], s['state']) for s in job['steps']]}"


def test_worker_skips_existing_artifacts(tmp_path, monkeypatch, clean_jobs):
    """If sentinels/manifests already exist on disk, the worker skips both
    steps (resume-from-disk semantics)."""
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "tiles_v2_test"
    (base / "heightfield").mkdir(parents=True)
    (base / ".sm5_ok").touch()
    (base / "heightfield" / "manifest.json").write_text("{}")

    _patch_sm5_sentinel(monkeypatch)
    monkeypatch.setattr(locations.subprocess, "Popen",
                        _fake_heightfield_proc(success=True))

    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert [s["state"] for s in job["steps"]] == ["skipped", "skipped"]


def test_worker_failure_stops_loop(tmp_path, monkeypatch, clean_jobs):
    """heightfield step returns non-zero → step fail. sm5 already ran ok."""
    monkeypatch.chdir(tmp_path)
    _patch_sm5_sentinel(monkeypatch)
    monkeypatch.setattr(locations.subprocess, "Popen",
                        _fake_heightfield_proc(success=False))

    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    # sm5 ok, heightfield fail.
    assert job["steps"][0]["state"] == "ok"
    assert job["steps"][1]["state"] == "fail"
    assert "ValueError" in job["steps"][1]["error"]


def test_worker_unlinks_partial_manifest_on_failure(tmp_path, monkeypatch, clean_jobs):
    """Subprocess writes the heightfield manifest, then crashes. Worker must
    delete the partial manifest so retry sees the step as not-yet-done
    (otherwise the resume-skip check would treat it as skipped/done)."""
    monkeypatch.chdir(tmp_path)
    _patch_sm5_sentinel(monkeypatch)
    monkeypatch.setattr(locations.subprocess, "Popen",
                        _fake_heightfield_proc(success=False, write_partial=True))

    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert job["steps"][1]["state"] == "fail"
    assert not locations.expected_glb("test", "heightfield").exists(), \
        "partial heightfield manifest must be deleted on failure"
