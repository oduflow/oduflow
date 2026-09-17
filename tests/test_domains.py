"""Team base-domain policy and global Host() namespace checks (domains.py)."""

import pytest

from oduflow import production_registry
from oduflow.domains import (
    NameIndex,
    assert_public_hostname_free,
    default_production_domain,
    env_hostname,
    find_owner,
    in_zone,
    service_hostname,
    service_parent_domain,
    validate_production_domain,
)
from oduflow.errors import ConflictError
from oduflow.settings import ExtraRoute, Settings, TeamSettings


@pytest.fixture
def team(tmp_path):
    return TeamSettings(
        team_id="1",
        hostname="oduflow.demo.example.com",
        base_domain="demo.example.com",
        data_dir=str(tmp_path / "team_1"),
    )


@pytest.fixture
def settings(team):
    return Settings(routing_mode="traefik", routing_tls=False, teams={"1": team})


def _register_production(team, name, domain, extra_domains=()):
    import os

    os.makedirs(team.data_dir, exist_ok=True)
    production_registry.create_production(
        team, name, {"domain": domain, "extra_domains": list(extra_domains)}
    )


class TestInZone:
    def test_apex_and_subdomain_match(self):
        assert in_zone("demo.example.com", "demo.example.com")
        assert in_zone("erp.demo.example.com", "demo.example.com")

    def test_lookalike_suffix_does_not_match(self):
        assert not in_zone("otherdemo.example.com", "demo.example.com")
        assert not in_zone("erp.example.com", "demo.example.com")

    def test_empty_zone_never_matches(self):
        assert not in_zone("erp.example.com", "")


class TestFindOwner:
    def test_free_name(self, settings):
        assert find_owner(settings, "feature.demo.example.com", own_team="1") == ""

    def test_dashboard_hostname_is_taken(self, settings):
        owner = find_owner(settings, "oduflow.demo.example.com", own_team="1")
        assert "dashboard hostname" in owner

    def test_other_team_zone_is_taken(self, settings, team, tmp_path):
        other = TeamSettings(
            team_id="2",
            hostname="oduflow.other.example.com",
            base_domain="other.example.com",
            data_dir=str(tmp_path / "team_2"),
        )
        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            teams={"1": team, "2": other},
        )

        assert "zone" in find_owner(settings, "erp.other.example.com", own_team="1")
        # The owning team itself may use names in its own zone.
        assert find_owner(settings, "erp.other.example.com", own_team="2") == ""

    def test_extra_route_is_taken(self, team):
        settings = Settings(
            routing_mode="traefik",
            routing_tls=False,
            teams={"1": team},
            extra_routes=(
                ExtraRoute(
                    name="grafana", host="grafana.demo.example.com", url="http://x"
                ),
            ),
        )

        owner = find_owner(settings, "grafana.demo.example.com", own_team="1")
        assert "static route" in owner

    def test_production_domains_are_taken(self, settings, team):
        _register_production(
            team, "erp", "demo.example.com", extra_domains=["myodoo.pl"]
        )

        assert "production 'erp'" in find_owner(
            settings, "demo.example.com", own_team="1"
        )
        assert "production 'erp'" in find_owner(settings, "myodoo.pl", own_team="1")

    def test_own_production_is_excluded(self, settings, team):
        _register_production(team, "erp", "demo.example.com")

        assert (
            find_owner(
                settings, "demo.example.com", own_team="1", exclude_production="erp"
            )
            == ""
        )

    def test_assert_raises_conflict(self, settings):
        with pytest.raises(ConflictError, match="dashboard hostname"):
            assert_public_hostname_free(
                settings, "oduflow.demo.example.com", own_team="1"
            )


class TestProductionDomainPolicy:
    def test_first_production_defaults_to_apex(self, settings, team):
        assert default_production_domain(settings, team, "erp") == "demo.example.com"

    def test_later_productions_default_to_subdomain(self, settings, team):
        _register_production(team, "erp", "demo.example.com")

        assert (
            default_production_domain(settings, team, "crm") == "crm.demo.example.com"
        )

    def test_no_default_without_base_domain(self, settings, tmp_path):
        plain = TeamSettings(
            team_id="1",
            hostname="dev.example.com",
            data_dir=str(tmp_path / "plain"),
        )

        assert default_production_domain(settings, plain, "erp") == ""

    def test_primary_domain_must_stay_in_zone(self, settings, team):
        with pytest.raises(ValueError, match="outside the team zone"):
            validate_production_domain(
                settings, team, "erp", "erp.customer.com", is_primary=True
            )

    def test_primary_apex_and_subdomain_are_accepted(self, settings, team):
        assert (
            validate_production_domain(
                settings, team, "erp", "demo.example.com", is_primary=True
            )
            == "demo.example.com"
        )
        assert (
            validate_production_domain(
                settings, team, "erp", "ERP.demo.example.com", is_primary=True
            )
            == "erp.demo.example.com"
        )

    def test_extra_domain_may_be_any_fqdn(self, settings, team):
        assert (
            validate_production_domain(
                settings, team, "erp", "myodoo.pl", is_primary=False
            )
            == "myodoo.pl"
        )

    def test_primary_anywhere_without_base_domain(self, settings, tmp_path):
        plain = TeamSettings(
            team_id="1",
            hostname="dev.example.com",
            data_dir=str(tmp_path / "plain"),
        )

        assert (
            validate_production_domain(
                settings, plain, "erp", "erp.customer.com", is_primary=True
            )
            == "erp.customer.com"
        )


def _live(fqdn, team_id="1", kind="environment", resource="erp"):
    """A NameIndex standing in for one container serving ``fqdn``."""
    return NameIndex(live={fqdn: (team_id, kind, resource)})


class TestLiveHostnamesAreClaimed:
    """A live environment or service owns its FQDN as firmly as a production.

    The claim exists only as a Traefik label, so before this the check was
    one-directional: an environment could not take a production's domain, but
    a production created afterwards could take the environment's.
    """

    def test_production_cannot_take_a_live_environment_name(self, settings):
        index = _live("erp.demo.example.com", resource="erp")

        owner = find_owner(settings, "erp.demo.example.com", own_team="1", index=index)
        assert owner == "environment 'erp' (team '1')"

    def test_service_name_is_claimed_too(self, settings):
        index = _live("fs.demo.example.com", kind="service", resource="fs")

        assert "service 'fs'" in find_owner(
            settings, "fs.demo.example.com", own_team="1", index=index
        )

    def test_environment_does_not_collide_with_itself(self, settings):
        """update_environment re-checks while the container still holds the rule."""
        index = _live("erp.demo.example.com", resource="erp")

        assert (
            find_owner(
                settings,
                "erp.demo.example.com",
                own_team="1",
                exclude_env="erp",
                index=index,
            )
            == ""
        )

    def test_exclusion_does_not_leak_across_kinds(self, settings):
        """A service named `erp` must not be excused by exclude_env='erp'."""
        index = _live("erp.demo.example.com", kind="service", resource="erp")

        assert "service 'erp'" in find_owner(
            settings,
            "erp.demo.example.com",
            own_team="1",
            exclude_env="erp",
            index=index,
        )

    def test_another_team_environment_still_blocks(self, settings):
        index = _live("erp.demo.example.com", team_id="2", resource="erp")

        assert "team '2'" in find_owner(
            settings,
            "erp.demo.example.com",
            own_team="1",
            exclude_env="erp",
            index=index,
        )


class TestPathRoutedServicesShareTheDashboardHost:
    """`routes` publish Host() && PathPrefix() routers and no catch-all, so
    sitting on the team hostname under a prefix is a supported layout."""

    def test_team_host_allowed_for_own_team(self, settings):
        assert (
            find_owner(
                settings,
                "oduflow.demo.example.com",
                own_team="1",
                allow_own_team_host=True,
            )
            == ""
        )

    def test_other_teams_host_still_blocked(self, team, tmp_path):
        other = TeamSettings(
            team_id="2",
            hostname="oduflow.other.example.com",
            data_dir=str(tmp_path / "team_2"),
        )
        settings = Settings(
            routing_mode="traefik", routing_tls=False, teams={"1": team, "2": other}
        )

        assert "dashboard hostname" in find_owner(
            settings,
            "oduflow.other.example.com",
            own_team="1",
            allow_own_team_host=True,
        )

    def test_catch_all_service_still_blocked(self, settings):
        with pytest.raises(ConflictError, match="dashboard hostname"):
            assert_public_hostname_free(
                settings, "oduflow.demo.example.com", own_team="1"
            )


class TestHostnameHelpers:
    """One definition of which domain a short name hangs off."""

    def test_service_short_name_uses_the_zone(self, team):
        assert service_hostname(team, "redis", None) == "redis.demo.example.com"
        assert service_hostname(team, "redis", "cache") == "cache.demo.example.com"

    def test_service_dotted_hostname_is_used_as_is(self, team):
        assert service_hostname(team, "redis", "x.customer.com") == "x.customer.com"

    def test_service_legacy_nests_under_team_hostname(self, tmp_path):
        plain = TeamSettings(
            team_id="1", hostname="dev.example.com", data_dir=str(tmp_path / "plain")
        )

        assert service_parent_domain(plain) == "dev.example.com"
        assert service_hostname(plain, "redis", None) == "redis.dev.example.com"

    def test_environment_short_name_uses_the_zone(self, team):
        assert env_hostname(team, "feature-a") == "feature-a.demo.example.com"

    def test_environment_legacy_layout_is_unchanged(self, tmp_path):
        plain = TeamSettings(
            team_id="1", hostname="dev.example.com", data_dir=str(tmp_path / "plain")
        )

        assert env_hostname(plain, "feature-a") == "feature-a.dev.example.com"
