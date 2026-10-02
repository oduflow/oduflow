import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.docker_ops.stats import _dir_size_bytes
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError, git_env_for_team
from oduflow.naming import sanitize_repo_url
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,63}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")

# odoo.conf [options] keys managed via container env vars (HOST, USER, ...).
# Stripped from generated confs and refused as per-production overrides; kept
# in one place so the two sites cannot drift.
DB_CONN_CONF_KEYS = ("db_host", "db_port", "db_user", "db_password")

_WILDCARD_REFSPEC = "+refs/heads/*:refs/heads/*"
# Marks a repo whose fetch refspecs list exactly the branches it tracks (every
# repo registered on demand, and older subset repos once they untrack a branch),
# so having none is a normal state, not a legacy clone that lost its wildcard.
_ON_DEMAND_MARKER = ".on-demand"
# Branch names last seen on the remote (git ls-remote), refreshed at
# registration and on Update. Hints only: an environment may still request a
# branch that is missing here.
_REMOTE_BRANCHES_FILE = ".remote-branches.json"

# The first download of a big branch can legitimately take many minutes. The
# hard ceiling only stops runaway commands; a stalled HTTPS transfer aborts much
# sooner through git's low-speed check (SSH has no equivalent).
_FETCH_TIMEOUT = 3600
_CHECKOUT_TIMEOUT = 600
# Removing a branch keeps cached checkouts handed out this recently: an
# environment being created or updated holds its checkout before its container
# (the in-use record) exists.
_CHECKOUT_GRACE_SECONDS = 6 * 3600
# Cached checkouts are moved here (inside the repo's cache dir) under the repo
# lock and deleted after it is released.
_REMOVING_PREFIX = ".removing-"
_LOW_SPEED_ENV = {
    "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
    "GIT_HTTP_LOW_SPEED_TIME": "120",
}


def validate_extra_repo_name(name: str) -> None:
    """Reject repo names unsafe as path components (used under workspaces)."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid repo name '{name}': only [a-zA-Z0-9_-] allowed, "
            "no dots or slashes, max 63 chars."
        )


_REPO_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[str, threading.RLock] = {}

_AUTH_ERROR_KEYWORDS = (
    "Authentication failed",
    "could not read Username",
    "Permission denied",
    "Repository not found",
    "terminal prompts disabled",
    "Invalid username or password",
)


def _raise_if_auth_error(repo_url: str, stderr: str) -> None:
    """Map a git auth failure to a RepoAuthError with actionable guidance."""
    if not any(kw in stderr for kw in _AUTH_ERROR_KEYWORDS):
        return
    from oduflow.git_ops import is_ssh_url

    if is_ssh_url(repo_url):
        raise RepoAuthError(
            f"Authentication failed for '{sanitize_repo_url(repo_url)}'. "
            "The remote uses SSH: register the team deploy key "
            "(get_ssh_public_key) with the git host, or use an "
            "HTTPS URL with setup_repo_auth."
        )
    raise RepoAuthError(
        f"Authentication failed for '{sanitize_repo_url(repo_url)}'. "
        "Use setup_repo_auth to configure credentials first."
    )


def validate_branch_name(branch: str) -> None:
    """Reject branch names git itself would refuse (and option injection)."""
    if not branch or branch.startswith("-"):
        raise ValueError(f"Invalid branch name {branch!r}.")
    try:
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"],
            check=True,
            capture_output=True,
            timeout=10,
            env=GIT_ENV,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError(f"Invalid branch name {branch!r}.")


def list_remote_branches(
    team: TeamSettings, repo_url: str, git_user: str = ""
) -> list[str]:
    """List branch names of a remote repo (``git ls-remote --heads``).

    Used by the add-repo wizard to offer a branch selection before anything is
    downloaded; doubles as an early URL/credentials check.
    """
    from oduflow.git_ops import inject_credential_user

    remote_url = inject_credential_user(repo_url, git_user)
    return _ls_remote_heads(team, ["git", "ls-remote", "--heads", remote_url], repo_url)


def _remote_git_env(team: TeamSettings) -> dict[str, str]:
    """Environment for git commands that talk to a team's remotes."""
    return {
        **git_env_for_team(team.git_credentials_file(), team.ssh_dir()),
        **_LOW_SPEED_ENV,
    }


def _ls_remote_heads(team: TeamSettings, args: list[str], repo_url: str) -> list[str]:
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=_remote_git_env(team),
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        _raise_if_auth_error(repo_url, stderr)
        raise ExternalCommandError("git ls-remote --heads", e.returncode, stderr)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git ls-remote --heads", -1, "Listing branches timed out (60s)."
        )

    branches = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches.append(parts[1][len("refs/heads/") :])
    return sorted(branches)


def _read_remote_branches(repo_path: str) -> list[str]:
    try:
        with open(os.path.join(repo_path, _REMOTE_BRANCHES_FILE)) as f:
            branches = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(branches, list):
        return []
    return [b for b in branches if isinstance(b, str)]


def _write_remote_branches(repo_path: str, branches: list[str]) -> None:
    target = os.path.join(repo_path, _REMOTE_BRANCHES_FILE)
    tmp = f"{target}.tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(branches), f)
    os.replace(tmp, target)


def _refresh_remote_branches(team: TeamSettings, repo_path: str) -> list[str]:
    url = _git_config(repo_path, "remote.origin.url")
    branches = _ls_remote_heads(
        team, ["git", "-C", repo_path, "ls-remote", "--heads", "origin"], url
    )
    _write_remote_branches(repo_path, branches)
    return branches


def _git_config(repo_path: str, key: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_path, "config", "--get", key],
        capture_output=True,
        text=True,
        timeout=10,
        env=GIT_ENV,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _configured_refspecs(repo_path: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo_path, "config", "--get-all", "remote.origin.fetch"],
        capture_output=True,
        text=True,
        timeout=10,
        env=GIT_ENV,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _branch_refspec(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/heads/{branch}"


def _untrack_deleted_branches(
    repo_path: str, name: str, remote_branches: list[str]
) -> None:
    """Stop tracking branches that no longer exist on the remote.

    A configured refspec whose source ref is gone makes every ``git fetch``
    fail before ``--prune`` could clean it up, so Update would stay broken for
    good. Drop the refspec and the local branch; worktrees and cached
    checkouts keep their commits.
    """
    for refspec in _configured_refspecs(repo_path):
        if refspec == _WILDCARD_REFSPEC:
            continue
        match = re.fullmatch(r"\+refs/heads/(.+):refs/heads/\1", refspec)
        if not match or match.group(1) in remote_branches:
            continue
        branch = match.group(1)
        _forget_branch(repo_path, branch)
        logger.info(
            "Extra repo '%s': branch '%s' was deleted on the remote; untracked",
            name,
            branch,
        )


def _forget_branch(repo_path: str, branch: str) -> None:
    """Drop *branch*'s fetch refspec (if configured) and its local ref."""
    # Subset repos created before the marker existed may lose their last
    # refspec here; mark them so the empty set stays intentional instead of
    # being "repaired" into the all-branches wildcard.
    open(os.path.join(repo_path, _ON_DEMAND_MARKER), "w").close()
    refspec = _branch_refspec(branch)
    if refspec in _configured_refspecs(repo_path):
        _unset_refspec(repo_path, refspec)
    subprocess.run(
        ["git", "-C", repo_path, "update-ref", "-d", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=GIT_ENV,
    )


def _unset_refspec(repo_path: str, refspec: str) -> None:
    # The value pattern is a POSIX ERE (--fixed-value needs git 2.30+).
    pattern = "^" + re.sub(r"([.\[\](){}*+?|^$\\])", r"\\\1", refspec) + "$"
    subprocess.run(
        ["git", "-C", repo_path, "config", "--unset", "remote.origin.fetch", pattern],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=GIT_ENV,
    )


@contextmanager
def _repo_operation_lock(team: TeamSettings, repo_name: str) -> Iterator[None]:
    """Serialize fetch/worktree/cache mutations for one team's extra repo.

    Environment locks intentionally allow different environments to run in
    parallel. Extra repositories are shared between those environments, so Git
    operations need their own, narrower lock. RLock keeps composed helpers
    (ensure checkout -> fetch) safe without widening the lock to the whole team.
    """
    key = os.path.realpath(os.path.join(team.shared_repos_dir, repo_name))
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def register_extra_repo(
    team: TeamSettings,
    name: str,
    repo_url: str,
    git_user: str = "",
    branches: list[str] | None = None,
) -> dict[str, Any]:
    """Register a remote extra-addons repo without downloading its code.

    Only the remote branch list is read (``git ls-remote``), which doubles as
    the URL/credentials check; the bare repo starts empty. An environment
    downloads a branch the first time it uses it, so a repo holding every Odoo
    version costs only the versions actually in use. *branches* are downloaded
    right away instead, to have them ready before the first environment.
    """
    validate_extra_repo_name(name)
    branches = [b.strip() for b in (branches or []) if b and b.strip()]
    for branch in branches:
        validate_branch_name(branch)

    from oduflow.git_ops import inject_credential_user

    remote_url = inject_credential_user(repo_url, git_user)
    target = os.path.join(team.shared_repos_dir, name)
    with _repo_operation_lock(team, name):
        if os.path.exists(target):
            raise ConflictError(f"Extra repo '{name}' already exists at {target}")

        available = _ls_remote_heads(
            team, ["git", "ls-remote", "--heads", remote_url], repo_url
        )
        missing = [b for b in branches if b not in available]
        if missing:
            raise NotFoundError(
                f"Branch(es) not found in '{sanitize_repo_url(repo_url)}': "
                f"{', '.join(missing)}."
            )

        os.makedirs(team.shared_repos_dir, exist_ok=True)
        try:
            _init_on_demand_repo(target, remote_url, available)
            if branches:
                _fetch_branches_unlocked(team, name, target, branches)
        except BaseException:
            # Leave nothing half-registered, so a retry does not hit
            # "already exists" after a timeout or a failed download.
            shutil.rmtree(target, ignore_errors=True)
            raise

    logger.info(
        "Registered extra repo '%s' from %s (downloaded: %s)",
        name,
        sanitize_repo_url(repo_url),
        ", ".join(branches) or "nothing",
    )
    return {
        "name": name,
        "repo_url": repo_url,
        "path": target,
        "branches": branches,
        "available_branches": available,
    }


def _init_on_demand_repo(target: str, remote_url: str, available: list[str]) -> None:
    # `git remote add` would seed the wildcard refspec; setting only the URL
    # leaves the refspec list empty until a branch is actually downloaded.
    for args in (
        ["git", "init", "--bare", target],
        ["git", "-C", target, "config", "remote.origin.url", remote_url],
    ):
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=GIT_ENV,
            )
        except subprocess.CalledProcessError as e:
            raise ExternalCommandError(" ".join(args[:4]), e.returncode, e.stderr or "")
    open(os.path.join(target, _ON_DEMAND_MARKER), "w").close()
    _write_remote_branches(target, available)


def create_local_repo(
    team: TeamSettings, name: str, source_dir: str, branch: str
) -> dict[str, Any]:
    """Create an extra-addons repo from local files, with no remote origin.

    Used by the Odoo.sh import for addons that cannot be cloned (Enterprise,
    Themes, private extra repos): the uploaded directory is seeded into a real
    bare git repo with a single *branch*, so the normal worktree / mount /
    pull_and_apply machinery works unchanged. A ``.local`` marker file records
    that the repo has no origin; :func:`fetch_extra_repo` short-circuits on it,
    so worktree creation and pulls never attempt a (non-existent) fetch.
    """
    validate_extra_repo_name(name)
    if not branch:
        raise ValueError("A branch name is required for a local extra repo.")

    target = os.path.join(team.shared_repos_dir, name)
    if os.path.exists(target):
        raise ConflictError(f"Extra repo '{name}' already exists at {target}")

    os.makedirs(team.shared_repos_dir, exist_ok=True)

    # A clean environment has no git identity configured, so the seed commit
    # would fail — supply one explicitly for this operation only.
    seed_env = {
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "Oduflow",
        "GIT_AUTHOR_EMAIL": "import@oduflow.local",
        "GIT_COMMITTER_NAME": "Oduflow",
        "GIT_COMMITTER_EMAIL": "import@oduflow.local",
    }

    def _git(args: list[str], timeout: int = 300) -> None:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=seed_env,
        )

    tmp = tempfile.mkdtemp(prefix="oduflow-localrepo-")
    try:
        _git(["git", "init", "--bare", target], timeout=30)
        _git(["git", "-C", tmp, "init"], timeout=30)
        # Copy the source tree in, skipping any stray VCS metadata (Odoo.sh
        # worktrees leave a .git gitdir pointer; the tar upload excludes it, but
        # be defensive here too).
        for entry in os.listdir(source_dir):
            if entry == ".git":
                continue
            src = os.path.join(source_dir, entry)
            dst = os.path.join(tmp, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        _git(["git", "-C", tmp, "add", "-A"], timeout=120)
        _git(
            [
                "git",
                "-C",
                tmp,
                "commit",
                "--allow-empty",
                "-m",
                "Imported from Odoo.sh",
            ],
            timeout=120,
        )
        _git(["git", "-C", tmp, "branch", "-M", branch], timeout=30)
        _git(["git", "-C", tmp, "push", target, f"{branch}:{branch}"], timeout=300)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(target, ignore_errors=True)
        raise ExternalCommandError(
            "git (create local repo)", e.returncode, e.stderr or ""
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise ExternalCommandError("git (create local repo)", -1, "Seed timed out.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Mark as local (no origin) — the marker is what fetch_extra_repo keys on.
    open(os.path.join(target, ".local"), "w").close()

    logger.info("Created local extra repo '%s' (branch '%s')", name, branch)
    return {
        "name": name,
        "repo_url": "",
        "path": target,
        "local": True,
        "branch": branch,
    }


def is_local_repo(team: TeamSettings, name: str) -> bool:
    """True if the extra repo is a remote-less local repo (Odoo.sh import)."""
    return os.path.exists(os.path.join(team.shared_repos_dir, name, ".local"))


def list_extra_repos(team: TeamSettings) -> list[dict[str, Any]]:
    repos_dir = team.shared_repos_dir
    if not os.path.isdir(repos_dir):
        return []

    return [
        _repo_info(os.path.join(repos_dir, entry), entry)
        for entry in sorted(os.listdir(repos_dir))
        if os.path.isdir(os.path.join(repos_dir, entry))
    ]


def get_extra_repo(team: TeamSettings, name: str) -> dict[str, Any]:
    """One entry of :func:`list_extra_repos`."""
    validate_extra_repo_name(name)
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    return _repo_info(path, name)


def _repo_info(path: str, entry: str) -> dict[str, Any]:
    try:
        url = subprocess.run(
            ["git", "-C", path, "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        url = ""

    try:
        # lstrip, not :short — short names turn into "heads/17.0" when a tag
        # of the same name exists.
        branches_raw = subprocess.run(
            [
                "git",
                "-C",
                path,
                "for-each-ref",
                "--format=%(refname:lstrip=2)",
                "refs/heads/",
                "refs/remotes/",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        ).stdout.strip()
        branches = [b for b in branches_raw.splitlines() if b]
    except subprocess.CalledProcessError:
        branches = []

    return {
        "name": entry,
        "repo_url": sanitize_repo_url(url),
        # Downloaded branches; available_branches is the remote list, unknown
        # until the first refresh for repos registered before it was stored.
        "branches": branches,
        "available_branches": _read_remote_branches(path),
        "available_known": os.path.exists(os.path.join(path, _REMOTE_BRANCHES_FILE)),
        "protected": os.path.exists(os.path.join(path, ".protected")),
        "local": os.path.exists(os.path.join(path, ".local")),
    }


def is_extra_repo_protected(team: TeamSettings, name: str) -> bool:
    path = os.path.join(team.shared_repos_dir, name)
    return os.path.exists(os.path.join(path, ".protected"))


def protect_extra_repo(team: TeamSettings, name: str) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    marker = os.path.join(path, ".protected")
    open(marker, "w").close()
    logger.info("Extra repo protected: %s", name)
    return {"name": name, "protected": True}


def unprotect_extra_repo(team: TeamSettings, name: str) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    marker = os.path.join(path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Extra repo unprotected: %s", name)
    return {"name": name, "protected": False}


def _extra_repo_consumers(
    settings: Settings, team: TeamSettings, name: str
) -> list[dict[str, str]]:
    """Managed containers (running or stopped) that mount extra repo *name*.

    Each entry names the consumer (environment branch label or container
    name), the repo branch it uses and the checkout revision it is pinned to;
    branch and revision are empty when the labels do not record them.
    """
    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    consumers: list[dict[str, str]] = []
    for c in client.containers.list(all=True, filters=filters):
        raw = c.labels.get("oduflow.extra_addons", "")
        if not raw:
            continue
        try:
            extras = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(extras, (dict, list)) or name not in extras:
            continue
        try:
            revisions = json.loads(c.labels.get("oduflow.extra_addons_revisions", "{}"))
        except (json.JSONDecodeError, TypeError):
            revisions = {}
        branch = extras.get(name) if isinstance(extras, dict) else ""
        revision = revisions.get(name) if isinstance(revisions, dict) else ""
        prod_name = c.labels.get("oduflow.prod_name", "")
        consumers.append(
            {
                # Production containers deliberately carry no branch label.
                "consumer": f"production {prod_name}"
                if prod_name
                else c.labels.get(settings.branch_label, c.name),
                "branch": branch if isinstance(branch, str) else "",
                "revision": revision if isinstance(revision, str) else "",
            }
        )
    return consumers


def _delete_extra_repo_unlocked(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.exists(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    if is_extra_repo_protected(team, name):
        raise ProtectedError(
            f"Extra repo '{name}' is protected. Unprotect it before deleting."
        )

    dependent = [c["consumer"] for c in _extra_repo_consumers(settings, team, name)]
    if dependent:
        raise ConflictError(
            f"Cannot delete extra repo '{name}': used by environments: "
            f"{', '.join(dependent)}"
        )

    # Containers are the dependency record, so there is a window this guard
    # cannot see: create_environment materialises shared checkouts *before* its
    # container exists. A delete landing exactly there is accepted — the
    # in-flight create fails with a clear "not found" instead of every unrelated
    # repo operation being serialised behind a team lock for minutes.

    # Cached SHA checkouts intentionally outlive environments. They disappear
    # only with the owning extra repo, after the dependency guard above proves
    # that no running/stopped managed container still mounts them.
    checkout_root = os.path.join(team.shared_extra_checkouts_dir, name)
    if os.path.isdir(checkout_root):
        shutil.rmtree(checkout_root)
    shutil.rmtree(path)
    logger.info("Deleted extra repo '%s'", name)
    return {"name": name, "deleted": True}


def delete_extra_repo(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    with _repo_operation_lock(team, name):
        return _delete_extra_repo_unlocked(settings, team, name)


def _get_branch_refs(repo_path: str) -> dict[str, str]:
    """Return a mapping of branch name → commit SHA for all local branches."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads/",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    refs: dict[str, str] = {}
    # Full ref names: %(refname:short) yields "heads/17.0" when a tag of the
    # same name exists (legacy all-branches clones fetched tags too).
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].startswith("refs/heads/"):
            refs[parts[0][len("refs/heads/") :]] = parts[1]
    return refs


def _fetch_branches_unlocked(
    team: TeamSettings, name: str, path: str, branches: list[str]
) -> None:
    """Download *branches* into the bare repo and keep them tracked.

    A branch absent locally is fetched shallow (``--depth 1``), so the first use
    of e.g. ``19.0`` in a big multi-version repo brings no history. A branch
    already present is fetched incrementally: the new tip stays connected to
    the old one, which keeps ``new_commits`` exact and keeps the older SHAs that
    production rollback resets to reachable (a depth-1 refetch would orphan
    them for ``git gc``). Each fetched branch then joins the fetch refspecs
    (unless the wildcard already covers it), so Update keeps refreshing it.
    """
    present = _get_branch_refs(path)
    env = _remote_git_env(team)
    for group, depth in (
        ([b for b in branches if b not in present], ["--depth", "1"]),
        ([b for b in branches if b in present], []),
    ):
        if not group:
            continue
        label = f"git fetch origin {' '.join(group)}"
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    path,
                    "fetch",
                    *depth,
                    "--no-tags",
                    "--recurse-submodules=no",
                    "origin",
                    *(_branch_refspec(b) for b in group),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_FETCH_TIMEOUT,
                env=env,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            if "couldn't find remote ref" in stderr:
                raise NotFoundError(
                    f"Branch '{', '.join(group)}' not found in the remote of "
                    f"extra repo '{name}'."
                )
            raise ExternalCommandError(label, e.returncode, stderr)
        except subprocess.TimeoutExpired:
            raise ExternalCommandError(
                label, -1, f"Fetch timed out ({_FETCH_TIMEOUT}s)."
            )

    configured = _configured_refspecs(path)
    if _WILDCARD_REFSPEC in configured:
        return
    for branch in branches:
        refspec = _branch_refspec(branch)
        if refspec in configured:
            continue
        subprocess.run(
            ["git", "-C", path, "config", "--add", "remote.origin.fetch", refspec],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
        logger.info("Extra repo '%s': now tracking branch '%s'", name, branch)


def _fetch_extra_repo_unlocked(
    team: TeamSettings, name: str, branch: str | None = None
) -> dict[str, Any]:
    """Fetch latest changes and return a summary of what changed.

    When *branch* is given, only that one branch is fetched (see
    :func:`_fetch_branches_unlocked`) instead of every tracked branch: creating
    or updating a checkout needs just its own branch, and fetching every
    branch of a large repo like Odoo Enterprise otherwise takes far longer.
    The explicit "update repo" path passes no branch: it refreshes the remote
    branch list and fetches every tracked branch to report changes.

    Returns a dict with keys: name, up_to_date, new_branches,
    deleted_branches, updated_branches.
    """
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    # Local (remote-less) repos have no origin to fetch from. Short-circuit so
    # every caller — create_worktree, pull_extra_worktree, the pull REST/MCP
    # path — treats them as always up to date instead of failing on git fetch.
    if os.path.exists(os.path.join(path, ".local")):
        return {
            "name": name,
            "local": True,
            "up_to_date": True,
            "new_branches": [],
            "deleted_branches": [],
            "updated_branches": [],
        }

    # Bare repos cloned before the refspec fix lack one; restore the wildcard
    # they were cloned with. Never for on-demand repos, where no refspec just
    # means nothing has been downloaded yet.
    if not os.path.exists(os.path.join(path, _ON_DEMAND_MARKER)):
        try:
            if not _configured_refspecs(path):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        path,
                        "config",
                        "remote.origin.fetch",
                        _WILDCARD_REFSPEC,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=GIT_ENV,
                )
                logger.info("Added missing fetch refspec for extra repo '%s'", name)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass  # best-effort; fetch will still run

    refs_before = _get_branch_refs(path)

    if branch:
        _fetch_branches_unlocked(team, name, path, [branch])
    else:
        _untrack_deleted_branches(path, name, _refresh_remote_branches(team, path))
        # With no refspec, `git fetch` would download the remote HEAD with its
        # whole history — the very cost on-demand registration avoids.
        if _configured_refspecs(path):
            fetch_label = "git fetch --all --prune"
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        path,
                        "fetch",
                        "--all",
                        "--prune",
                        "--recurse-submodules=no",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_FETCH_TIMEOUT,
                    env=_remote_git_env(team),
                )
            except subprocess.CalledProcessError as e:
                raise ExternalCommandError(fetch_label, e.returncode, e.stderr or "")
            except subprocess.TimeoutExpired:
                raise ExternalCommandError(
                    fetch_label, -1, f"Fetch timed out ({_FETCH_TIMEOUT}s)."
                )

    refs_after = _get_branch_refs(path)

    new_branches = sorted(set(refs_after) - set(refs_before))
    deleted_branches = sorted(set(refs_before) - set(refs_after))
    updated_branches: list[dict[str, Any]] = []
    for branch_name in sorted(set(refs_before) & set(refs_after)):
        old_sha = refs_before[branch_name]
        new_sha = refs_after[branch_name]
        if old_sha != new_sha:
            try:
                count_result = subprocess.run(
                    ["git", "-C", path, "rev-list", "--count", f"{old_sha}..{new_sha}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=GIT_ENV,
                )
                new_commits = int(count_result.stdout.strip())
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                ValueError,
            ):
                new_commits = 0
            updated_branches.append(
                {
                    "branch": branch_name,
                    "new_commits": new_commits,
                }
            )

    up_to_date = not new_branches and not deleted_branches and not updated_branches
    logger.info("Fetched extra repo '%s' (up_to_date=%s)", name, up_to_date)
    return {
        "name": name,
        "up_to_date": up_to_date,
        "new_branches": new_branches,
        "deleted_branches": deleted_branches,
        "updated_branches": updated_branches,
    }


def fetch_extra_repo(
    team: TeamSettings, name: str, branch: str | None = None
) -> dict[str, Any]:
    with _repo_operation_lock(team, name):
        return _fetch_extra_repo_unlocked(team, name, branch)


def _remote_repo_path(team: TeamSettings, name: str) -> str:
    validate_extra_repo_name(name)
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    if os.path.exists(os.path.join(path, ".local")):
        raise PrerequisiteNotMetError(
            f"Extra repo '{name}' is local (no remote); its branches cannot be "
            "downloaded or removed."
        )
    return path


def track_branches(
    team: TeamSettings, name: str, branches: list[str]
) -> dict[str, Any]:
    """Download *branches* now and keep them updated by later Updates.

    Environments download a branch on first use anyway; this warms them up
    ahead of time (``update_extra_repo(add_branch=...)``, dashboard Branches).
    The remote branch list is refreshed first, so a missing name fails with a
    clear message before anything is fetched.
    """
    branches = list(dict.fromkeys(b.strip() for b in branches if b and b.strip()))
    if not branches:
        raise PrerequisiteNotMetError("No branch given to download.")
    for branch in branches:
        validate_branch_name(branch)
    with _repo_operation_lock(team, name):
        path = _remote_repo_path(team, name)
        available = _refresh_remote_branches(team, path)
        missing = [b for b in branches if b not in available]
        if missing:
            raise NotFoundError(
                f"Branch(es) not found in the remote of extra repo '{name}': "
                f"{', '.join(missing)}."
            )
        present = _get_branch_refs(path)
        _fetch_branches_unlocked(team, name, path, branches)
        return {
            "name": name,
            "branches": branches,
            "downloaded": [b for b in branches if b not in present],
            "updated": [b for b in branches if b in present],
        }


def track_branch(team: TeamSettings, name: str, branch: str) -> dict[str, Any]:
    """Download one *branch* now (see :func:`track_branches`)."""
    track_branches(team, name, [branch])
    return {"name": name, "branch": branch, "tracked": True}


def refresh_remote_branches(team: TeamSettings, name: str) -> list[str]:
    """Re-read the remote branch list (``git ls-remote``); downloads nothing."""
    with _repo_operation_lock(team, name):
        return _refresh_remote_branches(team, _remote_repo_path(team, name))


def branch_usage(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, list[str]]:
    """Map each branch of extra repo *name* to the consumers that use it.

    Consumers are environments and productions, from their containers
    (running or stopped) plus the production registry, so a production whose
    container is gone mid-deploy still counts.
    """
    return _branch_usage(team, name, _extra_repo_consumers(settings, team, name))


def _branch_usage(
    team: TeamSettings, name: str, consumers: list[dict[str, str]]
) -> dict[str, list[str]]:
    from oduflow import production_registry

    usage: dict[str, set[str]] = {}
    for c in consumers:
        if c["branch"]:
            usage.setdefault(c["branch"], set()).add(c["consumer"])
    for prod_name, record in production_registry.list_productions(team).items():
        extras = record.get("extra_addons") or {}
        branch = extras.get(name) if isinstance(extras, dict) else None
        if isinstance(branch, str) and branch:
            usage.setdefault(branch, set()).add(f"production {prod_name}")
    return {branch: sorted(consumers) for branch, consumers in usage.items()}


def _reachable_from_branches(repo_path: str, revisions: set[str]) -> set[str]:
    """The *revisions* some local branch contains, from one history walk.

    On failure every revision counts as reachable: when in doubt, keep the
    checkout.
    """
    if not revisions:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-list", "--branches"],
            capture_output=True,
            text=True,
            timeout=300,
            env=GIT_ENV,
        )
    except subprocess.TimeoutExpired:
        return set(revisions)
    if result.returncode != 0:
        return set(revisions)
    return {line for line in result.stdout.splitlines() if line in revisions}


def _detach_unused_checkouts(
    team: TeamSettings, name: str, repo_path: str, pinned: set[str]
) -> list[tuple[str, str]]:
    """Move cached checkouts nothing needs any more out of the cache.

    A checkout is kept while a container pins it, a remaining branch contains
    it, or it was handed out within the grace period. The rest are renamed
    into a ``.removing-*`` directory, so the caller can size and delete them
    after releasing the repo lock; ``git worktree prune`` then drops their
    registrations. Returns ``(revision, moved path)`` pairs, including
    leftovers of an earlier removal that was interrupted.
    """
    cache_dir = os.path.join(team.shared_extra_checkouts_dir, name)
    if not os.path.isdir(cache_dir):
        return []
    now = time.time()
    candidates: list[str] = []
    detached: list[tuple[str, str]] = []
    for entry in sorted(os.listdir(cache_dir)):
        entry_path = os.path.join(cache_dir, entry)
        try:
            age = now - os.path.getmtime(entry_path)
        except OSError:
            continue
        if entry.startswith(_REMOVING_PREFIX):
            # Fresh ones may belong to a removal still deleting outside the lock.
            if age > _CHECKOUT_GRACE_SECONDS:
                detached.append(("", entry_path))
        elif (
            _REVISION_RE.fullmatch(entry)
            and entry not in pinned
            and age > _CHECKOUT_GRACE_SECONDS
        ):
            candidates.append(entry)
    reachable = _reachable_from_branches(repo_path, set(candidates))
    for revision in candidates:
        if revision in reachable:
            continue
        holder = tempfile.mkdtemp(prefix=_REMOVING_PREFIX, dir=cache_dir)
        moved = os.path.join(holder, revision)
        os.rename(os.path.join(cache_dir, revision), moved)
        detached.append((revision, holder))
    if detached:
        subprocess.run(
            ["git", "-C", repo_path, "worktree", "prune"],
            capture_output=True,
            text=True,
            timeout=30,
            env=GIT_ENV,
        )
    return detached


def untrack_branch(
    settings: Settings, team: TeamSettings, name: str, branch: str
) -> dict[str, Any]:
    """Remove a downloaded *branch* to free disk; it can be downloaded again.

    Refused for a protected repo and while an environment or production uses
    the branch. Drops the branch ref and its fetch refspec (a legacy
    all-branches repo is converted to an explicit list of its other downloaded
    branches, so Update does not bring the branch straight back) and the
    cached checkouts nothing needs any more (see
    :func:`_detach_unused_checkouts`). Deleting those checkouts and
    garbage-collecting the repo happen after the repo lock is released, so
    environment and production operations on the repo are not held up.
    """
    validate_branch_name(branch)
    with _repo_operation_lock(team, name):
        path = _remote_repo_path(team, name)
        if is_extra_repo_protected(team, name):
            raise ProtectedError(
                f"Extra repo '{name}' is protected. Unprotect it before removing "
                "branches."
            )
        refs = _get_branch_refs(path)
        if branch not in refs:
            raise NotFoundError(
                f"Branch '{branch}' is not downloaded in extra repo '{name}'."
            )
        consumers = _extra_repo_consumers(settings, team, name)
        users = _branch_usage(team, name, consumers).get(branch, [])
        if users:
            raise ConflictError(
                f"Cannot remove branch '{branch}' of extra repo '{name}': used by "
                f"{', '.join(users)}."
            )
        pinned = {c["revision"] for c in consumers if c["revision"]}

        configured = _configured_refspecs(path)
        if _WILDCARD_REFSPEC in configured:
            # Mark first and drop the wildcard last: until then the repo still
            # tracks every branch, so a failure part way leaves it unchanged
            # and a retry picks up where this one stopped.
            open(os.path.join(path, _ON_DEMAND_MARKER), "w").close()
            for other in sorted(refs):
                refspec = _branch_refspec(other)
                if other == branch or refspec in configured:
                    continue
                subprocess.run(
                    [
                        "git",
                        "-C",
                        path,
                        "config",
                        "--add",
                        "remote.origin.fetch",
                        refspec,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=GIT_ENV,
                )
            _unset_refspec(path, _WILDCARD_REFSPEC)
            logger.info(
                "Extra repo '%s': switched from all branches to the %d downloaded ones",
                name,
                len(refs) - 1,
            )
        _forget_branch(path, branch)
        detached = _detach_unused_checkouts(team, name, path, pinned)

    checkouts_size = 0
    for _revision, holder in detached:
        checkouts_size += _dir_size_bytes(holder)
        shutil.rmtree(holder, ignore_errors=True)

    # gc guards against concurrent writers by itself (gc.pid, and the prune
    # grace period protects objects a parallel fetch has just written).
    # Objects of a branch downloaded within that grace period stay until a
    # later gc.
    objects_dir = os.path.join(path, "objects")
    size_before = _dir_size_bytes(objects_dir)
    try:
        subprocess.run(
            ["git", "-C", path, "gc", "--prune=1.hour.ago", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
            timeout=_FETCH_TIMEOUT,
            env=GIT_ENV,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        # The branch is already gone; a later gc reclaims the space.
        logger.warning("Extra repo '%s': git gc failed: %s", name, e)

    removed_checkouts = [revision for revision, _holder in detached if revision]
    freed = max(size_before - _dir_size_bytes(objects_dir), 0) + checkouts_size
    logger.info(
        "Extra repo '%s': removed branch '%s' (%d cached checkout(s), %d bytes freed)",
        name,
        branch,
        len(removed_checkouts),
        freed,
    )
    return {
        "name": name,
        "branch": branch,
        "removed_checkouts": removed_checkouts,
        "freed_bytes": freed,
    }


def ensure_branch_revision(team: TeamSettings, repo_name: str, branch: str) -> str:
    """Commit SHA of *branch*, downloading the branch first if it is missing.

    Registered repos start empty, so a branch missing locally does not mean it
    is missing on the remote. Local (remote-less) repos cannot fetch, so for
    them a missing branch is final.
    """
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
    with _repo_operation_lock(team, repo_name):
        try:
            return _resolve_branch_revision(bare_path, repo_name, branch)
        except NotFoundError:
            if is_local_repo(team, repo_name):
                raise
        _fetch_extra_repo_unlocked(team, repo_name, branch)
        return _resolve_branch_revision(bare_path, repo_name, branch)


def _create_worktree_unlocked(
    team: TeamSettings, repo_name: str, branch: str, target_path: str
) -> str:
    # A blank branch would produce `git worktree add ... ""` → the cryptic
    # `fatal: invalid reference:`. Reject it up front with a clear message,
    # naming the repo (the empty branch itself is useless in the error). This
    # is the shared choke point for every caller — env create, production
    # deploy, webhook auto-deploy — so guarding here covers them all.
    if not branch or not branch.strip():
        raise PrerequisiteNotMetError(
            f"Extra addon repo '{repo_name}' requires a branch (e.g. '18.0'); "
            "none was given."
        )
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
    if not os.path.isdir(bare_path):
        raise NotFoundError(f"Extra repo '{repo_name}' not found. Add it first.")

    fetch_extra_repo(team, repo_name, branch=branch)

    subprocess.run(
        ["git", "-C", bare_path, "worktree", "prune"],
        capture_output=True,
        text=True,
        timeout=30,
        env=GIT_ENV,
    )

    try:
        subprocess.run(
            [
                "git",
                "-C",
                bare_path,
                "worktree",
                "add",
                "--detach",
                target_path,
                branch,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_CHECKOUT_TIMEOUT,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(
            "git worktree add",
            e.returncode,
            f"Failed to create worktree for branch '{branch}': {e.stderr or ''}",
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git worktree add", -1, f"Command timed out ({_CHECKOUT_TIMEOUT}s)."
        )

    logger.info(
        "Created worktree for %s branch '%s' at %s",
        repo_name,
        branch,
        target_path,
    )
    return target_path


def create_worktree(
    team: TeamSettings, repo_name: str, branch: str, target_path: str
) -> str:
    """Create a mutable per-consumer worktree (currently production only)."""
    with _repo_operation_lock(team, repo_name):
        return _create_worktree_unlocked(team, repo_name, branch, target_path)


def _remove_worktree_unlocked(
    team: TeamSettings, repo_name: str, target_path: str
) -> None:
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
    try:
        subprocess.run(
            ["git", "-C", bare_path, "worktree", "remove", target_path, "--force"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=GIT_ENV,
        )
        logger.info("Removed worktree %s from repo %s", target_path, repo_name)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        logger.debug(
            "Ignoring worktree removal error for %s (may already be cleaned up)",
            target_path,
        )


def remove_worktree(team: TeamSettings, repo_name: str, target_path: str) -> None:
    with _repo_operation_lock(team, repo_name):
        _remove_worktree_unlocked(team, repo_name, target_path)


def _resolve_branch_revision(bare_path: str, repo_name: str, branch: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                bare_path,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}^{{commit}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise NotFoundError(
            f"Branch '{branch}' not found in extra repo '{repo_name}'."
        ) from e
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git rev-parse", -1, "Command timed out (10s).")
    revision = result.stdout.strip().lower()
    if not _REVISION_RE.fullmatch(revision):
        raise ExternalCommandError(
            "git rev-parse", -1, f"Unexpected commit id for branch '{branch}'."
        )
    return revision


def _checkout_head(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return ""
    revision = result.stdout.strip().lower()
    return revision if _REVISION_RE.fullmatch(revision) else ""


def checkout_revision(path: str) -> str:
    """Best-effort commit SHA for a mutable or shared extra checkout."""
    return _checkout_head(path)


def _changed_files_between(
    bare_path: str, old_revision: str, new_revision: str
) -> list[str]:
    if old_revision == new_revision:
        return []
    if not _REVISION_RE.fullmatch(old_revision):
        return []
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                bare_path,
                "diff",
                "--name-only",
                f"{old_revision}..{new_revision}",
                "--",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git diff --name-only", e.returncode, e.stderr or "")
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git diff --name-only", -1, "Command timed out (30s)."
        )
    return [path for path in result.stdout.splitlines() if path]


def ensure_shared_checkout(
    team: TeamSettings,
    repo_name: str,
    branch: str,
    *,
    current_revision: str = "",
) -> dict[str, Any]:
    """Return a persistent immutable checkout for the branch's current SHA.

    Checkouts are shared by all development environments in the team and are
    never reset in place. Moving a branch creates (or reuses) another SHA-keyed
    checkout, so environments pinned to the old revision remain isolated.
    """
    if not branch or not branch.strip():
        raise PrerequisiteNotMetError(
            f"Extra addon repo '{repo_name}' requires a branch (e.g. '18.0'); "
            "none was given."
        )
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
    if not os.path.isdir(bare_path):
        raise NotFoundError(f"Extra repo '{repo_name}' not found. Add it first.")

    with _repo_operation_lock(team, repo_name):
        _fetch_extra_repo_unlocked(team, repo_name, branch)
        revision = _resolve_branch_revision(bare_path, repo_name, branch)
        repo_cache_dir = os.path.join(team.shared_extra_checkouts_dir, repo_name)
        checkout_path = os.path.join(repo_cache_dir, revision)

        existing_head = (
            _checkout_head(checkout_path) if os.path.isdir(checkout_path) else ""
        )
        if existing_head != revision:
            if os.path.exists(checkout_path):
                _remove_worktree_unlocked(team, repo_name, checkout_path)
                shutil.rmtree(checkout_path, ignore_errors=True)
            os.makedirs(repo_cache_dir, exist_ok=True)
            subprocess.run(
                ["git", "-C", bare_path, "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=30,
                env=GIT_ENV,
            )
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        bare_path,
                        "worktree",
                        "add",
                        "--detach",
                        checkout_path,
                        revision,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_CHECKOUT_TIMEOUT,
                    env=GIT_ENV,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                # A killed checkout may already point HEAD at the revision;
                # drop it so the next attempt cannot reuse a half-written tree.
                _remove_worktree_unlocked(team, repo_name, checkout_path)
                shutil.rmtree(checkout_path, ignore_errors=True)
                if isinstance(e, subprocess.TimeoutExpired):
                    raise ExternalCommandError(
                        "git worktree add",
                        -1,
                        f"Command timed out ({_CHECKOUT_TIMEOUT}s).",
                    )
                raise ExternalCommandError(
                    "git worktree add",
                    e.returncode,
                    f"Failed to create shared checkout for '{repo_name}' at "
                    f"{revision}: {e.stderr or ''}",
                )
            logger.info(
                "Created shared extra-addons checkout %s/%s at %s",
                repo_name,
                revision,
                checkout_path,
            )

        # Handed out now: branch removal leaves recently used checkouts alone
        # while the caller's container does not exist yet.
        try:
            os.utime(checkout_path)
        except OSError:
            pass

        changed_files = _changed_files_between(
            bare_path, current_revision.lower(), revision
        )
        return {
            "name": repo_name,
            "branch": branch,
            "revision": revision,
            "path": checkout_path,
            "changed_files": changed_files,
        }


def _pull_extra_worktree_unlocked(
    team: TeamSettings, repo_name: str, branch: str, worktree_path: str
) -> tuple[str, list[str]]:
    """Fetch the bare repo and reset the worktree to the branch tip.

    Returns ``(old_head, changed_files)`` where *old_head* is the
    commit hash before the pull and *changed_files* are paths relative
    to the worktree root.  Returns ``("", [])`` if already up to date.
    """
    fetch_extra_repo(team, repo_name, branch=branch)

    old_head = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    try:
        subprocess.run(
            ["git", "-C", worktree_path, "reset", "--hard", branch],
            check=True,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git reset --hard", e.returncode, e.stderr or "")

    new_head = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    if old_head == new_head:
        return "", []

    result = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--name-only", f"{old_head}..{new_head}"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    changed = [f for f in result.stdout.strip().splitlines() if f]
    logger.info(
        "Updated worktree %s/%s: %d files changed",
        repo_name,
        branch,
        len(changed),
    )
    return old_head, changed


def pull_extra_worktree(
    team: TeamSettings, repo_name: str, branch: str, worktree_path: str
) -> tuple[str, list[str]]:
    """Update a mutable per-consumer worktree (currently production only)."""
    with _repo_operation_lock(team, repo_name):
        return _pull_extra_worktree_unlocked(team, repo_name, branch, worktree_path)


def resolve_main_addons_path(repo_path: str) -> str:
    """Container addons path for the main repo.

    Odoo scans addons_path non-recursively, so when a repo keeps its modules in
    a top-level ``addons/`` directory, point Odoo at ``/mnt/extra-addons/addons``
    instead of the repo root.
    """
    if os.path.isdir(os.path.join(repo_path, "addons")):
        return "/mnt/extra-addons/addons"
    return "/mnt/extra-addons"


def resolve_extra_addons_path(checkout_path: str, repo_name: str) -> str:
    """Container addons path for an extra-addons checkout.

    The extra-repo counterpart of :func:`resolve_main_addons_path`: the
    checkout is always mounted at ``/mnt/extra-addons-{name}``, but when the
    repo keeps its modules in a top-level ``addons/`` directory the addons_path
    entry must point at that subdirectory of the mount, not the mount root.
    An unknown/missing host path falls back to the mount root.
    """
    base = f"/mnt/extra-addons-{repo_name}"
    if checkout_path and os.path.isdir(os.path.join(checkout_path, "addons")):
        return f"{base}/addons"
    return base


def generate_odoo_conf(
    base_conf_path: str,
    output_path: str,
    extra_paths: list[str],
    main_addons_path: str = "/mnt/extra-addons",
    overrides: dict[str, str] | None = None,
) -> str:
    parser = configparser.RawConfigParser()
    # Preserve option case (Odoo config keys are case-sensitive); the default
    # optionxform lowercases them. Assigning to this method is the documented
    # configparser idiom but trips mypy's method-assignment check.
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    parser.read(base_conf_path)

    existing = parser.get("options", "addons_path", fallback="/mnt/extra-addons")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    # Repo keeps modules in addons/ → point at that subdir, not the repo root.
    parts = [main_addons_path if p == "/mnt/extra-addons" else p for p in parts]
    if main_addons_path not in parts:
        parts.insert(0, main_addons_path)
    for p in extra_paths:
        if p not in parts:
            parts.append(p)
    parser.set("options", "addons_path", ",".join(parts))

    # Applied after the merge: the production profile injects auto-tuned
    # worker/limit settings that must win over whatever the base conf says.
    for key, value in (overrides or {}).items():
        parser.set("options", key, value)

    # Strip DB connection keys — these are managed via container env vars
    # (HOST, USER, PASSWORD).  If left in the conf file the Odoo entrypoint
    # uses them instead of the env vars, breaking per-environment credentials.
    # Case-insensitive: Odoo lowercases option names on read, so a DB_HOST in
    # the conf would still override the env vars.
    for key in list(parser.options("options")):
        if key.lower() in DB_CONN_CONF_KEYS:
            parser.remove_option("options", key)

    with open(output_path, "w") as f:
        parser.write(f)

    logger.info(
        "Generated Odoo config at %s with extra paths: %s", output_path, extra_paths
    )
    return output_path
