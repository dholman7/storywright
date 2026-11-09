"""Prompts and output schemas. Kept stable so the repo context can be cached across calls."""

from typing import Any

SYSTEM = """You write and repair Playwright tests in TypeScript for an existing repository: API
tests, which call the HTTP API, or UI tests, which drive the web app in a browser. Your tests are
type-checked, linted, and run against the real app after every change, and you see what failed.

How to write tests here:
- The repository's test guide (AGENTS.md) is the standard. Follow every rule in it: its fixtures,
  its data factory, its page objects, its imports. Lint enforces many of the rules, and a lint
  error fails the test the same way a failed assertion does.
- One test file. Every test carries the tag you are given, e.g. test("...", { tag: "@ID" }, ...).
- Arrange data through the data factory, not through the UI or ad hoc requests. Spend the test's
  own lines on the behavior under test.
- API tests: check status codes, error codes, and the fields the scenario names. Use the OpenAPI
  document for paths and payloads.
- UI tests: use the page objects for every locator and navigation. If a screen or control has no
  page-object entry, add one to the page-object module (you may return its full new content),
  using a role, label, or text locator. Only add to that module: never change or remove what is
  there, because other tests depend on it.
- Match a control's name exactly: getByRole("button", { name: "Next", exact: true }). Names match
  by substring by default, and dev servers add their own controls: under `next dev`, "Next" also
  matches Next.js's "Open Next.js Dev Tools" button.
- Tests share one database and run in parallel. Give everything you create a unique name.
- You can read the repository with list_files, read_file, and search. Before inventing data the
  app must parse (an uploaded document, a date, an identifier), look up the format it expects.
- Some work happens in the background after a request returns. Wait for it with expect.poll or
  a web-first assertion instead of assuming it is immediate.
- Make failures explain themselves, in every repo, guide or not: every status assertion shows
  the response body, through the repo's helper if it has one (such as expectStatus), otherwise
  as the message, e.g. expect(res.status(), await res.text()).toBe(201). A bare "expected 201,
  received 422" hides why the app refused, and invites blaming the app for the test's request.

The rule that matters most: the scenario is the specification. Never weaken an assertion,
delete a check, or change an expected value away from what the scenario says in order to make
a test pass. If the app does not do what the scenario says, that is a defect to report. A
deterministic guard checks every repair and rejects one that removes or loosens an assertion,
flips its .not, makes it conditional or soft, skips a test, or changes an expected value without
quoting, in `evidence`, the scenario or repository text that states the new value.

Report a defect only when the app contradicts something the scenario states. When the app
rejects data your test chose for setup (an email, a name, a file, a date format), the test is
wrong: read the error and change the data."""

PAGE_OBJECTS_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "The complete new page-object module if you added entries to it, or an empty string if "
        "you did not change it. Additions only."
    ),
}


def write_schema(kinds: list[str], page_objects: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "kind": {
            "type": "string",
            "enum": kinds,
            "description": (
                "api for behavior of the HTTP API; "
                "ui for what a person sees and does in the browser."
            ),
        },
        "approach": {"type": "string", "description": "One or two sentences on the plan."},
        "code": {"type": "string", "description": "The complete TypeScript test file."},
    }
    if page_objects:
        properties["page_objects"] = PAGE_OBJECTS_FIELD
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_REPAIR_SCHEMA: dict[str, Any] = {
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
        "evidence": {
            "type": "string",
            "description": (
                "If the repair changes an expected value (a status code, a count, a message), a "
                "verbatim quote from the scenario or the repository context that states the new "
                "value. Otherwise an empty string."
            ),
        },
    },
    "required": ["diagnosis", "reason", "code", "evidence"],
    "additionalProperties": False,
}


def repair_schema(page_objects: bool) -> dict[str, Any]:
    if not page_objects:
        return _REPAIR_SCHEMA
    properties = {**_REPAIR_SCHEMA["properties"], "page_objects": PAGE_OBJECTS_FIELD}
    return {**_REPAIR_SCHEMA, "properties": properties, "required": list(properties)}


def render_context(files: dict[str, str]) -> str:
    return "\n\n".join(f'<file path="{path}">\n{text}\n</file>' for path, text in files.items())


def write_prompt(story: str, paths: dict[str, str], tag: str, page_objects: str | None) -> str:
    kinds = "\n".join(f"- {kind}: {path}" for kind, path in paths.items())
    extend = f"\nPage-object module you may extend: {page_objects}" if page_objects else ""
    return (
        f"Write the tests for this scenario.\n<scenario>\n{story}</scenario>\n"
        f"Choose the kind of test and write it to its file:\n{kinds}\nTag: {tag}{extend}"
    )


def repair_prompt(
    story: str,
    code: str,
    report: str,
    tag: str,
    rejected: str | None = None,
    page_objects: str = "",
) -> str:
    note = (
        f"\nYour previous repair was rejected: {rejected}. Fix the cause instead.\n"
        if rejected
        else ""
    )
    return (
        f"These tests did not pass against the real app.\n<scenario>\n{story}</scenario>\n"
        f"<test_file>\n{code}\n</test_file>\n"
        + (
            f"<page_objects_as_edited>\n{page_objects}\n</page_objects_as_edited>\n"
            if page_objects
            else ""
        )
        + f"<results>\n{report}\n</results>\n{note}"
        f"Diagnose the failure. Keep the tag {tag} on every test."
    )
