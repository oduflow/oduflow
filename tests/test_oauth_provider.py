"""Unit tests for the self-hosted OAuth Authorization Server."""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest

from oduflow.oauth_provider import OduflowOAuthProvider
from oduflow.settings import Settings, TeamSettings


def _settings(**overrides):
    teams = overrides.pop(
        "teams",
        {
            "1": TeamSettings(
                team_id="1",
                hostname="team-a.example.com",
                auth_token="tok-a",
                port_range_start=50000,
                port_range_end=50100,
            ),
            "2": TeamSettings(
                team_id="2",
                hostname="team-b.example.com",
                auth_token="tok-b",
                port_range_start=50100,
                port_range_end=50200,
            ),
        },
    )
    return Settings(
        # Minted-token store lives under base_data_dir; give each provider its own
        # temp dir unless a test needs two providers to share one (persistence).
        base_data_dir=overrides.pop("base_data_dir", tempfile.mkdtemp()),
        teams=teams,
        **overrides,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mint(provider, client_id="team_1", scopes=None):
    """Drive a code exchange and return the issued OAuthToken (opaque pair)."""
    from mcp.server.auth.provider import AuthorizationCode
    from pydantic import AnyUrl

    client = _run(provider.get_client(client_id))
    assert client is not None
    code = AuthorizationCode(
        code="dummy",
        client_id=client_id,
        redirect_uri=AnyUrl("https://claude.ai/cb"),
        redirect_uri_provided_explicitly=True,
        scopes=scopes if scopes is not None else [],
        expires_at=9999999999,
        code_challenge="abc",
    )
    return _run(provider.exchange_authorization_code(client, code))


def _host_settings(*hostnames):
    """Settings with a team per OAuth issuer hostname."""
    return Settings(
        base_data_dir=tempfile.mkdtemp(),
        teams={
            str(i): TeamSettings(
                team_id=str(i),
                auth_token=f"tok-{i}",
                hostname=h,
                port_range_start=50000 + i * 100,
                port_range_end=50100 + i * 100,
            )
            for i, h in enumerate(hostnames, start=1)
        },
    )


class TestOduflowOAuthProvider:
    def test_team_hostname_is_used_as_internal_placeholder(self):
        s = Settings(
            routing_mode="traefik",
            acme_email="admin@example.com",
            base_data_dir=tempfile.mkdtemp(),
            teams={
                "1": TeamSettings(
                    team_id="1",
                    auth_token="tok",
                    hostname="team1.example.com",
                    port_range_start=50000,
                    port_range_end=50100,
                )
            },
        )
        provider = OduflowOAuthProvider(s)
        assert str(provider.base_url).rstrip("/") == "https://team1.example.com"

    def test_preregistered_clients(self):
        provider = OduflowOAuthProvider(_settings())
        # client_id is the public, non-secret team identifier; the secret is the
        # team's auth_token.
        client_a = _run(provider.get_client("team_1"))
        assert client_a is not None
        assert client_a.client_id == "team_1"
        assert client_a.client_secret == "tok-a"

        client_b = _run(provider.get_client("team_2"))
        assert client_b is not None
        assert client_b.client_id == "team_2"
        assert client_b.client_secret == "tok-b"

        # The secret must NOT double as a client_id — otherwise it would leak
        # into the /authorize URL.
        assert _run(provider.get_client("tok-a")) is None
        assert _run(provider.get_client("unknown")) is None

    def test_register_client_disabled(self):
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        new_client = OAuthClientInformationFull(
            client_id="new",
            client_secret="new",
            redirect_uris=[AnyUrl("https://example.com/cb")],
        )
        with pytest.raises(ValueError, match="Dynamic client registration"):
            _run(provider.register_client(new_client))

    def test_verify_token(self):
        provider = OduflowOAuthProvider(_settings())
        access = _run(provider.verify_token("tok-a"))
        assert access is not None
        assert access.token == "tok-a"
        assert access.client_id == "1"
        assert access.expires_at is None

        access_b = _run(provider.verify_token("tok-b"))
        assert access_b is not None
        assert access_b.client_id == "2"

        assert _run(provider.verify_token("nope")) is None

    def test_exchange_authorization_code_mints_opaque_token(self):
        provider = OduflowOAuthProvider(_settings())
        token = _mint(provider, scopes=["mcp"])
        # The issued tokens are independent, opaque, and NOT the auth_token or
        # the public client_id.
        assert token.access_token not in ("tok-a", "team_1")
        assert token.refresh_token not in ("tok-a", "team_1")
        assert token.access_token != token.refresh_token
        assert len(token.access_token) >= 32
        assert token.token_type == "Bearer"
        # The access token expires.
        assert token.expires_in == 3600
        # It resolves to the team (numeric team_id) so _resolve_team routes it
        # exactly like the auth_token, preserving requested scopes.
        access = _run(provider.load_access_token(token.access_token))
        assert access is not None
        assert access.client_id == "1"
        assert access.scopes == ["mcp"]
        assert access.expires_at is not None

    def test_bearer_invariant(self):
        provider = OduflowOAuthProvider(_settings())
        # The auth_token still resolves to the team id via the preseeded,
        # secret-keyed access token — the direct Bearer path is unchanged and
        # never expires.
        access = _run(provider.load_access_token("tok-a"))
        assert access is not None
        assert access.client_id == "1"
        assert access.expires_at is None
        # The public client_id is NOT a valid Bearer/access token on its own.
        assert _run(provider.load_access_token("team_1")) is None

    def test_refresh_token_rotation(self):
        provider = OduflowOAuthProvider(_settings())
        token = _mint(provider)
        client = _run(provider.get_client("team_1"))

        rt = _run(provider.load_refresh_token(client, token.refresh_token))
        assert rt is not None
        assert rt.client_id == "team_1"

        rotated = _run(provider.exchange_refresh_token(client, rt, []))
        # A brand-new pair is issued.
        assert rotated.access_token != token.access_token
        assert rotated.refresh_token != token.refresh_token
        # The old pair is dead after rotation.
        assert _run(provider.load_access_token(token.access_token)) is None
        assert _run(provider.load_refresh_token(client, token.refresh_token)) is None
        # The new access token still resolves to the team.
        new_access = _run(provider.load_access_token(rotated.access_token))
        assert new_access is not None
        assert new_access.client_id == "1"
        # A non-issued value is not a valid refresh token.
        assert _run(provider.load_refresh_token(client, "team_1")) is None

    def test_expired_access_token_keeps_refresh_token_across_restart(self, monkeypatch):
        from mcp.server.auth.provider import TokenError

        from oduflow import oauth_token_store as store_mod

        data_dir = tempfile.mkdtemp()
        provider = OduflowOAuthProvider(_settings(base_data_dir=data_dir))
        clock = {"t": 1000.0}
        monkeypatch.setattr(store_mod.time, "time", lambda: clock["t"])
        token = _mint(provider)
        assert _run(provider.load_access_token(token.access_token)) is not None

        # A restart after the TTL prunes only the expired access record. The
        # refresh record remains available to rotate into a new pair.
        clock["t"] = 1000.0 + 3600 + 1
        restarted = OduflowOAuthProvider(_settings(base_data_dir=data_dir))
        assert _run(restarted.load_access_token(token.access_token)) is None

        client = _run(restarted.get_client("team_1"))
        refresh = _run(restarted.load_refresh_token(client, token.refresh_token))
        assert refresh is not None
        rotated = _run(restarted.exchange_refresh_token(client, refresh, []))
        assert _run(restarted.load_access_token(rotated.access_token)) is not None

        # Rotation consumes the old refresh token; it cannot be replayed.
        assert _run(restarted.load_refresh_token(client, token.refresh_token)) is None
        with pytest.raises(TokenError, match="Unknown or already-used") as exc_info:
            _run(restarted.exchange_refresh_token(client, refresh, []))
        assert exc_info.value.error == "invalid_grant"

    def test_revoke_deletes_minted_token(self):
        provider = OduflowOAuthProvider(_settings())
        token = _mint(provider)
        access = _run(provider.load_access_token(token.access_token))
        assert access is not None

        _run(provider.revoke_token(access))
        # Both the access token and its paired refresh token are gone.
        assert _run(provider.load_access_token(token.access_token)) is None
        client = _run(provider.get_client("team_1"))
        assert _run(provider.load_refresh_token(client, token.refresh_token)) is None

        # Explicitly revoking the refresh side also removes its access partner.
        token = _mint(provider)
        refresh = _run(provider.load_refresh_token(client, token.refresh_token))
        assert refresh is not None
        _run(provider.revoke_token(refresh))
        assert _run(provider.load_refresh_token(client, token.refresh_token)) is None
        assert _run(provider.load_access_token(token.access_token)) is None

        # The preseeded auth_token is NOT revocable at runtime (config-bound).
        preseeded = _run(provider.load_access_token("tok-a"))
        assert preseeded is not None
        _run(provider.revoke_token(preseeded))
        assert _run(provider.load_access_token("tok-a")) is not None

    def test_minted_tokens_persist_across_restart(self):
        data_dir = tempfile.mkdtemp()
        provider = OduflowOAuthProvider(_settings(base_data_dir=data_dir))
        token = _mint(provider)

        # A fresh provider on the same data dir (simulated restart) still honors
        # the minted token — connections survive an upgrade/config reload.
        restarted = OduflowOAuthProvider(_settings(base_data_dir=data_dir))
        access = _run(restarted.load_access_token(token.access_token))
        assert access is not None
        assert access.client_id == "1"

        # A revoke persists too: a third instance sees it gone.
        _run(restarted.revoke_token(access))
        again = OduflowOAuthProvider(_settings(base_data_dir=data_dir))
        assert _run(again.load_access_token(token.access_token)) is None

    def test_flexible_redirect_uri(self):
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("team_1"))
        assert client is not None
        # Legitimate MCP callbacks are accepted: https (claude.ai) and loopback
        # http (IDEs).
        assert (
            str(client.validate_redirect_uri(AnyUrl("https://claude.ai/some/cb")))
            == "https://claude.ai/some/cb"
        )
        assert client.validate_redirect_uri(AnyUrl("http://127.0.0.1:8976/cb"))

    def test_redirect_uri_rejects_dangerous_and_cleartext(self):
        from mcp.shared.auth import InvalidRedirectUriError
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("team_1"))
        # Cleartext http to a non-loopback host would leak the auth code.
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl("http://evil.example/cb"))

    def test_env_token_is_bearer_only(self, monkeypatch):
        from oduflow import oauth_provider as op

        mapping = {"env-secret": ("2", "feature/x"), "tok-a": ("1", None)}
        monkeypatch.setattr(
            op.env_tokens,
            "resolve_token",
            lambda settings, token: mapping.get(token),
        )
        provider = OduflowOAuthProvider(_settings())

        # A per-env token must not be accepted as an OAuth client_id because it
        # would put the secret in the /authorize URL. Scoped endpoints are
        # Bearer-only.
        assert _run(provider.get_client("env-secret")) is None

        # Its access token carries the team_id and an env-binding scope.
        access = _run(provider.verify_token("env-secret"))
        assert access is not None
        assert access.client_id == "2"
        assert access.scopes == ["oduflow_env:feature/x"]

        # Team tokens still resolve via the preseeded path (no env scope).
        team_access = _run(provider.verify_token("tok-a"))
        assert team_access is not None
        assert team_access.client_id == "1"
        assert team_access.scopes == []

    def test_well_known_route_exposed(self):
        provider = OduflowOAuthProvider(_settings())
        routes = provider.get_routes("/mcp")
        paths = [getattr(r, "path", "") for r in routes]
        assert "/.well-known/oauth-authorization-server" in paths
        assert "/authorize" in paths
        assert "/token" in paths
        # Revocation is enabled so minted tokens can be invalidated at runtime.
        assert "/revoke" in paths

    def test_metadata_via_test_client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        provider = OduflowOAuthProvider(_settings())
        app = Starlette(routes=provider.get_routes("/mcp"))
        client = TestClient(app)
        resp = client.get(
            "/.well-known/oauth-authorization-server",
            headers={"host": "team-a.example.com", "x-forwarded-proto": "https"},
        )
        assert resp.status_code == 200
        meta = json.loads(resp.text)
        assert meta["issuer"].rstrip("/") == "https://team-a.example.com"
        assert "authorization_endpoint" in meta
        assert "token_endpoint" in meta
        # Revocation is advertised; DCR is disabled and must not be.
        assert "revocation_endpoint" in meta
        assert "registration_endpoint" not in meta

    def test_metadata_host_relative(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        # Issuer/endpoints are derived per-request from the team Host.
        provider = OduflowOAuthProvider(
            _host_settings("zipfit.oduflow.dev", "other.example.com")
        )
        app = Starlette(routes=provider.get_routes("/mcp"))
        client = TestClient(app)

        headers = {"host": "zipfit.oduflow.dev", "x-forwarded-proto": "https"}
        meta = json.loads(
            client.get("/.well-known/oauth-authorization-server", headers=headers).text
        )
        assert meta["issuer"].rstrip("/") == "https://zipfit.oduflow.dev"
        assert meta["authorization_endpoint"] == "https://zipfit.oduflow.dev/authorize"
        assert meta["token_endpoint"] == "https://zipfit.oduflow.dev/token"

        prm = json.loads(
            client.get(
                "/.well-known/oauth-protected-resource/mcp", headers=headers
            ).text
        )
        assert prm["resource"].rstrip("/") == "https://zipfit.oduflow.dev/mcp"
        assert (
            prm["authorization_servers"][0].rstrip("/") == "https://zipfit.oduflow.dev"
        )

        # A different Host yields a different issuer — OAuth is per-team-host.
        other = json.loads(
            client.get(
                "/.well-known/oauth-authorization-server",
                headers={"host": "other.example.com", "x-forwarded-proto": "https"},
            ).text
        )
        assert other["issuer"].rstrip("/") == "https://other.example.com"

    @pytest.mark.parametrize(
        ("host", "proto"),
        [
            ("zipfit.oduflow.dev:443@evil.example.com", "https"),
            ("zipfit.oduflow.dev:not-a-port", "https"),
            ("zipfit.oduflow.dev", "javascript"),
        ],
    )
    def test_metadata_rejects_malformed_external_origin(self, host, proto):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        provider = OduflowOAuthProvider(_host_settings("zipfit.oduflow.dev"))
        app = Starlette(routes=provider.get_routes("/mcp"))
        response = TestClient(app).get(
            "/.well-known/oauth-authorization-server",
            headers={"host": host, "x-forwarded-proto": proto},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    def test_auth_challenge_rewrites_resource_metadata_host(self):
        # The 401 WWW-Authenticate resource_metadata origin must follow the
        # request host, or fastmcp's static URL would point team B's client at
        # team A's hostname to discover OAuth.
        from oduflow.oauth_provider import HostRelativeAuthChallenge

        static = (
            'Bearer error="invalid_token", error_description="Authentication '
            'required", resource_metadata="https://team-a.example.com/'
            '.well-known/oauth-protected-resource/mcp"'
        )

        async def inner(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", static.encode())],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        sent: list = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "scheme": "http",  # Traefik terminates TLS; app sees http
            "headers": [
                (b"host", b"team-b.example.com"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
        app = HostRelativeAuthChallenge(inner, _host_settings("team-b.example.com"))
        _run(app(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        hdr = dict(start["headers"])[b"www-authenticate"].decode()
        assert (
            'resource_metadata="https://team-b.example.com/.well-known/'
            'oauth-protected-resource/mcp"' in hdr
        )
        # The error parts are preserved untouched.
        assert 'error="invalid_token"' in hdr

    def test_metadata_rejects_unknown_host(self):
        # A forged/absent host must not be reflected as the issuer: the discovery
        # endpoints reject it with 400 rather than advertise an attacker origin
        # or blow up on an invalid https:// URL.
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        provider = OduflowOAuthProvider(_host_settings("zipfit.oduflow.dev"))
        client = TestClient(Starlette(routes=provider.get_routes("/mcp")))

        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            r = client.get(
                path, headers={"host": "evil.com", "x-forwarded-proto": "https"}
            )
            assert r.status_code == 400, path

    def test_auth_challenge_skips_unknown_host(self):
        # A forged host must not be injected into the 401 challenge; the static
        # (placeholder team) resource_metadata is left untouched.
        from oduflow.oauth_provider import HostRelativeAuthChallenge

        static = (
            'Bearer error="invalid_token", resource_metadata='
            '"https://team-a.example.com/.well-known/oauth-protected-resource/mcp"'
        )

        async def inner(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", static.encode())],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        sent: list = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "scheme": "http",
            "headers": [(b"host", b"evil.com"), (b"x-forwarded-proto", b"https")],
        }
        app = HostRelativeAuthChallenge(inner, _host_settings("team-b.example.com"))
        _run(app(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        hdr = dict(start["headers"])[b"www-authenticate"].decode()
        assert "team-a.example.com" in hdr  # unchanged
        assert "evil.com" not in hdr


class TestAccessTokenType:
    """Every token this provider hands out must be FastMCP's ``AccessToken``.

    ``fastmcp.server.dependencies.get_access_token()`` raises on a plain MCP-SDK
    ``AccessToken`` (converting one passes the SDK's ``claims=None`` into a field
    typed ``dict``). ``oduflow.scoped_access`` calls it on every request to read
    a token's env scope, and a raise there fails closed — so an SDK-typed token
    silently turns into "no tools, every call denied" for OAuth-mode servers.
    """

    def _provider(self, monkeypatch):
        from oduflow import oauth_provider as op

        mapping = {"env-secret": ("2", "feature/x"), "tok-a": ("1", None)}
        monkeypatch.setattr(
            op.env_tokens,
            "resolve_token",
            lambda settings, token: mapping.get(token),
        )
        return OduflowOAuthProvider(_settings())

    def test_every_load_path_returns_a_fastmcp_token(self, monkeypatch):
        from fastmcp.server.auth.auth import AccessToken as FastMCPAccessToken

        provider = self._provider(monkeypatch)
        minted = _mint(provider, scopes=["mcp"]).access_token

        for label, value in (
            ("preseeded auth_token", "tok-a"),
            ("minted OAuth token", minted),
            ("per-env token", "env-secret"),
        ):
            access = _run(provider.load_access_token(value))
            assert access is not None, label
            assert isinstance(access, FastMCPAccessToken), label
            assert access.claims == {}, label

    def test_fastmcp_reads_the_env_scope_off_a_live_token(self, monkeypatch):
        # End-to-end proxy of the request path: park the token where the SDK's
        # auth middleware would, then let scoped_access resolve the env through
        # the real fastmcp dependency.
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        from oduflow import scoped_access

        provider = self._provider(monkeypatch)
        access = _run(provider.load_access_token("env-secret"))
        assert access is not None
        auth_context_var.set(AuthenticatedUser(access))
        # Inside a request scoped_access re-raises instead of degrading to None,
        # so a failure here is the deny-everything outage, not a quiet miss.
        active = scoped_access._HTTP_REQUEST_ACTIVE.set(True)
        try:
            assert scoped_access.env_from_access_token() == "feature/x"
        finally:
            scoped_access._HTTP_REQUEST_ACTIVE.reset(active)
            auth_context_var.set(None)


class TestRedirectUriValidation:
    """The redirect_uri is where the authorization code is delivered, so an
    over-permissive check hands the code to an attacker."""

    def _client(self):
        return _run(OduflowOAuthProvider(_settings()).get_client("team_1"))

    @pytest.mark.parametrize(
        "uri",
        [
            "javascript://claude.ai/%0aalert(1)",
            "data://text/html,<script>",
            "vbscript://x",
            "file://localhost/etc/passwd",
        ],
    )
    def test_dangerous_schemes_are_refused(self, uri):
        from mcp.shared.auth import InvalidRedirectUriError
        from pydantic import AnyUrl

        with pytest.raises(InvalidRedirectUriError, match="not allowed"):
            self._client().validate_redirect_uri(AnyUrl(uri))

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_cleartext_http_is_allowed_only_on_loopback(self, host):
        from pydantic import AnyUrl

        uri = f"http://{host}:8976/cb"
        assert str(self._client().validate_redirect_uri(AnyUrl(uri))) == uri

    @pytest.mark.parametrize(
        "host", ["evil.example", "claude.ai", "127.0.0.2", "localhost.evil.example"]
    )
    def test_cleartext_http_elsewhere_is_refused(self, host):
        from mcp.shared.auth import InvalidRedirectUriError
        from pydantic import AnyUrl

        with pytest.raises(InvalidRedirectUriError, match="loopback"):
            self._client().validate_redirect_uri(AnyUrl(f"http://{host}/cb"))

    def test_https_to_any_host_is_allowed(self):
        from pydantic import AnyUrl

        assert self._client().validate_redirect_uri(AnyUrl("https://evil.example/cb"))

    def test_a_custom_app_scheme_is_allowed(self):
        # Native IDE clients register their own scheme (cursor://, vscode://).
        from pydantic import AnyUrl

        assert self._client().validate_redirect_uri(AnyUrl("cursor://callback/oauth"))

    def test_omitting_the_uri_falls_back_to_the_first_registered_one(self):
        client = self._client()

        assert client.redirect_uris
        assert client.validate_redirect_uri(None) == client.redirect_uris[0]


class TestForwardedSchemeHost:
    """Behind Traefik the advertised OAuth URLs come from forwarded headers;
    getting them wrong makes discovery point at an unreachable origin."""

    def _call(self, headers, fallback=None):
        from oduflow.oauth_provider import _forwarded_scheme_host

        raw = [(k.encode(), v.encode()) for k, v in headers.items()]
        return _forwarded_scheme_host(raw, fallback)

    def test_the_forwarded_host_wins_over_the_host_header(self):
        assert self._call(
            {"host": "internal:8000", "x-forwarded-host": "erp.example.com"}
        ) == ("https", "erp.example.com")

    def test_the_host_header_is_used_when_nothing_is_forwarded(self):
        assert self._call({"host": "erp.example.com"}) == ("https", "erp.example.com")

    def test_the_forwarded_proto_is_reflected(self):
        assert self._call({"host": "h", "x-forwarded-proto": "http"})[0] == "http"

    def test_only_the_first_hop_is_taken(self):
        # A proxy chain appends; the client-facing hop is first.
        scheme, host = self._call(
            {
                "x-forwarded-host": "erp.example.com, internal",
                "x-forwarded-proto": "https, http",
            }
        )

        assert (scheme, host) == ("https", "erp.example.com")

    def test_the_fallback_scheme_applies_only_without_a_forwarded_proto(self):
        assert self._call({"host": "h"}, fallback="http")[0] == "http"
        assert self._call({"host": "h", "x-forwarded-proto": "https"}, "http")[0] == (
            "https"
        )

    def test_https_is_the_last_resort(self):
        # Never downgrade to http when nothing says otherwise.
        assert self._call({"host": "h"})[0] == "https"

    def test_header_names_are_matched_case_insensitively(self):
        assert self._call({"Host": "erp.example.com", "X-Forwarded-Proto": "HTTP"}) == (
            ("http", "erp.example.com")
        )

    def test_no_headers_at_all_yields_an_empty_host(self):
        assert self._call({}) == ("https", "")
