import pytest

from oduflow.naming import (
    get_db_name,
    get_env_hostname,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_service_container_name,
    get_service_database_name,
    get_service_database_role,
    get_template_db_name,
    get_workspace_path,
    parse_service_command,
    slugify_branch,
    split_team_hostname,
    validate_env_hostname,
    validate_service_database_name,
    validate_service_name,
    validate_template_name,
)


class TestEnvironmentHostname:
    def test_short_hostname_is_normalized(self):
        assert validate_env_hostname("  Env-12 ") == "env-12"

    @pytest.mark.parametrize("value", ["", "env.example.com", "-env", "env-", "env_1"])
    def test_invalid_short_hostname_is_rejected(self, value):
        with pytest.raises(ValueError, match="Invalid environment hostname"):
            validate_env_hostname(value)

    def test_numbered_hostname_replaces_team_prefix(self):
        assert (
            get_env_hostname("feature-a", "dev.example.com", "dev3")
            == "dev3.example.com"
        )

    def test_legacy_hostname_still_uses_branch_subdomain(self):
        assert (
            get_env_hostname("feature-a", "dev.example.com")
            == "feature-a.dev.example.com"
        )

    def test_base_domain_flattens_branch_hostname(self):
        assert (
            get_env_hostname(
                "feature-a", "oduflow.demo.example.com", "", "demo.example.com"
            )
            == "feature-a.demo.example.com"
        )

    def test_base_domain_applies_to_slot_hostname(self):
        assert (
            get_env_hostname(
                "feature-a", "oduflow.demo.example.com", "dev3", "demo.example.com"
            )
            == "dev3.demo.example.com"
        )

    def test_team_hostname_splits_first_label(self):
        assert split_team_hostname("odoo.dev.example.com") == (
            "odoo",
            "dev.example.com",
        )

    def test_team_hostname_requires_parent_domain(self):
        with pytest.raises(ValueError, match="host prefix and parent domain"):
            split_team_hostname("localhost")


class TestSlugifyBranch:
    def test_simple(self):
        assert slugify_branch("main") == "main"

    def test_slash(self):
        assert slugify_branch("feature/payments") == "feature-payments"

    def test_complex(self):
        assert slugify_branch("hotfix/CRM-123/fix") == "hotfix-crm-123-fix"

    def test_special_chars(self):
        assert slugify_branch("feat/hello@world!") == "feat-helloworld"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(slugify_branch(long_name)) == 63

    def test_uppercase(self):
        assert slugify_branch("Feature/BRANCH") == "feature-branch"

    def test_underscores_preserved(self):
        assert slugify_branch("fix_bug") == "fix_bug"

    def test_dots_removed(self):
        assert slugify_branch("release/1.2.3") == "release-123"

    def test_empty_string(self):
        assert slugify_branch("") == ""


class TestGetDbName:
    def test_main(self):
        assert get_db_name("main") == "oduflow_1_main"

    def test_feature(self):
        assert get_db_name("feature/payments") == "oduflow_1_feature-payments"

    def test_complex(self):
        assert get_db_name("hotfix/CRM-123/fix") == "oduflow_1_hotfix-crm-123-fix"

    def test_with_team_id(self):
        assert get_db_name("main", team_id="2") == "oduflow_2_main"

    def test_feature_with_team_id(self):
        assert (
            get_db_name("feature/payments", team_id="3") == "oduflow_3_feature-payments"
        )


class TestGetResourceName:
    def test_odoo(self):
        assert (
            get_resource_name("main", "odoo", "oduflow-", "1") == "oduflow-1-main-odoo"
        )

    def test_slash_branch(self):
        assert (
            get_resource_name("feature/payments", "odoo", "oduflow-", "1")
            == "oduflow-1-feature-payments-odoo"
        )

    def test_custom_prefix(self):
        assert get_resource_name("main", "odoo", "test-", "2") == "test-2-main-odoo"


class TestServiceNaming:
    @pytest.mark.parametrize(
        "good",
        ["redis", "Odoo-MCP-server", "service_1", "service.v2", "1-service"],
    )
    def test_accepts_docker_safe_names(self, good):
        assert validate_service_name(good) == good
        assert (
            get_service_container_name(good, "oduflow-", "1") == f"oduflow-1-svc-{good}"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "Odoo MCP server",
            "-service",
            "_service",
            ".service",
            "service/name",
            "service:name",
            "service@host",
            "service\n",
        ],
    )
    def test_rejects_names_docker_would_reject(self, bad):
        with pytest.raises(ValueError, match="Invalid service name"):
            validate_service_name(bad)
        with pytest.raises(ValueError, match="Invalid service name"):
            get_service_container_name(bad, "oduflow-", "1")

    def test_error_explains_how_to_replace_spaces(self):
        with pytest.raises(ValueError) as exc_info:
            get_service_container_name("Odoo MCP server", "oduflow-", "1")

        assert str(exc_info.value) == (
            "Invalid service name 'Odoo MCP server': must start with a letter "
            "or digit and contain only letters, digits, dots, hyphens, and "
            "underscores. Spaces are not allowed; use hyphens instead (for "
            "example, 'odoo-mcp-server')."
        )


class TestServiceDatabaseNaming:
    @pytest.mark.parametrize("good", ["worker", "worker-data", "events_2", "1db"])
    def test_accepts_stable_names(self, good):
        assert validate_service_database_name(good) == good
        assert get_service_database_name(good, "1") == f"oduflow_service_1_{good}"
        assert get_service_database_role(good, "1") == f"svc_1_{good}"

    @pytest.mark.parametrize(
        "bad", ["", "Worker", "worker.data", "worker/data", "-worker", "x" * 32]
    )
    def test_rejects_ambiguous_or_unsafe_names(self, bad):
        with pytest.raises(ValueError, match="Invalid service database name"):
            validate_service_database_name(bad)

    def test_long_team_id_gets_stable_bounded_identifiers(self):
        team_id = "customer-" + "x" * 70
        database = get_service_database_name("events", team_id)
        role = get_service_database_role("events", team_id)

        assert len(database) <= 63
        assert len(role) <= 63
        assert database == get_service_database_name("events", team_id)
        assert role == get_service_database_role("events", team_id)

    def test_underscore_in_team_id_cannot_collide_with_another_team(self):
        """``_`` separates the segments, so team ``a``/db ``b_c`` and team
        ``a_b``/db ``c`` would otherwise render to the same identifier."""
        assert get_service_database_name("b_c", "a") != get_service_database_name(
            "c", "a_b"
        )
        assert get_service_database_role("b_c", "a") != get_service_database_role(
            "c", "a_b"
        )

    def test_team_id_case_cannot_collide(self):
        """Identifiers are lower-cased, so ``A`` and ``a`` would otherwise
        render identically."""
        assert get_service_database_name("events", "A") != get_service_database_name(
            "events", "a"
        )

    def test_unambiguous_team_id_keeps_the_readable_form(self):
        assert get_service_database_name("events", "1") == "oduflow_service_1_events"
        assert get_service_database_name("events", "acme-eu") == (
            "oduflow_service_acme-eu_events"
        )

    def test_disambiguated_identifiers_stay_within_the_postgres_limit(self):
        for team_id in ("a_b", "A", "team ${x}", "customer_" + "y" * 70):
            for builder in (get_service_database_name, get_service_database_role):
                identifier = builder("events", team_id)
                assert len(identifier.encode("utf-8")) <= 63, identifier
                assert identifier == builder("events", team_id)


class TestGetWorkspacePath:
    def test_simple(self):
        assert get_workspace_path("main", "/tmp/ws") == "/tmp/ws/main"

    def test_slash(self):
        assert (
            get_workspace_path("feature/payments", "/tmp/ws")
            == "/tmp/ws/feature-payments"
        )


class TestGetRepoPath:
    def test_simple(self):
        assert get_repo_path("main", "/tmp/ws") == "/tmp/ws/main/repo"

    def test_slash(self):
        assert (
            get_repo_path("feature/payments", "/tmp/ws")
            == "/tmp/ws/feature-payments/repo"
        )


class TestGetFilestorePaths:
    def test_keys(self):
        paths = get_filestore_paths("main", "/tmp/ws")
        assert set(paths.keys()) == {"upper", "work", "merged"}

    def test_paths(self):
        paths = get_filestore_paths("main", "/tmp/ws")
        assert paths["upper"] == "/tmp/ws/main/filestore_upper"
        assert paths["work"] == "/tmp/ws/main/filestore_work"
        assert paths["merged"] == "/tmp/ws/main/filestore"

    def test_slash_branch(self):
        paths = get_filestore_paths("feature/payments", "/tmp/ws")
        assert paths["merged"] == "/tmp/ws/feature-payments/filestore"


class TestGetTemplateDbName:
    def test_named(self):
        assert get_template_db_name("prod") == "oduflow_template_1_prod"

    def test_custom_name(self):
        assert (
            get_template_db_name("myproject-v17") == "oduflow_template_1_myproject-v17"
        )

    def test_slash(self):
        assert get_template_db_name("client/prod") == "oduflow_template_1_client-prod"

    def test_with_team_id(self):
        assert get_template_db_name("prod", team_id="2") == "oduflow_template_2_prod"

    def test_named_with_team_id(self):
        assert (
            get_template_db_name("myproject-v17", team_id="3")
            == "oduflow_template_3_myproject-v17"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "../etc",
            "client/../prod",
            "/abs",
            "client/",  # trailing slash -> empty segment
            "..",
            ".",
            ".hidden",
            "a b",  # whitespace
            "a;b",
            "a'b",
            'a"b',
            "a`b",
            "x'; DROP DATABASE oduflow_1_main; --",
            "",
            "a" * 64,  # too long
        ],
    )
    def test_rejects_dangerous_names(self, bad):
        # Path-traversal / SQL-identifier break-out must be refused (issue #38).
        with pytest.raises(ValueError, match="Invalid template name"):
            get_template_db_name(bad)
        with pytest.raises(ValueError, match="Invalid template name"):
            validate_template_name(bad)

    @pytest.mark.parametrize(
        "good", ["prod", "client/prod", "myproject-v17", "v17.0", "a", "A_b-c.d"]
    )
    def test_accepts_valid_names(self, good):
        assert validate_template_name(good) == good


class TestProdNaming:
    @pytest.mark.parametrize("good", ["erp", "erp-main", "a", "x1", "client2-erp"])
    def test_accepts_valid_prod_names(self, good):
        from oduflow.naming import prod_env_name, validate_prod_name

        assert validate_prod_name(good) == good
        assert prod_env_name(good) == f"prod-{good}"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "ERP",  # uppercase
            "-erp",  # leading dash
            # A prod name is also a DNS label: "erp-" would build the invalid
            # default domain "erp-.<base_domain>" and fail validate_domain for
            # a domain the user never typed.
            "erp-",  # trailing dash
            "erp/main",  # slash
            "erp_main",  # underscore
            "a b",
            "a" * 32,  # too long
            "erp;drop",
            "../etc",
        ],
    )
    def test_rejects_bad_prod_names(self, bad):
        from oduflow.naming import validate_prod_name

        with pytest.raises(ValueError, match="Invalid production name"):
            validate_prod_name(bad)

    def test_prod_env_name_feeds_existing_helpers(self):
        # The derived internal name flows through the shared naming chain
        # unchanged: dashes survive slugification, so DB/container names stay
        # aligned with the prod-{name} namespace.
        from oduflow.naming import prod_env_name

        env = prod_env_name("erp")
        assert get_db_name(env, "2") == "oduflow_2_prod-erp"
        assert get_resource_name(env, "odoo", "oduflow-", "2") == (
            "oduflow-2-prod-erp-odoo"
        )
        assert get_workspace_path(env, "/data/ws") == "/data/ws/prod-erp"


class TestValidateDomain:
    def test_normalizes_case_whitespace_and_trailing_dot(self):
        from oduflow.naming import validate_domain

        assert validate_domain("  ERP.Example.COM.  ") == "erp.example.com"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            ".",
            "https://erp.example.com",
            "erp.example.com:8069",
            "erp.example.com/path",
            "*.example.com",
            "erp example.com",
        ],
    )
    def test_rejects_non_hostnames(self, bad):
        from oduflow.naming import validate_domain

        with pytest.raises(ValueError, match="Invalid domain"):
            validate_domain(bad)

    def test_length_boundary_is_253(self):
        # A 253-char FQDN is the DNS maximum and must be accepted; 254 must not.
        from oduflow.naming import validate_domain

        # 63 + 63 + 63 + 61 chars + 3 dots = 253 (63 is the per-label maximum).
        longest = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])
        assert len(longest) == 253
        assert validate_domain(longest) == longest

        too_long = longest + "a"
        assert len(too_long) == 254
        with pytest.raises(ValueError, match="Invalid domain"):
            validate_domain(too_long)

    def test_trailing_dot_is_stripped_before_the_length_check(self):
        # The root dot is not part of the name, so a 253-char name written as an
        # absolute FQDN (254 chars incl. the dot) is still valid.
        from oduflow.naming import validate_domain

        absolute = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61]) + "."
        assert len(absolute) == 254
        assert validate_domain(absolute) == absolute.rstrip(".")


class TestSanitizeRepoUrl:
    def test_strips_userinfo_but_keeps_host_and_port(self):
        from oduflow.naming import sanitize_repo_url

        assert (
            sanitize_repo_url("https://user:pat@git.example.com:8443/acme/repo.git")
            == "https://git.example.com:8443/acme/repo.git"
        )

    def test_at_sign_inside_the_password_is_still_fully_stripped(self):
        # A PAT or password may itself contain '@'; only the LAST '@' separates
        # userinfo from the host, so everything before it must go.
        from oduflow.naming import sanitize_repo_url

        assert (
            sanitize_repo_url("https://user:p@ss@git.example.com/acme/repo.git")
            == "https://git.example.com/acme/repo.git"
        )

    def test_url_without_credentials_is_untouched(self):
        from oduflow.naming import sanitize_repo_url

        url = "https://git.example.com/acme/repo.git"
        assert sanitize_repo_url(url) == url

    def test_empty_input_passes_through(self):
        from oduflow.naming import sanitize_repo_url

        assert sanitize_repo_url("") == ""


class TestRedactUrlCredentials:
    """Guards the scrubber applied to git stderr before it reaches an agent."""

    def test_redacts_credentials_embedded_in_free_text(self):
        from oduflow.naming import redact_url_credentials

        text = (
            "fatal: could not read from "
            "https://user:ghp_secret@github.com/acme/repo.git\n"
        )
        redacted = redact_url_credentials(text)

        assert "ghp_secret" not in redacted
        assert "user" not in redacted
        assert "https://***@github.com/acme/repo.git" in redacted

    def test_redacts_every_occurrence(self):
        from oduflow.naming import redact_url_credentials

        text = "a https://u:p1@host/x b ssh://u:p2@host/y"
        redacted = redact_url_credentials(text)

        assert "p1" not in redacted and "p2" not in redacted
        assert redacted == "a https://***@host/x b ssh://***@host/y"

    def test_leaves_credential_free_urls_alone(self):
        from oduflow.naming import redact_url_credentials

        text = "cloning https://github.com/acme/repo.git"
        assert redact_url_credentials(text) == text

    def test_empty_input_passes_through(self):
        from oduflow.naming import redact_url_credentials

        assert redact_url_credentials("") == ""


class TestParseServiceCommand:
    def test_splits_with_shell_quoting(self):
        assert parse_service_command('server /data --address ":9001"') == [
            "server",
            "/data",
            "--address",
            ":9001",
        ]

    def test_empty_means_no_override(self):
        assert parse_service_command("") == []
        assert parse_service_command("   ") == []

    def test_unbalanced_quote_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid command"):
            parse_service_command("server '/data")
