"""Re-build ortho composites for an existing heightfield location.

Reuses cached SM5 ortho sheets (no ČÚZK refetch) and updates the manifest
in place. Useful for:
  - Adding a new tier (e.g. `ultra` for 8192² composite) without
    regenerating heightmaps / DMR5G / cadastre.
  - Bumping JPG quality on an existing slug.
  - Re-encoding all KTX2 with different basisu flags.

Run:
    python3 refresh_ortho.py --slug hnojice --tiers ultra
    python3 refresh_ortho.py --slug hnojice --tiers mid,high,ultra
    python3 refresh_ortho.py --only hnojice,decin --tiers high
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import gen_heightfield as g
from PIL import Image

TILES_DIR_PREFIX = "tiles_v2_"


def _init_cache_dir():
    # gen_heightfield.discover_ortho reads from g.CACHE_DIR, which
    # gen_heightfield's own main() populates from CLI/env. We're bypassing
    # main() so we have to wire it up explicitly. Lazy (called from main, not
    # at import) so test/repl imports of this module don't SystemExit when
    # there's no cache configured.
    if g.CACHE_DIR is None:
        g.CACHE_DIR = g.resolve_cache_dir(None)


def refresh_one(slug: str, tiers: list[str]) -> tuple[bool, str]:
    hd = Path(f"{TILES_DIR_PREFIX}{slug}") / "heightfield"
    manifest_path = hd / "manifest.json"
    if not manifest_path.is_file():
        return False, "no manifest"
    m = json.loads(manifest_path.read_text())
    cx, cy = m["cx"], m["cy"]
    for ring in m.get("rings", []):
        ring_slug = ring["slug"]
        half = ring["half"]
        if "ortho_size" not in ring:
            print(f"  WARN: ring {ring_slug} has no ortho_size in manifest, "
                  f"falling back to 4096 — re-run gen_heightfield to get correct value")
        size = ring.get("ortho_size", 4096)
        ortho_files = ring.get("ortho_tiers") or {}
        # Build the largest tier first; smaller tiers come from a cheap
        # downsample of that image. Saves ~60% wall-time vs. rebuilding the
        # composite from source sheets at every tier size.
        tier_specs = []
        for tier_name in tiers:
            if tier_name not in g.ORTHO_TIERS:
                return False, f"unknown tier {tier_name}"
            t = g.ORTHO_TIERS[tier_name]
            tier_size = max(64, int(round(size * t["scale"])))
            tier_specs.append((tier_name, t, tier_size))
        tier_specs.sort(key=lambda s: -s[2])
        # Pre-fetch any missing SM5 sheets — same recurring "one stripe
        # didn't load" fix as gen_heightfield. Done once per ring before
        # the tier loop touches build_ortho_composite.
        g.ensure_sm5_cached(cx, cy, half, fetch_missing=True)
        base_composite = None
        base_size = None
        for tier_name, t, tier_size in tier_specs:
            if base_composite is None:
                print(f"  [{ring_slug}/{tier_name}] composite {tier_size}×{tier_size}")
                base_composite = g.build_ortho_composite(cx, cy, half, tier_size)
                base_size = tier_size
                composite = base_composite
            else:
                print(f"  [{ring_slug}/{tier_name}] resize {base_size}→{tier_size}")
                composite = base_composite.resize(
                    (tier_size, tier_size), Image.LANCZOS)
            jpg_path = hd / f"{ring_slug}_ortho_{tier_name}.jpg"
            webp_path = hd / f"{ring_slug}_ortho_{tier_name}.webp"
            g.save_ortho_jpeg(composite, jpg_path, t["quality"], t["subsampling"])
            # WebP encoder hard-caps each dimension at 16383 px. Skip it for
            # the super tier (16384²) and rely on the JPG + KTX2 pair —
            # viewer picks KTX2 first anyway. Drop the stale .webp from a
            # previous run if it's lying around.
            jpg_kb = round(jpg_path.stat().st_size / 1024, 1)
            entry = {
                "file": jpg_path.name,
                "size_px": tier_size,
                "kb": jpg_kb,
            }
            if tier_size < 16384:
                g.save_ortho_webp(composite, webp_path, t["webp_quality"])
                webp_kb = round(webp_path.stat().st_size / 1024, 1)
                entry["webp_file"] = webp_path.name
                entry["webp_kb"] = webp_kb
            else:
                if webp_path.exists():
                    webp_path.unlink()
                webp_kb = None
            if tier_size >= 2048:
                ktx2_path = hd / f"{ring_slug}_ortho_{tier_name}.ktx2"
                try:
                    g.encode_ortho_ktx2(jpg_path, ktx2_path)
                    entry["ktx2_file"] = ktx2_path.name
                    entry["ktx2_kb"] = round(ktx2_path.stat().st_size / 1024, 1)
                except FileNotFoundError:
                    print(f"    (ktx2 skipped: basisu not installed)")
                except subprocess.CalledProcessError as e:
                    err = (e.stderr or "").strip()[:200] or repr(e)
                    print(f"    (ktx2 skipped: rc={e.returncode}, stderr={err})")
            ortho_files[tier_name] = entry
            extras = [f"jpg {jpg_kb:.1f} KB"]
            if webp_kb is not None:
                extras.append(f"webp {webp_kb:.1f} KB")
            else:
                extras.append("webp skipped (>16383 px)")
            if "ktx2_file" in entry:
                extras.append(f"ktx2 {entry['ktx2_kb']:.1f} KB")
            print(f"    {' / '.join(extras)}")
        ring["ortho_tiers"] = ortho_files
    g.write_json_atomic(manifest_path, m)
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="single slug to refresh")
    ap.add_argument("--only", help="comma-separated slugs (overrides --slug)")
    ap.add_argument("--tiers", required=True,
                    help="comma-separated tier names (low,mid,high,ultra)")
    args = ap.parse_args()
    _init_cache_dir()

    if args.only:
        targets = [s.strip() for s in args.only.split(",") if s.strip()]
    elif args.slug:
        targets = [args.slug]
    else:
        targets = sorted(p.name[len(TILES_DIR_PREFIX):]
                         for p in Path(".").glob(f"{TILES_DIR_PREFIX}*")
                         if (p / "heightfield" / "manifest.json").is_file())

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    if not tiers:
        print("--tiers required")
        sys.exit(1)

    print(f"Refreshing {len(targets)} location(s) for tiers: {tiers}")
    t0 = time.time()
    fails = []
    for i, slug in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {slug}")
        ts = time.time()
        ok, msg = refresh_one(slug, tiers)
        elapsed = time.time() - ts
        if ok:
            print(f"  OK  {elapsed:.1f}s")
        else:
            print(f"  FAIL ({msg})")
            fails.append(slug)
    print(f"\nDone in {(time.time()-t0)/60:.1f} min. "
          f"{len(targets)-len(fails)}/{len(targets)} ok")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
