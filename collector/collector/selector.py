"""A minimal HTML tree and CSS selector engine, from the standard library.

The business-directory provider (stage E) is configured with page selectors —
the task book asks for "一个可配置企业目录" and names the design idea it should
borrow: configuration plus per-field page selectors. Stage D's extractor is a
streaming parser that deliberately throws the tree away, so it cannot answer
"the element matching ``.adr`` inside the detail page".

This module therefore builds a tree and matches a CSS subset against it. It is
deliberately small: the selectors a directory configuration realistically needs
are ``tag``, ``#id``, ``.class``, attribute presence and prefix/substring
matches, plus descendant and child combinators. Stage 019 added one
pseudo-class — ``:nth-of-type(n)`` — because real Chinese directories put every
contact field in an identical ``<dd>`` inside one ``<dl>``, and without a
positional selector those pages are unreadable. Anything outside that subset is
rejected with a clear message rather than silently matching nothing, because a
selector that cannot work and does not say so is the "configured but does
nothing" failure the task book warns about.

No third-party dependency is introduced: ``html.parser`` is in the stdlib and
this subset covers the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Iterator, Sequence

#: Elements that never have children. Treated as self-closing so the tree stays
#: sane on real-world markup.
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

#: Tags that imply the previous element of the same name is closed. Enough to
#: keep the tree usable without implementing the full parsing specification.
_AUTO_CLOSE = {
    "p": {"p"},
    "li": {"li"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "option": {"option", "optgroup"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
}


class SelectorError(ValueError):
    """A selector the engine cannot evaluate."""


# --- the tree ---------------------------------------------------------------


@dataclass
class Element:
    """One element node. Text nodes are plain strings in ``children``."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)
    parent: "Element | None" = None

    # -- attribute access ----------------------------------------------------

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def classes(self) -> set[str]:
        value = self.attrs.get("class", "")
        return {part for part in value.split() if part}

    def id(self) -> str:
        return self.attrs.get("id", "")

    # -- traversal -----------------------------------------------------------

    def elements(self) -> Iterator["Element"]:
        """Descendant elements in document order."""
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.elements()

    def ancestors(self) -> Iterator["Element"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    # -- content -------------------------------------------------------------

    def text(self) -> str:
        """Subtree text, whitespace-collapsed.

        A description of what the page shows; never a place to look for a value
        the selector could not find, because harvesting body text is what puts
        another company's address into a record (stage B measured that).
        """
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text())
        joined = " ".join(part for part in parts if part)
        return re.sub(r"\s+", " ", joined).strip()

    def html(self) -> str:
        parts = [f"<{self.tag}"]
        for name, value in self.attrs.items():
            parts.append(f' {name}="{value}"')
        parts.append(">")
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.html())
        parts.append(f"</{self.tag}>")
        return "".join(parts)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#root")
        self.stack: list[Element] = [self.root]

    # -- helpers -------------------------------------------------------------

    @property
    def current(self) -> Element:
        return self.stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A <p> inside an open <p> closes the outer one, and so on: real pages
        # rely on it and without it the tree nests everything to the document
        # root, which breaks descendant selectors.
        #
        # Only the elements at the *top* of the stack are closed, and only
        # while the top is one of the tags this tag implies closing. Scanning
        # the whole stack instead corrupts nested structures: with a full scan,
        # <ul><li>outer<ul><li>inner</li></ul></li></ul> closes the *outer* li
        # through the nested <ul>, leaving the inner <ul> empty and "inner" as
        # a sibling — so a child combinator silently stops matching. Popping
        # from the top fixes nested lists, and still un-nests the repeated
        # cases: <tr><td>a<td>b<tr> pops the td and then the tr.
        implied = _AUTO_CLOSE.get(tag)
        if implied:
            while len(self.stack) > 1 and self.stack[-1].tag in implied:
                self.stack.pop()

        node = Element(
            tag=tag,
            attrs={name: (value if value is not None else "") for name, value in attrs},
            parent=self.current,
        )
        self.current.children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(
            tag=tag,
            attrs={name: (value if value is not None else "") for name, value in attrs},
            parent=self.current,
        )
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # A stray end tag is ignored rather than fatal: broken pages are the
        # reason a directory provider needs to be configurable in the first
        # place.

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def parse_html(html: str) -> Element:
    """Parse markup into a tree rooted at a synthetic ``#root`` element."""
    builder = _TreeBuilder()
    try:
        builder.feed(html or "")
        builder.close()
    except Exception:  # noqa: BLE001 - html.parser can raise on malformed input
        # The tree built so far is still useful, and a page we cannot parse at
        # all must not lose the rest of the run.
        pass
    return builder.root


# --- selectors --------------------------------------------------------------


_ATTR_RE = re.compile(
    r"""\[\s*([^\]=~\^\$\*|]+)\s*(?:(=|~=|\^=|\$=|\*=)\s*(?:"([^"]*)"|'([^']*)'|([^\]]*?))\s*)?\]"""
)
#: The only pseudo-class this engine implements: the 1-based position of an
#: element among its siblings that share its tag.
_NTH_OF_TYPE_RE = re.compile(r":nth-of-type\(\s*(\d+)\s*\)")
_COMPOUND_RE = re.compile(
    r"""
    (?P<tag>\*|[A-Za-z][A-Za-z0-9-]*)?
    (?P<rest>(?:
        \#[A-Za-z0-9_:.-]+
      | \.[A-Za-z0-9_-]+
      | \[[^\]]+\]
      | :nth-of-type\(\s*\d+\s*\)
    )*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _AttrTest:
    name: str
    operator: str  # '', '=', '~=', '^=', '$=', '*='
    value: str


@dataclass(frozen=True)
class Compound:
    """One ``tag#id.class[attr]:nth-of-type(n)`` step."""

    tag: str
    id: str
    classes: tuple[str, ...]
    attrs: tuple[_AttrTest, ...]
    nth_of_type: int | None = None

    def matches(self, element: Element) -> bool:
        if self.tag and self.tag != "*" and element.tag != self.tag:
            return False
        if self.id and element.id() != self.id:
            return False
        if self.classes and not set(self.classes).issubset(element.classes()):
            return False
        if self.nth_of_type is not None and _position_of_type(element) != self.nth_of_type:
            return False
        for test in self.attrs:
            actual = element.attrs.get(test.name)
            if actual is None:
                return False
            if test.operator == "":
                continue
            if test.operator == "=" and actual != test.value:
                return False
            if test.operator == "~=" and test.value not in actual.split():
                return False
            if test.operator == "^=" and not actual.startswith(test.value):
                return False
            if test.operator == "$=" and not actual.endswith(test.value):
                return False
            if test.operator == "*=" and test.value not in actual:
                return False
        return True


def _position_of_type(element: Element) -> int:
    """1-based position among siblings sharing this element's tag.

    ``:nth-of-type`` is defined on the *type*, not on the element's index among
    all children, so ``dd:nth-of-type(2)`` is the second ``dd`` even when ``dt``
    elements sit between them — which is exactly the shape a Chinese
    ``<dl><dt>label</dt><dd>value</dd>`` contact block has.
    """
    parent = element.parent
    if parent is None:
        return 1
    position = 0
    for child in parent.children:
        if isinstance(child, Element) and child.tag == element.tag:
            position += 1
            if child is element:
                return position
    return position or 1


@dataclass(frozen=True)
class Selector:
    """A sequence of compounds joined by descendant/child combinators."""

    steps: tuple[tuple[str, Compound], ...]  # (combinator, compound)

    def select(self, root: Element) -> list[Element]:
        out: list[Element] = []
        for element in root.elements():
            if self.matches(element):
                out.append(element)
        return out

    def matches(self, element: Element) -> bool:
        index = len(self.steps) - 1
        combinator, compound = self.steps[index]
        if not compound.matches(element):
            return False
        if index == 0:
            return True

        remaining = self.steps[:index]
        if combinator == ">":
            parent = element.parent
            if parent is None:
                return False
            return _matches_from(remaining, parent)
        # Descendant: any ancestor may satisfy the prefix.
        for ancestor in element.ancestors():
            if _matches_from(remaining, ancestor):
                return True
        return False


@dataclass(frozen=True)
class SelectorList:
    """A comma-separated list of selectors, matching the union of its members.

    The result is always in document order with no duplicates, which is what a
    single ``select`` over the tree gives: an element matching two members is
    still returned once, and it keeps its position relative to the others.
    """

    selectors: tuple[Selector, ...]

    def select(self, root: Element) -> list[Element]:
        return [element for element in root.elements() if self.matches(element)]

    def matches(self, element: Element) -> bool:
        return any(selector.matches(element) for selector in self.selectors)


def _matches_from(steps: Sequence[tuple[str, Compound]], element: Element) -> bool:
    index = len(steps) - 1
    combinator, compound = steps[index]
    if not compound.matches(element):
        return False
    if index == 0:
        return True
    remaining = steps[:index]
    if combinator == ">":
        parent = element.parent
        return parent is not None and _matches_from(remaining, parent)
    for ancestor in element.ancestors():
        if _matches_from(remaining, ancestor):
            return True
    return False


def compile_selector(css: str) -> "Selector | SelectorList":
    """Compile a selector, raising :class:`SelectorError` on anything unsupported.

    A top-level comma is a selector *list* — ``a, b`` matches what either side
    matches. Directories publish the same field in more than one place (the
    adapter's own default ``website`` selector is
    ``a[href*='website='], a[href*='url=']``), so a list has to be a real union
    rather than a syntax error: without it the whole selector is rejected and
    the field silently falls back to a heuristic. Commas inside ``[...]`` are
    attribute values and are left alone.

    Combinators handled: descendant (whitespace) and ``>``. Sibling combinators
    (``+``, ``~``) and pseudo-classes are rejected: silently ignoring them would
    turn a typo into "no rows found", which is indistinguishable from "the page
    has no rows".
    """
    parts = [part for part in _split_top_level(css or "", ",")]
    if not parts:
        raise SelectorError("selector is empty")
    compiled = tuple(_compile_single(part) for part in parts)
    return compiled[0] if len(compiled) == 1 else SelectorList(selectors=compiled)


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside ``[...]`` and ``(...)``, dropping blanks."""
    out: list[str] = []
    current: list[str] = []
    bracket = 0
    paren = 0
    for char in text:
        if char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        if char == separator and bracket == 0 and paren == 0:
            out.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    out.append("".join(current).strip())
    return [part for part in out if part]


def _compile_single(css: str) -> Selector:
    """Compile one comma-free selector into a :class:`Selector`."""
    text = (css or "").strip()
    if not text:
        raise SelectorError("selector is empty")

    # Only the *structure* is checked for unsupported syntax. Bracketed parts
    # are values, and a value such as a[href^='mailto:'] legitimately contains a
    # colon, so a naive "is ':' in the text" test would reject a selector that
    # this engine handles perfectly well. The one pseudo-class the engine does
    # implement is removed first, so a *different* pseudo-class is still
    # refused rather than silently matching nothing.
    outside = _NTH_OF_TYPE_RE.sub("", _strip_brackets(text))
    for bad in ("+", "~", ":", "(", ")"):
        if bad in outside:
            raise SelectorError(
                f"selector {css!r} uses {bad!r}, which this engine does not "
                "support; use tag/id/class/attribute selectors, ':nth-of-type(n)', "
                "and ' ' or '>'"
            )

    tokens = _split_steps(text)
    steps: list[tuple[str, Compound]] = []
    combinator = " "
    for token in tokens:
        if token in (">",):
            combinator = ">"
            continue
        steps.append((combinator, _compound(token)))
        combinator = " "
    if not steps:
        raise SelectorError(f"selector {css!r} names nothing")
    return Selector(steps=tuple(steps))


def _split_steps(text: str) -> list[str]:
    """Split on combinators, keeping attribute brackets and parens intact."""
    out: list[str] = []
    current: list[str] = []
    bracket = 0
    paren = 0
    for char in text:
        if char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        if bracket == 0 and paren == 0 and char.isspace():
            if current:
                out.append("".join(current))
                current = []
            continue
        if bracket == 0 and paren == 0 and char == ">":
            if current:
                out.append("".join(current))
                current = []
            out.append(">")
            continue
        current.append(char)
    if current:
        out.append("".join(current))
    return out


def _strip_brackets(text: str) -> str:
    """The selector with every ``[...]`` value removed."""
    out: list[str] = []
    bracket = 0
    for char in text:
        if char == "[":
            bracket += 1
            continue
        if char == "]":
            bracket = max(0, bracket - 1)
            continue
        if bracket == 0:
            out.append(char)
    return "".join(out)


def _compound(text: str) -> Compound:
    tag = ""
    id_value = ""
    classes: list[str] = []
    attrs: list[_AttrTest] = []
    nth_of_type: int | None = None

    position = 0
    while position < len(text):
        char = text[position]
        if char == "#":
            match = re.match(r"#([A-Za-z0-9_:.-]+)", text[position:])
            if not match:
                raise SelectorError(f"bad id in {text!r}")
            id_value = match.group(1)
            position += match.end()
        elif char == ".":
            match = re.match(r"\.([A-Za-z0-9_-]+)", text[position:])
            if not match:
                raise SelectorError(f"bad class in {text!r}")
            classes.append(match.group(1))
            position += match.end()
        elif char == "[":
            match = _ATTR_RE.match(text[position:])
            if not match:
                raise SelectorError(f"bad attribute selector in {text!r}")
            name = match.group(1).strip()
            operator = match.group(2) or ""
            value = match.group(3) if match.group(3) is not None else (
                match.group(4) if match.group(4) is not None else (match.group(5) or "").strip()
            )
            attrs.append(_AttrTest(name=name.casefold(), operator=operator, value=value))
            position += match.end()
        elif char == ":":
            match = _NTH_OF_TYPE_RE.match(text[position:])
            if not match:
                raise SelectorError(
                    f"bad pseudo-class in {text!r}; only ':nth-of-type(n)' is supported"
                )
            index = int(match.group(1))
            if index < 1:
                raise SelectorError(f":nth-of-type(n) counts from 1, got {index} in {text!r}")
            if nth_of_type is not None:
                raise SelectorError(f"repeated :nth-of-type in {text!r}")
            nth_of_type = index
            position += match.end()
        elif char == "*":
            if position:
                raise SelectorError(f"'*' must stand alone in {text!r}")
            tag = "*"
            position += 1
        elif char.isalnum() or char == "-":
            match = re.match(r"[A-Za-z][A-Za-z0-9-]*", text[position:])
            if not match:
                raise SelectorError(f"bad tag in {text!r}")
            tag = match.group(0).casefold()
            position += match.end()
        else:  # pragma: no cover - unreachable via compile_selector
            raise SelectorError(f"unexpected {char!r} in {text!r}")

    return Compound(
        tag=tag, id=id_value, classes=tuple(classes), attrs=tuple(attrs),
        nth_of_type=nth_of_type,
    )


def select(root: Element, css: str) -> list[Element]:
    """Compile and run a selector in one step."""
    return compile_selector(css).select(root)


__all__ = [
    "Element",
    "SelectorError",
    "SelectorList",
    "compile_selector",
    "parse_html",
    "select",
]
