import json
from types import SimpleNamespace

import pytest

from oduflow.service_runtime import inspect_runtime, normalize_runtime
from oduflow.stack_models import Service


def test_runtime_round_trip_and_explicit_clear():
    value = {
        "tmpfs": {"/run": "rw,nosuid,nodev,mode=755"},
        "cgroupns": "private",
        "stop_signal": "SIGRTMIN+3",
        "stop_timeout": 180,
    }
    container = SimpleNamespace(labels={"oduflow.runtime": json.dumps(value)})
    assert inspect_runtime(container) == normalize_runtime(value) == value
    assert normalize_runtime({}) == {}
    service = Service(image="fixture:1", port=65535, runtime=value)
    assert normalize_runtime(service.runtime.model_dump()) == value


@pytest.mark.parametrize(
    "value",
    [
        {"privileged": True},
        {"cgroupns": "host"},
        {"tmpfs": {"/etc": "rw"}},
        {"stop_timeout": 0},
        {"stop_timeout": True},
        {"stop_signal": "SIGKILL"},
    ],
)
def test_invalid_runtime_rejected_before_docker(value):
    with pytest.raises(ValueError):
        normalize_runtime(value)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"stop_timeout": 30, "init": True}),
        json.dumps({"stop_signal": "SIGUSR1"}),
    ],
)
def test_invalid_runtime_label_ignored(raw):
    """A label from another Oduflow version must not break lifecycle ops."""
    container = SimpleNamespace(labels={"oduflow.runtime": raw})
    assert inspect_runtime(container) == {}


def test_stack_manifest_accepts_camel_case_runtime():
    service = Service.model_validate(
        {
            "image": "fixture:1",
            "port": 8080,
            "runtime": {"stopSignal": "SIGRTMIN+3", "stopTimeout": 180},
        }
    )
    assert normalize_runtime(service.runtime.model_dump(exclude_none=True)) == {
        "stop_signal": "SIGRTMIN+3",
        "stop_timeout": 180,
    }
