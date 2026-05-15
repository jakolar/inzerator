"""V2 location pipeline: address search → enqueue → worker spawns
gen_panorama.py + 3× gen_detail.py → exposes job state to UI."""
from __future__ import annotations
import json
import re
import threading
import unicodedata
from pathlib import Path

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
