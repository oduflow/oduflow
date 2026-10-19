# Oduflow Promotion Plan

Status: Draft · Owner: core team · Horizon: 6 months (Q4 2026 – Q1 2027)
Budget ceiling: ≈ $500 total (everything below is time-cost unless marked otherwise).

The goal is awareness and bottom-up adoption, not paid acquisition. Oduflow sits
at the intersection of two high-attention audiences — **AI coding agents / MCP**
and **Odoo development** — and has a demonstrable "wow moment" (the closed
autonomous loop) that most tools in either space cannot show. Every activity in
this plan exists to put that moment in front of the right people.

---

## 1. Positioning & messaging

### One-liner (use everywhere, verbatim)

> **Oduflow** — per-branch, ephemeral Odoo environments for AI coding agents.
> The agent writes the module, installs it, reads the traceback, fixes it, runs
> the tests, and click-tests the UI — autonomously, over MCP.

### Supporting messages (pick per audience)

| Audience | Lead message | Proof point |
|---|---|---|
| AI/MCP crowd | "A real closed feedback loop, not a chat wrapper" | `install_odoo_modules` returns the traceback; `connect_as_user` mints a session cookie so Playwright lands past `/web/login` as any role |
| Odoo developers | "Environment per git branch in seconds, powered by reusable DB templates" | Template DBs + fuse-overlayfs filestores; no more 2-hour setup per task |
| Agency operators | "CI + dev + production deploys for an Odoo fleet, on your own server" | Deploy engine with code rollback, WAL-G PITR, chunkstore filestore backups, per-team quotas, webhooks auto-deploy |
| Freelancers | "Free for noncommercial use; your whole AI toolchain on one box" | PolyForm Noncommercial license; bundled coder image (Agent Chat / Agent CLI) |

### Narrative: "Beyond Vibe Coding — Spec-Driven Development"

The README already contains the manifesto. The external content program (§5)
unpacks it into standalone pieces. Core claim: vibe coding breaks on ERP;
SDD works because the agent has a closed loop with a running system on real
data. This is a thesis, not a feature list — it travels on its own.

### Tone

Same as the product: precise, fast, confident. No marketing gloss, no emoji
walls, no "revolutionize". Show the machine; let real terminal output carry
the message.

---

## 2. Assets to produce first (Phase 0 prerequisites)

### 2.1 The killer demo video ("one take, no humans")

The single most important asset. Everything else links to it.

**Format:** 2–4 min screen recording, 1080p+, real time with ×4 speedups over
boring stretches (visible timer so speedups are honest). No slides, no
voice-over initially — captions only. Optional: a second, narrated 10-min cut
for YouTube.

**Storyboard:**

1. *(0:00)* Split screen: left — a short module spec in Markdown (a concrete,
   relatable Odoo task, e.g. "partner credit-hold: block confirming sale orders
   for partners over credit limit; acceptance criteria listed"). Right — empty
   Oduflow dashboard.
2. *(0:15)* Agent (any MCP client — Cursor / Claude Code) gets the spec.
   `create_environment` fires; dashboard shows the per-branch environment come
   up **in seconds** (this is the template-DB payoff — leave the timer on).
3. *(0:40)* Agent writes the module, calls `install_odoo_modules`. **Show a
   real traceback coming back.** Agent reads it, fixes code, retries. This
   failure-and-recovery beat is the most convincing moment of the whole video —
   do not cut it.
4. *(1:30)* `run_odoo_tests` — tests pass.
5. *(1:50)* `connect_as_user` → Playwright MCP opens the browser already
   logged in as a sales manager, clicks through the flow, verifies the
   acceptance criteria from the spec.
6. *(2:30)* Final frame: spec on the left with all acceptance criteria checked,
   green environment on the right. One caption: "No human touched the keyboard.
   `pip install oduflow`".

**Production notes:**
- Record with OBS; terminal font ≥ 16pt (will be watched on phones).
- Rehearse the agent run until a take shows exactly one organic failure+fix
  cycle; do not fake the traceback.
- Publish: YouTube (own channel), embed on oduflow.dev front page above the
  fold, link from README top.
- Cut three short derivatives: 30-sec vertical teaser (X/LinkedIn/Shorts),
  GIF of the traceback-fix beat (for Reddit/HN comments), GIF of
  `connect_as_user` + Playwright (for MCP crowd).

**Deadline:** end of Week 2. Blocks Phase 1.

### 2.2 Social proof pack

Current users are enthusiastic — capture it while it's warm.

- [ ] Ask 3–5 active users for a 1–2 sentence quote with name/company and a
      concrete number ("env setup went from 2 hours to 40 seconds").
- [ ] Place quotes on oduflow.dev and in README (short section, no carousel).
- [ ] Ask each of them (individually, not a broadcast) to star the repo and,
      if they're on X/LinkedIn, to post one honest line when the demo video
      drops. Coordinated but not scripted.
- [ ] One deeper mini case study (½ page): the team, the before/after, numbers.
      Lives in docs; feeds content program later.

### 2.3 GitHub & docs polish

- [ ] Repo topics: `odoo`, `mcp`, `mcp-server`, `ai-agents`, `docker`, `ci`,
      `erp`, `devtools`.
- [ ] Social preview image for the repo (dashboard screenshot in brand style).
- [ ] README: add the demo video link/GIF at the very top; add quotes section.
- [ ] A `docs/why-oduflow.md` / landing section answering the three searches
      people actually make (see SEO, §5.3).
- [ ] Verify `pip install oduflow && oduflow` works flawlessly on a clean
      Ubuntu VM and a Mac — the launch traffic will test exactly this path.
      First-run friction kills launch conversions.

### 2.4 Measurement baseline (so we can tell what worked)

- Existing telemetry (first startup + environment creation) is the adoption
  ground truth. Record the weekly baseline **now**, before any promotion.
- [ ] UTM parameters on every outbound link we control (`?utm_source=hn`,
      `utm_source=mcp-registry`, `utm_source=reddit`, …); any privacy-friendly
      analytics on oduflow.dev (Plausible self-hosted ≈ $0) if not present.
- [ ] Weekly snapshot spreadsheet: GitHub stars, PyPI downloads
      (pypistats.org), Docker Hub pulls, docs visits, telemetry first-starts,
      inbound license inquiries.

---

## 3. Phase 1 — Launch burst (Week 3, all within ~5 days)

Order matters: directories first (they take days to propagate), then HN, then
reuse HN momentum on Reddit/forums the same week.

### 3.1 MCP Registry + directories (Day 1)

The ecosystem consolidated around the official registry; downstream
directories ingest it automatically, so one publish propagates outward.

- [ ] Publish to the **official MCP Registry**
      ([registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io)):
      CLI push of `server.json`, namespace ownership proven via GitHub login.
      Capability description under 100 chars:
      *"Per-branch ephemeral Odoo environments: install modules, run tests, read logs, click-test UI."*
      Make sure every MCP tool has a rich description (they become the listing).
- [ ] Verify/claim listings on the main downstream directories after
      propagation: [mcp.so](https://mcp.so), Smithery, PulseMCP,
      [Glama](https://glama.ai/mcp/servers).
- [ ] PRs to curated lists: `awesome-mcp-servers`, `awesome-odoo` (and any
      maintained `awesome-selfhosted` section that fits). One-line pitch per
      list's format, link to repo + demo video.

### 3.2 Show HN (Day 3, Tue–Thu, 8–10am ET)

**Title:** `Show HN: Oduflow – per-branch Odoo environments for AI agents (MCP)`

**First comment (post immediately, as the author):** the honest technical
story, roughly:

> I build Odoo modules for a living. The slowest part was never writing code —
> it was the environment: a database with real data, the right Odoo version,
> addons, filestore. Multiply by every git branch and every AI-agent
> experiment.
>
> Oduflow provisions an isolated Odoo environment per git branch in seconds.
> The trick is reusable database templates (`CREATE DATABASE ... TEMPLATE`)
> plus fuse-overlayfs for large filestores, so a 50 GB production copy doesn't
> get duplicated per branch. It exposes the whole loop over MCP:
> `install_odoo_modules` returns the actual traceback, `run_odoo_tests` returns
> failures, `connect_as_user` mints a passwordless session cookie so Playwright
> can click-test the UI as any role. The agent iterates until the spec's
> acceptance criteria pass — the demo video is one unedited take.
>
> Python 3.10+, FastMCP, Docker. Source-available (PolyForm Noncommercial),
> free for noncommercial use; commercial licenses fund development. Happy to
> answer anything about the overlay filesystems, the chunkstore backup engine
> (clean-room duplicacy-inspired CDC), or production deploys with rollback.

**Prep:**
- [ ] Clear the calendar for launch day — answer every comment within minutes
      for the first 4–6 hours; technical depth wins HN threads.
- [ ] Expect and pre-draft answers to: "why not just docker-compose?",
      "why source-available and not OSS?" (answer plainly: funding model;
      noncommercial use is free), "does it work without AI agents?" (yes — CLI
      and dashboard are first-class), "Odoo is niche" (ERP is a $XXB market and
      exactly where vibe coding fails — that's the point).
- [ ] Do not ask anyone to upvote (HN detects voting rings). Sharing the link
      "FYI, we're live" is fine; asking for votes is not.
- If the post doesn't take off (<10 points): fine, retry once in 2–3 months
  with a different angle (e.g. the benchmark results from §5.2). HN allows
  reposts of unnoticed Show HNs.

### 3.3 Reddit + Odoo forum (Days 4–5, riding the same week)

- [ ] **r/Odoo** — own post, written as a developer, not a vendor: "I got an
      AI agent to build and click-test Odoo modules autonomously — here's the
      setup". Lead with the video, disclose being the author, focus on the
      template-DB mechanics (that sub loves ops details). Check sub rules on
      self-promotion first; if strict, answer existing env-setup threads
      instead and link the tool contextually.
- [ ] **r/selfhosted** (angle: your own AI-dev server, no SaaS) and
      **r/LocalLLaMA / r/mcp** if active (angle: a real MCP server with a
      closed loop, works with any client).
- [ ] **Official Odoo forum / OCA mailing lists:** one well-written post in
      the dev-tooling category; thereafter answer env/CI questions as they
      appear (see §6.2 — this is an ongoing habit, not a blast).
- [ ] **X/Twitter + LinkedIn:** thread with the 30-sec teaser; tag/DM a handful
      of MCP-ecosystem accounts that showcase servers. LinkedIn matters more
      than X for Odoo integrators — post the agency-operator angle there.

---

## 4. Phase 2 — Content flywheel (Weeks 4–10, one piece/week)

Publish on oduflow.dev/blog (or docs) first, cross-post to dev.to and LinkedIn,
thread on X. Every piece embeds the demo or a GIF and ends with
`pip install oduflow`.

**Editorial calendar (titles are drafts):**

| Week | Piece | Angle / channel emphasis |
|---|---|---|
| 4 | "Why vibe coding breaks on ERP (and what replaces it)" | The SDD manifesto, expanded. Dev.to + HN-submittable. |
| 5 | "Database templates: Odoo environments in seconds, not hours" | Deep technical: template DBs, fuse-overlayfs thresholds, port registry. r/Odoo gold. |
| 6 | "Anatomy of one autonomous run: spec → module, every MCP call logged" | Annotated transcript of a real run, failures included. MCP crowd. |
| 7 | "connect_as_user: letting Playwright test Odoo as any role, no passwords" | Short, sharp, very linkable in MCP/testing circles. |
| 8 | Public benchmark write-up (see §4.1) | The numbers piece — biggest reach potential. |
| 9 | "Running an Odoo agency fleet on one box: deploys, rollback, PITR" | Agency/operator angle; LinkedIn. |
| 10 | Mini case study from §2.2 user | Social proof as content. |

### 4.1 The public benchmark experiment (the centerpiece)

- Take 5 real-world module specs of increasing difficulty (publish the specs).
- Run each through 2–3 agent/model combos against Oduflow, fully autonomous.
- Publish everything: success/failure per spec, iterations to green,
  wall-clock time, token cost, full transcripts in a public repo.
- Honest failures make it credible and discussion-worthy; a 100% success rate
  reads as marketing and dies. This piece is independently submittable to HN
  ("We measured whether AI agents can really build ERP modules") and is the
  natural second Show HN if the first one underperformed.

### 4.2 SEO pages (write once, compound forever)

Target the searches Odoo developers actually make; docs.oduflow.dev already
ranks for its own name, these capture problem-intent traffic:

- [ ] "Odoo Docker development environment" → guide page that solves it
      generically, with Oduflow as the one-command path.
- [ ] "Odoo CI / run Odoo tests automatically" → same pattern.
- [ ] "Odoo database template / fast test database" → same pattern.
- [ ] "MCP server for Odoo" → short, definitive page (low volume, 100% intent).

---

## 5. Phase 3 — Community & partnerships (ongoing, from Week 4)

### 5.1 Odoo community events

- **OCA Days 2026 happened Sep 21–23 (Liège)** — missed for this cycle.
  - [ ] Watch the [OCA events page](https://odoo-community.org/event) and the
        OCA Days workgroup list for the **2027 CFP**; submit the talk
        "Autonomous Odoo development: spec in, tested module out" (live demo).
  - [ ] Nearer-term: regional events accept talks year-round — Spanish OCA
        Days, Odoo Community Days India, national Odoo meetups/roadshows.
        Target 1–2 in H1 2027.
- [ ] Odoo Experience (Odoo SA's own event): apply for a community talk slot
      for the next edition; even without a slot, the hallway track + a demo on
      a laptop is worth the ticket. (~$300–500 incl. travel — the one real
      budget line.)
- [ ] One self-hosted **webinar/livestream** (Week 6–8): "Watch an agent build
      an Odoo module live", 45 min, unedited, Q&A. Announce in r/Odoo, forum,
      LinkedIn, RU channels. Recording becomes evergreen content.

### 5.2 Influencer/reviewer licenses program

- [ ] List 5–8 visible Odoo educators (YouTube channels doing Odoo tutorials,
      popular OCA contributors, consultants with newsletters/blogs).
- [ ] Offer each a free commercial license + a 30-min personal onboarding
      call, in exchange for **nothing contractual** — just "if you find it
      useful, say whatever you honestly think, publicly or not".
      Honest > scripted; the product is strong enough.
- [ ] Same offer to 2–3 mid-size Odoo agencies as design partners: free
      Integrator license for a quarter + direct support channel, in exchange
      for a case study with numbers.

### 5.3 Russian-speaking Odoo community

Existing reputation and warm audience — lowest-friction channel.

- [ ] Announce in the main RU Odoo Telegram channels/chats with the demo video
      and a RU-language summary post (Habr article is a strong option: Habr's
      devops/AI audience overlaps and the format rewards deep technical
      write-ups — translate the Week 5 template-DB piece).
- [ ] First webinar can be run twice: EN and RU editions.

### 5.4 MCP / AI-agents ecosystem

- [ ] Pitch Oduflow as a case study to MCP-ecosystem newsletters/podcasts
      (PulseMCP newsletter, Latent Space, agent-engineering newsletters):
      the hook is "a vertical MCP server with a genuinely closed feedback
      loop in a boring-but-huge domain (ERP)".
- [ ] Short "works with Oduflow" setup recipes in docs for each major client —
      Claude Code, Cursor, Cline, Amp — one page each; these get found by
      client-specific searches and are easy for client communities to share.

---

## 6. Always-on habits (15–30 min/day, assignable)

1. **Answer the environment-pain questions.** Saved searches for new posts on
   r/Odoo, Odoo forum, Stack Overflow (`odoo` + docker/testing/CI tags).
   Answer the question *fully* first, mention Oduflow only where it genuinely
   solves it, always disclose affiliation. 2–3 quality answers/week compound
   into the dominant share of voice on this pain.
2. **Ship visibly.** Every release: changelog is already written — turn its top
   3 items into a short post (X + Telegram + LinkedIn). Dormant-looking repos
   lose evaluations; a visible cadence wins them.
3. **Convert delight.** When a user says something nice in any channel, ask
   the same day for a public version of it (star, quote, post). Keep a
   `social-proof.md` scratchpad of collected quotes.

---

## 7. KPIs & review cadence

Review every 2 weeks against the Phase-0 baseline; kill/double-down per channel.

| Metric | Baseline (fill at Phase 0) | +1 month target | +3 months target |
|---|---|---|---|
| Telemetry: weekly first-starts | — | ×3 | ×6 |
| Telemetry: weekly env creations | — | ×3 | ×8 |
| GitHub stars | — | +300 (HN effect) | +800 |
| PyPI downloads/week | — | ×3 | ×5 |
| Docs unique visits/week | — | ×4 | ×6 |
| Inbound commercial inquiries | — | 3 | 10 |
| Published content pieces | 0 | 4 | 10+ |

Attribution: UTM for links we control; "where did you hear about us" question
in any inbound license conversation; channel-level correlation with telemetry
spikes otherwise.

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| HN post flops | Planned: directories + Reddit + content don't depend on it; retry in 2–3 months with the benchmark angle. |
| "Not open source" pushback | Answer honestly and consistently (funding model; noncommercial free; all plans same code). Never defensive, never buried. |
| Launch traffic hits first-run bugs | §2.3 clean-VM install test *before* Phase 1; launch-day hotfix readiness. |
| Demo looks staged | One unedited take, visible timer, organic failure included; publish the full agent transcript alongside. |
| Odoo SA perception (stepping on partner toes) | Position as complementary dev tooling (like runbot, but self-hosted and AI-first); never position against Odoo itself. |
| Solo-founder bandwidth | Phases are strictly sequential; always-on habits capped at 30 min/day; everything else is batched. |

---

## 9. Explicit non-goals

- **Paid ads** (Google/LinkedIn/X): audience too narrow, CAC indefensible at
  this stage.
- **Product Hunt:** wrong audience (B2C/SaaS browsers, not Odoo devs); the
  effort goes to HN instead.
- **Press releases / PR agencies:** no one in either target community reads
  them.
- **New social accounts beyond X + LinkedIn + Telegram:** maintenance cost
  exceeds reach.

---

## 10. Week-by-week summary

| Week | Focus | Deliverables |
|---|---|---|
| 1–2 | Phase 0 | Demo video + teaser cuts; quotes on site/README; repo polish; clean-VM install test; metrics baseline |
| 3 | Phase 1 launch burst | MCP Registry publish; awesome-list PRs; Show HN; r/Odoo + forum + social posts |
| 4–10 | Phase 2 flywheel | 1 content piece/week per calendar; SEO pages; benchmark experiment (wk 8) |
| 4+ | Phase 3 ongoing | Influencer license outreach; RU community + Habr; webinar (wk 6–8); CFPs for 2027 events |
| every 2 wks | Review | KPI snapshot vs baseline; drop what's flat, double what moves |

## References

- OCA events calendar: <https://odoo-community.org/event> (OCA Days 2026 was
  Sep 21–23, Liège)
- Official MCP Registry: <https://registry.modelcontextprotocol.io>
- Directory landscape overviews: <https://geekflare.com/guides/mcp-server-directories/>,
  <https://roxyapi.com/blogs/mcp-registries-where-to-list-your-server>
