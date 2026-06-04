# Proč heightfield pyramid — decision log

Zapsaný myšlenkový proces, který vedl k volbě heightmap pyramidy pro „3D mapa nemovitostí pro celou ČR". Cíl tohoto dokumentu je *ne* obhájit volbu, ale zachovat **honest critique vlastních argumentů + reframing který volbu validoval**, aby budoucí čtenář (vy za rok, nebo někdo jiný) viděl pod kapotu rozhodnutí — nejen výsledek.

## Cíl

> **„Datově levné a rychlé řešení generování 3D mapy nemovitostí pro celou ČR."**

Rozklad:

- **Datově levné** = minimum GB na disku, minimum bandwidth per session, minimum infrastruktury
- **Rychlé** = krátká per-listing latence (od kliknutí po viditelnou scénu), krátký one-time setup
- **3D mapa nemovitostí** = uživatel zadá adresu → vidí teren + budovy + textury z ortofota
- **Pro celou ČR** = libovolný bod CZ musí jít zobrazit, bez white-listu lokací

## Honest critique předchozí argumentace

Při návrhu HEIGHTFIELD_PYRAMID.md jsem dělal několik chyb v rozhodovacím procesu, které stojí za zaznamenání aby se neopakovaly:

### 1. Confirmation bias k extension stávajícího kódu

`heightfield/index.html` už používá LERC + KTX2 + vertex shader displacement. „Rozšíření" znamenalo 1–2 týdny vývoje místo 2–4 týdnů pro mesh pyramid. To je výhoda **pro implementátora**, ne nutně pro produkt. Když jsem argumentoval „heightmap vyhrává protože reuse" — byl to argument pro path of least resistance, ne pro nejlepší architekturu.

**Lesson:** „Správná architektura" závisí na use-case, ne na tom co je už hotové. Reuse je faktor, ale ne primární metrika.

### 2. Strawman srovnání s mesh přístupem

Tvrdil jsem „mesh = 1–5 TB pro ČR", ale to byla **extrapolace `gen_detail.py` rozšířeného naivně na ČR** (inner ring 1 km² všude = 51 MB × 78 866 km²). To nikdo seriózně nenavrhuje.

Praktická mesh alternativa by byla:
- **Closeup-only ALL CR** (non-overlapping 3×3 km tiles) = ~460 GB — *srovnatelně s heightmap pyramidou*
- **Outer + closeup ALL CR jako base + inner on-demand** = ~600 GB

Realistický rozhodovací prostor byl `370 GB heightmap vs ~460 GB mesh closeup-everywhere`, ne `370 GB vs 1–5 TB`. Tím jsem mu rozhodnutí udělal **falešně jednodušší**.

**Lesson:** Při srovnání alternativ vždy uvádět *realistickou* variantu konkurence, ne worst-case strawman.

### 3. Cherry-picked per-tile bandwidth

Tvrdil jsem 30× výhru heightmap (12 KB vs 1 MB per tile). Ale to je **per-tile**, ne **per-scene**. Mesh-based potřebuje menší počet větších tilů; total initial load může být srovnatelný (~200 KB heightmap vs ~4 MB mesh). 10× ano, ne 30×. A s CDN cache je to často jedno.

**Lesson:** Bandwidth čísla uvádět jako „per session" / „per first-load" / „per pan-step", ne izolovaná per-tile.

### 4. Nedostatečně silné varování o urban architektuře

V downsides jsem napsal „heightmap neumí caves/overhangs" ale dodal „real-estate ne v jeskyni". Reálně:

- Praha 1, Brno-střed, Olomouc — historická centra **mají hodně arkád, podloubí, balkonů, mostů**
- Vrstvený byt s loggií → heightmap to zploští
- Pohled na ulici pod podloubím → půjde to skrz strop

Pokud cíl je urban real-estate, tohle je vážnější red flag než jsem prezentoval.

**Lesson:** Limitace formátu hodnotit proti **konkrétnímu use-case**, ne v abstraktu.

### 5. Nezpochybnil jsem zda pyramid vůbec potřebujete

Toto je nejdůležitější bod, který jsem nikdy nepoložil:

Aktuální workflow je **per-location generation** s 17 min latencí. Reálná bolest je ČÚZK rate-limit (5 min retry kaskáda). Řešení = **lokální DMPOK cache + cache refactor**. Per-listing latence padne na ~10 min, **bez ČR pyramidy**.

Pyramid je úplně jiný produkt — „instant 3D scéna pro libovolný bod ČR". Ten use-case je validní jen pokud existuje konkrétní UX scénář.

**Lesson:** Vždy se ptát „řešíme aktuální bolest, nebo budujeme nový feature?"

## Reframing, který volbu validoval

Uživatel reagoval: *„me nevedi, ze jeden pixel jeden bod, to je ok, klidne pokd existuje takove zjedodiswni v mesh tak jse ok, ho vyzkouset, me jde o datove levne a rychle reseni generovani 3d mapy nemovitosti pro celou cr"*

Z toho plyne:

1. **„Jeden pixel jeden bod" simplifikace je přijatelná.** Tj. urban overhangs nejsou dealbreaker pro tento produkt.
2. **Datově levné** je primary criteria.
3. **Rychlé generování** je secondary criteria.
4. **Celá ČR coverage** je nutná podmínka.

## Proč pro tato kritéria heightmap vyhrává

Při akceptovaném zjednodušení „jeden pixel jeden bod" se mesh redukuje na **regular grid mesh** (každý vertex odpovídá jednomu pixelu heightmapy). To je strukturálně to samé co heightmap, jen jinak zabalené:

| Aspekt | Heightmap (LERC) | Regular-grid mesh „1:1 s heightmap" |
|---|---|---|
| Stejný povrch? | ano | ano |
| Caves/overhangs | ne | ne (stejné omezení) |
| Storage 1 km² @ 0,5 m | 2 MB | 30 MB (15× víc) |
| Encoding CPU | rychlé (LERC stream) | pomalé (Draco quantization + mesh build) |
| Decoding CPU | rychlé (LERC unpack + texture upload) | rychlé (GLB parse + GPU upload) |
| Render CPU/GPU per frame | per-vertex displacement v shaderu | baked, zero compute |
| Standards compat | custom | glTF / Cesium |

**Při „jeden pixel jeden bod" mesh ztrácí jediný argument** který by ho odlišil (overhangs, baked normals adaptive geometry). Co zbývá:

- Mesh stojí 15× víc bajtů za stejnou informaci
- Mesh trvá déle generovat (Draco quantization je drahý)
- Mesh má lepší ekosystémovou kompatibilitu (Cesium, glTF tooling)

Pokud cíl je „datově levné a rychlé", **heightmap je strictly lepší volba**. Ekosystémový bonus mesh se nedá converted na úspory v core kritériích.

Mesh získává výhodu **pouze** s adaptive simplification (decimate flat areas) — ale tu uživatel explicitně neakceptuje, jak vyplývá z „jeden pixel jeden bod is OK". Tj. adaptive mesh je „mimo design space" pro tento produkt.

## Test, který validuje volbu empiricky

Existující data umožňují přímé side-by-side srovnání **bez dalšího kódu**:

| Stejná Hnojice scéna, dva renderery | Soubor(y) | Velikost |
|---|---|---|
| Mesh přístup (`v2.html?region=hnojice`) | `tiles_v2_hnojice/details/inner.glb` | 51 MB |
| Heightmap přístup (`heightfield/?slug=hnojice`) | `inner_heightmap.lerc` + `inner_ortho_ultra.ktx2` | 2 + 10 = 12 MB |

Pokud heightfield viewer vypadá pro vás *vizuálně srovnatelně* (nebo lépe díky per-pixel normals), pak **rozhodnutí je empirické**: heightmap je 4× menší a vizuálně postačuje. Pyramid je extension téhle techniky.

Pokud heightfield viewer vypadá *vizuálně horší* (jemné stínování, ostré hrany budov atd.), pak pyramid postavený na téhle technice taky bude horší. Test je kritická validace **před** commitnutím 1–2 týdnů na pyramid implementaci.

**Akce: otevřít `/heightfield/?slug=hnojice` na iPadu, posoudit vizuální kvalitu pro real-estate kontext.**

## Cheap + fast plán

Pokud test prošel (heightfield kvalita je akceptovatelná), plán je tříkrokový:

### Krok 1 — Bulk DMPOK (probíhá)

Stav: stahuje se, ~3 noci do konce. Storage: 1,14 TB na Elements.

Tím získáte zdroj výškových dat pro libovolný bod ČR. Tohle je společný předpoklad pro všechny varianty (per-location pipeline, pyramid, hybrid).

### Krok 2 — Cache refactor pro per-location pipeline (~1 den vývoje)

Sjednotit `INZERATOR_CACHE` env honoring přes `gen_detail.py`, `download_tiff.py`, `locations.py`. Tím:

- Per-location generování přestane fetchovat ČÚZK
- Per-listing latence padne ze 17 min na ~10 min (CPU-bound, deterministická)
- Žádný downtime když je ČÚZK rate-limit

Tohle samostatně **už řeší aktuální bolest**. Pokud goal je „pomoct stávajícímu workflow", krok 3 není nutný.

### Krok 3 — Heightmap pyramid (~1–2 týdny vývoje + 42 h compute)

Postavit ČR-wide pyramid podle HEIGHTFIELD_PYRAMID.md:

- Storage: 370 GB pyramid + (společných) 1,14 TB DMPOK = 1,5 TB
- One-time setup: 42 h compute (single machine)
- Per-listing: instant zobrazení (žádné per-listing generování pro view)
- HD detail volitelně na pozadí: existující inner ring pipeline na lazy load (10 min)

Tím se cíl „3D mapa nemovitostí pro celou ČR" splní v plné šíři:

| User journey | Bez pyramid | S pyramid |
|---|---|---|
| Klik na známou adresu (cached) | instant | instant |
| Klik na novou adresu | 10 min wait | **instant** (pyramid view) + 10 min lazy HD generation |
| Browse mapa, zoom out, hover | nedostupné | **instant** plynulé |
| Sledovat 10 nemovitostí za 5 min | 50 min cumulative wait | **30 sec** cumulative |

## Datový rozpočet shrnutí

| Komponenta | Velikost | Generace | Účel |
|---|---|---|---|
| DMPOK bulk cache | 1,14 TB | 3 nochi (probíhá) | source data |
| Heightmap pyramid | 370 GB | 42 h compute (one-time) | instant browse |
| Per-listing inner GLB cache | 30 MB × inzerát | 10 min per inzerát (lazy) | HD detail |
| **Total při 100k inzerátů** | **1,5 TB + 3 TB** | | |
| **Total při 10k inzerátů** | **1,5 TB + 300 GB** | | |

Elements 9,1 TiB pojme **až ~250k inzerátů** v plné kvalitě, plus pyramid + DMPOK. Storage scaling není problém.

## Final verdict

**Heightmap pyramid je správná volba** pro stated cíl „datově levné + rychlé + ČR coverage", **za předpokladu že:**

1. Side-by-side test (`heightfield/?slug=hnojice` vs `v2.html?region=hnojice`) ukáže akceptovatelnou vizuální kvalitu heightmap přístupu
2. Use-case nezahrnuje urban architekturu kde overhangs jsou estetická priorita (Praha 1, Brno-střed historic)
3. Akceptujeme custom viewer stack jako asset (ne potřebu interop s Cesium / QGIS / ArcGIS)

Pokud kterákoliv z těchto podmínek selže, návrat k vyhodnocení:
- Pro urban: mesh-based s adaptive simplification (2–4 týdny nový pipeline, ~200 GB)
- Pro standards interop: Cesium 3D Tiles export (3–4 týdny refactor klienta)
- Pro „jen offline aktuální workflow": stop u kroku 2 (cache refactor), pyramid ignore

**Nejlepší další akce: pustit empirický test na iPadu.** Bez něho je pyramid commitment založený na inferenci, ne na pozorování.

## Co jsem se naučil o vlastním rozhodování během této konverzace

Pro budoucí design dialogues s vámi:

1. **Když uvedu range (např. „200 GB – 1 TB"), pojmenovat která koncová hodnota odpovídá které variantě.** Range bez kontextu je rétorický trik, ne informace.

2. **Reuse existujícího kódu citovat jako tradeoff, ne jako primary argument.** Path of least resistance má skrytou cenu (lock-in do stávající architektury), kterou musím explicitně přiznat.

3. **Vždy se ptát „jakou bolest tohle řeší?" před tím než vymýšlím větší řešení.** Pokud bolest je „existující workflow je pomalý", small refactor je lepší než pyramid. Pokud bolest je „chybí browse-CR feature", pyramid je odůvodněný.

4. **Side-by-side test existujících artefaktů > abstraktní debata.** Inzerator má hotové scény oběma způsoby — používat to.

5. **Akceptovaná simplifikace mění design space.** Když user řekne „jeden pixel jeden bod is OK", odpadají argumenty které tu komplexitu obhajují. Mé argumenty „mesh umí overhangs" se v tu chvíli stávají irrelevant a měly by se z analýzy okamžitě odstranit.
