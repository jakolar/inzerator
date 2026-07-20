"""DMR5G bulk downloader — bare-earth LiDAR point clouds, resumable.

A faithful clone of bulk_dmpok.py for the DMR 5G open-data product (ground-only
LAZ, one `<MAPNOM>.laz` per SM5 sheet). The surface bulk (bulk_dmpok.py) carries
buildings+vegetation; this is the ground-only twin used for bare terrain.

Distribution: https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/<CODE>.zip
Each zip holds a single <CODE>.laz (~2-6 MB); full CR ≈ 16 299 sheets ≈ 55 GB.

State lives in `<BULK_DMR5G_OUT_DIR>/sheets.json`:

    pending → downloading → done | failed | missing

`missing` = ČÚZK has no DMR5G for that SM5 sheet (404). `failed` = other error;
NOT auto-retried across runs (reset in JSON to retry). Pause via SIGTERM/Ctrl-C:
the in-flight sheet finishes, state flushes, process exits. Resume: re-run.

Seed the inventory once from the DMPOK sheet grid (identical MAPNOM codes):
    python3 -c "import json,pathlib; \
      src=json.load(open('/Volumes/Elements/cuzk-bulk/sheets.json')); \
      codes=src.get('sheets',src).keys(); \
      out={c:{'status':'pending'} for c in codes}; \
      p=pathlib.Path('/Volumes/Elements/cuzk-dmr5g'); p.mkdir(parents=True,exist_ok=True); \
      (p/'sheets.json').write_text(json.dumps(out,indent=2,sort_keys=True))"
"""
from __future__ import annotations
import json
import os
import random
import shutil
import signal
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import argparse
import requests

OUT_ROOT = Path(os.environ.get("BULK_DMR5G_OUT_DIR", "/Volumes/Elements/cuzk-dmr5g"))
STATE_FILE = OUT_ROOT / "sheets.json"
LOG_FILE = OUT_ROOT / "download.log"
# Lock dir on the internal disk (launchd/TCC can't always mkdir under /Volumes/*).
# Hash of OUT_ROOT keeps it distinct from the DMPOK downloader's lock.
import hashlib as _hashlib
_LOCK_PARENT = Path.home() / "Library" / "Caches" / "inzerator"
LOCK_DIR = _LOCK_PARENT / (
    f"bulk_dmr5g-{_hashlib.sha1(str(OUT_ROOT).encode()).hexdigest()[:8]}.lock"
)

DMR5G_BASE = "https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514"
UA = "inzerator/1.0; alacremex@gmail.com"
# Bare-earth LAZ streams are ~20x smaller than DMPOK TIFFs, so download time
# per sheet is short and 4 workers stays well inside ČÚZK's politeness budget.
WORKERS = 4
SLEEP_MIN, SLEEP_MAX = 0.5, 1.5
TIMEOUT_SECS = 600
MAX_ATTEMPTS = 5

stop_event = threading.Event()
state_lock = threading.Lock()
log_lock = threading.Lock()
_state: dict[str, dict] = {}
_counters = {"ok": 0, "skip": 0, "missing": 0, "fail": 0}


def _handle_signal(signum, _frame):
    name = signal.Signals(signum).name
    print(f"\n[{_now()}] {name} received — draining in-flight downloads…",
          flush=True)
    stop_event.set()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(line: str) -> None:
    with log_lock:
        with LOG_FILE.open("a") as f:
            f.write(f"{_now()} {line}\n")


def _load_state() -> None:
    global _state
    _state = json.loads(STATE_FILE.read_text())


def _persist_state() -> None:
    """Atomic write after every sheet — never lose >1 sheet×WORKERS on kill."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with state_lock:
        snapshot = json.dumps(_state, indent=2, sort_keys=True)
    tmp.write_text(snapshot)
    tmp.replace(STATE_FILE)


def _mark(code: str, status: str, **extra) -> None:
    with state_lock:
        entry = _state.get(code, {})
        entry["status"] = status
        entry.update(extra)
        _state[code] = entry


def _acquire_lock() -> bool:
    _LOCK_PARENT.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        pid_path = LOCK_DIR / "pid"
        owner = pid_path.read_text().strip() if pid_path.exists() else "?"
        print(f"Another bulk_dmr5g already running (lock dir held by PID {owner}). "
              f"Delete {LOCK_DIR} if you're sure no process is alive.",
              file=sys.stderr)
        return False
    (LOCK_DIR / "pid").write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        (LOCK_DIR / "pid").unlink(missing_ok=True)
        LOCK_DIR.rmdir()
    except OSError:
        pass


def _fetch_one(code: str) -> str:
    """Download + extract one sheet's .laz. Returns ok/skip/missing/fail/cancelled."""
    if stop_event.is_set():
        return "cancelled"

    out_laz = OUT_ROOT / f"{code}.laz"
    if out_laz.exists():
        _mark(code, "done", size_mb=out_laz.stat().st_size // (1024 * 1024))
        _counters["skip"] += 1
        return "skip"

    zip_path = OUT_ROOT / f".{code}.zip"       # hidden per-code temp (flat dir)
    tmp_laz = OUT_ROOT / f".{code}.laz.tmp"
    url = f"{DMR5G_BASE}/{code}.zip"
    _mark(code, "downloading")

    last_err: Optional[str] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if stop_event.is_set():
            zip_path.unlink(missing_ok=True)
            _mark(code, "pending")
            return "cancelled"
        try:
            with requests.get(url, stream=True, timeout=TIMEOUT_SECS,
                              headers={"User-Agent": UA}) as r:
                if r.status_code == 404:
                    zip_path.unlink(missing_ok=True)
                    _mark(code, "missing")
                    _counters["missing"] += 1
                    _log(f"MISSING {code} (404)")
                    return "missing"
                r.raise_for_status()
                with zip_path.open("wb") as f:
                    for chunk in r.iter_content(4 * 1024 * 1024):
                        if stop_event.is_set():
                            f.close()
                            zip_path.unlink(missing_ok=True)
                            _mark(code, "pending")
                            return "cancelled"
                        if chunk:
                            f.write(chunk)
            # Extract the single .laz member straight to the final name (atomic
            # via a .tmp rename). Reading the named member avoids a flat-dir
            # glob picking up another worker's file.
            with zipfile.ZipFile(zip_path) as zf:
                laz_members = [n for n in zf.namelist()
                               if n.lower().endswith(".laz")]
                if not laz_members:
                    raise RuntimeError("ZIP has no .laz member")
                with zf.open(laz_members[0]) as src, tmp_laz.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
            tmp_laz.replace(out_laz)
            zip_path.unlink(missing_ok=True)
            size_mb = out_laz.stat().st_size // (1024 * 1024)
            _mark(code, "done", size_mb=size_mb)
            _counters["ok"] += 1
            _log(f"OK {code} {size_mb}MB attempt={attempt}")
            return "ok"
        except (requests.RequestException, zipfile.BadZipFile, OSError,
                RuntimeError) as e:
            last_err = repr(e)
            zip_path.unlink(missing_ok=True)
            tmp_laz.unlink(missing_ok=True)
            if attempt < MAX_ATTEMPTS and not stop_event.is_set():
                wait = min(60, 2 ** attempt) + random.uniform(0, 1.5)
                _log(f"RETRY {code} attempt={attempt} wait={wait:.1f}s err={last_err}")
                time.sleep(wait)
            continue

    _mark(code, "failed", error=last_err)
    _counters["fail"] += 1
    _log(f"FAIL {code} {last_err}")
    return "fail"


def _worker_loop(work_queue: list[str], worker_id: int) -> None:
    while not stop_event.is_set():
        with state_lock:
            if not work_queue:
                return
            code = work_queue.pop(0)
        result = _fetch_one(code)
        _persist_state()
        if stop_event.is_set():
            return
        if result not in ("skip", "cancelled"):
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def _progress_reporter(total: int) -> None:
    while not stop_event.is_set():
        for _ in range(60):
            if stop_event.is_set():
                return
            time.sleep(0.5)
        done = _counters["ok"] + _counters["skip"]
        miss = _counters["missing"]
        fail = _counters["fail"]
        processed = done + miss + fail
        print(f"[{_now()}] progress: ok={_counters['ok']} skip={_counters['skip']} "
              f"missing={miss} fail={fail} ({processed}/{total} this session)",
              flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"parallel download workers (default {WORKERS})")
    args = ap.parse_args()
    workers = max(1, args.workers)

    if not STATE_FILE.exists():
        raise SystemExit(
            f"{STATE_FILE} not found — seed the inventory first (see module docstring).")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock():
        return 1
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        _load_state()
        with state_lock:
            recovered = 0
            for code, entry in _state.items():
                if entry.get("status") == "downloading":
                    entry["status"] = "pending"
                    recovered += 1
        if recovered:
            print(f"Recovered {recovered} 'downloading' entries from prior session "
                  "→ marked pending")
            _persist_state()

        pending = [c for c, s in _state.items() if s.get("status") == "pending"]
        if not pending:
            print(f"Nothing pending. ({STATE_FILE} has no 'pending' entries.)")
            return 0
        print(f"[{_now()}] starting: {workers} workers, {len(pending)} pending sheets")
        _log(f"START workers={workers} pending={len(pending)}")

        reporter = threading.Thread(target=_progress_reporter,
                                    args=(len(pending),), daemon=True)
        reporter.start()

        with ThreadPoolExecutor(max_workers=workers) as exe:
            for wid in range(workers):
                exe.submit(_worker_loop, pending, wid)
        _persist_state()

        msg = (f"END ok={_counters['ok']} skip={_counters['skip']} "
               f"missing={_counters['missing']} fail={_counters['fail']}")
        _log(msg)
        print(f"[{_now()}] {msg}")
        return 0
    finally:
        _release_lock()


if __name__ == "__main__":
    sys.exit(main())
