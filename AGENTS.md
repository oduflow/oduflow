# Agent Instructions

This file provides guidance to Claude Code (claude.ai/code) and other AI coding agents when working with code in this repository.

## What is Oduflow

AI-first Odoo development and CI tool. Provisions isolated, ephemeral Odoo environments on Docker (one per git branch) and exposes them to AI coding agents via MCP. Python 3.10+, built on FastMCP.

## Design Context

Any change to the web dashboard (`src/oduflow/templates/dashboard.html`, served by `web_ui.py`) must follow the project's design docs:

- `PRODUCT.md` — register (product), users, brand personality, anti-references, design principles.
- `DESIGN.md` — normative visual system ("The Engineer's Console", shared with oduflow.dev): color tokens, typography, components, do's and don'ts.

Read both before touching dashboard UI. Key hard rules: no external CDNs (all assets ship with the package), every `var(--*)` must be declared in `:root`, status is never conveyed by color alone, no emoji as UI affordances.

The dashboard loads `/static/chat.js` and `/static/acp-client.js` with a
shared positive integer cache version in the query string, held in the
`CHAT_V` variable in `src/oduflow/templates/dashboard.html`. Whenever either
`src/oduflow/templates/static/chat.js` or
`src/oduflow/templates/static/acp-client.js` changes, increment `CHAT_V`. The
two files are an interdependent pair of our own code, so one shared version
busts both at once and prevents a stale mismatch between them. This cache
version is independent of the Oduflow product version. (The vendored
third-party assets — `marked.min.js`, `xterm.js`, etc. — are not versioned this
way; bump the filename if you ever upgrade them.)

## Commands

```bash
# Install from source (editable)
pip install -e .

# Run the server (stdio transport by default)
oduflow

# Run in HTTP mode (for remote/multi-user)
oduflow --transport http

# Lint
ruff check src/oduflow tests

# Format
ruff format src/oduflow tests

# Type check
mypy src/oduflow

# Tests (default: excludes heavyweight)
pytest

# Run only unit tests (no Docker required)
pytest -m "not integration and not heavyweight"

# Run integration tests (requires Docker)
pytest -m integration

# Run heavyweight tests (slow: full init/destroy cycles)
pytest -m heavyweight

# Run a single test
pytest tests/test_naming.py::test_slugify_branch -v

# Build & deploy docs
pip install -r requirements-docs.txt
mkdocs gh-deploy --force
```

## Architecture

```
MCP Clients (Cursor, Claude, etc.)
        │ MCP (Streamable HTTP)
        ▼
   server.py ── FastMCP + CLI entry point (MCP tools + CLI)
        │         ├── @handle_errors decorator → ToolError
        │         └── LockManager (per-branch / per-team / system) → BusyError
        ├── web_ui.py ── Starlette dashboard + REST API + Basic auth
        ├── settings.py ── @dataclass Settings, loads from oduflow.toml (TOML)
        ├── migrations.py ── Startup data migrations (Odoo-style, applied automatically on server start)
        ├── quotas.py ── Per-team disk quotas (XFS project quotas)
        ├── locking.py ── LockManager with per-branch, per-team, system locks
        ├── git_ops.py ── Clone, credentials, manifest parsing
        ├── git_analysis.py ── Classify changed files → action (install/upgrade/restart/nothing)
        ├── bundled_upgrade.py ── Three-way merge deployed bundle files with persistent baselines
        ├── naming.py ── Pure functions: slugify, DB names, paths
        ├── extra_addons.py ── Extra addon repo management (bare clones + worktrees)
        ├── port_registry.py ── Stable port allocation (ports.json)
        ├── env_credentials.py ── Per-environment PostgreSQL credentials
        ├── sanitizer.py ── DB sanitization (SQL/Python scripts)
        ├── production_registry.py ── Per-team productions.json (authoritative prod records)
        ├── prod_tune.py ── Production PG + Odoo worker auto-tuning profiles
        ├── walg.py ── WAL-G bootstrap, WAL archiving, base backups, cluster PITR
        ├── s3_client.py ── boto3 wrapper + chunkstore S3 backend + multipart streaming
        ├── backup_ops.py ── Production snapshots/restore/prune orchestration
        ├── backup_scheduler.py ── Scheduled snapshots/base backups/retention (daemon thread)
        ├── webhooks.py ── GitHub push webhooks → auto-deploy (HMAC, coalescing)
        ├── health.py ── /healthz checks (dev/prod PG, Traefik, S3, disk)
        ├── chunkstore/ ── Clean-room duplicacy-inspired CDC backup engine (filestore→S3)
        └── docker_ops/
            ├── client.py ── Docker SDK wrapper, UID/GID detection
            ├── system_ops.py ── init_system, destroy, template management, prod PG infra
            ├── env_ops.py ── Environment create/delete, overlay filesystems
            ├── production_ops.py ── Production lifecycle + deploy engine with code rollback
            ├── odoo_ops.py ── Module install/upgrade/test, exec, logs
            ├── service_ops.py ── Auxiliary services (Redis, Meilisearch, etc.)
            ├── service_presets.py ── Save/restore service configs
            └── stats.py ── Container and system metrics
```

**Key patterns:**
- Every MCP tool is a function in `server.py` decorated with `@mcp.tool()`, `@handle_errors`, and optionally `@with_branch_lock` or `@with_team_lock`
- Granular locking via `LockManager`: per-branch, per-team, and system locks; operations on different branches run in parallel
- Error hierarchy: `FlowError` → `BusyError | NotFoundError | ConflictError | PrerequisiteNotMetError | ExternalCommandError | ProtectedError` (in `errors.py`)
- Settings are a `@dataclass` loaded from `oduflow.toml` via `Settings.from_toml()`; multi-team via `[team.*]` sections
- Filestore isolation: small templates use plain copies; large ones use fuse-overlayfs (threshold: `overlay_threshold_mb`)
- File ownership: `os.chown()` on Linux, fallback to container-based `chown` on macOS

## Testing

- Test markers: `integration` (needs Docker), `heavyweight` (slow full cycles)
- Default `addopts` in `pyproject.toml` excludes heavyweight tests
- Mocking pattern: patch `oduflow.docker_ops.*` functions, not Docker SDK directly
- `conftest.py` at root ignores `src/**` for collection (tests live in `tests/`)

### Testing the FreeSWITCH auxiliary service

When Oduflow runs in Traefik TLS mode, every newly created or recreated
auxiliary service automatically receives
`oduflow-traefik-acme:/etc/traefik:ro`. Do not include that system volume in
the MCP/CLI `volumes` argument; `/etc/traefik` is reserved. The implicit mount
is not stored in the service preset, but `get_service_info` reports it from the
live container.

The test FreeSWITCH service must keep its own sounds volume and run in host
mode. Use placeholders for deployment secrets — never commit the live values:

```bash
oduflow call create_service '{
  "name": "fs",
  "image": "oduist/freeswitch:latest",
  "port": 8080,
  "hostname": "fs",
  "host_mode": true,
  "volumes": "fs-sounds:/usr/share/freeswitch/sounds:rw",
  "env_vars": "ODOO_URL=https://<environment-host>,FS_DOMAIN=<team-host>,FS_LOG_LEVEL=debug,FS_ESL_PASSWORD=<secret>,FS_SOFIA_LOG_LEVEL=2,SOUND_RATES=8000:16000:32000:48000,SOUND_TYPES=music:en-us-callie,EPMD=false,DUMPCAP=false,FS_WEBHOOK_TOKEN=<environment-token>"
}'
```

`FS_WEBHOOK_TOKEN` must be the target environment's token. Before changing an
existing `fs`, call `get_service_info`; `volumes` and `env_vars` overrides are
full replacements. After deploying an Oduflow change, `update_service fs`
recreates a legacy service that is missing the implicit ACME mount. Verify the
result with `get_service_info fs`, confirm that `/etc/traefik/acme.json` is
readable from the service, inspect the generated `wss.pem` issuer/dates, and
then check the Odoo XML-RPC status.

## Plan First, Then Act

Before making any code changes, always:
1. Explain your understanding of the task
2. Present a detailed plan of changes (which files will be created/modified, what exactly will change)
3. Ask for explicit confirmation before proceeding with implementation

Do NOT write or modify any code until the user explicitly approves the plan.

## Record Architectural Decisions

When we add a **new architectural decision or a significant new capability**
(not a bugfix, refactor, copy tweak, or other minor change), record it as a
**decision record** in `specs/`.

- **What counts:** a new pillar or a meaningful shift in how the system works —
  a new subsystem, a change to the runtime/orchestration/tenancy/auth model, a
  new major MCP capability, a delivery-mode change, etc. If you're unsure whether
  it's "macro" enough, it probably isn't — skip it.
- **Source of truth:** write the record **from the conversation/decisions that
  produced the change** — the *why*, the forces and trade-offs, and the *what*
  at a macro level. Do not transcribe code or list every file; capture the
  reasoning a future reader needs, not the diff.
- **Format:** one Markdown file per decision, ADR-style, named
  `NNNN-short-slug.md`. Follow the shape of the existing records: a header block
  (Status · Type · First introduced · Key code) then Context · Decision · How it
  works (macro) · Consequences · (Evolution) · History (commit pointers). Keep it
  to roughly a page — macro altitude, not implementation detail. Link related
  records with `[[NNNN-slug]]`.
- **Numbering:** records are numbered **chronologically** by when the decision
  was first made. A genuinely new decision is the latest in time, so it simply
  takes the next free number. Add a row to `specs/README.md` (the chronological
  index).
- **When:** add the record as part of the same change/PR that introduces the
  capability, so the rationale is captured while it's fresh.

## Env

If you run into any missing python dependency errors, try running your command with source .venv/bin/activate
to assume the python venv.

## Publishing Documentation

Documentation is published to GitHub Pages **automatically** by `.github/workflows/docs.yml`: on every push to `main` touching `docs/`, `mkdocs.yml`, or `requirements-docs.txt`, it installs `requirements-docs.txt` and runs `mkdocs gh-deploy --force` to update the `gh-pages` branch, which GitHub's `pages-build-deployment` then publishes live.

Do NOT run `mkdocs gh-deploy` (or otherwise deploy docs) from a working/feature branch — that would push unmerged content live. Just commit the `docs/`/`mkdocs.yml` changes as part of your branch; the site updates once they merge to `main`. A manual redeploy is available via the workflow's `workflow_dispatch` trigger.

The site is hosted at: https://docs.oduflow.dev/

## Publishing a New Version

When preparing a new release, update the product version before creating the tag:

1. Update `pyproject.toml` to the new version.
2. Update `docs/changelog.md` so the top section is titled with the new version and includes the release notes for the changes being published.
3. Commit and push those version/changelog changes to `main`.
4. Create the annotated tag (for example `git tag -a vX.Y.Z -m "vX.Y.Z"`) on the final `main` commit and push the tag.
5. **Publish a GitHub Release for that tag — this is the step that actually ships the package.** The PyPI publishing workflow (`.github/workflows/publish.yml`) triggers on `release: [published]`, **not** on the tag push, so a pushed tag with no published GitHub Release never reaches PyPI. Create the release from the existing tag, using a short human-readable **title** (a descriptive phrase, not the version number — for example `Light UI scheme. Bigger fonts.`) and that version's `docs/changelog.md` section, copied **verbatim**, as the body:

   ```bash
   # Extract this version's changelog section (everything between its heading and the previous release's):
   awk 'NR>1 && /^## vPREV$/{exit} /^## vX.Y.Z$/{f=1} f' docs/changelog.md > /tmp/release-notes.md
   gh release create vX.Y.Z --title "<short phrase>" --notes-file /tmp/release-notes.md --latest --verify-tag
   ```

Do not create or move a release tag until the version bump and changelog commit is already on `main`: the publish workflow builds from the tagged commit, so its `pyproject.toml` version must already be correct. Remember that publishing is gated on the GitHub Release (step 5) — a tag alone does not publish.

Publishing the GitHub Release also triggers the **Docker image** workflow (`.github/workflows/docker.yml`, same `release: [published]` event), which builds and pushes `oduist/oduflow:<VERSION>` and `oduist/oduflow:latest` to Docker Hub automatically. No separate manual `docker push` is required — see the next section.

## Publishing Docker Image

The Docker image is published **automatically** by `.github/workflows/docker.yml`
on every published GitHub Release (`release: [published]` — the same trigger as
the PyPI publish). It uses `docker/build-push-action` with Buildx/QEMU to build
`linux/amd64` + `linux/arm64` and pushes `oduist/oduflow:<VERSION>` (version from
the release git tag) plus `oduist/oduflow:latest` (skipped for pre-releases) to
Docker Hub. This requires the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repo
secrets (a Docker Hub access token with Read & Write scope, not the account
password).

So a normal release needs no manual Docker step. The manual build/push below is a
**fallback** for re-publishing an image out of band (or building locally):

```bash
# Read version from pyproject.toml, then:
docker build -t oduist/oduflow:<VERSION> -t oduist/oduflow:latest .
docker push oduist/oduflow:<VERSION>
docker push oduist/oduflow:latest
```

Registry: hub.docker.com, repository: `oduist/oduflow`

## Publishing the Coder Image

The per-team coding-agent image (the "coder", built from `docker/agent/`) is
published **separately** to `oduist/oduflow-coder`; the dashboard's Agent Chat
and Agent CLI run it. **Licensing:** the image bakes in only permissively
licensed, redistributable open-source components (Apache-2.0: OpenAI Codex CLI,
the Codex ACP adapter, Agent Browser; MIT: OpenCode, the GitHub CLI). Claude
Code and its ACP adapter are proprietary-adjacent (Anthropic Commercial
Terms), so they are NOT redistributed — `entrypoint.sh` npm-installs them at first container start onto
the persistent home volume. Keep it that way when editing the Dockerfile.

**Publication is automatic and merge-gated — never publish from a feature
branch.** The CI workflow `.github/workflows/publish-coder.yml` builds and pushes
`oduist/oduflow-coder` when a change under `docker/agent/**` lands on `main`.
It reads the version from the Dockerfile's `ARG CODER_VERSION` and pushes only
the immutable `:<version>` tag as a **multi-arch manifest (`linux/amd64` +
`linux/arm64`)**, so the image pulls on both x86_64 servers and Apple Silicon
dev machines. Oduflow's `DEFAULT_AGENT_IMAGE` pins that exact tag; no rolling
`:latest` tag is published. So the flow is:

1. Edit `docker/agent/` (Dockerfile / `entrypoint.sh` / `clone-env.sh`).
2. **Bump `ARG CODER_VERSION`** in `docker/agent/Dockerfile` — that value is what
   CI ships (a new `:<version>`); CI refuses to overwrite an existing tag.
3. Update `DEFAULT_AGENT_IMAGE` in `src/oduflow/settings.py` to the same tag;
   the unit suite checks that both versions match.
4. Merge to `main` → CI builds and pushes. (A manual run is available via the
   workflow's `workflow_dispatch` trigger.)

Do NOT run `docker push` for the coder image by hand from a working branch — that
would ship an image built from unmerged code. The workflow needs repo secrets
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub token with push rights to
the `oduist` org).
The runtime requires the configured image to be pullable before it replaces an
existing agent container. Registry: hub.docker.com, repository:
`oduist/oduflow-coder`.
