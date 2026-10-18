"""Server-side OCA batch import.

The dashboard submits one batch and only polls its progress: repositories are
registered (and their selected branches downloaded) in a background thread, so a
reload, a closed tab or a proxy timeout cannot lose the queue or misreport a
download that is still running. Only the latest job per team
is kept, in memory. After a server restart the job is gone; repositories that
were already cloned are recognised by origin when the catalogue is reopened.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from oduflow import extra_addons, oca_catalog
from oduflow.errors import BusyError, FlowError
from oduflow.settings import TeamSettings

logger = logging.getLogger("oduflow")

MAX_REPOS = 300
_WORKERS = 3

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OcaImportItem:
    name: str
    extra_name: str
    repo_url: str
    # Downloaded right away; empty means every branch downloads on first use.
    branches: list[str]
    # queued | adding | done | error | skipped
    status: str = "queued"
    error: str = ""


@dataclass
class OcaImportJob:
    id: str
    items: list[OcaImportItem]
    status: str = STATUS_RUNNING
    created_at: str = field(default_factory=_now)
    finished_at: str = ""


# Latest job per team id. Guards job and item fields, which workers update.
_guard = threading.Lock()
_jobs: dict[str, OcaImportJob] = {}


def get_job(team: TeamSettings) -> dict[str, Any] | None:
    with _guard:
        job = _jobs.get(team.team_id)
        return asdict(job) if job else None


def start_import(
    team: TeamSettings,
    selections: object,
    public_error: Callable[[Exception], str],
) -> dict[str, Any]:
    """Validate a batch against the catalogue and start adding it.

    Clone URLs and import names come from the catalogue, never from the
    caller, so this endpoint can only reach github.com/OCA.
    """
    _raise_if_running(team)
    job = OcaImportJob(id=f"oca-{secrets.token_hex(6)}", items=_plan(team, selections))
    with _guard:
        _raise_if_running(team)
        _jobs[team.team_id] = job
        snapshot = asdict(job)
    threading.Thread(
        target=_run,
        args=(team, job, public_error),
        name=f"oduflow-oca-import-{team.team_id}",
        daemon=True,
    ).start()
    return snapshot


def _raise_if_running(team: TeamSettings) -> None:
    job = _jobs.get(team.team_id)
    if job is not None and job.status == STATUS_RUNNING:
        raise BusyError("An OCA import is already running for this team.")


def _plan(team: TeamSettings, selections: object) -> list[OcaImportItem]:
    if not isinstance(selections, list) or not selections:
        raise ValueError("Select at least one repository.")
    if len(selections) > MAX_REPOS:
        raise ValueError(f"At most {MAX_REPOS} repositories per import.")
    catalogue = {
        repo["name"]: repo for repo in oca_catalog.list_oca_repositories()["repos"]
    }
    items: list[OcaImportItem] = []
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError("Each repository must be an object.")
        name = selection.get("name")
        repo = catalogue.get(name) if isinstance(name, str) else None
        if repo is None:
            raise ValueError(f"{name!r} is not in the OCA catalogue.")
        if any(item.name == repo["name"] for item in items):
            raise ValueError(f"{repo['name']} is selected twice.")
        raw = selection.get("branches") or []
        if not isinstance(raw, list) or any(not isinstance(b, str) for b in raw):
            raise ValueError("branches must be a list of names.")
        branches = list(dict.fromkeys(b.strip() for b in raw if b.strip()))
        items.append(
            OcaImportItem(
                name=repo["name"],
                extra_name=repo["extra_name"],
                repo_url=repo["repo_url"],
                branches=branches,
            )
        )
    rows = oca_catalog.annotate_existing(
        [catalogue[item.name] for item in items],
        extra_addons.list_extra_repos(team),
    )
    for item, row in zip(items, rows):
        if row["existing_as"]:
            item.status, item.error = (
                "skipped",
                f"Already added as {row['existing_as']}",
            )
        elif row["name_taken"]:
            item.status, item.error = "error", f"Name already in use: {item.extra_name}"
    return items


def _run(
    team: TeamSettings, job: OcaImportJob, public_error: Callable[[Exception], str]
) -> None:
    queued = [item for item in job.items if item.status == "queued"]
    try:
        with ThreadPoolExecutor(
            max_workers=_WORKERS, thread_name_prefix="oduflow-oca-add"
        ) as pool:
            list(pool.map(lambda item: _add(team, item, public_error), queued))
    finally:
        with _guard:
            job.status = STATUS_FINISHED
            job.finished_at = _now()
        logger.info("OCA import %s finished for team %s", job.id, team.team_id)


def _add(
    team: TeamSettings, item: OcaImportItem, public_error: Callable[[Exception], str]
) -> None:
    with _guard:
        item.status = "adding"
    try:
        extra_addons.register_extra_repo(
            team, item.extra_name, item.repo_url, branches=item.branches
        )
    except (FlowError, ValueError) as exc:
        status, error = "error", public_error(exc)
    except Exception:
        logger.exception("OCA import of '%s' failed", item.name)
        status, error = "error", "Internal error."
    else:
        status, error = "done", ""
    with _guard:
        item.status, item.error = status, error
