"""Everything Storywright does to the target repo: read context, write tests, run them for real."""

import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    cwd: str
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    health: str


class TestsConfig(BaseModel):
    cwd: str
    typecheck: str
    run: str
    env: dict[str, str] = Field(default_factory=dict)


class RepoConfig(BaseModel):
    context_files: list[str]
    test_dir: str
    app: AppConfig
    tests: TestsConfig

    @classmethod
    def load(cls, path: Path) -> "RepoConfig":
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Verification(BaseModel):
    """What happened when the tests ran against the real app."""

    typechecks: bool
    typecheck_errors: list[str] = Field(default_factory=list)
    passed: int = 0
    failed: int = 0
    passed_tags: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    # Collection or syntax errors, or an app that never started.
    errors: list[str] = Field(default_factory=list)

    def green(self, tag: str) -> bool:
        return (
            self.typechecks
            and not self.errors
            and self.failed == 0
            and self.passed > 0
            and tag in self.passed_tags
        )


class AppStartError(RuntimeError):
    pass


def read_context(repo: Path, config: RepoConfig) -> dict[str, str]:
    return {f: (repo / f).read_text() for f in config.context_files if (repo / f).is_file()}


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
def running_app(repo: Path, app: AppConfig, timeout_s: float = 60) -> Iterator[int]:
    """Starts the app on a free port with a fresh data directory; yields the port."""
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="storywright-app-") as data_dir:
        values = {"port": str(port), "data_dir": data_dir}
        env = _env(app.env, values)
        log = Path(data_dir) / "app.log"
        with log.open("w") as out:
            process = subprocess.Popen(
                app.command.format(**values),
                shell=True,
                cwd=repo / app.cwd,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout_s
                while not _healthy(app.health.format(**values)):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AppStartError(log.read_text()[-1500:] or "app exited without output")
                    time.sleep(0.2)
                yield port
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)


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


def verify(repo: Path, config: RepoConfig, test_file: Path) -> Verification:
    """Type-checks the repo's tests, then runs `test_file` against a freshly started app."""
    tests_cwd = repo / config.tests.cwd
    check = subprocess.run(
        config.tests.typecheck, shell=True, cwd=tests_cwd, capture_output=True, text=True
    )
    # Only errors in our file count; the rest of the repo isn't ours to fix.
    ours = [ln for ln in check.stdout.splitlines() if test_file.name in ln]
    verification = Verification(typechecks=not ours, typecheck_errors=ours[:10])
    try:
        with running_app(repo, config.app) as port:
            values = {"port": str(port)}
            result = subprocess.run(
                config.tests.run.format(path=os.path.relpath(test_file, tests_cwd)),
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
