# Morph Infrastructure

Technical spec for the Product Theater system that hosts morph animations.

## Stage container

```html
<div class="pt-panel" data-product="product-name" data-accent="color-name">
  <div class="pt-panel__stage" aria-hidden="true">
    <div class="morph morph--xx">
      <!-- product-specific phases here -->
    </div>
  </div>
</div>
```

### Stage styling

| Property | Value |
|----------|-------|
| Aspect ratio | `4:3` (auto-scales) |
| Max width | `480px` |
| Background | Two-layer gradient (elevated surface → deep background) |
| Border | `1px` subtle border with inset glow |
| Overflow | `clip` (for rounded corners) |
| Perspective | `800px` (enables 3D transforms) |
| Container | `container-type: inline-size; container-name: morph-stage` |

### Per-product ambient glow

The `::before` pseudo-element on the stage renders a radial gradient using the product's accent color via `--stage-glow` (set from `[data-accent]`).

## Base morph class

```css
.morph {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  animation: morph-idle-float 6s ease-in-out infinite;
  will-change: transform;
}

@keyframes morph-idle-float {
  0%, 100% { transform: rotateX(0) rotateY(0) translateZ(0); }
  50%      { transform: rotateX(1deg) rotateY(-1deg) translateZ(4px); }
}
```

### Container query scaling

```css
@container morph-stage (min-width: 300px) { .morph { scale: 1.15; } }
@container morph-stage (min-width: 400px) { .morph { scale: 1.3; } }
@container morph-stage (min-width: 500px) { .morph { scale: 1.45; } }
```

Note: morphs with wide layouts (e.g., hub-and-spokes) may need smaller scale values to avoid clipping.

## Product Theater JavaScript

Located in `ainvirion_com/assets/js/app.js`.

### Key functions

| Function | Purpose |
|----------|---------|
| `activate(idx)` | Switch to panel at index. Updates tabs, stage height, indicator, glow. Restarts CSS animations via reflow trick. |
| `startAuto()` | Auto-rotate to next panel on a timer based on `AUTO_INTERVALS[product]`. Only runs on desktop when section is visible. |
| `stopAuto()` | Clear auto-rotation timer. |

### Auto-rotation intervals

Per-product delays between panel switches:

| Product | Interval | Rationale |
|---------|----------|-----------|
| AgenticJunior | 33s | Complex multi-spoke animation needs extra viewing time |
| Cliquey | 10s | 10s cycle + brief pause |
| Product Calibration | 10s | 10s cycle + brief pause |
| Default (yt2txt, Kenos) | 8s | 8s cycle matches animation length |

### Interaction handlers

- **Tab click**: `activate(idx)`, stop + restart auto-rotate
- **Keyboard**: Arrow keys, Home, End for tab navigation
- **Hover/Focus**: pause auto-rotate on `mouseenter`/`focusin`, resume on leave/out
- **Visibility**: `IntersectionObserver` (15% threshold) starts/stops auto-rotate when section enters/leaves viewport

### Mobile behavior

Below `1024px`:
- Tabs hidden, all panels stacked vertically
- Auto-rotate disabled
- Each panel gets its own `IntersectionObserver` — animations restart when scrolled into view
- Stage height unconstrained (auto)

### Desktop behavior

Above `1024px`:
- 2-column grid: tabs sidebar (18rem) + stage area
- Single active panel visible at a time
- Animated tab indicator follows active selection
- Auto-rotate enabled while section is visible

## Performance

- **Inactive panels**: `animation-play-state: paused !important`
- **GPU acceleration**: `will-change: transform` on `.morph` and key animated elements
- **Reflow restart**: `display: none → offsetHeight → display: ''` to reset animation state
- **Mobile optimization**: animations only play when panel is in viewport (IntersectionObserver)
