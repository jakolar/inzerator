"""V2 location pipeline: address search → enqueue → worker spawns
gen_panorama.py + 3× gen_detail.py → exposes job state to UI."""
from __future__ import annotations
import json
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TILES_DIR_PREFIX = "tiles_v2_"
JOB_LOG_DIR = Path("cache/jobs")
STEP_TIMEOUT_SECS = 60 * 60   # 60 min: cold ČÚZK DMR5G cache can take 10–30 min
STEP_NAMES = ("panorama", "outer", "closeup", "inner")

JOBS: dict[str, dict] = {}
JOB_QUEUE: list[str] = []
JOB_LOCK = threading.Lock()
JOB_CV = threading.Condition(JOB_LOCK)
CURRENT_JOB: str | None = None
CURRENT_PROC: subprocess.Popen | None = None
_WORKER_THREAD: threading.Thread | None = None

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def slugify(text: str) -> str:
    """NFD-rozdělit diakritiku, ASCII fold, převést mezery/non-alnum na pomlčky.
    Sekvence pomlček se collapsuje, trim leading/trailing dashes."""
    nfd = unicodedata.normalize("NFD", text)
    ascii_text = nfd.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return s


def is_valid_slug(s: str) -> bool:
    return bool(s) and bool(_SLUG_RE.match(s))


def next_free_slug(base: str, existing: set[str]) -> str:
    """Pokud `base` není v `existing`, vrátí `base`. Jinak hledá první volné
    `<base>-N` pro N=2,3,4,…"""
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def parse_obec(adresa: str) -> str:
    """Z RUIAN adresy '<něco>, <PSČ> <Obec>' vrátí název obce.
    Adresa má vždy alespoň jednu čárku a PSČ je 5 nebo 6 znaků (s mezerou)."""
    after_comma = adresa.rsplit(",", 1)[-1].strip()
    # Odstranit PSČ (5 číslic, případně s mezerou '100 00')
    return re.sub(r"^\d{3}\s?\d{2}\s+", "", after_comma).strip()


def expected_glb(slug: str, step: str) -> Path:
    if step == "panorama":
        return Path(f"{TILES_DIR_PREFIX}{slug}") / "panorama.glb"
    return Path(f"{TILES_DIR_PREFIX}{slug}") / "details" / f"{step}.glb"


def location_status(slug: str) -> str:
    """'missing' = adresář nebo panorama.glb chybí.
    'partial' = panorama.glb existuje ale chybí alespoň jeden detail.
    'ready'   = všechny 4 .glb existují."""
    pano = expected_glb(slug, "panorama")
    if not pano.exists():
        return "missing" if not pano.parent.exists() else "partial"
    for step in ("outer", "closeup", "inner"):
        if not expected_glb(slug, step).exists():
            return "partial"
    return "ready"


def _read_label(slug: str) -> str:
    """Zkusit přečíst manifest.json a vrátit region.label. Fallback = slug."""
    manifest = Path(f"{TILES_DIR_PREFIX}{slug}") / "manifest.json"
    if not manifest.exists():
        return slug
    try:
        data = json.loads(manifest.read_text())
        return data.get("region", {}).get("label") or slug
    except (json.JSONDecodeError, OSError):
        return slug


def list_locations() -> list[dict]:
    """Scan working dir pro 'tiles_v2_*' adresáře, vrátí list s
    {slug, label, status, has_panorama, has_outer, has_closeup, has_inner}."""
    out = []
    for path in sorted(Path(".").glob(f"{TILES_DIR_PREFIX}*")):
        if not path.is_dir():
            continue
        slug = path.name[len(TILES_DIR_PREFIX):]
        if not slug:
            continue
        out.append({
            "slug": slug,
            "label": _read_label(slug),
            "status": location_status(slug),
            "has_panorama": expected_glb(slug, "panorama").exists(),
            "has_outer":    expected_glb(slug, "outer").exists(),
            "has_closeup":  expected_glb(slug, "closeup").exists(),
            "has_inner":    expected_glb(slug, "inner").exists(),
        })
    return out


RUIAN_ADRESNI_MISTO_URL = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/1/query"


class RuianUnavailable(Exception):
    """ČÚZK RUIAN AdresniMisto service nedostupný (network / 5xx)."""


def _escape_like(q: str) -> str:
    """Escapuje single-quote (SQL injection) a procento/podtržítko (LIKE wildcardy)."""
    return q.replace("'", "''").replace("%", r"\%").replace("_", r"\_")


def ruian_search(q: str) -> list[dict]:
    """Volá ČÚZK RUIAN AdresniMisto LIKE query. Vrací list {label, sjtsk_cx,
    sjtsk_cy, obec}. Empty list = 0 hits. RuianUnavailable = network/5xx."""
    if not q.strip():
        return []
    escaped = _escape_like(q.strip())
    params = {
        "where": f"UPPER(adresa) LIKE UPPER('%{escaped}%')",
        "outFields": "adresa,psc,cislodomovni,kod",
        "outSR": "5514",
        "returnGeometry": "true",
        "resultRecordCount": "10",
        "f": "json",
    }
    url = RUIAN_ADRESNI_MISTO_URL + "?" + urllib.parse.urlencode(params)
    req = Request(url, headers={"User-Agent": "inzerator/1.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        raise RuianUnavailable(str(e)) from e

    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        adresa = attrs.get("adresa", "")
        if not adresa or "x" not in geom or "y" not in geom:
            continue
        out.append({
            "label": adresa,
            "sjtsk_cx": float(geom["x"]),
            "sjtsk_cy": float(geom["y"]),
            "obec": parse_obec(adresa),
        })
    return out


def _new_job(slug: str, label: str, cx: float, cy: float) -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "slug": slug,
        "label": label,
        "cx": cx,
        "cy": cy,
        "cancelled": False,
        "created_at": time.time(),
        "steps": [
            {"name": name, "state": "pending", "error": None,
             "started_at": None, "finished_at": None}
            for name in STEP_NAMES
        ],
    }


def enqueue_job(slug: str, label: str, cx: float, cy: float) -> str | None:
    """Add new job. Returns job_id, or None if a non-terminal job with the
    same slug is already queued or running (caller maps to 409).

    Check for existing active job is atomic under JOB_CV, closing TOCTOU
    race where two concurrent POSTs could both pass disk status check
    and enqueue duplicate jobs."""
    with JOB_CV:
        # Check for existing active job with this slug
        for jid in JOB_QUEUE:
            existing = JOBS.get(jid)
            if existing and existing["slug"] == slug:
                return None
        if CURRENT_JOB is not None:
            existing = JOBS.get(CURRENT_JOB)
            if existing and existing["slug"] == slug:
                return None
        job = _new_job(slug, label, cx, cy)
        JOBS[job["job_id"]] = job
        JOB_QUEUE.append(job["job_id"])
        JOB_CV.notify()   # vzbudí worker
    return job["job_id"]


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


def list_active_jobs() -> list[dict]:
    """Vrátí všechny joby, které jsou v queue nebo právě běží
    (CURRENT_JOB). Každá položka má queue_position (0 = right now / next)."""
    with JOB_LOCK:
        out = []
        # Currently running first (queue_position = -1 nebo 0 podle konvence)
        if CURRENT_JOB and CURRENT_JOB in JOBS:
            job = JOBS[CURRENT_JOB]
            out.append({**job, "queue_position": -1})
        for i, jid in enumerate(JOB_QUEUE):
            if jid in JOBS:
                out.append({**JOBS[jid], "queue_position": i})
        return out


def retry_job(job_id: str) -> bool:
    """Reset `fail` stepy zpět na `pending`, znovu zařadit do fronty.
    Worker resume-skipne hotové stepy (existence .glb na disku).
    Vrátí True pokud nalezeno, False pokud job_id neznámé."""
    with JOB_CV:
        job = JOBS.get(job_id)
        if job is None:
            return False
        # Neretrujeme job, který je právě běžící (CURRENT_JOB)
        if CURRENT_JOB == job_id:
            return False
        if job_id in JOB_QUEUE:
            return True   # už je ve frontě, idempotent
        for step in job["steps"]:
            if step["state"] in ("fail", "cancelled"):
                step["state"] = "pending"
                step["error"] = None
                step["started_at"] = None
                step["finished_at"] = None
        job["cancelled"] = False
        JOB_QUEUE.append(job_id)
        JOB_CV.notify()
        return True


def cancel_job(job_id: str) -> bool:
    """Zruší job. Pokud queued: vyhodí z fronty. Pokud running: nastaví
    `cancelled = True`, worker zabije proces po dokončení aktuálního I/O
    polling cyklu (max 10 s po terminate(), pak kill()).
    Vrátí False pokud job už hotový (ok / fail final state)."""
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return False
        if all(s["state"] in ("ok", "fail", "cancelled", "skipped") for s in job["steps"]):
            return False   # už hotový
        job["cancelled"] = True
        if job_id in JOB_QUEUE:
            JOB_QUEUE.remove(job_id)
        if CURRENT_JOB == job_id and CURRENT_PROC is not None:
            try:
                CURRENT_PROC.terminate()
                # 10 s grace, pak kill
                _proc = CURRENT_PROC
                threading.Timer(
                    10.0,
                    lambda: _proc.kill() if _proc.poll() is None else None
                ).start()
            except (ProcessLookupError, AttributeError):
                pass
        return True


# ---------------------------------------------------------------------------
# LOD presets + subprocess command builder
# ---------------------------------------------------------------------------

# Hnojice-preset LOD parametry (fixní pro všechny lokace).
# Inner step 0.5 m kvůli zarovnání s gen_multitile village_flat profilem.
_LOD_PRESET = {
    "outer":   {"half": 2500, "step": "2.5", "fade": 100, "fade_to": "panorama", "zoom": 17, "size": 4096},
    "closeup": {"half": 1500, "step": "1.5", "fade":  50, "fade_to": "outer",    "zoom": 21, "size": 8192},
    "inner":   {"half":  500, "step": "0.5", "fade":  30, "fade_to": "closeup",  "zoom": 21, "size": 8192},
}


def cmd_for(step: str, slug: str, cx: float, cy: float) -> list[str]:
    """Vyrobí subprocess command pro daný step. `--center-sjtsk=cx,cy`
    musí mít `=` syntax (argparse jinak parsuje negativní cx jako flag)."""
    base = ["python3"]
    center = f"--center-sjtsk={cx},{cy}"
    if step == "panorama":
        return base + ["gen_panorama.py", "--region", slug, center]
    p = _LOD_PRESET[step]
    return base + [
        "gen_detail.py",
        "--region", slug,
        "--slug", step,
        center,
        "--half", str(p["half"]),
        "--step", p["step"],
        "--fade", str(p["fade"]),
        "--fade-to", p["fade_to"],
        "--zoom", str(p["zoom"]),
        "--size", str(p["size"]),
    ]


def _format_error(stderr: str) -> str:
    """Pythonský traceback má informaci na začátku (file/line) i na konci
    (exception class + message). Vrátíme head 300 + tail 300, pokud > 600."""
    if len(stderr) <= 600:
        return stderr
    return stderr[:300] + "\n... [truncated] ...\n" + stderr[-300:]


def _run_step(job: dict, step: dict) -> bool:
    """Spustí jeden step jako subprocess. Vrátí True pokud ok/skipped,
    False pokud fail/cancelled (= worker má break).
    Pokud step skončí ve stavu fail/cancelled, smaže částečný .glb soubor
    aby retry neviděl corrupted file jako skipped."""
    global CURRENT_PROC
    expected = expected_glb(job["slug"], step["name"])
    if expected.exists():
        step["state"] = "skipped"
        return True

    step["state"] = "running"
    step["started_at"] = time.time()
    cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"])
    log_dir = JOB_LOG_DIR / job["job_id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{step['name']}.log"

    try:
        try:
            with JOB_LOCK:
                # Protect CURRENT_PROC write so cancel_job (HTTP thread) sees it
                # immediately after Popen — closes race between spawn and terminate.
                CURRENT_PROC = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
        except OSError as e:
            step["state"] = "fail"
            step["error"] = f"spawn failed: {e}"
            return False

        try:
            out, err = CURRENT_PROC.communicate(timeout=STEP_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            CURRENT_PROC.kill()
            out, err = CURRENT_PROC.communicate()
            log_path.write_text((out or "") + "\n--- STDERR ---\n" + (err or ""))
            step["state"] = "fail"
            step["error"] = f"timeout {STEP_TIMEOUT_SECS // 60}m"
            step["finished_at"] = time.time()
            return False

        log_path.write_text((out or "") + "\n--- STDERR ---\n" + (err or ""))
        step["finished_at"] = time.time()

        if job["cancelled"]:
            step["state"] = "cancelled"
            return False
        if CURRENT_PROC.returncode == 0 and expected.exists():
            step["state"] = "ok"
            return True
        step["state"] = "fail"
        step["error"] = _format_error(err or f"exit {CURRENT_PROC.returncode}, .glb missing")
        return False
    finally:
        # Drop partial .glb so retry doesn't see corrupted/half-written file as skipped
        if step["state"] in ("fail", "cancelled") and expected.exists():
            try:
                expected.unlink()
            except OSError:
                pass


def _run_one_job(job_id: str) -> None:
    """Zpracuje jeden job ze začátku do konce / do fail/cancel.
    Volá worker_loop i test helper."""
    global CURRENT_JOB, CURRENT_PROC
    job = JOBS.get(job_id)
    if job is None or job["cancelled"]:
        return
    with JOB_LOCK:
        # Set CURRENT_JOB atomically under lock, so cancel_job cannot observe
        # an inconsistent state.
        CURRENT_JOB = job_id
    CURRENT_PROC = None
    try:
        for step in job["steps"]:
            if job["cancelled"]:
                step["state"] = "cancelled"
                break
            if not _run_step(job, step):
                break
    finally:
        with JOB_LOCK:
            CURRENT_JOB = None
            CURRENT_PROC = None


def _run_one_job_for_test() -> None:
    """Helper: spotřebuje 1 job z fronty + zpracuje sériově (bez threadingu).
    Používá se v unit testech."""
    with JOB_LOCK:
        if not JOB_QUEUE:
            return
        job_id = JOB_QUEUE.pop(0)
    _run_one_job(job_id)


def worker_loop() -> None:
    """Background thread: čeká na frontu, spotřebuje joby."""
    while True:
        with JOB_CV:
            while not JOB_QUEUE:
                JOB_CV.wait()
            job_id = JOB_QUEUE.pop(0)
        _run_one_job(job_id)


def start_worker() -> threading.Thread:
    """Spustí worker thread jako daemon. Vrátí Thread objekt (idempotent)."""
    global _WORKER_THREAD
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return _WORKER_THREAD
    _WORKER_THREAD = threading.Thread(target=worker_loop, daemon=True, name="job-worker")
    _WORKER_THREAD.start()
    return _WORKER_THREAD
