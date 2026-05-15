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
| POST | `/api/jobs/<id>/cancel` | Pokud je queued: odstraní z fronty. Pokud běží: pošle SIGTERM subprocesu, počká, mark `cancelled` |

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
    {"name": "panorama", "state": "pending|running|ok|fail|skipped|cancelled",
     "error": None | "<head 300 + tail 300 chars stderr>",
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
  job_id = JOB_QUEUE.pop(0)
  job = JOBS[job_id]
  if job.cancelled: continue   # cancel-while-queued, nepouštět
  CURRENT_JOB = job_id; CURRENT_PROC = None
  for step in job.steps:
    if job.cancelled:
      step.state = "cancelled"; break
    expected_glb = path_for(step.name, job.slug)
    if expected_glb.exists():
      step.state = "skipped"; continue
    step.state = "running"; step.started_at = now()
    CURRENT_PROC = subprocess.Popen(cmd_for(step, job), stdout=PIPE, stderr=PIPE, text=True)
    try:
      out, err = CURRENT_PROC.communicate(timeout=60*60)   # 60 min per step (cold cache)
    except subprocess.TimeoutExpired:
      CURRENT_PROC.kill(); out, err = CURRENT_PROC.communicate()
      step.state = "fail"; step.error = "timeout 60m"; break
    write cache/jobs/<job_id>/<step.name>.log with out + err
    if job.cancelled:
      step.state = "cancelled"; break
    if CURRENT_PROC.returncode == 0 and expected_glb.exists():
      step.state = "ok"
    else:
      step.state = "fail"
      step.error = err[:300] + "\n...\n" + err[-300:] if len(err) > 600 else err
      break  # zastavit, partial zůstane na disku
    step.finished_at = now()
  CURRENT_JOB = None; CURRENT_PROC = None
```

`POST /api/jobs/<id>/cancel` (handler):
- Najde job. Pokud `job_id in JOB_QUEUE`: odebere z fronty, mark `job.cancelled = True`. Vrátí 200.
- Pokud `CURRENT_JOB == job_id`: mark `job.cancelled = True` + `CURRENT_PROC.terminate()`, počká 10 s, pak `kill()`. Vrátí 200.
- Jinak (job už `ok`/`fail`): 409.

Subprocess příkazy (fixní Hnojice defaulty):

| Step | Příkaz |
|---|---|
| panorama | `python3 gen_panorama.py --region <slug> --center-sjtsk=<cx>,<cy>` |
| outer | `python3 gen_detail.py --region <slug> --slug outer   --center-sjtsk=<cx>,<cy> --half 2500 --step 2.5 --fade 100 --fade-to panorama --zoom 17 --size 4096` |
| closeup | `python3 gen_detail.py --region <slug> --slug closeup --center-sjtsk=<cx>,<cy> --half 1500 --step 1.5 --fade  50 --fade-to outer    --zoom 21 --size 8192` |
| inner | `python3 gen_detail.py --region <slug> --slug inner   --center-sjtsk=<cx>,<cy> --half  500 --step 0.5 --fade  30 --fade-to closeup  --zoom 21 --size 8192` |

Inner `--step 0.5` (jemnější než stávající Hnojice manifest = 1.0) — odpovídá `gen_multitile.py` `village_flat` profilu (`step_m: 0.5` pro innermost ring). 4× vertex count proti 1.0 m, ale generation time + disk akceptovatelné, vizuální zisk reálný (terénní hrany pod 1 m). Hnojice manifest tím pádem nebude přesně reprodukovatelný novou pipeline — pokud chceš starou Hnojici regenerovat s novými parametry, smaž `tiles_v2_hnojice/` a spusť přes UI.

`path_for("panorama", slug)` = `tiles_v2_<slug>/panorama.glb`, ostatní `tiles_v2_<slug>/details/<step>.glb`.

## RUIAN search

Endpoint `/api/ruian/search?q=<text>` v backendu volá **ČÚZK RUIAN MapServer, vrstva 1 (AdresniMisto)**. URL: `https://ags.cuzk.cz/arcgis/rest/services/RUIAN/MapServer/1/query`. (Spike ověřen 2026-05-15 — `Geokoder/GeocodeServer` na ČÚZK serverech neexistuje, žádná dedikovaná address-search služba není dostupná.)

Query parametry (`POST` form-encoded, ne GET — bezpečnější escapování):

```
where=UPPER(adresa) LIKE UPPER('%<escaped_q>%')
outFields=adresa,psc,cislodomovni,kod
outSR=5514
returnGeometry=true
resultRecordCount=10
f=json
```

`UPPER(adresa) LIKE UPPER('%…%')` obchází case-sensitivity. Sanitize: escape `'` → `''` a `%` → `\%` v `q`.

**Diacritics warning:** RUIAN ukládá adresy s diakritikou. Dotaz „Strážek" funguje, „Strazek" vrátí 0 hits. UI při 0 hits zobrazí „Zkus i s diakritikou (např. Strážek místo Strazek)."

Příklad odpovědi (jeden feature):

```json
{
  "attributes": {
    "adresa": "č.p. 136, 78501 Hnojice",
    "psc": 78501,
    "cislodomovni": 136,
    "kod": 12345678
  },
  "geometry": {"x": -547980.76, "y": -1107944.18}
}
```

Backend mapuje na:

```json
{
  "label": "č.p. 136, 78501 Hnojice",
  "sjtsk_cx": -547980.76,
  "sjtsk_cy": -1107944.18,
  "obec": "Hnojice"
}
```

`obec` se extrahuje jako poslední segment po čárce a po PSČ: `adresa.rsplit(',', 1)[-1].strip().split(' ', 1)[1]` → „Hnojice". Slug = `unicodedata.normalize('NFD', obec).encode('ascii', 'ignore').decode().lower()` → `hnojice` / `strazek` (stdlib, žádné nové dependencies).

**Fallback při výpadku ČÚZK:** `/api/ruian/search` vrátí 503 s body `{error: "ČÚZK unavailable"}`. UI ukáže warning + collapsible „Ruční S-JTSK input" (cx,cy textové pole), který obejde search a pošle rovnou `/api/jobs` s ručně zadaným slugem.

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
| gen_panorama.py / gen_detail.py exit ≠ 0 | step `fail`, error = prvních 300 chars stderr + posledních 300 chars stderr (Pythonský traceback má důležitou informaci na obou koncích) |
| Subprocess timeout (>60 min per step) | step `fail`, error = "timeout 60m" (60 min počítá s cold ČÚZK cache — viz níže) |
| User cancel | Worker pošle subprocess.terminate() (SIGTERM), počká 10 s, pak kill (SIGKILL). Step → `cancelled`, loop break. Partial `.glb` zůstanou na disku stejně jako u `fail`. |
| Backend crash mid-job | Při startu `JOBS = {}`; uživatel ve dashboardu vidí `partial` + Retry |

**Cold cache poznámka:** `gen_panorama.py` stahuje DMR5G výškové dlaždice z ČÚZK do `cache/dmpok_tiff_*`. Pro Hnojice už hotové, pro novou lokaci to znamená 5–30 minut sítového stahování navíc před vlastním renderem (závisí na velikosti regionu a ČÚZK propustnosti). Z toho plyne timeout 60 min per step (ne 30 jako MVP odhad) a UX-očekávání v UI: u prvního stepu (panorama) zobrazit „⏳ panorama running… (první lokace v regionu může trvat 10–30 min — stahuje výškové dlaždice z ČÚZK)".

## Logy

Per-step log soubor: `cache/jobs/<job_id>/<step_name>.log` (stdout+stderr subprocesu). UI je nezobrazuje (Q4 = B), ale jsou na disku pro post-mortem debugging. Žádné rotation/cleanup — disk levný, počet jobs malý.

## Testování

**Automatizované pytest testy** (`tests/test_locations_api.py`, ~50 řádků) na threading-citlivá místa, kde manuální test nezachytí race conditions:

1. **`test_enqueue_creates_job`** — POST /api/jobs vrátí job_id, který je v `JOBS` a v `JOB_QUEUE`
2. **`test_two_enqueues_serialize`** — dva POST hned po sobě → druhý dostane queue_position=1, worker je zpracuje sériově (mock subprocess.run vracející okamžitě)
3. **`test_retry_resumes_skip`** — předpřipravený `tiles_v2_test/panorama.glb` na disku → POST /api/jobs → step panorama je `skipped`, ne `running`
4. **`test_slug_collision_ready_409`** — všechny 4 `.glb` existují → POST /api/jobs vrátí 409
5. **`test_slug_collision_partial_200`** — chybí closeup.glb → POST /api/jobs vrátí 200 + job_id
6. **`test_cancel_running`** — mock subprocess.Popen → POST /api/jobs/<id>/cancel → step `cancelled`, worker neblokovaný

Mock pattern: `subprocess.run` patchneme tak, aby vrátil exit=0 + dotkl se očekávaného `.glb` souboru (touch). Testy běží sekvenčně se sdíleným tempdir.

**Manual smoke test** (nezbytný — pytest mockuje subprocess):
1. Spustit server, otevřít `/index.html`
2. Verify dashboard ukáže existující Hnojice jako `ready`
3. Klik [+ Nová lokace], hledat „Šantovka" (s diakritikou!), vybrat, Generovat
4. Sledovat polling: panorama → outer → closeup → inner
5. All ✓ → klik Otevřít viewer → ověřit, že `v2.html?region=santovka` načte
6. **Failure test:** uprostřed generování `kill -9` python subprocesu → ověřit, že step → fail, lokace ukáže `partial` + Retry tlačítko
7. **Retry test:** klik Retry → worker projde, hotové kroky skipne, failed step přespustí
8. **Cancel test:** spustit nový job, hned klik Cancel → ověřit, že subprocess je zabit a step `cancelled`

## Out of scope (YAGNI)

- Delete / archive lokace (žádný odpovídající endpoint, dashboard nepokazí; ruční `rm -rf tiles_v2_<slug>/`)
- Edit existující lokace (parametry, label) — regenerate from scratch
- Streamování stdout logu (logy na disku stačí)
- Quality preset picker (low/medium/high) — fixní Hnojice defaulty
- Advanced LOD form (`--half`, `--step`, `--zoom` per layer)
- Auth (Tailscale-only network)
- Notifikace (web push, e-mail) po dokončení
- Persistence queue přes restart (in-memory acceptable)
- Mobile-specific tweaky (vanilla flexbox + Tailscale + iPad funguje)
- UI tlačítko pro destruktivní rebuild lokace v `ready` stavu (ruční `rm -rf` stačí)

## Soubory, které se změní / přidají

| Soubor | Akce |
|---|---|
| `index.html` | **Nový** — dashboard + create form (~ 400 řádků inline JS+HTML) |
| `server.py` | **Modifikace** — přidat endpointy + worker thread + job state struktury; cca 300 řádků |
| `cache/jobs/` | **Vznikne automaticky** runtime — per-job logy |

Žádné nové python balíčky. Žádný build step.
