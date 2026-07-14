"""HTTP server with WMS proxy for ortofoto."""
import http.server
import socket
import urllib.request
import urllib.parse
import os
import threading
import json
from pathlib import Path

# Force IPv4 for all stdlib urllib / http.client calls in this process.
# Our network's IPv6 path to ČÚZK (ags.cuzk.cz, openzu.cuzk.gov.cz,
# atom.cuzk.gov.cz) currently returns "Connection refused" while IPv4
# works fine. Python's macOS getaddrinfo otherwise prefers AF_INET6 and
# all urlopen calls die at the connect step. Filtering the resolver
# results is the smallest, least-surprising shim.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kw):
    results = _orig_getaddrinfo(host, *args, **kw)
    v4 = [r for r in results if r[0] == socket.AF_INET]
    return v4 if v4 else results   # fall back if host is v6-only
socket.getaddrinfo = _ipv4_only_getaddrinfo

import locations

# Cap concurrent outgoing requests to ČÚZK across all server threads —
# avoids Connection Refused when 9 HD tiles each spawn 6 workers (54 in flight).
_CUZK_SEM = threading.Semaphore(6)

# OSM tile.openstreetmap.org has a strict TOS — modest concurrency and bulk
# download discouraged. Global cap across server keeps us neighbourly even when
# multiple regions are fetched concurrently.
_OSM_SEM = threading.Semaphore(2)

# On-demand heightmap pyramid tiles (/cuzk-pyramid/dmpok/z/x/y.lerc). A missing
# tile at z>=14 is built from DMPOK on first request and written to disk, so
# subsequent hits are plain static serves. Cap concurrent builds (CPU+USB-disk
# bound) and dedupe identical concurrent requests with a per-tile lock.
_PYRAMID_BUILD_SEM = threading.Semaphore(3)
_PYRAMID_TILE_LOCKS: dict = {}
_PYRAMID_TILE_LOCKS_GUARD = threading.Lock()
_PYRAMID_MOD = None
_PYRAMID_MOD_GUARD = threading.Lock()
# z<14 tiles can't be built live (a low-zoom tile would merge a multi-GB DMPOK
# mosaic); they must be pre-baked. z>=14 builds a small mosaic (~100 MB at z14,
# tiny above). Cap the top to keep oversampled junk out.
_PYRAMID_ONDEMAND_ZMIN = 14
_PYRAMID_ONDEMAND_ZMAX = 20


def _pyramid_module():
    """Lazy singleton: import build_pyramid_tile, load the DMPOK inventory once,
    and silence its per-tile prints. Returns the module."""
    global _PYRAMID_MOD
    if _PYRAMID_MOD is None:
        with _PYRAMID_MOD_GUARD:
            if _PYRAMID_MOD is None:
                import build_pyramid_tile as bpt
                inv = bpt.load_or_build_inventory(bpt.BULK_OUT_DIR)
                bpt.load_or_build_inventory = lambda _b: inv
                bpt.print = lambda *a, **k: None
                _PYRAMID_MOD = bpt
    return _PYRAMID_MOD


def _pyramid_tile_lock(key):
    with _PYRAMID_TILE_LOCKS_GUARD:
        lk = _PYRAMID_TILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PYRAMID_TILE_LOCKS[key] = lk
        return lk


# Whole-ČR raw ortofoto bulk (the bulk_ortofoto pull). The raw-crop path prefers
# these local 12.5 cm sheets over the WMS. Override root with INZERATOR_ORTHO_BULK.
ORTHO_BULK_DIR = Path(os.environ.get("INZERATOR_ORTHO_BULK",
                                     "/Volumes/Elements/cuzk-bulk"))


def _dmpok_inventory():
    """MAPNOM → {path,left,bottom,right,top} for every DMPOK sheet, memoized via
    the pyramid module. DMPOK + ortho share the SM5 grid, so these sheet bboxes
    double as the ortho coverage index — no separate 16k-file scan needed."""
    bpt = _pyramid_module()
    return bpt.load_or_build_inventory(bpt.BULK_OUT_DIR)

# Czech labels for unnamed POIs — Overpass returns lots of un-named amenity
# nodes (especially leisure tags like swimming_pool / playground). For
# real-estate context "Hřiště" is more useful than "(bez názvu)".
_POI_FALLBACK_LABELS = {
    "school": "Škola", "kindergarten": "Školka", "pharmacy": "Lékárna",
    "doctors": "Lékař", "post_office": "Pošta", "place_of_worship": "Kostel",
    "restaurant": "Restaurace", "cafe": "Kavárna", "pub": "Hospoda",
    "bar": "Bar", "fast_food": "Občerstvení", "bank": "Banka",
    "library": "Knihovna", "community_centre": "KD", "townhall": "Úřad",
    "police": "Policie", "fire_station": "Hasiči", "fuel": "Čerpací st.",
    "hospital": "Nemocnice",
    "swimming_pool": "Bazén", "playground": "Hřiště", "park": "Park",
    "sports_centre": "Sportoviště", "pitch": "Hřiště", "fitness_centre": "Fitness",
    "garden": "Zahrada", "nature_reserve": "Přírodní rezervace",
    "supermarket": "Supermarket", "bakery": "Pekárna", "butcher": "Řezník",
    "convenience": "Smíšenka", "greengrocer": "Zelenina", "hairdresser": "Kadeřník",
    "chemist": "Drogerie", "department_store": "Obchodní dům", "kiosk": "Stánek",
    "station": "Nádraží", "halt": "Zastávka", "tram_stop": "Tramvaj",
    "bus_stop": "Bus",
    "peak": "Vrchol",
}

# Prompt for /api/image-edit. Kept server-side so clients can't override the
# system instruction (which is tuned for orthophoto-derived 3D mesh repair).
# The full text lives in image_edit_prompt.txt — single source of truth that
# both the server reads and the UI can preview read-only.
_IMAGE_EDIT_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "image_edit_prompt.txt")
try:
    with open(_IMAGE_EDIT_PROMPT_PATH, "r", encoding="utf-8") as _f:
        _IMAGE_EDIT_PROMPT = _f.read().strip()
except OSError:
    _IMAGE_EDIT_PROMPT = ""

# In-memory LRU cache for fetched ČÚZK subtiles — once fetched, never refetch.
# Bounded to ~2000 entries (≈500 MB if every tile full PNG; real avg much less).
_CUZK_CACHE = {}
_CUZK_CACHE_ORDER = []
_CUZK_CACHE_LOCK = threading.Lock()
_CUZK_CACHE_MAX = 2000

def _cache_get(key):
    with _CUZK_CACHE_LOCK:
        if key in _CUZK_CACHE:
            # Move to end (recent)
            try:
                _CUZK_CACHE_ORDER.remove(key)
            except ValueError:
                pass
            _CUZK_CACHE_ORDER.append(key)
            return _CUZK_CACHE[key]
    return None

def _cache_put(key, img):
    with _CUZK_CACHE_LOCK:
        _CUZK_CACHE[key] = img
        _CUZK_CACHE_ORDER.append(key)
        while len(_CUZK_CACHE_ORDER) > _CUZK_CACHE_MAX:
            old = _CUZK_CACHE_ORDER.pop(0)
            _CUZK_CACHE.pop(old, None)


class _BoundedCache:
    """Thread-safe LRU cache for response bytes (buildings/roads endpoints)."""
    def __init__(self, max_entries=200):
        self.max = max_entries
        self.data = {}
        self.order = []
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                try: self.order.remove(key)
                except ValueError: pass
                self.order.append(key)
                return self.data[key]
        return None

    def put(self, key, val):
        with self.lock:
            self.data[key] = val
            self.order.append(key)
            while len(self.order) > self.max:
                old = self.order.pop(0)
                self.data.pop(old, None)

_BUILDINGS_CACHE = _BoundedCache(200)

# RÚIAN způsob využití kódy (CC-CZ classification, https://nahlizenidokn.cuzk.cz)
_ZPUSOB_VYUZITI = {
    1: "rodinný dům",
    2: "bytový dům",
    3: "stavba pro rodinnou rekreaci",
    4: "stavba pro shromažďování většího počtu osob",
    5: "stavba pro obchod",
    6: "stavba ubytovacího zařízení",
    7: "stavba pro výrobu a skladování",
    8: "zemědělská stavba",
    9: "stavba pro administrativu",
    10: "stavba občanského vybavení",
    11: "stavba technického vybavení",
    12: "stavba pro dopravu",
    13: "víceúčelová stavba",
    14: "garáž",
    16: "rozestavěná",
    99: "jiná stavba",
}
_ROADS_CACHE = _BoundedCache(200)
_POI_CACHE = _BoundedCache(200)
_WIKI_CACHE = _BoundedCache(500)

def _clamp_radius(r):
    """Clamp user-supplied radius so malicious/wrong values can't DoS backend."""
    return max(50.0, min(r, 2000.0))

# Overpass mirrors — try in order, fall through on 4xx/5xx
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

def _ruian_get(url, timeout=30, retries=5, backoff=0.5):
    """GET against ČÚZK ArcGIS / RÚIAN with exponential backoff. The service
    flaps under load — measured ~40% direct-hit rate during outages. With 5
    retries (0.5+1+2+4+8 = 15.5 s total wait) the per-call failure rate drops
    from ~21% (3 retries) to ~8%, and most outages clear inside that window."""
    import time as _time
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, ConnectionError,
                TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                _time.sleep(backoff * (2 ** attempt))
    raise last_err if last_err else RuntimeError("RÚIAN unreachable")


def _query_overpass(query_text, timeout=30):
    """POST Overpass query to mirrors with fallback. Returns parsed JSON or raises."""
    post_data = urllib.parse.urlencode({"data": query_text}).encode("utf-8")
    last_err = None
    for url in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(
                url, data=post_data,
                headers={"User-Agent": "gtaol/1.0",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Overpass mirrors failed")


# Default model for POI curation. gpt-5.4-mini (released 2026-03-17) is
# our cheap-tier sweet spot: structured JSON output, fast, ~$0.0005 per
# POI batch. Override via env if a newer mini lands or budget changes.
_POI_CURATOR_MODEL = os.environ.get("POI_CURATOR_MODEL", "gpt-5.4-mini")

def _curate_pois_with_ai(pois, max_items=15):
    """Send raw OSM POI list to OpenAI, get a curated TOP-N back with a
    Czech 'why' caption per item. Drops duplicates (no five bus stops),
    prioritises real-estate-relevant categories.

    Input:  list of dicts {id, name, category, type, coords, wikipedia,
                           wikidata}
    Output: filtered list, each item enriched with 'why' (short Czech
            sentence). On any failure the caller falls back to raw.
    """
    api_key = os.environ["OPENAI_API_KEY"]   # caller checked
    # Send only the fields the model needs to decide; keep id as the
    # join key for filtering the original list.
    digest = [
        {"id": p["id"], "name": p["name"], "category": p["category"],
         "type": p["type"], "coords": p["coords"]}
        for p in pois
    ]
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["integer", "string"]},
                        "why": {"type": "string"},
                    },
                    "required": ["id", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    sys_prompt = (
        "Jsi asistent kurátor POI pro realitní portál. "
        f"Dostaneš seznam OSM POI v okolí nemovitosti v Česku. "
        f"Vyber nejvýše {max_items} opravdu užitečných a unikátních pro budoucího kupce. "
        "Pravidla výběru:\n"
        "1) Priorita: školy, školky, lékař/lékárna/nemocnice, supermarket, "
        "vlak/tram, památky, voda/park/příroda, kostel.\n"
        "2) Nikdy nevracej více než JEDEN supermarket / školku / školu / "
        "kostel / hřiště pokud nejsou výrazně daleko od sebe (>500 m) "
        "a obě mají jmené; preferuj pojmenovaná místa, jen pokud nejsou pojmenovaná, vrať jedno nejbližší.\n"
        "3) Nikdy nevracej více než 2 hospody/restaurace/kavárny dohromady.\n"
        "4) Vynechej úplně: police, fire_station, fuel, bank, community_centre, "
        "library — pokud nejsou jediným zajímavým bodem v okolí.\n"
        "5) Vynechej nepojmenované POI pokud je pojmenovaný stejného typu poblíž.\n"
        "6) Pro každý vybraný POI vrať 'id' (přesně jak přišel) a 'why' "
        "= krátká česká věta (max 8 slov) proč je relevantní pro kupce. "
        "Příklady why: 'Základní škola v dosahu', 'Místní bazén', "
        "'Kostel — orientační bod', 'Vlaková zastávka — denní spojení'."
    )
    user_prompt = "POI seznam (JSON):\n" + json.dumps(digest, ensure_ascii=False)

    body = json.dumps({
        "model": _POI_CURATOR_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "poi_curation", "schema": schema, "strict": True},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp_data = json.loads(resp.read())
    content = resp_data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    items = parsed.get("items", [])

    # Build id → why lookup. Coerce both sides to string in case the
    # model echoed ints as strings.
    why_by_id = {str(it["id"]): it["why"] for it in items}
    out = []
    for p in pois:
        why = why_by_id.get(str(p["id"]))
        if why is None:
            continue
        q = dict(p)
        q["why"] = why
        out.append(q)
    # Preserve OpenAI's chosen ordering (top-priority first) when possible.
    order = {str(it["id"]): i for i, it in enumerate(items)}
    out.sort(key=lambda p: order.get(str(p["id"]), 999))
    return out


def _ransac_plane(pts, n_iter=200, eps=0.30, seed=42):
    """Fit a single plane z = ax + by + c to (N,3) points via RANSAC.
    Returns (a, b, c, inlier_mask) for the largest consensus, or None."""
    import numpy as np
    n = len(pts)
    if n < 10:
        return None
    rng = np.random.default_rng(seed)
    best = None
    best_count = 0
    for _ in range(n_iter):
        idx = rng.choice(n, 3, replace=False)
        p3 = pts[idx]
        try:
            A = np.column_stack([p3[:, 0], p3[:, 1], np.ones(3)])
            a, b, c = np.linalg.solve(A, p3[:, 2])
        except np.linalg.LinAlgError:
            continue
        resid = np.abs(pts[:, 2] - (a * pts[:, 0] + b * pts[:, 1] + c))
        inliers = resid < eps
        cnt = int(inliers.sum())
        if cnt > best_count:
            best_count = cnt
            best = (a, b, c, inliers)
    if best is None or best_count < 10:
        return None
    # Refine on inliers via least squares.
    _, _, _, mask = best
    pin = pts[mask]
    A = np.column_stack([pin[:, 0], pin[:, 1], np.ones(len(pin))])
    coef, *_ = np.linalg.lstsq(A, pin[:, 2], rcond=None)
    return float(coef[0]), float(coef[1]), float(coef[2]), mask


def _detect_roof_type(in_pts, max_iter=6, eps=0.30):
    """Iteratively peel off RANSAC planes, merge near-duplicates, classify."""
    import math, numpy as np
    if len(in_pts) < 30:
        return None
    n_total = len(in_pts)
    remaining = in_pts.copy()
    raw = []
    while len(remaining) >= 30 and len(raw) < max_iter:
        fit = _ransac_plane(remaining, eps=eps)
        if fit is None:
            break
        a, b, c, mask = fit
        if mask.sum() < max(15, 0.05 * n_total):
            break
        slope = math.degrees(math.atan(math.hypot(a, b)))
        azimuth = (math.degrees(math.atan2(-a, -b)) + 360) % 360
        raw.append({"slope_deg": slope, "azimuth_deg": azimuth,
                    "n_pts": int(mask.sum())})
        remaining = remaining[~mask]

    if not raw:
        return None

    # Merge near-duplicates (RANSAC re-discovers the same plane in fragments).
    planes = []
    for p in raw:
        for m in planes:
            ds = abs(p["slope_deg"] - m["slope_deg"])
            da = abs(p["azimuth_deg"] - m["azimuth_deg"])
            da = min(da, 360 - da)
            if ds <= 8 and da <= 25:
                w = m["n_pts"] + p["n_pts"]
                # Circular mean for azimuth, weighted by point count.
                cos_sum = (math.cos(math.radians(m["azimuth_deg"])) * m["n_pts"]
                           + math.cos(math.radians(p["azimuth_deg"])) * p["n_pts"])
                sin_sum = (math.sin(math.radians(m["azimuth_deg"])) * m["n_pts"]
                           + math.sin(math.radians(p["azimuth_deg"])) * p["n_pts"])
                m["azimuth_deg"] = (math.degrees(math.atan2(sin_sum, cos_sum)) + 360) % 360
                m["slope_deg"] = ((m["slope_deg"] * m["n_pts"]
                                   + p["slope_deg"] * p["n_pts"]) / w)
                m["n_pts"] = w
                break
        else:
            planes.append(dict(p))

    planes.sort(key=lambda x: -x["n_pts"])
    for p in planes:
        p["slope_deg"] = round(p["slope_deg"], 1)
        p["azimuth_deg"] = round(p["azimuth_deg"], 0)
        p["n_pts"] = int(p["n_pts"])

    n = len(planes)
    n_used = sum(p["n_pts"] for p in planes)
    avg_slope = sum(p["slope_deg"] * p["n_pts"] for p in planes) / max(n_used, 1)
    top2_pts = sum(p["n_pts"] for p in planes[:2])

    def _gable_pair(p0, p1):
        d_az = abs(p0["azimuth_deg"] - p1["azimuth_deg"])
        d_az = min(d_az, 360 - d_az)
        d_slope = abs(p0["slope_deg"] - p1["slope_deg"])
        return 150 <= d_az <= 210 and d_slope < 15

    if n == 1:
        roof_type = "plochá" if planes[0]["slope_deg"] < 8 else "pultová"
    elif n == 2:
        roof_type = "sedlová" if _gable_pair(*planes[:2]) else "složitá"
    elif n >= 4:
        azs = sorted(p["azimuth_deg"] for p in planes[:4])
        gaps = [((azs[(i + 1) % 4] - azs[i]) + 360) % 360 for i in range(4)]
        if max(gaps) - min(gaps) < 60:
            roof_type = "valbová"
        elif _gable_pair(*planes[:2]) and top2_pts >= 0.6 * n_used:
            roof_type = "sedlová"
        else:
            roof_type = "složitá"
    else:  # n == 3
        if _gable_pair(*planes[:2]) and top2_pts >= 0.6 * n_used:
            roof_type = "sedlová"
        else:
            roof_type = "složitá"

    ridge_az = None
    if roof_type == "sedlová" and n >= 2:
        ridge_az = round((planes[0]["azimuth_deg"] + 90) % 180, 0)

    return {
        "type": roof_type,
        "n_planes": n,
        "mean_slope_deg": round(avg_slope, 1),
        "ridge_azimuth_deg": ridge_az,
        "planes": planes,
    }


def _classify_vegetation(band, transform, poly, ground_z,
                         n_passes=2, low_margin=0.5, high_margin=1.0):
    """Return a boolean mask same shape as band: True = suspect vegetation.

    Two-signal heuristic from user observation: tree pixels are (a) outside
    the cadastre polygon AND (b) at heights significantly different from the
    building's roof percentile range. Two passes — second pass recomputes
    the roof range from already-filtered inside pixels.
    """
    import numpy as np
    from rasterio.features import rasterize as _rasterize
    h, w = band.shape
    poly_mask = _rasterize(
        [(poly, 1)], out_shape=(h, w), transform=transform,
        fill=0, dtype=np.uint8,
    ).astype(bool)
    tall = band > ground_z + 2.0
    if not tall.any():
        return np.zeros_like(band, dtype=bool)
    veg = np.zeros_like(band, dtype=bool)
    for _ in range(n_passes):
        in_tall = tall & poly_mask & ~veg
        if int(in_tall.sum()) < 10:
            break
        v_above = band[in_tall]
        v_above = v_above[v_above > ground_z + 1.5]
        if len(v_above) < 10:
            break
        roof_lo = float(np.percentile(v_above, 10)) - low_margin
        roof_hi = float(np.percentile(v_above, 90)) + high_margin
        # KEEP if inside polygon OR within roof percentile range.
        keep = poly_mask | ((band >= roof_lo) & (band <= roof_hi))
        veg = tall & ~keep
    return veg


def _detect_dsm_footprint(poly, src, neighbours, search_buffer=8.0,
                          cluster_bound=5.0,
                          min_overlap=10, area_min_ratio=0.8, area_max_ratio=3.0):
    """Detect the actual building outline from DSM connected components.

    Unlike a plain translation, this catches RÚIAN polygons that are too
    SMALL for the real building (footprint missing eaves / additions), not
    just shifted. Algorithm:

      1. Sample DSM in `poly.buffer(search_buffer)` minus neighbour polygons.
      2. Filter vegetation via `_classify_vegetation`.
      3. Connected components on the (tall AND not-veg) mask.
      4. Pick the cluster with the most pixels overlapping the cadastre poly.
      5. Polygonize that cluster, simplify to remove pixel jitter.
      6. Sanity-check the resulting area against the cadastre area.

    Returns (dsm_poly, (dx, dy)) where dx/dy is the centroid offset
    versus the cadastre polygon. Falls back to (poly, (0, 0)) if any step
    fails or the result is implausible.
    """
    import numpy as np
    import rasterio.mask
    import rasterio.features
    from scipy.ndimage import label as nd_label
    from shapely.geometry import mapping, shape as shapely_shape
    from shapely.ops import unary_union

    search = poly.buffer(search_buffer)
    if neighbours:
        search = search.difference(unary_union(neighbours))
    if search.is_empty:
        return poly, (0.0, 0.0)
    try:
        arr, t = rasterio.mask.mask(src, [mapping(search)], crop=True, nodata=0)
    except Exception:
        return poly, (0.0, 0.0)
    band = arr[0]
    valid = band[band > 0]
    if len(valid) < 50:
        return poly, (0.0, 0.0)
    terrain_z = float(np.percentile(valid, 10))

    veg = _classify_vegetation(band, t, poly, terrain_z)
    tall = (band > terrain_z + 2.0) & ~veg

    # Limit clusters to (cadastre + cluster_bound). Prevents cluster from
    # merging with a neighbouring building via courtyard / connecting roofs
    # — important for large yard polygons (statek) and dense villages.
    h_, w_ = band.shape
    extent_mask = rasterio.features.rasterize(
        [(poly.buffer(cluster_bound), 1)], out_shape=(h_, w_), transform=t,
        fill=0, dtype=np.uint8,
    ).astype(bool)
    tall = tall & extent_mask
    if int(tall.sum()) < 30:
        return poly, (0.0, 0.0)

    # 8-connectivity so diagonally adjacent roof cells stay one cluster.
    labels_arr, n_clusters = nd_label(tall, structure=np.ones((3, 3)))
    if n_clusters == 0:
        return poly, (0.0, 0.0)

    poly_mask = rasterio.features.rasterize(
        [(poly, 1)], out_shape=(h_, w_), transform=t,
        fill=0, dtype=np.uint8,
    ).astype(bool)

    # Cluster with max overlap with the cadastre polygon = the building.
    best_cluster_id = 0
    best_overlap = 0
    for cid in range(1, n_clusters + 1):
        ov = int(((labels_arr == cid) & poly_mask).sum())
        if ov > best_overlap:
            best_overlap = ov
            best_cluster_id = cid
    if best_cluster_id == 0 or best_overlap < min_overlap:
        return poly, (0.0, 0.0)

    cluster_mask = (labels_arr == best_cluster_id)
    cluster_area_px = int(cluster_mask.sum())
    cadastre_area_px = int(poly_mask.sum())
    overlap_px = int((cluster_mask & poly_mask).sum())
    containment = overlap_px / max(cadastre_area_px, 1)  # cadastre coverage
    expansion = cluster_area_px / max(cadastre_area_px, 1)  # cluster vs cadastre size

    shapes_iter = rasterio.features.shapes(
        cluster_mask.astype(np.uint8),
        mask=cluster_mask.astype(bool), transform=t)
    polys = [shapely_shape(g) for g, v in shapes_iter if v == 1]
    if not polys:
        return poly, (0.0, 0.0)
    cluster_poly = max(polys, key=lambda p: p.area)
    cluster_poly = cluster_poly.simplify(0.4, preserve_topology=True)
    if not cluster_poly.is_valid or cluster_poly.is_empty:
        return poly, (0.0, 0.0)
    if cluster_poly.geom_type == "MultiPolygon":
        cluster_poly = max(cluster_poly.geoms, key=lambda p: p.area)

    pc = poly.centroid
    cpc = cluster_poly.centroid
    dx, dy = round(cpc.x - pc.x, 2), round(cpc.y - pc.y, 2)

    # Decide whether the cluster is a better representation of the building
    # than the cadastre polygon. Two-tier thresholds split houses from yards:
    #
    #   Small polygons (< 300 m², single-building cadastre): be permissive.
    #     Even small expansion or shift signals real misalignment (Hnojice 226
    #     and 151 cases).
    #
    #   Large polygons (≥ 300 m², yard / statek): be strict. Centroid offsets
    #     here are typically uneven distribution of buildings inside the yard,
    #     not misalignment. Only intervene when the cluster overwhelmingly
    #     covers the cadastre AND grows clearly (rare).
    shift_mag = (dx * dx + dy * dy) ** 0.5
    is_yard = poly.area >= 300.0
    if is_yard:
        needed_containment, needed_expansion, needed_shift = 0.92, 1.4, 6.0
    else:
        needed_containment, needed_expansion, needed_shift = 0.65, 1.15, 2.0
    if (containment >= needed_containment
            and (expansion >= needed_expansion or shift_mag >= needed_shift)
            and expansion <= area_max_ratio):
        return cluster_poly, (dx, dy)
    return poly, (0.0, 0.0)


# Back-compat name; older callers still imported `_align_polygon_to_dsm`.
_align_polygon_to_dsm = _detect_dsm_footprint


def _fetch_parcels_local(building_poly, cx, cy, buffer_m=8):
    """Query RÚIAN ArcGIS layer 5 (Parcela) for polygons intersecting the
    building's neighbourhood; convert each ring to local mesh frame
    (centroid origin, X=east, Z=south = -world_y). Returns [] on any failure."""
    out = []
    try:
        bx_min, by_min, bx_max, by_max = building_poly.buffer(buffer_m).bounds
        url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/5/query"
        params = {
            "geometry": json.dumps({
                "xmin": bx_min, "ymin": by_min, "xmax": bx_max, "ymax": by_max,
                "spatialReference": {"wkid": 5514},
            }),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "kmenovecislo,poddelenicisla",
            "outSR": "5514", "f": "json", "returnGeometry": "true",
            "resultRecordCount": "100",
        }
        raw = json.loads(_ruian_get(
            f"{url}?" + urllib.parse.urlencode(params), timeout=15))
        for f in raw.get("features", []):
            for ring in f["geometry"].get("rings", []):
                local = [
                    [round(sx - cx, 2), round(-(sy - cy), 2)] for sx, sy in ring
                ]
                if len(local) >= 3:
                    out.append(local)
    except Exception as e:
        print(f"[building-detail] parcels fetch failed: {e}")
    return out


DRUH_POZEMKU_LABELS = {
    2:  "orná půda",
    3:  "chmelnice",
    4:  "vinice",
    5:  "zahrada",
    6:  "ovocný sad",
    7:  "trvalý travní porost",
    10: "lesní pozemek",
    11: "vodní plocha",
    13: "zastavěná plocha a nádvoří",
    14: "ostatní plocha",
}

def _fetch_parcels_area(gcx, gcy, radius):
    """Query RÚIAN layer 5 for parcels in a square around (gcx, gcy);
    return rings in the local frame with per-vertex DSM-sampled Y.

    DSM Y is absolute m n.m. (matches GLB tile Y datum used by area
    viewers). Vertex outside any cached DSM TIFF → terrain_y = 0.
    """
    import rasterio
    out = []
    bx_min, by_min = gcx - radius, gcy - radius
    bx_max, by_max = gcx + radius, gcy + radius
    url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/5/query"

    # Page through results. Layer 5 reports maxRecordCount=1_000_000 (Sep
    # 2026), but the actual per-response soft limit varies (~1000-2000); we
    # ask for 5000 to fit most villages in a single round-trip and paginate
    # with orderByFields=objectid so consecutive pages are deterministic.
    # Without orderBy ArcGIS may dedup/skip across pages — earlier code
    # without it terminated at 1999 features for opatovice (true count 5045)
    # because the 2nd page returned 999 unique features and the < page_size
    # break fired, silently dropping ~60% of the cadastre.
    page_size = 5000
    offset = 0
    raw_features = []
    while True:
        params = {
            "geometry": json.dumps({
                "xmin": bx_min, "ymin": by_min, "xmax": bx_max, "ymax": by_max,
                "spatialReference": {"wkid": 5514},
            }),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "id,kmenovecislo,poddelenicisla,druhpozemkukod,vymeraparcely",
            "outSR": "5514", "f": "json", "returnGeometry": "true",
            "orderByFields": "objectid",
            "resultOffset": str(offset),
            "resultRecordCount": str(page_size),
        }
        raw = json.loads(_ruian_get(f"{url}?" + urllib.parse.urlencode(params), timeout=60))
        feats = raw.get("features", [])
        # Empty page = we can't advance the offset; treat as terminal even
        # when the server claims exceededTransferLimit (rare ČÚZK quirk —
        # without this guard offset += 0 → infinite identical query).
        if not feats:
            break
        raw_features.extend(feats)
        # exceededTransferLimit signals "there are more pages"; absence means
        # we got the tail. Some ArcGIS servers omit this field on the final
        # page, others return False — handle both.
        more = raw.get("exceededTransferLimit") or len(feats) >= page_size
        if not more:
            break
        offset += len(feats)
        # Hard safety cap: even densely zoned villages stay under ~10k
        # parcels for our largest closeup ring; 50k means the loop has lost
        # the plot. Better to truncate than spin.
        if offset > 50000:
            break

    # Open every cached DSM TIFF once; per-vertex sample picks the right one.
    tifs = []
    for d in sorted(Path("cache").glob("dmpok_tiff_*")):
        for tif in d.glob("*.tif"):
            try:
                tifs.append(rasterio.open(tif))
            except Exception:
                pass

    def sample_y(sx, sy):
        for src in tifs:
            b = src.bounds
            if b.left <= sx <= b.right and b.bottom <= sy <= b.top:
                try:
                    val = next(src.sample([(sx, sy)]))[0]
                    if val > 0:
                        return round(float(val), 2)
                except Exception:
                    continue
        return 0.0

    try:
        for f in raw_features:
            attrs = f.get("attributes", {})
            rings = f["geometry"].get("rings", []) if f.get("geometry") else []
            if not rings:
                continue
            outer = rings[0]   # outer ring only in v1
            ring_local = []
            for sx, sy in outer:
                ty = sample_y(sx, sy)
                ring_local.append([
                    round(sx - gcx, 2),
                    round(-(sy - gcy), 2),
                    ty,
                ])
            # Fill DSM-miss vertices (sample_y returned 0) with the mean of
            # the valid ring vertices. CZ absolute elevation is always ≥ 115
            # m, so ty=0 is a coverage hole, not real ground. Without this
            # fix the client subtracts groundZ (~250 m) and the outline
            # tunnels deep underground at the village edge.
            valid_ys = [v[2] for v in ring_local if v[2] > 0]
            if valid_ys and len(valid_ys) < len(ring_local):
                fill = round(sum(valid_ys) / len(valid_ys), 2)
                for v in ring_local:
                    if v[2] == 0:
                        v[2] = fill
            kmen = attrs.get("kmenovecislo")
            podd = attrs.get("poddelenicisla")
            label = f"{kmen}/{podd}" if podd else (str(kmen) if kmen else "—")
            use_code = attrs.get("druhpozemkukod")
            out.append({
                "id": str(attrs.get("id", "")),
                "label": label,
                "use_code": use_code,
                "use_label": DRUH_POZEMKU_LABELS.get(use_code, "—"),
                "area_m2": int(float(attrs.get("vymeraparcely") or 0)),
                "ring_local": ring_local,
            })
    finally:
        for src in tifs:
            try: src.close()
            except Exception: pass
    return out


PORT = int(os.environ.get("PORT", 8080))
DIRECTORY = os.path.dirname(os.path.abspath(__file__))


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1 keep-alive: the 3D viewer streams thousands of small tiles
    # and paid a TCP handshake per tile under the HTTP/1.0 default
    # (audited 2026-07-07: every send_response block sets Content-Length,
    # which keep-alive requires). timeout reaps parked idle connections.
    protocol_version = "HTTP/1.1"
    timeout = 75

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def send_response(self, code, message=None):
        self._last_status = code
        super().send_response(code, message)

    def end_headers(self):
        # Central cache policy for the tile pyramid + viewer assets.
        # Tiles are immutable-ish (rebuilds are rare) → a day of caching;
        # viewer HTML/JSON must revalidate on every load (no-cache means
        # "revalidate", not "don't store") so code changes arrive without
        # hard reloads. Never attach caching to non-200s (a cached 404
        # would outlive the on-demand build that later fills the tile).
        if getattr(self, "_last_status", None) == 200:
            p = (self.path or "").split("?", 1)[0]
            if "/cuzk-pyramid/" in p:
                # Tiles are immutable per URL: in-place rebuilds bump the
                # viewer's ?v= version, so a month-long cache is safe and
                # saves re-fetches across phone sessions.
                self.send_header("Cache-Control",
                                 "public, max-age=2592000, immutable")
            elif p.endswith((".html", ".json")) or p.endswith("/"):
                self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _proxy_ortofoto_vhr(self, query):
        """Fetch ČÚZK ortofoto at 0.125 m/px native via the public WMS at
        `ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/MapServer/WMSServer`.

        Native pixel resolution: 12.5 cm/px (vs 25 cm in our cached SM5
        ATOM source). For a 1×1 km inner detail that's a 8000×8000 ideal
        sample — the WMS caps WIDTH/HEIGHT at 4000 per request, so we
        split into an N×N sub-tile grid (~4 requests for inner), stitch
        in PIL, then route through the same resize + KTX2 encode pipeline
        as the raw branch.

        Cache key includes 'hires' so VHR + non-VHR cache entries coexist.
        """
        from PIL import Image
        from io import BytesIO
        import hashlib
        import urllib.request as _urlreq

        bbox_str = query.get("BBOX", [""])[0]
        if not bbox_str:
            # Derive the S-JTSK fetch bbox from WBBOX (WGS84) when no explicit
            # S-JTSK BBOX is given. Lets clients that only know a WGS84 / XYZ
            # tile extent (e.g. the pyramid viewer) hit this endpoint with just
            # WBBOX; the WGS reproject branch below then aligns output to it.
            wbbox_q = query.get("WBBOX", [""])[0]
            if not wbbox_q:
                self.send_error(400, "Missing BBOX (or WBBOX)")
                return
            try:
                w, s, e, n = (float(x) for x in wbbox_q.split(","))
            except ValueError:
                self.send_error(400, "WBBOX must be west,south,east,north")
                return
            from pyproj import Transformer
            to_sjtsk = Transformer.from_crs(
                "EPSG:4326", "EPSG:5514", always_xy=True)
            corners = [to_sjtsk.transform(lon, lat)
                       for lon in (w, e) for lat in (s, n)]
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            bbox_str = f"{min(xs)},{min(ys)},{max(xs)},{max(ys)}"
        try:
            sjtsk_xmin, sjtsk_ymin, sjtsk_xmax, sjtsk_ymax = (
                float(x) for x in bbox_str.split(","))
        except ValueError:
            self.send_error(400, "BBOX must be xmin,ymin,xmax,ymax in S-JTSK")
            return

        out_format = query.get("format", ["jpeg"])[0].lower()
        if out_format not in ("jpeg", "jpg", "png", "ktx2"):
            out_format = "jpeg"

        cache_key_src = "hires|" + "|".join(
            f"{k}={query.get(k, [''])[0]}"
            for k in ("BBOX", "WBBOX", "size", "format", "ktx2", "quality")
        )
        cache_key = hashlib.sha256(cache_key_src.encode()).hexdigest()[:16]
        cache_dir = Path("cache/orto_render")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"vhr_{cache_key}.{out_format}"
        _mime_for = {"png": "image/png", "ktx2": "image/ktx2"}
        if cache_path.exists():
            data = cache_path.read_bytes()
            mime = _mime_for.get(out_format, "image/jpeg")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Ortofoto-Cache", "hit-vhr")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        # Target native resolution at 0.125 m/px.
        bbox_w = sjtsk_xmax - sjtsk_xmin
        bbox_h = sjtsk_ymax - sjtsk_ymin
        native_w = int(round(bbox_w / 0.125))
        native_h = int(round(bbox_h / 0.125))

        # XYZ-tile fallback clamp: a ≤1024px output (map3d z>=14 tiles ask
        # for 512) must not composite at native 0.125 m/px — a z=14 tile is
        # ~12.5k px native → 16 WMS GetMaps + a ~475 MB PIL image for a 512²
        # JPEG whose useful resolution is 3 m/px. 2× target keeps Lanczos
        # supersampling quality; big renders (refresh_ortho size=4096/8192)
        # are above the guard and byte-identical.
        try:
            _tgt = int(query.get("size", ["0"])[0] or 0)
        except ValueError:
            _tgt = 0
        if 0 < _tgt <= 1024:
            _k = min(1.0, 2 * _tgt / max(native_w, native_h, 1))
            native_w = max(64, int(round(native_w * _k)))
            native_h = max(64, int(round(native_h * _k)))

        # Cap pixels we ever ask the WMS for — bbox > 5×5 km is silly here.
        MAX_TILE = 4000
        n_x = (native_w + MAX_TILE - 1) // MAX_TILE
        n_y = (native_h + MAX_TILE - 1) // MAX_TILE
        sub_w = native_w // n_x
        sub_h = native_h // n_y
        # adjust last column/row to absorb integer-division remainder
        last_sub_w = native_w - sub_w * (n_x - 1)
        last_sub_h = native_h - sub_h * (n_y - 1)

        Image.MAX_IMAGE_PIXELS = None
        composite = Image.new("RGB", (native_w, native_h), (0, 0, 0))

        WMS = ("https://ags.cuzk.gov.cz/arcgis1/services/ORTOFOTO/"
               "MapServer/WMSServer")
        fetched_count = 0
        for ix in range(n_x):
            for iy in range(n_y):
                # WMS Y axis goes south-up, image rows go north-down. We
                # iterate iy bottom-up so paste row 0 = top (north) of image.
                sxmin = sjtsk_xmin + bbox_w * ix / n_x
                sxmax = sjtsk_xmin + bbox_w * (ix + 1) / n_x
                symin = sjtsk_ymin + bbox_h * iy / n_y
                symax = sjtsk_ymin + bbox_h * (iy + 1) / n_y
                w = last_sub_w if ix == n_x - 1 else sub_w
                h = last_sub_h if iy == n_y - 1 else sub_h
                params = urllib.parse.urlencode({
                    "service": "WMS",
                    "version": "1.1.1",
                    "request": "GetMap",
                    "layers": "0",
                    "styles": "",
                    "SRS": "EPSG:5514",
                    "BBOX": f"{sxmin},{symin},{sxmax},{symax}",
                    "WIDTH": w,
                    "HEIGHT": h,
                    "FORMAT": "image/jpeg",
                })
                req = _urlreq.Request(f"{WMS}?{params}",
                                      headers={"User-Agent": "Mozilla/5.0"})
                try:
                    with _urlreq.urlopen(req, timeout=60) as resp:
                        ct = resp.headers.get("Content-Type", "")
                        body = resp.read()
                except Exception as e:
                    self.send_error(502, f"ČÚZK VHR WMS fetch: {e}")
                    return
                if not ct.startswith("image/"):
                    self.send_error(502,
                        f"ČÚZK WMS returned {ct}: {body[:200]!r}")
                    return
                sub = Image.open(BytesIO(body))
                paste_x = sub_w * ix
                paste_y = (native_h - sub_h * iy - sub.size[1])
                composite.paste(sub, (paste_x, paste_y))
                fetched_count += 1

        # WGS84 reprojection. The detail mesh UVs are computed over a
        # WGS-aligned envelope (gen_detail.py:367), so the texture must be
        # WGS-axis-aligned too — otherwise the SJTSK-axis composite shows
        # a ~7.7° rotation against the mesh (S-JTSK Krovak axes are rotated
        # against WGS by that angle). Over a 1 km bbox the corner offset
        # is ~135 m, which matches the user-reported "200 m square corner
        # missing" symptom. raw branch already does this; VHR was the only
        # path skipping it. Same reproject helper as raw.
        wbbox_str = query.get("WBBOX", [""])[0]
        if wbbox_str:
            try:
                west, south, east, north = (float(x) for x in wbbox_str.split(","))
            except ValueError:
                self.send_error(400, "WBBOX must be west,south,east,north (WGS84)")
                return
            import numpy as np
            import rasterio
            import rasterio.transform
            import rasterio.warp
            src_arr = np.array(composite)
            src_chw = src_arr.transpose(2, 0, 1).copy()
            src_transform = rasterio.transform.from_origin(
                sjtsk_xmin, sjtsk_ymax,
                (sjtsk_xmax - sjtsk_xmin) / composite.width,
                (sjtsk_ymax - sjtsk_ymin) / composite.height,
            )
            try:
                size_param = max(64, min(int(query.get("size", ["4096"])[0]), 8192))
            except ValueError:
                size_param = 4096
            out_w = out_h = size_param
            dst_transform = rasterio.transform.from_bounds(
                west, south, east, north, out_w, out_h,
            )
            dst_arr = np.zeros((3, out_h, out_w), dtype=np.uint8)
            rasterio.warp.reproject(
                src_chw, dst_arr,
                src_transform=src_transform, src_crs="EPSG:5514",
                dst_transform=dst_transform, dst_crs="EPSG:4326",
                resampling=rasterio.warp.Resampling.lanczos,
            )
            composite = Image.fromarray(dst_arr.transpose(1, 2, 0))

        # Resize + encode reusing the same pipeline as the raw branch.
        size_str = query.get("size", [""])[0]
        if size_str and not wbbox_str:
            # WBBOX path already sized the output via reproject; only
            # raw-SJTSK path needs the secondary resize.
            try:
                target = max(64, min(int(size_str), 8192))
                ratio = composite.width / max(composite.height, 1)
                if ratio >= 1:
                    new_w, new_h = target, max(1, int(target / ratio))
                else:
                    new_h, new_w = target, max(1, int(target * ratio))
                if new_w < composite.width or new_h < composite.height:
                    composite = composite.resize((new_w, new_h), Image.LANCZOS)
            except ValueError:
                pass

        buf = BytesIO()
        if out_format == "png":
            composite.save(buf, "PNG", optimize=True)
            mime = "image/png"
            data = buf.getvalue()
        elif out_format == "ktx2":
            composite.save(buf, "JPEG", quality=95, optimize=True, subsampling=0)
            import subprocess, tempfile, uuid
            ktx2_mode = query.get("ktx2", ["etc1s"])[0].lower()
            try:
                q_val = max(1, min(255, int(query.get("quality", ["220"])[0])))
            except ValueError:
                q_val = 220
            tmpdir = Path(tempfile.gettempdir())
            stem = f"orto_vhr_{uuid.uuid4().hex}"
            tmp_jpg = tmpdir / f"{stem}.jpg"
            tmp_ktx2 = tmpdir / f"{stem}.ktx2"
            cmd = ["basisu", "-ktx2", "-mipmap", "-y_flip"]
            if ktx2_mode == "uastc":
                cmd += ["-uastc", "-uastc_rdo_l", "2.0"]
            else:
                cmd += ["-q", str(q_val)]
            cmd += [str(tmp_jpg), "-output_file", str(tmp_ktx2)]
            try:
                tmp_jpg.write_bytes(buf.getvalue())
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                data = tmp_ktx2.read_bytes()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                self.send_error(500, f"basisu KTX2 encode failed: {e}")
                return
            finally:
                tmp_jpg.unlink(missing_ok=True)
                tmp_ktx2.unlink(missing_ok=True)
            mime = "image/ktx2"
        else:
            composite.save(buf, "JPEG", quality=95, optimize=True, subsampling=0)
            mime = "image/jpeg"
            data = buf.getvalue()

        cache_tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        cache_tmp.write_bytes(data)
        cache_tmp.replace(cache_path)

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Ortofoto-Source", f"VHR-WMS ({fetched_count} subtiles)")
        self.send_header("X-Ortofoto-Native-PxSize-cm", "12.5")
        self.send_header("X-Ortofoto-Native-Px", f"{native_w}x{native_h}")
        self.send_header("X-Ortofoto-Cache", "miss-vhr")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)


    def _proxy_ortofoto_raw(self, query):
        """Crop ČÚZK ortofoto from the locally cached raw SM5 JPEG.

        The canonical ČÚZK distribution is a per-SM5-list JPEG at native
        12.5 cm/px in S-JTSK with a JGW sidecar. By cropping that file
        directly we avoid:
          1. tile-service LOD interpolation above zoom 21
          2. the composite + LANCZOS resize cycle in this proxy
          3. our final JPEG re-encode (when format=png)

        Two coordinate inputs:
          BBOX  = S-JTSK xmin,ymin,xmax,ymax — output is SJTSK-aligned crop
          WBBOX = WGS84 west,south,east,north — output is reprojected to a
                  WGS84 axis-aligned image matching exactly that rectangle
                  (used by gen_multitile.py area viewers, whose mesh UVs are
                  WGS-aligned with a 5 % margin)

        Defaults to native pixel resolution (no upscale). Pass ?size=N to
        downscale; ?upscale=1 + ?size=N to force upscale above native.
        """
        from PIL import Image
        from io import BytesIO
        from pathlib import Path
        import hashlib

        wbbox_str = query.get("WBBOX", [""])[0]
        bbox_str = query.get("BBOX", [""])[0]
        if not wbbox_str and not bbox_str:
            self.send_error(400, "Missing BBOX or WBBOX")
            return

        # On-disk cache for rendered ortho — short-circuits the multi-sheet
        # composite + warp + resize cycle on repeat requests (outer 3km warp
        # is 42s on cold cache, ~1s warm). Cache key covers every parameter
        # that affects pixel output; raw SM5 files themselves don't change.
        # Manual cleanup: `rm -rf cache/orto_render/` to invalidate.
        out_format = query.get("format", ["jpeg"])[0].lower()
        if out_format not in ("jpeg", "jpg", "png", "ktx2"):
            out_format = "jpeg"
        cache_key_src = "|".join(
            f"{k}={query.get(k, [''])[0]}"
            for k in ("BBOX", "WBBOX", "size", "upscale", "format", "look",
                      "ktx2", "quality")
        )
        cache_key = hashlib.sha256(cache_key_src.encode()).hexdigest()[:16]
        cache_dir = Path("cache/orto_render")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{cache_key}.{out_format}"
        _mime_for = {
            "png": "image/png", "ktx2": "image/ktx2",
        }
        if cache_path.exists():
            data = cache_path.read_bytes()
            mime = _mime_for.get(out_format, "image/jpeg")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Ortofoto-Cache", "hit")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        # Determine the S-JTSK rectangle to crop from the source file.
        # Prefer BBOX as the authoritative SJTSK bbox (sheet finding uses it).
        # WBBOX is used only to define the WGS reproject output extent.
        west = south = east = north = None
        if wbbox_str:
            try:
                west, south, east, north = (float(x) for x in wbbox_str.split(","))
            except ValueError:
                self.send_error(400, "WBBOX must be west,south,east,north (WGS84)")
                return
        if bbox_str:
            try:
                sjtsk_xmin, sjtsk_ymin, sjtsk_xmax, sjtsk_ymax = (
                    float(x) for x in bbox_str.split(","))
            except ValueError:
                self.send_error(400, "BBOX must be xmin,ymin,xmax,ymax in S-JTSK")
                return
        else:
            # Fallback: derive SJTSK AABB from WBBOX corners (S-JTSK is rotated
            # ~7.7° from WGS, so this AABB overshoots the actual extent — only
            # used if caller didn't send a precise BBOX).
            from pyproj import Transformer
            to_sjtsk = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
            wgs_corners = [
                to_sjtsk.transform(west, south), to_sjtsk.transform(east, south),
                to_sjtsk.transform(west, north), to_sjtsk.transform(east, north),
            ]
            sx_list = [c[0] for c in wgs_corners]
            sy_list = [c[1] for c in wgs_corners]
            sjtsk_xmin, sjtsk_xmax = min(sx_list), max(sx_list)
            sjtsk_ymin, sjtsk_ymax = min(sy_list), max(sy_list)

        Image.MAX_IMAGE_PIXELS = None
        sheets = self._find_raw_ortofotos_covering(
            sjtsk_xmin, sjtsk_ymin, sjtsk_xmax, sjtsk_ymax,
        )
        if not sheets:
            self.send_error(404,
                "No raw ortofoto cached covering this BBOX. "
                "Download via download_ortofoto.py --code <MAPNOM>.")
            return

        # Composite all intersecting SM5 sheets onto an SJTSK-aligned canvas
        # at the sheets' native pixel resolution (12.5 cm). All SM5 sheets
        # share the same pixel scale so we use the first sheet's px size as
        # the reference. crop_x0 / crop_y0 are the world coords of the
        # composite's top-left corner.
        ref_px_x, ref_px_y = sheets[0][1][0], sheets[0][1][1]
        abs_px_x = abs(ref_px_x)
        abs_px_y = abs(ref_px_y)
        # Same XYZ-tile fallback clamp as the VHR branch: a ≤1024px output
        # doesn't need a native 12.5 cm/px composite (z=14 tile ≈ 12.5k px,
        # ~475 MB PIL image). Implemented by inflating the composite pixel
        # size — paste positions and the reproject transform scale with it.
        try:
            _tgt = int(query.get("size", ["0"])[0] or 0)
        except ValueError:
            _tgt = 0
        scale_k = 1.0
        if 0 < _tgt <= 1024:
            _nat = max((sjtsk_xmax - sjtsk_xmin) / abs_px_x,
                       (sjtsk_ymax - sjtsk_ymin) / abs_px_y, 1.0)
            scale_k = min(1.0, 2 * _tgt / _nat)
            abs_px_x /= scale_k
            abs_px_y /= scale_k
        crop_x0 = sjtsk_xmin
        crop_y0 = sjtsk_ymax           # top edge (px_y < 0 means rows scan down)
        out_w = max(1, int(round((sjtsk_xmax - sjtsk_xmin) / abs_px_x)))
        out_h = max(1, int(round((sjtsk_ymax - sjtsk_ymin) / abs_px_y)))
        composite = Image.new("RGB", (out_w, out_h), (0, 0, 0))
        used = []   # (filename, src_px_w, src_px_h) — for response header

        for jpg, vals in sheets:
            s_px_x, s_px_y, s_x0, s_y0, s_w, s_h = vals
            s_left = s_x0
            s_right = s_x0 + s_w * s_px_x
            s_top = s_y0
            s_bottom = s_y0 + s_h * s_px_y
            s_lo_x, s_hi_x = min(s_left, s_right), max(s_left, s_right)
            s_lo_y, s_hi_y = min(s_top, s_bottom), max(s_top, s_bottom)
            # Overlap in SJTSK
            ox_lo = max(s_lo_x, sjtsk_xmin)
            ox_hi = min(s_hi_x, sjtsk_xmax)
            oy_lo = max(s_lo_y, sjtsk_ymin)
            oy_hi = min(s_hi_y, sjtsk_ymax)
            if ox_hi <= ox_lo or oy_hi <= oy_lo:
                continue
            # Source pixel range inside this sheet
            sa_x = (ox_lo - s_x0) / s_px_x
            sb_x = (ox_hi - s_x0) / s_px_x
            sa_y = (oy_hi - s_y0) / s_px_y    # top of overlap (max y, low row)
            sb_y = (oy_lo - s_y0) / s_px_y    # bottom of overlap (min y, high row)
            spl = max(0, int(round(min(sa_x, sb_x))))
            spr = min(s_w, int(round(max(sa_x, sb_x))))
            spt = max(0, int(round(min(sa_y, sb_y))))
            spb = min(s_h, int(round(max(sa_y, sb_y))))
            if spr <= spl or spb <= spt:
                continue
            with Image.open(jpg) as src_img:
                src_crop = src_img.crop((spl, spt, spr, spb)).copy()
            if scale_k < 1.0:
                src_crop = src_crop.resize(
                    (max(1, int(round(src_crop.width * scale_k))),
                     max(1, int(round(src_crop.height * scale_k)))),
                    Image.LANCZOS)
            # Destination pixel position in composite (anchor top-left of crop)
            dpl = int(round((ox_lo - crop_x0) / abs_px_x))
            dpt = int(round((crop_y0 - oy_hi) / abs_px_y))
            composite.paste(src_crop, (dpl, dpt))
            used.append((jpg.name, src_crop.width, src_crop.height))

        if not used:
            self.send_error(400, "BBOX outside cached raw ortofoto coverage")
            return

        cropped = composite
        # Drive the WBBOX reproject and the rest of the pipeline off the
        # composite's reference. (Below code expects these names.)
        px_x = ref_px_x
        px_y = ref_px_y
        pl, pt = 0, 0
        pr, pb = composite.width, composite.height
        raw_path = type("MultiSheet", (), {"name": "+".join(u[0] for u in used)})()

        # If WBBOX requested → reproject the SJTSK crop to a WGS84 axis-
        # aligned image whose extent matches WBBOX exactly. Required for
        # gen_multitile.py area-viewer textures (mesh UVs are WGS-aligned).
        if wbbox_str:
            import numpy as np
            import rasterio
            import rasterio.transform
            import rasterio.warp

            # crop_x0 / crop_y0 already set on the composite (top-left =
            # sjtsk_xmin, sjtsk_ymax); just feed them into the warp transform.
            src_arr = np.array(cropped)        # H × W × 3
            if src_arr.ndim == 2:
                src_arr = np.stack([src_arr]*3, axis=-1)
            src_chw = src_arr.transpose(2, 0, 1).copy()
            src_h, src_w = src_arr.shape[:2]
            # abs_px_*, not abs(px_*): the clamp above may have inflated the
            # composite pixel size, and the transform must match the pixels.
            src_transform = rasterio.transform.from_origin(
                crop_x0, crop_y0, abs_px_x, abs_px_y,
            )
            # Output size — square to match what the area viewer asks for.
            try:
                size_param = max(64, min(int(query.get("size", ["768"])[0]), 8192))
            except ValueError:
                size_param = 768
            out_w = out_h = size_param
            dst_transform = rasterio.transform.from_bounds(
                west, south, east, north, out_w, out_h,
            )
            dst_arr = np.zeros((3, out_h, out_w), dtype=np.uint8)
            rasterio.warp.reproject(
                src_chw, dst_arr,
                src_transform=src_transform, src_crs="EPSG:5514",
                dst_transform=dst_transform, dst_crs="EPSG:4326",
                resampling=rasterio.warp.Resampling.lanczos,
            )
            cropped = Image.fromarray(dst_arr.transpose(1, 2, 0))

        size_str = query.get("size", [""])[0]
        upscale = query.get("upscale", ["0"])[0] == "1"
        if size_str:
            try:
                target = max(64, min(int(size_str), 8192))
                ratio = cropped.width / max(cropped.height, 1)
                if ratio >= 1:
                    new_w, new_h = target, max(1, int(target / ratio))
                else:
                    new_h, new_w = target, max(1, int(target * ratio))
                if (new_w > cropped.width or new_h > cropped.height) and not upscale:
                    pass  # keep native — no fake detail
                else:
                    cropped = cropped.resize((new_w, new_h), Image.LANCZOS)
            except ValueError:
                pass

        out_format = query.get("format", ["jpeg"])[0].lower()
        if out_format not in ("jpeg", "jpg", "png", "ktx2"):
            out_format = "jpeg"
        buf = BytesIO()
        if out_format == "png":
            cropped.save(buf, "PNG", optimize=True)
            mime = "image/png"
            data = buf.getvalue()
        elif out_format == "ktx2":
            # basisu doesn't take stdin → write JPEG, run encoder, read KTX2.
            # Mode/quality from query: ?ktx2=etc1s|uastc, ?quality=1..255 (ETC1S
            # only; lower = better quality, default 220). UASTC mode ignores -q
            # and produces ~5× larger files at near-original quality.
            cropped.save(buf, "JPEG", quality=95, optimize=True, subsampling=0)
            import subprocess, tempfile, uuid
            ktx2_mode = query.get("ktx2", ["etc1s"])[0].lower()
            try:
                q_val = max(1, min(255, int(query.get("quality", ["220"])[0])))
            except ValueError:
                q_val = 220
            tmpdir = Path(tempfile.gettempdir())
            stem = f"orto_{uuid.uuid4().hex}"
            tmp_jpg = tmpdir / f"{stem}.jpg"
            tmp_ktx2 = tmpdir / f"{stem}.ktx2"
            cmd = ["basisu", "-ktx2", "-mipmap", "-y_flip"]
            if ktx2_mode == "uastc":
                cmd += ["-uastc", "-uastc_rdo_l", "2.0"]
            else:
                cmd += ["-q", str(q_val)]
            cmd += [str(tmp_jpg), "-output_file", str(tmp_ktx2)]
            try:
                tmp_jpg.write_bytes(buf.getvalue())
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                data = tmp_ktx2.read_bytes()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                self.send_error(500, f"basisu KTX2 encode failed: {e}")
                return
            finally:
                tmp_jpg.unlink(missing_ok=True)
                tmp_ktx2.unlink(missing_ok=True)
            mime = "image/ktx2"
        else:
            cropped.save(buf, "JPEG", quality=95, optimize=True, subsampling=0)
            mime = "image/jpeg"
            data = buf.getvalue()

        # Persist to the on-disk render cache (write to a temp file first so a
        # mid-write crash doesn't leave a half-baked cache entry that future
        # requests would happily serve).
        cache_tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        cache_tmp.write_bytes(data)
        cache_tmp.replace(cache_path)

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Ortofoto-Source", str(raw_path.name))
        self.send_header("X-Ortofoto-Native-PxSize-cm",
                         f"{abs(px_x) * 100:.1f}")
        self.send_header("X-Ortofoto-Crop-Px", f"{pr - pl}x{pb - pt}")
        self.send_header("X-Ortofoto-Cache", "miss")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)


    def _query_sjtsk_bbox(self, query):
        """S-JTSK (xmin,ymin,xmax,ymax) for the request — from BBOX when it is
        already S-JTSK, else by reprojecting the WBBOX corners. None if neither
        is usable. Used to test local raw coverage before hitting the WMS."""
        bbox_str = query.get("BBOX", [""])[0]
        if bbox_str:
            try:
                p = [float(x) for x in bbox_str.split(",")]
            except ValueError:
                p = []
            if len(p) == 4 and p[0] < 0 and p[1] < 0:   # S-JTSK is negative E,N in CZ
                return (p[0], p[1], p[2], p[3])
        wbbox_q = query.get("WBBOX", [""])[0]
        if wbbox_q:
            try:
                wp = [float(x) for x in wbbox_q.split(",")]
            except ValueError:
                return None
            if len(wp) == 4:
                from pyproj import Transformer
                to_sjtsk = Transformer.from_crs("EPSG:4326", "EPSG:5514",
                                                always_xy=True)
                c = [to_sjtsk.transform(lon, lat)
                     for lon in (wp[0], wp[2]) for lat in (wp[1], wp[3])]
                xs = [q[0] for q in c]
                ys = [q[1] for q in c]
                return (min(xs), min(ys), max(xs), max(ys))
        return None

    def _find_raw_ortofotos_covering(self, xmin, ymin, xmax, ymax):
        """Return a list of (jpg_path, jgw_vals) for cached raw SM5 orthos
        that COLLECTIVELY cover the given S-JTSK bbox. Empty list if not
        fully covered (so the caller falls through to the tile pipeline).

        Coverage check: every corner of the requested bbox must lie inside
        at least one candidate sheet (SM5 sheets tile cleanly with no gaps
        so corner-coverage implies area-coverage for axis-aligned bboxes).
        """
        from PIL import Image
        from pathlib import Path
        Image.MAX_IMAGE_PIXELS = None

        # Candidate sheets via the in-memory DMPOK inventory bboxes (same SM5
        # grid as ortho) — microseconds, no disk. Then touch disk only for the
        # handful that intersect, reading each one's real JGW (authoritative
        # pixel scale; the inventory bbox is only accurate enough to filter).
        try:
            inv = _dmpok_inventory()
        except Exception:
            inv = {}
        cand_mapnoms = [
            m for m, b in inv.items()
            if b["left"] < xmax and b["right"] > xmin
            and b["bottom"] < ymax and b["top"] > ymin
        ]
        roots = [Path("cache"), ORTHO_BULK_DIR]
        candidates = []  # (jpg, vals, (lo_x, lo_y, hi_x, hi_y))
        for mapnom in cand_mapnoms:
            jpg = jgw_path = None
            for root in roots:
                d = root / f"ortofoto_{mapnom}"
                if d.is_dir():
                    for j in d.glob("*.jpg"):
                        if j.with_suffix(".jgw").exists():
                            jpg, jgw_path = j, j.with_suffix(".jgw")
                            break
                if jpg:
                    break
            if not jpg:
                continue
            try:
                with open(jgw_path) as f:
                    vals = [float(line.strip()) for line in f if line.strip()]
                if len(vals) != 6:
                    continue
                px_x, _, _, px_y, x0, y0 = vals
                with Image.open(jpg) as probe:
                    w, h = probe.size
            except Exception:
                continue
            vals_full = (px_x, px_y, x0, y0, w, h)
            left, right = x0, x0 + w * px_x
            top, bottom = y0, y0 + h * px_y
            lo_x, hi_x = min(left, right), max(left, right)
            lo_y, hi_y = min(top, bottom), max(top, bottom)
            if hi_x <= xmin or lo_x >= xmax or hi_y <= ymin or lo_y >= ymax:
                continue
            candidates.append((jpg, vals_full, (lo_x, lo_y, hi_x, hi_y)))

        if not candidates:
            return []

        corners = [(xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)]
        for cx, cy in corners:
            if not any(lo_x <= cx <= hi_x and lo_y <= cy <= hi_y
                       for _, _, (lo_x, lo_y, hi_x, hi_y) in candidates):
                return []
        return [(c[0], c[1]) for c in candidates]

    def _proxy_ortofoto(self):
        """Compose ČÚZK ortofoto from tiles matching S-JTSK bbox."""
        import math
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        # ?hires=1 — 0.125 m/px native ortho. Prefer the local raw SM5 sheets
        # (the bulk_ortofoto pull) over the WMS: same ČÚZK source, but no double
        # JPEG re-encode and no network round-trip. Fall back to the public WMS
        # only when the bbox isn't fully covered by local sheets.
        if query.get("hires", [""])[0] == "1":
            try:
                sj = self._query_sjtsk_bbox(query)
                if sj and self._find_raw_ortofotos_covering(*sj):
                    return self._proxy_ortofoto_raw(query)
            except Exception as e:
                print(f"[ortofoto] hires raw-route failed, using WMS: {e}")
            return self._proxy_ortofoto_vhr(query)
        # ?source=raw — crop directly from the locally cached raw SM5 JPEG
        # (ČÚZK 0.25 m/px). Eliminates tile-service upscaling + our
        # composite/re-encode pipeline. Highest available quality from cache.
        if query.get("source", [""])[0] == "raw":
            return self._proxy_ortofoto_raw(query)
        bbox_str = query.get("BBOX", [""])[0]
        if not bbox_str:
            self.send_error(400, "Missing BBOX")
            return
        # Auto-route to raw when the bbox (BBOX or WBBOX) is fully covered
        # by a cached raw SM5 file. Eliminates the tile-service round trip
        # (which rate-limits hard during area-viewer bulk loads).
        try:
            sj_check = None
            parts = [float(x) for x in bbox_str.split(",")]
            if len(parts) == 4 and parts[0] < 0 and parts[1] < 0:
                sj_check = parts                                  # BBOX is SJTSK
            else:
                # BBOX might be Mercator (Leaflet WMS). Use WBBOX if set.
                wbbox_q = query.get("WBBOX", [""])[0]
                if wbbox_q:
                    wp = [float(x) for x in wbbox_q.split(",")]
                    if len(wp) == 4:
                        from pyproj import Transformer
                        to_sjtsk = Transformer.from_crs(
                            "EPSG:4326", "EPSG:5514", always_xy=True)
                        c = [
                            to_sjtsk.transform(wp[0], wp[1]),
                            to_sjtsk.transform(wp[2], wp[1]),
                            to_sjtsk.transform(wp[0], wp[3]),
                            to_sjtsk.transform(wp[2], wp[3]),
                        ]
                        xs = [p[0] for p in c]; ys = [p[1] for p in c]
                        sj_check = [min(xs), min(ys), max(xs), max(ys)]
            if sj_check and self._find_raw_ortofotos_covering(*sj_check):
                print(f"[ortofoto] auto-route → raw for bbox {sj_check}")
                return self._proxy_ortofoto_raw(query)
        except Exception as e:
            import traceback
            print(f"[ortofoto] auto-route FAILED, falling through: {e}")
            traceback.print_exc()

        # Use exact WGS84 bbox if provided (matches UV computation exactly)
        wbbox_str = query.get("WBBOX", [""])[0]
        if wbbox_str:
            wparts = [float(x) for x in wbbox_str.split(",")]
            min_lon, min_lat, max_lon, max_lat = wparts
        else:
            parts = [float(x) for x in bbox_str.split(",")]
            sjtsk_xmin, sjtsk_ymin, sjtsk_xmax, sjtsk_ymax = parts
            from pyproj import Transformer
            t = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
            lon1, lat1 = t.transform(sjtsk_xmin, sjtsk_ymin)
            lon2, lat2 = t.transform(sjtsk_xmax, sjtsk_ymax)
            min_lon, max_lon = min(lon1, lon2), max(lon1, lon2)
            min_lat, max_lat = min(lat1, lat2), max(lat1, lat2)

        # Zoom configurable — 19 is ~19cm/px, 20 is ~9.7cm/px (ČÚZK native)
        try:
            zoom = int(query.get("zoom", ["19"])[0])
        except ValueError:
            zoom = 19
        # zoom 23 is the deepest LOD ČÚZK serves (~1.87 cm/px effective).
        # Beyond zoom 21 the data is server-side interpolated.
        # Below 17 is for panoramic / horizon use (LOD outer rings) — z=10
        # is ~80m/px, z=11 ~40m/px, z=12 ~20m/px. Min z=7 prevents
        # accidentally fetching a single tile covering an entire country.
        zoom = max(7, min(23, zoom))
        n = 2 ** zoom

        def deg2tile(lat, lon):
            x = int((lon + 180.0) / 360.0 * n)
            y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1/math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
            return x, y

        def tile2deg(x, y):
            lon = x / n * 360.0 - 180.0
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
            return lat, lon

        tx_min, ty_max = deg2tile(min_lat, min_lon)
        tx_max, ty_min = deg2tile(max_lat, max_lon)

        cols = tx_max - tx_min + 1
        rows = ty_max - ty_min + 1

        from PIL import Image
        from io import BytesIO
        composite = Image.new("RGB", (cols * 256, rows * 256))

        source = query.get("source", ["cuzk"])[0]

        # Archival WMS (year source like "2018", "2005")
        if source.isdigit() and len(source) == 4:
            # Use S-JTSK bbox (ČÚZK archival WMS works reliably with EPSG:5514)
            parts = [float(x) for x in bbox_str.split(",")]
            sjtsk_xmin, sjtsk_ymin, sjtsk_xmax, sjtsk_ymax = parts
            wms_url = (
                f"https://geoportal.cuzk.cz/WMS_ORTOFOTO_ARCHIV/WMService.aspx?"
                f"SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={source}"
                f"&SRS=EPSG:5514&BBOX={sjtsk_xmin},{sjtsk_ymin},{sjtsk_xmax},{sjtsk_ymax}"
                f"&WIDTH=2048&HEIGHT=2048&FORMAT=image/jpeg&STYLES="
            )
            try:
                req = urllib.request.Request(wms_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(502, f"Archival WMS error: {e}")
            return

        # Tile-based sources
        tile_sources = {
            "cuzk": "https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO_WM/MapServer/tile/{z}/{y}/{x}",
            "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "google": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        }
        tile_template = tile_sources.get(source, tile_sources["cuzk"])

        # Fetch source tiles in parallel with global ČÚZK rate limit,
        # in-memory cache, 3 retries with backoff, and zoom-1 fallback.
        from concurrent.futures import ThreadPoolExecutor
        import time as _time
        import sys as _sys
        def _fetch_raw(z, tx, ty, source_key=None, timeout=20):
            src = source_key or source
            key = (src, z, tx, ty)
            cached = _cache_get(key)
            if cached is not None:
                return cached
            tpl = tile_sources.get(src, tile_sources["cuzk"])
            url = tpl.format(z=z, y=ty, x=tx)
            with _CUZK_SEM:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    img = Image.open(BytesIO(resp.read())).convert("RGB")
            _cache_put(key, img)
            return img

        def _fetch_subtile(tx, ty):
            last_err = None
            # 1. Try main source. Skip retries on HTTP 404 — that just means
            # this LOD has no tile here and ČÚZK won't grow one with retries.
            for attempt in range(3):
                try:
                    return (tx, ty, _fetch_raw(zoom, tx, ty))
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code == 404:
                        break  # straight to parent-tile fallback
                    _time.sleep(0.3 * (2 ** attempt))
                except Exception as e:
                    last_err = e
                    _time.sleep(0.3 * (2 ** attempt))
            # 2. Fallback: zoom-1 parent tile from main source. Walk up the
            # pyramid until we find a tile that exists (zoom 21 is fully
            # populated, so we stop there at worst).
            for parent_lvl in range(1, zoom - 16):
                pz = zoom - parent_lvl
                ptx, pty = tx >> parent_lvl, ty >> parent_lvl
                try:
                    fb = _fetch_raw(pz, ptx, pty)
                    # Quadrant within the parent tile, scaled by 2^parent_lvl.
                    scale = 1 << parent_lvl
                    qsize = 256 // scale
                    qx = (tx & (scale - 1)) * qsize
                    qy = (ty & (scale - 1)) * qsize
                    return (tx, ty,
                            fb.crop((qx, qy, qx + qsize, qy + qsize))
                              .resize((256, 256), Image.LANCZOS))
                except Exception:
                    continue
            # 3. Last resort (only for default 'cuzk'): ESRI World Imagery covers
            # Polish/border areas where ČÚZK has no data. Slight color/quality
            # shift across border but no black voids.
            if source == "cuzk":
                try:
                    return (tx, ty, _fetch_raw(zoom, tx, ty, source_key="esri", timeout=15))
                except Exception as e_esri:
                    print(f"[ortofoto] ESRI fallback fail {tx},{ty}: {e_esri}", file=_sys.stderr)
            print(f"[ortofoto] FAIL z{zoom} {tx},{ty}: {last_err}", file=_sys.stderr)
            return (tx, ty, None)

        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda p: _fetch_subtile(*p),
                                  [(tx, ty) for ty in range(ty_min, ty_max + 1)
                                            for tx in range(tx_min, tx_max + 1)]))
        fetched = sum(1 for _, _, img in results if img is not None)
        total = len(results)
        for tx, ty, img in results:
            if img is not None:
                composite.paste(img, ((tx - tx_min) * 256, (ty - ty_min) * 256))
        if fetched < total:
            print(f"[ortofoto] z{zoom}: {fetched}/{total} subtiles OK", file=_sys.stderr)
        # Reject composites only when nearly ALL subtiles failed (extreme edge).
        # ESRI fallback covers Polish/border voids, so 60% threshold was too strict.
        if total > 0 and fetched / total < 0.15:
            self.send_error(503, f"ortofoto: only {fetched}/{total} subtiles")
            return

        # Crop to exact bbox
        top_lat, left_lon = tile2deg(tx_min, ty_min)
        bot_lat, right_lon = tile2deg(tx_max + 1, ty_max + 1)

        px_left = int((min_lon - left_lon) / (right_lon - left_lon) * composite.width)
        px_right = int((max_lon - left_lon) / (right_lon - left_lon) * composite.width)
        px_top = int((top_lat - max_lat) / (top_lat - bot_lat) * composite.height)
        px_bot = int((top_lat - min_lat) / (top_lat - bot_lat) * composite.height)

        cropped = composite.crop((max(0, px_left), max(0, px_top), min(composite.width, px_right), min(composite.height, px_bot)))
        size_param = int(query.get("size", ["512"])[0])
        size_param = min(max(size_param, 256), 8192)
        cropped = cropped.resize((size_param, size_param), Image.LANCZOS)

        # Apply game look
        look = query.get("look", [""])[0]
        if look:
            cropped = self._apply_look(cropped, look)

        # Output format: ?format=png returns lossless PNG (no second-stage
        # JPEG re-compression on top of ČÚZK's source JPEG tiles). Default
        # remains JPEG for bandwidth.
        out_format = query.get("format", ["jpeg"])[0].lower()
        if out_format not in ("jpeg", "jpg", "png", "ktx2"):
            out_format = "jpeg"
        buf = BytesIO()
        if out_format == "png":
            cropped.save(buf, "PNG", optimize=True)
            mime = "image/png"
            data = buf.getvalue()
        elif out_format == "ktx2":
            jpeg_quality = {256: 60, 512: 70, 640: 75, 768: 90, 1024: 92, 2048: 94}.get(size_param, 85)
            cropped.save(buf, "JPEG", quality=jpeg_quality, optimize=True, subsampling=0)
            import subprocess, tempfile, uuid
            ktx2_mode = query.get("ktx2", ["etc1s"])[0].lower()
            try:
                q_val = max(1, min(255, int(query.get("quality", ["220"])[0])))
            except ValueError:
                q_val = 220
            tmpdir = Path(tempfile.gettempdir())
            stem = f"orto_{uuid.uuid4().hex}"
            tmp_jpg = tmpdir / f"{stem}.jpg"
            tmp_ktx2 = tmpdir / f"{stem}.ktx2"
            cmd = ["basisu", "-ktx2", "-mipmap", "-y_flip"]
            if ktx2_mode == "uastc":
                cmd += ["-uastc", "-uastc_rdo_l", "2.0"]
            else:
                cmd += ["-q", str(q_val)]
            cmd += [str(tmp_jpg), "-output_file", str(tmp_ktx2)]
            try:
                tmp_jpg.write_bytes(buf.getvalue())
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                data = tmp_ktx2.read_bytes()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                self.send_error(500, f"basisu KTX2 encode failed: {e}")
                return
            finally:
                tmp_jpg.unlink(missing_ok=True)
                tmp_ktx2.unlink(missing_ok=True)
            mime = "image/ktx2"
        else:
            jpeg_quality = {256: 60, 512: 70, 640: 75, 768: 90, 1024: 92, 2048: 94}.get(size_param, 85)
            cropped.save(buf, "JPEG", quality=jpeg_quality, optimize=True, subsampling=0)
            mime = "image/jpeg"
            data = buf.getvalue()

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)


    @staticmethod
    def _apply_look(img, look):
        """Apply game-style visual filter to ortofoto."""
        from PIL import Image, ImageEnhance, ImageFilter
        import numpy as np

        if look == "gta":
            # GTA-style: boost saturation + contrast + warm tint + slight posterize
            img = ImageEnhance.Color(img).enhance(1.6)
            img = ImageEnhance.Contrast(img).enhance(1.3)
            img = ImageEnhance.Brightness(img).enhance(1.05)
            # Warm tint
            arr = np.array(img, dtype=np.float32)
            arr[:,:,0] = np.clip(arr[:,:,0] * 1.08, 0, 255)  # red boost
            arr[:,:,2] = np.clip(arr[:,:,2] * 0.9, 0, 255)   # blue reduce
            # Posterize (reduce to ~24 levels per channel)
            arr = (arr / 10).astype(np.uint8) * 10
            img = Image.fromarray(arr.astype(np.uint8))

        elif look == "pixel":
            # Pixel art: downsample to 64x64, upscale nearest
            small = img.resize((64, 64), Image.LANCZOS)
            small = ImageEnhance.Color(small).enhance(1.8)
            small = ImageEnhance.Contrast(small).enhance(1.4)
            # Posterize hard
            arr = np.array(small, dtype=np.uint8)
            arr = (arr // 32) * 32 + 16
            small = Image.fromarray(arr)
            img = small.resize(img.size, Image.NEAREST)

        elif look == "toon":
            # Cartoon: heavy posterize + edge darkening + saturation
            img = ImageEnhance.Color(img).enhance(2.0)
            img = ImageEnhance.Contrast(img).enhance(1.5)
            arr = np.array(img, dtype=np.uint8)
            # Posterize to 6 levels
            arr = (arr // 42) * 42 + 21
            img = Image.fromarray(arr)
            # Edge detection overlay
            edges = img.convert("L").filter(ImageFilter.FIND_EDGES)
            edges = ImageEnhance.Contrast(edges).enhance(3.0)
            edges_arr = np.array(edges)
            # Darken where edges are strong
            arr = np.array(img, dtype=np.float32)
            edge_mask = (edges_arr > 30).astype(np.float32)
            for ch in range(3):
                arr[:,:,ch] *= (1.0 - edge_mask * 0.6)
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

        elif look == "vice":
            # Vice City: pink/cyan palette + high contrast
            img = ImageEnhance.Color(img).enhance(1.4)
            img = ImageEnhance.Contrast(img).enhance(1.4)
            arr = np.array(img, dtype=np.float32)
            # Shift toward pink/magenta highlights, cyan shadows
            lum = arr.mean(axis=2)
            bright = (lum > 140).astype(np.float32)
            dark = (lum < 100).astype(np.float32)
            arr[:,:,0] += bright * 20   # pink highlights
            arr[:,:,2] += bright * 10
            arr[:,:,1] += dark * 15     # cyan shadows
            arr[:,:,2] += dark * 20
            arr = np.clip(arr, 0, 255)
            arr = (arr / 12).astype(np.uint8) * 12
            img = Image.fromarray(arr.astype(np.uint8))

        elif look == "night":
            # Night mode: dark blue tint + low brightness
            img = ImageEnhance.Brightness(img).enhance(0.3)
            img = ImageEnhance.Color(img).enhance(0.5)
            arr = np.array(img, dtype=np.float32)
            arr[:,:,0] *= 0.6
            arr[:,:,1] *= 0.7
            arr[:,:,2] = np.clip(arr[:,:,2] * 1.3 + 15, 0, 255)
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

        return img

    def do_GET(self):
        # Parse path + query once for Lokace UI /api/* endpoints
        _parsed = urllib.parse.urlparse(self.path)
        _path = _parsed.path
        _query = urllib.parse.parse_qs(_parsed.query)

        # API endpoints (Lokace UI)
        if _path == "/api/locations":
            self._send_json(200, locations.list_locations())
            return
        if _path == "/api/ruian/search":
            q = _query.get("q", [""])[0]
            kind = _query.get("kind", ["all"])[0]
            if kind not in ("all", "parcel", "address"):
                self._send_json(400, {"error": "kind must be 'all', 'parcel', or 'address'"})
                return
            try:
                results = locations.ruian_search(q, kind=kind)
            except locations.RuianUnavailable as e:
                self._send_json(503, {"error": f"ČÚZK unavailable: {e}"})
                return
            self._send_json(200, results)
            return
        if _path == "/api/sjtsk2wgs":
            # Used by the index.html manual-S-JTSK fallback so the verify
            # map can still place a marker without bundling proj4 client-side.
            try:
                cx = float(_query.get("cx", ["nan"])[0])
                cy = float(_query.get("cy", ["nan"])[0])
            except ValueError:
                self._send_json(400, {"error": "cx, cy must be numbers"})
                return
            lat, lon = locations._sjtsk_to_wgs(cx, cy)
            self._send_json(200, {"lat": lat, "lon": lon})
            return
        if _path == "/api/jobs":
            # ?active=1 (default) → running + queued only. Anything else
            # → all known jobs, including terminated ones (in-memory only,
            # not persisted, so this is "since worker process started").
            active_only = _query.get("active", ["1"])[0] == "1"
            if active_only:
                self._send_json(200, locations.list_active_jobs())
            else:
                self._send_json(200, list(locations.JOBS.values()))
            return
        if _path.startswith("/api/jobs/"):
            job_id = _path[len("/api/jobs/"):]
            job = locations.get_job(job_id)
            if job is None:
                self._send_json(404, {"error": "job not found"})
                return
            self._send_json(200, job)
            return

        if self.path.startswith("/proxy/ortofoto?"):
            self._proxy_ortofoto()
        elif self.path.startswith("/proxy/cadastre?"):
            self._proxy_cadastre()
        elif self.path.startswith("/proxy/osm?"):
            self._proxy_osm()
        elif self.path.startswith("/api/sjtsk-to-wgs?"):
            self._api_sjtsk_to_wgs()
        elif self.path == "/api/image-edit/prompt":
            self._api_image_edit_prompt()
        elif self.path == "/api/image-edit/status":
            self._api_image_edit_status()
        elif self.path.startswith("/api/buildings?"):
            self._api_buildings()
        elif self.path.startswith("/api/parcel-at-point?"):
            self._api_parcel_at_point()
        elif self.path.startswith("/api/parcels?"):
            self._api_parcels()
        elif self.path.startswith("/api/roads?"):
            self._api_roads()
        elif self.path.startswith("/api/poi?"):
            self._api_poi()
        elif self.path.startswith("/api/wiki?"):
            self._api_wiki()
        elif self.path.startswith("/api/building-detail?"):
            self._api_building_detail()
        elif _path.startswith("/cuzk-pyramid/dmpok/") and _path.endswith(".lerc"):
            self._serve_pyramid_tile(_path)
        elif self.path.endswith(".glb"):
            self._serve_glb_gzipped()
        else:
            super().do_GET()

    def _serve_pyramid_tile(self, path):
        """Serve a heightmap pyramid tile, building it on demand from DMPOK when
        the static .lerc is missing (z>=14). Built tiles are written to disk so
        the next request is a plain static serve."""
        import re
        m = re.match(r"^/cuzk-pyramid/dmpok/(\d+)/(\d+)/(\d+)\.lerc$", path)
        if not m:
            self.send_error(404)
            return
        z, x, y = (int(g) for g in m.groups())
        bpt = _pyramid_module()
        out_path = bpt.OUT_DIR / "dmpok" / str(z) / str(x) / f"{y}.lerc"
        if not out_path.exists():
            if not (_PYRAMID_ONDEMAND_ZMIN <= z <= _PYRAMID_ONDEMAND_ZMAX):
                self.send_error(
                    404, f"tile not pre-baked (on-demand only z="
                         f"{_PYRAMID_ONDEMAND_ZMIN}..{_PYRAMID_ONDEMAND_ZMAX})")
                return
            # Negative cache: DMPOK coverage is static, so a no-coverage tile
            # 404s forever — without the sentinel every repeat request re-ran
            # the whole build attempt (permit + pyproj + inventory scan).
            empty_marker = out_path.with_suffix(".empty")
            if empty_marker.exists():
                self.send_error(404, "no DMPOK coverage for this tile")
                return
            # Per-tile lock FIRST, build permit second: duplicate requests
            # for the same missing tile (phone + desktop over one village)
            # park on the tile lock WITHOUT each burning one of the 3 build
            # permits — 3 waiters on a hot tile starved every other build.
            with _pyramid_tile_lock((z, x, y)):
                if not out_path.exists():                 # built while we waited
                    if empty_marker.exists():             # ...or negative-cached
                        self.send_error(404, "no DMPOK coverage for this tile")
                        return
                    try:
                        with _PYRAMID_BUILD_SEM:
                            ok = bpt.build_tile(z, x, y, bpt.BULK_OUT_DIR, bpt.OUT_DIR,
                                                max_z_error=0.10, overwrite=False)
                    except Exception as e:                # noqa: BLE001
                        self.send_error(500, f"pyramid tile build failed: {e}")
                        return
                    if not ok:
                        try:                              # sentinel is best-effort
                            empty_marker.parent.mkdir(parents=True, exist_ok=True)
                            empty_marker.touch()
                        except OSError:
                            pass
                        self.send_error(404, "no DMPOK coverage for this tile")
                        return
        # File exists now (pre-baked or just built) — let the static handler
        # serve it through the cuzk-pyramid symlink.
        super().do_GET()

    def _api_buildings(self):
        """Fetch RÚIAN building footprints, return in local coords relative to gcx,gcy."""
        import json
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        try:
            gcx = float(query["gcx"][0])
            gcy = float(query["gcy"][0])
            radius = _clamp_radius(float(query.get("radius", ["600"])[0]))
        except (KeyError, ValueError, IndexError):
            self.send_error(400, "Required params: gcx, gcy (S-JTSK), optional radius")
            return

        cache_key = f"{gcx:.0f}_{gcy:.0f}_{radius:.0f}"
        data = _BUILDINGS_CACHE.get(cache_key)
        if data is None:
            ruian_url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/3/query"
            # Page through results — RÚIAN soft-caps each response below
            # maxRecordCount; without pagination a 3 km closeup ring in a
            # dense village silently truncates at the first page. Same loop
            # shape as _fetch_parcels_area; orderBy keeps pages deterministic.
            page_size = 5000
            offset = 0
            raw_features = []
            try:
                while True:
                    raw = json.loads(_ruian_get(
                        f"{ruian_url}?" + urllib.parse.urlencode({
                            "geometry": json.dumps({
                                "xmin": gcx - radius, "ymin": gcy - radius,
                                "xmax": gcx + radius, "ymax": gcy + radius,
                                "spatialReference": {"wkid": 5514},
                            }),
                            "geometryType": "esriGeometryEnvelope",
                            "spatialRel": "esriSpatialRelIntersects",
                            "outFields": "kod,cisladomovni,zpusobvyuzitikod,zastavenaplocha",
                            "outSR": "5514", "f": "json", "returnGeometry": "true",
                            "orderByFields": "objectid",
                            "resultOffset": str(offset),
                            "resultRecordCount": str(page_size),
                        }),
                        timeout=60,
                    ))
                    feats = raw.get("features", [])
                    # Same guard as _fetch_parcels_area — empty page with
                    # exceededTransferLimit=true would loop forever otherwise.
                    if not feats:
                        break
                    raw_features.extend(feats)
                    more = raw.get("exceededTransferLimit") or len(feats) >= page_size
                    if not more:
                        break
                    offset += len(feats)
                    if offset > 50000:
                        break
            except Exception as e:
                self.send_error(502, f"RÚIAN query failed: {e}")
                return

            buildings = []
            for f in raw_features:
                rings = f["geometry"]["rings"][0]
                a = f["attributes"]
                # Convert S-JTSK to local coords (same system as GLB meshes)
                local_coords = []
                for sx, sy in rings:
                    lx = sx - gcx
                    lz = -(sy - gcy)
                    local_coords.append([round(lx, 2), round(lz, 2)])
                # Compute area from S-JTSK coords
                area = 0
                for i in range(len(rings) - 1):
                    area += rings[i][0] * rings[i+1][1] - rings[i+1][0] * rings[i][1]
                area = abs(area) / 2
                if area < 1:
                    continue
                buildings.append({
                    "kod": a.get("kod"),
                    "cislo": a.get("cisladomovni", "") or "",
                    "zpusob": a.get("zpusobvyuzitikod", 0),
                    "plocha": round(area, 0),
                    "coords": local_coords,
                })

            data = json.dumps(buildings).encode()
            _BUILDINGS_CACHE.put(cache_key, data)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _api_parcels(self):
        """Fetch RÚIAN parcels in a square around (gcx, gcy); cache to disk."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            gcx = float(query["gcx"][0])
            gcy = float(query["gcy"][0])
            radius = _clamp_radius(float(query.get("radius", ["2000"])[0]))
        except (KeyError, ValueError):
            self.send_error(400, "Required params: gcx, gcy (S-JTSK), optional radius")
            return

        cache_path = Path("cache") / f"parcels_{gcx:.0f}_{gcy:.0f}_{radius:.0f}.json"
        if cache_path.exists():
            body = cache_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            parcels = _fetch_parcels_area(gcx, gcy, radius)
        except Exception as e:
            # No fallback — disk cache miss + RÚIAN unreachable = 503.
            import sys as _sys
            print(f"[parcels] fetch failed: {e}", file=_sys.stderr)
            self.send_error(503, f"parcels: {e}")
            return

        body = json.dumps(parcels).encode("utf-8")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_parcel_at_point(self):
        """Find the RÚIAN parcel containing a given SJTSK point.

        Query params:
          gcx, gcy — game/scene origin in SJTSK (used to compute local coords)
          sx, sy — SJTSK coordinates of the click point

        Returns single parcel dict with same shape as /api/parcels list element,
        or 404 if no parcel intersects.
        """
        import rasterio
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        # Two callers: the heightfield viewer sends S-JTSK (gcx,gcy,sx,sy) and
        # wants ring_local + DSM heights; map3d sends WGS (lon,lat, like
        # building-detail) and wants a lightweight ring_wgs + attrs, no TIFFs.
        wgs_mode = "lon" in query and "lat" in query
        if wgs_mode:
            try:
                lon = float(query["lon"][0])
                lat = float(query["lat"][0])
            except (KeyError, ValueError):
                self.send_error(400, "Required params: lon, lat (WGS84)")
                return
            from pyproj import Transformer
            sx, sy = Transformer.from_crs(
                "EPSG:4326", "EPSG:5514", always_xy=True).transform(lon, lat)
            out_sr = "4326"     # RÚIAN returns rings in WGS → ring_wgs directly
        else:
            try:
                gcx = float(query["gcx"][0])
                gcy = float(query["gcy"][0])
                sx = float(query["sx"][0])
                sy = float(query["sy"][0])
            except (KeyError, ValueError):
                self.send_error(400, "Required params: gcx, gcy, sx, sy (S-JTSK)")
                return
            out_sr = "5514"

        try:
            url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/5/query"
            params = {
                "geometry": json.dumps({
                    "x": sx, "y": sy,
                    "spatialReference": {"wkid": 5514},
                }),
                "geometryType": "esriGeometryPoint",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "id,kmenovecislo,poddelenicisla,druhpozemkukod,vymeraparcely",
                "outSR": out_sr, "f": "json", "returnGeometry": "true",
                "resultRecordCount": "1",
            }
            raw = json.loads(_ruian_get(f"{url}?" + urllib.parse.urlencode(params), timeout=15))
            feats = raw.get("features", [])
            if not feats:
                # Click landed on road / public space (not a registered parcel).
                # Fall back to envelope query in 10m × 10m box around the point
                # and return the parcel whose centroid is closest to the click.
                env_params = {
                    "geometry": json.dumps({
                        "xmin": sx - 10, "ymin": sy - 10,
                        "xmax": sx + 10, "ymax": sy + 10,
                        "spatialReference": {"wkid": 5514},
                    }),
                    "geometryType": "esriGeometryEnvelope",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "id,kmenovecislo,poddelenicisla,druhpozemkukod,vymeraparcely",
                    "outSR": out_sr, "f": "json", "returnGeometry": "true",
                    "resultRecordCount": "10",
                }
                raw = json.loads(_ruian_get(f"{url}?" + urllib.parse.urlencode(env_params), timeout=15))
                feats = raw.get("features", [])
                if not feats:
                    # Even the 10m envelope had nothing. Probably outside village.
                    if wgs_mode:
                        self._send_json(200, {"found": False})
                        return
                    self.send_error(404, "No parcel near this point")
                    return
                # Pick the parcel whose ring centroid is closest to (sx, sy).
                def centroid_dist2(feat):
                    rings = feat.get("geometry", {}).get("rings", [])
                    if not rings: return float('inf')
                    ring = rings[0]
                    if len(ring) < 3: return float('inf')
                    cx_p = sum(p[0] for p in ring) / len(ring)
                    cy_p = sum(p[1] for p in ring) / len(ring)
                    return (cx_p - sx) ** 2 + (cy_p - sy) ** 2
                feats.sort(key=centroid_dist2)
                feats = feats[:1]    # keep only the closest
            f = feats[0]
            attrs = f.get("attributes", {})
            rings = f["geometry"].get("rings", []) if f.get("geometry") else []
            if not rings:
                if wgs_mode:
                    self._send_json(200, {"found": False})
                    return
                self.send_error(404, "Parcel has no geometry")
                return
            outer = rings[0]
            if wgs_mode:
                # Rings already in WGS (outSR=4326): x=lon, y=lat. Light
                # response for the map3d viewer — no DSM TIFF sampling.
                kmen = attrs.get("kmenovecislo")
                podd = attrs.get("poddelenicisla")
                label = f"{kmen}/{podd}" if podd else (str(kmen) if kmen else "—")
                use_code = attrs.get("druhpozemkukod")
                self._send_json(200, {
                    "found": True,
                    "id": str(attrs.get("id", "")),
                    "label": label,
                    "use_code": use_code,
                    "use_label": DRUH_POZEMKU_LABELS.get(use_code, "—"),
                    "area_m2": int(float(attrs.get("vymeraparcely") or 0)),
                    "ring_wgs": [[round(p[0], 7), round(p[1], 7)] for p in outer],
                })
                return
            # Open every cached DSM TIFF once for per-vertex Y sample.
            tifs = []
            for d in sorted(Path("cache").glob("dmpok_tiff_*")):
                for tif in d.glob("*.tif"):
                    try:
                        tifs.append(rasterio.open(tif))
                    except Exception:
                        pass

            def sample_y(vx, vy):
                for src in tifs:
                    b = src.bounds
                    if b.left <= vx <= b.right and b.bottom <= vy <= b.top:
                        try:
                            val = next(src.sample([(vx, vy)]))[0]
                            if val > 0:
                                return round(float(val), 2)
                        except Exception:
                            continue
                return 0.0

            try:
                ring_local = []
                for vx, vy in outer:
                    ty = sample_y(vx, vy)
                    ring_local.append([
                        round(vx - gcx, 2),
                        round(-(vy - gcy), 2),
                        ty,
                    ])
            finally:
                for src in tifs:
                    try: src.close()
                    except Exception: pass

            kmen = attrs.get("kmenovecislo")
            podd = attrs.get("poddelenicisla")
            label = f"{kmen}/{podd}" if podd else (str(kmen) if kmen else "—")
            use_code = attrs.get("druhpozemkukod")
            parcel = {
                "id": str(attrs.get("id", "")),
                "label": label,
                "use_code": use_code,
                "use_label": DRUH_POZEMKU_LABELS.get(use_code, "—"),
                "area_m2": int(float(attrs.get("vymeraparcely") or 0)),
                "ring_local": ring_local,
            }
        except Exception as e:
            import sys as _sys
            print(f"[parcel-at-point] failed: {e}", file=_sys.stderr)
            self.send_error(503, f"parcel-at-point: {e}")
            return

        body = json.dumps(parcel).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _api_building_detail(self):
        """Single-building inspector: RÚIAN attributes + DSM-derived height for a clicked point.

        Input:  /api/building-detail?lon=<lon>&lat=<lat>  (WGS84)
        Output: JSON with footprint (WGS+S-JTSK), RÚIAN attrs, height (if DSM TIFF cached).
        """
        import json
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        try:
            lon = float(query["lon"][0])
            lat = float(query["lat"][0])
        except (KeyError, ValueError, IndexError):
            self.send_error(400, "Required params: lon, lat (WGS84)")
            return

        from pyproj import Transformer
        to_sjtsk = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
        to_wgs = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
        sx, sy = to_sjtsk.transform(lon, lat)

        # RÚIAN ArcGIS query around the click point.
        R = 30  # 30m bbox is generous for one-building selection
        ruian_url = "https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/3/query"
        try:
            raw = json.loads(_ruian_get(
                f"{ruian_url}?" + urllib.parse.urlencode({
                    "geometry": json.dumps({
                        "xmin": sx - R, "ymin": sy - R,
                        "xmax": sx + R, "ymax": sy + R,
                        "spatialReference": {"wkid": 5514},
                    }),
                    "geometryType": "esriGeometryEnvelope",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "kod,cisladomovni,zpusobvyuzitikod,zastavenaplocha",
                    "outSR": "5514", "f": "json", "returnGeometry": "true",
                    "resultRecordCount": "20",
                }),
                timeout=30,
            ))
        except Exception as e:
            self.send_error(502, f"RÚIAN failed: {e}")
            return

        # Find building footprint containing (or nearest to) the click point.
        from shapely.geometry import Polygon, Point, mapping
        click = Point(sx, sy)
        best = None
        best_dist = float("inf")
        for f in raw.get("features", []):
            try:
                rings = f["geometry"]["rings"][0]
                poly = Polygon(rings)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                d = 0.0 if poly.contains(click) else poly.distance(click)
                if d < best_dist:
                    best_dist = d
                    best = (f, poly)
            except Exception:
                continue
        if best is None or best_dist > 5.0:  # accept up to 5m off-edge
            self._send_json(200, {"found": False, "reason": "Žádná budova v okolí 5 m"})
            return
        feat, poly = best
        a = feat["attributes"]

        # DSM-derived height: search cache for a TIFF whose bbox covers the polygon.
        # Roof = max DSM inside footprint; ground = p10 of a 1-8m ring outside the
        # footprint with all RÚIAN neighbour polygons subtracted (DSM contains
        # buildings, so sampling INSIDE the footprint catches roof, not terrain).
        import rasterio, rasterio.mask, rasterio.windows, numpy as np
        from shapely.ops import unary_union
        from pathlib import Path
        height_m = ground_z = roof_z = None
        typical_height_m = None
        mesh_data = None
        roof_data = None
        volume_m3 = None
        terrain_slope_pct = None
        align_offset = None  # (dx, dy) m if cadastre and DSM disagree
        cx_p, cy_p = poly.centroid.x, poly.centroid.y

        # Neighbour polygons from the same ArcGIS response (drop self).
        neighbours = []
        for nf in raw.get("features", []):
            if nf["attributes"].get("kod") == a.get("kod"):
                continue
            try:
                np_poly = Polygon(nf["geometry"]["rings"][0])
                if not np_poly.is_valid:
                    np_poly = np_poly.buffer(0)
                if np_poly.is_valid and not np_poly.is_empty:
                    neighbours.append(np_poly)
            except Exception:
                continue
        # `ring` is rebuilt inside the rasterio loop once we know the aligned
        # polygon — leave it None here so we always use the right reference.
        ring = None

        for d in sorted(Path("cache").glob("dmpok_tiff_*")):
            for tif in d.glob("*.tif"):
                try:
                    with rasterio.open(tif) as src:
                        b = src.bounds
                        if not (b.left <= cx_p <= b.right and b.bottom <= cy_p <= b.top):
                            continue
                        # Align cadastre polygon to the actual DSM building so
                        # height/volume reflect the real structure when RÚIAN
                        # and DSM disagree (typical for shifted/rebuilt houses).
                        sample_poly, (dx, dy) = _align_polygon_to_dsm(
                            poly, src, neighbours)
                        if dx or dy:
                            align_offset = [dx, dy]
                        inside_arr, inside_transform = rasterio.mask.mask(
                            src, [mapping(sample_poly)], crop=True, nodata=0)
                        inside_band = inside_arr[0]
                        v_in = inside_band[inside_band > 0]
                        if len(v_in) < 10:
                            break
                        roof_z = float(v_in.max())

                        # Build ring around the aligned polygon, minus neighbours.
                        ring = sample_poly.buffer(8).difference(sample_poly.buffer(1))
                        if neighbours:
                            ring = ring.difference(unary_union(neighbours))

                        # Sample ground from ring (sans neighbours); fall back to
                        # min of inside if ring is empty (e.g. inner courtyard).
                        v_ring = np.array([])
                        ring_band = None
                        ring_transform = None
                        if not ring.is_empty:
                            ring_arr, ring_transform = rasterio.mask.mask(
                                src, [mapping(ring)], crop=True, nodata=0)
                            ring_band = ring_arr[0]
                            v_ring = ring_band[ring_band > 0]
                        if len(v_ring) >= 10:
                            # Trees in the ring inflate the terrain estimate.
                            # First-pass p10 gives an initial ground hint;
                            # drop ring pixels >2.5 m above it (vegetation /
                            # missed neighbour roof) and re-take p10.
                            initial = float(np.percentile(v_ring, 10))
                            v_ring_clean = v_ring[v_ring < initial + 2.5]
                            if len(v_ring_clean) >= 10:
                                ground_z = float(np.percentile(v_ring_clean, 10))
                            else:
                                ground_z = initial
                        else:
                            ground_z = float(v_in.min())

                        height_m = round(roof_z - ground_z, 1)

                        # Typical roof level — median of pixels that are
                        # actually built up (>1.5 m above ground). Robust
                        # against yard-scoped RÚIAN polygons that include
                        # courtyard ground and one tall barn / chimney.
                        v_above = v_in[v_in > ground_z + 1.5]
                        if len(v_above) >= 10:
                            typical_height_m = round(
                                float(np.median(v_above) - ground_z), 1)

                        # Volume = sum(dsm − ground) × pixel_area over inside pixels.
                        cell_area = float(src.res[0]) * float(src.res[1])
                        above = (v_in - ground_z).clip(min=0)
                        volume_m3 = round(float(above.sum() * cell_area), 0)

                        # Roof type: RANSAC planes on (x, y, z) of inside pixels.
                        try:
                            ys_in, xs_in = np.where(inside_band > 0)
                            if len(xs_in) >= 30:
                                wx_in = inside_transform.c + (xs_in + 0.5) * inside_transform.a
                                wy_in = inside_transform.f + (ys_in + 0.5) * inside_transform.e
                                in_pts = np.column_stack([
                                    wx_in - cx_p,
                                    wy_in - cy_p,
                                    inside_band[ys_in, xs_in].astype(float) - ground_z,
                                ])
                                roof_data = _detect_roof_type(in_pts)
                        except Exception as e:
                            print(f"[building-detail] roof detect failed: {e}")

                        # Terrain slope: fit a tilted plane to the lowest 70 % of
                        # ring pixels (filters out neighbour-roof / vegetation
                        # leaks above true terrain). slope_pct = |∇z| × 100.
                        if ring_band is not None and ring_transform is not None:
                            ys, xs = np.where(ring_band > 0)
                            if len(xs) >= 20:
                                z = ring_band[ys, xs].astype(float)
                                low_mask = z <= np.percentile(z, 70)
                                if low_mask.sum() >= 10:
                                    wx = ring_transform.c + (xs + 0.5) * ring_transform.a
                                    wy = ring_transform.f + (ys + 0.5) * ring_transform.e
                                    A = np.column_stack([
                                        wx[low_mask] - cx_p,
                                        wy[low_mask] - cy_p,
                                        np.ones(int(low_mask.sum())),
                                    ])
                                    coef, *_ = np.linalg.lstsq(A, z[low_mask], rcond=None)
                                    terrain_slope_pct = round(
                                        float(np.hypot(coef[0], coef[1])) * 100.0, 1)

                        roof_z = round(roof_z, 2)
                        ground_z = round(ground_z, 2)

                        # 3D mesh patch: DSM grid covering footprint + 5m buffer,
                        # elevations relative to ground_z, footprint outline in
                        # the same local frame (origin = footprint centroid,
                        # X=east, Z=south, Y=height-above-ground).
                        try:
                            # Patch covers cadastre poly + aligned building so
                            # both outlines are visible in the mesh.
                            patch_geom = poly.buffer(5).union(sample_poly.buffer(5))
                            bx_min, by_min, bx_max, by_max = patch_geom.bounds
                            win = rasterio.windows.from_bounds(
                                bx_min, by_min, bx_max, by_max, src.transform
                            ).round_offsets().round_lengths()
                            if win.width > 0 and win.height > 0:
                                patch = src.read(1, window=win)
                                wt = src.window_transform(win)
                                cell = float(wt.a)
                                rows, cols = patch.shape
                                # Elevations relative to ground; nodata → None.
                                nodata_val = src.nodata
                                elevs = []
                                for i in range(rows):
                                    row = []
                                    for j in range(cols):
                                        v = float(patch[i, j])
                                        if (nodata_val is not None and v == nodata_val) or v <= 0:
                                            row.append(None)
                                        else:
                                            row.append(round(v - ground_z, 2))
                                    elevs.append(row)
                                # Pixel-(0,0) center in local frame (centroid origin).
                                x0 = float(wt.c) - cx_p + 0.5 * cell
                                y_top = float(wt.f) + 0.5 * float(wt.e) - cy_p
                                z0 = -y_top  # SJTSK Y north-positive → three.js Z south-positive
                                fp_local = [
                                    [round(sxv - cx_p, 2), round(-(syv - cy_p), 2)]
                                    for sxv, syv in poly.exterior.coords
                                ]
                                aligned_fp_local = None
                                if align_offset is not None:
                                    aligned_fp_local = [
                                        [round(sxv - cx_p, 2), round(-(syv - cy_p), 2)]
                                        for sxv, syv in sample_poly.exterior.coords
                                    ]
                                # Outer SJTSK bbox of the patch — used by client
                                # to fetch a matching ortofoto texture from the
                                # /proxy/ortofoto endpoint.
                                ortho_bbox = [
                                    round(float(wt.c), 2),
                                    round(float(wt.f) - rows * cell, 2),
                                    round(float(wt.c) + cols * cell, 2),
                                    round(float(wt.f), 2),
                                ]
                                mesh_data = {
                                    "rows": rows, "cols": cols, "cell": cell,
                                    "x0": round(x0, 2), "z0": round(z0, 2),
                                    "elevations": elevs,
                                    "footprint_local": fp_local,
                                    "aligned_footprint_local": aligned_fp_local,
                                    "align_offset": align_offset,
                                    "ortho_bbox": ortho_bbox,
                                    "parcels": _fetch_parcels_local(
                                        poly, cx_p, cy_p),
                                }
                        except Exception as e:
                            print(f"[building-detail] mesh extract failed: {e}")
                        break
                except Exception:
                    continue
            if height_m is not None:
                break

        # Geometry stats — pure shapely, no DSM needed.
        import math
        mrr = poly.minimum_rotated_rectangle
        mrr_pts = list(mrr.exterior.coords)[:4]
        # MRR corners in the same local frame the mesh uses (centroid origin,
        # X=east, Z=south = -world_y). Frontend uses these for koty.
        mrr_local = [
            [round(px - cx_p, 2), round(-(py - cy_p), 2)] for px, py in mrr_pts
        ]
        e0 = math.hypot(mrr_pts[1][0] - mrr_pts[0][0], mrr_pts[1][1] - mrr_pts[0][1])
        e1 = math.hypot(mrr_pts[2][0] - mrr_pts[1][0], mrr_pts[2][1] - mrr_pts[1][1])
        bbox_length_m = round(max(e0, e1), 1)
        bbox_width_m = round(min(e0, e1), 1)
        # Azimuth of the longest edge from north (S-JTSK +Y), folded to 0–180.
        if e0 >= e1:
            edx = mrr_pts[1][0] - mrr_pts[0][0]
            edy = mrr_pts[1][1] - mrr_pts[0][1]
        else:
            edx = mrr_pts[2][0] - mrr_pts[1][0]
            edy = mrr_pts[2][1] - mrr_pts[1][1]
        azimuth_deg = round((math.degrees(math.atan2(edx, edy)) + 360) % 180, 0)
        # Compactness: 4πA / P²  (1 = circle, ≈0.785 = square, lower = elongated/jagged).
        compactness = round(4 * math.pi * poly.area / (poly.length ** 2), 2) if poly.length > 0 else None
        # Floors from typical height (more accurate for yard polygons); fall
        # back to peak height when typical isn't available.
        h_for_floors = typical_height_m if typical_height_m else height_m
        floors_est = max(1, round(h_for_floors / 2.8)) if h_for_floors else None
        nearest_neighbour_m = round(min(poly.distance(n) for n in neighbours), 1) if neighbours else None

        geom = {
            "floors_est": floors_est,
            "volume_m3": volume_m3,
            "bbox_length_m": bbox_length_m,
            "bbox_width_m": bbox_width_m,
            "azimuth_deg": azimuth_deg,
            "compactness": compactness,
            "neighbours_30m": len(neighbours),
            "nearest_neighbour_m": nearest_neighbour_m,
            "terrain_slope_pct": terrain_slope_pct,
            "mrr_local": mrr_local,
        }

        # Convert footprint S-JTSK → WGS for client display.
        rings_wgs = []
        for sx_, sy_ in feat["geometry"]["rings"][0]:
            lon_, lat_ = to_wgs.transform(sx_, sy_)
            rings_wgs.append([round(lon_, 6), round(lat_, 6)])

        out = {
            "found": True,
            "ruian_id": a.get("kod"),
            "cisladomovni": (a.get("cisladomovni") or None),
            "zpusob_vyuziti_kod": a.get("zpusobvyuzitikod"),
            "zpusob_vyuziti": _ZPUSOB_VYUZITI.get(a.get("zpusobvyuzitikod"), "neznámý"),
            "plocha_zastavena_m2": a.get("zastavenaplocha"),
            "plocha_polygon_m2": round(poly.area, 1),
            "footprint_wgs": rings_wgs,
            "vertex_count": len(rings_wgs),
            "height_m": height_m,
            "typical_height_m": typical_height_m,
            "ground_z_m": ground_z,
            "roof_z_m": roof_z,
            "dsm_source": "cache" if height_m is not None else None,
            "mesh": mesh_data,
            "geom": geom,
            "roof": roof_data,
        }
        self._send_json(200, out)

    def _api_roads(self):
        """Fetch OSM roads via Overpass API, return in local coords."""
        import json
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        try:
            gcx = float(query["gcx"][0])
            gcy = float(query["gcy"][0])
            radius = _clamp_radius(float(query.get("radius", ["600"])[0]))
        except (KeyError, ValueError, IndexError):
            self.send_error(400, "Required params: gcx, gcy (S-JTSK)")
            return

        cache_key = f"roads_{gcx:.0f}_{gcy:.0f}_{radius:.0f}"
        data = _ROADS_CACHE.get(cache_key)
        if data is None:
            # Convert center S-JTSK to WGS84 for Overpass query
            from pyproj import Transformer
            t = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
            t_back = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
            clon, clat = t.transform(gcx, gcy)

            # Overpass query — roads + paths
            overpass_q = f"""
            [out:json][timeout:30];
            (
              way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|service|living_street|track|path|footway)$"](around:{radius},{clat},{clon});
            );
            out body;>;out skel qt;
            """
            try:
                raw = _query_overpass(overpass_q, timeout=30)
            except Exception as e:
                self.send_error(502, f"Overpass query failed: {e}")
                return

            # Collect nodes
            nodes = {}
            for el in raw.get("elements", []):
                if el["type"] == "node":
                    nodes[el["id"]] = (el["lon"], el["lat"])

            # Build road segments
            road_widths = {
                "motorway": 8, "trunk": 7, "primary": 6, "secondary": 5.5,
                "tertiary": 5, "residential": 4, "unclassified": 3.5,
                "service": 3, "living_street": 3.5, "track": 2.5,
                "path": 1.5, "footway": 1.2,
            }
            roads = []
            for el in raw.get("elements", []):
                if el["type"] != "way":
                    continue
                hw = el.get("tags", {}).get("highway", "")
                name = el.get("tags", {}).get("name", "")
                width = road_widths.get(hw, 3)
                coords = []
                for nid in el.get("nodes", []):
                    if nid not in nodes:
                        continue
                    lon, lat = nodes[nid]
                    sx, sy = t_back.transform(lon, lat)
                    lx = sx - gcx
                    lz = -(sy - gcy)
                    coords.append([round(lx, 2), round(lz, 2)])
                if len(coords) >= 2:
                    roads.append({
                        "type": hw,
                        "name": name,
                        "width": width,
                        "coords": coords,
                    })

            data = json.dumps(roads).encode()
            _ROADS_CACHE.put(cache_key, data)
            print(f"Roads: {len(roads)} ways fetched")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _api_poi(self):
        """Fetch OSM POI/landmarks via Overpass, return in local coords."""
        import json
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        try:
            gcx = float(query["gcx"][0])
            gcy = float(query["gcy"][0])
            radius = _clamp_radius(float(query.get("radius", ["1000"])[0]))
        except (KeyError, ValueError, IndexError):
            self.send_error(400, "Required params: gcx, gcy (S-JTSK), optional radius")
            return

        cache_key = f"{gcx:.0f}_{gcy:.0f}_{radius:.0f}"
        data = _POI_CACHE.get(cache_key)
        if data is None:
            from pyproj import Transformer
            t = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
            t_back = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
            clon, clat = t.transform(gcx, gcy)

            # Broad query — let OpenAI do the curation downstream. This
            # gives the model enough raw signal to dedupe + rank without
            # us having to guess which tags matter in every village
            # context (e.g. a single hospoda in a hamlet IS relevant;
            # five in a tourist town are noise).
            overpass_q = f"""
            [out:json][timeout:30];
            (
              node["historic"](around:{radius},{clat},{clon});
              node["tourism"~"^(viewpoint|attraction|museum|hotel|information)$"](around:{radius},{clat},{clon});
              node["natural"="peak"](around:{radius},{clat},{clon});
              node["amenity"~"^(school|kindergarten|pharmacy|doctors|hospital|post_office|place_of_worship|townhall|community_centre|library|restaurant|cafe|pub|bank|fuel|police|fire_station)$"](around:{radius},{clat},{clon});
              node["leisure"~"^(swimming_pool|park|nature_reserve|garden|playground|sports_centre|pitch|fitness_centre)$"](around:{radius},{clat},{clon});
              node["shop"~"^(supermarket|bakery|department_store|convenience)$"](around:{radius},{clat},{clon});
              node["railway"~"^(station|halt|tram_stop)$"](around:{radius},{clat},{clon});
              way["historic"](around:{radius},{clat},{clon});
              way["amenity"~"^(school|kindergarten|hospital|place_of_worship|townhall|library|community_centre)$"](around:{radius},{clat},{clon});
              way["leisure"~"^(swimming_pool|park|nature_reserve|garden|playground|sports_centre|pitch)$"](around:{radius},{clat},{clon});
              way["shop"~"^(supermarket|department_store)$"](around:{radius},{clat},{clon});
            );
            out center tags;
            """
            try:
                raw = _query_overpass(overpass_q, timeout=30)
            except Exception as e:
                self.send_error(502, f"Overpass POI query failed (all mirrors): {e}")
                return

            pois = []
            for el in raw.get("elements", []):
                if el["type"] == "node":
                    lon, lat = el["lon"], el["lat"]
                elif "center" in el:
                    lon, lat = el["center"]["lon"], el["center"]["lat"]
                else:
                    continue
                sx, sy = t_back.transform(lon, lat)
                lx = sx - gcx
                lz = -(sy - gcy)
                tags = el.get("tags", {})
                # Decide category (broad bucket for UI grouping/icons) and
                # subtype (specific tag value for label). Order matters —
                # `historic` wins over `tourism` when both present.
                if tags.get("historic"):
                    category, subtype = "historic", tags["historic"]
                elif tags.get("tourism"):
                    category, subtype = "tourism", tags["tourism"]
                elif tags.get("natural") == "peak":
                    category, subtype = "peak", "peak"
                elif tags.get("amenity"):
                    category, subtype = "amenity", tags["amenity"]
                elif tags.get("leisure"):
                    category, subtype = "leisure", tags["leisure"]
                elif tags.get("shop"):
                    category, subtype = "shop", tags["shop"]
                elif tags.get("railway"):
                    category, subtype = "transit", tags["railway"]
                elif tags.get("highway") == "bus_stop":
                    category, subtype = "transit", "bus_stop"
                else:
                    category, subtype = "unknown", "unknown"
                name = tags.get("name") or tags.get("name:cs")
                if not name:
                    # Unnamed but tagged — fall back to a human-readable
                    # subtype label (e.g. "Hřiště" instead of "(bez názvu)"
                    # for an unnamed playground).
                    name = _POI_FALLBACK_LABELS.get(subtype, "(bez názvu)")
                pois.append({
                    "id": el["id"],
                    "name": name,
                    "category": category,
                    "type": subtype,
                    "coords": [round(lx, 1), round(lz, 1)],
                    "wikipedia": tags.get("wikipedia"),
                    "wikidata": tags.get("wikidata"),
                })

            # AI curation: trim + dedupe to the 12-15 most useful for a
            # real-estate buyer, attach short "why" text. Skipped when
            # there's no OPENAI_API_KEY (the endpoint stays useful in
            # raw mode) or when the raw list is already short.
            if os.environ.get("OPENAI_API_KEY") and len(pois) > 8:
                try:
                    pois = _curate_pois_with_ai(pois)
                except Exception as e:
                    import sys as _sys
                    print(f"[poi] AI curation failed, returning raw: {e}", file=_sys.stderr)
            data = json.dumps(pois).encode()
            _POI_CACHE.put(cache_key, data)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _api_wiki(self):
        """Wikipedia REST summary endpoint — proxy to cs.wikipedia.org."""
        import json
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        title = query.get("title", [""])[0]
        lang = query.get("lang", ["cs"])[0]
        if not title:
            self.send_error(400, "Required: title")
            return
        if lang not in ("cs", "en", "sk", "de"):
            lang = "cs"

        cache_key = f"{lang}|{title}"
        data = _WIKI_CACHE.get(cache_key)
        if data is None:
            url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "gtaol/1.0 (paraglide game)"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    data = json.dumps({"extract": "(článek nenalezen)", "missing": True}).encode()
                    _WIKI_CACHE.put(cache_key, data)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(502, f"Wiki fetch failed: {e}")
                return
            except Exception as e:
                self.send_error(502, f"Wiki fetch failed: {e}")
                return

            slim = {
                "title": raw.get("title"),
                "extract": raw.get("extract", "")[:600],
                "thumbnail": (raw.get("thumbnail") or {}).get("source"),
                "lang": lang,
            }
            data = json.dumps(slim).encode()
            _WIKI_CACHE.put(cache_key, data)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_glb_gzipped(self):
        """Serve GLB files with brotli (preferred) or gzip on the wire.
        Brotli typically beats gzip by 15-20% for already-Draco GLBs. We
        cache the compressed bytes on disk so we don't re-compress per req.
        """
        import gzip as gz
        import posixpath
        accept = self.headers.get("Accept-Encoding", "") or ""
        prefer_br = "br" in accept
        # Normalise the URL path and refuse anything that escapes DIRECTORY.
        # Without this, `/../../etc/file.glb` would be joined naively and
        # read+cache an arbitrary file outside the serving root.
        raw = self.path.split("?", 1)[0]
        norm = posixpath.normpath(raw).lstrip("/")
        file_path = os.path.realpath(os.path.join(DIRECTORY, norm))
        if not (file_path == DIRECTORY or file_path.startswith(DIRECTORY + os.sep)):
            self.send_error(403)
            return
        if not os.path.exists(file_path):
            self.send_error(404)
            return

        # Disk-cache the compressed payload keyed on file mtime + encoding +
        # quality level. Brotli quality 11 (max) compresses ~30% better than
        # q=6 on already-Draco GLBs (33.6 MB → 20 MB vs 28 MB at q=6) but
        # takes 10-20× longer to encode. We cache so the slow encode is a
        # one-time cost per Draco re-compress; subsequent requests serve the
        # cached blob directly.
        st = os.stat(file_path)
        BROTLI_Q = 11
        enc = "br" if prefer_br else "gzip"
        suffix = f"br{BROTLI_Q}" if enc == "br" else "gzip6"
        cache_path = f"{file_path}.{suffix}.{int(st.st_mtime)}"
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                compressed = f.read()
        else:
            with open(file_path, "rb") as f:
                data = f.read()
            if prefer_br:
                try:
                    import brotli
                    compressed = brotli.compress(data, quality=BROTLI_Q)
                except Exception:
                    enc = "gzip"
                    compressed = gz.compress(data, compresslevel=6)
            else:
                compressed = gz.compress(data, compresslevel=6)
            try:
                tmp = cache_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(compressed)
                os.replace(tmp, cache_path)
            except OSError:
                pass  # best-effort caching

        self.send_response(200)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(compressed)))
        self.send_header("Content-Encoding", enc)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(compressed)

    def _proxy_cadastre(self):
        """Fetch ČÚZK katastrální mapa WMS as transparent PNG overlay.

        Query params:
          BBOX  — S-JTSK envelope, "xmin,ymin,xmax,ymax" (required)
          layer — ČÚZK WMS layer name (default 'KN' = full cadastre with
                  parcel numbers; 'DKM' = boundaries only; see WMS caps for
                  more)
          size  — output PNG side in px (default 4096, max 8192). Numbers
                  start appearing at ≥ 1.5 px/m in the rendered bbox, so
                  inner (1 km) at 4096² = 4 px/m → very readable numbers;
                  closeup (3 km) at 4096² = 1.4 px/m → numbers legible.
          style — line styling: thin/normal/medium/thick (dilation+blur).
          bg    — 'transparent' (default, alpha-cuts white for orto overlay)
                  or 'white' (keep WMS white background, opaque PNG → hides
                  orto for clean cadastre-only view).
        """
        import numpy as np
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])

        bbox_str = query.get("BBOX", [""])[0]
        if not bbox_str:
            self.send_error(400, "Missing BBOX")
            return

        layer = query.get("layer", ["KN"])[0]
        if not layer.replace("_", "").replace(",", "").isalnum():
            self.send_error(400, "Invalid layer name")
            return
        try:
            size = min(8192, max(256, int(query.get("size", ["4096"])[0])))
        except ValueError:
            size = 4096
        style = query.get("style", ["normal"])[0]
        bg = query.get("bg", ["transparent"])[0]
        # SRS: default S-JTSK (heightfield viewer sends 5514 bboxes). map3d
        # sends its Web-Mercator tile bbox as EPSG:3857 so the PNG drapes
        # pixel-aligned over the ortho with no reprojection. ČÚZK WMS renders
        # both natively. Whitelist to keep it out of the URL as an injection.
        srs = query.get("SRS", query.get("srs", ["EPSG:5514"]))[0]
        if srs not in ("EPSG:5514", "EPSG:3857", "EPSG:4326"):
            self.send_error(400, "Invalid SRS")
            return

        # Disk cache (mirrors cache/osm): map3d fetches one live WMS PNG per
        # z>=16 tile — browser max-age only helps one device for a day; every
        # new session/device re-paid a 200-1000 ms ČÚZK round trip + numpy
        # alpha-key + PNG encode. Key = the full render recipe.
        import hashlib
        cad_key = hashlib.sha256(
            f"{layer}|{srs}|{bbox_str}|{size}|{style}|{bg}".encode()
        ).hexdigest()[:16]
        cad_cache_dir = Path("cache/cadastre")
        cad_cache_dir.mkdir(parents=True, exist_ok=True)
        cad_cache_path = cad_cache_dir / f"kn_{cad_key}.png"
        if cad_cache_path.exists():
            data_bytes = cad_cache_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cadastre-Cache", "hit")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data_bytes)
            return
        # ČÚZK WMS caps each request at 4096² ("Size of requested map is
        # larger then MaxClientSize"). For larger outputs we fetch a k×k grid
        # of sub-tiles at 4096² each and stitch. k = ceil(size/4096) gives
        # 1 for ≤4096, 2 for 4097-8192, 3 for 8193-12288, etc. Sub-tile size
        # rounds down so the stitched result lands at-or-below the requested
        # size; we resize up slightly to hit `size` exactly.
        from PIL import Image
        from io import BytesIO
        WMS_MAX = 4096
        k = max(1, (size + WMS_MAX - 1) // WMS_MAX)
        sub_size = (size + k - 1) // k     # px per sub-tile
        try:
            xmin, ymin, xmax, ymax = (float(v) for v in bbox_str.split(","))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid BBOX")
            return
        bbox_w = (xmax - xmin) / k
        bbox_h = (ymax - ymin) / k

        try:
            stitched = Image.new("RGBA", (k * sub_size, k * sub_size), (0, 0, 0, 0))
            # Tile (col, row) is at WMS BBOX (xmin+col*w, ymax-(row+1)*h,
            # xmin+(col+1)*w, ymax-row*h). Row 0 = north band.
            for row in range(k):
                sub_ymax = ymax - row * bbox_h
                sub_ymin = sub_ymax - bbox_h
                for col in range(k):
                    sub_xmin = xmin + col * bbox_w
                    sub_xmax = sub_xmin + bbox_w
                    sub_bbox = f"{sub_xmin},{sub_ymin},{sub_xmax},{sub_ymax}"
                    sub_url = (
                        f"https://services.cuzk.cz/wms/wms.asp?"
                        f"SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
                        f"&LAYERS={layer}&SRS={srs}&BBOX={sub_bbox}"
                        f"&WIDTH={sub_size}&HEIGHT={sub_size}"
                        f"&FORMAT=image/png&TRANSPARENT=TRUE&STYLES="
                    )
                    sub_req = urllib.request.Request(
                        sub_url, headers={"User-Agent": "Mozilla/5.0"})
                    # Per-tile retry: ČÚZK occasionally drops the first
                    # connection (Errno 61). 3 attempts × short backoff
                    # absorbs the common case without making 4-tile fetches
                    # slow when ČÚZK is healthy.
                    sub_bytes = None
                    last_err = None
                    for tile_attempt in range(3):
                        try:
                            # _CUZK_SEM: same courtesy cap as the ortofoto
                            # tile path — a katastr toggle on a wide view
                            # used to fire an unbounded burst at ČÚZK.
                            with _CUZK_SEM, urllib.request.urlopen(
                                    sub_req, timeout=30) as resp:
                                sub_bytes = resp.read()
                            break
                        except (urllib.error.URLError, ConnectionError,
                                TimeoutError, OSError) as e:
                            last_err = e
                            if tile_attempt < 2:
                                import time as _t
                                _t.sleep(1 + tile_attempt)
                    if sub_bytes is None:
                        raise RuntimeError(
                            f"tile ({col},{row}): {last_err}")
                    sub_img = Image.open(BytesIO(sub_bytes)).convert("RGBA")
                    stitched.paste(sub_img, (col * sub_size, row * sub_size))
            if stitched.size != (size, size):
                stitched = stitched.resize((size, size), Image.LANCZOS)
            arr = np.array(stitched)

            if bg == "white":
                # Keep WMS output as-is (white background opaque). Used when
                # cadastre is meant to fully cover the orto for a clean read.
                pass
            else:
                # Make white/light background transparent, keep already-transparent pixels
                orig_alpha = arr[:, :, 3].copy()
                # int32 — (250 - lightness) * 255 overflows int16 (max 32767 < 63750)
                lightness = np.minimum(arr[:,:,0], np.minimum(arr[:,:,1], arr[:,:,2])).astype(np.int32)
                bg_alpha = np.clip((250 - lightness) * 255 // 50, 0, 255).astype(np.uint8)
                arr[:, :, 3] = np.minimum(orig_alpha, bg_alpha)

            if style in ("medium", "thick"):
                dilation_iter = 2 if style == "medium" else 3
                blur_radius = 0.8 if style == "medium" else 1.0

            if style in ("medium", "thick"):
                # Dilate lines (make thicker) + gaussian blur for antialiasing
                from PIL import ImageFilter
                # Extract non-transparent as mask
                mask = arr[:, :, 3] > 0
                # Dilate mask
                from scipy.ndimage import binary_dilation
                dilated = binary_dilation(mask, iterations=dilation_iter)
                # Apply dilated mask with original colors
                for ch in range(3):
                    channel = arr[:, :, ch].copy()
                    # Fill dilated area with nearest non-zero color
                    from scipy.ndimage import maximum_filter
                    channel_filled = maximum_filter(channel, size=7)
                    arr[:, :, ch] = np.where(dilated & ~mask, channel_filled, arr[:, :, ch])
                arr[:, :, 3] = np.where(dilated, 220, 0).astype(np.uint8)

                # Slight blur for antialiasing
                result = Image.fromarray(arr)
                alpha = result.split()[3]
                alpha = alpha.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                result.putalpha(alpha)
            else:
                result = Image.fromarray(arr)

            # WMS already returned at the requested `size`; no downscale.
            buf = BytesIO()
            result.save(buf, "PNG")
            data_bytes = buf.getvalue()

            # Atomic cache write (same pattern as manifests / orto_render).
            tmp = cad_cache_path.with_suffix(".png.tmp")
            tmp.write_bytes(data_bytes)
            tmp.replace(cad_cache_path)

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Cadastre-Cache", "miss")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data_bytes)
        except Exception as e:
            self.send_error(502, f"Cadastre WMS error: {e}")

    def _proxy_osm(self):
        """Fetch OSM raster reprojected from EPSG:3857 to EPSG:5514.

        Query params:
          BBOX — S-JTSK envelope, "xmin,ymin,xmax,ymax" (required)
          size — output PNG side in px (default 2048, max 4096)

        OpenStreetMap tile servers serve Web Mercator (EPSG:3857). Our
        heightfield ortho/cadastre live in S-JTSK (EPSG:5514). At 5 km
        extents the angular difference is enough (~5° rotation) that
        nearest-neighbour stamping would offset features by hundreds of
        metres. We do proper per-pixel reprojection with pyproj + numpy
        bilinear so OSM features line up with the cadastre / hillshade.

        Cached on disk under cache/osm/ (BBOX+size key). Cache key is
        truncated to 0.1 m which is far below source resolution.
        """
        import math
        import numpy as np
        from PIL import Image
        from io import BytesIO
        from pyproj import Transformer

        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        bbox_str = query.get("BBOX", [""])[0]
        if not bbox_str:
            self.send_error(400, "Missing BBOX")
            return
        try:
            xmin, ymin, xmax, ymax = (float(v) for v in bbox_str.split(","))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid BBOX")
            return
        try:
            size = min(4096, max(256, int(query.get("size", ["2048"])[0])))
        except ValueError:
            size = 2048

        cache_dir = Path("cache/osm")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / (
            f"{xmin:.1f}_{ymin:.1f}_{xmax:.1f}_{ymax:.1f}_{size}.png")
        if cache_path.is_file():
            data = cache_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            self.wfile.write(data)
            return

        to_wgs = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
        cx_grid = [xmin, xmin, xmax, xmax]
        cy_grid = [ymin, ymax, ymin, ymax]
        lons, lats = to_wgs.transform(cx_grid, cy_grid)
        lon_min, lon_max = min(lons), max(lons)
        lat_min, lat_max = min(lats), max(lats)
        lat_mid = (lat_min + lat_max) / 2

        # Pick zoom: source px/m should match target px/m. Mercator pixel size
        # at zoom z: 2π·R·cos(lat) / (256·2^z), R=6378137. Each +1 quadruples
        # tile count → cap z and let bilinear handle the rest. z=18 is OSM's
        # highest street-level zoom with full label/icon detail (building
        # entrances, house numbers); going beyond just upscales.
        extent_m = max(xmax - xmin, ymax - ymin)
        target_res = extent_m / size
        R = 6378137.0
        Z_MAX = 18
        z = 10
        while z < Z_MAX:
            px_m = (2 * math.pi * R * math.cos(math.radians(lat_mid))) / (256 * (1 << (z + 1)))
            if px_m <= target_res:
                break
            z += 1
        z = max(10, min(Z_MAX, z))

        def lonlat_to_tile(lon, lat, zoom):
            n = 1 << zoom
            x = (lon + 180.0) / 360.0 * n
            lat_rad = math.radians(lat)
            y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
            return x, y

        # Determine integer tile range. SW lower-left, NE upper-right.
        tx_sw, ty_sw = lonlat_to_tile(lon_min, lat_min, z)
        tx_ne, ty_ne = lonlat_to_tile(lon_max, lat_max, z)
        tx_min = int(math.floor(tx_sw))
        tx_max = int(math.floor(tx_ne))
        ty_min = int(math.floor(ty_ne))   # ty grows southward
        ty_max = int(math.floor(ty_sw))
        nx = tx_max - tx_min + 1
        ny = ty_max - ty_min + 1
        if nx * ny > 600:
            # Safety guard. At z=18 a 5 km outer ring lands ≤ 33×33 ≈ 1089
            # tiles, so we wouldn't pick z=18 for outer (loop breaks earlier
            # when source resolution already meets target). 600 covers
            # closeup/inner at full z=18; anything past that = misuse.
            self.send_error(400, f"OSM tile range too large ({nx}×{ny}, z={z})")
            return

        canvas = Image.new("RGB", (nx * 256, ny * 256), (240, 240, 230))
        ua = "inzerator-heightfield/1.0 (https://github.com/alacremex/inzerator)"

        def fetch_one(ix, iy):
            tx = tx_min + ix
            ty = ty_min + iy
            url = f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png"
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            last_err = None
            for attempt in range(3):
                with _OSM_SEM:
                    try:
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            return ix, iy, resp.read()
                    except (urllib.error.URLError, ConnectionError,
                            TimeoutError, OSError) as e:
                        last_err = e
                if attempt < 2:
                    import time as _t
                    _t.sleep(1 + attempt)
            raise RuntimeError(f"tile {z}/{tx}/{ty}: {last_err}")

        # tile.openstreetmap.org's usage policy allows modest concurrency;
        # 4 worker threads cut wall time ~3× on outer rings (~80 tiles), but
        # the global _OSM_SEM(2) further caps in-flight requests across all
        # concurrent server handlers to stay under OSM TOS.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(fetch_one, ix, iy)
                           for iy in range(ny) for ix in range(nx)]
                for f in as_completed(futures):
                    ix, iy, tile_bytes = f.result()
                    tile = Image.open(BytesIO(tile_bytes)).convert("RGB")
                    canvas.paste(tile, (ix * 256, iy * 256))
        except Exception as e:
            self.send_error(502, f"OSM tile fetch failed: {e}")
            return

        # Build target grid in S-JTSK (Y inverted: image row 0 = north).
        xs = np.linspace(xmin, xmax, size, dtype=np.float64)
        ys = np.linspace(ymax, ymin, size, dtype=np.float64)
        X, Y = np.meshgrid(xs, ys)
        lons_out, lats_out = to_wgs.transform(X.ravel(), Y.ravel())
        lons_out = lons_out.reshape(size, size)
        lats_out = lats_out.reshape(size, size)

        # Mercator fractional tile coords → canvas pixel coords.
        n_tiles = 1 << z
        px_x = (lons_out + 180.0) / 360.0 * n_tiles
        lat_rad = np.radians(lats_out)
        px_y = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / np.pi) / 2.0 * n_tiles
        cpx = (px_x - tx_min) * 256
        cpy = (px_y - ty_min) * 256

        src = np.array(canvas, dtype=np.float32)
        H, W, _ = src.shape
        x0 = np.clip(np.floor(cpx).astype(np.int32), 0, W - 2)
        y0 = np.clip(np.floor(cpy).astype(np.int32), 0, H - 2)
        fx = (cpx - x0).astype(np.float32)[..., None]
        fy = (cpy - y0).astype(np.float32)[..., None]
        p00 = src[y0,   x0]
        p01 = src[y0,   x0 + 1]
        p10 = src[y0 + 1, x0]
        p11 = src[y0 + 1, x0 + 1]
        out = (p00 * (1 - fx) * (1 - fy) + p01 * fx * (1 - fy)
               + p10 * (1 - fx) * fy     + p11 * fx * fy)
        out_img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")

        buf = BytesIO()
        out_img.save(buf, "PNG", optimize=True)
        data_bytes = buf.getvalue()
        # Atomic write — concurrent requests on the same BBOX won't observe
        # a half-written file (PIL would explode on the partial PNG).
        try:
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            tmp_path.write_bytes(data_bytes)
            os.replace(tmp_path, cache_path)
        except OSError as e:
            print(f"[osm] cache write failed: {e}")

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data_bytes)

    def _api_sjtsk_to_wgs(self):
        """Convert EPSG:5514 (Křovák) → EPSG:4326 (lat/lon). Used by the
        heightfield viewer to compute sun position from time-of-day at the
        ring centre. pyproj does the heavy lifting; we just reach through.
        """
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        try:
            cx = float(query.get("cx", ["0"])[0])
            cy = float(query.get("cy", ["0"])[0])
        except ValueError:
            self.send_error(400, "Bad cx/cy")
            return
        try:
            import locations as _loc
            lat, lon = _loc._sjtsk_to_wgs(cx, cy)
        except Exception as e:
            self.send_error(500, f"projection error: {e}")
            return
        self._send_json(200, {"lat": lat, "lon": lon})

    # ------------------------------------------------------------------
    # POST /api/image-edit — OpenAI gpt-image-1 proxy
    # ------------------------------------------------------------------

    def _api_image_edit_prompt(self):
        """Return the server-side prompt as plain text so the UI can show it
        read-only. The endpoint is intentionally unauthenticated — the prompt
        itself isn't a secret, only the OpenAI key is."""
        body = _IMAGE_EDIT_PROMPT.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _api_image_edit_status(self):
        """Report whether the image-edit endpoint is configured. UI uses this
        to enable/disable the upload button and show a friendly hint when
        OPENAI_API_KEY is missing from env."""
        ready = bool(os.environ.get("OPENAI_API_KEY"))
        self._send_json(200, {
            "ready": ready,
            "openai_key_set": ready,
            "prompt_loaded": bool(_IMAGE_EDIT_PROMPT),
        })

    def _api_image_edit(self):
        """Edit an oblique aerial 3D mesh render via OpenAI gpt-image-2.

        Body: multipart with `image_a` (required, target), `refs[]` (optional,
        up to 5 imperfect reference images), `size` (auto / WxH where both
        dims are multiples of 16 and longest edge ≤ 3840), `quality`
        (low/medium/high; default high). Prompt is held server-side in
        `_IMAGE_EDIT_PROMPT` — clients can't override it.

        Auth: none. The endpoint is open to anyone who can reach the server.
        Set INZERATOR_API_TOKEN env + re-enable the header check below if
        you ever expose this beyond a trusted LAN — otherwise random clients
        in the network can spend your OPENAI_API_KEY credit.

        Result: PNG bytes of the edited image. Also writes input + output +
        metadata to `cache/image_edit/<timestamp>/` for audit and rerun.
        """
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self._send_json(503, {"error": "OPENAI_API_KEY not set on server"})
            return

        # Parse multipart. cgi.FieldStorage is deprecated in 3.13 but still
        # ships, and works for our modest payload (single image_a + a few
        # refs, ~5-20 MB total). Switch to email.parser-based parsing if it
        # ever gets removed.
        import cgi
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send_json(400, {"error": "expected multipart/form-data"})
            return
        try:
            fs = cgi.FieldStorage(
                fp=self.rfile, headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
                keep_blank_values=True,
            )
        except Exception as e:
            self._send_json(400, {"error": f"multipart parse failed: {e}"})
            return

        if "image_a" not in fs:
            self._send_json(400, {"error": "field image_a is required"})
            return
        image_a = fs["image_a"]
        if not getattr(image_a, "file", None):
            self._send_json(400, {"error": "image_a must be a file upload"})
            return
        image_a_bytes = image_a.file.read()
        if not image_a_bytes:
            self._send_json(400, {"error": "image_a is empty"})
            return

        # Optional references — accept either `refs` (single) or `refs[]`
        # (browser convention); cgi exposes them as a list.
        refs = []
        ref_field = fs["refs"] if "refs" in fs else None
        if ref_field is not None:
            ref_items = ref_field if isinstance(ref_field, list) else [ref_field]
            for r in ref_items:
                if getattr(r, "file", None):
                    rb = r.file.read()
                    if rb:
                        refs.append(rb)
                if len(refs) >= 5:
                    break

        size = fs.getvalue("size", "auto")
        quality = fs.getvalue("quality", "high")
        # gpt-image-2 accepts flexible sizes: both dimensions multiples of 16,
        # longest edge ≤ 3840, minimum total pixel budget (≈512²). We let the
        # OpenAI API do the final validation but pre-check the obvious shape
        # to fail fast with a clearer message.
        if size != "auto":
            import re
            m = re.match(r"^(\d{3,4})x(\d{3,4})$", size)
            if not m:
                self._send_json(400, {"error": f"size must be 'auto' or WIDTHxHEIGHT (e.g. 2048x2048), got {size!r}"})
                return
            w, h = int(m.group(1)), int(m.group(2))
            if w % 16 or h % 16:
                self._send_json(400, {"error": f"size {size}: both dimensions must be divisible by 16"})
                return
            if max(w, h) > 3840:
                self._send_json(400, {"error": f"size {size}: longest edge must be ≤ 3840 for gpt-image-2"})
                return
            if w < 512 or h < 512:
                self._send_json(400, {"error": f"size {size}: each dimension must be ≥ 512"})
                return
        if quality not in ("low", "medium", "high", "auto"):
            self._send_json(400, {"error": f"invalid quality: {quality}"})
            return

        # Build multipart for OpenAI.
        import uuid, mimetypes
        boundary = f"----InzeratorBoundary{uuid.uuid4().hex}"
        parts = []
        def add_field(name, value):
            parts.append(f'--{boundary}\r\n'
                         f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                         f'{value}\r\n'.encode())
        def add_file(name, filename, content, ctype="image/png"):
            parts.append(f'--{boundary}\r\n'
                         f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                         f'Content-Type: {ctype}\r\n\r\n'.encode())
            parts.append(content)
            parts.append(b'\r\n')

        # gpt-image-2 (released 2026-04-21) — flexible sizes up to 3840 px on
        # the longest edge (vs gpt-image-1's 1536 max). Same prompt format,
        # same /v1/images/edits endpoint. Older gpt-image-1 still works if
        # you swap the model id back.
        add_field("model", "gpt-image-2")
        add_field("prompt", _IMAGE_EDIT_PROMPT)
        add_field("size", size)
        add_field("quality", quality)
        # gpt-image-2 always returns b64_json on /edits (the parameter was
        # removed entirely — passing it now triggers 'Unknown parameter:
        # response_format'). For gpt-image-1 it was explicit; for v2 the
        # default is the only option.
        # First image is the target. Subsequent images are references.
        # gpt-image-1 accepts `image[]` array form.
        add_file("image[]", "image_a.png", image_a_bytes)
        for i, rb in enumerate(refs):
            add_file("image[]", f"ref_{i}.png", rb)
        parts.append(f'--{boundary}--\r\n'.encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            "https://api.openai.com/v1/images/edits",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        # gpt-image-2 high quality at 2048×1376 routinely takes 60-180 s,
        # but during OpenAI capacity spikes can exceed 300 s. We let it
        # run up to 10 minutes — well past anything reasonable — to avoid
        # spurious 500s on the client side. The client's progress text
        # ("20-60 s") is just a hint, not a contract.
        import socket   # for socket.timeout — pre-3.10 isn't aliased to TimeoutError
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp_body = resp.read()
        except urllib.error.HTTPError as e:
            import sys as _sys
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"[image-edit] OpenAI {e.code}: {err_body}", file=_sys.stderr)
            self._send_json(e.code, {"error": f"OpenAI {e.code}", "details": err_body})
            return
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            # On 3.9 socket.timeout is its own class (subclass of OSError),
            # not aliased to TimeoutError — without it the read-timeout
            # case fell through to do_POST's generic 500 handler.
            import sys as _sys
            print(f"[image-edit] OpenAI request timed out / failed: {e}", file=_sys.stderr)
            self._send_json(504, {"error": f"OpenAI request failed (timeout or network): {e}"})
            return
        except OSError as e:
            # Connection reset / SSL errors / similar transport issues —
            # all surface as OSError subclasses on 3.9. Map to 502 with
            # an explicit message so the client doesn't see "500 internal".
            import sys as _sys
            print(f"[image-edit] transport error: {e}", file=_sys.stderr)
            self._send_json(502, {"error": f"OpenAI transport error: {e}"})
            return

        # Decode b64 PNG from response.
        try:
            payload = json.loads(resp_body)
            import base64
            out_png = base64.b64decode(payload["data"][0]["b64_json"])
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
            self._send_json(502, {"error": f"OpenAI response unparseable: {e}"})
            return

        # Persist for audit. Atomic write via tmp.
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        run_dir = Path("cache/image_edit") / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "image_a.png").write_bytes(image_a_bytes)
        for i, rb in enumerate(refs):
            (run_dir / f"ref_{i}.png").write_bytes(rb)
        (run_dir / "output.png").write_bytes(out_png)
        (run_dir / "meta.json").write_text(json.dumps({
            "ts": ts, "size": size, "quality": quality,
            "image_a_bytes": len(image_a_bytes),
            "ref_count": len(refs),
            "output_bytes": len(out_png),
        }, indent=2))
        print(f"[image-edit] {ts}: {len(refs)} ref(s), {len(image_a_bytes)} in, {len(out_png)} out")

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(out_png)))
        self.send_header("X-Run-Id", ts)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(out_png)

    # ------------------------------------------------------------------
    # Helpers shared by GET and POST handlers
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    # ------------------------------------------------------------------
    # POST /api/* (Lokace UI)
    # ------------------------------------------------------------------

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/jobs":
                self._handle_post_jobs()
            elif path.startswith("/api/jobs/") and path.endswith("/retry"):
                self._handle_post_jobs_retry(path[len("/api/jobs/"):-len("/retry")])
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                self._handle_post_jobs_cancel(path[len("/api/jobs/"):-len("/cancel")])
            elif path == "/api/image-edit":
                self._api_image_edit()
            else:
                self.send_error(404, "POST endpoint not found")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": "internal server error"})

    def _handle_post_jobs(self):
        body = self._read_json_body()
        slug = body.get("slug", "").strip()
        label = body.get("label", slug).strip() or slug

        # Resume-from-disk: cx/cy + label recovered from on-disk metadata.
        # Used when the original job_id was lost (server restart) and the
        # caller wants to relaunch the pipeline for an existing partial
        # location. Lookup order:
        #   1. tiles_v2_<slug>/location.json (current persistence path,
        #      written by locations._persist_location_meta at enqueue).
        #   2. tiles_v2_<slug>/heightfield/manifest.json (re-gen of an
        #      already-encoded location).
        #   3. tiles_v2_<slug>/manifest.json (legacy v2 manifest — pre-
        #      retirement lokace whose top-manifest is still on disk).
        if body.get("resume_from_disk"):
            base = Path(f"tiles_v2_{slug}")
            cx = cy = None
            resume_label = None
            sources = [
                (base / "location.json",
                 lambda m: (m.get("cx"), m.get("cy"), m.get("label"))),
                (base / "heightfield" / "manifest.json",
                 lambda m: (m.get("cx"), m.get("cy"), None)),
                (base / "manifest.json",
                 lambda m: (
                     (m.get("region") or {}).get("center_sjtsk", [None, None])[0],
                     (m.get("region") or {}).get("center_sjtsk", [None, None])[1],
                     (m.get("region") or {}).get("label"),
                 )),
            ]
            for path, extract in sources:
                if not path.is_file():
                    continue
                try:
                    m = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                _cx, _cy, _lbl = extract(m)
                if _cx is not None and _cy is not None:
                    cx, cy = _cx, _cy
                    if _lbl:
                        resume_label = _lbl
                    break
            if cx is None or cy is None:
                self._send_json(400, {
                    "error": "no recoverable centre on disk — partial "
                             "location must have location.json or a heightfield "
                             "or legacy v2 manifest"})
                return
            if resume_label:
                label = resume_label
        else:
            cx = body.get("cx")
            cy = body.get("cy")

        if not slug or cx is None or cy is None:
            self._send_json(400, {"error": "required fields: slug, cx, cy (or resume_from_disk=true)"})
            return
        if not locations.is_valid_slug(slug):
            self._send_json(400, {"error": "slug must match ^[a-z0-9-]+$"})
            return
        try:
            cx = float(cx); cy = float(cy)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "cx, cy must be numbers"})
            return
        force_recompress = bool(body.get("force_recompress"))
        status = locations.location_status(slug)
        if status == "ready" and not force_recompress:
            self._send_json(409, {
                "error": "slug already exists and is ready",
                "slug": slug,
                "suggestion": locations.next_free_slug(slug,
                    set(loc["slug"] for loc in locations.list_locations())),
            })
            return
        if force_recompress:
            # Drop .compress_ok so the worker's resume-skip doesn't bail on
            # the compress step. Other sentinels stay → only compress reruns.
            sentinel = locations.expected_glb(slug, "compress")
            if sentinel.exists():
                try:
                    sentinel.unlink()
                except OSError as e:
                    self._send_json(500, {"error": f"could not drop .compress_ok: {e}"})
                    return
        try:
            inner_half, parcel_ids = locations.parse_job_extent(body)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        # 'partial' or 'missing' = OK; worker will resume-skip completed steps
        job_id = locations.enqueue_job(slug, label, cx, cy,
                                       force_recompress=force_recompress,
                                       inner_half=inner_half,
                                       parcel_ids=parcel_ids)
        if job_id is None:
            self._send_json(409, {"error": f"a job for slug '{slug}' is already queued or running"})
            return
        self._send_json(200, {"job_id": job_id, "slug": slug})

    def _handle_post_jobs_retry(self, job_id):
        ok = locations.retry_job(job_id)
        self._send_json(200 if ok else 404,
            {"job_id": job_id, "retried": ok} if ok else {"error": "job not found"})

    def _handle_post_jobs_cancel(self, job_id):
        ok = locations.cancel_job(job_id)
        self._send_json(200 if ok else 404,
            {"job_id": job_id, "cancelled": ok} if ok else {"error": "job not found or already done"})


def _ensure_self_signed_cert(cert_path: Path, key_path: Path):
    """Generate a self-signed cert (10y validity) so the dev server can
    speak HTTPS. WebCodecs / OffscreenCanvas / clipboard / mediarecorder
    over a LAN hostname all need a secure context; plain HTTP only
    qualifies for localhost. Cert is per-machine, never committed."""
    if cert_path.exists() and key_path.exists():
        return
    import subprocess
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Generating self-signed cert at {cert_path} (one-time, valid 10y)…")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key_path), "-out", str(cert_path),
        "-days", "3650", "-subj", "/CN=inzerator-dev",
        "-addext", "subjectAltName=DNS:localhost,DNS:jans-mac-mini.local,DNS:jans-mac-mini,IP:127.0.0.1,IP:0.0.0.0",
    ], check=True, capture_output=True)


if __name__ == "__main__":
    # ThreadingHTTPServer so the 9 parallel tile requests from the client
    # don't queue behind each other (each HD composite does many ČÚZK fetches).
    # Plain HTTP — for WebCodecs (which needs a secure-context origin) wrap
    # the server with Tailscale Serve or a similar reverse proxy that
    # terminates TLS upstream:
    #     tailscale serve --bg --https=443 http://localhost:8080
    locations.start_worker()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    print(f"Server: http://0.0.0.0:{PORT}/ (threaded, job worker started)")
    server.serve_forever()
