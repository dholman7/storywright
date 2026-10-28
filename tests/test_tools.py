"""Repo tools and the bounded tool loop, with a scripted client: no API calls."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from storywright.llm import Claude, LLMError
from storywright.tools import Toolbox, ToolError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "insurance.txt").write_text(
        "Insurer: Harbor\nExpiry Date: 2027-03-31\n"
    )
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "extraction.py").write_text('"expiry_date": ("expiry_date", _date),\n')
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("Expiry Date: secret\n")
    (tmp_path.parent / "outside.txt").write_text("not yours")
    return tmp_path


def test_list_read_and_search_find_the_expected_format(repo: Path) -> None:
    tools = Toolbox(repo)

    assert tools.list_files("samples", "*.txt") == "samples/insurance.txt"
    assert "2: Expiry Date: 2027-03-31" in tools.read_file("samples/insurance.txt", 1)
    hits = tools.search(r"Expiry Date", ".")
    assert "samples/insurance.txt:2:" in hits
    assert "node_modules" not in hits  # excluded directories are never searched


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("read_file", {"path": "../outside.txt", "start_line": 1}, "outside the repository"),
        ("read_file", {"path": "node_modules/pkg/index.js", "start_line": 1}, "excluded"),
        ("list_files", {"path": "samples/insurance.txt", "glob": "*"}, "not a directory"),
        ("search", {"pattern": "(", "path": "."}, "invalid regular expression"),
        ("delete_file", {"path": "x"}, "unknown tool"),
    ],
)
def test_bad_tool_calls_become_errors_not_crashes(
    repo: Path, name: str, args: dict[str, Any], message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        Toolbox(repo).run(name, args)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )


def _tool_use(block_id: str, name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=args)


def _response(stop: str, content: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-haiku-5-5", stop_reason=stop, usage=_usage(), content=content
    )


class ScriptedMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        # Snapshot the conversation: the loop keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return next(self.responses)


def _claude(
    repo: Path, responses: list[Any], max_tool_calls: int = 12
) -> tuple[Claude, ScriptedMessages]:
    messages = ScriptedMessages(responses)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return Claude(client, toolbox=Toolbox(repo), max_tool_calls=max_tool_calls), messages


def test_tool_loop_runs_tools_then_returns_the_final_json(repo: Path) -> None:
    thinking = SimpleNamespace(type="thinking", thinking="")
    claude, messages = _claude(
        repo,
        [
            _response(
                "tool_use",
                [
                    thinking,
                    _tool_use(
                        "t1", "read_file", {"path": "samples/insurance.txt", "start_line": 1}
                    ),
                ],
            ),
            _response(
                "end_turn", [SimpleNamespace(type="text", text='{"approach": "a", "code": "b"}')]
            ),
        ],
    )

    data = claude.call("system", "context", "prompt", {"type": "object"})

    assert data == {"approach": "a", "code": "b"}
    assert claude.tool_calls == 1
    second = messages.calls[1]["messages"]
    assert second[1]["content"][0] is thinking  # assistant content passed back unchanged
    result = second[2]["content"][0]
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert "Expiry Date: 2027-03-31" in result["content"]
    assert messages.calls[0]["tools"][0]["strict"] is True


def test_tool_budget_refuses_extra_calls_so_the_model_must_answer(repo: Path) -> None:
    call = _tool_use("t", "list_files", {"path": ".", "glob": "*"})
    claude, messages = _claude(
        repo,
        [
            _response("tool_use", [call]),
            _response("tool_use", [call]),
            _response("end_turn", [SimpleNamespace(type="text", text="{}")]),
        ],
        max_tool_calls=1,
    )

    claude.call("system", "context", "prompt", {"type": "object"})

    refused = messages.calls[2]["messages"][-1]["content"][0]
    assert refused["is_error"] and "budget used up" in refused["content"]
    assert claude.tool_calls == 1


def test_a_model_that_never_stops_calling_tools_is_cut_off(repo: Path) -> None:
    call = _tool_use("t", "list_files", {"path": ".", "glob": "*"})
    claude, _ = _claude(repo, [_response("tool_use", [call])] * 10, max_tool_calls=1)

    with pytest.raises(LLMError, match="kept calling tools"):
        claude.call("system", "context", "prompt", {"type": "object"})
