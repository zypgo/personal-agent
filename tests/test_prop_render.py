"""Properties of the Telegram renderer: whatever Markdown a model writes (or a web page makes it
repeat), every piece is short enough, is markup Telegram accepts, and loses nothing the reader needs.

Three real defects came out of these: a NUL in the text crashed `render` with an IndexError (NUL
stands in for a link's markup while a line is converted), a fence with a very long language made
`render` loop forever, and markup that Telegram would refuse could come back as an over-long piece.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers import within
from personal_agent.channels.telegram.render import (
    LIMIT,
    SPLIT_AT,
    balanced,
    render,
    split_markdown,
    to_html,
)

# ---- what Telegram accepts ---------------------------------------------------------------
ALLOWED_ATTRIBUTES = {"a": {"href"}, "code": {"class"}}
ALLOWED_TAGS = {"b", "i", "s", "code", "pre", "a", "blockquote"}
BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot);)")
TAG = re.compile(r"</?[a-z]+(?:\s[^>]*)?>")


class Strict(HTMLParser):
    """Reads Telegram HTML as strictly as the service does: a small set of tags, closed in order,
    only the attributes Telegram knows, links that go to http(s), text escaped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED_TAGS, f"tag <{tag}> is not allowed"
        for name, value in attrs:
            assert name in ALLOWED_ATTRIBUTES.get(tag, set()), f"<{tag} {name}> is not allowed"
            if tag == "a":
                assert value and value.startswith(("http://", "https://")), value
                assert '"' not in value
                self.links.append(value)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"</{tag}> does not close the open tag"

    def handle_data(self, data):
        self.text.append(data)


def read_strictly(markup: str) -> Strict:
    bare = TAG.sub("", markup)
    assert "<" not in bare and ">" not in bare, "a bare angle bracket would be read as a tag"
    assert not BARE_AMPERSAND.search(bare), "a bare ampersand would be read as an entity"
    parser = Strict()
    parser.feed(markup)
    parser.close()
    assert not parser.stack, f"left open: {parser.stack}"
    return parser


def alphanumerics(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


# ---- what models write -------------------------------------------------------------------
_ORDINARY = ["hello", "world", "42", "x", "foo_bar", "snake_case_name", "2*3*4", "a&b", "x<y"]
_UNICODE = ["\u4f60\u597d", "caf\u00e9", "\U0001f600", "\u0645\u0631\u062d\u0628\u0627"]
WORDS = st.sampled_from([*_ORDINARY, "z>w", *_UNICODE, "1.", "-"])
_MARKS = ["**", "__", "*", "_", "~~", "`", "```", "~~~", "> ", "# ", "## ", "- ", "* ", "+ ", "---"]
_MARKUP = ["|", "| a | b |", "[", "](", ")", "&amp;", "<b>", "</b>", "<script>", "&", "<", ">", '"']
SYNTAX = st.sampled_from([*_MARKS, *_MARKUP])
LINKS = st.sampled_from(
    ["[a](http://x.y/z)", "[b c](https://e.com/?q=1&r=2)", '[e](http://q.r/"s)']
)
SPACES = st.sampled_from([" ", " ", " ", "\n", "\n", "\n\n", "\t", "  "])
LONG = st.builds(lambda unit, n: unit * n, WORDS, st.integers(200, 3000))
LONG_LINE = st.builds(lambda n: "w" * n, st.integers(1000, 9000))


def fenced(languages: list[str]):
    return st.builds(
        lambda marker, language, body: f"{marker}{language}\n{body}\n{marker}\n",
        st.sampled_from(["```", "~~~", "````"]),
        st.sampled_from(languages),
        st.builds(lambda n: "code line\n" * n, st.integers(1, 700)),
    )


def markdownish(*, links: bool = True, fences: str | None = "languages", nul: bool = False):
    """`fences` is None (no code blocks), "plain" (blocks without a language) or "languages"."""
    tokens = [WORDS, WORDS, WORDS, SYNTAX, SPACES, SPACES]
    if links:
        tokens.append(LINKS)
    if fences:
        tokens.append(fenced([""] if fences == "plain" else ["", "python", "c++", "x" * 25]))
    if nul:
        tokens.append(st.sampled_from(["\x00", "\x000\x00", "\x005\x00"]))
    token = st.one_of(*tokens)
    short = st.lists(token, max_size=40).map("".join)
    big = st.lists(st.one_of(token, LONG, LONG_LINE), min_size=1, max_size=12).map("".join)
    return st.one_of(short, short, big)


def assert_every_piece_is_sendable(pieces) -> None:
    assert pieces
    for piece in pieces:
        assert 0 < len(piece.html) <= LIMIT, len(piece.html)
        assert len(piece.plain) <= LIMIT
        assert piece.html.strip()
        assert "\x00" not in piece.html + piece.plain
        assert balanced(piece.html)
        read_strictly(piece.html)


# ---- the invariants ------------------------------------------------------------------------
@settings(max_examples=300)
@given(markdownish(nul=True))
def test_whatever_is_written_every_piece_is_short_valid_and_the_call_returns(text):
    with within(10):
        pieces = render(text)
    assert_every_piece_is_sendable(pieces)


@settings(max_examples=300)
@given(markdownish(links=False, fences=None))
def test_nothing_the_reader_needs_is_lost(text):
    if not text.strip():
        return
    shown = "".join("".join(read_strictly(p.html).text) for p in render(text))
    assert alphanumerics(shown) == alphanumerics(text)


FENCE_LINE = r"(?m)^\s*(?:`{3,10}|~{3,10})[\w+#.-]{0,30}\s*$"


@settings(max_examples=300)
@given(markdownish(links=False, fences="plain"))
def test_a_code_block_keeps_its_text_when_it_is_cut_across_messages(text):
    if not text.strip():
        return
    shown = "".join("".join(read_strictly(p.html).text) for p in render(text))
    assert alphanumerics(shown) == alphanumerics(re.sub(FENCE_LINE, "", text))


@settings(max_examples=200)
@given(markdownish(links=False, fences=None), st.integers(100, 4000))
def test_splitting_cuts_only_whitespace_and_respects_the_limit(text, limit):
    pieces = split_markdown(text, limit)
    assert alphanumerics("".join(pieces)) == alphanumerics(text)
    assert all(len(piece) <= limit for piece in pieces)


@settings(max_examples=200)
@given(markdownish(links=False), st.integers(100, 600))
def test_splitting_text_with_code_blocks_adds_a_bounded_amount_of_fencing(text, limit):
    pieces = split_markdown(text, limit)
    # A cut block is closed (at most ten characters) and reopened (a marker and a language).
    assert all(len(piece) <= limit + 45 for piece in pieces)


@settings(max_examples=200)
@given(
    st.lists(st.sampled_from(["```", "```py", "~~~", "text", "", "`"]), min_size=1, max_size=60),
    st.integers(100, 300),
)
def test_a_piece_never_ends_inside_a_code_block(lines, limit):
    text = "\n".join(line + " " + "word " * 12 if line == "text" else line for line in lines)
    text = (text + "\n") * (limit // (len(text) + 1) + 2)  # long enough to need cutting
    for piece in split_markdown(text, limit):
        opened: str | None = None
        for line in piece.split("\n"):
            fence = re.fullmatch(r"\s*(`{3,10}|~{3,10})\s*([\w+#.-]{0,30})\s*", line)
            if fence is None:
                continue
            if opened is None:
                opened = fence.group(1)[0]
            elif fence.group(1)[0] == opened and not fence.group(2):
                opened = None
        assert opened is None, piece


# ---- the defects these found, spelled out ---------------------------------------------------
@pytest.mark.parametrize(
    "text", ["\x005\x00", "a \x007\x00 b", "[x](http://a.b) \x000\x00 \x009\x00"]
)
def test_a_nul_in_the_text_is_dropped_not_taken_for_markup(text):
    assert_every_piece_is_sendable(render(text))


@pytest.mark.parametrize(
    "text",
    [
        "```" + "x" * 3400 + "\n" + "y" * 3400 + "\n```",
        "```" + "x" * 3490 + "\n" + "y" * 3000 + "\n" + "z" * 3000,
        "`" * 3000 + "\n" + "y" * 6000,
        "~" * 3400 + "\n" + "y" * 3400 + "\n" + "~" * 3400,
    ],
)
def test_a_fence_with_a_very_long_language_cannot_make_the_renderer_loop(text):
    with within(5):
        pieces = render(text)
    assert_every_piece_is_sendable(pieces)


def test_text_that_escaping_would_make_five_times_longer_still_fits():
    text = "&" * 20_000
    pieces = render(text)
    assert_every_piece_is_sendable(pieces)
    assert alphanumerics("".join("".join(read_strictly(p.html).text) for p in pieces)) == ""
    assert "".join("".join(read_strictly(p.html).text) for p in pieces).count("&") == 20_000


def test_html_in_the_text_is_shown_as_text_not_passed_on():
    text = "<b>bold</b> <script>alert(1)</script> <a href='javascript:alert(1)'>x</a>"
    pieces = render(text)
    assert_every_piece_is_sendable(pieces)
    shown = "".join("".join(read_strictly(p.html).text) for p in pieces)
    assert "<script>" in shown and "<b>bold</b>" in shown


def test_overlapping_emphasis_and_links_fall_back_to_text_that_still_fits():
    text = ("[**a](http://x.y/z) b** " * 400) + "tail"
    pieces = render(text)
    assert_every_piece_is_sendable(pieces)
    assert "tail" in "".join("".join(read_strictly(p.html).text) for p in pieces)


def test_the_limits_are_what_the_service_documents():
    assert LIMIT == 4096 and SPLIT_AT < LIMIT


def test_to_html_is_idempotent_about_nul():
    assert to_html("a\x00b") == to_html("ab")
