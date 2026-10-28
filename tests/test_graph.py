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


def _run(llm: ScriptedLLM, results: list[Verification], **kwargs: Any) -> agent_graph.State:
    verifications = iter(results)
    compiled = agent_graph.build(
        llm, "<context/>", "e2e/api/generated", lambda task, code: next(verifications), **kwargs
    )
    return agent_graph.run(compiled, "VEN-01", "Scenario: Create a vendor", TAG)


def _write(code: str = GOOD) -> dict[str, Any]:
    return {"approach": "create then assert", "code": code}


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
