"""A tree its tenant can write to is copied and re-owned without following links.

A filestore is mounted read-write into its tenant's Odoo container, so the
tenant decides what is in it — including a symbolic link such as
``aa/x -> /etc/shadow`` or a link to another team's data directory. Oduflow
handles these trees as the account it runs as, usually root, on the host:

- ``shutil.copytree`` follows links, so publishing the environment as a
  template and creating an environment (or a production) from it delivered the
  host file's content back to the tenant as an ordinary file in its filestore;
- ``os.chown`` follows links, so fixing the tree's ownership handed the host
  file to the Odoo account.

These tests pin that a link stays a link in every copy, and that ownership is
changed on the link itself.
"""

import ast
import os
import pathlib
import stat
from unittest.mock import MagicMock, patch

import pytest

from oduflow import template_generations
from oduflow.docker_ops import client as docker_client
from oduflow.docker_ops import env_ops, system_ops
from oduflow.fsutil import copy_tenant_tree
from oduflow.settings import Settings, TeamSettings

SECRET = "the host's own file"


def _tenant_tree(tmp_path, *, fifo=False):
    """A filestore whose tenant planted links out of it, and the host files
    those links name."""
    outside = tmp_path / "host"
    (outside / "team_2").mkdir(parents=True)
    (outside / "shadow").write_text(SECRET)
    (outside / "team_2" / "dump.pgdump").write_text(SECRET)
    tree = tmp_path / "filestore"
    (tree / "aa").mkdir(parents=True)
    (tree / "aa" / "attachment").write_text("an attachment")
    os.symlink(outside / "shadow", tree / "aa" / "file-link")
    os.symlink(outside / "team_2", tree / "dir-link")
    os.symlink("/nonexistent/in/the/container/only", tree / "aa" / "dangling")
    if fifo:
        os.mkfifo(tree / "aa" / "fifo")
    return tree, outside


def _assert_links_were_not_followed(copy, outside):
    """*copy* holds the tenant's links as links and nothing of the host's."""
    copy = pathlib.Path(copy)
    assert (copy / "aa" / "attachment").read_text() == "an attachment"
    for link, target in (
        (copy / "aa" / "file-link", outside / "shadow"),
        (copy / "dir-link", outside / "team_2"),
        (copy / "aa" / "dangling", "/nonexistent/in/the/container/only"),
    ):
        assert link.is_symlink(), link
        assert os.readlink(link) == str(target)
    for dirpath, _dirs, files in os.walk(copy):
        for name in files:
            path = pathlib.Path(dirpath) / name
            if not path.is_symlink():
                assert path.read_text() != SECRET, path
    # Nothing was hardlinked out of the host's files either.
    assert (outside / "shadow").stat().st_nlink == 1
    assert (outside / "team_2" / "dump.pgdump").stat().st_nlink == 1


# --------------------------------------------------------------------------
# copies
# --------------------------------------------------------------------------


def test_a_plain_copytree_would_have_followed_the_links(tmp_path):
    # The behaviour the helper exists to avoid, so the tests below mean
    # something: shutil's default delivers the host file's content.
    import shutil

    tree, _outside = _tenant_tree(tmp_path)
    (tree / "aa" / "dangling").unlink()
    shutil.copytree(tree, tmp_path / "copy")

    leaked = tmp_path / "copy" / "aa" / "file-link"
    assert not leaked.is_symlink()
    assert leaked.read_text() == SECRET
    assert (tmp_path / "copy" / "dir-link" / "dump.pgdump").read_text() == SECRET


def test_copy_tenant_tree_keeps_links_and_leaves_special_files_out(tmp_path):
    tree, outside = _tenant_tree(tmp_path, fifo=True)

    # A FIFO would block a plain copy forever; here it is not opened at all.
    copy_tenant_tree(str(tree), str(tmp_path / "copy"))

    _assert_links_were_not_followed(tmp_path / "copy", outside)
    assert not os.path.lexists(tmp_path / "copy" / "aa" / "fifo")


def test_copy_tenant_tree_merges_into_an_existing_directory(tmp_path):
    tree, outside = _tenant_tree(tmp_path)
    dest = tmp_path / "copy"
    dest.mkdir()

    copy_tenant_tree(str(tree), str(dest), dirs_exist_ok=True)

    _assert_links_were_not_followed(dest, outside)


def test_copy_tenant_tree_does_not_hardlink_what_a_link_names(tmp_path):
    tree, outside = _tenant_tree(tmp_path)

    copy_tenant_tree(str(tree), str(tmp_path / "copy"), copy_function=os.link)

    _assert_links_were_not_followed(tmp_path / "copy", outside)
    # Regular files are still shared with the source, which is the point.
    assert (tree / "aa" / "attachment").stat().st_nlink == 2


def test_a_file_swapped_for_a_link_during_the_copy_is_copied_as_a_link(tmp_path):
    from oduflow import fsutil

    outside = tmp_path / "shadow"
    outside.write_text(SECRET)
    swapped = tmp_path / "swapped"
    os.symlink(outside, swapped)

    fsutil._copy_without_following(str(swapped), str(tmp_path / "copied"))

    assert (tmp_path / "copied").is_symlink()


def test_every_tree_copy_goes_through_the_guarded_helper():
    """``shutil.copytree`` is called in one place, with the links kept.

    The copies of filestores live in docker_ops and template_generations; a
    new one that calls shutil directly would follow links again.
    """
    src = pathlib.Path(system_ops.__file__).resolve().parents[1]
    callers: dict[str, list[ast.Call]] = {}
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "copytree"
            ):
                callers.setdefault(path.relative_to(src).as_posix(), []).append(node)

    # extra_addons copies a git checkout, not a filestore, and keeps links.
    assert sorted(callers) == ["extra_addons.py", "fsutil.py"]
    for calls in callers.values():
        for call in calls:
            keywords = {kw.arg: kw.value for kw in call.keywords}
            assert isinstance(keywords.get("symlinks"), ast.Constant)
            assert keywords["symlinks"].value is True


def test_a_template_copied_into_an_environment_keeps_its_links(tmp_path):
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "shadow").write_text(SECRET)
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team_1"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    template_fs = pathlib.Path(team.get_template_filestore_path("leak"))
    (template_fs / "aa").mkdir(parents=True)
    (template_fs / "aa" / "attachment").write_text("an attachment")
    # What publishing an environment leaves in a template: rsync -a keeps the
    # link the tenant made.
    os.symlink(outside / "shadow", template_fs / "aa" / "file-link")
    pathlib.Path(team.get_template_metadata_path("leak")).write_text(
        '{"odoo_image": "odoo:17.0", "use_overlay": false}'
    )
    odoo_volumes: dict = {}

    with (
        patch.object(env_ops, "get_odoo_uid_gid", return_value="0:0"),
        patch.object(env_ops, "chown_recursive"),
    ):
        env_ops._mount_filestore(
            MagicMock(),
            settings,
            team,
            env_name="myenv",
            env_db="myenv_db",
            odoo_image="odoo:17.0",
            odoo_volumes=odoo_volumes,
            template_name="leak",
        )

    merged = pathlib.Path(next(iter(odoo_volumes)))
    assert (merged / "aa" / "attachment").read_text() == "an attachment"
    assert (merged / "aa" / "file-link").is_symlink()
    assert os.readlink(merged / "aa" / "file-link") == str(outside / "shadow")


def test_a_retained_generation_does_not_hardlink_what_a_link_names(tmp_path):
    tree, outside = _tenant_tree(tmp_path)
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    template_fs = pathlib.Path(team.get_template_filestore_path("prod"))
    template_fs.parent.mkdir(parents=True)
    os.rename(tree, template_fs)

    generation = template_generations.snapshot(team, "prod")

    retained = pathlib.Path(team.data_dir) / "template_generations" / generation
    _assert_links_were_not_followed(retained / "filestore", outside)


# --------------------------------------------------------------------------
# publishing an environment
# --------------------------------------------------------------------------


def test_a_published_snapshot_keeps_links_and_drops_special_files(tmp_path):
    tree, outside = _tenant_tree(tmp_path, fifo=True)
    dest = tmp_path / "snapshot"

    transferred = system_ops._snapshot_filestore(str(tree), str(dest), link_dests=[])

    assert transferred is not None, "rsync path expected on a dev/CI host"
    _assert_links_were_not_followed(dest, outside)
    assert not os.path.lexists(dest / "aa" / "fifo")


def test_the_snapshot_rsync_leaves_devices_and_special_files_out(tmp_path):
    import subprocess

    tree, _outside = _tenant_tree(tmp_path)

    with patch.object(system_ops.subprocess, "run", wraps=subprocess.run) as run:
        system_ops._snapshot_filestore(
            str(tree), str(tmp_path / "snapshot"), link_dests=[]
        )

    cmd = run.call_args.args[0]
    assert cmd[:2] == ["rsync", "-a"]
    # After -a, which would turn both on.
    assert cmd.index("--no-devices") > cmd.index("-a")
    assert cmd.index("--no-specials") > cmd.index("-a")
    # Links are kept as links, not followed: none of the options that would.
    assert not {"-L", "--copy-links", "--copy-unsafe-links", "-k"} & set(cmd)


def test_the_full_copy_fallback_of_a_snapshot_keeps_links(tmp_path):
    tree, outside = _tenant_tree(tmp_path, fifo=True)
    dest = tmp_path / "snapshot"

    with patch.object(system_ops.shutil, "which", return_value=None):
        transferred = system_ops._snapshot_filestore(
            str(tree), str(dest), link_dests=[]
        )

    assert transferred is None
    _assert_links_were_not_followed(dest, outside)


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------


def _recorded_chowns(module):
    """Run with ``os.chown`` recorded and resolved the way the kernel would."""
    calls: list[tuple[str, bool]] = []

    def record(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None:
            path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
        calls.append((str(path), follow_symlinks))

    return patch.object(module.os, "chown", side_effect=record), calls


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"), reason="needs /proc to name a dir_fd"
)
def test_chown_recursive_gives_links_away_as_links(tmp_path):
    tree, outside = _tenant_tree(tmp_path)
    patched, calls = _recorded_chowns(docker_client)

    with patched:
        docker_client.chown_recursive(str(tree), 101, 101, MagicMock(), "odoo:17.0")

    changed = dict(calls)
    assert set(changed) == {
        str(tree),
        str(tree / "aa"),
        str(tree / "aa" / "attachment"),
        str(tree / "aa" / "file-link"),
        str(tree / "aa" / "dangling"),
        str(tree / "dir-link"),
    }
    # Only the top, which is Oduflow's own path, is followed.
    assert [path for path, follow in calls if follow] == [str(tree)]
    # Nothing under what the links name was walked into.
    assert not [path for path in changed if path.startswith(str(outside))]


def test_chown_recursive_does_not_fail_on_a_dangling_link(tmp_path):
    # Inside the container a link such as /usr/lib/x resolves; on the host it
    # dangles, and following it used to fail the whole operation.
    tree, outside = _tenant_tree(tmp_path)
    before = (outside / "shadow").stat()

    docker_client.chown_recursive(
        str(tree), os.getuid(), os.getgid(), MagicMock(), "odoo:17.0"
    )

    after = (outside / "shadow").stat()
    assert (after.st_uid, after.st_gid, after.st_ctime_ns) == (
        before.st_uid,
        before.st_gid,
        before.st_ctime_ns,
    )


def test_chown_recursive_of_a_missing_tree_does_nothing(tmp_path):
    client = MagicMock()

    docker_client.chown_recursive(
        str(tmp_path / "absent"), 101, 101, client, "odoo:17.0"
    )

    client.containers.run.assert_not_called()


def test_the_container_chown_fallback_does_not_follow_links_either(tmp_path):
    tree, _outside = _tenant_tree(tmp_path)
    client = MagicMock()

    with patch.object(docker_client.os, "chown", side_effect=PermissionError):
        docker_client.chown_recursive(str(tree), 101, 102, client, "odoo:17.0")

    args, kwargs = client.containers.run.call_args
    assert args == ("odoo:17.0", "chown -hR 101:102 /mnt/target")
    assert kwargs["volumes"] == {str(tree): {"bind": "/mnt/target", "mode": "rw"}}


def test_transferred_paths_are_given_away_without_following_links(tmp_path):
    root = tmp_path / "filestore"
    (root / "aa").mkdir(parents=True)
    (root / "aa" / "attachment").write_text("an attachment")
    # A link that stays inside the tree passes the containment check.
    os.symlink("attachment", root / "aa" / "inner-link")
    patched, calls = _recorded_chowns(system_ops)

    with patched:
        system_ops._chown_filestore(
            str(root),
            ["aa/attachment", "aa/inner-link"],
            101,
            102,
            MagicMock(),
            "odoo:19.0",
        )

    assert dict(calls) == {
        str(root): True,
        str(root / "aa" / "attachment"): False,
        str(root / "aa" / "inner-link"): False,
    }
    assert stat.S_ISLNK(os.lstat(root / "aa" / "inner-link").st_mode)
