"""Web pages as text a model can read: main content, headings, lists, links, code, tables.

Dependency-free (standard library `html.parser`) on purpose: sending page addresses to a third-party
"reader" service would tell it everything you browse. Where `main` or `article` elements exist only
their content is kept; navigation, footers, scripts, forms and the like are dropped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urljoin

log = logging.getLogger(__name__)

INDENT_LEVELS = 6
"""How far a nested list is indented. Indentation costs output proportional to depth for every item,
so a page of lists nested thousands deep would otherwise cost the square of its size."""


def _tags(names: str) -> frozenset[str]:
    return frozenset(names.split())


SKIP = _tags(
    "script style noscript template svg canvas iframe object embed head "
    "nav footer aside form dialog select button"
)
LENIENT_SKIP = SKIP - _tags("nav footer aside form dialog")
"""What is still dropped when a page seems to have lost its content to the stricter rules."""
VOID = _tags("br hr img input meta link area base col embed source track wbr")
BLOCKS = _tags(
    "p div section article main header h1 h2 h3 h4 h5 h6 ul ol li table tr pre blockquote "
    "figure figcaption dl dt dd details summary"
)
HEADINGS = _tags("h1 h2 h3 h4 h5 h6")
LISTS = _tags("ul ol")
CONTENT = _tags("main article")
LINE_BREAKERS = _tags("br li tr dt dd")
"""Opening one of these only starts a new line; any other block starts a paragraph."""
SPARSE_TEXT = 200
SPARSE_HTML = 2000

Attrs = dict[str, str]


class _Extractor(HTMLParser):
    """Reads a page once, front to back, and writes text as it goes.

    What it is in the middle of is a handful of counters (inside something skipped, inside
    `main`, inside a list, inside `pre`) and the link being read. Each tag has a small method to
    open it and, if needed, one to close it; the two tables at the end of the class say which.
    """

    def __init__(self, base_url: str, only_main: bool, skip: frozenset[str] = SKIP) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.only_main = only_main
        self.skip = skip
        self.title = ""
        self.parts: list[str] = []
        self._newlines = 2  # trailing newlines already emitted (the start counts as a break)
        self._skip = 0
        self._main = 0  # depth inside main/article, when only_main
        self._in_title = False
        self._pre = 0
        self._list_depth = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    # -- writing
    def _live(self) -> bool:
        return self._skip == 0 and (not self.only_main or self._main > 0)

    def _emit(self, text: str) -> None:
        if not text:
            return
        self.parts.append(text)
        stripped = text.rstrip("\n")
        self._newlines = self._newlines + len(text) if not stripped else len(text) - len(stripped)

    def _out(self, text: str) -> None:
        """Text of the page: part of the link being read, if there is one."""
        if not self._live():
            return
        if self._href is not None:
            self._link_text.append(text)
        else:
            self._emit(text)

    def _break(self, lines: int = 2) -> None:
        """Make sure the output ends with at least `lines` newlines (never adds blank runs)."""
        if self._live() and self._newlines < lines:
            self._emit("\n" * (lines - self._newlines))

    def _break_around(self, tag: str) -> None:
        """The line or paragraph break before and after a block."""
        nested_list = tag in LISTS and self._list_depth > 0
        self._break(1 if nested_list or tag in LINE_BREAKERS else 2)

    def _skipping(self, tag: str) -> bool:
        return tag in self.skip and tag not in VOID

    # -- tags opening
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif self._skipping(tag):
            self._skip += 1
        else:
            if self.only_main and tag in CONTENT:
                self._main += 1
            if tag in BLOCKS or tag == "br":
                self._break_around(tag)
            opening = self._OPENING.get(tag)
            if opening is not None:
                # `<img alt>` has no value at all
                opening(self, tag, {name: value or "" for name, value in attrs})

    def _open_heading(self, tag: str, attrs: Attrs) -> None:
        if self._live():
            self._emit("#" * int(tag[1]) + " ")

    def _open_item(self, tag: str, attrs: Attrs) -> None:
        if self._live():
            self._emit("  " * min(max(0, self._list_depth - 1), INDENT_LEVELS) + "- ")

    def _open_list(self, tag: str, attrs: Attrs) -> None:
        self._list_depth += 1

    def _open_pre(self, tag: str, attrs: Attrs) -> None:
        self._pre += 1
        if self._live():
            self._emit("```\n")

    def _open_code(self, tag: str, attrs: Attrs) -> None:
        if not self._pre and self._live():
            self._emit("`")

    def _open_link(self, tag: str, attrs: Attrs) -> None:
        href = attrs.get("href", "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            self._href = urljoin(self.base, href)
            self._link_text = []

    def _open_image(self, tag: str, attrs: Attrs) -> None:
        if alt := attrs.get("alt", "").strip():
            self._out(f"[image: {alt}]")

    def _open_row(self, tag: str, attrs: Attrs) -> None:
        if self._live():
            self._emit("| ")

    def _open_rule(self, tag: str, attrs: Attrs) -> None:
        self._break(1)

    # -- tags closing
    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif self._skipping(tag):
            self._skip = max(0, self._skip - 1)
        else:
            closing = self._CLOSING.get(tag)
            if closing is not None:
                closing(self)
            if self.only_main and tag in CONTENT:
                self._main = max(0, self._main - 1)
            if tag in BLOCKS:
                self._break_around(tag)

    def _close_link(self) -> None:
        if self._href is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
        href, self._href, self._link_text = self._href, None, []
        if self._live():
            self._emit(f"[{text}]({href})" if text else href)

    def _close_list(self) -> None:
        self._list_depth = max(0, self._list_depth - 1)

    def _close_pre(self) -> None:
        self._pre = max(0, self._pre - 1)
        if self._live():
            self._break(1)
            self._emit("```")

    def _close_cell(self) -> None:
        if self._live():
            self._emit(" | ")

    def _close_code(self) -> None:
        if not self._pre and self._live():
            self._emit("`")

    # -- text
    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if not self._live():
            return
        if self._pre:
            self._out(data)
            return
        text = re.sub(r"\s+", " ", data)
        if self._newlines > 0:
            text = text.lstrip()  # nothing to separate from at the start of a line
        self._out(text)

    # -- which tag does what
    _OPENING: ClassVar[dict[str, Callable[[_Extractor, str, Attrs], None]]] = {
        **dict.fromkeys(HEADINGS, _open_heading),
        **dict.fromkeys(LISTS, _open_list),
        "li": _open_item,
        "pre": _open_pre,
        "code": _open_code,
        "a": _open_link,
        "img": _open_image,
        "tr": _open_row,
        "hr": _open_rule,
    }
    _CLOSING: ClassVar[dict[str, Callable[[_Extractor], None]]] = {
        **dict.fromkeys(LISTS, _close_list),
        "a": _close_link,
        "pre": _close_pre,
        "td": _close_cell,
        "th": _close_cell,
        "code": _close_code,
    }


def _only_main(html: str) -> bool:
    return bool(re.search(r"<(?:main|article)\b", html, re.I))


def _extract(html: str, base_url: str, only_main: bool, skip: frozenset[str]) -> tuple[str, str]:
    extractor = _Extractor(base_url, only_main, skip)
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # html.parser is lenient, but never let a page break the tool
        log.debug("html parsing stopped early", exc_info=True)
    text = _tidy("".join(extractor.parts))
    return re.sub(r"\s+", " ", extractor.title).strip(), text.strip()


def _tidy(text: str) -> str:
    """No trailing spaces on a line and never more than one blank line in a row.

    Not `re.sub(r"[ \t]+\n", ...)`: on a long run of spaces that tries every start, which costs
    the square of the run."""
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text)


def html_to_text(html: str, base_url: str = "") -> str:
    """Readable text for `html`. Never raises on malformed markup.

    Dropping navigation, footers and forms is right for most pages and wrong for a few (a page
    whose whole body sits in one <form>, an unclosed <nav>). When almost nothing is left of a
    sizeable page, it is read again with only the clearly invisible parts dropped."""
    title, body = _extract(html, base_url, _only_main(html), SKIP)
    if len(body) < SPARSE_TEXT and len(html) >= SPARSE_HTML:
        fallback_title, fallback = _extract(html, base_url, False, LENIENT_SKIP)
        if len(fallback) > len(body):
            title, body = title or fallback_title, fallback
    return f"Title: {title}\n\n{body}".strip() if title else body
