"""The integration fixtures must never act on a developer's own server.

``live_environment`` runs init_system/destroy_system against the local Docker
daemon. A Settings name left at its default (``oduflow-traefik``,
``oduflow-prod-db``, ...) would make those calls stop or remove the live
server's resources of the same name. Pure check, no Docker.

It covers the names Settings defines. Code must derive Docker names and the
managed/team labels from Settings rather than literals, or the per-run values
here cannot reach them.
"""

from __future__ import annotations

import dataclasses
import re

from oduflow.settings import Settings
from tests.conftest import _TEST_PREFIX, _test_settings

# Every Docker container, volume and network name in Settings, plus the
# prefix of per-environment names and the label that destroy_system and
# reconcile_prod_workloads use to find containers and networks. Only string
# settings are names; a flag or a size whose name happens to match is not.
_RESOURCE_FIELD = re.compile(r"(_container|_volume|_network)$|^(prefix|managed_label)$")


def _resource_fields() -> list[str]:
    return [
        field.name
        for field in dataclasses.fields(Settings)
        if _RESOURCE_FIELD.search(field.name) and isinstance(field.default, str)
    ]


def test_every_docker_resource_name_is_unique_to_the_test_run(tmp_path):
    settings, _ = _test_settings(str(tmp_path))
    suffix = settings.prefix[len(_TEST_PREFIX) : -1]
    assert suffix

    leaking = {
        name: getattr(settings, name)
        for name in _resource_fields()
        if not (
            getattr(settings, name).startswith(_TEST_PREFIX)
            and suffix in getattr(settings, name)
        )
    }

    assert not leaking, (
        "These test settings reuse a server's Docker resources; give them the "
        f"per-run suffix in conftest._test_settings: {leaking}"
    )


def test_resource_field_selection_covers_the_known_names():
    """Guard the pattern itself, so the check above cannot pass vacuously."""
    assert {
        "prefix",
        "managed_label",
        "shared_network",
        "shared_db_container",
        "shared_db_volume",
        "traefik_container",
        "traefik_acme_volume",
        "prod_db_container",
        "prod_db_volume",
    } <= set(_resource_fields())


def test_runs_in_different_directories_do_not_share_names(tmp_path):
    first, _ = _test_settings(str(tmp_path / "a"))
    second, _ = _test_settings(str(tmp_path / "b"))

    for name in _resource_fields():
        assert getattr(first, name) != getattr(second, name), name
