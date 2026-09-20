import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oduflow import wal_monitor, walg
from oduflow.docker_ops import production_ops, system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import DEFAULT_PROD_POSTGRES_IMAGE, BackupSettings, Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        base_data_dir=str(tmp_path),
        backup=BackupSettings(bucket="test", access_key="a", secret_key="s"),
    )


@pytest.fixture(autouse=True)
def reset_registries():
    for registry in (
        wal_monitor._monitors,
        wal_monitor._emergency,
        wal_monitor._policies,
        walg._readiness,
    ):
        registry.clear()
    yield
    for registry in (
        wal_monitor._monitors,
        wal_monitor._emergency,
        wal_monitor._policies,
        walg._readiness,
    ):
        registry.clear()


def healthy_sample(**overrides):
    return {
        "status": "ok",
        "sampled_at": time.time(),
        "no_progress_seconds": 0,
        "storage": {"status": "ok"},
        "archiver": {
            "archive_mode": "on",
            "archive_command": walg.archive_command(True),
        },
        **overrides,
    }


@pytest.fixture
def monitor():
    with (
        patch.object(wal_monitor.Monitor, "tick"),
        patch.object(wal_monitor, "cancel_attempt"),
    ):
        yield


def test_preflight_runs_as_postgres_bounded_without_credentials_in_argv(settings):
    client = MagicMock()
    pg = client.containers.get.return_value
    pg.exec_run.return_value = (0, b"")
    walg.storage_preflight(client, settings)
    args, kwargs = pg.exec_run.call_args
    assert args[0][:4] == ["timeout", "--kill-after=7s", "30s", "sh"]
    assert kwargs["user"] == "postgres"
    assert kwargs["environment"] == {"WALG_S3_MAX_RETRIES": "0"}
    assert len(args[0][-1]) == 32


@pytest.mark.parametrize(
    "code,output,match",
    [
        (
            1,
            b"x509: certificate signed by unknown authority secret-data",
            "TLS certificate",
        ),
        (1, b"AccessDenied secret-data", "access denied"),
        (124, b"secret-data", "timed out"),
        (1, b"preflight content mismatch", "did not match"),
        (1, b"preflight prerequisites missing", "prerequisites missing"),
    ],
)
def test_preflight_failure_is_safe_for_ui(settings, code, output, match):
    client = MagicMock()
    client.containers.get.return_value.exec_run.return_value = (code, output)
    with pytest.raises(PrerequisiteNotMetError, match=match) as error:
        walg.storage_preflight(client, settings)
    assert "secret-data" not in str(error.value)


def test_failed_preflight_preserves_legacy_archive_and_never_enables_noop(
    settings, monitor
):
    legacy = '/opt/oduflow-bin/wal-g --config /etc/walg/walg.json wal-push "%p"'
    with (
        patch.object(walg, "_pg_probe", return_value=legacy),
        patch.object(
            walg,
            "storage_preflight",
            side_effect=PrerequisiteNotMetError("TLS failure"),
        ),
        patch.object(walg, "_set_archive_command") as change,
    ):
        with pytest.raises(PrerequisiteNotMetError, match="TLS failure"):
            walg.prepare_archiving(MagicMock(), settings)
    change.assert_not_called()
    assert walg.readiness_status(settings)["status"] == "error"
    assert wal_monitor.status(settings)["detail"] == "TLS failure"


def test_old_noop_is_made_retaining_before_failed_preflight(settings, monitor):
    events = []

    def preflight(*args):
        events.append("preflight")
        raise PrerequisiteNotMetError("denied")

    with (
        patch.object(walg, "_pg_probe", return_value="/bin/true"),
        patch.object(
            walg,
            "_set_archive_command",
            side_effect=lambda c, s, command: events.append(command),
        ),
        patch.object(walg, "storage_preflight", side_effect=preflight),
    ):
        with pytest.raises(PrerequisiteNotMetError):
            walg.prepare_archiving(MagicMock(), settings)
    assert events == ["", "preflight"]


def test_verified_segment_required_after_storage_preflight(settings, monitor):
    segment = "00000001000000000000000A"
    events = []
    queries = []
    replies = iter(
        ["", walg.archive_command(True), "on", "0/A000008", segment, "f", "t"]
    )

    def sql(c, s, query):
        queries.append(query)
        return next(replies)

    with (
        patch.object(walg, "_pg_probe", side_effect=sql),
        patch.object(
            walg, "storage_preflight", side_effect=lambda *a: events.append("preflight")
        ),
        patch.object(
            walg,
            "apply_archive_command",
            side_effect=lambda *a, **kw: events.append("activate"),
        ),
        patch.object(walg.time, "sleep"),
    ):
        walg.prepare_archiving(MagicMock(), settings)
    assert events == ["preflight", "activate"]
    assert any("pg_create_restore_point" in q for q in queries)
    assert sum(segment + ".done" in q for q in queries) == 2
    assert walg.readiness_status(settings)["status"] == "ok"


def test_missing_archive_confirmation_blocks_start_even_when_s3_works(
    settings, monitor
):
    replies = [
        "",
        walg.archive_command(True),
        "on",
        "lsn",
        "00000001000000000000000A",
        "f",
    ]
    with (
        patch.object(walg, "_pg_probe", side_effect=replies),
        patch.object(walg, "storage_preflight"),
        patch.object(walg, "apply_archive_command"),
        patch.object(walg.time, "monotonic", side_effect=[0, 0, 999]),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="not archived"):
            walg.prepare_archiving(MagicMock(), settings)
    assert walg.readiness_status(settings)["status"] == "error"


def test_latch_blocks_preflight_before_generating_wal(settings):
    wal_monitor._save(settings, {"version": 1, "latched": True})
    with patch.object(walg, "_pg_probe") as sql:
        with pytest.raises(PrerequisiteNotMetError, match="protection"):
            walg.prepare_archiving(MagicMock(), settings)
    sql.assert_not_called()


def test_provisioning_propagates_preflight_failure(settings):
    client = MagicMock()
    with (
        patch.object(walg, "ensure_walg"),
        patch.object(walg, "write_walg_config"),
        patch.object(walg, "apply_walg_config_ownership"),
        patch.object(system_ops, "_ensure_prod_pg_container"),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_reconcile_pg_hba"),
        patch.object(
            walg,
            "prepare_archiving",
            side_effect=PrerequisiteNotMetError("upload denied"),
        ),
        patch.object(walg, "apply_archive_command") as command,
    ):
        with pytest.raises(PrerequisiteNotMetError, match="upload denied"):
            system_ops.ensure_prod_infra(client, settings, force=True)
    command.assert_not_called()


def test_production_image_matches_build_and_preserves_custom_major():
    dockerfile = (Path(__file__).parents[1] / "docker/postgres/Dockerfile").read_text()
    assert (
        "ARG POSTGRES_IMAGE_VERSION=" + DEFAULT_PROD_POSTGRES_IMAGE.split(":")[1]
        in dockerfile
    )
    assert Settings().production_pg_image == DEFAULT_PROD_POSTGRES_IMAGE
    assert Settings(postgres_image="postgres:17").production_pg_image == "postgres:17"
    assert Settings(prod_postgres_image="custom:15").production_pg_image == "custom:15"


@pytest.mark.parametrize("operation", ["start_production", "restart_production"])
def test_failed_admission_never_starts_or_restarts_application(settings, operation):
    from oduflow.settings import TeamSettings

    client = MagicMock()
    with (
        patch("oduflow.production_registry.get_production"),
        patch.object(production_ops, "get_client", return_value=client),
        patch.object(
            production_ops,
            "ensure_prod_infra",
            side_effect=PrerequisiteNotMetError("write denied"),
        ),
        patch.object(production_ops, "_require_container") as app,
    ):
        with pytest.raises(PrerequisiteNotMetError, match="write denied"):
            getattr(production_ops, operation)(
                settings, TeamSettings(team_id="1"), "erp"
            )
    app.assert_not_called()


def test_live_evidence_skips_the_forced_verification_segment(settings, monitor):
    # A restart of an already-admitted production must not wait minutes for a
    # fresh S3 round trip when the current sample already proves archiving works.
    wal_monitor._monitor(settings).latest = healthy_sample()
    with (
        patch.object(walg, "_pg_probe") as sql,
        patch.object(walg, "storage_preflight") as preflight,
        patch.object(walg, "apply_archive_command") as activate,
    ):
        walg.prepare_archiving(MagicMock(), settings, accept_live_evidence=True)
    sql.assert_not_called()
    preflight.assert_not_called()
    activate.assert_not_called()
    assert walg.readiness_status(settings)["status"] == "ok"


@pytest.mark.parametrize(
    "broken",
    [
        {"status": "warn"},
        {"storage": {"status": "error", "detail": "probe failed"}},
        {"no_progress_seconds": 10_000},
        {"archiver": {"archive_mode": "off", "archive_command": "wal-archive.sh"}},
        {"archiver": {"archive_mode": "on", "archive_command": "/bin/true"}},
        {"sampled_at": time.time() - 10 * wal_monitor.STALE_AFTER},
        {},  # no sample at all
    ],
)
def test_without_live_evidence_the_full_verification_still_runs(
    settings, monitor, broken
):
    wal_monitor._monitor(settings).latest = (
        {**healthy_sample(), **broken} if broken else {}
    )
    with (
        patch.object(walg, "_pg_probe", return_value=""),
        patch.object(
            walg, "storage_preflight", side_effect=PrerequisiteNotMetError("denied")
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="denied"):
            walg.prepare_archiving(MagicMock(), settings, accept_live_evidence=True)


def test_admission_of_a_new_production_never_accepts_live_evidence(settings, monitor):
    wal_monitor._monitor(settings).latest = healthy_sample()
    with (
        patch.object(walg, "_pg_probe", return_value=""),
        patch.object(
            walg, "storage_preflight", side_effect=PrerequisiteNotMetError("denied")
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="denied"):
            walg.prepare_archiving(MagicMock(), settings)


@pytest.mark.parametrize(
    "operation,accepts",
    [
        ("start_production", False),
        ("restart_production", True),
    ],
)
def test_only_running_productions_may_skip_the_verification_segment(
    settings, operation, accepts
):
    from oduflow.settings import TeamSettings

    client = MagicMock()
    with (
        patch("oduflow.production_registry.get_production"),
        patch.object(production_ops, "get_client", return_value=client),
        patch.object(production_ops, "ensure_prod_infra") as ensure,
        patch.object(production_ops, "_require_container"),
    ):
        getattr(production_ops, operation)(settings, TeamSettings(team_id="1"), "erp")
    assert ensure.call_args.kwargs.get("accept_live_evidence", False) is accepts


def _run_sample_script(pgdata):
    script = (
        Path(__file__).parents[1] / "src/oduflow/templates/wal-sample.sh"
    ).read_text()
    return subprocess.run(
        ["sh", "-c", script],
        env={**os.environ, "PGDATA": str(pgdata)},
        capture_output=True,
        timeout=60,
    )


def test_sampling_survives_segments_vanishing_mid_scan(tmp_path):
    # The archiver recycles segments and .ready files while we walk them, so a
    # strict find would fail the sample and block every production operation.
    wal_dir = tmp_path / "pg_wal"
    (wal_dir / "archive_status").mkdir(parents=True)

    def populate():
        for index in range(300):
            (wal_dir / f"{index:024X}").write_bytes(b"x" * 16)
            (wal_dir / "archive_status" / f"{index:024X}.ready").touch()

    def churn():
        for path in sorted(wal_dir.rglob("*")):
            if path.is_file():
                path.unlink(missing_ok=True)

    for _ in range(5):
        populate()
        worker = threading.Thread(target=churn)
        worker.start()
        try:
            result = _run_sample_script(tmp_path)
        finally:
            worker.join()
        assert result.returncode == 0, result.stderr
        assert b"READY" in result.stdout


def test_sampling_fails_when_the_wal_directory_is_missing(tmp_path):
    (tmp_path / "pg_wal").mkdir()
    assert _run_sample_script(tmp_path).returncode != 0  # no archive_status
