import json
import locations
import pytest
from unittest.mock import patch, MagicMock


def test_module_imports():
    assert locations.STEP_NAMES == ("panorama", "sm5", "outer", "closeup", "inner", "compress")
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
    assert locations.expected_glb("hnojice", "sm5") == Path("tiles_v2_hnojice/.sm5_ok")
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
    (base / ".compress_ok").touch()
    assert locations.location_status("foo") == "ready"


def test_location_status_partial_when_compress_missing(tmp_path, monkeypatch):
    """All 4 glb present but no .compress_ok → still partial."""
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "tiles_v2_foo"
    (base / "details").mkdir(parents=True)
    (base / "panorama.glb").touch()
    for s in ("outer", "closeup", "inner"):
        (base / "details" / f"{s}.glb").touch()
    assert locations.location_status("foo") == "partial"


def test_list_locations_scan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Dvě lokace: jedna ready, jedna partial
    for slug in ("alpha", "beta"):
        (tmp_path / f"tiles_v2_{slug}" / "details").mkdir(parents=True)
        (tmp_path / f"tiles_v2_{slug}" / "panorama.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "outer.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "closeup.glb").touch()
    (tmp_path / "tiles_v2_alpha" / "details" / "inner.glb").touch()
    (tmp_path / "tiles_v2_alpha" / ".compress_ok").touch()
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
    assert result[0] == {
        "kind": "address",
        "label": "č.p. 136, 78501 Hnojice",
        "sjtsk_cx": -547980.76,
        "sjtsk_cy": -1107944.18,
        "obec": "Hnojice",
    }
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
    """User types 'Fugnerova 355/16 Decin' (no diacritics). Numeric token
    '355/16' makes server fetch broad, Python-side ASCII-fold filter
    must accept 'Fügnerova … 40502 Děčín' as a match."""
    fake_features = [
        # The target — should be returned
        {"attributes": {"adresa": "Fügnerova 355/16, 40502 Děčín I-Děčín, Děčín"},
         "geometry": {"x": -750000.0, "y": -960000.0}},
        # Same parcel-number elsewhere — should be filtered out
        {"attributes": {"adresa": "Riegrova 355/16, 25001 Brandýs nad Labem"},
         "geometry": {"x": -740000.0, "y": -1040000.0}},
    ]
    with patch("locations.urlopen", return_value=_mock_ruian_response(fake_features)):
        result = locations.ruian_search("Fugnerova 355/16 Decin")
    # Only the Děčín hit survives ASCII-fold filter (Fugnerova + 355/16 + Decin)
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


def test_enqueue_creates_job_with_5_steps(clean_jobs):
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
    """Retry odebere `fail` stepy zpět na `pending` a vrátí job_id do fronty."""
    job_id = locations.enqueue_job("t", "T", 0.0, 0.0)
    # Simuluj že job už proběhl a closeup selhal:
    locations.JOB_QUEUE.clear()
    job = locations.JOBS[job_id]
    job["steps"][0]["state"] = "ok"      # panorama
    job["steps"][1]["state"] = "ok"      # sm5 (NEW)
    job["steps"][2]["state"] = "ok"      # outer
    job["steps"][3]["state"] = "fail"    # closeup
    job["steps"][3]["error"] = "test error"
    job["steps"][4]["state"] = "pending" # inner stayed
    assert locations.retry_job(job_id) is True
    assert locations.JOB_QUEUE == [job_id]
    # Failed se reset; ok zůstane (worker je preskočí přes existenci .glb)
    assert job["steps"][3]["state"] == "pending"
    assert job["steps"][3]["error"] is None
    assert job["steps"][0]["state"] == "ok"   # ok zůstane


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


def test_cmd_for_panorama():
    cmd = locations.cmd_for("panorama", "test", -547700.0, -1107700.0)
    assert cmd[0] == "python3"
    assert "gen_panorama.py" in cmd[1]
    assert "--region" in cmd
    assert "test" in cmd
    # --center-sjtsk používá `=` syntax kvůli negativním číslům + argparse
    assert any(arg.startswith("--center-sjtsk=") for arg in cmd)


def test_cmd_for_inner_has_step_0_5():
    """Inner detail = --step 0.5 (jemnější než stávající Hnojice manifest)."""
    cmd = locations.cmd_for("inner", "test", -547700.0, -1107700.0)
    cmd_str = " ".join(cmd)
    assert "--slug inner" in cmd_str
    assert "--step 0.5" in cmd_str
    assert "--half 500" in cmd_str
    assert "--fade-to closeup" in cmd_str


def test_cmd_for_outer_closeup_steps():
    outer = " ".join(locations.cmd_for("outer", "x", 0.0, 0.0))
    closeup = " ".join(locations.cmd_for("closeup", "x", 0.0, 0.0))
    assert "--half 2500" in outer and "--step 2.5" in outer and "--fade-to panorama" in outer
    assert "--half 1500" in closeup and "--step 1.5" in closeup and "--fade-to outer" in closeup


def test_worker_runs_job_to_completion(tmp_path, monkeypatch, clean_jobs):
    """Mock subprocess.Popen, aby vytvořil expected .glb. Worker projde
    všech 5 stepů → state ok pro každý."""
    monkeypatch.chdir(tmp_path)

    def fake_sm5(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "sm5")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_sm5_download", fake_sm5)

    def fake_compress(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "compress")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_compress", fake_compress)

    class FakeProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 0
        def communicate(self, timeout=None):
            # Najdi --region a --slug v cmd, vytvoř expected .glb
            args = self.cmd
            region = args[args.index("--region") + 1]
            slug_step = "panorama"
            if "--slug" in args:
                slug_step = args[args.index("--slug") + 1]
            out_path = locations.expected_glb(region, slug_step)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.touch()
            return "fake stdout", ""
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): pass

    monkeypatch.setattr(locations.subprocess, "Popen", FakeProc)

    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    # Spustit worker jednou (single-iteration helper)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert all(s["state"] == "ok" for s in job["steps"]), \
        f"states: {[(s['name'], s['state']) for s in job['steps']]}"


def test_worker_skips_existing_glb(tmp_path, monkeypatch, clean_jobs):
    """Pokud panorama.glb + .sm5_ok už existují, oba stepy → skipped."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tiles_v2_test").mkdir()
    (tmp_path / "tiles_v2_test" / "panorama.glb").touch()
    (tmp_path / "tiles_v2_test" / ".sm5_ok").touch()

    def fake_sm5(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "sm5")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_sm5_download", fake_sm5)

    def fake_compress(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "compress")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_compress", fake_compress)

    class FakeProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 0
        def communicate(self, timeout=None):
            args = self.cmd
            region = args[args.index("--region") + 1]
            slug_step = args[args.index("--slug") + 1] if "--slug" in args else "panorama"
            out_path = locations.expected_glb(region, slug_step)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.touch()
            return "", ""
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): pass

    monkeypatch.setattr(locations.subprocess, "Popen", FakeProc)
    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert job["steps"][0]["state"] == "skipped"   # panorama
    assert job["steps"][1]["state"] == "skipped"   # sm5
    assert all(s["state"] == "ok" for s in job["steps"][2:])


def test_worker_failure_stops_loop(tmp_path, monkeypatch, clean_jobs):
    """Pokud subprocess vrátí non-zero, step → fail, loop break,
    zbývající stepy zůstanou pending."""
    monkeypatch.chdir(tmp_path)

    def fake_sm5(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "sm5")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_sm5_download", fake_sm5)

    def fake_compress(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "compress")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_compress", fake_compress)

    class FailingProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 1
        def communicate(self, timeout=None):
            return "", "Traceback (most recent call last):\n  File 'foo'\nValueError: bad"
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): pass

    monkeypatch.setattr(locations.subprocess, "Popen", FailingProc)
    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    # panorama (step[0]) fails first — sm5 mock never invoked since panorama
    # is the first step and FailingProc fires on it
    assert job["steps"][0]["state"] == "fail"
    assert "ValueError" in job["steps"][0]["error"]
    assert all(s["state"] == "pending" for s in job["steps"][1:])


def test_worker_unlinks_partial_glb_on_failure(tmp_path, monkeypatch, clean_jobs):
    """Subprocess fails AFTER writing partial .glb → file must be deleted so
    retry doesn't see it as skipped."""
    monkeypatch.chdir(tmp_path)

    def fake_sm5(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "sm5")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_sm5_download", fake_sm5)

    def fake_compress(job, log_path):
        sentinel = locations.expected_glb(job["slug"], "compress")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        return True
    monkeypatch.setattr(locations, "_do_compress", fake_compress)

    class PartialThenFailProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 1
        def communicate(self, timeout=None):
            args = self.cmd
            region = args[args.index("--region") + 1]
            slug_step = args[args.index("--slug") + 1] if "--slug" in args else "panorama"
            out_path = locations.expected_glb(region, slug_step)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.touch()  # partial write before crash
            return "", "ValueError: simulated crash"
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): pass

    monkeypatch.setattr(locations.subprocess, "Popen", PartialThenFailProc)
    job_id = locations.enqueue_job("test", "T", 0.0, 0.0)
    locations._run_one_job_for_test()
    job = locations.JOBS[job_id]
    assert job["steps"][0]["state"] == "fail"
    assert not locations.expected_glb("test", "panorama").exists(), \
        "partial .glb must be deleted on failure so retry redoes the step"
