# E-mail pro ČÚZK podporu

**Komu:** podpora@cuzk.cz
**Předmět:** Informace o plánovaném bulk downloadu otevřených dat (DMPOK)

---

Dobrý den,

obracím se na Vás s informací o plánovaném hromadném stažení otevřených dat ČÚZK
pro účely interního zpracování v rámci projektu vizualizace terénu České
republiky.

## O projektu

Vyvíjím webovou aplikaci pro 3D vizualizaci českého terénu s pokrytím celé ČR
(LERC výškové mapy v prohlížeči). Data ČÚZK jsou pro projekt primárním zdrojem
výškové informace.

## Plánovaný rozsah

| Datová sada | Endpoint | Předpokládaný objem |
|---|---|---|
| DMPOK TIFF | `openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514/` | ~880 GB (~12 600 mapových listů) |

Celkový download ~880 GB, předpokládaná doba 1–3 dny.

## Technické ohledy

Stahování bude probíhat slušně, aby nezatížilo Vaši infrastrukturu:

- **Maximálně 4–8 paralelních spojení** z jediné IP
- **Pauzy mezi requesty** (0,5–2 s na ATOM endpointu)
- **Identifikující User-Agent** (`<název-projektu>/1.0; <kontaktní e-mail>`)
- **Restart-friendly** stahovač — listy, které jsou již lokálně přítomné, se
  přeskakují (po případném výpadku není nutné začínat od nuly)
- **Exponential backoff** při HTTP 5xx odpovědích

## Licence a attribution

Data budou používána v souladu s licencí **CC BY 4.0**. Ve výsledné aplikaci
bude jasně viditelná attribuce v podobě:

> Mapová data © ČÚZK — otevřená data dle CC BY 4.0

odkazující na zdroj a text licence.

## Dotazy

Pokud máte preferovaný čas, kdy bulk download nevadí Vaší infrastruktuře
(např. mimo pracovní hodiny / o víkendu), rád se podle toho zařídím. Stejně tak
pokud existuje bulk-friendly mirror nebo jiný způsob distribuce, který by byl
pro Vás příznivější, ocením doporučení.

Pokud nebude reakce vnímána jako problematická, plánuji začít se stahováním
přibližně **<DOPLNIT DATUM>**.

Děkuji za otevřená data a za případnou zpětnou vazbu.

S pozdravem,
**<DOPLNIT JMÉNO>**
<DOPLNIT E-MAIL>
<DOPLNIT TELEFON nebo organizaci, pokud relevantní>
