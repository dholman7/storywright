"""`storywright` command line.

storywright run --repo PATH --story FILE --task-id ID [--config FILE] [--out DIR]
storywright run --repo PATH --jira KEY [--kind auto|api|ui] [--out DIR]   # needs JIRA_* vars
storywright publish --repo PATH --jira KEY [--out DIR] [--yes]       # PR + Jira comment
storywright close --branch B --pr-url URL --sha SHA [--yes]          # after the PR merges
storywright aeg [--config FILE]     # agent-eval-gate command contract (AEG_* variables)
"""

import argparse
import json
import os
import sys
from pathlib import Path

from storywright import graph as agent_graph
from storywright import prompts
from storywright.jira import JiraClient, JiraError, JiraWriter
from storywright.llm import Claude
from storywright.mcp_tools import McpTools, ServerSpec, connect
from storywright.publish import Handover, PublishError, close, key_from_branch, plan, publish
from storywright.repo import RepoConfig, Verification, read_context, verify
from storywright.tools import Toolbox

DEFAULT_CONFIG = Path(__file__).parents[2] / "configs" / "vendor-intake.yaml"


def solve(
    repo: Path,
    config: RepoConfig,
    task_id: str,
    story: str,
    tag: str,
    out: Path,
    *,
    kinds: list[str],
    max_repairs: int,
    budget_usd: float,
    mcp: McpTools | None = None,
    support_dir: Path | None = None,
) -> agent_graph.State:
    """Writes and verifies tests for one story. `kinds` are the test kinds the model may choose.

    Extended shared files (page objects) are handed over next to the test, or, with
    `support_dir` (agent-eval-gate's contract), under it at their path in the repo.
    """
    llm = Claude(toolbox=Toolbox(repo), mcp=mcp)
    pages_file = repo / config.page_objects if config.page_objects else None
    original_pages = pages_file.read_text() if pages_file and pages_file.is_file() else None
    # UI tests may extend the page objects; API tests never need to.
    editable = original_pages is not None and "ui" in kinds

    def restore_pages() -> None:
        if pages_file and original_pages is not None:
            pages_file.write_text(original_pages)

    def verifier(state: agent_graph.State) -> Verification:
        kind = state["kind"]
        path = agent_graph.test_path(repo, config.kinds[kind].test_dir, state["task_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state["code"])
        extended = bool(pages_file and state.get("page_objects"))
        if pages_file and extended:
            pages_file.write_text(state["page_objects"])
        else:
            restore_pages()
        # Lint the page objects too when this run added to them: its new locators live there.
        return verify(repo, config, kind, path, [pages_file] if pages_file and extended else None)

    context = prompts.render_context(read_context(repo, config, kinds))
    compiled = agent_graph.build(
        llm,
        context,
        {kind: config.kinds[kind].test_dir for kind in kinds},
        verifier,
        page_objects=(config.page_objects, original_pages)
        if editable and config.page_objects and original_pages is not None
        else None,
        max_repairs=max_repairs,
        budget_usd=budget_usd,
    )
    final = agent_graph.run(compiled, task_id, story, tag)

    out.mkdir(parents=True, exist_ok=True)
    kind = final.get("kind")
    test_file = f"{config.kinds[kind].test_dir}/{task_id}.spec.ts" if kind else None
    extra_files: dict[str, str] = {}
    # Hand over tests only when they're green, or when they document a suspected app defect.
    if final.get("verdict") in ("passing", "app_defect") and final.get("code"):
        (out / f"{task_id}.spec.ts").write_text(final["code"])
        if config.page_objects and final.get("page_objects"):
            if support_dir is not None:
                target = support_dir / config.page_objects
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(final["page_objects"])
            else:
                (out / Path(config.page_objects).name).write_text(final["page_objects"])
                extra_files[config.page_objects] = Path(config.page_objects).name
    else:
        # A failed run leaves no half-finished edit to code other tests share.
        restore_pages()
    (out / "usage.json").write_text(
        json.dumps(
            {
                "input_tokens": llm.spend.input_tokens,
                "output_tokens": llm.spend.output_tokens,
                "cache_read_input_tokens": llm.spend.cache_read_tokens,
                "cache_write_input_tokens": llm.spend.cache_write_tokens,
                "cost_usd": round(llm.spend.cost_usd, 6),
                "model": ",".join(sorted(llm.spend.models)) or llm.model,
            }
        )
    )
    (out / "storywright-log.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "kind": kind,
                "test_file": test_file,
                # Repo path -> file name in this directory, for shared files the test needs.
                "extra_files": extra_files,
                "verdict": final.get("verdict"),
                "reason": final.get("reason"),
                "tool_calls": llm.tool_calls,
                "log": final.get("log", []),
            },
            indent=2,
        )
    )
    final["log"] = [*final.get("log", []), f"tool calls: {llm.tool_calls}"]
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="storywright", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "aeg"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--max-repairs", type=int, default=3)
        p.add_argument("--budget-usd", type=float, default=1.00)
        p.add_argument(
            "--mcp-server",
            action="append",
            default=[],
            metavar="NAME=COMMAND",
            help="attach an MCP server over stdio (repeatable)",
        )
        p.add_argument(
            "--mcp-env",
            action="append",
            default=[],
            metavar="VAR",
            help="environment variable to pass to MCP servers; nothing else is passed",
        )
        if name == "run":
            p.add_argument("--repo", type=Path, required=True)
            source = p.add_mutually_exclusive_group(required=True)
            source.add_argument("--story", type=Path, help="file with the scenario text")
            source.add_argument("--jira", metavar="KEY", help="read the story from a Jira issue")
            p.add_argument("--task-id", help="test tag; defaults to the Jira key")
            p.add_argument("--out", type=Path, default=Path("storywright-out"))
            p.add_argument(
                "--kind",
                choices=["auto", "api", "ui"],
                default="auto",
                help="kind of test to write; auto lets the agent choose from the story",
            )
    pub = sub.add_parser("publish", help="open a PR and update Jira from a finished run")
    pub.add_argument(
        "--repo", type=Path, required=True, help="git checkout whose origin gets the PR"
    )
    pub.add_argument("--jira", metavar="KEY", required=True)
    pub.add_argument("--out", type=Path, default=Path("storywright-out"))
    pub.add_argument("--base", default="main")
    pub.add_argument("--status", default="In Review", help="Jira status after a PR is opened")
    pub.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pub.add_argument(
        "--yes", action="store_true", help="perform the plan; without it, only print it"
    )
    cl = sub.add_parser("close", help="close the Jira issue once a Storywright PR merges")
    cl.add_argument("--branch", required=True, help="the merged PR's head branch")
    cl.add_argument("--pr-url", required=True)
    cl.add_argument("--sha", required=True, help="the merge commit")
    cl.add_argument("--status", default="Done")
    cl.add_argument("--yes", action="store_true", help="perform it; without it, only print it")
    args = parser.parse_args(argv)
    if args.command == "close":
        return _close(args)
    if args.command == "run" and args.story and not args.task_id:
        parser.error("--task-id is required with --story")
    config = RepoConfig.load(args.config)
    if args.command == "publish":
        return _publish(args, config)

    if args.command == "aeg":
        task = json.loads(Path(os.environ["AEG_TASK"]).read_text())
        repo, out = Path(os.environ["AEG_WORKSPACE"]), Path(os.environ["AEG_OUTPUT"])
        task_id, story, tag = task["task_id"], task["story"], task["required_tag"]
        # The benchmark decides the kind; older task files predate the field.
        kinds = [task.get("kind", "api")]
        support_dir: Path | None = out / "support"
    else:
        repo, out = args.repo, args.out
        if args.jira:
            try:
                issue = JiraClient.from_env().story(args.jira)
            except JiraError as e:
                print(f"storywright: {e}", file=sys.stderr)
                return 2
            story, task_id = issue.text, args.task_id or issue.key
            if not issue.gherkin:
                print(f"  {issue.key} has no Gherkin block; using its description", file=sys.stderr)
        else:
            story, task_id = args.story.read_text(), args.task_id
        tag = f"@{task_id}"
        kinds = list(config.kinds) if args.kind == "auto" else [args.kind]
        support_dir = None

    specs = [ServerSpec.parse(s, args.mcp_env) for s in args.mcp_server]
    with connect(specs) as mcp:
        final = solve(
            repo,
            config,
            task_id,
            story,
            tag,
            out,
            kinds=kinds,
            support_dir=support_dir,
            max_repairs=args.max_repairs,
            budget_usd=args.budget_usd,
            mcp=mcp,
        )
    for line in final.get("log", []):
        print(f"  {line}", file=sys.stderr)
    print(f"{task_id}: {final.get('verdict')}: {final.get('reason')}", file=sys.stderr)
    return 0 if final.get("verdict") in ("passing", "app_defect") else 1


def _publish(args: argparse.Namespace, config: RepoConfig) -> int:
    try:
        jira = JiraWriter.from_env()
        handover = Handover.load(args.out, args.jira)
        the_plan = plan(
            handover, jira.story(args.jira).summary, config.kinds["api"].test_dir, args.status
        )
        print(the_plan.describe(args.base), file=sys.stderr)
        if not args.yes:
            print("dry run: pass --yes to do this", file=sys.stderr)
            return 0
        url = publish(args.repo, the_plan, args.base, jira)
    except (JiraError, PublishError) as e:
        print(f"storywright: {e}", file=sys.stderr)
        return 2
    print(url or f"commented on {args.jira}")
    return 0


def _close(args: argparse.Namespace) -> int:
    key = key_from_branch(args.branch)
    if key is None:
        print(f"{args.branch} isn't a Storywright branch; nothing to do", file=sys.stderr)
        return 0
    print(f"{key}: comment the merge ({args.sha[:7]}), move to {args.status!r}", file=sys.stderr)
    if not args.yes:
        print("dry run: pass --yes to do this", file=sys.stderr)
        return 0
    try:
        close(JiraWriter.from_env(), key, args.pr_url, args.sha, args.status)
    except JiraError as e:
        print(f"storywright: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
