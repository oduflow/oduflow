import datetime
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow import production_registry
from oduflow.docker_ops import production_ops
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", hostname="dev.example.com", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(
        routing_mode="traefik",
        acme_email="a@b.co",
        base_data_dir=str(tmp_path),
        etc_dir=str(tmp_path / "etc"),
        teams={"1": team},
    )


class TestProdUrl:
    def test_defaults_to_https(self, settings):
        assert (
            production_ops.prod_url(settings, {"domain": "erp.example.com"})
            == "https://erp.example.com"
        )

    def test_follows_public_scheme(self, team, tmp_path):
        # tls = false with no upstream terminator: the link must be reachable.
        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            base_data_dir=str(tmp_path),
            etc_dir=str(tmp_path / "etc"),
            public_scheme_setting="http",
            teams={"1": team},
        )
        assert (
            production_ops.prod_url(settings, {"domain": "erp.example.com"})
            == "http://erp.example.com"
        )


def _mock_client():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("nf")
    return client


def _patch_create_stack(client, **overrides):
    """Patch everything create_production touches beyond the logic under test."""

    def fake_clone(repo_url, branch, repo_path, team, **kw):
        import os

        os.makedirs(repo_path, exist_ok=True)

    patches = {
        "get_client": patch.object(production_ops, "get_client", return_value=client),
        "ensure_prod_infra": patch.object(production_ops, "ensure_prod_infra"),
        "ensure_team_network": patch.object(production_ops, "ensure_team_network"),
        "_db_exists": patch.object(
            production_ops, "_db_exists", return_value=overrides.get("db_exists", False)
        ),
        "_exec_sql": patch.object(production_ops, "_exec_sql"),
        "_create_pg_role": patch.object(production_ops, "_create_pg_role"),
        "create_credentials": patch.object(
            production_ops,
            "create_credentials",
            return_value={"pg_user": "u_1_prod-erp", "pg_password": "pw"},
        ),
        "_clone_repo": patch(
            "oduflow.docker_ops.env_ops._clone_repo", side_effect=fake_clone
        ),
        "_init_empty_database": patch(
            "oduflow.docker_ops.env_ops._init_empty_database",
            return_value="[INIT] ok",
        ),
        "_install_apt_packages": patch(
            "oduflow.docker_ops.env_ops._install_apt_packages", return_value=""
        ),
        "_install_pip_requirements": patch(
            "oduflow.docker_ops.env_ops._install_pip_requirements",
            return_value=(False, ""),
        ),
        "get_odoo_uid_gid": patch.object(
            production_ops, "get_odoo_uid_gid", return_value="100:101"
        ),
        "chown_recursive": patch.object(production_ops, "chown_recursive"),
        "_copy_file_to_container": patch.object(
            production_ops, "_copy_file_to_container"
        ),
        "_build_prod_odoo_conf": patch.object(
            production_ops, "_build_prod_odoo_conf", return_value="/tmp/odoo.conf"
        ),
        "rev_parse": patch("oduflow.git_ops.rev_parse", return_value="deadbeef" * 5),
    }
    return patches


class _PatchAll:
    def __init__(self, patches):
        self.patches = patches
        self.mocks = {}

    def __enter__(self):
        for key, p in self.patches.items():
            self.mocks[key] = p.start()
        return self.mocks

    def __exit__(self, *exc):
        for p in self.patches.values():
            p.stop()
        return False


class TestCreateProduction:
    def test_requires_traefik_mode(self, settings, team):
        port_settings = Settings(
            routing_mode="port",
            base_data_dir=settings.base_data_dir,
            teams={"1": team},
        )
        with pytest.raises(PrerequisiteNotMetError, match="traefik"):
            production_ops.create_production(
                port_settings,
                team,
                "erp",
                "https://github.com/o/r.git",
                "main",
                "erp.example.com",
                "odoo:18.0",
            )

    def test_domain_conflict_rejected(self, settings, team):
        production_registry.create_production(
            team, "other", {"domain": "erp.example.com"}
        )
        with pytest.raises(ConflictError, match="already used"):
            production_ops.create_production(
                settings,
                team,
                "erp",
                "https://github.com/o/r.git",
                "main",
                "erp.example.com",
                "odoo:18.0",
            )

    def test_invalid_domain_rejected(self, settings, team):
        with pytest.raises(ValueError, match="Invalid domain"):
            production_ops.create_production(
                settings,
                team,
                "erp",
                "https://github.com/o/r.git",
                "main",
                "https://erp.example.com",
                "odoo:18.0",
            )

    def test_existing_db_refused(self, settings, team):
        client = _mock_client()
        with _PatchAll(_patch_create_stack(client, db_exists=True)):
            with pytest.raises(ConflictError, match="already exists in the production"):
                production_ops.create_production(
                    settings,
                    team,
                    "erp",
                    "https://github.com/o/r.git",
                    "main",
                    "erp.example.com",
                    "odoo:18.0",
                )
        # No registry record left behind.
        assert "erp" not in production_registry.list_productions(team)

    def test_create_labels_and_registry(self, settings, team):
        client = _mock_client()
        with _PatchAll(_patch_create_stack(client)):
            result = production_ops.create_production(
                settings,
                team,
                "erp",
                "https://github.com/o/r.git",
                "production",
                "erp.example.com",
                "odoo:18.0",
                auto_update=True,
            )

        kwargs = client.containers.run.call_args[1]
        labels = kwargs["labels"]
        # Production namespace markers, no dev branch label, no scoped token.
        assert labels["oduflow.prod"] == "true"
        assert labels["oduflow.prod_name"] == "erp"
        assert labels["oduflow.domain"] == "erp.example.com"
        assert settings.branch_label not in labels
        assert not any(k.startswith("oduflow.mcp_token") for k in labels)
        # Custom domain routed via Traefik with TLS.
        assert (
            labels["traefik.http.routers.oduflow-1-prod-erp.rule"]
            == "Host(`erp.example.com`)"
        )
        assert (
            labels["traefik.http.routers.oduflow-1-prod-erp.tls.certresolver"]
            == "letsencrypt"
        )
        # Serving command has no --dev=xml.
        assert kwargs["command"] == "odoo -d oduflow_1_prod-erp"
        assert kwargs["environment"]["HOST"] == settings.prod_db_container
        assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}

        record = production_registry.get_production(team, "erp")
        assert record["auto_update"] is True
        assert record["branch"] == "production"
        assert result["database"] == "oduflow_1_prod-erp"
        # Deploy history recorded the creation.
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["action"] == "create"
        assert deploys[-1]["status"] == "success"

    def test_failure_rolls_back_registry(self, settings, team):
        client = _mock_client()
        client.containers.run.side_effect = RuntimeError("boom")
        with _PatchAll(_patch_create_stack(client)):
            with pytest.raises(RuntimeError, match="boom"):
                production_ops.create_production(
                    settings,
                    team,
                    "erp",
                    "https://github.com/o/r.git",
                    "main",
                    "erp.example.com",
                    "odoo:18.0",
                )
        assert "erp" not in production_registry.list_productions(team)


class TestDeleteProduction:
    def test_missing_raises(self, settings, team):
        with pytest.raises(NotFoundError):
            production_ops.delete_production(settings, team, "nope")

    def test_default_keeps_database(self, settings, team):
        production_registry.create_production(team, "erp", {"domain": "e.x.com"})
        client = _mock_client()
        with patch.object(production_ops, "get_client", return_value=client):
            result = production_ops.delete_production(settings, team, "erp")
        assert result["database_dropped"] is False
        assert any("oduflow_1_prod-erp" in k for k in result["kept"])
        assert "erp" not in production_registry.list_productions(team)

    def test_drop_database(self, settings, team):
        production_registry.create_production(team, "erp", {"domain": "e.x.com"})
        client = _mock_client()
        issued = []
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(
                production_ops,
                "_exec_sql",
                side_effect=lambda c, s, sql, **kw: issued.append(sql) or "",
            ),
            patch.object(production_ops, "_drop_pg_role"),
        ):
            result = production_ops.delete_production(
                settings, team, "erp", drop_database=True
            )
        assert result["database_dropped"] is True
        assert any("DROP DATABASE" in s for s in issued)


class TestRuntimeStatus:
    def test_status_priorities(self):
        running = MagicMock()
        running.status = "running"
        assert production_ops._runtime_status(running, {}) == "running"
        assert (
            production_ops._runtime_status(running, {"deploy_in_progress": True})
            == "deploying"
        )
        assert (
            production_ops._runtime_status(running, {"unhealthy": True}) == "unhealthy"
        )
        assert production_ops._runtime_status(None, {}) == "broken"
        stopped = MagicMock()
        stopped.status = "exited"
        assert production_ops._runtime_status(stopped, {}) == "stopped"


class TestProdConfChain:
    def test_bundled_fallback(self, team, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        path = production_ops._prod_base_conf_path(team, str(repo))
        assert path.endswith("odoo-prod.conf")

    def test_repo_conf_wins(self, team, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".oduflow").mkdir(parents=True)
        conf = repo / ".oduflow" / "odoo.prod.conf"
        conf.write_text("[options]\n")
        assert production_ops._prod_base_conf_path(team, str(repo)) == str(conf)

    def test_team_conf_beats_bundled(self, team, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        team_conf = tmp_path / "team_1" / "odoo.prod.conf"
        team_conf.write_text("[options]\n")
        assert production_ops._prod_base_conf_path(team, str(repo)) == str(team_conf)


class TestTombstone:
    def test_soft_delete_writes_tombstone(self, settings, team):
        production_registry.create_production(team, "erp", {"domain": "e.x.com"})
        client = _mock_client()
        with patch.object(production_ops, "get_client", return_value=client):
            production_ops.delete_production(settings, team, "erp")
        ts = production_ops._tombstone_path(team, "erp")
        assert os.path.isfile(ts)
        with open(ts) as f:
            payload = json.load(f)
        assert payload["name"] == "erp"
        assert payload["db_name"] == "oduflow_1_prod-erp"
        assert payload["deleted_at"]

    def test_drop_database_leaves_no_tombstone(self, settings, team):
        production_registry.create_production(team, "erp", {"domain": "e.x.com"})
        client = _mock_client()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_exec_sql", return_value=""),
            patch.object(production_ops, "_drop_pg_role"),
        ):
            production_ops.delete_production(settings, team, "erp", drop_database=True)
        assert not os.path.isdir(production_ops._workspace(team, "erp"))


class TestPurgeDeletedProductions:
    def _soft_delete(self, settings, team, name="erp"):
        production_registry.create_production(team, name, {"domain": "e.x.com"})
        client = _mock_client()
        with patch.object(production_ops, "get_client", return_value=client):
            production_ops.delete_production(settings, team, name)

    def _age_tombstone(self, team, name, hours):
        ts = production_ops._tombstone_path(team, name)
        with open(ts) as f:
            payload = json.load(f)
        moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            hours=hours
        )
        payload["deleted_at"] = moment.isoformat()
        with open(ts, "w") as f:
            json.dump(payload, f)

    def test_purges_tombstoned_leftovers(self, settings, team):
        self._soft_delete(settings, team)
        issued = []
        with (
            patch.object(production_ops, "get_client", return_value=_mock_client()),
            patch.object(
                production_ops,
                "_exec_sql",
                side_effect=lambda c, s, sql, **kw: issued.append(sql) or "",
            ),
            patch.object(production_ops, "_drop_pg_role"),
        ):
            result = production_ops.purge_deleted_productions(settings, team)
        assert result["purged"] == ["erp"]
        assert any("DROP DATABASE" in s for s in issued)
        assert not os.path.isdir(production_ops._workspace(team, "erp"))

    def test_respects_age_cutoff(self, settings, team):
        self._soft_delete(settings, team)
        result = production_ops.purge_deleted_productions(
            settings, team, older_than_hours=72
        )
        assert result["purged"] == []
        assert result["pending"] == ["erp"]
        assert os.path.isfile(production_ops._tombstone_path(team, "erp"))

    def test_purges_after_cutoff(self, settings, team):
        self._soft_delete(settings, team)
        self._age_tombstone(team, "erp", hours=73)
        with (
            patch.object(production_ops, "get_client", return_value=_mock_client()),
            patch.object(production_ops, "_exec_sql", return_value=""),
            patch.object(production_ops, "_drop_pg_role"),
        ):
            result = production_ops.purge_deleted_productions(
                settings, team, older_than_hours=72
            )
        assert result["purged"] == ["erp"]

    def test_dry_run_touches_nothing(self, settings, team):
        self._soft_delete(settings, team)
        result = production_ops.purge_deleted_productions(settings, team, dry_run=True)
        assert result["dry_run"] is True
        assert result["purged"] == ["erp"]
        assert os.path.isdir(production_ops._workspace(team, "erp"))

    def test_registered_production_never_purged(self, settings, team):
        # A stale tombstone inside a live production's workspace: cleared,
        # bytes untouched.
        production_registry.create_production(team, "erp", {"domain": "e.x.com"})
        production_ops._write_tombstone(team, "erp", "oduflow_1_prod-erp")
        result = production_ops.purge_deleted_productions(settings, team)
        assert result["purged"] == []
        assert not os.path.isfile(production_ops._tombstone_path(team, "erp"))
        assert os.path.isdir(production_ops._workspace(team, "erp"))
        assert "erp" in production_registry.list_productions(team)

    def test_workspace_without_tombstone_ignored(self, settings, team):
        os.makedirs(production_ops._workspace(team, "erp"))
        result = production_ops.purge_deleted_productions(settings, team)
        assert result["purged"] == []
        assert os.path.isdir(production_ops._workspace(team, "erp"))

    def test_db_drop_failure_keeps_workspace_for_retry(self, settings, team):
        self._soft_delete(settings, team)
        with (
            patch.object(production_ops, "get_client", return_value=_mock_client()),
            patch.object(
                production_ops, "_exec_sql", side_effect=RuntimeError("pg down")
            ),
        ):
            result = production_ops.purge_deleted_productions(settings, team)
        assert result["purged"] == []
        assert result["pending"] == ["erp"]
        assert result["warnings"]
        assert os.path.isfile(production_ops._tombstone_path(team, "erp"))

    def test_unreadable_tombstone_restarts_clock(self, settings, team):
        self._soft_delete(settings, team)
        with open(production_ops._tombstone_path(team, "erp"), "w") as f:
            f.write("not json")
        result = production_ops.purge_deleted_productions(
            settings, team, older_than_hours=72
        )
        assert result["purged"] == []
        assert result["warnings"]
        with open(production_ops._tombstone_path(team, "erp")) as f:
            assert json.load(f)["name"] == "erp"
