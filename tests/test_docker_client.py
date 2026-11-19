"""Unit tests for docker_ops.client helpers that need no Docker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import client as docker_client


@pytest.fixture(autouse=True)
def _clean_uid_gid_cache():
    docker_client._uid_gid_cache.clear()
    yield
    docker_client._uid_gid_cache.clear()


class TestGetOdooUidGid:
    def test_successful_lookup_is_cached(self):
        with patch.object(
            docker_client, "run_for_output", return_value=b"uid=101(odoo) gid=102(odoo)"
        ) as run:
            assert docker_client.get_odoo_uid_gid(MagicMock(), "odoo:19") == "101:102"
            assert docker_client.get_odoo_uid_gid(MagicMock(), "odoo:19") == "101:102"
        run.assert_called_once()

    def test_transient_failure_falls_back_without_poisoning_the_cache(self):
        # A daemon hiccup on the first call must not pin 100:101 for the
        # image until the server restarts: the next call retries the real
        # lookup and later chowns use the correct uid.
        with patch.object(
            docker_client,
            "run_for_output",
            side_effect=[RuntimeError("daemon hiccup"), b"uid=101(odoo) gid=102(odoo)"],
        ) as run:
            assert docker_client.get_odoo_uid_gid(MagicMock(), "odoo:19") == "100:101"
            assert "odoo:19" not in docker_client._uid_gid_cache
            assert docker_client.get_odoo_uid_gid(MagicMock(), "odoo:19") == "101:102"
        assert run.call_count == 2
        assert docker_client._uid_gid_cache["odoo:19"] == "101:102"

    def test_unparseable_id_output_falls_back_without_caching(self):
        with patch.object(
            docker_client, "run_for_output", return_value=b"not an id output"
        ):
            assert docker_client.get_odoo_uid_gid(MagicMock(), "odoo:19") == "100:101"
        assert "odoo:19" not in docker_client._uid_gid_cache
