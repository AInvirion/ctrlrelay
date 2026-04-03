# Token Architecture

CSS custom property system for AInvirion products. Each product defines its own palette but must use the same semantic token names.

## Core rule

**Zero magic numbers.** Every reusable value (color, spacing, font-size, easing, duration) must be a CSS custom property. Never hardcode `#2563EB` or `16px` directly in rules.

## Color tokens (required)

Every product must define at least these tokens in `:root` or `[data-theme]`:

| Token | Purpose |
|-------|---------|
| `--bg` | Main page background |
| `--surface` | Elevated background (cards, panels, navbar) |
| `--text` | Primary text color |
| `--text-secondary` | Secondary / muted text |
| `--accent` | Product accent color |
| `--accent-hover` | Accent in hover state |
| `--border` | Subtle border |
| `--border-strong` | Emphasized border (island navbar dark mode) |

### Color derivation

Use `color-mix(in srgb, ...)` over `rgba()` to derive theme variants:

```css
/* Correct */
--accent-15: color-mix(in srgb, var(--accent) 15%, transparent);
--surface-85: color-mix(in srgb, var(--surface) 85%, transparent);

/* Incorrect — hardcoded, does not adapt to theme */
--accent-15: rgba(37, 99, 235, 0.15);
```

## Spacing tokens

Consistent rem-based scale:

| Token | Value |
|-------|-------|
| `--space-xs` | `0.5rem` |
| `--space-sm` | `0.75rem` |
| `--space-md` | `1rem` |
| `--space-lg` | `1.5rem` |
| `--space-xl` | `2rem` |
| `--space-2xl` | `3rem` |
| `--space-3xl` | `4rem` |
| `--space-4xl` | `6rem` |

## Typography tokens

| Token | Purpose | Typical value |
|-------|---------|--------------|
| `--fs-xs` | Legal, captions | `0.75rem` |
| `--fs-sm` | Labels, secondary | `0.875rem` |
| `--fs-base` | Body text | `1rem` |
| `--fs-lg` | Subtitles | `1.125rem` |
| `--fs-xl` | Section headings | `1.5rem` |
| `--fs-2xl` | Major headings | `2rem` |
| `--fs-hero` | Hero headline | `clamp(2.1rem, 7.5vw, 5.5rem)` |

## Motion tokens

| Token | Value | Use |
|-------|-------|-----|
| `--ease-out-expo` | `cubic-bezier(0.16, 1, 0.3, 1)` | Major transitions (navbar morph, entrance) |
| `--ease-out` | `cubic-bezier(0.33, 1, 0.68, 1)` | Minor transitions (hover, indicator) |
| `--duration-fast` | `150ms` | Immediate feedback (hover, active) |
| `--duration-base` | `300ms` | UI transitions (indicator, menus) |
| `--duration-slow` | `400ms` | Major transitions (navbar morph) |
| `--duration-entrance` | `600ms–700ms` | Entrance animations |

## Palettes by product

Each product has its own accent color. Other tokens adapt to theme (dark/light):

| Product | Accent | Value | Color system |
|---------|--------|-------|-------------|
| **ainvirion_com** | Cobalt Blue | `#2563EB` / `#60A5FA` | hex, dark-only |
| **yt2txt** | Sky Blue | `#2563EB` | hex, light/dark toggle |
| **AIProxyGuard** | Forest Teal | `oklch(52% 0.12 160)` | oklch, multi-palette |
| **AgentJunior** | Purple/Azure | via Tailwind v4 `@theme` | oklch |
| **Cliquey** | Emerald | `#10B981` | hex |

## Theming

- Prefer `data-theme="dark"` / `data-theme="light"` over media queries for manual toggle
- Dark mode by default with `color-scheme: dark` on `:root`
- Dark backgrounds: creative textures, **never** grids or checkered patterns
- New products: adopt oklch for better color interpolation
