"""One-shot profiler for the per-sheet pipeline.

Runs OUTSIDE the production downloader (no shared state, no lock) and
fetches a small batch into a tempdir, timing every stage to decide where
the wall-clock budget goes:

  download  → bytes from openzu.cuzk.gov.cz to local file
  unzip     → ZIP open + extractall
  rename    → normalise filenames to <CODE>.tif / <CODE>.tfw
  cleanup   → unlink the ZIP

Run two ways: with a `--target` switch you choose where bytes land —
external Elements (USB) vs the internal SSD — so you can isolate
network-bound vs disk-bound behaviour with the same code path.
"""
from __future__ import annotations
import argparse
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
import requests

DMPOK_BASE = "https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514"
UA = "inzerator/1.0; alacremex@gmail.com"
TIMEOUT = 600


def fetch_one(code: str, root: Path) -> dict:
    out_dir = root / f"dmpok_tiff_{code}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{code}.zip"
    out_tif = out_dir / f"{code}.tif"
    url = f"{DMPOK_BASE}/{code}.zip"

    t = {}
    t0 = time.monotonic()
    bytes_in = 0
    with requests.get(url, stream=True, timeout=TIMEOUT,
                      headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        # Separate "time to first byte" so we see network setup overhead.
        t["ttfb"] = time.monotonic() - t0
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    bytes_in += len(chunk)
    t["download"] = time.monotonic() - t0
    zip_size = zip_path.stat().st_size

    t1 = time.monotonic()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    t["unzip"] = time.monotonic() - t1

    t2 = time.monotonic()
    for ext in ("tif", "TIF", "tiff"):
        for p in out_dir.glob(f"*.{ext}"):
            if p.name != out_tif.name:
                p.rename(out_tif)
    for ext in ("tfw", "TFW"):
        for p in out_dir.glob(f"*.{ext}"):
            target = out_dir / f"{code}.tfw"
            if p != target:
                p.rename(target)
    t["rename"] = time.monotonic() - t2

    t3 = time.monotonic()
    zip_path.unlink(missing_ok=True)
    t["cleanup"] = time.monotonic() - t3

    t["total"] = time.monotonic() - t0
    t["mbps_download"] = (bytes_in / (1024 * 1024)) / t["download"]
    t["mb_zip"] = zip_size / (1024 * 1024)
    t["mb_tif"] = out_tif.stat().st_size / (1024 * 1024)
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="+",
                    default=["ZNOJ88", "ZNOJ89", "ZNOJ90",
                             "ZNOJ91", "ZNOJ92"],
                    help="MAPNOM codes to fetch (default: 5 from ZNOJ end)")
    ap.add_argument("--target", default="ssd",
                    choices=["ssd", "elements"],
                    help="ssd → /tmp (internal NVMe); "
                         "elements → /Volumes/Elements (USB external)")
    args = ap.parse_args()

    if args.target == "ssd":
        root = Path(tempfile.mkdtemp(prefix="dmpok-profile-",
                                     dir="/tmp"))
    else:
        root = Path("/Volumes/Elements/cuzk-bulk-profile")
        root.mkdir(parents=True, exist_ok=True)
    print(f"Target: {root}")

    rows = []
    grand = time.monotonic()
    for code in args.codes:
        print(f"  fetching {code}…", flush=True)
        try:
            row = fetch_one(code, root)
            row["code"] = code
            rows.append(row)
            print(f"    download={row['download']:.2f}s "
                  f"(ttfb={row['ttfb']:.2f}s, "
                  f"{row['mbps_download']:.1f} MB/s, "
                  f"{row['mb_zip']:.0f} MB) "
                  f"unzip={row['unzip']:.2f}s "
                  f"rename={row['rename']:.3f}s "
                  f"cleanup={row['cleanup']:.3f}s "
                  f"total={row['total']:.2f}s",
                  flush=True)
        except requests.RequestException as e:
            print(f"    FAIL: {e}", flush=True)

    elapsed = time.monotonic() - grand
    if rows:
        def avg(k):
            return sum(r[k] for r in rows) / len(rows)
        print()
        print(f"=== Profile summary ({len(rows)} sheets, target={args.target}) ===")
        print(f"  avg download : {avg('download'):.2f} s  ({avg('mbps_download'):.1f} MB/s)")
        print(f"  avg ttfb     : {avg('ttfb'):.2f} s")
        print(f"  avg unzip    : {avg('unzip'):.2f} s")
        print(f"  avg rename   : {avg('rename'):.3f} s")
        print(f"  avg cleanup  : {avg('cleanup'):.3f} s")
        print(f"  avg total    : {avg('total'):.2f} s")
        print(f"  avg ZIP size : {avg('mb_zip'):.0f} MB")
        print(f"  wall clock   : {elapsed:.1f} s (serial; {elapsed/len(rows):.1f} s/sheet)")

    if args.target == "ssd":
        shutil.rmtree(root, ignore_errors=True)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
