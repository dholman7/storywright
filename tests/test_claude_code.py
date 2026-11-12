"""The claude -p transport and its tool server, against a fake `claude` binary: no model calls."""

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from storywright.claude_code import EXTRA_ROUNDS, ClaudeCode
from storywright.llm import LLMError, Tools
from storywright.mcp_tools import ServerSpec, connect
from storywright.tool_server import build, exposed_name
from storywright.tools import Toolbox, ToolError

# Records what it was given, optionally calls read_file through the real tool server named in
# --mcp-config, then prints FAKE_RESULT (with the tool's output as structured_output.text).
FAKE = """#!{python}
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
record = {{
    "argv": argv,
    "stdin": sys.stdin.read(),
    "system": Path(flag("--system-prompt-file")).read_text(),
    "saw_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "nonessential": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
}}
Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
result = json.loads(os.environ["FAKE_RESULT"])
config = flag("--mcp-config")
if config and os.environ.get("FAKE_CALL_TOOL"):
    import anyio
    from mcp import Client, StdioServerParameters

    server = json.loads(Path(config).read_text())["mcpServers"]["storywright"]
    params = StdioServerParameters(
        command=server["command"], args=server["args"], env=server["env"]
    )

    async def call():
        async with Client(params) as client:
            out = await client.call_tool("read_file", {{"path": "story.txt", "start_line": 1}})
            return out.content[0].text

    result["structured_output"] = {{"text": anyio.run(call)}}
if config and os.environ.get("FAKE_STATE"):
    server = json.loads(Path(config).read_text())["mcpServers"]["storywright"]
    state = server["args"][server["args"].index("--state") + 1]
    Path(state).write_text(os.environ["FAKE_STATE"])
print(os.environ.get("FAKE_STDOUT") or json.dumps(result))
"""

SUCCESS = {
    "subtype": "success",
    "is_error": False,
    "stop_reason": "tool_use",
    "structured_output": {"approach": "a", "code": "b"},
    "modelUsage": {
        "claude-haiku-5-5": {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheCreationInputTokens": 1000,
            "cacheReadInputTokens": 2000,
            "costUSD": 0.0042,
        }
    },
}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "claude"
    binary.write_text(FAKE.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(SUCCESS))
    monkeypatch.setenv("STORYWRIGHT_CLAUDE", str(binary))
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "story.txt").write_text("Given a vendor\n")
    return record


def _claude(tmp_path: Path) -> ClaudeCode:
    return ClaudeCode(tmp_path / "repo", max_tool_calls=2)


def test_a_call_runs_headless_without_the_api_key_or_user_settings(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-seen")

    data = _claude(tmp_path).call("SYSTEM", "CONTEXT", "PROMPT", {"type": "object"})

    record = json.loads(fake.read_text())
    argv = record["argv"]
    assert data == {"approach": "a", "code": "b"}
    assert not record["saw_api_key"]  # with a key, Claude Code would bill it
    assert record["nonessential"] == "1"  # no uncached side request repeating the prompt
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools
    assert argv[argv.index("--setting-sources") + 1] == ""  # no user or project settings
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(
        argv
    )
    assert argv[argv.index("--allowedTools") + 1] == "mcp__storywright"
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert record["stdin"] == "PROMPT"
    assert record["system"] == "SYSTEM\n\nCONTEXT"


def test_tools_go_through_the_real_tool_server_and_usage_is_recorded(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CALL_TOOL", "1")
    claude = _claude(tmp_path)

    data = claude.call("system", "context", "prompt", {"type": "object"})

    assert data == {"text": "1: Given a vendor"}
    assert claude.tool_calls == 1  # counted by the server, read back from its state file
    assert claude.spend.input_tokens == 3100  # all input, cached or not
    assert claude.spend.cache_read_tokens == 2000
    assert claude.spend.cache_write_tokens == 1000
    assert claude.spend.output_tokens == 50
    assert claude.spend.cost_usd == pytest.approx(0.0042)  # Claude Code's own estimate
    assert claude.spend.models == {"claude-haiku-5-5"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"subtype": "error_during_execution", "is_error": True}, "error_during_execution"),
        ({"stop_reason": "refusal"}, "declined"),
        ({"structured_output": None}, "no structured output"),
    ],
)
def test_failed_results_raise_after_recording_spend(
    fake: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setenv("FAKE_RESULT", json.dumps({**SUCCESS, **change}))
    claude = _claude(tmp_path)

    with pytest.raises(LLMError, match=message):
        claude.call("system", "context", "prompt", {"type": "object"})
    assert claude.spend.cost_usd == pytest.approx(0.0042)


def test_output_that_is_not_a_json_result_raises(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STDOUT", "Invalid API key · Please run /login")

    with pytest.raises(LLMError, match="no JSON result"):
        _claude(tmp_path).call("system", "context", "prompt", {"type": "object"})


@pytest.mark.parametrize(("refused", "raises"), [(EXTRA_ROUNDS, False), (EXTRA_ROUNDS + 1, True)])
def test_calling_tools_long_past_the_budget_fails_like_the_api_loop(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: int, raises: bool
) -> None:
    monkeypatch.setenv("FAKE_STATE", json.dumps({"calls": 2, "refused": refused}))
    claude = _claude(tmp_path)

    if raises:
        with pytest.raises(LLMError, match="kept calling tools"):
            claude.call("system", "context", "prompt", {"type": "object"})
    else:
        claude.call("system", "context", "prompt", {"type": "object"})
    assert claude.tool_calls == 2


def test_the_tool_server_serves_the_repo_tools_under_the_budget(tmp_path: Path) -> None:
    (tmp_path / "story.txt").write_text("Given a vendor\n")
    state = tmp_path / "state.json"
    server = build(Tools(Toolbox(tmp_path), None, max_calls=1), state)

    with connect([ServerSpec("sw", server)]) as mcp:
        assert mcp is not None
        names = {d["name"] for d in mcp.definitions}
        first = mcp.run("mcp__sw__read_file", {"path": "story.txt", "start_line": 1})
        with pytest.raises(ToolError, match="budget used up"):
            mcp.run("mcp__sw__read_file", {"path": "story.txt", "start_line": 1})

    assert names == {"mcp__sw__list_files", "mcp__sw__read_file", "mcp__sw__search"}
    assert first == "1: Given a vendor"
    assert json.loads(state.read_text()) == {"calls": 1, "refused": 1}


def test_proxied_mcp_tools_keep_their_server_name_under_the_bridge() -> None:
    assert exposed_name("mcp__ci__latest_failure") == "ci__latest_failure"
    assert exposed_name("read_file") == "read_file"
