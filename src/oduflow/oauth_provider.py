"""Self-hosted OAuth 2.1 Authorization Server for Oduflow.

Each team is preregistered as an OAuth client whose ``client_id`` is a public,
non-secret identifier (``team_<id>``, e.g. ``team_1``) and whose
``client_secret`` is the team's ``auth_token``. Keeping the secret out of the
``client_id`` matters because ``client_id`` travels in the ``/authorize`` query
string (logs, browser history, Referer); the secret is sent only in the POST
``/token`` body. A successful Authorization Code + PKCE flow mints an
*independent*, opaque, expiring ``access_token`` (with a rotating
``refresh_token``) — the OAuth client never receives the team ``auth_token``.
Minted tokens are persisted (see :mod:`oduflow.oauth_token_store`) so they
survive a restart, expire, and can be revoked. The team ``auth_token`` itself
stays valid as a preseeded, non-expiring direct Bearer credential (curl/CLI),
and a minted token carries the numeric ``team_id`` as its ``client_id`` so
``_resolve_team()`` routes it exactly like the ``auth_token``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

# FastMCP's AccessToken (a subclass of the MCP SDK's that adds ``claims``) —
# never the SDK's own. ``fastmcp.server.dependencies.get_access_token()``, which
# scoped_access uses to read a token's env scope, rejects a plain SDK token: it
# tries to convert it and passes ``claims=None`` (the SDK's default) into a
# field typed ``dict``, so validation fails and the request is denied outright.
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.provider import (
    AccessToken as SDKAccessToken,
)
from mcp.server.auth.provider import (
    AuthorizationCode,
    RefreshToken,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from oduflow import env_tokens
from oduflow.oauth_token_store import OAuthTokenStore
from oduflow.scoped_access import ENV_SCOPE_PREFIX
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


# Schemes that are never a legitimate OAuth callback and could turn the
# authorization redirect into an XSS / data-exfil primitive.
_DANGEROUS_REDIRECT_SCHEMES = {"javascript", "data", "vbscript", "file"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')

# Minted OAuth access tokens expire after this many seconds; the client obtains
# a fresh pair via its refresh token. Intentionally not a config knob — a
# sensible default (matches the SDK's DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS).
_ACCESS_TOKEN_TTL_SECONDS = 3600
_TOKEN_STORE_FILENAME = "oauth_tokens.json"


def public_client_id(team_id: str) -> str:
    """The non-secret OAuth ``client_id`` for a team, safe to appear in the
    ``/authorize`` URL. The team's ``auth_token`` is the ``client_secret``."""
    return f"team_{team_id}"


def _as_fastmcp_token(token: SDKAccessToken) -> AccessToken:
    """Return ``token`` as a FastMCP ``AccessToken``.

    Every token this provider hands out must be the FastMCP subclass:
    ``fastmcp.server.dependencies.get_access_token()`` (used by
    :mod:`oduflow.scoped_access` to read a token's env scope) raises on a plain
    SDK token, because converting one passes the SDK's ``claims=None`` into a
    field typed ``dict``. A raise there denies the whole request, so a token
    that arrives from the base class's SDK-typed store is normalised here rather
    than trusted to be the right type.
    """
    if isinstance(token, AccessToken):
        return token
    data = token.model_dump()
    data["claims"] = data.get("claims") or {}
    return AccessToken(**data)


def _forwarded_scheme_host(
    raw_headers: list[tuple[bytes, bytes]], fallback_scheme: str | None = None
) -> tuple[str, str]:
    """The ``(scheme, host)`` the client reached us on, from ASGI headers.

    Behind Traefik the app is reached over http with the team's public host in
    the Host header and ``X-Forwarded-Proto: https``; reflect the forwarded
    proto/host (first hop) so advertised OAuth URLs are reachable.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
    fwd_host = headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = fwd_host or headers.get("host", "")
    fwd_proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = fwd_proto or fallback_scheme or "https"
    return scheme, host


def _unknown_host_response() -> JSONResponse:
    """400 for a discovery request whose host is not a registered team — never
    advertise an issuer derived from a forged or absent Host header."""
    return JSONResponse(
        {"error": "invalid_request", "error_description": "Unrecognized host."},
        status_code=400,
    )


def _rewrite_resource_metadata_origin(header_value: bytes, origin: str) -> bytes:
    """Rewrite the scheme+host of a ``WWW-Authenticate`` ``resource_metadata``
    URL to ``origin``, preserving its path."""
    text = header_value.decode("latin-1")
    m = _RESOURCE_METADATA_RE.search(text)
    if not m:
        return header_value
    old = m.group(1)
    new = f"{origin}{urlparse(old).path}"
    return text.replace(
        f'resource_metadata="{old}"', f'resource_metadata="{new}"'
    ).encode("latin-1")


class HostRelativeAuthChallenge:
    """ASGI wrapper that makes the 401 auth challenge host-relative.

    In host-relative OAuth mode the issuer is per-team-host, but fastmcp bakes a
    single static ``resource_metadata`` URL — built from the placeholder
    base_url — into RequireAuthMiddleware's ``WWW-Authenticate`` challenge. An
    unauthenticated MCP client (e.g. Claude.ai) begins OAuth discovery from that
    header, so without this rewrite team B's client would start OAuth on team A's
    hostname. We rewrite the URL's scheme+host to the host the request actually
    arrived on, keeping the path; the discovery it points to is already
    host-relative (see OduflowOAuthProvider.get_routes). The rewrite only fires
    for a host that maps to a registered team, so a forged X-Forwarded-Host can't
    inject an attacker origin into the challenge.
    """

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        scheme, host = _forwarded_scheme_host(
            scope.get("headers", []), scope.get("scheme")
        )
        # Only rewrite for a host that belongs to a registered team; a forged or
        # unexpected host is left with fastmcp's own (placeholder team) URL.
        if (
            scheme not in {"http", "https"}
            or self._settings.get_team_by_hostname(host) is None
        ):
            await self.app(scope, receive, send)
            return
        origin = f"{scheme}://{host}"

        async def send_wrapper(message: Any) -> None:
            if message.get("type") == "http.response.start" and message.get("headers"):
                message = {
                    **message,
                    "headers": [
                        (k, _rewrite_resource_metadata_origin(v, origin))
                        if k.lower() == b"www-authenticate"
                        else (k, v)
                        for k, v in message["headers"]
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _FlexibleClient(OAuthClientInformationFull):
    """Client info that accepts the varied callback URLs MCP clients use.

    Preregistered team clients have ``client_id = team_<id>`` (non-secret) and
    ``client_secret = auth_token``. The secret-bearing /token exchange proves
    identity, so we let MCP clients (claude.ai over https, IDEs over loopback or
    a custom scheme) bring their own callback — but we still reject callbacks that
    are never legitimate: dangerous schemes (javascript:, data:, …) and cleartext
    http:// to a non-loopback host (which would leak the authorization code in the
    clear).
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            if self.redirect_uris:
                return self.redirect_uris[0]
            raise InvalidRedirectUriError("redirect_uri must be specified")

        scheme = (redirect_uri.scheme or "").lower()
        if not scheme or scheme in _DANGEROUS_REDIRECT_SCHEMES:
            raise InvalidRedirectUriError(
                f"redirect_uri scheme '{scheme}' is not allowed."
            )
        if scheme == "http" and (redirect_uri.host or "") not in _LOOPBACK_HOSTS:
            raise InvalidRedirectUriError(
                "Plaintext http:// redirect_uri is only allowed for loopback "
                "addresses; use https for remote callbacks."
            )
        return redirect_uri


class OduflowOAuthProvider(InMemoryOAuthProvider):
    """OAuth Authorization Server backed by ``team.auth_token`` values."""

    def __init__(self, settings: Settings) -> None:
        # The SDK requires one static base URL even though Oduflow's issuer is
        # always the hostname of the team reached by the request. Use a real team
        # hostname only as an internal placeholder: get_routes() rebuilds
        # discovery per request, and HostRelativeAuthChallenge rewrites the 401
        # challenge to the validated request origin.
        placeholder_host = next(
            (t.hostname for t in settings.teams.values() if t.hostname),
            "localhost",
        )
        base_url = f"https://{placeholder_host}"
        # Enable the /revoke endpoint so a client can really invalidate a minted
        # token (revoke_token deletes it from the persistent store).
        super().__init__(
            base_url=base_url,
            revocation_options=RevocationOptions(enabled=True),
        )

        self._settings = settings
        self._token_to_team: dict[str, str] = {}
        # Persistent store of minted (opaque, expiring) access/refresh tokens.
        self._token_store = OAuthTokenStore(
            os.path.join(settings.base_data_dir, _TOKEN_STORE_FILENAME),
            access_ttl=_ACCESS_TOKEN_TTL_SECONDS,
        )
        placeholder_redirect = AnyUrl("https://claude.ai/")

        for team in settings.teams.values():
            token = team.auth_token
            if not token:
                continue
            # Public, non-secret client_id (e.g. "team_1"): it travels in the
            # /authorize query string, so it must NOT be the secret. The secret
            # lives only in client_secret (POST /token body) and is what the
            # issued access_token equals.
            client_id = public_client_id(team.team_id)
            client = _FlexibleClient(
                client_id=client_id,
                client_secret=token,
                redirect_uris=[placeholder_redirect],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                scope=None,
            )
            self.clients[client_id] = client
            self._token_to_team[client_id] = team.team_id
            # Preseed the access token — keyed by the SECRET auth_token, not the
            # public client_id — so direct Bearer-token calls also work (curl/CLI
            # clients that skip the OAuth dance). The public client_id is not a
            # valid Bearer token on its own.
            self.access_tokens[token] = AccessToken(
                token=token,
                client_id=team.team_id,
                scopes=[],
                expires_at=None,
            )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """OAuth routes with discovery metadata built from the request origin.

        The SDK would otherwise bake the internal placeholder issuer into static
        ``/.well-known/...`` documents. Each team's OAuth flow instead runs on
        its own hostname behind Traefik or another TLS-terminating upstream such
        as Cloudflare Tunnel. ``/authorize`` and ``/token`` are path-only and
        need no change.
        """
        routes = super().get_routes(mcp_path)
        patched: list[Route] = []
        for route in routes:
            if not isinstance(route, Route):
                patched.append(route)
            elif route.path.startswith("/.well-known/oauth-authorization-server"):
                patched.append(
                    Route(
                        route.path,
                        endpoint=cors_middleware(
                            self._authorization_server_metadata, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            elif route.path.startswith("/.well-known/oauth-protected-resource"):
                patched.append(
                    Route(
                        route.path,
                        endpoint=cors_middleware(
                            self._protected_resource_metadata, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            else:
                patched.append(route)
        return patched

    def _validated_origin(self, request: Request) -> str | None:
        """External origin (scheme://host) the client reached us on, but only for
        a host that belongs to a registered team; otherwise ``None`` so the caller
        rejects the request. This keeps a forged ``X-Forwarded-Host`` from
        advertising an attacker-controlled issuer, and turns a missing Host into a
        clean 400 rather than an invalid ``https://`` URL."""
        scheme, host = _forwarded_scheme_host(
            request.scope["headers"], request.scope.get("scheme")
        )
        if (
            scheme not in {"http", "https"}
            or self._settings.get_team_by_hostname(host) is None
        ):
            return None
        return f"{scheme}://{host}"

    async def _authorization_server_metadata(self, request: Request) -> Response:
        origin = self._validated_origin(request)
        if origin is None:
            return _unknown_host_response()
        issuer = AnyHttpUrl(origin)
        # Default the options exactly as the SDK's create_auth_routes does —
        # build_metadata dereferences them, and both are None on this provider.
        metadata = build_metadata(
            issuer,
            self.service_documentation_url,
            self.client_registration_options or ClientRegistrationOptions(),
            self.revocation_options or RevocationOptions(),
        )
        # No-store: the document is host-specific, so a shared cache must not
        # replay one team's issuer for another host.
        return PydanticJSONResponse(
            content=metadata, headers={"Cache-Control": "no-store"}
        )

    async def _protected_resource_metadata(self, request: Request) -> Response:
        origin = self._validated_origin(request)
        if origin is None:
            return _unknown_host_response()
        resource_path = (
            urlparse(str(self._resource_url)).path if self._resource_url else "/mcp"
        )
        supported_scopes = (
            self.client_registration_options.valid_scopes
            if (
                self.client_registration_options
                and self.client_registration_options.valid_scopes
            )
            else self.required_scopes
        )
        metadata = ProtectedResourceMetadata(
            resource=AnyHttpUrl(f"{origin}{resource_path}"),
            authorization_servers=[AnyHttpUrl(origin)],
            scopes_supported=supported_scopes,
        )
        return PydanticJSONResponse(
            content=metadata, headers={"Cache-Control": "no-store"}
        )

    async def _env_identity(self, token: str | None) -> tuple[str, str] | None:
        """Return ``(team_id, env_name)`` for a valid per-environment token.

        Team tokens are handled by the preseeded clients/access tokens, so they
        return ``None`` here.
        """
        if not token:
            return None
        resolved = await env_tokens.resolve_token_async(self._settings, token)
        if resolved is None:
            return None
        team_id, env_name = resolved
        if env_name is None:
            return None
        return (team_id, env_name)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await super().get_client(client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # 1. Preseeded team auth_token (direct Bearer, non-expiring). The base
        # class types its store with the SDK's AccessToken, so normalise what
        # comes back — see _as_fastmcp_token.
        existing = await super().load_access_token(token)
        if existing is not None:
            return _as_fastmcp_token(existing)
        # 2. A minted OAuth access token from the persistent store. Its stored
        # client_id is the numeric team_id, so it routes like the auth_token.
        # get_access() drops the token and returns None once expired.
        record = self._token_store.get_access(token)
        if record is not None:
            return AccessToken(
                token=token,
                client_id=str(record.get("client_id", "")),
                scopes=list(record.get("scopes", [])),
                expires_at=record.get("expires_at"),
            )
        # 3. A per-environment token (scoped Bearer).
        identity = await self._env_identity(token)
        if identity is None:
            return None
        team_id, env_name = identity
        return AccessToken(
            token=token,
            client_id=team_id,
            scopes=[f"{ENV_SCOPE_PREFIX}{env_name}"],
            expires_at=None,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise ValueError(
            "Dynamic client registration is disabled. Use the preregistered "
            "client: client_id = team_<id> (e.g. team_1), client_secret = that "
            "team's auth_token from oduflow.toml."
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Single-use code.
        self.auth_codes.pop(authorization_code.code, None)

        cid = client.client_id
        if cid is None or cid not in self._token_to_team:
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_client", "Unknown client.")

        # Mint an independent, opaque, expiring access token (+ refresh token)
        # rather than handing back the team auth_token. The minted token's stored
        # client_id is the numeric team_id, so a full-access (empty-scope) token
        # routes exactly like the auth_token in _resolve_team().
        team_id = self._token_to_team[cid]
        scopes = list(authorization_code.scopes or [])
        access, refresh, _expires_at = self._token_store.mint_pair(
            client_id=team_id, scopes=scopes
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        cid = client.client_id
        if cid is None or cid not in self._token_to_team:
            return None
        record = self._token_store.get_refresh(refresh_token)
        # The refresh token must belong to this client's team.
        if record is None or record.get("client_id") != self._token_to_team[cid]:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=cid,
            scopes=list(record.get("scopes", [])),
            expires_at=None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the old access+refresh pair is invalidated and a new pair is
        # minted with the refresh token's original team/scopes.
        rotated = self._token_store.rotate(refresh_token.token)
        if rotated is None:
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_grant", "Unknown or already-used refresh token.")
        access, refresh, _expires_at, _client_id, minted_scopes = rotated
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh,
            scope=" ".join(minted_scopes) if minted_scopes else None,
        )

    async def revoke_token(self, token: SDKAccessToken | RefreshToken) -> None:
        # Really delete the minted token (and its paired token) from the store.
        # The preseeded auth_token and per-env tokens are not in the store, so
        # revoking them is a no-op (rotate auth_token in oduflow.toml instead).
        self._token_store.revoke(token.token)
