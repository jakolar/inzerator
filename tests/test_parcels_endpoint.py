import json
import urllib.request
import urllib.error


def test_parcels_returns_array_of_parcels():
    """Parcels endpoint vrací JSON pole {id, label, use_code, use_label, area_m2, ring_local}."""
    url = (
        "http://127.0.0.1:8080/api/parcels"
        "?gcx=-547700&gcy=-1107700&radius=2000"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read())
    assert isinstance(data, list)
    assert len(data) > 0, "Hnojice should have parcels"
    first = data[0]
    for key in ("id", "label", "use_code", "use_label", "area_m2", "ring_local"):
        assert key in first, f"missing key {key} in parcel response"
    assert isinstance(first["ring_local"], list)
    assert len(first["ring_local"]) >= 3, "ring should have ≥3 vertices"
    for vertex in first["ring_local"]:
        assert len(vertex) == 3, "each vertex is [lx, lz, terrain_y]"


def test_parcels_missing_params_returns_400():
    """Missing gcx/gcy → 400."""
    url = "http://127.0.0.1:8080/api/parcels?radius=2000"
    try:
        urllib.request.urlopen(url, timeout=10)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 400
