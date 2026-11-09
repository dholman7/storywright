# 1. A structural repair guard, with evidence for changed expectations

Status: accepted, 2026-11-09

## Context

The repair step hands a model a red test and asks for a green one. The cheapest green is a test that checks less. v0.1's guard rejected a repair only if it dropped the tag (substring match) or lowered a regex count of `expect(`. That missed most ways to weaken a test: loosening `toBe` to `toBeTruthy`, flipping `.not`, wrapping an assertion in `try`/`catch`, `test.skip`, asserting on a constant, renaming the tag to a superstring, and changing an expected value to whatever the app returned. It also didn't count `expectStatus(...)`, the repo helper that carries the status checks that matter most.

## Decision

- **Parse, don't grep.** tree-sitter's TypeScript grammar gives an inventory of every assertion per test: kind (`expect`, `soft`, `poll`, or an `expect*` helper), subject, matcher, negation, whether a condition guards it, and its expected literals. Comments and strings can no longer fake an assertion or a tag.
- **Compare inventories.** Reject when the repair:
  - cuts the count;
  - skips a test;
  - adds a guarded, soft, or constant-subject assertion;
  - flips `.not`;
  - loosens a matcher on the same subject;
  - lowers the number of exact or partial checks overall. This catches a loosened check moved to a new subject.
- **Changed expected values need evidence.** The repair schema gains `evidence`: a verbatim quote from the scenario or the repo context. A changed literal passes only if the quote is found in those sources and contains the new value. A string the test's own setup sent may be expected back without a quote; a number may not.
- **Tightening is free only if it keeps the old value.** `toContain("validation")` → `toBe("validation_error")` passes; → `toBe("ok")` needs evidence.
- **Unparseable output falls back** to the old tag-and-count check. Verify fails it anyway.

## Alternatives considered

- **Ask a second model whether the repair weakened the test.** Non-deterministic, and costs a call per repair. The guard exists so that this decision doesn't depend on a model.
- **Run the repaired test against seeded bugs (mutation check).** The strongest test of "still catches the bug", but only possible where seeded bugs exist (the benchmark), not on a real Jira story.
- **Forbid any change to expected values.** Rejects legitimate fixes: the first draft often guesses a status the scenario never stated.
- **TypeScript compiler API in Node.** An exact AST, but it adds a Node process to the guard. tree-sitter's grammar is enough for call shapes and runs in-process.

## Consequences

- The README publishes the matrix: 30 weakening probes rejected and 13 legitimate repairs allowed, all run in CI.
- Known gaps: a new subject isn't checked for meaning, and a relevant-looking quote isn't checked for relevance. Both are stated in the README.
- The prompt now tells the model what the guard enforces, so a rejected repair is explained, not mysterious.
