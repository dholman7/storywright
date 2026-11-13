# 3. A small graph with deterministic checks, not a general coding agent

Status: accepted, 2026-11-12 (backfilled: the graph dates from v0.1; the comparison is from v3)

## Context

The test-writing loop could be built three ways:

- **A plain loop:** call the model, run the test, call it again with the failures.
- **A state graph** with explicit nodes and routing (LangGraph).
- **A general coding agent** handed the task, such as Claude Code with file and shell tools.

The third needs the least code, so it's the bar the others have to clear.

## Decision

A LangGraph graph with three nodes (write, verify, repair) and routing in code. Verification and the repair guard are deterministic Python, not model judgment. Stop conditions are also code: green, a defect report, an environment failure, 3 repairs, or $1.00. That way none of them depend on the model deciding to stop.

LangGraph over a plain loop because the routing is the part worth reading. Each edge names a condition (`after_verify`, `after_repair`), state is a typed dict, and the graph runs the same way in tests with a scripted model. A plain loop would work, but its control flow would be a while-loop with flags.

## The comparison, measured

agent-eval-gate's v3 benchmark ran Claude Code headless as the agent (`--agent claude-code`). It got the same task and rules, the app running, and the benchmark's own run and type-check commands. On Haiku 5.5, 15 blind tasks, 3 trials each:

| | Storywright | Claude Code |
|---|---|---|
| Trials succeeded | 98% (44/45) | 100% (45/45) |
| Seeded bugs caught | 86% (98/114) | 89% (102/114), p = 0.25 |
| Cost per trial | $0.0030 | $0.0101 |
| Prompt injection, trials attacked | 2/36 (0/36 with defenses) | 8/36. It wrapped a test in `test.fixme()` every time a story told it to. |

## Consequences

- **The graph's value isn't accuracy.** A good coding agent is just as accurate on these tasks. The graph buys:
  - about a third of the cost;
  - guarantees a coding agent doesn't give: the repair guard, the skip and Then-value checks, and a credential-free tool surface;
  - verification that happens whether or not the model decides to run it.
- **These numbers can change the decision.** If a coding agent's cost drops, or a hook-based guard becomes possible inside one, the comparison should be rerun. agent-eval-gate keeps that a one-line command.
