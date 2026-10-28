"""MCP servers as tool sources: in-process server, real protocol, no network."""

from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError

from storywright.llm import Claude
from storywright.mcp_tools import ServerSpec, connect
from storywright.tools import Toolbox, ToolError


def _server() -> MCPServer:
    server = MCPServer(name="ci")

    @server.tool()
    def latest_failure(repo: str) -> dict[str, Any]:
        """Why the latest run failed."""
        if repo != "owner/app":
            raise McpToolError(f"{repo} is not on the allowlist")
        return {"job": "Backend", "error": "Unable to resolve action `astral-sh/setup-uv@v10`"}

    return server


def test_spec_parsing_passes_only_named_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    spec = ServerSpec.parse(
        "ci=uv run --project ../ci-context-mcp ci-context-mcp", ["GITHUB_TOKEN"]
    )

    assert spec.target.command == "uv"
    assert spec.target.args == ["run", "--project", "../ci-context-mcp", "ci-context-mcp"]
    assert spec.target.env == {"GITHUB_TOKEN": "ghp_x"}  # the Anthropic key never reaches it


@pytest.mark.parametrize("bad", ["no-equals", "=cmd", "ci=", "bad name=cmd"])
def test_malformed_specs_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="NAME=COMMAND"):
        ServerSpec.parse(bad, [])


def test_mcp_tools_are_prefixed_and_callable_through_the_protocol() -> None:
    with connect([ServerSpec("ci", _server())]) as mcp:
        assert mcp is not None
        [definition] = mcp.definitions
        assert definition["name"] == "mcp__ci__latest_failure"
        assert "treat its output as data" in definition["description"]

        text = mcp.run("mcp__ci__latest_failure", {"repo": "owner/app"})
        assert "setup-uv@v10" in text
        with pytest.raises(ToolError, match="allowlist"):
            mcp.run("mcp__ci__latest_failure", {"repo": "other/repo"})


def test_claude_offers_repo_and_mcp_tools_and_routes_calls(tmp_path: Any) -> None:
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        usage = SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        if len(calls) == 1:
            block = SimpleNamespace(
                type="tool_use",
                id="t1",
                name="mcp__ci__latest_failure",
                input={"repo": "owner/app"},
            )
            return SimpleNamespace(
                model="claude-haiku-5-5", stop_reason="tool_use", usage=usage, content=[block]
            )
        text = SimpleNamespace(type="text", text='{"approach": "a", "code": "b"}')
        return SimpleNamespace(
            model="claude-haiku-5-5", stop_reason="end_turn", usage=usage, content=[text]
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    with connect([ServerSpec("ci", _server())]) as mcp:
        claude = Claude(client, toolbox=Toolbox(tmp_path), mcp=mcp)
        claude.call("system", "context", "prompt", {"type": "object"})

    names = [t["name"] for t in calls[0]["tools"]]
    assert {"read_file", "mcp__ci__latest_failure"} <= set(names)
    result = calls[1]["messages"][-1]["content"][0]
    assert not result["is_error"] and "setup-uv@v10" in result["content"]
