"""V2 location pipeline: address search → enqueue → worker spawns
gen_panorama.py + 3× gen_detail.py → exposes job state to UI."""
from __future__ import annotations
import json
import math
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
STEP_NAMES = ("sm5", "heightfield")

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
    """Sentinel/artifact path used by run_step's resume-skip to .exists()-
    check whether the step is already done. Two steps in the heightfield-
    only pipeline:
      - sm5:        zero-byte `.sm5_ok` sentinel under the slug dir
      - heightfield: real heightfield manifest at heightfield/manifest.json
    """
    base = Path(f"{TILES_DIR_PREFIX}{slug}")
    if step == "sm5":
        return base / ".sm5_ok"
    if step == "heightfield":
        return base / "heightfield" / "manifest.json"
    raise ValueError(f"unknown step: {step!r}")


def location_status(slug: str) -> str:
    """missing = slug directory absent.
    partial = directory present but heightfield manifest not (yet) written
              (either generation in flight or interrupted mid-pipeline).
    ready   = heightfield manifest on disk → viewer can open the location."""
    base = Path(f"{TILES_DIR_PREFIX}{slug}")
    if not base.is_dir():
        return "missing"
    if expected_glb(slug, "heightfield").exists():
        return "ready"
    return "partial"


def _persist_location_meta(slug: str, label: str, cx: float, cy: float,
                           inner_half: float | None = None,
                           parcel_ids: list | None = None) -> None:
    """Write tiles_v2_<slug>/location.json with {slug, label, cx, cy} so
    the label survives the only on-disk persistence path we have now that
    the v2 top-level manifest is no longer written. Called at job enqueue
    so the dashboard sees the right label even before sm5 finishes."""
    base = Path(f"{TILES_DIR_PREFIX}{slug}")
    base.mkdir(parents=True, exist_ok=True)
    meta = {"slug": slug, "label": label, "cx": cx, "cy": cy,
            "created_at": time.time()}
    if inner_half is not None:
        meta["inner_half"] = inner_half
    if parcel_ids:
        meta["subject_parcels"] = list(parcel_ids)
    out = base / "location.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(out)


def _read_label(slug: str) -> str:
    """Read label for a slug. Order:
      1. tiles_v2_<slug>/location.json (current path, written by
         _persist_location_meta at job enqueue).
      2. tiles_v2_<slug>/manifest.json (legacy v2 manifest;
         region.label was set by gen_panorama.py before v2 retirement).
      3. fallback to slug itself."""
    base = Path(f"{TILES_DIR_PREFIX}{slug}")
    for path, extract in (
        (base / "location.json", lambda d: d.get("label")),
        (base / "manifest.json", lambda d: (d.get("region") or {}).get("label")),
    ):
        if path.is_file():
            try:
                lbl = extract(json.loads(path.read_text())) or None
                if lbl:
                    return lbl
            except (json.JSONDecodeError, OSError):
                pass
    return slug


def list_locations() -> list[dict]:
    """Scan working dir for `tiles_v2_*` directories. Returns a list of
    {slug, label, status, has_heightfield, modified_ts}, sorted newest-
    first by modified_ts. mtime fallback chain:
    heightfield/manifest.json → tile dir → 0.
    """
    out = []
    for path in Path(".").glob(f"{TILES_DIR_PREFIX}*"):
        if not path.is_dir():
            continue
        slug = path.name[len(TILES_DIR_PREFIX):]
        if not slug:
            continue
        hf_manifest = path / "heightfield" / "manifest.json"
        try:
            ts = hf_manifest.stat().st_mtime if hf_manifest.is_file() else path.stat().st_mtime
        except OSError:
            ts = 0
        out.append({
            "slug": slug,
            "label": _read_label(slug),
            "status": location_status(slug),
            "has_heightfield": hf_manifest.exists(),
            "modified_ts": ts,
        })
    out.sort(key=lambda d: d["modified_ts"], reverse=True)
    return out


RUIAN_ADRESNI_MISTO_URL = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/1/query"
RUIAN_PARCELA_URL = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/0/query"
RUIAN_KU_URL = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/7/query"
SM5_KLADY_URL = "https://ags.cuzk.cz/arcgis/rest/services/KladyMapovychListu/MapServer/25/query"

# (kod, nazev, folded_nazev). Lazy-loaded on first KÚ lookup; ~13k entries.
_KU_CACHE: list | None = None
_KU_CACHE_LOCK = threading.Lock()

_NUMERIC_TOKEN_RE = re.compile(r"\d")
_PARCEL_TOKEN_RE = re.compile(r"^\d+(/\d+)?$")


def _ascii_fold(s: str) -> str:
    """NFD + drop combining marks + lowercase. 'Fügnerova Děčín' →
    'fugnerova decin'. Used for diacritic-insensitive substring match
    on RUIAN results — ČÚZK ArcGIS has no UPPER-like fold function."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower()


_SJTSK_TO_WGS = None

def _sjtsk_to_wgs(cx: float, cy: float) -> tuple[float, float]:
    """Convert EPSG:5514 (S-JTSK / Krovak East-North) → (lat, lon) WGS84.
    Lazy-init the Transformer (importing pyproj is ~30 ms, skip in tests
    that don't need it)."""
    global _SJTSK_TO_WGS
    if _SJTSK_TO_WGS is None:
        from pyproj import Transformer
        _SJTSK_TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
    lon, lat = _SJTSK_TO_WGS.transform(cx, cy)
    return lat, lon


class RuianUnavailable(Exception):
    """ČÚZK RUIAN AdresniMisto service nedostupný (network / 5xx)."""


def _escape_like(q: str) -> str:
    """Escapuje single-quote (SQL injection) a procento/podtržítko (LIKE wildcardy)."""
    return q.replace("'", "''").replace("%", r"\%").replace("_", r"\_")


def _ruian_get(url, timeout=15):
    # ČÚZK občas zavře první spojení (Errno 61 Connection refused) a druhé
    # projde. Retry 5× s exponenciálním backoffem (0.5 + 1 + 2 + 4 + 8 =
    # 15.5 s total). Bez retry kullen každý address search při ČÚZK flake.
    req = Request(url, headers={"User-Agent": "inzerator/1.0"})
    import time as _t
    last_err = None
    for attempt in range(5):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < 4:
                _t.sleep(0.5 * (2 ** attempt))
    raise RuianUnavailable(str(last_err)) from last_err


def _address_query(where_clauses: list[str], record_count: int) -> list[dict]:
    """Single AdresniMisto query helper — issue one ČÚZK call with the given
    WHERE clauses joined by AND, return raw features list."""
    params = {
        "where": " AND ".join(where_clauses),
        "outFields": "adresa,psc,cislodomovni,kod",
        "outSR": "5514",
        "returnGeometry": "true",
        "resultRecordCount": str(record_count),
        "f": "json",
    }
    url = RUIAN_ADRESNI_MISTO_URL + "?" + urllib.parse.urlencode(params)
    data = _ruian_get(url)
    return data.get("features", [])


def _features_to_hits(features: list[dict], folded_tokens: list[str] | None) -> list[dict]:
    """Convert ČÚZK address features → search hits, optionally fold-filtering
    each candidate against the user's tokens. Returns at most 10."""
    out = []
    for feat in features:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        adresa = attrs.get("adresa", "")
        if not adresa or "x" not in geom or "y" not in geom:
            continue
        if folded_tokens is not None:
            folded_adr = _ascii_fold(adresa)
            if not all(t in folded_adr for t in folded_tokens):
                continue
        cx = float(geom["x"]); cy = float(geom["y"])
        lat, lon = _sjtsk_to_wgs(cx, cy)
        out.append({
            "kind": "address",
            "label": adresa,
            "sjtsk_cx": cx, "sjtsk_cy": cy,
            "wgs_lat": lat, "wgs_lon": lon,
            "obec": parse_obec(adresa),
        })
        if len(out) >= 10:
            break
    return out


def _search_addresses(tokens: list[str]) -> list[dict]:
    """Address layer (AdresniMisto, MapServer/1) search.

    Two-phase to balance precision with diacritic tolerance:

    1. **Literal LIKE on ALL tokens** server-side. Fast, exact. Works for
       inputs where the obec / street has no diacritics or the user typed
       them correctly ('Hilmarovo 55 Kopidlno' → 1 hit out of 95k '55'
       candidates because 'Hilmarovo' narrows server-side).

    2. **Fallback on 0 hits when a numeric token exists**: re-query with
       only the numeric LIKE (diacritic-invariant), fetch broader (1000
       rows), Python-fold and substring-match every token. Handles
       'Fugnerova 355/16 Decin' where ČÚZK has 'Děčín'/'Fügnerova' with
       diacritics — literal LIKE would miss but the broad numeric-keyed
       fetch + fold catches them.

    Pure-text queries with 0 hits just return [] — there's no fallback
    that helps without a numeric anchor (broad fetch of every address is
    ~5M rows, not feasible)."""
    if not tokens:
        return []

    # Phase 1 — literal LIKE on all tokens. 10 rows is plenty when the
    # server filter already narrows by the unique tokens.
    literal_clauses = [
        f"UPPER(adresa) LIKE UPPER('%{_escape_like(t)}%')"
        for t in tokens
    ]
    features = _address_query(literal_clauses, record_count=10)
    hits = _features_to_hits(features, folded_tokens=None)
    if hits:
        return hits

    # Phase 2 — diacritic-tolerant fallback. Only meaningful with a numeric
    # anchor (otherwise we'd fetch the entire address layer).
    numeric = [t for t in tokens if _NUMERIC_TOKEN_RE.search(t)]
    if not numeric:
        return []
    broad_clauses = [
        f"UPPER(adresa) LIKE UPPER('%{_escape_like(t)}%')"
        for t in numeric
    ]
    features = _address_query(broad_clauses, record_count=1000)
    folded_tokens = [_ascii_fold(t) for t in tokens]
    return _features_to_hits(features, folded_tokens=folded_tokens)


def _fetch_all_ku() -> list[tuple[int, str]]:
    """Stáhne celý seznam (kod, nazev) z KÚ layeru (~13k záznamů, 2026:13074).
    Paginated po 2000; raises RuianUnavailable on network error. Pro testy
    se mockuje urlopen + locations._KU_CACHE = None."""
    out: list[tuple[int, str]] = []
    offset = 0
    page_size = 2000
    while True:
        params = {
            "where": "1=1",
            "outFields": "kod,nazev",
            "returnGeometry": "false",
            "resultRecordCount": str(page_size),
            "resultOffset": str(offset),
            "f": "json",
        }
        url = RUIAN_KU_URL + "?" + urllib.parse.urlencode(params)
        data = _ruian_get(url, timeout=30)
        features = data.get("features", [])
        for feat in features:
            attrs = feat.get("attributes", {})
            kod = attrs.get("kod")
            nazev = attrs.get("nazev")
            if kod is not None and nazev:
                out.append((int(kod), nazev))
        if len(features) < page_size:
            break
        offset += len(features)
    return out


def _get_ku_cache() -> list[tuple[int, str, str]]:
    """Lazy-init thread-safe cache (kod, nazev, folded_nazev). První hit
    udělá full download (≈ 13k řádků, ~0.5 s pásmo). RuianUnavailable
    propaguje do caller."""
    global _KU_CACHE
    with _KU_CACHE_LOCK:
        if _KU_CACHE is None:
            entries = _fetch_all_ku()
            _KU_CACHE = [(k, n, _ascii_fold(n)) for k, n in entries]
        return _KU_CACHE


def _resolve_ku_codes(obec_tokens: list[str]) -> list[int]:
    """Vrátí kódy KÚ, jejichž název obsahuje VŠECHNY tokeny (po fold)
    jako substring. Empty list pokud nic nesedí.
    'Stribrnice' (bez háčků) → kódy pro 'Stříbrnice' i 'Stříbrnice u UH'."""
    if not obec_tokens:
        return []
    folded_tokens = [_ascii_fold(t) for t in obec_tokens]
    return [kod for kod, _nazev, folded in _get_ku_cache()
            if all(t in folded for t in folded_tokens)]


def _search_parcels(tokens: list[str]) -> list[dict]:
    """Parcela layer (ParcelaDefinicniBod, MapServer/0) search. Fires
    when at least one token matches '<int>' or '<int>/<int>' — the
    canonical parcel-number format (kmenovecislo/poddelenicisla).

    Other tokens are obec/KÚ filter. ArcGIS LIKE is diacritic-sensitive,
    so we cannot fold server-side; instead we resolve obec text → KÚ kód
    via cached layer 7 download (`_resolve_ku_codes`) and narrow the
    parcel query with `katastralniuzemi IN (codes)`. This sidesteps the
    200-row server cap that previously dropped Stříbrnice-style entries
    when 'cisloparcely' matched hundreds of parcels nationwide."""
    parcel_tokens = [t for t in tokens if _PARCEL_TOKEN_RE.match(t)]
    if not parcel_tokens:
        return []

    other_tokens = [t for t in tokens if not _PARCEL_TOKEN_RE.match(t)]

    # If user provided obec text, resolve to KÚ codes. No match → empty
    # result (don't fan out ČR-wide; that just buries the right hit again).
    if other_tokens:
        ku_codes = _resolve_ku_codes(other_tokens)
        if not ku_codes:
            return []
    else:
        ku_codes = None

    clauses = [f"cisloparcely = '{_escape_like(t)}'" for t in parcel_tokens]
    if ku_codes:
        # IN clause; 200-code cap keeps URL well under 2 kB even with 7-digit
        # codes. In practice obec text narrows to 1-5 KÚ; the cap is defensive.
        codes_list = ",".join(str(k) for k in ku_codes[:200])
        clauses.append(f"katastralniuzemi IN ({codes_list})")

    params = {
        "where": " AND ".join(clauses),
        "outFields": "cisloparcely,katastralniuzemicisloparcely,vymeraparcely",
        "outSR": "5514",
        "returnGeometry": "true",
        "resultRecordCount": "200",
        "f": "json",
    }
    url = RUIAN_PARCELA_URL + "?" + urllib.parse.urlencode(params)
    data = _ruian_get(url)

    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        label = attrs.get("katastralniuzemicisloparcely") or attrs.get("cisloparcely", "")
        if not label or "x" not in geom or "y" not in geom:
            continue
        # `katastralniuzemicisloparcely` = "<obec> <cisloparcely>", so the
        # obec is everything before the trailing parcel number.
        obec = label.rsplit(" ", 1)[0] if " " in label else label
        cx = float(geom["x"]); cy = float(geom["y"])
        lat, lon = _sjtsk_to_wgs(cx, cy)
        out.append({
            "kind": "parcel",
            "label": f"Parcela {label} ({attrs.get('vymeraparcely', '?')} m²)",
            "sjtsk_cx": cx, "sjtsk_cy": cy,
            "wgs_lat": lat, "wgs_lon": lon,
            "obec": obec,
        })
        if len(out) >= 10:
            break
    return out


def ruian_search(q: str, kind: str = "all") -> list[dict]:
    """Top-level RUIAN search — combines address + parcel hits. Tokens
    split on whitespace + commas. Returns list of {kind, label, sjtsk_cx,
    sjtsk_cy, obec}. Empty list = 0 hits. RuianUnavailable = network/5xx.

    `kind`:
      - 'all'     (default) — parcel + address layers; preserves legacy behaviour
      - 'parcel'  — only ParcelaDefinicniBod (MapServer/0) + KÚ resolver
      - 'address' — only AdresniMisto (MapServer/1)

    UI toggle uses 'parcel' / 'address' to disambiguate inputs that would
    otherwise match both (e.g. '350/2 Stříbrnice' is unambiguous as a
    parcel; without the toggle we'd also hit the address layer and waste
    one ČÚZK round-trip).

    Diacritic-insensitive for parcels (KÚ fold-cache) and addresses with
    any numeric token (server fetches broad, Python filters folded).
    Pure-text addresses fall back to literal LIKE — RUIAN's diacritic
    chars must match exactly. See _search_addresses for details."""
    if not q.strip():
        return []
    tokens = [t for t in re.split(r"[\s,]+", q.strip()) if t]
    if not tokens:
        return []
    parcels = _search_parcels(tokens) if kind in ("all", "parcel") else []
    addresses = _search_addresses(tokens) if kind in ("all", "address") else []
    # Parcels first (they're more specific) then addresses; result types
    # are distinct so no dedup needed.
    return parcels + addresses


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
        # Pre-warm envelope: cover the closeup ring (3× clamped inner_half for
        # selection-driven gen), floored at the legacy 2500 m so the default /
        # RÚIAN flow is unchanged. The heightfield subprocess still self-heals
        # any remaining gap via ensure_sm5_cached(fetch_missing=True).
        _ih = job.get("inner_half")
        sm5_half = max(2500.0, 3.0 * max(500.0, min(2000.0, _ih))) if _ih is not None else 2500
        log(f"Resolving sheets for envelope (cx={job['cx']}, cy={job['cy']}, half={sm5_half})…")
        codes = _resolve_sm5_codes(job["cx"], job["cy"], half=sm5_half)
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

    # Lazy import — keeps test environments without the compressor module
    # importable. We switched from Draco (custom DracoPy wrapper) to
    # meshopt (gltfpack subprocess) for 10× smaller wire bytes under brotli
    # AND faster decode. Module name kept stable so existing tests that
    # monkeypatch sys.modules["meshopt_compress_glb"] keep working.
    try:
        import meshopt_compress_glb
    except ImportError as e:
        log(f"FAIL: meshopt_compress_glb import failed: {e}")
        return False

    region_dir = Path(f"{TILES_DIR_PREFIX}{job['slug']}")
    details_dir = region_dir / "details"
    orig_dir = region_dir / "_orig_uncompressed"
    orig_dir.mkdir(parents=True, exist_ok=True)

    force = bool(job.get("force_recompress"))
    # Compress targets — panorama lives one level up (region_dir/panorama.glb),
    # the three detail rings live in details/. _orig_uncompressed/ keeps a
    # flat layout (all four side-by-side) regardless of where the deployed
    # copy sits.
    targets = [
        ("panorama", region_dir / "panorama.glb",   orig_dir / "panorama.glb"),
        ("outer",    details_dir / "outer.glb",     orig_dir / "outer.glb"),
        ("closeup",  details_dir / "closeup.glb",   orig_dir / "closeup.glb"),
        ("inner",    details_dir / "inner.glb",     orig_dir / "inner.glb"),
    ]
    for slug, compressed_path, orig_path in targets:
        # Skip targets whose source doesn't exist at all (e.g. panorama from
        # a half-finished pipeline run where gen_panorama didn't complete).
        if not compressed_path.exists() and not orig_path.exists():
            log(f"[{slug}] no source on disk, skip")
            continue

        if force:
            # Re-encode from _orig_uncompressed/<step>.glb (must exist; the
            # current deployed glb is already lossy-quantised so re-encoding
            # it can't improve quality). Soft-skip targets where the orig is
            # missing but a compressed copy exists — that means the file was
            # generated before per-LOD compress landed (e.g. legacy panorama
            # from manual Draco run).
            if not orig_path.exists():
                log(f"[{slug}] no _orig_uncompressed/{slug}.glb, skip "
                    f"(deployed glb stays as-is)")
                continue
        else:
            if orig_path.exists():
                log(f"[{slug}] already compressed (orig stored), skip")
                continue
            if not compressed_path.exists():
                log(f"FAIL: {slug} — no source .glb at {compressed_path}")
                return False

        size_before = compressed_path.stat().st_size if compressed_path.exists() else orig_path.stat().st_size
        # meshopt_compress_glb pre-snaps positions to the old Draco-compatible
        # 14-bit uniform-cube grid, then runs gltfpack with -noq so the viewer
        # gets Float32 attributes without KHR_mesh_quantization/EXPONENTIAL
        # filtering artefacts.
        mode = "re-compressing" if force else "compressing"
        log(f"[{slug}] {size_before // (1024*1024)} MB → {mode} (meshopt -cc -noq)…")

        # First-time compress: move source aside FIRST so we don't lose data
        # if meshopt crashes (recovery in `except`).
        # Force-recompress: orig already exists, just write a new dst on top.
        if not force:
            try:
                compressed_path.rename(orig_path)
            except OSError as e:
                log(f"FAIL: {slug} — rename source aside: {e}")
                return False

        try:
            meshopt_compress_glb.compress(str(orig_path), str(compressed_path))
            size_after = compressed_path.stat().st_size
            log(f"[{slug}] → {size_after // (1024*1024)} MB "
                f"({100 * size_after // size_before}% of original)")
        except Exception as e:
            log(f"FAIL: {slug} — meshopt compress: {e}")
            if force:
                # In force mode the orig file in _orig_uncompressed/ is the
                # ONLY lossless copy; the (partially-written, possibly
                # corrupt) compressed_path still sits in details/. Don't
                # rename — that would move the orig OUT of _orig_uncompressed/
                # and a future Recompress would hit "no orig" and fail
                # permanently. Leaving compressed_path as-is means the viewer
                # might load a corrupt glb until the next Recompress; that's
                # recoverable, data loss isn't.
                return False
            # Non-force: orig_path is the source we moved aside from
            # compressed_path. Rename it back so retry doesn't see a hole
            # where the .glb should be.
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


def _new_job(slug: str, label: str, cx: float, cy: float,
             force_recompress: bool = False) -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "slug": slug,
        "label": label,
        "cx": cx,
        "cy": cy,
        "cancelled": False,
        # When true, the compress step re-Dracos every detail from
        # _orig_uncompressed/<step>.glb even if the orig file already exists
        # (i.e. previously compressed). Used by the dashboard's "Recompress"
        # button to pick up a new draco_quant preset.
        "force_recompress": force_recompress,
        "created_at": time.time(),
        "steps": [
            {"name": name, "state": "pending", "error": None,
             "started_at": None, "finished_at": None}
            for name in STEP_NAMES
        ],
    }


def parse_job_extent(body: dict):
    """Extract optional (inner_half, parcel_ids) from a /api/jobs JSON body.
    Returns (inner_half|None, parcel_ids|None). Raises ValueError on bad types
    so the HTTP handler can map to 400."""
    inner_half = body.get("inner_half")
    if inner_half is not None:
        if (isinstance(inner_half, bool) or not isinstance(inner_half, (int, float))
                or not math.isfinite(inner_half) or inner_half <= 0):
            raise ValueError("inner_half must be a positive finite number")
        inner_half = float(inner_half)
    parcel_ids = body.get("parcel_ids")
    if parcel_ids is not None:
        if (not isinstance(parcel_ids, list) or len(parcel_ids) > 500
                or not all(isinstance(x, int) and not isinstance(x, bool)
                           for x in parcel_ids)):
            raise ValueError("parcel_ids must be a list of ≤500 ints")
    return inner_half, parcel_ids


def enqueue_job(slug: str, label: str, cx: float, cy: float,
                force_recompress: bool = False,
                inner_half: float | None = None,
                parcel_ids: list | None = None) -> str | None:
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
        job = _new_job(slug, label, cx, cy, force_recompress=force_recompress)
        if inner_half is not None:
            job["inner_half"] = inner_half
        JOBS[job["job_id"]] = job
        JOB_QUEUE.append(job["job_id"])
        JOB_CV.notify()   # vzbudí worker
    # Persist label outside the lock — pure filesystem write, no shared
    # state contention. Failure is non-fatal: dashboard would just show
    # the slug instead of the human-readable label until next gen.
    try:
        _persist_location_meta(slug, label, cx, cy, inner_half, parcel_ids)
    except OSError as e:
        print(f"[enqueue_job] persist meta failed for {slug!r}: {e}")
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
    "closeup": {"half": 1500, "step": "1.5", "fade":  50, "fade_to": "outer",    "zoom": 21, "size": 4096},
    "inner":   {"half":  500, "step": "0.5", "fade":  30, "fade_to": "closeup",  "zoom": 21, "size": 8192},
}


def cmd_for(step: str, slug: str, cx: float, cy: float,
            inner_half: float | None = None) -> list[str]:
    """Vyrobí subprocess command pro daný step. `--center-sjtsk=cx,cy`
    musí mít `=` syntax (argparse jinak parsuje negativní cx jako flag).
    Raises ValueError for 'sm5' and 'compress' — those are in-process,
    not subprocess steps."""
    if step in ("sm5", "compress"):
        raise ValueError(f"{step!r} is in-process, no subprocess command")
    # Defensive: callers (enqueue_job, HTTP handlers) already validate slugs,
    # but path traversal via `--slug ../whatever` would let a child read or
    # write `tiles_v2_../whatever/*`. Refuse anything that doesn't pass the
    # canonical slug regex so this remains true regardless of caller.
    if not is_valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    base = ["python3"]
    center = f"--center-sjtsk={cx},{cy}"
    if step == "panorama":
        return base + ["gen_panorama.py", "--region", slug, center]
    if step == "heightfield":
        # Pass centre explicitly: v2 panorama step (which used to write the
        # top-level manifest gen_heightfield read from) is retired. gen_
        # heightfield will fall back to disk manifests only for legacy lokace
        # that already have one — new ones rely on these CLI args.
        cmd = base + [
            "gen_heightfield.py", "--slug", slug,
            f"--cx={cx}", f"--cy={cy}",
        ]
        if inner_half is not None:
            cmd += ["--inner-half", str(inner_half)]
        return cmd
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
    cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"],
                  inner_half=job.get("inner_half"))

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
