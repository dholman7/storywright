"""Deterministic checks on model output. The model proposes; these decide what is allowed."""

import difflib
import re

_EXPECT = re.compile(r"\bexpect(?:\.poll|\.soft)?\s*\(")


def assertion_count(code: str) -> int:
    return len(_EXPECT.findall(code))


def weakening(before: str, after: str, tag: str) -> str | None:
    """Why a repair is rejected as weakening the suite, or None if it's acceptable.

    A heuristic, not a proof: it catches the common ways to "fix" a red test by testing less
    (deleting assertions, dropping the tagged test). Changing an expected value is allowed,
    because correcting a wrong expectation is a legitimate repair.
    """
    if tag not in after:
        return f"the repair removed the required tag {tag}"
    if assertion_count(after) < assertion_count(before):
        return (
            f"the repair cut assertions from {assertion_count(before)} to {assertion_count(after)}"
        )
    return None


def changed_shared_code(before: str, after: str) -> str | None:
    """Why an edit to a shared module (the page objects) is rejected, or None if it only adds.

    Other specs depend on every existing line, so a generated test may extend the module but
    never change or remove what is there.
    """
    old, new = before.splitlines(), after.splitlines()
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            return (
                f"the page-object edit changed or removed existing lines {i1 + 1}-{i2}; "
                "only additions are allowed"
            )
    return None
