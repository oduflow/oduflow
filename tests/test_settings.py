from dataclasses import replace
from pathlib import Path

import pytest

from oduflow.settings import DEFAULT_AGENT_IMAGE, Settings, TeamSettings, find_toml


class TestSettings:
    def test_find_toml_explicit_env_has_priority(self, monkeypatch, tmp_path):
        explicit = tmp_path / "custom.toml"
        monkeypatch.setenv("ODUFLOW_TOML", str(explicit))

        def exists(path):
            return path in {str(explicit), "/etc/oduflow/oduflow.toml"}

        monkeypatch.setattr("oduflow.settings.os.path.isfile", exists)

        assert find_toml() == str(explicit)

    def test_find_toml_implicit_candidates(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        user_conf = home / ".oduflow" / "conf" / "oduflow.toml"
        monkeypatch.delenv("ODUFLOW_TOML", raising=False)
        monkeypatch.setenv("HOME", str(home))

        seen = []

        def exists(path):
            seen.append(path)
            return path == str(user_conf)

        monkeypatch.setattr("oduflow.settings.os.path.isfile", exists)

        assert find_toml() == str(user_conf)
        assert seen == [
            "/etc/oduflow/oduflow.toml",
            str(user_conf),
        ]

    def test_find_toml_ignores_legacy_home_file(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        legacy = home / ".oduflow" / "oduflow.toml"
        monkeypatch.delenv("ODUFLOW_TOML", raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(
            "oduflow.settings.os.path.isfile", lambda path: path == str(legacy)
        )

        with pytest.raises(FileNotFoundError) as exc:
            find_toml()

        message = str(exc.value)
        assert str(legacy) not in message
        assert str(home / ".oduflow" / "conf" / "oduflow.toml") in message

    def test_lifecycle_defaults(self):
        s = Settings(teams={"1": TeamSettings(team_id="1")})
        assert s.auto_stop_hours == 48
        # auto-delete and prod purge are destructive, so they are opt-in
        # (disabled by default).
        assert s.auto_delete_hours == 0
        assert s.prod_purge_hours == 0

    def test_malformed_port_range_raises(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\nport_range = [50000]\n')
        with pytest.raises(ValueError, match="port_range"):
            Settings.from_toml(str(toml))

    def test_lifecycle_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[lifecycle]\nauto_stop_hours = 12\nauto_delete_hours = 0\n"
            "prod_purge_hours = 168\n"
            '[team.1]\nhostname = "localhost"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.auto_stop_hours == 12
        assert s.auto_delete_hours == 0
        assert s.prod_purge_hours == 168

    def test_defaults(self):
        s = Settings()
        assert s.db_user == "odoo"
        assert s.routing_mode == "port"

    def test_routing_hostname_is_ignored_in_port_mode(self, tmp_path, caplog):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "port"\nhostname = "shared.example.com"\n[team.1]\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.get_team("1").hostname == ""
        assert "[routing].hostname is ignored" in caplog.text
        with pytest.raises(ValueError, match="hostname must be set explicitly"):
            s.validate()

    def test_routing_hostname_ignored_in_traefik_mode(self, tmp_path):
        # In traefik mode the shared default is NOT inherited; a team without
        # its own hostname is left empty so validate() flags the misconfig
        # instead of silently colliding two teams on one host.
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\nacme_email = "a@b.co"\n'
            'hostname = "shared.example.com"\n[team.1]\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.get_team("1").hostname == ""
        with pytest.raises(ValueError, match="hostname must be set"):
            s.validate()

    def test_bind_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[server]\nbind = "127.0.0.1"\n[team.1]\nhostname = "localhost"\n'
        )

        assert Settings.from_toml(str(toml)).bind_host == "127.0.0.1"

    def test_legacy_server_host_is_supported(self, tmp_path, caplog):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[server]\nhost = "127.0.0.1"\n[team.1]\nhostname = "localhost"\n'
        )

        assert Settings.from_toml(str(toml)).bind_host == "127.0.0.1"
        assert "[server].host is deprecated" in caplog.text

    def test_matching_bind_and_legacy_host_are_supported(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[server]\nbind = "127.0.0.1"\nhost = "127.0.0.1"\n'
            '[team.1]\nhostname = "localhost"\n'
        )

        assert Settings.from_toml(str(toml)).bind_host == "127.0.0.1"

    def test_conflicting_bind_and_legacy_host_are_rejected(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[server]\nbind = "127.0.0.1"\nhost = "0.0.0.0"\n'
            '[team.1]\nhostname = "localhost"\n'
        )

        with pytest.raises(ValueError, match="disagree"):
            Settings.from_toml(str(toml))

    def test_empty_bind_is_rejected(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[server]\nbind = ""\n[team.1]\nhostname = "localhost"\n')

        with pytest.raises(ValueError, match="must not be empty"):
            Settings.from_toml(str(toml))

    def test_empty_legacy_host_is_rejected(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[server]\nhost = ""\n[team.1]\nhostname = "localhost"\n')

        with pytest.raises(ValueError, match="must not be empty"):
            Settings.from_toml(str(toml))

    def test_validate_no_teams(self):
        s = Settings()
        with pytest.raises(ValueError, match="No \\[team\\.\\*\\] sections"):
            s.validate()

    def test_validate_ok(self):
        team = TeamSettings(team_id="1", port_range_start=50000, port_range_end=50100)
        s = Settings(teams={"1": team})
        s.validate()

    def test_validate_port_range(self):
        team = TeamSettings(team_id="1", port_range_start=50100, port_range_end=50000)
        s = Settings(teams={"1": team})
        with pytest.raises(ValueError, match="invalid port range"):
            s.validate()

    def test_team_slots_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\n'
            '[team.1]\nhostname = "dev.example.com"\n'
            "environment_slots = 7\n"
            'environment_hostname_mode = "slots"\n'
            "service_slots = 3\n"
        )

        team = Settings.from_toml(str(toml)).get_team("1")

        assert team.environment_slots == 7
        assert team.environment_hostname_mode == "slots"
        assert team.service_slots == 3
        assert team.hostname_registry_path.endswith("team_1/hostnames.json")

    def test_team_slot_defaults(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text("[team.1]\n")

        team = Settings.from_toml(str(toml)).get_team("1")

        assert team.environment_slots == 20
        assert team.environment_hostname_mode == "branch"
        assert team.service_slots == 10

    def test_negative_environment_slots_rejected(self):
        team = TeamSettings(team_id="1", environment_slots=-1)
        settings = Settings(teams={"1": team})

        with pytest.raises(ValueError, match="environment_slots"):
            settings.validate()

    def test_negative_service_slots_rejected(self):
        team = TeamSettings(team_id="1", service_slots=-1)
        settings = Settings(teams={"1": team})

        with pytest.raises(ValueError, match="service_slots"):
            settings.validate()

    def test_base_domain_defaults_hostname(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\n'
            '[team.1]\nbase_domain = "demo.example.com"\n'
        )

        team = Settings.from_toml(str(toml)).get_team("1")

        assert team.base_domain == "demo.example.com"
        assert team.hostname == "oduflow.demo.example.com"

    def test_explicit_hostname_wins_over_base_domain_default(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\n'
            '[team.1]\nbase_domain = "demo.example.com"\n'
            'hostname = "panel.demo.example.com"\n'
        )

        assert (
            Settings.from_toml(str(toml)).get_team("1").hostname
            == "panel.demo.example.com"
        )

    def test_base_domain_requires_traefik(self):
        team = TeamSettings(
            team_id="1",
            hostname="oduflow.demo.example.com",
            base_domain="demo.example.com",
        )

        with pytest.raises(ValueError, match="base_domain requires"):
            Settings(teams={"1": team}).validate()

    def test_invalid_base_domain_rejected(self):
        team = TeamSettings(
            team_id="1", hostname="oduflow.example.com", base_domain="not a domain"
        )
        settings = Settings(
            routing_mode="traefik", routing_tls=False, teams={"1": team}
        )

        with pytest.raises(ValueError, match="invalid base_domain"):
            settings.validate()

    def test_duplicate_base_domain_rejected(self):
        teams = {
            "1": TeamSettings(
                team_id="1",
                hostname="a.demo.example.com",
                base_domain="demo.example.com",
            ),
            "2": TeamSettings(
                team_id="2",
                hostname="b.demo.example.com",
                base_domain="demo.example.com",
            ),
        }
        settings = Settings(routing_mode="traefik", routing_tls=False, teams=teams)

        with pytest.raises(ValueError, match="duplicate base_domain"):
            settings.validate()

    def test_team_hostname_inside_other_team_zone_rejected(self):
        teams = {
            "1": TeamSettings(
                team_id="1",
                hostname="oduflow.demo.example.com",
                base_domain="demo.example.com",
            ),
            "2": TeamSettings(team_id="2", hostname="intruder.demo.example.com"),
        }
        settings = Settings(routing_mode="traefik", routing_tls=False, teams=teams)

        with pytest.raises(ValueError, match="lies inside team '1'"):
            settings.validate()

    def test_invalid_environment_hostname_mode_rejected(self):
        team = TeamSettings(team_id="1", environment_hostname_mode="magic")

        with pytest.raises(ValueError, match="environment_hostname_mode"):
            Settings(teams={"1": team}).validate()

    def test_hostname_slots_require_traefik(self):
        team = TeamSettings(team_id="1", environment_hostname_mode="slots")

        with pytest.raises(ValueError, match="requires routing_mode=traefik"):
            Settings(teams={"1": team}).validate()

    def test_hostname_slots_require_positive_environment_limit(self):
        team = TeamSettings(
            team_id="1",
            hostname="dev.example.com",
            environment_slots=0,
            environment_hostname_mode="slots",
        )
        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            teams={"1": team},
        )

        with pytest.raises(ValueError, match="environment_slots > 0"):
            settings.validate()

    def test_hostname_slots_require_prefixed_domain(self):
        team = TeamSettings(
            team_id="1",
            hostname="localhost",
            environment_slots=2,
            environment_hostname_mode="slots",
        )
        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            teams={"1": team},
        )

        with pytest.raises(ValueError, match="dev.example.com"):
            settings.validate()

    def test_validate_overlapping_port_ranges(self):
        # Two teams left on the default (identical) range would draw host ports
        # from the same pool — issue #46.
        t1 = TeamSettings(
            team_id="1",
            hostname="one.example.com",
            port_range_start=50000,
            port_range_end=50100,
        )
        t2 = TeamSettings(
            team_id="2",
            hostname="two.example.com",
            port_range_start=50050,
            port_range_end=50150,
        )
        s = Settings(teams={"1": t1, "2": t2})
        with pytest.raises(ValueError, match="overlapping port ranges"):
            s.validate()

    def test_validate_adjacent_port_ranges_ok(self):
        # Half-open ranges that merely touch at the boundary do not overlap.
        t1 = TeamSettings(
            team_id="1",
            hostname="one.example.com",
            port_range_start=50000,
            port_range_end=50100,
        )
        t2 = TeamSettings(
            team_id="2",
            hostname="two.example.com",
            port_range_start=50100,
            port_range_end=50200,
        )
        s = Settings(teams={"1": t1, "2": t2})
        s.validate()

    def test_validate_duplicate_team_hostnames(self):
        t1 = TeamSettings(
            team_id="1",
            hostname="same.example.com",
            port_range_start=50000,
            port_range_end=50100,
        )
        t2 = TeamSettings(
            team_id="2",
            hostname="SAME.example.com",
            port_range_start=50100,
            port_range_end=50200,
        )

        with pytest.raises(ValueError, match="duplicate hostname"):
            Settings(teams={"1": t1, "2": t2}).validate()

    def test_get_team_by_hostname_is_case_insensitive_and_strips_port(self):
        team = TeamSettings(team_id="1", hostname="Oduflow.Example.com")
        settings = Settings(teams={"1": team})

        assert settings.get_team_by_hostname("oduflow.example.com:8000") is team

    @pytest.mark.parametrize(
        "authority",
        [
            "oduflow.example.com:443@evil.example.com",
            "oduflow.example.com:not-a-port",
            "oduflow.example.com/path",
        ],
    )
    def test_get_team_by_hostname_rejects_malformed_authority(self, authority):
        team = TeamSettings(team_id="1", hostname="oduflow.example.com")
        settings = Settings(teams={"1": team})

        assert settings.get_team_by_hostname(authority) is None

    def test_frozen(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.routing_mode = "changed"  # type: ignore[misc]

    def test_get_team(self):
        team = TeamSettings(team_id="1")
        s = Settings(teams={"1": team})
        assert s.get_team("1") is team

    def test_get_team_not_found(self):
        s = Settings(teams={})
        with pytest.raises(ValueError, match="Team '99' not found"):
            s.get_team("99")

    def test_get_team_by_token(self):
        team = TeamSettings(team_id="1", auth_token="secret-token")
        s = Settings(teams={"1": team})
        assert s.get_team_by_token("secret-token") is team
        assert s.get_team_by_token("wrong") is None

    def test_validate_duplicate_tokens(self):
        t1 = TeamSettings(
            team_id="1",
            hostname="one.example.com",
            auth_token="same",
            port_range_start=50000,
            port_range_end=50100,
        )
        t2 = TeamSettings(
            team_id="2",
            hostname="two.example.com",
            auth_token="same",
            port_range_start=50100,
            port_range_end=50200,
        )
        s = Settings(teams={"1": t1, "2": t2})
        with pytest.raises(ValueError, match="Duplicate auth_token"):
            s.validate()

    def test_routing_tls_default(self):
        # Traefik terminates TLS by default.
        assert Settings().routing_tls is True

    def test_routing_tls_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\n'
            '[team.1]\nhostname = "dev.example.com"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.routing_tls is False

    def test_validate_traefik_tls_requires_acme_email(self):
        # tls = true (default) still requires an ACME e-mail.
        team = TeamSettings(team_id="1", hostname="dev.example.com")
        s = Settings(routing_mode="traefik", acme_email="", teams={"1": team})
        with pytest.raises(ValueError, match="acme_email"):
            s.validate()

    def test_validate_traefik_no_tls_acme_email_optional(self):
        # Behind a TLS-terminating upstream (tls = false) ACME is unused, so
        # acme_email is not required.
        team = TeamSettings(team_id="1", hostname="dev.example.com")
        s = Settings(
            routing_mode="traefik", routing_tls=False, acme_email="", teams={"1": team}
        )
        s.validate()


class TestPublicScheme:
    """[routing] public_scheme: the scheme of every URL Oduflow hands out."""

    def _settings(self, routing_mode="traefik", tls=True, scheme=""):
        team = TeamSettings(team_id="1", hostname="dev.example.com")
        return Settings(
            routing_mode=routing_mode,
            routing_tls=tls,
            acme_email="admin@example.com",
            public_scheme_setting=scheme,
            teams={"1": team},
        )

    def test_traefik_defaults_to_https(self):
        assert self._settings().public_scheme == "https"

    def test_traefik_without_tls_still_defaults_to_https(self):
        # tls = false means "an upstream terminates TLS" (Cloudflare tunnel),
        # not "plain HTTP" — the public URL keeps the upstream's scheme.
        assert self._settings(tls=False).public_scheme == "https"

    def test_port_mode_defaults_to_http(self):
        assert self._settings(routing_mode="port").public_scheme == "http"

    def test_explicit_http_overrides_traefik_default(self):
        # The whole point of the option: a tls = false deployment with nothing
        # terminating TLS in front must hand out reachable http:// links.
        assert self._settings(tls=False, scheme="http").public_scheme == "http"

    def test_validate_rejects_https_in_port_mode(self):
        # Published per-environment ports serve plain HTTP; https:// links (and
        # the internal probes built from get_env_base_url) could never work.
        with pytest.raises(ValueError, match="port mode"):
            self._settings(routing_mode="port", scheme="https").validate()

    def test_validate_rejects_http_with_traefik_tls(self):
        # tls = true keeps the :80->:443 redirect, so http:// links would
        # bounce and leak tokens on the first plaintext hop.
        with pytest.raises(ValueError, match="tls = false"):
            self._settings(tls=True, scheme="http").validate()

    def test_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\npublic_scheme = "HTTP"\n'
            '[team.1]\nhostname = "dev.example.com"\n'
        )
        s = Settings.from_toml(str(toml))
        # Case-insensitive, and it does not touch routing_tls.
        assert s.public_scheme == "http"
        assert s.routing_tls is False

    def test_from_toml_default_is_empty(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\n[team.1]\nhostname = "dev.example.com"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.public_scheme_setting == ""
        assert s.public_scheme == "https"

    def test_validate_rejects_other_schemes(self):
        with pytest.raises(ValueError, match="public_scheme"):
            self._settings(scheme="ftp").validate()

    def test_validate_accepts_supported_schemes(self):
        # Every combination that matches what actually answers on the wire.
        self._settings(scheme="").validate()
        self._settings(scheme="https").validate()
        self._settings(tls=False, scheme="http").validate()
        self._settings(routing_mode="port", scheme="http").validate()


class TestTeamPublicScheme:
    """[team.X] public_scheme: per-team override of the global URL scheme.

    The mixed-deployment case: one team on plain HTTP over the LAN, another
    behind a TLS-terminating upstream (Cloudflare tunnel) handing out https://
    links — same Traefik, same tls = false.
    """

    def _settings(self, routing_mode="traefik", tls=False, scheme="", team_schemes=()):
        teams = {}
        for i, team_scheme in enumerate(team_schemes or ("",), start=1):
            teams[str(i)] = TeamSettings(
                team_id=str(i),
                hostname=f"dev{i}.example.com",
                port_range_start=50000 + i * 200,
                port_range_end=50100 + i * 200,
                public_scheme_setting=team_scheme,
            )
        return Settings(
            routing_mode=routing_mode,
            routing_tls=tls,
            acme_email="admin@example.com",
            public_scheme_setting=scheme,
            teams=teams,
        )

    def test_team_override_wins_over_global(self):
        s = self._settings(scheme="http", team_schemes=("", "https"))
        assert s.public_scheme_for(s.teams["1"]) == "http"
        assert s.public_scheme_for(s.teams["2"]) == "https"

    def test_empty_override_falls_back_to_derived_default(self):
        s = self._settings(team_schemes=("",))
        assert s.public_scheme_for(s.teams["1"]) == "https"

    def test_any_public_scheme_https(self):
        assert not self._settings(
            scheme="http", team_schemes=("", "http")
        ).any_public_scheme_https
        assert self._settings(
            scheme="http", team_schemes=("", "https")
        ).any_public_scheme_https
        # A team without an override resolves to the global default
        # (traefik → https), so it counts.
        assert self._settings(team_schemes=("",)).any_public_scheme_https

    def test_all_teams_http_disables_forwarded_header_trust(self):
        # The global default derives to https, but every team overrides to
        # http: no URL Oduflow hands out is https, so the unused global value
        # must not keep Traefik trusting client-supplied X-Forwarded-*.
        assert not self._settings(team_schemes=("http", "http")).any_public_scheme_https

    def test_from_toml_parses_team_public_scheme(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\npublic_scheme = "http"\n'
            '[team.1]\nhostname = "dev1.example.com"\nport_range = [50100, 50200]\n'
            '[team.2]\nhostname = "dev2.example.com"\nport_range = [50300, 50400]\n'
            'public_scheme = "HTTPS"\n'
        )
        s = Settings.from_toml(str(toml))
        s.validate()
        # Case-insensitive, and the other team keeps the global value.
        assert s.public_scheme_for(s.teams["1"]) == "http"
        assert s.public_scheme_for(s.teams["2"]) == "https"

    def test_validate_rejects_other_schemes(self):
        with pytest.raises(ValueError, match="Team '1'.*public_scheme"):
            self._settings(team_schemes=("ftp",)).validate()

    def test_validate_rejects_team_https_in_port_mode(self):
        # Same wire-reality rule as the global setting: published ports serve
        # plain HTTP, so per-team https:// links could never work either.
        with pytest.raises(ValueError, match="Team '1'.*port mode"):
            self._settings(routing_mode="port", team_schemes=("https",)).validate()

    def test_validate_rejects_team_http_with_traefik_tls(self):
        # tls = true keeps the :80->:443 redirect for every hostname, so a
        # per-team http:// link would bounce just like a global one.
        with pytest.raises(ValueError, match="Team '1'.*tls = false"):
            self._settings(tls=True, team_schemes=("http",)).validate()

    def test_validate_accepts_mixed_schemes_without_tls(self):
        self._settings(scheme="http", team_schemes=("", "https")).validate()


class TestOAuthSettings:
    def _team(self, **kw):
        defaults = {"team_id": "1", "port_range_start": 50000, "port_range_end": 50100}
        defaults.update(kw)
        return TeamSettings(**defaults)

    def test_oauth_defaults(self):
        s = Settings()
        assert not s.oauth_enabled

    def test_oauth_enabled_by_team_token(self):
        s = Settings(teams={"1": self._team(auth_token="tok-a")})

        s.validate()

        assert s.oauth_enabled

    def test_removed_oauth_section_is_rejected(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[oauth]\noauth_base_url = "https://example.com"\n'
            '[team.1]\nhostname = "example.com"\n'
        )

        with pytest.raises(ValueError, match=r"\[oauth\] has been removed"):
            Settings.from_toml(str(toml))

    def test_removed_server_oauth_base_url_is_rejected(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[server]\noauth_base_url = "https://example.com"\n'
            '[team.1]\nhostname = "example.com"\n'
        )

        with pytest.raises(ValueError, match="oauth_base_url has been removed"):
            Settings.from_toml(str(toml))


class TestTeamSettings:
    def test_defaults(self):
        t = TeamSettings(team_id="1")
        assert t.team_id == "1"
        assert t.hostname == "localhost"
        assert t.port_range_start == 50000
        assert t.port_range_end == 50100

    def test_workspaces_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.workspaces_dir == "/srv/data/workspaces"

    def test_shared_repos_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.shared_repos_dir == "/srv/data/shared_repos"

    def test_shared_extra_checkouts_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.shared_extra_checkouts_dir == "/srv/data/shared_extra_checkouts"

    def test_frozen(self):
        t = TeamSettings(team_id="1")
        with pytest.raises(AttributeError):
            t.team_id = "2"  # type: ignore[misc]


class TestTeamTemplatePaths:
    def test_get_template_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_dir("default") == "/srv/data/templates/default"

    def test_get_template_dir_named(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_dir("myproject") == "/srv/data/templates/myproject"

    def test_get_template_sql_path(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_sql_path("v17") == "/srv/data/templates/v17/dump.pgdump"

    def test_get_template_sql_path_accepts_db_dump(self, tmp_path):
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        tpl_dir = tmp_path / "templates" / "v17"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "db.dump").write_bytes(b"PGDMP")
        assert t.get_template_sql_path("v17") == str(tpl_dir / "db.dump")

    def test_get_template_sql_path_accepts_db_dump_gz(self, tmp_path):
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        tpl_dir = tmp_path / "templates" / "v17"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "db.dump.gz").write_bytes(b"\x1f\x8b")
        assert t.get_template_sql_path("v17") == str(tpl_dir / "db.dump.gz")

    def test_canonical_dump_wins_over_db_dump(self, tmp_path):
        """A dump Oduflow persisted must outrank a hand-placed leftover."""
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        tpl_dir = tmp_path / "templates" / "v17"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "db.dump").write_bytes(b"PGDMP")
        (tpl_dir / "dump.pgdump").write_bytes(b"PGDMP")
        assert t.get_template_sql_path("v17") == str(tpl_dir / "dump.pgdump")

    def test_get_template_filestore_path(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert (
            t.get_template_filestore_path("v17") == "/srv/data/templates/v17/filestore"
        )

    def test_dump_methods_delegate_to_template(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert (
            t.get_template_sql_path("default")
            == "/srv/data/templates/default/dump.pgdump"
        )
        assert (
            t.get_template_filestore_path("default")
            == "/srv/data/templates/default/filestore"
        )

    def test_list_templates_empty(self, tmp_path):
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        assert t.list_templates() == []

    def test_list_templates(self, tmp_path):
        templates_dir = tmp_path / "templates"
        (templates_dir / "alpha").mkdir(parents=True)
        (templates_dir / "beta").mkdir(parents=True)
        (templates_dir / "not-a-dir").touch()  # should be ignored
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        assert t.list_templates() == ["alpha", "beta"]

    def test_git_credentials_file(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.git_credentials_file() == "/srv/data/.git-credentials"


class TestQuotas:
    def test_defaults(self):
        t = TeamSettings(team_id="1")
        assert t.db_quota_gb == 50
        assert t.disk_quota_gb == 0  # off until FS quota enforcement lands

    def test_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[team.1]\nhostname = "localhost"\ndb_quota_gb = 10\ndisk_quota_gb = 20\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.teams["1"].db_quota_gb == 10
        assert s.teams["1"].disk_quota_gb == 20

    def test_negative_quota_rejected(self):
        team = TeamSettings(team_id="1", db_quota_gb=-1)
        s = Settings(teams={"1": team})
        with pytest.raises(ValueError, match="quotas must be >= 0"):
            s.validate()


class TestAgentSettings:
    def test_global_defaults(self):
        s = Settings()
        assert s.agent_image == "oduist/oduflow-coder:0.3.1"
        assert s.agent_claude_model == ""
        assert s.agent_codex_model == ""
        assert s.agent_opencode_model == ""

    def test_default_image_matches_dockerfile_version(self):
        dockerfile = Path(__file__).parents[1] / "docker" / "agent" / "Dockerfile"
        version = next(
            line.removeprefix("ARG CODER_VERSION=")
            for line in dockerfile.read_text().splitlines()
            if line.startswith("ARG CODER_VERSION=")
        )
        assert DEFAULT_AGENT_IMAGE == f"oduist/oduflow-coder:{version}"

    def test_team_defaults_agent_off(self):
        # The agent is a hosting feature; a team must opt in explicitly.
        t = TeamSettings(team_id="1")
        assert t.agent_enabled is False
        assert t.agent_default == "claude"
        assert t.agent_env == {}

    def test_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[agent]\n"
            'image = "oduist/oduflow-coder:dev"\n'
            'claude_model = "claude-sonnet-4-6"\n'
            'opencode_model = "anthropic/claude-sonnet-4-6"\n'
            "\n"
            '[team.1]\nhostname = "localhost"\n'
            "agent_enabled = true\n"
            'agent_default = "Codex"\n'
            "[team.1.agent_env]\n"
            'OPENAI_API_KEY = "sk-oai"\n'
            "MY_FLAG = 1\n"
        )
        s = Settings.from_toml(str(toml))
        assert s.agent_image == "oduist/oduflow-coder:dev"
        assert s.agent_claude_model == "claude-sonnet-4-6"
        assert s.agent_codex_model == ""
        assert s.agent_opencode_model == "anthropic/claude-sonnet-4-6"
        team = s.teams["1"]
        assert team.agent_enabled is True
        assert team.agent_default == "codex"  # normalised to lowercase
        # Values are coerced to strings (env vars).
        assert team.agent_env == {"OPENAI_API_KEY": "sk-oai", "MY_FLAG": "1"}

    def test_missing_keys_use_defaults(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\n')
        s = Settings.from_toml(str(toml))
        assert s.agent_image == DEFAULT_AGENT_IMAGE
        assert s.teams["1"].agent_enabled is False
        assert s.teams["1"].agent_env == {}

    def test_legacy_latest_image_migrates_to_pinned_default(self, tmp_path, caplog):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[agent]\nimage = "oduist/oduflow-coder:latest"\n'
            '[team.1]\nhostname = "localhost"\n'
        )

        s = Settings.from_toml(str(toml))

        assert s.agent_image == DEFAULT_AGENT_IMAGE
        assert "is deprecated; using pinned image" in caplog.text

    def test_agent_env_must_be_table(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\nagent_env = "oops"\n')
        with pytest.raises(ValueError, match="agent_env must be a table"):
            Settings.from_toml(str(toml))


class TestExtraRoutes:
    def _traefik_toml(self, tmp_path, body):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\nacme_email = "a@b.co"\n'
            '[team.1]\nhostname = "dev.example.com"\n' + body
        )
        return toml

    def test_parse_route(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.legacy-api]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n',
        )
        s = Settings.from_toml(str(toml))
        s.validate()
        assert len(s.extra_routes) == 1
        route = s.extra_routes[0]
        assert route.name == "legacy-api"
        assert route.host == "api.example.com"
        assert route.url == "http://127.0.0.1:3000"

    def test_host_scheme_is_stripped(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "https://api.example.com/"\nurl = "http://10.0.0.5:80"\n',
        )
        s = Settings.from_toml(str(toml))
        assert s.extra_routes[0].host == "api.example.com"

    def test_route_host_with_path_rejected(self, tmp_path):
        # A path component would land in Traefik's Host() rule and silently
        # never match; it must be a loud config error.
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "api.example.com/v1"\nurl = "http://127.0.0.1:3000"\n',
        )
        with pytest.raises(ValueError, match="plain hostname"):
            Settings.from_toml(str(toml)).validate()

    def test_route_requires_traefik_mode(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "port"\n[team.1]\nhostname = "localhost"\n'
            '[route.r]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n'
        )
        with pytest.raises(ValueError, match="require routing.mode = 'traefik'"):
            Settings.from_toml(str(toml)).validate()

    def test_route_empty_url_rejected(self, tmp_path):
        toml = self._traefik_toml(tmp_path, '[route.r]\nhost = "api.example.com"\n')
        with pytest.raises(ValueError, match="url must be set"):
            Settings.from_toml(str(toml)).validate()

    def test_route_bad_url_scheme_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "api.example.com"\nurl = "ftp://x/1"\n',
        )
        with pytest.raises(ValueError, match="url must start with http"):
            Settings.from_toml(str(toml)).validate()

    def test_route_host_collision_with_team_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "dev.example.com"\nurl = "http://127.0.0.1:3000"\n',
        )
        with pytest.raises(ValueError, match="collides"):
            Settings.from_toml(str(toml)).validate()

    def test_route_host_collision_with_team_is_case_insensitive(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "DEV.example.com"\nurl = "http://127.0.0.1:3000"\n',
        )
        with pytest.raises(ValueError, match="collides"):
            Settings.from_toml(str(toml)).validate()

    def test_route_host_collision_between_routes_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.a]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n'
            '[route.b]\nhost = "api.example.com"\nurl = "http://127.0.0.1:4000"\n',
        )
        with pytest.raises(ValueError, match="collides"):
            Settings.from_toml(str(toml)).validate()


class TestProductionSettings:
    def test_defaults(self):
        s = Settings()
        assert s.prod_enabled is False
        assert s.prod_db_container == "oduflow-prod-db"
        assert s.prod_db_volume == "oduflow-prod-db-data"
        assert s.prod_postgres_image == ""
        assert s.prod_workers_cap == 8
        assert s.backup is None

    def test_production_section_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[production]\n"
            "enabled = true\n"
            'postgres_image = "postgres:17"\n'
            "workers_cap = 12\n"
            '[team.1]\nhostname = "localhost"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.prod_enabled is True
        assert s.prod_postgres_image == "postgres:17"
        assert s.prod_workers_cap == 12

    def test_production_section_without_enabled_stays_disabled(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text("[production]\nworkers_cap = 12\n[team.1]\n")
        assert Settings.from_toml(str(toml)).prod_enabled is False

    @pytest.mark.parametrize(
        "body",
        [
            "stop_free_gb = 4\nresume_free_gb = 3",
            "upload_timeout = 0",
            "warn_after = 301\nstall_after = 300",
            "stop_free_gb = nan",
            "warn_queue_gb = 8\nstop_queue_gb = 4",
            "stop_queue_gb = 0",
            "stop_within = -1",
        ],
    )
    def test_invalid_wal_safety_thresholds(self, tmp_path, body):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[production.wal]\n" + body + '\n[team.1]\nhostname="localhost"\n'
        )
        with pytest.raises(ValueError, match=r"\[production.wal\]"):
            Settings.from_toml(str(toml))

    def test_wal_thresholds_are_loaded(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[production.wal]\nupload_timeout=45\nwarn_queue_gb=3\nstop_queue_gb=12\nstop_free_gb=8\nresume_free_gb=16\n[team.1]\nhostname="localhost"\n'
        )
        settings = Settings.from_toml(str(toml))
        assert settings.wal_upload_timeout == 45
        assert settings.wal_warn_queue_gb == 3
        assert settings.wal_stop_queue_gb == 12
        assert settings.wal_stop_free_gb == 8
        assert settings.wal_resume_free_gb == 16

    def test_production_enabled_must_be_boolean(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[production]\nenabled = "true"\n[team.1]\n')
        with pytest.raises(ValueError, match="enabled must be true or false"):
            Settings.from_toml(str(toml))

    def test_bundled_template_has_commented_opt_in(self):
        template = (
            Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "oduflow.toml"
        ).read_text()
        assert "# [production]\n# enabled = true\n" in template


class TestBackupSettings:
    def _toml(self, tmp_path, body: str):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(body + '\n[team.1]\nhostname = "localhost"\n')
        return str(toml)

    def test_absent_section_disables_backups(self, tmp_path):
        s = Settings.from_toml(self._toml(tmp_path, ""))
        assert s.backup is None

    def test_minimal_section(self, tmp_path):
        s = Settings.from_toml(
            self._toml(
                tmp_path,
                '[backup]\nbucket = "b"\naccess_key = "ak"\nsecret_key = "sk"\n',
            )
        )
        assert s.backup is not None
        assert s.backup.bucket == "b"
        assert s.backup.prefix == "oduflow"
        assert s.backup.snapshot_time == "02:00"
        assert s.backup.walg_keep_full == 7
        assert s.backup.upload_threads == 16
        s.validate()

    def test_upload_threads_parsed_and_clamped(self, tmp_path):
        base = '[backup]\nbucket = "b"\naccess_key = "ak"\nsecret_key = "sk"\n'
        s = Settings.from_toml(self._toml(tmp_path, base + "upload_threads = 4\n"))
        assert s.backup is not None and s.backup.upload_threads == 4
        s = Settings.from_toml(self._toml(tmp_path, base + "upload_threads = 0\n"))
        assert s.backup is not None and s.backup.upload_threads == 1

    def test_partial_section_raises(self, tmp_path):
        with pytest.raises(ValueError, match="requires all of"):
            Settings.from_toml(self._toml(tmp_path, '[backup]\nbucket = "b"\n'))

    @pytest.mark.parametrize(
        "body",
        [
            '[backup]\nbucket = "b"\naccess_key = "ak"\n',
            '[backup]\nbucket = "b"\nsecret_key = "sk"\n',
            '[backup]\naccess_key = "ak"\nsecret_key = "sk"\n',
            '[backup]\naccess_key = "ak"\n',
            '[backup]\nsecret_key = "sk"\n',
            '[backup]\nbucket = "b"\naccess_key = "ak"\nsecret_key = "  "\n',
            '[backup]\nbucket = "  "\naccess_key = "ak"\nsecret_key = "sk"\n',
        ],
    )
    def test_every_missing_credential_is_rejected(self, tmp_path, body):
        # All three of bucket/access_key/secret_key are required together; any
        # one of them missing (or blank) must fail loudly rather than silently
        # disable backups.
        with pytest.raises(ValueError, match="requires all of"):
            Settings.from_toml(self._toml(tmp_path, body))

    def test_bad_snapshot_time_rejected_by_validate(self, tmp_path):
        s = Settings.from_toml(
            self._toml(
                tmp_path,
                '[backup]\nbucket = "b"\naccess_key = "a"\nsecret_key = "s"\n'
                'snapshot_time = "25:99"\n',
            )
        )
        with pytest.raises(ValueError, match="snapshot_time"):
            s.validate()

    def test_bad_keep_pair_rejected(self, tmp_path):
        s = Settings.from_toml(
            self._toml(
                tmp_path,
                '[backup]\nbucket = "b"\naccess_key = "a"\nsecret_key = "s"\n'
                'keep = ["weekly"]\n',
            )
        )
        with pytest.raises(ValueError, match="keep entries"):
            s.validate()

    def test_prefix_normalized(self, tmp_path):
        s = Settings.from_toml(
            self._toml(
                tmp_path,
                '[backup]\nbucket = "b"\naccess_key = "a"\nsecret_key = "s"\n'
                'prefix = "/my/prefix/"\n',
            )
        )
        assert s.backup.prefix == "my/prefix"


class TestDirectoryResolution:
    """Where Oduflow puts its data and config when the TOML says nothing.

    Both resolvers memoize into a module global. The probing (``os.access``)
    must happen exactly once, and the cached value must be the one returned on
    every later call — an inverted cache check re-probes forever or, worse,
    returns the unset global.
    """

    @pytest.fixture(autouse=True)
    def _reset_caches(self):
        from oduflow import settings as settings_mod

        settings_mod._cached_data_dir = None
        settings_mod._cached_etc_dir = None
        yield
        settings_mod._cached_data_dir = None
        settings_mod._cached_etc_dir = None

    def test_explicit_data_dir_wins_and_is_not_cached(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(settings_mod.os, "access", lambda *a: False)

        assert settings_mod._resolve_data_dir("/explicit") == "/explicit"
        # An explicit value must not poison the cache for later default lookups.
        assert settings_mod._cached_data_dir is None

    def test_data_dir_uses_srv_when_parent_is_writable(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.os, "access", lambda path, mode: path == "/srv"
        )

        assert settings_mod._resolve_data_dir("") == "/srv/oduflow"

    def test_data_dir_uses_existing_writable_srv_oduflow(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.os, "access", lambda path, mode: path == "/srv/oduflow"
        )
        monkeypatch.setattr(
            settings_mod.os.path, "isdir", lambda path: path == "/srv/oduflow"
        )

        assert settings_mod._resolve_data_dir("") == "/srv/oduflow"

    def test_data_dir_needs_both_isdir_and_write_access_for_the_fallback_probe(
        self, monkeypatch
    ):
        # /srv is not writable and /srv/oduflow exists but is read-only ->
        # neither branch qualifies, so the home directory is used.
        from oduflow import settings as settings_mod

        monkeypatch.setattr(settings_mod.os, "access", lambda path, mode: False)
        monkeypatch.setattr(settings_mod.os.path, "isdir", lambda path: True)
        monkeypatch.setattr(settings_mod.os.path, "expanduser", lambda p: "/home/me")

        assert settings_mod._resolve_data_dir("") == "/home/me/.oduflow/data"

    def test_data_dir_falls_back_to_home(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(settings_mod.os, "access", lambda *a: False)
        monkeypatch.setattr(settings_mod.os.path, "isdir", lambda path: False)
        monkeypatch.setattr(settings_mod.os.path, "expanduser", lambda p: "/home/me")

        assert settings_mod._resolve_data_dir("") == "/home/me/.oduflow/data"

    def test_data_dir_is_probed_once_and_then_served_from_cache(self, monkeypatch):
        from oduflow import settings as settings_mod

        calls = []

        def _access(path, mode):
            calls.append(path)
            return path == "/srv"

        monkeypatch.setattr(settings_mod.os, "access", _access)

        first = settings_mod._resolve_data_dir("")
        probes_after_first = len(calls)
        second = settings_mod._resolve_data_dir("")

        assert first == second == "/srv/oduflow"
        assert probes_after_first > 0
        assert len(calls) == probes_after_first  # no re-probing

    def test_etc_dir_uses_etc_oduflow_when_writable(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.os, "access", lambda path, mode: path == "/etc/oduflow"
        )

        assert settings_mod._resolve_etc_dir() == "/etc/oduflow"

    def test_etc_dir_accepts_a_writable_parent(self, monkeypatch):
        # /etc/oduflow does not exist yet, but /etc is writable, so it can be
        # created there.
        from oduflow import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.os, "access", lambda path, mode: path == "/etc"
        )

        assert settings_mod._resolve_etc_dir() == "/etc/oduflow"

    def test_etc_dir_falls_back_to_home(self, monkeypatch):
        from oduflow import settings as settings_mod

        monkeypatch.setattr(settings_mod.os, "access", lambda *a: False)
        monkeypatch.setattr(settings_mod.os.path, "expanduser", lambda p: "/home/me")

        assert settings_mod._resolve_etc_dir() == "/home/me/.oduflow/conf"

    def test_etc_dir_is_probed_once_and_then_served_from_cache(self, monkeypatch):
        from oduflow import settings as settings_mod

        calls = []

        def _access(path, mode):
            calls.append(path)
            return path == "/etc/oduflow"

        monkeypatch.setattr(settings_mod.os, "access", _access)

        first = settings_mod._resolve_etc_dir()
        probes_after_first = len(calls)
        second = settings_mod._resolve_etc_dir()

        assert first == second == "/etc/oduflow"
        assert probes_after_first > 0
        assert len(calls) == probes_after_first  # no re-probing


@pytest.mark.parametrize(
    "value, email, enabled, auto, resolver",
    [
        # tls = true + acme_email: resolver declared and auto-assigned.
        ("true", True, True, True, True),
        # tls = false: TLS and ACME both off.
        ("false", True, False, False, False),
        # tls = {} + acme_email: resolver declared, only explicit routes use it.
        ("{}", True, True, False, True),
        # tls = {} without acme_email: HTTPS without ACME.
        ("{}", False, True, False, False),
    ],
)
def test_tls_modes_from_toml(tmp_path, value, email, enabled, auto, resolver):
    config = tmp_path / "oduflow.toml"
    config.write_text(
        f'[routing]\nmode = "traefik"\ntls = {value}\n'
        + ('acme_email = "admin@example.com"\n' if email else "")
        + '[team.1]\nhostname = "dev.example.com"\n'
    )
    settings = Settings.from_toml(str(config))
    settings.validate()
    assert settings.routing_tls is enabled
    assert settings.uses_acme is (enabled and auto)
    assert settings.acme_enabled is resolver
    assert settings.public_scheme == "https"
    if enabled:
        settings = replace(settings, public_scheme_setting="http")
        with pytest.raises(ValueError, match="tls = false"):
            settings.validate()


@pytest.mark.parametrize("value", ['"false"', "0", "[]", '{ certResolver = "other" }'])
def test_invalid_tls_value_rejected(tmp_path, value):
    config = tmp_path / "oduflow.toml"
    config.write_text(f"[routing]\ntls = {value}\n")
    with pytest.raises(ValueError, match="must be true, false, or"):
        Settings.from_toml(str(config))
