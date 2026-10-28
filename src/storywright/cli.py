"""`storywright` command line.

storywright run --repo PATH --story FILE --task-id ID [--config FILE] [--out DIR]
storywright run --repo PATH --jira KEY [--config FILE] [--out DIR]   # needs JIRA_* variables
storywright publish --repo PATH --jira KEY [--out DIR] [--yes]       # PR + Jira comment
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
from storywright.publish import Handover, PublishError, plan, publish
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
    max_repairs: int,
    budget_usd: float,
    mcp: McpTools | None = None,
) -> agent_graph.State:
    llm = Claude(toolbox=Toolbox(repo), mcp=mcp)

    def verifier(task: str, code: str) -> Verification:
        path = agent_graph.test_path(repo, config.test_dir, task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
        return verify(repo, config, path)

    context = prompts.render_context(read_context(repo, config))
    compiled = agent_graph.build(
        llm, context, config.test_dir, verifier, max_repairs=max_repairs, budget_usd=budget_usd
    )
    final = agent_graph.run(compiled, task_id, story, tag)

    out.mkdir(parents=True, exist_ok=True)
    # Hand over tests only when they're green, or when they document a suspected app defect.
    if final.get("verdict") in ("passing", "app_defect") and final.get("code"):
        (out / f"{task_id}.spec.ts").write_text(final["code"])
    (out / "usage.json").write_text(
        json.dumps(
            {
                "input_tokens": llm.spend.input_tokens,
                "output_tokens": llm.spend.output_tokens,
                "cost_usd": round(llm.spend.cost_usd, 6),
                "model": ",".join(sorted(llm.spend.models)) or llm.model,
            }
        )
    )
    (out / "storywright-log.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
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
    args = parser.parse_args(argv)
    if args.command == "run" and args.story and not args.task_id:
        parser.error("--task-id is required with --story")
    config = RepoConfig.load(args.config)
    if args.command == "publish":
        return _publish(args, config)

    if args.command == "aeg":
        task = json.loads(Path(os.environ["AEG_TASK"]).read_text())
        repo, out = Path(os.environ["AEG_WORKSPACE"]), Path(os.environ["AEG_OUTPUT"])
        task_id, story, tag = task["task_id"], task["story"], task["required_tag"]
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

    specs = [ServerSpec.parse(s, args.mcp_env) for s in args.mcp_server]
    with connect(specs) as mcp:
        final = solve(
            repo,
            config,
            task_id,
            story,
            tag,
            out,
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
        the_plan = plan(handover, jira.story(args.jira).summary, config.test_dir, args.status)
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


if __name__ == "__main__":
    sys.exit(main())
