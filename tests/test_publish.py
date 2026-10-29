"""Publishing a finished run: real git against a local bare remote, fake `gh`, mock Jira."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from storywright import cli
from storywright.jira import JiraError, JiraWriter
from storywright.publish import (
    Handover,
    PublishError,
    close,
    key_from_branch,
    plan,
    publish,
    run_command,
)

TEST_DIR = "e2e/api/generated"
PR_URL = "https://github.com/owner/app/pull/7"


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")


def _handover(
    out: Path, verdict: str = "passing", task_id: str = "KAN-1", spec: bool = True
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    log = {"task_id": task_id, "verdict": verdict, "reason": "every test passes", "tool_calls": 8}
    (out / "storywright-log.json").write_text(json.dumps(log))
    usage = {"cost_usd": 0.0043, "model": "claude-haiku-5-5"}
    (out / "usage.json").write_text(json.dumps(usage))
    if spec:
        (out / f"{task_id}.spec.ts").write_text('test("x", { tag: "@KAN-1" }, async () => {});\n')
    return out


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' with one commit on main, and a working clone of it."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    run_command(["git", "init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
    run_command(["git", "clone", "-q", str(origin), str(clone)], tmp_path)
    (clone / "README.md").write_text("app\n")
    run_command(["git", "add", "README.md"], clone)
    run_command(["git", "commit", "-q", "-m", "init"], clone)
    run_command(["git", "push", "-q", "origin", "HEAD:main"], clone)
    (clone / "scratch.txt").write_text("uncommitted work the publisher must not touch\n")
    return origin, clone


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path) -> str:
        if argv[0] == "gh":
            self.calls.append(argv)
            return PR_URL
        return run_command(argv, cwd)


def _jira(seen: list[httpx.Request], transitions: list[dict[str, Any]] | None = None) -> JiraWriter:
    available = transitions or [
        {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
        {"id": "31", "name": "Review", "to": {"name": "In Review"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/transitions") and request.method == "GET":
            return httpx.Response(200, json={"transitions": available})
        if request.method == "GET":
            return httpx.Response(200, json={"fields": {"summary": "Members cannot decide"}})
        return httpx.Response(204 if request.url.path.endswith("/transitions") else 201, json={})

    return JiraWriter(
        "https://example.atlassian.net", "dev@example.com", "tok", httpx.MockTransport(handler)
    )


def test_handover_loads_what_run_wrote(tmp_path: Path) -> None:
    h = Handover.load(_handover(tmp_path / "out"), "KAN-1")

    assert (h.verdict, h.tool_calls, h.model) == ("passing", 8, "claude-haiku-5-5")
    assert h.spec == tmp_path / "out" / "KAN-1.spec.ts"


def test_handover_refuses_a_missing_or_mismatched_run(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="run `storywright run` first"):
        Handover.load(tmp_path, "KAN-1")
    with pytest.raises(PublishError, match="holds a run for KAN-2"):
        Handover.load(_handover(tmp_path / "out", task_id="KAN-2"), "KAN-1")


@pytest.mark.parametrize("verdict", ["test_bug", "environment", "budget", "None"])
def test_only_passing_and_app_defect_runs_are_published(tmp_path: Path, verdict: str) -> None:
    h = Handover.load(_handover(tmp_path / "out", verdict=verdict), "KAN-1")

    with pytest.raises(PublishError, match="nothing to publish"):
        plan(h, "s", TEST_DIR)


def test_a_passing_run_without_its_test_file_is_refused(tmp_path: Path) -> None:
    h = Handover.load(_handover(tmp_path / "out", spec=False), "KAN-1")

    with pytest.raises(PublishError, match="test file is missing"):
        plan(h, "s", TEST_DIR)


def test_passing_run_opens_a_one_file_pr_then_comments_and_moves_the_issue(tmp_path: Path) -> None:
    origin, clone = _repos(tmp_path)
    gh, seen = FakeGh(), list[httpx.Request]()
    p = plan(Handover.load(_handover(tmp_path / "out"), "KAN-1"), "Members cannot decide", TEST_DIR)

    url = publish(clone, p, "main", _jira(seen), run=gh)

    assert url == PR_URL
    # Exactly one new commit on the branch, adding exactly the test file.
    changed = run_command(["git", "diff", "--name-status", "main", "storywright/kan-1"], origin)
    assert changed == f"A\t{TEST_DIR}/KAN-1.spec.ts"
    assert run_command(["git", "rev-list", "--count", "main..storywright/kan-1"], origin) == "1"
    # The caller's checkout is untouched and the temporary worktree is gone.
    assert run_command(["git", "status", "--porcelain"], clone) == "?? scratch.txt"
    assert len(run_command(["git", "worktree", "list"], clone).splitlines()) == 1
    [pr] = gh.calls
    assert (
        pr[pr.index("--base") + 1] == "main" and pr[pr.index("--head") + 1] == "storywright/kan-1"
    )
    assert "claude-haiku-5-5, 8 repo-tool calls, $0.0043" in pr[pr.index("--body") + 1]
    comment, transition = (r for r in seen if r.method == "POST")
    body = json.loads(comment.content)["body"]["content"][0]["content"]
    assert body[-1]["marks"] == [{"type": "link", "attrs": {"href": PR_URL}}]
    assert json.loads(transition.content) == {"transition": {"id": "31"}}


def test_app_defect_comments_the_diagnosis_without_git_or_a_transition(tmp_path: Path) -> None:
    gh, seen = FakeGh(), list[httpx.Request]()
    out = _handover(tmp_path / "out", verdict="app_defect")
    p = plan(Handover.load(out, "KAN-1"), "s", TEST_DIR)

    def no_commands(argv: list[str], cwd: Path) -> str:
        raise AssertionError(f"ran {argv}")

    assert publish(tmp_path, p, "main", _jira(seen), run=no_commands) is None
    [comment] = [r for r in seen if r.method == "POST"]
    assert "suspects the app doesn't match" in comment.content.decode()
    assert gh.calls == []


def test_a_missing_transition_names_the_ones_available() -> None:
    seen: list[httpx.Request] = []
    jira = _jira(seen, [{"id": "11", "name": "Start", "to": {"name": "In Progress"}}])

    with pytest.raises(JiraError, match="available: In Progress"):
        jira.transition("KAN-1", "In Review")


def test_cli_publish_without_yes_only_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setattr(JiraWriter, "from_env", classmethod(lambda cls: _jira(seen)))
    out = _handover(tmp_path / "out")

    code = cli.main(["publish", "--repo", str(tmp_path), "--jira", "KAN-1", "--out", str(out)])

    assert code == 0
    assert {r.method for r in seen} == {"GET"}
    err = capsys.readouterr().err
    assert "open a pull request into main from storywright/kan-1" in err
    assert "dry run: pass --yes" in err


@pytest.mark.parametrize(
    ("branch", "key"),
    [
        ("storywright/kan-1", "KAN-1"),
        ("storywright/abc2-104", "ABC2-104"),
        ("main", None),
        ("feature/kan-1", None),
        ("storywright/not-a-key", None),
        ("storywright/kan-1/../x", None),
    ],
)
def test_key_from_branch_accepts_only_branches_publish_creates(
    branch: str, key: str | None
) -> None:
    assert key_from_branch(branch) == key


def test_close_comments_the_merge_and_moves_the_issue_to_done() -> None:
    seen: list[httpx.Request] = []
    jira = _jira(seen, [{"id": "41", "name": "Done", "to": {"name": "Done"}}])

    close(jira, "KAN-1", PR_URL, "c3b71490000000000000000000000000000000000")

    comment, transition = (r for r in seen if r.method == "POST")
    text = json.loads(comment.content)["body"]["content"][0]["content"]
    assert text[0]["text"].startswith("Merged in c3b7149;")
    assert text[-1]["marks"][0]["attrs"]["href"] == PR_URL
    assert json.loads(transition.content) == {"transition": {"id": "41"}}


def test_cli_close_ignores_other_branches_and_dry_runs_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_jira(cls: Any) -> JiraWriter:
        raise AssertionError("Jira must not be contacted")

    monkeypatch.setattr(JiraWriter, "from_env", classmethod(no_jira))
    common = ["--pr-url", PR_URL, "--sha", "c3b7149"]

    assert cli.main(["close", "--branch", "feature/x", *common, "--yes"]) == 0
    assert cli.main(["close", "--branch", "storywright/kan-1", *common]) == 0
    err = capsys.readouterr().err
    assert "isn't a Storywright branch" in err
    assert "KAN-1: comment the merge (c3b7149), move to 'Done'" in err
    assert "dry run" in err
