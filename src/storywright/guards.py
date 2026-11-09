"""Deterministic checks on model output. The model proposes; these decide what is allowed.

The repair guard parses the test file before and after a repair and compares what each test
asserts. A repair may fix setup, waits, locators, and payloads, and it may add or tighten checks.
It may not test less: drop the tag, cut or loosen an assertion, flip its negation, make it
conditional or soft, point it at a constant, skip the test, or change an expected value without
showing where the new value comes from. It is a heuristic, not a proof; the README lists what it
covers and what it doesn't.
"""

import difflib
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cache

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

_EXPECT = re.compile(r"\bexpect(?:\.poll|\.soft)?\s*\(")
# Repo assertion helpers, such as expectStatus(response, 201).
_HELPER = re.compile(r"^expect[A-Z]\w*$")
_TESTS = {"test", "test.only", "test.skip", "test.fixme", "test.fail", "it"}
_HOOKS = {"test.beforeEach", "test.afterEach", "test.beforeAll", "test.afterAll"}
_SKIPS = {
    "test.skip",
    "test.fixme",
    "test.fail",
    "test.only",
    "test.describe.skip",
    "test.describe.fixme",
    "test.describe.only",
}
_CONSTANTS = {"string", "number", "true", "false", "null", "undefined", "template_string"}
_CONDITIONAL = {"try_statement", "if_statement", "ternary_expression", "switch_statement"}

# Matcher strength. EXACT pins a value or a state, PARTIAL narrows it, WEAK only says "something".
EXACT, PARTIAL, WEAK = 2, 1, 0
_WEAK = {"toBeTruthy", "toBeFalsy", "toBeDefined"}
_PARTIAL = {
    "toContain",
    "toContainEqual",
    "toContainText",
    "toMatch",
    "toMatchObject",
    "toBeGreaterThan",
    "toBeGreaterThanOrEqual",
    "toBeLessThan",
    "toBeLessThanOrEqual",
    "toBeCloseTo",
    "toBeInstanceOf",
    "toBeOK",
}
# Element states: negated or not, each pins one state.
_STATES = {
    "toBeVisible",
    "toBeHidden",
    "toBeChecked",
    "toBeEnabled",
    "toBeDisabled",
    "toBeEditable",
    "toBeFocused",
    "toBeAttached",
    "toBeEmpty",
    "toBeInViewport",
}
# Exact with a string argument, partial with a regex.
_PATTERN_ARG = {
    "toHaveText",
    "toHaveURL",
    "toHaveTitle",
    "toHaveValue",
    "toHaveAttribute",
    "toHaveAccessibleName",
    "toHaveAccessibleDescription",
    "toHaveClass",
    "toHaveId",
}
# A quote offered as evidence must be at least this long, so "201" alone can't prove "201".
MIN_EVIDENCE = 12


@dataclass(frozen=True)
class Assertion:
    test: str
    kind: str  # "expect", "soft", "poll", or a helper name such as "expectStatus"
    subject: str
    matcher: str
    negated: bool
    rank: int
    guarded: bool
    constant_subject: bool
    literals: tuple[str, ...]

    def show(self) -> str:
        if self.kind not in ("expect", "soft", "poll"):
            return f"{self.kind}({self.subject}, ...)"
        call = "expect" if self.kind == "expect" else f"expect.{self.kind}"
        return f"{call}({self.subject}){'.not' if self.negated else ''}.{self.matcher}"


@dataclass
class Inventory:
    assertions: list[Assertion]
    skips: int
    strings: list[str]
    # The file with assertion arguments and test titles blanked: where setup data lives.
    setup: str

    def has_tag(self, tag: str) -> bool:
        token = re.compile(rf"(?<![\w@-]){re.escape(tag)}(?![\w-])")
        return any(token.search(s) for s in self.strings)


@cache
def _parser() -> Parser:
    return Parser(Language(tree_sitter_typescript.language_typescript()))


def _text(node: Node | None) -> str:
    return node.text.decode() if node is not None and node.text is not None else ""


def _same(a: Node | None, b: Node | None) -> bool:
    return (
        a is not None
        and b is not None
        and (a.start_byte, a.end_byte, a.type)
        == (
            b.start_byte,
            b.end_byte,
            b.type,
        )
    )


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _string_value(node: Node) -> str:
    """A string or template literal's content, without quotes."""
    return _text(node)[1:-1]


def _literals(nodes: Sequence[Node]) -> tuple[str, ...]:
    values = []
    for arg in nodes:
        for n in _walk(arg):
            if n.type in ("string", "template_string") and not any(
                c.type == "template_substitution" for c in n.children
            ):
                values.append(_string_value(n))
            elif n.type in ("number", "true", "false", "null", "regex"):
                values.append(_text(n))
    return tuple(values)


def _rank(matcher: str, negated: bool, args: Sequence[Node]) -> int:
    if matcher in _STATES:
        return EXACT
    if negated:
        return WEAK if matcher in {"toBeNull", "toBeUndefined", *_WEAK} else PARTIAL
    if matcher in _WEAK:
        return WEAK
    if matcher in _PARTIAL:
        return PARTIAL
    if matcher == "toHaveProperty":
        return EXACT if len(args) > 1 else PARTIAL
    if matcher in _PATTERN_ARG and any(n.type == "regex" for a in args for n in _walk(a)):
        return PARTIAL
    return EXACT


def _enclosing(node: Node) -> tuple[str, bool]:
    """The title of the test (or hook) around `node`, and whether a condition guards it."""
    guarded = False
    current = node.parent
    while current is not None:
        if current.type in _CONDITIONAL or (
            current.type == "binary_expression"
            and _text(current.child_by_field_name("operator")) in ("&&", "||", "??")
        ):
            guarded = True
        if current.type == "call_expression":
            name = _text(current.child_by_field_name("function"))
            if name in _TESTS:
                args = current.child_by_field_name("arguments")
                first = args.named_children[0] if args and args.named_children else None
                return (_string_value(first) if first is not None else name), guarded
            if name in _HOOKS:
                return name, guarded
        current = current.parent
    return "", guarded


def _from_expect(root: Node) -> tuple[Assertion, Node] | None:
    """The assertion whose chain starts at `expect(...)`, `expect.soft(...)`, or
    `expect.poll(...)`."""
    fn = root.child_by_field_name("function")
    kind = (
        "expect"
        if fn is not None and fn.type == "identifier"
        else _text(fn.child_by_field_name("property") if fn is not None else None)
    )
    args = root.child_by_field_name("arguments")
    subject = args.named_children[0] if args is not None and args.named_children else None
    mods: list[str] = []
    node = root
    while (
        node.parent is not None
        and node.parent.type == "member_expression"
        and _same(node.parent.child_by_field_name("object"), node)
    ):
        prop = _text(node.parent.child_by_field_name("property"))
        node = node.parent
        call = node.parent
        if (
            call is not None
            and call.type == "call_expression"
            and _same(call.child_by_field_name("function"), node)
        ):
            matcher_args = call.child_by_field_name("arguments")
            margs = list(matcher_args.named_children) if matcher_args is not None else []
            negated = "not" in mods
            test, guarded = _enclosing(call)
            catch = call.parent
            if (
                catch is not None
                and catch.type == "member_expression"
                and _text(catch.child_by_field_name("property")) == "catch"
            ):
                guarded = True
            assertion = Assertion(
                test=test,
                kind=kind,
                subject=" ".join(_text(subject).split()),
                matcher=prop,
                negated=negated,
                rank=_rank(prop, negated, margs),
                guarded=guarded,
                constant_subject=subject is not None and subject.type in _CONSTANTS,
                literals=_literals(margs),
            )
            return assertion, matcher_args if matcher_args is not None else call
        mods.append(prop)
    return None  # `expect(x)` with no matcher asserts nothing


def _from_helper(call: Node, name: str) -> tuple[Assertion, Node]:
    args = call.child_by_field_name("arguments")
    nodes = list(args.named_children) if args is not None else []
    test, guarded = _enclosing(call)
    subject = nodes[0] if nodes else None
    assertion = Assertion(
        test=test,
        kind=name,
        subject=" ".join(_text(subject).split()),
        matcher=name,
        negated=False,
        rank=EXACT,
        guarded=guarded,
        constant_subject=subject is not None and subject.type in _CONSTANTS,
        literals=_literals(nodes[1:]),
    )
    return assertion, args if args is not None else call


def inventory(code: str) -> Inventory | None:
    """Every assertion in the file, or None when it doesn't parse (verify reports that)."""
    source = code.encode()
    tree = _parser().parse(source)
    if tree.root_node.has_error:
        return None
    assertions: list[Assertion] = []
    skips = 0
    strings: list[str] = []
    blank: list[tuple[int, int]] = []
    for node in _walk(tree.root_node):
        if node.type in ("string", "template_string"):
            strings.append(_string_value(node))
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        name = _text(fn)
        if name in _SKIPS:
            skips += 1
        if name in _TESTS:
            args = node.child_by_field_name("arguments")
            if args is not None and args.named_children:
                title = args.named_children[0]
                blank.append((title.start_byte, title.end_byte))
        found: tuple[Assertion, Node] | None = None
        if fn is not None and (
            name == "expect"
            or (
                fn.type == "member_expression"
                and _text(fn.child_by_field_name("object")) == "expect"
                and _text(fn.child_by_field_name("property")) in ("soft", "poll")
            )
        ):
            found = _from_expect(node)
        elif fn is not None and fn.type == "identifier" and _HELPER.match(name):
            found = _from_helper(node, name)
        if found is not None:
            assertions.append(found[0])
            blank.append((found[1].start_byte, found[1].end_byte))
    setup = bytearray(source)
    for start, end in blank:
        setup[start:end] = b" " * (end - start)
    return Inventory(assertions, skips, strings, setup.decode(errors="replace"))


def assertion_count(code: str) -> int:
    found = inventory(code)
    return len(found.assertions) if found is not None else len(_EXPECT.findall(code))


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _sourced(value: str, setup: str, evidence: str, sources: Sequence[str]) -> bool:
    """Whether a new expected value comes from somewhere other than the model's wish."""
    is_number = re.fullmatch(r"-?\d+(\.\d+)?", value) is not None
    # A string the test itself sets up (a name, a note) may be expected back. A number may not:
    # status codes and counts need a stated source.
    if not is_number and value and value in setup:
        return True
    quote = _normalize(evidence)
    if len(quote) < MIN_EVIDENCE or not any(quote in _normalize(s) for s in sources):
        return False
    return re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", quote) is not None


def _coarse(before: str, after: str, tag: str) -> str | None:
    if tag not in after:
        return f"the repair removed the required tag {tag}"
    if len(_EXPECT.findall(after)) < len(_EXPECT.findall(before)):
        return (
            f"the repair cut assertions from {len(_EXPECT.findall(before))} "
            f"to {len(_EXPECT.findall(after))}"
        )
    return None


def weakening(
    before: str, after: str, tag: str, evidence: str = "", sources: Sequence[str] = ()
) -> str | None:
    """Why a repair is rejected as testing less, or None if it's acceptable.

    `evidence` is the model's quote for any changed expected value; it counts only if it appears
    in one of `sources` (the scenario, the repo context) and contains the new value.
    """
    old, new = inventory(before), inventory(after)
    if old is None or new is None:
        return _coarse(before, after, tag)
    if not new.has_tag(tag):
        return f"the repair removed the required tag {tag} from every test"
    if len(new.assertions) < len(old.assertions):
        return f"the repair cut assertions from {len(old.assertions)} to {len(new.assertions)}"
    if new.skips > old.skips:
        return "the repair skips a test or marks it as expected to fail (skip, fixme, fail, only)"

    def tally(inv: Inventory, attr: str) -> int:
        return sum(bool(getattr(a, attr)) for a in inv.assertions)

    if tally(new, "guarded") > tally(old, "guarded"):
        return (
            "the repair made an assertion conditional (try/catch, if, or .catch), so it can't fail"
        )
    if sum(a.kind == "soft" for a in new.assertions) > sum(
        a.kind == "soft" for a in old.assertions
    ):
        return "the repair made an assertion soft, so it no longer stops the test"
    if tally(new, "constant_subject") > tally(old, "constant_subject"):
        return "the repair asserts on a constant, which can't fail"

    remaining: dict[tuple[str, str, str], list[Assertion]] = {}
    for a in new.assertions:
        remaining.setdefault((a.test, a.kind, a.subject), []).append(a)
    for was in old.assertions:
        candidates = remaining.get((was.test, was.kind, was.subject))
        if not candidates:
            continue
        now = candidates.pop(0)
        if now.matcher == was.matcher and now.negated != was.negated:
            return f"the repair flipped {was.show()} to {now.show()}"
        if now.rank < was.rank:
            return f"the repair loosened {was.show()} to {now.show()}"
        # Tightening (toContain("validation") -> toBe("validation_error")) needs no evidence when
        # it keeps what was expected; pinning a different value does, or a repair could make a
        # failing check pass by expecting whatever the app returned.
        keeps_old = all(any(old in new for new in now.literals) for old in was.literals)
        if now.rank > was.rank and keeps_old:
            continue
        for value in (Counter(now.literals) - Counter(was.literals)).elements():
            if not _sourced(value, new.setup, evidence, sources):
                return (
                    f"the repair changed an expected value in {was.show()} to {value!r} without "
                    "evidence: quote the line of the scenario or the repo that states it"
                )
    for level, label in ((EXACT, "exact"), (PARTIAL, "exact or partial")):
        had = sum(a.rank >= level for a in old.assertions)
        has = sum(a.rank >= level for a in new.assertions)
        if has < had:
            return f"the repair loosened assertions: {label} checks went from {had} to {has}"
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
