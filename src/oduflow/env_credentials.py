import json
import logging
import os
import secrets

from oduflow.errors import PrerequisiteNotMetError
from oduflow.fsutil import atomic_write_private_json
from oduflow.naming import get_workspace_path, slugify_branch

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


def create_credentials(
    env_name: str, team_id: str, workspaces_dir: str
) -> dict[str, str]:
    username = generate_pg_username(env_name, team_id)
    password = generate_pg_password()
    creds = {"pg_user": username, "pg_password": password}

    workspace_path = get_workspace_path(env_name, workspaces_dir)
    os.makedirs(workspace_path, exist_ok=True)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    # Atomic write with restrictive permissions: the file holds a plaintext PG
    # password, and a crash mid-write must not leave a half-written file.
    atomic_write_private_json(creds_path, creds)

    logger.info(
        "Created PG credentials for environment '%s' (user=%s)", env_name, username
    )
    return creds


class MissingCredentialsError(PrerequisiteNotMetError):
    """No per-environment PostgreSQL credentials exist and fallback is disallowed."""


def load_credentials(
    env_name: str,
    workspaces_dir: str,
    fallback_user: str,
    fallback_password: str,
    *,
    allow_fallback: bool = True,
) -> dict[str, str]:
    """Load an environment's scoped PostgreSQL credentials.

    When the per-environment credentials file is missing, callers that perform
    *arbitrary* SQL/shell against the environment (interactive psql, run_db_query,
    run_odoo_shell) must pass ``allow_fallback=False``: the fallback is the
    cluster **superuser**, and handing it to those surfaces would let a tenant
    ``\\c`` into another team's database or ``COPY ... FROM PROGRAM`` for RCE.
    Fixed, admin-time operations keep the fallback so legacy environments still
    work.
    """
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        with open(creds_path) as f:
            creds: dict[str, str] = json.load(f)
            return creds
    if not allow_fallback:
        raise MissingCredentialsError(
            f"Environment '{env_name}' has no scoped database credentials. "
            "Recreate or update the environment to provision a per-environment "
            "PostgreSQL role (the shared superuser is never used for interactive "
            "or arbitrary queries)."
        )
    return {"pg_user": fallback_user, "pg_password": fallback_password}


def delete_credentials(env_name: str, workspaces_dir: str) -> None:
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        os.remove(creds_path)
        logger.info("Deleted PG credentials for environment '%s'", env_name)
