# V2 pipeline — jak funguje a co byly největší problémy

Stav k **2026-05-16**, branch `main`, HEAD `b45112e`.

Tahle nota popisuje šestikrokovou pipeline `tiles_v2_<slug>/`, kterou orchestruje
[`locations.py`](../../locations.py) přes worker thread spuštěný v `server.py`.
Důraz je na **proč to vypadá, jak to vypadá** — pořadí kroků, sentinely,
in-process vs. subprocess split, a lekce, které z toho vypadly.

---

## 1. Vstupní bod a životní cyklus jobu

```
UI (index.html)
  └── POST /api/jobs  { slug, label, cx, cy }
        └── locations.enqueue_job()      ← atomická TOCTOU kontrola pod JOB_CV
              └── JOB_QUEUE.append + JOB_CV.notify
                    └── worker_loop (daemon thread, locations.start_worker)
                          └── _run_one_job
                                └── _run_step ×6
```

Klíčové datové struktury ([`locations.py:21-27`](../../locations.py#L21-L27)):

| Symbol | Co drží |
|---|---|
| `JOBS` | `{job_id: {slug, cx, cy, cancelled, steps:[…]}}` |
| `JOB_QUEUE` | FIFO seznam `job_id`, čeká se přes `JOB_CV.wait()` |
| `CURRENT_JOB`, `CURRENT_PROC` | běžící job + jeho subprocess (pro cancel) |
| `JOB_LOCK` / `JOB_CV` | jedna pod-condvar; veškerý mutating přístup pod ní |

Worker je **jedno-vláknový** (jeden job naráz). To je úmysl — gen_detail při
step 0.5 m saje ~6 GB RAM, paralelně by to mac-mini neutáhl.

### Statusy lokace ([`location_status`](../../locations.py#L78-L92))

Čtené pouze z disku, žádný in-memory bridge:

- `missing` — `tiles_v2_<slug>/` neexistuje, nebo chybí i parent dir
- `partial` — chybí libovolný z `panorama.glb`, `details/{outer,closeup,inner}.glb`
  **nebo** chybí `.compress_ok` sentinel
- `ready` — všechny 4 glb + `.compress_ok` na disku

Tohle umožňuje **resume-from-disk**: pokud server padne v půlce, příští spuštění
ví, co je hotové, a worker resume-skipne dokončené kroky (`_run_step` testuje
`expected_glb(slug, step).exists()` před spuštěním).

---

## 2. Šest kroků pipeline

`STEP_NAMES = ("panorama", "sm5", "outer", "closeup", "inner", "compress")`
([`locations.py:19`](../../locations.py#L19))

Každý krok je buď **subprocess** (samostatný Python skript) nebo **in-process**
(handler v `locations.py`). Důvod: subprocess se snadno cancel-uje
`SIGTERM`em, in-process zase nemá overhead startu a sdílí Python state.

| # | Krok | Druh | Vstup | Výstup | Sentinel |
|---|------|------|-------|--------|----------|
| 1 | `panorama` | subprocess `gen_panorama.py` | DMR5G WCS bbox 30 km | `panorama.glb` (~30 MB, step 30 m) | — |
| 2 | `sm5` | in-process `_do_sm5_download` | MAPNOM list z `KladyMapovychListu/25` | `cache/dmpok_tiff_<CODE>/*.tif` + `cache/ortofoto_<CODE>/*.jpg` | `.sm5_ok` |
| 3 | `outer` | subprocess `gen_detail.py` | SM5 tify + `panorama.glb` | `details/outer.glb` (5 km, step 2.5 m, ~120 MB) | — |
| 4 | `closeup` | subprocess `gen_detail.py` | SM5 + `outer.glb` | `details/closeup.glb` (3 km, step 1.5 m, ~150 MB) | — |
| 5 | `inner` | subprocess `gen_detail.py` | SM5 + `closeup.glb` | `details/inner.glb` (1 km, step 0.5 m, ~170 MB) | — |
| 6 | `compress` | in-process `_do_compress` | originály v `details/` | Draco-encoded glb (~30 MB každý), originály do `_orig_uncompressed/` | `.compress_ok` |

### 2.1 `panorama` — širokozábér 30×30 km

[`gen_panorama.py`](../../gen_panorama.py) stahuje **DMR5G** (Digital Model
Reliéfu 5G — terénní mřížka bez budov) z `ags.cuzk.gov.cz/.../3D/dmr5g`
přes WCS `exportImage`. Velikost 2048×2048 na čtverec 5 km × 5 km kolem
S-JTSK středu lokace. Tile je relativně levný (~30 MB GLB) a slouží jako
**vzdálené pozadí** ve viewer scéně.

### 2.2 `sm5` — předpříprava dat pro detail

In-process, ne subprocess, kvůli sdílení monkey-patchnutého
`socket.getaddrinfo` v `server.py` (viz problém #1 níže).

Kroky:

1. Z `KladyMapovychListu/25` vrátí MAPNOM kódy listů (např. `JESE43`,
   `JESE44`, …) pokrývajících bbox `cx ± 2500`.
2. Pro každý kód stáhne **dva** soubory:
   - `cache/dmpok_tiff_<CODE>/<CODE>.tif` — SM5 DSM (s budovami) pro
     `gen_detail.py`
   - `cache/ortofoto_<CODE>/*.jpg` — surové ortofoto JPEGs konzumované za
     běhu `server.py _proxy_ortofoto_raw`
3. Každý download obalený `_with_retries(label, fn, attempts=3)` (backoff
   1 s, 3 s) — ČÚZK občas vrátí `Connection refused` na jeden tile, sousední
   uspějí.
4. Když jsou všechny soubory na disku, touch `.sm5_ok`.

Důvod, proč se stahuje i ortofoto: některé sítě nemají reliable přístup na
ESRI tile service nebo Google fallback, raw JPEGy ten problém obejdou
([`locations.py:347-354`](../../locations.py#L347-L354)).

### 2.3-2.5 `outer` / `closeup` / `inner` — LOD prsteny

[`gen_detail.py`](../../gen_detail.py) generuje **jeden detailní mesh**
kolem S-JTSK středu, s odstupňovaným rozlišením:

```python
_LOD_PRESET = {
    "outer":   {"half": 2500, "step": "2.5", "fade": 100, "fade_to": "panorama"},
    "closeup": {"half": 1500, "step": "1.5", "fade":  50, "fade_to": "outer"},
    "inner":   {"half":  500, "step": "0.5", "fade":  30, "fade_to": "closeup"},
}
```
([`locations.py:638-642`](../../locations.py#L638-L642))

Klíčový mechanismus: **`--fade-to <parent>`**. Detail vezme svůj outer ring
o šířce `fade` metrů a v něm interpoluje Y-souřadnici lineárně z vlastního
SM5 sample směrem k parentově Y (bilinear sample přes `_sample_grid_y`).
Tím se vyhne viditelný "schod" mezi outer a closeup ringem.

Pořadí `panorama → outer → closeup → inner` je **kvůli fade-to nutné**:
parent (větší ring) musí existovat dřív než child (menší). Reverze by
rozbila Y sampling fade bandu.

Inner step 0.5 m → mesh má ≈ 4 milióny vertexů. To je strop pro
prohlížení v Three.js na desktop GPU. RAM peak při generování ~6 GB.

### 2.6 `compress` — Draco re-pack

[`_do_compress`](../../locations.py#L438-L513) + [`draco_compress_glb.py`](../../draco_compress_glb.py).

In-process protože:

1. `gltf-pipeline` (Node) padá na V8 heap limitu při 800 MB+ glb
2. `DracoPy` umožňuje sub-cm position error s `quant_pos=16` (default; ≈ 15 mm
   pro inner, 76 mm pro outer)

Per-detail flow:

1. `if _orig_uncompressed/<step>.glb exists: skip` — idempotent retry
2. `mv details/<step>.glb _orig_uncompressed/<step>.glb` (atomická záchrana
   originálu před compress crashem)
3. Draco encode zpět do `details/<step>.glb`
4. Pokud encode selže → rename zpět, fail; retry uvidí existující glb
   a half-progress

Když jsou všechny 3 detaily hotové, touch `.compress_ok`. Bez něj
[`location_status`](../../locations.py#L78-L92) reportuje `partial`,
i kdyby všechny glb existovaly — protože by viewer tahal 5× větší soubory.

---

## 3. Subprocess vs. in-process — proč to není uniformní

Původně mělo být všech 6 kroků subprocess. Realita:

- **`sm5`** — chce sdílet `socket.getaddrinfo` monkey-patch (IPv4-only),
  který je nainstalovaný v `server.py`. Subprocess by ho nezdědil
  (problém #1).
- **`compress`** — chce sdílet otevřený DracoPy modul a držet velké numpy
  arrays v paměti (mvprocess by trval déle na startu + nemá smysl, je to
  čistý compute, ne I/O).

Proto `_run_step` má dvě větve ([`locations.py:698-783`](../../locations.py#L698-L783)):

```python
_inproc_handlers = {"sm5": _do_sm5_download, "compress": _do_compress}
handler = _inproc_handlers.get(step["name"])
if handler is not None:
    ok = handler(job, log_path)
    ...
# === Subprocess step: panorama, outer, closeup, inner ===
cmd = cmd_for(step["name"], job["slug"], job["cx"], job["cy"])
CURRENT_PROC = subprocess.Popen(cmd, ...)
```

`cmd_for("sm5"|"compress", …)` cíleně **raises ValueError** — chytí
omylem zapnutý subprocess path.

---

## 4. Sentinely vs. .glb — proč existuje `expected_glb` returns marker

[`expected_glb(slug, step)`](../../locations.py#L64-L75) vrací buď cestu ke
glb (panorama/outer/closeup/inner) **nebo** sentinel soubor (`.sm5_ok` /
`.compress_ok`). Worker volá `.exists()` na to v `_run_step`, takže rozdíl
sentinel-vs-glb je transparentní pro skip logiku.

Proč to není uniformní:

- `sm5` produkuje **n souborů** v `cache/dmpok_tiff_*` a `cache/ortofoto_*`,
  ne jeden glb. Test "všechny n existují" je drahý a křehký. Sentinel
  `touch` na konci úspěšného běhu je jediný atomický gesture.
- `compress` přepisuje existující `details/*.glb` in-place. Po compressu se
  velikost změnila, ale path je stejná — bez sentinelu by `location_status`
  nepoznal, jestli `outer.glb` je 168 MB original nebo 30 MB Draco verze.

---

## 5. Cancel + retry — kde to umí, kde ne

[`cancel_job`](../../locations.py#L601-L629):

- queued job → `JOB_QUEUE.remove(job_id)`, hotovo
- running job →
  - pro subprocess: `CURRENT_PROC.terminate()` + `Timer(10s, kill)` (daemon)
  - pro in-process: nastaví `job["cancelled"] = True`, **ale handler musí
    sám pollovat tu flag** (`_do_sm5_download` to dnes nedělá → cancel se
    projeví až mezi tily, viz issue I3 v session handoff)

[`retry_job`](../../locations.py#L576-L598):

- failed/cancelled steps → reset na `pending`
- `JOB_QUEUE.append(job_id)`
- worker resume-skipne hotové stepy (existence souboru)

Důsledek: retry je **idempotentní** v normálním případě, ale **netroubí
sentinely zpět**. Pokud uživatel smaže `.sm5_ok` ručně, retry uvidí
existující tify v `cache/` a sentinel znovu touchne — což chceme.

---

## 6. Nejvyšší dosažené problémy

### 6.1 IPv6 do ČÚZK je z naší LAN mrtvá — patch `getaddrinfo` (commit `9176f38`)

ČÚZK má AAAA záznam pro `ags.cuzk.cz`, ale v6 routing odněkud na cestě
padá. urllib se přes `getaddrinfo` natáhne na v6, čeká 30 s timeout, pak
fall-through na v4. Každý ČÚZK call → +30 s.

Řešení v `server.py`: process-wide monkey-patch
`socket.getaddrinfo` filtrující na `AF_INET`. Standalone skripty
(`download_ortofoto.py`, `download_tiff.py`) přešly z `urllib` na
`requests` — urllib3 má vlastní v4 fallback.

**Nevyřešené (issue I1):** subprocess children (`gen_panorama.py`,
`gen_detail.py`) patch nedědí. Každé ČÚZK volání z gen_*.py má pořád
v6 fall-through latency. Akceptovatelné dnes, ale je to potenciální spike.

### 6.2 `gen_detail.children` carving × `--fade-to` pořadí — neslučitelné (commit `9176f38`)

`gen_detail.py` má dva nezávislé feature na sebe ortogonální:

- **`--fade-to <parent>`** — chce, aby parent (větší) byl hotový dřív
- **`--children <list>`** — chce, aby children (menší) byly hotové dřív,
  protože z parenta vystřihne díry v místech, kde leží children
  (a tedy: large → small fade pořadí **přesně reverzí** large → small
  carve pořadí)

Build-time fix by chtěl dvojprůchodovou generaci (large → small fade,
pak regen large s carve-children z manifestu). Místo toho:
**runtime workaround ve fragment shaderu** — každý LOD parent's
`MeshBasicMaterial.onBeforeCompile` injectne `discard` v world-XZ bbox
toho nejbližšího menšího LOD. Vidět ten kód v `v2.html` (search:
`onBeforeCompile`, kontext v commitu `9176f38`).

Cena: trochu fragment overhead a krátký pohled "skrz" inner ring na
outer mesh v rohu, kde shader hranice nezalícuje úplně s mesh hranou. Pro
real-estate marketing video to nevadí.

### 6.3 `ground_z` != `village_centroid` (commit `a030ddd`)

`gen_panorama.py` napíše do manifestu `region.ground_z` = elevace ve
**středu 30 km panorama bboxu**. Ten střed je ale často někde v terénu
mimo vesnici. Kryry leží +50 m nad svým bbox-centrem, Kamýk +80 m.

Důsledek: scena má detail mesh na world Y ≈ village_centroid, ale
kamera + parcel pick plane mířily na ground_z. Vesnice mimo záběr.

Fix v `v2.html`:

1. Po načtení `inner.glb` spočítat centroidní Y (mean Y vertices)
2. Retargetnout `controls.target.y` a kameru
3. Parcel pick plane (raycaster `Plane`) používá `controls.target.y`
   místo hardkódované 0

### 6.4 Detail glb fileze — Draco s konkrétními gotchas (commits `eb5d50a`, `968900c`, `345ec66`)

Tři ortogonální problémy navrch jeden po druhém:

1. **gltf-pipeline (Node) padá na V8 heap** při Draco-encode 800 MB+ glb.
   → přepsat in-process Pythonem (`DracoPy`).
2. **DracoPy 2.0 sortuje atributy podle `attribute_type`, ne pořadí
   `encode()` argumentů.** Naivní mapping `unique_id` v0 → POSITION,
   v1 → TEXCOORD_0 fungoval na DracoPy 1.x; po upgradu byly UV souřadnice
   přiřazené pozici a obráceně, výsledný glb byl nečitelný. Fix v
   `draco_compress_glb.py`: explicitní mapping podle DracoPy `attribute_type`
   (`POSITION = 0`, `TEX_COORD = 2`), ne arg-index.
3. **`.compress_ok` sentinel je nutný** — bez něj viewer tahá 168 MB inner
   glb místo 30 MB. Pipeline status `partial`, dokud sentinel chybí.

### 6.5 Diacritická vyhledávka (commit `f16f2e2`)

ČÚZK ArcGIS REST `where`-clause **nemá unaccent / fold funkci**. RUIAN
ale ukládá `Fügnerova 355/16, 40502 Děčín` přesně s diakritikou. Uživatel
zadá `Fugnerova 355/16 Decin` — 0 hits.

Řešení v [`_search_addresses`](../../locations.py#L176-L232):

- Pokud query obsahuje **alespoň jeden numerický token** (`355/16`),
  ten token diakritiku nemá → server-side `LIKE '%355/16%'` vrátí
  ~10–100 kandidátů.
- Python-side ASCII-fold (`NFD` + drop combining marks + lower) každého
  kandidáta i query tokenu, AND substring match.
- Když query nemá číselný token, fallback na literal LIKE (diakritika
  musí sedět přesně).

To samé pro parcely (`_search_parcels`) — filter na `cisloparcely`
exact-match (číslo parcely diakritiku nemá), zbylé tokeny fold-filterujou
`katastralniuzemicisloparcely` label.

### 6.6 OSM verify map (commit `b8d27f0`)

RUIAN search vrátí adresu, kterou si uživatel myslí, že je správná.
Reálně byly případy, kdy adresa s číslem 355/16 v RUIANu odkazovala na
úplně jinou stranu obce než si uživatel pamatoval. Předtím to vyšlo
najevo až po **15-30 minutovém generování** celé pipeline.

Fix: po kliknutí na hit se v dialogu zobrazí Leaflet mapa s pinem na
WGS souřadnicích. Tlačítka **Potvrdit ✓** / **Zpět**. Address hits zoom 18,
parcel 17 (parcely jsou v průměru větší). Použili jsme Leaflet 1.9.4
z CDN, žádný build step.

### 6.7 L-corner výška při kresbě parcel outline (commit `f55ed76`)

Parcel outline se na inner mesh **drapuje přes DSM heightfield** — pro
každý vertex outline se z 2001² Float32Array Y gridu bilineárně sampluje
výška (matchuje `gen_detail._sample_grid_y` triangle conventionu).

Problém: když outline překračuje hranu budovy, dva sousední vertexy
mají rozdíl ~3 m. Lineární segment mezi nimi vede pod střechou →
outline mizí. Fix: detekce Y jumps ≥ 1.5 m, vložení **L-corneru**
(jeden dodatečný vertex přesně nad sousedním vertexem) → outline
vystoupá kolmo po stěně, pak vodorovně po střeše.

### 6.8 Subprocess race na CURRENT_PROC (commit `bcd1480`)

Cancel-job (HTTP thread) četl `CURRENT_PROC` mezitím, co worker thread
volal `Popen()`. Race: cancel přišel mezi `_run_step` startem a
přiřazením `CURRENT_PROC`. Subprocess pak běžel dál, cancel-job vrátil
True, UI ukazovalo "cancelled", ale pipeline dojela.

Fix: zápis `CURRENT_PROC` pod `JOB_LOCK`:

```python
with JOB_LOCK:
    CURRENT_PROC = subprocess.Popen(cmd, ...)
```

### 6.9 Atomická slug-collision check (commit `0dd9fac`)

Dva souběžné POSTy `/api/jobs` se stejným slugem oba prošly disk-status
checkem (žádný adresář, status `missing`) → oba enqueueovaly → race.

Fix v [`enqueue_job`](../../locations.py#L533-L554): kontrola
existujícího jobu se stejným slugem pod `JOB_CV` (jak v queue, tak
`CURRENT_JOB`). Vrátí `None` → HTTP layer mapuje na 409.

### 6.10 Worker thread crash → silently broken queue (commit `bcd1480`)

Bug v `_run_step` (disk full při psaní logu, future bug, …) by vyhodil
unhandled exception, zabil worker thread, **queue by tiše zůstal
nezpracovaný**. UI by ukazovalo "running" donekonečna.

Fix: `try/except` kolem worker body. Failed job je ztracen (popnut
z queue, nerequeue-ovaný), ale další joby se zpracovávají. `_tb.print_exc()`
do server log.

---

## 7. Co je v rukávech do budoucna

Z `2026-05-16-session-handoff.md` Known issues + reálné pozorování:

- **I3:** sm5/compress nemají overall timeout. Cancel během sm5 reaguje
  až mezi tily (max ~10 min stuck na jednom big tiff).
- **I1:** subprocess gen_*.py nedědí IPv4 patch. +30 s na call.
- **Force-recompress mode** pro `partial` lokace s vyšším `quant_pos`
  (default 16 ≈ 15 mm error inner; 20 ≈ 1 mm error, +25 % size). Dnes
  by se musely manuálně smazat z `_orig_uncompressed/`.
- **Resume-from-disk pro lokace bez in-memory jobu** (commit `b39362a`)
  funguje, ale uživatel musí znát původní `cx, cy` (čte se z `manifest.json`).
  Pokud manifest chybí → musí znovu RUIAN search.
- **Polling fixed 2 s** v index.html — bez exponential backoffu. Při
  10 souběžných uživatelích by to nebylo příjemné, ale dnes je to jediný
  uživatel.

---

## 8. Mapa souborů

| Soubor | Role |
|---|---|
| [`locations.py`](../../locations.py) | orchestrátor, job state, in-process kroky |
| [`server.py`](../../server.py) | HTTP API, IPv4 patch, ortofoto proxy |
| [`gen_panorama.py`](../../gen_panorama.py) | step 1 — DMR5G 30 km mesh |
| [`download_tiff.py`](../../download_tiff.py) | step 2a — DMPOK-TIFF (SM5 DSM) |
| [`download_ortofoto.py`](../../download_ortofoto.py) | step 2b — raw JPEG ortofoto |
| [`gen_detail.py`](../../gen_detail.py) | step 3-5 — LOD detail meshes |
| [`draco_compress_glb.py`](../../draco_compress_glb.py) | step 6 — Draco encode |
| [`index.html`](../../index.html) | dashboard + create form, polling |
| [`v2.html`](../../v2.html) | viewer (LOD shader-clip, drape outline, retarget camera) |
| `tiles_v2_<slug>/manifest.json` | region metadata (label, cx/cy, ground_z, bbox) |
| `cache/jobs/<job_id>/<step>.log` | per-step log pro UI a debug |

---

## 9. Tldr v jedné větě

Šest kroků (panorama → sm5 → outer → closeup → inner → compress),
serializovaných jedním worker threadem; sentinely (`.sm5_ok`,
`.compress_ok`) řeší to, co jeden glb file na disku není schopen
vyjádřit (multi-soubor, in-place rewrite); největší pain points byly
**ČÚZK IPv6 timeout** a **konflikt mezi `--fade-to` a `--children`
v gen_detail**, kde se druhé řeší až runtime ve viewer shaderu.
