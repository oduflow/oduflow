# MCP Tools Reference

![Agent Instructions](img/agent_instructions.png)

Oduflow exposes **108 tools**. They are reachable from any MCP client (Cursor,
Cline, Amp, Claude Code, …), locally with `oduflow call`, against a remote
Oduflow server with `oduflow client`, and — for a subset — over the
[REST API](web-api.md).

Every tool below is documented with its parameters, defaults and the situations
it is meant for. Jump straight to one from the index, or read a category
end to end.

!!! info "Locking"
    Many tools acquire a lock on exactly what they touch: **one environment**,
    **one production**, **one service / volume / database**, the **team's
    credential store** — or the **whole team**, for the few operations that
    really are team-wide (template publishing). Each tool's section states its
    lock. Operations on *different* resources run in parallel. If another
    operation on the **same** resource is already in progress, the call is
    rejected with `BusyError`, naming the operation holding the lock and how
    long it has held it (e.g. *"Another operation on environment 'main'
    (pull_and_apply, running for 4m12s) is in progress"*), so a long install is
    distinguishable from a hung one. A lock is released when its operation
    finishes — including when the client that started it timed out and stopped
    waiting, which is why restarting the environment is the wrong response.

!!! tip "Signatures straight from the server"
    `oduflow list` prints the current tool list, and `oduflow list --verbose`
    adds descriptions — always in sync with the running version.

## Tool index

<div class="grid cards odu-tool-index" markdown>

-   __[Environment Management](#environment-management)__

    ---

    [`create_environment`](#create_environment)&nbsp;·
    [`delete_environment`](#delete_environment)&nbsp;·
    [`list_environments`](#list_environments)&nbsp;·
    [`get_environment_info`](#get_environment_info)&nbsp;·
    [`start_environment`](#start_environment)&nbsp;·
    [`stop_environment`](#stop_environment)&nbsp;·
    [`restart_environment`](#restart_environment)&nbsp;·
    [`update_environment`](#update_environment)&nbsp;·
    [`switch_branch`](#switch_branch)

-   __[Code Sync, Modules & Tests](#code-sync-modules-tests)__

    ---

    [`pull_and_apply`](#pull_and_apply)&nbsp;·
    [`install_odoo_modules`](#install_odoo_modules)&nbsp;·
    [`upgrade_odoo_modules`](#upgrade_odoo_modules)&nbsp;·
    [`run_odoo_tests`](#run_odoo_tests)&nbsp;·
    [`list_installed_modules`](#list_installed_modules)&nbsp;·
    [`get_environment_logs`](#get_environment_logs)&nbsp;·
    [`read_output`](#read_output)

-   __[Odoo Data Access](#odoo-data-access)__

    ---

    [`odoo_schema`](#odoo_schema)&nbsp;·
    [`odoo_search_read`](#odoo_search_read)&nbsp;·
    [`odoo_create`](#odoo_create)&nbsp;·
    [`odoo_write`](#odoo_write)&nbsp;·
    [`odoo_unlink`](#odoo_unlink)&nbsp;·
    [`odoo_call`](#odoo_call)&nbsp;·
    [`run_db_query`](#run_db_query)

-   __[Inside the Odoo Container](#inside-the-odoo-container)__

    ---

    [`read_file_in_odoo`](#read_file_in_odoo)&nbsp;·
    [`write_file_in_odoo`](#write_file_in_odoo)&nbsp;·
    [`search_in_odoo`](#search_in_odoo)&nbsp;·
    [`run_odoo_command`](#run_odoo_command)&nbsp;·
    [`run_odoo_shell`](#run_odoo_shell)&nbsp;·
    [`http_request_to_odoo`](#http_request_to_odoo)&nbsp;·
    [`reset_admin_password`](#reset_admin_password)&nbsp;·
    [`connect_as_user`](#connect_as_user)

-   __[Translations](#translations)__

    ---

    [`export_module_translations`](#export_module_translations)&nbsp;·
    [`translation_status`](#translation_status)

-   __[Template Management](#template-management)__

    ---

    [`save_as_template`](#save_as_template)&nbsp;·
    [`save_production_as_template`](#save_production_as_template)&nbsp;·
    [`list_templates`](#list_templates)&nbsp;·
    [`rename_template`](#rename_template)&nbsp;·
    [`delete_template`](#delete_template)&nbsp;·
    [`import_template_from_odoo`](#import_template_from_odoo)&nbsp;·
    [`refresh_template`](#refresh_template)&nbsp;·
    [`attach_filestore`](#attach_filestore)

-   __[Auxiliary Services](#auxiliary-services)__

    ---

    [`create_service`](#create_service)&nbsp;·
    [`update_service`](#update_service)&nbsp;·
    [`delete_service`](#delete_service)&nbsp;·
    [`restart_service`](#restart_service)&nbsp;·
    [`list_services`](#list_services)&nbsp;·
    [`get_service_info`](#get_service_info)&nbsp;·
    [`get_service_logs`](#get_service_logs)&nbsp;·
    [`run_service_command`](#run_service_command)

-   __[Service PostgreSQL Databases](#service-postgresql-databases)__

    ---

    [`create_service_database`](#create_service_database)&nbsp;·
    [`list_service_databases`](#list_service_databases)&nbsp;·
    [`get_service_database`](#get_service_database)&nbsp;·
    [`rotate_service_database_password`](#rotate_service_database_password)&nbsp;·
    [`delete_service_database`](#delete_service_database)

-   __[Volumes](#volumes)__

    ---

    [`create_volume`](#create_volume)&nbsp;·
    [`list_volumes`](#list_volumes)&nbsp;·
    [`inspect_volume`](#inspect_volume)&nbsp;·
    [`delete_volume`](#delete_volume)&nbsp;·
    [`read_file_in_volume`](#read_file_in_volume)&nbsp;·
    [`write_file_in_volume`](#write_file_in_volume)&nbsp;·
    [`search_in_volume`](#search_in_volume)&nbsp;·
    [`delete_file_in_volume`](#delete_file_in_volume)

-   __[Service Presets](#service-presets)__

    ---

    [`list_service_presets`](#list_service_presets)&nbsp;·
    [`restore_service`](#restore_service)&nbsp;·
    [`delete_service_preset`](#delete_service_preset)

-   __[Secrets](#secrets)__

    ---

    [`list_secrets`](#list_secrets)

-   __[Container Image Builds](#container-image-builds)__

    ---

    [`start_image_build`](#start_image_build)&nbsp;·
    [`get_image_build`](#get_image_build)&nbsp;·
    [`publish_image_build`](#publish_image_build)&nbsp;·
    [`cancel_image_build`](#cancel_image_build)

-   __[Repository Auth](#repository-auth)__

    ---

    [`setup_repo_auth`](#setup_repo_auth)&nbsp;·
    [`get_ssh_public_key`](#get_ssh_public_key)

-   __[Extra Addons](#extra-addons)__

    ---

    [`add_extra_repo`](#add_extra_repo)&nbsp;·
    [`list_extra_repos`](#list_extra_repos)&nbsp;·
    [`update_extra_repo`](#update_extra_repo)&nbsp;·
    [`delete_extra_repo`](#delete_extra_repo)

-   __[Production Hosting](#production-hosting)__

    ---

    [`create_production`](#create_production)&nbsp;·
    [`list_productions`](#list_productions)&nbsp;·
    [`get_production_info`](#get_production_info)&nbsp;·
    [`production_logs`](#production_logs)&nbsp;·
    [`start_production`](#start_production)&nbsp;·
    [`stop_production`](#stop_production)&nbsp;·
    [`restart_production`](#restart_production)&nbsp;·
    [`reconfigure_production`](#reconfigure_production)&nbsp;·
    [`set_production_odoo_conf`](#set_production_odoo_conf)&nbsp;·
    [`delete_production`](#delete_production)

-   __[Production Deployment](#production-deployment)__

    ---

    [`update_production`](#update_production)&nbsp;·
    [`rollback_production`](#rollback_production)&nbsp;·
    [`set_production_auto_update`](#set_production_auto_update)&nbsp;·
    [`production_deploys`](#production_deploys)

-   __[Production Backup & Recovery](#production-backup-recovery)__

    ---

    [`snapshot_production`](#snapshot_production)&nbsp;·
    [`list_production_snapshots`](#list_production_snapshots)&nbsp;·
    [`restore_production`](#restore_production)&nbsp;·
    [`set_production_backup_schedule`](#set_production_backup_schedule)&nbsp;·
    [`prune_production_backups`](#prune_production_backups)&nbsp;·
    [`production_backup_status`](#production_backup_status)&nbsp;·
    [`production_wal_status`](#production_wal_status)&nbsp;·
    [`control_production_wal`](#control_production_wal)&nbsp;·
    [`restore_cluster_pitr`](#restore_cluster_pitr)

-   __[Production Odoo API](#production-odoo-api)__

    ---

    [`sync_production_mcp`](#sync_production_mcp)&nbsp;·
    [`production_odoo_info`](#production_odoo_info)&nbsp;·
    [`production_odoo_read`](#production_odoo_read)&nbsp;·
    [`production_odoo_preview_change`](#production_odoo_preview_change)&nbsp;·
    [`production_odoo_change_status`](#production_odoo_change_status)&nbsp;·
    [`production_odoo_execute_change`](#production_odoo_execute_change)

-   __[Agent Guidance & Feedback](#agent-guidance-feedback)__

    ---

    [`get_agent_instructions`](#get_agent_instructions)&nbsp;·
    [`get_odoo_development_guide`](#get_odoo_development_guide)&nbsp;·
    [`report_issue`](#report_issue)

</div>

## Environment Management

Ephemeral, isolated Odoo environments — one per git branch. See
[Environment Management](environments.md) for the concepts behind them.

### `create_environment`

Provision a new ephemeral Odoo environment: clone the repository, copy the
template database, mount the filestore, start the container and route it.

Safe to call first, without listing environments: if one already exists under
this name it is **returned as is** — with its URL, and started when it was
stopped — and nothing is recreated. The single refusal is a branch mismatch: an
environment tracking another branch is left alone, since its database and URL
are in use. Move it with [`switch_branch`](#switch_branch), pass a different
`env_name`, or delete it first.

**Parameters**

`branch`
:   *str · required* — The git branch to clone (e.g. `19.0`, `feature/my-feature`).

`env_name`
:   *str · default empty* — Environment name. Empty defaults to the branch name. Use it to create several environments from the same branch (e.g. `env_name="client-a"` with `branch="19.0"`).

`template_name`
:   *str · default empty* — Template profile to use as the database template. Pass `"none"` to skip the template and initialise Odoo from scratch with `-i base`. When a template is given, `repo_url` and `odoo_image` are loaded from its metadata (and can still be overridden).

`repo_url`
:   *str · default empty* — Git repository URL. Optional when `template_name` supplies it.

`odoo_image`
:   *str · default empty* — Full Docker image with tag (e.g. `odoo:19.0`). Optional when `template_name` supplies it.

`extra_addons`
:   *str · default empty* — Comma-separated extra addon repos **with branches**, e.g. `"enterprise:19.0,custom-themes:main"`. The branch after the colon is mandatory.

`sanitize`
:   *bool · default `True`* — Run Odoo's native neutralization (deactivates outgoing mail servers and crons, disables payment providers, scrubs third-party API credentials, sets `database.is_neutralized`) plus any custom scripts in the repository's `.oduflow/odoo_sanitize/`. Only applies to environments created from a template.

`auto_install_modules`
:   *str · default empty* — Comma-separated modules to install right after provisioning (e.g. `"sale,purchase,stock"`). Loaded from template metadata when a template is used and this is empty.

`env_vars`
:   *str · default empty* — Comma- or newline-separated `KEY=VALUE` pairs injected into the container. Commas inside values are preserved unless what follows looks like another `KEY=`; put one pair per line when in doubt. Merged per key over the template's own values, with these winning. A value `secret:<name>` references a [team secret](#list_secrets).

`hostname`
:   *str · default empty* — Short Traefik hostname (e.g. `"qa"` → `qa.example.com`). Replaces the team prefix in either hostname mode.

`from_production`
:   *str · default empty* — Build this environment from a production's real data (database + filestore + repo/image/extra addons). Mutually exclusive with `template_name` and `local_path`. The copy goes through one managed `prod-<name>` template, published on first use and reused afterwards.

`local_path`
:   *str · default empty* — **Local fast path.** Absolute path to a checkout on this host: Oduflow skips the clone and bind-mounts the directory live, so file edits are visible instantly with no git round-trip. `repo_url` is not required. Gated by `allow_local_path`.

**Use it when**

- A new feature branch needs its own running Odoo with realistic data.
- You want a throwaway copy of production data to reproduce a customer bug (`from_production`).
- You are iterating on code on this machine and want edits live in the container (`local_path`).
- An agent needs a safe target: calling it repeatedly is idempotent, not destructive.

```bash
oduflow call create_environment '{
  "branch": "feature/invoice-report",
  "template_name": "acme-19",
  "extra_addons": "enterprise:19.0",
  "auto_install_modules": "sale,account"
}'
```

### `delete_environment`

Stop and remove every resource associated with an environment — container,
database, filestore, ports and routing.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to tear down.

**Use it when**

- A branch is merged and its environment is no longer needed.
- You need to free an environment slot and no existing environment is worth keeping (otherwise prefer [`switch_branch`](#switch_branch)).

### `list_environments`

List all managed environments with status, URL, current git branch,
creation / last-activity / stopped timestamps, stop source, protection, Stack
ownership and operator note.

**Parameters**

*None.*

**Use it when**

- Taking stock before creating another environment (slot pressure).
- Finding which environments are idle and can be reused or reclaimed.

### `get_environment_info`

Full details for one environment: lifecycle and reuse metadata, database name,
URL, repository, image, template, extra addons, workspace path, container
status, and CPU/RAM stats.

**Parameters**

`env_name`
:   *str · required* — The environment to inspect.

**Use it when**

- You need the URL or database name to hand to someone or to another tool.
- Diagnosing "is it actually running, and what is it running?" before digging into logs.
- Checking which template and extra addons an environment was built from.

### `start_environment`

Start all containers for a stopped environment.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to start.

`wait`
:   *bool · default `True`* — Wait for Odoo to become ready, polling `/web/health` every 2 seconds for up to 120 seconds.

**Use it when**

- Resuming work on an environment the idle reaper stopped.
- A scripted flow must not continue until Odoo answers (`wait=True`).

### `stop_environment`

Stop the Odoo container, keeping the database, filestore and configuration.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to stop.

**Use it when**

- Freeing RAM and CPU on a busy host without losing the environment.
- Parking an environment you will come back to.

### `restart_environment`

Restart the Odoo container. Python code is reloaded; the database is untouched.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to restart.

`wait`
:   *bool · default `True`* — Wait for Odoo to become ready, polling `/web/health` every 2 seconds for up to 120 seconds.

**Use it when**

- Only Python logic changed and no database-backed definition moved (otherwise upgrade the module — see [`pull_and_apply`](#pull_and_apply)).
- Odoo is wedged and you want a clean process before investigating further.

### `update_environment`

Re-create the Odoo container **without losing the database or filestore**.
Pulls the target image and rebuilds the container.

With no arguments it simply rebuilds from the current image and configuration —
the fix for a broken container (packages accidentally removed, system files
corrupted) reconnected to the existing data.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to update.

`env_vars`
:   *str · default empty* — Comma- or newline-separated `KEY=VALUE` pairs that **fully replace** the current user-supplied variables. Empty keeps them. Database connection variables (`HOST`/`USER`/`PASSWORD`) are always preserved. `secret:<name>` references are supported.

`odoo_image`
:   *str · default empty* — New Docker image with tag to pull and run (e.g. `odoo:19.0`). Empty keeps the current image.

`hostname`
:   *str · default empty* — New short Traefik hostname (e.g. `"qa"` → `qa.example.com`). Changes the public URL.

`new_name`
:   *str · default empty* — Rename the environment. The database, filestore, ports and credentials move with it. A pooled or explicit hostname is kept, so the URL stays; a name-derived hostname follows the new name. **The scoped MCP endpoint moves** from `/mcp/<old>` to `/mcp/<new>`, so MCP clients must be re-pointed. Productions and stack members cannot be renamed here.

**Use it when**

- Bumping the Odoo image (e.g. `odoo:18.0` → `odoo:19.0`) while keeping the data.
- Changing container environment variables or wiring in a new secret.
- The container is damaged and you want a fresh one on the same data.
- The environment's name no longer describes what it holds. (If it should also move onto another branch, use [`switch_branch`](#switch_branch) — it renames along the way.)

```bash
oduflow call update_environment '{"env_name": "main", "odoo_image": "odoo:19.0"}'
```

### `switch_branch`

Move an existing environment onto another git branch. Everything except the
code stays: the database, filestore, URL / hostname, ports, database
credentials and the scoped MCP token.

Reach for this when the team has **no free environment slots** and a previous
branch is finished (merged), or when that database and URL are worth keeping —
it replaces "delete the old environment, create a fresh one", which re-clones
the repository and re-copies the template database. With slots still free,
[`create_environment`](#create_environment) is simpler.

The branch must already exist on origin, so push it first. Oduflow diffs the old
and new tips and applies exactly the logic of [`pull_and_apply`](#pull_and_apply).
Errors and tracebacks come back in the response — do not chase them in the logs.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to move.

`branch`
:   *str · required* — Target git branch; it must already exist on origin.

`install`
:   *str · default empty* — Comma-separated modules to install (`-i`). Empty leaves classification to Oduflow.

`upgrade`
:   *str · default empty* — Comma-separated modules to upgrade (`-u`). Empty leaves classification to Oduflow.

`restart`
:   *bool · default `False`* — Restart the container (for Python-only differences).

`strict`
:   *bool · default `False`* — Refuse instead of warning when the requested action looks incomplete for the diff.

`extra_addons`
:   *str · default empty* — Extra addon repos with branches (e.g. `"enterprise:19.0"`) to switch along with the main repo. Empty keeps the current ones.

`new_name`
:   *str · default empty* — Rename the environment in the same operation. The URL is kept, but the scoped MCP endpoint moves to `/mcp/<new name>`.

**Use it when**

- Out of environment slots and an old branch is merged.
- A long-lived QA database and URL should follow a new branch.
- Keeping stakeholders on a stable link while the code underneath changes.

!!! warning "Database compatibility is not checked"
    Switching does not inspect which modules are installed in the retained
    database. If the target code is incompatible, the apply command returns the
    real failure or Odoo reports it at runtime. Live-mounted environments are
    rejected — there the checkout is yours, so switch the branch in it and call
    [`pull_and_apply`](#pull_and_apply).

## Code Sync, Modules & Tests

### `pull_and_apply`

Sync the latest code into an environment and apply the right Odoo action. This
is the main development loop tool.

It works for both code-delivery modes, chosen automatically per environment:

- **git** — pulls the branch and resolves extra addons to shared immutable SHA checkouts before applying changes.
- **live-mount** (from `create_environment(local_path=...)`) — your edits are already on disk; this just applies them, no git needed.

There are two ways to drive it:

- **Explicit** *(recommended — you know what you changed)*: pass `install` / `upgrade` and/or `restart=True`. A guardrail compares your request against the detected changes and appends non-blocking warnings if something looks missing. `strict=True` refuses instead of warning.
- **Auto** *(leave everything empty)*: Oduflow classifies the changed files and decides install / upgrade / restart / refresh itself. Best when pulling commits you did not author.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment to apply changes to.

`install`
:   *str · default empty* — Comma-separated modules to install (`-i`).

`upgrade`
:   *str · default empty* — Comma-separated modules to upgrade (`-u`).

`restart`
:   *bool · default `False`* — Restart the Odoo container (for Python-only changes).

`strict`
:   *bool · default `False`* — Refuse to apply when the guardrail finds a likely missing action, instead of warning and applying anyway.

`summary_only`
:   *bool · default `False`* — Return a compact one-line action/status summary plus an `output_id` instead of command logs and changed-file names. The raw output stays available through [`read_output`](#read_output).

**What to pass for which change**

| You changed | Pass |
|---|---|
| View/QWeb XML, JS, CSS only | nothing — refresh the browser |
| Python logic / methods (no new fields or models) | `restart=True` |
| A field, model, security rule, data record, `ir.cron`, mail template, or manifest `data`/`depends` | `upgrade="module"` |
| A brand-new module | `install="module"` |
| `requirements.txt`, `.oduflow/requirements.txt`, `.oduflow/apt_packages.txt` | nothing — dependencies are reinstalled and the container restarted automatically |

!!! note "Removed dependencies"
    Packages deleted from a requirements file are not uninstalled until the
    container is rebuilt with [`update_environment`](#update_environment).

**Use it when**

- Every time you push code and want it live in the environment.
- Pulling someone else's commits and you are not sure what they touched (auto mode).
- A CI-style flow needs a single call that both syncs and applies.

!!! tip "Errors come back inline"
    Errors and tracebacks are returned directly in the response — do **not**
    call [`get_environment_logs`](#get_environment_logs) to look for them. With
    `summary_only=True` they are not inlined: read the full log with
    [`read_output`](#read_output).

```bash
oduflow call pull_and_apply '{"env_name": "main", "upgrade": "sale_custom"}'
```

### `install_odoo_modules`

Install Odoo modules (`odoo -i`) in an environment.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`modules`
:   *str · required* — Comma-separated modules to install (e.g. `"sale,crm,web"`).

**Use it when**

- Adding a module to an existing environment without pulling new code.
- Setting up prerequisites before [`run_odoo_tests`](#run_odoo_tests) — testing an uninstalled module yields "0 of 0 tests".

### `upgrade_odoo_modules`

Upgrade already-installed Odoo modules (`odoo -u`).

Unknown or uninstalled modules are rejected with a suggestion to use
[`install_odoo_modules`](#install_odoo_modules) or
`pull_and_apply(install=...)` instead.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`modules`
:   *str · required* — Comma-separated modules to upgrade (e.g. `"sale,crm,web"`).

**Use it when**

- A model, field, view, security rule or data record changed and must be reloaded into the database.
- Re-applying a module's data files after editing them by hand.

### `run_odoo_tests`

Run Odoo tests for specific modules. The modules must already be installed —
by default the run happens through an upgrade (`-u`).

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`modules`
:   *str · required* — Comma-separated already-installed modules to test.

`test_tags`
:   *str · default empty* — Odoo `--test-tags` expression narrowing the run: `"/my_module:TestInvoice"` (one class), `"/my_module:TestInvoice.test_total"` (one method), `"-slow"` (exclude a tag). Comma-separated, no spaces. Empty runs every test of the listed modules. With `upgrade=False`, positive selectors must include one of the requested modules (e.g. `"slow/my_module"`); exclusion-only selectors are scoped automatically.

`upgrade`
:   *bool · default `True`* — Upgrade the modules before testing. Set `False` for a much faster re-run when the code is already loaded — but note Odoo then collects only **`post_install`** tests, so plain `TransactionCase` classes at the default `at_install` position report "0 tests". If a class you expect does not run, re-run with `upgrade=True`.

`summary_only`
:   *bool · default `False`* — Return only Odoo's aggregate `N failed, M error(s) of K tests` line and an `output_id`; the full output stays available through [`read_output`](#read_output).

**Use it when**

- Verifying a change before opening a pull request.
- Iterating on one failing test — narrow with `test_tags` rather than spending minutes on a full module upgrade.
- Driving CI from an agent: `summary_only=True` keeps the response small and the log reachable.

```bash
oduflow call run_odoo_tests '{
  "env_name": "main",
  "modules": "sale_custom",
  "test_tags": "/sale_custom:TestInvoice.test_total",
  "upgrade": false,
  "summary_only": true
}'
```

### `list_installed_modules`

List Odoo modules and their states as a table of name, state and installed
version. By default only installed modules are shown.

**Parameters**

`env_name`
:   *str · required* — The environment.

`name_filter`
:   *str · default empty* — Substring match on the module name (e.g. `"sale"` matches `sale`, `sale_management`, `pos_sale`).

`state_filter`
:   *str · default `"installed"`* — Module state: `installed`, `uninstalled`, `to upgrade`, `to install`. Pass an empty string for all states.

**Use it when**

- Checking whether a dependency is present before installing or testing.
- Auditing what a template-provisioned database actually has enabled.
- Finding modules stuck in `to upgrade` after a failed run.

### `get_environment_logs`

Retrieve the last N lines from the environment's Odoo container log.

**Parameters**

`env_name`
:   *str · required* — The environment.

`n_lines`
:   *int · default `100`* — Number of recent log lines to retrieve.

`grep`
:   *str · default empty* — Case-insensitive substring filter — useful for finding a specific error, module or message.

`level`
:   *str · default empty* — Odoo log level filter: `ERROR`, `WARNING` or `CRITICAL`. Combines with `grep`.

**Use it when**

- Odoo is running but misbehaving at runtime (a cron, a request, a worker).
- Tracking a warning that does not surface in a tool response.

!!! note "Not for apply failures"
    [`pull_and_apply`](#pull_and_apply), [`switch_branch`](#switch_branch) and
    the module tools already return their errors and tracebacks inline. Reach
    for the container log for *runtime* problems, not for those.

### `read_output`

Read from a cached tool output by ID. Tools that can produce large output
(installs, upgrades, tests, `pull_and_apply`) cache it server-side and return
an `output_id`; this tool explores it without re-running anything.

**Parameters**

`output_id`
:   *str · required* — The cached output ID (e.g. `"a3f7c012"`) returned by the original tool.

`mode`
:   *str · default `"lines"`* — `lines` (a range, paginated with `start`/`end`, default first 200), `errors` (only ERROR/WARNING/CRITICAL with ±5 lines of context), `grep` (case-insensitive substring search with line numbers), `info` (metadata only — line count, char count, error count, source tool), `tail` (last 100 lines).

`start`
:   *int · default `1`* — First line to return, 1-indexed. Used with `lines` and `grep`.

`end`
:   *int · default `0`* — Last line to return. `0` means `start+200` for `lines`, all results for `grep`.

`grep`
:   *str · default empty* — Search pattern for `mode="grep"`; case-insensitive substring.

**Use it when**

- A `summary_only=True` call reported a failure and you need the traceback.
- A test run produced thousands of lines and you want only the errors (`mode="errors"`).
- Paging through a long install log without flooding the conversation.

```bash
oduflow call read_output '{"output_id": "a3f7c012", "mode": "errors"}'
```

## Odoo Data Access

The `odoo_*` tools talk to the live Odoo HTTP server, exactly like an external
RPC client. Two consequences worth internalising:

- Edited **Python** code stays invisible until the environment is restarted ([`pull_and_apply`](#pull_and_apply) / [`restart_environment`](#restart_environment)); XML views do reload.
- Every call is **its own committed transaction**. Use [`run_odoo_shell`](#run_odoo_shell) when you need a fresh registry, `sudo()`, private methods, a rollback, or several steps in one transaction.

### `odoo_schema`

Inspect the Odoo schema: list models, or describe one model's fields
(XML-RPC `fields_get`).

Call this **before** writing a domain or a values dict — guessing field names
is the most common cause of an empty result set or a confusing error.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · default empty* — Technical model name (e.g. `sale.order`). Empty lists models instead.

`name_filter`
:   *str · default empty* — Substring filter — on model names when listing models, on field names when describing a model.

`attributes`
:   *str · default `"string,type,relation,required,readonly,selection"`* — Comma-separated field attributes to return. Odoo prunes them server-side, so a short list keeps the response small. Empty string returns every attribute.

`as_user`
:   *str · default empty* — Login or numeric id to inspect as (empty = admin). Field visibility can differ per user.

`limit`
:   *int · default `200`* — Maximum models when listing (`0` = no limit). Ignored when describing one model.

`offset`
:   *int · default `0`* — Models to skip when listing, for paging.

**Use it when**

- You need the exact technical name of a field before querying or writing.
- Finding which models a custom addon added.
- Checking whether a field is required, readonly, or a relation — and to what.

### `odoo_search_read`

Search and read records — the ORM equivalent of XML-RPC `search_read`, with
access rights and record rules applied.

Prefer this over [`run_odoo_shell`](#run_odoo_shell) for reading data: it is far
faster and returns JSON you can parse.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · required* — Technical model name (e.g. `res.partner`).

`domain`
:   *str · default `"[]"`* — Odoo search domain as JSON (e.g. `'[["state","=","sale"]]'`). A single bare leaf is accepted and wrapped for you.

`fields`
:   *str · default empty* — Comma-separated field names, or a JSON array. **Always pass this** — reading every field pulls binary columns and blows up the response.

`limit`
:   *int · default `80`* — Maximum rows, applied server-side.

`offset`
:   *int · default `0`* — Rows to skip, for paging.

`order`
:   *str · default empty* — SQL-style ordering (e.g. `"date_order desc, id"`).

`count_only`
:   *bool · default `False`* — Return only the number of matching records (`search_count`); `fields` and `limit` are ignored.

`as_user`
:   *str · default empty* — Login or numeric user id to run as. Empty = the environment's admin.

`context`
:   *str · default empty* — JSON object added to the call context (e.g. `'{"lang": "fr_FR", "active_test": false}'`).

**Use it when**

- Reading business data to verify a change took effect.
- Checking what a *particular* user can see (`as_user="portal@example.com"`) — permissions bugs show up here and nowhere else.
- Counting records cheaply (`count_only=True`).
- Including archived records (`context='{"active_test": false}'`).

```bash
oduflow call odoo_search_read '{
  "env_name": "main",
  "model": "sale.order",
  "domain": "[[\"state\",\"=\",\"sale\"]]",
  "fields": "name,partner_id,amount_total",
  "limit": 10
}'
```

### `odoo_create`

Create one or many records — the ORM equivalent of XML-RPC `create`. Returns
the new ids.

!!! warning "Committed on success — no dry run"
    For a deliberate rollback, or several steps that must succeed or fail
    together, use [`run_odoo_shell`](#run_odoo_shell). If the call times out,
    **verify with a read before retrying** — a repeat can create duplicates.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · required* — Technical model name (e.g. `res.partner`).

`values`
:   *str · required* — JSON object of field values, or a JSON array of such objects to create several records in one call.

`as_user`
:   *str · default empty* — Login or numeric user id to run as. Empty = admin.

`context`
:   *str · default empty* — JSON object added to the call context.

**Use it when**

- Seeding test data for a scenario.
- Reproducing a customer record that triggers a bug.

### `odoo_write`

Update records — the ORM equivalent of XML-RPC `write`.

!!! warning "Committed on success — no dry run"
    Use [`run_odoo_shell`](#run_odoo_shell) when you need a rollback or one
    transaction across several steps.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · required* — Technical model name.

`ids`
:   *str · required* — Record ids: `"42"`, `"1,2,3"` or `"[1,2,3]"`.

`values`
:   *str · required* — JSON object of field values to set.

`as_user`
:   *str · default empty* — Login or numeric user id to run as. Empty = admin.

`context`
:   *str · default empty* — JSON object added to the call context.

**Use it when**

- Flipping a record into the state a test needs.
- Archiving instead of deleting (`values='{"active": false}'`) — usually what is actually wanted.

### `odoo_unlink`

Delete records — the ORM equivalent of XML-RPC `unlink`.

!!! danger "Destructive and immediate"
    The records are gone when this returns, and there is no rollback. Confirm
    the target set with [`odoo_search_read`](#odoo_search_read) first.
    **Archiving** (`active = false` via [`odoo_write`](#odoo_write)) is usually
    what is actually wanted.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · required* — Technical model name.

`ids`
:   *str · required* — Record ids to delete: `"42"`, `"1,2,3"` or `"[1,2,3]"`.

`as_user`
:   *str · default empty* — Login or numeric user id to run as. Empty = admin.

`context`
:   *str · default empty* — JSON object added to the call context.

**Use it when**

- Cleaning up records you created for a test, in a throwaway environment.

### `odoo_call`

Call a public Odoo model method — the XML-RPC `execute_kw` escape hatch for
everything the dedicated tools do not cover: `read_group`, `name_search`,
`default_get`, `copy`, `message_post`, `action_confirm`, and any method a
custom addon exposes.

`ids` is prepended as the first positional argument, so `model="sale.order"`,
`method="action_confirm"`, `ids="42"` sends `args=[[42]]`. Leave `ids` empty for
model-level (`@api.model`) methods.

**Parameters**

`env_name`
:   *str · required* — The environment.

`model`
:   *str · required* — Technical model name (e.g. `sale.order`).

`method`
:   *str · required* — Public method name. The CRUD mutations `create`, `write` and `unlink` are **rejected** here — use their dedicated tools.

`ids`
:   *str · default empty* — Record ids prepended as the first positional argument. Empty for model-level methods.

`args`
:   *str · default `"[]"`* — JSON array of the remaining positional arguments.

`kwargs`
:   *str · default `"{}"`* — JSON object of keyword arguments.

`as_user`
:   *str · default empty* — Login or numeric user id to run as. Empty = admin.

`context`
:   *str · default empty* — JSON object added to the call context.

**Use it when**

- Grouping and aggregating: `method="read_group"`, `args='[[], ["amount_total:sum"], ["partner_id"]]'`.
- Autocomplete lookups: `method="name_search"`, `kwargs='{"name": "Acme"}'`.
- Triggering business logic: `method="action_confirm"`, `ids="42"`.

!!! note "Private methods are rejected"
    Methods with a leading underscore are refused here (Odoo 19 refuses them
    server-side too). Use [`run_odoo_shell`](#run_odoo_shell) for those.

### `run_db_query`

Execute SQL directly against the environment's PostgreSQL database.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`query`
:   *str · required* — SQL to execute (e.g. `"SELECT id, name FROM res_partner LIMIT 10"`).

`output_format`
:   *str · default `"csv"`* — `csv` (compact, good for agents) or `human` (pretty table — use when relaying results to a person).

`max_rows`
:   *int · default `100`* — Maximum rows returned. The query itself is **not** modified — truncation happens on the output, and a note suggests adding `LIMIT`.

**Use it when**

- Inspecting data the ORM hides or makes awkward (`ir_model_data`, raw join tables, orphan rows).
- Diagnosing a migration or a broken `ir.module.module` state.
- Fast aggregate checks that would be slow through the ORM.

!!! note "The ORM is usually the right layer"
    SQL bypasses computed fields, record rules and constraints. Prefer
    [`odoo_search_read`](#odoo_search_read) unless you specifically need the
    raw tables.

## Inside the Odoo Container

### `read_file_in_odoo`

Read a text file, or list a directory, inside the Odoo container. A directory
path returns a listing (like `ls -la`); a text file returns its contents (first
100 KB by default). Binary files are not supported — use
[`run_odoo_command`](#run_odoo_command) for those.

Prefer this over `run_odoo_command` with `cat` or `ls`.

**Parameters**

`env_name`
:   *str · required* — The environment.

`path`
:   *str · required* — Absolute path inside the container (e.g. `/mnt/extra-addons/my_module/__manifest__.py`).

`read_range`
:   *str · default empty* — Line range `"START:END"` (e.g. `"1:50"`, `"100:200"`). Omitted returns the whole file, up to 100 KB.

**Use it when**

- Reading Odoo core source to understand a method you are overriding.
- Inspecting the addon layout actually mounted at `/mnt/extra-addons/`.
- Checking `/etc/odoo/odoo.conf`.
- Verifying a file landed after [`pull_and_apply`](#pull_and_apply).

### `write_file_in_odoo`

Write a text file inside the Odoo container. Parent directories are created,
existing files are overwritten, and content is transferred via container stdin
so shell escaping is never an issue.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`path`
:   *str · required* — Absolute path inside the container (e.g. `/tmp/import_data.csv`).

`content`
:   *str · required* — Text content to write.

`user`
:   *str · default `"odoo"`* — OS user to own the file. Use `"root"` for system paths.

**Use it when**

- Writing a CSV for a data import.
- Creating or amending `odoo.conf` settings.
- Dropping a one-off Python script for `odoo shell` to execute.
- Placing test fixture files (demo data, config).

!!! warning "Not for source code"
    Do **not** edit repository source this way. All code changes must go
    through git commit → push → [`pull_and_apply`](#pull_and_apply).

### `search_in_odoo`

Recursive fixed-string grep inside the Odoo container, returning matching lines
with file paths and line numbers.

**Parameters**

`env_name`
:   *str · required* — The environment.

`pattern`
:   *str · required* — Search pattern (fixed string, case-sensitive). Regex is deliberately unsupported to avoid escaping problems.

`path`
:   *str · default `"/mnt/extra-addons"`* — Directory to search. Use `/usr/lib/python3/dist-packages/odoo/addons` to search Odoo core.

`glob`
:   *str · default `"*.py"`* — File glob. Use `"*.xml"` for views and data, `"*.js"` for frontend, `"*"` for everything.

`max_results`
:   *int · default `50`* — Maximum matching lines to return.

**Use it when**

- Finding where a field is defined across all addons.
- Locating a model class in Odoo core (`pattern="class SaleOrder"`).
- Finding every import of a module, or an XML record id.

### `run_odoo_command`

Execute an arbitrary shell command inside the Odoo container. The command runs
through `sh -c`, so pipes, redirections, `&&`, `cd x && y`, `$VAR` and quoting
all behave as written — one call, one shell line.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`command`
:   *str · required* — The shell command (e.g. `"ls /mnt/extra-addons | head"`).

`user`
:   *str · default `"odoo"`* — OS user to run as. Use `"root"` for privileged operations.

`shell`
:   *bool · default `True`* — Run via `sh -c`. Pass `False` for exact argv semantics: the string is split on whitespace and executed directly, so `|`, `>`, `&&`, `*` and `$VAR` reach the program as literal arguments.

**Use it when**

- Installing a Python package ad hoc to test a hypothesis.
- Inspecting processes, disk usage or file permissions inside the container.
- Running a binary tool the dedicated file tools cannot cover.

### `run_odoo_shell`

Execute Python inside `odoo shell` with full ORM access — `self.env`, all
models, and the environment's database. Use `print()` to produce output.

**Transaction handling.** `odoo shell` rolls back its cursor when the piped
script finishes, so ORM writes would otherwise be discarded. With
`auto_commit=True` (the default) the transaction is committed after your code
runs, so a successful run persists; if the code raises, the commit is never
reached and the transaction is rolled back with the traceback returned.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`python_code`
:   *str · required* — Python to execute. Use `print()` for output.

`auto_commit`
:   *bool · default `True`* — Commit after a successful run. Set `False` for a read-only or dry-run inspection where nothing should persist.

**Use it when**

- You need a fresh registry, `sudo()`, a private method, a rollback, or several steps in one transaction — none of which the `odoo_*` RPC tools can give you.
- Testing computed fields, or debugging workflow transitions and access rights.
- Running a data-fix script.
- Inspecting without persisting (`auto_commit=False`).

```bash
oduflow call run_odoo_shell '{
  "env_name": "main",
  "python_code": "print(self.env[\"sale.order\"].search_count([]))",
  "auto_commit": false
}'
```

### `http_request_to_odoo`

Make an HTTP request to the running Odoo instance, from the host to the
container's mapped port.

**Parameters**

`env_name`
:   *str · required* — The environment.

`path`
:   *str · required* — URL path (e.g. `/web/health`, `/jsonrpc`, `/my/invoices`).

`method`
:   *str · default `"GET"`* — One of `GET`, `POST`, `PUT`, `DELETE`.

`body`
:   *str · default empty* — Request body, typically JSON. Empty for GET.

`headers`
:   *str · default empty* — Comma-separated `KEY:VALUE` pairs (e.g. `"Content-Type:application/json,Accept:text/html"`).

`session_id`
:   *str · default empty* — Odoo session ID for authenticated requests. Obtain one by POSTing to `/web/session/authenticate`, or mint one with [`connect_as_user`](#connect_as_user).

**Use it when**

- Testing a custom web controller or REST endpoint.
- Making a JSON-RPC call.
- Verifying access rights by checking 200 vs 403.
- A quick health check (`GET /web/health`).

### `reset_admin_password`

Reset the `admin` user's password in the environment's Odoo database. The
password is hashed with passlib (pbkdf2_sha512) inside the container and
written to the `res_users` record where `login = 'admin'`.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`new_password`
:   *str · default `"test"`* — The new admin password.

**Use it when**

- A template or production copy carries an admin password nobody knows.
- Handing a demo environment to someone who needs to log in normally.

### `connect_as_user`

Mint a passwordless Odoo login session for a user and return its `session_id`
cookie plus a URL — the same authenticated state a password login produces,
without setting or transmitting any password.

Hand the cookie to a browser automation tool (e.g. Playwright
`context.add_cookies([...])` then `page.goto(url)`) to land directly in an
authenticated session, skipping the login form.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`user`
:   *str · required* — The target user's login (e.g. `"jane@acme.com"`) or numeric id.

**Use it when**

- Driving end-to-end browser tests without scripting the login form.
- Exercising a feature across roles in one run — admin, sales manager, portal. Portal users are supported: they land on `/web` and Odoo redirects them to their portal.
- Reproducing a "works for me, not for them" permissions report.

!!! warning "The session id is a live credential"
    It is shown in the tool's output (and therefore in the transcript) — treat
    it like a password. The tool grants no new privilege: whoever can call it
    can already [`run_odoo_shell`](#run_odoo_shell).

## Translations

### `export_module_translations`

Export a module's translation catalogue using Odoo's own exporter.

Without `lang` this produces the `.pot` template: every translatable term with
an empty translation, including the `_()` / `_lt()` messages from the module's
Python sources. With `lang` it produces a `.po` whose translations are filled
from what the database currently holds — useful for seeing what actually got
applied.

The file is written into the module's own `i18n/` directory inside the
container, which is a read-write mount of the environment's checkout — so in
live-mount mode it lands directly in your working tree. The response carries a
summary plus a one-time download URL, never the file body.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`module`
:   *str · required* — A single installed module's technical name (e.g. `sale_custom`).

`lang`
:   *str · default empty* — Locale to fill translations from (e.g. `pl_PL`). Omit for a `.pot` template.

**Use it when**

- Getting the authoritative term list before writing translations.
- Checking that `_()` messages are being picked up — look at the `code` count.
- Snapshotting what the database holds for one language.

### `translation_status`

Report whether a module's translations actually landed, and what to do next.

It compares the term template Odoo derives from the module, the translations
stored in the database, and the committed `i18n/<lang>.po` files, returning a
**verdict per language** rather than three catalogues to reconcile. Use it
after loading translations: Odoo's importer is silent about the two ways a
`.po` fails, and this is what makes them visible.

- Entries with no `#:` reference line import as **zero** translations, with no warning at all, unless a sibling `<module>.pot` supplies the metadata.
- Entries with no `#. module:` comment **abort the import** outright unless that sibling template supplies it.

Verdicts: `OK`, `PARTIAL`, `NOT LOADED`, `NOT TRANSLATED`,
`IMPORT SILENTLY DROPPED`, `IMPORT ABORTS`, `NO FILE`, `NOT ACTIVATED` — each
with the coverage behind it and the call that fixes it.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment.

`module`
:   *str · required* — A single installed module's technical name.

`langs`
:   *str · default empty* — Comma-separated locales to check (e.g. `"pl_PL,ru_RU"`). Omit to check every language activated in the database except `en_US`.

**Use it when**

- A translation "was loaded" but the UI is still in English.
- Reviewing translation coverage before a release.
- Diagnosing a `.po` import that reported success and did nothing.

## Template Management

Templates are reusable database + filestore snapshots that make environment
creation fast. See [Template Management](templates.md).

### `save_as_template`

Save an environment's database and filestore as a template.

By default this creates a **new** template and refuses to overwrite an existing
one — pick a fresh `template_name`. `overwrite=True` deliberately re-baselines
an existing template: its database and filestore are replaced, and other
environments using it with overlay-mounted filestores are remounted against the
new baseline. On re-baseline their filestore changes (the overlay upper layer)
are **preserved** by default; `reset_env_changes=True` discards them. The source
environment itself is always reset — its data just became the new template.

Lock: team. Destructive when `overwrite=True` or `reset_env_changes=True`.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment whose database and filestore become the template.

`template_name`
:   *str · required* — Template profile to publish into.

`reset_env_changes`
:   *bool · default `False`* — Discard other environments' filestore deltas (destructive). Default preserves them.

`overwrite`
:   *bool · default `False`* — Allow re-baselining an existing template. Default refuses if it already exists.

**Use it when**

- A configured environment (modules installed, data set up) should become the starting point for future branches.
- Re-baselining a team template after a round of configuration work.

!!! danger "Requires explicit user permission"
    If the user has not clearly and unambiguously asked to save a *specific*
    environment as a template, do not call this. Both `overwrite=True` and
    `reset_env_changes=True` require an explicit request of their own.

### `save_production_as_template`

Save a **production's** database and filestore as a dev template. The
production keeps serving throughout — the dump is a consistent snapshot and
nothing is stopped or modified on the production side.

Overwrite and reset semantics are identical to
[`save_as_template`](#save_as_template).

Lock: team. Requires production hosting. Destructive when `overwrite=True` or `reset_env_changes=True`.
{ .odu-tool-meta }

**Parameters**

`prod_name`
:   *str · required* — The production whose database and filestore are copied.

`template_name`
:   *str · required* — Template profile to publish into.

`reset_env_changes`
:   *bool · default `False`* — Discard other environments' filestore deltas (destructive).

`overwrite`
:   *bool · default `False`* — Allow re-baselining an existing template.

**Use it when**

- Developers need to work against realistic production data.
- Refreshing the managed `prod-<name>` template that backs `create_environment(from_production=...)`.

!!! danger "The template holds unsanitized production data"
    Real customer records, real email addresses, real API credentials.
    Sanitization happens *later*, when an environment is created from it
    ([`create_environment`](#create_environment) runs Odoo's neutralization
    plus the repository's custom sanitize scripts by default). Treat the
    template itself as production-confidential. Requires explicit user
    permission. Refused when the production has MCP copy-to-dev disabled.

### `list_templates`

List available template profiles (database + filestore snapshots), including
the branch and commit each database snapshot was taken from.

**Parameters**

*None.*

**Use it when**

- Choosing a `template_name` for [`create_environment`](#create_environment).
- Checking how stale a template is before building on it.

### `rename_template`

Rename a template profile — its directory and its PostgreSQL template
database.

Refused if any environment was created from this template: the template
reference is fixed at creation time and cannot be updated on a running
environment. Delete those environments first, or leave the template as is.

Lock: team.
{ .odu-tool-meta }

**Parameters**

`template_name`
:   *str · required* — Current name.

`new_name`
:   *str · required* — New name.

**Use it when**

- A template's name no longer reflects the Odoo version or customer it holds.

### `delete_template`

Permanently remove a template profile — its template database and its files on
disk.

Lock: team. Destructive and irreversible.
{ .odu-tool-meta }

**Parameters**

`template_name`
:   *str · required* — Template profile to delete.

**Use it when**

- A template is genuinely obsolete and disk space must be reclaimed.

!!! danger "Never on your own initiative"
    Every environment depending on this template loses its baseline and cannot
    be recreated until a new template is set up. Requires explicit user
    permission and confirmation.

### `import_template_from_odoo`

Import a template from a running Odoo instance through its database manager
API. Downloads a full ZIP backup, or a database-only PostgreSQL custom dump,
and loads it into PostgreSQL as a template database.

Lock: team.
{ .odu-tool-meta }

**Parameters**

`odoo_url`
:   *str · required* — Base URL of the Odoo instance (e.g. `https://my-odoo.example.com`).

`master_pwd`
:   *str · required* — Odoo master password (database manager password).

`db_name`
:   *str · default empty* — Database to back up. Empty auto-detects, and fails if several databases exist.

`template_name`
:   *str · default `"default"`* — Template profile to create.

`without_filestore`
:   *bool · default `False`* — Request a database-only PostgreSQL custom dump instead of the full ZIP.

**Use it when**

- Onboarding a customer whose Odoo runs elsewhere.
- Seeding a first template without shell access to the source server.

### `refresh_template`

Re-apply a template's current filestore to live overlay environments: unmount
and remount every overlay-mounted environment using this template against the
template's current on-disk filestore.

By default each environment's filestore changes (the overlay upper layer) are
**preserved** — non-destructive.

Lock: team. Destructive when `reset_env_changes=True`.
{ .odu-tool-meta }

**Parameters**

`template_name`
:   *str · required* — Template profile to re-apply.

`reset_env_changes`
:   *bool · default `False`* — Discard environments' filestore deltas and reset every affected environment to the template baseline (destructive).

**Use it when**

- The template filestore was changed on disk and live environments should see it.
- Re-syncing an environment that was busy and got skipped during an import or save.

!!! danger "Requires explicit user permission"
    Especially with `reset_env_changes=True`.

### `attach_filestore`

Attach or replace a template's filestore from a directory, an archive, or a
remote rsync source.

Archive and directory sources are normalized to the Odoo filestore layout
(`XX/<sha1>`). Live environments' changes are preserved by default.

Lock: team.
{ .odu-tool-meta }

**Parameters**

`template_name`
:   *str · required* — Template profile to attach the filestore to.

`source`
:   *str · required* — A local directory, a local `.zip` / `.tar` / `.tar.gz` archive, an `rsync://` URL, or an SSH-style rsync source such as `user@host:/path`.

`reset_env_changes`
:   *bool · default `False`* — Discard environments' filestore deltas (destructive).

`strip_prefix`
:   *str · default `"auto"`* — Wrapper directory to strip. `"auto"` detects one such as the database name; pass an explicit prefix when auto-detection is ambiguous.

**Use it when**

- A database-only import ([`import_template_from_odoo`](#import_template_from_odoo) with `without_filestore=True`) needs its attachments.
- The filestore arrives separately, e.g. rsynced from the customer's server.

## Auxiliary Services

Managed side-car containers — Redis, Meilisearch, MinIO, anything your stack
needs. See [Auxiliary Services](services.md).

### `create_service`

Create a managed auxiliary service container.

A service has **exactly one exposure model**: a catch-all `port`, or a
restricted Traefik `routes` allowlist. The two are mutually exclusive, and
`port` remains required outside Traefik mode.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — Short service name (e.g. `redis`, `meilisearch`).

`image`
:   *str · required* — Docker image with tag (e.g. `redis:7`, `getmeili/meilisearch:v1.6`).

`port`
:   *int · default `0`* — Catch-all exposure: forward every path to this one container port. Required outside Traefik. Mutually exclusive with `routes`.

`routes`
:   *list · default none* — Traefik exposure allowlist. Each object has `path`, a backend `port`, and optional `strip_prefix`. Unlisted paths return Traefik 404. Mutually exclusive with `port`.

`hostname`
:   *str · default empty* — Custom hostname for Traefik routing (Traefik mode only).

`env_vars`
:   *str · default empty* — Comma- or newline-separated `KEY=VALUE` pairs. Commas inside values are preserved unless what follows looks like another `KEY=` — so `"CONNECT_MCP_TOOL_GROUPS=write,collaboration,documents"` is one variable; put one pair per line when in doubt. A value `secret:<name>` references a [team secret](#list_secrets).

`host_mode`
:   *bool · default `False`* — Run in host network mode instead of the shared Docker network, for services needing direct host network access. Traefik routing still works.

`volumes`
:   *str · default empty* — Comma-separated mounts, each `volume_name:/container/path[:ro|rw]`. Volumes must exist first ([`create_volume`](#create_volume)).

`privileged`
:   *bool · default `False`* — Full host access; implies all Linux capabilities. Mutually exclusive with `net_admin`.

`net_admin`
:   *bool · default `False`* — Add the `NET_ADMIN` capability — required for VPN/WireGuard, tun/tap devices and iptables inside the container.

`command`
:   *str · default empty* — Start command overriding the image `CMD`, as a shell-quoted string (e.g. `"server /data --console-address :9001"`). The image `ENTRYPOINT` is not affected.

`runtime`
:   *dict · default none* — Explicit Docker `tmpfs`, private cgroupns, `stop_signal` and `stop_timeout` settings.

**Use it when**

- Odoo needs a cache, search engine, object store or message broker alongside it.
- You want a scratch container on the team network for an experiment.

!!! note "Reserved mount in Traefik TLS mode"
    The system ACME volume is mounted automatically at `/etc/traefik:ro`. Do
    not include it in `volumes` — `/etc/traefik` is reserved.

```bash
oduflow call create_service '{
  "name": "redis",
  "image": "redis:7",
  "port": 6379,
  "volumes": "redis-data:/data"
}'
```

### `update_service`

Preflight the configuration, pull the latest image and optionally change any
setting. The container is recreated when the image or a setting changes;
settings that are not overridden are preserved. This is the **preferred** way
to change a service — no manual delete-and-recreate.

Three parameters are **tri-state**: omitted keeps the current value, a value
fully replaces it, and an empty value clears it.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The service to update.

`env_vars`
:   *str · default empty* — `KEY=VALUE` pairs that **fully replace** existing variables. Empty keeps them.

`image`
:   *str · default empty* — New image with tag (e.g. `redis:8`). Empty keeps the current image.

`port`
:   *int · default `0`* — New container port. `0` keeps the current one.

`hostname`
:   *str · default empty* — New Traefik hostname. Empty keeps the current one.

`host_mode`
:   *bool · default none* — Host network mode. Unset keeps the current mode.

`volumes`
:   *str · default none* — Mounts that **fully replace** existing user volumes. Unset keeps them; an **empty string unmounts every volume** (the volumes keep their data). The implicit Traefik TLS mount at `/etc/traefik:ro` is preserved separately.

`privileged`
:   *bool · default none* — Privileged mode. Unset keeps the current setting. Mutually exclusive with `net_admin`.

`net_admin`
:   *bool · default none* — Add (`true`) or remove (`false`) the `NET_ADMIN` capability. Unset keeps current capabilities.

`routes`
:   *list · default none* — Full replacement route list. Unset preserves it. Pass `[]` together with `port` to return to a single catch-all port.

`command`
:   *str · default none* — New start command as a shell-quoted string. Unset keeps the current command; an **empty string drops the override** and falls back to the image `CMD`. Note this differs from `env_vars`/`image`, where empty means "keep".

`runtime`
:   *dict · default none* — Replace lifecycle settings; omit to preserve, pass `{}` to clear.

**Use it when**

- Pulling a new image tag for a running service.
- Rotating a service's credentials or pointing it at a new database.
- Recreating a legacy service so it picks up newly implicit system mounts.

!!! tip "Read before you write"
    Call [`get_service_info`](#get_service_info) first. `volumes` and
    `env_vars` are full replacements, so a partial argument silently drops
    what you left out. Protected services refuse updates until an
    administrator unprotects them in the dashboard.

### `delete_service`

Stop and remove a service container, keeping its preset so
[`restore_service`](#restore_service) can bring it back.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The service to delete.

`save_preset`
:   *bool · default `True`* — Keep the saved configuration. Pass `false` to delete the preset along with the container.

**Use it when**

- Freeing resources while keeping the ability to restore the exact configuration.
- Retiring a service for good (`save_preset=false`).

!!! note "Protected services"
    A protected service refuses to be deleted until an administrator
    unprotects it in the dashboard.

### `restart_service`

Restart a managed service container.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The service to restart (e.g. `redis`, `meilisearch`).

**Use it when**

- The service is wedged, or picked up a configuration file you rewrote in its volume.

### `list_services`

List all managed service containers.

**Parameters**

*None.*

**Use it when**

- Taking stock of what is running alongside your environments.

### `get_service_info`

Full live state of one service: image with digest, runtime status, port and
routes, hostname, URL, `host_mode`, start command, volumes, environment
variables, capabilities, privileged flag, restart count, `started_at`, and
whether a saved preset exists.

In Traefik TLS mode this also reports the implicit `/etc/traefik:ro` ACME mount,
which is not stored in the preset.

**Parameters**

`name`
:   *str · required* — The service to inspect (e.g. `redis`, `fs`).

**Use it when**

- **Before** [`update_service`](#update_service) — so full-replacement arguments preserve what you are not changing.
- Confirming which image digest is actually running.
- Debugging routing: catch-all port vs. route allowlist.

### `get_service_logs`

Retrieve recent logs from a managed service container.

**Parameters**

`name`
:   *str · required* — The service.

`n_lines`
:   *int · default `100`* — Number of recent log lines.

**Use it when**

- A service starts and immediately exits.
- Checking whether a service accepted its configuration.

### `run_service_command`

Execute a shell command inside a service container. The command runs through
`sh -c`, so pipes, redirections and `&&` work as written.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The service (e.g. `redis`, `meilisearch`).

`command`
:   *str · required* — The shell command (e.g. `"redis-cli ping"`, `"ls /data | wc -l"`).

`user`
:   *str · default `"root"`* — OS user to run as.

`shell`
:   *bool · default `True`* — Run via `sh -c`. Pass `False` for exact argv semantics, or when the image ships no shell at all (scratch/distroless).

**Use it when**

- Probing the service with its own CLI (`redis-cli`, `mc`, `psql`).
- Verifying a mounted volume actually contains what you expect.

## Service PostgreSQL Databases

Persistent, team-scoped databases for auxiliary services — separate from Odoo's
own databases and from the environment lifecycle.

### `create_service_database`

Create a persistent PostgreSQL database with a dedicated **non-superuser**
owner. It belongs to the current team, survives service updates and deletion,
and is reachable from bridge-mode team services at the returned host and port.
Returns `DATABASE_URL` and the individual `PG*` credentials.

Lock: database.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — Stable lowercase resource name using letters, digits, `-` or `_`.

`cluster`
:   *str · default `"dev"`* — `dev` (the shared development cluster) or `prod` (the dedicated production cluster; requires production hosting). Databases on `prod` are covered by the cluster-wide WAL-G backups and are **not** counted against the development disk quota.

**Use it when**

- A service (n8n, Keycloak, a custom app) needs durable storage that outlives its container.
- You want backup coverage for a non-Odoo database (`cluster="prod"`).

### `list_service_databases`

List managed database names, live status, size and connection count — without
passwords.

**Parameters**

*None.*

**Use it when**

- Auditing what the team has provisioned and how much space it uses.
- Checking whether anything is still connected before a deletion.

### `get_service_database`

Explicitly reveal the connection credentials for one managed database.

Lock: database.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The database.

**Use it when**

- Wiring the credentials into a service's `env_vars`.
- Recovering a `DATABASE_URL` nobody wrote down.

!!! warning "The response contains a plaintext password"
    Treat it as a secret, and pass only the variables the target service
    actually needs. Consider storing it as a [team secret](#list_secrets)
    instead of inlining it.

### `rotate_service_database_password`

Rotate the owner password and return replacement credentials.

Lock: database.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The database.

**Use it when**

- A credential leaked, or appeared somewhere it should not have.
- Routine rotation before handing a project over.

!!! note "Containers keep the old value"
    Existing containers keep the old password until their environment
    variables are updated and the containers are recreated or restarted —
    see [`update_service`](#update_service).

### `delete_service_database`

Permanently drop the database and its login role, terminating active
PostgreSQL connections first. Service containers are **not** modified and will
fail to reconnect until reconfigured.

Lock: database. Destructive and irreversible.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The database to drop.

**Use it when**

- The owning service is gone for good and the data is genuinely disposable.

!!! danger "Protected databases"
    A protected database cannot be deleted until an administrator unprotects
    it in the dashboard.

## Volumes

Named Docker volumes for use with services. See
[Auxiliary Services](services.md).

### `create_volume`

Create a named Docker volume.

Lock: volume.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — Short volume name (e.g. `redis-data`, `meilisearch-data`).

`description`
:   *str · default empty* — What this volume is for.

**Use it when**

- A service needs durable storage. Volumes must exist **before** they can be mounted by [`create_service`](#create_service).

### `list_volumes`

List all managed Docker volumes and which services use them.

**Parameters**

*None.*

**Use it when**

- Finding orphaned volumes to reclaim.
- Checking what a volume is attached to before deleting it.

### `inspect_volume`

Detailed information about one volume, including which services use it.

**Parameters**

`name`
:   *str · required* — The volume to inspect.

**Use it when**

- Confirming a volume is unused before [`delete_volume`](#delete_volume).

### `delete_volume`

Delete a managed Docker volume. Fails if any service still uses it.

Lock: volume. Destructive — the data goes with it.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The volume to delete.

**Use it when**

- A service was retired and its data is no longer needed.

### `read_file_in_volume`

Read a text file, or list a directory, inside a Docker volume. A temporary
helper container is spun up to access the contents. Directories return an
`ls -la`-style listing; text files return contents up to 100 KB. Binary files
are detected and rejected.

**Parameters**

`name`
:   *str · required* — The volume (e.g. `redis-data`).

`path`
:   *str · required* — Path inside the volume (e.g. `data/dump.rdb`, `config/redis.conf`). A leading `/` is optional — paths are relative to the volume root.

`read_range`
:   *str · default empty* — Line range `"START:END"` (e.g. `"1:50"`). Omitted returns the full file, up to 100 KB.

**Use it when**

- Checking a service's configuration file without starting the service.
- Confirming data landed in the volume after an import.

### `write_file_in_volume`

Write a text file inside a Docker volume, creating parent directories and
overwriting an existing file.

Lock: volume.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The volume.

`path`
:   *str · required* — Path inside the volume (e.g. `config/my.conf`). Leading `/` optional.

`content`
:   *str · required* — Text content to write.

**Use it when**

- Seeding a service's configuration before its first start.
- Fixing a config that keeps the service from booting (then [`restart_service`](#restart_service)).

### `search_in_volume`

Recursive fixed-string grep inside a Docker volume, returning matching lines
with paths and line numbers.

**Parameters**

`name`
:   *str · required* — The volume.

`pattern`
:   *str · required* — Search pattern (fixed string, case-sensitive).

`path`
:   *str · default empty* — Directory relative to the volume root. Default searches the entire volume.

`glob`
:   *str · default `"*"`* — File glob (e.g. `"*.conf"`, `"*.xml"`).

`max_results`
:   *int · default `50`* — Maximum matching lines.

**Use it when**

- Locating which config file in a volume carries a setting.
- Finding a stale hostname or credential across a service's data.

### `delete_file_in_volume`

Delete a file or directory inside a Docker volume. Cannot delete the volume
root — use [`delete_volume`](#delete_volume) for that.

Lock: volume. Destructive.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The volume.

`path`
:   *str · required* — Path inside the volume to delete (e.g. `data/old-dump.rdb`). Leading `/` optional.

**Use it when**

- Clearing a corrupt cache or an outdated dump so the service rebuilds it.

## Service Presets

A preset is a service's saved configuration. [`delete_service`](#delete_service)
keeps one by default, so a service can be brought back exactly as it was.

### `list_service_presets`

List saved service presets.

**Parameters**

*None.*

**Use it when**

- Checking what can be restored after a cleanup.

### `restore_service`

Recreate a service container from its saved preset, with the same
configuration.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The saved preset to restore.

**Use it when**

- Bringing back a service deleted to free resources.
- Rebuilding a service stack on a fresh host.

### `delete_service_preset`

Remove a saved service preset.

Lock: service.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The preset to delete.

**Use it when**

- A service is retired permanently and its configuration should not linger.

## Secrets

### `list_secrets`

List the **names** of the team's named secrets. Values are write-only: a human
sets them in the Oduflow dashboard, and they can never be read back through
MCP.

Reference a secret in any `env_vars` argument —
[`create_environment`](#create_environment),
[`update_environment`](#update_environment),
[`create_service`](#create_service),
[`update_service`](#update_service) — as `KEY=secret:<name>`. The real value is
substituted only inside the container, while every stored or displayed
configuration keeps the reference.

**Parameters**

*None.*

**Use it when**

- Finding out which secret names exist before wiring one into a container.
- Keeping an API key out of tool arguments, transcripts and stored configuration.

## Container Image Builds

Requires a `[team.X.image_registry]` section in `oduflow.toml`.

### `start_image_build`

Build a container image from the environment checkout's **current HEAD commit**
— sealed with `git archive` under the environment lock, so the source cannot
shift mid-build. The build runs asynchronously server-side and this call
returns a `build_id` immediately. Hard timeouts, log-size caps and per-team
concurrency limits apply.

Lock: environment.
{ .odu-tool-meta }

**Parameters**

`env_name`
:   *str · required* — The environment whose checkout to build from.

`dockerfile`
:   *str · default `"Dockerfile"`* — Dockerfile path relative to the context.

`context`
:   *str · default `"."`* — Build context directory relative to the repository root.

`target`
:   *str · default empty* — Multi-stage build target.

`build_args`
:   *str · default empty* — Comma-separated `KEY=VALUE` Docker build arguments.

**Use it when**

- Producing a deployable image from the exact code you just tested.
- Building a release candidate from a tagged commit.

!!! warning "Build arguments are not secret"
    Their values reach the Dockerfile and the image history. Never pass
    credentials. Push your commits and [`pull_and_apply`](#pull_and_apply)
    first — the build uses the checkout's HEAD at call time.

### `get_image_build`

Status, source commit, publication history and a build log tail for one build.

**Parameters**

`env_name`
:   *str · required* — The environment the build belongs to.

`build_id`
:   *str · required* — The build to inspect.

`tail_lines`
:   *int · default `100`* — Trailing build log lines to include.

**Use it when**

- Polling an asynchronous build to completion.
- Reading the failing Dockerfile step.

### `publish_image_build`

Push a **succeeded** build's exact image — never a rebuild — to one or more
tags under the team's configured registry namespace.

Any syntactically valid tag is accepted, including `latest` and semver.
Overwriting an existing tag is intentional and last-writer-wins. Tags are
pushed one by one, so partial success is possible and each tag reports its own
outcome.

**Parameters**

`env_name`
:   *str · required* — The environment the build belongs to.

`build_id`
:   *str · required* — A build in status `succeeded`.

`repository`
:   *str · required* — Destination repository below the configured prefix (e.g. `"app"`).

`tags`
:   *str · required* — Comma-separated tags to publish, e.g. `"1.4.0,latest"`.

**Use it when**

- Promoting a verified build to `latest` and a version tag in one call.
- Publishing the identical bits you tested, with no risk of a rebuild drifting.

### `cancel_image_build`

Terminate a running build and its Docker connection — even when the current
Dockerfile step produces no output — and move the job to `cancelled`.

**Parameters**

`env_name`
:   *str · required* — The environment the build belongs to.

`build_id`
:   *str · required* — The build to cancel.

**Use it when**

- A build is hung on a silent step and is holding team build concurrency.
- You started a build from the wrong commit.

## Repository Auth

### `setup_repo_auth`

Cache git credentials for a private git host. The token is stored in the team's
git credential store and verified. Git matches credentials by host (and
username), so **one entry covers every repository on that host**; afterwards
[`create_environment`](#create_environment) and
[`add_extra_repo`](#add_extra_repo) can clone with a plain `https://` URL.

Access is verified with `git ls-remote` against `repo_url` when one is given,
otherwise against the provider's API (GitHub, GitLab, Bitbucket).

Lock: team credential store.
{ .odu-tool-meta }

**Parameters**

`repo_url`
:   *str · default empty* — Repository HTTPS URL, used to derive the host and to verify access.

`token`
:   *str · default empty* — Personal access token / app password. **Preferred form** — pass the token here rather than inline in the URL.

`username`
:   *str · default empty* — Account name stored with the token. Optional for GitHub, GitLab and Azure DevOps (defaults to `x-access-token`); **required for Bitbucket app passwords**. Use distinct usernames to keep several tokens for the same host.

`host`
:   *str · default empty* — Git host such as `github.com` or `git.example.com:8443`. Only needed when `repo_url` is omitted.

**Use it when**

- Onboarding a private repository for the first time.
- Replacing an expired token.

!!! note "Legacy form"
    A `repo_url` with inline credentials
    (`https://user:PAT@github.com/owner/repo.git`) and no `token` is still
    accepted, but the explicit `token` form is preferred.

```bash
oduflow call setup_repo_auth '{
  "repo_url": "https://github.com/owner/repo.git",
  "token": "ghp_..."
}'
```

### `get_ssh_public_key`

Return the team's SSH public key. Oduflow maintains one SSH deploy key per team,
generated automatically at server start. Register it with your git hosting — as
a repository deploy key (read access is enough) or on a machine-user account —
and SSH repository URLs (`git@github.com:owner/repo.git`) work in
[`create_environment`](#create_environment),
[`add_extra_repo`](#add_extra_repo) and productions without a token.

Lock: team credential store.
{ .odu-tool-meta }

**Parameters**

*None.*

**Use it when**

- You prefer deploy keys over tokens, or the host mandates them.
- Setting up access to a self-hosted git server.

!!! note "One repository per GitHub deploy key"
    GitHub allows a given deploy key on only one repository. To reach several
    repositories with the same key, attach it to a machine-user account
    instead.

## Extra Addons

Shared addon repositories — Odoo Enterprise, OCA, your own theme repo — cloned
once and mounted into environments. See
[Extra Addons Repositories](extra-addons.md).

### `add_extra_repo`

Clone an extra addons repository. It is cloned as a **shallow bare** repo (only
the latest commit of each branch, no history) into the shared repos directory,
so large repositories like Odoo Enterprise clone quickly. All branches are
kept, so one clone serves any Odoo version.

**Parameters**

`name`
:   *str · required* — Short name for the repo (e.g. `enterprise`, `custom-themes`).

`repo_url`
:   *str · required* — HTTPS (`https://github.com/owner/repo.git`) or SSH (`git@github.com:owner/repo.git`, needs the team deploy key — see [`get_ssh_public_key`](#get_ssh_public_key)).

**Use it when**

- Making Odoo Enterprise available to environments.
- Sharing an OCA collection or an internal theme repo across the team.

### `list_extra_repos`

List all cloned extra addons repositories.

**Parameters**

*None.*

**Use it when**

- Finding the exact name to use in an `extra_addons` argument.

### `update_extra_repo`

Fetch the latest changes from the remote, fetching all branches and pruning
deleted remote refs.

**Parameters**

`name`
:   *str · required* — The extra repo to update (e.g. `enterprise`).

**Use it when**

- A new Odoo version branch appeared upstream.
- An environment needs a fix that landed in the shared addons repo.

### `delete_extra_repo`

Delete a cloned extra addons repository.

**Parameters**

`name`
:   *str · required* — The extra repo to delete.

**Use it when**

- A shared repo is no longer used by any environment.

## Production Hosting

Long-lived Odoo instances with their own domain, on a dedicated production
PostgreSQL cluster. Every tool in this section and the two that follow requires
`[production].enabled = true`. Read [Production Hosting](production.md) for the
workflow and the disaster-recovery consequences.

### `create_production`

Provision a production Odoo environment: long-lived, its own domain, the
dedicated production PostgreSQL cluster, auto-tuned workers, and **no
sanitization**. Requires `routing_mode = "traefik"`.

Productions are rarely created and rarely deleted — they live on and get
updated with [`update_production`](#update_production).

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — Production name, e.g. `erp` (lowercase letters, digits, dashes).

`repo_url`
:   *str · default empty* — HTTPS git repository URL. Required unless `from_environment` supplies it.

`branch`
:   *str · default empty* — Git branch to deploy (full history is kept). Required unless `from_environment` supplies it.

`domain`
:   *str · default empty* — The public domain; DNS must point at this server, TLS via Let's Encrypt. In a team with `base_domain` configured it must be the zone apex or a subdomain of it (e.g. `erp.demo.example.com`). Empty defaults to the apex for the team's first production and `<name>.<base_domain>` afterwards. Client-owned domains go in `extra_domains`.

`extra_domains`
:   *list · default none* — Additional public FQDNs routed to the same production (e.g. the client's own `erp.customer.com`). Each gets its own Let's Encrypt certificate; DNS must point here.

`odoo_image`
:   *str · default empty* — Docker image, e.g. `odoo:18.0`. Required unless `from_environment` supplies it.

`git_user`
:   *str · default empty* — Git username for credential matching.

`extra_addons`
:   *dict · default none* — Extra addon repos as `{repo_name: branch}`.

`auto_update`
:   *bool · default `False`* — Deploy automatically on GitHub push webhooks.

`allow_copy_to_dev_mcp`
:   *bool · default `True`* — Allow MCP/agent-initiated copies of this production's data into dev ([`save_production_as_template`](#save_production_as_template) and the first `create_environment(from_production=...)`). When `False`, those tools refuse to publish a new copy; an already published template stays usable, but only with `sanitize=True`. The dashboard UI is never gated, and **no MCP tool can change this flag afterwards** — an administrator toggles it in the dashboard.

`template_name`
:   *str · default empty* — Template to seed the database and filestore from (e.g. an import of the customer's existing production). Empty starts a fresh database (`odoo -i base`). Mutually exclusive with `from_environment`.

`from_environment`
:   *str · default empty* — Dev environment to **promote**: its database and filestore are copied (Odoo briefly stopped for a consistent copy, then restarted — the environment is not reset), and empty `repo_url` / `branch` / `odoo_image` / `git_user` / `extra_addons` default to the environment's own. **No sanitization** — the data goes *into* production.

`env_vars`
:   *dict · default none* — User environment variables; values may be `secret:<name>` references. Omit to inherit the source environment's variables; pass `{}` to inherit none. Managed `HOST`/`PORT`/`USER`/`PASSWORD` cannot be overridden. References survive reconfiguration.

**Use it when**

- Going live with a customer after development settles.
- Migrating a customer's existing Odoo onto Oduflow (`template_name` from an import).
- Promoting a validated dev environment straight into production (`from_environment`).

### `list_productions`

List the team's productions with status, domain, deployed commit,
auto-update state and last deploy result.

**Parameters**

*None.*

**Use it when**

- A quick overview of what is live and whether anything is behind.

### `get_production_info`

Detailed information about one production: status, health, deployed commit,
recent branch commits, deploy history, current `odoo.conf` overrides, and
backup state.

**Parameters**

`name`
:   *str · required* — The production name.

**Use it when**

- Checking what is deployed versus what is on the branch.
- Confirming backup coverage and health before a risky change.
- Reviewing which `odoo.conf` overrides are in force.

### `production_logs`

Read a production's Odoo container logs.

**Parameters**

`name`
:   *str · required* — The production name.

`n_lines`
:   *int · default `100`* — Number of log lines to return.

`grep`
:   *str · default empty* — Case-insensitive substring filter.

`level`
:   *str · default empty* — Log level filter (e.g. `ERROR`, `WARNING`).

**Use it when**

- Investigating a customer-reported error at a known time.
- Watching for errors after a deploy.

### `start_production`

Start a stopped production.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

**Use it when**

- Bringing a production back online after maintenance.
- Restarting applications after a WAL disk-protection recovery — see [`control_production_wal`](#control_production_wal).

### `stop_production`

Stop a production.

Lock: production. **Takes the production offline.**
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

**Use it when**

- A planned maintenance window requires the application down.
- Something is actively causing damage and must be halted.

### `restart_production`

Restart a production's Odoo container — brief downtime.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

**Use it when**

- Odoo is wedged or leaking, and a clean process is the fastest remedy.
- A configuration change needs to be picked up.

### `reconfigure_production`

Change a production's infrastructure settings and recreate its container to
match. Omitted or empty arguments are left unchanged. The **database and
filestore are preserved**; expect brief downtime while the container is
replaced.

Changeable: the public domain (Traefik host rule + Let's Encrypt), the extra
domains, the Odoo image, the deployed branch or repository URL, the git
credential user, the extra addon repos, and the user environment variables.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`domain`
:   *str · default empty* — New public domain. DNS must point at this server. With `base_domain` configured it must be the zone apex or a subdomain of it; client-owned domains go in `extra_domains`.

`extra_domains`
:   *list · default none* — New **full set** of additional FQDNs. Pass `[]` to remove all; omit to leave unchanged.

`odoo_image`
:   *str · default empty* — New Docker image, e.g. `odoo:19.0`.

`branch`
:   *str · default empty* — New git branch to deploy.

`repo_url`
:   *str · default empty* — New git repository URL (HTTPS or SSH).

`git_user`
:   *str · default none* — New git username for credential matching. Pass `""` to clear it; omit to leave unchanged.

`extra_addons`
:   *dict · default none* — New **full set** of extra addon repos `{repo_name: branch}`. Pass `{}` to remove all; omit to leave unchanged.

`env_vars`
:   *dict · default none* — **Full replacement** user environment variables, including `secret:<name>` references. Omit to preserve; `{}` clears them.

**Use it when**

- A customer's domain changes, or they bring their own.
- Moving production onto a release branch.
- Adding an addons repository production now depends on.

!!! warning "Changing the image does not migrate the database"
    A major Odoo version bump additionally needs an explicit module upgrade
    plan. After a branch or repository change, run
    [`update_production`](#update_production) with `install=` / `upgrade=` if
    the new code needs module changes.

### `set_production_odoo_conf`

Set or remove `odoo.conf` `[options]` overrides for a production and re-apply
the configuration. Overrides are stored per production, **win over the
auto-tuned worker settings**, and survive deploys and retunes. Current
overrides are shown by [`get_production_info`](#get_production_info).

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`options`
:   *dict · default none* — Options to set, e.g. `{"limit_time_real": "300"}`.

`unset`
:   *str · default empty* — Comma-separated option names to remove, reverting them to the managed or base value.

`restart`
:   *bool · default `True`* — Restart the container so Odoo picks the change up (brief downtime).

**Use it when**

- A long-running report needs a higher `limit_time_real`.
- The auto-tuned worker count does not suit this workload.

!!! note "Managed keys are refused"
    `addons_path`, `data_dir` and the `db_*` keys are managed by Oduflow and
    cannot be overridden.

### `delete_production`

Delete a production. The container and registry record are removed; the
**database and workspace** (filestore, repository, deploy history) are **kept**
unless `drop_database=true`.

Kept leftovers are tombstoned: the reaper purges them `[lifecycle]
prod_purge_hours` after deletion (`0` = keep forever, the default), and
`oduflow cleanup --purge-deleted-productions --force` purges them immediately.
Re-creating a production with the same name revives the leftovers'
tombstone-free state — the kept database itself must still be dealt with
explicitly.

Lock: production. Destructive.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`confirm`
:   *str · required in practice* — Must equal the production name (safety check).

`drop_database`
:   *bool · default `False`* — Also drop the database and delete the workspace.

**Use it when**

- A customer engagement ended and the hosting is being wound down.
- A production was created by mistake and nothing depends on it.

## Production Deployment

### `update_production`

Deploy the latest commits of a production's branch — **with automatic code
rollback on failure**.

It pulls the branch (and extra-addon worktrees), decides or applies the Odoo
action, then verifies the deploy (module exit codes plus a health check). If
the deploy fails, the checkout is reset to the previous commit, the config is
re-applied and the container restarted.

Drive it like [`pull_and_apply`](#pull_and_apply): **explicit** (pass
`install` / `upgrade` / `restart=True`) or **auto** (all empty — changed files
are classified automatically). Note that in production a "refresh"-class change
(XML/JS) still restarts the container, because there is no `--dev=xml` in
production.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`install`
:   *str · default empty* — Comma-separated modules to install (`-i`).

`upgrade`
:   *str · default empty* — Comma-separated modules to upgrade (`-u`).

`restart`
:   *bool · default `False`* — Restart the container (for Python-only changes).

**Use it when**

- Shipping a release to a customer.
- Applying a hotfix you have already validated in a dev environment.

!!! danger "The database is never rolled back automatically"
    Code rollback is automatic; data is not. If module upgrades left the
    database inconsistent, restore a snapshot manually — take
    [`snapshot_production`](#snapshot_production) **before** a risky deploy.

### `rollback_production`

Manually roll a production's **code** back to a previous commit and restart.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`to_commit`
:   *str · default empty* — Target commit sha, or any git ref present in the checkout. Empty rolls back to the previous deploy's starting commit.

**Use it when**

- A deploy succeeded technically but the change is wrong in production.
- Reverting to a known-good commit while you investigate.

!!! warning "Code only"
    The database is not touched. For a data rollback, restore a snapshot with
    [`restore_production`](#restore_production).

### `set_production_auto_update`

Enable or disable automatic deployment from GitHub push webhooks. When enabled,
a push to the production's branch triggers
[`update_production`](#update_production) in the background, with automatic
code rollback on failure.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`enabled`
:   *bool · required* — `True` to deploy automatically on push.

**Use it when**

- A mature project should ship continuously from a protected branch.
- Freezing deploys during a change window (`enabled=false`).

### `production_deploys`

Deploy history, newest last: commits, actions, modules, status
(`success` / `rolled_back` / `rollback_failed`) and errors.

**Parameters**

`name`
:   *str · required* — The production name.

`limit`
:   *int · default `20`* — Maximum number of records.

**Use it when**

- Correlating "it broke on Tuesday" with what was deployed.
- Finding the commit to pass to [`rollback_production`](#rollback_production).
- Auditing whether an auto-update deploy silently rolled back.

## Production Backup & Recovery

Snapshots are the per-production restore unit; WAL-G covers the whole cluster.
Both require a `[backup]` section in `oduflow.toml`.

### `snapshot_production`

Take a snapshot of a production to S3: a database dump, a deduplicated
filestore revision, and a manifest recording the deployed commit sha.

Lock: production + backup store.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`note`
:   *str · default empty* — Free-form note stored in the manifest.

**Use it when**

- **Before** any risky deploy, module upgrade or data migration.
- Capturing a known-good state you may want to return to.

```bash
oduflow call snapshot_production '{"name": "erp", "note": "before v2 migration"}'
```

### `list_production_snapshots`

List a production's snapshots, oldest first: id, `created_at`, sizes and commit
sha.

**Parameters**

`name`
:   *str · required* — The production name.

`refresh`
:   *bool · default `False`* — Re-list S3 (the source of truth) instead of using the local cache.

**Use it when**

- Picking a `snapshot_id` for [`restore_production`](#restore_production).
- Verifying that scheduled snapshots are actually landing (`refresh=True`).

### `restore_production`

Restore a production's **database and filestore** from a snapshot, or replace
them with a dev environment's data (promotion into an *existing* production).

The restore is swap-based, so a failed restore leaves the previous state in
place. The **code checkout is not touched** — a warning is returned if it does
not match the source's commit.

Lock: production + backup store. Destructive for current data.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`snapshot_id`
:   *str · default empty* — Snapshot to restore (see [`list_production_snapshots`](#list_production_snapshots)). Mutually exclusive with `from_environment`.

`from_environment`
:   *str · default empty* — Dev environment whose database and filestore replace this production's. The environment's Odoo is briefly stopped for a consistent copy, then restarted — the environment itself is not reset. **No sanitization** — the data goes *into* production.

`confirm`
:   *str · required in practice* — Must equal the production name (safety check).

**Use it when**

- A bad migration or data loss needs the last good snapshot back.
- Promoting a rebuilt dataset from dev into an existing production.

!!! danger "Take a snapshot first"
    This replaces the production's current data. If that data may still be
    needed, call [`snapshot_production`](#snapshot_production) before
    restoring.

### `set_production_backup_schedule`

Override a production's daily snapshot time.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — The production name.

`schedule`
:   *str · required* — `"HH:MM"` in server-local time, or `"off"` to disable scheduled snapshots for this production. Unset (the default) follows `[backup] snapshot_time`.

**Use it when**

- A customer's quiet hours differ from the team default.
- Staggering snapshots so several productions do not back up at once.

### `prune_production_backups`

Apply the retention policy (`[backup] keep`) to the team's snapshots and
filestore chunk store immediately. This also runs weekly on schedule.

Pruning uses safe two-step fossil collection: chunks are only permanently
deleted on a *later* prune, after every production has produced a newer
revision.

Lock: backup store.
{ .odu-tool-meta }

**Parameters**

*None.*

**Use it when**

- S3 costs need reining in before the weekly run.
- You just lowered `keep` and want it applied now.

### `production_backup_status`

Backup posture for the team: per-production snapshot state (schedule, last
snapshot, last error) and cluster WAL-G state (base backups, WAL archiver
health, S3 reachability).

**Parameters**

*None.*

**Use it when**

- The weekly "are we actually backed up?" check.
- Diagnosing why a scheduled snapshot did not appear.

### `production_wal_status`

Cached shared-cluster WAL queue, upload progress, disk reserve, stale-data and
protection state.

**Parameters**

*None.*

**Use it when**

- WAL archiving is falling behind and the PostgreSQL volume is filling up.
- Confirming the cluster has left disk-protection mode.

### `control_production_wal`

Control the shared production cluster's WAL handling. **Affects all teams on
this server.** Unarchived WAL is never discarded.

Lock: none — but cluster-wide in effect.
{ .odu-tool-meta }

**Parameters**

`action`
:   *str · required* — One of `pause` (retain WAL), `resume`, `retry` (interrupt only `wal-push`), `recover` (start PostgreSQL only, under disk protection), `release` (clear protection after checks; applications remain stopped).

`confirm`
:   *str · required in practice* — Must equal `ALL-PRODUCTIONS`.

**Use it when**

- S3 is unreachable and WAL uploads must be paused deliberately.
- Recovering a cluster that hit the disk reserve and stopped.

!!! warning "Recovery is deliberately partial"
    `recover` starts PostgreSQL only; `release` clears protection but leaves
    applications stopped. Bring each production back with
    [`start_production`](#start_production) once you are satisfied the cluster
    is healthy.

### `restore_cluster_pitr`

**Disaster recovery.** Restore the whole production PostgreSQL cluster from
WAL-G — base backup plus WAL replay. This affects **every production database
at once**; to restore a single production use
[`restore_production`](#restore_production).

This is also the "resurrect production elsewhere" path: a fresh Oduflow server
with the same `[backup]` section can rebuild the cluster from S3.

The current data directory is *displaced* inside the volume, not destroyed.
Production Odoo containers are stopped and restarted.

Destructive and cluster-wide.
{ .odu-tool-meta }

**Parameters**

`target_time`
:   *str · default empty* — PITR target, e.g. `"2026-07-10 12:00:00+00"`. Empty replays the whole archive to the latest state.

`confirm`
:   *str · required in practice* — Must equal `RESTORE-CLUSTER`.

**Use it when**

- The cluster is lost or corrupted at the storage layer.
- Rebuilding production on new hardware from S3 alone.
- Rewinding every database to a point in time before a catastrophic change.

## Production Odoo API

Read and change production Odoo records through OduMCP, the connector addon
Oduflow installs into the production itself. Reads are policy-bounded and
changes go through an approval plan that a human approves in Odoo, so no
arbitrary ORM call ever reaches a live database. Every tool here requires
`[production].enabled = true` and — over HTTP — the `/production` endpoint with
the team's `production_token`; see
[Production access and rotation](production.md#separate-production-mcp-access).

### `sync_production_mcp`

Install or upgrade the `odumcp` addon on a production and synchronize the
team's configured key with Odoo. An empty `name` processes every production of
the team and reports each one separately, so a single failure does not hide the
rest.

New productions are provisioned automatically; this tool is for existing ones,
for retrying a failed setup, and for rotating the key after `production_token`
changes in `oduflow.toml` (change the value, restart Oduflow, then run this).
No secret is accepted or returned. Stopped productions must be started first,
and adding the managed addon mount recreates the container.

Lock: production (each one in turn).
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · default empty* — One production, or empty for every production of the team.

**Use it when**

- Adopting a production that predates automatic provisioning.
- The connector reports itself unavailable after a failed setup.
- Rotating `production_token`.

### `production_odoo_info`

Odoo identity, profile and the capabilities the deployed policy actually
enables — the cheapest way to confirm the connector is reachable and what it
will allow.

**Parameters**

`name`
:   *str · required* — Production name.

**Use it when**

- Verifying a fresh install or a rotated key.
- Discovering which operations the production's policy permits before planning work.

### `production_odoo_read`

Policy-governed reads: `models.list`, `models.describe`, `records.search`,
`records.read`, `records.count`, `records.aggregate`, `attachments.read` and
`reports.render`. Parameters follow the OduMCP API (`model`, `domain`,
`fields`, `ids`, `limit`, …). There is no arbitrary ORM call.

Start from `models.describe`: it reports the fields this production is willing
to expose, which is not the full Odoo schema.

**Parameters**

`name`
:   *str · required* — Production name.

`operation`
:   *str · required* — One of the read operations listed above.

`params`
:   *dict · default none* — Operation parameters as defined by the OduMCP API.

**Use it when**

- Answering a question about live data without copying the database into dev.
- Rendering a production report for a customer.

### `production_odoo_preview_change`

Store a change plan in Odoo and return its `approval_id`. **This does not
execute the business change.** Actions include `record.create` / `update` /
`delete`, `method.call`, `message.post`, `activity.schedule` / `update` /
`done` and `attachment.create`.

Approval is a human act in Odoo, unless that production's own policy explicitly
permits auto-approval. Retrying the same intent means reusing the same
`idempotency_key`, never composing a second plan.

**Parameters**

`name`
:   *str · required* — Production name.

`action`
:   *str · required* — The planned action, e.g. `record.update`.

`payload`
:   *dict · required* — Action payload as defined by the OduMCP API.

`idempotency_key`
:   *str · required* — Stable key for this intent; reuse it when retrying.

`batch_key`
:   *str · default empty* — Groups plans that must be approved and executed together.

**Use it when**

- A production record genuinely has to change and the change needs an audit trail.

### `production_odoo_change_status`

Read a plan's approval state and, once executed, its stored result.

This is also the correct response to an uncertain execute: the change tools
never retry writes on their own, so the status is what tells you whether the
earlier call landed.

**Parameters**

`name`
:   *str · required* — Production name.

`approval_id`
:   *str · required* — The ID returned by `production_odoo_preview_change`.

**Use it when**

- Waiting on a human approval.
- A previous execute failed in transport and the outcome is unknown.

### `production_odoo_execute_change`

Execute the stored, approved plan by ID. It never grants approval and never
substitutes a payload — the plan that runs is the plan that was approved.

Check the returned state: `pending`, `expired`, `rejected` and `failed` all
mean the change did **not** happen. After a transport failure, check the status
before creating any new plan.

Lock: production.
{ .odu-tool-meta }

**Parameters**

`name`
:   *str · required* — Production name.

`approval_id`
:   *str · required* — The approved plan to execute.

**Use it when**

- A human has approved the plan in Odoo and the change should now be applied.

## Agent Guidance & Feedback

### `get_agent_instructions`

Load the compact Oduflow agent workflow and the active code-delivery mode.

**Parameters**

*None.*

**Use it when**

- Once at the start of an agent session, before other Oduflow tools. Not before every call — the guide holds for the session.

### `get_odoo_development_guide`

Get the Odoo development standards and constraints guide for a specific Odoo
version (15–19).

**Parameters**

`version`
:   *str · required* — Odoo version number. Both `"19"` and `"19.0"` are accepted.

**Use it when**

- Before writing or refactoring Odoo module code. Determine the version from the request, from [`get_environment_info`](#get_environment_info), or from the `odoo_image` value — `odoo:18.0` means `version="18"`.
- Immediately after [`create_environment`](#create_environment) tells you to.

### `report_issue`

Build a prefilled link the user can follow to file a bug, feature request or
feedback about **Oduflow itself** on GitHub.

The tool does **not** create the issue: it returns a prefilled link to the
`oduist/oduflow` issue form. Show the link to the user and let them submit it
from their own GitHub account, so the report is attributable to them and they
can edit it first. Oduflow version, Python version, platform, transport and
routing mode are attached automatically.

**Parameters**

`details`
:   *str · required* — The report body: what happened, what was expected, or the feedback.

`kind`
:   *str · default `"feedback"`* — One of `bug`, `feature`, `feedback`. Selects the issue form and its labels.

`title`
:   *str · default empty* — One-line summary used as the issue title.

**Use it when**

- The user hits a bug in Oduflow, wants a feature, or wants to send feedback — **not** for problems in their own Odoo code.

!!! warning "Never include identifying or customer data"
    No hostnames, repository URLs, branch or database names, credentials, or
    customer data in the text.

---

The exact current signature and defaults for every tool are also available from
`oduflow list` (`oduflow list --verbose` adds descriptions). The production
workflow and disaster-recovery consequences are covered in
[Production Hosting](production.md); the CLI equivalents are in the
[CLI Reference](cli.md).
