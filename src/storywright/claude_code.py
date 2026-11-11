"""Storywright's model calls through headless Claude Code (`claude -p`), billed to a subscription.

Selected with STORYWRIGHT_TRANSPORT=claude-code, for local runs that shouldn't spend API credits.
Each `call()` is one `claude -p` process with no built-in tools, no user or project settings or
skills, Storywright's system prompt, and Storywright's tools served by `storywright.tool_server`
under the same budget. Differences from the API path, which matter when comparing numbers:
- Claude Code adds a short identity line and an environment section to the context.
- Structured output arrives through a tool call: one extra turn.
- Claude Code places its own cache breakpoints (1-hour TTL); there is no refusal fallback.
- Repo tools are named `mcp__storywright__<tool>`.
- `cost_usd` is Claude Code's list-price estimate of the same requests. A subscription bills
  none of it, but it keeps the per-run budget and cost columns comparable.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from storywright.llm import LLMError, Spend, resolve
from storywright.tool_server import NAME

TIMEOUT_S = 900
# Claude Code bills an API key when it finds one; without one it uses the claude.ai login.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Without this, Claude Code sends the whole prompt again, uncached, for a side request (such as
# a session title): about double the input per call. `--safe-mode` would also drop
# --mcp-config servers, which the tools need, so isolation is by explicit flags instead.
ADDED_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
# Refused tool calls tolerated past the budget, matching the API loop's extra rounds.
EXTRA_ROUNDS = 3


class ClaudeCode:
    def __init__(
        self,
        repo: Path | None,
        max_tool_calls: int = 12,
        mcp_servers: list[str] | None = None,
        mcp_env: list[str] | None = None,
        binary: str | None = None,
    ) -> None:
        self.repo = repo
        self.max_tool_calls = max_tool_calls
        self.mcp_servers = mcp_servers or []
        self.mcp_env = mcp_env or []
        self.binary = binary or os.environ.get("STORYWRIGHT_CLAUDE", "claude")
        self.model, self.effort = resolve()
        self.spend = Spend()
        self.tool_calls = 0

    def mcp_config(self, state: Path) -> dict[str, Any]:
        args = ["-m", "storywright.tool_server"]
        if self.repo is not None:
            args += ["--root", str(self.repo)]
        args += ["--max-calls", str(self.max_tool_calls), "--state", str(state)]
        for spec in self.mcp_servers:
            args += ["--mcp-server", spec]
        for name in self.mcp_env:
            args += ["--mcp-env", name]
        env = {k: os.environ[k] for k in self.mcp_env if k in os.environ}
        return {
            "mcpServers": {
                NAME: {"type": "stdio", "command": sys.executable, "args": args, "env": env}
            }
        }

    def command(self, system_file: Path, schema: dict[str, Any], config: Path | None) -> list[str]:
        command = [
            self.binary, "-p",
            "--model", self.model,
            "--effort", self.effort,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt-file", str(system_file),
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]  # fmt: skip
        if config is not None:
            command += ["--mcp-config", str(config), "--allowedTools", f"mcp__{NAME}"]
        return command

    def _run(self, command: list[str], prompt: str, cwd: Path) -> str:
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV} | ADDED_ENV
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(prompt, timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # The tool server is in the same process group.
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise LLMError(f"claude -p timed out after {TIMEOUT_S}s") from None
        if not stdout.strip():
            raise LLMError(f"claude -p exited {process.returncode}: {stderr.strip()[-500:]}")
        return stdout

    def _record(self, result: dict[str, Any]) -> None:
        for model, usage in (result.get("modelUsage") or {}).items():
            self.spend.add_tokens(
                model,
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
                usage.get("cacheCreationInputTokens", 0),
                usage.get("cacheReadInputTokens", 0),
                usage.get("costUSD"),
            )

    def call(
        self, system: str, context: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="storywright-") as tmp:
            directory = Path(tmp)
            system_file = directory / "system.md"
            # The API path caches system + context as one prefix; here it's one system prompt.
            system_file.write_text(f"{system}\n\n{context}")
            state = directory / "tools.json"
            config: Path | None = None
            if self.repo is not None or self.mcp_servers:
                config = directory / "mcp.json"
                config.write_text(json.dumps(self.mcp_config(state)))
            stdout = self._run(self.command(system_file, schema, config), prompt, directory)
            counts = json.loads(state.read_text()) if state.is_file() else {}

        try:
            result: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"claude -p printed no JSON result: {stdout[-300:]!r}") from exc
        self._record(result)
        self.tool_calls += counts.get("calls", 0)
        if counts.get("refused", 0) > EXTRA_ROUNDS:
            raise LLMError("kept calling tools after the tool budget was used up")
        if result.get("stop_reason") == "refusal":
            raise LLMError("the model declined the request")
        if result.get("is_error") or result.get("subtype") != "success":
            detail = result.get("api_error_status") or str(result.get("result", ""))[:300]
            raise LLMError(f"claude -p {result.get('subtype')}: {detail}")
        data = result.get("structured_output")
        if not isinstance(data, dict):
            raise LLMError("claude -p returned no structured output")
        return data
