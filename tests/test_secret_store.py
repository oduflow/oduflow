"""Team-scoped named secrets: write-only store + env-var reference resolution."""

import json
import os
import stat

import pytest

from oduflow import secret_store
from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


class TestNamesAndRefs:
    def test_valid_names(self):
        for name in ("a", "openai-api-key", "fs.esl_password", "0token"):
            assert secret_store.validate_secret_name(name) == name

    @pytest.mark.parametrize(
        "name", ["", "Upper", "-leading", ".leading", "has space", "a" * 65]
    )
    def test_invalid_names(self, name):
        with pytest.raises(ValueError):
            secret_store.validate_secret_name(name)

    def test_ref_detection(self):
        assert secret_store.is_secret_ref("secret:x")
        assert not secret_store.is_secret_ref("plain")
        assert not secret_store.is_secret_ref(None)
        assert not secret_store.is_secret_ref(42)

    def test_ref_name_extraction(self):
        assert secret_store.secret_ref_name("secret:api-key") == "api-key"
        with pytest.raises(ValueError):
            secret_store.secret_ref_name("secret:")
        with pytest.raises(ValueError):
            secret_store.secret_ref_name("secret:Bad Name")


class TestStore:
    def test_set_list_delete_roundtrip(self, team):
        assert secret_store.list_secrets(team) == []
        result = secret_store.set_secret(team, "api-key", "s3cret")
        assert result == {"name": "api-key", "created": True}

        records = secret_store.list_secrets(team)
        assert [r["name"] for r in records] == ["api-key"]
        # The value never appears in a listing.
        assert all("value" not in r for r in records)
        assert records[0]["created_at"] and records[0]["updated_at"]

        result = secret_store.set_secret(team, "api-key", "rotated")
        assert result == {"name": "api-key", "created": False}

        secret_store.delete_secret(team, "api-key")
        assert secret_store.list_secrets(team) == []

    def test_update_keeps_created_at(self, team):
        secret_store.set_secret(team, "k", "v1")
        first = secret_store.list_secrets(team)[0]
        secret_store.set_secret(team, "k", "v2")
        second = secret_store.list_secrets(team)[0]
        assert second["created_at"] == first["created_at"]

    def test_file_is_owner_only(self, team):
        secret_store.set_secret(team, "k", "v")
        mode = stat.S_IMODE(os.stat(secret_store.secrets_path(team)).st_mode)
        assert mode == 0o600

    def test_delete_missing_raises(self, team):
        with pytest.raises(NotFoundError):
            secret_store.delete_secret(team, "nope")

    def test_empty_value_rejected(self, team):
        with pytest.raises(ValueError):
            secret_store.set_secret(team, "k", "")

    def test_corrupt_store_is_a_prerequisite_error(self, team):
        with open(secret_store.secrets_path(team), "w") as fh:
            fh.write("not json")
        with pytest.raises(PrerequisiteNotMetError):
            secret_store.list_secrets(team)

    def test_store_keeps_versioned_shape(self, team):
        secret_store.set_secret(team, "k", "v")
        with open(secret_store.secrets_path(team)) as fh:
            data = json.load(fh)
        assert data["version"] == 1
        assert data["secrets"]["k"]["value"] == "v"


class TestResolveEnvSecrets:
    def test_none_and_empty_pass_through(self, team):
        assert secret_store.resolve_env_secrets(team, None) is None
        assert secret_store.resolve_env_secrets(team, {}) == {}

    def test_plain_values_pass_through(self, team):
        env = {"WORKERS": "2"}
        resolved = secret_store.resolve_env_secrets(team, env)
        assert resolved == env
        assert resolved is not env  # caller keeps mutating its own copy

    def test_references_are_substituted(self, team):
        secret_store.set_secret(team, "esl-password", "hunter2")
        resolved = secret_store.resolve_env_secrets(
            team, {"FS_ESL_PASSWORD": "secret:esl-password", "FS_DOMAIN": "x"}
        )
        assert resolved == {"FS_ESL_PASSWORD": "hunter2", "FS_DOMAIN": "x"}

    def test_missing_secret_aborts_with_all_names(self, team):
        secret_store.set_secret(team, "known", "v")
        with pytest.raises(PrerequisiteNotMetError, match="gone-a.*gone-b"):
            secret_store.resolve_env_secrets(
                team,
                {"A": "secret:gone-b", "B": "secret:gone-a", "C": "secret:known"},
            )

    def test_malformed_reference_names_the_variable(self, team):
        with pytest.raises(PrerequisiteNotMetError, match="API_KEY"):
            secret_store.resolve_env_secrets(team, {"API_KEY": "secret:Bad Name"})


@pytest.mark.parametrize(
    "value", ['{\n  "key": "private"\n}', "[]", "null", "true", "42", '"token"']
)
def test_json_values_preserve_original_text(team, value):
    secret_store.set_secret(team, "key", value, "json")
    assert secret_store.list_secrets(team)[0]["value_type"] == "json"
    assert secret_store.resolve_env_secrets(team, {"KEY": "secret:key"}) == {
        "KEY": value
    }


@pytest.mark.parametrize(
    "value", ['{"private":}', "NaN", "Infinity", "-Infinity", '{"x": NaN}', "   "]
)
def test_invalid_json_does_not_replace_secret(team, value):
    secret_store.set_secret(team, "key", "{}", "json")
    before = open(secret_store.secrets_path(team)).read()
    with pytest.raises(ValueError, match="Invalid JSON") as error:
        secret_store.set_secret(team, "key", value)
    assert "private" not in str(error.value)
    assert open(secret_store.secrets_path(team)).read() == before


def test_legacy_secret_defaults_to_text_and_can_change_type(team):
    with open(secret_store.secrets_path(team), "w") as handle:
        json.dump({"version": 1, "secrets": {"key": {"value": "old"}}}, handle)
    assert secret_store.list_secrets(team)[0]["value_type"] == "text"
    secret_store.set_secret(team, "key", "new")
    secret_store.set_secret(team, "key", "{}", "json")
    secret_store.set_secret(team, "key", "[]")
    assert secret_store.list_secrets(team)[0]["value_type"] == "json"
    secret_store.set_secret(team, "key", "plain", "text")
    assert secret_store.list_secrets(team)[0]["value_type"] == "text"


@pytest.mark.parametrize("value_type", ["xml", "", False, [], {}])
def test_unknown_type_is_rejected(team, value_type):
    with pytest.raises(ValueError, match="value_type"):
        secret_store.set_secret(team, "key", "{}", value_type)
    assert secret_store.list_secrets(team) == []
