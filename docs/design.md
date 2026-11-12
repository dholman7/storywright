# Storywright design

## Context

Teams write a Gherkin scenario for a story, then someone turns it into an automated test. That second step is slow, and it's where tests get weakened. A test that fails on correct code gets "fixed" until it passes, and sometimes that means it no longer checks what the scenario said. Storywright takes the scenario and the repo and returns a Playwright test that has already passed against the real app. If the app contradicts the scenario, it reports a defect instead.

## Goals

- **A verified test, not a draft.** A test is handed over only after it has type-checked, linted, and run green against the app, or with a defect report that says why it can't.
- **Never test less to get green.** A repair that cuts, loosens, or hedges an assertion is rejected deterministically, not by asking a model ([ADR 0001](adr/0001-structural-repair-guard.md)).
- **Follow the repo's own standard:** its fixtures, factory, page objects, and lint rules, read from the repo at run time.
- **Cheap enough to run on every story,** with a measured cost per run.
- **Untrusted input stays data.** Stories and repo files can carry instructions, and Storywright shouldn't follow them ([ADR 0002](adr/0002-prompt-injection-defenses.md)).

## Non-goals

- **Changing production code.** Storywright writes tests, and a defect report goes to a person.
- **Choosing what to test.** The scenario is the specification. Coverage planning belongs upstream.
- **Running unattended in CI on every push.** It's run per story, by a person or a Jira workflow. `publish` opens a pull request; it doesn't merge one.

## Architecture

```
write ──> verify ──green──> passing
            │
            └─red──> repair ──test_bug──> verify
                       │   └─rejected by the guard──> repair
                       └─app_defect / environment──> stop
```

- **Model calls** go through the Anthropic SDK with structured output, prompt caching, and a bounded tool loop ([ADR 0004](adr/0004-anthropic-sdk-not-langchain.md)). A second transport sends the same calls through headless Claude Code, billed to a subscription.
- **The graph** is LangGraph: three nodes with explicit routing, so the stop conditions are code, not prompt text ([ADR 0003](adr/0003-a-graph-not-a-coding-agent.md)).
- **Verification is deterministic:** the repo's type-checker and linter, then the test against the app on free ports with a fresh database. It also checks that the test skips nothing and asserts every value the Then steps state.
- **Publishing is a separate command with no model in it,** so the agent that writes tests never holds the credentials that publish them.

## Options considered

| Option | Why not |
|---|---|
| One model call per story | Measured: 16% of blind trials succeed and 7% of seeded bugs are caught, against 98% and 86% for the loop. |
| A general coding agent (Claude Code) | Measured level with Storywright on Haiku, at about 3.4× the cost per trial, and it skips tests when a story tells it to. Details in ADR 0003. |
| An LLM judge for "did this repair weaken the test?" | Non-deterministic, and a call per repair. The structural guard decides instead (ADR 0001). |

## Failure modes

| Failure | What happens | Seen |
|---|---|---|
| The spec is wrong | Storywright keeps the scenario's value and reports a defect | REV-03 in v1. The benchmark fixed the scenario in v2. |
| The app rejects the test's own setup data | Diagnosed as a test bug and repaired. The prompt says setup data is the test's fault. | v0.3's first held-out run, fixed in the prompt |
| The guard rejects every repair | Ends as `gave_up`, and no weaker test is handed over | AUD-02 in a v3 dev run: three repairs of a lint error each cut an assertion |
| The model returns no structured output | Ends as `agent_error` | 1 of 45 v3 trials, through the Claude Code transport |
| An injected instruction in a story or file | The four defenses: prompt rule, hidden credentials, skip check, Then-value check | 7/72 trials before, 0/72 after |
| A runaway tool loop or spend | 12 tool calls per model call, 3 repairs, and $1.00 per task | |

## Cost model

Measured per trial, which is one story to a verified test:

| Profile | Per trial | Source |
|---|---|---|
| `dev`: Haiku 5.5, low effort | $0.0025–0.0030 | v2 held-out (API) and v3 blind (subscription estimate) |
| `report`: Opus 5.5, medium | $0.09–0.20 | v2 API and UI, v3 |

At a team's pace of 50 stories a week, the default profile costs about $0.15 a week. [ADR 0005](adr/0005-cheap-by-default.md) explains why Haiku is the default.

## Metrics

agent-eval-gate scores every version. For each trial it records:

- task success: the test type-checks, passes on correct code, and carries the tag;
- seeded bugs caught;
- tests failing on correct code;
- cost.

Every rate is reported with a 95% interval. The paired gate fails a change only on a significant drop of at least 5 points. A version is compared against a blind held-out split, and prompts are only tuned on dev.
