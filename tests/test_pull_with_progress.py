"""Image pulls report progress instead of blocking silently."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from oduflow.docker_ops import client as client_module
from oduflow.docker_ops.client import pull_with_progress
from oduflow.errors import PrerequisiteNotMetError


def test_progress_is_throttled_but_milestones_are_not(monkeypatch, caplog):
    clock = iter([100.0, 105.0, 116.0, 117.0])
    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    client = MagicMock()
    client.api.pull.return_value = [
        {"status": "Pulling from oduist/oduflow-coder", "id": "0.3.4"},
        {
            "status": "Downloading",
            "progressDetail": {"current": 1, "total": 9},
            "progress": "[=>   ] 1MB/9MB",
            "id": "abc",
        },
        {
            "status": "Downloading",
            "progressDetail": {"current": 2, "total": 9},
            "progress": "[==>  ] 2MB/9MB",
            "id": "abc",
        },
        {"status": "Digest: sha256:feed"},
    ]

    with caplog.at_level(logging.INFO, logger="oduflow"):
        pull_with_progress(client, "oduist/oduflow-coder:0.3.4", "Agent image")

    client.api.pull.assert_called_once_with(
        "oduist/oduflow-coder:0.3.4", stream=True, decode=True
    )
    # The 5 s-old layer update is dropped; the one 16 s after the last line
    # and the progress-less milestones are kept.
    assert [record.getMessage() for record in caplog.records] == [
        "Agent image: Pulling from oduist/oduflow-coder 0.3.4",
        "Agent image: Downloading abc [==>  ] 2MB/9MB",
        "Agent image: Digest: sha256:feed",
    ]


def test_error_reported_in_stream_raises():
    client = MagicMock()
    client.api.pull.return_value = [{"error": "manifest unknown"}]

    with pytest.raises(PrerequisiteNotMetError, match="manifest unknown"):
        pull_with_progress(client, "oduist/oduflow-coder:9.9.9", "Agent image")
