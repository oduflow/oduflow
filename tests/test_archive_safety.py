"""Regression tests for archive extraction path-traversal guard (issue #43)."""

import io
import os
import stat
import tarfile
import zipfile

from oduflow import template_import
from oduflow.docker_ops import system_ops
from oduflow.docker_ops.system_ops import _is_within_directory


def test_within_directory_accepts_inside_paths(tmp_path):
    root = str(tmp_path)
    assert _is_within_directory(root, root)
    assert _is_within_directory(root, os.path.join(root, "a"))
    assert _is_within_directory(root, os.path.join(root, "a/b/c.dat"))


def test_within_directory_rejects_traversal(tmp_path):
    root = str(tmp_path / "filestore")
    os.makedirs(root)
    # Classic zip-slip members.
    assert not _is_within_directory(root, os.path.join(root, "../../etc/passwd"))
    assert not _is_within_directory(root, os.path.join(root, "../sibling"))
    assert not _is_within_directory(root, "/etc/passwd")
    # A sibling dir that merely shares a name prefix must not count as inside.
    assert not _is_within_directory(root, str(tmp_path / "filestore-evil"))


# --------------------------------------------------------------------------
# Hostile members. An uploaded or imported archive is its sender's to build:
# besides ``../`` names it can carry absolute names, symbolic and hard links
# to host files, and device nodes. None of them may leave a trace outside the
# destination, and no link may be created for a later member to write through.
# --------------------------------------------------------------------------

SHA = "609e7ca59cc05bf0de7233c6781a381b742a2931"


def _tar_member(tf, name, data=None, *, kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name=name)
    info.type = kind
    info.linkname = linkname
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    else:
        tf.addfile(info)


def _hostile_tar(path, secret, *, good_name):
    with tarfile.open(path, "w") as tf:
        _tar_member(tf, good_name, b"ok")
        _tar_member(tf, "/abs/escape", b"pwn")
        _tar_member(tf, "ab/../../escape", b"pwn")
        # A link to a host file, then a member written "through" it.
        _tar_member(tf, "ab/link", kind=tarfile.SYMTYPE, linkname=str(secret))
        _tar_member(tf, "ab/hard", kind=tarfile.LNKTYPE, linkname=str(secret))
        _tar_member(tf, "outdir", kind=tarfile.SYMTYPE, linkname=str(secret.parent))
        _tar_member(tf, "outdir/planted", b"pwn")
        _tar_member(tf, "ab/device", kind=tarfile.CHRTYPE)
        _tar_member(tf, "ab/fifo", kind=tarfile.FIFOTYPE)


def _only_regular_files(root):
    found = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            assert stat.S_ISDIR(mode) or stat.S_ISREG(mode), full
            if stat.S_ISREG(mode):
                with open(full, "rb") as fh:
                    found[os.path.relpath(full, root)] = fh.read()
    return found


def _host_secret(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    secret = host / "secret"
    secret.write_bytes(b"the host's own file")
    return secret


def _assert_host_untouched(tmp_path, secret):
    assert secret.read_bytes() == b"the host's own file"
    assert secret.stat().st_nlink == 1
    assert sorted(os.listdir(secret.parent)) == ["secret"]
    assert not (tmp_path / "escape").exists()
    assert not os.path.exists("/abs/escape")


def test_uploaded_filestore_tar_writes_only_regular_files_inside(tmp_path):
    secret = _host_secret(tmp_path)
    tar_path = tmp_path / "upload.tar"
    _hostile_tar(tar_path, secret, good_name="ab/file")
    dest = tmp_path / "staging" / "filestore"

    written = system_ops.extract_filestore_tar(str(tar_path), str(dest))

    # "outdir/planted" lands in a real directory of that name: the link that
    # would have redirected it was never created.
    assert _only_regular_files(dest) == {"ab/file": b"ok", "outdir/planted": b"pwn"}
    assert written == 2
    _assert_host_untouched(tmp_path, secret)


def test_attached_filestore_tar_takes_only_attachment_shaped_files(tmp_path):
    secret = _host_secret(tmp_path)
    tar_path = tmp_path / "filestore.tar"
    _hostile_tar(tar_path, secret, good_name=f"60/{SHA}")
    with tarfile.open(tar_path, "a") as tf:
        # Attachment-shaped names that are links, not files.
        _tar_member(tf, f"60/{SHA[:-1]}0", kind=tarfile.SYMTYPE, linkname=str(secret))
        _tar_member(tf, f"60/{SHA[:-1]}1", kind=tarfile.LNKTYPE, linkname=str(secret))
    dest = tmp_path / "prepared"
    dest.mkdir()

    written, _prefix = system_ops._extract_tar_filestore(str(tar_path), str(dest))

    assert written == 1
    assert _only_regular_files(dest) == {f"60/{SHA}": b"ok"}
    _assert_host_untouched(tmp_path, secret)


def _zip_symlink(zf, name, target):
    info = zipfile.ZipInfo(name)
    info.create_system = 3  # Unix: the mode bits below mark a symbolic link
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    zf.writestr(info, target)


def test_attached_filestore_zip_skips_links_and_escaping_members(tmp_path):
    secret = _host_secret(tmp_path)
    zip_path = tmp_path / "filestore.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"60/{SHA}", b"ok")
        zf.writestr("../escape", b"pwn")
        zf.writestr("/abs/escape", b"pwn")
        _zip_symlink(zf, f"60/{SHA[:-1]}0", str(secret))
    dest = tmp_path / "prepared"
    dest.mkdir()

    written, _prefix = system_ops._extract_zip_filestore(str(zip_path), str(dest))

    assert written == 1
    assert _only_regular_files(dest) == {f"60/{SHA}": b"ok"}
    _assert_host_untouched(tmp_path, secret)


def test_odoo_backup_zip_never_creates_a_link(tmp_path):
    secret = _host_secret(tmp_path)
    zip_path = tmp_path / "backup.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("dump.sql", b"-- sql")
        zf.writestr("filestore/60/" + SHA, b"ok")
        zf.writestr("filestore/../../escape", b"pwn")
        zf.writestr("filestore//abs/escape", b"pwn")
        # A link member is stored as its target's path; unpacked as a link, the
        # member after it would be written into the host directory.
        _zip_symlink(zf, "filestore/outdir", str(secret.parent))
        _zip_symlink(zf, "filestore/60/link", str(secret))
    dump = tmp_path / "template" / "dump.sql"
    filestore = tmp_path / "template" / "filestore"

    template_import.extract_odoo_backup_zip(str(zip_path), str(dump), str(filestore))

    assert dump.read_bytes() == b"-- sql"
    # The links arrive as small regular files holding the target's name.
    assert _only_regular_files(filestore) == {
        f"60/{SHA}": b"ok",
        "60/link": str(secret).encode(),
        "outdir": str(secret.parent).encode(),
    }
    _assert_host_untouched(tmp_path, secret)
