"""One-time token store for the cross-subdomain Connect As landing."""

from oduflow import connect_tokens


def test_issue_then_consume_returns_sid_once():
    token = connect_tokens.issue("180.dev.example.com", "sid-abc")
    assert connect_tokens.consume(token, "180.dev.example.com") == "sid-abc"
    # One-time: a second consume of the same token fails.
    assert connect_tokens.consume(token, "180.dev.example.com") is None


def test_unknown_token_returns_none():
    assert connect_tokens.consume("does-not-exist", "180.dev.example.com") is None


def test_host_mismatch_rejected():
    token = connect_tokens.issue("180.dev.example.com", "sid")
    assert connect_tokens.consume(token, "181.dev.example.com") is None


def test_host_match_is_case_insensitive():
    token = connect_tokens.issue("180.DEV.Example.com", "sid")
    assert connect_tokens.consume(token, "180.dev.example.COM") == "sid"


def test_valid_within_ttl():
    token = connect_tokens.issue("h", "sid", now=1000.0)
    assert connect_tokens.consume(token, "h", now=1000.0 + 119) == "sid"


def test_expired_token_rejected():
    token = connect_tokens.issue("h", "sid", now=1000.0)
    assert connect_tokens.consume(token, "h", now=1000.0 + 121) is None


# -- Deferred grants: the connect_link MCP tool --

_GRANT = connect_tokens.LinkGrant(
    team_id="1", env_name="18.0", user="", login="boss@acme.com"
)


def test_peek_link_returns_the_grant_without_spending_it():
    token = connect_tokens.issue_link("180.dev.example.com", _GRANT)
    # Any number of reads (the landing page, a link scanner) leave it valid.
    assert connect_tokens.peek_link(token, "180.dev.example.com") == _GRANT
    assert connect_tokens.peek_link(token, "180.DEV.example.com") == _GRANT


def test_discard_makes_a_link_single_use():
    token = connect_tokens.issue_link("h", _GRANT)
    connect_tokens.discard(token)
    assert connect_tokens.peek_link(token, "h") is None
    connect_tokens.discard(token)  # idempotent


def test_link_is_bound_to_its_host():
    token = connect_tokens.issue_link("180.dev.example.com", _GRANT)
    assert connect_tokens.peek_link(token, "181.dev.example.com") is None
    # A wrong-host read is not a redemption attempt: the link survives it.
    assert connect_tokens.peek_link(token, "180.dev.example.com") == _GRANT


def test_link_expires():
    token = connect_tokens.issue_link("h", _GRANT, now=1000.0)
    ttl = connect_tokens.LINK_TTL_SECONDS
    assert connect_tokens.peek_link(token, "h", now=1000.0 + ttl - 1) == _GRANT
    assert connect_tokens.peek_link(token, "h", now=1000.0 + ttl + 1) is None


def test_link_token_is_not_a_session_token():
    # consume() is the dashboard's one-hop GET. It must neither hand out nor
    # burn a link token, whose session does not exist yet.
    token = connect_tokens.issue_link("h", _GRANT)
    assert connect_tokens.consume(token, "h") is None
    assert connect_tokens.peek_link(token, "h") == _GRANT


def test_session_token_is_not_a_link_token():
    token = connect_tokens.issue("h", "sid")
    assert connect_tokens.peek_link(token, "h") is None
    assert connect_tokens.consume(token, "h") == "sid"
