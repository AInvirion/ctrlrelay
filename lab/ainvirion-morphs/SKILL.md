---
name: ainvirion-morphs
description: >
  Use when creating, modifying, or reviewing product morph animations for
  AInvirion landing pages. Covers the Product Theater system, morph behavior
  patterns, animation infrastructure, and step-by-step guide for building
  new morphs. Triggers on: morph, product animation, product theater,
  landing animation, CSS keyframes, phase animation, typing effect,
  SVG stroke animation, product showcase.
---

# AInvirion Product Morphs

Guide for creating animated product representations ("morphs") for the Product Theater on AInvirion landing pages.

| Reference | Content |
|-----------|---------|
| Behavior Patterns | `references/morph-patterns.md` |
| Infrastructure | `references/morph-infrastructure.md` |
| Existing Catalog | `references/morph-catalog.md` |

## Design rules

- **Max 3 phases** per morph (sequential, with clear visual transitions)
- **Cycle length**: 8–10 seconds looped
- **All values via tokens**: colors use `var(--accent)`, timing uses `var(--duration-*)`, easing uses `var(--ease-out-expo)`
- **Keyframe prefix**: use 2-letter product code (e.g., `aj-`, `yt-`, `cq-`)
- **`prefers-reduced-motion`**: mandatory — disable all animations, show elements in final visible state
- **`aria-hidden="true"`** on all morph stages (decorative content)

## Checklist: creating a new morph

1. **Define the visual story** — what does the product do, told in 1–3 phases?
2. **Choose phase structure** — divide the cycle into percentage ranges (e.g., 0–30%, 30–70%, 70–100%)
3. **Pick a 2-letter prefix** for all keyframe and class names (e.g., `xx-`)
4. **Create HTML** inside a `.pt-panel[data-product="name"][data-accent="color"]` container
   - Stage wrapper: `.pt-panel__stage[aria-hidden="true"]`
   - Morph root: `.morph.morph--xx`
   - Phase containers: one `<div>` per phase, absolutely positioned
5. **Write CSS keyframes** — one per animated element, using percentage-based timing within the cycle
   - Use patterns from `references/morph-patterns.md`
   - All colors/easing/durations via CSS tokens
6. **Add reduced-motion fallback** — `@media (prefers-reduced-motion: reduce)` block setting all elements to visible, `animation: none`
7. **Register in Product Theater JS** — add entry to `AUTO_INTERVALS` with rotation delay (typically cycle length + 2s buffer)
8. **Test** — verify animation restarts on tab switch (desktop) and scroll-into-view (mobile)

## Canonical implementation

All 5 existing morphs live in `ainvirion_com/`:
- **HTML**: `ainvirion_com/index.html` (Product Theater section)
- **CSS**: `ainvirion_com/assets/css/styles.css` (keyframes and morph styles)
- **JS**: `ainvirion_com/assets/js/app.js` (theater system: tab switching, auto-rotation, responsive)
