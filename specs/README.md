# Decision records

Macro-level Architecture Decision Records (ADRs) for Oduflow — the *why* behind
each architectural pillar and major capability, reconstructed from the project's
history and (going forward) written alongside the change that introduces it. See
the "Record Architectural Decisions" section in `AGENTS.md` for the convention.

Each record is `NNNN-slug.md`, ADR-style: Context · Decision · How it works
(macro) · Consequences · (Evolution) · History. Macro altitude — the reasoning a
future reader needs, not the diff.

**Numbering is chronological** by the date each decision was first made, so the
catalogue reads as the project's evolution timeline. New records take the next
number for *their* place in time; the date column below is the defining-decision
date (a record's own header carries the full commit lineage, including any
precursors).

## Index (chronological)

| # | First decided | Decision |
|---|---|----------|
| [0001](0001-mcp-orchestrated-ephemeral-per-branch-environments.md) | 2026-02-05 | MCP-orchestrated ephemeral per-branch Odoo environments (founding architecture) |
| [0002](0002-remote-multi-user-mcp-access.md) | 2026-02-05 | Remote, multi-user MCP access over streamable HTTP |
| [0003](0003-database-templates-and-filestore-isolation.md) | 2026-02-06 | Database templates + filestore isolation (copy vs fuse-overlayfs) |
| [0004](0004-stable-addressing-port-registry-and-traefik.md) | 2026-02-07 | Stable environment addressing: persistent port registry + Traefik routing |
| [0005](0005-web-dashboard-and-rest-api.md) | 2026-02-07 | Web dashboard + REST API + interactive consoles |
| [0006](0006-git-driven-change-classification.md) | 2026-02-08 | Git-driven change classification → automatic install/upgrade/restart |
| [0007](0007-auxiliary-services-and-volumes.md) | 2026-02-11 | Auxiliary services, presets, and Docker volumes |
| [0008](0008-licensing.md) | 2026-02-12 | Licensing: Polyform Noncommercial + in-app license activation |
| [0009](0009-agent-guidance-system.md) | 2026-02-13 | Agent guidance system: editable MCP instructions + Odoo version dev guides |
| [0010](0010-extra-addons-repositories.md) | 2026-02-13 | Extra addons repositories (bare clones + per-environment worktrees) |
| [0011](0011-per-user-git-credentials.md) | 2026-02-20 | Per-user git credentials and repository authentication |
| [0012](0012-managed-storage-file-access.md) | 2026-02-24 | Agent file access into managed storage (container files + Docker volumes) |
| [0013](0013-per-environment-db-credentials-and-sanitization.md) | 2026-02-24 | Per-environment PostgreSQL credentials + two-tier database sanitization |
| [0014](0014-team-based-multi-tenancy.md) | 2026-03-01 | Team-based multi-tenancy (replacing instance-based isolation) |
| [0015](0015-granular-locking.md) | 2026-03-01 | Granular locking: per-branch / per-team / system locks |
| [0016](0016-configuration-model.md) | 2026-03-01 | Configuration model: `oduflow.toml` + repo-level `.oduflow/` config |
| [0017](0017-mcp-tool-execution-output-cache.md) | 2026-03-02 | MCP tool execution model: server-side output cache + `read_output` |
| [0018](0018-onboarding-stdio-default-auto-init.md) | 2026-03-04 | Onboarding: stdio default transport + auto-init on startup |
| [0019](0019-telemetry.md) | 2026-03-11 | Anonymous usage telemetry |
| [0020](0020-authentication-oauth.md) | 2026-03-13 | Authentication for MCP HTTP: GitHub OAuth → self-hosted OAuth Authorization Server |
| [0021](0021-code-delivery-modes.md) | 2026-06-12 | Code delivery modes: `repo_url` git push vs `local_path` live-mount + `pull_and_apply` guardrail |
| [0022](0022-engineers-console-design-system.md) | 2026-06-12 | The Engineer's Console: dashboard design system + lifecycle automation |
| [0023](0023-import-from-odoo-sh.md) | 2026-07-02 | Import a template from Odoo.sh via a push-based, resumable shell client |
| [0024](0024-business-source-license.md) | 2026-07-02 | Relicense to Business Source License 1.1 with three commercial tiers |
| [0025](0025-startup-data-migrations.md) | 2026-07-02 | Startup data migrations (Odoo-style upgrade steps) |
| [0026](0026-per-team-pg-tablespaces.md) | 2026-07-02 | Per-team PostgreSQL tablespaces |
| [0027](0027-hard-tenant-isolation.md) | 2026-07-02 | Hard tenant isolation: per-team networks, resource limits, disk quotas |
| [0028](0028-scoped-environment-mcp-access.md) | 2026-07-03 | Scoped single-environment MCP access: `/mcp/<env>` + per-environment tokens |
| [0029](0029-agent-console-and-chat.md) | 2026-07-03 | Per-team coding agent: browser console and ACP chat |
| [0030](0030-odoo-sh-addons-import.md) | 2026-07-04 | Odoo.sh import brings the addons-path (Enterprise/Themes/extra) + local extra-addons + template rename |
| [0031](0031-connect-as-user-impersonation.md) | 2026-07-09 | Passwordless "Connect as user" session minting (`connect_as_user`) for agent browser testing |
| [0032](0032-implicit-traefik-acme-mount-for-services.md) | 2026-07-13 | Auxiliary services receive the Traefik ACME store read-only |
| [0033](0033-restricted-http-path-routing-for-services.md) | 2026-07-14 | Restricted HTTP path routing for auxiliary services |
| [0034](0034-external-traefik-routes.md) | 2026-07-16 | External `[route.*]` domains → upstream URLs, plus operator drop-in Traefik dynamic files |
| [0035](0035-production-hosting.md) | 2026-07-11 | Production hosting: dedicated PG cluster, WAL-G/S3 backups, snapshots, auto-rollback deploys |
| [0036](0036-cross-subdomain-connect-as-landing.md) | 2026-07-19 | Cross-subdomain "Connect as user" landing: one-time token → env-host `/oduflow-connect` sets the session cookie host-only |
| [0037](0037-built-in-agent-browser-and-noninteractive-codex.md) | 2026-07-21 | Built-in Agent Browser MCP and non-interactive Codex trust model |
| [0038](0038-agent-chat-file-attachments.md) | 2026-07-22 | Agent Chat file attachments through persistent workspace resources |
| [0039](0039-shared-immutable-extra-addons-checkouts.md) | 2026-07-22 | Shared immutable extra-addons checkouts for development environments |
| [0040](0040-versioned-coder-image-contract.md) | 2026-07-22 | Immutable, release-coupled coder image tags replace the rolling runtime |
| [0041](0041-structured-odoo-orm-tools.md) | 2026-08-02 | Structured Odoo ORM tools (`execute_kw` semantics) over the web JSON-RPC endpoint |
| [0042](0042-translation-tooling.md) | 2026-07-27 | Translation tooling on Odoo's own exporter, plus one-time artifact download links |
| [0043](0043-template-code-provenance-and-lineage.md) | 2026-08-08 | Templates record the commit their data was snapshotted at; environments report code/database drift at creation |
| [0044](0044-unified-host-resource-planning.md) | 2026-08-02 | One host-wide resource plan for dev PostgreSQL, production PostgreSQL, and production Odoo |
| [0045](0045-user-attributed-github-feedback.md) | 2026-08-11 | User-attributed GitHub feedback through prefilled, review-before-submit issue forms |
| [0046](0046-declarative-oduflow-stacks.md) | 2026-08-11 | Declarative Oduflow Stacks: versioned YAML, plan/apply reconciliation, ownership and startup bootstrap |
| [0047](0047-three-way-bundled-upgrades.md) | 2026-08-14 | Three-way upgrades for deployed bundled files with persistent baselines and safe conflict sidecars |
| [0048](0048-reusable-environment-hostname-slots.md) | 2026-08-16 | Reusable environment hostname slots decouple public routing from branch identity |
| [0049](0049-environment-reuse-by-branch-switch.md) | 2026-08-17 | Environment reuse: the branch becomes a mutable property switched in place |
| [0050](0050-granular-resource-locks.md) | 2026-08-17 | Resource-scoped locks: the team lock stops being the catch-all |
| [0051](0051-remote-mcp-cli-client.md) | 2026-08-29 | Built-in remote CLI over the live FastMCP tool surface |
| [0052](0052-managed-postgresql-databases-for-auxiliary-services.md) | 2026-08-29 | Managed PostgreSQL databases for auxiliary services |
| [0053](0053-explicit-start-commands-for-auxiliary-services.md) | 2026-08-31 | Explicit Docker CMD overrides for auxiliary services |
| [0054](0054-agent-container-image-build-and-publish.md) | 2026-09-01 | Agent-driven container image build and publication |
| [0055](0055-scoped-environment-ui-sharing.md) | 2026-09-02 | Shared single-environment dashboard (`/env/<name>` share links) |
| [0056](0056-public-url-scheme.md) | 2026-09-04 | Public URL scheme (`[routing] public_scheme`) and conditional forwarded-header trust |
| [0057](0057-service-container-lifecycle.md) | 2026-09-12 | Explicit service container lifecycle settings |

## Design docs

Forward-looking feature design docs also live here, named by date:

- [`2026-06-08-mkdocs-material-redesign-design.md`](2026-06-08-mkdocs-material-redesign-design.md) — docs-site (MkDocs Material) redesign.
