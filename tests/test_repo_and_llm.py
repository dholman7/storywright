import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from storywright import llm as llm_module
from storywright.repo import (
    RepoConfig,
    Verification,
    _lint,
    parse_report,
    read_context,
)
from storywright.repo import TestsConfig as LintSettings  # not collected as a test class

CONFIG = Path(__file__).parents[1] / "configs" / "vendor-intake.yaml"


def test_shipped_config_loads() -> None:
    config = RepoConfig.load(CONFIG)
    assert config.kinds["api"].test_dir == "e2e/api/generated"
    assert config.kinds["ui"].services == ["api", "web"]
    assert "{api_port}" in config.services["api"].command
    assert "{api_port}" in config.services["web"].env["API_ORIGIN"]


def test_read_context_adds_examples_for_the_kinds_and_skips_missing_files(tmp_path: Path) -> None:
    config = RepoConfig.load(CONFIG)
    (tmp_path / "e2e" / "ui").mkdir(parents=True)
    (tmp_path / "e2e" / "AGENTS.md").write_text("rules")
    (tmp_path / "e2e" / "ui" / "vendors.spec.ts").write_text("ui example")

    api_only = read_context(tmp_path, config, ["api"])
    both = read_context(tmp_path, config, ["api", "ui"])

    assert set(api_only) == {"e2e/AGENTS.md"}
    assert set(both) == {"e2e/AGENTS.md", "e2e/ui/vendors.spec.ts"}


def test_lint_runs_only_once_the_repo_has_adopted_the_rules(tmp_path: Path) -> None:
    messages = [
        {"line": 4, "message": "PW-4: no passwords", "severity": 2},
        {"line": 9, "message": "style", "severity": 1},
    ]
    report = json.dumps([{"messages": messages}])
    tests = LintSettings(
        cwd=".", typecheck="true", lint=f"echo '{report}'", lint_if="eslint.config.mjs"
    )
    spec = tmp_path / "a.spec.ts"

    before = _lint(tmp_path, tests, spec)
    (tmp_path / "eslint.config.mjs").write_text("export default []")
    after = _lint(tmp_path, tests, spec)

    assert before == []
    assert after == ["line 4: PW-4: no passwords"]  # warnings don't block


def test_parse_report_collects_failures_and_tags() -> None:
    verification = Verification(typechecks=True)
    report = {
        "suites": [
            {
                "specs": [
                    {"title": "ok", "tags": ["VEN-01"], "tests": [{"status": "expected"}]},
                    {
                        "title": "bad",
                        "tags": ["VEN-01"],
                        "tests": [
                            {
                                "status": "unexpected",
                                "results": [{"error": {"message": "Expected: 201\nReceived: 422"}}],
                            }
                        ],
                    },
                ]
            }
        ]
    }

    parse_report(report, verification)

    assert (verification.passed, verification.failed) == (1, 1)
    assert verification.passed_tags == ["@VEN-01"]
    assert verification.failures == ["bad: Expected: 201 | Received: 422"]
    assert not verification.green("@VEN-01")


class StubMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


def _client(text: str, model: str, stop_reason: str = "end_turn") -> Any:
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=8000,
        cache_read_input_tokens=0,
    )
    response = SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        usage=usage,
        content=[SimpleNamespace(type="text", text=text)],
    )
    return SimpleNamespace(beta=SimpleNamespace(messages=StubMessages(response)))


def test_dev_profile_uses_haiku_without_fallback_and_caches_context() -> None:
    client = _client('{"approach": "a", "code": "b"}', "claude-haiku-5-5")
    claude = llm_module.Claude(client)

    data = claude.call("system", "<context/>", "prompt", {"type": "object"})

    call = client.beta.messages.calls[0]
    assert data == {"approach": "a", "code": "b"}
    assert call["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in call
    assert call["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    # 1000 input + 500 output + 8000 cache-write tokens at Haiku 5.5 rates.
    assert claude.spend.cost_usd == pytest.approx((1000 * 0.10 + 500 * 0.50 + 8000 * 0.125) / 1e6)


def test_report_profile_uses_opus_with_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORYWRIGHT_PROFILE", "report")
    client = _client('{"approach": "a", "code": "b"}', "claude-opus-5-5")

    llm_module.Claude(client).call("system", "ctx", "prompt", {"type": "object"})

    call = client.beta.messages.calls[0]
    assert (call["model"], call["output_config"]["effort"]) == ("claude-opus-5-5", "medium")
    assert call["fallbacks"] == "default"


@pytest.mark.parametrize(
    ("text", "stop_reason", "message"),
    [
        ("", "refusal", "declined"),
        ("{", "end_turn", "not valid JSON"),
        ("{}", "max_tokens", "max_tokens"),
    ],
)
def test_bad_responses_raise_llm_error(text: str, stop_reason: str, message: str) -> None:
    claude = llm_module.Claude(_client(text, "claude-haiku-5-5", stop_reason))
    with pytest.raises(llm_module.LLMError, match=message):
        claude.call("s", "c", "p", {"type": "object"})
