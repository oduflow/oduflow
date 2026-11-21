import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from oduflow.stack_loader import (
    StackValidationError,
    load_stack,
    read_stack_file,
)
from oduflow.stack_models import Service, ServiceRoute, StackManifest, ValueFrom

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


SERVICE_REFERENCES = """\
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: inbox
spec:
  environment:
    name: inbox-dev
    branch: main
    repoUrl: https://github.com/acme/addons.git
    odooImage: odoo:18.0
  services:
    app:
      image: example/app:1
      port: 3000
      env:
        UPSTASH_REDIS_URL:
          service: redis-http
          serviceField: containerName
          format: "http://{value}:80"
    redis-http:
      image: hiett/serverless-redis-http:0.0.10
      port: 8888
      env:
        REDIS_HOST:
          service: redis
          serviceField: containerName
    redis:
      image: redis:7
      port: 8888
"""

_DEV_TARGET = {
    "name": "acme-dev",
    "branch": "main",
    "repoUrl": "https://github.com/acme/addons.git",
    "odooImage": "odoo:18.0",
}
_PROD_TARGET = {
    "name": "control",
    "domain": "example.org",
    "repoUrl": "https://github.com/acme/app.git",
    "branch": "production",
    "odooImage": "odoo:19.0",
}


def _ref(service: str, **extra: str) -> dict[str, str]:
    return {"service": service, "serviceField": "containerName", **extra}


def _svc(env: dict | None = None, **extra: object) -> dict:
    return {"image": "example/svc:1", "port": 8080, "env": env or {}, **extra}


def _stack(services: dict, *, target_env: dict | None = None, production=False):
    target = dict(_PROD_TARGET if production else _DEV_TARGET, env=target_env or {})
    return StackManifest.model_validate(
        {
            "metadata": {"name": "acme"},
            "spec": {
                "production" if production else "environment": target,
                "services": services,
            },
        }
    )


def test_service_references_load_raw_and_formatted(tmp_path):
    manifest = load_stack(_write(tmp_path, SERVICE_REFERENCES))

    raw = manifest.spec.services["redis-http"].env["REDIS_HOST"]
    formatted = manifest.spec.services["app"].env["UPSTASH_REDIS_URL"]
    assert (raw.service, raw.service_field, raw.format) == (
        "redis",
        "containerName",
        None,
    )
    assert (formatted.service, formatted.format) == ("redis-http", "http://{value}:80")


def test_service_reference_round_trips_through_its_dump():
    manifest = _stack(
        {
            "redis": _svc(),
            "app": _svc({"REDIS_URL": _ref("redis", format="redis://{value}:6379")}),
        }
    )

    dumped = manifest.model_dump(mode="json", by_alias=True)

    assert dumped["spec"]["services"]["app"]["env"]["REDIS_URL"] == {
        "fromEnv": None,
        "environmentField": None,
        "productionField": None,
        "database": None,
        "databaseField": None,
        "service": "redis",
        "serviceField": "containerName",
        "format": "redis://{value}:6379",
    }
    assert StackManifest.model_validate(dumped) == manifest


def test_unset_service_source_keys_are_absent_from_dumps():
    """Ownership hashes are taken over these dumps; the later-added keys must
    not appear as nulls in a manifest that does not use them."""
    manifest = _stack({"svc": _svc({"TOKEN": {"fromEnv": "TOKEN"}})})

    dumped = manifest.spec.services["svc"].env["TOKEN"].model_dump(by_alias=True)

    assert not {"service", "serviceField", "format"} & set(dumped)


@pytest.mark.parametrize(
    "value,message",
    [
        ({"service": "redis"}, "service and serviceField must be set together"),
        ({"serviceField": "containerName"}, "set exactly one of"),
        ({"fromEnv": "HOST", "format": "{value}"}, "format is only allowed"),
        ({"service": "redis", "serviceField": "hostname"}, "containerName"),
        (dict(_ref("redis"), fromEnv="HOST"), "set exactly one of"),
        (dict(_ref("redis"), port=6379), "Extra inputs are not permitted"),
    ],
)
def test_service_reference_shape_is_strict(value, message):
    with pytest.raises(ValidationError, match=message):
        _stack({"redis": _svc(), "app": _svc({"REDIS_HOST": value})})


@pytest.mark.parametrize(
    "template,message",
    [
        ("http://redis:80", "exactly one '{value}'"),
        ("{value}:{value}", "exactly one '{value}'"),
        ("{value}:{port}", "braces other than '{value}'"),
        ("{{value}}", "braces other than '{value}'"),
        ("${value}}", "braces other than '{value}'"),
        ("{value}\x00", "NUL"),
    ],
)
def test_service_reference_format_is_narrow(template, message):
    with pytest.raises(ValidationError, match=f"format .*{message}"):
        _stack(
            {
                "redis": _svc(),
                "app": _svc({"REDIS_URL": _ref("redis", format=template)}),
            }
        )


def test_value_from_itself_refuses_an_invalid_format():
    """resolve_env_values trusts the model, so a ValueFrom built
    programmatically must be refused here, not silently misresolved later."""
    with pytest.raises(ValidationError, match="format must contain exactly one"):
        ValueFrom(
            service="redis", serviceField="containerName", format="redis://host:6379"
        )


def test_service_reference_format_whitespace_is_preserved():
    """The docs promise a literal one-shot replace, so declared whitespace
    must survive the model-wide strip_whitespace."""
    manifest = _stack(
        {"redis": _svc(), "app": _svc({"URL": _ref("redis", format=" {value} ")})}
    )

    assert manifest.spec.services["app"].env["URL"].format == " {value} "


def test_service_reference_format_is_not_a_template_language():
    """Shell-looking text around the one placeholder is passed through."""
    manifest = _stack(
        {"redis": _svc(), "app": _svc({"URL": _ref("redis", format="$HOME/{value}")})}
    )

    assert manifest.spec.services["app"].env["URL"].format == "$HOME/{value}"


def test_literal_braces_stay_literal_strings():
    manifest = _stack({"app": _svc({"TEMPLATE": "${REDIS_HOST}:{value}"})})

    assert manifest.spec.services["app"].env["TEMPLATE"] == "${REDIS_HOST}:{value}"


def test_service_reference_target_must_be_declared():
    with pytest.raises(
        ValidationError,
        match="service 'app' env 'REDIS_HOST' references undeclared service 'redis'",
    ):
        _stack({"app": _svc({"REDIS_HOST": _ref("redis")})})


def test_service_cannot_reference_itself():
    with pytest.raises(
        ValidationError,
        match="service 'redis' env 'REDIS_HOST' cannot reference itself",
    ):
        _stack({"redis": _svc({"REDIS_HOST": _ref("redis")})})


def test_service_reference_rejects_host_mode_target():
    with pytest.raises(
        ValidationError,
        match="service 'app' env 'REDIS_HOST' cannot reference host-mode service 'redis'",
    ):
        _stack(
            {"redis": _svc(hostMode=True), "app": _svc({"REDIS_HOST": _ref("redis")})}
        )


def test_host_mode_service_cannot_reference_a_sibling():
    with pytest.raises(
        ValidationError,
        match="host-mode service 'app' env 'REDIS_HOST' cannot reference",
    ):
        _stack(
            {"redis": _svc(), "app": _svc({"REDIS_HOST": _ref("redis")}, hostMode=True)}
        )


@pytest.mark.parametrize("production", [False, True])
def test_odoo_target_cannot_reference_a_service(production):
    kind = "production" if production else "environment"
    with pytest.raises(
        ValidationError,
        match=f"{kind} env 'REDIS_HOST' cannot reference a service; service "
        "references are only available to auxiliary services",
    ):
        _stack(
            {"redis": _svc()},
            target_env={"REDIS_HOST": _ref("redis")},
            production=production,
        )


@pytest.mark.parametrize(
    "graph,cycle",
    [
        ({"a": ["b"], "b": ["a"]}, "services.a -> services.b -> services.a"),
        (
            {"a": ["b"], "b": ["c"], "c": ["a"]},
            "services.a -> services.b -> services.c -> services.a",
        ),
        (
            {"entry": ["a"], "a": ["b"], "b": ["a"]},
            "services.a -> services.b -> services.a",
        ),
    ],
)
def test_service_reference_cycle_shows_its_path(graph, cycle):
    services = {
        name: _svc({f"{target.upper()}_HOST": _ref(target) for target in targets})
        for name, targets in graph.items()
    }

    with pytest.raises(ValidationError, match=f"service reference cycle: {cycle}"):
        _stack(services)


def test_shared_reference_target_is_not_a_cycle():
    manifest = _stack(
        {
            "app": _svc({"CACHE": _ref("redis"), "BRIDGE": _ref("redis-http")}),
            "redis-http": _svc({"REDIS_HOST": _ref("redis")}),
            "redis": _svc(),
        }
    )

    assert list(manifest.spec.services) == ["app", "redis-http", "redis"]


def test_cycle_in_yaml_fails_validation_before_planning(tmp_path):
    content = SERVICE_REFERENCES.replace(
        "    redis:\n      image: redis:7\n      port: 8888\n",
        "    redis:\n      image: redis:7\n      port: 8888\n      env:\n"
        "        UPSTREAM:\n          service: app\n          serviceField: containerName\n",
    )

    with pytest.raises(StackValidationError, match="service reference cycle"):
        load_stack(_write(tmp_path, content))
