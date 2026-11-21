import json
import logging
import os
import secrets

from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import PROD_ENV_PREFIX, get_workspace_path, slugify_branch

logger = logging.getLogger("oduflow")

_CREDENTIALS_FILE = "env_credentials.json"


def generate_pg_username(env_name: str, team_id: str) -> str:
    slug = slugify_branch(env_name)
    username = f"u_{team_id}_{slug}"
    return username[:63]


def generate_pg_password() -> str:
    while True:
        password = secrets.token_urlsafe(18)
        # The official Odoo entrypoint passes this value as a separate
        # ``--db_password`` argument. A leading dash is parsed as another CLI
        # option, leaving --db_password without a value and crash-looping the
        # container. Reject that rare token shape while keeping the same length
        # and entropy for accepted passwords.
        if not password.startswith("-"):
            return password


def new_credentials(env_name: str, team_id: str) -> dict[str, str]:
    """A role name and a fresh password for *env_name*, not yet on disk."""
    return {
        "pg_user": generate_pg_username(env_name, team_id),
        "pg_password": generate_pg_password(),
    }


def save_credentials(env_name: str, workspaces_dir: str, creds: dict[str, str]) -> None:
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    os.makedirs(workspace_path, exist_ok=True)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    # Atomic write with restrictive permissions: the file holds a plaintext PG
    # password, and a crash mid-write must not leave a half-written file.
    atomic_write_private_json(creds_path, creds)


def create_credentials(
    env_name: str, team_id: str, workspaces_dir: str
) -> dict[str, str]:
    creds = new_credentials(env_name, team_id)
    save_credentials(env_name, workspaces_dir, creds)
    logger.info(
        "Created PG credentials for environment '%s' (user=%s)",
        env_name,
        creds["pg_user"],
    )
    return creds


class MissingCredentialsError(PrerequisiteNotMetError):
    """No per-environment PostgreSQL credentials exist and fallback is disallowed."""


def missing_credentials_message(env_name: str) -> str:
    """What to tell the caller about an environment or production without a role.

    Recreating the container is the remedy for both: it is the step that
    provisions the role (system_ops.provision_scoped_role), and it keeps the
    database.
    """
    if env_name.startswith(PROD_ENV_PREFIX):
        return (
            f"Production '{env_name[len(PROD_ENV_PREFIX) :]}' has no scoped "
            "database credentials. Run reconfigure_production (no settings "
            "need to change) to give it a PostgreSQL role of its own: the "
            "container is recreated, the database and filestore are kept (the "
            "cluster superuser is never used in the role's place)."
        )
    return (
        f"Environment '{env_name}' has no scoped database credentials. "
        "Run update_environment to give it a PostgreSQL role of its own: "
        "the container is recreated, the database and filestore are kept "
        "(the cluster superuser is never used in the role's place)."
    )


def load_credentials(
    env_name: str,
    workspaces_dir: str,
    fallback_user: str,
    *,
    allow_fallback: bool = True,
) -> dict[str, str]:
    """Load an environment's scoped PostgreSQL credentials.

    When the per-environment credentials file is missing, the fallback names
    the cluster **superuser** and carries no password: there is nothing here
    to connect with, and so nothing that could end up in a container whose
    tenant is root in it. It is left for callers that only *name* the role —
    to drop or report it, or to notice that there is none and provision one
    (system_ops.provision_scoped_role). Whatever runs SQL in the environment's
    database passes ``allow_fallback=False`` and gets an error instead: the
    superuser's local socket needs no password, and even a fixed query
    resolves the tenant's own tables, functions and operators with the rights
    of the role that runs it.
    """
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        with open(creds_path) as f:
            creds: dict[str, str] = json.load(f)
            return creds
    if not allow_fallback:
        raise MissingCredentialsError(missing_credentials_message(env_name))
    return {"pg_user": fallback_user, "pg_password": ""}


def recorded_roles(workspaces_dir: str) -> dict[str, str]:
    """``{workspace directory name: role}`` for every workspace with credentials.

    The directory name stands in for the environment name: it differs only in
    ``/`` being ``-``, which every name derived from it (the database, the
    role) folds the same way. Unreadable files are skipped.
    """
    roles: dict[str, str] = {}
    try:
        entries = os.listdir(workspaces_dir)
    except OSError:
        return roles
    for entry in entries:
        try:
            with open(os.path.join(workspaces_dir, entry, _CREDENTIALS_FILE)) as f:
                role = json.load(f).get("pg_user")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(role, str) and role:
            roles[entry] = role
    return roles


def delete_credentials(env_name: str, workspaces_dir: str) -> None:
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        os.remove(creds_path)
        logger.info("Deleted PG credentials for environment '%s'", env_name)
