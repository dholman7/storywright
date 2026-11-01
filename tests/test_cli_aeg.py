"""agent-eval-gate mode: the benchmark picks the kind, and extended files go to the support dir."""

import json
from pathlib import Path
from typing import Any

import pytest

from storywright import cli


@pytest.mark.parametrize(("task_kind", "expected"), [({"kind": "ui"}, ["ui"]), ({}, ["api"])])
def test_aeg_mode_uses_the_task_kind_and_the_support_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task_kind: dict[str, str], expected: list[str]
) -> None:
    task = {"task_id": "UI-01", "story": "Scenario: x", "required_tag": "@UI-01", "test_dir": "e2e"}
    (tmp_path / "task.json").write_text(json.dumps(task | task_kind))
    monkeypatch.setenv("AEG_TASK", str(tmp_path / "task.json"))
    monkeypatch.setenv("AEG_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AEG_OUTPUT", str(tmp_path / "out"))
    seen: dict[str, Any] = {}

    def fake_solve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"verdict": "passing", "reason": "green", "log": []}

    monkeypatch.setattr(cli, "solve", fake_solve)

    assert cli.main(["aeg"]) == 0
    assert seen["kinds"] == expected
    assert seen["support_dir"] == tmp_path / "out" / "support"
