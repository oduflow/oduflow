"""The oduflow.toml editing engine behind the Server settings console."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from oduflow import config_schema as schema
from oduflow import config_store as cs
from oduflow import settings as settings_module
from oduflow.errors import ConfigError, ConflictError
from oduflow.settings import Settings

TEMPLATE = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "oduflow.toml"
).read_text(encoding="utf-8")

BASE = """\
# Deployment config — hand-written comment that must survive.
[server]
port = 8000            # HTTP port
# trace = false        # verbose tracing

[lifecycle]
auto_stop_hours = 48

[team.1]
hostname = "a.example.com"   # team one
auth_token = "token-one-aaaaaaaaaaaa"
ui_password = "password-one-aaaa"
port_range = [50000, 50100]
# agent_enabled = false        # per-team agent
# trailing comment of team 1
"""

SCATTERED = """\
[server]
port = 8000

[team.1]
hostname = "a.example.com"
auth_token = "token-one-aaaaaaaaaaaa"
ui_password = "password-one-aaaa"

[route.api]
host = "api.example.com"
url = "http://127.0.0.1:9000"

[team.2]
hostname = "b.example.com"
auth_token = "token-two-aaaaaaaaaaaa"
ui_password = "password-two-aaaa"
"""


def _apply(text: str, *changes: dict) -> str:
    return cs.apply_changes(text, cs.parse_changes(list(changes)))


def _write(tmp_path: Path, text: str = BASE) -> str:
    path = tmp_path / "oduflow.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


class TestApplyChanges:
    def test_replacing_a_value_keeps_comments_and_layout(self):
        out = _apply(BASE, {"path": ["server", "port"], "value": 9000})
        assert "port = 9000            # HTTP port" in out
        assert out.startswith("# Deployment config — hand-written comment")
        assert out.replace("9000", "8000") == BASE

    def test_out_of_order_tables_are_editable(self):
        """[team.1] … [route.api] … [team.2]: a split super table still edits."""
        out = _apply(
            SCATTERED, {"path": ["team", "2", "hostname"], "value": "c.example.com"}
        )
        assert cs.parse_text(out)["team"]["2"]["hostname"] == "c.example.com"
        added = cs.parse_text(
            _apply(
                SCATTERED,
                {
                    "path": ["team", "3"],
                    "value": {"hostname": "d.example.com"},
                },
            )
        )
        assert sorted(added["team"]) == ["1", "2", "3"]
        assert added["route"]["api"]["host"] == "api.example.com"
        removed = cs.parse_text(
            _apply(SCATTERED, {"path": ["team", "1"], "unset": True})
        )
        assert sorted(removed["team"]) == ["2"]

    def test_documented_commented_key_is_uncommented_in_place(self):
        out = _apply(BASE, {"path": ["server", "trace"], "value": True})
        lines = out.splitlines()
        index = lines.index("[server]")
        assert lines[index + 2].startswith("trace = true")
        assert "# verbose tracing" in lines[index + 2]
        assert "# trace = false" not in out

    def test_team_key_uncommented_in_its_own_table(self):
        out = _apply(BASE, {"path": ["team", "1", "agent_enabled"], "value": True})
        assert "agent_enabled = true" in out
        assert "# agent_enabled" not in out
        assert Settings.from_raw(cs.parse_text(out), "x").teams["1"].agent_enabled

    def test_new_key_goes_after_last_key_not_after_trailing_comments(self):
        out = _apply(BASE, {"path": ["team", "1", "service_slots"], "value": 3})
        lines = out.splitlines()
        assert lines[lines.index("port_range = [50000, 50100]") + 1] == (
            "service_slots = 3"
        )
        assert out.rstrip().endswith("# trailing comment of team 1")

    def test_empty_text_removes_key_so_default_applies(self):
        out = _apply(BASE, {"path": ["team", "1", "hostname"], "value": "b.example"})
        out = _apply(out, {"path": ["lifecycle", "auto_stop_hours"], "unset": True})
        assert "auto_stop_hours" not in out
        assert Settings.from_raw(cs.parse_text(out), "x").auto_stop_hours == 48

    def test_tls_inline_table(self):
        out = _apply(BASE, {"path": ["routing", "tls"], "value": "{}"})
        assert "tls = {}" in out
        assert cs.parse_text(out)["routing"]["tls"] == {}

    def test_alias_removed_when_canonical_key_written(self):
        text = BASE.replace("port = 8000", 'host = "127.0.0.1"\nport = 8000')
        out = _apply(text, {"path": ["server", "bind"], "value": "0.0.0.0"})
        server = cs.parse_text(out)["server"]
        assert server["bind"] == "0.0.0.0"
        assert "host" not in server

    def test_add_and_remove_team(self):
        out = _apply(
            BASE,
            {
                "path": ["team", "2"],
                "value": {
                    "hostname": "b.example.com",
                    "port_range": [50100, 50200],
                    "auth_token": "token-two-bbbbbbbbbbbb",
                    "ui_password": "password-two-bbbb",
                },
            },
        )
        assert "[team.2]" in out
        settings, _ = cs.validate_text(out, "x")
        assert sorted(settings.teams) == ["1", "2"]
        out = _apply(out, {"path": ["team", "2"], "unset": True})
        assert "[team.2]" not in out

    def test_adding_an_existing_team_conflicts(self):
        with pytest.raises(ConflictError):
            _apply(BASE, {"path": ["team", "1"], "value": {"hostname": "x"}})

    def test_member_names_are_restricted(self):
        with pytest.raises(ConfigError, match="Invalid"):
            _apply(BASE, {"path": ["team", "a b"], "value": {"hostname": "x"}})

    def test_route_is_created_without_empty_parent_header(self):
        out = _apply(
            BASE,
            {
                "path": ["route", "api"],
                "value": {"host": "api.example.com", "url": "http://127.0.0.1:3000"},
            },
        )
        assert "[route.api]" in out
        assert "[route]\n" not in out

    def test_locked_keys_are_refused(self):
        with pytest.raises(ConfigError, match="read-only"):
            _apply(BASE, {"path": ["database", "password"], "value": "new"})

    def test_unknown_keys_are_refused(self):
        with pytest.raises(ConfigError, match="Unknown setting"):
            _apply(BASE, {"path": ["server", "nope"], "value": 1})

    def test_type_errors_are_reported(self):
        with pytest.raises(ConfigError, match="whole number"):
            _apply(BASE, {"path": ["server", "port"], "value": "eighty"})
        with pytest.raises(ConfigError, match="true or false"):
            _apply(BASE, {"path": ["server", "trace"], "value": "yes"})

    def test_agent_env_keep_markers_resolve_against_the_file(self):
        out = _apply(
            BASE,
            {"path": ["team", "1", "agent_env"], "value": {"OPENAI_API_KEY": "sk-1"}},
        )
        out = _apply(
            out,
            {
                "path": ["team", "1", "agent_env"],
                "value": {"OPENAI_API_KEY": {"keep": True}, "EXTRA": "x"},
            },
        )
        env = cs.parse_text(out)["team"]["1"]["agent_env"]
        assert env == {"OPENAI_API_KEY": "sk-1", "EXTRA": "x"}
        with pytest.raises(ConfigError, match="no stored value"):
            _apply(
                out,
                {
                    "path": ["team", "1", "agent_env"],
                    "value": {"MISSING": {"keep": True}},
                },
            )

    def test_bundled_template_round_trips_unchanged(self):
        assert cs.apply_changes(TEMPLATE, []) == TEMPLATE


class TestValidation:
    def test_parser_errors_surface_as_config_errors(self):
        text = _apply(BASE, {"path": ["team", "1", "port_range"], "value": [1, 2]})
        text += '\n[team.2]\nhostname = "a.example.com"\n'
        with pytest.raises(ConfigError, match="duplicate hostname"):
            cs.validate_text(text, "x")

    def test_syntax_errors(self):
        with pytest.raises(ConfigError, match="syntax"):
            cs.validate_text("[server\n", "x")

    def test_http_fail_closed_checks(self):
        text = _apply(BASE, {"path": ["team", "1", "ui_password"], "unset": True})
        with pytest.raises(ConfigError, match="unauthenticated web dashboard"):
            cs.validate_text(text, "x")
        cs.validate_text(text, "x", http=False)

    def test_multi_team_needs_every_token(self):
        text = BASE + '\n[team.2]\nhostname = "b"\nui_password = "pw-two-bbbbbbbbbb"\n'
        text += "port_range = [50100, 50200]\n"
        with pytest.raises(ConfigError, match="missing for: 2"):
            cs.validate_text(text, "x")

    def test_console_password_policy(self):
        old = cs.parse_text(BASE)
        short = cs.parse_text(BASE + '[admin]\npassword = "short"\n')
        with pytest.raises(ConfigError, match="at least 12"):
            cs.check_console_policy(old, short)
        good = cs.parse_text(BASE + '[admin]\npassword = "long-enough-123"\n')
        cs.check_console_policy(old, good)
        with pytest.raises(ConfigError, match="oduflow admin disable"):
            cs.check_console_policy(good, old)

    def test_admin_password_must_not_reuse_a_team_credential(self):
        text = BASE + '[admin]\npassword = "password-one-aaaa"\n'
        with pytest.raises(ConfigError, match="must differ"):
            cs.validate_text(text, "x")

    def test_from_raw_does_not_touch_trace(self, monkeypatch):
        monkeypatch.setattr(settings_module, "TRACE", False)
        Settings.from_raw(cs.parse_text(BASE.replace("# trace", "trace")), "x")
        assert settings_module.TRACE is False


class TestDiff:
    def test_classification(self):
        old = cs.parse_text(BASE)
        new = cs.parse_text(
            _apply(
                BASE,
                {"path": ["lifecycle", "auto_stop_hours"], "value": 12},
                {"path": ["server", "port"], "value": 9000},
                {"path": ["routing", "mode"], "value": "traefik"},
                {"path": ["team", "1", "ui_password"], "value": "password-new-aaaa"},
            )
        )
        running = Settings.from_raw(old, "x")
        by_path = {c.path: c for c in cs.diff_raw(old, new, running)}
        assert by_path[("lifecycle", "auto_stop_hours")].apply == schema.LIVE
        assert by_path[("server", "port")].apply == schema.RESTART
        assert by_path[("routing", "mode")].apply == schema.RECREATE
        secret = by_path[("team", "1", "ui_password")]
        assert secret.apply == schema.LIVE
        assert secret.to_json()["new"] == "(set)"
        assert "password-new" not in str(secret.to_json())

    def test_restart_if_overrides_live(self):
        text = BASE.replace("auto_stop_hours = 48", "auto_stop_hours = 0")
        old = cs.parse_text(text)
        new = cs.parse_text(
            _apply(text, {"path": ["lifecycle", "auto_stop_hours"], "value": 5})
        )
        running = Settings.from_raw(old, "x")
        (change,) = cs.diff_raw(old, new, running)
        assert change.apply == schema.RESTART

    def test_team_added_is_one_presence_change(self):
        old = cs.parse_text(BASE)
        new = cs.parse_text(BASE + '\n[team.2]\nhostname = "b"\n')
        (change,) = cs.diff_raw(old, new)
        assert change.presence and change.path == ("team", "2")
        assert change.apply == schema.RESTART

    def test_live_overlay_takes_only_live_keys(self):
        old = cs.parse_text(BASE)
        new = cs.parse_text(
            _apply(
                BASE,
                {"path": ["lifecycle", "auto_stop_hours"], "value": 12},
                {"path": ["server", "port"], "value": 9000},
            )
        )
        overlay, applied = cs.live_overlay(old, new, Settings.from_raw(old, "x"))
        assert overlay["lifecycle"]["auto_stop_hours"] == 12
        assert overlay["server"]["port"] == 8000
        assert [c.path for c in applied] == [("lifecycle", "auto_stop_hours")]

    def test_masked_diff_hides_secrets_but_shows_they_changed(self):
        new = _apply(
            BASE, {"path": ["team", "1", "auth_token"], "value": "token-new-ccccccccc"}
        )
        diff = cs.masked_diff(BASE, new)
        assert "token-one" not in diff and "token-new" not in diff
        assert f'auth_token = "{cs.MASK} (changed)"' in diff

    def test_masking_follows_out_of_order_tables(self):
        masked = cs.mask_text(SCATTERED)
        assert "password-one" not in masked and "password-two" not in masked
        assert "token-one" not in masked and "token-two" not in masked
        assert masked.count(f'ui_password = "{cs.MASK}"') == 2
        new = _apply(
            SCATTERED, {"path": ["team", "2", "ui_password"], "value": "password-new-a"}
        )
        diff = cs.masked_diff(SCATTERED, new)
        assert "password-two" not in diff and "password-new" not in diff
        assert f'ui_password = "{cs.MASK} (changed)"' in diff

    def test_mask_raw(self):
        masked = cs.mask_raw(
            cs.parse_text(
                _apply(
                    BASE,
                    {"path": ["team", "1", "agent_env"], "value": {"K": "v"}},
                )
            )
        )
        team = masked["team"]["1"]
        assert team["ui_password"] == {"$secret": True, "set": True}
        assert team["agent_env"] == {"K": {"$secret": True, "set": True}}
        assert team["hostname"] == "a.example.com"

    def test_unknown_keys(self):
        raw = cs.parse_text(BASE + "[mystery]\nx = 1\n")
        assert cs.unknown_keys(raw) == ["mystery.x"]


class TestWrite:
    def test_write_keeps_history_and_owner_only_mode(self, tmp_path):
        path = _write(tmp_path)
        snap = cs.read_snapshot(path)
        new = _apply(snap.text, {"path": ["server", "port"], "value": 9000})
        revision = cs.write_config(
            path, new, snap.revision, note="test", paths=["server.port"]
        )
        assert revision == cs.revision_of(new)
        assert Path(path).read_text() == new
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        (entry,) = cs.list_history(path)
        assert entry["paths"] == ["server.port"]
        _, before, after = cs.read_history(path, entry["id"])
        assert before == BASE and after == new
        history = Path(cs.history_dir(path))
        assert stat.S_IMODE(history.stat().st_mode) == 0o700
        for child in history.iterdir():
            if child.name != ".lock":
                assert stat.S_IMODE(child.stat().st_mode) == 0o600

    def test_stale_revision_conflicts(self, tmp_path):
        path = _write(tmp_path)
        snap = cs.read_snapshot(path)
        Path(path).write_text(BASE + "\n# edited by hand\n")
        with pytest.raises(ConflictError, match="changed since you opened it"):
            cs.write_config(path, BASE, snap.revision, note="x", paths=[])

    def test_symlinked_config_is_followed_not_replaced(self, tmp_path):
        real = _write(tmp_path)
        link = tmp_path / "link.toml"
        link.symlink_to(real)
        snap = cs.read_snapshot(str(link))
        cs.write_config(str(link), BASE + "# x\n", snap.revision, note="x", paths=[])
        assert link.is_symlink()
        assert Path(real).read_text().endswith("# x\n")

    def test_history_is_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cs, "HISTORY_KEEP", 3)
        path = _write(tmp_path)
        for port in range(9000, 9005):
            snap = cs.read_snapshot(path)
            text = _apply(snap.text, {"path": ["server", "port"], "value": port})
            cs.write_config(path, text, snap.revision, note=str(port), paths=[])
        notes = [e["note"] for e in cs.list_history(path)]
        assert notes == ["9004", "9003", "9002"]

    def test_unknown_history_id(self, tmp_path):
        path = _write(tmp_path)
        with pytest.raises(ConfigError):
            cs.read_history(path, "../../etc/passwd")


class _Runtime:
    def __init__(self, text: str) -> None:
        self.raw = cs.parse_text(text)
        self.settings = Settings.from_raw(self.raw, "x")
        self.swaps = 0

    def running(self):
        return self.settings, self.raw

    def swap(self, settings, raw):
        self.settings, self.raw = settings, raw
        self.swaps += 1


def test_apply_live_swaps_live_values_and_leaves_the_rest_pending():
    fake = _Runtime(BASE)
    runtime = cs.ConfigRuntime(running=fake.running, swap=fake.swap)
    new = cs.parse_text(
        _apply(
            BASE,
            {"path": ["lifecycle", "auto_stop_hours"], "value": 12},
            {"path": ["server", "port"], "value": 9000},
        )
    )
    applied, problem = cs.apply_live(runtime, new)
    assert not problem
    assert [c.path for c in applied] == [("lifecycle", "auto_stop_hours")]
    assert fake.settings.auto_stop_hours == 12
    assert fake.settings.port == 8000
    pending = cs.pending_changes(runtime, new)
    assert [c.path for c in pending] == [("server", "port")]


def test_restart_if_asks_the_boot_state_not_the_live_one():
    """Lifecycle keys stay live once the reaper thread exists.

    Live-setting them to 0 does not stop the thread the boot config started,
    so re-enabling them must not be withheld until a restart.
    """
    fake = _Runtime(BASE)  # auto_stop_hours = 48: the reaper ran at boot
    runtime = cs.ConfigRuntime(running=fake.running, swap=fake.swap, boot=fake.settings)
    off = cs.parse_text(
        _apply(BASE, {"path": ["lifecycle", "auto_stop_hours"], "value": 0})
    )
    applied, problem = cs.apply_live(runtime, off)
    assert not problem and [c.path for c in applied] == [
        ("lifecycle", "auto_stop_hours")
    ]
    assert fake.settings.auto_stop_hours == 0
    back = cs.parse_text(
        _apply(BASE, {"path": ["lifecycle", "auto_stop_hours"], "value": 24})
    )
    applied, problem = cs.apply_live(runtime, back)
    assert not problem and [c.path for c in applied] == [
        ("lifecycle", "auto_stop_hours")
    ]
    assert fake.settings.auto_stop_hours == 24
    assert cs.pending_changes(runtime, back) == []


def test_schema_defaults_match_the_parser():
    """A key left out of the file must render with the default it really gets."""
    raw = cs.parse_text(BASE)
    settings = Settings.from_raw(raw, "x")
    team = settings.teams["1"]
    checks = {
        ("server", "allow_local_path"): settings.allow_local_path,
        ("lifecycle", "auto_delete_hours"): settings.auto_delete_hours,
        ("storage", "overlay_threshold_mb"): settings.overlay_threshold_mb,
        ("agent", "image"): settings.agent_image,
        ("production", "workers_cap"): settings.prod_workers_cap,
        ("production", "wal", "stop_queue_gb"): settings.wal_stop_queue_gb,
        ("team", "1", "environment_slots"): team.environment_slots,
        ("team", "1", "service_slots"): team.service_slots,
        ("team", "1", "db_quota_gb"): team.db_quota_gb,
        ("team", "1", "agent_default"): team.agent_default,
        ("database", "image"): settings.postgres_image,
    }
    for path, actual in checks.items():
        _, f = schema.find_field(path)
        assert f.default == actual, path


def test_every_parsed_section_has_a_schema_group():
    tables = {g.table[0] for g in schema.GROUPS}
    for section in (
        "admin",
        "server",
        "routing",
        "database",
        "storage",
        "lifecycle",
        "agent",
        "production",
        "backup",
        "team",
        "route",
    ):
        assert section in tables, section
