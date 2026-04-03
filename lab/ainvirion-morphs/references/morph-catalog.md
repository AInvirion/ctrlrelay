# Morph Catalog

Summary of existing product morphs. All live in `ainvirion_com/`.

## Overview

| Product | Class | Accent | Cycle | Phases | Visual concept |
|---------|-------|--------|-------|--------|---------------|
| AgenticJunior | `morph--aj` | Coral (`--c-coral`) | 8s | Hub + 4 spokes | Central AI agent dispatching 4 parallel task pipelines |
| yt2txt | `morph--yt` | Ocean (`--c-ocean`) | 8s | 3-stage flow | URL input → audio waveform → transcript lines |
| Cliquey | `morph--cq` | Emerald (`--c-emerald`) | 10s | 3 phases | Verify identity → Discover matches → Connect peers |
| Kenos | `morph--kn` | Amber (`--c-amber`) | 8s | 3 phases | Discover assets → Investigate vulns → Build evidence case |
| Product Calibration | `morph--pc` | Teal (`--c-teal`) | 10s | 3 phases | Idea input → 6-stage pipeline → GO/NO-GO verdict |

## File locations (relative to repo root)

| Content | File | Approximate lines |
|---------|------|-------------------|
| HTML (all morphs) | `ainvirion_com/index.html` | 336–750 |
| CSS: AgenticJunior | `ainvirion_com/assets/css/styles.css` | 1974–2288 |
| CSS: yt2txt | `ainvirion_com/assets/css/styles.css` | 2288–2460 |
| CSS: Cliquey | `ainvirion_com/assets/css/styles.css` | 2460–2900 |
| CSS: Kenos | `ainvirion_com/assets/css/styles.css` | 2900–3700 |
| CSS: Product Calibration | `ainvirion_com/assets/css/styles.css` | 3700–3900 |
| JS: Theater system | `ainvirion_com/assets/js/app.js` | 240–457 |

## Per-morph detail

### AgenticJunior (`morph--aj`)

**Story**: A central hub (pulsing ring system + logo) dispatches 4 simultaneous workflows to the corners. Each spoke shows: diagonal SVG arm → input label → 4-node pipeline → result badge.

**Key patterns used**: SVG stroke animation (arms), staggered sub-elements (nodes), glow on activation (hub pulse), transform entry (result badges).

**Unique**: only morph without sequential phases — all 4 spokes animate in parallel with staggered start delays (0s, 1s, 2.5s, 3.5s).

### yt2txt (`morph--yt`)

**Story**: Video URL with typing cursor → chevron arrow → 12-bar audio waveform undulating → chevron arrow → 5 transcript text lines growing with shimmer.

**Key patterns used**: typing effect (URL), staggered sub-elements (waveform bars, transcript lines), transform entry (flow arrows).

**Unique**: waveform bars have infinite `scaleY` animation (1.2s) that runs independently of the main 8s cycle.

### Cliquey (`morph--cq`)

**Story**: Phase 1 (Verify) — profile card with scan beam and verification badge. Phase 2 (Discover) — search box with typing + staggered result items. Phase 3 (Connect) — two peer avatars with SVG wire and "Trusted" label.

**Key patterns used**: all 8 patterns. Multi-phase sequential, typing, SVG stroke (wire + checkmark), glow (peer pulse), staggered (results), transform entry (badges).

### Kenos (`morph--kn`)

**Story**: Phase 1 (Discover) — command prompt typing + radar rings expanding + asset grid appearing. Phase 2 (Investigate) — rotating crosshair reticle + finding badges (OK/VIOLATION) + risk meter filling. Phase 3 (Build Case) — evidence card with stats + "CASE READY" badge. Red alarm flash between phases 2–3.

**Key patterns used**: typing (prompt), glow (violations, alarm flash), SVG stroke (checkmark), staggered (assets, stats), transform entry (evidence card).

**Unique**: alarm overlay — red flash at ~43% of cycle. Crosshair uses `linear` infinite rotation (not ease-in-out).

### Product Calibration (`morph--pc`)

**Story**: Phase 1 (Input) — form field with typing "AI task manager for teams". Phase 2 (Pipeline) — 6 circular nodes light up sequentially with connecting wires. Phase 3 (Verdict) — SVG gauge fills, score "82" scales in, GO badge appears.

**Key patterns used**: typing (input field), staggered (pipeline nodes + wires), glow (active nodes), SVG stroke (gauge), transform entry (score + badge).

## Color reference

| Product | CSS variable | Hex value |
|---------|-------------|-----------|
| AgenticJunior | `--c-coral` | `#EF4444` |
| yt2txt | `--c-ocean` | `#06B6D4` |
| Cliquey | `--c-emerald` | `#10B981` |
| Kenos | `--c-amber` | `#F59E0B` |
| Product Calibration | `--c-teal` | `#14B8A6` |
