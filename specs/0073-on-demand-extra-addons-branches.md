# 0073 — On-demand extra-addons branches

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-09-29)
**Key code:** `extra_addons.py` (registration, per-branch fetch, remote branch list), `docker_ops/system_ops.py` (template addon wiring)

## Context

Adding an extra-addons repository ([[0010-extra-addons-repositories]]) cloned it
as a shallow bare repo holding the latest commit of **every** branch. Later work
made environment creation fetch only its own branch and let the add wizard pick
a subset, but the default add path still downloaded every branch up front.
Community repositories such as CybroAddons keep one branch per Odoo version and
exceed 10 GiB; the clone ran into the 300-second limit, failed, and left a
partial bare repo that blocked the retry.

Most teams use one or two Odoo versions of such a repository. Downloading all of
them wastes bandwidth and disk, and a single long clone in one HTTP request is
the least recoverable place to pay for it.

## Decision

Adding a remote extra repo **registers** it without downloading code. Each
branch is downloaded the first time an environment, production or template
import uses it, and is then tracked for updates.

- Registration reads the remote branch list (`git ls-remote`), which doubles as
  the access check, and creates an empty bare repo. Selected branches may still
  be downloaded up front to warm them.
- The first download of a branch is shallow (`--depth 1`). A branch already
  present is fetched incrementally, so its old tips stay reachable for
  production rollback and update summaries keep exact commit counts.
- The fetch refspec list is the source of truth: the wildcard keeps legacy
  all-branches repos unchanged; explicit refspecs name the downloaded branches.
  An `.on-demand` marker says that an empty list is normal, so Update never
  restores the wildcard or runs a refspec-less fetch. Older subset repos get
  the marker when they untrack a branch.
- A template import whose remote lacks the requested branch stays a strict
  error; only download failures fall back to the uploaded addon files.
- No new settings: long first downloads get a fixed one-hour ceiling, and a
  stalled HTTPS transfer aborts through git's low-speed check.

## How it works (macro)

The shared per-repo lock now also covers registration, so concurrent adds of the
same name cannot race, and any failure removes the half-created repo. Every
fetch path (dev checkout, production worktree, `update_extra_repo(add_branch)`,
template import) goes through one per-branch helper that picks shallow or
incremental fetch and records the branch as tracked. Update refreshes the stored
remote branch list, untracks branches deleted on the remote (a stale refspec
would otherwise fail every later fetch), then fetches only tracked branches. The dashboard and MCP
show downloaded branches separately from the remote list; branch inputs stay
free text, so a stale list never blocks a request.

## Consequences

- Adding any repository takes seconds regardless of its size; disk and traffic
  scale with the branches actually used.
- The wait moves to the first environment on a new branch. That request is
  still synchronous, but an interrupted client does not stop the server-side
  download, and a retry reuses whatever finished.
- The per-repo lock is held for the whole first download, so other branches of
  the same repository queue behind it.
- A force-pushed tracked branch can still pull history back to the merge base
  on an incremental fetch, as the previous Update did.

## Evolution

Refines [[0010-extra-addons-repositories]] (how bare repos are populated) and
builds on [[0039-shared-immutable-extra-addons-checkouts]], whose SHA-keyed
checkouts are unchanged. Background downloads with progress are a possible next
step if first-use waits become common.

The dashboard's Branches dialog (and `update_extra_repo(add_branch=[...])`)
later made the branch set user-managed in both directions: download chosen
branches ahead of use, or remove an unused one to reclaim disk. Removal is
refused for protected repos and branches in use; it keeps checkouts handed out
in the last six hours (an environment being created has no container yet) and
runs `git gc` outside the repo lock so it never stalls environment work.

## History

- 2026-09-29 — register extra repos without downloading code; per-branch
  on-demand shallow fetch with incremental updates.
- 2026-10-02 — Branches dialog: download or remove branches; removal frees
  cached checkouts and git objects.
