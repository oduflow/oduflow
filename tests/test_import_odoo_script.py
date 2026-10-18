"""Run the real import-odoo.sh client against a fake Odoo.sh home and a stub curl."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "oduflow"
    / "templates"
    / "import-odoo.sh"
)
DB = "acme-main-123"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Answers the importer's requests: /status reports the manifest and dump as
# already uploaded (so no dd-based dump upload runs), /finalize succeeds, and
# streamed bodies (-T -) are drained. Every call is logged as a JSON argv line.
_CURL_STUB = """\
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
url = next((a for a in args if a.startswith("http")), "")
out = args[args.index("-o") + 1] if "-o" in args else None
if "-T" in args:
    sys.stdin.buffer.read()
if url.endswith("/api/templates/import/status"):
    body = {"ok": True, "progress": {"manifest": True, "dump": True}}
elif "/api/templates/import/finalize" in url:
    filestore = "kept" if "without_filestore=1" in url else "imported"
    body = {
        "ok": True,
        "result": {"template_name": "acme", "template_db": "db", "filestore": filestore},
    }
else:
    body = None
if body is not None and out:
    with open(out, "w") as f:
        json.dump(body, f)
"""


def _home(tmp_path, *, with_filestore):
    home = tmp_path / "home"
    backups = home / "backup.daily"
    backups.mkdir(parents=True)
    (backups / f"{DB}_daily.sql.gz").write_bytes(b"gz")
    (backups / f"{DB}_daily.json").write_text("{}")
    if with_filestore:
        chunk = backups / f"{DB}_daily/home/odoo/data/filestore/{DB}/ab"
        chunk.mkdir(parents=True)
        (chunk / "abcdef").write_bytes(b"blob")
    return home


def _run(tmp_path, home, *flags):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "curl"
    stub.write_text(f"#!{sys.executable}\n{_CURL_STUB}")
    stub.chmod(0o755)
    log = tmp_path / "curl.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "PGDATABASE": DB,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "CURL_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--server", "https://srv", "--token", "tok", *flags],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    urls = []
    if log.exists():
        for line in log.read_text().splitlines():
            urls += [a for a in json.loads(line) if a.startswith("http")]
    return proc, urls


def test_without_filestore_imports_a_backup_that_has_no_filestore(tmp_path):
    home = _home(tmp_path, with_filestore=False)

    proc, urls = _run(tmp_path, home, "--without-filestore")

    assert proc.returncode == 0, proc.stderr
    assert "filestore: skipped (--without-filestore)" in proc.stdout
    assert "https://srv/api/templates/import/finalize?without_filestore=1" in urls
    assert not any("/filestore?chunk=" in u for u in urls)
    assert (
        "Filestore: not imported — the template keeps its existing filestore"
        in proc.stdout
    )


def test_without_filestore_does_not_upload_an_existing_filestore(tmp_path):
    home = _home(tmp_path, with_filestore=True)

    proc, urls = _run(tmp_path, home, "--without-filestore")

    assert proc.returncode == 0, proc.stderr
    assert not any("/filestore?chunk=" in u for u in urls)
    assert "https://srv/api/templates/import/finalize?without_filestore=1" in urls


def test_missing_filestore_without_flag_fails_before_upload(tmp_path):
    home = _home(tmp_path, with_filestore=False)

    proc, urls = _run(tmp_path, home)

    assert proc.returncode == 3
    assert "filestore directory not found" in proc.stderr
    assert "--without-filestore" in proc.stderr
    assert not any("/api/templates/import" in u for u in urls)


def test_full_import_uploads_filestore_chunks(tmp_path):
    home = _home(tmp_path, with_filestore=True)

    proc, urls = _run(tmp_path, home)

    assert proc.returncode == 0, proc.stderr
    assert "https://srv/api/templates/import/filestore?chunk=ab" in urls
    assert "https://srv/api/templates/import/finalize" in urls
    assert not any("without_filestore" in u for u in urls)
    assert "Filestore: imported" in proc.stdout


def test_help_prints_the_whole_header_and_nothing_else(tmp_path):
    home = _home(tmp_path, with_filestore=False)

    proc, urls = _run(tmp_path, home, "--help")

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "--without-filestore" in proc.stdout
    assert lines[-1] == "dashboard and is valid for 15 minutes."
    assert not any(line.startswith("!") for line in lines)  # no shebang
    assert urls == []
