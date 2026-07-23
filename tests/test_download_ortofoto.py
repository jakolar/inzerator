"""Unit tests for download_ortofoto bulk-archive shortcut (no network, no Elements)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import download_ortofoto


@pytest.fixture
def _in_tmp_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_bulk(root: Path, code: str):
    d = root / f"ortofoto_{code}"
    d.mkdir(parents=True)
    (d / f"WRTO24.2024.{code}.jpg").write_bytes(b"JPEGDATA")
    (d / f"WRTO24.2024.{code}.jgw").write_bytes(b"WORLD")
    (d / f"WRTO24.2024.{code}.TM33N.jgw").write_bytes(b"TM33")
    return d


def test_copies_from_bulk_instead_of_downloading(_in_tmp_cwd, tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    _make_bulk(bulk, "FAKE01")
    monkeypatch.setattr(download_ortofoto, "BULK_ORTOFOTO_DIR", bulk)
    # Any feed lookup means the bulk shortcut was skipped — make it explode.
    monkeypatch.setattr(download_ortofoto, "find_dataset_feed_url",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network reached")))

    out = download_ortofoto.download("FAKE01", Path("cache"))

    assert out == Path("cache/ortofoto_FAKE01/WRTO24.2024.FAKE01.jpg")
    assert out.read_bytes() == b"JPEGDATA"
    assert out.with_suffix(".jgw").read_bytes() == b"WORLD"
    # sidecar TM variants come along too (bit-for-bit same as a real download)
    assert (out.parent / "WRTO24.2024.FAKE01.TM33N.jgw").exists()


def test_bulk_miss_falls_through_to_feed(_in_tmp_cwd, tmp_path, monkeypatch):
    bulk = tmp_path / "bulk"
    bulk.mkdir()  # present but no sheet

    monkeypatch.setattr(download_ortofoto, "BULK_ORTOFOTO_DIR", bulk)

    class _Boom(Exception):
        pass

    monkeypatch.setattr(download_ortofoto, "find_dataset_feed_url",
                        lambda *a, **k: (_ for _ in ()).throw(_Boom("feed reached")))
    with pytest.raises(_Boom):
        download_ortofoto.download("NOPE99", Path("cache"))


def test_existing_cache_pair_short_circuits_before_bulk(_in_tmp_cwd, tmp_path, monkeypatch):
    # A cached jpg+jgw pair must win before we even look at the bulk archive.
    cache = Path("cache/ortofoto_HAVE01")
    cache.mkdir(parents=True)
    (cache / "x.jpg").write_bytes(b"CACHED")
    (cache / "x.jgw").write_bytes(b"W")

    def _boom(*a, **k):
        raise AssertionError("bulk should not be consulted")

    monkeypatch.setattr(download_ortofoto, "_copy_from_bulk", _boom)
    out = download_ortofoto.download("HAVE01", Path("cache"))
    assert out.read_bytes() == b"CACHED"
