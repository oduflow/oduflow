"""Preview and remove unused images across the local Docker daemon.

Images are shared by every team, so "unused" is judged server-wide. An image is
kept when any container (any team, running or stopped, managed or not) uses
it, when it has child images, when it is an image build's staging candidate,
or when an Oduflow operation requested it recently (see
:func:`note_image_requested`).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import docker
from oduflow.docker_ops.build_ops import STAGING_REPOSITORY_PREFIX
from oduflow.docker_ops.client import get_client

logger = logging.getLogger("oduflow")

# Pulling an image and creating its container is not atomic: an operation can
# fall back to a local copy (private or offline registry) and create the
# container much later, after a template restore. Recently requested images are
# kept so a cleanup cannot remove a local-only copy that is about to be used.
_REQUEST_LEASE_SECONDS = 6 * 3600
_requested: dict[str, float] = {}
# Also held across each image's final check and removal, so an operation that
# notes its image during that window waits for the removal to finish first.
_requested_guard = threading.Lock()


def note_image_requested(ref: str) -> None:
    """Keep ``ref`` out of image cleanup before pulling or running it."""
    if not ref:
        return
    with _requested_guard:
        _requested[ref] = time.monotonic()


def _requested_image_ids(client: Any) -> set[str]:
    """IDs of recently requested refs. The caller holds ``_requested_guard``."""
    cutoff = time.monotonic() - _REQUEST_LEASE_SECONDS
    for ref in [ref for ref, at in _requested.items() if at < cutoff]:
        del _requested[ref]
    ids: set[str] = set()
    for ref in _requested:
        try:
            ids.add(client.images.get(ref).id)
        except docker.errors.NotFound:
            pass  # Not present locally: there is no copy to protect.
        except docker.errors.APIError as exc:
            if not exc.is_client_error():
                raise
    return ids


def _used_image_ids(client: Any) -> set[str]:
    # Sparse listing reads image IDs from one API call; inspecting each
    # container would fail the whole scan when one is removed mid-listing.
    return {
        str(c.attrs.get("ImageID", ""))
        for c in client.containers.list(all=True, sparse=True)
    }


def _tags(summary: dict[str, Any]) -> list[str]:
    return [tag for tag in summary.get("RepoTags") or [] if tag != "<none>:<none>"]


def _is_staging(tags: list[str]) -> bool:
    return any(tag.startswith(STAGING_REPOSITORY_PREFIX) for tag in tags)


def _image_summaries(client: Any) -> tuple[list[dict[str, Any]], set[str]]:
    """All images and the IDs that are another image's parent."""
    summaries = client.api.images(all=True)
    parents = {str(s.get("ParentId") or "") for s in summaries} - {""}
    return summaries, parents


def list_unused_images() -> list[dict[str, Any]]:
    client = get_client()
    used = _used_image_ids(client)
    summaries, parents = _image_summaries(client)
    with _requested_guard:
        requested = _requested_image_ids(client)
    # Docker cannot remove an image while a child image exists, which also
    # rules out the intermediate layers of classic builds.
    keep = used | parents | requested
    return sorted(
        (
            {
                "id": summary["Id"],
                "tags": _tags(summary),
                "size_bytes": summary.get("Size", 0),
            }
            for summary in summaries
            if summary["Id"] not in keep and not _is_staging(_tags(summary))
        ),
        key=lambda image: (image["tags"] or [image["id"]])[0],
    )


def _keep_reason(
    client: Any, image: Any, used: set[str], parents: set[str]
) -> str | None:
    if image.id in used:
        return "Now used by a container."
    if image.id in parents:
        return "Now has child images."
    if _is_staging(image.tags):
        return "Image build staging image."
    if image.id in _requested_image_ids(client):
        return "Recently requested by an Oduflow operation."
    return None


def remove_unused_images(image_ids: list[str]) -> dict[str, Any]:
    """Remove only reviewed IDs that are still unused.

    Never force removal or implicitly prune parents outside the reviewed list.
    Docker's own conflict check covers containers created after the listing.
    """
    client = get_client()
    used = _used_image_ids(client)
    _, parents = _image_summaries(client)
    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for image_id in dict.fromkeys(image_ids):
        try:
            with _requested_guard:
                image = client.images.get(image_id)
                reason = _keep_reason(client, image, used, parents)
                if reason:
                    skipped.append({"id": image_id, "reason": reason})
                    continue
                # Docker refuses to remove an ID that several tags reference,
                # so drop the extra tags first; removing the ID then checks
                # for containers and deletes the image.
                for tag in image.tags[1:]:
                    client.images.remove(tag, force=False, noprune=True)
                client.images.remove(image_id, force=False, noprune=True)
            removed.append(image_id)
        except docker.errors.ImageNotFound:
            skipped.append({"id": image_id, "reason": "Already removed."})
        except docker.errors.APIError as exc:
            logger.warning("Failed to remove image %s: %s", image_id, exc)
            errors.append(
                {
                    "id": image_id,
                    "reason": (
                        "Docker refused removal: image is in use or has dependent images."
                        if exc.status_code == 409
                        else "Docker could not remove the image. Check server logs."
                    ),
                }
            )
    return {"removed": removed, "skipped": skipped, "errors": errors}
