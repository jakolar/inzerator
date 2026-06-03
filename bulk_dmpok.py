"""DMPOK bulk downloader — 3 workers, SIGTERM-safe, resumable.

State lives in `<BULK_OUT_DIR>/sheets.json` (built by
`bulk_dmpok_inventory.py`). Each sheet flows through statuses:

    pending → downloading → done | failed | missing

`missing` = ČÚZK doesn't carry DMPOK for that sheet (404 / no .zip).
`failed` = something else broke; safe to retry by resetting in JSON.

Pause: SIGTERM (or Ctrl-C). In-flight downloads finish their current
sheet, state flushes, process exits. Resume: re-run the same command.

Designed to be driven by launchd plists that start at 22:00 and pkill at
06:00 (see `contrib/launchd/`). No internal time-window check — that's
launchd's job.
"""
from __future__ import annotations
import json
import os
import random
import signal
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import requests

OUT_ROOT = Path(os.environ.get("BULK_OUT_DIR", "/Volumes/Elements/cuzk-bulk"))
STATE_FILE = OUT_ROOT / "sheets.json"
LOG_FILE = OUT_ROOT / "download.log"
LOCK_DIR = OUT_ROOT / ".lock"

DMPOK_BASE = "https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514"
UA = "inzerator/1.0; alacremex@gmail.com"
WORKERS = 3
# Inter-sheet jitter per worker — caps aggregate rate even on a fast
# uplink and matches the email's "0.5–2 s pause" promise.
SLEEP_MIN, SLEEP_MAX = 0.5, 1.5
# Per-attempt timeout; ČÚZK occasionally stalls mid-stream.
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
    """Append a single line to the log file. Cheap lock — only one thread
    writes at a time, but the disk write itself can overlap with workers."""
    with log_lock:
        with LOG_FILE.open("a") as f:
            f.write(f"{_now()} {line}\n")


def _load_state() -> None:
    global _state
    _state = json.loads(STATE_FILE.read_text())


def _persist_state() -> None:
    """Atomic write. Called after every sheet so we never lose more than
    1 sheet × WORKERS of progress on an unclean kill."""
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
    """mkdir is atomic on POSIX → use the dir as a lockfile. Returns
    False if another instance already holds it."""
    try:
        LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        pid_path = LOCK_DIR / "pid"
        owner = pid_path.read_text().strip() if pid_path.exists() else "?"
        print(f"Another bulk_dmpok already running (lock dir held by PID {owner}). "
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
    """Download + extract one sheet. Returns one of: ok / skip / missing
    / fail / cancelled. Updates `_state` and `_counters`."""
    if stop_event.is_set():
        return "cancelled"

    out_dir = OUT_ROOT / f"dmpok_tiff_{code}"
    out_tif = out_dir / f"{code}.tif"
    if out_tif.exists():
        _mark(code, "done", size_mb=out_tif.stat().st_size // (1024 * 1024))
        _counters["skip"] += 1
        return "skip"

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{code}.zip"
    url = f"{DMPOK_BASE}/{code}.zip"
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
                    # ČÚZK doesn't publish DMPOK for every SM5 sheet —
                    # treat as terminal "missing", not failure.
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
            # Validate + extract
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(out_dir)
            # Normalise filename to <CODE>.tif (ZIP may use .TIF or different case)
            for ext in ("tif", "TIF", "tiff"):
                for p in out_dir.glob(f"*.{ext}"):
                    if p.name.lower() != f"{code.lower()}.tif":
                        p.rename(out_tif)
                    elif p.name != out_tif.name:
                        p.rename(out_tif)
            for ext in ("tfw", "TFW"):
                for p in out_dir.glob(f"*.{ext}"):
                    target = out_dir / f"{code}.tfw"
                    if p != target:
                        p.rename(target)
            zip_path.unlink(missing_ok=True)
            if not out_tif.exists():
                raise RuntimeError("ZIP extracted but no TIFF found inside")
            size_mb = out_tif.stat().st_size // (1024 * 1024)
            _mark(code, "done", size_mb=size_mb)
            _counters["ok"] += 1
            _log(f"OK {code} {size_mb}MB attempt={attempt}")
            return "ok"
        except (requests.RequestException, zipfile.BadZipFile, OSError,
                RuntimeError) as e:
            last_err = repr(e)
            zip_path.unlink(missing_ok=True)
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
    """Each worker pulls from the shared queue (popleft semantics via the
    state_lock; len + pop are atomic together)."""
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
    """Print a one-line status update every 30 s while running."""
    while not stop_event.is_set():
        for _ in range(60):  # 60 × 0.5s = 30 s, but checkable
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
    if not STATE_FILE.exists():
        raise SystemExit(
            f"{STATE_FILE} not found — run bulk_dmpok_inventory.py first.")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock():
        return 1
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        _load_state()
        # Re-arm any 'downloading' entries from a previous unclean exit.
        # The next pass will retry them.
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

        pending = [c for c, s in _state.items() if s["status"] == "pending"]
        # Failed sheets are NOT auto-retried. Operator picks them up
        # explicitly via `bulk_dmpok_status.py --retry-failed` (which
        # flips them back to pending), so a flaky 5-attempt sheet
        # doesn't burn the whole worker pool.
        if not pending:
            print(f"Nothing pending. ({STATE_FILE} has no 'pending' entries.)")
            return 0
        print(f"[{_now()}] starting: {WORKERS} workers, {len(pending)} pending sheets")
        _log(f"START workers={WORKERS} pending={len(pending)}")

        reporter = threading.Thread(target=_progress_reporter,
                                    args=(len(pending),), daemon=True)
        reporter.start()

        with ThreadPoolExecutor(max_workers=WORKERS) as exe:
            for wid in range(WORKERS):
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
