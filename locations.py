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
STEP_NAMES = ("panorama", "sm5", "outer", "closeup", "inner", "compress")

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
    """For 'sm5' and 'compress' returns a sentinel marker (no actual GLB).
    Worker's resume-skip uses .exists() on this so the sentinel-vs-glb
    distinction is transparent to the loop."""
    base = Path(f"{TILES_DIR_PREFIX}{slug}")
    if step == "panorama":
        return base / "panorama.glb"
    if step == "sm5":
        return base / ".sm5_ok"
    if step == "compress":
        return base / ".compress_ok"
    return base / "details" / f"{step}.glb"


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
SM5_KLADY_URL = "https://ags.cuzk.cz/arcgis/rest/services/KladyMapovychListu/MapServer/25/query"


class RuianUnavailable(Exception):
    """ČÚZK RUIAN AdresniMisto service nedostupný (network / 5xx)."""


def _escape_like(q: str) -> str:
    """Escapuje single-quote (SQL injection) a procento/podtržítko (LIKE wildcardy)."""
    return q.replace("'", "''").replace("%", r"\%").replace("_", r"\_")


def ruian_search(q: str) -> list[dict]:
    """Volá ČÚZK RUIAN AdresniMisto LIKE query. Vrací list {label, sjtsk_cx,
    sjtsk_cy, obec}. Empty list = 0 hits. RuianUnavailable = network/5xx.

    Multi-token search: input se rozdělí na slova (whitespace + čárky) a
    spojí AND-řetězcem LIKE klauzulí. Tj. 'Stebno 74 Kryry' najde
    'Stebno 74, 44101 Kryry' i když uživatel vynechá PSČ. Case-insensitive
    přes UPPER() na obou stranách."""
    if not q.strip():
        return []
    tokens = [t for t in re.split(r"[\s,]+", q.strip()) if t]
    if not tokens:
        return []
    clauses = [
        f"UPPER(adresa) LIKE UPPER('%{_escape_like(t)}%')"
        for t in tokens
    ]
    where = " AND ".join(clauses)
    params = {
        "where": where,
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


def _resolve_sm5_codes(cx: float, cy: float, half: float = 2500) -> list[str]:
    """Query ČÚZK KladyMapovychListu for SM5 sheet MAPNOM codes covering
    the envelope (cx ± half, cy ± half) in S-JTSK. Returns list of codes
    like ['JESE44', 'JESE45', ...]. Raises RuianUnavailable on network
    failure (reuses the same exception — same ČÚZK ArcGIS infrastructure)."""
    geometry = json.dumps({
        "xmin": cx - half, "ymin": cy - half,
        "xmax": cx + half, "ymax": cy + half,
        "spatialReference": {"wkid": 5514},
    })
    params = {
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "5514",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "MAPNOM",
        "outSR": "5514",
        "f": "json",
    }
    url = SM5_KLADY_URL + "?" + urllib.parse.urlencode(params)
    req = Request(url, headers={"User-Agent": "inzerator/1.0"})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        raise RuianUnavailable(f"sheet lookup failed: {e}") from e
    codes = []
    for feat in data.get("features", []):
        code = feat.get("attributes", {}).get("MAPNOM")
        if code:
            codes.append(code)
    return codes


def _do_sm5_download(job: dict, log_path: Path) -> bool:
    """In-process 'sheets' step: resolve MAPNOM codes for outer-ring bbox
    (half=2500), then download BOTH DMPOK-TIFF (SM5 height data used by
    gen_detail.py) AND raw ortofoto JPEGs (consumed at runtime by
    server.py _proxy_ortofoto_raw — the tile service / ESRI / Google
    fallbacks aren't reliably reachable from every network, raw files
    sidestep that). Touches `.sm5_ok` sentinel when both are complete."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        # Best-effort incremental write so user can tail the log
        try:
            log_path.write_text("\n".join(log_lines) + "\n")
        except OSError:
            pass

    try:
        log(f"Resolving sheets for envelope (cx={job['cx']}, cy={job['cy']}, half=2500)…")
        codes = _resolve_sm5_codes(job["cx"], job["cy"], half=2500)
        log(f"ČÚZK returned {len(codes)} sheet(s): {', '.join(codes)}")
    except RuianUnavailable as e:
        log(f"FAIL: {e}")
        return False

    if not codes:
        log("FAIL: no SM5 sheets cover this envelope — location is outside ČR coverage?")
        return False

    # Import lazily so locations.py doesn't fail to import if the
    # downloader dependencies are missing in a test environment.
    import download_tiff
    import download_ortofoto

    cache_root = Path("cache")

    def _with_retries(label, fn, attempts=3):
        """Retry transient network failures with exponential backoff —
        ČÚZK openzu/atom often returns Connection refused for a single
        request while neighbouring ones succeed. 3 attempts × backoff
        (1 s, 3 s) covers the typical glitch without holding the whole
        pipeline hostage for hours."""
        import time as _t
        last = None
        for attempt in range(1, attempts + 1):
            try:
                fn()
                return True
            except Exception as e:
                last = e
                if attempt < attempts:
                    backoff = attempt * 2 - 1   # 1, 3
                    short = str(e)[:120]
                    log(f"  {label} attempt {attempt}/{attempts} failed: {short}; retry in {backoff}s")
                    _t.sleep(backoff)
        log(f"FAIL: {label} after {attempts} attempts: {last}")
        return False

    # 1) DMPOK-TIFF (SM5 DSM with buildings) — needed by gen_detail.py
    for i, code in enumerate(codes, 1):
        tif_path = cache_root / f"dmpok_tiff_{code}" / f"{code}.tif"
        if tif_path.exists():
            log(f"[SM5 {i}/{len(codes)}] {code} — cached, skip")
            continue
        log(f"[SM5 {i}/{len(codes)}] {code} — downloading…")
        if not _with_retries(f"{code} DMPOK-TIFF",
                             lambda c=code: download_tiff.download_tiff(c)):
            return False
        log(f"[SM5 {i}/{len(codes)}] {code} — done")

    # 2) Raw ortofoto JPEG — needed by server _proxy_ortofoto_raw at runtime
    for i, code in enumerate(codes, 1):
        ortho_dir = cache_root / f"ortofoto_{code}"
        if ortho_dir.exists() and any(ortho_dir.glob("*.jpg")):
            log(f"[ORTO {i}/{len(codes)}] {code} — cached, skip")
            continue
        log(f"[ORTO {i}/{len(codes)}] {code} — downloading…")
        if not _with_retries(f"{code} ortofoto",
                             lambda c=code: download_ortofoto.download(c, cache_root)):
            return False
        log(f"[ORTO {i}/{len(codes)}] {code} — done")

    # Write sentinel
    sentinel = expected_glb(job["slug"], "sm5")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    log("OK: all SM5 + ortofoto sheets ready; sentinel written")
    return True


def _do_compress(job: dict, log_path: Path) -> bool:
    """In-process 'compress' step: Draco-encode each detail GLB.
    gen_detail.py writes uncompressed glb (~168 MB for inner @ 0.5 m);
    Draco at quant_pos=16 brings that to ~30–40 MB with sub-cm position
    error (~3 % of mesh step). Per-detail flow:
      1. If <slug>/_orig_uncompressed/<step>.glb already exists, skip
         (idempotent — previous run finished this child).
      2. Otherwise move <slug>/details/<step>.glb to _orig_uncompressed/
         and Draco-compress back into details/.
    Writes .compress_ok sentinel when all three details are done."""
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        try:
            log_path.write_text("\n".join(log_lines) + "\n")
        except OSError:
            pass

    # Lazy import — keeps test environments without DracoPy importable.
    try:
        import draco_compress_glb
    except ImportError as e:
        log(f"FAIL: draco_compress_glb import failed: {e}")
        return False

    region_dir = Path(f"{TILES_DIR_PREFIX}{job['slug']}")
    details_dir = region_dir / "details"
    orig_dir = region_dir / "_orig_uncompressed"
    orig_dir.mkdir(parents=True, exist_ok=True)

    details = ("outer", "closeup", "inner")
    for slug in details:
        compressed_path = details_dir / f"{slug}.glb"
        orig_path = orig_dir / f"{slug}.glb"

        if orig_path.exists():
            log(f"[{slug}] already compressed (orig stored), skip")
            continue
        if not compressed_path.exists():
            log(f"FAIL: {slug} — no source .glb at {compressed_path}")
            return False

        size_before = compressed_path.stat().st_size
        log(f"[{slug}] {size_before // (1024*1024)} MB → compressing…")

        # Atomic-ish: move source aside FIRST so we don't lose data if
        # Draco encode crashes. If compression fails below, the original
        # is still in _orig_uncompressed/ and the next retry will see
        # `compressed_path.exists() == False`, treat as fail, and surface
        # the issue rather than silently re-running.
        try:
            compressed_path.rename(orig_path)
        except OSError as e:
            log(f"FAIL: {slug} — rename source aside: {e}")
            return False

        try:
            draco_compress_glb.compress(str(orig_path), str(compressed_path))
            size_after = compressed_path.stat().st_size
            log(f"[{slug}] → {size_after // (1024*1024)} MB "
                f"({100 * size_after // size_before}% of original)")
        except Exception as e:
            log(f"FAIL: {slug} — Draco compress: {e}")
            # Restore the uncompressed file so retry doesn't see a hole.
            try:
                orig_path.rename(compressed_path)
            except OSError:
                pass
            return False

    sentinel = expected_glb(job["slug"], "compress")
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    log("OK: all 3 details Draco-compressed; sentinel written")
    return True


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
                # 10 s grace, pak kill. Daemon thread so it doesn't keep
                # the process alive past server shutdown.
                _proc = CURRENT_PROC
                _t = threading.Timer(
                    10.0,
                    lambda: _proc.kill() if _proc.poll() is None else None
                )
                _t.daemon = True
                _t.start()
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
    musí mít `=` syntax (argparse jinak parsuje negativní cx jako flag).
    Raises ValueError for 'sm5' and 'compress' — those are in-process,
    not subprocess steps."""
    if step in ("sm5", "compress"):
        raise ValueError(f"{step!r} is in-process, no subprocess command")
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
    """Run one step. Returns True on success (state ok/skipped), False on
    fail/cancelled (worker should break).
    On fail/cancelled, drops partial output file so retry doesn't skip it."""
    global CURRENT_PROC
    expected = expected_glb(job["slug"], step["name"])
    if expected.exists():
        now = time.time()
        step["state"] = "skipped"
        step["started_at"] = now
        step["finished_at"] = now
        return True

    step["state"] = "running"
    step["started_at"] = time.time()
    log_dir = JOB_LOG_DIR / job["job_id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{step['name']}.log"

    # === In-process steps: sm5, compress ===
    _inproc_handlers = {
        "sm5": _do_sm5_download,
        "compress": _do_compress,
    }
    handler = _inproc_handlers.get(step["name"])
    if handler is not None:
        try:
            ok = handler(job, log_path)
        except Exception as e:
            ok = False
            step["error"] = f"{step['name']} internal error: {e}"
            existing_text = log_path.read_text() if log_path.exists() else ""
            log_path.write_text(existing_text + f"\nException: {e}")
        step["finished_at"] = time.time()
        if job["cancelled"]:
            step["state"] = "cancelled"
            if expected.exists():
                try:
                    expected.unlink()
                except OSError:
                    pass
            return False
        if ok:
            step["state"] = "ok"
            return True
        step["state"] = "fail"
        if not step.get("error"):
            step["error"] = f"{step['name']} failed (see log)"
        if expected.exists():
            try:
                expected.unlink()
            except OSError:
                pass
        return False

    # === Subprocess step: panorama, outer, closeup, inner ===
    cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"])

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
        # Drop partial output so retry doesn't see corrupted/half-written file as skipped
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
    """Background thread: čeká na frontu, spotřebuje joby.

    Wrapped in try/except so an unhandled exception in a step (disk full
    writing a log, PermissionError on an existing dir, future bugs in
    _run_step …) doesn't kill the thread — that would leave the queue
    silently un-serviced with no error visible to the UI. The job that
    raised is lost (already popped from queue, not requeued) but future
    work keeps running."""
    import traceback as _tb
    while True:
        try:
            with JOB_CV:
                while not JOB_QUEUE:
                    JOB_CV.wait()
                job_id = JOB_QUEUE.pop(0)
            _run_one_job(job_id)
        except Exception:
            _tb.print_exc()


def start_worker() -> threading.Thread:
    """Spustí worker thread jako daemon. Vrátí Thread objekt (idempotent)."""
    global _WORKER_THREAD
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return _WORKER_THREAD
    _WORKER_THREAD = threading.Thread(target=worker_loop, daemon=True, name="job-worker")
    _WORKER_THREAD.start()
    return _WORKER_THREAD
