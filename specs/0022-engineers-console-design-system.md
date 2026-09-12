# 0022 — The Engineer's Console: dashboard design system + lifecycle automation

**Status:** Adopted (still in force)
**Type:** Architecture / Design
**First introduced:** `e992971` "Engineer's Console dashboard redesign + environment lifecycle automation" (2026-06-12, `#55`)
**Key docs:** `PRODUCT.md`, `DESIGN.md`
**Key code today:** `templates/dashboard.html`, `web_ui.py`, `reaper.py` (background sweeper), `activity.py` (per-team activity log), `settings.py` (`[lifecycle]`)

## Context

The dashboard ([[0005-web-dashboard-and-rest-api]]) had grown organically and
read like a borrowed identity — the palette was GitHub's dark theme verbatim,
assets were pulled from CDNs, and status leaned on color alone. Two problems
needed solving together in one change:

1. **No design discipline.** Oduflow's marketing site (oduflow.dev) had a
   coherent brand — "The Engineer's Console" — but the working dashboard, *the
   machine the site only demonstrates*, didn't practice it. There was no
   normative source of truth a human or an agent could design against, so every
   UI tweak risked drift (random hexes, ad-hoc components, emoji as
   affordances, an external font request that breaks air-gapped installs).

2. **Environments accumulate.** Agents create per-branch environments faster
   than anyone cleans them up. Left alone, a host fills with idle and abandoned
   environments, and the dashboard's "show real fleet state" promise turns into
   "show a graveyard."

## Decision

Adopt a **normative visual system, codified in `PRODUCT.md` + `DESIGN.md`**, and
add **environment lifecycle automation** so the fleet self-prunes — shipped as
one change because the design system's job is to show fleet state honestly and
the automation is what keeps that state worth showing.

**The design system ("The Engineer's Console", shared with oduflow.dev):**

- `PRODUCT.md` captures the register (product), the user (a terminal-native
  operator whose agents do the real work), brand personality, anti-references,
  and design principles — including that the dashboard is **visualization-first**
  and that creation ergonomics / deep links / keyboard accelerators are
  **explicit non-goals**.
- `DESIGN.md` is the normative visual system: OKLCH tokens on a blue-black ramp,
  Source Sans 3 + Source Code Pro, a categorical **signal palette**, raised-layer elevation,
  and named components down to "The Embedded Terminal." `AGENTS.md` points every
  agent at both docs before touching dashboard UI.
- **Hard rules** (binding, not suggestions): **no external CDNs** — every asset
  (xterm.js, fonts, icons) ships with the package and is served from a local
  `/static` route, so the dashboard works air-gapped; **every `var(--*)` must
  resolve to a token declared in `:root`** (the Defined Token Rule); **status is
  never conveyed by color alone** — badges always carry text; **no emoji as UI
  affordances**; semantic color appears **on intent** (hover/focus), not at rest,
  so status badges own the resting color story.

**The lifecycle automation:**

- A background **reaper** thread sweeps every few minutes: a running environment
  idle past `auto_stop_hours` (default 48) is stopped; a stopped environment
  nobody restarted past `auto_delete_hours` (default 72) is deleted.
- **Protected** environments are exempt from both — Protect is the "keep for
  customers" switch, no new flag invented.
- "Stopped" becomes a **routine state, not an error**: container-level tools and
  `pull_and_apply` **wake** a stopped environment and prepend a one-line note,
  while read-only/diagnostic tools never wake anything.

## How it works (macro)

- **Tokens, not hexes.** The dashboard derives every color, font, radius and
  elevation from the `DESIGN.md` token system declared once in `:root`; JS and
  inline styles never hardcode hex. A light theme was later added purely as a
  second token scheme, proving the discipline.
- **Self-contained assets.** xterm.js and the woff2 fonts are vendored under
  `templates/static` (lazy-loaded on first console open) and served by the
  `/static` route — a CDN `<script>` is treated as a bug.
- **Activity as the lifecycle clock.** `activity.json` per team (written with the
  same flock + unique-tmp discipline as `ports.json` from
  [[0004-stable-addressing-port-registry-and-traefik]]) records the last real
  work on each environment: any env-scoped MCP tool call or dashboard lifecycle
  action counts; listing/polling does not. The reaper reads this to decide what
  to stop or delete, takes the same per-env locks as tools
  ([[0015-granular-locking]]) so busy environments are skipped, and a value of 0
  disables either behavior.
- **Wake-on-use.** Tools that physically need the container start it first and
  tell the agent they did; `get_agent_instructions` explains the auto-stop /
  auto-delete clocks and Protect as the keep-alive switch, so agents treat a
  stopped environment as normal.
- **State honesty in the UI.** Cards show real container state (running /
  partial / exited), "Active: 2h ago" and "Stopped: 1d ago (auto)", a busy chip
  during manual interventions, and a transition pulse when state changes — the
  one celebratory motion the system allows.

## Consequences

- The dashboard gained a **single source of design truth**: changes (by humans or
  agents) are checked against `PRODUCT.md` / `DESIGN.md`, and the hard rules make
  whole classes of regressions (CDN dependency, undeclared token, color-only
  status, emoji affordance) reviewable as bugs. The later light-theme and
  type-scale work slotted in cleanly because the token system already existed.
- The brand is now **continuous from marketing site to product**: oduflow.dev and
  the operator console share one visual language, so the site demonstrates the
  same machine the user runs.
- Lifecycle automation made **"visualization-first" sustainable**: the fleet
  prunes itself, the dashboard keeps reflecting a live working set rather than
  accumulated cruft, and Protect is the single intentional escape hatch.
- Treating "stopped" as routine required teaching both the **tools** (wake-on-use
  + notes) and the **agents** (instructions), tightening the contract between the
  orchestration layer and the agents that drive it.

## Evolution

- `4676d9f` (2026-06-15, `#63`) — enlarge the type scale via rem tokens; improve
  the note UX — all within the established token system.
- `b8c1f28` (2026-06-15, `#65`) — add a **light theme** as a second token scheme
  (v1.50.5).
- `0ee3eef` (2026-06-16, `#66`) — STOPPED status surfacing + service env vars in
  the Info modal.
- (2026-09-12, `#232`) — **retypeset on Adobe Source**: Outfit →
  **Source Sans 3**, Geist Mono → **Source Code Pro**. The original pairing was
  chosen for identity — a geometric sans carrying every UI role through weight.
  In use that identity cost legibility: Outfit's closed apertures and monotone
  geometry make dense agent-chat prose and long log lines tiring to read, which
  is the dashboard's dominant content. The replacement inverts the priority:
  humanist text proportions first, personality second. Measured, Source Sans 3
  runs a 4% larger x-height than Outfit in 5.4% less width, so the fix costs no
  type-scale change and gains density. The sibling relationship is the second
  reason: Source Sans 3 and Source Code Pro share an x-height exactly, so inline
  `code` now sits flush in prose instead of running chunkier than its
  surroundings as Geist Mono did. Both faces are SIL OFL 1.1 and ship as
  variable woff2 with a bundled license notice, preserving the no-CDN rule.

This record covers the **product dashboard** design system. The separate
`specs/2026-06-08-mkdocs-material-redesign-design.md` is the related but distinct
**docs-site** (MkDocs Material) redesign; both share the oduflow.dev blue accent
but are different surfaces.

## History

- `92be004` (2026-06-12) — add `PRODUCT.md` and `DESIGN.md` design context (the
  normative system) and point `AGENTS.md` at them. *(Squash-merged into
  `e992971` on `main`.)*
- `2e12f6a` (2026-06-12) — Engineer's Console redesign: OKLCH tokens, Outfit +
  Geist Mono, signal palette, raised-layer elevation, self-contained assets
  (`/static`), a11y (dialog semantics, tablist, focus rings, reduced motion),
  responsive layout. *(Squash-merged into `e992971`.)*
- `e992971` (2026-06-12, `#55`) — the merged change: design system + a11y +
  semantic-on-intent color + bulk multi-select cleanup + login-page alignment,
  **and** the lifecycle reaper (`reaper.py`, `activity.py`, `[lifecycle]`
  settings), wake-on-use for container tools, and agent-instruction updates.
