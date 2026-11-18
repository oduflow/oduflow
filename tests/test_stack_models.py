import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from oduflow.stack_loader import (
    StackValidationError,
    load_stack,
    read_stack_file,
)
from oduflow.stack_models import Service, ServiceRoute, StackManifest

VALID = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: acme
spec:
  environment:
    name: acme-dev
    branch: main
    repoUrl: https://github.com/acme/addons.git
    odooImage: odoo:18.0
    template: default
    env:
      LOG_LEVEL: info
    modules:
      install: [acme_base]
  extraRepositories:
    enterprise:
      repoUrl: https://github.com/odoo/enterprise.git
      branch: "18.0"
  volumes:
    fs-data:
      description: FreeSWITCH data
  files:
    - source: files/fs.conf
      volume: fs-data
      path: config/fs.conf
  services:
    fs:
      image: example/fs:1
      port: 8080
      env:
        ODOO_URL:
          environmentField: url
        ESL_PASSWORD:
          fromEnv: FS_ESL_PASSWORD
      volumes:
        - source: fs-data
          target: /data
"""


def _write(tmp_path: Path, content: str = VALID) -> Path:
    path = tmp_path / "oduflow.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_valid_stack(tmp_path):
    manifest = load_stack(_write(tmp_path))

    assert manifest.metadata.name == "acme"
    assert manifest.spec.environment.odoo_image == "odoo:18.0"
    assert manifest.spec.services["fs"].env["ODOO_URL"].environment_field == "url"


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        VALID.replace("  name: acme\n", "  name: acme\n  name: duplicate\n", 1),
    )

    with pytest.raises(StackValidationError, match="duplicate YAML key 'name'"):
        load_stack(path)


def test_unknown_fields_are_rejected(tmp_path):
    path = _write(tmp_path, VALID.replace("spec:\n", "spec:\n  unsupported: true\n"))

    with pytest.raises(StackValidationError, match="unsupported"):
        load_stack(path)


def test_undeclared_volume_reference_is_rejected(tmp_path):
    path = _write(tmp_path, VALID.replace("volume: fs-data", "volume: missing"))

    with pytest.raises(StackValidationError, match="undeclared volume 'missing'"):
        load_stack(path)


def test_environment_cannot_reference_its_own_token(tmp_path):
    path = _write(
        tmp_path,
        VALID.replace(
            "LOG_LEVEL: info",
            "LOG_LEVEL:\n        environmentField: token",
        ),
    )

    with pytest.raises(StackValidationError, match="cannot reference environmentField"):
        load_stack(path)


def test_service_database_reference_must_be_declared(tmp_path):
    path = _write(
        tmp_path,
        VALID.replace(
            "environmentField: url",
            "database: events\n          databaseField: url",
        ),
    )

    with pytest.raises(StackValidationError, match="undeclared database 'events'"):
        load_stack(path)


def test_service_database_reference_is_typed(tmp_path):
    content = VALID.replace(
        "  volumes:\n",
        "  databases:\n    events: {}\n  volumes:\n",
        1,
    ).replace(
        "environmentField: url",
        "database: events\n          databaseField: password",
    )

    manifest = load_stack(_write(tmp_path, content))

    value = manifest.spec.services["fs"].env["ODOO_URL"]
    assert value.database == "events"
    assert value.database_field == "password"


def test_host_mode_service_cannot_reference_managed_database(tmp_path):
    content = (
        VALID.replace(
            "  volumes:\n",
            "  databases:\n    events: {}\n  volumes:\n",
            1,
        )
        .replace(
            "environmentField: url",
            "database: events\n          databaseField: url",
        )
        .replace(
            "      image: example/fs:1\n",
            "      image: example/fs:1\n      hostMode: true\n",
        )
    )

    with pytest.raises(StackValidationError, match="host-mode service 'fs'"):
        load_stack(_write(tmp_path, content))


def test_odoo_environment_cannot_reference_managed_database(tmp_path):
    content = VALID.replace(
        "  volumes:\n",
        "  databases:\n    events: {}\n  volumes:\n",
        1,
    ).replace(
        "LOG_LEVEL: info",
        "LOG_LEVEL:\n        database: events\n        databaseField: url",
    )

    with pytest.raises(StackValidationError, match="only available to auxiliary"):
        load_stack(_write(tmp_path, content))


def test_stack_file_must_stay_inside_manifest_directory(tmp_path):
    path = _write(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(StackValidationError, match="escapes the stack directory"):
        read_stack_file(path, "../outside.txt")


def test_service_route_paths_are_canonicalized_before_duplicate_validation():
    route = ServiceRoute(path="/api/", port=8080)

    assert route.path == "/api"
    with pytest.raises(ValidationError, match="duplicate paths"):
        Service(
            image="example/service:1",
            routes=[
                ServiceRoute(path="/api", port=8080),
                ServiceRoute(path="/api/", port=8081),
            ],
        )


def test_service_command_accepts_argv_list_and_shell_string():
    assert Service(
        image="example/service:1", port=1, command=["server", "/data"]
    ).command == [
        "server",
        "/data",
    ]
    # The string form is the one operators type; it is split with shell rules.
    assert Service(
        image="example/service:1", port=1, command='server "/data dir"'
    ).command == ["server", "/data dir"]
    assert Service(image="example/service:1", port=1).command == []


def test_service_command_preserves_argument_whitespace():
    assert Service(
        image="example/service:1", port=1, command=["printf", "  value  "]
    ).command == ["printf", "  value  "]
    assert Service(
        image="example/service:1", port=1, command='printf "  value  "'
    ).command == ["printf", "  value  "]


def test_service_command_json_schema_accepts_argv_list_and_shell_string():
    command_schema = StackManifest.model_json_schema(by_alias=True)["$defs"]["Service"][
        "properties"
    ]["command"]

    assert {branch["type"] for branch in command_schema["anyOf"]} == {
        "array",
        "string",
    }


def test_shipped_json_schema_matches_models():
    schema_path = (
        Path(__file__).parents[1] / "src/oduflow/schemas/oduflow-stack-v1alpha1.json"
    )
    shipped = json.loads(schema_path.read_text(encoding="utf-8"))

    assert shipped == StackManifest.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            {
                "extraRepositories": {
                    "addons": {
                        "repoUrl": "https://github.com/acme/app",
                        "branch": "main",
                    }
                }
            },
            "extraRepositories requires",
        ),
        (
            {
                "services": {
                    "api": {
                        "image": "api",
                        "port": 80,
                        "env": {"URL": {"environmentField": "url"}},
                    }
                }
            },
            "environmentField requires",
        ),
        (
            {
                "services": {
                    "api": {
                        "image": "api",
                        "port": 80,
                        "env": {"URL": {"productionField": "url"}},
                    }
                }
            },
            "productionField requires",
        ),
        ({"databases": {"app": {"cluster": "primary"}}}, "cluster"),
        ({}, "at least one resource"),
    ],
)
def test_service_stack_rejects_invalid_spec(spec, message):
    with pytest.raises(ValidationError, match=message):
        StackManifest.model_validate({"metadata": {"name": "app"}, "spec": spec})
