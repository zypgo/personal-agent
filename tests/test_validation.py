"""What a person is told when something they wrote is wrong: where, and why, and never the secret
they pasted into the wrong place.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from personal_agent.config import Config
from personal_agent.validation import HIDDEN, hide_credentials, problems

KEY = (
    "sk-" + "proj-" + "Zq8Lm2Xv9Rt4Ws7Yb1Nc"
)  # shaped like an OpenAI key; assembled so nothing scans it
BLOB = "q" * 40  # not a known format, but nobody types forty characters without a space as prose


def refusal(**sections) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Config.model_validate(sections)
    return caught.value


def test_a_problem_says_where_it_is_and_what_is_wrong():
    [(where, what)] = problems(refusal(notify={"quiet_hours": "late"}))
    assert where == "notify.quiet_hours"
    assert "23:00-08:00" in what


def test_a_short_value_that_is_only_a_typo_is_still_shown_so_it_can_be_found():
    [(_, what)] = problems(refusal(notify={"quiet_hours": "late"}))
    assert "'late'" in what


@pytest.mark.parametrize("pasted", [KEY, BLOB, f"Bearer {KEY}", f"{KEY}@example.com"])
def test_a_credential_pasted_into_a_setting_is_not_repeated(pasted):
    [(where, what)] = problems(refusal(notify={"quiet_hours": pasted}))
    assert where == "notify.quiet_hours"
    assert KEY not in what and BLOB not in what
    assert HIDDEN in what


def test_a_credential_used_as_the_name_of_a_setting_is_not_repeated_in_where():
    found = problems(refusal(secrets={KEY: {"env": "NOT A NAME"}}))
    assert found
    for where, what in found:
        assert KEY not in where and KEY not in what
        assert where.startswith("secrets.") and HIDDEN in where


def test_a_credential_nested_in_a_list_or_a_table_is_found_wherever_it_is():
    for value in ([KEY], {"x": [KEY]}, [[{"k": KEY}]]):
        for where, what in problems(refusal(notify={"quiet_hours": value})):
            assert KEY not in where + what


def test_what_a_validator_says_about_a_whole_section_is_cleaned_too():
    class Section(BaseModel):
        a: str
        b: str

        @field_validator("b")
        @classmethod
        def _differs(cls, value: str) -> str:
            raise ValueError(f"cannot use {value!r} here")

    with pytest.raises(ValidationError) as caught:
        Section.model_validate({"a": "x", "b": KEY})
    [(_, what)] = problems(caught.value)
    assert KEY not in what


def test_parts_of_the_path_can_be_left_out():
    found = problems(refusal(mcp={"servers": [{"name": "x", "command": "y", "timeout": "no"}]}))
    assert found and all(where.startswith("mcp.servers.0") for where, _ in found)
    shown = problems(
        refusal(mcp={"servers": [{"name": "x", "command": "y", "timeout": "no"}]}),
        skip={"servers", "mcp"},
    )
    assert all(not where.startswith(("mcp", "servers")) for where, _ in shown)


def test_the_top_level_has_an_empty_where():
    class Whole(BaseModel):
        pass

    with pytest.raises(ValidationError) as caught:
        Whole.model_validate("not a table")
    assert problems(caught.value)[0][0] == ""


def test_text_with_no_credential_in_it_is_left_alone():
    assert hide_credentials("plain words") == "plain words"
    assert hide_credentials("got short", "short") == "got short"


def test_a_key_is_hidden_by_its_shape_even_when_nothing_says_it_was_typed():
    assert KEY not in hide_credentials(f"got {KEY}")


def test_a_long_unbroken_piece_of_what_was_typed_is_hidden_even_if_it_has_no_known_shape():
    assert hide_credentials(f"got {BLOB}!", BLOB) == f"got {HIDDEN}!"


def test_a_sentence_is_not_a_credential_however_long():
    sentence = "hello world, this is a long sentence"
    assert hide_credentials(f"got {sentence!r}", sentence) == f"got {sentence!r}"


def test_a_credential_at_the_bottom_of_a_very_deep_document_is_still_found():
    """A file of nothing but brackets must be refused in words, not with a RecursionError (the
    walk over what was typed is a loop, so no depth can overflow it)."""
    deep: object = BLOB
    for _ in range(50_000):
        deep = {"inside": [deep]}
    assert hide_credentials(f"got {BLOB}!", deep) == f"got {HIDDEN}!"
