# 0058 — Team secrets for environment variables (`secret:<name>` references)

**Status:** Adopted (still in force)
**Type:** Architecture / Security
**First introduced:** `litnimax/agitated-wolverine` branch (2026-09-12)
**Key code today:** `secret_store.py` (the store + reference resolution), `docker_ops/service_ops.py` (resolution at create, `oduflow.secret_env` label, reference overlay in `_container_env_vars`), `docker_ops/env_ops.py` (resolution in create/update), `server.py` (`list_secrets`), `web_ui.py` (`/api/secrets*`), dashboard Credentials tab

## Context

Env vars on services and environments routinely carry passwords and API keys,
and every read surface an agent uses — `get_service_info`, `list_services`,
`get_environment_info`, the REST API — returned them in plaintext, so any
casual inspection dumped the secrets into the agent conversation. The only
protections in the codebase were point solutions: names-only template listing
in `list_templates`, inline git credentials moved out of Docker labels
([[0002]]-era `.git-credentials` store), and a cosmetic client-side mask in
one dashboard list. At the same time, secrets had to keep *migrating* with
their configuration: restore-from-preset, environment rename, save-as-template
and create-from-template must not require re-entering values by hand.

Two models were considered: per-object secret copies (each service/environment
carries its own encrypted-ish value blob, copied on every migration path) and
a team-scoped vault referenced by name. Copies multiply the at-rest exposure
and demand explicit copy logic in every present and future migration path;
references make migration free, because the reference is just an ordinary
env-var value that already travels everywhere.

## Decision

One write-only vault per team (`{team.data_dir}/secrets.json`, 0600), with
values set exclusively by a human in the dashboard. Env vars reference a
secret as `secret:<name>`; the reference is the persisted and displayed form
everywhere (service presets, the `oduflow.env_vars` label, template metadata,
all MCP/REST read surfaces), and the real value is substituted only at the
`docker_ops` chokepoints that hand the environment to `containers.run`. No
MCP tool or REST endpoint ever returns a stored value — agents get
`list_secrets` (names + timestamps) and can freely *reference* secrets they
cannot read.

## How it works (macro)

- **Resolution** happens in `service_ops.create_service`,
  `env_ops._create_environment_impl` and `env_ops.update_environment` — one
  hook per chokepoint covers MCP, REST, CLI and stacks. Resolution runs
  *before* any destructive or expensive step, so a dangling reference fails
  fast with a "have an operator set it in the dashboard" error instead of
  costing a container.
- **Read-surface fidelity**: services record their reference map in an
  `oduflow.secret_env` container label at creation; `_container_env_vars`
  overlays it, so even container-inspect-based reads (and the legacy
  no-preset update path) show references, independent of the preset file's
  existence. Environments already read env vars from their label, which holds
  references by construction.
- **Migration for free**: presets, labels and template metadata store
  references, so restore_service, rename, save_as_template →
  create_environment all carry secrets without touching values. (The same
  change fixed save_as_template's stale pre-[[0025]]-migration container name,
  which had been silently dropping *all* template metadata.)
- **Rotation**: replace the value in the dashboard, then recreate the
  consumers (`update_service`/`update_environment`).

## Consequences

- Agents can wire secrets into any service/environment without ever seeing
  them; a "read the config, paste it in chat" leak is structurally closed.
- The boundary is explicit: resolved values still live in the container's
  `Config.Env`, so code executed *inside* a container can read them. This is
  accepted — the goal is protecting the MCP/REST/dashboard read surfaces.
- Values are plaintext-at-rest under 0600, consistent with every other
  credential store ([[0052]] service-database credentials set the pattern);
  `service_presets.json` was brought to 0600 too (migration
  `0006-service-presets-0600`).
- Stack manifests need no new syntax: a literal `secret:<name>` string value
  resolves at the same chokepoint, and drift comparison sees reference vs
  reference.

## History

- Introduced together with the save_as_template metadata fix and the presets
  permission migration (branch `litnimax/agitated-wolverine`, 2026-09-12).
