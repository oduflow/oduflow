---
hide:
  - navigation
  - toc
---

<section class="odu-hero">
  <span class="odu-hero__eyebrow">⎇ AI-First Odoo Development</span>
  <h1 class="odu-hero__title">Oduflow Docs</h1>
  <p class="odu-hero__subtitle">
    Provision isolated, ephemeral <strong>Odoo</strong> environments on Docker —
    one per git branch — and hand them to your AI agents over <strong>MCP</strong>.
    A closed feedback loop for fully autonomous, spec-driven Odoo development.
  </p>
  <div class="odu-hero__actions">
    <a class="odu-btn odu-btn--primary" href="quick-start/">Read the Docs →</a>
    <a class="odu-btn odu-btn--changelog" href="changelog/">Changelog</a>
    <a class="odu-btn odu-btn--ghost" href="https://github.com/oduflow/oduflow">View on GitHub</a>
  </div>
</section>

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Quick Start**

    ---

    Spin up a fully working Odoo instance for any git branch with a single command.

    [:octicons-arrow-right-24: Quick Start](quick-start.md)

-   :material-content-duplicate:{ .lg .middle } **Reusable Templates**

    ---

    Clone large production databases instantly via PostgreSQL templates and overlayfs.

    [:octicons-arrow-right-24: Template Management](templates.md)

-   :material-source-branch:{ .lg .middle } **Branch Environments**

    ---

    One isolated, ephemeral environment per branch — sharing the template DB and filestore.

    [:octicons-arrow-right-24: Environment Management](environments.md)

-   :material-robot-happy-outline:{ .lg .middle } **MCP for AI Agents**

    ---

    Expose install, test, log and upgrade tools to Cursor, Cline, Amp, Claude and more.

    [:octicons-arrow-right-24: MCP Tools Reference](mcp-tools.md)

-   :material-api:{ .lg .middle } **Dashboard & REST API**

    ---

    Manage everything from a built-in web dashboard or a full JSON HTTP API.

    [:octicons-arrow-right-24: Web Dashboard & REST API](web-api.md)

-   :material-console-line:{ .lg .middle } **CLI Tooling**

    ---

    Use `oduflow call` for local in-process execution or `oduflow client` for a
    remote authenticated server from terminals, scripts, and CI pipelines.

    [:octicons-arrow-right-24: CLI Reference](cli.md)

</div>

<div class="odu-shot">
  <img src="img/envs.png" alt="Oduflow web dashboard">
</div>

## Beyond Vibe Coding: Spec-Driven Development

**Vibe coding** — chatting with an AI and eyeballing the output — was the first wave. It works for prototypes, but breaks down on real ERP systems where a module must install cleanly, pass tests, and work against production data.

**Spec-Driven Development (SDD)** is the next step: you write a precise specification of *what* the module should do, and the AI agent autonomously implements *how* — because it has a **closed feedback loop** with the running system:

```
┌──────────────────────────────────────────────────────┐
│                    AI Agent                          │
│          (Cursor, Cline, Amp, Claude, …)             │
└──────┬──────────────────────────────▲────────────────┘
       │ 1. Read spec                 │ 5. Read errors,
       │ 2. Write code                │    fix code,
       │ 3. Install module via MCP    │    retry
       │ 4. Click-test UI via         │
       │    Playwright MCP            │
┌──────▼──────────────────────────────┴────────────────┐
│               Oduflow (MCP Server)                   │
│  • install_odoo_modules → traceback or success       │
│  • run_odoo_tests → test pass/fail with details      │
│  • get_environment_logs → runtime errors             │
│  • upgrade_odoo_modules → upgrade output             │
├──────────────────────────────────────────────────────┤
│            + Playwright MCP / other tools            │
│  • Navigate Odoo UI, click buttons, fill forms       │
│  • Verify business logic end-to-end                  │
│  • Validate acceptance criteria from the spec        │
└──────────────────────────────────────────────────────┘
```

The agent writes code, installs the module, reads the traceback, fixes the error, retries — and when it installs cleanly, it can open the browser via [Playwright MCP](https://github.com/anthropics/mcp-playwright) to click through the UI, verify business flows, and validate acceptance criteria — **all without human intervention**. `connect_as_user` closes the last gap: it mints a passwordless Odoo session and hands back the cookie, so Playwright lands past `/web/login` as any role (admin, sales manager, portal) — no credentials to type, no login form.

| | Vibe Coding | Spec-Driven Development |
|---|---|---|
| **Input** | Conversational prompts | Formal specification with acceptance criteria |
| **Feedback** | Human eyeballs the code | System returns errors, test results, and UI state automatically |
| **Iteration** | Human copy-pastes errors back | Agent retries autonomously via MCP |
| **Scope** | Single files, prototypes | Full modules against real databases |
| **Verification** | "Looks right" | Module installs, tests pass, UI works on production data |

## Key Features

### Core
- **One command to provision** a fully working Odoo instance for any git branch
- **Instant environment creation** from large production databases via PostgreSQL templates and overlayfs
- **Minimal disk footprint** — environments share the template DB and filestore; only per-branch changes consume additional space
- **Template-free mode** — create environments from scratch (`template_name="none"`) when no production dump is available
- **Auto branch creation** — if a branch doesn't exist on the remote, Oduflow clones the default branch and creates the new branch automatically
- **Extra addons repositories** — mount shared addon repos (e.g. Odoo Enterprise) into environments via git worktrees; `addons_path` is auto-merged into `odoo.conf`
- **Environment protection** — protect environments from accidental deletion via a toggle in the dashboard or REST API

### Smart Automation
- **Smart pull** — `pull_and_apply` analyzes changed files (manifest, Python fields, security XML, JS) and automatically decides whether to install, upgrade, restart, or do nothing
- **Auto-install dependencies** — `.oduflow/requirements.txt` (pip, falls back to the repo root) and `.oduflow/apt_packages.txt` (apt) are automatically installed when creating an environment
- **Custom odoo.conf** — if the repository contains an `odoo.conf` in its `.oduflow/` directory, it is used instead of the default template
- **Field change detection** — Python files are analyzed for `fields.*` definition changes, triggering module upgrades only when necessary

### Infrastructure
- **Auxiliary services** — managed sidecar containers for Redis, Meilisearch, Elasticsearch, or any other service your Odoo setup needs
- **Sidecar PostgreSQL storage** — persistent team-scoped databases with dedicated roles, password rotation, quota accounting, and declarative Stack wiring
- **Traefik auto-HTTPS** — optional reverse proxy with Let's Encrypt certificates for production-like access
- **Stable port registry** — port assignments are persisted in `ports.json` and survive container restarts
- **Resource monitoring** — per-container CPU and RAM stats, plus system-level metrics (memory, load average)

### Integration
- **AI-agent friendly** — the server exposes tools via [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), so LLM-based coding agents (Cursor, Cline, Amp, etc.) can provision and manage Odoo environments programmatically
- **Hosted coding agent** — an opt-in, per-team AI agent (Claude Code / OpenAI Codex / OpenCode) with a browser **Agent Chat** and **Agent CLI**, driving environments through MCP (see [Coding Agent](agent.md))
- **Web dashboard** — a built-in HTML dashboard for managing environments from a browser
- **REST API** — full JSON API for programmatic control from any HTTP client
- **CLI tools** — call every MCP tool locally via `oduflow call` or remotely via
  the schema-driven `oduflow client`
- **Dual transport** — stdio (default, for local MCP clients) and HTTP (Streamable HTTP, for remote/multi-user)
