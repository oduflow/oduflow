"""Real production data paths. Run serially on the gated disposable Linux engine.

Traefik needs port 80. The backup cases additionally need a bridge gateway
reachable from pytest and Docker, SeaweedFS, and the release binary used by WAL-G.
Nothing in this module loads an installed server's configuration.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import docker
from oduflow import backup_ops, chunkstore, production_registry, s3_client, walg
from oduflow.docker_ops import env_ops, odoo_ops, system_ops
from oduflow.docker_ops import production_ops as prod
from oduflow.docker_ops.client import get_client
from oduflow.errors import ExternalCommandError
from oduflow.locking import LockManager
from oduflow.naming import prod_env_name
from oduflow.settings import BackupSettings

pytestmark = pytest.mark.integration

NAME = "erp"
DOMAIN = "erp.example.test"
IMAGE = "odoo:19.0"
PROD = prod_env_name(NAME)
SEAWEEDFS = "chrislusf/seaweedfs:3.80"


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo, message):
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Integration",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin"
    addon = origin / "live_probe"
    addon.mkdir(parents=True)
    _git(origin, "init", "-b", "main")
    (addon / "__init__.py").touch()
    (addon / "__manifest__.py").write_text(
        repr(
            {
                "name": "Live probe",
                "version": "19.0.1.0.0",
                "depends": ["base"],
                "license": "LGPL-3",
                "data": ["probe.xml"],
            }
        )
    )
    (addon / "probe.xml").write_text("<odoo/>\n")
    _commit(origin, "Initial working addon")
    return origin


@pytest.fixture
def production_settings(live_environment, repo):
    settings, team = live_environment
    team = replace(
        team,
        hostname="dev.example.test",
        production_token=secrets.token_hex(24),
        port_range_start=52100,
        port_range_end=52199,
    )
    settings = replace(
        settings,
        teams={team.team_id: team},
        routing_mode="traefik",
        routing_tls=False,
        public_scheme_setting="http",
        prod_enabled=True,
        prod_workers_cap=2,
        # The optional OduMCP connector is not part of these data contracts.
        # Missing series follows the normal warning path without fetching GitHub.
        prod_odumcp_repo_url=repo.as_uri(),
    )
    try:
        yield settings, team
    finally:
        # destroy_system correctly refuses to destroy any registered production.
        # Remove ours first, even if PITR failed with PostgreSQL offline. The
        # outer fixture then removes this isolated cluster and its entire volume.
        if NAME in production_registry.list_productions(team):
            assert not prod.delete_production(settings, team, NAME)["warnings"]


@pytest.fixture
def backed_up_settings(production_settings, tmp_path, monkeypatch):
    settings, team = production_settings
    client = get_client()
    gateway = client.networks.get("bridge").attrs["IPAM"]["Config"][0]["Gateway"]
    # VM runners may proxy public image/binary downloads. S3 is private to
    # this daemon and must never be sent through the ambient HTTP proxy.
    bypass = os.environ.get("no_proxy") or os.environ.get("NO_PROXY", "")
    for variable in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(variable, ",".join(filter(None, [bypass, gateway])))
    key, secret = secrets.token_hex(10), secrets.token_hex(24)
    config = tmp_path / "s3.json"
    config.touch(mode=0o600)
    config.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "integration",
                        "credentials": [{"accessKey": key, "secretKey": secret}],
                        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                    }
                ]
            }
        )
    )
    # Publish only on the disposable VM's bridge gateway, not its LAN or
    # loopback. The default-network PITR helper must reach the same URL as PG.
    container = client.containers.run(
        SEAWEEDFS,
        [
            "server",
            "-dir=/data",
            "-s3",
            "-s3.config=/etc/s3.json",
            "-volume.max=2",
            "-master.volumeSizeLimitMB=100",
        ],
        name=settings.prefix + "s3",
        detach=True,
        volumes={str(config): {"bind": "/etc/s3.json", "mode": "ro"}},
        ports={"8333/tcp": (gateway, None)},
        labels={settings.managed_label: "true"},
    )
    try:
        container.reload()
        port = int(container.ports["8333/tcp"][0]["HostPort"])
        backup = BackupSettings(
            endpoint=f"http://{gateway}:{port}",
            bucket="live-" + secrets.token_hex(8),
            access_key=key,
            secret_key=secret,
            region="us-east-1",
            keep=("0:1",),
        )
        s3 = s3_client.make_client(backup)
        deadline = time.monotonic() + 60
        while True:
            try:
                s3.list_buckets()
                break
            except (EndpointConnectionError, ClientError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        s3.create_bucket(Bucket=backup.bucket)
        yield replace(settings, backup=backup), team
    finally:
        container.remove(force=True, v=True)
        config.unlink()


def _create(settings, team, repo, source=None):
    info = prod.create_production(
        settings,
        team,
        NAME,
        "" if source else repo.as_uri(),
        "" if source else "main",
        DOMAIN,
        "" if source else IMAGE,
        from_environment=source,
    )
    assert info["commit"] == _git(repo, "rev-parse", "HEAD")
    assert prod.wait_production_healthy(get_client(), settings, team, NAME)
    return info


def _attachment(settings, team, env_name, expected, *, write=False):
    """Assert the DB row points to a file and Odoo actually reads its bytes."""
    if env_name == PROD:
        settings = replace(settings, shared_db_container=settings.prod_db_container)
    script = """
model = env['ir.attachment'].sudo()
row = model.search([('name', '=', 'live-proof.bin')])
"""
    if write:
        script += f"""
if row:
    row.write({{'raw': {expected!r}}})
else:
    row = model.create({{'name': 'live-proof.bin', 'raw': {expected!r}, 'type': 'binary'}})
"""
    script += """
row.ensure_one()
assert row.store_fname and not row.db_datas
print('FILE=' + row.store_fname + ' BYTES=' + row.raw.hex())
"""
    result = odoo_ops.run_odoo_shell(settings, team, env_name, script)
    assert result["exit_code"] == 0, result["output"]
    digest = hashlib.sha1(expected).hexdigest()
    relative = f"{digest[:2]}/{digest}"
    assert f"FILE={relative} BYTES={expected.hex()}" in result["output"]
    return relative


def test_promotion_copy_and_lifecycle(production_settings, repo):
    settings, team = production_settings
    system_ops.init_system(settings)
    env_ops.create_environment(
        settings, team, "main", repo.as_uri(), IMAGE, env_name="dev"
    )
    _attachment(settings, team, "dev", b"source", write=True)
    info = _create(settings, team, repo, source="dev")
    _attachment(settings, team, PROD, b"source")
    _attachment(settings, team, "dev", b"new dev", write=True)
    _attachment(settings, team, PROD, b"source")

    restored = backup_ops.restore_production_from_environment(
        settings, team, NAME, "dev"
    )
    assert restored["healthy"]
    relative = _attachment(settings, team, PROD, b"new dev")
    _attachment(settings, team, "dev", b"new dev")
    assert prod.get_production_info(settings, team, NAME)["commit"] == info["commit"]

    system_ops.publish_production_as_template(settings, team, NAME, "prod-erp")
    env_ops.create_environment(
        settings,
        team,
        "main",
        repo.as_uri(),
        IMAGE,
        env_name="copy",
        template_name="prod-erp",
    )
    _attachment(settings, team, "copy", b"new dev")
    _attachment(settings, team, "copy", b"copy only", write=True)
    _attachment(settings, team, PROD, b"new dev")

    client = get_client()
    prod.stop_production(settings, team, NAME)
    assert client.containers.get(info["odoo_container"]).status == "exited"
    prod.start_production(settings, team, NAME)
    assert prod.wait_production_healthy(client, settings, team, NAME)
    prod.delete_production(settings, team, NAME)
    assert prod.list_productions(settings, team) == []
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(info["odoo_container"])
    assert system_ops._db_exists(
        client, settings, info["database"], container_name=settings.prod_db_container
    )
    assert (
        Path(prod.prod_filestore_dir(team, NAME)) / relative
    ).read_bytes() == b"new dev"
    purged = prod.purge_deleted_productions(settings, team)
    assert purged["purged"] == [NAME] and not purged["warnings"]
    assert not system_ops._db_exists(
        client, settings, info["database"], container_name=settings.prod_db_container
    )
    assert not Path(info["workspace"]).exists()


def test_failed_upgrade_and_manual_code_rollback(production_settings, repo):
    settings, team = production_settings
    system_ops.init_system(settings)
    initial = _create(settings, team, repo)["commit"]
    (repo / "README.md").write_text("Deploy the probe addon.\n")
    good = _commit(repo, "Install working version")
    success = prod.update_production(settings, team, NAME, install=["live_probe"])
    assert success["deploy"]["status"] == "success" and success["commit"] == good
    _attachment(settings, team, PROD, b"keep me", write=True)

    (repo / "live_probe" / "probe.xml").write_text("<odoo><unclosed></odoo>")
    bad = _commit(repo, "Invalid module data")
    result = prod.update_production(settings, team, NAME, upgrade=["live_probe"])
    assert (result["action"], result["failed_commit"]) == ("rolled_back", bad)
    info = prod.get_production_info(settings, team, NAME)
    assert info["healthy"] and info["commit"] == good
    assert info["deploys"][-1]["failed_step"] == "upgrade"
    _attachment(settings, team, PROD, b"keep me")
    assert prod.rollback_production(settings, team, NAME, initial)["healthy"]
    assert prod.get_production_info(settings, team, NAME)["commit"] == initial
    _attachment(settings, team, PROD, b"keep me")


def test_snapshot_pair_integrity_and_retention(backed_up_settings, repo):
    settings, team = backed_up_settings
    system_ops.init_system(settings)
    commit = _create(settings, team, repo)["commit"]
    _attachment(settings, team, PROD, b"snapshot A", write=True)
    a = backup_ops.snapshot_production(settings, team, NAME)
    b_file = Path(prod.prod_filestore_dir(team, NAME)) / _attachment(
        settings, team, PROD, b"snapshot B", write=True
    )
    b = backup_ops.snapshot_production(settings, team, NAME)
    assert a["commit_sha"] == b["commit_sha"] == commit
    assert a["filestore"]["revision"] < b["filestore"]["revision"]
    assert b_file.read_bytes() == b"snapshot B"
    assert backup_ops.restore_production(settings, team, NAME, a["id"])["healthy"]
    assert not b_file.exists()  # DB-only rollback would leave this blob live.
    _attachment(settings, team, PROD, b"snapshot A")

    storage = s3_client.S3Storage(settings.backup, "")
    # Break only the old dump in the private bucket. The live pair differs
    # from that snapshot, so even a partial restore would be observable.
    _attachment(settings, team, PROD, b"live C", write=True)
    storage.put(a["db"]["key"], b"truncated")
    with pytest.raises(ExternalCommandError, match="sha256"):
        backup_ops.restore_production(settings, team, NAME, a["id"])
    _attachment(settings, team, PROD, b"live C")

    # Seed a historical date in the persisted manifest rather than mock time.
    manifest_key = f"{settings.backup.prefix}/snapshots/{team.team_id}/{NAME}/manifests/{a['id']}.json"
    aged = {
        **a,
        "created_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
        ).isoformat(),
    }
    storage.put(manifest_key, json.dumps(aged).encode())
    prune = backup_ops.prune_backups(settings, team)
    assert prune["deleted_snapshots"] == [f"{NAME}/{a['id']}"]
    assert not storage.exists(manifest_key) and not storage.exists(a["db"]["key"])
    assert chunkstore.list_revisions(
        backup_ops.filestore_storage(settings, team), NAME
    ) == [b["filestore"]["revision"]]
    assert backup_ops.list_snapshots(settings, team, NAME, refresh=True) == [b]
    assert backup_ops.restore_production(settings, team, NAME, b["id"])["healthy"]
    _attachment(settings, team, PROD, b"snapshot B")


def test_cluster_pitr_replays_only_through_target(backed_up_settings, repo):
    settings, team = backed_up_settings
    system_ops.init_system(settings)
    _create(settings, team, repo)
    client = get_client()
    role = prod.production_db_credentials(settings, team, NAME)["pg_user"]
    db = prod.prod_db_name(team, NAME)

    def sql(statement, *, tenant=False):
        return system_ops._exec_sql(
            client,
            settings,
            statement,
            db=db if tenant else "postgres",
            user=role if tenant else None,
            container_name=settings.prod_db_container,
        ).strip()

    sql("CREATE TABLE live_replay (value text PRIMARY KEY)", tenant=True)
    walg.backup_push(client, settings)
    assert walg.backup_list(client, settings)
    sql("INSERT INTO live_replay VALUES ('before')", tenant=True)
    target = sql("SELECT clock_timestamp()::text")
    sql("INSERT INTO live_replay VALUES ('after')", tenant=True)
    assert sql("SELECT count(*) FROM live_replay", tenant=True) == "2"
    segment = sql("SELECT pg_walfile_name(pg_switch_wal())")
    assert re.fullmatch(r"[0-9A-F]{24}", segment)
    deadline = time.monotonic() + settings.wal_upload_timeout + 75
    while True:
        last = walg.archiver_status(client, settings).get("last_archived_wal", "")
        if last[:8] == segment[:8] and last >= segment:
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"WAL segment {segment} was not archived (last: {last})")
        time.sleep(1)

    restored = backup_ops.restore_cluster_pitr(
        settings, LockManager(), target_time=target, confirm="RESTORE-CLUSTER"
    )
    assert restored["restarted"] == [f"{team.team_id}/{NAME}"]
    pg = client.containers.get(settings.prod_db_container)
    data_dir = sql("SHOW data_directory")
    old = f"{data_dir}/{restored['displaced_data_dir']}"
    assert pg.exec_run(["test", "-s", f"{old}/global/pg_control"]).exit_code == 0
    assert sql("SELECT pg_is_in_recovery()") == "f"
    assert sql("SELECT value FROM live_replay", tenant=True) == "before"
    assert prod.wait_production_healthy(client, settings, team, NAME)
