"""Read-only repo tools the model may call: list, read, search. Confined to the repo root."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Never listed, read, or searched: dependencies, caches, VCS, runtime data, reports.
EXCLUDED = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".next",
    "data",
    "test-results",
    "playwright-report",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}
MAX_LIST = 200
MAX_LINES = 400
MAX_MATCHES = 60
MAX_FILE_BYTES = 1_000_000

DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List files under a directory of the repo, recursively, matching a glob.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the repo root."},
                "glob": {"type": "string", "description": 'Filename pattern, e.g. "*.txt" or "*".'},
            },
            "required": ["path", "glob"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": f"Read up to {MAX_LINES} lines of a text file, starting at a 1-based line.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File relative to the repo root."},
                "start_line": {"type": "integer", "description": "First line to return (1-based)."},
            },
            "required": ["path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search",
        "description": "Search text files under a directory for a regular expression.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path": {"type": "string", "description": "Directory relative to the repo root."},
            },
            "required": ["pattern", "path"],
            "additionalProperties": False,
        },
    },
]


class ToolError(ValueError):
    """Reported back to the model as an error result; never raised past the tool loop."""


class Toolbox:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ToolError(f"{relative!r} is outside the repository")
        if any(part in EXCLUDED for part in path.relative_to(self.root).parts):
            raise ToolError(f"{relative!r} is in an excluded directory")
        return path

    def _files(self, directory: Path, glob: str) -> list[Path]:
        found = []
        for path in sorted(directory.rglob(glob)):
            parts = path.relative_to(self.root).parts
            if path.is_file() and not any(part in EXCLUDED for part in parts):
                found.append(path)
        return found

    def list_files(self, path: str, glob: str) -> str:
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ToolError(f"{path!r} is not a directory")
        files = self._files(directory, glob or "*")
        lines = [str(f.relative_to(self.root)) for f in files[:MAX_LIST]]
        if len(files) > MAX_LIST:
            lines.append(f"... {len(files) - MAX_LIST} more; narrow the glob")
        return "\n".join(lines) or "no matching files"

    def read_file(self, path: str, start_line: int) -> str:
        file = self._resolve(path)
        if not file.is_file():
            raise ToolError(f"{path!r} is not a file")
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"{path!r} is larger than {MAX_FILE_BYTES} bytes")
        lines = file.read_text(errors="replace").splitlines()
        start = max(start_line, 1)
        chunk = lines[start - 1 : start - 1 + MAX_LINES]
        body = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))
        remaining = len(lines) - (start - 1 + len(chunk))
        return body + (f"\n... {remaining} more lines" if remaining > 0 else "")

    def search(self, pattern: str, path: str) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        directory = self._resolve(path)
        matches: list[str] = []
        for file in self._files(directory, "*"):
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                text = file.read_text()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{file.relative_to(self.root)}:{number}: {line.strip()[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        return "\n".join([*matches, "... more matches; narrow the search"])
        return "\n".join(matches) or "no matches"

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        handlers: dict[str, Callable[..., str]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search": self.search,
        }
        if name not in handlers:
            raise ToolError(f"unknown tool {name!r}")
        try:
            return handlers[name](**arguments)
        except TypeError as exc:
            raise ToolError(f"bad arguments for {name}: {exc}") from exc
