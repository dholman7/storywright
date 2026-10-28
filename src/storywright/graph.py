"""The agent as a LangGraph state machine: write -> verify -> (repair -> verify)* -> outcome.

write --> verify --green--> passing
             |
             +--red--> repair --test_bug--> verify
                          |  \--rejected (weakened)--> repair
                          +--app_defect / environment --> stop
Every loop is bounded by max_repairs and a per-task budget.
"""

import operator
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, TypedDict, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from storywright import prompts
from storywright.guards import weakening
from storywright.llm import LLM, LLMError
from storywright.repo import Verification

Verdict = Literal["passing", "app_defect", "environment", "gave_up", "over_budget", "agent_error"]


class State(TypedDict, total=False):
    task_id: str
    story: str
    tag: str
    code: str
    repairs: int
    verification: dict[str, Any]
    rejected: str | None
    verdict: Verdict
    reason: str
    # Appended by each node; the run's audit trail.
    log: Annotated[list[str], operator.add]


Verifier = Callable[[str, str], Verification]  # (task_id, code) -> result


class Node(Protocol):
    # LangGraph matches node functions by a parameter named `state`; a bare Callable has none.
    def __call__(self, state: State) -> State: ...


def _report(v: Verification) -> str:
    parts = []
    if not v.typechecks:
        parts.append("Type errors:\n" + "\n".join(v.typecheck_errors))
    if v.errors:
        parts.append("Run errors:\n" + "\n".join(v.errors))
    if v.failures:
        parts.append(f"{v.failed} failed, {v.passed} passed:\n" + "\n".join(v.failures))
    if not parts:
        parts.append(f"{v.passed} passed, but no passing test carries the required tag.")
    return "\n\n".join(parts)


def build(
    llm: LLM,
    context: str,
    test_dir: str,
    verifier: Verifier,
    *,
    max_repairs: int = 3,
    budget_usd: float = 1.00,
) -> CompiledStateGraph[State, None, State, State]:
    def write(state: State) -> State:
        path = f"{test_dir}/{state['task_id']}.spec.ts"
        try:
            data = llm.call(
                prompts.SYSTEM,
                context,
                prompts.write_prompt(state["story"], path, state["tag"]),
                prompts.WRITE_SCHEMA,
            )
        except LLMError as exc:
            return {"verdict": "agent_error", "reason": str(exc), "log": [f"write failed: {exc}"]}
        return {"code": data["code"], "repairs": 0, "log": [f"plan: {data['approach']}"]}

    def verify(state: State) -> State:
        result = verifier(state["task_id"], state["code"])
        summary = f"verify: {result.passed} passed, {result.failed} failed"
        if not result.typechecks:
            summary += ", type errors"
        if result.errors:
            summary += ", run errors"
        return {"verification": result.model_dump(), "rejected": None, "log": [summary]}

    def repair(state: State) -> State:
        result = Verification.model_validate(state["verification"])
        prompt = prompts.repair_prompt(
            state["story"], state["code"], _report(result), state["tag"], state.get("rejected")
        )
        try:
            data = llm.call(prompts.SYSTEM, context, prompt, prompts.REPAIR_SCHEMA)
        except LLMError as exc:
            return {"verdict": "agent_error", "reason": str(exc), "log": [f"repair failed: {exc}"]}
        repairs = state["repairs"] + 1
        diagnosis = data["diagnosis"]
        if diagnosis != "test_bug":
            return {
                "verdict": diagnosis,
                "reason": data["reason"],
                "repairs": repairs,
                "log": [f"repair {repairs}: {diagnosis}: {data['reason']}"],
            }
        violation = weakening(state["code"], data["code"], state["tag"])
        if violation:
            return {
                "repairs": repairs,
                "rejected": violation,
                "log": [f"repair {repairs} rejected: {violation}"],
            }
        return {
            "code": data["code"],
            "repairs": repairs,
            # Clear any earlier rejection, or routing would treat this accepted repair as rejected.
            "rejected": None,
            "log": [f"repair {repairs}: test_bug: {data['reason']}"],
        }

    def stop(verdict: Verdict, reason: str) -> Node:
        def node(state: State) -> State:
            return {"verdict": verdict, "reason": reason, "log": [f"stop: {verdict}"]}

        return node

    def after_write(state: State) -> str:
        return END if state.get("verdict") else "verify"

    def after_verify(state: State) -> str:
        if Verification.model_validate(state["verification"]).green(state["tag"]):
            return "passing"
        return _next_attempt(state)

    def after_repair(state: State) -> str:
        if state.get("verdict"):
            return END
        return _next_attempt(state) if state.get("rejected") else "verify"

    def _next_attempt(state: State) -> str:
        if state["repairs"] >= max_repairs:
            return "gave_up"
        if llm.spend.cost_usd >= budget_usd:
            return "over_budget"
        return "repair"

    # Explicit parameters: left to inference, the unused context type becomes Never.
    graph: StateGraph[State, None, State, State] = StateGraph(State)
    graph.add_node("write", write)
    graph.add_node("verify", verify)
    graph.add_node("repair", repair)
    graph.add_node("passing", stop("passing", "every test passes against the real app"))
    graph.add_node("gave_up", stop("gave_up", f"still failing after {max_repairs} repairs"))
    graph.add_node("over_budget", stop("over_budget", f"spent the ${budget_usd:.2f} budget"))
    graph.add_edge(START, "write")
    graph.add_conditional_edges("write", after_write, ["verify", END])
    graph.add_conditional_edges(
        "verify", after_verify, ["passing", "repair", "gave_up", "over_budget"]
    )
    graph.add_conditional_edges(
        "repair", after_repair, ["verify", "repair", "gave_up", "over_budget", END]
    )
    for terminal in ("passing", "gave_up", "over_budget"):
        graph.add_edge(terminal, END)
    # The checkpointer lets a run be inspected step by step and resumed by thread id.
    return graph.compile(checkpointer=InMemorySaver())


def run(
    graph: CompiledStateGraph[State, None, State, State], task_id: str, story: str, tag: str
) -> State:
    config: Any = {"configurable": {"thread_id": task_id}, "recursion_limit": 50}
    start: State = {"task_id": task_id, "story": story, "tag": tag, "log": []}
    # invoke() is typed as returning a plain dict; it is the final graph state.
    return cast(State, graph.invoke(start, config))


def test_path(repo: Path, test_dir: str, task_id: str) -> Path:
    return repo / test_dir / f"{task_id}.spec.ts"
