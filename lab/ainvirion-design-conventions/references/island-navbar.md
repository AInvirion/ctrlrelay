# Island Navbar

El **island navbar** es el elemento visual distintivo y compartido entre todos los productos AInvirion. Cualquier proyecto con interfaz web bajo este directorio debe adoptarlo como barra de navegacion principal.

## Anatomia

La navbar tiene 4 zonas y dos estados visuales — standard (sin scroll) e island (scrolled):

```
Standard (sin scroll):
┌──────────────────────────────────────────────────────────────────────────┐
│  [Brand]                    [Steps + indicator]  [Links]  [Actions]     │
└──────────────────────────────────────────────────────────────────────────┘

Island (scrolled):
              ┌──────────────────────────────────────────────┐
              │  [Brand] │ [Steps] │ [Links] │ [Actions]     │
              └──────────────────────────────────────────────┘
```

1. **Brand** — Logo (24x24px, `border-radius: 6px`) + nombre del producto. Clase: `.island-brand`
2. **Steps** — Items de navegacion primaria con iconos SVG (16x16) y labels. Incluyen un sliding indicator. Clase: `.island-steps` > `.island-step`
3. **Links** — Navegacion secundaria (texto plano, sin iconos). Clase: `.island-link`
4. **Actions** — Theme toggle y controles utilitarios. Clase: `.island-actions`

Las zonas se separan con dividers verticales (`.island-divider`: 1px x 20px).

## Layout: brand-left / nav-right

En el estado standard (full-width), la navegacion se alinea a la derecha mediante `margin-left: auto` en `.island-steps`, empujando steps, links y actions al extremo derecho mientras el brand queda a la izquierda.

- `.island-steps` lleva `margin-left: auto` — es el primer elemento que se empuja a la derecha
- `.island-actions` **no** lleva `margin-left: auto`
- El divider entre brand y steps se oculta con `.island-brand + .island-divider { display: none }` porque el gap visual grande lo hace innecesario
- En el estado island (`width: fit-content`), el `margin-left: auto` no tiene efecto porque no hay espacio extra dentro de la capsula

## Transicion standard → island

La navbar morphs entre un bar full-width transparente y una capsula flotante compacta al hacer scroll.

**Trigger:** Clase `body.scrolled-past-hero` toggled por JS cuando `scrollY > hero.offsetHeight * 0.6`.

**Estados CSS:**

| Propiedad | Standard | Island |
|-----------|----------|--------|
| `width` | `100%` | `var(--island-scroll-width, fit-content)` |
| `padding` | `4px var(--gutter)` | `4px 6px` |
| `border-radius` | `0` | `2rem` |
| `background` | `transparent` | `var(--island-bg)` / `color-mix(in srgb, var(--surface) 85%, transparent)` |
| `backdrop-filter` | `blur(20px) saturate(1.4)` | `blur(20px) saturate(1.4)` (via `::before`) |
| `border-color` | `transparent` | `var(--island-border-strong)` |
| `box-shadow` | `none` | Multi-layer + glow |
| `margin-top` | `0` | `12px` (via `--island-top-offset`) |
| `::before` opacity | `0` | `1` (capa con fondo semitransparente + blur) |

**Frosted glass en ambos estados:** El `backdrop-filter: blur(20px) saturate(1.4)` se aplica directamente en `.navbar-island` (con prefijo `-webkit-`) en **ambos estados**. En el estado standard, el fondo es `transparent` y el blur actua directamente sobre el contenido detras. En el estado island, el `::before` pseudo-element aporta un fondo mas denso (`color-mix` al 85-90% de `--surface`) por encima del blur.

**Timing:** 400ms para propiedades geometricas/visuales mayores (`width`, `border-radius`, `background`, `border-color`, `box-shadow`, `margin-top`), 300ms para `height` y `padding`. Easing: `cubic-bezier(0.16, 1, 0.3, 1)` (expo-out, almacenado en `--ease-out-expo`).

**Patron JS obligatorio:**

1. Medir el ancho `fit-content` con `getBoundingClientRect().width` (aplicando temporalmente la clase morphed)
2. Guardar el valor en `--island-scroll-width` como custom property en el elemento
3. Anadir clase `navbar--no-transition` durante el setup inicial para evitar flash de transicion
4. Remover `navbar--no-transition` con doble `requestAnimationFrame` para permitir que el layout se estabilice
5. Re-medir en `resize` (debounced a 150ms), re-aplicando el guard de no-transition durante la medicion
6. Scroll listener pasivo (`{ passive: true }`) para toggle de `scrolled-past-hero` en `<body>`

## Tratamiento visual

Los valores siguientes corresponden al **estado island** (scrolled). El estado standard es transparente, full-width y sin forma de capsula (ver tabla en "Transicion standard -> island").

| Propiedad | Valor |
|-----------|-------|
| Forma | `border-radius: 2rem` — capsula completa |
| Ancho | `width: fit-content` — la capsula se dimensiona por contenido, nunca `100%`. Requerido para que el centrado `left: 50%; transform: translateX(-50%)` funcione cuando coexiste con clases de frameworks (ej: DaisyUI `.navbar` aplica `width: 100%`) |
| Altura | `48px`, padding interno `4px 6px` |
| Fondo | `color-mix(in srgb, var(--surface) 90%, transparent)` |
| Blur | `backdrop-filter: blur(20px) saturate(1.4)` |
| Borde | `1px solid var(--border)` |
| Sombra (light) | `0 1px 2px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.06), 0 12px 40px rgba(0,0,0,0.03)` |
| Sombra (dark) | Se agrega glow: `0 0 40px var(--accent-15)` — un halo sutil del color accent |
| Dark fondo | `color-mix(in srgb, var(--surface) 85%, transparent)` con `border-color: var(--border-strong)` |

## Sliding indicator

Barra horizontal de 2px en el borde inferior de los steps, que se desliza al item activo:

- Color: `var(--accent)` con `box-shadow: 0 0 8px var(--accent-30)`
- Transicion: `left 300ms, width 300ms, opacity 300ms` con `var(--ease-out)`
- Controlado por scroll — passive `IntersectionObserver` o scroll listener que detecta que seccion es visible
- **Estado null**: cuando ninguna seccion es visible, el indicator hace fade out (`opacity: 0`)

## Entrada animada

```css
@keyframes island-entrance {
    from { opacity: 0; transform: translateX(-50%) scale(0.95); }
    to   { opacity: 1; transform: translateX(-50%) scale(1); }
}
```

La navbar entra con `animation: island-entrance 0.6s var(--ease-out) both`. Respetar `prefers-reduced-motion` deshabilitando esta animacion.

## Principios de navegacion

- **Todas las secciones de la pagina deben ser accesibles desde el navbar**, tanto en desktop como en mobile. Nunca ocultar secciones sin ofrecer un camino alternativo para llegar a ellas.
- **El island debe quedar centrado horizontalmente en la pantalla** en todos los viewports. La capsula no se estira al ancho completo; se ajusta al contenido y flota centrada.
- **Brand a la izquierda, navegacion a la derecha** en el estado standard (full-width). El `margin-left: auto` en `.island-steps` crea la separacion. En el estado island, todo queda empacado dentro de la capsula.

## Responsive

2 modos de layout — desktop con navegacion inline, mobile con hamburger expandible. Ambos soportan la transicion standard → island:

| Breakpoint | Standard (sin scroll) | Island (scrolled) |
|------------|----------------------|-------------------|
| `>= 769px` | Brand izq, Steps+Links+Actions der (full-width) | Capsula centrada con todo empacado |
| `<= 768px` | Brand + Hamburger + CTA (space-between, full-width) | Igual pero con forma capsula |

**Desktop (`>= 769px`):** Todas las zonas visibles inline. Los steps incluyen sliding indicator controlado por scroll.

**Mobile (`<= 768px`):** La capsula muestra solo Brand, hamburger y CTA (ej: Contact). Al tocar el hamburger, un menu full-width se despliega debajo de la capsula como una burbuja independiente — `position: absolute`, `width: calc(100vw - 2rem)`, centrado horizontalmente con margen lateral de `1rem`. El menu conserva el tratamiento visual de la capsula (blur, borde, sombra, border-radius). Clases: `.island-hamburger`, `.island-menu`, `.island-menu-item`. La apertura se controla con `.island-open` en `.navbar-island`.

- La capsula compacta queda centrada horizontalmente y usa `justify-content: space-between` para dispersar Brand, hamburger y CTA.
- `.island-actions` usa `display: contents` en mobile para promover hamburger y CTA como flex children directos.
- Los `.island-menu-item` usan `font-size: 1rem` y `padding: 14px 20px` para mejorar legibilidad y touch targets en pantallas tactiles.
- Los menu items entran con animacion staggered (`island-item-enter`) respetando `prefers-reduced-motion`.

## Accesibilidad

- Envolver en `<nav>` con `aria-label` descriptivo (ej: `"Navegacion principal"`)
- Steps como `<a>` o `<button>` — nunca `<div>` con click handlers
- `aria-hidden="true"` en iconos decorativos y dividers
- `prefers-reduced-motion: reduce` desactiva animaciones de entrada e indicator
- Focus visible en todos los items interactivos

## Morphing contextual

La navbar puede transformarse segun el estado de la aplicacion (ej: contraer steps cuando hay resultados visibles). Esto se logra con clases en `<body>` (ej: `body.has-result .island-steps { opacity: 0; width: 0; }`) y transiciones CSS.
