"""One-shot crawler that builds the ortofoto sheet inventory.

Unlike `bulk_dmpok_inventory.py` (which enumerates KladyMapovychListu), the
authoritative list of published ortofoto sheets is the ČÚZK ortofoto ATOM
index itself. We fetch it once (~24 MB), parse every dataset-feed id of the
form `..._WRTO24.<YEAR>.<MAPNOM>.xml`, keep the NEWEST year per MAPNOM, and
derive the (deterministic, HEAD-verified) opendata ZIP url. No per-sheet feed
fetch needed.

Writes `<BULK_OUT_DIR>/ortofoto_sheets.json` keyed by MAPNOM:

    { "BENE00": {"status": "pending", "year": 2025,
                 "zip_url": "https://openzu.cuzk.gov.cz/opendata/ORTOFOTO/WRTO24.2025.BENE00.zip"} }

Idempotent: existing done/failed/missing entries are preserved on re-run.
Distinct filename from DMPOK `sheets.json` so both coexist in one BULK_OUT_DIR.

Run once before `bulk_ortofoto.py`. Refresh later if ČÚZK flies a new cycle.
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

import requests

OUT_ROOT = Path(os.environ.get("BULK_OUT_DIR", "/Volumes/Elements/cuzk-bulk"))
STATE_FILE = OUT_ROOT / "ortofoto_sheets.json"
ATOM_INDEX = "https://atom.cuzk.gov.cz/Ortofoto/Ortofoto.xml"
ZIP_BASE = "https://openzu.cuzk.gov.cz/opendata/ORTOFOTO"
UA = "inzerator/1.0; alacremex@gmail.com"

# dataset id e.g. ...CUZK_ORTOFOTO_WRTO24.2025.BENE00.xml — cycle prefix is
# captured so the ZIP url stays correct if ČÚZK rolls a new cycle (WRTO25…).
_ENTRY_RE = re.compile(
    r'datasetFeeds/[^<"]*?_(?P<cyc>[A-Z]+\d+)\.(?P<year>\d{4})\.(?P<mapnom>[A-Z0-9]+)\.xml'
)


def fetch_newest_sheets() -> dict[str, dict]:
    """Return {MAPNOM: {year, cyc}} keeping the newest year per sheet."""
    print(f"Fetching ortofoto ATOM index {ATOM_INDEX} …")
    xml = requests.get(ATOM_INDEX, headers={"User-Agent": UA}, timeout=120).text
    print(f"  {len(xml) / 1e6:.1f} MB")
    best: dict[str, tuple[int, str]] = {}
    for m in _ENTRY_RE.finditer(xml):
        mapnom = m.group("mapnom")
        year = int(m.group("year"))
        cyc = m.group("cyc")
        prev = best.get(mapnom)
        if prev is None or year > prev[0]:
            best[mapnom] = (year, cyc)
    return {
        mapnom: {
            "year": year,
            "zip_url": f"{ZIP_BASE}/{cyc}.{year}.{mapnom}.zip",
        }
        for mapnom, (year, cyc) in best.items()
    }


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Output root: {OUT_ROOT}")
    sheets = fetch_newest_sheets()
    print(f"Discovered {len(sheets)} unique ortofoto sheets")
    if not sheets:
        raise SystemExit("Got zero sheets — refusing to overwrite state.")
    years: dict[int, int] = {}
    for v in sheets.values():
        years[v["year"]] = years.get(v["year"], 0) + 1
    print(f"  by year: {dict(sorted(years.items()))}")

    existing: dict[str, dict] = {}
    if STATE_FILE.exists():
        existing = json.loads(STATE_FILE.read_text())
        print(f"Existing state has {len(existing)} entries — preserving "
              "done/failed/missing")

    state: dict[str, dict] = {}
    kept = 0
    for mapnom, info in sheets.items():
        prev = existing.get(mapnom, {})
        # Preserve terminal states ONLY when the resolved year is unchanged —
        # a newer cycle means the old file is stale and must be re-fetched.
        if (prev.get("status") in ("done", "failed", "missing")
                and prev.get("year") == info["year"]):
            entry = dict(prev)
            entry["zip_url"] = info["zip_url"]
            state[mapnom] = entry
            kept += 1
        else:
            state[mapnom] = {"status": "pending", **info}

    removed = [c for c in existing if c not in state]
    for c in removed:
        prev = dict(existing[c])
        prev["_removed_from_index"] = True
        state[c] = prev

    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)
    pending = sum(1 for s in state.values() if s.get("status") == "pending")
    print(f"Wrote {STATE_FILE}")
    print(f"  {pending} pending · {kept} preserved · {len(removed)} no-longer-in-index")


if __name__ == "__main__":
    main()
