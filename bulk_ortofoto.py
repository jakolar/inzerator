"""Ortofoto bulk downloader — 4 workers, SIGTERM-safe, resumable.

Same engine as `bulk_dmpok.py`; the only differences are the source (per-sheet
ZIP url comes from the inventory state, not a fixed base) and the extracted
product (JPEG + JGW instead of TIFF). State lives in
`<BULK_OUT_DIR>/ortofoto_sheets.json` (built by `bulk_ortofoto_inventory.py`):

    pending → downloading → done | failed | missing

`missing` = 404 from openzu (should be ~0 for ortho — full-ČR coverage; a high
count means the inventory url pattern is wrong, not a real gap).

Pause: SIGTERM (or Ctrl-C). In-flight downloads drop back to `pending`, state
flushes, process exits. Resume: re-run the same command. No internal time
window — leave that to launchd / a shell wrapper.
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

import argparse
import requests

OUT_ROOT = Path(os.environ.get("BULK_OUT_DIR", "/Volumes/Elements/cuzk-bulk"))
STATE_FILE = OUT_ROOT / "ortofoto_sheets.json"
LOG_FILE = OUT_ROOT / "ortofoto_download.log"
# Lock dir on the internal disk (TCC blocks mkdir under /Volumes/* for launchd
# agents). Hash OUT_ROOT so multiple BULK_OUT_DIRs coexist.
import hashlib as _hashlib
_LOCK_PARENT = Path.home() / "Library" / "Caches" / "inzerator"
LOCK_DIR = _LOCK_PARENT / (
    f"bulk_ortofoto-{_hashlib.sha1(str(OUT_ROOT).encode()).hexdigest()[:8]}.lock"
)

UA = "inzerator/1.0; alacremex@gmail.com"
WORKERS = 4                       # matches DMPOK politeness budget
SLEEP_MIN, SLEEP_MAX = 0.5, 1.5   # inter-sheet jitter per worker
TIMEOUT_SECS = 600                # ortho ZIPs are ~60–80 MB; allow slow streams
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
        print(f"Another bulk_ortofoto already running (lock held by PID {owner}). "
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


def _existing_jpg(out_dir: Path) -> Optional[Path]:
    """A sheet is done when a *.jpg with a sibling *.jgw exists."""
    for jpg in out_dir.glob("*.jpg"):
        if jpg.with_suffix(".jgw").exists():
            return jpg
    return None


def _flatten_extracted(out_dir: Path) -> None:
    """Some ČÚZK ortho zips wrap their files in a subdir
    (`WRTO24.<year>.<MAPNOM>/…`). Hoist jpg + jgw world files to the dir root
    so the layout matches the common case (and the server raw-crop top-level
    glob), then drop the emptied subdirs."""
    for p in list(out_dir.rglob("*")):
        if p.is_file() and p.parent != out_dir and p.suffix.lower() in (".jpg", ".jgw"):
            target = out_dir / p.name
            if not target.exists():
                p.replace(target)
    for d in sorted((p for p in out_dir.glob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def _fetch_one(code: str) -> str:
    """Download + extract one sheet. ok / skip / missing / fail / cancelled."""
    if stop_event.is_set():
        return "cancelled"

    entry = _state.get(code, {})
    zip_url = entry.get("zip_url")
    if not zip_url:
        _mark(code, "failed", error="no zip_url in state (re-run inventory)")
        _counters["fail"] += 1
        return "fail"

    out_dir = OUT_ROOT / f"ortofoto_{code}"
    existing = _existing_jpg(out_dir) if out_dir.exists() else None
    if existing:
        _mark(code, "done", size_mb=existing.stat().st_size // (1024 * 1024))
        _counters["skip"] += 1
        return "skip"

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{code}.zip"
    _mark(code, "downloading")

    last_err: Optional[str] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if stop_event.is_set():
            zip_path.unlink(missing_ok=True)
            _mark(code, "pending")
            return "cancelled"
        try:
            with requests.get(zip_url, stream=True, timeout=TIMEOUT_SECS,
                              headers={"User-Agent": UA}) as r:
                if r.status_code == 404:
                    zip_path.unlink(missing_ok=True)
                    _mark(code, "missing")
                    _counters["missing"] += 1
                    _log(f"MISSING {code} (404 {zip_url})")
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
            # Some sheets come back as HTTP 200 with a 0-byte body (an empty
            # file ČÚZK never populated). Terminal upstream gap, not a retry.
            if zip_path.stat().st_size == 0:
                zip_path.unlink(missing_ok=True)
                _mark(code, "missing", note="empty 0-byte file from openzu")
                _counters["missing"] += 1
                _log(f"MISSING {code} (empty 0-byte upstream file)")
                return "missing"
            # Extract JPEG + world files, then drop the ZIP (it only wraps
            # the JPEG — keeping it would double on-disk size). A few sheets
            # nest everything one subdir deep, so flatten afterwards.
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(out_dir)
            zip_path.unlink(missing_ok=True)
            _flatten_extracted(out_dir)
            jpg = _existing_jpg(out_dir)
            if jpg is None:
                raise RuntimeError("ZIP extracted but no JPEG+JGW pair found")
            size_mb = jpg.stat().st_size // (1024 * 1024)
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
        for _ in range(60):  # 30 s, but cancellable every 0.5 s
            if stop_event.is_set():
                return
            time.sleep(0.5)
        done = _counters["ok"] + _counters["skip"]
        processed = done + _counters["missing"] + _counters["fail"]
        print(f"[{_now()}] progress: ok={_counters['ok']} skip={_counters['skip']} "
              f"missing={_counters['missing']} fail={_counters['fail']} "
              f"({processed}/{total} this session)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"parallel download workers (default {WORKERS})")
    args = ap.parse_args()
    workers = max(1, args.workers)

    if not STATE_FILE.exists():
        raise SystemExit(
            f"{STATE_FILE} not found — run bulk_ortofoto_inventory.py first.")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock():
        return 1
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        _load_state()
        # Re-arm 'downloading' entries left by an unclean exit.
        with state_lock:
            recovered = 0
            for code, entry in _state.items():
                if entry.get("status") == "downloading":
                    entry["status"] = "pending"
                    recovered += 1
        if recovered:
            print(f"Recovered {recovered} 'downloading' entries → pending")
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
