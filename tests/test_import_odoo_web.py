"""End-to-end tests for the push-based Odoo.sh template import endpoints."""

import io
import json
import os
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.docker_ops import system_ops
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    # No ui_password -> UI mounts without auth, so token endpoints are reachable.
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app), settings, team


def _mint(client, template_name="zipfit"):
    r = client.post(
        "/api/templates/import-token", json={"template_name": template_name}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    return data["token"], data


def _tar_bytes(members):
    """Build an in-memory tar. `members` maps arcname -> file content (bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_import_script_served(tmp_path):
    client, _s, _t = _client(tmp_path)
    r = client.get("/import-odoo.sh")
    assert r.status_code == 200
    assert "PGDATABASE" in r.text
    assert "backup.daily" in r.text
    assert "finalize" in r.text
    assert "curl --help all" in r.text
    assert "urllib.parse.quote" in r.text
    assert "Addon warnings" in r.text


def test_token_command_contains_server_and_token(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, data = _mint(client)
    assert token in data["command"]
    assert "/import-odoo.sh" in data["command"]
    assert data["template_name"] == "zipfit"


def test_full_push_flow(tmp_path):
    client, _settings, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}

    # status: nothing done yet
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["manifest"] is False
    assert st["progress"]["filestore_chunks"] == []

    # manifest
    manifest = {
        "name": "zipfit-odoo-main-7950600",
        "odoo_branch": "18.0",
        "revision": "deadbeef",
        "repository": "git@github.com:zipfit/odoo.git",
        "installed_modules": {"standard": "base,web", "custom": "zipfit_x"},
        "backup_datetime_utc": "2026-07-02 02:09:47",
    }
    r = client.post(
        "/api/templates/import/manifest",
        headers=hdr,
        content=json.dumps(manifest).encode(),
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    staging = team.get_import_staging_dir("zipfit")
    meta = json.loads(open(os.path.join(staging, "metadata.json")).read())
    assert meta["odoo_image"] == "odoo:18.0"
    assert meta["source"] == "odoo.sh"
    assert meta["source_db"] == "zipfit-odoo-main-7950600"
    # Nothing touches the live template until finalize.
    assert not os.path.exists(team.get_template_dir("zipfit"))

    # dump (gzip bytes, contents irrelevant here)
    r = client.post(
        "/api/templates/import/dump", headers=hdr, content=b"\x1f\x8bFAKEGZIP"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert os.path.isfile(os.path.join(staging, "dump.sql.gz"))

    # filestore chunk "ab"
    tar = _tar_bytes({"ab/abcdef0123": b"BLOBDATA"})
    r = client.post(
        "/api/templates/import/filestore?chunk=ab", headers=hdr, content=tar
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    extracted = os.path.join(staging, "filestore", "ab", "abcdef0123")
    assert open(extracted, "rb").read() == b"BLOBDATA"

    # status reflects progress
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["manifest"] is True
    assert st["progress"]["dump"] is True
    assert st["progress"]["filestore_chunks"] == ["ab"]

    # re-uploading the same chunk does not duplicate it (resume idempotency)
    client.post("/api/templates/import/filestore?chunk=ab", headers=hdr, content=tar)
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == ["ab"]

    # finalize (mock the heavy DB restore)
    with patch.object(
        system_ops,
        "finalize_imported_template",
        return_value={
            "status": "imported",
            "template_name": "zipfit",
            "template_db": "oduflow_template_1_zipfit",
            "restore_seconds": 1.2,
            "affected_envs": [],
            "remount_failures": [],
        },
    ) as fin:
        r = client.post("/api/templates/import/finalize", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"] is True
    fin.assert_called_once()
    assert fin.call_args.kwargs["staging_dir"] == staging
    assert r.json()["result"]["template_db"] == "oduflow_template_1_zipfit"

    # token invalidated after finalize
    st = client.get("/api/templates/import/status", headers=hdr)
    assert st.json()["ok"] is False


def test_resume_survives_a_fresh_token(tmp_path):
    """Progress is derived from disk, so a brand-new token (minted after the
    previous one expired mid-upload) sees what already landed."""
    client, _s, _t = _client(tmp_path)
    token_a, _ = _mint(client, "zipfit")
    hdr_a = {"Authorization": f"Bearer {token_a}"}
    manifest = {"name": "db", "odoo_branch": "18.0", "installed_modules": {}}
    client.post(
        "/api/templates/import/manifest",
        headers=hdr_a,
        content=json.dumps(manifest).encode(),
    )
    client.post("/api/templates/import/dump", headers=hdr_a, content=b"gz")
    client.post(
        "/api/templates/import/filestore?chunk=00",
        headers=hdr_a,
        content=_tar_bytes({"00/x": b"y"}),
    )

    # A different token for the same template still reports the staged progress.
    token_b, _ = _mint(client, "zipfit")
    st = client.get(
        "/api/templates/import/status",
        headers={"Authorization": f"Bearer {token_b}"},
    ).json()
    assert st["progress"]["manifest"] is True
    assert st["progress"]["dump"] is True
    assert st["progress"]["filestore_chunks"] == ["00"]


def test_finalize_requires_manifest_and_dump(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/templates/import/finalize", headers=hdr)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_invalid_chunk_rejected(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/filestore?chunk=../evil", headers=hdr, content=b"x"
    )
    assert r.status_code == 400


def test_bad_token_rejected(tmp_path):
    client, _s, _t = _client(tmp_path)
    r = client.get(
        "/api/templates/import/status",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.json()["ok"] is False


def test_dump_chunked_upload(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    payload = b"0123456789ABCDEFGHIJ"  # 20 bytes
    total = len(payload)

    r = client.post(
        f"/api/templates/import/dump?offset=0&total={total}",
        headers=hdr,
        content=payload[:12],
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["complete"] is False and j["received"] == 12

    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["dump"] is False
    assert st["progress"]["dump_bytes"] == 12

    r = client.post(
        f"/api/templates/import/dump?offset=12&total={total}",
        headers=hdr,
        content=payload[12:],
    )
    assert r.json()["complete"] is True
    staging = team.get_import_staging_dir("zipfit")
    assert open(os.path.join(staging, "dump.sql.gz"), "rb").read() == payload

    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["dump"] is True
    assert st["progress"]["dump_bytes"] == total

    # Idempotent: a re-sent final chunk after completion just reports complete.
    r = client.post(
        f"/api/templates/import/dump?offset=12&total={total}",
        headers=hdr,
        content=payload[12:],
    )
    assert r.json()["complete"] is True


def test_dump_chunk_gap_returns_409(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/dump?offset=5&total=20", headers=hdr, content=b"xxxxx"
    )
    assert r.status_code == 409
    assert r.json()["expected"] == 0


def test_dump_chunk_resume_from_reported_bytes(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    payload = b"abcdefghijklmnop"  # 16 bytes
    total = len(payload)
    client.post(
        f"/api/templates/import/dump?offset=0&total={total}",
        headers=hdr,
        content=payload[:6],
    )
    # A fresh token (previous expired mid-upload) still sees progress on disk.
    token2, _ = _mint(client, "zipfit")
    hdr2 = {"Authorization": f"Bearer {token2}"}
    st = client.get("/api/templates/import/status", headers=hdr2).json()
    off = st["progress"]["dump_bytes"]
    assert off == 6
    r = client.post(
        f"/api/templates/import/dump?offset={off}&total={total}",
        headers=hdr2,
        content=payload[off:],
    )
    assert r.json()["complete"] is True
    staging = team.get_import_staging_dir("zipfit")
    assert open(os.path.join(staging, "dump.sql.gz"), "rb").read() == payload


def test_addon_chunked_upload(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    tar = _tar_bytes({"enterprise_src/mod/__manifest__.py": b"{}"})
    total = len(tar)
    half = total // 2

    r = client.post(
        f"/api/templates/import/addon?name=enterprise&branch=18.0&offset=0&total={total}",
        headers=hdr,
        content=tar[:half],
    )
    assert r.json()["complete"] is False
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["addon_bytes"]["enterprise"] == half

    r = client.post(
        f"/api/templates/import/addon?name=enterprise&branch=18.0&offset={half}&total={total}",
        headers=hdr,
        content=tar[half:],
    )
    assert r.json()["complete"] is True
    staging = team.get_import_staging_dir("zipfit")
    assert os.path.isfile(
        os.path.join(staging, "addons", "enterprise", "mod", "__manifest__.py")
    )
    entries = json.loads(open(os.path.join(staging, "addons.json")).read())
    assert entries[0] == {
        "name": "enterprise",
        "kind": "local",
        "branch": "18.0",
        "origin_url": "",
        "category": "",
    }
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["addons"] == ["enterprise"]


def test_addon_upload_and_status(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    tar = _tar_bytes({"enterprise_src/sale_ent/__manifest__.py": b"{}"})
    r = client.post(
        "/api/templates/import/addon?name=enterprise&branch=18.0&category=enterprise",
        headers=hdr,
        content=tar,
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    staging = team.get_import_staging_dir("zipfit")
    assert os.path.isfile(
        os.path.join(staging, "addons", "enterprise", "sale_ent", "__manifest__.py")
    )
    entries = json.loads(open(os.path.join(staging, "addons.json")).read())
    assert entries[0]["name"] == "enterprise"
    assert entries[0]["kind"] == "local"
    assert entries[0]["branch"] == "18.0"
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["addons"] == ["enterprise"]


def test_concurrent_addon_finalizers_are_serialized(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    first_started = Event()
    release_first = Event()
    second_started = Event()
    state_lock = Lock()
    active = 0
    real_extract = system_ops.extract_addon_dir

    def extract(tar_path, addons_dir, name):
        nonlocal active
        with state_lock:
            active += 1
            if active == 2:
                second_started.set()
        try:
            if not first_started.is_set():
                first_started.set()
                assert release_first.wait(timeout=5)
            return real_extract(tar_path, addons_dir, name)
        finally:
            with state_lock:
                active -= 1

    staging = team.get_import_staging_dir("zipfit")
    uploads = {
        "enterprise": _tar_bytes({"enterprise_src/mod_a/__manifest__.py": b"{}"}),
        "themes": _tar_bytes({"themes_src/mod_b/__manifest__.py": b"{}"}),
    }
    with (
        client,
        patch.object(system_ops, "extract_addon_dir", side_effect=extract),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(
            client.post,
            "/api/templates/import/addon?name=enterprise&branch=18.0",
            headers=hdr,
            content=uploads["enterprise"],
        )
        assert first_started.wait(timeout=2)
        second = executor.submit(
            client.post,
            "/api/templates/import/addon?name=themes&branch=18.0",
            headers=hdr,
            content=uploads["themes"],
        )
        deadline = time.monotonic() + 2
        staged_tars: list[str] = []
        while time.monotonic() < deadline:
            staged_tars = [
                name
                for name in os.listdir(staging)
                if name.startswith(".addon_") and name.endswith(".tar")
            ]
            if len(staged_tars) == 2:
                break
            time.sleep(0.01)
        try:
            assert len(staged_tars) == 2
            assert not second_started.wait(timeout=0.2)
        finally:
            release_first.set()

        assert first.result(timeout=2).status_code == 200
        assert second.result(timeout=2).status_code == 200

    entries = json.loads(open(os.path.join(staging, "addons.json")).read())
    assert {entry["name"] for entry in entries} == {"enterprise", "themes"}


def test_addon_remote_announce(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/addon-remote",
        headers=hdr,
        json={
            "name": "oca-account-reconcile",
            "origin_url": "https://github.com/OCA/account-reconcile.git",
            "branch": "18.0",
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["remote_addons"] == ["oca-account-reconcile"]


def test_addon_invalid_name_rejected(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/addon?name=bad/name",
        headers=hdr,
        content=_tar_bytes({"x/y": b"z"}),
    )
    assert r.status_code == 400


def test_addon_remote_requires_origin(tmp_path):
    client, _s, _t = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post(
        "/api/templates/import/addon-remote",
        headers=hdr,
        json={"name": "foo", "branch": "18.0"},
    )
    assert r.status_code == 400


def test_import_token_flags_in_command(tmp_path):
    from oduflow import import_tokens

    client, settings, _team = _client(tmp_path)
    r = client.post(
        "/api/templates/import-token",
        json={
            "template_name": "zipfit",
            "with_enterprise": True,
            "with_extra_addons": True,
            "addon_error_policy": "best_effort",
        },
    )
    data = r.json()
    assert "--with-enterprise" in data["command"]
    assert "--with-extra-addons" in data["command"]
    assert "--with-themes" not in data["command"]
    _resolved_team, record = import_tokens.load_token(settings, data["token"])
    assert record["addon_error_policy"] == "best_effort"


def test_import_token_without_filestore_flag(tmp_path):
    from oduflow import import_tokens

    client, settings, _team = _client(tmp_path)
    _token, plain = _mint(client)
    assert "--without-filestore" not in plain["command"]
    _t, plain_record = import_tokens.load_token(settings, plain["token"])
    assert plain_record["without_filestore"] is False

    r = client.post(
        "/api/templates/import-token",
        json={"template_name": "zipfit", "without_filestore": True},
    )
    data = r.json()
    assert data["command"].endswith(" --without-filestore")
    _t, record = import_tokens.load_token(settings, data["token"])
    assert record["without_filestore"] is True


def test_finalize_passes_without_filestore_from_token(tmp_path):
    client, _settings, team = _client(tmp_path)
    r = client.post(
        "/api/templates/import-token",
        json={"template_name": "zipfit", "without_filestore": True},
    )
    token = r.json()["token"]
    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(staging)
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    with patch.object(
        system_ops,
        "finalize_imported_template",
        return_value={"template_name": "zipfit", "addon_warnings": []},
    ) as finalize:
        response = client.post(
            "/api/templates/import/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert finalize.call_args.kwargs["without_filestore"] is True


def test_import_script_accepts_without_filestore(tmp_path):
    import subprocess

    script = os.path.join(
        os.path.dirname(system_ops.__file__), "..", "templates", "import-odoo.sh"
    )
    subprocess.run(["bash", "-n", script], check=True)
    help_out = subprocess.run(
        ["bash", script, "--help"], capture_output=True, text=True, check=True
    )
    assert "--without-filestore" in help_out.stdout
    # The flag parses; the run then stops at the missing Odoo.sh environment
    # (PGDATABASE) instead of rejecting the argument. Port 9 refuses fast.
    env = {k: v for k, v in os.environ.items() if k != "PGDATABASE"}
    run = subprocess.run(
        [
            "bash",
            script,
            "--server",
            "http://127.0.0.1:9",
            "--token",
            "t",
            "--without-filestore",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "unknown argument" not in run.stderr
    assert "PGDATABASE is not set" in run.stderr
    assert run.returncode == 2


def test_import_token_rejects_unknown_addon_policy(tmp_path):
    client, _settings, _team = _client(tmp_path)

    r = client.post(
        "/api/templates/import-token",
        json={
            "template_name": "zipfit",
            "addon_error_policy": "ignore_everything",
        },
    )

    assert r.status_code == 400
    assert "addon_error_policy" in r.json()["error"]


def test_finalize_uses_addon_policy_stored_in_token(tmp_path):
    client, _settings, team = _client(tmp_path)
    r = client.post(
        "/api/templates/import-token",
        json={
            "template_name": "zipfit",
            "addon_error_policy": "best_effort",
        },
    )
    token = r.json()["token"]
    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(staging)
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    with patch.object(
        system_ops,
        "finalize_imported_template",
        return_value={"template_name": "zipfit", "addon_warnings": []},
    ) as finalize:
        response = client.post(
            "/api/templates/import/finalize",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert finalize.call_args.kwargs["addon_error_policy"] == "best_effort"


def test_filestore_tar_zip_slip_is_skipped(tmp_path):
    dest = tmp_path / "fs"
    dest.mkdir()
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, "w") as tf:
        data = b"pwn"
        info = tarfile.TarInfo(name="../escape")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        good = b"ok"
        info2 = tarfile.TarInfo(name="ab/file")
        info2.size = len(good)
        tf.addfile(info2, io.BytesIO(good))
    written = system_ops.extract_filestore_tar(str(tar_path), str(dest))
    assert written == 1
    assert (dest / "ab" / "file").read_bytes() == b"ok"
    assert not (tmp_path / "escape").exists()


def test_import_paths_do_not_bypass_auth_for_sibling_routes(tmp_path):
    """Regression: a public PREFIX /api/templates/import/ used to expose
    /api/templates/{name}/delete with name="import" without credentials. The
    ingest endpoints must be public as exact paths only."""
    team = TeamSettings(
        team_id="1", data_dir=str(tmp_path / "team1"), ui_password="secret"
    )
    settings = Settings(
        routing_mode="port", base_data_dir=str(tmp_path), teams={"1": team}
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    # Sibling route captured by {name}="import" must require auth.
    r = client.post("/api/templates/import/delete")
    assert r.status_code == 401

    # The token-authed ingest endpoint stays reachable without Basic auth
    # (404 = unknown token, i.e. it got past the auth middleware).
    r = client.get(
        "/api/templates/import/status",
        headers={"Authorization": "Bearer " + "a" * 24},
    )
    assert r.status_code == 404

    # Minting a token still requires the UI login.
    r = client.post("/api/templates/import-token", json={"template_name": "x"})
    assert r.status_code == 401


def test_existing_template_does_not_fake_resume(tmp_path):
    """Regression: progress must come from the staging dir, not the live
    template — otherwise re-importing into an existing template name skips
    every upload and finalize silently restores the OLD data."""
    client, _s, team = _client(tmp_path)
    tpl_dir = team.get_template_dir("zipfit")
    os.makedirs(os.path.join(tpl_dir, "filestore", "ab"))
    with open(team.get_template_metadata_path("zipfit"), "w") as f:
        f.write("{}")
    with open(os.path.join(tpl_dir, "dump.sql.gz"), "wb") as f:
        f.write(b"old")

    token, _ = _mint(client, "zipfit")
    st = client.get(
        "/api/templates/import/status",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert st["progress"]["manifest"] is False
    assert st["progress"]["dump"] is False
    assert st["progress"]["filestore_chunks"] == []


def test_promoted_import_reports_live_artifacts_for_retry(tmp_path):
    """After promotion, a fresh token must skip re-uploading live artifacts."""
    client, _s, team = _client(tmp_path)
    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(staging)
    open(os.path.join(staging, ".promoted"), "w").close()
    tpl_dir = team.get_template_dir("zipfit")
    os.makedirs(os.path.join(tpl_dir, "filestore", "ab"))
    with open(team.get_template_metadata_path("zipfit"), "w") as f:
        f.write("{}")
    with open(os.path.join(tpl_dir, "dump.sql.gz"), "wb") as f:
        f.write(b"promoted")

    token, _ = _mint(client, "zipfit")
    st = client.get(
        "/api/templates/import/status",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert st["progress"]["manifest"] is True
    assert st["progress"]["dump"] is True
    assert st["progress"]["dump_bytes"] == len(b"promoted")
    assert st["progress"]["filestore_chunks"] == ["ab"]


def test_private_remote_addon_url_is_rejected(tmp_path):
    client, _s, _team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    r = client.post(
        "/api/templates/import/addon-remote",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "private",
            "origin_url": "https://127.0.0.1/repo.git",
            "branch": "18.0",
        },
    )
    assert r.status_code == 400
    assert "blocked address" in r.json()["error"]


def test_corrupt_chunk_is_not_marked_complete(tmp_path):
    """Regression: a truncated/corrupt chunk tar must fail the request AND
    leave no chunk directory behind, so resume re-uploads it."""
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}

    r = client.post(
        "/api/templates/import/filestore?chunk=ab",
        headers=hdr,
        content=b"this is not a tar archive",
    )
    assert r.status_code == 500
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == []
    fs_dir = os.path.join(team.get_import_staging_dir("zipfit"), "filestore")
    assert not os.path.isdir(os.path.join(fs_dir, "ab"))

    # A good re-upload of the same chunk then completes it.
    r = client.post(
        "/api/templates/import/filestore?chunk=ab",
        headers=hdr,
        content=_tar_bytes({"ab/f": b"data"}),
    )
    assert r.status_code == 200
    st = client.get("/api/templates/import/status", headers=hdr).json()
    assert st["progress"]["filestore_chunks"] == ["ab"]


def test_concurrent_filestore_retry_uses_unique_staging_tar(tmp_path):
    client, _s, team = _client(tmp_path)
    token, _ = _mint(client, "zipfit")
    hdr = {"Authorization": f"Bearer {token}"}
    tar = _tar_bytes({"ab/f": b"data"})
    first_started = Event()
    release_first = Event()
    real_extract = system_ops.extract_filestore_chunk

    def extract(tar_path, filestore_dir, chunk):
        first_started.set()
        assert release_first.wait(timeout=5)
        return real_extract(tar_path, filestore_dir, chunk)

    staging = team.get_import_staging_dir("zipfit")
    with (
        client,
        patch.object(
            system_ops, "extract_filestore_chunk", side_effect=extract
        ) as mocked,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(
            client.post,
            "/api/templates/import/filestore?chunk=ab",
            headers=hdr,
            content=tar,
        )
        assert first_started.wait(timeout=2)
        retry = executor.submit(
            client.post,
            "/api/templates/import/filestore?chunk=ab",
            headers=hdr,
            content=tar,
        )
        deadline = time.monotonic() + 2
        staged_tars: list[str] = []
        while time.monotonic() < deadline:
            staged_tars = [
                name
                for name in os.listdir(staging)
                if name.startswith(".chunk_ab_") and name.endswith(".tar")
            ]
            if len(staged_tars) == 2:
                break
            time.sleep(0.01)
        try:
            assert len(staged_tars) == 2
        finally:
            release_first.set()

        assert first.result(timeout=2).status_code == 200
        assert retry.result(timeout=2).status_code == 200
        assert mocked.call_count == 1

    extracted = os.path.join(staging, "filestore", "ab", "f")
    assert open(extracted, "rb").read() == b"data"
    assert not any(name.startswith(".chunk_ab_") for name in os.listdir(staging))


def test_extract_filestore_chunk_missing_dir_fails_cleanly(tmp_path):
    """A tar that does not contain the expected <chunk>/ dir is rejected and
    leaves nothing behind."""
    import pytest

    from oduflow.errors import ExternalCommandError

    tar_path = tmp_path / "wrong.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tarfile.TarInfo(name="cd/file")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    fs = tmp_path / "fs"
    with pytest.raises(ExternalCommandError):
        system_ops.extract_filestore_chunk(str(tar_path), str(fs), "ab")
    assert not (fs / "ab").exists()
    assert not (fs / ".incoming_ab").exists()


def test_finalize_swaps_staging_into_template(tmp_path):
    """finalize_imported_template promotes the staged upload into the live
    template inside the remount guard and removes the staging dir."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})

    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(os.path.join(staging, "filestore", "ab"))
    with open(os.path.join(staging, "filestore", "ab", "f"), "wb") as f:
        f.write(b"blob")
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump({"odoo_version": "18.0"}, f)
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    # Pre-existing template content that must be replaced, not merged.
    tpl_dir = team.get_template_dir("zipfit")
    os.makedirs(os.path.join(tpl_dir, "filestore", "ff"))
    with open(os.path.join(tpl_dir, "dump.pgdump"), "wb") as f:
        f.write(b"old")

    remount = MagicMock(affected=[], failures=[])

    @contextmanager
    def fake_remount(*a, **kw):
        yield remount

    from oduflow.docker_ops import env_ops

    with (
        patch.object(system_ops, "get_client"),
        patch.object(env_ops, "remount_template_overlays", fake_remount),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="101:101"),
        patch.object(system_ops, "chown_recursive"),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(
            system_ops,
            "reload_template",
            return_value={"template_db": "tdb", "restore_seconds": 1.0},
        ),
    ):
        result = system_ops.finalize_imported_template(
            settings, team, "zipfit", staging_dir=staging
        )

    assert result["template_db"] == "tdb"
    assert result["addon_warnings"] == []
    fs = team.get_template_filestore_path("zipfit")
    assert open(os.path.join(fs, "ab", "f"), "rb").read() == b"blob"
    assert not os.path.exists(os.path.join(fs, "ff"))  # old filestore replaced
    assert not os.path.exists(os.path.join(tpl_dir, "dump.pgdump"))  # stale gone
    assert os.path.isfile(os.path.join(tpl_dir, "dump.sql.gz"))
    assert (
        json.load(open(team.get_template_metadata_path("zipfit")))["odoo_version"]
        == "18.0"
    )
    assert not os.path.exists(staging)  # staging cleaned up


def test_finalize_can_retry_after_reload_failure_without_reupload(tmp_path):
    """Promotion leaves a marker and live artifacts when DB reload fails."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(os.path.join(staging, "filestore", "ab"))
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump({"odoo_version": "18.0"}, f)
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    remount = MagicMock(affected=[], failures=[])

    @contextmanager
    def fake_remount(*a, **kw):
        yield remount

    from oduflow.docker_ops import env_ops

    reload_results = [
        RuntimeError("temporary restore failure"),
        {"template_db": "tdb", "restore_seconds": 2.0},
    ]
    with (
        patch.object(system_ops, "get_client"),
        patch.object(env_ops, "remount_template_overlays", fake_remount),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="101:101"),
        patch.object(system_ops, "chown_recursive"),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(
            system_ops, "reload_template", side_effect=reload_results
        ) as reload,
    ):
        with pytest.raises(RuntimeError, match="temporary restore failure"):
            system_ops.finalize_imported_template(
                settings, team, "zipfit", staging_dir=staging
            )

        assert os.path.isfile(os.path.join(staging, ".promoted"))
        assert not os.path.isfile(os.path.join(staging, "dump.sql.gz"))
        assert os.path.isfile(
            os.path.join(team.get_template_dir("zipfit"), "dump.sql.gz")
        )

        result = system_ops.finalize_imported_template(
            settings, team, "zipfit", staging_dir=staging
        )

    assert result["template_db"] == "tdb"
    assert reload.call_count == 2
    assert not os.path.exists(staging)


def _finalize_with_mocks(settings, team, staging, **kwargs):
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from oduflow.docker_ops import env_ops

    remount = MagicMock(affected=[], failures=[])

    @contextmanager
    def fake_remount(*a, **kw):
        yield remount

    with (
        patch.object(system_ops, "get_client"),
        patch.object(env_ops, "remount_template_overlays", fake_remount),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="101:101"),
        patch.object(system_ops, "chown_recursive") as chown,
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(
            system_ops,
            "reload_template",
            return_value={"template_db": "tdb", "restore_seconds": 1.0},
        ),
    ):
        result = system_ops.finalize_imported_template(
            settings, team, "zipfit", staging_dir=staging, **kwargs
        )
    return result, chown


def test_finalize_without_filestore_keeps_existing_filestore(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    staging = team.get_import_staging_dir("zipfit")
    # A leftover staged chunk (e.g. an earlier run with the filestore) must not
    # replace the live filestore of a database-only import.
    os.makedirs(os.path.join(staging, "filestore", "ab"))
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump({"odoo_version": "18.0", "includes_filestore": True}, f)
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")
    fs = team.get_template_filestore_path("zipfit")
    os.makedirs(os.path.join(fs, "ff"))
    with open(os.path.join(fs, "ff", "blob"), "wb") as f:
        f.write(b"keep")

    result, chown = _finalize_with_mocks(
        settings, team, staging, without_filestore=True
    )

    assert result["template_db"] == "tdb"
    assert open(os.path.join(fs, "ff", "blob"), "rb").read() == b"keep"
    assert not os.path.exists(os.path.join(fs, "ab"))
    chown.assert_not_called()
    md = json.load(open(team.get_template_metadata_path("zipfit")))
    assert md["includes_filestore"] is False
    assert md["odoo_version"] == "18.0"
    assert not os.path.exists(staging)


def test_finalize_without_filestore_creates_empty_filestore(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    staging = team.get_import_staging_dir("zipfit")
    os.makedirs(staging)
    with open(os.path.join(staging, "metadata.json"), "w") as f:
        json.dump({"odoo_version": "18.0"}, f)
    with open(os.path.join(staging, "dump.sql.gz"), "wb") as f:
        f.write(b"gz")

    _finalize_with_mocks(settings, team, staging, without_filestore=True)

    fs = team.get_template_filestore_path("zipfit")
    assert os.path.isdir(fs)
    assert os.listdir(fs) == []
    md = json.load(open(team.get_template_metadata_path("zipfit")))
    assert md["includes_filestore"] is False
