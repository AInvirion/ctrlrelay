# Morph Behavior Patterns

Eight reusable animation patterns shared across all product morphs. Each pattern includes a description, when to use it, and a CSS/JS snippet.

---

## 1. Multi-Phase Sequential Animation

Phases appear and disappear in sequence using percentage-based keyframes within a single `animation-duration`.

**When**: every morph uses this as its top-level structure.

```css
@keyframes xx-phase-one {
  0%         { opacity: 0; }
  2%, 30%    { opacity: 1; }
  33%, 100%  { opacity: 0; }
}

@keyframes xx-phase-two {
  0%, 30%    { opacity: 0; }
  33%, 65%   { opacity: 1; }
  68%, 100%  { opacity: 0; }
}

.xx-phase-one { animation: xx-phase-one var(--morph-cycle) infinite; }
.xx-phase-two { animation: xx-phase-two var(--morph-cycle) infinite; }
```

---

## 2. Staggered Sub-Element Animation

Child elements animate with cascading delays (0.1s–0.5s apart) within a phase.

**When**: pipeline nodes, search results, list items appearing one by one.

```css
.xx-node:nth-child(1) { animation-delay: 0s; }
.xx-node:nth-child(2) { animation-delay: 0.2s; }
.xx-node:nth-child(3) { animation-delay: 0.4s; }

/* Or use a custom property for dynamic stagger */
.xx-node { animation-delay: calc(var(--i, 0) * 0.2s); }
```

Set `--i` per element: `style="--i: 0"`, `style="--i: 1"`, etc.

---

## 3. SVG Stroke Animation

SVG paths use `stroke-dasharray` and `stroke-dashoffset` to create "drawing" effects.

**When**: connecting lines, wire diagrams, checkmarks, decorative paths.

```css
.xx-wire {
  stroke-dasharray: 100;           /* total path length */
  stroke-dashoffset: 100;          /* fully hidden */
  animation: xx-wire-draw var(--morph-cycle) infinite;
}

@keyframes xx-wire-draw {
  0%, 30%  { stroke-dashoffset: 100; opacity: 0; }
  35%      { opacity: 1; }
  50%, 65% { stroke-dashoffset: 0; opacity: 1; }
  70%, 100%{ stroke-dashoffset: 0; opacity: 0; }
}
```

Tip: measure actual path length with `path.getTotalLength()` in JS.

---

## 4. Typing Effect

Text appears character-by-character using CSS `steps()` with `max-width` animation.

**When**: URL inputs, command prompts, search queries, form fields.

```css
.xx-text {
  max-width: 0;
  overflow: hidden;
  white-space: nowrap;
  animation: xx-typing var(--morph-cycle) infinite;
}

@keyframes xx-typing {
  0%       { max-width: 0; }
  5%, 28%  { max-width: 12ch; }   /* visible: use ch units for text */
  32%, 100%{ max-width: 0; }
}

/* Blinking cursor companion */
.xx-cursor {
  animation: xx-blink 0.6s step-end infinite;
}

@keyframes xx-blink {
  0%, 100% { border-color: var(--text); }
  50%      { border-color: transparent; }
}
```

---

## 5. Glow / Emphasis on Activation

Elements gain `box-shadow`, `color`, and `border` changes when "active" in their phase.

**When**: pipeline nodes lighting up, violation badges, active scan indicators.

```css
@keyframes xx-node-glow {
  0%, 30%  { box-shadow: none; border-color: var(--border); }
  35%, 60% {
    box-shadow: 0 0 12px var(--accent-15);
    border-color: var(--accent);
  }
  65%, 100%{ box-shadow: none; border-color: var(--border); }
}
```

For threat/violation emphasis, use a stronger glow with color shift:

```css
box-shadow: 0 0 16px color-mix(in srgb, var(--c-threat) 40%, transparent);
```

---

## 6. Transform & Scale on Entry

Elements scale up or slide in with `translateY`/`scale` transforms on appearance.

**When**: badges, verdict scores, evidence cards, result items.

```css
@keyframes xx-badge-pop {
  0%, 75%  { opacity: 0; transform: scale(0.7); }
  80%      { opacity: 1; transform: scale(1.05); }
  85%, 95% { opacity: 1; transform: scale(1); }
  100%     { opacity: 0; transform: scale(1); }
}
```

For slide-in: replace `scale()` with `translateY(8px)` → `translateY(0)`.

---

## 7. Responsive Animation Restart

JavaScript explicitly restarts CSS animations when panels become active (desktop tab switch) or visible (mobile scroll).

**When**: always required — CSS animations get "stuck" at their current frame without restart.

```js
function restartAnimations(panel) {
  const stage = panel.querySelector('.pt-panel__stage');
  stage.style.display = 'none';
  void stage.offsetHeight;  // force reflow
  stage.style.display = '';
}
```

Desktop: call on tab `activate()`. Mobile: call from `IntersectionObserver` callback when panel enters viewport.

---

## 8. Accessibility Fallback

All morphs must include a `prefers-reduced-motion` block that disables animations and shows a static final state.

**When**: always required — mandatory for every morph.

```css
@media (prefers-reduced-motion: reduce) {
  .morph--xx,
  .morph--xx * {
    animation: none !important;
    transition: none !important;
  }

  /* Show all phases in their visible state */
  .xx-phase-one,
  .xx-phase-two,
  .xx-phase-three {
    opacity: 1;
  }

  /* Show final-state elements */
  .xx-badge, .xx-result, .xx-text {
    opacity: 1;
    transform: none;
  }
}
```

Choose which phase to show (usually the most visually informative one, not all at once).
