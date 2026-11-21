"""Reading and writing Obsidian-style Markdown on plain strings."""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from personal_agent.vault import note as md


def test_properties_are_read_and_the_body_is_what_follows():
    parsed = md.parse("---\ntitle: 周报\ntags: [a, b]\ncreated: 2026-11-21\n---\n# 标题\n正文\n")
    assert parsed.front == {"title": "周报", "tags": ["a", "b"], "created": "2026-11-21"}
    assert parsed.body == "# 标题\n正文\n"
    assert parsed.front_error is None


def test_a_note_without_properties_is_all_body():
    parsed = md.parse("# 只有正文\n")
    assert parsed.front == {} and parsed.front_raw is None and parsed.body == "# 只有正文\n"


def test_properties_must_open_on_the_first_line():
    assert md.parse("\n---\na: 1\n---\n").front == {}


def test_broken_properties_are_reported_not_raised():
    parsed = md.parse("---\na: [unclosed\n---\ntext")
    assert parsed.front == {} and parsed.front_error and parsed.body == "text"


def test_properties_that_are_not_a_mapping_are_reported():
    assert md.parse("---\n- a\n- b\n---\nx").front_error


def test_a_properties_block_cannot_grow_when_it_is_walked():
    bomb = "---\n" + "a: &a [x]\n" + "".join(f"b{i}: &b{i} [*a, *a]\n" for i in range(1)) + "---\n"
    deep = "---\nk: " + "[" * 50 + "]" * 50 + "\n---\n"
    assert md.parse(bomb).body == ""  # parsed or refused, but it returns
    assert md.parse(deep).front_error  # too deeply nested


def test_dates_become_text():
    assert md.parse("---\nwhen: 2026-11-21 08:00:00\n---\n").front["when"].startswith("2026-11-21")


def test_render_keeps_order_and_quotes_links():
    text = md.render({"title": "A", "tags": ["x", "y"], "related": ["[[b]]"]}, "# A\n")
    assert text.startswith("---\ntitle: A\ntags: [x, y]\nrelated: ['[[b]]']\n---\n# A")
    assert md.parse(text).front["related"] == ["[[b]]"]


def test_links_are_found_outside_code_and_comments_only():
    body = (
        "见 [[张三]] 和 [[项目/预算#第二节|预算]]。\n"
        "```\n[[在代码里]]\n```\n"
        "行内 `[[也不算]]` 和 %% [[注释]] %% 都不算。\n"
        "![[图.png]]\n"
    )
    found = md.links(md.parse(body))
    assert [(link.target, link.heading, link.alias, link.embed) for link in found] == [
        ("张三", "", "", False),
        ("项目/预算", "第二节", "预算", False),
        ("图.png", "", "", True),
    ]


def test_links_in_properties_count():
    parsed = md.parse('---\nrelated: ["[[李四]]"]\n---\n正文')
    assert [link.target for link in md.links(parsed)] == ["李四"]


def test_tags_come_from_properties_and_text_but_not_code_or_headings_or_numbers():
    parsed = md.parse(
        "---\ntags: [项目, 'qixu/thread']\n---\n# 标题\n"
        "正文 #待办 和 #2026 和 网址 http://a.b/#frag\n```\n#代码\n```\n（#会议）\n"
    )
    assert md.tags_of(parsed) == ["项目", "qixu/thread", "待办", "会议"]


def test_title_comes_from_properties_then_first_heading_then_name():
    assert md.title_of(md.parse("---\ntitle: 甲\n---\n# 乙"), "文件") == "甲"
    assert md.title_of(md.parse("先说点别的\n# 乙 **粗**\n"), "文件") == "乙 粗"
    assert md.title_of(md.parse("没有标题"), "文件") == "文件"


def test_summary_skips_headings_callouts_and_code():
    parsed = md.parse("# 标题\n> [!info] 状态\n\n```\ncode\n```\n- 第一件事：订票\n")
    assert md.summary_of(parsed) == "第一件事：订票"
    assert md.summary_of(md.parse("---\nsummary: 一句话\n---\n正文")) == "一句话"


def test_a_section_is_replaced_and_the_rest_is_left_alone():
    body = "# T\n\n## 经过\n- 一\n\n## 笔记\n我的话\n"
    out = md.replace_section(body, "经过", "- 一\n- 二")
    assert "- 二" in out and "## 笔记\n我的话" in out
    assert out.index("## 经过") < out.index("## 笔记")


def test_appending_to_a_missing_section_creates_it_at_the_end():
    out = md.append_section("# T\n\n正文\n", "笔记", "新的一行")
    assert out.endswith("## 笔记\n\n新的一行\n")
    again = md.append_section(out, "笔记", "再一行")
    assert again.count("## 笔记") == 1 and again.endswith("新的一行\n再一行\n")


def test_a_heading_inside_code_is_not_a_section():
    body = "# T\n```\n## 经过\n```\n"
    assert not md.has_section(body, "经过")


def test_slugify_keeps_chinese_and_drops_what_files_cannot_hold():
    assert md.slugify('查 Q3/财报: "最终版"?') == "查-Q3-财报-最终版"
    assert md.slugify("///") == "笔记"
    assert len(md.slugify("长" * 100, limit=40)) <= 40


@given(st.text())
def test_parsing_never_raises(text):
    parsed = md.parse(text)
    assert isinstance(parsed.body, str)
    md.links(parsed)
    md.tags_of(parsed)
    md.headings(parsed.body)


@given(st.text())
def test_masking_code_keeps_every_line_and_length(text):
    masked = md.mask_code(text)
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")


@given(
    st.text(alphabet=st.characters(blacklist_characters="#\r"), max_size=200),
    st.text(min_size=1, max_size=30).filter(lambda s: s.strip()),
)
def test_replacing_a_section_twice_equals_once(content, heading):
    heading = " ".join(heading.replace("#", "").split()) or "x"
    body = "# T\n\n正文\n"
    once = md.replace_section(body, heading, content)
    assert md.replace_section(once, heading, content) == once


@pytest.mark.parametrize("name", ["a", "含有 空格", "x.y"])
def test_slugify_is_stable(name):
    assert md.slugify(md.slugify(name)) == md.slugify(name)


def test_properties_are_rendered_one_to_a_line_and_read_back_the_same():
    front = {"origin": "assistant", "created": "2026-11-21", "tags": ["a", "项目"]}
    text = md.render(front, "# 标题\n")
    assert text == "---\norigin: assistant\ncreated: '2026-11-21'\ntags: [a, 项目]\n---\n# 标题\n"
    assert md.parse(text).front == front
    nested = md.render({"a": {"b": 1}, "c": [{"d": 1}]}, "x")
    assert "{" not in nested.split("---")[1] and md.parse(nested).front == {
        "a": {"b": 1},
        "c": [{"d": 1}],
    }


# ---- hostile text: reading a note must always come back quickly -------------------------------
def _within(seconds: float, fn, *args):
    started = time.perf_counter()
    result = fn(*args)
    assert time.perf_counter() - started < seconds
    return result


@pytest.mark.parametrize(
    "line",
    [
        "# a" + " " * 30_000 + "x",  # a heading with a long gap in it
        "## " + "x " * 15_000 + "#" * 15_000 + " y",
        "a" + "`" * 60_000 + "b",  # one long run of backticks
        "[a" * 30_000,  # brackets that never close
        "[[a" * 30_000,
        "# " + "[[a" * 30_000,
    ],
)
def test_a_hostile_line_cannot_make_reading_a_note_take_minutes(line):
    parsed = md.parse(line)
    _within(2.0, md.headings, parsed.body)
    _within(2.0, md.mask_code, parsed.body)
    _within(2.0, md.links, parsed)
    _within(2.0, md.summary_of, parsed)
    _within(2.0, md.title_of, parsed, "stem")
    _within(2.0, md.has_section, parsed.body, "笔记")


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("# 标题", (1, "标题")),
        ("###### 六级", (6, "六级")),
        ("## 标题 ##", (2, "标题")),
        ("### 标题 ###   ", (3, "标题")),
        ("# C#", (1, "C#")),  # a # that touches the word is part of it
        ("# 标题 #号", (1, "标题 #号")),
        ("#标题", None),  # a tag, not a heading
        ("####### 七级", None),
        ("# ", None),
        ("# #", None),
        ("  # 缩进的不算", None),
    ],
)
def test_what_is_a_heading_and_what_it_says(line, found):
    heads = md.headings(line)
    assert [(h.level, h.text) for h in heads] == ([found] if found else [])


def test_a_code_span_closes_with_a_run_of_the_same_length():
    assert [link.target for link in md.links(md.parse("``[[a]]`` [[b]]"))] == ["b"]
    assert [link.target for link in md.links(md.parse("`` [[a]]` [[b]]"))] == ["a", "b"]
    assert [link.target for link in md.links(md.parse("`x` [[a]] `y`"))] == ["a"]


def test_a_note_marked_private_stays_private_when_its_properties_cannot_be_read():
    broken = "---\ntitle: 我的日记: 2026\nprivate: true\n---\n正文\n"
    assert md.parse(broken).front_error
    assert md.declares_private(md.parse(broken))
    assert md.declares_private(md.parse("---\nprivate: 是\n---\n"))
    assert md.declares_private(md.parse("---\nprivate: 'yes'\nbad: [\n---\n"))
    assert not md.declares_private(md.parse("---\nprivate: false\n---\n"))
    assert not md.declares_private(md.parse("---\ntitle: 我的日记: 2026\n---\n"))
    assert not md.declares_private(md.parse("---\nnotes: private: true is a word\nx: [\n---\n"))
    assert not md.declares_private(md.parse("# 正文里写 private: true 不算\n"))
