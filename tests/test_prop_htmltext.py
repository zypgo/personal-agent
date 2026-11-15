"""A web page is written by a stranger. Turning it into text must never fail, never stop half way
without saying so, never show what the page hides, and never cost more than the page does.

Found by this: a page of 27 KB made of `<ul>` and `<li>` took longer than ten seconds, and 180 KB
needed almost two gigabytes. Every list item was indented by the depth of the lists around it, and
the line was then cleaned with a pattern that costs the square of a run of spaces.
"""

from __future__ import annotations

import logging
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helpers import within
from personal_agent.tools.htmltext import INDENT_LEVELS, _tidy, html_to_text

BASE = "https://example.com/dir/"

# ---- cost ---------------------------------------------------------------------------------------
HOSTILE = {
    "lists nested and items": "<ul>" * 20_000 + "<li>x" * 20_000,
    "lists nested and numbered": "<ol>" * 5_000 + "<li>x</li>" * 5_000,
    "divs": "<div>" * 100_000 + "hi" + "</div>" * 100_000,
    "links": '<a href="/x">y</a>' * 20_000,
    "links with long relative paths": '<a href="../../a/../b/./c">y</a>' * 20_000,
    "table": "<table>" + "<tr><td>a</td><td>b</td></tr>" * 20_000 + "</table>",
    "tags never closed": "<p><b><i><u>" * 30_000,
    "one huge attribute": '<a href="' + "a" * 1_000_000 + '">x</a>',
    "many attributes": "<div " + " ".join(f"a{i}=b" for i in range(50_000)) + ">x</div>",
    "nested pre": "<pre>" * 50_000 + "x",
    "comments opened": "<!--" * 50_000,
    "cdata opened": "<![CDATA[" * 50_000,
    "entities": "&amp;" * 200_000,
    "quotes": "<blockquote>" * 50_000 + "q",
    "a run of spaces then more lines": ("<pre>" + " " * 50_000 + "x\n") * 20,
    "headings": "<h1>" * 50_000 + "t",
    "title": "<title>" + "t" * 500_000 + "</title>x",
    "title pieces": "<title>" + "t&amp;" * 100_000 + "</title>x",
    "unclosed script": "<script>" + "x" * 500_000,
    "tags with no end": "<" * 200_000,
    "angle brackets in text": "a < b > c " * 100_000,
}


@pytest.mark.parametrize("name", HOSTILE)
def test_a_page_built_to_be_expensive_costs_about_what_it_weighs(name):
    page = HOSTILE[name]
    with within(5):
        text = html_to_text(page, BASE)
    assert len(text) <= 4 * len(page) + 1_000, f"{len(page):,} characters became {len(text):,}"


def test_a_list_nested_deeper_than_anything_real_is_indented_no_deeper():
    text = html_to_text("<ul>" * 50 + "<li>a</li><li>b</li>" + "</ul>" * 50, BASE)
    assert text.splitlines()[1] == "  " * INDENT_LEVELS + "- b"


def test_lists_of_a_real_depth_are_indented_by_their_depth():
    text = html_to_text("<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li></ul>", BASE)
    assert text.splitlines() == ["- a", "  - b", "    - c"]


# ---- the tidy step is the old pattern, only faster ---------------------------------------------------
@settings(max_examples=500)
@given(st.text(alphabet=" \t\n\rx", max_size=120))
def test_tidying_is_what_the_two_patterns_did(text):
    expected = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text))
    # They differ only at the very end of the text, where the caller strips anyway
    assert _tidy(text).rstrip() == expected.rstrip()
    assert _tidy(text).strip() == expected.strip()


# ---- what is kept and what is hidden ------------------------------------------------------------
_serial = iter(range(10**9))


def word() -> str:
    return f"kw{next(_serial)}x"


@st.composite
def pages(draw):
    """A page as a list of chunks: visible words wrapped in (balanced) markup, and hidden ones in
    a script or a style. Returns (html, visible words in order, hidden words)."""
    html: list[str] = []
    visible: list[str] = []
    hidden: list[str] = []
    for _ in range(draw(st.integers(1, 12))):
        w = word()
        kind = draw(
            st.sampled_from(
                [
                    "p",
                    "div",
                    "b",
                    "i",
                    "li",
                    "h2",
                    "a",
                    "pre",
                    "code",
                    "td",
                    "blockquote",
                    "plain",
                    "script",
                    "style",
                    "comment",
                    "br",
                    "entity",
                ]
            )
        )
        if kind == "script" or kind == "style":
            hidden.append(w)
            html.append(f"<{kind}>var {w} = 1;</{kind}>")
        elif kind == "comment":
            hidden.append(w)
            html.append(f"<!-- {w} -->")
        elif kind == "br":
            visible.append(w)
            html.append(f"{w}<br>")
        elif kind == "a":
            visible.append(w)
            html.append(f'<a href="/{draw(st.sampled_from(["x", "y/z", "?q=1", "#top"]))}">{w}</a>')
        elif kind == "plain":
            visible.append(w)
            html.append(w + " ")
        elif kind == "entity":
            visible.append(w)
            html.append(f"{w}&amp;{w}&lt;")  # the word twice; the second is the same word
            visible.append(w)
        else:
            visible.append(w)
            html.append(f"<{kind}>{w}</{kind}>")
    return "".join(html), visible, hidden


@settings(max_examples=300)
@given(pages())
def test_everything_visible_is_kept_in_order_and_nothing_hidden_appears(page):
    html, visible, hidden = page
    text = html_to_text(html, BASE)
    found = re.findall(r"kw\d+x", text)
    assert found == visible
    assert not set(found) & set(hidden)


# ---- never raises, never stops half way without saying so -----------------------------------------
PIECES = st.sampled_from(
    [
        "<",
        ">",
        "</",
        "<!",
        "<!--",
        "-->",
        "<?",
        "<![CDATA[",
        "]]>",
        "&",
        "&#",
        "&#x",
        "&#xZ;",
        "&amp",
        "<a",
        "<a href",
        "<a href=",
        '<a href="',
        "<a href='x'",
        "<img alt",
        "<img alt>",
        "<br/>",
        "</br>",
        "<p>",
        "</p>",
        "<div class=",
        "<script>",
        "</script>",
        "<style>",
        "<title>",
        "</title>",
        "<ul>",
        "<li>",
        "</ul>",
        "<pre>",
        "</pre>",
        "<table>",
        "<tr>",
        "<td>",
        "<main>",
        "</main>",
        "<article>",
        "<nav>",
        "<form>",
        "</form>",
        "text",
        " ",
        "\n",
        "\t",
        "\x00",
        "\ufffd",
        "\u202e",
        "word ",
        "=",
        '"',
        "'",
        "/",
        "\\",
        "#",
        "?",
        "javascript:alert(1)",
        "<a href=javascript:x>",
    ]
)


@settings(max_examples=600, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(markup=st.lists(PIECES, max_size=60).map("".join))
def test_whatever_the_markup_looks_like_the_text_is_made_without_the_parser_giving_up(
    markup, caplog
):
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="personal_agent.tools.htmltext")
    with within(5):
        text = html_to_text(markup, BASE)
    assert isinstance(text, str)
    assert not [r for r in caplog.records if "stopped early" in r.getMessage()], (
        "the parser raised and the rest of the page was dropped"
    )
