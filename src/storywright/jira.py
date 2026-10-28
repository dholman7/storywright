"""Jira Cloud. JiraClient reads stories with GETs only; JiraWriter adds comments and transitions."""

import os
import re
from dataclasses import dataclass
from typing import Any, Self

import httpx

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
GHERKIN_LANGUAGES = {"gherkin", "feature", "cucumber"}
GHERKIN_START = re.compile(r"^\s*(Feature|Scenario|Scenario Outline|Rule):", re.MULTILINE)


class JiraError(RuntimeError):
    pass


@dataclass(frozen=True)
class Story:
    key: str
    summary: str
    text: str
    # True when the text came from a Gherkin block rather than the issue's prose.
    gherkin: bool


def adf_text(node: Any) -> str:
    """Plain text from an Atlassian Document Format tree. Unknown nodes keep their text."""
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    children = [adf_text(c) for c in node.get("content", [])]
    if kind == "listItem":
        return "- " + "".join(children).strip()
    if kind in ("doc", "bulletList", "orderedList", "blockquote", "panel", "table", "tableRow"):
        return "\n".join(c for c in children if c)
    return "".join(children)


def _blocks(node: Any) -> list[dict[str, Any]]:
    """Every codeBlock in the tree, in document order."""
    if not isinstance(node, dict):
        return []
    if node.get("type") == "codeBlock":
        return [node]
    return [b for c in node.get("content", []) for b in _blocks(c)]


def story_text(summary: str, description: Any) -> tuple[str, bool]:
    """A Gherkin code block wins; otherwise the summary plus the description as prose."""
    gherkin = [
        adf_text(b)
        for b in _blocks(description)
        if (b.get("attrs") or {}).get("language", "").lower() in GHERKIN_LANGUAGES
        or GHERKIN_START.search(adf_text(b))
    ]
    if gherkin:
        return "\n\n".join(g.strip() for g in gherkin), True
    body = "\n\n".join(
        part
        for part in (adf_text(c).strip() for c in (description or {}).get("content", []))
        if part
    )
    return f"{summary}\n\n{body}".strip(), False


class JiraClient:
    def __init__(
        self, base_url: str, email: str, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/rest/api/3",
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> Self:
        names = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise JiraError(f"set {', '.join(missing)} to read stories from Jira")
        return cls(os.environ[names[0]], os.environ[names[1]], os.environ[names[2]])

    def story(self, key: str) -> Story:
        _check_key(key)
        response = self._http.get(f"/issue/{key}", params={"fields": "summary,description"})
        fields = _ok(response, key).json()["fields"]
        text, gherkin = story_text(fields.get("summary") or "", fields.get("description"))
        if not text:
            raise JiraError(f"{key} has no summary or description to write tests from")
        return Story(key=key, summary=fields.get("summary") or "", text=text, gherkin=gherkin)


class JiraWriter(JiraClient):
    """Adds the two writes `storywright publish` makes. `storywright run` never constructs one."""

    def comment(self, key: str, text: str, link: str | None = None) -> None:
        _check_key(key)
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if link:
            content += [
                {"type": "text", "text": " "},
                {
                    "type": "text",
                    "text": link,
                    "marks": [{"type": "link", "attrs": {"href": link}}],
                },
            ]
        body = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}
        _ok(self._http.post(f"/issue/{key}/comment", json={"body": body}), key)

    def transition(self, key: str, status: str) -> None:
        """Moves the issue to the named status, if the workflow allows it from where it is."""
        _check_key(key)
        transitions = _ok(self._http.get(f"/issue/{key}/transitions"), key).json()["transitions"]
        match = next(
            (
                t
                for t in transitions
                if status.lower() in (t["name"].lower(), t["to"]["name"].lower())
            ),
            None,
        )
        if match is None:
            available = ", ".join(sorted(t["to"]["name"] for t in transitions)) or "none"
            raise JiraError(f"{key} can't move to {status!r} from here; available: {available}")
        _ok(
            self._http.post(f"/issue/{key}/transitions", json={"transition": {"id": match["id"]}}),
            key,
        )


def _check_key(key: str) -> None:
    if not ISSUE_KEY.match(key):
        raise JiraError(f"{key!r} is not a Jira issue key like KAN-12")


def _ok(response: httpx.Response, key: str) -> httpx.Response:
    if response.status_code in (401, 403):
        raise JiraError("Jira rejected the credentials; check JIRA_EMAIL and JIRA_TOKEN")
    if response.status_code == 404:
        raise JiraError(f"{key} doesn't exist or this account can't see it")
    response.raise_for_status()
    return response
