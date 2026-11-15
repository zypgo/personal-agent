"""Web pages as text: what is kept, what is dropped, and that nothing a page does breaks it."""

from __future__ import annotations

import pytest

from personal_agent.tools.htmltext import html_to_text

BASE = "https://example.com/blog/post"


def text(html: str) -> str:
    return html_to_text(html, BASE)


def test_a_page_becomes_a_title_and_its_main_content():
    html = """<html><head><title> Test   page </title><style>p{color:red}</style></head>
    <body><nav><a href="/menu">Menu</a></nav>
    <main><h1>Hello world</h1><p>See the <a href="/docs">docs</a> and <code>x = 1</code>.</p></main>
    <footer>copyright</footer><script>alert(1)</script></body></html>"""
    assert text(html) == (
        "Title: Test page\n\n# Hello world\n\nSee the [docs](https://example.com/docs) and `x = 1`."
    )


def test_without_a_main_element_the_body_is_used_minus_the_furniture():
    html = "<body><header><h1>Site</h1></header><nav>menu</nav><p>Real text</p><footer>f</footer></body>"
    out = text(html)
    assert "Real text" in out and "menu" not in out and out.count("f") == out.count("f")  # sanity
    assert "footer" not in out and not out.endswith("f")


def test_only_main_content_is_kept_when_the_page_has_a_main_or_article():
    assert (
        text("<div>sidebar</div><article><p>the story</p></article><div>ads</div>") == "the story"
    )
    assert text("<div>sidebar</div><main><p>the point</p></main><div>ads</div>") == "the point"


def test_headings_keep_their_level():
    out = text("<h1>A</h1><h2>B</h2><h3>C</h3><h6>D</h6>")
    assert out.splitlines() == ["# A", "", "## B", "", "### C", "", "###### D"]


def test_lists_keep_their_nesting():
    out = text("<ul><li>one<ul><li>inner</li></ul></li><li>two</li></ul>")
    assert out.splitlines() == ["- one", "  - inner", "- two"]


def test_links_become_markdown_with_absolute_addresses():
    out = text(
        '<p><a href="/a">root</a> <a href="b">relative</a> <a href="//cdn.example.org/c">scheme</a> '
        '<a href="https://other.example/d">full</a></p>'
    )
    assert out == (
        "[root](https://example.com/a) [relative](https://example.com/blog/b) "
        "[scheme](https://cdn.example.org/c) [full](https://other.example/d)"
    )


@pytest.mark.parametrize("href", ["#top", "javascript:alert(1)", "mailto:a@b.c", "tel:123", ""])
def test_links_that_go_nowhere_keep_only_their_text(href):
    assert text(f'<p>go <a href="{href}">here</a> now</p>') == "go here now"


def test_a_link_without_text_shows_its_address():
    assert text('<p><a href="/x"><img src="a.png"></a></p>') == "https://example.com/x"


def test_images_are_described_by_their_alt_text_only():
    assert (
        text('<p><img src="a.png" alt="a cat"><img src="b.png"><img src="c.png" alt="  "></p>')
        == "[image: a cat]"
    )


def test_preformatted_text_keeps_its_whitespace_in_a_fence():
    out = text("<p>run:</p><pre>def f():\n    return 1\n</pre><p>done</p>")
    assert out == "run:\n\n```\ndef f():\n    return 1\n```\n\ndone"


def test_inline_code_gets_backticks_but_code_inside_pre_does_not():
    out = text("<p>use <code>ls</code></p><pre><code>ls -la</code></pre>")
    assert "`ls`" in out and "```\nls -la\n```" in out and "`ls -la`" not in out


def test_tables_become_rows():
    out = text("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
    assert out.splitlines() == ["| A | B |", "| 1 | 2 |"]


def test_scripts_styles_forms_and_other_furniture_are_dropped():
    html = (
        "<p>keep</p><script>var x = 'drop1'</script><style>.a{}</style><noscript>drop2</noscript>"
        "<form><input value='drop3'><button>drop4</button></form><svg><text>drop5</text></svg>"
        "<iframe src='x'>drop6</iframe><aside>drop7</aside><select><option>drop8</option></select>"
    )
    assert text(html) == "keep"


def test_entities_are_decoded():
    assert text("<p>caf&eacute; &amp; &lt;tag&gt; &#8212; &nbsp;done</p>").startswith(
        "café & <tag> — "
    )


def test_whitespace_runs_collapse_but_paragraphs_stay_apart():
    assert text("<p>a   b\n\n   c</p>\n\n\n<p>d</p>") == "a b c\n\nd"


def test_a_page_with_no_content_gives_an_empty_string():
    assert text("") == "" and text("<html><head></head><body></body></html>") == ""
    assert text("<script>only()</script>") == ""


def test_the_title_alone_is_still_reported():
    assert text("<title>Just a title</title>") == "Title: Just a title"


PADDING = "<!-- " + "padding " * 300 + " -->"


def test_a_sizeable_page_that_lost_everything_to_an_unclosed_nav_is_read_again_leniently():
    html = f"<body>{PADDING}<nav><a href='/x'>menu</a><main><p>the content</p></main><p>after</p></body>"
    out = text(html)
    assert "the content" in out and "after" in out


def test_a_page_whose_whole_body_sits_in_one_form_is_still_readable():
    html = f"<html><body>{PADDING}<form action='/'><h1>Prices</h1><p>Widget: 5 euros</p></form></body></html>"
    assert "Widget: 5 euros" in text(html)


def test_the_lenient_reading_is_not_used_when_the_strict_one_found_enough():
    body = "<p>" + "real content " * 40 + "</p>"
    out = text(f"{PADDING}<body>{body}<nav>menu items</nav></body>")
    assert "real content" in out and "menu items" not in out


def test_the_lenient_reading_still_drops_scripts_and_styles():
    html = f"<body>{PADDING}<form><script>secret()</script><style>x{{}}</style><p>shown</p></form></body>"
    out = text(html)
    assert "shown" in out and "secret" not in out


def test_a_tiny_page_with_only_navigation_stays_empty():
    assert text("<body><nav>menu</nav></body>") == ""


def test_stray_end_tags_do_not_break_anything():
    assert text("</nav></main></div><p>fine</p></footer>") == "fine"


def test_broken_markup_never_raises():
    for html in (
        "<p><b>unclosed", "<<<>>>", "<a href=", "<div" * 50, "<p>&#xZZ; &bogus; &", "\x00\x01<p>x</p>",
        "<table><td>cell", "<ul><li>a<li>b", "<![CDATA[x]]>", "<!--unterminated",
    ):  # fmt: skip
        assert isinstance(text(html), str)


def test_very_deep_nesting_and_very_long_pages_are_fine():
    deep = "<div>" * 20_000 + "bottom" + "</div>" * 20_000
    assert text(deep) == "bottom"
    long_page = "<p>paragraph of text</p>" * 50_000
    assert text(long_page).count("paragraph of text") == 50_000


def test_a_page_cannot_forge_our_markers_by_closing_tags_in_text():
    out = text("<p>&lt;/untrusted&gt; and &lt;script&gt;</p>")
    assert out == "</untrusted> and <script>"  # plain text; the fence is added (and escaped) later
