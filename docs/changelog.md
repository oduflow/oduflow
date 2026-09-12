# Changelog

## v1.75.0

### Features

- **Plain-HTTP deployments via `[routing] public_scheme`** — a new setting fixes
  the scheme (`http` or `https`) of every URL Oduflow hands out: environment,
  service and production URLs, dashboard share links, MCP endpoints and artifact
  links. Unset, it derives the historical default — `https` in traefik mode,
  `http` in port mode — so existing deployments, including `tls = false` behind
  a Cloudflare tunnel, are untouched. Declaring `public_scheme = "http"`
  (plain HTTP end to end, nothing terminating TLS) also switches off trust in
  client-supplied `X-Forwarded-Proto` on Traefik's `web` entrypoint — with no
  trusted terminator in front, that header must not be believed — and the
  Traefik drift check recreates the container when the trust flag no longer
  matches. Contradictory combinations are rejected at validation:
  `public_scheme = "http"` with traefik `tls = true` (the :80→:443 redirect
  would bounce every link and leak tokens on the plaintext first hop), and
  `public_scheme = "https"` in port mode (published ports serve plain HTTP).
  See the new "Plain HTTP, with nothing terminating TLS" section in the Traefik
  docs. (#226)

## v1.74.0

### Features

- **Share a single environment by link** — an operator can now mint a revocable
  share link from the environment card and hand out
  `https://<team-host>/env/<name>?key=<secret>` without giving away the team
  password. The link exchanges the key for a signed host-only cookie and lands
  on the same dashboard scoped to that one environment: the client gets logs,
  consoles, module install/upgrade, Connect As, the `/mcp/<env>` credentials and
  Agent Chat, but no templates, services, volumes, extra repos, productions,
  host statistics or any other environment. The boundary is a server-side
  default-deny allowlist that runs in the auth middleware for every HTTP and
  WebSocket request a scoped session makes — hiding buttons is presentation,
  this is policy. Agent CLI is deliberately denied: its PTY lives in the
  per-team agent container, whose workspace holds a checkout of every
  environment. Share secrets live in the team's `shares.json` rather than a
  container label, so environments that already exist are shareable too; they
  survive recreation and are revoked on deletion. See `docs/security.md` and
  specs/0055. (#223)

- **Rename an environment in place** — `update_environment` (and the dashboard's
  Update environment dialog / `POST /api/environments/{branch}/update`) accepts
  a `new_name` and renames the environment on the same container recreate. The
  database, filestore, workspace, port, credentials, activity, Agent Chat
  history and the coding agent's checkout (moved, never re-cloned) all follow
  the name, and the scoped MCP endpoint moves to `/mcp/<new name>`. The target
  name is locked for the duration, so a concurrent `create_environment` cannot
  claim it mid-rename, and productions are refused outright so a rename cannot
  move a production out from under `productions.json`. A hostname derived from
  the environment name follows the new name; pooled and explicit hostnames stay
  as they are. (#225)

### Bug Fixes

- **Auxiliary service start failures now say what went wrong** — when the
  container behind `create_service`, `update_service` or `restart_service`
  fails to start, the MCP response used to be a bare "Error calling tool" with
  the daemon's reason buried in the server log. A host port that is already
  taken is now reported as a conflict that names the port and the tool that
  can apply a new one (`create_service` after a failed create, `update_service`
  after a failed restart); any other start failure carries the Docker daemon's
  explanation minus the SDK's HTTP wrapper, container IDs and absolute paths.
  The container Docker leaves behind after a failed start is removed, so the
  retry no longer collides with its own name, and `create_service` against a
  stopped service of the same name now explains itself instead of surfacing
  Docker's 409. Docker errors outside service start stay masked as before.
  (#224)

### Breaking Changes

- **Registry credentials are configured directly** — the
  `[team.X.image_registry]` section introduced in v1.73.0 took the push token
  through a `token_env` indirection naming an environment variable. It now takes
  the token itself as `token`, alongside `username`; `token_env` is gone. The
  value stays request-scoped for each push and is hidden from the settings
  repr. Update `oduflow.toml` if you enabled agent image builds on v1.73.0.
  (#222)

## v1.73.0

### Features

- **Agents can build and publish container images** — environment-scoped agents
  get four new MCP tools (`start_image_build`, `get_image_build`,
  `publish_image_build`, `cancel_image_build`) that build a Dockerfile from the
  environment's managed checkout and push the exact built image to the team's
  registry namespace, so shipping an auxiliary-service image no longer needs a
  human with Docker and registry access. Oduflow keeps the socket: the agent
  never receives Docker access or registry credentials. The build source is
  pinned to the checkout's `HEAD` commit, exported under the environment lock
  into a sealed per-job context, which closes the build/pull race. A successful
  build is staged in the local daemon as `oduflow-build/team-<id>:<build-id>`,
  and publishing is a tag and push of that same image — nothing is rebuilt on
  the way out. The capability is gated on a `[team.X.image_registry]` TOML
  section whose `repository_prefix` is the authorization boundary; registry
  credentials are read directly from `username` + `token` in that config and
  passed request-scoped for each push, without persisting them in build jobs.
  Concurrency, context size, log size, and wall-clock limits are enforced per
  team, and older staging images are pruned automatically (the newest
  `keep_images` are kept). (#221)

- **Custom start command for auxiliary services** — `create_service` and
  `update_service` accept an optional `command` that replaces the image `CMD`,
  so images whose entrypoint expects arguments (`minio/minio server /data`)
  no longer need a repackaged or legacy variant. The shell-quoted string is
  split into argv once and stored that way in the service preset; the image
  `ENTRYPOINT` is untouched. `get_service_info` reports the effective command
  (and the image default when it is not overridden), the dashboard exposes it
  in the Create/Restore service forms and on the service card, and declarative
  Stacks gained a `command` field that participates in drift detection. On
  update the parameter is tri-state: omitted keeps the current command, a
  string replaces it, and an empty string drops the override. (#219)

### Dashboard

- **Service environment variables are editable from the card** — **Update** on a
  service now opens a dialog prefilled with the service's saved environment
  variables; editing them applies the change and pulls the latest image in one
  step, leaving the field untouched keeps the previous pull-and-recreate
  behaviour, and clearing it removes every variable. `update_service` has always
  accepted an env override — only the dashboard had no way to send one, so
  changing a variable meant dropping to MCP or the CLI. The prefill comes from a
  new `GET /api/services/{name}/env-vars` that returns the saved preset (falling
  back to container inspection for services created before presets existed)
  rather than the container's whole environment, so image defaults are never
  offered for editing and cannot leak into the preset on first save. The
  container name on a service card now copies on click, like the one on an
  environment card. (#220)

## v1.72.1

### Bug Fixes

- **Invalid environment hostnames now explain themselves** — creating an
  environment from the dashboard with a hostname that is not a DNS label (for
  example `old_staging`) failed with a bare "Internal server error." while the
  one useful sentence stayed in the server log. The hostname is now validated up
  front, next to the environment name and before the environment lock is taken,
  and a `ValueError` raised by the create call itself is surfaced as a 400. This
  is the policy the MCP tools already apply: messages authored for the user are
  shown, every other exception stays masked. The create dialog now says what to
  type instead of reporting an internal error. (#218)

## v1.72.0

### Features

- **Managed PostgreSQL databases for auxiliary services** — teams can create,
  inspect, rotate, and explicitly delete persistent databases that are
  independent of Odoo environments and sidecar container lifecycles. Each gets
  a dedicated non-superuser owner, lives in the team's existing tablespace,
  counts toward database/disk quotas, and returns ready-to-use `DATABASE_URL`
  and `PG*` values only on explicit credential surfaces. Declarative Stacks can
  declare databases and resolve individual connection fields into auxiliary
  service environment variables without persisting generated passwords in the
  manifest or Stack state. (#217)

- **Built-in remote MCP CLI** — `oduflow client <tool>` now calls a running
  Oduflow server through FastMCP using `ODUFLOW_MCP_URL` and
  `ODUFLOW_MCP_TOKEN`. It discovers the live tool schemas, accepts named flags
  or a JSON argument object, derives a required environment target from an
  explicit override or the current Git branch (and the `create_environment`
  branch from the current checkout), supports full team and scoped
  `/mcp/<env>` endpoints, emits machine-readable JSON for automation, and
  preserves server-side tool errors and output-cache summaries. The existing
  `oduflow call <tool>` remains the local in-process path. (#214)

- **Token-safe summaries for apply and test calls** — `pull_and_apply` and
  `run_odoo_tests` now accept `summary_only=True`, keeping verbose Odoo command
  logs server-side instead of injecting them into the calling agent's context.
  Test runs return the final `N failed, M error(s) of K tests` result; applies
  return one line with the action, changed-file count and exit status. Both
  include an `output_id` when detailed output is available, so failures can be
  inspected selectively through `read_output`. The existing verbose responses
  remain the default for backward compatibility. (#210)

- **Templates carry environment variables** — a template's `metadata.json` can
  now hold an `env_vars` object, and every environment created from that
  template gets those variables injected into its Odoo container. Saving a
  template from a live environment records that environment's variables
  automatically, so a tuned configuration (`WORKERS`, `LIMIT_TIME_CPU`, service
  credentials) survives into every environment built from the snapshot instead
  of being retyped at each `create_environment` call. Values passed at creation
  time are merged **per key** over the template's, so one variable can be
  overridden without restating the rest. Names are validated as shell
  identifiers when a template is saved; a hand-edited file with an invalid entry
  is ignored with a warning rather than blocking provisioning. (#207)

- **`create_environment` returns an environment that already exists** — instead
  of raising a conflict it answers with the existing environment's URL,
  database, image and template, starting it when it was stopped and
  provisioning nothing. Agents no longer need a defensive `list_environments`
  round-trip before every create. The lookup runs under the environment lock
  ahead of template and repository validation, so the fast path stays fast. A
  branch mismatch is still refused — that database and URL are in use, and
  moving them stays an explicit `switch_branch` decision. (#212)

### Dashboard

- **Install and upgrade modules from the environment card** — each running
  development environment gains Install modules and Upgrade modules actions,
  backed by a fixed internal SQL query for the installed-module list with a
  legacy-credential fallback. Input is validated, normalized production aliases
  are rejected, and failed attempts are reported truthfully rather than as
  success. A successful install or upgrade restarts the environment, and a
  restart failure is still surfaced as a partial success. Stale upgrade-picker
  responses can no longer replace the active environment's module list. (#216)

- **Service database controls** — the new Databases tab shows live database
  size, connections, and drift status, with guarded create, credential reveal,
  password rotation, copy-environment, and permanent-delete actions. (#217)

- **Environment variables in the template Settings dialog** — a new field edits
  a template's variables as one `KEY=VALUE` per line, the template card shows
  how many are set (names and values stay hidden — they routinely carry
  secrets), and the create-environment form prefills them from the selected
  template. Multiline values stay inherited server-side instead of being put
  through the line-based create field. In template Settings, such values are
  shown read-only and pointed at the raw JSON editor. (#207)

### Fixes

- **Auto-installed modules are now part of atomic environment provisioning** —
  creation no longer reports success when the serving Odoo registry misses its
  120-second readiness deadline or the requested module install fails. The
  readiness probe now preserves its last error and includes a bounded Odoo log
  tail, required setup failures stop before sanitization and roll back the new
  environment, and a final database-bound registry check runs after the
  post-sanitization restart. This prevents an environment from surviving with
  requested modules missing or with their neutralization SQL skipped. Module
  names are validated up front, so a typo is rejected before anything is
  provisioned instead of after the rollback discards it. (#211)

- **Environment limits no longer rewrite existing Traefik hostnames** —
  `environment_slots` is now only a concurrency-safe team capacity limit and
  applies in both port and Traefik modes. The new explicit
  `environment_hostname_mode = "slots"` opt-in enables reusable `dev1`, `dev2`,
  ... routes; the default `branch` mode preserves established addresses such as
  `feature.dev.example.com`, including across `update_environment`. Explicit
  custom hostnames remain stable. Environments that were automatically moved
  to a numbered hostname by v1.69-v1.71 return to their branch-derived route on
  their next update in branch mode, while their capacity reservation remains.
  (#213)

- **Stale filestore overlays are recovered safely** — stale and unexpectedly
  absent fuse-overlayfs mounts are now detected at startup and recovered
  fail-closed, preserving each environment's upper layer rather than risking
  its writes. Overlay health is exposed through `/healthz` and the dashboard
  with path-safe reporting. (#208)

- **A Markdown-only pull no longer restarts a production container** — a pull
  carrying documentation alone used to be classified as `refresh`, which the
  production deploy engine promotes to a container restart, so a docs commit
  bounced Odoo for nothing. `classify_changes` and `shallow_classify` now
  return `action="none"` when every changed file is Markdown, while still
  reporting what was pulled. (#212)

- **A failed install is no longer masked by a later successful upgrade** — when
  a single `pull_and_apply` both installed and upgraded modules, the reported
  exit code was the last command's, so a broken install followed by a clean
  upgrade came back as a success. The apply now keeps the first non-zero exit
  code, which is what the compact `summary_only` status line and the live-mount
  snapshot both key off. (#210)

- **Environment listings now carry the evidence needed for safe slot reuse** —
  the MCP `list_environments` output previously discarded lifecycle metadata
  that the backend already tracked and the dashboard already showed. It now
  reports the current git branch, creation and last-activity timestamps,
  stopped time and source, protection, Stack ownership and operator note;
  legacy records explicitly say `Last Activity: unknown`. The same lifecycle
  metadata is available from `get_environment_info`, and the agent guide no
  longer treats an empty GitHub PR result as proof that a slot is reusable.
  (#209)

- **Translation instructions start with Odoo's exporter** — agents are now told
  to run `export_module_translations` before creating a `.po`, never invent
  `#.`/`#:` metadata by hand, and verify the loaded result with
  `translation_status` after the module upgrade. The guide also warns that
  re-exporting over a catalogue that has not been imported overwrites it with
  the database's contents. This makes the existing silent-zero-import detector
  preventive instead of merely diagnostic. (#209)

### Security

- **Dashboard errors no longer leak external command output** —
  `ExternalCommandError` carries the failed process's full output (Docker
  internals, absolute paths, Python tracebacks), and the dashboard returned it
  verbatim. It now answers `Operation failed. Check server logs for details.`
  and keeps the detail server-side, matching the existing HTTP-500 handlers;
  the status code moves from 400 to 500, since a failed external command is a
  server-side failure. The Connect As bridge shares the same rule and no longer
  interpolates raw exceptions into its plain-text response. Both Connect As
  endpoints now take the environment lock — after reading the request body, so
  a stalled client cannot hold the branch lock open — so minting a session
  cannot race a delete or recreate of the same branch. MCP tool responses stay
  verbose and are unaffected. (#215)

## v1.71.0

### Features

- **Custom environment names, decoupled from the git branch** — an environment
  can now carry a name of its own instead of inheriting the branch name, so one
  branch can back several isolated environments. The dashboard's create form
  gained an optional "Environment name" field (still defaulting to the branch),
  and `/api/environments/create` takes `branch` and `env_name` separately.
  Recreate reads the stored `oduflow.git_branch` label, so an environment named
  apart from its branch comes back on the right one. Dashboards still sending
  only `env_name` keep working — it is read as the branch. (#205)

### Dashboard

- **Edit environment variables from the Update dialog** — the Update
  environment dialog used to be a bare confirmation; it now prefills the
  environment's user-supplied container variables and lets them be edited (one
  `KEY=VALUE` per line) before the container is recreated. A new
  `GET /api/environments/{branch}/env-vars` endpoint serves the prefill, and
  `POST /api/environments/{branch}/update` distinguishes replacement (an
  `env_vars` key in the body), clearing (an empty string) and keeping the
  current set (no key at all), so an untouched field — or a failed prefill —
  never wipes the existing variables. Body-less update calls stay compatible
  and the MCP tools are unchanged. (#204)

### Fixes

- **PostgreSQL access follows the real Docker networks** — on every startup,
  Oduflow now reads the actual IPAM subnets of its shared and per-team networks
  and reconciles a marked block in the active `pg_hba.conf` for both development
  and production clusters. This repairs reused or partially initialized data
  volumes that lack a Docker host rule, avoids hard-coded `172.x` assumptions,
  preserves every standard/operator rule outside the managed block, validates
  the reloaded file and rolls back an invalid candidate. The rules use `md5`
  while any role still holds a pre-PostgreSQL-14 md5 verifier and
  `scram-sha-256` once every role has migrated, so reconciling an old data
  volume never locks its environments out. The update travels through the
  Docker API, so it also works when Oduflow itself runs in a container with
  named config volumes. (#206)

- **URL imports may target private-network hosts** — `import_template_from_odoo`
  and the dashboard's remote-addon import rejected every RFC1918 target as an
  SSRF risk, which blocked the ordinary case of an operator-managed Odoo
  instance or an internal git server on the LAN — and was stricter than
  `validate_repo_url`, which the same dashboard endpoint already hands off to.
  Both call sites now allow private ranges. The parts of the guard that carry
  the real risk are untouched: loopback, link-local (including the cloud
  metadata endpoint), unspecified, multicast and reserved addresses are still
  refused, and the library defaults are unchanged. (#203)

### Security

- **Team resolution only trusts the verified credential** — the MCP team lookup
  read `client_id` from `Context`, which carries caller-controlled request
  metadata rather than the credential established by auth, so a client could
  name a team other than its own. It now reads `client_id` from the verified
  access token instead. Host-header and single-team resolution are
  unchanged. (#206)

## v1.70.0

### Features

- **Environment reuse by branch switch** — `switch_branch` moves an existing
  environment onto another branch instead of forcing a delete-and-create cycle.
  Everything except the code stays in place: the name, database, filestore,
  hostname and URL, ports, PostgreSQL credentials and the scoped MCP endpoint
  and token, so a finished branch can hand its slot to the next one without
  reissuing certificates or reconfiguring an agent. The switch fetches and
  checks out the target branch, then reuses the regular pull pipeline to
  classify the diff and install, upgrade or restart as needed. A preflight
  warns when the target branch does not carry a module that is installed in the
  database (`strict=True` refuses instead), and productions and live-mounted
  environments are rejected. Available as an MCP tool, a REST endpoint, and the
  branch chip on each dashboard card. See `specs/0049`. (#198)

- **Rename an environment in place** — `switch_branch` accepts an optional
  `new_name`, so a reused slot whose name still echoes a finished branch can be
  relabelled during the same container recreate. Everything the name keys moves
  with it: the workspace directory, the database, port and hostname registry
  entries, the activity record, agent chat sessions and the coding agent's
  checkout (moved rather than re-cloned, so uncommitted work survives). The URL,
  credentials and scoped token stay; only the `/mcp/<name>` endpoint path
  follows the new name, and the tool response says so. A taken or invalid target
  is refused before anything mutates. (#200)

- **Granular resource locks** — the coarse per-team lock is replaced by narrow
  keyed locks, so operations that do not share data now run in parallel: a
  minutes-long `create_environment` no longer blocks `delete_service`,
  `create_volume`, `add_extra_repo` or `list_templates` for the whole team.
  Services and volumes get their own keys behind a narrow registry mutex that
  also closes an existing delete/update race and a TOCTOU in the volume in-use
  check; `restart_service` and `run_service_command` gain the lock they never
  had; credential setup, production backups and cluster PITR get dedicated
  keyspaces; and the XML-RPC `odoo_*` tools drop their environment lock, since
  Odoo is transactional. Template mutations keep the team lock, because they
  remount live overlay filestores. The dashboard's REST routes mirror the same
  keys, so no new REST/MCP races appear. See `specs/0050`. (#199)

- **`oduflow upgrade --force` now overwrites unresolved bundled files** —
  `--force` used to only skip the confirmation prompt: conflicts, legacy files
  with no stored baseline and failed merges still kept the live file, wrote a
  sidecar and exited non-zero, so an "unattended" upgrade still needed a human.
  A forced run now resolves those cases in favour of the new bundle — the live
  file is backed up under `<team-data>/.bundled_upgrade/backups/`, the
  destination is overwritten, the baseline advances, stale sidecars and the
  pending marker are removed, and the run exits 0 with nothing left to review.
  Clean three-way merges still merge, local-only changes are still left alone,
  and a first-line `# KEEP` remains an unconditional opt-out. Behaviour without
  `--force` is unchanged. (#201)

### Dashboard

- **Import a template from a running Odoo instance** — the dashboard gained a
  UI-authenticated flow (with a matching REST endpoint) for importing a template
  straight from a live Odoo server, including optional database autodetection
  and database-only backups. HTTP sources are allowed but warn that the master
  password travels without transport encryption; the outbound host safety checks
  still apply. (#202)

## v1.69.0

### Features

- **Reusable environment hostname slots** — Traefik teams can set
  `environment_slots = N` to decouple public routing from branch names and
  number the team hostname prefix: `dev.example.com` becomes the fixed pool
  `dev1.example.com` through `devN.example.com`. Assignments are concurrency-safe,
  persist across stops and container updates, and return to the pool on deletion,
  bounding normal Let's Encrypt certificate issuance to the configured pool.
  `create_environment(hostname="qa")` requests `qa.example.com` while still
  consuming one team slot. The default is 20 environment slots. Teams also
  receive a separate `service_slots = 10` cap for managed auxiliary
  services; deleting an unused service immediately frees its slot, and either
  limit can be disabled with `0`. See `specs/0048`. (#197)

- **OpenCode hosted agent** — OpenCode joins Claude Code and Codex as a full Agent CLI and Agent Chat runtime. The new immutable coder image includes the MIT-licensed `opencode` CLI and native ACP server; OpenCode gets generic provider authentication, an optional `provider/model` override, approval-free execution inside the existing container boundary, per-environment Agent Browser, scoped Oduflow MCP, modern ACP model selection, and isolated conversation history. (#181)

- **Declarative Oduflow Stacks** — a versioned `oduflow.yaml` can now describe
  one development environment together with its Git source, database template,
  extra-addons repositories, required modules, auxiliary services, named
  volumes, and text files copied into those volumes. `oduflow stack
  validate/plan/apply/status` provide strict schema validation, read-only drift
  previews, team-locked idempotent reconciliation, and machine-readable status;
  `oduflow --stack ...` applies the same manifest before starting the MCP
  server. Stack resources carry ownership/spec-hash labels, secret values can be
  resolved from the launching process or generated environment outputs without
  entering state files or plans, and V1 deliberately refuses replacement or
  deletion rather than risking persisted data. See `docs/stacks.md` and
  `specs/0046`. (#183)

- **Disk space admission control** — creating an environment now refuses up
  front instead of failing halfway through a full disk. Oduflow estimates what
  creation will actually write (the exact `pg_database_size()` of the template
  database, filestore copy versus overlay headroom from template metadata, and a
  remote-clone budget), groups the target directories by device — tablespace
  directory, workspaces directory, and the PGDATA volume that holds WAL and
  catalogs — and rejects the request with an actionable
  `PrerequisiteNotMetError` unless a safety margin survives on every device.
  Database quota checks became predictive as well: `used + estimated_new_db` is
  evaluated before `CREATE DATABASE`, and the dashboard's recreate flow runs the
  check before deleting anything. Only creation is gated, so deletion and
  cleanup always remain available to recover a full disk, and a failed
  measurement never blocks. Built-in constants, no new configuration. (#196)

- **Unified host resource planning** — dev PostgreSQL, production PostgreSQL and
  production Odoo workers used to size themselves independently against the
  whole host, so a combined deployment could overcommit. One deterministic
  host-wide CPU/RAM plan now drives all three. Plans carry a fingerprint, so
  Oduflow detects configs that went stale after a CPU/RAM change or a
  production-mode switch (and recognises legacy managed configs while leaving
  hand-written ones alone), and `retune-postgres` gained an explicit
  preview/apply flow that backs up the previous config and stages Odoo worker
  settings without restarting anything on its own. See `specs/0044`. (#171)

- **Template and code lineage** — templates now record the branch, commit and
  snapshot time they were made from, and `create_environment` warns when the
  checked-out code and the template database come from different points in
  history, with separate install and upgrade remediation. Lineage checks work
  with depth-one managed clones and linked worktrees. `BusyError` now names the
  lock holder and how long it has been held, so an agent can tell active work
  from a stale lock. Odoo and service commands run through a shell by default
  with an exact-argv opt-out, and Odoo test runs accept targeted tags and an
  optional upgrade-free rerun. See `specs/0043`. (#170)

- **Incremental template snapshots** — publishing a template used to copy large
  database dumps and filestores several times. Dumps now stream straight to
  their destination (custom archives restore in parallel), publish dumps stage
  through a quota-aware PostgreSQL exchange mount when one is available, and
  filestore snapshots use rsync hardlinks so only the environment's delta
  consumes new disk. Staged publication stays atomic, interrupted staging no
  longer leaves quota-consuming leftovers, and hardlinked storage is counted
  correctly. (#180)

- **Module installs and upgrades are verified** — Odoo exits `0` even when
  `-i`/`-u` targets a module that is unknown, uninstallable, or never actually
  installed, and Oduflow reported that no-op as success. Installs are now
  verified against `ir_module_module` before reporting success, and
  `upgrade_odoo_modules` / `pull_and_apply(upgrade=…)` reject unknown or
  uninstalled modules instead of returning a false positive. (#195, #186)

- **Two feedback channels** — `report_issue` and a dashboard Feedback modal build
  a prefilled GitHub issue form (bug, feature request, or general feedback) with
  a short non-identifying environment summary, so reports stay attributed to the
  user's own GitHub account and Oduflow never holds GitHub credentials. Separately,
  an operator-enabled, anonymous channel lets coding agents report friction with
  the MCP surface itself; it scrubs hosts, addresses, paths, tokens and
  instance-specific names, and restricts the tool-name field to registered
  Oduflow tools. See `specs/0045`. (#175, #173)

- **Three-way bundled-file upgrades** — `oduflow upgrade` no longer compares
  only file sizes and overwrites every differing deployed copy. Each team's
  `odoo.conf`, agent guides, and sanitize script now keep a pristine bundled
  baseline and merge local and upstream changes through `git merge-file`, with
  an atomic pre-update backup. Conflicts never put markers into a live config:
  they create `*.oduflow-merge`, preserve the old baseline, and exit non-zero.
  Existing customized installations without a baseline receive a conservative
  one-time `*.oduflow-new` sidecar. `--force` remains automation-friendly but
  skips only confirmation, `# KEEP` remains a complete opt-out, and generated
  `postgresql.conf` is now exclusively managed by `retune-postgres`. See
  `specs/0047`. (#194)

- **Structured Odoo ORM tools** — six new MCP tools give agents the semantics of
  standard Odoo XML-RPC `execute_kw` without writing Python: `odoo_search_read`,
  `odoo_create`, `odoo_write`, `odoo_unlink`, `odoo_call` (public model methods
  other than those policy-visible CRUD mutations) and `odoo_schema` (paged model
  list + `fields_get`). They call
  `/web/dataset/call_kw` over HTTP from inside the environment's Odoo container,
  so they are independent of the routing mode and need no DNS, TLS or published
  port. Every tool takes `as_user` (a login or user id, empty = the environment's
  admin) and runs inside a real session for that user — access rights and record
  rules apply exactly as they do in the web client, which `run_odoo_shell` cannot
  do without hand-rolling `env(user=…)` and XML-RPC cannot do without that user's
  password. The session is minted passwordlessly through the existing
  "Connect as user" mechanism and cached per environment and user. JSON arguments
  also accept Python literals, and Odoo-side failures (`AccessError`,
  `ValidationError`, …) come back as text with the server traceback instead of a
  masked tool error. `run_odoo_shell` remains the escape hatch for a fresh
  registry, `sudo()`, private methods, dry runs and multi-step transactions. See
  `specs/0041`. (#166)

- **Module translation tooling** — two new MCP tools cover the i18n loop. `export_module_translations` runs Odoo's own exporter with the environment's real addons path (so `_()` and `_lt()` messages are picked up along with database terms), writes the `.pot`/`.po` into the module's `i18n/` directory under Odoo's own filename rule, and returns a per-type summary rather than the file. `translation_status` lines up the module's terms, the translations actually stored in the database, and the committed `.po` files — reporting the two ways Odoo can fail silently: entries without a `#:` reference import as zero translations without a warning, and entries without a `#. module:` comment abort the import. When a sibling `<module>.pot` exists, status models Odoo's automatic metadata merge before judging either defect. Both tools accept Odoo locale modifiers such as `sr@latin`, handle catalogues up to their documented 5 MB limit, reject partial failed exports, and work across Odoo 15 through 19 — including 19's replacement of the `--i18n-*` options with the `odoo i18n` subcommand — with no dependency on the `ir.translation` model removed in 16. (#169)
- **One-time artifact download links** — files an MCP tool generates inside an environment can now leave it without passing through the agent's context window. Over HTTP the tool returns a single-use `/oduflow-artifact` URL (10-minute TTL) using the configured public hostname; under stdio it returns the checkout path or materializes a private process-lifetime temporary file when the source is a read-only/core module. (#169)

- **`translation_status` returns a verdict, not three catalogues** — the tool
  used to print what it measured: template, database and file counts plus a
  capped list of missing or stale msgids, which on a module whose Russian
  catalogue covered 3 of 442 terms meant roughly 450 lines of view bodies to
  restate what the first two numbers already said. The server holds all three
  sources and the inference is mechanical, so it now makes the call itself:
  each language is classified as `OK`, `PARTIAL`, `NOT LOADED`, `NOT
  TRANSLATED`, `IMPORT SILENTLY DROPPED`, `IMPORT ABORTS`, `NO FILE` or `NOT
  ACTIVATED`, ordered so failures that mask each other are reported in the order
  they must be fixed, alongside coverage against the module's own term count and
  the single call that moves that language forward. (#189)

- **Agent guidance sharpened** — `get_agent_instructions` returned a reference
  manual whose workflow steps were buried under material an agent can discover
  from the tools themselves; the bundled guide is now trimmed to the workflow
  essentials and is loaded once per session rather than before every call.
  Agents in `repo_url` mode are told to publish the branch with
  `git push -u origin HEAD` before the first `create_environment`, since Oduflow
  clones from the remote and cannot see a branch that only exists in the agent's
  local workspace (`local_path` mode stays exempt). Teardown now states plainly
  that deleting a finished environment is the expected end of a task rather than
  an exception, so environments stop piling up until the idle reaper collects
  them. And `psycopg2.pool.PoolError: The Connection Pool Is Full` finally has a
  documented remedy — raise `db_maxconn` once (8 → 16, ceiling 32) in the
  container's `odoo.conf` and restart — with the boundaries spelled out: a
  recurring pool error means leaking cursors, `FATAL: sorry, too many clients
  already` is the shared PostgreSQL limit where raising it makes things worse,
  and the container-local edit is lost on recreation, so permanent changes belong
  in `.oduflow/odoo.conf`. (#191, #192, #172, #178)

### Dashboard

- **Template settings editor** — a Template Settings modal inspects and edits a
  template's complete `metadata.json` without filesystem access, through
  structured controls for the common fields while preserving unknown metadata and
  keeping raw JSON editing available for advanced repairs. Invalid metadata stays
  visible and repairable rather than hidden, writes are atomic with optimistic
  revisions, and template listing is serialized against metadata updates so a
  derived size refresh cannot overwrite an edit. Repository names containing
  quotes, backslashes, apostrophes or JavaScript prototype keys are handled
  safely. (#168, #186)

- **The dashboard stays responsive during long operations** — several blocking
  Docker, Git, backup and filesystem calls ran directly inside async handlers, so
  one slow request stalled the event loop for every other one. That work moved to
  the thread pool, empty and non-JSON gateway responses (a proxy's HTML timeout
  page, for instance) are parsed safely instead of crashing the UI, save-as-template
  shows elapsed progress, and Odoo.sh import staging — previously protected only
  by the blocked loop — is now explicitly serialized. (#174)

### Bug Fixes

- **Declarative Stack plans now converge on the runtime they create** — apply
  rejects unavailable database templates before creating anything and treats a
  changed sanitization policy as replacement-only drift. Service image defaults
  no longer look like undeclared environment variables, text files up to the
  documented 1 MB limit compare cleanly, route paths are canonicalized before
  duplicate checks and hashing, and `stack validate` verifies every referenced
  local file. Environments created by the first Stack implementation carry a spec
  hash but no explicit sanitization label; an ambiguous mismatch there is now
  treated as replacement-only drift, so an unsanitized database can never be
  reported as converged. (#185, #187)

- **OAuth clients see the tools again** — in OAuth mode (Traefik routing or an
  explicit `oauth_base_url`) every authenticated request was denied: the OAuth
  provider handed out the MCP SDK's `AccessToken`, and FastMCP's
  `get_access_token()` rejects that type, so the scoped-access middleware — which
  calls it to read a token's environment scope, and fails closed — returned an
  empty tool list on `tools/list` and refused every `tools/call`, on both `/mcp`
  and `/mcp/<env>`. The provider now issues FastMCP's `AccessToken` on every
  path. Deployments using static Bearer tokens were unaffected. (#184)

- **A wedged Docker call can no longer take the server down silently** — startup
  runs migrations, `init_system` and quotas before the HTTP listener binds, and
  docker-py disables the socket timeout while reading exec output, so a Docker
  daemon restarting underneath Oduflow (typically `unattended-upgrades` letting
  `needrestart` restart Oduflow in the same batch as `containerd`) could block
  the start forever: an `active (running)` unit serving nothing, for hours. Four
  changes close that hole. A startup watchdog aborts the process — after dumping
  every thread's stack to the journal — when startup emits no log line for 15
  minutes, so systemd restarts instead of waiting; the PostgreSQL readiness
  probe now runs detached and polls `exec_inspect`, making its 30-second budget
  real; startup first waits up to a minute for the daemon to answer; and
  `oduflow systemd-install` now generates a unit ordered after
  `containerd.service` with `Restart=always` and no start-rate limit, plus an
  `/etc/needrestart/conf.d/oduflow.conf` override that keeps Oduflow out of
  needrestart's automatic restart batches. Existing installs get the unit and
  override by re-running `oduflow systemd-install`. (#177)

- **`http_request_to_odoo` now requests the path you pass it** — the base URL was
  taken from `get_environment_info()["url"]`, which ends in `/web?debug=1`, so
  appending the path buried it in the query string and every request silently hit
  `/web`. Both this tool and the environment readiness probe now build on the
  path-free `get_env_base_url`, making `wait_for_odoo_ready` check the real
  `/web/health` instead of passing because the login page rendered. (#166)

- **Deleting an environment no longer breaks the environment list** — the Docker
  SDK expands a container listing by inspecting each result, so a container
  removed inside that window raised `NotFound` and took down environment listing
  and the container-statistics endpoint. Both paths now ignore containers that
  disappear mid-listing; the Docker SDK requirement moves to 3.3.0 or newer,
  where `ignore_removed` exists. (#193)

- **Auto-install waits for the Odoo registry** — Odoo's HTTP listener can answer
  `/web/health` before registry preloading finishes, and starting a second Odoo
  process at that moment races the serving one, which cloned Odoo 15 databases
  hit while recreating registry signaling sequences. Readiness is now a
  cookie-aware `/web/login?db=…` probe against the target database rather than a
  database-independent health check. (#176)

- **Translation export creates a missing `i18n/` directory** — exporting a
  module's first catalogue wrote through `write_file_in_environment`, whose
  `mkdir -p` ran as the unprivileged Odoo user against a root-owned checkout and
  failed silently; the following archive upload then surfaced as a raw Docker
  404. Only the missing directory tree is now created as root and handed to the
  requesting user, and archive/ownership failures come back as ordinary
  `ExternalCommandError` responses. (#190)

- **A truncated state file no longer crashes a tool** — `activity._load` and
  `port_registry._load_registry` called `.items()` on whatever `json.load`
  returned while catching only decode/OS errors, so a file holding valid JSON of
  the wrong shape (a truncated write leaving `null`) raised `AttributeError`
  straight through the handler — aborting `stop_environment` over a corrupt
  best-effort tracking file. Both loaders now check the parsed shape. (#165)

- **Odoo.sh imports are resumable, and scoped access fails closed** — a sweep of
  the actionable review findings from earlier pull requests: import retries no
  longer lose staged artifacts, template finalization is retryable with explicit
  addon wiring under a strict default and an opt-in best-effort policy, an
  incomplete addon template is no longer reported ready, scoped MCP/token
  handling fails closed instead of open, duplicate token scans and inactive
  dependency files triggering restarts are gone, and WebSocket validation,
  accessibility and CI concurrency are hardened alongside. (#182)

### Documentation & Testing

- **Documentation re-audited against the public code surface** — the CLI, MCP,
  REST/WebSocket, TOML, production, ORM, authentication, and PostgreSQL-tuning
  references now match the implementation. The separate `oduflow upgrade` step
  is documented, and invalid named-option examples for `oduflow call` were
  corrected. `llms-full.txt` is now generated from every current manual page,
  with tests preventing undocumented commands, tools, routes, settings, or
  stale LLM output. `oduflow upgrade --force` makes bundled-file refreshes
  unattended while keeping warnings and `# KEEP` behaviour, and Ruff is pinned to
  a stable lint profile enforced in CI. (#167)
- **Record the translation tooling decision** — specs/0042 documents why the tools build on Odoo's own exporter instead of a hand-rolled term extractor, why one export primitive answers both "what is translatable" and "what actually loaded" across Odoo majors, and why retrieving a generated file needed a new one-time-token route. (#169)
- **Mutation testing across the pure-logic core** — a scoped `[tool.mutmut]`
  configuration now covers 39 modules, with the tests needed to kill the mutants
  it surfaced: killed mutants go from 1553 to 6409 and uncovered mutants from 585
  to 10, while the unit suite grows from 1249 to 1723 tests and still runs in
  about eight seconds. Almost every finding was a missing assertion rather than a
  bug — the one real defect it caught is the malformed-JSON crash above. A flaky
  chunkstore incremental-backup test that asserted on non-deterministic CDC chunk
  counts was rewritten around seed-independent invariants. (#165, #188, #179)

## v1.68.1

### Bug Fixes

- **Translation changes now reach the database** — a pull that only touched
  `i18n/*.po` was classified as a plain refresh (or restart), so the new terms
  were never loaded: translation catalogs only enter the database through a
  module install or upgrade. `classify_changes`/`shallow_classify` now classify a
  changed `.po` as an upgrade of the module that owns it, reported in the new
  `details["i18n_changed"]` list and merged across the main repository and the
  extra-addon worktrees. `.pot` templates stay ignored (Odoo never loads them),
  and a `.po` inside a module that is new in the same push keeps the stronger
  install action. The guardrail hint and the live-mount agent instructions —
  where the agent picks the action itself — now mention `i18n/*.po` as well.
  (#164)

## v1.68.0

### Features

- **Unified host resource planning** — dev PostgreSQL, production PostgreSQL, and production Odoo workers now consume one host-wide CPU/RAM plan that accounts for whether production hosting is enabled. Generated PostgreSQL configs carry a fingerprint so resource or mode drift is reported without silently rewriting a live database; `oduflow retune-postgres` previews every managed config diff, while `--apply` backs up and writes PostgreSQL configs, stages updated worker settings in existing Odoo containers, and leaves all restarts under operator control. Custom PostgreSQL configs require an explicit `--force`. (specs/0044)
- **Production hosting** — Oduflow now runs long-lived Odoo productions alongside ephemeral dev environments. A production is a namespaced environment (`prod-<name>`) with its own metadata plane: a per-team `productions.json` registry, a dedicated production PostgreSQL cluster with auto-tuned settings and Odoo worker tuning, a custom-domain Traefik route, a full git clone, and a production `odoo.conf` chain (no `--dev=xml`, no sanitization). Deploys reuse the shared pull → classify → apply engine with production semantics and verify health in-container; a failed deploy automatically rolls the checkout and extra-addon worktrees back to their pre-update commits, and `rollback_production` targets any commit in the recorded history. Backups are first-class: `pg_dump` streamed straight to S3, filestore snapshots through a new content-defined-chunking backup engine (`chunkstore/`), WAL-G WAL archiving with base backups and cluster PITR, plus a scheduler daemon for daily snapshots and weekly pruning. GitHub push webhooks (HMAC-verified, coalesced) auto-deploy matching productions, and a public `/healthz` endpoint reports dev/prod PG, Traefik, S3 and disk state. See `docs/production.md` and specs/0031. (#116, #125)
- **Production hosting is opt-in** — the production tier stays entirely dormant until it is enabled, so dev-only installs never grow a second PostgreSQL container. Re-enabling starts all managed productions, while an ordinary server restart no longer resurrects productions that were deliberately stopped. (#147)
- **External Traefik routes and operator drop-in config** — declarative `[route.<name>]` TOML sections forward arbitrary domains to upstreams Oduflow does not manage, reusing the team TLS/ACME logic. The Traefik dynamic configuration is now a watched directory, so operators can drop in their own `*.yml` files that Oduflow never overwrites; drift control recreates a Traefik container that is still on the old single-file provider. (#132, specs/0034)
- **`run_odoo_shell` commits by default** — `odoo shell` rolls back its cursor when the piped script ends, so ORM writes made through `run_odoo_shell` were silently discarded despite the docstring promising a commit. A new `auto_commit` parameter (default `True`) commits through a private cursor handle after the script succeeds; pass `auto_commit=False` for a read-only dry run. (#133)
- **Services report their reachable internal hostname** — service tools now return the exact internal hostname and distinguish it from the external routed URL, including host-mode services. Odoo containers also receive a host-gateway mapping so `host.docker.internal` resolves on Linux. (#163)
- **Richer coder shell toolbox** — the coder image now ships `curl`, `less`, `ripgrep`, `fd`, `tree`, and `python3` + `ruff`, so a hosted agent can make ad-hoc HTTP calls and lint Odoo Python locally before pushing. (#157)
- **Versioned coder runtime contract** — hosted agents now use the immutable `oduist/oduflow-coder:0.2.3` image instead of a rolling `:latest` tag. The publish workflow emits only versioned multi-architecture tags, legacy official `:latest` configuration resolves to the release's pinned image, and Oduflow pulls a changed image before replacing a working container. Container recreation is derived from the actual Docker run specification rather than a manually bumped runtime epoch.
- **Shared immutable extra-addons checkouts** — development environments using the same extra-addons commit now mount one persistent team-level checkout instead of materialising identical per-environment worktrees. Branch updates create a new SHA-keyed checkout and `pull_and_apply` switches only the requested environment, preserving independent database state; production keeps private worktrees for rollback. Existing environments migrate lazily on their next sync, and cached revisions remain until the extra repository is deleted.
- **Improved hosted agent runtime and MCP support** — the hosted Agent CLI and Agent Chat runtime gained better MCP wiring, a fixed Codex Agent Browser environment, and a short `-t` alias for `--transport`. (#142)

### Dashboard

- **Production tab** — productions are managed from the dashboard: status badge, custom-domain link, `branch@sha`, deploy history, snapshots and restore, logs, the webhook box, and a create modal, plus health chips in the system bar. (#116)
- **Create templates from the dashboard** — new templates can be created directly in the UI, with template saves serialized against environment operations. (#130)
- **Agent Chat polish** — activity details collapse, scroll position is preserved across updates, and file attachments (including cancelled uploads) are handled properly. (#150)
- **Connect As rework** — the user picker is split into dedicated Internal and Portal selects (portal membership resolved by xml_id rather than the imprecise `share` flag), and a single **Connect** button opens the environment already logged in. Login now works across environment subdomains in Traefik mode through a one-time host-bound token and a high-priority `/oduflow-connect` route, and `update_environment` recomputes Traefik routing labels so a routing-mode, TLS, or hostname switch can be repaired without destroying the database. (#137, specs/0036)
- **Agent Chat conversation history** — each environment and agent now keeps up to 20 recent conversations in MRU order, titled from the first user prompt. The new **History** menu resumes a selected conversation through ACP `session/load`, preserves legacy sessions without a migration, and recovers safely when an adapter cannot load an older session.

### Bug Fixes

- **Live-mount project sanitization uses the active checkout** — project scripts
  now live under `.oduflow/odoo_sanitize` and resolve against the actual
  `local_path` checkout instead of the empty managed-clone location. Existing
  repositories keep running with an explicit migration warning, and
  `get_environment_info` plus the dashboard now show the live-mount source path.
- **Agent Chat image uploads keep their filesystem path** — small images still
  reach multimodal agents as inline visual input, but now also retain the
  persisted `/workspace/.oduflow-uploads/...` resource link. Claude and Codex
  can therefore use the exact uploaded file with shell and Odoo tools instead
  of guessing temporary upload locations.
- **Hosted agents use the reachable scoped MCP endpoint** — Agent CLI and Agent Chat now use the team's public HTTPS `/mcp/<environment>` URL in Traefik mode (and an explicit `oauth_base_url` in port mode), matching the dashboard's **MCP Access** dialog. Local port-mode deployments retain the `host.docker.internal` fallback.
- **Claude Agent Chat explains rejected credentials** — provider credentials copied into `[team.*.agent_env]` now have surrounding whitespace removed before they reach the coder container. When Claude ACP returns a recognizable authentication failure, Agent Chat preserves the provider error and adds recovery specific to the active setup-token, API-key, or interactive-login mode. Oduflow still fails closed instead of silently switching accounts or billing methods.
- **claude.ai OAuth connector reaches the authorization endpoint** — a claude.ai custom connector derives its OAuth endpoints path-relative to the MCP URL (requesting `https://<host>/mcp/authorize` and `/mcp/token`) instead of following the root endpoints advertised by discovery. The outer scoped-access shim treated `authorize`/`token` as an environment name and rewrote the request onto the auth-protected `/mcp` endpoint, so the flow failed with `401 invalid_token` before it could start. Oduflow now routes the reserved OAuth/discovery sub-paths requested under `/mcp/` (`/mcp/authorize`, `/mcp/token`, `/mcp/register`, `/mcp/.well-known/*`) to the real root routes. Scoped `/mcp/<env>` connectors remain Bearer-only.
- **Claude Agent CLI onboarding is pre-seeded** — Claude Code onboarding state is written when an OAuth token or API key is configured, preserving valid settings and normalizing API-key approval so the CLI no longer stops on an interactive prompt. The resolved authentication mode (never the secret) is now logged on agent container creation, matching the documented troubleshooting flow. (#144, #146)
- **OAuth refresh tokens survive access-token expiry** — expired access tokens were pruned together with their non-expiring refresh partners, so refreshing failed after the access TTL or a server restart. Cleanup now removes only the access record while preserving pair revocation and rotation semantics. (#154)
- **Concurrent Connect As requests no longer collide** — every invocation gets a random temporary script basename and cleans up in a `finally`, so simultaneous requests against one Odoo container can no longer overwrite or delete each other's script. (#153)
- **Server shutdown no longer hangs on idle terminals** — the shell and SQL terminal websockets leaked a non-daemon executor thread parked in `recv()`, blocking interpreter shutdown and leaving the exec'd `odoo shell`/`psql` process running. Teardown now mirrors the agent console pattern: return on first completion, shut the Docker socket down to wake the blocked thread, then cancel and reap. (#151)
- **Template dumps are cleaned up and concurrency-safe** — every dump copied into the shared database container's `/tmp` is now tracked and removed in a `finally`, and staged under a unique per-restore name, so parallel imports cannot clobber each other and the `oduflow-db` writable layer stops growing without bound. (#139, #140)
- **Extra addon branches are validated up front** — a blank or missing branch is rejected with an actionable prerequisite error naming the repository, before `git worktree` is invoked. (#141)
- **Service errors are actionable** — a missing image or other Docker pull failure now surfaces as a safe, specific error across REST and MCP while preserving the existing container, and service names Docker cannot use are rejected with a `400` at the shared container-name boundary instead of a raw Internal Server Error. (#158, #161)
- **Quiet benign MCP disconnect logs** — a client disconnecting before a long-running tool finishes no longer produces an ERROR "Stateless session crashed" traceback; genuine crashes still propagate. (#138)
- **Agent Chat assets are cache-busted correctly** — `chat.js` and `acp-client.js` now share one cache version held in the dashboard template, so a single bump busts the interdependent pair and users can no longer end up on a fresh `chat.js` paired with a stale `acp-client.js`. (#148, #149)

### Security

- **Live-mount startup warning** — every server start with
  `allow_local_path=true` now warns that environment creators can bind host
  directories read/write and that hosted, remote, and multi-user deployments
  should disable the trusted local-development mode.
- **OAuth `client_id` is no longer the secret** — a team's OAuth `client_id` is now the non-secret identifier `team_<id>` (e.g. `team_1`); the team's `auth_token` is the `client_secret` and remains the issued access token. Previously `client_id == client_secret == auth_token`, so the secret appeared in the `/authorize` query string (and thus in server logs, browser history, and the `Referer`). The secret now travels only in the POST `/token` body. **Breaking:** re-enter any existing claude.ai connector as `Client ID = team_<id>`, `Client Secret = auth_token`.
- **OAuth issues independent, expiring, revocable tokens** — completing the previous item, the OAuth Authorization Code / refresh exchange now mints an *independent, opaque* `access_token` (with a rotating refresh token) instead of handing back the team's `auth_token`. A minted access token expires (~1h) and can be revoked via the now-enabled `/revoke` endpoint; using a refresh token rotates the pair so a leaked refresh token is single-use. The OAuth client (claude.ai/IDE) therefore never stores the team's long-lived master secret. Minted tokens are persisted (`oauth_token_store.py`) so live connections survive an Oduflow restart. The `auth_token` still works as a non-expiring direct Bearer credential for curl/CLI, and per-environment tokens remain Bearer-only. (#83, #135)
- **Hardened git credentials, import tokens, CSRF and Web UI auth** — inline URL credentials are redacted from git stderr and are no longer written into the world-readable `oduflow.repo` Docker label (they move into the team `.git-credentials` store, created `0700`/`0600`); import-from-Odoo endpoints accept the bearer token only via the `Authorization` header and require HTTPS, since the request carries the Odoo master password; `BasicAuthMiddleware` rejects cross-site state-changing requests and every WebSocket handshake via an Origin/Referer check; and the Web UI now default-denies with `401` when auth is enabled but no team resolves, instead of silently acting as team 1. Also fixes an output-cache id collision and a naive-timestamp timezone bug, and rejects environment names that slugify to an empty string. (#136)
- **Secrets kept off the process list and out of error pages** — the per-environment database password is passed to `run_odoo_tests`/`run_odoo_shell` via `PGPASSWORD` instead of `-w <password>` on the Odoo CLI (visible via `ps` inside the container), and the dashboard's HTTP 500 handlers return a generic message instead of `str(e)`, which could leak absolute paths and Docker internals. Details are still logged server-side; MCP responses, `FlowError` messages, 400 validation errors, and interactive terminals stay verbose. (#84, #134)

### Documentation & Testing

- **mypy strict is green and gated in CI** — `strict = true` had been configured from the first commit but never ran in CI and emitted 391 errors. All findings are fixed (including several dishonest return annotations and a namespace-package shadowing of the `docker` SDK by the repo's own `docker/` directory), the type-checker version is pinned via a `dev` extra, and a `mypy` job now runs on push and PR. (#131)
- **Production hosting documentation** — specs/0031 records the decision, `docs/production.md` covers configuration, deploys and rollback, webhooks, snapshots and restore, the cluster PITR runbook, `/healthz`, and the tool table; the bundled `oduflow.toml` gains commented `[production]` and `[backup]` sections. (#116)
- **First-run credentials and config paths** — the legacy `~/.oduflow/oduflow.toml` fallback is removed and the remaining `ODUFLOW_TOML` → `/etc/oduflow/oduflow.toml` → `~/.oduflow/conf/oduflow.toml` lookup order is documented, so first-run users know where the generated `auth_token` and `ui_password` live. (#152)
- **Record the versioned coder-image contract** — specs/0040 documents immutable image publication, server/image version coupling, safe replacement ordering, and removal of the rolling `:latest` channel.
- **Document the short transport flag** — HTTP server startup examples now show `oduflow -t http` / `uvx oduflow -t http` as the short form of `--transport http`.
- **Record the shared extra-addons cache decision** — specs/0039 documents SHA-keyed checkout sharing, isolation from moving branches, persistent cache lifecycle, and why production remains on private worktrees.
- **Document the agent UI live-mount limitation** — the dashboard agent UI cannot be used with live-mounted (`local_path`) environments; this is now stated explicitly in the docs.

## v1.67.0

### Features

- **Restricted HTTP path routing for services** — auxiliary services can now expose a curated set of `routes` alongside (and mutually exclusive with) the existing catch-all `port` model. Restricted routes use segment-safe Traefik rules, work for both bridge and host networking, can optionally strip prefixes, and leave every undeclared path at Traefik's 404 so a service no longer has to publish its whole HTTP surface. The route configuration is preserved through presets, live inspection, updates, MCP, REST, and the dashboard. (#126)

### Bug Fixes

- **Dependency changes are applied on Sync** — a changed `requirements.txt`, `.oduflow/requirements.txt`, or `.oduflow/apt_packages.txt` during `pull_and_apply`/Sync now reinstalls the apt/pip dependencies into the running container and restarts it, instead of being misreported as an XML/JS-only browser refresh. The classifier previously ignored these files, so a dependency-only change silently did nothing. Reinstall runs before any module install/upgrade so new libraries are importable; packages *removed* from the file are not uninstalled until the container is rebuilt via `update_environment`. (#127)
- **Real client IPs behind Traefik** — access logs and the login rate-limiter recorded the Traefik container IP for every request instead of the real client, because uvicorn only trusts `X-Forwarded-For` from `127.0.0.1` by default. Oduflow now passes `proxy_headers=True` with a `forwarded_allow_ips` trust list — the stable Traefik Docker-network CIDRs in `traefik` mode, and nothing in `port` mode so a direct client cannot spoof its IP. The login rate-limiter now throttles per real client. (#123, #124)
- **Bounded MCP HTTP graceful shutdown** — HTTP transport now applies a 10s Uvicorn graceful-shutdown timeout so long-lived MCP StreamableHTTP streams no longer keep server restarts waiting indefinitely. (#122)

### Security

- **Hardened proxy-header trust and credential redaction** — forwarded headers are trusted only from loopback plus the stable Traefik Docker-network CIDRs, failing closed for empty team maps and when Docker-network trust cannot be established; embedded repository credentials are now redacted from non-auth git clone failures; and a staged template filestore replacement that fails to install preserves the previous filestore. (#124)

### Documentation & Testing

- **Restricted service-routing decision record** — specs/0033 documents the restricted HTTP path-routing model for services, its security boundary, and the trade-offs versus catch-all port exposure. (#126)
- **Superseded PR test runs are cancelled** — `tests.yml` now uses a per-ref `concurrency` group with `cancel-in-progress`, so pushing a new commit to an open PR cancels the still-running test workflow for that ref while keeping `main` protected by pre-merge CI. (#128)

## v1.66.0

### Features

- **Traefik certificates available to auxiliary services** — in Traefik TLS mode every created or recreated service receives the exact `oduflow-traefik-acme` volume at `/etc/traefik` read-only, with no per-service TLS flags or wildcard allowance for other system volumes. Existing services gain the mount on their next update.
- **Database-only Odoo template import** — `oduflow import-template` and `import_template_from_odoo` now accept `--without-filestore` / `without_filestore=True`, requesting a PostgreSQL custom-format dump without filestore files and deriving template metadata after restore.
- **Attach separate template filestores** — new `oduflow attach-filestore` CLI and `attach_filestore` MCP tool attach or replace a template filestore after a database-only import. Sources can be local directories, zip/tar archives, `rsync://` URLs, or SSH rsync paths; wrapper directories such as the database name are auto-detected and live overlay env changes are preserved by default.

### Bug Fixes

- **Invalid service volume updates are non-destructive** — `update_service` now resolves the complete candidate volume configuration before stopping the running container, so a missing or reserved volume no longer deletes the service before returning an error.
- **Generated PostgreSQL passwords are safe for the Odoo entrypoint** — per-environment passwords that randomly began with `-` were parsed as another CLI option, leaving `--db_password` without a value and putting the Odoo container into a restart loop. Password generation now retries that rare token shape.
- **Odoo template import now refuses existing template names** — `import-template` fails before DB listing or backup download if the target template directory or template database already exists, avoiding accidental overwrites.
- **Database-only template import can restore newer custom dumps** — when the shared PostgreSQL container's `pg_restore` cannot read a custom dump archive version, Oduflow converts it to plain SQL with a temporary `postgres:17` helper container and restores that SQL through the shared database container.

### Documentation & Testing

- **Implicit service certificate-store decision record** — specs/0032 records the deliberate shared-read, read-only ACME mount and its trust-boundary consequences.

## v1.65.0

### Features

- **Per-team OAuth issuer with host-relative discovery** — in traefik mode, each team's OAuth Authorization Server derives its issuer from the incoming Host, so each team runs OAuth on its own already-certificated hostname with no need for a central `oauth_base_url`. `OduflowOAuthProvider` makes discovery metadata host-relative, `HostRelativeAuthChallenge` rewrites 401 challenge origins, and `[routing].hostname` is now validated per-mode (shared default deleted in traefik). (#112)

### Security

- **Hardened web-UI and MCP surfaces** — fresh installs and upgrades now bootstrap a `ui_password` so the dashboard is never served unauthenticated; the interactive shell, SQL, agent, and service-creation surfaces are fail-closed. Closed two SSRF vectors (`http_request_to_odoo` path rewrite and `setup_repo_auth` loopback/link-local validation), removed the shared-PostgreSQL-superuser fallback so tenants cannot cross databases on legacy envs, added validation guards and escaped output across the surface. (#110)
- **psql WebSocket branch validation** — `ws_sql_terminal` now validates the branch path param before lookup to close an opaque resolution path in scoped-credentials. (#114)

### Bug Fixes

- **Overlay filestore no longer duplicated into each environment** — `_ensure_user_site_packages` was recursively chowning `.local/share`, causing fuse-overlayfs to copy the entire template filestore (~10 GB) into each env's upper layer, defeating overlay space savings. Now chowns only the pip dirs non-recursively, keeping filestore overlay-bound. (#113)
- **Overlay unmount now works on Ubuntu 24.04 with enforced fusermount AppArmor profile** — switched mount cleanup to try clean `umount` first (not mediated by the fusermount3 profile on Ubuntu 24.04+), falling back to `fusermount`/`fusermount3` helpers and lazy `umount -l` only as a last resort. (#113)
- **Multi-team UI password provisioning for newly-added passwordless teams** — `_ensure_web_ui_password` was using `any()` so a multi-team config with one team already set would skip provisioning for others, locking them out. Now uses `all()` to provision every team and enforce that all are set at startup. (#114)
- **Template clone now reassigns objects owned by any non-env role** — when creating an environment from a template imported via `import_template_from_odoo`, objects owned by roles other than the template env's own role were not reassigned, leaving the new env unable to touch them. Now reassigns all non-env-role-owned objects unconditionally. (#111)
- **Native Odoo neutralize skipped for Odoo 15** — the official `odoo:15.0` image does not include the `odoo neutralize` CLI, so Oduflow now skips it for Odoo 15 and tries only for Odoo 16+, keeping custom `.odoo_sanitize` scripts as the baseline. (#109)

### Documentation & Testing

- **Per-team OAuth decision record** — specs/0020 captures the evolution from a static `oauth_base_url` to host-relative issuer discovery. (#112)

## v1.64.0

### Features

- **Odoo.sh addons-path import** — extend push-based import to carry over the addons-path Odoo.sh ran with, supporting Enterprise, Themes, and extra repos. The client detects the live addons-path, classifies each entry, tars private repos and announces reachable extras to the server. Also adds `rename_template` MCP tool, REST endpoint, and dashboard button to rename template directories and their PostgreSQL backing databases. (#108)
- **Chunked Odoo.sh import uploads** — split large SQL dumps and addon tarballs into resumable chunks to pass proxy body-size limits (e.g., Cloudflare's 100 MB cap). Resume state is derived from what is already staged on disk. (#108)
- **Local extra-addons repos** — `extra_addons.create_local_repo` seeds a remote-less bare repo from files with a `.local` marker; fetch short-circuits so worktree creation never attempts a remote pull. (#108)

### Dashboard

- **Import acknowledgement gate** — Enterprise/Themes/Extra addon checkboxes now gated behind a licensing acknowledgment, linking to `oduflow.dev/odoo-sh-import-notes` for documentation. (#108)

### Documentation & Testing

- **Odoo.sh import decision record** — specs/0030 captures the architecture and rationale for the resumable, push-based import model. (#108)

## v1.63.0

### Features

- **Native Odoo database neutralization** — sanitized template-based environments now run Odoo's own `odoo-bin neutralize` inside the serving container after auto-installed modules are present, then continue through the existing custom `.odoo_sanitize` scripts. This gives copied databases Odoo's module-aware baseline protections for outgoing mail, crons, payment providers, connectors and webhooks without adding a new config knob; failures remain warning-only so provisioning is not blocked. (#107)

### Dashboard

- **Unlicensed badge signal** — the unlicensed license badge now plays a brief red edge pulse and jolt once on dashboard load as a registration nudge, while keeping the text as the actual status signal and skipping the animation entirely for reduced-motion users. A follow-up tightened the animation cleanup path so reduced-motion sessions do not retain unused listeners. (#104, #105)

### Documentation

- **Release workflow captured** — a project-scoped `/publish-release` skill now records the Oduflow release flow: analyze commits since the last tag, propose the version/changelog/title for sign-off, bump and push the release commit, publish the GitHub Release that triggers PyPI and Docker, then verify artifacts. (#106)

## v1.62.0

### Features

- **HTTP-only Traefik mode** — a new `[routing].tls` toggle (bool, default `true`). With `mode = "traefik"` and `tls = false`, Traefik listens on plain HTTP `:80` only — no HTTP→HTTPS redirect and no Let's Encrypt/ACME — for running behind an upstream that already terminates TLS (e.g. a Cloudflare tunnel). Environment/service routers bind to the `web` entrypoint without TLS, the `acme_email` requirement is lifted, and `forwardedHeaders.insecure` lets the upstream's `X-Forwarded-Proto: https` through so cookies stay `Secure` and public URLs stay `https://`. `_ensure_traefik` self-corrects on a flipped `tls` setting by recreating the container. (#103)

### Bug Fixes

- **Agent checkout removal guard** — `_agent_remove_env` now refuses when an environment name slugifies to an empty string, which would resolve the checkout dir to `/workspace/` itself and let `rm -rf` wipe every environment's checkout from the shared volume (matching the guard `clone-env.sh` already had on the create side). (#100)
- **Agent session file permissions** — `agent_sessions._save` now creates its temp file `0600` from birth via `os.open` (the mode carries over through `os.replace`), closing the brief default-permissions window that existed with chmod-after-rename. (#100)
- **Surfaced degraded agent modes** — non-fatal `_chat/notice` frames now render as system lines in Agent Chat, making two previously silent degraded modes visible: an environment without a scoped MCP token, and Codex chat's missing Oduflow MCP wiring. (#100)

### Documentation

- **Docs synced with code** — a documentation audit corrected several drifts: the retired "PolyForm Noncommercial 1.0.0" license name → BUSL-1.1 in `llms.txt`/`llms-full.txt`; `auto_delete_hours` default corrected to `0` (opt-in) across pages; `[server].allow_local_path`, `allow_insecure_http` and `[routing].hostname` documented; and the v1.61.0 coding-agent feature fully documented (new `docs/agent.md`, installation/web-api pages, `llms.txt`/`llms-full.txt`). (#101, #102)

## v1.61.0

### Features

- **Per-team coding agent — Agent CLI & Agent Chat** — an opt-in hosting feature that runs one coding-agent container per team (`oduflow-{team}-agent`) on the team's isolated network, with persistent HOME/workspace volumes and one git checkout per environment. The agent drives environments only through the Oduflow MCP server (git push → `pull_and_apply`). Two front-ends ship: **Agent CLI**, the agent's own TUI in xterm.js over a WebSocket ↔ `docker exec` bridge; and **Agent Chat**, a framework-free browser client talking to the agent's ACP adapter, with a durable session per (environment, agent) that resumes via ACP `session/load` and minimizes to a dock so several chats run in parallel. Configured statically in `oduflow.toml` (`agent_enabled`, `agent_default`, `[team.X.agent_env]`); the container carries a config-hash label and is recreated automatically on drift. The agent UI is hidden for live-mount environments. (#99)
- **Scoped per-environment MCP tokens for agents** — the team `auth_token` never enters the agent container; each console/chat session injects the environment's scoped `/mcp/<env>` token (default-deny dev-loop allowlist) into its own `docker exec` environment via a `${ODUFLOW_MCP_TOKEN}` placeholder, so no secret lands on disk and a leaked session credential grants only the one environment it already controls. (#99)
- **Coder image (`oduist/oduflow-coder`)** — a new merge-gated `publish-coder.yml` workflow publishes the agent image, which redistributes only Apache-2.0 software (Codex CLI + codex-acp); Claude Code and its ACP adapter are installed at first container start onto the persistent home volume. (#99)

### Bug Fixes

- **Traefik dynamic config now actually loads** — the per-team hostname→dashboard routing config was written as `.json`, which Traefik's file provider rejects ("unsupported file extension"), so it never loaded. It is now written as `oduflow.yml` (JSON is valid YAML, so no new dependency), placed under `etc_dir` (`/etc/oduflow` when writable, else `~/.oduflow/conf`) with the parent directory created first so a first traefik-mode start no longer crashes on macOS. Migration 0005 removes a still-running Traefik container that mounts the old `.json` path (the ACME volume persists, so certificates survive) and system init recreates it with the corrected path. (#98)

## v1.60.0

### Features

- **Scoped single-environment MCP access** — a new `/mcp/<env>` endpoint exposes only the in-environment dev-loop tools (sync, install/upgrade modules, tests, Odoo shell, SQL, file read/write/search, HTTP request, logs, info, restart) and denies every lifecycle/system tool. Access is isolated by a per-environment token generated at create time and stored in the `oduflow.mcp_token` container label; the Secret Key works as a Bearer token or an OAuth client credential. A dashboard **More → MCP Access** modal surfaces the URL and Secret Key. (#96)

### Dashboard

- **Minimize-to-dock windows** — the Log, Console (Odoo shell) and SQL Console modals gain a minimize button that docks the window as a chip in the system bar. Minimizing only hides the overlay, so the WebSocket, xterm buffer and log state stay alive and restoring resumes the exact session; reopening a window of the same type replaces the docked session without leaking its WebSocket, and focus returns to the launching control across the cycle. (#95)

### Documentation

- **Per-version Odoo development guides (15–19)** — `get_odoo_development_guide` now serves concise, self-contained per-version cheat sheets covering the key conventions and breaking changes for each release, plus a "Migrating a module to this version" block (migration scripts, pre/post split, openupgradelib helpers) for 16–19. Version-boundary facts were verified against `odoo/odoo` 16.0–19.0. (#97)

## v1.59.0

### Features

- **Multi-tenant hosting: hard tenant isolation** — a hosting-grade tenancy pass so environments running arbitrary client code cannot reach across teams. Container names are now team-scoped (`oduflow-{team}-{env}-{type}` and `oduflow-{team}-svc-{name}`); each team gets its own Docker network (only the shared PostgreSQL and Traefik bridge across teams), its own PostgreSQL tablespace (`oduflow_team_{id}`), and default per-container memory/pids limits auto-derived from host size. Odoo-style startup migrations (recorded in `migrations.json`) retrofit existing installs in place — renaming containers, moving tablespaces, and re-homing networks — with no manual steps and a clean resume after a partial run. (#91)
- **Per-team quotas** — `db_quota_gb` (default 50, 0 = off) caps the combined size of a team's PostgreSQL databases, checked before any operation that creates a new database; `disk_quota_gb` (default 0) is enforced on XFS with project quotas, giving a team's files and its PG tablespace one project ID and a single kernel limit. (#91)
- **Per-environment usage stats** — environment cards show DB size / disk usage after the CPU/RAM stats with a per-card refresh control, backed by a cached storage-stats subsystem and a `GET /api/usage` + `POST /api/usage/refresh` REST surface that external billing/quota tooling can consume per team. (#91)
- **Strict HTTP team resolution** — the single-team fallback now applies to stdio only; in HTTP mode an unresolved request is rejected unless `allow_insecure_http` is set, and multi-team HTTP requires an `auth_token` for every team at startup. (#91)

### Bug Fixes

- **Review cleanups** — dropped dead team-guard branches now that `team` is a required argument, extracted the duplicated Traefik-label helper to module level, and `fsync` before the atomic storage-cache rename so a crash mid-write cannot leave an empty file. (#93)

## v1.58.0

### Licensing

- **Relicense to Business Source License 1.1** — replaces PolyForm Noncommercial 1.0.0 with BUSL-1.1 (the canonical MariaDB text). The Additional Use Grant keeps production use free forever for non-commercial purposes (evaluation, education, personal projects, non-profits) and defines three commercial tiers — Individual, Business (internal use), and Integrator (Odoo services to third parties) — matching the existing license-key types. Each release converts to MPL 2.0 four years after publication, per standard BSL mechanics. (#90)

## v1.57.0

### Features

- **Neutral dashboard create buttons** — the `.btn-create` primary buttons move from a solid Console Blue fill to the outline treatment (surface fill, hairline border, ink text) that reveals a blue border, text and 10% tint on hover/focus, matching the Sync/Logs actions; weight 600 and the larger radius keep them distinguishable without a resting fill. The destructive confirm keeps its solid red. (#87)

### CI

- **Automatic Docker image publishing** — a new `docker.yml` workflow builds and pushes `oduist/oduflow:<version>` plus `:latest` (linux/amd64 + arm64, via Buildx/QEMU) to Docker Hub on every published GitHub Release — the same trigger as the PyPI publish. The version comes from the release git tag; `latest` is skipped for pre-releases. (#88)

## v1.56.0

### Features

- **Import templates from Odoo.sh** — Odoo.sh blocks `pg_dump` and `/web/database/manager`, so import is inverted: a push-based, resumable shell client (served at `/import-odoo.sh`, launched from the dashboard "Import from Odoo.sh" button) rides the platform's own daily backup, streaming the SQL dump as-is and the filestore tar'd per hash-directory chunk to short-lived (15 min), token-authed ingest endpoints that stage and restore it through the existing template machinery. Resume state is derived from what is already staged on disk, so a fresh token minted after the previous one expired continues the transfer where it stopped. (#85)

### Security

- **Odoo.sh import hardening** — closed an auth-bypass where the public ingest prefix also exposed sibling routes like template deletion; uploads now land in a staging dir (no fake-resume over an existing template, no writes into a mounted overlay lower layer); chunk extraction is atomic (a truncated upload is never treated as finished on resume); and the client resolves http→https redirects up front, streams with `curl -T` instead of buffering whole payloads in RAM, and fails fast on non-JSON responses instead of crashing with a raw traceback. (#89)

### Dashboard

- **Import from Odoo.sh modal** — widened to 80vw, with the input placeholder updated to `oduist-prod`. (#92, #94)

## v1.55.0

### Security

- **Security & bug audit remediation (17 fixes)** — a broad hardening pass across the tenancy and request surfaces: `template_name` is now validated at both the filesystem and PostgreSQL-identifier sinks (blocking path traversal and SQL injection); env DB roles no longer receive cluster-superuser membership (closing a `SET ROLE` → `COPY … TO PROGRAM` RCE) and the post-clone ownership transfer was made comprehensive; reserved `oduflow-*` volumes can no longer be mounted by a service; SSRF guards were added for `import_from_odoo` and git remote URLs; zip-slip/tar extraction is now containment-checked; `get_client` raises a catchable `FlowError` instead of `SystemExit`; the web create handler no longer releases a lock it does not own; and overlapping team port ranges are rejected in `Settings.validate()`, with constant-time secret compares and atomic credential writes. (#82)

### Bug Fixes

- **Destructive auto-delete off by default** — `auto_delete_hours` now defaults to `0` (opt-in) so a stopped environment is never silently deleted; the non-destructive 48h auto-stop stays on, and enabling auto-delete logs a prominent warning naming the consequence and how to disable it. (#82)
- **`oduflow cleanup --force` actually removes orphans** — `cleanup_orphans` now passes the per-team settings to `_unmount_filestore`, fixing an `AttributeError` that was silently swallowed while the command reported success but removed nothing. (#82)

## v1.54.0

### Bug Fixes

- **Repo `odoo.conf` changes now actually apply** — a changed `.oduflow/odoo.conf` is now reconstructed (merged `addons_path`, stripped `db_*`) and copied into the container before the restart during `pull_and_apply`, in both the restart and upgrade-then-restart paths. Previously (#69) it only triggered a plain restart, which reused the stale `/etc/odoo/odoo.conf` copy and silently ignored the new config; the regeneration only ever ran on a full `update_environment` recreate. (#81)

## v1.53.0

### Features

- **`addons/` sub-directory auto-detected** — when the main repo keeps its modules under a top-level `addons/` directory, the generated `odoo.conf` now points `addons_path` at `/mnt/extra-addons/addons` instead of the repo root. Odoo scans `addons_path` non-recursively, so the root mount alone would miss modules nested under `addons/`; detection is automatic and falls back to `/mnt/extra-addons` otherwise. (#80)
- **Complete bundled config template** — the shipped `oduflow.toml` now includes every option `Settings.from_toml` reads, adding the `[lifecycle]` section (`auto_stop_hours`, `auto_delete_hours`) and a commented `routing.hostname` fallback, so no knob is left undocumented. (#86)

## v1.52.0

### Dashboard

- **`Created` label for services** — service cards now show a Created timestamp. (#79)

### Documentation

- **Release process** — documented the mandatory GitHub Release step (the event that actually triggers the PyPI and Docker publishes) in `AGENTS.md`.

## v1.51.0

### Features

- **Auto-generated DB superuser password** — on a fresh install the bootstrap injects a random `secrets.token_urlsafe(24)` password into the generated `oduflow.toml` `[database]` section, so every install gets a unique PostgreSQL superuser secret. The bundled template no longer ships the hardcoded `password = "odoo"`; an explicit `[database].password` still overrides, and existing configs are never rewritten. (#74)
- **Auto-tuned PostgreSQL config** — first-time system init now generates `postgresql.conf` from the host's detected CPU/RAM (new `pg_tune.py`) instead of copying a static file hardcoded for "2 vCPU / 4 GB / HDD". The tuning is lean and SSD-oriented for a host running many lightweight single-user Odoo environments (e.g. `shared_buffers` ≈ 10% of RAM, floored at 128 MB and capped at 1 GB); the generated file is marked `# KEEP` so `oduflow upgrade` never overwrites it, and falls back to the static bundled config on any failure. The template `db_maxconn` was lowered from 16 to 8. (#67)
- **Restart on repo `odoo.conf` changes** — changes to the repository-level `odoo.conf` now trigger an environment restart during `pull_and_apply`. (#69)
- **Version logging on init** — the Oduflow version is now logged during system initialization. (#70)

### Dashboard

- **Remove templates** — each card in the Templates tab gains a Remove action (with a danger confirm dialog), wired to a new `POST /api/templates/{name}/delete` endpoint. (#76)
- **Wider logs modal + wrap toggle** — the logs modal now fills 80vw instead of being capped at ~900px, and a new "Wrap" toggle switches long log lines (tracebacks, SQL) between no-wrap and soft-wrap to avoid horizontal scrolling. (#75)
- **Calmer template badges** — the Templates tab's readiness badges are toned down (lower-saturation green fill plus a hairline border) and the `Copy`/`Overlay` mode badge is now a neutral gray chip rather than a success-green one, ending the eye-straining "wall of green". (#77)
- **Branch-header commit & PR links** — the Pull Requests and Commits icon-links moved from the variable-length Repo URL line into the card header next to the branch name, giving them a stable, easy-to-find position. (#73)
- **Copyable container names** — running-environment container names are now click-to-copy via the existing clipboard affordance.
- **Stopped status + service env vars** — environments whose containers are all stopped now report a distinct "stopped" status (amber) instead of "partial", and the redundant exited-container row is dropped; service environment variables moved out of the card into an "Info" modal that lists the real (unmasked) `KEY=value` pairs. (#66)

### Bug Fixes

- **`oduflow call` async tools** — the CLI `call` subcommand printed `<coroutine object ...>` (and a "coroutine was never awaited" warning) for tools wrapped by `@handle_errors`; their awaitable results are now run to completion via `asyncio.run()`. Sync tools are unaffected. (#68)

### Documentation

- **Architectural decision records** — the project's macro-level decisions were reconstructed from git history into ADR-style records under `specs/` (0001–0022) with a chronological index, and a "Record Architectural Decisions" rule was added to `AGENTS.md` so future decisions are captured alongside the change that introduces them. (#72)

## v1.50.5

### Features

- **Light theme** — the dashboard and login page gain a light theme alongside the dark "Engineer's Console" field. It follows the OS (`prefers-color-scheme`) by default; a header toggle cycles **System → Light → Dark** and persists the choice to `localStorage`, applied before first paint so there is no flash. It is built by re-pointing the existing `:root` color tokens — signal colors switch to AA-verified darkened variants on the light field, while the embedded terminal stays dark — and the logo is now transparent so it reads cleanly on either field.
- **Larger dashboard type scale** — 68 hardcoded `px` font sizes were replaced with six semantic rem tokens (`--text-2xs`…`--text-xl`), raising every tier ~+2px (body 13→15px, titles 15→17px, header 20→23px) for readability at density. (#63)
- **Note dialog Delete button** — the environment note editor now has an explicit Delete action. (#63)


## v1.50.4

### Features

- **MCP bootstrap instructions** — the MCP initialize response now tells agents to load `get_agent_instructions` first and then call `get_odoo_development_guide(version=...)` before writing Odoo module code.

## v1.50.3

### Features

- **Git-independent live-mount apply flow** — `local_path` environments now use an Oduflow-owned per-environment file snapshot instead of Git status to detect local changes. Git commits are optional in live-mount mode, non-git folders are supported as-is, and already-applied dirty/untracked files are no longer reported repeatedly.
- **Config-gated live-mounts** — added `[server].allow_local_path` (default: `true`) to enable the single-developer live-mount workflow by default while still allowing operators to disable local bind mounts explicitly.
- **Mode-aware agent instructions** — `get_agent_instructions` now starts with a dynamic code-delivery-mode preface. When a live-mounted environment is active, agents see the local workflow first, including the explicit `install`/`upgrade`/`restart` rules that apply to snapshot-based detection.

### Bug Fixes

- **Live-mount templates in Web UI** — environments created from templates that record `local_path` can now be recreated as live-mounts when `allow_local_path` is enabled, instead of always being rejected as HTTP-only.
- **Local `pull_and_apply` repeat detection** — snapshots advance only after successful apply operations, and stay unchanged when strict guardrails block or install/upgrade fails.

### Documentation

- **Agent guide local workflow** — clarified the split between `repo_url` mode (`commit` → `push` → `pull_and_apply`) and `local_path` live-mount mode (edit local files directly; commits optional; pass explicit actions for database-affecting changes).

## v1.50.2

### Bug Fixes

- **License config path** — license activation now stores `license.key` in the resolved Oduflow config directory instead of hard-coding `/etc/oduflow`; when `/etc/oduflow` is not writable it follows the same `~/.oduflow/conf` fallback as the rest of the config. The dashboard license API now uses the running server settings, so the unlicensed badge still appears when no key is installed. ([fe142ec](https://github.com/oduist/oduflow/commit/fe142ec))
- **CLI startup warning** — suppress the noisy Authlib deprecation warning emitted by FastMCP on startup. ([44872e8](https://github.com/oduist/oduflow/commit/44872e8))

### Documentation

- **MCP tool count** — correct the documented MCP tool count from 43 to 54. ([513a53d](https://github.com/oduist/oduflow/commit/513a53d))
- **Release process** — document that version and changelog updates must be committed before creating the release tag. ([9371be5](https://github.com/oduist/oduflow/commit/9371be5))

## v1.50.1 (since v1.20.1)

### Breaking Changes

- **`rebuild_environment` renamed to `update_environment`** — the tool (and its REST route `/api/environments/{branch}/update`) now also accepts `odoo_image` and `env_vars` to switch the image and replace container environment variables. Called with no arguments it behaves exactly like the old `rebuild_environment` (re-create the container, preserving DB and filestore). ([a13c73a](https://github.com/oduist/oduflow/commit/a13c73a))
- **Removed `template-up` / `template-down` CLI commands** ([8342768](https://github.com/oduist/oduflow/commit/8342768))

### Features

- **Stdio live-mount + explicit `pull_and_apply` guardrail** — `create_environment(local_path=...)` bind-mounts the agent's own checkout live into the container (stdio transport only), so edits apply instantly with no git push/pull; `pull_and_apply` is transport-agnostic and takes explicit `install`/`upgrade`/`restart` actions, with a guardrail that cross-checks the requested action against the detected diff and surfaces non-blocking warnings ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Environment lifecycle automation** — idle environments auto-stop and long-stopped environments auto-delete after configurable hours (protected environments are exempt); container-level tools auto-wake a stopped environment and prepend a note; activity is tracked per team in `activity.json` ([e992971](https://github.com/oduist/oduflow/commit/e992971))
- **Environment variables for environments** — `create_environment` accepts `env_vars` (comma-separated `KEY=VALUE`) injected on top of the database connection variables; `update_environment` can replace them later. Env vars are persisted on the container, reported by `get_environment_info`, and editable from the web dashboard create form ([a13c73a](https://github.com/oduist/oduflow/commit/a13c73a))
- **Self-hosted OAuth Authorization Server for MCP clients** ([5f32f58](https://github.com/oduist/oduflow/commit/5f32f58))
- **GitHub OAuth support for MCP HTTP transport** ([97c3fc8](https://github.com/oduist/oduflow/commit/97c3fc8))
- **`auto_install_modules` parameter** for environments and templates ([e868d90](https://github.com/oduist/oduflow/commit/e868d90))
- **Docker volume management** — manage persistent service data via Docker volumes, mount external Docker volumes to services, and browse/manage files inside volumes ([6b05f8a](https://github.com/oduist/oduflow/commit/6b05f8a), [a396583](https://github.com/oduist/oduflow/commit/a396583), [5cabc2c](https://github.com/oduist/oduflow/commit/5cabc2c))
- **Host network mode for services** ([d4609e6](https://github.com/oduist/oduflow/commit/d4609e6))
- **`privileged` and `NET_ADMIN` capability options for services** ([edad2ef](https://github.com/oduist/oduflow/commit/edad2ef))
- **`restart_service` and `run_service_command` tools** with increased summary limits ([a2fa612](https://github.com/oduist/oduflow/commit/a2fa612))
- **`update_service` can change any setting** — env, image, port, hostname, host_mode, volumes, privileged, and net_admin ([970aba4](https://github.com/oduist/oduflow/commit/970aba4), [5bb3f7e](https://github.com/oduist/oduflow/commit/5bb3f7e))
- **`get_service_info` tool** ([27ec4c9](https://github.com/oduist/oduflow/commit/27ec4c9))
- **Non-destructive template update for fuse-overlayfs environments** ([1edafff](https://github.com/oduist/oduflow/commit/1edafff))
- **`reload-template --source` flag** for S3/local sync ([2e2e0cd](https://github.com/oduist/oduflow/commit/2e2e0cd))
- **`# KEEP` marker** to protect files from being overwritten during upgrade ([a97e13f](https://github.com/oduist/oduflow/commit/a97e13f))
- **Read repo-level `odoo.conf` from `.oduflow/` directory** ([48eea3e](https://github.com/oduist/oduflow/commit/48eea3e))
- **`created_at` timestamp, editable notes, and preset-based service updates** ([b849551](https://github.com/oduist/oduflow/commit/b849551))
- **odoo.conf `db_maxconn=16`** — bumped from 4, which was too small ([90e7e19](https://github.com/oduist/oduflow/commit/90e7e19))

### Dashboard

- **Engineer's Console redesign** — visual system aligned with oduflow.dev (OKLCH tokens, Outfit + Geist Mono, signal palette); all assets vendored (xterm.js and fonts, no CDN, works air-gapped); state-driven Start/Stop toggle; in-system dialogs replace native `confirm()`/`prompt()`; accessibility (focus traps, ARIA roles, keyboard navigation, ≥44px touch targets); responsive down to 390px; single polling interval; real RAM/CPU metrics on macOS ([e992971](https://github.com/oduist/oduflow/commit/e992971))
- **Session-cookie auth with login form and logout** — replaces the HTTP Basic browser dialog; signed `itsdangerous` session cookies (7-day expiry, persistent server-side secret); Basic auth retained for API/CLI clients ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))
- **Show git branch in env list** when it differs from the environment name ([ae5441d](https://github.com/oduist/oduflow/commit/ae5441d))
- **`host_mode` checkbox** on service creation and restore forms ([73fbee7](https://github.com/oduist/oduflow/commit/73fbee7))
- **PR and commits icon-links** next to the repo URL ([25017cd](https://github.com/oduist/oduflow/commit/25017cd))
- **Copy DB name on click** ([7f8d654](https://github.com/oduist/oduflow/commit/7f8d654))

### Bug Fixes

- **WebSocket terminal auth** — Console/SQL terminals failed because browsers can't send a Basic auth header on a WS handshake; added a signed cookie auth fallback for HTTP and WebSocket scopes ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))
- **Greenfield DB init race** — for `template_name=none`, initialize the empty DB in an isolated short-lived container before the serving container starts, avoiding a concurrent `orm_signaling_registry` collision that left `base` uninstalled ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Orphan PG role on template restore** — restore env-derived templates with `--no-owner` so deleting the source environment can drop its per-environment role ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Test port flag by Odoo version** — pick `--longpolling-port` vs `--gevent-port` based on the environment's Odoo major version ([79b1c35](https://github.com/oduist/oduflow/commit/79b1c35))
- **`run_odoo_tests` port & execution** — pass `--no-http --workers 0` to avoid an 8069 conflict; override test ports and use `-u` so tests actually run ([6311dc4](https://github.com/oduist/oduflow/commit/6311dc4), [738263e](https://github.com/oduist/oduflow/commit/738263e))
- **Post-clone DB fixup** — transfer object ownership and drop signaling sequences ([205e79f](https://github.com/oduist/oduflow/commit/205e79f))
- **Filter containers by prefix** to isolate test/production environments ([4a50e1c](https://github.com/oduist/oduflow/commit/4a50e1c))
- **Skip submodule recursion on git fetch** to tolerate inaccessible submodules ([5a1a476](https://github.com/oduist/oduflow/commit/5a1a476))
- **Raise `NotFoundError` when deleting a non-existent environment** ([0a1d875](https://github.com/oduist/oduflow/commit/0a1d875))
- **`update_service` URL KeyError; fresh pull on create** ([27ec4c9](https://github.com/oduist/oduflow/commit/27ec4c9))
- **Propagate `cap_add`/`privileged` in `restore_service`** and surface them in `list_service_presets` ([6bca16c](https://github.com/oduist/oduflow/commit/6bca16c))
- **Strip docker volume name prefix** in `resolve_volume_binds` ([2573b17](https://github.com/oduist/oduflow/commit/2573b17))
- **Exclude checkboxes from full-width input styling** in forms ([7f685c9](https://github.com/oduist/oduflow/commit/7f685c9))
- **Pass missing `team` arg** to `_get_used_ports` in `template_up` ([fbfc85e](https://github.com/oduist/oduflow/commit/fbfc85e))
- **Recreate live-mount environments as live-mounts** — record `local_path` in template metadata and restore it on create ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))

### Performance

- **`reload-template`** — pipe `gunzip` into `psql` to avoid a temp file ([0bd0838](https://github.com/oduist/oduflow/commit/0bd0838))

### Documentation

- **Use Odoo 19.0 across examples** and bump default image fallbacks to `odoo:19.0` ([a1fba72](https://github.com/oduist/oduflow/commit/a1fba72))
- **Docs Material redesign** ([7980fea](https://github.com/oduist/oduflow/commit/7980fea))
- **Document self-hosted OAuth** in the config reference, quick start, and llms docs ([73aa9b9](https://github.com/oduist/oduflow/commit/73aa9b9))
- **Consolidate agent guidance into `AGENTS.md`**, making `CLAUDE.md` a thin include ([7b60d8c](https://github.com/oduist/oduflow/commit/7b60d8c))
- **Document `refresh_template` MCP tool** in mcp-tools, llms, llms-full ([73a4768](https://github.com/oduist/oduflow/commit/73a4768))
- **Rework transport mode documentation** (stdio/http), add `uvx` examples ([f2c8eef](https://github.com/oduist/oduflow/commit/f2c8eef))
- **Fix `create_environment` CLI examples** — wrong positional arg order ([67dae80](https://github.com/oduist/oduflow/commit/67dae80))
- **Docs auto-deploy and `.oduflow/` dependency files** ([7f8d654](https://github.com/oduist/oduflow/commit/7f8d654))

## v1.20.1 (since v1.15.1)

### Breaking Changes

- **Stdio transport is now the default** — `oduflow` starts in stdio mode by default (previously HTTP). Use `oduflow --transport http` for HTTP mode. ([34e42fa](https://github.com/oduist/oduflow/commit/34e42fa), [f33dfe6](https://github.com/oduist/oduflow/commit/f33dfe6))

### Features

- **Move repo-level `odoo.conf` to `.oduflow/odoo.conf`** — per-repo Odoo config is now read from `<repo>/.oduflow/odoo.conf` instead of the repo root
- **Auto-initialize on startup** — `oduflow` automatically runs system setup and the Docker check on first start ([34e42fa](https://github.com/oduist/oduflow/commit/34e42fa), [f33dfe6](https://github.com/oduist/oduflow/commit/f33dfe6))
- **MCP tools refinement** — output cache for long-running tool results, 7 new MCP tools, 3 enhanced tools; renamed `exec_in_odoo` to `run_odoo_command` ([44810aa](https://github.com/oduist/oduflow/commit/44810aa))
- **Include odoo.conf in upgrade** — module upgrades now apply odoo.conf changes, skipping files that haven't changed ([f69bc2f](https://github.com/oduist/oduflow/commit/f69bc2f))
- **Show template database name** in `list-templates` output ([c816229](https://github.com/oduist/oduflow/commit/c816229))
- **Default odoo.conf values** — added `workers=0` and `db_maxconn=4` to the odoo.conf template for single-process development ([f16ab37](https://github.com/oduist/oduflow/commit/f16ab37))

### Dashboard

- **Rebuild button** — added rebuild button to web UI; pull Docker image before create/rebuild operations ([0004ac7](https://github.com/oduist/oduflow/commit/0004ac7))

### Bug Fixes

- **Sanitization: delete mail servers** — delete mail servers entirely instead of just disabling them during database sanitization ([4f729b1](https://github.com/oduist/oduflow/commit/4f729b1))
- **`search_in_odoo` filenames and limit** — always show filenames in search results and fix `max_results` limit not being applied correctly ([fc3c53c](https://github.com/oduist/oduflow/commit/fc3c53c))
- **Missing team parameter in `pull_environment`** — pass team parameter when running module operations during pull ([dc43104](https://github.com/oduist/oduflow/commit/dc43104))
- **`delete_service_preset` lock** — fix locking for delete_service_preset operation ([18b18b1](https://github.com/oduist/oduflow/commit/18b18b1))

### Documentation

- Add MCP tools refinement spec (`mcp-ref.md`) ([87edd27](https://github.com/oduist/oduflow/commit/87edd27), [417eba7](https://github.com/oduist/oduflow/commit/417eba7))
- Rename Mutex → Lock in documentation, add glightbox for image zoom ([18b18b1](https://github.com/oduist/oduflow/commit/18b18b1))
- Rename `exec_in_odoo` → `run_odoo_command`, add 7 missing tools to docs ([518e2c8](https://github.com/oduist/oduflow/commit/518e2c8))
- Advise agents to prefer local search over container search for Odoo sources ([961b9dc](https://github.com/oduist/oduflow/commit/961b9dc))

---

## v1.15.1 (since v1.10.1)

### Breaking Changes

- **Team-based multi-tenancy replaces instance-based isolation** — configuration migrated from `.env` / `ODUFLOW_INSTANCE_ID` to TOML-based `oduflow.toml` with per-team settings (workspaces, templates, credentials, port ranges, hostnames). The `.env.example` file has been removed. ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382), [14503a0](https://github.com/oduist/oduflow/commit/14503a0))

### Features

- **Team-based multi-tenancy** — per-team isolation with dedicated hostnames, git credentials, port ranges, and MCP token auth; auto-generated Traefik dynamic config for per-team routing; team resolution from Host header or bearer token ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382), [14503a0](https://github.com/oduist/oduflow/commit/14503a0))
- **CLI: `run-instance` command, `--version` and `--instance` flags** — run a named instance directly from CLI with version info support ([0094369](https://github.com/oduist/oduflow/commit/0094369))
- **CLI: `systemd-install` / `systemd-uninstall` commands** — install/remove Oduflow as a systemd service ([d266ca9](https://github.com/oduist/oduflow/commit/d266ca9))
- **Per-environment PostgreSQL credentials** — each environment gets its own isolated database role and password ([e4949b0](https://github.com/oduist/oduflow/commit/e4949b0))
- **Two-tier database sanitization** — system-wide sanitization scripts plus per-repo `.odoo_sanitize/` folder support ([cd0b9ec](https://github.com/oduist/oduflow/commit/cd0b9ec))
- **MCP tool: `read_file_in_odoo`** — read files and list directories inside Odoo containers without shell commands ([abcc525](https://github.com/oduist/oduflow/commit/abcc525))
- **MCP tool: `reset_admin_password`** — reset the admin user password in any environment's database ([608b476](https://github.com/oduist/oduflow/commit/608b476))
- **Extra repos: fetch summary and propagation** — `update_extra_repo` returns a summary of fetched branches and propagates updates to running environments ([a80ba53](https://github.com/oduist/oduflow/commit/a80ba53))
- **Per-branch locking module** — new `LockManager` replaces the single global mutex with per-branch and per-team locks ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Docker publishing support** — added `.dockerignore` and Docker publishing instructions ([24f653a](https://github.com/oduist/oduflow/commit/24f653a))

### Dashboard

- **Interactive SQL console (psql)** — run SQL queries directly from the web dashboard ([15d0e59](https://github.com/oduist/oduflow/commit/15d0e59))
- **Interactive Odoo shell console** — access Odoo shell via the web dashboard ([894f1ea](https://github.com/oduist/oduflow/commit/894f1ea))
- **Colored logs rendering** — dashboard shows ANSI-colored logs; MCP tools receive clean stripped output ([2e3989b](https://github.com/oduist/oduflow/commit/2e3989b))
- **Detailed sync results popup** — sync operations show detailed results in a popup instead of a plain toast ([ade3ee6](https://github.com/oduist/oduflow/commit/ade3ee6))
- **Editable restore service dialog** — service restore dialog shows editable preset values before confirming ([4eac120](https://github.com/oduist/oduflow/commit/4eac120))
- **Wider logs modal** — logs modal expanded to 80vw with horizontal scroll ([e9d0273](https://github.com/oduist/oduflow/commit/e9d0273))

### Bug Fixes

- **MCP concurrency** — pass `stateless_http=True` to unblock concurrent MCP requests; run sync MCP tools in thread pool to prevent event loop blocking during long operations ([81457b1](https://github.com/oduist/oduflow/commit/81457b1), [ed4a265](https://github.com/oduist/oduflow/commit/ed4a265))
- **TLS certresolver name** — match Docker label to Traefik's ACME provider name ("letsencrypt" not "le") ([727448b](https://github.com/oduist/oduflow/commit/727448b))
- **HOME fallback for systemd** — add `HOME=/root` to `GIT_ENV` so git operations work under systemd ([a982b9d](https://github.com/oduist/oduflow/commit/a982b9d))
- **Reject SSH repo URLs** — SSH URLs caused hangs due to interactive host-key prompts; now rejected early with a clear error ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Database ownership on template clone** — fixed ownership reassignment for tables, sequences, views, and materialized views; switched to `GRANT role` approach ([31df320](https://github.com/oduist/oduflow/commit/31df320), [afb66d0](https://github.com/oduist/oduflow/commit/afb66d0), [5c7b737](https://github.com/oduist/oduflow/commit/5c7b737))
- **Environment deletion** — drop database before role to avoid dependency errors ([4b28e9e](https://github.com/oduist/oduflow/commit/4b28e9e))
- **Strip `db_password` from odoo.conf** — ensures the Docker entrypoint uses environment variables instead ([f9f89d5](https://github.com/oduist/oduflow/commit/f9f89d5))
- **Template publish** — prevent self-copy in `reload_template`, scope publish to environments matching the template ([b92631a](https://github.com/oduist/oduflow/commit/b92631a))
- **Git sync** — replace `git pull --rebase` with `fetch + reset --hard` for reliable sync; use explicit refspec in `pull_repo` ([d57ee7e](https://github.com/oduist/oduflow/commit/d57ee7e), [74ef53d](https://github.com/oduist/oduflow/commit/74ef53d))
- **Load extra_addons from template metadata** in `create_environment` ([3d629e6](https://github.com/oduist/oduflow/commit/3d629e6))
- **Dump restoration** — handle gzip files and verify table count ([7ef7a02](https://github.com/oduist/oduflow/commit/7ef7a02))
- **Fallback data dir** — fallback `ODUFLOW_HOME` to `~/oduflow_data_{id}` when `/srv` is read-only ([d45307c](https://github.com/oduist/oduflow/commit/d45307c))

### Refactoring

- **TOML-based configuration** — replace `.env` with `oduflow.toml`; first launch auto-bootstraps the default config ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Merge `external_host` and `base_domain` into per-team `hostname`** ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Rename `ODUFLOW_HOME` → `ODUFLOW_DATA_DIR`** with instance subdirectories ([52224cb](https://github.com/oduist/oduflow/commit/52224cb))
- **Move odoo.conf and odoo_sanitize** from `etc/` to instance data directory ([dbfc969](https://github.com/oduist/oduflow/commit/dbfc969))
- **Require explicit branch for extra_addons** — format is now `name:branch` ([9254169](https://github.com/oduist/oduflow/commit/9254169))
- **`chown_recursive()` helper** — replaces manual `os.chown` loops with macOS fallback support ([872059d](https://github.com/oduist/oduflow/commit/872059d))
- **Rename `get_environment_status` → `get_environment_info`** with comprehensive details ([20e4abf](https://github.com/oduist/oduflow/commit/20e4abf))
- **Rename `get_agent_guide` → `get_agent_skill`** ([8625790](https://github.com/oduist/oduflow/commit/8625790))
- **Rename MCP tools** for clarity ([fbbcf54](https://github.com/oduist/oduflow/commit/fbbcf54))
- **Async MCP tool wrapper** — `handle_errors` decorator converted to async, offloading all tools to thread pool ([ed4a265](https://github.com/oduist/oduflow/commit/ed4a265))

### Documentation

- Comprehensive docs rewrite for TOML-based multi-team architecture ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- Add MCP tools refinement spec (`mcp-ref.md`) ([87edd27](https://github.com/oduist/oduflow/commit/87edd27))
- Add missing features to documentation, fix inaccuracies ([e0c3bc3](https://github.com/oduist/oduflow/commit/e0c3bc3), [1aaa6a5](https://github.com/oduist/oduflow/commit/1aaa6a5))
- Add `llms-full.txt` for complete LLM-consumable documentation ([e330ad4](https://github.com/oduist/oduflow/commit/e330ad4))
- Add screenshots to documentation pages ([8726d01](https://github.com/oduist/oduflow/commit/8726d01))
- Add macOS vs Linux file ownership section ([ac4a7a8](https://github.com/oduist/oduflow/commit/ac4a7a8))
- Add `CLAUDE.md` with project architecture and dev commands ([008d43f](https://github.com/oduist/oduflow/commit/008d43f))

---

## v1.10.1 and earlier

## Features

- Add `reset_admin_password` MCP tool to reset the admin password in Odoo environments ([608b476](https://github.com/oduist/oduflow/commit/608b476))
- Refactor Agent Guides into multi-guide system with Odoo version-specific development guides ([8680a18](https://github.com/oduist/oduflow/commit/8680a18b284ba1a7e0e2de9a2d0c5b9af489af69))
- Add `import_template_from_odoo` — import templates from running Odoo instances ([a6d53fc](https://github.com/oduist/oduflow/commit/a6d53fc988e5c387c2d49d98436a530fee3bab19))
- Store `use_overlay` flag in template metadata instead of computing filestore size on every env creation ([e393c44](https://github.com/oduist/oduflow/commit/e393c445fbc040ea451c6876aa82a35b1d5ff392))
- Show `use_overlay` mode in CLI `list-templates` and dashboard UI ([cf85489](https://github.com/oduist/oduflow/commit/cf854894eeb8c33e13b05599652a7e6894c8b4f0))
- Show template name in `get_environment_info` and dashboard UI ([e79a6ae](https://github.com/oduist/oduflow/commit/e79a6ae80917d9d64e5b22d5ca0c61e78ed0019e))
- Add Sync button to environment UI ([66ceed8](https://github.com/oduist/oduflow/commit/66ceed8ef8747b3f679ad1ed7ed62aeab8e779b6))
- Add `ODUFLOW_TRACE=1` trace logging for sync/classify pipeline ([af7b9bc](https://github.com/oduist/oduflow/commit/af7b9bcb55ba43cb4daaaa884cfc9edcfd59a9cb))
- Docker deploy added ([e934bea](https://github.com/oduist/oduflow/commit/e934bea4e9e72e5259c94b6bd54c558e4a0e4daf))
- Append `/web?debug=1` to environment URLs in dashboard ([ff913ad](https://github.com/oduist/oduflow/commit/ff913ada0ff1406b505c5fa103e85977b57d0428))
- Add logo to Web UI header ([6fd52ce](https://github.com/oduist/oduflow/commit/6fd52ce17369f30d889aa09fb847bfb136dfeae9))
- Add `run_db_query` MCP tool for executing SQL queries against environment databases ([d7b1b44](https://github.com/oduist/oduflow/commit/d7b1b44d3a378d1357125526860b6a459fc8bbf1))
- Credentials management UI + git auth improvements ([259164e](https://github.com/oduist/oduflow/commit/259164ebcbd74a1216e5585e2d93c1526a6d69ae))
- Per-user git credentials for repo operations ([5c28100](https://github.com/oduist/oduflow/commit/5c28100df0b9072c0b1da85a230ba06e6fd45bf8))
- Add `oduflow cleanup` CLI command to remove orphaned resources ([d1fa09c](https://github.com/oduist/oduflow/commit/d1fa09cf346726892653f724f0ce4a47de19a8b5))
- Add protect/unprotect for extra repos ([1245d6c](https://github.com/oduist/oduflow/commit/1245d6c5f953ac876c52d113ba5b43d82c94a0af))
- Add `update_extra_repo` MCP tool; remove one-off migration SQL ([3c678cc](https://github.com/oduist/oduflow/commit/3c678ccffe26060043a3deb28095ed66150c1108))
- Report elapsed time in `create_environment` response ([90ca9e1](https://github.com/oduist/oduflow/commit/90ca9e19ea1beb6957c30066cbec1a4f78f14927))
- Show filestore and dump sizes in template listing ([a1403a7](https://github.com/oduist/oduflow/commit/a1403a7737d071b42f3052214ae3eef7684c4f65))
- Add Recreate button for environments in web UI ([31afb9e](https://github.com/oduist/oduflow/commit/31afb9e7a28d7e785d05849783db36927f851137))

## Fixes

- Show friendly error when Docker is not available ([5df6df7](https://github.com/oduist/oduflow/commit/5df6df71f6f4886c815f59b1f7469884beb30045))
- Service logs filtering and state isolation from env logs ([9926c2a](https://github.com/oduist/oduflow/commit/9926c2aa6946f80c7e0ef6ac0c51cddaac7a9612))
- Update `test_settings` to match current Settings API ([b92fe80](https://github.com/oduist/oduflow/commit/b92fe800e75c466384cb3f8290543c3b56183f3c))
- Restart container after successful module installation ([b819c65](https://github.com/oduist/oduflow/commit/b819c65c4eb526ba4a51f97682c239b377bc1319))
- Fix tests: expect `ToolError` instead of `ValueError` ([dcf7f23](https://github.com/oduist/oduflow/commit/dcf7f23c7d608603e2fdd589a0471ea594026432))
- License load fix ([8e66e8f](https://github.com/oduist/oduflow/commit/8e66e8f005ac4aa1f8fdd64a50bd84e639459319))
- Stream dump file to container to avoid OOM; save dump to workspace on `reload-template --dump-path` ([8615fe2](https://github.com/oduist/oduflow/commit/8615fe29354943110b3c75def142ad2a9ab3d292))
- Eliminate OOM in all container file extraction (`init_template`, `template_down`, `publish_env_as_template`) ([b40395b](https://github.com/oduist/oduflow/commit/b40395b769480e09cc340c3c68f8dd31217f1cab))
- Surface database drop failures as warnings in `delete_environment` ([02a628b](https://github.com/oduist/oduflow/commit/02a628b4ebc59e142791f8d88539d0a4a6d865c7))
- Use `odoo_image` version as fallback branch for extra addons worktree ([ea297fd](https://github.com/oduist/oduflow/commit/ea297fdf4d81bc06a7bd0c22f89cb1b8427582c0))
- Configure fetch refspec for bare extra repos so `git fetch` updates branches ([54bc937](https://github.com/oduist/oduflow/commit/54bc9373e55569f85db0fe2737691d1a9cb10be3))

## Documentation

- Rework README — add missing config vars, licensing, template metadata, CLI flags ([0b8de7e](https://github.com/oduist/oduflow/commit/0b8de7eea6cb92cffc7678fc9e7c6101f7463df4))
- Restructure README — reorder sections, remove odoo.sh comparison, merge internals ([70af4df](https://github.com/oduist/oduflow/commit/70af4df31e3295db864a034d6afbc43e21218ad2))
- Update README.md ([fe6674f](https://github.com/oduist/oduflow/commit/fe6674ff41ccdcf0a0e4e2f9cab0400f287b2af6))
- Add MkDocs with Material theme, build site to docs/ for GitHub Pages ([bbcf1e9](https://github.com/oduist/oduflow/commit/bbcf1e90691ad96368fd7b6c9270870f64a39311))
- Switch to GitBook theme, use gh-pages branch for deployment ([3cfbbb2](https://github.com/oduist/oduflow/commit/3cfbbb261fabba064e0877430ae5dceeac91d88b))
- Add `requirements-docs.txt` for mkdocs setup ([b620ff5](https://github.com/oduist/oduflow/commit/b620ff57153623f5b47cfcec89b261389ea81936))
- Split README into structured mkdocs documentation ([c52084f](https://github.com/oduist/oduflow/commit/c52084f0772fc0abe1564a384e0341b0907d35ae))
- Add database migrations workflow guide for agents ([63629fd](https://github.com/oduist/oduflow/commit/63629fd267170e1af7a3e5d1d162653da61f99bc))
- Document extra addons pinning behavior in environments ([651f877](https://github.com/oduist/oduflow/commit/651f8778266fd350399bd1605c32ad4ea96e9e77))

## Refactor

- Rename CLI command `promote` to `template-from-env` ([ecaaf84](https://github.com/oduist/oduflow/commit/ecaaf84c1a8bf205ed6b3451ec0d58f0842dce58))
- Rename template DB prefix to `oduflow_template_{id}_{name}`, make `template_name` required ([c3bee0f](https://github.com/oduist/oduflow/commit/c3bee0fbeece89253860389ad144461d4d291861))

## Chore

- Make dev guide hint more assertive in `create_environment` output ([b9933ce](https://github.com/oduist/oduflow/commit/b9933cec0b72aa4832c711a9fbea8ffae9d10b7e))
- Clean up startup logs: use websockets-sansio, suppress docker noise ([a486d87](https://github.com/oduist/oduflow/commit/a486d87da6c7fdf94ae4711d13fe2b12553ad225))
- Add `site/` to `.gitignore`, remove tracked build output ([b36e832](https://github.com/oduist/oduflow/commit/b36e8327eacc900a44f0ea65a0e585b9005a3dc6))
- Add `logo.png` to tracked files for package distribution ([935f5f7](https://github.com/oduist/oduflow/commit/935f5f764d0544d32627c48e046596c0fa441fe8))
