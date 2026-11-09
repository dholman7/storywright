"""The repair guard's coverage matrix: ways to test less that it must reject, and real repairs it
must allow. The README publishes this list."""

import pytest

from storywright.guards import assertion_count, inventory, weakening

TAG = "@KAN-4"
BEFORE = """import { expect, expectStatus, test } from "../../fixtures";

test("A whitespace-only note is refused", { tag: "@KAN-4" }, async ({ apiAs, factory }) => {
  const reviewer = await apiAs("reviewer");
  const { documents, extractions } = await factory.vendorInReview();
  const response = await reviewer.post(`/api/extractions/${extractions.w9.id}/decision`, {
    data: { decision: "reject", note: "   " },
  });
  const body = await expectStatus(response, 422);
  expect(body.error.code).toBe("validation_error");
  expect(body.error.fields).toHaveLength(1);
  await expect
    .poll(async () => (await factory.extraction(documents.w9.id, "reviewer")).status)
    .toBe("pending_review");
});

test("The review queue is shown", { tag: "@KAN-4" }, async ({ pages }) => {
  await pages.reviewQueue.goto();
  await expect(pages.reviewQueue.heading).toBeVisible();
  await expect(pages.reviewQueue.empty).not.toBeVisible();
  await expect(pages.reviewQueue.title).toHaveText("Review queue");
});
"""
STORY = """Scenario: A whitespace-only note is refused
  When the reviewer rejects a document with a note of only spaces
  Then the response is 422 with error code validation_error
  And the document is still pending review"""
CONTEXT = (
    '<openapi>{"responses": {"200": {"description": "OK"}, '
    '"422": {"description": "Validation Error"}}}</openapi>'
)
SOURCES = (STORY, CONTEXT)


def edit(old: str, new: str, code: str = BEFORE, count: int = 1) -> str:
    assert code.count(old) >= count, f"probe doesn't apply: {old!r}"
    return code.replace(old, new) if count > 1 else code.replace(old, new, 1)


ASSERT_CODE = 'expect(body.error.code).toBe("validation_error");'
ASSERT_FIELDS = "expect(body.error.fields).toHaveLength(1);"
HEADING = "await expect(pages.reviewQueue.heading).toBeVisible();"

WEAKENED = {
    "deletes an assertion": (edit(f"  {ASSERT_FIELDS}\n", ""), "cut assertions"),
    "drops the tag": (edit('{ tag: "@KAN-4" }', "{}", count=2), "removed the required tag"),
    "renames the tag": (edit('"@KAN-4"', '"@KAN-41"', count=2), "removed the required tag"),
    "moves the tag into a comment": (
        edit('{ tag: "@KAN-4" }, ', "", count=2).replace("import", "// @KAN-4\nimport"),
        "removed the required tag",
    ),
    "toBe -> toBeTruthy": (
        edit(ASSERT_CODE, "expect(body.error.code).toBeTruthy();"),
        "loosened",
    ),
    "toHaveLength -> toBeDefined": (
        edit(ASSERT_FIELDS, "expect(body.error.fields).toBeDefined();"),
        "loosened",
    ),
    "toHaveLength -> not.toBeNull": (
        edit(ASSERT_FIELDS, "expect(body.error.fields).not.toBeNull();"),
        "loosened",
    ),
    "toBe -> toContain": (
        edit(ASSERT_CODE, 'expect(body.error.code).toContain("validation");'),
        "loosened",
    ),
    "exact length -> length > 0, on a new subject": (
        edit(ASSERT_FIELDS, "expect(body.error.fields.length).toBeGreaterThan(0);"),
        "exact checks went from",
    ),
    "exact text -> regex": (
        edit('toHaveText("Review queue")', "toHaveText(/Review/)"),
        "loosened",
    ),
    "adds .not": (
        edit("expect(body.error.code).toBe(", "expect(body.error.code).not.toBe("),
        "flipped",
    ),
    "removes .not": (edit(".not.toBeVisible()", ".toBeVisible()"), "flipped"),
    "wraps it in try/catch": (
        edit(ASSERT_CODE, f"try {{ {ASSERT_CODE} }} catch {{}}"),
        "conditional",
    ),
    "wraps it in an if": (edit(ASSERT_CODE, f"if (body.error) {{ {ASSERT_CODE} }}"), "conditional"),
    "guards it with &&": (
        edit(ASSERT_CODE, f"body.error && {ASSERT_CODE}"),
        "conditional",
    ),
    "swallows it with .catch": (
        edit(HEADING, HEADING.replace("();", "().catch(() => {});")),
        "conditional",
    ),
    "test.skip": (edit('test("A whitespace', 'test.skip("A whitespace'), "skips a test"),
    "test.fixme": (edit('test("A whitespace', 'test.fixme("A whitespace'), "skips a test"),
    "test.fail": (edit('test("A whitespace', 'test.fail("A whitespace'), "skips a test"),
    "test.only hides the rest": (edit('test("The review', 'test.only("The review'), "skips a test"),
    "a skip inside the body": (
        edit(
            "  await pages.reviewQueue.goto();",
            '  test.skip(true, "flaky");\n  await pages.reviewQueue.goto();',
        ),
        "skips a test",
    ),
    "expect -> expect.soft": (
        edit("expect(body.error.code)", "expect.soft(body.error.code)"),
        "soft",
    ),
    "asserts on a constant": (edit(ASSERT_CODE, "expect(true).toBe(true);"), "constant"),
    "comments one out and adds a constant": (
        edit(ASSERT_FIELDS, f"// {ASSERT_FIELDS}\n  expect(1).toBe(1);"),
        "constant",
    ),
    "drops the matcher": (edit(ASSERT_CODE, "expect(body.error.code);"), "cut assertions"),
    "changes the status without evidence": (
        edit("expectStatus(response, 422)", "expectStatus(response, 200)"),
        "without evidence",
    ),
    "changes an expected message without evidence": (
        edit('toBe("validation_error")', 'toBe("ok")'),
        "without evidence",
    ),
    "changes the status, citing a quote that isn't in the sources": (
        edit("expectStatus(response, 422)", "expectStatus(response, 200)"),
        "without evidence",
    ),
}
# Probes that start from a different "before" than BEFORE: (before, after, expected reason).
WEAKENED_FROM = {
    "tightens toContain to a value that drops the old one": (
        edit(ASSERT_CODE, 'expect(body.error.code).toContain("validation");'),
        edit(ASSERT_CODE, 'expect(body.error.code).toBe("ok");'),
        "without evidence",
    ),
}
# Evidence offered with some probes: (quote).
WEAKENED_EVIDENCE = {
    "changes the status, citing a quote that isn't in the sources": "Then the response is 200 OK",
}


@pytest.mark.parametrize("name", list(WEAKENED))
def test_the_guard_rejects(name: str) -> None:
    after, reason = WEAKENED[name]
    assert after != BEFORE
    problem = weakening(BEFORE, after, TAG, WEAKENED_EVIDENCE.get(name, ""), SOURCES)
    assert problem is not None and reason in problem, problem


@pytest.mark.parametrize("name", list(WEAKENED_FROM))
def test_the_guard_rejects_from_another_start(name: str) -> None:
    before, after, reason = WEAKENED_FROM[name]
    problem = weakening(before, after, TAG, "", SOURCES)
    assert problem is not None and reason in problem, problem


def test_the_guard_rejects_a_quote_too_short_to_be_evidence() -> None:
    after = edit("expectStatus(response, 422)", "expectStatus(response, 200)")
    assert '"200"' in CONTEXT
    problem = weakening(BEFORE, after, TAG, '"200"', SOURCES)
    assert problem is not None and "without evidence" in problem


FIXED_WAIT = edit(HEADING, f"await page.waitForTimeout(500);\n  {HEADING}")
STATUS_400 = edit("expectStatus(response, 422)", "expectStatus(response, 400)")
CONTAINS = edit(ASSERT_CODE, 'expect(body.error.code).toContain("validation");')
TRUTHY = edit(ASSERT_CODE, "expect(body.error.code).toBeTruthy();")
STORED = edit(
    "  await expect\n",
    '  expect(body.review_note).toBe("spaces");\n  await expect\n',
    code=edit('note: "   "', 'note: "  spaces  "'),
)
FIRST, SECOND = BEFORE.split('\ntest("The review')
REORDERED = 'import { expect, expectStatus, test } from "../../fixtures";\n\ntest("The review' + (
    SECOND + FIRST.split("\n", 1)[1]
)

ALLOWED = {
    "fixes setup data": (BEFORE, edit('note: "   "', 'note: "\\t  "'), ""),
    "replaces a fixed wait with a web-first assertion": (FIXED_WAIT, BEFORE, ""),
    "adds an assertion": (
        BEFORE,
        edit(ASSERT_FIELDS, f"{ASSERT_FIELDS}\n  expect(body.error.message).toBeTruthy();"),
        "",
    ),
    "corrects a status with a quote from the scenario": (
        STATUS_400,
        BEFORE,
        "Then the response is 422 with error code validation_error",
    ),
    "corrects a status with a quote from the OpenAPI document": (
        STATUS_400,
        BEFORE,
        '"422": {"description": "Validation Error"}',
    ),
    "expects back a string its own setup sent": (
        STORED,
        STORED.replace('toBe("spaces")', 'toBe("  spaces  ")'),
        "",
    ),
    "renames a variable used as a subject": (BEFORE, BEFORE.replace("body", "payload"), ""),
    "tightens toContain to toBe": (CONTAINS, BEFORE, ""),
    "tightens toBeTruthy to toBe": (TRUTHY, BEFORE, ""),
    "reorders the tests": (BEFORE, REORDERED, ""),
    "reformats": (BEFORE, BEFORE.replace("  ", "    "), ""),
    "switches to an exact role locator": (
        BEFORE,
        edit(
            "expect(pages.reviewQueue.heading)",
            'expect(page.getByRole("heading", { name: "Review queue", exact: true }))',
        ),
        "",
    ),
    "adds a message to a status assertion": (
        BEFORE,
        edit(
            ASSERT_CODE,
            f"{ASSERT_CODE}\n  expect(response.status(), await response.text()).toBe(422);",
        ),
        "",
    ),
}


@pytest.mark.parametrize("name", list(ALLOWED))
def test_the_guard_allows(name: str) -> None:
    before, after, evidence = ALLOWED[name]
    assert after != before
    assert weakening(before, after, TAG, evidence, SOURCES) is None


def test_reorder_probe_keeps_every_test() -> None:
    assert sorted(a.test for a in inventory(REORDERED).assertions) == sorted(  # type: ignore[union-attr]
        a.test
        for a in inventory(BEFORE).assertions  # type: ignore[union-attr]
    )


def test_assertion_count_includes_helpers_poll_and_soft_but_not_bare_expect() -> None:
    assert assertion_count(BEFORE) == 7
    code = 'test("t", async () => { expect(a); expect.soft(b).toBe(1); notexpect(c).toBe(2); });'
    assert assertion_count(code) == 1


def test_unparseable_output_falls_back_to_the_coarse_checks() -> None:
    broken = BEFORE.replace("});\n\ntest(", "});\n\ntest((", 1)
    assert inventory(broken) is None
    assert weakening(BEFORE, broken, TAG) is None
    assert "cut assertions" in (weakening(BEFORE, broken.replace(ASSERT_FIELDS, ""), TAG) or "")
