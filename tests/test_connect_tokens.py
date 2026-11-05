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


def test_login_link_builds_env_host_url_and_token_resolves_once():
    from urllib.parse import parse_qs, urlparse

    link = connect_tokens.login_link("https://180.dev.example.com", "sid-1")

    parsed = urlparse(link)
    assert parsed.scheme == "https"
    assert parsed.netloc == "180.dev.example.com"
    assert parsed.path == "/oduflow-connect"
    token = parse_qs(parsed.query)["token"][0]
    assert connect_tokens.consume(token, "180.dev.example.com") == "sid-1"
    assert connect_tokens.consume(token, "180.dev.example.com") is None


def test_login_link_carries_a_grant_bound_to_the_env_host():
    from urllib.parse import parse_qs, urlparse

    grant = connect_tokens.LoginGrant("1", "feature", "admin")
    link = connect_tokens.login_link("http://Feature.Dev.example.com/", grant)
    assert link.startswith("http://Feature.Dev.example.com/oduflow-connect?token=")
    token = parse_qs(urlparse(link).query)["token"][0]
    assert connect_tokens.consume(token, "other.dev.example.com") is None
    link = connect_tokens.login_link("http://Feature.Dev.example.com", grant)
    token = parse_qs(urlparse(link).query)["token"][0]
    assert connect_tokens.consume(token, "feature.dev.example.com") == grant
