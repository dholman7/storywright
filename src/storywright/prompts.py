"""Prompts and output schemas. Kept stable so the repo context can be cached across calls."""

from typing import Any

SYSTEM = """You write and repair Playwright API tests in TypeScript for an existing repository.
Your tests are run against the real app after every change, and you will see what failed.

How to write tests here:
- One file. Import helpers from "../../fixtures" (persona clients, unique names, samples).
- Every test carries the tag you are given, e.g. test("...", { tag: "@ID" }, async (...) => ...).
- Test behavior through the HTTP API: status codes, error codes, and the fields the scenario
  names. Use the OpenAPI document for paths and payloads.
- Tests share one database and run in parallel. Give everything you create a unique name.
- You can read the repository with list_files, read_file, and search. Before inventing test
  data the app must parse (an uploaded document, a date, an identifier), look up the format it
  expects in sample files or the code that parses it.
- Some work happens in the background after a request returns. Poll with expect.poll until
  the state you need is reached instead of assuming it is immediate.
- Make failures explain themselves: give every status assertion the response body as its
  message, e.g. expect(res.status(), await res.text()).toBe(201). A bare status mismatch
  hides why the app refused.

The rule that matters most: the scenario is the specification. Never weaken an assertion,
delete a check, or change an expected value away from what the scenario says in order to make
a test pass. If the app does not do what the scenario says, that is a defect to report.

Report a defect only when the app contradicts something the scenario states. When the app
rejects data your test chose for setup (an email, a name, a file, a date format), the test is
wrong: read the error body and change the data."""

WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approach": {"type": "string", "description": "One or two sentences on the plan."},
        "code": {"type": "string", "description": "The complete TypeScript test file."},
    },
    "required": ["approach", "code"],
    "additionalProperties": False,
}

REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diagnosis": {
            "type": "string",
            "enum": ["test_bug", "app_defect", "environment"],
            "description": (
                "test_bug: the test is wrong (bad setup data, payload, wait, or an expectation "
                "the scenario does not state). app_defect: the app contradicts a behavior the "
                "scenario explicitly states; a rejected setup step is not one. environment: the "
                "app or tooling could not run at all."
            ),
        },
        "reason": {"type": "string", "description": "The evidence for the diagnosis."},
        "code": {
            "type": "string",
            "description": "For test_bug, the complete corrected file. Otherwise an empty string.",
        },
    },
    "required": ["diagnosis", "reason", "code"],
    "additionalProperties": False,
}


def render_context(files: dict[str, str]) -> str:
    return "\n\n".join(f'<file path="{path}">\n{text}\n</file>' for path, text in files.items())


def write_prompt(story: str, test_path: str, tag: str) -> str:
    return (
        f"Write the tests for this scenario.\n<scenario>\n{story}</scenario>\n"
        f"File: {test_path}\nTag: {tag}"
    )


def repair_prompt(story: str, code: str, report: str, tag: str, rejected: str | None = None) -> str:
    note = (
        f"\nYour previous repair was rejected: {rejected}. Fix the cause instead.\n"
        if rejected
        else ""
    )
    return (
        f"These tests did not pass against the real app.\n<scenario>\n{story}</scenario>\n"
        f"<test_file>\n{code}\n</test_file>\n<results>\n{report}\n</results>\n{note}"
        f"Diagnose the failure. Keep the tag {tag} on every test."
    )
