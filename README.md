# Storywright

An agent that turns a user story into Playwright API tests and proves they work: it writes the tests, runs them against the real app, reads what failed, and repairs them, without weakening an assertion to get a pass. Built on LangGraph, with every model call going through the Anthropic SDK.

## Results

Scored by [agent-eval-gate](https://github.com/dholman7/agent-eval-gate) on the 20 **held-out** tasks of its vendor-intake benchmark. Nothing was tuned on these tasks. One trial each, Claude Haiku 5.5 at low effort.

| Agent | Tasks succeeded | Seeded bugs caught | Test files failing on correct code | Cost per task |
|---|---|---|---|---|
| **Storywright** | **95%** (19/20, CI 76–99%) | **92%** (22/24, CI 74–98%) | 5% (1/20) | $0.0015 |
| One Claude call, same model | 55% (11/20, CI 34–74%) | 54% (13/24, CI 35–72%) | 45% (9/20) | $0.0018 |
| The repo's existing tests | 0% | 17% (4/24) | 0% | – |

The intervals don't overlap: at 95% confidence, the loop is better than a single call, not just luckier. It's also slightly cheaper per task, although it makes more calls.

The prompts were tuned on the 12 dev tasks, where Storywright reached 83% (10/12) with no test files failing on correct code.

### What still goes wrong

- **REV-08: a false defect claim.** The test uploaded an insurance certificate labeling the date "Policy expires:", which the app doesn't read. The date came back empty, so the vendor wasn't flagged, and the agent blamed the app. Storywright only sees a fixed list of context files and can't look up the expected format in the repo. A file-reading tool is the planned fix.
- **REV-03: passing but too weak.** The test checked the order of only its own vendors in the review queue, so a queue sorted backwards went unnoticed. Catching that is exactly why agent-eval-gate scores bugs caught separately from tests passing.

## How it works

```
write ──> verify ──green──> passing
            │
            └─red──> repair ──test_bug──> verify
                       │   └─rejected (weakened)──> repair
                       └─app_defect / environment──> stop
```

| Step | What happens |
|---|---|
| **write** | One Claude call with the scenario plus repo context (OpenAPI document, test fixtures, an example spec). Returns typed JSON: `{approach, code}`. |
| **verify** | Deterministic. Type-checks the repo's tests, starts the app on a free port with a fresh database, runs the file, and collects each failing test's first error lines. |
| **repair** | One Claude call with the failures. Returns `{diagnosis, reason, code}`, where `diagnosis` is `test_bug`, `app_defect` (the app contradicts the scenario), or `environment` (nothing could run). |
| **guard** | Deterministic check on every repair. A repair that drops the task tag or cuts the number of assertions is rejected and retried without running it. |
| **stop** | After green, a defect report, an environment failure, 3 repairs, or the per-task budget. |

The agent hands over a test file only when it's green or when it documents a suspected app defect. It never hands over tests it couldn't get working.

Failure handling, in order of how often it came up:

- **Broken environment:** the agent says so and stops. On its first real run it correctly reported that the app couldn't start, caused by a bug in the eval harness's workspace copies, and left its tests untouched.
- **Bare status mismatches:** "expected 201, got 422" alone led the model to blame the app for rejecting test data it had chosen. The prompts now require the response body on every status assertion, and a defect only counts when the app contradicts the scenario.
- **Model errors**, such as refusals, truncated output, or invalid JSON, end the run as `agent_error`. The SDK retries rate limits and server errors.

## Run it

```bash
uv sync
export ANTHROPIC_API_KEY=...
storywright run --repo ../vendor-intake-app --story story.feature --task-id VEN-01
```

Through agent-eval-gate:

```bash
uv run aeg run --agent storywright --command "uv run --project ../storywright storywright aeg" --split heldout
```

`STORYWRIGHT_PROFILE` picks the model, and the cheap one is the default:

| Profile | Model and effort | Use for |
|---|---|---|
| `dev` (default) | `claude-haiku-5-5`, low | Iterating; a full held-out run costs about 3 cents |
| `report` | `claude-opus-5-5`, medium | Comparison runs, with server-side refusal fallback on |

`--max-repairs` (default 3) and `--budget-usd` (default $1.00 per task) bound every run. How to start the app and run tests comes from a config file; [`configs/vendor-intake.yaml`](configs/vendor-intake.yaml) is the one for the benchmark's target.

## Not in v1

- Reading files beyond the configured context (the REV-08 failure)
- Production code changes; Storywright writes tests only
- Jira input and opening pull requests; stories come from files for now
- Multiple trials per task, and an Opus `report` run on the held-out split

## License

[MIT](LICENSE)
