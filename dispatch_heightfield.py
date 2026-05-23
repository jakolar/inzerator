"""Batch-generate heightfield/ for every ready location.

Sequential to avoid hammering ČÚZK ArcGIS. Skips locations that already
have heightfield/manifest.json. Per-slug stdout/stderr captured into
dispatch_log/<slug>.log so failures don't lose context. Continues on error;
prints summary at the end.

Run:
    python3 dispatch_heightfield.py              # generate all missing
    python3 dispatch_heightfield.py --force      # regenerate even existing
    python3 dispatch_heightfield.py --only foo,bar   # restrict to subset
"""
from __future__ import annotations
import argparse
import subprocess
import sys
import time
from pathlib import Path

import locations

LOG_DIR = Path("dispatch_log")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-generate even if heightfield/ already exists")
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of slugs to process")
    ap.add_argument("--refresh-bare", action="store_true",
                    help="pass --refresh-bare to gen_heightfield (re-fetch "
                         "DMR5G even if cached). Default reuses cached bare.")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    only = set(s.strip() for s in args.only.split(",")) if args.only else None

    targets = []
    for loc in locations.list_locations():
        if loc["status"] != "ready":
            continue
        if only and loc["slug"] not in only:
            continue
        has_hf = (Path(f"tiles_v2_{loc['slug']}") / "heightfield"
                  / "manifest.json").exists()
        if has_hf and not args.force:
            continue
        targets.append(loc["slug"])

    if not targets:
        print("Nothing to do — all ready locations already have heightfield.")
        return

    print(f"Dispatching {len(targets)} location(s):")
    for s in targets:
        print(f"  • {s}")
    print()

    results = []
    t_total = time.time()
    for i, slug in enumerate(targets, 1):
        log_path = LOG_DIR / f"{slug}.log"
        cmd = ["python3", "gen_heightfield.py", "--slug", slug]
        if args.refresh_bare:
            cmd.append("--refresh-bare")
        print(f"[{i}/{len(targets)}] {slug} …", end=" ", flush=True)
        t0 = time.time()
        with log_path.open("w") as logf:
            proc = subprocess.run(
                cmd, stdout=logf, stderr=subprocess.STDOUT)
        elapsed = time.time() - t0
        ok = (proc.returncode == 0)
        results.append((slug, ok, elapsed))
        status = "OK" if ok else f"FAIL (exit {proc.returncode})"
        print(f"{status}  {elapsed:.1f}s  → {log_path}")

    elapsed_total = time.time() - t_total
    ok_count = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== Summary: {ok_count}/{len(targets)} ok in "
          f"{elapsed_total/60:.1f} min ===")
    for slug, ok, t in results:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {slug:<28} {t:6.1f}s")
    if ok_count < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
