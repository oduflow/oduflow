"""Dotenv parsing and fromEnv overlay for declarative Stacks."""

import os

import pytest

from oduflow.stack_loader import (
    StackValidationError,
    load_env_file,
    resolve_env_values,
    stack_environ,
)


def test_load_env_file_parses_basic_syntax(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """\
# comment
FS_ESL_PASSWORD=s3cret

export API_KEY="quoted value"
SINGLE='single quoted'
SPACED =  padded value
EMPTY=
""",
        encoding="utf-8",
    )
    assert load_env_file(env_file) == {
        "FS_ESL_PASSWORD": "s3cret",
        "API_KEY": "quoted value",
        "SINGLE": "single quoted",
        "SPACED": "padded value",
        "EMPTY": "",
    }


def test_load_env_file_keeps_equals_and_mismatched_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'URL=postgres://u:p@host/db?sslmode=require\nODD="half quoted\n',
        encoding="utf-8",
    )
    assert load_env_file(env_file) == {
        "URL": "postgres://u:p@host/db?sslmode=require",
        "ODD": '"half quoted',
    }


@pytest.mark.parametrize(
    "content,match",
    [
        ("NOT A LINE\n", "expected KEY=VALUE"),
        ("1BAD=value\n", "expected KEY=VALUE"),
        ("KEY=a\nKEY=b\n", "duplicate key 'KEY'"),
    ],
)
def test_load_env_file_rejects_malformed_input(tmp_path, content, match):
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    with pytest.raises(StackValidationError, match=match):
        load_env_file(env_file)


def test_load_env_file_missing_file(tmp_path):
    with pytest.raises(StackValidationError, match="cannot read env file"):
        load_env_file(tmp_path / "absent.env")


def test_stack_environ_without_env_file_is_none(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    assert stack_environ(manifest) is None


def test_stack_environ_process_env_overrides_file(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "FROM_FILE=file\nOVERRIDDEN=file\n", encoding="utf-8"
    )
    merged = stack_environ(manifest, environ={"OVERRIDDEN": "process"})
    assert merged == {"FROM_FILE": "file", "OVERRIDDEN": "process"}


def test_stack_environ_explicit_file_must_exist(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(StackValidationError, match="cannot read env file"):
        stack_environ(manifest, tmp_path / "missing.env", environ={})


def test_stack_environ_explicit_file_beats_adjacent_dotenv(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=adjacent\n", encoding="utf-8")
    other = tmp_path / "prod.env"
    other.write_text("KEY=explicit\n", encoding="utf-8")
    merged = stack_environ(manifest, other, environ={})
    assert merged == {"KEY": "explicit"}


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unsupported")
def test_stack_environ_rejects_dotenv_symlink_escape(tmp_path):
    outside = tmp_path / "outside.env"
    outside.write_text("KEY=value\n", encoding="utf-8")
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    manifest = stack_dir / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    os.symlink(outside, stack_dir / ".env")
    with pytest.raises(StackValidationError, match="escapes the stack directory"):
        stack_environ(manifest, environ={})


def test_env_file_values_feed_from_env_resolution(tmp_path):
    manifest = tmp_path / "oduflow.yaml"
    manifest.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("FS_ESL_PASSWORD=s3cret\n", encoding="utf-8")
    from oduflow.stack_models import ValueFrom

    merged = stack_environ(manifest, environ={})
    resolved = resolve_env_values(
        {"ESL_PASSWORD": ValueFrom(from_env="FS_ESL_PASSWORD")}, environ=merged
    )
    assert resolved == {"ESL_PASSWORD": "s3cret"}
