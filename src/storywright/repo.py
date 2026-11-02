"""Everything Storywright does to the target repo: read context, write tests, run them for real."""

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ServiceConfig(BaseModel):
    """A process the tests need, started on a free port per run. `{<name>_port}` and
    `{data_dir}` in `command`, `env`, and `health` are filled in at start."""

    cwd: str
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    health: str
    timeout_s: float = 60


class KindConfig(BaseModel):
    """One kind of test (api, ui): where it goes, how it runs, and what must be up for it."""

    test_dir: str
    run: str
    services: list[str]
    # Existing specs of this kind, shown to the model as examples; missing ones are skipped.
    examples: list[str] = Field(default_factory=list)


class TestsConfig(BaseModel):
    cwd: str
    typecheck: str
    # Lint command for one file ({path}), run only when `lint_if` exists under `cwd`, i.e. when
    # the repo has adopted the rules. Lint errors count as failures, like type errors.
    lint: str | None = None
    lint_if: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class RepoConfig(BaseModel):
    context_files: list[str]
    services: dict[str, ServiceConfig]
    kinds: dict[str, KindConfig]
    tests: TestsConfig
    # The page-object module UI tests may extend (additions only), if the repo has one.
    page_objects: str | None = None

    @classmethod
    def load(cls, path: Path) -> "RepoConfig":
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Verification(BaseModel):
    """What happened when the tests ran against the real app."""

    typechecks: bool
    typecheck_errors: list[str] = Field(default_factory=list)
    lint_errors: list[str] = Field(default_factory=list)
    passed: int = 0
    failed: int = 0
    passed_tags: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    # Collection or syntax errors, or an app that never started.
    errors: list[str] = Field(default_factory=list)

    def green(self, tag: str) -> bool:
        return (
            self.typechecks
            and not self.lint_errors
            and not self.errors
            and self.failed == 0
            and self.passed > 0
            and tag in self.passed_tags
        )


class AppStartError(RuntimeError):
    pass


def read_context(repo: Path, config: RepoConfig, kinds: list[str]) -> dict[str, str]:
    """Context files and examples for `kinds`. Files the repo doesn't have (an older commit, say)
    are skipped."""
    paths = [*config.context_files]
    if config.page_objects:
        paths.append(config.page_objects)
    for kind in kinds:
        paths += config.kinds[kind].examples
    unique = list(dict.fromkeys(paths))
    return {f: (repo / f).read_text() for f in unique if (repo / f).is_file()}


def _env(extra: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """Child-process environment without our own virtualenv, which would leak into the target's."""
    base = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return {**base, **{k: v.format(**values) for k, v in extra.items()}}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310 - local URL
            return bool(response.status == 200)
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


@contextmanager
def _service(
    repo: Path, name: str, service: ServiceConfig, values: dict[str, str]
) -> Iterator[None]:
    with tempfile.TemporaryDirectory(prefix=f"storywright-{name}-") as data_dir:
        values = {**values, "data_dir": data_dir}
        log = Path(data_dir) / f"{name}.log"
        with log.open("w") as out:
            process = subprocess.Popen(
                service.command.format(**values),
                shell=True,
                cwd=repo / service.cwd,
                env=_env(service.env, values),
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + service.timeout_s
                while not _healthy(service.health.format(**values)):
                    if process.poll() is not None or time.monotonic() > deadline:
                        output = log.read_text()[-1500:] or "exited without output"
                        raise AppStartError(f"{name}: {output}")
                    time.sleep(0.2)
                yield
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)


@contextmanager
def running_services(repo: Path, config: RepoConfig, names: list[str]) -> Iterator[dict[str, str]]:
    """Starts `names` in order, each on a free port with a fresh data directory. Yields the
    template values (`api_port`, `web_port`, ...) for the test command's environment."""
    values = {f"{name}_port": str(_free_port()) for name in config.services}
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(_service(repo, name, config.services[name], values))
        yield values


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _first_error(test: dict[str, Any]) -> str:
    last = (test.get("results") or [{}])[-1]
    error: dict[str, Any] = last.get("error") or next(iter(last.get("errors") or []), {})
    lines = [ln.strip() for ln in _ANSI.sub("", str(error.get("message", ""))).splitlines()]
    return " | ".join([ln for ln in lines if ln][:4])[:400]


def parse_report(report: dict[str, Any], verification: Verification) -> None:
    verification.errors += [str(e.get("message", e))[:400] for e in report.get("errors", [])]

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                if test.get("status") in ("expected", "flaky"):
                    verification.passed += 1
                    verification.passed_tags += [f"@{t.lstrip('@')}" for t in spec.get("tags", [])]
                elif test.get("status") != "skipped":
                    verification.failed += 1
                    verification.failures.append(f"{spec.get('title', '?')}: {_first_error(test)}")
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)


def _lint(tests_cwd: Path, config: TestsConfig, files: list[Path]) -> list[str]:
    """Lint errors in `files`, each prefixed with its file name when there is more than one."""
    if not (config.lint and config.lint_if and (tests_cwd / config.lint_if).is_file()):
        return []
    paths = " ".join(shlex.quote(os.path.relpath(f, tests_cwd)) for f in files)
    result = subprocess.run(
        # replace, not format: a command may contain other braces (JSON, ${VAR}).
        config.lint.replace("{path}", paths),
        shell=True,
        cwd=tests_cwd,
        capture_output=True,
        text=True,
    )
    try:
        reports = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [] if result.returncode == 0 else [(result.stderr or result.stdout)[-400:]]
    named = len(files) > 1
    return [
        (f"{Path(r.get('filePath', '')).name} " if named else "")
        + f"line {m.get('line', '?')}: {m.get('message', '')}"
        for r in reports
        for m in r.get("messages", [])
        if m.get("severity", 2) >= 2
    ][:15]


def verify(
    repo: Path, config: RepoConfig, kind: str, test_file: Path, lint_also: list[Path] | None = None
) -> Verification:
    """Type-checks and lints the test file (plus `lint_also`, such as page objects the run
    extended), then runs it against freshly started services."""
    tests_cwd = repo / config.tests.cwd
    check = subprocess.run(
        config.tests.typecheck, shell=True, cwd=tests_cwd, capture_output=True, text=True
    )
    # Only errors in our files count; the rest of the repo isn't ours to fix.
    ours_names = {test_file.name} | (
        {Path(config.page_objects).name} if config.page_objects else set()
    )
    ours = [ln for ln in check.stdout.splitlines() if any(n in ln for n in ours_names)]
    verification = Verification(
        typechecks=not ours,
        typecheck_errors=ours[:10],
        lint_errors=_lint(tests_cwd, config.tests, [test_file, *(lint_also or [])]),
    )
    spec = config.kinds[kind]
    try:
        with running_services(repo, config, spec.services) as values:
            result = subprocess.run(
                spec.run.format(path=os.path.relpath(test_file, tests_cwd)),
                shell=True,
                cwd=tests_cwd,
                env=_env(config.tests.env, values),
                capture_output=True,
                text=True,
            )
    except AppStartError as exc:
        verification.errors.append(f"app did not start: {exc}")
        return verification
    try:
        parse_report(json.loads(result.stdout), verification)
    except json.JSONDecodeError:
        verification.errors.append((result.stderr or result.stdout)[-800:] or "no test output")
    return verification
