# MCP Tools Reference

![Agent Instructions](img/agent_instructions.png)

All tools are accessible via MCP clients (Cursor, Cline, Amp, etc.), locally via
`oduflow call`, and on a remote Oduflow server via `oduflow client`. A subset is
also available via the [REST API](web-api.md).

| Tool | Lock | Description |
|---|:---:|---|
| **Environment Management** | | |
| `create_environment` | ✓ | Provision an Odoo environment for a branch (clone, DB, container, filestore); an environment that already exists is returned as is (started if stopped) instead of raising, unless it tracks another branch; optional `hostname` selects a short Traefik hostname and `env_vars` injects container environment variables, merged per key over any recorded on the template; `from_production` builds the environment from a production's real data through the managed `prod-<name>` template |
| `delete_environment` | ✓ | Tear down all resources for a branch |
| `list_environments` | | List all managed environments with status, URL, current git branch, creation/last-activity/stopped timestamps, stop source, protection, Stack ownership and operator note |
| `get_environment_info` | | Full environment details: lifecycle/reuse metadata, DB name, URL, repo, image, template, extra addons, workspace, container status, CPU/RAM stats |
| `start_environment` | | Start a stopped environment |
| `stop_environment` | | Stop a running environment |
| `restart_environment` | | Restart the Odoo container |
| `update_environment` | ✓ | Re-create the container, preserving DB and filestore; optional `odoo_image` switches the image, `hostname` changes the public Traefik hostname, `env_vars` replaces the container environment variables and `new_name` renames the environment |
| `switch_branch` | ✓ | Point an existing environment at another branch and apply the difference, keeping its database, filestore, hostname and URL — the way to work when the team is out of environment slots; optional `new_name` renames the environment in the same operation (its scoped MCP endpoint moves with the name) |
| **Odoo Operations** | | |
| `pull_and_apply` | ✓ | Git pull + smart analysis → auto install/upgrade/restart; `summary_only=True` returns one action/status line and caches command logs for `read_output` |
| `install_odoo_modules` | ✓ | Install Odoo modules (`-i`) |
| `upgrade_odoo_modules` | ✓ | Upgrade Odoo modules (`-u`) |
| `export_module_translations` | ✓ | Export a module's `.pot`/`.po` with Odoo's own exporter, write it into the module's `i18n/`, and return a summary plus an HTTP download URL or local temporary path |
| `translation_status` | ✓ | Verdict per language (OK, PARTIAL, NOT LOADED, NOT TRANSLATED, IMPORT SILENTLY DROPPED, IMPORT ABORTS, NO FILE, NOT ACTIVATED) from the module's terms, the database and the committed `.po` files, including the sibling-POT metadata merge Odoo performs before import, plus the call that fixes it |
| `run_odoo_tests` | ✓ | Run Odoo tests for specific modules; `summary_only=True` returns the final `N failed, M error(s) of K tests` line and an `output_id`; `test_tags` narrows the run, while `upgrade=False` skips `-u` for a fast `post_install` re-run |
| `get_environment_logs` | | Retrieve recent container logs |
| `run_odoo_command` | ✓ | Execute an arbitrary shell command inside the Odoo container (runs through `sh -c`, so pipes, redirections and `&&` work; `shell=False` for exact argv) |
| `run_odoo_shell` | ✓ | Execute Python code in the Odoo shell context with full ORM access; `auto_commit=True` commits successful writes, while `False` leaves the shell transaction uncommitted |
| `odoo_search_read` | | Search and read records (XML-RPC `search_read`/`search_count`) as any user, with ACLs and record rules applied |
| `odoo_create` | | Create one or many records (XML-RPC `create`). Committed immediately |
| `odoo_write` | | Update records (XML-RPC `write`). Committed immediately |
| `odoo_unlink` | | ⚠️ Delete records (XML-RPC `unlink`). Committed immediately, not recoverable |
| `odoo_call` | | Call public model methods not covered by the dedicated CRUD tools (`read_group`, `name_search`, `action_*`, …) |
| `odoo_schema` | | Page through models, or describe one model's fields (XML-RPC `fields_get`) |
| `read_file_in_odoo` | | Read a text file or list a directory inside the Odoo container. Supports line ranges (e.g. `"1:50"`) |
| `write_file_in_odoo` | ✓ | Write a text file inside the container (CSV imports, scripts, configs) |
| `search_in_odoo` | | Search for a pattern (fixed-string grep) in files inside the Odoo container |
| `http_request_to_odoo` | | Make an HTTP request to the running Odoo instance (test controllers, JSON-RPC, REST) |
| `list_installed_modules` | | List Odoo modules and their states with name/state filtering |
| `run_db_query` | ✓ | Execute SQL against the environment's PostgreSQL database; supports `output_format="csv"` or `"json"` and caps returned rows with `max_rows` (default `100`) |
| `reset_admin_password` | ✓ | Reset the admin user password in the Odoo database (default: "test") |
| `connect_as_user` | ✓ | Mint a passwordless Odoo login session for a user (by login or id) and return the `session_id` cookie + URL — hand to Playwright to skip the login form and test as any role (incl. portal) |
| `read_output` | | Read from a cached tool output by ID (paginate, grep, errors, tail) |
| **Template Management** | | |
| `save_as_template` | ✓ | ⚠️ Save a branch DB + filestore as a new template |
| `save_production_as_template` | ✓ | ⚠️ Save a production's DB + filestore as a template (unsanitized production data; refused when the production has MCP copy-to-dev disabled) |
| `list_templates` | | List available template profiles, including the branch/commit each database snapshot was taken from |
| `delete_template` | ✓ | ⚠️ Delete a template profile (DB + files) |
| `rename_template` | ✓ | Rename a template (directory + PostgreSQL template DB); refused if any environment uses it |
| `import_template_from_odoo` | ✓ | Import a template from a running Odoo instance via database manager API; optional `without_filestore` requests a database-only PostgreSQL custom dump |
| `refresh_template` | ✓ | ⚠️ Re-apply a template's filestore to live overlay environments (preserves env changes by default; `reset_env_changes=True` discards them — destructive) |
| `attach_filestore` | ✓ | Attach or replace a template filestore from a local directory, archive, `rsync://` URL, or SSH rsync source; normalizes wrapper paths and preserves live env changes by default |
| **Auxiliary Services** | | |
| `create_service` | ✓ | Create a managed service with exactly one exposure model: catch-all `port`, or restricted Traefik `routes` (`path`, backend `port`, optional `strip_prefix`). The two parameters are mutually exclusive; `port` remains required outside Traefik. Optional `command` (shell-quoted string) replaces the image `CMD` |
| `delete_service` | ✓ | Stop and remove a service container |
| `restart_service` | ✓ | Restart a service container |
| `update_service` | ✓ | Preflight configuration, pull the latest image and/or change settings. `routes` replaces the complete allowlist; use `routes=[]` with `port` only when switching back to catch-all mode. `command` is tri-state: omitted keeps it, a string replaces it, an empty string falls back to the image `CMD` |
| `list_services` | | List all managed service containers |
| `get_service_info` | | Full live state of a single service (image+digest, port/routes, hostname, host_mode, command, volumes, env, capabilities, restart count, preset). Call before recreating it |
| `get_service_logs` | | Retrieve service container logs |
| `run_service_command` | ✓ | Execute a shell command inside a service container (through `sh -c`; `shell=False` for exact argv) |
| **Service PostgreSQL Databases** | | |
| `create_service_database` | ✓ | Create a persistent team-scoped PostgreSQL database with a dedicated non-superuser owner; returns `DATABASE_URL` and `PG*` credentials |
| `list_service_databases` | | List database names, live status, size, and connections without passwords |
| `get_service_database` | | Explicitly reveal connection credentials for one managed database |
| `rotate_service_database_password` | ✓ | Rotate the owner password and return replacement credentials; containers must be reconfigured |
| `delete_service_database` | ✓ | ⚠️ Permanently drop the database and role after terminating active connections; service containers are unchanged |
| **Volumes** | | |
| `create_volume` | ✓ | Create a named Docker volume for use with services |
| `list_volumes` | | List all managed Docker volumes and their usage by services |
| `inspect_volume` | | Get detailed information about a specific volume |
| `delete_volume` | ✓ | Delete a managed Docker volume (fails if in use) |
| `read_file_in_volume` | | Read a text file or list a directory inside a Docker volume |
| `write_file_in_volume` | ✓ | Write a text file inside a Docker volume |
| `search_in_volume` | | Search for a pattern (fixed-string grep) in files inside a Docker volume |
| `delete_file_in_volume` | ✓ | Delete a file or directory inside a Docker volume |
| **Service Presets** | | |
| `list_service_presets` | | List saved service presets (configurations that can be restored) |
| `restore_service` | ✓ | Restore a service from a saved preset |
| `delete_service_preset` | ✓ | Remove a saved service preset |
| **Secrets** | | |
| `list_secrets` | | List the names of the team's named secrets; values are write-only (set in the dashboard) and are never readable over MCP. Reference one in any `env_vars` argument as `KEY=secret:<name>` |
| **Container Image Builds** | | Requires a `[team.X.image_registry]` section |
| `start_image_build` | ✓ | Build a container image from the environment checkout's current commit (asynchronous; returns a `build_id`); the source is sealed with `git archive` under the environment lock, with hard timeout, log-size, and per-team concurrency limits |
| `get_image_build` | | Status, source commit, publication history, and a build log tail for one build |
| `publish_image_build` | ✓ | Push a succeeded build's exact image (never a rebuild) to comma-separated tags — `latest`, semver, anything valid — under the team's configured registry namespace; per-tag outcomes, last-writer-wins |
| `cancel_image_build` | | Terminate a running build and its Docker connection, including a silent Dockerfile step |
| **Repository Auth** | | |
| `setup_repo_auth` | ✓ | Cache git credentials for a private repository |
| **Extra Addons** | | |
| `add_extra_repo` | | Clone an extra addons repository (e.g. Odoo Enterprise) for use with environments |
| `list_extra_repos` | | List all cloned extra addons repositories |
| `update_extra_repo` | | Fetch latest changes from the remote for an extra addons repository |
| `delete_extra_repo` | | Delete a cloned extra addons repository |
| **Production Hosting** | | Requires `[production].enabled = true` |
| `create_production` | ✓ | Provision a long-lived production with its own domain and the dedicated production PostgreSQL cluster; optionally seed it from a template or promote a dev environment (`from_environment` copies its DB + filestore and inherits repo/branch/image); `allow_copy_to_dev_mcp` (default true) controls whether agents may publish new copies of it into dev |
| `list_productions` | | List productions with status, domain, deployed commit, and auto-update state |
| `get_production_info` | | Detailed status, configuration, deployed commit, deploy history, and backup information |
| `start_production` | ✓ | Start a stopped production |
| `stop_production` | ✓ | Stop a production, taking it offline |
| `restart_production` | ✓ | Restart a production's Odoo container |
| `set_production_auto_update` | ✓ | Enable or disable GitHub push webhook deployments |
| `reconfigure_production` | ✓ | Change domain, Odoo image, branch, repository, git user, or extra addon repos; recreates the container (database and filestore preserved, brief downtime) |
| `set_production_odoo_conf` | ✓ | Set or remove per-production `odoo.conf` overrides that win over the auto-tuned worker settings and survive deploys; managed keys (`addons_path`, `data_dir`, `db_*`) are refused |
| `update_production` | ✓ | Deploy pulled commits with explicit/automatic actions, health verification, and automatic code rollback on failure |
| `rollback_production` | ✓ | Roll production code back to a selected commit and restart it; does not roll back the database |
| `production_deploys` | | Read deploy history, including actions, modules, trigger, and rollback status |
| `production_logs` | | Read production Odoo logs with line, substring, and level filtering |
| `snapshot_production` | ✓ | Create an S3 snapshot containing the database, deduplicated filestore, and deployed commit |
| `list_production_snapshots` | | List S3 snapshots; `refresh=True` bypasses the cached index |
| `restore_production` | ✓ | Restore one production's database and filestore; requires its name in `confirm` |
| `production_backup_status` | | Inspect snapshot schedules, WAL archiving, base backups, and S3 reachability |
| `set_production_backup_schedule` | ✓ | Set a production's daily snapshot time (`HH:MM`) or disable it with `off` |
| `prune_production_backups` | ✓ | Apply configured snapshot and chunk-store retention immediately |
| `restore_cluster_pitr` | ✓ | ⚠️ Restore the entire production PostgreSQL cluster from WAL-G; requires `confirm="RESTORE-CLUSTER"` |
| `delete_production` | ✓ | Remove a production; requires its name in `confirm`, and preserves its database unless `drop_database=True` |
| **Agent Instructions** | | |
| `get_agent_instructions` | | Load the compact Oduflow agent workflow once at the start of a session |
| `get_odoo_development_guide` | | Get Odoo development standards guide for a specific version (15–19) |
| **Feedback** | | |
| `report_issue` | | Build a prefilled link for the user to file a bug, feature request, or feedback about Oduflow on GitHub |

!!! info "Locking"
    Tools marked with ✓ acquire a lock on exactly what they touch: the branch, one service, one volume, the team's credential store — or the whole team, for the few operations that really are team-wide (template publishing). Operations on different resources run in parallel. If another operation on the **same** resource is already in progress, the call is rejected with `BusyError`. The rejection names the operation holding the lock and how long it has held it (e.g. *"Another operation on environment 'main' (pull_and_apply, running for 4m12s) is in progress"*), so a long install is distinguishable from a hung one. A lock is released when its operation finishes — including when the client that started it timed out and stopped waiting, which is why restarting the environment is the wrong response.

The exact current signature and defaults for every tool are also available from
`oduflow list` (`oduflow list --verbose` adds descriptions). The production
workflow and disaster-recovery consequences are covered in
[Production Hosting](production.md).
