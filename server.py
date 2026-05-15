"""HTTP server with WMS proxy for ortofoto."""
import http.server
import urllib.request
import urllib.parse
import os
import threading
import json
from pathlib import Path
import locations

# Cap concurrent outgoing requests to ČÚZK across all server threads —
# avoids Connection Refused when 9 HD tiles each spawn 6 workers (54 in flight).
_CUZK_SEM = threading.Semaphore(6)

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

def _ruian_get(url, timeout=30, retries=3, backoff=0.5):
    """GET against ČÚZK ArcGIS / RÚIAN with exponential backoff. The service
    intermittently refuses connections under load; one immediate failure is
    common but a 2nd or 3rd retry within 1–2 s usually succeeds."""
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

    # Page through results (RÚIAN max 1000 per response).
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
            "resultOffset": str(offset),
            "resultRecordCount": "1000",
        }
        raw = json.loads(_ruian_get(f"{url}?" + urllib.parse.urlencode(params), timeout=30))
        feats = raw.get("features", [])
        raw_features.extend(feats)
        if len(feats) < 1000:
            break
        offset += 1000

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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

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
            src_transform = rasterio.transform.from_origin(
                crop_x0, crop_y0, abs(px_x), abs(px_y),
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
            # only; lower = better quality, default 128). UASTC mode ignores -q
            # and produces ~5× larger files at near-original quality.
            cropped.save(buf, "JPEG", quality=95, optimize=True, subsampling=0)
            import subprocess, tempfile, uuid
            ktx2_mode = query.get("ktx2", ["etc1s"])[0].lower()
            try:
                q_val = max(1, min(255, int(query.get("quality", ["128"])[0])))
            except ValueError:
                q_val = 128
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
        candidates = []  # (jpg, vals, (lo_x, lo_y, hi_x, hi_y))
        for d in sorted(Path("cache").glob("ortofoto_*")):
            for jpg in d.glob("*.jpg"):
                jgw_path = jpg.with_suffix(".jgw")
                if not jgw_path.exists():
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
                # Skip sheets that don't intersect the bbox at all
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
        # ?source=raw — crop directly from the locally cached raw SM5 JPEG
        # (ČÚZK native 12.5 cm/px). Eliminates tile-service upscaling +
        # our composite/re-encode pipeline. Highest available quality.
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
                q_val = max(1, min(255, int(query.get("quality", ["128"])[0])))
            except ValueError:
                q_val = 128
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
            try:
                results = locations.ruian_search(q)
            except locations.RuianUnavailable as e:
                self._send_json(503, {"error": f"ČÚZK unavailable: {e}"})
                return
            self._send_json(200, results)
            return
        if _path == "/api/jobs":
            self._send_json(200, locations.list_active_jobs())
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
        elif self.path.endswith(".glb"):
            self._serve_glb_gzipped()
        else:
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
            try:
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
                        "resultRecordCount": "500",
                    }),
                    timeout=60,
                ))
            except Exception as e:
                self.send_error(502, f"RÚIAN query failed: {e}")
                return

            buildings = []
            for f in raw.get("features", []):
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
        try:
            gcx = float(query["gcx"][0])
            gcy = float(query["gcy"][0])
            sx = float(query["sx"][0])
            sy = float(query["sy"][0])
        except (KeyError, ValueError):
            self.send_error(400, "Required params: gcx, gcy, sx, sy (S-JTSK)")
            return

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
                "outSR": "5514", "f": "json", "returnGeometry": "true",
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
                    "outSR": "5514", "f": "json", "returnGeometry": "true",
                    "resultRecordCount": "10",
                }
                raw = json.loads(_ruian_get(f"{url}?" + urllib.parse.urlencode(env_params), timeout=15))
                feats = raw.get("features", [])
                if not feats:
                    # Even the 10m envelope had nothing. Probably outside village.
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
                self.send_error(404, "Parcel has no geometry")
                return
            outer = rings[0]
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
            self._send_json({"found": False, "reason": "Žádná budova v okolí 5 m"})
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
        self._send_json(out)

    def _send_json(self, obj):
        import json
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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

            overpass_q = f"""
            [out:json][timeout:30];
            (
              node["historic"](around:{radius},{clat},{clon});
              node["tourism"](around:{radius},{clat},{clon});
              node["natural"="peak"](around:{radius},{clat},{clon});
              node["amenity"="place_of_worship"](around:{radius},{clat},{clon});
              way["historic"](around:{radius},{clat},{clon});
              way["amenity"="place_of_worship"](around:{radius},{clat},{clon});
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
                poi_type = (tags.get("historic") or tags.get("tourism")
                            or tags.get("natural") or tags.get("amenity") or "unknown")
                pois.append({
                    "id": el["id"],
                    "name": tags.get("name") or tags.get("name:cs") or "(bez názvu)",
                    "type": poi_type,
                    "coords": [round(lx, 1), round(lz, 1)],
                    "wikipedia": tags.get("wikipedia"),
                    "wikidata": tags.get("wikidata"),
                })

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

        # Disk-cache the compressed payload keyed on file mtime + encoding.
        st = os.stat(file_path)
        enc = "br" if prefer_br else "gzip"
        cache_path = f"{file_path}.{enc}.{int(st.st_mtime)}"
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                compressed = f.read()
        else:
            with open(file_path, "rb") as f:
                data = f.read()
            if prefer_br:
                try:
                    import brotli
                    compressed = brotli.compress(data, quality=6)
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
        """Fetch ČÚZK katastrální mapa WMS as transparent PNG overlay."""
        import numpy as np
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1])

        bbox_str = query.get("BBOX", [""])[0]
        if not bbox_str:
            self.send_error(400, "Missing BBOX")
            return

        style = query.get("style", ["normal"])[0]
        # Higher resolution WMS for thicker/antialiased lines
        wms_size = {"thin": 512, "normal": 1024, "medium": 1536, "thick": 2048}.get(style, 1024)

        wms_url = (
            f"https://services.cuzk.cz/wms/wms.asp?"
            f"SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
            f"&LAYERS=DKM&SRS=EPSG:5514&BBOX={bbox_str}"
            f"&WIDTH={wms_size}&HEIGHT={wms_size}&FORMAT=image/png&TRANSPARENT=TRUE&STYLES="
        )

        try:
            req = urllib.request.Request(wms_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                img_data = resp.read()

            from PIL import Image
            from io import BytesIO

            img = Image.open(BytesIO(img_data)).convert("RGBA")
            arr = np.array(img)

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

            # Resize to output size
            result = result.resize((1024, 1024), Image.LANCZOS)
            buf = BytesIO()
            result.save(buf, "PNG")
            data_bytes = buf.getvalue()

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data_bytes)
        except Exception as e:
            self.send_error(502, f"Cadastre WMS error: {e}")

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
        cx = body.get("cx")
        cy = body.get("cy")
        if not slug or cx is None or cy is None:
            self._send_json(400, {"error": "required fields: slug, cx, cy"})
            return
        if not locations.is_valid_slug(slug):
            self._send_json(400, {"error": "slug must match ^[a-z0-9-]+$"})
            return
        try:
            cx = float(cx); cy = float(cy)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "cx, cy must be numbers"})
            return
        status = locations.location_status(slug)
        if status == "ready":
            self._send_json(409, {
                "error": "slug already exists and is ready",
                "slug": slug,
                "suggestion": locations.next_free_slug(slug,
                    set(loc["slug"] for loc in locations.list_locations())),
            })
            return
        # 'partial' or 'missing' = OK; worker will resume-skip completed steps
        job_id = locations.enqueue_job(slug, label, cx, cy)
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
