import pathlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tool_helpers import call_tool

from oduflow import feedback
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_ISSUE_TEMPLATE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"
)


def _params(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == feedback.NEW_ISSUE_URL
    return {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}


def _client(tmp_path) -> TestClient:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_url_carries_template_title_and_details():
    url = feedback.build_issue_url("bug", "Sync fails", "Ran sync, got a traceback")

    params = _params(url)
    assert params["template"] == "bug.yml"
    assert params["title"] == "Sync fails"
    assert params["details"] == "Ran sync, got a traceback"
    assert "Oduflow:" in params["environment"]


def test_unknown_kind_falls_back_to_feedback_form():
    assert _params(feedback.build_issue_url("nonsense"))["template"] == "feedback.yml"


def test_labels_are_never_passed_as_a_query_parameter():
    # GitHub answers 404 for `labels=` from users without triage rights, so the
    # labels live in the issue form YAML instead.
    assert "labels" not in _params(feedback.build_issue_url("bug", "t", "d"))


def test_empty_title_is_omitted():
    assert "title" not in _params(feedback.build_issue_url("feedback", "  ", "d"))


def test_long_details_are_truncated_to_keep_the_url_usable():
    url = feedback.build_issue_url("bug", "t", "x" * 50_000)

    assert len(url) <= feedback.MAX_URL_LEN
    assert "truncated" in _params(url)["details"]


def test_long_title_is_bounded_to_keep_the_url_usable():
    url = feedback.build_issue_url("bug", "😀" * 5_000, "details")

    assert len(url) <= feedback.MAX_URL_LEN
    assert len(_params(url)["title"]) == feedback.MAX_TITLE_LEN


def test_diagnostics_carry_no_identifying_deployment_data(tmp_path):
    team = TeamSettings(
        team_id="acme", hostname="odoo.acme.example", data_dir=str(tmp_path)
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"acme": team})

    rendered = feedback.format_diagnostics(feedback.diagnostics(settings))

    assert "acme" not in rendered
    assert "odoo.acme.example" not in rendered
    assert str(tmp_path) not in rendered
    assert "Oduflow:" in rendered


def test_dashboard_renders_version_and_diagnostics(tmp_path):
    html = _client(tmp_path).get("/").text

    assert "__ODUFLOW_VERSION__" not in html
    assert "__ODUFLOW_DIAGNOSTICS__" not in html
    assert f'onclick="openVersionModal()">v{feedback.oduflow_version()}<' in html


def test_feedback_link_endpoint_builds_a_prefilled_url(tmp_path):
    response = _client(tmp_path).post(
        "/api/feedback/link",
        json={"kind": "bug", "title": "Sync fails", "details": "traceback here"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    params = _params(body["url"])
    assert params["template"] == "bug.yml"
    assert params["details"] == "traceback here"


def test_feedback_link_endpoint_requires_details(tmp_path):
    response = _client(tmp_path).post(
        "/api/feedback/link", json={"kind": "bug", "details": "   "}
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False


@pytest.mark.parametrize("body", [[], 42, "feedback"])
def test_feedback_link_endpoint_requires_a_json_object(tmp_path, body):
    response = _client(tmp_path).post("/api/feedback/link", json=body)

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "Request body must be a JSON object.",
    }


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"details": ["traceback"]}, "details"),
        ({"details": "traceback", "kind": 1}, "kind"),
        ({"details": "traceback", "title": {"text": "Sync fails"}}, "title"),
    ],
)
def test_feedback_link_endpoint_rejects_non_string_fields(tmp_path, body, field):
    response = _client(tmp_path).post("/api/feedback/link", json=body)

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": f"{field} must be a string.",
    }


def test_feedback_link_endpoint_rejects_malformed_json(tmp_path):
    response = _client(tmp_path).post(
        "/api/feedback/link",
        content=b"{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "Request body must be valid JSON.",
    }


def test_every_kind_has_a_shipped_issue_form_with_the_prefilled_fields():
    for kind, filename in feedback.KINDS.items():
        form = (_ISSUE_TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        assert "labels:" in form, f"{kind} form must carry its own labels"
        assert "id: details" in form
        assert "id: environment" in form


def test_report_issue_tool_returns_a_link_without_filing_anything(
    monkeypatch, tmp_path
):
    from oduflow import server

    settings = Settings(base_data_dir=str(tmp_path))
    monkeypatch.setattr(server, "_get_settings", lambda: settings)

    result = call_tool(
        "report_issue", details="Sync hangs", kind="bug", title="Sync hangs"
    )

    assert feedback.NEW_ISSUE_URL in result
    assert "your own GitHub" in result
    url = result.split(feedback.NEW_ISSUE_URL)[1]
    assert "template=bug.yml" in url


def test_report_issue_tool_rejects_an_unknown_kind():
    with pytest.raises(ToolError, match="Unknown kind"):
        call_tool("report_issue", details="x", kind="rant")


def test_report_issue_tool_requires_details():
    with pytest.raises(ToolError, match="details is required"):
        call_tool("report_issue", details="   ")
