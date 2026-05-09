# THREE.js color space gotcha — sRGB textures look washed out by default

## TL;DR

Když ti v THREE.js scéně **ortofoto / fotky vypadají vybledlé, ploché, bez kontrastu** (i když originální JPG je krásně saturovaný a v Leafletu / `<img>` se zobrazí správně) — pravděpodobně jsi zapomněl na:

```js
texture.colorSpace = THREE.SRGBColorSpace;
```

Tenhle řádek patří **ihned poté, co dostaneš `texture` objekt** (z `TextureLoader.load`, `new THREE.CanvasTexture(...)`, `new THREE.Texture(image)`), **před** jeho použitím v materiálu.

## Proč to vadí

JPG, PNG, WebP, JPEG2000, TIFF — všechny standardní obrazové formáty kódují pixely v **sRGB color space** s gamma ≈ 2.2 (perceptuálně lineární vůči lidskému oku). To je proč obyčejný `<img>` v prohlížeči nebo `image.png` v IDE vypadá správně — browser/OS ví, že to je sRGB a aplikuje display gamma.

THREE.js od r152 zacházel s color management formálně:

- **Default `texture.colorSpace`** = `NoColorSpace` (od r152) nebo `LinearSRGBColorSpace` (starší/výchozí v některých configs).
- **Default `renderer.outputColorSpace`** = `SRGBColorSpace` (od r152, `LinearSRGBColorSpace` v r150 a starších).

Co dělá pipeline když textura má NoColorSpace:

1. JPG načten, GPU má sRGB-encoded byte hodnoty (= šedý pixel cca 128, ne 188 jako by byl v lineáru)
2. THREE čte texturu s `colorSpace = NoColorSpace` → **interpretuje hodnoty jako kdyby už byly lineární**
3. Render výstup s `outputColorSpace = SRGBColorSpace` → renderer aplikuje gamma encoding na hodnoty co považuje za lineární
4. Display ukáže texturu — vypadá **vybledle**, protože gamma byl aplikován jen jednou (na výstupu) místo dvakrát (decode + encode = identita)

Když nastavíš `texture.colorSpace = SRGBColorSpace`:

1. JPG načten s sRGB-encoded hodnotami
2. THREE ví, že je to sRGB → **dekóduje na lineární** před použitím v shaderech (nebo poznamená sRGB při čtení v fragment shaderu)
3. Veškeré výpočty (lighting, blending) probíhají v lineárním prostoru — fyzikálně korektní
4. Renderer enkóduje výstup zpět do sRGB pro display
5. Display ukáže texturu se správnou saturací a kontrastem

## Kde se to projeví v tomhle projektu

- `hnojice_multi.html` — `texLoader.load(ortUrl, ...)` pro ortofoto + cadastre overlay (5 + 1 callbacků)
- `gen_multitile.py` template — stejné callbacky pro per-location vieweri (5 v template)
- `inspector.html` — `loadOrthoTexture` (commit `9c09ddb`) **už má** `tex.colorSpace = SRGBColorSpace` od počátku, protože ten kód jsem psal později a věděl o tom

## Pravidlo pro budoucnost

**Vždycky když vytváříš `THREE.Texture` z JPG/PNG souboru (lokálního nebo stáhnutého):**

```js
texture.colorSpace = THREE.SRGBColorSpace;
```

Výjimky:

- **Normal map / depth / data texture** — drží 0..1 nebo signed hodnoty, ne barvy. Color space NoColorSpace nebo LinearSRGBColorSpace.
- **HDR / EXR / RGBE** — to je už lineární HDR data, ne sRGB. Použij `LinearSRGBColorSpace`.
- **Procedurálně generovaná `CanvasTexture` co kreslí matematické vzorce** — záleží jak ji kreslíš; pokud jsou to RGB barvy z `fillStyle = '#abc'`, jsou sRGB, takže **ANO** dej SRGBColorSpace.
- **HUD / UI textury z 2D canvasu s textem** — sRGB (jsou to barvy), takže **ANO** SRGBColorSpace.

## Související: `renderer.outputColorSpace`

Default v r170 je už správný (`THREE.SRGBColorSpace`). Pokud explicitně nastavíš na `LinearSRGBColorSpace`, output bude lineární a vypadá vybledle bez ohledu na nastavení textur. Nedělej to, jen kdybys postprocessoval výstup vlastním shaderem co dělá vlastní sRGB encoding.

## Související: tonemapping

`renderer.toneMapping = THREE.NoToneMapping` (default v r170) — barvy projdou tak jak jsou, jen s sRGB enkódováním na konci. Pro HDR scény / fyzicky korektní lighting můžeš přepnout na ACES, ReinhartFilmic atd. — ty zase posunou jas a saturaci podle algoritmu. Pro ortofoto realitní marketing → drž `NoToneMapping`.

## Reference debug commit

V tomhle repu byl bug s vybledlým ortofotem komitnutý jako `6957639` (fix). Diff ukazuje minimální 1-řádkovou změnu na callback + odůvodnění v commit message.

## Externí odkazy

- THREE.js r152 release notes — color management overhaul: https://github.com/mrdoob/three.js/wiki/Updated-color-management-in-three.js-r152
- THREE.js docs — Texture.colorSpace: https://threejs.org/docs/#api/en/textures/Texture.colorSpace
