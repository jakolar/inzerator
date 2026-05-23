"""Re-fetch cadastre PNGs for all locations at a higher resolution.

Skips full heightfield regen (SM5 + DMR5G + ortho) and only re-pulls
`<slug>_cadastre.png` per ring at the given --size. Saves ~30-60 s per
location vs `dispatch_heightfield.py`.

Run:
    python3 refresh_cadastre.py                 # 8192² for all
    python3 refresh_cadastre.py --size 4096
    python3 refresh_cadastre.py --only hrensko,hnojice
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import locations

PROXY = "http://127.0.0.1:8080"


def refetch(slug, size):
    hf_dir = Path(f"tiles_v2_{slug}") / "heightfield"
    manifest_path = hf_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, "no heightfield manifest"
    m = json.loads(manifest_path.read_text())
    cx, cy = m["cx"], m["cy"]
    ok_rings = 0
    for ring in m["rings"]:
        half = ring["half"]
        bbox = f"{cx-half},{cy-half},{cx+half},{cy+half}"
        url = f"{PROXY}/proxy/cadastre?BBOX={bbox}&layer=KN&size={size}"
        out = hf_dir / f"{ring['slug']}_cadastre.png"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=180) as r:
                    out.write_bytes(r.read())
                ring["cadastre_file"] = out.name
                ring["cadastre_size"] = size
                ok_rings += 1
                break
            except (urllib.error.URLError, ConnectionError,
                    TimeoutError, OSError) as e:
                if attempt < 2:
                    time.sleep(2 + attempt * 2)
                else:
                    return False, f"ring {ring['slug']}: {e}"
    manifest_path.write_text(json.dumps(m, indent=2))
    return True, f"{ok_rings} rings"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=8192,
                    help="cadastre PNG side in px (max 8192)")
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of slugs")
    args = ap.parse_args()
    only = set(s.strip() for s in args.only.split(",")) if args.only else None

    targets = []
    for loc in locations.list_locations():
        if not loc.get("has_heightfield"):
            continue
        if only and loc["slug"] not in only:
            continue
        targets.append(loc["slug"])

    if not targets:
        print("No heightfield locations to refresh.")
        return

    print(f"Refreshing cadastre for {len(targets)} location(s) at {args.size}²")
    results = []
    t0 = time.time()
    for i, slug in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {slug} …", end=" ", flush=True)
        ts = time.time()
        ok, msg = refetch(slug, args.size)
        elapsed = time.time() - ts
        marker = "OK" if ok else f"FAIL ({msg})"
        print(f"{marker}  {elapsed:.1f}s")
        results.append((slug, ok))

    ok_count = sum(1 for _, ok in results if ok)
    print(f"\n{ok_count}/{len(targets)} ok in {(time.time()-t0)/60:.1f} min")
    if ok_count < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
