"""Quick status summary for the ortofoto bulk download.

Reads `<BULK_OUT_DIR>/ortofoto_sheets.json` and prints counters, aggregate
disk usage, a rough ETA from the log tail, year split, and (with -v) top
failure reasons. `--retry-failed` flips 'failed' → 'pending'; doesn't touch
'missing'.

Note: for ortho, a non-trivial `missing` count is suspicious (full-ČR
coverage) — it points at an inventory url-pattern bug, not a real gap.
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
STATE_FILE = OUT_ROOT / "ortofoto_sheets.json"
LOG_FILE = OUT_ROOT / "ortofoto_download.log"

_LOG_OK_RE = re.compile(r"^(\S+ \S+) OK ")


def _fmt_bytes(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _estimate_rate(window: int = 200) -> tuple[float, float] | None:
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
    counts: Counter = Counter()
    years: Counter = Counter()
    total_bytes = 0
    fail_reasons: Counter = Counter()
    for entry in state.values():
        st = entry.get("status", "pending")
        counts[st] += 1
        if entry.get("year"):
            years[entry["year"]] += 1
        if st == "done":
            total_bytes += int(entry.get("size_mb", 0)) * 1024 * 1024
        if st == "failed":
            err = entry.get("error", "?")
            short = re.sub(r"\d", "0", err)[:80]
            fail_reasons[short] += 1
    total = sum(counts.values())
    done = counts["done"]
    pct = done * 100 / total if total else 0

    print(f"Ortofoto bulk download — {OUT_ROOT}")
    print(f"  done:    {done:>6} / {total} ({pct:.1f} %)")
    print(f"  pending: {counts['pending']:>6}")
    print(f"  failed:  {counts['failed']:>6}")
    print(f"  missing: {counts['missing']:>6}   (404 — should be ~0 for ortho!)")
    print(f"  in-flight (stale): {counts['downloading']:>6}")
    print(f"  on disk: {_fmt_bytes(total_bytes)}")
    if years:
        print(f"  by year: {dict(sorted(years.items()))}")

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
        print(f"{STATE_FILE} not found — run bulk_ortofoto_inventory.py first.",
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
