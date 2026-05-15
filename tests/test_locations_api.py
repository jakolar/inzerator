"""Integration testy proti běžícímu serveru na :8080.
Tyto testy předpokládají, že server.py běží — spustit:
    python3 server.py &

Cleanup: testy používají test-only slug 'test-' prefix, takže nezasahují
do skutečných lokací. Po skončení manuálně:
    rm -rf tiles_v2_test-* cache/jobs/
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8080"


def _get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as r:
        return r.status, json.loads(r.read())


def _post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read()) if r.length else {}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()) if e.length else {}


def test_locations_endpoint_returns_list():
    status, data = _get("/api/locations")
    assert status == 200
    assert isinstance(data, list)


def test_create_job_invalid_slug_400():
    status, _ = _post("/api/jobs", {
        "slug": "Bad Slug!", "label": "X", "cx": -500000.0, "cy": -1100000.0,
    })
    assert status == 400


def test_create_job_missing_params_400():
    status, _ = _post("/api/jobs", {"slug": "test-ok"})
    assert status == 400


def test_ruian_search_empty_query_returns_empty():
    status, data = _get("/api/ruian/search?q=")
    assert status == 200
    assert data == []


def test_resume_from_disk_missing_manifest_400():
    """Resume bez panorama.glb / manifest.json → 400."""
    status, data = _post("/api/jobs", {"slug": "nonexistent-partial", "resume_from_disk": True})
    assert status == 400
    assert "manifest" in data.get("error", "").lower()
