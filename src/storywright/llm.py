"""Claude calls with structured JSON output, model profiles, and cost accounting."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from storywright.tools import DEFINITIONS as TOOL_DEFINITIONS
from storywright.tools import Toolbox, ToolError

# (model, effort) per profile. `dev` is the default so routine runs stay cheap; publish numbers
# from `report`. STORYWRIGHT_MODEL and STORYWRIGHT_EFFORT override either part.
PROFILES: dict[str, tuple[str, str]] = {
    "dev": ("claude-haiku-5-5", "low"),
    "report": ("claude-opus-5-5", "medium"),
}

# Models that accept server-side refusal fallback (`fallbacks: "default"`). Haiku 5.5 has none.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

# USD per million tokens (input, output, cache write, cache read), Claude API pricing table
# (2026-10-06). Cache writes are 1.25x input (5-minute TTL). Cache-read rates not listed there
# assume the usual 10% of input.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
}


@dataclass
class Spend:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    models: set[str] = field(default_factory=set)

    def add(self, model: str, usage: Any) -> None:
        price = PRICES.get(model) or PRICES[resolve()[0]]
        cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        self.input_tokens += usage.input_tokens + cache_write + cache_read
        self.output_tokens += usage.output_tokens
        self.cost_usd += (
            usage.input_tokens * price[0]
            + usage.output_tokens * price[1]
            + cache_write * price[2]
            + cache_read * price[3]
        ) / 1_000_000
        self.models.add(model)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    spend: Spend

    def call(
        self, system: str, context: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Returns JSON matching `schema`. `context` is cached across calls in one run."""
        ...


def resolve() -> tuple[str, str]:
    profile = os.environ.get("STORYWRIGHT_PROFILE", "dev")
    if profile not in PROFILES:
        raise SystemExit(f"STORYWRIGHT_PROFILE must be one of {sorted(PROFILES)}, got {profile!r}")
    model, effort = PROFILES[profile]
    return os.environ.get("STORYWRIGHT_MODEL", model), os.environ.get("STORYWRIGHT_EFFORT", effort)


class Claude:
    def __init__(
        self, client: Any | None = None, toolbox: Toolbox | None = None, max_tool_calls: int = 12
    ) -> None:
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.model, self.effort = resolve()
        self.spend = Spend()
        self.toolbox = toolbox
        self.max_tool_calls = max_tool_calls
        self.tool_calls = 0

    def _create(self, params: dict[str, Any]) -> Any:
        import anthropic

        try:
            response = self.client.beta.messages.create(**params)
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc
        self.spend.add(response.model, response.usage)
        return response

    def _tool_results(self, response: Any, budget: int) -> tuple[list[dict[str, Any]], int]:
        """Runs each requested tool. Over budget, the call is answered with an error instead."""
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if self.toolbox is None:
                raise LLMError(f"model called {block.name!r} but no tools were offered")
            if budget <= 0:
                content, is_error = "Tool budget used up. Answer now with what you have.", True
            else:
                budget -= 1
                self.tool_calls += 1
                try:
                    content, is_error = self.toolbox.run(block.name, dict(block.input)), False
                except ToolError as exc:
                    content, is_error = f"Error: {exc}", True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        return results, budget

    def call(
        self, system: str, context: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            # Stable instructions and repo context first, so later calls hit the cache.
            "system": [
                {"type": "text", "text": system},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            ],
            "messages": messages,
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        if self.toolbox is not None:
            params["tools"] = TOOL_DEFINITIONS
        if self.model in FALLBACK_MODELS:
            params |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        budget = self.max_tool_calls
        # Each round either answers or calls tools; past the budget every call is refused, so
        # the model has to answer. The extra rounds leave room for that final answer.
        for _ in range(self.max_tool_calls + 3):
            response = self._create(params)
            if response.stop_reason != "tool_use":
                break
            # Append the content unchanged: thinking blocks must be passed back as received.
            messages.append({"role": "assistant", "content": response.content})
            results, budget = self._tool_results(response, budget)
            messages.append({"role": "user", "content": results})
        else:
            raise LLMError("kept calling tools after the tool budget was used up")

        if response.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"response was not valid JSON: {exc}") from exc
        return data
