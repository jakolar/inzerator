# Lokace UI — design (2026-05-15)

## Cíl

Webové UI pro vytváření nových v2 lokací bez nutnosti zadávat příkazy z CLI. Uživatel zadá adresu, klikne „Generovat", a backend spustí celou pipeline (`gen_panorama.py` + 3× `gen_detail.py`). UI zároveň slouží jako dashboard existujících lokací s odkazem na viewer.

## Brainstorming volby (zafixované)

| # | Otázka | Volba |
|---|---|---|
| 1 | Vstup adresy | **B** — RUIAN search (ČÚZK API) |
| 2 | Rozsah UI | **B** — Dashboard + create form (žádné delete/edit) |
| 3 | LOD parametry | **A** — Fixní Hnojice defaulty |
| 4 | Progress UX | **B** — 4-step status s ikonkami, polling 2 s |
| 5 | Slug | **C** — Auto-prefill + editable |
| 6 | Concurrency | **B** — FIFO queue, in-memory |
| 7 | Failure | **B** — Stop & keep partial, retry skipuje hotové kroky |

## Architektura

Tři vrstvy:

### 1. Frontend — `/index.html`

Statická stránka, vanilla JS + fetch. Stejný styl jako `v2.html` (žádný build, žádné node deps). Routing root `/` → `index.html` (automatický pickup `SimpleHTTPRequestHandler`).

Layout:

```
Lokace
  • Hnojice          ✓ ready                    [Otevřít viewer]
  • Šantovka         ⚠ partial — outer failed   [Retry] [Otevřít]
  • Strážek          ⏳ panorama running…

[+ Nová lokace]   (toggle inline form)

Nová lokace
  Adresa: [_____________________] [Hledat]
  Výsledky (po klikem hledat):
    • Hnojice 47, 783 99 Hnojice (Olomouc)     ← klik vybere
    • Hnojice 33, 783 99 Hnojice (Olomouc)
  Po výběru:
    Slug:  [hnojice]      (auto z obce, editable)
    Label: [Hnojice 47]   (auto, editable)
    S-JTSK: -547700.0, -1107700.0  (read-only)
    ▾ Pokročilé: ruční cx,cy override (collapsible)
  [Generovat] [Zrušit]
```

Polling: jeden globální tick každé 2 s na `/api/jobs?active=1` vrátí všechny aktivní + queued joby v jednom requestu. Tick se zastaví, když je seznam prázdný.

### 2. Backend endpoints (rozšíření `server.py` `ProxyHandler`)

| Method | Path | Odpověď |
|---|---|---|
| GET | `/api/locations` | List `{slug, label, status: ready\|partial\|generating, has_panorama, has_outer, has_closeup, has_inner}` |
| GET | `/api/ruian/search?q=<text>` | Top 10 `{label, sjtsk_cx, sjtsk_cy, obec}` |
| POST | `/api/jobs` | Body `{slug, label, cx, cy}` → `{job_id}`; 409 pokud slug existuje |
| GET | `/api/jobs?active=1` | List `{job_id, slug, queue_position, steps[]}` všech queued+running |
| GET | `/api/jobs/<id>` | Detail jednoho jobu |
| POST | `/api/jobs/<id>/retry` | Zařadí znovu (resume-skip hotové kroky) |

### 3. Job runner (background thread v server.py)

In-memory struktura:

```python
JOBS = {}            # job_id (uuid4) → job dict
JOB_QUEUE = []       # FIFO list job_id
CURRENT_JOB = None
JOB_LOCK = threading.Lock()
JOB_CV = threading.Condition(JOB_LOCK)  # worker wait/notify
```

Job dict:

```python
{
  "slug": "hnojice", "label": "Hnojice 47",
  "cx": -547700.0, "cy": -1107700.0,
  "steps": [
    {"name": "panorama", "state": "pending|running|ok|fail|skipped",
     "error": None | "<last 200 chars stderr>",
     "started_at": None | float, "finished_at": None | float},
    {"name": "outer", ...},
    {"name": "closeup", ...},
    {"name": "inner", ...},
  ],
  "created_at": float,
}
```

Worker loop (jeden thread, startuje v `__main__` před `serve_forever()`):

```
while True:
  with JOB_CV: while not JOB_QUEUE: JOB_CV.wait()
  job_id = JOB_QUEUE.pop(0); CURRENT_JOB = job_id
  for step in job.steps:
    expected_glb = path_for(step.name, job.slug)
    if expected_glb.exists():
      step.state = "skipped"; continue
    step.state = "running"; step.started_at = now()
    proc = subprocess.run(cmd_for(step, job), capture_output=True, text=True, timeout=30*60)
    write cache/jobs/<job_id>/<step.name>.log with stdout+stderr
    if proc.returncode == 0 and expected_glb.exists():
      step.state = "ok"
    else:
      step.state = "fail"; step.error = proc.stderr[-200:]
      break  # zastavit, partial zůstane na disku
    step.finished_at = now()
  CURRENT_JOB = None
```

Subprocess příkazy (fixní Hnojice defaulty):

| Step | Příkaz |
|---|---|
| panorama | `python3 gen_panorama.py --region <slug> --center-sjtsk=<cx>,<cy>` |
| outer | `python3 gen_detail.py --region <slug> --slug outer   --center-sjtsk=<cx>,<cy> --half 2500 --step 2.5 --fade 100 --fade-to panorama --zoom 17 --size 4096` |
| closeup | `python3 gen_detail.py --region <slug> --slug closeup --center-sjtsk=<cx>,<cy> --half 1500 --step 1.5 --fade  50 --fade-to outer    --zoom 21 --size 8192` |
| inner | `python3 gen_detail.py --region <slug> --slug inner   --center-sjtsk=<cx>,<cy> --half  500 --step 1.0 --fade  30 --fade-to closeup  --zoom 21 --size 8192` |

`path_for("panorama", slug)` = `tiles_v2_<slug>/panorama.glb`, ostatní `tiles_v2_<slug>/details/<step>.glb`.

## RUIAN search

Endpoint `/api/ruian/search?q=<text>` v backendu volá **ČÚZK Geokoder REST**. Přesná adresa služby není ve `server.py` zatím použitá — implementace si při bootstrapu ověří dostupnost dvou kandidátů (od ČÚZK to bývá `https://ags.cuzk.cz/arcgis/rest/services/Geokoder/GeocodeServer/findAddressCandidates` nebo `https://ags.cuzk.gov.cz/arcgis2/rest/services/Geokoder/GeocodeServer/findAddressCandidates`) — kterýkoli vrátí 200 na sanity dotaz, ten se zafixuje. Parametry:

```
SingleLine=<q>&outSR=5514&maxLocations=10&f=json
```

`outSR=5514` zajistí, že odpověď nese souřadnice v S-JTSK rovnou (žádná lokální konverze WGS↔S-JTSK).

**Fallback API**, pokud Geokoder nevrátí 200 ani z jedné varianty: použít **`RUIAN/MapServer/3/query`** (stejný endpoint, který `server.py:1333` už používá pro budovy) s `where`-filtrem na `cisladomovni` + `nazevobce` jako sekundární cesta. Detail mapování `q → where` se vyřeší v plánu implementace (regex split „<obec> <č.p.>").

Backend mapuje JSON odpověď na:

```json
{
  "label": "Hnojice 47, 783 99 Hnojice u Šternberka",
  "sjtsk_cx": -547700.0,
  "sjtsk_cy": -1107700.0,
  "obec": "Hnojice"
}
```

`obec` se extrahuje z `address.City` (nebo posledního segmentu před PSČ) — slouží pro auto-slug `slugify(obec).lower()`.

**Fallback při výpadku ČÚZK:** `/api/ruian/search` vrátí 503 s body `{error: "ČÚZK unavailable"}`. UI ukáže warning + collapsible „Ruční S-JTSK input" (cx,cy textové pole), který obejde search a pošle rovnou `/api/jobs`.

## Data flow (happy path)

```
1. User open /index.html
2. fetch /api/locations → vykreslí seznam (Hnojice ✓, ...)
3. User klik [+ Nová lokace] → form se rozbalí
4. User type "Hnojice 47", klik Hledat
5. fetch /api/ruian/search?q=Hnojice%2047 → 5 hits
6. User klik na hit → form prefilled: slug="hnojice-2" (kolize), label="Hnojice 47", cx/cy z RUIAN
7. User upraví slug → "hnojice-statek", klik Generovat
8. POST /api/jobs {slug, label, cx, cy} → 201 {job_id}
9. Form se zavře, nová řádka "hnojice-statek ⏳ queued"
10. Polling /api/jobs?active=1 každé 2 s
11. Worker thread: panorama running → ok → outer running → ... → inner ok
12. Všech 4 steps ✓ → řádka "✓ ready" + [Otevřít viewer] aktivní
13. Klik Otevřít viewer → /v2.html?region=hnojice-statek
```

## Resume / retry mechanika

Klíčový princip: **resume jede přes existenci `.glb` souborů na disku, ne přes job state v paměti.** Worker pro každý krok zkontroluje `expected_glb.exists()` → pokud ano, mark `skipped`. Tj. "retry" je technicky to samé jako "nový job se stejným slugem" — worker projde 4 stepy a hotové preskočí.

Z toho plynou dvě cesty k retry:

1. **Job stále v paměti** (job_id existuje v `JOBS`) — UI tlačítko Retry pošle `POST /api/jobs/<id>/retry`. Backend resetuje stepy ze stavu `fail` na `pending` a zařadí job zpět do `JOB_QUEUE`. Žádný nový `job_id`.
2. **Job ztracen** (restart backendu, partial lokace nalezena scanem disku) — UI nemá `job_id`, místo toho zavolá `POST /api/jobs` se `slug` partial lokace. Backend přečte `tiles_v2_<slug>/manifest.json`, extrahuje `region.center_sjtsk` jako `cx,cy`, vytvoří nový job. Slug-kolize check vrátí 200 (ne 409), pokud lokace je `partial` (chybí alespoň jeden ze 4 `.glb`). 409 vrátí jen pokud všechny 4 `.glb` existují (lokace `ready`) — to chrání před náhodným přepsáním kompletní lokace.

UI tlačítko **Retry** se zobrazí u lokace ve stavu `partial` (dashboard scan) nebo u jobu se stavem `fail` v aktivní listě. Klik na tlačítko: pokud UI ví o `job_id`, použije cestu 1; jinak cestu 2. Manuální „force regenerate all" lokace v `ready` stavu: ruční `rm -rf tiles_v2_<slug>/` v shellu, pak nový job přes UI (YAGNI: UI tlačítko pro destruktivní rebuild zatím není).

## Stale state po restartu backendu

`JOBS`, `JOB_QUEUE` jsou in-memory, restart = lost. Důsledky:
- Lokace s plnými `.glb` souboru se stále ukazují jako `ready` (scan disku).
- Lokace s částečnými `.glb` (panorama OK, ale outer chybí) se ukazují jako `partial`.
- Běžící job v okamžiku restartu se ztratí. Uživatel ve dashboardu uvidí `partial`, klikne Retry → nový job zařazen.

YAGNI: persistence queue do souboru přes restart se nedělá.

## Error handling

| Situace | Chování |
|---|---|
| ČÚZK Geocoder 5xx / timeout | `/api/ruian/search` → 503; UI warning + manual S-JTSK input |
| `/api/ruian/search` 0 hits | UI „nic nenalezeno, zkus jiné spelling" |
| Slug existuje a je `ready` (všechny 4 `.glb`) | `POST /api/jobs` → 409; UI nabídne přepsání slugu (auto append `-2`). Force-overwrite v UI není (`rm -rf` ručně) |
| Slug existuje a je `partial` (chybí ≥1 `.glb`) | `POST /api/jobs` → 200 + `job_id`; worker resume-skipuje hotové kroky |
| Slug invalid (regex `^[a-z0-9-]+$`) | `POST /api/jobs` → 400 |
| gen_panorama.py / gen_detail.py exit ≠ 0 | step `fail`, error = last 200 chars stderr; loop break |
| Subprocess timeout (>30 min per step) | step `fail`, error = "timeout 30m" |
| Backend crash mid-job | Při startu `JOBS = {}`; uživatel ve dashboardu vidí `partial` + Retry |

## Logy

Per-step log soubor: `cache/jobs/<job_id>/<step_name>.log` (stdout+stderr subprocesu). UI je nezobrazuje (Q4 = B), ale jsou na disku pro post-mortem debugging. Žádné rotation/cleanup — disk levný, počet jobs malý.

## Testování

Manual smoke test:
1. Spustit server, otevřít `/index.html`
2. Verify dashboard ukáže existující Hnojice jako `ready`
3. Klik [+ Nová lokace], hledat „Šantovka", vybrat, Generovat
4. Sledovat polling: panorama → outer → closeup → inner
5. All ✓ → klik Otevřít viewer → ověřit, že `v2.html?region=santovka` načte
6. **Failure test:** uprostřed generování `kill -9` python subprocesu → ověřit, že step → fail, lokace ukáže `partial` + Retry tlačítko
7. **Retry test:** klik Retry → worker projde, hotové kroky skipne, failed step přespustí

Automatizovaný test (volitelný — viz YAGNI sekce): pytest na endpoint contracts.

## Out of scope (YAGNI)

- Delete / archive lokace (žádný odpovídající endpoint, dashboard nepokazí; ruční `rm -rf tiles_v2_<slug>/`)
- Edit existující lokace (parametry, label) — regenerate from scratch
- Streamování stdout logu (logy na disku stačí)
- Quality preset picker (low/medium/high) — fixní Hnojice defaulty
- Advanced LOD form (`--half`, `--step`, `--zoom` per layer)
- Auth (Tailscale-only network)
- Notifikace (web push, e-mail) po dokončení
- Persistence queue přes restart (in-memory acceptable)
- Automatizované pytest testy pro endpoint contracts (manual smoke stačí)
- Mobile-specific tweaky (vanilla flexbox + Tailscale + iPad funguje)
- Cancel button pro běžící job (kill subprocesu) — uživatel ho prostě nechá doběhnout nebo restartuje server

## Soubory, které se změní / přidají

| Soubor | Akce |
|---|---|
| `index.html` | **Nový** — dashboard + create form (~ 400 řádků inline JS+HTML) |
| `server.py` | **Modifikace** — přidat endpointy + worker thread + job state struktury; cca 300 řádků |
| `cache/jobs/` | **Vznikne automaticky** runtime — per-job logy |

Žádné nové python balíčky. Žádný build step.
