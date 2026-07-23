"""Unit tests for download_tiff bulk-archive shortcut (no network, no Elements)."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import download_tiff


@pytest.fixture
def _in_tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_bulk(root: Path, code: str, tif=b"TIFDATA", tfw=b"WORLD"):
    d = root / f"dmpok_tiff_{code}"
    d.mkdir(parents=True)
    (d / f"{code}.tif").write_bytes(tif)
    if tfw is not None:
        (d / f"{code}.tfw").write_bytes(tfw)
    return d


def test_copies_from_bulk_instead_of_downloading(_in_tmp_cwd, tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    _make_bulk(bulk, "FAKE01", tif=b"REALTIF", tfw=b"WORLDFILE")
    monkeypatch.setattr(download_tiff, "BULK_DMPOK_DIR", bulk)
    # Any network attempt is a failure: bulk hit must return before requests.
    monkeypatch.setattr(download_tiff, "requests", None)

    out = download_tiff.download_tiff("FAKE01")

    assert out == Path("cache/dmpok_tiff_FAKE01/FAKE01.tif")
    assert out.read_bytes() == b"REALTIF"
    assert out.with_suffix(".tfw").read_bytes() == b"WORLDFILE"


def test_bulk_miss_falls_through_to_download(_in_tmp_cwd, tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    bulk.mkdir()  # present but empty → no sheet
    monkeypatch.setattr(download_tiff, "BULK_DMPOK_DIR", bulk)
    # Sentinel: reaching the network path raises a recognizable error.
    class _Boom(Exception):
        pass

    def _no_net(*a, **k):
        raise _Boom("network reached")

    monkeypatch.setattr(download_tiff.requests, "get", _no_net)
    with pytest.raises(_Boom):
        download_tiff.download_tiff("NOPE99")


def test_tfw_absent_in_bulk_is_ok(_in_tmp_cwd, tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    _make_bulk(bulk, "NOTFW1", tfw=None)
    monkeypatch.setattr(download_tiff, "BULK_DMPOK_DIR", bulk)

    out = download_tiff.download_tiff("NOTFW1")

    assert out.exists()
    assert not out.with_suffix(".tfw").exists()
