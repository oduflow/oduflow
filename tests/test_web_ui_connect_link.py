"""The /oduflow-connect landing for links issued by the connect_link MCP tool.

The dashboard's Open button (tests/test_web_ui_connect_as.py) mints a session
up front and its first GET consumes the token. A connect_link token is
different: it travels through another application to a person, so it carries
only *who to sign in as*. A GET shows a page and changes nothing; the POST that
page makes mints the session. These tests pin that split and what a person sees
when the environment is busy, stopped or gone at that moment.
"""

import re
import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import connect_tokens
from oduflow.errors import (
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_ENV_HOST = "180.dev.example.com"
_SID = "t" * 80
_MINT = {
    "sid": _SID,
    "login": "boss@acme.com",
    "uid": "2",
    "base_url": f"https://{_ENV_HOST}",
    "cookie_domain": _ENV_HOST,
    "url": f"https://{_ENV_HOST}/web",
    "expires_at": "2026-07-20T00:00:00Z",
}


def _settings(tmp_path, **team):
    return Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", hostname="dev.example.com", **team)},
    )


def _client(settings, locks=None):
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks or LockManager())
    return TestClient(app)


def _issue(user="", login="boss@acme.com", team_id="1", host=_ENV_HOST):
    return connect_tokens.issue_link(
        host,
        connect_tokens.LinkGrant(
            team_id=team_id, env_name="18.0", user=user, login=login
        ),
    )


def _land(client, token, method="GET", host=_ENV_HOST):
    if method == "POST":
        return client.post(
            "/oduflow-connect",
            data={"token": token},
            headers={"host": host},
            follow_redirects=False,
        )
    return client.request(
        method,
        "/oduflow-connect",
        params={"token": token},
        headers={"host": host},
        follow_redirects=False,
    )


class _Redemption:
    """Patch everything a redemption touches and expose the mocks."""

    def __init__(self, running=True, mint=None):
        self._patchers = {
            "connect": patch(
                "oduflow.web_ui.odoo_ops.connect_as_user",
                **(mint or {"return_value": dict(_MINT)}),
            ),
            "ensure_running": patch(
                "oduflow.web_ui.env_ops.ensure_running", return_value=not running
            ),
            "wait_ready": patch(
                "oduflow.web_ui.env_ops.wait_for_odoo_ready", return_value=True
            ),
            "touch": patch("oduflow.web_ui.activity.touch"),
            "mark_started": patch("oduflow.web_ui.activity.mark_started"),
        }

    def __enter__(self):
        for name, patcher in self._patchers.items():
            setattr(self, name, patcher.start())
        return self

    def __exit__(self, *exc):
        for patcher in self._patchers.values():
            patcher.stop()


# -- A fetch is not a sign-in --


def test_get_shows_a_page_and_changes_nothing(tmp_path):
    """A link scanner or chat unfurler fetches the URL before the person does."""
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption() as r:
        for method in ("GET", "HEAD", "GET"):
            resp = _land(client, token, method)
            assert resp.status_code == 200
            assert "set-cookie" not in resp.headers
        # No session was minted; the environment was neither touched nor started.
        r.connect.assert_not_called()
        r.ensure_running.assert_not_called()
        r.touch.assert_not_called()

        # ...and the link is still good for the person it was meant for.
        assert _land(client, token, "POST").status_code == 303


def test_page_names_the_target_and_posts_the_token_back(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue(login="boss@acme.com")
    resp = _land(client, token)
    assert resp.headers["content-type"].startswith("text/html")
    assert _ENV_HOST in resp.text
    assert "boss@acme.com" in resp.text
    # The redemption is a POST carrying the token in the body, not in a URL.
    assert 'method="post" action="/oduflow-connect"' in resp.text
    assert f'name="token" value="{token}"' in resp.text
    # A person's browser continues by itself; an automated one that merely
    # renders the page (navigator.webdriver) has to press the button.
    assert "data-auto-ms=" in resp.text
    assert "navigator.webdriver" in resp.text
    assert "<button" in resp.text
    # The URL holds a bearer token: keep it out of caches and Referer headers.
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_page_is_self_contained(tmp_path):
    """On an env host only /oduflow-connect reaches Oduflow: any other URL the
    page referenced (/static/…, /logo.png) would be answered by Odoo."""
    page = _land(_client(_settings(tmp_path)), _issue()).text
    assert not re.findall(r'(?:src|href)="([^"]+)"', page)
    assert "url(" not in page
    # DESIGN.md: every custom property referenced is declared.
    declared = set(re.findall(r"(--[a-z-]+)\s*:", page))
    assert set(re.findall(r"var\((--[a-z-]+)", page)) <= declared


def test_page_escapes_the_login(tmp_path):
    resp = _land(
        _client(_settings(tmp_path)), _issue(login='<script>alert("x")</script>')
    )
    assert '<script>alert("x")</script>' not in resp.text
    assert "&lt;script&gt;" in resp.text


# -- Redemption --


def test_post_mints_for_the_granted_user_and_signs_in(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue(user="", login="boss@acme.com")
    with _Redemption() as r:
        resp = _land(client, token, "POST")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/web"
    set_cookie = resp.headers["set-cookie"]
    assert f"session_id={_SID}" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "domain=" not in set_cookie.lower()  # host-only, as for the dashboard
    # (settings, team, env, user). "" is the selector for base.user_admin: the
    # link carries the caller's selector, never a guessed 'admin' login.
    assert r.connect.call_args.args[1].team_id == "1"
    assert r.connect.call_args.args[2:] == ("18.0", "")
    r.touch.assert_called_once()
    # Already running: nothing to start, nothing to wait for.
    r.mark_started.assert_not_called()
    r.wait_ready.assert_not_called()


def test_link_is_single_use(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption() as r:
        assert _land(client, token, "POST").status_code == 303
        again = _land(client, token, "POST")
        assert again.status_code == 400
        assert _land(client, token).status_code == 400
        assert r.connect.call_count == 1
    assert "set-cookie" not in again.headers


def test_link_is_bound_to_the_environment_host(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption() as r:
        assert _land(client, token, host="evil.dev.example.com").status_code == 400
        wrong = _land(client, token, "POST", host="evil.dev.example.com")
        assert wrong.status_code == 400
        r.connect.assert_not_called()
        # A wrong-host attempt does not spend it for the right host.
        assert _land(client, token, "POST").status_code == 303


def test_expired_link_is_rejected(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    later = time.monotonic() + connect_tokens.LINK_TTL_SECONDS + 1
    with (
        _Redemption() as r,
        patch("oduflow.connect_tokens.time.monotonic", return_value=later),
    ):
        assert _land(client, token).status_code == 400
        assert _land(client, token, "POST").status_code == 400
        r.connect.assert_not_called()


def test_post_without_a_token_is_rejected(tmp_path):
    resp = _client(_settings(tmp_path)).post(
        "/oduflow-connect", headers={"host": _ENV_HOST}, follow_redirects=False
    )
    assert resp.status_code == 400


def test_oversized_post_body_is_not_buffered_or_honoured(tmp_path):
    """The landing is unauthenticated: it reads a token, not an upload."""
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption() as r:
        resp = client.post(
            "/oduflow-connect",
            content=f"pad={'x' * 20000}&token={token}",
            headers={
                "host": _ENV_HOST,
                "content-type": "application/x-www-form-urlencoded",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        r.connect.assert_not_called()
        assert _land(client, token, "POST").status_code == 303  # link unharmed


def test_link_of_a_removed_team_is_rejected(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue(team_id="9")
    with _Redemption() as r:
        assert _land(client, token, "POST").status_code == 400
        r.connect.assert_not_called()


# -- Busy and stopped environments: transient conditions do not cost the link --


def test_busy_environment_keeps_the_link_and_offers_a_retry(tmp_path):
    locks = LockManager()
    client = _client(_settings(tmp_path), locks)
    token = _issue()
    with _Redemption() as r:
        locks.acquire_env("18.0", "1", operation="update_environment")
        try:
            busy = _land(client, token, "POST")
        finally:
            locks.release_env("18.0")
        assert busy.status_code == 409
        assert "busy" in busy.text.lower()
        assert "set-cookie" not in busy.headers
        r.connect.assert_not_called()
        # The retry page re-posts the same token, by itself and on a button.
        assert f'name="token" value="{token}"' in busy.text
        assert "data-auto-ms=" in busy.text
        assert "Try again" in busy.text
        # Operation names and team ids are not for this audience.
        assert "update_environment" not in busy.text

        # Once the other operation is done, the very same link works.
        assert _land(client, token, "POST").status_code == 303


def test_redemption_holds_the_environment_lock(tmp_path):
    locks = LockManager()
    client = _client(_settings(tmp_path), locks)
    seen = []

    def _mint(*args):
        # A second operation on the environment is turned away meanwhile.
        try:
            locks.acquire_env("18.0", "1", operation="probe")
        except Exception as exc:
            seen.append(str(exc))
        else:
            locks.release_env("18.0")
        return dict(_MINT)

    with _Redemption(mint={"side_effect": _mint}):
        assert _land(client, _issue(), "POST").status_code == 303
    assert len(seen) == 1 and "connect_link" in seen[0]
    # ...and the lock is given back afterwards.
    locks.acquire_env("18.0", "1", operation="probe")
    locks.release_env("18.0")


def test_stopped_environment_is_started_and_waited_for(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    order = []

    def _wait(*args, **kwargs):
        # Once a session exists the link is spent — before this wait, so
        # nothing that interrupts it can leave a token able to mint another.
        order.append(("wait", connect_tokens.peek_link(token, _ENV_HOST)))
        return True

    with _Redemption(running=False) as r:
        r.ensure_running.side_effect = lambda *a: order.append("start") or True
        r.connect.side_effect = lambda *a: order.append("mint") or dict(_MINT)
        r.wait_ready.side_effect = _wait
        resp = _land(client, token, "POST")

    assert resp.status_code == 303
    assert "session_id=" in resp.headers["set-cookie"]
    # Started before the mint (odoo shell needs the container); the redirect
    # then waits for Odoo's HTTP server so the person does not land on a 502.
    assert order == ["start", "mint", ("wait", None)]
    assert r.ensure_running.call_args.args[1] == "18.0"
    r.mark_started.assert_called_once()


def test_slow_odoo_start_still_signs_in(tmp_path):
    with _Redemption(running=False) as r:
        r.wait_ready.return_value = False
        resp = _land(_client(_settings(tmp_path)), _issue(), "POST")
    assert resp.status_code == 303


def test_failed_start_keeps_the_link(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption() as r:
        r.ensure_running.side_effect = PrerequisiteNotMetError(
            "Cannot start '18.0': its filestore overlay could not be re-mounted"
        )
        resp = _land(client, token, "POST")
        assert resp.status_code == 400
        assert "could not be signed in" in resp.text
        assert "Try again" in resp.text
        r.connect.assert_not_called()
    with _Redemption():
        assert _land(client, token, "POST").status_code == 303


@pytest.mark.parametrize(
    ("error", "internal"),
    [
        (
            PrerequisiteNotMetError(
                "Cannot start '18.0': its filestore overlay could not be "
                "re-mounted (fuse-overlayfs: /srv/oduflow/1/overlay/18.0: "
                "Transport endpoint is not connected)."
            ),
            ["/srv/oduflow", "fuse-overlayfs", "filestore overlay"],
        ),
        (
            PrerequisiteNotMetError(
                "Interrupted template refresh needs recovery; inspect "
                "/srv/oduflow/1/workspaces/18.0/.refresh-journal before "
                "starting or updating this environment"
            ),
            ["/srv/oduflow", "refresh-journal"],
        ),
        (
            PrerequisiteNotMetError(
                "oduflow-db not found. System not initialized. Restart Oduflow."
            ),
            ["oduflow-db", "Restart Oduflow"],
        ),
    ],
)
def test_failure_page_keeps_operator_detail_for_the_log(
    error, internal, tmp_path, caplog
):
    """The page is read by whoever holds the link, with no dashboard login
    behind them: host paths, container names and exception text are for the
    operator's log, not for that page."""
    caplog.set_level("ERROR", logger="oduflow")
    with _Redemption() as r:
        r.ensure_running.side_effect = error
        resp = _land(_client(_settings(tmp_path)), _issue(), "POST")
    assert resp.status_code == 400
    for fragment in internal:
        assert fragment not in resp.text
    # The operator still gets all of it, with the environment named.
    assert str(error) in caplog.text
    assert "'18.0'" in caplog.text
    # ...and the person is told what they can do.
    assert "Try again" in resp.text
    assert "tell whoever gave you the link" in resp.text


def test_failed_mint_keeps_the_link_and_scrubs_the_detail(tmp_path, caplog):
    client = _client(_settings(tmp_path))
    token = _issue()
    internal_detail = "/mnt/extra-addons/private.py\nTraceback: registry failed"
    caplog.set_level("ERROR", logger="oduflow")
    failing = {
        "side_effect": ExternalCommandError(
            "odoo shell (connect_as_user)", 1, internal_detail
        )
    }
    with _Redemption(mint=failing):
        resp = _land(client, token, "POST")
    assert resp.status_code == 500
    assert "set-cookie" not in resp.headers
    # Scrubbed for the browser, kept for the operator.
    assert "private.py" not in resp.text
    assert internal_detail in caplog.text
    assert f'name="token" value="{token}"' in resp.text
    # Unlike "busy", a failure may not clear by itself: no automatic loop.
    assert "data-auto-ms=" not in resp.text

    with _Redemption():
        assert _land(client, token, "POST").status_code == 303


def test_unexpected_error_is_scrubbed_and_keeps_the_link(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue()
    with _Redemption(mint={"side_effect": RuntimeError("/srv/private/boom")}):
        resp = _land(client, token, "POST")
    assert resp.status_code == 500
    assert "/srv/private/boom" not in resp.text
    with _Redemption():
        assert _land(client, token, "POST").status_code == 303


def test_vanished_environment_or_user_retires_the_link(tmp_path):
    client = _client(_settings(tmp_path))
    token = _issue(user="jane@acme.com", login="jane@acme.com")
    gone = {"side_effect": NotFoundError("User 'jane@acme.com' not found")}
    with _Redemption(mint=gone):
        resp = _land(client, token, "POST")
    assert resp.status_code == 404
    assert "no longer exists" in resp.text
    assert "<form" not in resp.text  # nothing to retry
    with _Redemption() as r:
        assert _land(client, token, "POST").status_code == 400
        r.connect.assert_not_called()


# -- The two token kinds do not cross --


def test_dashboard_session_token_is_not_redeemable_by_post(tmp_path):
    client = _client(_settings(tmp_path))
    token = connect_tokens.issue(_ENV_HOST, _SID)
    assert _land(client, token, "POST").status_code == 400
    # Not spent by the stray POST: the dashboard's redirect still lands.
    resp = _land(client, token)
    assert resp.status_code == 303
    assert f"session_id={_SID}" in resp.headers["set-cookie"]


# -- Who reads the failure page, and who may reach the landing --


def test_invalid_link_page_speaks_to_someone_who_never_saw_the_dashboard(tmp_path):
    resp = _land(_client(_settings(tmp_path)), "nope")
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    text = re.search(r"<main.*?</main>", resp.text, re.S).group(0).lower()
    assert "no longer valid" in text
    # Says why and what to do next, without sending them to a dashboard they
    # may have no access to.
    assert "already been used" in text and "expired" in text
    assert "ask for a new one" in text
    assert "dashboard" not in text
    assert "<form" not in resp.text


def test_landing_is_public_but_token_gated(tmp_path):
    """Reached on an env host, where no dashboard session exists: the landing
    bypasses the UI login for the POST as much as for the GET."""
    client = _client(_settings(tmp_path, ui_password="s3cret-pw"))
    token = _issue()
    assert _land(client, token).status_code == 200
    with _Redemption():
        # The page that posts is same-origin, but nothing here depends on it:
        # there is no ambient cookie a cross-site form could ride on.
        resp = client.post(
            "/oduflow-connect",
            data={"token": token},
            headers={"host": _ENV_HOST, "origin": "https://app.example.net"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
