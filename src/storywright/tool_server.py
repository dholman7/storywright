"""Storywright's tools as one stdio MCP server, for runs through headless Claude Code.

Claude Code calls these instead of its built-in tools, so a run sees the same repo tools, the
same MCP tools, and the same per-call budget as one through the API (`llm.Tools`). After each
call the counts are written to `--state` for the parent to read.

python -m storywright.tool_server [--root REPO] --max-calls 12 --state FILE
    [--mcp-server NAME=COMMAND ...] [--mcp-env VAR ...]
"""

import argparse
import json
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from storywright.llm import Tools
from storywright.mcp_tools import ServerSpec, connect
from storywright.tools import Toolbox

NAME = "storywright"


def exposed_name(name: str) -> str:
    """`mcp__ci__get_run` -> `ci__get_run`: Claude Code adds its own `mcp__storywright__`."""
    return name.removeprefix("mcp__")


def build(tools: Tools, state: Path) -> Server[Any]:
    by_name = {exposed_name(d["name"]): d for d in tools.definitions}

    def save() -> None:
        tmp = state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"calls": tools.calls, "refused": tools.refused}))
        tmp.replace(state)

    async def list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=name, description=d["description"], input_schema=d["input_schema"])
                for name, d in by_name.items()
            ]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        definition = by_name.get(params.name)
        name = definition["name"] if definition else params.name
        # MCP tools block on their own event loop (a portal thread), so keep them off this one.
        content, is_error = await anyio.to_thread.run_sync(
            tools.run, name, dict(params.arguments or {})
        )
        save()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=content)], is_error=is_error
        )

    save()
    return Server(NAME, on_list_tools=list_tools, on_call_tool=call_tool)


async def _serve(server: Server[Any]) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="storywright.tool_server")
    parser.add_argument("--root", type=Path, help="repo for the repo tools; omit for none")
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--mcp-server", action="append", default=[])
    parser.add_argument("--mcp-env", action="append", default=[])
    args = parser.parse_args(argv)

    specs = [ServerSpec.parse(s, args.mcp_env) for s in args.mcp_server]
    with connect(specs) as mcp:
        tools = Tools(Toolbox(args.root) if args.root else None, mcp, args.max_calls)
        anyio.run(_serve, build(tools, args.state))


if __name__ == "__main__":
    main()
