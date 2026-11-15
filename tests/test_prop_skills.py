"""Whatever a SKILL.md contains, reading it ends in one of two ways: a skill, or a `SkillFormatError`
that says why. Skills come from other people (a download, a folder someone dropped in), and the
loader reads every one of them each time the agent starts: a file that makes the reader raise
something else, or take minutes, would stop the agent for the sake of one bad skill.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from helpers import within
from personal_agent.parsing import load_yaml
from personal_agent.skills.parse import (
    DESCRIPTION_MAX,
    SAFE_NAME,
    SkillFormatError,
    parse_skill,
    split_front_matter,
)

KEYS = st.sampled_from(
    [
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
        "allowed_tools",
        "disable-model-invocation",
        "disable_model_invocation",
        "other",
    ]
)
SCALARS = st.one_of(
    st.sampled_from(
        [
            "pdf-tools",
            "Use this when: the user asks",
            "2023-02-30",
            "2023-02-03 25:61:00",
            "2001-12-14t21:59:43.10-05:00",
            "9" * 5000,
            "1e999999",
            "-.inf",
            ".nan",
            "0o17",
            "0b101",
            "1_000",
            "yes",
            "null",
            "~",
            "true",
            "[a, b, [c]]",
            "{k: v, n: {x: 1}}",
            "!!binary |\n  aGk=",
            "!!set {a, b}",
            "!!omap [a: 1]",
            "&anchor value",
            "*anchor",
            "'quoted: colon'",
            '"escaped \\u0041 \\x00"',
            "|\n  block\n  text",
            ">\n  folded\n  text",
            "- item",
            "? complex\n: key",
            "\u202eoverride",
            "\x00\x01",
            "",
        ]
    ),
    st.text(max_size=120),
)
ENTRIES = st.lists(st.tuples(KEYS, SCALARS), max_size=8)


def document(entries: list[tuple[str, str]], body: str = "Do the thing.") -> str:
    lines = [f"{key}: {value}" for key, value in entries]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def read(text: str):
    """What the reader gives for `text`: a skill, or None for a refusal. Nothing else is allowed."""
    try:
        with within(10):
            return parse_skill(text)
    except SkillFormatError:
        return None


@settings(max_examples=300)
@given(entries=ENTRIES)
def test_front_matter_of_any_shape_gives_a_skill_or_a_refusal_and_nothing_else(entries):
    parsed = read(document(entries))
    if parsed is not None:
        assert SAFE_NAME.fullmatch(parsed.name)
        assert 0 < len(parsed.description) <= DESCRIPTION_MAX
        assert "\n" not in parsed.description


@settings(max_examples=300)
@given(text=st.text(max_size=1500))
def test_any_text_at_all_gives_a_skill_or_a_refusal_and_nothing_else(text):
    read(text)


@settings(max_examples=200)
@given(text=st.text(alphabet="-\n :#!&*[]{}?|>'\"\\x0\r\t ", max_size=300))
def test_the_characters_that_mean_something_to_yaml_do_not_get_past_the_reader(text):
    read("---\n" + text + "\n---\nbody")
    read(text)


@given(front=st.text(max_size=400), body=st.text(max_size=400))
def test_the_split_either_finds_both_halves_or_refuses(front, body):
    try:
        found_front, found_body = split_front_matter(f"---\n{front}\n---\n{body}")
    except SkillFormatError:
        return
    assert isinstance(found_front, str) and isinstance(found_body, str)


def test_a_date_that_does_not_exist_is_still_text():
    parsed = parse_skill("---\nname: dates\ndescription: 2023-02-30\n---\nbody")
    assert parsed.description == "2023-02-30"


def test_a_real_date_reads_as_the_text_the_author_wrote():
    parsed = parse_skill(
        "---\nname: dates\ndescription: Since 2023-02-03\nmetadata:\n  when: 2024-01-05\n---\n"
    )
    assert parsed.description == "Since 2023-02-03"
    assert parsed.metadata == {"when": "2024-01-05"}


def test_a_number_of_thousands_of_digits_is_a_refusal_not_a_crash():
    text = f"---\nname: big\ndescription: {'9' * 5000}\n---\nbody"
    try:
        parse_skill(text)
    except SkillFormatError as exc:
        assert "out of range" in str(exc)
    else:
        raise AssertionError("a 5000-digit number was accepted")


def test_the_yaml_reader_reports_what_it_cannot_hold_as_a_yaml_error():
    import pytest
    import yaml

    for text in ("n: " + "9" * 5000, "a: &x [1]\nb: *x", "[" * 5000):
        with pytest.raises(yaml.YAMLError):
            load_yaml(text)
