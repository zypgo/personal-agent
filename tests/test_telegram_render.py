from __future__ import annotations

import random
import re
import time

import pytest

from personal_agent.channels.telegram.render import (
    LIMIT,
    SPLIT_AT,
    balanced,
    render,
    split_markdown,
    to_html,
)


# ---- converting ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**bold** and __bold__", "<b>bold</b> and <b>bold</b>"),
        ("*italic* and _italic_", "<i>italic</i> and <i>italic</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("use `ls -la` here", "use <code>ls -la</code> here"),
        ("# Title", "<b>Title</b>"),
        ("### Sub ###", "<b>Sub</b>"),
        ("- one\n- two", "• one\n• two"),
        ("  - nested", "  • nested"),
        ("---", "──────────"),
        ("> quoted\n> more", "<blockquote>quoted\nmore</blockquote>"),
        (
            "[docs](https://example.com/a?b=1&c=2)",
            '<a href="https://example.com/a?b=1&amp;c=2">docs</a>',
        ),
    ],
)
def test_the_markdown_models_write_becomes_telegram_html(markdown, expected):
    assert to_html(markdown) == expected


def test_text_cannot_inject_markup():
    out = to_html("<script>alert(1)</script> & <b onclick=x>bold</b> a < b > c")
    assert "<script>" not in out and "<b onclick" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out
    assert balanced(out)


def test_only_web_links_become_links():
    assert "<a " not in to_html("[x](javascript:alert(1))")
    assert "<a " not in to_html("[x](file:///etc/passwd)")
    assert "<a " not in to_html("[x](tg://user?id=1)")
    assert '<a href="http://a.example/x">' in to_html("[x](http://a.example/x)")


def test_a_quote_in_a_link_cannot_end_the_attribute():
    out = to_html('[x](https://a.example/"onmouseover="y)')
    assert 'onmouseover="y' not in out.replace("%22", "")
    assert balanced(out)


def test_code_is_shown_as_typed_with_no_formatting_inside():
    assert to_html("`**not bold** <b>`") == "<code>**not bold** &lt;b&gt;</code>"
    block = to_html("```python\nx = a < b and **c**\n```")
    assert block == '<pre><code class="language-python">x = a &lt; b and **c**</code></pre>'


def test_a_hostile_language_name_does_not_become_an_attribute():
    out = to_html('```py"><script>\ncode\n```')
    assert "<script>" not in out and "language-" not in out
    assert balanced(out)
    # A language the fence grammar accepts but that is not a plain name never reaches the attribute.
    assert to_html("```c++\nx\n```") == '<pre><code class="language-c++">x</code></pre>'


def test_a_fence_that_is_never_closed_still_ends_the_message_cleanly():
    out = to_html("intro\n```\nopen forever")
    assert out.endswith("</pre>") and balanced(out)


def test_tables_are_shown_monospaced_so_columns_line_up():
    out = to_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert out.startswith("<pre>") and out.endswith("</pre>") and "| 1 | 2 |" in out


def test_underscores_inside_words_are_not_italics():
    assert to_html("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4"


def test_emphasis_markers_around_nothing_are_left_alone():
    assert to_html("a ** ** b") == "a ** ** b"
    assert to_html("a * b * c") == "a * b * c"


def test_chinese_and_emoji_survive():
    assert to_html("**你好** 🌍") == "<b>你好</b> 🌍"


# ---- well-formedness ----------------------------------------------------------------------
def test_balanced_markup_is_recognised():
    assert balanced("<b>a</b> <pre><code>x</code></pre>")
    assert balanced("plain")
    assert not balanced("<b>a")
    assert not balanced("<b><i>a</b></i>")
    assert not balanced("a</b>")
    assert not balanced("<script>a</script>")
    assert not balanced("<img src=x>")


# ---- splitting -----------------------------------------------------------------------------
def test_short_text_is_one_piece_exactly_as_written():
    assert split_markdown("hello\n\nworld") == ["hello\n\nworld"]


def test_long_text_is_cut_into_pieces_within_the_limit_and_loses_nothing():
    paragraphs = [f"Paragraph {n}: " + "word " * 120 for n in range(30)]
    text = "\n\n".join(paragraphs)
    pieces = split_markdown(text)
    assert len(pieces) > 1
    assert all(len(p) <= SPLIT_AT for p in pieces)
    assert "".join(re.findall(r"Paragraph \d+", "\n".join(pieces))) == "".join(
        re.findall(r"Paragraph \d+", text)
    )
    assert "".join("".join(p.split()) for p in pieces) == "".join(text.split())


def test_a_cut_prefers_a_blank_line():
    text = ("a" * 100 + "\n") * 20 + "\n" + ("b" * 100 + "\n") * 20
    first, *_ = split_markdown(text, 2200)
    assert first.rstrip().endswith("a" * 100) and "b" not in first


def test_one_enormous_line_is_wrapped_at_spaces():
    pieces = split_markdown("word " * 5000, 1000)
    assert all(len(p) <= 1000 for p in pieces)
    assert all(p.endswith("word ") or p.endswith("word") for p in pieces)


def test_a_line_with_no_spaces_is_still_cut():
    pieces = split_markdown("x" * 10_000, 3000)
    assert len(pieces) == 4 and all(len(p) <= 3000 for p in pieces)


def test_a_code_block_cut_in_two_is_closed_and_reopened():
    code = "\n".join(f"line_{n} = {n}" for n in range(400))
    pieces = split_markdown(f"intro\n```python\n{code}\n```\noutro", 1500)
    assert len(pieces) > 2
    for piece in pieces:
        assert piece.count("```") % 2 == 0, piece[-80:]
    assert pieces[1].startswith("```python")
    assert pieces[0].rstrip().endswith("```")
    rebuilt = "\n".join(re.findall(r"line_\d+ = \d+", "\n".join(pieces)))
    assert rebuilt == code


def test_text_after_a_closed_block_is_not_inside_a_block():
    text = "```\n" + "x\n" * 800 + "```\n" + "after\n" * 800
    pieces = split_markdown(text, 1500)
    last = pieces[-1]
    assert "```" not in last


# ---- the whole path ------------------------------------------------------------------------
def test_render_gives_html_and_a_plain_fallback_for_each_piece():
    [piece] = render("Hello **there**")
    assert piece.html == "Hello <b>there</b>"
    assert piece.plain == "Hello **there**"


def test_an_empty_message_still_says_something():
    assert render("   \n ")[0].plain == "(empty message)"


def test_markup_that_would_not_be_well_formed_is_sent_as_escaped_text():
    # `_BOLD` and `_ITALIC` can overlap on odd input; whatever happens, what is sent is balanced.
    for nasty in ["***a** b*", "**a *b** c*", "*a **b* c**", "__a _b__ c_"]:
        for piece in render(nasty):
            assert balanced(piece.html), (nasty, piece.html)


def test_a_message_of_a_hundred_kilobytes_arrives_in_valid_pieces():
    text = "\n\n".join(
        f"## Section {n}\n\n" + "- item **bold** `code` & <tag>\n" * 40 for n in range(80)
    )
    pieces = render(text)
    assert len(pieces) > 10
    assert all(len(p.html) <= LIMIT and len(p.plain) <= LIMIT for p in pieces)
    assert all(balanced(p.html) for p in pieces)


def test_rendering_expansion_cannot_push_a_piece_past_the_limit():
    # Every "&" becomes "&amp;" (5x) and every "<" "&lt;" (4x).
    pieces = render("&<" * 4000)
    assert len(pieces) > 1
    assert all(len(p.html) <= LIMIT for p in pieces)
    assert all(balanced(p.html) for p in pieces)


# ---- fuzz: whatever the model writes, what is sent is well formed and fits ---------------------
ATOMS = [
    "plain words here", "**bold**", "*it*", "_it_", "~~s~~", "`code`", "```", "```python", "~~~",
    "# head", "- item", "> quote", "| a | b |", "---", "[l](https://x.example)", "<b>", "</b>", "&",
    "<", "**", "*", "_", "`", "", "", "你好", "😀", "x" * 300, "word " * 60,
]  # fmt: skip


@pytest.mark.parametrize("seed", range(40))
def test_random_markdown_always_renders_into_valid_pieces(seed):
    rng = random.Random(seed)  # noqa: S311 - a reproducible test input, not a secret
    text = "\n".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 400)))
    pieces = render(text)
    assert pieces
    for piece in pieces:
        assert len(piece.html) <= LIMIT, (seed, len(piece.html))
        assert len(piece.plain) <= LIMIT
        assert balanced(piece.html), (seed, piece.html[:200])


# ---- a heading line is read by hand: the pattern for it was cubic ------------------------------
@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# C#", "<b>C#</b>"),  # a # that touches the word belongs to it
        ("## Using C# ##", "<b>Using C#</b>"),
        ("### Sub ###   ", "<b>Sub</b>"),
        ("#### a  ####", "<b>a</b>"),
        ("# #", "<b></b>"),
        ("   # 缩进三格", "<b>缩进三格</b>"),
        ("    # 缩进四格就不是标题", "    # 缩进四格就不是标题"),
        ("#标题", "#标题"),
        ("####### 七个", "####### 七个"),
    ],
)
def test_what_is_a_heading_line_and_what_stays_in_it(markdown, expected):
    assert to_html(markdown) == expected


def test_a_heading_line_with_a_long_run_of_spaces_in_it_is_converted_at_once():
    started = time.perf_counter()
    for line in (
        "# a" + " " * 3400 + "b",
        "## " + "x " * 1600 + "#" * 300 + " y",
        "# " + " #" * 1700 + "x",
    ):
        assert to_html(line).startswith("<b>")
    assert time.perf_counter() - started < 1.0
