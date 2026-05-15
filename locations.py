"""V2 location pipeline: address search → enqueue → worker spawns
gen_panorama.py + 3× gen_detail.py → exposes job state to UI."""
from __future__ import annotations
import json
import re
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


def enqueue_job(slug: str, label: str, cx: float, cy: float) -> str:
    """Přidá nový job do JOBS + JOB_QUEUE. Vrátí job_id.
    Neověřuje slug-collision — to volá vyšší vrstva (HTTP handler)."""
    job = _new_job(slug, label, cx, cy)
    with JOB_CV:
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
