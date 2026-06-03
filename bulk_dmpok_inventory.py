"""One-shot crawler that builds the canonical SM5 sheet inventory.

Queries ČÚZK KladyMapovychListu/25 with pagination, writes
`<BULK_OUT_DIR>/sheets.json` keyed by MAPNOM. Idempotent: existing
done/failed/missing entries are preserved on re-run so you can refresh
the list without losing progress.

Run once before `bulk_dmpok.py`. Refresh later if ČÚZK adds new sheets.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

OUT_ROOT = Path(os.environ.get("BULK_OUT_DIR", "/Volumes/Elements/cuzk-bulk"))
STATE_FILE = OUT_ROOT / "sheets.json"
LAYER_URL = (
    "https://ags.cuzk.cz/arcgis/rest/services/"
    "KladyMapovychListu/MapServer/25/query"
)
UA = "inzerator/1.0; alacremex@gmail.com"
PAGE = 1000


def fetch_all_codes() -> list[str]:
    """Paginated `where=1=1` over the SM5 layer. ArcGIS sorts unstable
    without `orderByFields`, so paginating without an ORDER BY can yield
    overlapping/missing rows — pin to MAPNOM for deterministic paging."""
    codes: list[str] = []
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "MAPNOM",
            "returnGeometry": "false",
            "orderByFields": "MAPNOM ASC",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "f": "json",
        }
        url = f"{LAYER_URL}?{urlencode(params)}"
        for attempt in range(4):
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                wait = 2 ** attempt
                print(f"  retry {attempt+1}/4 after {wait}s ({e})", file=sys.stderr)
                time.sleep(wait)
        else:
            raise SystemExit(f"Layer query failed at offset {offset}")
        feats = data.get("features", [])
        page_codes = [f["attributes"]["MAPNOM"] for f in feats
                      if f.get("attributes", {}).get("MAPNOM")]
        codes.extend(page_codes)
        print(f"  page offset={offset}: +{len(page_codes)} (total {len(codes)})")
        if not data.get("exceededTransferLimit") and len(feats) < PAGE:
            break
        offset += len(feats)
    return codes


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Output root: {OUT_ROOT}")
    print(f"Querying {LAYER_URL} for all SM5 sheets covering CR…")
    codes = fetch_all_codes()
    print(f"Discovered {len(codes)} sheets")
    if not codes:
        raise SystemExit("Got zero sheets — refusing to overwrite state.")

    existing: dict[str, dict] = {}
    if STATE_FILE.exists():
        existing = json.loads(STATE_FILE.read_text())
        print(f"Existing state has {len(existing)} entries — preserving "
              "done/failed/missing")

    state: dict[str, dict] = {}
    kept = 0
    for code in codes:
        prev = existing.get(code, {})
        # Preserve terminal states so a re-run of inventory doesn't lose
        # work. 'pending' or 'downloading' get reset to 'pending' so the
        # downloader picks them up on next pass.
        if prev.get("status") in ("done", "failed", "missing"):
            state[code] = prev
            kept += 1
        else:
            state[code] = {"status": "pending"}
    # Any sheets that disappeared from the layer between runs — keep them
    # but flag, so they're visible in status.
    removed = [c for c in existing if c not in state]
    for c in removed:
        prev = existing[c]
        prev["status"] = prev.get("status", "pending")
        prev["_removed_from_layer"] = True
        state[c] = prev

    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)
    pending = sum(1 for s in state.values() if s["status"] == "pending")
    print(f"Wrote {STATE_FILE}")
    print(f"  {pending} pending · {kept} preserved · {len(removed)} no-longer-in-layer")


if __name__ == "__main__":
    main()
