# 4. Call Claude through the Anthropic SDK, not LangChain's chat models

Status: accepted, 2026-11-12 (backfilled: decided in v0.1)

## Context

LangGraph pairs naturally with LangChain's model wrappers. Storywright depends on Claude features in their current form:

- structured output (`output_config.format`);
- prompt caching with explicit breakpoints;
- effort levels;
- server-side refusal fallback on Opus and Sonnet;
- thinking blocks passed back unchanged in the tool loop.

## Decision

LangGraph orchestrates; every model call goes through the `anthropic` SDK directly, in `llm.py`. That module exposes one method, `call(system, context, prompt, schema)`. The graph only sees that method, so a test can swap in a scripted model, and the headless Claude Code transport can stand in for the SDK without the graph changing.

## Alternatives

- **`langchain-anthropic`.** It's one more layer between the code and the API. New parameters reach it later than they reach the SDK, and cache and usage fields have to be mapped back.
- **No framework at all.** A reasonable choice for the model calls, which is what this ADR does. The graph is the part LangGraph earns (ADR 0003).

## Consequences

- **Cost accounting reads the API's own usage fields,** including cache reads and writes, priced from a table checked against the pricing page.
- **Moving to another model provider would mean a new `llm.py`.** That's acceptable. The benchmark exists to measure exactly that kind of change.
