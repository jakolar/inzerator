"""Quick status summary for the DMPOK bulk download.

Reads `<BULK_OUT_DIR>/sheets.json` and prints:
  - counters (done / pending / failed / missing)
  - aggregate disk usage from per-sheet size_mb
  - rough wall-clock ETA from the last 200 download.log entries
  - top failure reasons (when --verbose)

`--retry-failed` flips every 'failed' entry back to 'pending' so the
next `bulk_dmpok.py` run picks them up. Doesn't touch 'missing'.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

OUT_ROOT = Path(os.environ.get("BULK_OUT_DIR", "/Volumes/Elements/cuzk-bulk"))
STATE_FILE = OUT_ROOT / "sheets.json"
LOG_FILE = OUT_ROOT / "download.log"

_LOG_OK_RE = re.compile(r"^(\S+ \S+) OK ")


def _fmt_bytes(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _estimate_rate(window: int = 200) -> tuple[float, float] | None:
    """Return (sheets_per_minute, last_window_minutes) from log tail, or
    None if there's not enough data."""
    if not LOG_FILE.exists():
        return None
    lines = LOG_FILE.read_text().splitlines()
    ok_lines = [l for l in lines if " OK " in l][-window:]
    if len(ok_lines) < 5:
        return None
    import datetime
    first = _LOG_OK_RE.match(ok_lines[0])
    last = _LOG_OK_RE.match(ok_lines[-1])
    if not first or not last:
        return None
    t0 = datetime.datetime.strptime(first.group(1), "%Y-%m-%d %H:%M:%S")
    t1 = datetime.datetime.strptime(last.group(1), "%Y-%m-%d %H:%M:%S")
    minutes = (t1 - t0).total_seconds() / 60
    if minutes <= 0:
        return None
    return len(ok_lines) / minutes, minutes


def _summarise(state: dict[str, dict], verbose: bool = False) -> None:
    counts = Counter()
    total_bytes = 0
    fail_reasons: Counter = Counter()
    for entry in state.values():
        counts[entry.get("status", "pending")] += 1
        if entry.get("status") == "done":
            total_bytes += int(entry.get("size_mb", 0)) * 1024 * 1024
        if entry.get("status") == "failed":
            err = entry.get("error", "?")
            # Squash variable parts (URLs, addresses) for grouping.
            short = re.sub(r"\d", "0", err)[:80]
            fail_reasons[short] += 1
    total = sum(counts.values())
    done = counts["done"]
    pct = done * 100 / total if total else 0

    print(f"DMPOK bulk download — {OUT_ROOT}")
    print(f"  done:    {done:>6} / {total} ({pct:.1f} %)")
    print(f"  pending: {counts['pending']:>6}")
    print(f"  failed:  {counts['failed']:>6}")
    print(f"  missing: {counts['missing']:>6}   (404 — not published by ČÚZK)")
    print(f"  in-flight (stale): {counts['downloading']:>6}")
    print(f"  on disk: {_fmt_bytes(total_bytes)}")

    rate_window = _estimate_rate()
    if rate_window and counts["pending"]:
        rate, win_min = rate_window
        eta_min = counts["pending"] / rate if rate else 0
        print(f"  rate:    {rate:.1f} sheets/min "
              f"(last {win_min:.0f} min) → ETA {eta_min/60:.1f} h "
              f"({eta_min/60/8:.1f} nights at 8 h/night)")

    if verbose and fail_reasons:
        print("\nTop failure reasons:")
        for reason, n in fail_reasons.most_common(10):
            print(f"  {n:>4}× {reason}")


def _retry_failed(state: dict[str, dict]) -> int:
    n = 0
    for entry in state.values():
        if entry.get("status") == "failed":
            entry["status"] = "pending"
            entry.pop("error", None)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show top failure reasons")
    ap.add_argument("--retry-failed", action="store_true",
                    help="flip every 'failed' entry back to 'pending'")
    args = ap.parse_args()
    if not STATE_FILE.exists():
        print(f"{STATE_FILE} not found — run bulk_dmpok_inventory.py first.",
              file=sys.stderr)
        return 1
    state = json.loads(STATE_FILE.read_text())
    if args.retry_failed:
        n = _retry_failed(state)
        if n:
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
            tmp.replace(STATE_FILE)
        print(f"Reset {n} failed → pending")
    _summarise(state, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
