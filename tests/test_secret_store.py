"""Team-scoped named secrets: write-only store + env-var reference resolution."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

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

    @pytest.mark.parametrize("content", [b"not json", b"\xff\xfe{}"])
    def test_corrupt_store_is_a_prerequisite_error(self, team, content):
        Path(secret_store.secrets_path(team)).write_bytes(content)
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


def test_backfill_types_legacy_records_by_stored_value(team):
    legacy = {
        "config": '{\n  "environment": {"KEY": "private"}\n}',
        "list": "[1, 2]",
        "token": "s3cret",
        "number": "12345",
        "flag": "true",
        "quoted": '"token"',
        "nan": "NaN",
        "broken": '{"private":}',
    }
    path = Path(secret_store.secrets_path(team))
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "secrets": {
                    name: {"value": value, "created_at": "c", "updated_at": "u"}
                    for name, value in legacy.items()
                },
            }
        )
    )

    # Only objects and arrays become JSON; valid JSON scalars stay plain tokens.
    assert secret_store.backfill_value_types(team) == ["config", "list"]
    records = json.loads(path.read_text())["secrets"]
    assert {name: r["value_type"] for name, r in records.items()} == {
        name: "json" if name in ("config", "list") else "text" for name in legacy
    }
    # Values and timestamps are untouched.
    assert {name: r["value"] for name, r in records.items()} == legacy
    assert {r["updated_at"] for r in records.values()} == {"u"}

    before = path.read_bytes()
    assert secret_store.backfill_value_types(team) == []
    assert path.read_bytes() == before
    assert secret_store.update_secret_json(
        team, "config", "environment.KEY", "new"
    ) == {"name": "config", "created": False, "updated": True}
    stored = json.loads(path.read_text())["secrets"]["config"]["value"]
    assert json.loads(stored) == {"environment": {"KEY": "new"}}


def test_untyped_legacy_records_are_typed_on_read(team):
    # A store the backfill never saw (team re-added, restored backup) behaves
    # exactly as if it had been typed.
    path = Path(secret_store.secrets_path(team))
    legacy = {"config": '{"environment": {}}', "number": "12345"}
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "secrets": {name: {"value": value} for name, value in legacy.items()},
            }
        )
    )

    assert {r["name"]: r["value_type"] for r in secret_store.list_secrets(team)} == {
        "config": "json",
        "number": "text",
    }
    assert secret_store.update_secret_json(
        team, "config", "environment.KEY", "new"
    ) == {"name": "config", "created": True, "updated": False}
    stored = json.loads(path.read_text())["secrets"]["config"]["value"]
    assert json.loads(stored) == {"environment": {"KEY": "new"}}
    # An omitted type is inherited from the inferred one.
    with pytest.raises(ValueError, match="Invalid JSON"):
        secret_store.set_secret(team, "config", "plain")
    secret_store.set_secret(team, "number", "67890")
    assert json.loads(path.read_text())["secrets"]["number"]["value_type"] == "text"


def test_backfill_keeps_explicit_types_and_skips_missing_store(team):
    path = Path(secret_store.secrets_path(team))
    assert secret_store.backfill_value_types(team) == []
    assert not path.exists()

    # JSON deliberately saved as text after typing existed stays text.
    secret_store.set_secret(team, "chosen", '{"key": "value"}', "text")
    before = path.read_bytes()
    assert secret_store.backfill_value_types(team) == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("value_type", ["xml", "", False, [], {}])
def test_unknown_type_is_rejected(team, value_type):
    with pytest.raises(ValueError, match="value_type"):
        secret_store.set_secret(team, "key", "{}", value_type)
    assert secret_store.list_secrets(team) == []


def test_update_json_preserves_other_values_and_metadata(team):
    original = {"database": {"password": "old", "user": "private"}, "enabled": True}
    secret_store.set_secret(team, "config", json.dumps(original), "json")
    secret_store.set_secret(team, "other", "untouched")
    path = Path(secret_store.secrets_path(team))
    before = json.loads(path.read_text())

    result = secret_store.update_secret_json(
        team, "config", "/database/password", "new"
    )

    after = json.loads(path.read_text())
    record = after["secrets"]["config"]
    original["database"]["password"] = "new"
    assert json.loads(record["value"]) == original
    assert result == {"name": "config", "created": False, "updated": True}
    assert record["value_type"] == "json"
    assert record["created_at"] == before["secrets"]["config"]["created_at"]
    assert record["updated_at"] > before["secrets"]["config"]["updated_at"]
    assert after["secrets"]["other"] == before["secrets"]["other"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "value", [None, False, 0, 1.5, "", "secret", [], {"key": "new"}]
)
def test_update_json_accepts_json_value_types(team, value):
    secret_store.set_secret(team, "config", '{"key": "old", "keep": 42}', "json")
    secret_store.update_secret_json(team, "config", "/key", value)
    resolved = secret_store.resolve_env_secrets(team, {"CONFIG": "secret:config"})
    assert json.loads(resolved["CONFIG"]) == {"key": value, "keep": 42}


@pytest.mark.parametrize(
    "original,path,expected",
    [
        (
            {"accounts": [{"token": "old"}, {"token": "keep"}]},
            "/accounts/0/token",
            {"accounts": [{"token": "new"}, {"token": "keep"}]},
        ),
        (["keep", "old"], "/1", ["keep", "new"]),
        ({"a/b": {"~key": "old"}}, "/a~1b/~0key", {"a/b": {"~key": "new"}}),
        ({"~1": "old"}, "/~01", {"~1": "new"}),
        ({"": "old"}, "/", {"": "new"}),
        ({"": {"": "old"}}, "//", {"": {"": "new"}}),
        ({"01": "old"}, "/01", {"01": "new"}),
        (
            {"a.b": "old", "a": {"b": "keep"}},
            "/a.b",
            {"a.b": "new", "a": {"b": "keep"}},
        ),
        ({"key": None}, "/key", {"key": "new"}),
    ],
)
def test_update_json_pointer(team, original, path, expected):
    secret_store.set_secret(team, "config", json.dumps(original), "json")
    result = secret_store.update_secret_json(team, "config", path, "new")
    assert result == {"name": "config", "created": False, "updated": True}
    resolved = secret_store.resolve_env_secrets(team, {"CONFIG": "secret:config"})
    assert json.loads(resolved["CONFIG"]) == expected


@pytest.mark.parametrize(
    "pointer",
    [
        None,
        1,
        [],
        {},
        "",
        ".key",
        "key.",
        "key..nested",
        "key/nested",
        "#/key",
        "/bad~",
        "/bad~2",
        "/missing/key",
        "/key/nested",
        "/items/-",
        "/items/-1",
        "/items/01",
        "/items/+0",
        "/items/ 0",
        "/items/١",
        "/items/1",
        "/items/0/missing",
        "/items/" + "9" * 5000,
    ],
)
def test_update_json_invalid_path_changes_nothing(team, pointer):
    secret_store.set_secret(team, "config", '{"key":"private","items":[null]}', "json")
    path = Path(secret_store.secrets_path(team))
    before = path.read_bytes()
    with pytest.raises(ValueError) as error:
        secret_store.update_secret_json(team, "config", pointer, "new-private")
    assert "private" not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("original", ["null", "true", "42", '"private"', "[]"])
def test_update_json_requires_existing_element(team, original):
    secret_store.set_secret(team, "config", original, "json")
    with pytest.raises(ValueError, match="existing element"):
        secret_store.update_secret_json(team, "config", "/0", "new")
    assert secret_store.resolve_env_secrets(team, {"C": "secret:config"}) == {
        "C": original
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_update_json_rejects_text_without_converting(team, legacy):
    # An untyped legacy record is text unless it holds a JSON object or array.
    value = '"private"' if legacy else '{"key": "private"}'
    secret_store.set_secret(team, "config", value)
    path = Path(secret_store.secrets_path(team))
    if legacy:
        data = json.loads(path.read_text())
        del data["secrets"]["config"]["value_type"]
        path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="only supported for JSON secrets"):
        secret_store.update_secret_json(team, "config", "/key", "new")
    assert path.read_bytes() == before


def test_update_json_missing_secret_is_not_created(team):
    with pytest.raises(NotFoundError):
        secret_store.update_secret_json(team, "missing", "/key", "new")
    assert secret_store.list_secrets(team) == []


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {"nested": float("-inf")}, {1, 2}]
)
def test_update_json_invalid_value_changes_nothing(team, value):
    secret_store.set_secret(team, "config", '{"key": "private"}', "json")
    path = Path(secret_store.secrets_path(team))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="valid JSON values"):
        secret_store.update_secret_json(team, "config", "/key", value)
    assert path.read_bytes() == before


def test_update_json_corrupt_value_does_not_leak_or_overwrite(team):
    secret_store.set_secret(team, "config", "{}", "json")
    path = Path(secret_store.secrets_path(team))
    data = json.loads(path.read_text())
    data["secrets"]["config"]["value"] = '{"private": broken}'
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(PrerequisiteNotMetError) as error:
        secret_store.update_secret_json(team, "config", "/private", "new")
    assert "private" not in str(error.value)
    assert path.read_bytes() == before


def test_concurrent_json_updates_preserve_each_other(team):
    count = 8
    secret_store.set_secret(
        team, "config", json.dumps({str(i): "old" for i in range(count)}), "json"
    )
    barrier = Barrier(count)

    def update(index):
        barrier.wait(timeout=10)
        secret_store.update_secret_json(team, "config", f"/{index}", index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(update, range(count)))
    resolved = secret_store.resolve_env_secrets(team, {"C": "secret:config"})
    assert json.loads(resolved["C"]) == {str(i): i for i in range(count)}


@pytest.mark.parametrize(
    "path",
    ["environment.OPENROUTER_API_KEY", "/environment/OPENROUTER_API_KEY"],
)
def test_create_json_key_then_update_reports_actual_result(team, path):
    secret_store.set_secret(team, "config", '{"environment":{"KEEP":"same"}}', "json")
    before = secret_store.list_secrets(team)[0]
    created = secret_store.update_secret_json(team, "config", path, "first")
    assert created == {"name": "config", "created": True, "updated": False}
    updated = secret_store.update_secret_json(team, "config", path, "second")
    assert updated == {"name": "config", "created": False, "updated": True}
    resolved = secret_store.resolve_env_secrets(team, {"C": "secret:config"})
    assert json.loads(resolved["C"]) == {
        "environment": {"KEEP": "same", "OPENROUTER_API_KEY": "second"}
    }
    assert secret_store.list_secrets(team)[0]["created_at"] == before["created_at"]


@pytest.mark.parametrize(
    "original,path,expected",
    [
        ({}, "password", {"password": "new"}),
        ({}, "/", {"": "new"}),
        ({}, "/a~1b~0c", {"a/b~c": "new"}),
        ({"a": {}}, "a.b", {"a": {"b": "new"}}),
        ({"a": {}}, "/a.b", {"a": {}, "a.b": "new"}),
        ({"items": [{}]}, "items.0.token", {"items": [{"token": "new"}]}),
    ],
)
def test_create_only_final_object_key(team, original, path, expected):
    secret_store.set_secret(team, "config", json.dumps(original), "json")
    assert secret_store.update_secret_json(team, "config", path, "new")["created"]
    resolved = secret_store.resolve_env_secrets(team, {"C": "secret:config"})
    assert json.loads(resolved["C"]) == expected


@pytest.mark.parametrize("path", ["missing.key", "items.1", "items.-", "items.01"])
def test_dot_paths_do_not_create_parents_or_array_elements(team, path):
    secret_store.set_secret(team, "config", '{"items":["keep"]}', "json")
    store_path = Path(secret_store.secrets_path(team))
    before = store_path.read_bytes()
    with pytest.raises(ValueError):
        secret_store.update_secret_json(team, "config", path, "new")
    assert store_path.read_bytes() == before


def test_dot_path_updates_existing_array_element(team):
    secret_store.set_secret(team, "config", '{"items":["old","keep"]}', "json")
    result = secret_store.update_secret_json(team, "config", "items.0", "new")
    assert result == {"name": "config", "created": False, "updated": True}
    resolved = secret_store.resolve_env_secrets(team, {"C": "secret:config"})
    assert json.loads(resolved["C"]) == {"items": ["new", "keep"]}


def test_invalid_new_key_value_does_not_create_key(team):
    secret_store.set_secret(team, "config", "{}", "json")
    store_path = Path(secret_store.secrets_path(team))
    before = store_path.read_bytes()
    with pytest.raises(ValueError):
        secret_store.update_secret_json(team, "config", "new", float("nan"))
    assert store_path.read_bytes() == before


def test_concurrent_creation_reports_created_only_once(team):
    secret_store.set_secret(team, "config", "{}", "json")
    barrier = Barrier(2)

    def save(value):
        barrier.wait(timeout=10)
        return secret_store.update_secret_json(team, "config", "new", value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["first", "second"]))
    assert sorted(result["created"] for result in results) == [False, True]
    assert all(result["updated"] is not result["created"] for result in results)


def test_drifted_secret_keys_reports_changed_values_by_name(team):
    secret_store.set_secret(team, "api-key", "new-sensitive-value")
    secret_store.set_secret(team, "same", "unchanged")
    env_vars = {
        "API_KEY": "secret:api-key",
        "SAME": "secret:same",
        "PLAIN": "literal",
        "GONE": "secret:missing",
        "BAD": "secret:../x",
    }
    running = {
        "API_KEY": "old-sensitive-value",
        "SAME": "unchanged",
        "PLAIN": "other",
        "GONE": "whatever",
        "BAD": "whatever",
    }
    # Literal values are configuration, not secrets; dangling and malformed
    # references are reported when a recreate resolves them, not here.
    assert secret_store.drifted_secret_keys(team, env_vars, running) == ["API_KEY"]
    assert secret_store.drifted_secret_keys(team, None, running) == []
    assert secret_store.drifted_secret_keys(team, env_vars, {}) == ["API_KEY", "SAME"]
