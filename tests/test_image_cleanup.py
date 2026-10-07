from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

import docker
from oduflow.docker_ops import image_cleanup
from oduflow.docker_ops.image_cleanup import (
    list_unused_images,
    note_image_requested,
    remove_unused_images,
)


def _id(char):
    return "sha256:" + char * 64


def _summary(char, tags=None, parent=""):
    return {"Id": _id(char), "RepoTags": tags, "ParentId": parent, "Size": 1234}


def _container(char):
    return SimpleNamespace(attrs={"ImageID": _id(char)})


def _client(summaries=(), containers=(), local=None):
    """Fake Docker client; `local` maps refs and IDs to inspectable images."""
    local = dict(local or {})
    client = MagicMock()
    client.api.images.return_value = list(summaries)
    client.containers.list.return_value = list(containers)

    def get(ref):
        if ref not in local:
            raise docker.errors.ImageNotFound("missing")
        return local[ref]

    client.images.get.side_effect = get
    return client


def _image(char, tags=()):
    return SimpleNamespace(id=_id(char), tags=list(tags))


@pytest.fixture(autouse=True)
def _no_requested_images():
    image_cleanup._requested.clear()
    yield
    image_cleanup._requested.clear()


def _patched(client):
    return patch("oduflow.docker_ops.image_cleanup.get_client", return_value=client)


def test_preview_keeps_used_parent_staging_and_requested_images():
    client = _client(
        summaries=[
            _summary("a", ["odoo:running"]),
            _summary("b", ["odoo:stopped"]),
            _summary("c", ["odoo:old", "odoo:previous"]),
            _summary("d", None),
            _summary("e", ["<none>:<none>"], parent=_id("f")),
            _summary("f", None),  # intermediate layer of e
            _summary("g", ["base:1"]),  # tagged parent of h's layer chain
            _summary("h", ["child:1"], parent=_id("g")),
            _summary("i", ["oduflow-build/team-2:bld-0123456789ab"]),
            _summary("j", ["private/odoo:offline"]),
        ],
        containers=[_container("a"), _container("b"), _container("h")],
        local={"private/odoo:offline": _image("j")},
    )
    note_image_requested("private/odoo:offline")
    with _patched(client):
        result = list_unused_images()
    assert [image["id"] for image in result] == [_id("c"), _id("d"), _id("e")]
    assert result[0]["tags"] == ["odoo:old", "odoo:previous"]
    assert result[2]["tags"] == []
    client.containers.list.assert_called_once_with(all=True, sparse=True)
    client.api.images.assert_called_once_with(all=True)
    client.images.remove.assert_not_called()


def test_removal_lists_containers_once_and_skips_images_that_must_stay():
    images = {
        _id("a"): _image("a", ["odoo:old"]),
        _id("b"): _image("b", ["odoo:now-used"]),
        _id("c"): _image("c", ["oduflow-build/team-1:bld-0123456789ab"]),
        _id("d"): _image("d", ["base:1"]),
        _id("e"): _image("e", ["private/odoo:offline"]),
        _id("f"): _image("f"),
        "private/odoo:offline": _image("e", ["private/odoo:offline"]),
    }
    client = _client(
        summaries=[_summary("x", ["child:1"], parent=_id("d"))],
        containers=[_container("b")],
        local=images,
    )
    note_image_requested("private/odoo:offline")
    with _patched(client):
        result = remove_unused_images(
            [_id(c) for c in "abcdef"] + [_id("a")]  # duplicates are ignored
        )
    assert result["removed"] == [_id("a"), _id("f")]
    assert {item["id"]: item["reason"] for item in result["skipped"]} == {
        _id("b"): "Now used by a container.",
        _id("c"): "Image build staging image.",
        _id("d"): "Now has child images.",
        _id("e"): "Recently requested by an Oduflow operation.",
    }
    assert client.images.remove.call_args_list == [
        call(_id("a"), force=False, noprune=True),
        call(_id("f"), force=False, noprune=True),
    ]
    client.containers.list.assert_called_once_with(all=True, sparse=True)


def test_multi_tag_image_is_untagged_before_removal_by_id():
    client = _client(local={_id("a"): _image("a", ["odoo:17", "odoo:17.0", "x:1"])})
    with _patched(client):
        result = remove_unused_images([_id("a")])
    assert result["removed"] == [_id("a")]
    assert client.images.remove.call_args_list == [
        call("odoo:17.0", force=False, noprune=True),
        call("x:1", force=False, noprune=True),
        call(_id("a"), force=False, noprune=True),
    ]


def test_requested_image_lease_expires():
    client = _client(local={_id("a"): _image("a"), "odoo:17": _image("a")})
    note_image_requested("odoo:17")
    image_cleanup._requested["odoo:17"] -= image_cleanup._REQUEST_LEASE_SECONDS + 1
    with _patched(client):
        result = remove_unused_images([_id("a")])
    assert result["removed"] == [_id("a")]
    assert image_cleanup._requested == {}


@pytest.mark.parametrize("status", [409, 500])
def test_docker_conflict_or_failure_is_reported_and_other_images_continue(status):
    client = _client(local={_id("a"): _image("a"), _id("b"): _image("b")})
    client.images.remove.side_effect = [
        docker.errors.APIError(
            "private daemon details",
            response=SimpleNamespace(
                status_code=status, url="docker://local", reason="failed"
            ),
        ),
        None,
    ]
    with _patched(client):
        result = remove_unused_images([_id("a"), _id("b")])
    assert result["removed"] == [_id("b")]
    assert result["errors"][0]["id"] == _id("a")
    assert "private daemon details" not in str(result)


def test_already_removed_image_is_skipped():
    client = _client()
    with _patched(client):
        result = remove_unused_images([_id("a")])
    assert result["removed"] == []
    assert result["errors"] == []
    assert result["skipped"][0]["reason"] == "Already removed."
