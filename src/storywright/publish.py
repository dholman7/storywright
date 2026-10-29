"""Publishes a finished run: a pull request for passing tests, a Jira comment either way.

No model calls. It reads only what `storywright run` handed over, adds exactly one file to a
fresh branch in a temporary worktree (the caller's checkout is never touched), and refuses to
publish anything but a `passing` or `app_defect` verdict.
"""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from storywright.jira import ISSUE_KEY, JiraWriter

BRANCH_PREFIX = "storywright/"

# argv, cwd -> stdout. Tests swap in a fake for `gh`.
Runner = Callable[[list[str], Path], str]


class PublishError(RuntimeError):
    pass


def run_command(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PublishError(f"`{' '.join(argv[:3])}` failed: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


@dataclass(frozen=True)
class Handover:
    """What `storywright run` left in its output directory."""

    task_id: str
    verdict: str
    reason: str
    spec: Path | None
    tool_calls: int
    cost_usd: float
    model: str

    @classmethod
    def load(cls, out: Path, task_id: str) -> "Handover":
        log_file = out / "storywright-log.json"
        if not log_file.exists():
            raise PublishError(f"no storywright-log.json in {out}; run `storywright run` first")
        log = json.loads(log_file.read_text())
        if log.get("task_id", task_id) != task_id:
            raise PublishError(f"{out} holds a run for {log['task_id']}, not {task_id}")
        usage = json.loads((out / "usage.json").read_text())
        spec = out / f"{task_id}.spec.ts"
        return cls(
            task_id=task_id,
            verdict=str(log.get("verdict")),
            reason=str(log.get("reason") or ""),
            spec=spec if spec.exists() else None,
            tool_calls=int(log.get("tool_calls", 0)),
            cost_usd=float(usage["cost_usd"]),
            model=str(usage["model"]),
        )


@dataclass(frozen=True)
class Plan:
    handover: Handover
    summary: str
    test_file: str | None  # repo-relative; None means no pull request
    branch: str | None
    status: str | None  # Jira status to move to

    def describe(self, base: str) -> str:
        lines = [f"{self.handover.task_id}: verdict {self.handover.verdict}"]
        if self.branch:
            lines += [
                f"  open a pull request into {base} from {self.branch}",
                f"  adding only {self.test_file}",
                f"  comment the PR link on {self.handover.task_id}",
            ]
        else:
            lines.append(
                f"  comment the suspected defect on {self.handover.task_id}; no pull request"
            )
        if self.status:
            lines.append(f"  move {self.handover.task_id} to {self.status!r}")
        return "\n".join(lines)


def plan(handover: Handover, summary: str, test_dir: str, status: str = "In Review") -> Plan:
    if handover.verdict == "passing":
        if handover.spec is None:
            raise PublishError(f"{handover.task_id} passed but its test file is missing")
        return Plan(
            handover,
            summary,
            test_file=f"{test_dir}/{handover.task_id}.spec.ts",
            branch=f"{BRANCH_PREFIX}{handover.task_id.lower()}",
            status=status,
        )
    if handover.verdict == "app_defect":
        return Plan(handover, summary, test_file=None, branch=None, status=None)
    raise PublishError(
        f"nothing to publish: {handover.task_id} ended as {handover.verdict!r} ({handover.reason})"
    )


def pr_body(p: Plan) -> str:
    h = p.handover
    return (
        f"Adds `{p.test_file}`, written by Storywright from Jira {h.task_id}: {p.summary}\n\n"
        f"- Verdict: {h.verdict} ({h.reason})\n"
        f"- Model: {h.model}, {h.tool_calls} repo-tool calls, ${h.cost_usd:.4f}\n\n"
        "The test passed against the running app before this pull request was opened. "
        "Review it like any other change."
    )


def open_pull_request(repo: Path, p: Plan, base: str, run: Runner = run_command) -> str:
    """Commits the one test file on a new branch in a throwaway worktree, pushes, opens the PR."""
    h = p.handover
    if not (p.branch and p.test_file and h.spec):
        raise PublishError(f"{h.task_id} has no passing test to open a pull request with")
    with tempfile.TemporaryDirectory(prefix="storywright-") as tmp:
        tree = Path(tmp) / "tree"
        run(["git", "fetch", "origin", base], repo)
        run(["git", "worktree", "add", "-b", p.branch, str(tree), f"origin/{base}"], repo)
        try:
            target = tree / p.test_file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(h.spec, target)
            run(["git", "add", "--", p.test_file], tree)
            run(["git", "commit", "-m", f"test(api): {h.task_id} {p.summary}"], tree)
            run(["git", "push", "-u", "origin", p.branch], tree)
            return run(
                [
                    "gh", "pr", "create",
                    "--base", base,
                    "--head", p.branch,
                    "--title", f"test(api): {h.task_id} {p.summary}",
                    "--body", pr_body(p),
                ],
                tree,
            )  # fmt: skip
        finally:
            run(["git", "worktree", "remove", "--force", str(tree)], repo)


def publish(
    repo: Path, p: Plan, base: str, jira: JiraWriter, run: Runner = run_command
) -> str | None:
    """Performs the plan. Returns the PR URL, if one was opened."""
    h = p.handover
    if p.branch:
        url = open_pull_request(repo, p, base, run)
        jira.comment(
            h.task_id,
            f"Storywright opened a pull request with API tests for this story. They pass against "
            f"the running app ({h.model}, ${h.cost_usd:.4f}).",
            link=url,
        )
    else:
        url = None
        jira.comment(
            h.task_id,
            f"Storywright suspects the app doesn't match this story, so it opened no pull "
            f"request. Its diagnosis: {h.reason}",
        )
    if p.status:
        jira.transition(h.task_id, p.status)
    return url


def key_from_branch(branch: str) -> str | None:
    """`storywright/kan-1` -> `KAN-1`; None for any branch `publish` didn't create."""
    if not branch.startswith(BRANCH_PREFIX):
        return None
    key = branch.removeprefix(BRANCH_PREFIX).upper()
    return key if ISSUE_KEY.match(key) else None


def close(jira: JiraWriter, key: str, pr_url: str, merge_sha: str, status: str = "Done") -> None:
    """After the pull request merges: record where the tests landed, then close the issue."""
    jira.comment(key, f"Merged in {merge_sha[:7]}; the tests are on the main branch.", link=pr_url)
    jira.transition(key, status)
