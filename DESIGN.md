---
name: Oduflow Dashboard
description: Operator console for Oduflow — the Engineer's Console system from oduflow.dev, adapted to product-register density.
colors:
  primary: "oklch(0.6 0.2 260)"
  primary-foreground: "oklch(0.98 0 0)"
  background: "oklch(0.07 0.01 260)"
  foreground: "oklch(0.93 0.005 260)"
  card: "oklch(0.1 0.01 260)"
  muted: "oklch(0.15 0.01 260)"
  muted-foreground: "oklch(0.7 0.01 260)"
  border: "oklch(0.3 0.013 260)"
  surface-raised: "oklch(0.16 0.012 260)"
  border-raised: "oklch(0.34 0.015 260)"
  destructive: "oklch(0.577 0.245 27.325)"
  terminal-bg: "#060a12"
  signal-emerald: "#34d399"
  signal-cyan: "#22d3ee"
  signal-violet: "#a78bfa"
  signal-amber: "#fbbf24"
  signal-rose: "#fb7185"
typography:
  headline:
    fontFamily: "Source Sans 3, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.4375rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  title:
    fontFamily: "Source Sans 3, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
  body:
    fontFamily: "Source Sans 3, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Source Sans 3, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.02em"
  mono:
    fontFamily: "Source Code Pro, ui-monospace, SFMono-Regular, Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  sm: "0.225rem"
  md: "0.425rem"
  lg: "0.625rem"
  pill: "999px"
spacing:
  page: "1.5rem"
  card: "1rem 1.25rem"
  gap-sm: "0.5rem"
  gap: "0.75rem"
  section: "1.5rem"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
    padding: "0.375rem 0.875rem"
    typography: "{typography.label}"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: "0.3125rem 0.875rem"
    typography: "{typography.label}"
  badge-status:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.pill}"
    padding: "0.125rem 0.5rem"
    typography: "{typography.label}"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    padding: "{spacing.card}"
  input:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    height: "2.125rem"
    typography: "{typography.body}"
  tab-active:
    backgroundColor: "transparent"
    textColor: "{colors.primary}"
    padding: "0.5rem 1.125rem"
    typography: "{typography.body}"
---

# Design System: Oduflow Dashboard

## 1. Overview

**Creative North Star: "The Engineer's Console"**

The dashboard is the working instrument behind oduflow.dev: the same deep
blue-black console field, the same single confident blue power light, the same
categorical signal palette — but tuned for an operator in a task, not a
visitor on a tour. Where the site demonstrates the machine, the dashboard *is*
the machine: status badges, container stats, sync output, and live terminals
are the content, and the design's job is to keep them scannable at density.
Hierarchy comes from weight and tonal steps on the blue-black ramp, never from
decoration.

This is product register: fixed rem type scale (no fluid clamp), one sans
carrying every role through weight contrast, monospace as the native voice of
machine artifacts, motion limited to 150–250 ms state feedback. Familiar
affordances (tabs, cards, modals, toasts) are kept standard and consistent;
the tool should disappear into the task.

The system explicitly rejects glossy SaaS landing grammar (gradients,
gradient text, glassmorphism, marketing tone), toy no-code styling (oversized
cards, cartoon illustration, emoji affordances), overloaded-enterprise
clutter, and GitHub-clone anonymity — the console must read as Oduflow, not
as a borrowed GitHub theme.

**Key Characteristics:**
- Blue-black console field with one confident Console Blue accent (≤15% of any screen)
- Signal colors used categorically for state and channels, never decoratively
- One humanist sans (Source Sans 3) + its companion mono (Source Code Pro), both self-hosted
- Earned density: compact rows, full data, text contrast ≥4.5:1
- Flat at rest; depth via tonal surface steps, shadows only as state response
- Every asset ships with the package — no CDNs, ever

## 2. Colors

A near-monochrome blue-black field carrying one saturated brand blue, with a
tight categorical signal palette for environment and service state.

### Primary
- **Console Blue** (`oklch(0.6 0.2 260)`): The single accent. Active tab
  indicator, focus rings, links, selected state, and the hover/focus reveal on
  primary and Sync/Logs actions — not a resting button fill. Carries ≤15% of any
  screen; its rarity is what makes it read as confident.

### Secondary (Signal palette)
Categorical state and channel colors. Each names one meaning; they never mix
decoratively within an element.
- **Signal Emerald** (`#34d399`): Running / success / "live". Status badges,
  start affordances, passing states.
- **Signal Amber** (`#fbbf24`): Partial / warning / protected. Degraded
  environments, update-with-care actions.
- **Signal Cyan** (`#22d3ee`): Infrastructure / network channel (URLs, ports,
  traefik).
- **Signal Violet** (`#a78bfa`): Automation / sync channel.
- **Signal Rose** (`#fb7185`): Auxiliary services channel.

### Neutral
- **Ink** (`oklch(0.93 0.005 260)`): Foreground text on the dark field.
- **Muted Ink** (`oklch(0.7 0.01 260)`): Metadata, secondary copy, labels.
  Verify 4.5:1 on every surface it lands on; it is the contrast risk.
- **Console Field** (`oklch(0.07 0.01 260)`): Page background and input fills.
- **Surface** (`oklch(0.1 0.01 260)`): Cards, toolbars, system bar — in-page layers.
- **Subtle** (`oklch(0.15 0.01 260)`): Hover fills, badge tints, secondary fills.
- **Raised Surface** (`oklch(0.16 0.012 260)`): Overlay layers only — modals,
  dropdown menus, toasts. One full step above Surface so a raised layer never
  blends into the dimmed page behind it.
- **Hairline** (`oklch(0.3 0.013 260)`): Borders, dividers, input strokes.
  The site uses `oklch(0.2 0.01 260)`; the dashboard raises it a step because
  on a dense operator surface every card and neutral control must have a
  traceable edge — a hairline you have to hunt for is a bug, not restraint.
- **Raised Hairline** (`oklch(0.34 0.015 260)`): The border of raised layers
  (modals, menus, toasts); it draws the edge that the dark-on-dark shadow
  alone cannot.
- **Terminal Black** (`#060a12`): The embedded terminal's own field, one notch
  darker than the page so consoles read as devices.

### Destructive
- **Alert Red** (`oklch(0.577 0.245 27.325)`): Errors, exited state, and
  destructive actions (Stop, Delete) only. As small *text* on the Console
  Field this value sits at 4.37:1; the dashboard therefore uses the lightened
  text variant `oklch(0.65 0.22 25)` (5.8:1) for red text and badges, keeping
  the site's value for fills.

### Named Rules
**The One Light Rule.** Console Blue is the only brand accent that carries
weight; ≤15% of any screen. Do not introduce a second "primary". Even the
primary action is neutral at rest and reveals the blue only on intent
(hover/focus), so the accent stays scarce and confident.

**The Channel Rule.** Signal colors are categorical. A color may mark a state
or a capability channel; a surface never gets a signal color "for variety".
If you can't name the channel, use neutral or Console Blue.

**The Defined Token Rule.** Every `var(--*)` reference must resolve to a token
declared in `:root`. A reference to an undeclared custom property
(`--card-hover`, `--muted` in the current code) is a bug, not a fallback.

### Light Theme

The dark Console Field is the canonical brand surface; the light theme is a
comfort option for bright rooms, **not a rebrand**. It is built entirely by
re-pointing the same `:root` color tokens — every component already resolves
through them, so the markup is untouched.

**Mechanism.** The active theme is resolved in JS and written to
`<html data-theme="light|dark">`. It follows the OS (`prefers-color-scheme`) by
default; the header toggle cycles **System → Light → Dark** and persists the
choice to `localStorage` (`oduflow-theme`; absent = System). A tiny `<head>`
bootstrap applies it before first paint, so there is no flash of the wrong
theme. The toggle is inline SVG + text (monitor / sun / moon) — never emoji.

**Neutrals** invert on the blue-black ramp: Field → light grey
(`oklch(0.97 …)`), Surface/Raised → white, Hairline → a light
(`oklch(0.9 …)`) stroke, Ink → near-black (`oklch(0.25 …)`), Muted Ink →
`oklch(0.48 …)` (≥4.5:1 on every surface, the same contrast bar as dark).

**Signal colors get darkened text variants.** The bright signal values are
tuned for a dark field and fail 4.5:1 on white. The light theme therefore
swaps each signal token to a ~700-level variant verified ≥4.5:1 both as text on
white *and* on its own 15% badge tint (the tighter constraint). The token name
and meaning are unchanged; only the value darkens, so badges, semantic-hover
buttons, and the Note tint follow automatically.

**The Terminal Stays Dark Rule.** The embedded terminal (`--terminal-bg`) and
the ANSI log map are **not** re-themed — the console reads as a device in both
themes (see §5, the embedded terminal). Overlay scaffolding is tokenized for
the theme switch: `--shadow-bar/-menu/-modal` soften on light, `--backdrop`
dims with a desaturated dark, `--track`/`--track-border` flip to dark-on-light,
and `--accent-fg` carries button text in both themes.

## 3. Typography

**Display Font:** Source Sans 3 (with ui-sans-serif, system-ui fallback)
**Body Font:** Source Sans 3 (same family; weight contrast carries hierarchy)
**Label/Mono Font:** Source Code Pro (with ui-monospace, SFMono-Regular fallback)

**Character:** One humanist sans doing every UI role through weight, paired on
a true contrast axis with its own companion monospace. Open apertures and true
text proportions are the point: this is a console people read dense output and
long agent replies in, so legibility at 14-15px outranks geometric personality.
The mono is the console's native voice: container names, DB names, paths,
images, log output, counts. Source Sans 3 and Source Code Pro are siblings from
the same Adobe Source family, so the two voices share skeleton and rhythm while
staying unmistakably apart on the proportional/monospaced axis. Both faces are
bundled with the package as variable woff2 (SIL Open Font License 1.1 — see
`src/oduflow/templates/static/FONTS-LICENSE.md`), never loaded from a CDN;
until bundled, the system stack fallback is acceptable.

### Hierarchy
Fixed rem scale via semantic `--text-*` tokens (declared in `:root`) — product
register, no fluid clamp. The dense lower steps sit a tight ≈1.07 apart, so
in-card hierarchy leans on weight and tone, not size (see The Weight-Not-Size
Rule); the jump to Headline is the one wide step.
- **Headline** (`--text-xl`, 600, 1.4375rem/23px, 1.3): The page title in the
  header. One per page.
- **Title** (`--text-lg`, 600, 1.0625rem/17px, 1.4): Card titles — environment
  branch, service, template, volume names.
- **Body** (`--text-sm`, 400, 0.9375rem/15px, 1.5): Controls, form fields,
  modal copy, notes.
- **Label** (`--text-xs`, 600, 0.875rem/14px, 0.02em): Badges, form labels,
  meta keys, tab text. Uppercase only for status badges (≤2 words).
- **Mono** (`--text-xs`, 400, 0.875rem/14px, 1.5): Logs, terminal, DB names,
  images, paths, container names, sync output.

Two further ramp steps carry text that falls between these roles:
`--text-2xs` (0.8125rem/13px — header byline, port chips, the smallest meta)
and `--text-md` (1rem/16px — tab labels, sync message). All UI font sizes
reference one of these six tokens; raw `px` font sizes are not used.

### Named Rules
**The Console Voice Rule.** Monospace is reserved for what the machine
produces or consumes: names, paths, images, output, counts. Never set UI copy
in mono for flavor — and never set machine artifacts in the sans.

**The Weight-Not-Size Rule.** At this density, hierarchy inside a card comes
from weight (400 vs 600) and tone (Ink vs Muted Ink), not from adding type
sizes. Five sizes total; do not invent a sixth.

## 4. Elevation

A **flat, tonal system**: depth is stepped lightness on the blue-black ramp
(Field → Surface → Raised Surface). Resting surfaces carry a hairline border
and no shadow. Shadows exist only as a response to state: the raised toast,
the open modal, hover on interactive cards.

On a near-black field a shadow alone cannot separate an overlay, so raised
layers use all three cues together: the Raised Surface step, the Raised
Hairline border, and a dimmed (`rgba(0,0,0,0.72)`) backdrop with a 3px blur.
The blur here is functional layer separation, not decorative glass.

### Shadow Vocabulary
- **Modal Lift** (`box-shadow: 0 24px 64px rgba(0,0,0,0.65)`): Open modals,
  paired with the Raised Surface + Raised Hairline combination.
- **Menu Lift** (`box-shadow: 0 16px 48px rgba(0,0,0,0.5)`): Dropdown menus.
- **Focus Ring** (`box-shadow: 0 0 0 3px` Console Blue at 50%): The
  focus-visible treatment for buttons, inputs, tabs. Never removed.

### Named Rules
**The Flat-At-Rest Rule.** A shadow on a resting element is a bug. Layering at
rest is tonal: Field for the page, Surface for cards and bars, Subtle for
fills inside cards.

**The Raised Edge Rule.** Every overlay (modal, menu, toast) gets the Raised
Surface background and the Raised Hairline border. If an overlay's edge can't
be traced against a dimmed page, it is missing one of the two.

## 5. Components

### Buttons
- **Shape:** Gently rounded (`0.425rem`, the `md` radius), compact
  (`0.3125rem 0.875rem` padding), label weight 600.
- **Primary (Create / Activate):** the quiet primary — neutral at rest like the
  outline action (Surface fill, hairline border, Ink text), set apart only by
  its heavier weight (600) and the larger `lg` radius, not by a resting fill. It
  reveals its intent on hover/focus: Console Blue border + text + 10% Console
  Blue tint, same as Sync/Logs. One per view/modal. The destructive confirm is
  the sole exception — it keeps a solid Alert Red fill so the moment of choice
  stays loud.
- **Outline (default action):** Transparent fill, hairline border, Ink text.
  Hover: border and text shift to the action's semantic color.
- **Semantic on intent:** action buttons are neutral at rest (hairline
  border, Ink text) and reveal their semantic color on hover/focus —
  Start (emerald), Stop/Delete (alert red), Restart/Update (amber),
  Sync/Logs (Console Blue) as border + text + 10% tinted fill. At rest the
  status badges own the screen's color story; a row of permanently colored
  buttons makes color stop being a signal. Inside the More menu the danger
  item stays red (an open menu is already the moment of choice).
- **The lifecycle toggle:** Start and Stop are one button, not two. The label
  and semantic color follow the rendered container state: nothing running →
  "Start" (emerald); anything running → "Stop" (alert red, disabled on
  protected environments). Never render a dead twin button whose only job is
  to sit disabled.
- **States:** default, hover, focus-visible (3px Console Blue ring), disabled
  (40% opacity, not-allowed cursor), loading (label swaps to progressive verb:
  "Creating…"). All five are mandatory.

### Badges
- **Style:** Pill (`999px`), uppercase label ≤2 words, 10–15% tinted fill of
  the state's signal color with the signal color as text
  (`rgba` tint + solid signal text).
- **States:** running (emerald), partial (amber), exited (alert red),
  protected (amber), preset/info (Console Blue). Text always names the state;
  color never carries meaning alone.
- **The transition pulse:** when a status changes between renders, the badge
  pulses twice in its own signal color (0.9s ease-out rings, disabled under
  reduced motion). The moment "it came up" is felt, not hunted. This is the
  only celebratory motion in the system; do not add more.

### Cards / Containers
- **Corner Style:** `0.625rem` (the `lg` radius).
- **Background:** Surface on the Console Field; hairline border.
- **Shadow Strategy:** Flat at rest (see Elevation).
- **Internal Padding:** `1rem 1.25rem`; one padding step per card role.
- **Anatomy:** Title row (name + status badge left, action row right), then
  mono metadata rows in Muted Ink with Ink values. No nested cards.

### Inputs / Fields
- **Style:** Console Field fill, hairline border, `md` radius, `2.125rem`
  height, body size text. Mono optional for code-like values (images, URLs).
- **Focus:** Border shifts to Console Blue plus the 3px focus ring. Never
  `outline: none` without the ring replacement.
- **Error / Disabled:** Errors show Alert Red text adjacent to the field,
  never color alone; disabled drops to 40% opacity.

### Navigation
- **Style:** Header (logo + page actions) over a horizontal tab row with a
  hairline bottom border.
- **Tabs:** Body-size 500-weight text in Muted Ink; hover → Ink; active →
  Console Blue text with a 2px Console Blue underline. Keyboard reachable,
  `aria-selected` carried, overflow scrolls horizontally on narrow screens.

### The Note
- **Role:** the one human voice on a machine card — a developer's message to
  whoever looks next ("QA in progress, keep until release").
- **Set:** amber attention treatment (8% tint, 25% hairline, small NOTE
  kicker), full-brightness Ink text at body size, placed directly under the
  card header — above all machine metadata. Click/Enter opens the editor.
- **Empty:** the quiet ghost affordance ("Add a note...") stays dim, italic
  and tucked below the metadata; an empty slot must never shout.

### Bulk Selection Bar
- **Trigger:** appears (sticky, raised layer) only when at least one
  environment is selected via the card checkboxes; protected environments
  cannot be selected.
- **Scope:** Stop and Delete only — cleanup actions. No bulk restart or
  recreate; that scale of mutation belongs to agents.
- **Anatomy:** count ("3 environments selected") left, actions right
  (neutral-at-rest Stop selected / Delete selected / Clear). One confirm
  dialog names every affected branch.

### Signature Component — The Embedded Terminal
The web console and SQL console: a Terminal Black (`#060a12`) panel inside the
modal, Source Code Pro, cursor in Console Blue, ANSI palette mapped to the signal
colors. It is the dashboard's proof-of-machine — keep it darker than the page
so it reads as a device, and never decorate it.

## 6. Do's and Don'ts

### Do:
- **Do** keep Console Blue at ≤15% of any screen; semantic action colors carry
  the rest of the meaning.
- **Do** give every interactive component all five states (default, hover,
  focus-visible, disabled, loading); the focus ring is never removed.
- **Do** set machine artifacts (DB names, images, paths, logs, counts) in
  Source Code Pro per The Console Voice Rule.
- **Do** keep status badges textual; color reinforces, text informs.
- **Do** ship every asset (fonts, xterm.js, css) inside the package —
  the dashboard must work air-gapped. A CDN `<script>` is a bug.
- **Do** pair any animation (spinner, toast, usage bars) with a
  `prefers-reduced-motion` alternative.
- **Do** declare every CSS custom property you reference (The Defined Token
  Rule).

### Don't:
- **Don't** import glossy SaaS landing grammar: no gradients, no gradient
  text, no glassmorphism, no marketing tone in UI copy.
- **Don't** drift toward toy no-code styling: no oversized cards, no cartoon
  illustration, and no emoji as UI affordances (🔒/🔓 in buttons must become
  text or an inline SVG).
- **Don't** keep GitHub-clone anonymity: `#0d1117 / #58a6ff / #3fb950` is
  GitHub's palette, not Oduflow's. Migrate to the Console tokens above.
- **Don't** rebuild overloaded-enterprise clutter: an action row longer than
  ~5 buttons must split into primary actions + an overflow menu.
- **Don't** hardcode hex values in JS or inline styles outside the token
  system (`#7a1f1f`, `#1f4f7a`, the ANSI map); promote them to named tokens.
- **Don't** use native `prompt()`/`confirm()` as the long-term affordance for
  editing and destructive confirmation; replace with inline editing and a
  consistent confirm pattern when touched.
- **Don't** animate layout properties; usage-bar fills animate `transform:
  scaleX()`, not `width`.
