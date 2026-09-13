import logging
import os
import re
import subprocess
from typing import Any
from urllib.parse import ParseResult, urlparse

from oduflow.errors import ExternalCommandError, FlowError, NotFoundError
from oduflow.naming import redact_url_credentials, sanitize_repo_url

logger = logging.getLogger("oduflow")

# Base env for git operations (no credentials — just disables interactive prompts)
_GIT_BASE_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": os.environ.get("HOME", "/root"),
}


def git_env_for_team(cred_file: str) -> dict[str, str]:
    """Build a git environment dict with per-team credential helper."""
    return {
        **_GIT_BASE_ENV,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": f"store --file {cred_file}",
    }


class InvalidRepoURLError(FlowError):
    """Repository URL is invalid or missing credentials."""


class RepoAuthError(FlowError):
    """Repository authentication failed. Call setup_repo_auth first."""


def _credential_host(parsed: ParseResult) -> str:
    """Git credential-store host key, including an explicit port."""
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname


def validate_repo_url(repo_url: str) -> None:
    """Reject non-HTTPS repository URLs early.

    SSH URLs (``git@host:path`` or ``ssh://``) cause the server to hang
    because SSH prompts for host-key verification interactively.
    """
    # SCP-like SSH syntax: git@github.com:owner/repo.git
    if re.match(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+:", repo_url):
        raise InvalidRepoURLError(
            "SSH repository URLs are not supported. "
            "Use HTTPS format: https://github.com/owner/repo.git"
        )
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidRepoURLError(
            f"Only HTTPS repository URLs are supported (got {parsed.scheme or 'unknown'}://). "
            "Use format: https://github.com/owner/repo.git"
        )
    # SSRF guard: refuse loopback / link-local (cloud metadata) / unspecified
    # hosts. Internal RFC1918 git servers stay allowed (allow_private=True).
    from oduflow.url_safety import assert_allowed_host

    assert_allowed_host(parsed.hostname, allow_private=True)


def _parse_authenticated_url(repo_url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidRepoURLError(
            f"URL must use https:// scheme, got: {parsed.scheme}://"
        )
    if not parsed.username or not parsed.password:
        raise InvalidRepoURLError(
            "URL must contain credentials: https://user:PAT@github.com/owner/repo.git"
        )
    clean_url = sanitize_repo_url(repo_url)
    return clean_url, _credential_host(parsed), parsed.username, parsed.password


def _store_git_credentials(
    host: str, username: str, password: str, cred_file: str
) -> None:
    # Don't trust the process umask for the dir holding the plaintext PAT store.
    os.makedirs(os.path.dirname(cred_file), mode=0o700, exist_ok=True)
    env = git_env_for_team(cred_file)

    credential_input = (
        f"protocol=https\nhost={host}\nusername={username}\npassword={password}\n\n"
    )
    subprocess.run(
        ["git", "credential", "approve"],
        input=credential_input.encode(),
        check=True,
        capture_output=True,
        env=env,
    )
    # git's store helper defaults the file to 0600, but enforce it explicitly as
    # defense-in-depth (and to harden a pre-existing file created under a laxer umask).
    try:
        os.chmod(cred_file, 0o600)
    except OSError:
        pass
    # A few providers accept token-as-username URLs. Never log this field: the
    # caller cannot reliably distinguish an account name from a secret.
    logger.info("Git credentials stored for host=%s", host)


def extract_and_store_inline_credentials(
    repo_url: str, cred_file: str
) -> tuple[str, str]:
    """Move inline URL credentials into the git credential store.

    If *repo_url* embeds ``user:PAT@host`` credentials, store them in
    *cred_file* (the team credential store that clone/pull already
    authenticate against) and return ``(sanitized_url, username)``. Otherwise
    return ``(repo_url, "")`` unchanged.

    Purpose: keep the PAT out of the Docker ``oduflow.repo`` label, which is
    world-readable via ``docker inspect`` and copied verbatim into saved
    template metadata. This is a network-free move; SSRF at clone time is gated
    separately by ``validate_repo_url`` at the tool layer.
    """
    if not repo_url:
        return repo_url, ""
    try:
        parsed = urlparse(repo_url)
    except Exception:
        return repo_url, ""
    if not parsed.username and not parsed.password:
        return repo_url, ""
    host = _credential_host(parsed)
    if not host:
        return repo_url, ""
    _store_git_credentials(
        host, parsed.username or "", parsed.password or "", cred_file
    )
    # Surface the username as the (non-secret) account name only when a separate
    # password is present. For token-as-username forms (password empty, token in
    # the username slot) keep it out of the label and rely on host-based matching.
    label_user = parsed.username if (parsed.username and parsed.password) else ""
    return sanitize_repo_url(repo_url), label_user or ""


# Username stored alongside a bare token when the caller does not supply one.
# GitHub, GitLab and Azure DevOps accept any non-empty username with a PAT;
# this is GitHub's documented placeholder for token auth.
DEFAULT_TOKEN_USERNAME = "x-access-token"


def normalize_credential_host(raw_host: str) -> str:
    """Turn user input into a git credential-store host key.

    Accepts a bare hostname (``github.com``), a host with an explicit port
    (``git.example.com:8443``) or a full HTTPS URL, from which only the host
    (and port) is kept. Raises :class:`InvalidRepoURLError` for anything else.
    """
    raw = (raw_host or "").strip()
    if not raw:
        raise InvalidRepoURLError("Git host is required, e.g. github.com")
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
        host_key = _credential_host(parsed)  # ValueError for a non-numeric port
    except ValueError as e:
        raise InvalidRepoURLError(f"Invalid git host {raw!r}: {e}")
    if parsed.scheme not in ("https", "http") or not hostname:
        raise InvalidRepoURLError(
            f"Invalid git host {raw!r}: use a hostname such as github.com"
        )
    if parsed.username or parsed.password:
        raise InvalidRepoURLError(
            "Do not embed credentials in the host field; pass the token separately."
        )
    if "://" not in raw and parsed.path not in ("", "/"):
        raise InvalidRepoURLError(
            f"Invalid git host {raw!r}: give only the hostname, not a path"
        )
    return host_key


def _verify_repo_access(clean_url: str, cred_file: str) -> None:
    """Run ``git ls-remote`` with the team credential store; raise on failure."""
    env = git_env_for_team(cred_file)
    try:
        subprocess.run(
            ["git", "ls-remote", "--heads", clean_url],
            check=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = redact_url_credentials(
            e.stderr.decode("utf-8") if e.stderr else str(e)
        )
        raise ExternalCommandError(
            "git ls-remote (auth test)",
            e.returncode,
            f"Credentials saved but access check failed: {error_msg}",
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git ls-remote (auth test)",
            -1,
            "Access check timed out (30s).",
        )


def store_credential(
    host: str,
    token: str,
    cred_file: str,
    username: str = "",
    verify_repo_url: str = "",
) -> dict[str, str]:
    """Store a bare access token for *host* and verify it.

    This is the token-first counterpart of :func:`setup_repo_auth`: the caller
    pastes the PAT itself instead of composing a ``user:PAT@host`` URL. Git
    matches stored credentials by host and username only, so one entry covers
    every repository on that host; *username* defaults to
    :data:`DEFAULT_TOKEN_USERNAME` and only has to be a real account name for
    providers that require it (Bitbucket app passwords).

    Verification: when *verify_repo_url* is given, ``git ls-remote`` must
    succeed against it with the stored credential (the URL must live on
    *host*). Otherwise the token is checked against the provider's API for the
    hosts :func:`validate_credential` knows about; unknown hosts are stored
    as ``"unverified"``.

    Returns ``{"host", "username", "status"}`` where *status* is
    ``"authenticated"`` or ``"unverified"``.
    """
    host_key = normalize_credential_host(host)
    token = (token or "").strip()
    if not token:
        raise InvalidRepoURLError("Access token is required.")
    if "\n" in token or "\r" in token:
        raise InvalidRepoURLError("Access token must be a single line.")
    username = (username or "").strip() or DEFAULT_TOKEN_USERNAME
    if "\n" in username or "\r" in username or "@" in username or ":" in username:
        raise InvalidRepoURLError("Username must not contain ':', '@' or line breaks.")

    clean_verify_url = ""
    if verify_repo_url:
        validate_repo_url(verify_repo_url)
        clean_verify_url = sanitize_repo_url(verify_repo_url.strip())
        verify_host = _credential_host(urlparse(clean_verify_url))
        if verify_host != host_key:
            raise InvalidRepoURLError(
                f"Repository URL host {verify_host!r} does not match "
                f"credential host {host_key!r}."
            )

    # SSRF guard: same rationale as setup_repo_auth — never store (and later
    # send) a PAT for loopback / link-local / unspecified hosts.
    from oduflow.url_safety import assert_allowed_host

    assert_allowed_host(urlparse(f"https://{host_key}").hostname, allow_private=True)

    _store_git_credentials(host_key, username, token, cred_file)

    if clean_verify_url:
        _verify_repo_access(clean_verify_url, cred_file)
        status = "authenticated"
    else:
        api_status = _check_token_with_provider(host_key, username, token)
        if api_status == "invalid":
            # A definite 401/403 from the provider: the token itself is bad, so
            # do not leave it in the store (unlike a failed ls-remote, where the
            # repository URL rather than the token may be at fault).
            delete_credential(host_key, username, cred_file)
            raise ExternalCommandError(
                f"{host_key} API (auth test)",
                1,
                f"{host_key} rejected the token; nothing was saved.",
            )
        status = "authenticated" if api_status == "valid" else "unverified"

    logger.info("Credential stored for host=%s status=%s", host_key, status)
    return {"host": host_key, "username": username, "status": status}


def setup_repo_auth(repo_url: str, cred_file: str) -> dict[str, str]:
    clean_url, host, username, password = _parse_authenticated_url(repo_url)

    # SSRF guard (parity with validate_repo_url): refuse to store credentials for
    # — or run `git ls-remote` against — loopback / link-local (cloud metadata) /
    # unspecified hosts. Otherwise a caller could point this at an internal host
    # and exfiltrate the presented PAT (git sends the stored credential to that
    # host). Internal RFC1918 git servers stay allowed (allow_private=True).
    from oduflow.url_safety import assert_allowed_host

    assert_allowed_host(host, allow_private=True)

    _store_git_credentials(host, username, password, cred_file)
    _verify_repo_access(clean_url, cred_file)

    logger.info("Repo auth verified for %s", clean_url)
    return {"repo_url": clean_url, "host": host, "status": "authenticated"}


def inject_credential_user(repo_url: str, git_user: str) -> str:
    """Inject username into repo URL for credential matching."""
    if not git_user:
        return repo_url
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return repo_url
    if parsed.username:
        return repo_url
    netloc = f"{git_user}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def list_credentials(cred_file: str) -> list[dict[str, Any]]:
    if not os.path.exists(cred_file):
        return []

    results = []
    with open(cred_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = urlparse(line)
            except Exception:
                continue
            if not parsed.hostname or not parsed.username:
                continue
            results.append(
                {
                    "host": parsed.hostname,
                    "username": parsed.username,
                    "token_masked": (parsed.password or "")[:4] + "****"
                    if parsed.password and len(parsed.password) > 4
                    else "****",
                }
            )
    return results


# Providers whose API can confirm a token without a repository URL.
_PROVIDER_API_URLS = {
    "github.com": "https://api.github.com/user",
    "gitlab.com": "https://gitlab.com/api/v4/user",
    "bitbucket.org": "https://api.bitbucket.org/2.0/user",
}


def _check_token_with_provider(host: str, username: str, token: str) -> str:
    """Check *token* against the provider API; ``valid``/``invalid``/``unknown``.

    Hosts without a known API (see ``_PROVIDER_API_URLS``) return ``"unknown"``
    without any network call.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    api_url = _PROVIDER_API_URLS.get(host)
    if not api_url:
        return "unknown"

    try:
        req = Request(api_url)
        if host == "github.com":
            req.add_header("Authorization", f"token {token}")
        elif host == "gitlab.com":
            req.add_header("PRIVATE-TOKEN", token)
        elif host == "bitbucket.org":
            import base64

            b64 = base64.b64encode(f"{username}:{token}".encode()).decode()
            req.add_header("Authorization", f"Basic {b64}")
        req.add_header("User-Agent", "oduflow")
        resp = urlopen(req, timeout=10)
        return "valid" if resp.status == 200 else "invalid"
    except HTTPError as e:
        return "invalid" if e.code in (401, 403) else "unknown"
    except (URLError, OSError):
        return "unknown"


def validate_credential(host: str, username: str, cred_file: str) -> str:
    if not os.path.exists(cred_file):
        return "invalid"

    token = None
    with open(cred_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = urlparse(line)
            except Exception:
                continue
            if parsed.hostname == host and parsed.username == username:
                token = parsed.password
                break

    if not token:
        return "invalid"

    if host not in _PROVIDER_API_URLS:
        # No API to ask: the credential exists, report it as valid.
        return "valid"
    return _check_token_with_provider(host, username, token)


def delete_credential(host: str, username: str, cred_file: str) -> bool:
    if not os.path.exists(cred_file):
        return False

    with open(cred_file, "r") as f:
        lines = f.readlines()

    new_lines = []
    removed = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = urlparse(stripped)
        except Exception:
            new_lines.append(line)
            continue
        if parsed.hostname == host and parsed.username == username:
            removed = True
            continue
        new_lines.append(line)

    if removed:
        with open(cred_file, "w") as f:
            f.writelines(new_lines)
        logger.info("Credential deleted for host=%s user=%s", host, username)

    return removed


def pull_repo(
    repo_path: str, branch: str, cred_file: str = ""
) -> tuple[str, list[str]]:
    """Pull latest changes and return ``(old_head, changed_files)``.

    *old_head* is the commit hash before the pull — callers can pass it
    as ``base_ref`` to :func:`git_analysis.classify_changes` so that
    manifest / field comparisons cover the full range of pulled commits.
    """
    env = git_env_for_team(cred_file) if cred_file else _GIT_BASE_ENV

    old_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    try:
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "fetch",
                "--recurse-submodules=no",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        subprocess.run(
            ["git", "-C", repo_path, "reset", "--hard", f"origin/{branch}"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = redact_url_credentials(e.stderr or str(e))
        raise ExternalCommandError("git pull", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git pull", -1, "Fetch timed out (60s).")

    new_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    if old_head == new_head:
        return old_head, []

    result = subprocess.run(
        [
            "git",
            "-C",
            repo_path,
            "diff",
            "--name-only",
            f"{old_head}..{new_head}",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return old_head, [f for f in result.stdout.strip().splitlines() if f]


def fetch_branch(repo_path: str, branch: str, cred_file: str = "") -> str:
    """Fetch one branch from origin and return the commit it points at.

    The validate-before-mutate half of a branch switch: the working tree is not
    touched, so the common "the branch only exists on my machine" case fails
    here — with an actionable message — instead of half-way through a switch.
    """
    env = git_env_for_team(cred_file) if cred_file else _GIT_BASE_ENV

    try:
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "fetch",
                "--recurse-submodules=no",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = redact_url_credentials(e.stderr or str(e))
        # Git reports an absent branch as a plain fetch failure. Translate it:
        # "push the branch first" is a different action from "fix your remote".
        if "couldn't find remote ref" in error_msg or "not our ref" in error_msg:
            raise NotFoundError(
                f"Branch '{branch}' does not exist on origin. Push it first "
                f"(git push -u origin {branch}), then retry."
            )
        raise ExternalCommandError("git fetch", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git fetch", -1, "Fetch timed out (60s).")

    return rev_parse(repo_path, f"refs/remotes/origin/{branch}")


def checkout_branch(repo_path: str, branch: str) -> tuple[str, str, list[str]]:
    """Move the checkout onto *branch* at origin's already-fetched tip.

    Returns ``(old_head, new_head, changed_files)``. The file list is the *tree*
    diff between the two commits (``git diff A..B``), not a commit range: two
    branches need no shared history for the classifier to see what the running
    Odoo must catch up on, which is what keeps squash-merged branches sane.

    Local modifications are discarded, matching :func:`pull_repo` — the managed
    clone is Oduflow's, and the agent's own work lives in its own checkout.
    Requires the remote ref to be present; call :func:`fetch_branch` first.
    """
    old_head = rev_parse(repo_path, "HEAD")

    try:
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "checkout",
                "--force",
                "-B",
                branch,
                f"refs/remotes/origin/{branch}",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(
            "git checkout", e.returncode, redact_url_credentials(e.stderr or str(e))
        )

    new_head = rev_parse(repo_path, "HEAD")
    if old_head == new_head:
        return old_head, new_head, []
    return old_head, new_head, diff_names(repo_path, old_head, new_head)


def rev_parse(repo_path: str, ref: str = "HEAD") -> str:
    """Commit hash of *ref* in *repo_path*."""
    try:
        return subprocess.run(
            ["git", "-C", repo_path, "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git rev-parse", e.returncode, e.stderr or "")


def is_git_repository(repo_path: str) -> bool:
    """Whether *repo_path* is a Git working tree.

    Ask Git instead of inspecting ``.git``: linked worktrees store ``.git`` as
    a file that points at the common repository, while ordinary clones use a
    directory.
    """
    if not os.path.isdir(repo_path):
        return False
    result = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        env=_GIT_BASE_ENV,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def commit_exists(repo_path: str, commit: str) -> bool:
    """True when *commit* is present in *repo_path*'s object database.

    A template's snapshot commit can be absent for perfectly ordinary reasons —
    the branch it was taken from was deleted, the clone is shallow, the template
    came from a different repository. Callers treat "absent" as "no lineage
    information", never as an error.
    """
    if not commit:
        return False
    return (
        subprocess.run(
            ["git", "-C", repo_path, "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        ).returncode
        == 0
    )


def ensure_lineage_history(
    repo_path: str,
    commit: str,
    current_branch: str,
    source_branch: str = "",
    cred_file: str = "",
) -> bool:
    """Best-effort fetch of history needed to compare *commit* with ``HEAD``.

    Development environments are cloned with ``--depth 1``. Deepen the current
    branch first so ancestry is reliable, then fetch the template's source
    branch when it differs. Returns whether *commit* is available afterwards;
    callers keep lineage advisory when a branch was deleted or a fetch fails.
    """
    if not commit or not is_git_repository(repo_path):
        return False
    if commit_exists(repo_path, commit):
        return True

    env = git_env_for_team(cred_file) if cred_file else _GIT_BASE_ENV
    try:
        shallow = (
            subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "--is-shallow-repository"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()
            == "true"
        )

        if current_branch:
            cmd = [
                "git",
                "-C",
                repo_path,
                "fetch",
                "--recurse-submodules=no",
            ]
            if shallow:
                cmd.append("--unshallow")
            cmd.extend(
                [
                    "origin",
                    f"+refs/heads/{current_branch}:refs/remotes/origin/{current_branch}",
                ]
            )
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

        if source_branch and source_branch != current_branch:
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo_path,
                    "fetch",
                    "--recurse-submodules=no",
                    "origin",
                    f"+refs/heads/{source_branch}:refs/remotes/origin/{source_branch}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.info("Could not fetch template lineage history: %s", type(exc).__name__)

    return commit_exists(repo_path, commit)


def is_ancestor(repo_path: str, ancestor: str, descendant: str = "HEAD") -> bool:
    """True when *ancestor* is reachable from *descendant*.

    Thin wrapper over ``git merge-base --is-ancestor``.
    """
    return (
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        ).returncode
        == 0
    )


def diff_names(repo_path: str, base: str, head: str = "HEAD") -> list[str]:
    """Files changed between *base* and *head* (``git diff --name-only base..head``)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "diff", "--name-only", f"{base}..{head}"],
            check=True,
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git diff", e.returncode, e.stderr or "")
    return [f for f in out.strip().splitlines() if f]


def reset_hard(repo_path: str, ref: str) -> None:
    """``git reset --hard <ref>`` — the code-rollback primitive."""
    try:
        subprocess.run(
            ["git", "-C", repo_path, "reset", "--hard", ref],
            check=True,
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git reset --hard", e.returncode, e.stderr or "")


def log_commits(repo_path: str, n: int = 20) -> list[dict[str, str]]:
    """Recent commits of the checkout, newest first.

    Returns ``[{sha, short, subject, author, date}]``; used to display a
    production's branch history and pick rollback targets.
    """
    sep = "\x1f"
    fmt = sep.join(["%H", "%h", "%s", "%an", "%cI"])
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "log", f"-{n}", f"--pretty=format:{fmt}"],
            check=True,
            capture_output=True,
            text=True,
            env=_GIT_BASE_ENV,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git log", e.returncode, e.stderr or "")
    commits = []
    for line in out.splitlines():
        parts = line.split(sep)
        if len(parts) == 5:
            commits.append(
                {
                    "sha": parts[0],
                    "short": parts[1],
                    "subject": parts[2],
                    "author": parts[3],
                    "date": parts[4],
                }
            )
    return commits


def parse_manifest(manifest_path: str) -> dict[str, Any]:
    """Parse an Odoo __manifest__.py file and return its dict."""
    import ast

    with open(manifest_path, "r") as f:
        manifest: dict[str, Any] = ast.literal_eval(f.read())
        return manifest
