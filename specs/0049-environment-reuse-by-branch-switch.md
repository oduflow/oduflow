# 0049 — Environment reuse by branch switch

**Status:** Adopted
**Type:** Lifecycle / Capacity
**First introduced:** `litnimax/bangkok` branch (2026-08-17)
**Key code today:** `docker_ops/env_ops.py` (`switch_environment_branch`),
`git_ops.py` (`fetch_branch`, `checkout_branch`), `server.py`
(`switch_branch`), `web_ui.py`, `templates/dashboard.html`

## Context

[[0001-mcp-orchestrated-ephemeral-per-branch-environments]] made an environment
per branch, created and destroyed with the branch. In practice the unit of work
is not a branch but a *developer's slot*: the same person, agent or client works
one task at a time, and each task happens to have a new branch name.

The per-branch lifecycle charges the full provisioning cost at every task
boundary — a fresh clone and a template database copy — and it charges it again
in ceremony. The old environment has to be deleted by hand or waited out through
the idle reaper, and every downstream address changes with the name: the URL, the
scoped `/mcp/<env>` endpoint and its token, the agent's checkout directory. A
merged branch's environment is, by then, an almost perfect starting point for the
next branch: the same repository, the same image, and a database whose schema
already contains the merged work.

[[0048-reusable-environment-hostname-slots]] had already separated one identity
from the environment name — the public hostname became a pooled, reusable team
resource. The remaining coupling was the code: the branch was recorded once, at
creation, and never moved.

## Decision

Make the branch a mutable property of an existing environment. One operation,
`switch_branch`, points an environment at another git branch and brings its
running Odoo up to date with the difference. Everything that constitutes the
environment's identity is preserved: name, database, filestore, hostname and URL,
ports, PostgreSQL credentials, and the scoped MCP endpoint and token.

Deliberately **not** part of this decision: renaming an environment to match the
new branch. The name is the key from which the database name, workspace path,
container name, port and hostname registry keys, credential file and agent
checkout directory are all derived, and no transaction spans a rename of an
`ALTER DATABASE`, a fuse-overlayfs remount, a `docker rename` and three
registries — a half-renamed environment is a worse failure than a name that no
longer echoes the branch. Instead the branch is displayed as a first-class,
always-visible property of the card, and the environment name becomes what it
already effectively was: a stable slot label. (Revisited — see *Evolution*.)

## How it works (macro)

- The branch already lived in a Docker label (`oduflow.git_branch`), read by
  every pull. Switching therefore means flipping that label and moving the
  managed clone, in that order — reversed, a lost label flip would leave a
  switched tree that the next pull silently resets back to the old branch. Labels are
  frozen at container creation, so the flip recreates the container, reusing the
  same path that already switches immutable extra-addons mounts mid-pull.
- The target branch is fetched *before* anything is mutated. A branch that only
  exists on the developer's machine is the common case, and it fails there with
  "push it first" and no side effects.
- Applying the difference reuses the ordinary sync path. The checkout is moved
  first and the resulting `(old_head, changed_files)` is handed to
  `pull_environment`, so classification, the guardrail, extra-addons resolution
  and install/upgrade/restart have exactly one implementation. The file list is a
  *tree* diff between the two tips, which is what keeps squash-merged histories
  behaving.
- The retained database may outlive code from an earlier branch. The switch does
  not inspect installed module state or predict whether that mismatch is safe;
  install/upgrade/restart failures and later Odoo runtime errors are returned
  through their normal paths for the caller or coding agent to diagnose.
- The coding agent's own checkout for the environment follows the switch through
  the existing create hook, which already refuses to clobber uncommitted work.
- Live-mounted environments ([[0021-code-delivery-modes]]) are rejected: their
  git state belongs to the developer's checkout, not to Oduflow.

## Consequences

- The task boundary no longer costs a provision-and-destroy cycle, and the
  addresses an MCP client or a browser is already using survive it.
- Environment count stops tracking branch count, which makes the pooled hostname
  slots of [[0048-reusable-environment-hostname-slots]] and the idle reaper far
  less load-bearing: fewer environments are created, so fewer need reaping.
- Environment name and branch are now routinely different. Name-derived
  resources keep the original name forever, so operators read the branch, not the
  name, to know what an environment serves — the dashboard shows the branch on
  every card and the info tools report it.
- A reused database accumulates schema from every branch it has served. For the
  intended flow (branch merged, next branch cut from the updated default) that is
  simply the merged work; for abandoned branches, residue remains. Oduflow does
  not proactively classify that residue during a branch switch.
- Declarative Stacks ([[0046-declarative-oduflow-stacks]]) still treat a changed
  branch as immutable drift and recreate the environment. Reconciling it in place
  through this operation is a natural follow-up, not part of this decision.

## Evolution

**Renaming, after all (optional `new_name` on `switch_branch`).** The original
refusal was about the *failure mode*, not about the value: a name left over from
a merged branch is a real annoyance, and "the name is just a slot label" only
holds while somebody is willing to read it that way. What made it safe to add was
noticing that the switch already re-creates the container — labels are frozen at
creation — so the rename needs no recreate of its own. It rides on that same one,
in the window where the container is down.

The "no transaction" objection is answered by ordering rather than by a
transaction that cannot exist. Every check (name validity, the production
namespace, a container / workspace directory / database already holding the
target name, stack membership) runs before the first mutation. Then each step is
individually atomic and ordered so that an interruption leaves an environment's
*data* whole under exactly one of the two names: move the workspace directory
(atomic within a filesystem), rename the database (the one step that can still
fail on its own — a failure moves the directory back), then move the registry
keys. The container is the accepted casualty: it is removed before the
relocation, so a failure from that point on (the database rename, the final
container creation) leaves the environment without a working container, and
nothing here can bring the old one back. A relocation failure states that
outcome explicitly instead of pretending to roll back (a failed final container
creation surfaces the raw Docker error, as it always has); recovery is
re-provisioning the slot — a dev environment, so an acceptable cost for a
failure this rare.

Two properties are deliberately preserved rather than re-derived: the port and
hostname registry keys are *moved*, not released and re-allocated, because they
are the environment's address; and the coding agent's checkout is moved rather
than removed and re-cloned, because it can hold uncommitted work. What does not
survive is the scoped MCP path: `/mcp/<name>` follows the name, so an MCP client
configured against the old one must be re-pointed. That is the cost the rename
carries, and it is why the tool's response states the new name explicitly.

Stack-managed environments are refused: there the name comes from the stack
definition, and renaming behind it would read as drift on the next reconcile.
A strict guardrail refusal blocks the rename as well, so "blocked" keeps meaning
that nothing changed.

One deliberate rough edge remains: the agent checkout is moved *after* the
container recreate, so a recreate that fails leaves the environment under the new
name with its agent checkout still under the old slug. That is a best-effort
side path — the next console or chat re-clones it — and buying atomicity there
would mean moving it before a step that can still fail.

**Unconditional switching.** The original implementation compared module
manifests in the two Git trees and queried `ir_module_module` for modules that
disappeared. That heuristic could not see the effective addons path: code may
still come from an extra repository or the image, while a real incompatibility
may only appear on a later request or restart. Blocking a branch move under
`strict` therefore treated an incomplete prediction as stronger evidence than
the actual apply and runtime results. The preflight was removed: `switch_branch`
now moves the code unconditionally after fetch, and `strict` is limited to the
ordinary apply guardrail for an incomplete install/upgrade/restart action.

## History

- `litnimax/bangkok` (2026-08-17) — `switch_branch` MCP tool, REST endpoint and
  dashboard control; `fetch_branch` / `checkout_branch` / `tree_modules` git
  plumbing; dropped-module preflight; `presynced` sync path in
  `pull_environment`.
- `litnimax/montgomery-v1` (2026-08-17) — optional `new_name`: `rename_to` in
  `update_environment` with `check_rename_target` /
  `_relocate_environment_state`, `rename_env` in the port and hostname
  registries, `activity.rename`, `agent_sessions.rename`, `_agent_rename_env`.
- `litnimax/explain-strict-module-check` (2026-08-29) — removed the installed
  module preflight; branch switching now relies on actual apply and runtime
  failures, while `strict` remains scoped to action completeness.
