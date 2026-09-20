from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow import health
from oduflow.settings import BackupSettings, Settings, TeamSettings


@pytest.fixture(autouse=True)
def _clear_cache():
    health._cache["result"] = None
    health._cache["at"] = 0.0
    with patch(
        "oduflow.wal_monitor.status",
        return_value={"status": "ok", "detail": "WAL healthy"},
    ):
        yield
    health._cache["result"] = None


@pytest.fixture
def settings(tmp_path):
    team_dir = tmp_path / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(tmp_path),
        prod_enabled=True,
        routing_mode="traefik",
        acme_email="a@b.co",
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


def _client(running: dict[str, bool]):
    """Docker client mock: containers in `running` exist; exec is healthy."""
    client = MagicMock()

    def _get(name):
        if name not in running:
            raise docker.errors.NotFound("nf")
        container = MagicMock()
        container.status = "running" if running[name] else "exited"
        container.exec_run.return_value = (0, b"")
        return container

    client.containers.get.side_effect = _get
    client.containers.list.return_value = []
    return client


class TestCollectHealth:
    def test_walg_tls_error_degrades_even_when_host_s3_works(self, settings):
        settings = replace(
            settings, backup=BackupSettings(bucket="b", access_key="a", secret_key="s")
        )
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch("oduflow.s3_client.check_s3", return_value={"ok": True}),
            patch(
                "oduflow.wal_monitor.status",
                return_value={"status": "error", "detail": "TLS verification failed"},
            ) as probe,
        ):
            result = health.collect_health(settings, force=True)
        probe.assert_called_with(settings)
        assert result["checks"]["s3"]["status"] == "ok"
        assert result["checks"]["walg"]["status"] == "error"
        assert result["ok"] is False

    @pytest.mark.parametrize("pg_status", ["off", "error"])
    def test_no_walg_probe_when_pg_unavailable(self, settings, pg_status):
        settings = replace(
            settings, backup=BackupSettings(bucket="b", access_key="a", secret_key="s")
        )
        with patch("oduflow.walg.storage_status") as probe:
            result = health._check_walg(MagicMock(), settings, {"status": pg_status})
        assert result["status"] == pg_status
        probe.assert_not_called()

    def test_all_ok(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is True
        assert result["checks"]["dev_pg"]["status"] == "ok"
        assert result["checks"]["prod_pg"]["status"] == "ok"
        assert result["checks"]["traefik"]["status"] == "ok"
        # No [backup] section -> S3 check is off, not an error.
        assert result["checks"]["s3"]["status"] == "off"

    def test_missing_prod_pg_is_off_not_error(self, settings):
        client = _client({"oduflow-db": True, "oduflow-traefik": True})
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["prod_pg"]["status"] == "off"
        assert result["ok"] is True

    def test_stopped_dev_pg_degrades(self, settings):
        client = _client({"oduflow-db": False, "oduflow-traefik": True})
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["dev_pg"]["status"] == "error"
        assert result["ok"] is False

    def test_unhealthy_production_degrades(self, settings):
        from oduflow import production_registry

        production_registry.create_production(
            settings.teams["1"], "erp", {"unhealthy": True}
        )
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is False
        assert result["checks"]["productions"]["unhealthy"] == ["1/erp"]

    def test_s3_error_degrades(self, settings, tmp_path):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            prod_enabled=True,
            routing_mode="traefik",
            acme_email="a@b.co",
            backup=BackupSettings(bucket="b", access_key="a", secret_key="s"),
            teams=settings.teams,
        )
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch(
                "oduflow.s3_client.check_s3",
                return_value={"ok": False, "error": "denied"},
            ),
        ):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["s3"]["status"] == "error"
        assert result["ok"] is False

    def test_disabled_production_checks_are_off(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            routing_mode=settings.routing_mode,
            teams=settings.teams,
        )
        client = _client({"oduflow-db": True, "oduflow-traefik": True})
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is True
        assert result["checks"]["prod_pg"]["status"] == "off"
        assert result["checks"]["s3"]["status"] == "off"
        assert result["checks"]["productions"]["status"] == "off"

    def test_disabled_but_running_production_degrades(self, settings):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            routing_mode=settings.routing_mode,
            teams=settings.teams,
        )
        client = _client({"oduflow-db": True, "oduflow-traefik": True})
        running = MagicMock(name="production")
        running.name = "oduflow-1-prod-erp-odoo"
        running.status = "running"
        client.containers.list.return_value = [running]
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is False
        assert result["checks"]["productions"]["status"] == "error"
        assert result["checks"]["productions"]["running"] == [running.name]

    def test_disk_warn_does_not_degrade(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        fake_usage = MagicMock(total=100, used=90, free=10)
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch("shutil.disk_usage", return_value=fake_usage),
        ):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["disk"]["status"] == "warn"
        assert result["ok"] is True

    def test_cache(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch(
            "oduflow.docker_ops.client.get_client", return_value=client
        ) as getter:
            health.collect_health(settings, force=True)
            first_calls = getter.call_count
            health.collect_health(settings)  # cached
            assert getter.call_count == first_calls

    def test_expired_cache_is_refreshed(self, settings, monkeypatch):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        clock = {"now": 1000.0}
        monkeypatch.setattr(health.time, "monotonic", lambda: clock["now"])

        with patch(
            "oduflow.docker_ops.client.get_client", return_value=client
        ) as getter:
            health.collect_health(settings, force=True)
            calls_after_first = getter.call_count

            clock["now"] += health._CACHE_TTL_SECONDS - 0.1
            health.collect_health(settings)
            assert getter.call_count == calls_after_first  # still inside the TTL

            clock["now"] += 0.2  # now past the TTL
            health.collect_health(settings)
            assert getter.call_count > calls_after_first

    def test_cache_expires_exactly_at_the_ttl(self, settings, monkeypatch):
        # The check is `age < TTL`, so an age of exactly TTL is already stale.
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        clock = {"now": 1000.0}
        monkeypatch.setattr(health.time, "monotonic", lambda: clock["now"])

        with patch(
            "oduflow.docker_ops.client.get_client", return_value=client
        ) as getter:
            health.collect_health(settings, force=True)
            calls_after_first = getter.call_count

            clock["now"] += health._CACHE_TTL_SECONDS
            health.collect_health(settings)
            assert getter.call_count > calls_after_first

    def test_force_bypasses_a_fresh_cache(self, settings, monkeypatch):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        monkeypatch.setattr(health.time, "monotonic", lambda: 1000.0)

        with patch(
            "oduflow.docker_ops.client.get_client", return_value=client
        ) as getter:
            health.collect_health(settings, force=True)
            calls_after_first = getter.call_count
            health.collect_health(settings, force=True)
            assert getter.call_count > calls_after_first

    def test_empty_cache_is_never_served(self, settings, monkeypatch):
        # A zero timestamp with no stored result must not be mistaken for a
        # fresh entry: the very first call has to do real work.
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        monkeypatch.setattr(health.time, "monotonic", lambda: 0.0)

        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings)

        assert result["checks"]  # real checks ran, not an empty cached value


class TestDiskCheck:
    def _disk(self, settings, total, used, free):
        with patch(
            "shutil.disk_usage",
            return_value=MagicMock(total=total, used=used, free=free),
        ):
            return health._check_disk(settings)

    def test_warn_threshold_is_inclusive(self, settings):
        # Exactly DISK_WARN_PERCENT must already warn, one below must not.
        at_threshold = self._disk(settings, 100, health.DISK_WARN_PERCENT, 100)
        assert at_threshold["percent"] == health.DISK_WARN_PERCENT
        assert at_threshold["status"] == "warn"

        below = self._disk(settings, 100, health.DISK_WARN_PERCENT - 1, 100)
        assert below["status"] == "ok"

    def test_percent_is_used_over_total(self, settings):
        assert self._disk(settings, 200, 50, 150)["percent"] == 25

    def test_the_configured_data_dir_is_the_one_measured(self, settings):
        # Not the filesystem root: a data dir on a separate volume is exactly
        # the case where the two differ.
        with patch(
            "shutil.disk_usage", return_value=MagicMock(total=100, used=10, free=90)
        ) as disk_usage:
            health._check_disk(settings)

        disk_usage.assert_called_once_with(settings.base_data_dir)

    def test_root_is_measured_when_no_data_dir_is_configured(self):
        empty = Settings(base_data_dir="", teams={})
        with patch(
            "shutil.disk_usage", return_value=MagicMock(total=100, used=10, free=90)
        ) as disk_usage:
            health._check_disk(empty)

        disk_usage.assert_called_once_with("/")

    def test_free_space_is_reported_in_gib(self, settings):
        result = self._disk(settings, 100 * 1024**3, 25 * 1024**3, 75 * 1024**3)
        assert result["free_gb"] == 75.0

    def test_free_space_is_rounded_to_one_decimal(self, settings):
        # 1.25 GiB free -> 1.2 (banker's rounding), not 1.25 or 1.3.
        result = self._disk(settings, 10 * 1024**3, 0, int(1.25 * 1024**3))
        assert result["free_gb"] == 1.2

    def test_unreadable_path_reports_error_with_zero_percent(self, settings):
        with patch("shutil.disk_usage", side_effect=OSError("gone")):
            result = health._check_disk(settings)

        assert result["status"] == "error"
        assert result["percent"] == 0
        assert "gone" in result["detail"]


class TestOverlayCheck:
    """A stale overlay is invisible without this check: the environment's
    container is up and green while every filestore read inside it fails with
    ENOTCONN."""

    def test_no_overlays_is_ok(self, settings):
        assert health._check_overlays(settings)["status"] == "ok"

    def test_stale_overlay_is_an_error(self, settings):
        with patch(
            "oduflow.docker_ops.env_ops.overlay_mount_issues",
            return_value=[
                {
                    "team_id": "1",
                    "env_name": "wise-moth",
                    "state": "stale",
                    "path": "/srv/oduflow/team_1/workspaces/wise-moth/filestore",
                }
            ],
        ):
            result = health._check_overlays(settings)

        assert result["status"] == "error"
        assert result["count"] == 1
        assert result["affected"] == [
            {"team_id": "1", "env_name": "wise-moth", "state": "stale"}
        ]
        assert "wise-moth" in result["detail"]
        assert "/srv/oduflow" not in str(result)

    def test_scan_failure_is_reported_not_raised(self, settings):
        with patch(
            "oduflow.docker_ops.env_ops.overlay_mount_issues",
            side_effect=OSError("nope"),
        ):
            result = health._check_overlays(settings)

        assert result["status"] == "error"
        assert result["detail"] == "filestore overlay scan failed; check server logs"
        assert "nope" not in str(result)

    def test_stale_overlay_degrades_healthz(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch(
                "oduflow.docker_ops.env_ops.overlay_mount_issues",
                return_value=[
                    {
                        "team_id": "1",
                        "env_name": "prod",
                        "state": "stale",
                        "path": "/srv/oduflow/team_1/workspaces/prod/filestore",
                    }
                ],
            ),
        ):
            result = health.collect_health(settings, force=True)

        assert result["ok"] is False
        assert result["checks"]["overlays"]["status"] == "error"
