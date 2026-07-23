"""Download DMPOK GeoTIFF for given SM5 map codes.

ČÚZK distributes DMPOK directly as raster (TIFF) — no local rasterization needed.
~58 MB ZIP per SM5 list.

Usage: python download_tiff.py --code OLOM80
       python download_tiff.py --code OLOM80 OLOM81
"""
import argparse
import os
import shutil
import zipfile
from pathlib import Path
import requests

DMPOK_TIFF_BASE = "https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514"
# Full-ČR DMPOK bulk pull (bulk_dmpok.py) — same dmpok_tiff_<CODE>/<CODE>.tif
# layout as the cache. When mounted, copy from here instead of re-downloading
# the ZIP from ČÚZK openzu (slow + flaky). Override / disable via env.
BULK_DMPOK_DIR = Path(os.environ.get("BULK_DMPOK_DIR", "/Volumes/Elements/cuzk-bulk"))


def _copy_from_bulk(code: str, out_dir: Path, out_tif: Path) -> bool:
    """Copy a sheet from the local bulk archive if present. Returns True on
    success. Copy (not symlink) so the cache survives the external drive
    being unmounted."""
    bulk_tif = BULK_DMPOK_DIR / f"dmpok_tiff_{code}" / f"{code}.tif"
    if not bulk_tif.exists():
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bulk_tif, out_tif)
    bulk_tfw = bulk_tif.with_suffix(".tfw")
    if bulk_tfw.exists():
        shutil.copy2(bulk_tfw, out_tif.with_suffix(".tfw"))
    print(f"  Copied {out_tif} from bulk archive ({out_tif.stat().st_size // 1024 // 1024} MB)")
    return True


def download_tiff(code: str) -> Path:
    out_dir = Path(f"cache/dmpok_tiff_{code}")
    out_tif = out_dir / f"{code}.tif"
    if out_tif.exists():
        print(f"  {out_tif} cached")
        return out_tif

    if _copy_from_bulk(code, out_dir, out_tif):
        return out_tif

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = Path(f"cache/dmpok_tiff_{code}.zip")
    if not zip_path.exists() or zip_path.stat().st_size < 1_000_000:
        url = f"{DMPOK_TIFF_BASE}/{code}.zip"
        print(f"  Downloading {url}")
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r  {done * 100 // total}% ({done // 1024 // 1024} MB)", end="")
        print()

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)

    # ČÚZK ZIP may use different file casing / extension; normalize
    tifs = list(out_dir.glob("*.tif")) + list(out_dir.glob("*.TIF")) + list(out_dir.glob("*.tiff"))
    if not tifs:
        raise RuntimeError(f"No TIFF found in {zip_path}")
    if tifs[0].name != f"{code}.tif":
        tifs[0].rename(out_tif)
        # also rename sidecar tfw if present
        tfws = list(out_dir.glob("*.tfw")) + list(out_dir.glob("*.TFW"))
        if tfws and tfws[0].name != f"{code}.tfw":
            tfws[0].rename(out_dir / f"{code}.tfw")

    print(f"  Wrote {out_tif} ({out_tif.stat().st_size // 1024 // 1024} MB)")
    return out_tif


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", nargs="+", required=True, help="SM5 codes (e.g., OLOM80 OLOM81)")
    args = ap.parse_args()
    for code in args.code:
        download_tiff(code)


if __name__ == "__main__":
    main()
