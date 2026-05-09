"""Download DMPOK GeoTIFF for given SM5 map codes.

ČÚZK distributes DMPOK directly as raster (TIFF) — no local rasterization needed.
~58 MB ZIP per SM5 list.

Usage: python download_tiff.py --code OLOM80
       python download_tiff.py --code OLOM80 OLOM81
"""
import argparse
import zipfile
from pathlib import Path
import requests

DMPOK_TIFF_BASE = "https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514"


def download_tiff(code: str) -> Path:
    out_dir = Path(f"cache/dmpok_tiff_{code}")
    out_tif = out_dir / f"{code}.tif"
    if out_tif.exists():
        print(f"  {out_tif} cached")
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
