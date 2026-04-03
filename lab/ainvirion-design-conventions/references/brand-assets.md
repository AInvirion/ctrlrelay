# Brand Assets

Logo and visual asset guidelines for AInvirion products.

## General rules

- **Footer**: use logotype (horizontal, text + icon). Preferred format: SVG
- **Favicon / apple-touch-icon**: use square icon. Format: PNG 180x180
- **Navbar**: icon 24x24px with `border-radius: 6px`
- Copyright always references **AInvirion LLC**, regardless of product branding
- Self-hosted WOFF2 for fonts (no Google Fonts CDN in production)

## Assets by product

| Product | Footer logo | Favicon | Location (relative to repo root) |
|---------|-----------|---------|----------------------------------|
| **ainvirion_com** | `logotype.svg` | `logo.png` | `ainvirion_com/assets/images/` |
| **yt2txt** | `logo.png` + `wordmark.svg` | `logo.png` | `youtube-transcriber/site/static/images/` |
| **AIProxyGuard** | Inline SVG with `.logo-highlight` | `favicon.svg` | `aiproxyguard/web/assets/` |
| **AgentJunior** | (SaaS app, no separate landing) | — | `AgentJunior/public/` |
| **Cliquey** | Text + Bootstrap icon | — | `cliquey/static/` |

## Shared typography

| Role | Font | Fallback |
|------|------|----------|
| Display / headings | Space Grotesk or IBM Plex Sans | system-ui, sans-serif |
| Body | IBM Plex Sans or Satoshi | system-ui, sans-serif |
| Mono / code | JetBrains Mono | ui-monospace, monospace |

Each product may choose from approved fonts. The combination must be declared via tokens `--font-display`, `--font-body`, `--font-mono`.
