# 5. Haiku by default, Opus to report

Status: accepted, 2026-11-12 (backfilled: decided in v0.2)

## Context

Most runs are iteration: a prompt change, a guard probe, a dev-split check. A few are the numbers a README or a hiring manager reads. One model for both is either too expensive to iterate on or too weak to report.

## Decision

Two profiles, picked with `STORYWRIGHT_PROFILE`:

- **`dev`** (default): Claude Haiku 5.5, low effort.
- **`report`**: Claude Opus 5.5, medium effort, with server-side refusal fallback.

`STORYWRIGHT_MODEL` and `STORYWRIGHT_EFFORT` override either part. Published comparisons say which profile they used.

## Evidence

| | Haiku (`dev`) | Opus (`report`) |
|---|---|---|
| v3 blind, bugs caught | 86% | 95% (paired p = 0.125) |
| v3 blind, cost per trial | $0.0030 | $0.093 |
| v2 held-out, tasks succeeded | 97% | 100% (1 trial per task) |

Haiku gets within about 10 points of Opus for about 3% of the cost, so iteration happens on Haiku. A change that holds there is then confirmed on Opus, and the Opus run is the one quoted.

## Consequences

- **The default run costs under a cent,** so the benchmark can run on every change.
- **Some differences only show on one model.** In the prompt-injection red team, Opus followed a story's "add this comment" 3/3 times and Haiku never did. Security claims are measured on both.
