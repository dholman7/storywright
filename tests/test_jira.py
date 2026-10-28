"""Jira as a story source, against a mock transport: no network."""

import base64
from typing import Any

import httpx
import pytest

from storywright import cli
from storywright.jira import JiraClient, JiraError, story_text

SCENARIO = (
    "Scenario: A reviewer approves a vendor\n"
    "  Given a vendor awaiting review\n"
    "  When the reviewer approves it\n"
    "  Then its status is approved"
)


def _doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def _para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _code(text: str, language: str | None = None) -> dict[str, Any]:
    attrs = {"language": language} if language else {}
    return {"type": "codeBlock", "attrs": attrs, "content": [{"type": "text", "text": text}]}


def _client(
    issue: dict[str, Any] | None, status: int = 200, seen: list[httpx.Request] | None = None
) -> JiraClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"fields": issue} if issue else {})

    return JiraClient(
        "https://example.atlassian.net/", "dev@example.com", "tok", httpx.MockTransport(handler)
    )


def test_gherkin_block_becomes_the_story_and_the_request_is_a_basic_auth_get() -> None:
    seen: list[httpx.Request] = []
    issue = {
        "summary": "Approve vendors",
        "description": _doc(_para("Context for humans."), _code(SCENARIO, "gherkin")),
    }

    story = _client(issue, seen=seen).story("KAN-7")

    assert story.text == SCENARIO and story.gherkin
    [request] = seen
    assert request.method == "GET"
    assert request.url.path == "/rest/api/3/issue/KAN-7"
    assert (
        request.headers["Authorization"]
        == "Basic " + base64.b64encode(b"dev@example.com:tok").decode()
    )


def test_unlabeled_code_block_counts_when_it_reads_as_gherkin() -> None:
    text, gherkin = story_text("s", _doc(_code("const x = 1;"), _code(SCENARIO)))

    assert gherkin and text == SCENARIO


def test_prose_is_the_fallback_with_lists_kept_readable() -> None:
    description = _doc(
        _para("Reviewers need to approve vendors."),
        {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [_para("Approved vendors leave the queue")]},
                {"type": "listItem", "content": [_para("An audit event is written")]},
            ],
        },
    )

    text, gherkin = story_text("Approve vendors", description)

    assert not gherkin
    assert text == (
        "Approve vendors\n\nReviewers need to approve vendors.\n\n"
        "- Approved vendors leave the queue\n- An audit event is written"
    )


def test_issue_with_no_description_uses_the_summary() -> None:
    story = _client({"summary": "Approve vendors", "description": None}).story("KAN-1")

    assert story.text == "Approve vendors" and not story.gherkin


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the credentials"), (403, "rejected the credentials"), (404, "doesn't exist")],
)
def test_http_errors_become_plain_messages_without_the_token(status: int, message: str) -> None:
    with pytest.raises(JiraError, match=message) as error:
        _client(None, status=status).story("KAN-1")

    assert "tok" not in str(error.value)


@pytest.mark.parametrize("key", ["kan-1", "KAN", "KAN-1/../../myself", "1-KAN"])
def test_malformed_keys_are_refused_before_any_request(key: str) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(JiraError, match="not a Jira issue key"):
        _client({"summary": "x"}, seen=seen).story(key)

    assert seen == []


def test_from_env_names_every_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")

    with pytest.raises(JiraError, match="JIRA_BASE_URL, JIRA_TOKEN"):
        JiraClient.from_env()


def test_cli_uses_the_issue_key_as_task_id_and_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(
        JiraClient,
        "from_env",
        classmethod(
            lambda cls: _client({"summary": "s", "description": _doc(_code(SCENARIO, "gherkin"))})
        ),
    )

    def fake_solve(
        repo: Any, config: Any, task_id: str, story: str, tag: str, out: Any, **_: Any
    ) -> dict[str, Any]:
        calls.update(task_id=task_id, story=story, tag=tag)
        return {"verdict": "passing", "reason": "green", "log": []}

    monkeypatch.setattr(cli, "solve", fake_solve)

    code = cli.main(
        ["run", "--repo", str(tmp_path), "--jira", "KAN-7", "--out", str(tmp_path / "out")]
    )

    assert code == 0
    assert calls == {"task_id": "KAN-7", "story": SCENARIO, "tag": "@KAN-7"}


def test_cli_reports_jira_errors_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    code = cli.main(["run", "--repo", str(tmp_path), "--jira", "KAN-7"])

    assert code == 2
    assert "set JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN" in capsys.readouterr().err


def test_story_file_still_requires_a_task_id(tmp_path: Any) -> None:
    (tmp_path / "s.feature").write_text(SCENARIO)

    with pytest.raises(SystemExit):
        cli.main(["run", "--repo", str(tmp_path), "--story", str(tmp_path / "s.feature")])
