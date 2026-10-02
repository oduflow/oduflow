"""Unit tests for the short-lived Odoo.sh import tokens."""

import json
import os

import pytest

from oduflow import import_tokens
from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _settings(team):
    return Settings(teams={team.team_id: team})


def test_create_and_load_roundtrip(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "zipfit")
    assert rec["template_name"] == "zipfit"
    assert rec["addon_error_policy"] == "strict"
    team2, rec2 = import_tokens.load_token(settings, rec["token"])
    assert team2.team_id == "1"
    assert rec2["token"] == rec["token"]
    assert rec2["template_name"] == "zipfit"
    assert rec2["addon_error_policy"] == "strict"


def test_best_effort_addon_policy_roundtrip(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(
        team,
        "zipfit",
        addon_error_policy="best_effort",
    )

    _team2, loaded = import_tokens.load_token(settings, rec["token"])

    assert loaded["addon_error_policy"] == "best_effort"


def test_without_filestore_defaults_false_and_roundtrips(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    default = import_tokens.create_token(team, "zipfit")
    skipped = import_tokens.create_token(team, "zipfit", without_filestore=True)

    assert default["without_filestore"] is False
    _t, loaded = import_tokens.load_token(settings, skipped["token"])
    assert loaded["without_filestore"] is True


def test_legacy_token_without_filestore_key_reads_as_false(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "zipfit")
    path = os.path.join(team.data_dir, "import_tokens", f"{rec['token']}.json")
    with open(path) as f:
        legacy = json.load(f)
    legacy.pop("without_filestore")
    with open(path, "w") as f:
        json.dump(legacy, f)

    _t, loaded = import_tokens.load_token(settings, rec["token"])

    assert bool(loaded.get("without_filestore")) is False


def test_invalid_addon_policy_rejected(tmp_path):
    with pytest.raises(ValueError, match="addon_error_policy"):
        import_tokens.create_token(
            _team(tmp_path),
            "zipfit",
            addon_error_policy="ignore_everything",
        )


def test_malformed_and_unknown_tokens_raise_not_found(tmp_path):
    settings = _settings(_team(tmp_path))
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "")
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "has spaces!")
    # Well-formed shape but never issued.
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "a" * 24)


def test_expired_token_is_rejected_and_removed(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "tpl", ttl_seconds=1, now=1000.0)
    with pytest.raises(PrerequisiteNotMetError):
        import_tokens.load_token(settings, rec["token"], now=1002.0)
    # The expired token file is deleted, so a second look is a plain NotFound.
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, rec["token"], now=1002.0)


def test_invalidate_deletes_token(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "tpl")
    import_tokens.invalidate(team, rec["token"])
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, rec["token"])


def test_create_reaps_expired_tokens(tmp_path):
    team = _team(tmp_path)
    old = import_tokens.create_token(team, "old", ttl_seconds=1, now=1000.0)
    # A later mint triggers cleanup of the now-expired token file.
    import_tokens.create_token(team, "new", now=5000.0)
    assert not os.path.exists(import_tokens._token_path(team, old["token"]))


def test_invalid_template_name_rejected(tmp_path):
    team = _team(tmp_path)
    with pytest.raises(ValueError):
        import_tokens.create_token(team, "../escape")


class TestTokenShape:
    def test_a_minted_token_is_32_url_safe_chars(self, tmp_path):
        # The regex below is what keeps a token out of a path-traversal
        # position, so the minted shape has to stay inside it.
        rec = import_tokens.create_token(_team(tmp_path), "zipfit")

        token = str(rec["token"])
        assert len(token) == 32
        assert import_tokens._TOKEN_RE.match(token)

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/passwd",
            "a/b",
            "short",
            "with space",
            "with.dot",
            "x" * 65,
        ],
    )
    def test_malformed_tokens_are_rejected_before_touching_the_disk(
        self, tmp_path, bad
    ):
        # A non-empty but malformed token must still be refused; only checking
        # for emptiness would let a traversal segment reach _token_path.
        settings = _settings(_team(tmp_path))

        with pytest.raises(NotFoundError):
            import_tokens.load_token(settings, bad)

    def test_an_empty_token_is_rejected(self, tmp_path):
        with pytest.raises(NotFoundError):
            import_tokens.load_token(_settings(_team(tmp_path)), "")


class TestExpiryBoundary:
    def test_a_token_is_valid_up_to_its_expiry_instant(self, tmp_path):
        # The check is `expires_at < now`, so the token survives the exact
        # second it expires.
        team = _team(tmp_path)
        rec = import_tokens.create_token(team, "zipfit", ttl_seconds=100, now=1000.0)

        _, loaded = import_tokens.load_token(
            _settings(team), str(rec["token"]), now=1100.0
        )

        assert loaded["token"] == rec["token"]

    def test_a_token_is_dead_one_second_later(self, tmp_path):
        team = _team(tmp_path)
        rec = import_tokens.create_token(team, "zipfit", ttl_seconds=100, now=1000.0)

        with pytest.raises(PrerequisiteNotMetError):
            import_tokens.load_token(_settings(team), str(rec["token"]), now=1101.0)

    def test_a_record_without_an_expiry_is_treated_as_expired(self, tmp_path):
        # Default 0, not 1: a hand-written record missing expires_at must not
        # be honoured indefinitely.
        team = _team(tmp_path)
        rec = import_tokens.create_token(team, "zipfit")
        path = import_tokens._token_path(team, str(rec["token"]))
        with open(path, "w") as f:
            json.dump({"token": rec["token"], "template_name": "zipfit"}, f)

        with pytest.raises(PrerequisiteNotMetError):
            import_tokens.load_token(_settings(team), str(rec["token"]))


class TestCleanupExpired:
    def test_a_token_at_its_expiry_instant_survives_the_reap(self, tmp_path):
        team = _team(tmp_path)
        rec = import_tokens.create_token(team, "zipfit", ttl_seconds=100, now=1000.0)

        import_tokens._cleanup_expired(team, now=1100.0)

        assert os.path.isfile(import_tokens._token_path(team, str(rec["token"])))

    def test_a_token_past_its_expiry_is_reaped(self, tmp_path):
        team = _team(tmp_path)
        rec = import_tokens.create_token(team, "zipfit", ttl_seconds=100, now=1000.0)

        import_tokens._cleanup_expired(team, now=1101.0)

        assert not os.path.isfile(import_tokens._token_path(team, str(rec["token"])))

    def test_an_unreadable_token_file_is_dropped(self, tmp_path):
        team = _team(tmp_path)
        import_tokens.create_token(team, "zipfit")
        junk = os.path.join(import_tokens._tokens_dir(team), "garbage.json")
        with open(junk, "w") as f:
            f.write("{not json")

        import_tokens._cleanup_expired(team, now=0.0)

        assert not os.path.isfile(junk)

    def test_non_json_files_are_left_alone(self, tmp_path):
        team = _team(tmp_path)
        import_tokens.create_token(team, "zipfit")
        other = os.path.join(import_tokens._tokens_dir(team), "README.txt")
        with open(other, "w") as f:
            f.write("keep me")

        import_tokens._cleanup_expired(team, now=10**9)

        assert os.path.isfile(other)

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        import_tokens._cleanup_expired(_team(tmp_path), now=0.0)  # must not raise


class TestCrossTeamLookup:
    def test_a_token_is_found_in_the_team_that_minted_it(self, tmp_path):
        first = TeamSettings(team_id="1", data_dir=str(tmp_path / "t1"))
        second = TeamSettings(
            team_id="2",
            data_dir=str(tmp_path / "t2"),
            port_range_start=50100,
            port_range_end=50200,
        )
        settings = Settings(teams={"1": first, "2": second})
        rec = import_tokens.create_token(second, "zipfit")

        team, loaded = import_tokens.load_token(settings, str(rec["token"]))

        assert team.team_id == "2"
        assert loaded["template_name"] == "zipfit"
