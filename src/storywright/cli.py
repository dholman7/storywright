"""`storywright` command line.

storywright run --repo PATH --story FILE --task-id ID [--config FILE] [--out DIR]
storywright aeg [--config FILE]     # agent-eval-gate command contract (AEG_* variables)
"""

import argparse
import json
import os
import sys
from pathlib import Path

from storywright import graph as agent_graph
from storywright import prompts
from storywright.llm import Claude
from storywright.repo import RepoConfig, Verification, read_context, verify

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
) -> agent_graph.State:
    llm = Claude()

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
                "verdict": final.get("verdict"),
                "reason": final.get("reason"),
                "log": final.get("log", []),
            },
            indent=2,
        )
    )
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="storywright", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "aeg"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--max-repairs", type=int, default=3)
        p.add_argument("--budget-usd", type=float, default=1.00)
        if name == "run":
            p.add_argument("--repo", type=Path, required=True)
            p.add_argument("--story", type=Path, required=True, help="file with the scenario text")
            p.add_argument("--task-id", required=True)
            p.add_argument("--out", type=Path, default=Path("storywright-out"))
    args = parser.parse_args(argv)
    config = RepoConfig.load(args.config)

    if args.command == "aeg":
        task = json.loads(Path(os.environ["AEG_TASK"]).read_text())
        repo, out = Path(os.environ["AEG_WORKSPACE"]), Path(os.environ["AEG_OUTPUT"])
        task_id, story, tag = task["task_id"], task["story"], task["required_tag"]
    else:
        repo, out, task_id = args.repo, args.out, args.task_id
        story, tag = args.story.read_text(), f"@{args.task_id}"

    final = solve(
        repo,
        config,
        task_id,
        story,
        tag,
        out,
        max_repairs=args.max_repairs,
        budget_usd=args.budget_usd,
    )
    print(f"{task_id}: {final.get('verdict')}: {final.get('reason')}", file=sys.stderr)
    return 0 if final.get("verdict") in ("passing", "app_defect") else 1


if __name__ == "__main__":
    sys.exit(main())
