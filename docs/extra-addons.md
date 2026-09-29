# Extra Addons Repositories

![Extra Addons Dashboard](img/extra_addons.png)

Oduflow supports mounting **extra addon repositories** (e.g. Odoo Enterprise,
third-party themes) into environments. Git objects and immutable checkouts are
shared by all development environments in a team.

## Architecture

```
{data_dir}/team_{ID}/
  shared_repos/
    enterprise/          ← bare git repo, only the branches in use (shared)
    custom-themes/       ← bare git repo, only the branches in use (shared)
  shared_extra_checkouts/
    enterprise/
      a1b2c3.../          ← immutable checkout of one commit (shared)
    custom-themes/
      d4e5f6.../          ← immutable checkout of one commit (shared)
  workspaces/
    feature-x/
      repo/              ← main project repo (existing)
```

The requested branch selects a commit when an environment is created. Several
environments on the same commit mount the same checkout read-only, without
duplicating its files. Checkouts are keyed by commit rather than branch because
branches move; an environment stays isolated on its current revision until it
is explicitly synced.

Production deployments retain private worktrees because their deploy engine
records and resets each worktree HEAD during rollback.

## Setting Up Extra Repos

Add an extra repository once (it will be available for all environments):

```bash
# Via CLI
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git

# Private repos — store an access token first
oduflow call setup_repo_auth '{"repo_url": "https://github.com/odoo/enterprise.git", "token": "ghp_..."}'
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git
```

Adding a repository downloads **no code**. Oduflow checks access, reads the
remote branch list (`git ls-remote`) and creates an empty shared bare repo, so
even a multi-gigabyte repository with a branch per Odoo version is added in
seconds.

Each branch is downloaded the first time an environment or production uses it:
only that branch's latest commit, without history (`--depth 1`). A repository
that holds `16.0` to `19.0` therefore costs only the versions your environments
actually use. Once downloaded, a branch is kept up to date by
[`update_extra_repo`](#updating-extra-repos).

The first environment on a new branch waits for that download. To download a
branch ahead of time, select it when adding the repository (the dashboard's
**Load branches** picker, or `branches="18.0"` in `add_extra_repo`), or run
`update_extra_repo(name, add_branch="18.0")` later.

A stalled HTTPS download is aborted after two minutes without progress; a slow
but progressing one may run up to an hour.

## Using Extra Addons in Environments

When creating an environment, specify which extra repos to mount:

```bash
# Mount enterprise addons on branch 19.0
oduflow call create_environment feature-x "" default https://github.com/company/addons.git odoo:19.0 "enterprise:19.0"

# Mount multiple extra repos
oduflow call create_environment feature-x "" default https://github.com/company/addons.git odoo:19.0 "enterprise:19.0,custom-themes:main"
```

For each development environment Oduflow automatically:

1. Fetches the specified branch (downloading it on first use) and resolves
   its current commit SHA
2. Creates or reuses the team's immutable checkout for that SHA
3. Mounts the checkout **read-only** as `/mnt/extra-addons-{name}`
4. Generates a merged `odoo.conf` with all extra paths added to `addons_path`
   — modules may live either at the repository root or in a top-level
   `addons/` directory (the same convention as the main repo); in the latter
   case `addons_path` points at that subdirectory automatically
5. Installs the repo's `.oduflow/requirements.txt` / `.oduflow/apt_packages.txt`
   (with the same lookup rules as the [main repo's](environments.md#auto-dependency-installation)),
   so an extra repo declares its own Python/apt dependencies

## Managing Extra Repos

```bash
# List extra repos with downloaded and remote branches
oduflow call list_extra_repos

# Delete an extra repo (fails if any environment references it)
oduflow call delete_extra_repo enterprise
```

Extra repos can also be managed from the **Web Dashboard** under the "Extra Addons" tab.

## Protecting Extra Repos

Extra addon repositories can be **protected** from accidental deletion, similar to [environment protection](environments.md#environment-protection). A protected repo cannot be deleted until protection is removed.

Protection state is stored as a `.protected` marker file in the bare repository directory.

### Via REST API

```bash
# Protect an extra repo
curl -X POST http://localhost:8000/api/extra-repos/enterprise/protect

# Unprotect an extra repo
curl -X POST http://localhost:8000/api/extra-repos/enterprise/unprotect
```

### Via Web Dashboard

Extra repo protection can be toggled from the **Extra Addons** tab in the Web Dashboard. When protected:

- The **Delete** button is disabled
- Attempting to delete via API returns a `ProtectedError`

## Updating Extra Repos

Use `update_extra_repo` to fetch the latest changes from the remote:

```bash
oduflow call update_extra_repo enterprise
```

This refreshes the remote branch list and fetches every **downloaded** branch
into the **shared bare repository**, pruning branches deleted on the remote.
Branches that no environment has used yet stay undownloaded. It does **not**
change the checkout mounted by any running environment.

Downloaded branches are fetched incrementally, so the update reports how many
commits arrived and older commits stay available for production rollback.

Repositories added before on-demand downloads (Oduflow versions that cloned
every branch) keep that behaviour: their update still fetches all branches.

### Updating an environment

Run the normal sync operation:

```bash
oduflow call pull_and_apply feature-x
```

`pull_and_apply` fetches every configured extra-addons branch, creates or reuses
the new SHA checkout, classifies its changed files, switches only that
environment's read-only mount, and performs the required install, upgrade, or
restart. Other environments continue using their previous checkout.

Cached checkouts are deliberately not reference-counted or removed with an
environment. Deleting the extra repository removes its bare repo and every
cached revision after Oduflow verifies that no environment or production still
depends on it.
