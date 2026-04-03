# Footer Two-Tier

Convencion unificada de footer para todos los productos AInvirion. Dos filas: superior con brand + nav + contacto, inferior con copyright/direccion + links legales.

## Anatomia HTML

```html
<footer class="site-footer" role="contentinfo">
  <div class="container">
    <div class="footer-top">
      <div class="footer-brand">
        <img src="/assets/images/logotype.png" alt="AInvirion" class="logo-img logo-img--footer">
        <p class="footer-tagline">[Tagline del producto]</p>
      </div>
      <nav class="footer-nav" aria-label="Footer navigation">
        <a href="#">Link 1</a>
        <a href="#">Link 2</a>
      </nav>
      <div class="footer-contact">
        <a href="https://github.com/AInvirion/support/issues/new/choose" target="_blank" rel="noopener noreferrer">Get support</a>
        <span class="footer-phone">(425) 276-7365</span>
      </div>
    </div>
    <div class="footer-bottom">
      <p class="footer-legal">&copy; 2026 AInvirion LLC &middot; Spokane, WA</p>
      <nav class="footer-legal-links" aria-label="Legal">
        <a href="#">Privacy Policy</a>
        <a href="#">Terms of Service</a>
      </nav>
    </div>
  </div>
</footer>
```

## Zonas

| Zona | Clase | Contenido |
|------|-------|-----------|
| **Brand** | `.footer-brand` | Logo + tagline del producto |
| **Nav** | `.footer-nav` | Links contextuales (Features, Pricing, FAQ, etc.) |
| **Contact** | `.footer-contact` | Link "Get support" → GitHub Issues + teléfono |
| **Legal** | `.footer-legal` | Copyright + ciudad |
| **Legal Links** | `.footer-legal-links` | Privacy Policy, Terms of Service |

## Reglas de diseno

- **Separador:** `border-top: 1px solid var(--border)` entre contenido y footer, y entre fila superior e inferior
- **Tipografia:** `font-size` pequeno (equivalente a `--fs-xs` o `0.8rem`), color muted para textos informativos
- **Links:** Color `--text-secondary`, hover a `--text` o `--accent`
- **Layout:** `.footer-top` es flex row con `justify-content: space-between`, wrap en mobile
- **Mobile (<=768px):** Las 3 zonas de `.footer-top` stack verticalmente; `.footer-bottom` tambien stack con centrado
- **Spacing:** Padding vertical `var(--space-xl)` top, `var(--space-lg)` bottom

## Elementos opcionales

- Cada producto decide si mostrar logo propio o AInvirion, pero el copyright siempre referencia AInvirion LLC
- `.footer-contact` puede omitirse en productos simples; copyright + legal links son obligatorios
- Links legales apuntan a `#` (placeholder) hasta que se creen las paginas
