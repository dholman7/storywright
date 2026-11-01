"""Routing through the graph with a scripted model and verifier: no API calls, no app."""

from collections.abc import Iterator
from typing import Any

import pytest

from storywright import graph as agent_graph
from storywright.guards import assertion_count, weakening
from storywright.llm import LLMError, Spend
from storywright.repo import Verification

TAG = "@VEN-01"
GOOD = f'test("a", {{ tag: "{TAG}" }}, async () => {{ expect(1).toBe(1); expect(2).toBe(2); }});'
WEAK = f'test("a", {{ tag: "{TAG}" }}, async () => {{ expect(1).toBe(1); }});'
GREEN = Verification(typechecks=True, passed=1, passed_tags=[TAG])
RED = Verification(typechecks=True, failed=1, failures=["a: Expected: 201 | Received: 422"])


class ScriptedLLM:
    def __init__(self, replies: list[dict[str, Any] | Exception], cost_per_call: float = 0.01):
        self.replies: Iterator[dict[str, Any] | Exception] = iter(replies)
        self.spend = Spend()
        self.cost_per_call = cost_per_call
        self.prompts: list[str] = []

    def call(
        self, system: str, context: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.spend.cost_usd += self.cost_per_call
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


DIRS = {"api": "e2e/api/generated", "ui": "e2e/ui/generated"}


def _run(
    llm: ScriptedLLM,
    results: list[Verification],
    seen: list[agent_graph.State] | None = None,
    **kwargs: Any,
) -> agent_graph.State:
    verifications = iter(results)

    def verifier(state: agent_graph.State) -> Verification:
        if seen is not None:
            seen.append(dict(state))  # type: ignore[arg-type]
        return next(verifications)

    compiled = agent_graph.build(llm, "<context/>", kwargs.pop("dirs", DIRS), verifier, **kwargs)
    return agent_graph.run(compiled, "VEN-01", "Scenario: Create a vendor", TAG)


def _write(code: str = GOOD, kind: str = "api", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "approach": "create then assert", "code": code, **extra}


def _repair(diagnosis: str = "test_bug", code: str = GOOD) -> dict[str, Any]:
    return {"diagnosis": diagnosis, "reason": "used a reserved .test email", "code": code}


def test_green_on_first_verify_needs_no_repair() -> None:
    final = _run(ScriptedLLM([_write()]), [GREEN])
    assert final["verdict"] == "passing"
    assert final["repairs"] == 0


def test_a_test_bug_is_repaired_and_reverified() -> None:
    llm = ScriptedLLM([_write(), _repair()])
    final = _run(llm, [RED, GREEN])
    assert final["verdict"] == "passing"
    assert final["repairs"] == 1
    assert "Received: 422" in llm.prompts[1]  # the repair saw the real failure


def test_app_defect_stops_and_keeps_the_failing_test() -> None:
    final = _run(ScriptedLLM([_write(), _repair("app_defect", code="")]), [RED])
    assert final["verdict"] == "app_defect"
    assert final["code"] == GOOD  # the evidence is kept, not rewritten


def test_a_weakening_repair_is_rejected_and_retried_without_reverifying() -> None:
    llm = ScriptedLLM([_write(), _repair(code=WEAK), _repair(code=GOOD)])
    final = _run(llm, [RED, GREEN])  # only two verifies: the rejected code never runs
    assert final["verdict"] == "passing"
    assert any("rejected: the repair cut assertions from 2 to 1" in line for line in final["log"])
    assert "previous repair was rejected" in llm.prompts[2]


def test_repairs_are_bounded() -> None:
    final = _run(ScriptedLLM([_write(), _repair(), _repair()]), [RED, RED, RED], max_repairs=2)
    assert final["verdict"] == "gave_up"
    assert final["repairs"] == 2


def test_budget_stops_the_loop() -> None:
    final = _run(ScriptedLLM([_write(), _repair()], cost_per_call=0.6), [RED, RED], budget_usd=1.0)
    assert final["verdict"] == "over_budget"


def test_model_errors_end_the_run_as_agent_error() -> None:
    final = _run(ScriptedLLM([LLMError("API 529: overloaded")]), [])
    assert final["verdict"] == "agent_error"
    assert "529" in final["reason"]


@pytest.mark.parametrize(
    ("after", "problem"),
    [
        (GOOD, None),
        (WEAK, "cut assertions from 2 to 1"),
        (GOOD.replace(TAG, "@X"), "removed the required tag"),
    ],
)
def test_weakening_guard(after: str, problem: str | None) -> None:
    result = weakening(GOOD, after, TAG)
    assert (result is None) if problem is None else (problem in (result or ""))


def test_assertion_count_includes_poll_and_soft() -> None:
    code = "expect(a).toBe(1); await expect.poll(f).toBe(2); expect.soft(b).toBe(3); notexpect(c)"
    assert assertion_count(code) == 3


PAGES = "export class VendorPage {\n  readonly submitButton: Locator;\n}\n"
PAGES_PLUS = (
    "export class VendorPage {\n  readonly submitButton: Locator;\n  readonly notice: Locator;\n}\n"
)
PAGES_CHANGED = "export class VendorPage {\n  readonly submit: Locator;\n}\n"


def test_the_model_picks_the_kind_and_the_verifier_runs_that_kind() -> None:
    seen: list[agent_graph.State] = []

    final = _run(ScriptedLLM([_write(kind="ui")]), [GREEN], seen)

    assert final["kind"] == "ui" and seen[0]["kind"] == "ui"
    assert "kind: ui" in final["log"]


def test_the_prompt_offers_only_the_allowed_kinds_and_their_files() -> None:
    llm = ScriptedLLM([_write()])

    _run(llm, [GREEN], dirs={"api": "e2e/api/generated"})

    assert "- api: e2e/api/generated/VEN-01.spec.ts" in llm.prompts[0]
    assert "ui" not in llm.prompts[0].split("Choose the kind")[1]


def test_an_additive_page_object_edit_is_verified_with_the_test() -> None:
    seen: list[agent_graph.State] = []
    llm = ScriptedLLM([_write(kind="ui", page_objects=PAGES_PLUS)])

    final = _run(llm, [GREEN], seen, page_objects=("e2e/fixtures/pages.ts", PAGES))

    assert seen[0]["page_objects"] == PAGES_PLUS
    assert final["page_objects"] == PAGES_PLUS


def test_a_page_object_edit_that_changes_existing_lines_is_dropped_at_write() -> None:
    seen: list[agent_graph.State] = []
    llm = ScriptedLLM([_write(kind="ui", page_objects=PAGES_CHANGED)])

    final = _run(llm, [GREEN], seen, page_objects=("e2e/fixtures/pages.ts", PAGES))

    assert seen[0]["page_objects"] == ""
    assert any("page-object edit rejected" in line for line in final["log"])


def test_a_repair_that_changes_existing_page_objects_is_rejected() -> None:
    llm = ScriptedLLM(
        [
            _write(kind="ui", page_objects=""),
            _repair() | {"page_objects": PAGES_CHANGED},
            _repair() | {"page_objects": PAGES_PLUS},
        ]
    )

    final = _run(llm, [RED, GREEN], page_objects=("e2e/fixtures/pages.ts", PAGES))

    assert final["verdict"] == "passing"
    assert any("rejected: the page-object edit changed" in line for line in final["log"])


def test_lint_errors_block_green_and_reach_the_repair() -> None:
    linted = Verification(
        typechecks=True, passed=1, passed_tags=[TAG], lint_errors=["line 4: PW-4: no passwords"]
    )
    llm = ScriptedLLM([_write(), _repair()])

    final = _run(llm, [linted, GREEN])

    assert final["verdict"] == "passing" and final["repairs"] == 1
    assert "Lint errors" in llm.prompts[1] and "PW-4" in llm.prompts[1]


def test_the_system_prompt_requires_the_body_on_status_assertions_without_a_repo_guide() -> None:
    # Regression: v0.3 dropped this rule, relying on the repo's AGENTS.md. Against an app commit
    # with no guide, bare status checks came back, and the model blamed the app for its own 422s.
    from storywright import prompts

    assert "every status assertion shows" in prompts.SYSTEM
    assert "await res.text()" in prompts.SYSTEM
