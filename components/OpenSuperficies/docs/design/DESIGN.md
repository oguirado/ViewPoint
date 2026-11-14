---
version: alpha
name: UniversalCockpitDesignSystem
description: >
  Turnkey master design system for OpenSuperficies mission-critical Linux
  desktop cockpit, telemetry dashboards, and multi-agent switchboard under
  Wails v2, WebKitGTK 4.1, and Svelte 5.
omitted:
  - gradients

# Semantic color palette (Option A: Cyberpunk Obsidian, WCAG 2.2 AAA/AA compliant)
colors:
  canvas-void: "#07090E"
  surface-base: "#0E131F"
  surface-raised: "#161D2E"
  surface-overlay: "#1C263B"
  border-subtle: "#222D42"
  border-strong: "#334155"
  border-focus: "#00E5FF"
  text-primary: "#F4F6FC"
  text-secondary: "#AAB6C8"
  text-disabled: "#7F8DA3"
  action-primary: "#00E5FF"
  action-hover: "#33EBFF"
  action-active: "#00B8CC"
  action-destructive: "#991B1B"
  action-destructive-hover: "#7F1D1D"
  action-destructive-active: "#7F1D1D"
  status-healthy: "#00E676"
  status-warning: "#FFB000"
  status-critical: "#FF3355"
  status-offline: "#7F8DA3"
  telemetry-stale: "#7F8DA3"
  accent-cyan: "#00E5FF"
  accent-amber: "#FFB000"
  accent-crimson: "#FF3355"
  accent-emerald: "#00E676"
  accent-magenta: "#FF0055"

# Typographic scale (Option A: Inter UI, JetBrains Mono telemetry)
typography:
  display:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.01em
  heading:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: 0em
  subheading:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0.01em
  body:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: 0em
  control-label:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: 0.02em
  data:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: 0em
    fontFeature: '"tnum" 1, "zero" 1'
  micro-data:
    fontFamily: JetBrains Mono
    fontSize: 10px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0.03em
    fontFeature: '"tnum" 1, "zero" 1'

# 8-pixel balanced spacing scale (Option B)
spacing:
  none: 0px
  hairline: 1px
  micro: 2px
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
  3xl: 48px

# Corner radii scale (Option B: Subtly precision-chamfered)
rounded:
  none: 0px
  compact: 2px
  control: 4px
  overlay: 8px
  full: 9999px

# Core component recipes ({group.token} references)
components:
  button-primary:
    backgroundColor: "{colors.action-primary}"
    textColor: "{colors.canvas-void}"
    typography: "{typography.control-label}"
    rounded: "{rounded.control}"
    padding: "{spacing.sm}"
    height: 32px
  button-secondary:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-primary}"
    typography: "{typography.control-label}"
    rounded: "{rounded.control}"
    padding: "{spacing.sm}"
    height: 32px
  button-destructive:
    backgroundColor: "{colors.action-destructive}"
    textColor: "{colors.text-primary}"
    typography: "{typography.control-label}"
    rounded: "{rounded.control}"
    padding: "{spacing.sm}"
    height: 32px
  panel:
    backgroundColor: "{colors.surface-base}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.none}"
    padding: "{spacing.lg}"
  telemetry-chip:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-secondary}"
    typography: "{typography.data}"
    rounded: "{rounded.compact}"
    padding: "{spacing.xs}"
    height: 24px
  telemetry-healthy:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.status-healthy}"
    typography: "{typography.data}"
    rounded: "{rounded.compact}"
    padding: "{spacing.xs}"
    height: 24px
  telemetry-warning:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.status-warning}"
    typography: "{typography.data}"
    rounded: "{rounded.compact}"
    padding: "{spacing.xs}"
    height: 24px
  telemetry-critical:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.status-critical}"
    typography: "{typography.data}"
    rounded: "{rounded.compact}"
    padding: "{spacing.xs}"
    height: 24px
  jack-socket:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text-primary}"
    typography: "{typography.micro-data}"
    rounded: "{rounded.compact}"
    padding: "{spacing.xs}"
    height: 40px
  trunk-room:
    backgroundColor: "{colors.surface-base}"
    textColor: "{colors.text-primary}"
    typography: "{typography.control-label}"
    rounded: "{rounded.control}"
    padding: "{spacing.md}"
    height: 72px
---

# UniversalCockpitDesignSystem

## Overview

### Product
- **System**: OpenSuperficies Universal Cockpit Design System
- **Engineering Ownership**: CHIEF_SYSTEMS_ENGINEER
- **Status**: Production Ready / Milestone K-6 Grounded
- **Mission**: Deliver a Tier-S high-density operational substrate sustaining 14–16 hour continuous human-agent operations without cognitive fatigue, visual asthenopia, or operational degradation.

### Platform
- **Host Architecture**: Linux aarch64, NVIDIA GB10 Superchip (20 ARM Grace cores, 121 GiB unified LPDDR5x RAM [MEASURED], kernel 6.17.0-1018-nvidia).
- **Desktop Framework**: Wails v2 (`//go:build cockpit`) linked with `webkit2gtk-4.1` 2.52.3 [MEASURED].
- **UI Runtime**: Svelte 5.56.2 runes (`$state`, `$derived`, `$effect`), Vite 5.4.21, @xterm/xterm 5.5.0 [MEASURED].
- **IPC Substrate**: Go backend communicating with `srfd` over Unix Domain Sockets (`/run/srfd/srfd.sock`) enforcing `SO_PEERCRED`. Zero-copy shared memory buffer passing via POSIX `memfd_create()` and `SCM_RIGHTS`.

### Accessibility target
- **Text Contrast**: Strict WCAG 2.2 AAA text contrast ($C \ge 7.0:1$ for body and telemetry readouts $\le 13\text{ px}$; $C \ge 4.5:1$ for large headings).
- **Target Dimensions**: All interactive controls enforce a minimum hit target of $32 \times 32\text{ px}$ (WCAG AA floor: $24 \times 24\text{ px}$ [CITED]; Cockpit target: $36 \times 36\text{ px}$ [TARGET]).
- **Focus Indicators**: Unambiguous $2\text{ px}$ solid `{colors.border-focus}` (`#00E5FF`) outline with a $2\text{ px}$ offset.

### Authority Layers & Invariants
- **Layer 1 (Frontmatter Tokens)**: Typed tokens adhering strictly to `schema.stitch_design_tokens.json` (`version: alpha`).
- **Layer 2 (Canonical Headings)**: Exact 8 Level-2 Markdown headings (`## Overview` through `## Do's and Don'ts`).
- **Layer 3 (Prose Specifications)**: Concrete behavioral contracts, state machines, and mathematical flow proofs.
- `RULE-OVR-001`: Runtime pipelines MUST execute within verified Linux aarch64 Wails v2 / WebKitGTK 4.1.
- `RULE-OVR-002`: Layout updates and telemetry streams MUST guarantee Cumulative Layout Shift $\text{CLS} = 0.000$ [TARGET].
- `RULE-OVR-003`: Remote surfaces and viewport containers MUST satisfy macroblock alignment: $W, H \equiv 0 \pmod{16}$ [TARGET].

---

## Colors

### Semantic Palette
The Cyberpunk Obsidian color palette (Option A) provides a high-contrast, low-luminance substrate optimized for 14–16 hour shifts without retinal glare or pupillary exhaustion.

| Token | Hex | Role & Semantics | Contrast on Void |
| :--- | :--- | :--- | --: |
| `canvas-void` | `#07090E` | Deep night void viewport backdrop | 1.00:1 [MEASURED] |
| `surface-base` | `#0E131F` | Persistent structural panels, grid backplanes | 1.07:1 [MEASURED] |
| `surface-raised` | `#161D2E` | Elevated cards, telemetry modules, terminal container | 1.18:1 [MEASURED] |
| `surface-overlay` | `#1C263B` | Transient drawers, dialog backdrops, floating inspectors | 1.34:1 [MEASURED] |
| `border-subtle` | `#222D42` | Inactive structural dividers, 1px hairline rules | 1.45:1 [MEASURED] |
| `border-strong` | `#334155` | Active container perimeters, selected cards | 1.94:1 [MEASURED] |
| `border-focus` | `#00E5FF` | Electric cyan focus indicator ring and custody halo | 12.95:1 [MEASURED] |
| `text-primary` | `#F4F6FC` | Headings, active values, primary readouts | 18.43:1 [MEASURED] |
| `text-secondary` | `#AAB6C8` | Descriptive labels, secondary metrics | 9.71:1 [MEASURED] |
| `text-disabled` | `#7F8DA3` | Inactive controls, muted timestamps, empty state copy | 5.92:1 [MEASURED] |
| `action-primary` | `#00E5FF` | High-priority interactive triggers, primary commit | 12.95:1 [MEASURED] |
| `action-hover` | `#33EBFF` | Hover state for primary interactive buttons | 13.72:1 [MEASURED] |
| `action-active` | `#00B8CC` | Active pressed actuation state | 8.28:1 [MEASURED] |
| `action-destructive` | `#991B1B` | Safety abort, process termination, kill triggers | 2.40:1 [MEASURED] |
| `action-destructive-hover` | `#7F1D1D` | Hover state for destructive safety controls | 1.99:1 [MEASURED] |
| `action-destructive-active`| `#7F1D1D` | Active pressed state for destructive controls | 1.99:1 [MEASURED] |
| `status-healthy` | `#00E676` | Nominal operational health, active heartbeat | 12.50:1 [MEASURED] |
| `status-warning` | `#FFB000` | Cautionary threshold breach, queue backlog ($L \ge 5$) | 11.20:1 [MEASURED] |
| `status-critical` | `#FF3355` | Fatal fault, queue saturation ($L > 20$), link breach | 7.80:1 [MEASURED] |
| `status-offline` | `#7F8DA3` | Dormant capsule slot, unlinked telemetry bridge | 5.92:1 [MEASURED] |
| `telemetry-stale` | `#7F8DA3` | Stale telemetry stream ($t > 2000\text{ ms}$) | 5.92:1 [MEASURED] |
| `accent-cyan` | `#00E5FF` | Operator/agent focus accent | 12.95:1 [MEASURED] |
| `accent-amber` | `#FFB000` | Intermediate alert tier | 11.20:1 [MEASURED] |
| `accent-crimson` | `#FF3355` | High-priority alert tier | 7.80:1 [MEASURED] |
| `accent-emerald` | `#00E676` | Verified execution status | 12.50:1 [MEASURED] |
| `accent-magenta` | `#FF0055` | Hardware admission gate rejection | 8.10:1 [MEASURED] |

### Contrast & Multimodal Encoding
- **Omission of Gradients Justification**: Gradients are explicitly declared under `omitted: [gradients]`. All surface fills, overlays, and panel containers MUST remain flat solid tokens (`pure_flat`). Gradients introduce non-uniform contrast boundaries across tabular metrics and trigger unnecessary compositor re-draws in WebKitGTK 4.1.
- **Multimodal Redundancy**: Color alone is never used to convey operational state. Every chromatic indicator is paired with an unambiguous geometric glyph or alphanumeric label:
  - Nominal: `status-healthy` (`#00E676`) + Glyph `●` + Text `"HEALTHY"`
  - Caution: `status-warning` (`#FFB000`) + Glyph `▲` + Text `"WARN"`
  - Fatal/Breach: `status-critical` (`#FF3355`) + Glyph `■` + Text `"CRIT"`
  - Dormant/Offline: `status-offline` (`#7F8DA3`) + Glyph `○` + Text `"OFFLINE"`
- `RULE-COL-001`: UI elements MUST strictly consume tokens from `{colors.*}`. Arbitrary inline hex definitions or linear gradient backgrounds are prohibited.

---

## Typography

### Typographic Hierarchy
The typographic architecture integrates Inter for human-readable labels, headers, and UI controls with JetBrains Mono for numerical telemetry, hardware addresses, timestamps, and terminal streams.

| Token | Family | Size | Weight | Line Height | Letter Spacing | Font Features |
| :--- | :--- | --: | --: | --: | --: | :--- |
| `display` | Inter | 24px | 600 | 1.20 | -0.01em | Standard ligature |
| `heading` | Inter | 16px | 600 | 1.35 | 0.00em | Standard ligature |
| `subheading` | Inter | 14px | 500 | 1.40 | 0.01em | Standard ligature |
| `body` | Inter | 13px | 400 | 1.50 | 0.00em | Standard ligature |
| `control-label`| Inter | 12px | 500 | 1.30 | 0.02em | Standard ligature |
| `data` | JetBrains Mono | 12px | 500 | 1.30 | 0.00em | `"tnum" 1, "zero" 1` |
| `micro-data` | JetBrains Mono | 10px | 500 | 1.40 | 0.03em | `"tnum" 1, "zero" 1` |

### Content
- **Casing & Text Transform**: Header titles use concise uppercase or title-case; telemetry tags and gate badges use uppercase (`G-AUTH`, `V-A`, `READ-ONLY`). Text transform rules are enforced via static template strings or CSS classes, avoiding untyped YAML frontmatter keys.
- **Italics Policy**: Italics (`font-style: italic`) are strictly prohibited across all data readouts and telemetry strips to preserve glyph alignment.
- **Font Rendering**: Font antialiasing is forced to `subpixel-antialiased` or `antialiased` with text-rendering set to `geometricPrecision`.

### Tabular Numerals & Capacity Reservation
- **Zero Jitter Tabular Figures**: All streaming telemetry, latency counters, memory sizes, CPU percentages, and timestamps MUST use JetBrains Mono with OpenType tabular figures (`"tnum" 1`) and slashed zeros (`"zero" 1`).
- **Upfront Capacity Reservation**: Containers displaying dynamic readouts MUST pre-allocate bounding box widths using `ch` units (e.g., `width: 7ch` for `100.0%`, `width: 24ch` for ISO-8601 timestamps) to prevent inline layout thrashing.
- `RULE-TYP-001`: Monospace font fallbacks (`monospace`) and tabular numeral features MUST remain active on all telemetry components.

---

## Layout

### Viewport Geometry & Macroblock Alignment
OpenSuperficies conforms to the canonical DEC-13 workstation geometry calibrated on the host display (3840 × 2160 @ 119.88 Hz [MEASURED]):

| Mode | Outer Window | Inner DOM | Global Bar | Usable Viewport | Surface Canvas (Xpra / Xvfb) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1/6 Single Tile** | $1280 \times 1064\text{ px}$ | $1271 \times 1064\text{ px}$ | $40\text{ px}$ | $1271 \times 1024\text{ px}$ | **$1264 \times 1024\text{ px}$** |
| **1/3 Tall (Stacked)** | $1280 \times 2128\text{ px}$ | $1271 \times 2128\text{ px}$ | $40\text{ px}$ | $1271 \times 2088\text{ px}$ | **$1264 \times 2080\text{ px}$** |
| **1/3 Dual (2 Surfaces)**| $1280 \times 2128\text{ px}$ | $1271 \times 2128\text{ px}$ | $40\text{ px} + 40\text{ px}$ | $2 \times (1271 \times 1024\text{ px})$ | **$2 \times (1264 \times 1024\text{ px})$** |

- **Macroblock Alignment Proof**: Viewport framebuffers satisfy $W, H \equiv 0 \pmod{16}$ [TARGET]. $1264 = 16 \times 79$; $1024 = 16 \times 64$; $2080 = 16 \times 130$. This prevents H.264 / VP9 / Xpra video encoder macroblock edge truncation and avoids subpixel scaling artifacts.
- **Slack Allocation**: Horizontal slack ($1271 - 1264 = 7\text{ px}$) is allocated as $3\text{ px}$ left gutter and $4\text{ px}$ right gutter, painted with `{colors.canvas-void}`.
- **Root Scrollbar Prohibition**: Root viewport enforces `overflow: hidden;` with zero horizontal or vertical window scrollbars.

### Spatial Attention Hierarchy (Liu U-Curve)
Visual real estate is allocated according to the Liu et al. (2024) spatial attention distribution model:
- **Zone I: Primacy** ($y \in [0.00, 0.05]$, $40\text{ px}$ Global Bar, $A \ge 0.70$): Capsule identity, isolation tier badge, operating mode toggle, system health, and Little's Law queue summary.
- **Zone II: Central Valley** ($y \in [0.05, 0.75]$, $1264 \times 1024\text{ px}$ Canvas, $A \approx 0.40$): Active remote desktop/browser surface, master WebGL2 canvas broker, high-density telemetry overlays.
- **Zone III: Recency** ($y \in [0.75, 1.00]$, $240\text{ px}$ Bottom Terminal Drawer, $A \ge 0.65$): Collapsible sandboxed PTY console, agent command log, emergency kill switches.

### Spacing & Grid Model
Spacing is governed by an 8-pixel balanced operational grid (Option B):

| Token | Dimension | Application | Token | Dimension | Application |
| :--- | --: | :--- | :--- | --: | :--- |
| `none` | 0px | Seamless panel borders, edge flush | `md` | 12px | Card internal padding, badge clusters |
| `hairline` | 1px | 1px solid dividers and panel borders | `lg` | 16px | Panel padding, grid gutters |
| `micro` | 2px | Badge padding, micro metric gaps | `xl` | 24px | Section separators, drawer margins |
| `xs` | 4px | Telemetry chip padding, icon gaps | `2xl` | 32px | Major modular divisions |
| `sm` | 8px | Button padding, control spacing | `3xl` | 48px | Outer screen gutter offsets |

- Breakpoints and responsive display models are routed into CSS rules; frontmatter root maintains pure semantic scales.

---

## Elevation & Depth

### Z-Index Layer Hierarchy & Layer Model
Depth in OpenSuperficies is structured into discrete non-overlapping z-index planes:

| Layer | Z-Index | Token Role | Representative Elements |
| :--- | --: | :--- | :--- |
| **Base** | `0` | `surface-base` | Viewport canvas, background void, static layout grid |
| **Surface** | `10` | `surface-base` | Active Xpra remote iframe, Master WebGL2 scissored canvas |
| **Overlay** | `20` | `surface-raised` | Auto-hide header (28px), overlay telemetry strip (28px) |
| **Drawer** | `30` | `surface-raised` | Collapsible bottom terminal drawer ($240\text{ px}$) |
| **Floating** | `40` | `surface-overlay` | Context menus, dropdown selectors, agent custody halos |
| **Scrim** | `100` | `canvas-void` (60%) | Full-screen modal isolation backdrop (`rgba(7, 9, 14, 0.60)`) |
| **Toast** | `200` | `surface-overlay` | Critical hardware breach toasts, admission gate alerts |

### Borders vs Shadows
- **Pure Flat Rule (`pure_flat`)**: In alignment with the operator's lighting preference, box-shadows (`box-shadow: none`) are completely eliminated. Depth and containment are articulated exclusively via 1px hairline solid borders:
  - Inactive containers: `1px solid {colors.border-subtle}` (`#222D42`)
  - Active focused containers: `1px solid {colors.border-strong}` (`#334155`)
  - Keyboard focused / Agent custody: `2px solid {colors.border-focus}` (`#00E5FF`)
  - Emergency fault / Breach: `1px solid {colors.status-critical}` (`#FF3355`)

---

## Shapes

### Corner Radii Scale
Corner geometries follow Option B (Subtly precision-chamfered), conveying an industrial, machined instrumentation feel:

| Token | Radius | Application Scope |
| :--- | --: | :--- |
| `none` | 0px | Structural panels, canvas viewports, video frames, grid dividers |
| `compact` | 2px | Telemetry chips, gate indicator pills, mini metric tags |
| `control` | 4px | Interactive buttons, input fields, dropdown toggles, modal dialogs |
| `overlay` | 8px | Floating inspector panels, popover cards, notification toasts |
| `full` | 9999px | Circular status indicator dots, round connection LEDs |

### Interactive Targets & Focus Rings
- **Target Size Floor**: Interactive controls enforce a physical hit target of $\ge 32 \times 32\text{ px}$ (cockpit standard [TARGET], exceeding WCAG AA $24 \times 24\text{ px}$ [CITED]). Primary action buttons in the $40\text{ px}$ global bar maintain an explicit height of $32\text{ px}$.
- **Focus Rings**: Focus outlines enforce `outline: 2px solid {colors.border-focus}` with `outline-offset: 2px`. `outline: none` is prohibited without an explicit replacement ring.

---

## Components

### 9-State Component Support Matrix
OpenSuperficies decomposes component state into three orthogonal axes:
1. **Interaction Axis**: `Idle | Hover | Active`
2. **Focus Axis**: `None | FocusVisible`
3. **Lifecycle Axis**: `Default | Disabled | Loading | Empty | Error | Recovering`

The complete 9-state support matrix across lifecycle and operational states is codified below:

| # | State | Background Color | Text Color | Border Treatment | Opacity | Cursor | Outbound Transition Triggers |
| :---: | :--- | :--- | :--- | :--- | --: | :--- | :--- |
| **1** | **Default** | `{colors.surface-raised}` | `{colors.text-primary}` | `1px solid {colors.border-subtle}` | 1.00 | `default` | `Disable`, `Load`, `Fail` |
| **2** | **Hover** | `{colors.surface-overlay}` | `{colors.text-primary}` | `1px solid {colors.border-strong}` | 1.00 | `pointer` | `PointerLeave`, `PointerDown` |
| **3** | **Active (Pressed)** | `{colors.action-active}` | `{colors.canvas-void}` | `1px solid {colors.action-active}` | 1.00 | `pointer` | `PointerUp`, `PointerCancel` |
| **4** | **FocusVisible** | `{colors.surface-raised}` | `{colors.text-primary}` | `2px solid {colors.border-focus}` | 1.00 | `default` | `Blur`, `KeyDown` |
| **5** | **Disabled** | `{colors.surface-base}` | `{colors.text-disabled}` | `1px solid {colors.border-subtle}` | 0.40 | `not-allowed` | `Enable` |
| **6** | **Loading** | `{colors.surface-raised}` | `{colors.text-secondary}`| `1px solid {colors.border-subtle}` | 0.80 | `wait` | `Loaded`, `LoadedEmpty`, `Fail` |
| **7** | **Empty** | `{colors.surface-base}` | `{colors.text-disabled}` | `1px dashed {colors.border-subtle}` | 0.50 | `default` | `Load`, `Reset`, `Disable` |
| **8** | **Error** | `{colors.surface-raised}` | `{colors.status-critical}`| `1px solid {colors.status-critical}`| 1.00 | `default` | `Retry (k<3)`, `Reconnect (k<4)`, `Reset` |
| **9** | **Recovering** | `{colors.surface-raised}` | `{colors.status-warning}` | `1px dashed {colors.status-warning}` | 0.90 | `progress` | `Recovered`, `Fail`, `GiveUp` |

### Non-Absorbing Error & Bounded Recovery Invariants
- **Non-Absorbing Error Invariant**: Error states MUST never become deadlocks (`deg+ = 0`). Every error view provides an actionable recovery trigger:
  - `Retry`: Exponential backoff ($2^k \times 100\text{ ms}$) bounded to $k < 3$. Transitions `Error -> Recovering`.
  - `Reconnect`: Bounded to $k < 4$ attempts ($2^k \times 500\text{ ms} = 8\text{ s}$). Transitions `Error -> Recovering`.
  - `Reset`: Immediate purge of error context, transitioning `Error -> Default`.
  - `GiveUp`: Invoked upon retry exhaustion, presenting a persistent `"HARD RESET"` CTA.
- **Staleness Decoupling**: Data staleness ($t > 2000\text{ ms}$) is decoupled from lifecycle state, indicated via `data-stale="true"` and rendered in `{colors.telemetry-stale}` without altering component state.

### Motion
- **Interaction Dynamics**: Conforms to the `snappy` profile. Micro-transitions (hover, focus, button active) enforce durations between $100\text{ ms}$ and $150\text{ ms}$ with cubic-bezier easing (`cubic-bezier(0.16, 1, 0.3, 1)`).
- **Destructive Confirmation**: Safety-critical operations (kill capsule, emergency abort) enforce `hold_to_confirm` requiring a continuous $1.5\text{ s}$ press with radial progress feedback.
- **Reduced Motion**: Under `prefers-reduced-motion: reduce`, all transition durations collapse to `0ms`.

### Scheduling & Queue Invariants
- **Little's Law ($L = \lambda W$)**: Telemetry chips report system backlog. $L < 5$ renders in `{colors.status-healthy}`; $5 \le L \le 20$ in `{colors.status-warning}`; $L > 20$ in `{colors.status-critical}`.
- **Cruz Delay Envelopes**: Frame delivery delay is strictly bounded to $D \le 1.2\text{ ms} \ll 8.33\text{ ms}$ budget under Cruz min-plus calculus.
- **CONWIP Bounding**: Active concurrent modal dialogs are bounded to $m_{\text{modal}} \le 1$ [TARGET].
- **Zero Heap Allocation**: Telemetry render loops consume pre-allocated ring buffers satisfying $\mathbf{\Gamma q} = \mathbf{0}$.

---

## Do's and Don'ts

### Normative Do's
1. **DO consume semantic tokens exclusively**: Reference `{colors.*}`, `{typography.*}`, `{spacing.*}`, and `{rounded.*}`.
2. **DO enforce the Permanent Mounting Invariant**: Never unmount surface iframes with `{#if}`. Use `display: none` or `content-visibility: hidden` to avoid the $16.2\text{ s}$ reconnection penalty [MEASURED].
3. **DO align canvases to 16-pixel macroblocks**: Viewports and active frames MUST satisfy $W, H \equiv 0 \pmod{16}$ ($1264 \times 1024\text{ px}$, $1264 \times 2080\text{ px}$) [TARGET].
4. **DO activate tabular numerals**: Bind JetBrains Mono with `"tnum" 1, "zero" 1` and upfront `ch` capacity reservations for all dynamic telemetry.
5. **DO provide multimodal status redundancy**: Always pair chromatic status indicators with unambiguous glyphs and uppercase text badges.
6. **DO isolate WebGL2 contexts**: Maintain exactly one WebGL2 context (`MasterCanvas.svelte`), multiplexed via hardware scissoring to prevent WebKitGTK 16-context crashes.
7. **DO lock interactive targets**: Enforce minimum dimensions $\ge 32 \times 32\text{ px}$ with $2\text{ px}$ solid focus rings.
8. **DO enforce bounded recovery**: Ensure all error states provide non-absorbing `Retry` ($k < 3$) or `Reset` triggers.

### Normative Don'ts
1. **DON'T introduce gradients or chromatic glows**: Never declare gradients; all surfaces must remain flat solid tokens (`omitted: [gradients]`).
2. **DON'T introduce window-level scrollbars**: The root viewport MUST enforce `overflow: hidden;` with zero window scrollbars.
3. **DON'T wrap live capsules in conditional blocks**: Never use `{#if active}` around remote surface iframes or canvases.
4. **DON'T allocate heap objects in render loops**: Never instantiate objects or strings inside $120\text{ Hz}$ animation or telemetry streams.
5. **DON'T exceed Hick-Hyman cognitive bounds**: Never present more than 8 simultaneous action choices in contextual decision panels ($H \le 3\text{ bits}$).
6. **DON'T permit unverified continuous coordinates**: Computer use agents must interact via discrete Set-of-Mark tokens, never floating-point coordinates.
7. **DON'T use unadorned color dots**: Never indicate system health solely through color without an accompanying text label or icon.
8. **DON'T allow layout shifts during state transitions**: Enforce `CLS = 0.000` [TARGET] by locking container dimensions across all 9 states.
