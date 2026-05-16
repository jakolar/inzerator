# Ortofoto color grading — Lightroom-style preset

Idea zaznamenaná **2026-05-16**, zatím **neimplementováno**. Cíl: dát surovým ČÚZK
JPEGům real-estate-friendly "look" (teplejší tóny, vyšší clarity/dehaze,
zvednutý vibrance), aby viewer scéna nepůsobila ploše a šedivě jako raw orto.

## Cílový preset (Lightroom Classic ACR scale)

White balance:
- Temperature: +3 až +8
- Tint: +2 až +6 (směrem do magenty)

Exposure:
- Exposure: 0 až +0.15
- Contrast: +10 až +18
- Highlights: -10 až -25
- Shadows: +8 až +18
- Whites: +5 až +15
- Blacks: -8 až -18

Presence / clarity:
- Texture: -5 až 0
- Clarity: +8 až +18
- Dehaze: +4 až +10

Color:
- Vibrance: +18 až +30
- Saturation: +3 až +8

## Možné cesty implementace

### A) Server-side v [`download_ortofoto.py`](../../download_ortofoto.py) — **preferred**

Po stažení JPEGu pustit Pillow + numpy pipeline, uložit upravený JPEG zpět
do `cache/ortofoto_<CODE>/`. Lightroom slidery → vlastní implementace:

| Slider | Approximace |
|---|---|
| Exposure | `img *= 2 ** exposure_stops` |
| Contrast | S-curve kolem 0.5 (sigmoid nebo cubic) |
| Highlights / Shadows | piecewise gama na luma masce (highlights mask = pow(luma, k)) |
| Whites / Blacks | shift černého / bílého bodu (linear remap endpoints) |
| Temperature | shift R/B kanálů (R += k, B -= k v sRGB nebo přes Kelvin LUT) |
| Tint | shift G kanálu (G -= k pro magenta) |
| Clarity | unsharp mask na luminanci (gaussian blur σ=20-40 px, blend back) |
| Dehaze | dark-channel prior nebo zjednodušeně kontrast + sat boost v tmavém channelu |
| Vibrance | saturation s váhou `(1 - existing_saturation)` — boost jen málo-saturovaných pixelů |
| Saturation | HSL S kanál × multiplier |
| Texture | mid-frequency unsharp (menší kernel než clarity, σ=2-5 px) |

Většina operací = 5-15 řádků Python s `np.clip` a `scipy.ndimage`.

**Výhody:** zapečeno do JPEGu, nula GPU cost při render, menší velikost po
recompressu (po tone-curve má JPEG méně highlight detailu k uložení), cache-able,
stejný look na všech zařízeních.

**Nevýhody:** změna preset hodnot → přegenerovat všechny tiles (smazat
`cache/ortofoto_*` a `.sm5_ok` sentinel, retrigger sm5 step). Není live
tweakable.

### B) Client-side shader v `v2.html` / `hnojice_multi.html`

`MeshBasicMaterial.onBeforeCompile` injectne tone-curve do fragment shaderu,
parametry přes `uniforms`. UI sliders → live preview.

**Výhody:** real-time tweak bez redownloadu, lze A/B porovnání, různý preset
per-location.

**Nevýhody:** každý frame, každý tile fragment shader navíc (inner mesh
4M vertexů — fragment-level OK na desktopu, na mobilu už možná spike). Clarity/dehaze
vyžadují více-průchodový filtr nebo pre-pass texture, což znamená extra render
target — netriviální v current viewer. Texture stretch po unsharp masku v shader
limituje σ na jednotky pixelů (větší kernel → spike v ms/frame).

## Doporučená cesta

**A** — values jsou už zakotvené v rangu (fixní preset, ne uživatelský slider),
takže shader-side tweak nic nepřidá, jen sežere GPU. Pokud později vznikne
požadavek "per-location auto-tuning podle histogramu" nebo "uživatel chce vidět
raw vs. graded", lze přidat **A + B**: server bake jako default, shader override
zapnutelný query flagem (`?grade=off`, `?grade=warm`).

## Otevřené otázky před implementací

1. **Které slidery jsou priorita?** Všechny, nebo jen 4-5 nejviditelnějších
   (exposure, contrast, vibrance, dehaze, shadows)? Méně sliderů = jednodušší
   implementace, méně riziko že přefiltrujeme detail v meshi.
2. **Fixní hodnoty nebo per-tile auto-tune?** Použít uprostřed rangů
   (např. temp +5, contrast +14, shadows +13, vibrance +24), nebo měřit
   histogram per-tile a adaptivně? Adaptivní = lépe konzistentní napříč
   lokacemi s různým osvětlením (les vs. pole), ale složitější.
3. **Retroaktivně na existující cache?** Nově stažené JPEGy mají preset
   aplikovaný, ale `cache/ortofoto_<CODE>/*.jpg` z dřívějška ne. Buď:
   (a) flag `--regrade` v sm5 step který znovuprojde celý cache,
   (b) sentinel `.grade_v1` per directory který trigger-uje regrade při
   mismatch verze,
   (c) nic — staré tiles zůstanou raw, nové budou graded (visible inconsistency
   mezi lokacemi).
4. **Reversibility?** Originál JPEG před přepsáním zachovat v
   `cache/ortofoto_<CODE>/_orig/`? Cena = 2× disk pro orto cache (~hundreds MB
   per location).
5. **Per-channel kvalita JPEG re-encode?** Pillow default je quality=75; pro
   re-save po grade by mělo být ≥ 90 aby další generace nedegradovala detail.

## Kde to v pipeline natáhnout

```
locations._do_sm5_download
  └── after download_ortofoto.download(code, cache_root)
        └── NEW: apply_orto_grade(cache_root / f"ortofoto_{code}", preset)
              └── pillow load → numpy → 11 operations → save back
```

Případně před touch `.sm5_ok` sentinel — pokud grade selže, sentinel se
nenastaví a retry to dokončí.

## Reference

- Lightroom Classic adjustment math: <https://github.com/dvbuntu/lightroom-color-math>
  (3rd-party reverse-engineering, ne oficiální od Adobe)
- Dark-channel prior dehaze: He et al. 2009 "Single Image Haze Removal Using
  Dark Channel Prior"
- Vibrance vs. Saturation distinction: GIMP source `app/operations/gimpoperationhuesaturation.c`
- `cache/ortofoto_*` layout: viz [`docs/notes/2026-05-16-pipeline-overview.md`](./2026-05-16-pipeline-overview.md) §2.2
