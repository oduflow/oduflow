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
