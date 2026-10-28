# Storywright

An agent that turns a user story into Playwright API tests and proves they work: it reads the repo, writes the tests, runs them against the real app, reads what failed, and repairs them, without weakening an assertion to get a pass. Built on LangGraph, with every model call going through the Anthropic SDK.

## Results

Scored by [agent-eval-gate](https://github.com/dholman7/agent-eval-gate) on its vendor-intake benchmark. Claude Haiku 5.5 at low effort, **3 trials per task** on the 20 held-out tasks:

| Agent | Trials succeeded | All 3 trials succeed | Seeded bugs caught | Failing on correct code | Cost per trial |
|---|---|---|---|---|---|
| **Storywright v0.2** | **97%** (58/60, CI 89–99%) | **95%** (19/20) | **97%** (70/72, CI 90–99%) | 3% (2/60) | $0.0028 |
| One Claude call, same model | 60% (36/60, CI 47–71%) | 55% (11/20) | 60% (43/72, CI 48–70%) | 39% (23/59) | $0.0018 |

The intervals are far apart: the loop is better than a single call by a wide, measurable margin. The whole held-out run cost $0.17. On the 12 dev tasks, v0.2 succeeded on all 36 trials.

On the `report` profile (Claude Opus 5.5, medium effort, 1 trial per task), v0.2 succeeded on all 20 held-out tasks and caught all 24 seeded bugs, with no test file failing on correct code. That run cost $0.091 per task, $1.81 in total. Haiku gets within a few points of that for about 3% of the cost, so the cheap profile is the default.

**A note on fairness.** The prompts were tuned on the dev split. v0.1 was then scored once on held-out (19/20 tasks, 1 trial). v0.2 added the repo tools after that score was seen, to fix a failure it exposed, so v0.2's held-out numbers come from fresh trials but aren't a blind result.

### What still goes wrong

- **REV-03: the agent is right, the spec is wrong.** The scenario says the queue's first vendor has "lowest confidence 0.40", but the app scores an unparseable field 0.30 and never produces 0.40. In two of three trials Storywright kept the scenario's number and reported a defect rather than changing the expectation to match the app. The fix belongs in the scenario text, which would mean a new benchmark version.
- **v0.1's REV-08 false defect claim is fixed** by the repo tools. With no way to read the repo, v0.1 invented a document format the app doesn't parse. v0.2 looks up the format first. In the live smoke test it made 6 tool calls, passed on its first run, and caught the seeded bug.

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
| **write** | Claude gets the scenario plus repo context (OpenAPI document, test fixtures, an example spec) and may read more of the repo with tools. Returns typed JSON: `{approach, code}`. |
| **verify** | Deterministic. Type-checks the repo's tests, starts the app on a free port with a fresh database, runs the file, and collects each failing test's first error lines. |
| **repair** | Claude gets the failures, may use the same tools, and returns `{diagnosis, reason, code}`, where `diagnosis` is `test_bug`, `app_defect` (the app contradicts the scenario), or `environment` (nothing could run). |
| **guard** | Deterministic check on every repair. A repair that drops the task tag or cuts the number of assertions is rejected and retried without running it. |
| **stop** | After green, a defect report, an environment failure, 3 repairs, or the per-task budget. |

**Repo tools** (`list_files`, `read_file`, `search`) are read-only and confined to the repo. They never touch dependencies, caches, `.git`, or runtime data, and paths that escape the repo are refused. Each model call may make up to 12 tool calls. Past that, every call returns an error asking for an answer, and a model that still keeps calling tools ends the run. v0.2 averaged 3.1 tool calls per trial.

The agent hands over a test file only when it's green or when it documents a suspected app defect. It never hands over tests it couldn't get working.

Failure handling, in order of how often it came up:

- **Broken environment:** the agent says so and stops. On its first real run it correctly reported that the app couldn't start, caused by a bug in the eval harness's workspace copies, and left its tests untouched.
- **Bare status mismatches:** "expected 201, got 422" alone led the model to blame the app for rejecting test data it had chosen. The prompts now require the response body on every status assertion, and a defect only counts when the app contradicts the scenario.
- **Model errors**, such as refusals, truncated output, or invalid JSON, end the run as `agent_error`. Tool errors go back to the model as error results instead of crashing the run. The SDK retries rate limits and server errors.

## Run it

```bash
uv sync
export ANTHROPIC_API_KEY=...
storywright run --repo ../vendor-intake-app --story story.feature --task-id VEN-01
```

Through agent-eval-gate:

```bash
uv run aeg run --agent storywright --command "uv run --project ../storywright storywright aeg" --split heldout --trials 3
```

`STORYWRIGHT_PROFILE` picks the model, and the cheap one is the default:

| Profile | Model and effort | Use for |
|---|---|---|
| `dev` (default) | `claude-haiku-5-5`, low | Iterating; 3 trials over the held-out split cost about 17 cents |
| `report` | `claude-opus-5-5`, medium | Comparison runs, with server-side refusal fallback on |

`--max-repairs` (default 3) and `--budget-usd` (default $1.00 per task) bound every run. How to start the app and run tests comes from a config file; [`configs/vendor-intake.yaml`](configs/vendor-intake.yaml) is the one for the benchmark's target.

## Not in v1

- Production code changes; Storywright writes tests only
- Jira input and opening pull requests; stories come from files for now
- Multiple trials on the `report` profile

## License

[MIT](LICENSE)
