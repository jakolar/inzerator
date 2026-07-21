"""POST /api/estimate — needs server.py running on :8080 (repo convention)."""
import json
import os
import urllib.request

BASE = os.environ.get("INZERATOR_TEST_BASE", "http://localhost:8080")

def _post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def test_estimate_valid_polygon():
    poly = [[17.2215, 49.7175], [17.2300, 49.7175], [17.2258, 49.7230]]
    status, body = _post("/api/estimate", {"polygon": poly})
    assert status == 200
    assert body["inner_half"] == 500.0
    assert body["sheets_total"] >= 1
    assert 0 <= body["sheets_cached"] <= body["sheets_total"]

def test_estimate_rejects_garbage():
    status, body = _post("/api/estimate", {"polygon": [[1, 2]]})
    assert status == 400 and "polygon" in body["error"]
