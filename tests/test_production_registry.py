import json
import os
import threading

import pytest

from oduflow import production_registry as reg
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


class TestCreate:
    def test_create_and_get(self, team):
        reg.create_production(team, "erp", {"domain": "erp.example.com"})
        record = reg.get_production(team, "erp")
        assert record["name"] == "erp"
        assert record["domain"] == "erp.example.com"
        assert record["auto_update"] is False
        assert record["deploy_in_progress"] is False
        assert record["odoo_conf"] == {}

    def test_duplicate_raises_conflict(self, team):
        reg.create_production(team, "erp", {})
        with pytest.raises(ConflictError):
            reg.create_production(team, "erp", {})

    def test_webhook_secret_generated_once(self, team):
        assert reg.get_webhook_secret(team) == ""
        reg.create_production(team, "a", {})
        secret = reg.get_webhook_secret(team)
        assert len(secret) > 30
        reg.create_production(team, "b", {})
        assert reg.get_webhook_secret(team) == secret

    def test_registry_file_is_private(self, team):
        reg.create_production(team, "erp", {})
        mode = os.stat(reg.registry_path(team)).st_mode & 0o777
        assert mode == 0o600


class TestUpdateDelete:
    def test_update_merges(self, team):
        reg.create_production(team, "erp", {})
        reg.update_production(team, "erp", {"auto_update": True})
        assert reg.get_production(team, "erp")["auto_update"] is True

    def test_update_missing_raises(self, team):
        with pytest.raises(NotFoundError):
            reg.update_production(team, "nope", {})

    def test_set_nested_backup_section(self, team):
        reg.create_production(team, "erp", {})
        reg.set_nested(team, "erp", "backup", {"schedule": "02:00"})
        reg.set_nested(team, "erp", "backup", {"last_snapshot_id": "x"})
        backup = reg.get_production(team, "erp")["backup"]
        assert backup == {"schedule": "02:00", "last_snapshot_id": "x"}

    def test_delete_idempotent(self, team):
        reg.create_production(team, "erp", {})
        reg.delete_production(team, "erp")
        reg.delete_production(team, "erp")  # no error
        with pytest.raises(NotFoundError):
            reg.get_production(team, "erp")


class TestStaleFlags:
    def test_clear_stale_deploy_flags(self, team):
        reg.create_production(team, "a", {"deploy_in_progress": True})
        reg.create_production(team, "b", {})
        cleared = reg.clear_stale_deploy_flags(team)
        assert cleared == ["a"]
        assert reg.get_production(team, "a")["deploy_in_progress"] is False

    def test_clear_on_missing_registry(self, team):
        assert reg.clear_stale_deploy_flags(team) == []


class TestRobustness:
    def test_corrupt_registry_raises_not_resets(self, team):
        # Unlike ports.json, a corrupt productions registry must never be
        # silently replaced with an empty one.
        path = reg.registry_path(team)
        with open(path, "w") as f:
            f.write("{not json")
        with pytest.raises(RuntimeError, match="Corrupt"):
            reg.list_productions(team)

    def test_concurrent_creates_all_land(self, team):
        errors: list[Exception] = []

        def create(i: int) -> None:
            try:
                reg.create_production(team, f"prod{i}", {})
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(reg.list_productions(team)) == 10
        # State on disk is valid JSON with all records.
        with open(reg.registry_path(team)) as f:
            assert len(json.load(f)["productions"]) == 10


class TestStateShape:
    def test_a_fresh_registry_declares_schema_version_1(self, team):
        # The version gates future migrations; a wrong value would make an
        # upgrade skip or re-run them.
        reg.create_production(team, "erp", {})

        with open(reg.registry_path(team)) as f:
            assert json.load(f)["version"] == 1

    def test_a_legacy_registry_without_a_version_is_stamped_as_1(self, team):
        path = reg.registry_path(team)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"productions": {}}, f)

        assert reg._load_state(path)["version"] == 1

    def test_the_webhook_secret_is_43_url_safe_chars(self, team):
        # token_urlsafe(32) -> 43 chars; the value is a shared HMAC secret, so
        # its entropy is worth pinning.
        reg.create_production(team, "erp", {})

        secret = reg.get_webhook_secret(team)
        assert len(secret) == 43

    def test_a_registry_missing_the_productions_map_is_rejected(self, team):
        # Both halves of the shape check matter: a dict without "productions"
        # must not be accepted and then written back as a valid registry.
        path = reg.registry_path(team)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"version": 1, "webhook_secret": "s"}, f)

        with pytest.raises(RuntimeError, match="Malformed"):
            reg._load_state(path)

    def test_a_non_dict_registry_is_rejected(self, team):
        path = reg.registry_path(team)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(["not", "a", "registry"], f)

        with pytest.raises(RuntimeError, match="Malformed"):
            reg._load_state(path)

    def test_a_productions_value_of_the_wrong_type_is_rejected(self, team):
        path = reg.registry_path(team)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"version": 1, "productions": []}, f)

        with pytest.raises(RuntimeError, match="Malformed"):
            reg._load_state(path)
