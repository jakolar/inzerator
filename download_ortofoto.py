"""Download raw ČÚZK ortofoto for an SM5 mapový list (or by S-JTSK center).

The product distributed at native 12.5 cm/px in S-JTSK with a JGW sidecar —
this is the canonical ČÚZK source the tile service / WMS GetMap renders from.
Cropping locally from the raw file is the highest quality available.

Discovery uses the public ATOM feed (cycle prefix is per-area, so we can't
hard-code the URL pattern):
  https://atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml      → list of MAPNOM tiles
  https://atom.cuzk.gov.cz/.../<id>.xml               → per-tile dataset feed
                                                       with the actual ZIP url

Usage:
  python download_ortofoto.py --code OSTR81
  python download_ortofoto.py --center-sjtsk -547700,-1107700
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

UA = "gtaol/1.0 (ortofoto downloader)"
ATOM_INDEX = "https://atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml"


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def find_dataset_feed_url(mapnom: str) -> str:
    """Locate the per-tile ATOM dataset feed for the given SM5 code."""
    feed = _http_get(ATOM_INDEX, timeout=30).decode("utf-8", "replace")
    # Cycle prefix changes (WRTO24.2024.X / WRTO24.2025.X). Find any entry
    # whose dataset id ends with .{MAPNOM}
    pat = re.compile(
        rf'<id>(https://atom\.cuzk\.gov\.cz/ORTOFOTO/datasetFeeds/[^<]*\.{re.escape(mapnom)}\.xml)</id>',
        re.IGNORECASE,
    )
    m = pat.search(feed)
    if not m:
        raise SystemExit(
            f"MAPNOM {mapnom!r} not found in ČÚZK ortofoto ATOM feed.")
    return m.group(1)


def extract_zip_url(dataset_feed_xml: str) -> str:
    m = re.search(
        r'href="(https://[^"]+\.zip)"[^>]*type="image/jpeg"',
        dataset_feed_xml,
    )
    if not m:
        m = re.search(r'href="(https://[^"]+\.zip)"', dataset_feed_xml)
    if not m:
        raise SystemExit("No .zip URL found in dataset feed.")
    return m.group(1)


def find_mapnom_for_sjtsk(cx: float, cy: float) -> str:
    """Use ČÚZK KladyMapovychListu/24 (SM5) to map S-JTSK → MAPNOM."""
    url = ("https://ags.cuzk.cz/arcgis/rest/services/"
           "KladyMapovychListu/MapServer/24/query")
    params = {
        "geometry": json.dumps({
            "xmin": cx - 1, "ymin": cy - 1,
            "xmax": cx + 1, "ymax": cy + 1,
            "spatialReference": {"wkid": 5514},
        }),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "MAPNOM",
        "f": "json", "returnGeometry": "false",
    }
    raw = json.loads(_http_get(f"{url}?{urllib.parse.urlencode(params)}"))
    features = raw.get("features", [])
    if not features:
        raise SystemExit(f"No SM5 list covers S-JTSK ({cx}, {cy}).")
    return features[0]["attributes"]["MAPNOM"]


def download(mapnom: str, dest_root: Path, force: bool = False) -> Path:
    out_dir = dest_root / f"ortofoto_{mapnom}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Skip if any *.jpg + *.jgw pair exists in the dir.
    if not force:
        for jpg in out_dir.glob("*.jpg"):
            if jpg.with_suffix(".jgw").exists():
                print(f"  Already cached: {jpg}")
                return jpg

    feed_url = find_dataset_feed_url(mapnom)
    print(f"  Dataset feed: {feed_url}")
    feed_xml = _http_get(feed_url, timeout=30).decode("utf-8", "replace")
    zip_url = extract_zip_url(feed_xml)
    print(f"  Downloading: {zip_url}")

    zip_path = out_dir / f"{mapnom}.zip"
    req = urllib.request.Request(zip_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=600) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk_log = max(total // 14, 1)
        with open(zip_path, "wb") as f:
            while True:
                buf = resp.read(64 * 1024)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if total and downloaded // chunk_log != (downloaded - len(buf)) // chunk_log:
                    print(f"    {downloaded * 100 // total}% ({downloaded // (1024*1024)} MB)",
                          end="\r", flush=True)
    print(f"  Wrote {zip_path} ({zip_path.stat().st_size // (1024*1024)} MB)")

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    extracted = list(out_dir.glob("*.jpg"))
    if not extracted:
        raise SystemExit("Zip extracted, but no .jpg found inside.")
    print(f"  Extracted: {[p.name for p in extracted]}")
    return extracted[0]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", help="SM5 MAPNOM code (e.g. OSTR81)")
    g.add_argument("--center-sjtsk",
                   help="Lookup MAPNOM by S-JTSK center 'cx,cy'.")
    p.add_argument("--cache-dir", default="cache", type=Path)
    p.add_argument("--force", action="store_true",
                   help="Re-download even if cache file exists.")
    args = p.parse_args()

    if args.center_sjtsk:
        cx, cy = (float(v) for v in args.center_sjtsk.split(","))
        mapnom = find_mapnom_for_sjtsk(cx, cy)
        print(f"  S-JTSK ({cx}, {cy}) → MAPNOM {mapnom}")
    else:
        mapnom = args.code

    download(mapnom, args.cache_dir, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
