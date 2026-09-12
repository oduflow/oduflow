# Oduflow — Agentic Odoo Development
Version: 9

## Purpose

Oduflow provisions an isolated Odoo environment for each branch and gives the
agent a closed feedback loop: edit code → apply it → read the result → fix →
test. Load this guide once at the start of the session and use it as the
workflow contract for the rest of that session.

Before writing or refactoring Odoo module code, call
`get_odoo_development_guide(version="<major>")` once for the target Odoo
version (15–19).

## Choose the code-delivery mode

- **`repo_url` mode:** Oduflow owns a managed clone. Edit in your local
  checkout, commit and push, then call `pull_and_apply`.
- **`local_path` mode:** Oduflow bind-mounts a local checkout. Edit that folder
  directly; no push is required. Git commits are optional and do not control
  what Oduflow applies.

Call `create_environment` first, without looking anything up: if an environment
with that name already exists, the call returns its URL immediately, starts it
when it was stopped, and creates nothing. Prepare the selected delivery mode
before that call:

- **`repo_url`: first run `git push -u origin HEAD`** so the current branch
  exists in the remote repository, then create the environment. Oduflow can
  clone only pushed commits; it cannot see a local-only branch or uncommitted
  changes.
- **`local_path`:** create the environment from the absolute checkout path;
  no push is required.

Call `create_environment` with the branch, correct Odoo image, and either
`repo_url` or `local_path`. Show the returned URL to the user.

## Core workflow

```text
1. create_environment for your branch (repo_url must be pushed first;
   local_path needs no push). An environment that already exists is
   returned as is, so this is safe to call first
2. if the team is out of environment slots, take a finished (merged)
   environment instead: git push -u origin HEAD, then switch_branch
3. get_odoo_development_guide before module code changes
4. edit code locally
5. repo_url: commit + push; local_path: no delivery step
6. pull_and_apply with the action required by the edit and summary_only=true
7. read that response; if it failed, fix and repeat from step 4
8. run_odoo_tests with summary_only=true for the changed modules
9. leave the environment for the next branch, or delete_environment when
   nobody still needs the URL or its test data
```

Create a fresh environment while the team still has free slots — that is the
normal path, and there is no need to hunt for an environment to take over.
`create_environment` fails with "No free environment slots" when they run out;
that is when step 2 applies. `switch_branch` keeps the database, filestore, URL
and scoped MCP endpoint and only changes the code, and it applies the branch
difference exactly like `pull_and_apply`. The target branch must exist on
origin; live-mounted environments are rejected (switch the branch in the
mounted checkout and call `pull_and_apply`). The switch does not inspect
installed module state in the retained database; handle any real incompatibility
from the apply response or subsequent Odoo runtime errors.

`create_environment` refuses an existing environment only when it tracks a
different branch: that environment's database and URL are in use, so moving it
is the user's call. Report the conflict and let them choose `switch_branch`, a
different `env_name`, or deleting the old environment.

When you do have to take over a slot, treat the reuse metadata in
`list_environments` as evidence, not decoration.
Never switch a running, protected, Stack-managed, or explicitly reserved
(`Note:`) environment. When the repository's own instructions allow reclaiming
idle slots, choose among eligible stopped environments by `Last Activity` and
`Stopped At`, oldest first; `unknown` is not evidence that a slot is abandoned.
Confirm that its previous work is finished from an affirmative signal such as a
merged PR, a branch merged into the target branch, or the user's instruction.
An empty `gh pr list` result proves neither completion nor continued use. If the
safe choice cannot be established, create a new environment.

The environment name is a slot label, not a description of the branch, so a
mismatch is normal — no need to rename on every switch. When the user asks for
a different name, pass `new_name` to `switch_branch`: the URL and database
follow the rename, but the scoped MCP endpoint becomes `/mcp/<new name>`, so use
the new name in every later call.

Do not delete and recreate an environment to fix an application error or run
a migration. It recreates the same starting state and discards useful test
data. Environments auto-stop when idle and container-level tools wake them
automatically; a wake-up `Note:` is informational. Use `protect_environment`
only when an environment must survive the configured idle lifecycle.

## Choose the apply action

When you authored the change, tell `pull_and_apply` what it requires. Leave
all action arguments empty only when pulling commits you did not author and
want Oduflow to classify automatically.

| Change | Action |
|---|---|
| New, not-yet-installed module | `install="module"` |
| Fields/models, `_inherit`/`_name`, security or data records, cron, mail templates, translations, manifest `data`/`depends`, or migrations | `upgrade="module"` |
| Python method/logic only, with no schema or data change | `restart=True` |
| View/QWeb XML or browser assets only | Usually no server action; refresh the browser |

A field change needs an upgrade, not merely a restart. In explicit mode,
`pull_and_apply` may return a guardrail warning when the requested action looks
incomplete; correct the action and call it again. `strict=True` turns that
warning into a refusal.

## Read the right output

`pull_and_apply`, module install/upgrade, and `run_odoo_tests` return their own
command output, errors, and tracebacks. Read that response. Do not call
`get_environment_logs` looking for the same error: it only shows the running
Odoo server process and is appropriate for failures during HTTP requests or
after a restart.

Pass `summary_only=true` to `pull_and_apply` and `run_odoo_tests` during the
normal development loop. Test runs then return the final
`N failed, M error(s) of K tests` result, while applies return one compact line
with the action, changed-file count and exit status. The complete command log
stays server-side and the response includes an `output_id`; use `read_output`
with `errors`, `grep`, `lines`, or `tail` only when that compact result reports
a failure or does not answer the question. Without `summary_only`, command
responses keep their verbose backward-compatible format and large ones are
cached automatically.

Use the most specific inspection surface:

- Odoo records with real ACLs: `odoo_schema` and the `odoo_*` tools.
- Multi-step ORM logic, private methods, `sudo()`, or rollback-only inspection:
  `run_odoo_shell` (`auto_commit=false` for rollback).
- SQL and schema checks: `run_db_query`.
- Runtime process logs: `get_environment_logs`.
- Container files/source: `read_file_in_odoo` or `search_in_odoo`.

Each `odoo_*` call commits independently and talks to the already-running
server. After a Python edit it sees old code until the environment restarts;
`run_odoo_shell` starts a fresh registry. Confirm ids before `odoo_unlink` and
prefer archiving when the model supports it.

## Code and container boundary

The Odoo container is a runtime, not the agent's source workspace.

- Never edit repository source inside the container and never run Git there.
- In `repo_url` mode, edit locally → commit → push → `pull_and_apply`.
- In `local_path` mode, edit the mounted local folder → `pull_and_apply`.
- Reading Odoo/core files and running diagnostic commands inside the container
  is fine.
- Use `run_odoo_command` only when a dedicated Oduflow tool does not express
  the operation. It runs as `odoo` by default; privileged package or system
  changes require an explicit reason and user approval.

Mutexed operations on the same environment return `BusyError` while earlier
server-side work is still running, even if its client timed out. Wait for that
operation; do not restart or recreate the environment to clear the lock.

## Per-environment Odoo configuration

Use `/etc/odoo/odoo.conf` for a temporary setting that should affect only the
current environment:

1. Read the complete file with
   `read_file_in_odoo(env_name, "/etc/odoo/odoo.conf")`.
2. Change only the required option and preserve every other line.
3. Write the complete file back with
   `write_file_in_odoo(env_name, "/etc/odoo/odoo.conf", content, user="odoo")`.
4. Call `restart_environment(env_name)`; Odoo reads these settings at startup.
5. Re-read the file and verify the behavior or runtime logs.

Common scoped changes:

| Goal | Change | Important limit |
|---|---|---|
| Recover from `psycopg2.pool.PoolError: The Connection Pool Is Full` | `db_maxconn = 8` → `16` | Raise once; if it recurs, investigate leaked connections. Do not raise it for PostgreSQL's `FATAL: sorry, too many clients already`—that is a shared-cluster limit and a higher value makes it worse. |
| Exercise scheduled jobs | `max_cron_threads = 0` → `1` | Cron is disabled by default in disposable dev environments; enable it only when the task needs cron execution. |
| Reproduce worker-mode behavior | `workers = 0` → a small value such as `2` | Workers consume additional RAM and database connections. This is a focused test setting, not production sizing; keep `db_maxconn` and the shared PostgreSQL limit in mind. |

Explain the scoped runtime change before making it unless the user already
requested that condition explicitly. The existing `db_maxconn` 8 → 16 repair
for the exact Odoo pool-exhaustion error is a sanctioned diagnostic fix.

A direct `/etc/odoo/odoo.conf` edit survives `restart_environment` and normal
code sync, but is lost when `update_environment` recreates the container or a
source config is reapplied. For a durable project setting, edit
`.oduflow/odoo.conf` in the repository and deliver it through the normal
workflow; call `pull_and_apply(..., restart=True)` so Oduflow regenerates the
merged config. A team-wide default belongs in the team's server-side
`odoo.conf` and is an operator change, not a branch task.

## Templates, schema changes, and migrations

A template database is a snapshot of code at a particular commit.
`create_environment` reports whether the branch is aligned, ahead, behind, or
diverged from that snapshot.

- **Behind/diverged:** merge the template's source branch into the working
  branch before applying old code to newer database state.
- **Ahead:** apply the reported module installs/upgrades, dependencies first.
- **Aligned:** proceed normally.

Environment creation clones the database as-is; it does not run module
migrations. Deliver the migration and manifest version bump, then execute
`pull_and_apply(..., upgrade="module")` (or `upgrade_odoo_modules`) and verify
the expected schema/data with `run_db_query`. Read the upgrade response for
errors. Never recreate the environment as a substitute for the upgrade.

## Translations

Before creating a new `i18n/<lang>.po`, call
`export_module_translations(env_name, module, lang)` and edit the generated
catalogue. Never invent `#.` or `#:` metadata: Odoo derives the translation
target from it, and a malformed reference can import as zero translations
without a warning. Do not run that export over a catalogue you have already
written: it overwrites `i18n/<lang>.po` with what the database holds, so
translations that never imported are lost. When repairing an existing
hand-written catalogue, follow `translation_status`'s `Next:` action; it may
ask for a sibling `<module>.pot` whose generated metadata Odoo merges into the
`.po` during import.

After upgrading the module, call `translation_status`. It compares the module
catalogue, committed `.po`, and database, then returns a verdict and a `Next:`
action. Follow that action instead of deriving a diagnosis from raw counts.

Write English source strings; localized text belongs in `i18n/*.po`.

## Finish the task

Run the relevant Odoo tests after the last successful apply. Keep the
environment only for a concrete reason: ongoing work, user testing through its
URL, or irreplaceable test data. Otherwise delete it as the final workflow
step and report the verification result to the user.
