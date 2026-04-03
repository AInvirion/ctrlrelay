---
name: ainvirion-design-conventions
description: >
  Use when building, modifying, or reviewing a web interface for any AInvirion
  product landing. Covers the island navbar, two-tier footer, brand assets,
  support links, token architecture, and visual identity shared across all
  products. Also triggers on: navbar, footer, dark mode, frosted glass,
  scroll morph, capsule nav, island transition, AInvirion UI, landing page,
  support link, legal, copyright, i18n, internationalization, language.
---

# AInvirion Design Conventions

Shared UI conventions for all AInvirion product landings. Read the relevant reference before implementing.

| Component | Reference |
|-----------|-----------|
| Island Navbar | `references/island-navbar.md` |
| Footer Two-Tier | `references/footer-two-tier.md` |
| Brand Assets | `references/brand-assets.md` |
| Support & Legal | `references/support-and-legal.md` |
| Token Architecture | `references/token-architecture.md` |
| Internationalization | `references/i18n.md` |

## Quick rules

- **Tokens everywhere**: zero magic numbers — every color, spacing, font-size, easing, and duration must be a CSS custom property (see `references/token-architecture.md`)
- **Color derivation**: use `color-mix(in srgb, ...)` over `rgba()` for theme-derived colors
- **Dark backgrounds**: creative textures, never grids or checkered patterns
- **No aurora blurs**: do not use large blurred radial gradients drifting with `will-change: transform` as hero backgrounds — this is a well-known AI slop fingerprint (2024-2025). Use solid gradients, subtle radial vignettes, or no background effect at all
- **Per-product palettes allowed**: each product defines its own `--accent` and palette, but token names must match the shared semantic set
- **Footer congruence mandatory**: copyright, legal links, and support URL are immutable across products (see `references/support-and-legal.md`)
- **i18n required**: minimum English + Spanish, English as default (see `references/i18n.md`)

## Brand & legal (quick ref)

- **Company**: AInvirion · Seattle, USA · Santiago, Chile
- **Support**: `https://github.com/AInvirion/support/issues/new/choose`
- **Phone**: (425) 276-7365
- **Copyright**: `© {year} AInvirion | Seattle, USA · Santiago, Chile`
- **All contact CTAs** → GitHub Issues (never email or custom forms)

## Canonical implementation

`ainvirion_com/` is the reference implementation for island navbar and footer. Consult:
- `ainvirion_com/assets/css/styles.css` — full styles
- `ainvirion_com/index.html` — HTML structure
- `ainvirion_com/assets/js/app.js` — scroll morph logic and scroll indicator
