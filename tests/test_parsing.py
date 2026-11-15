"""A document nested a few thousand levels deep makes every recursive reader raise `RecursionError`,
which is not a `ValueError`, so each place that reads text somebody else wrote has to say "that is
not valid" for it too. A web server, a webhook sender or a downloaded skill can send one for free.
"""

from __future__ import annotations

import tomllib

import pytest
import yaml

from helpers import within
from personal_agent.channels.webhooks.service import parse_body
from personal_agent.config import ConfigError, load_config
from personal_agent.db import loads
from personal_agent.evals.spec import ScenarioError
from personal_agent.evals.spec import parse as parse_scenario
from personal_agent.parsing import load_yaml, loads_json, loads_toml
from personal_agent.paths import Paths
from personal_agent.security.ssrf import Fetched
from personal_agent.skills.parse import SkillFormatError, parse_skill
from personal_agent.tools.guard import parse_args
from personal_agent.tools.web import _readable

DEEP = 20_000
DEEP_JSON = "[" * DEEP + "]" * DEEP
DEEP_JSON_OBJECT = '{"a":' * DEEP + "1" + "}" * DEEP
DEEP_YAML = "[" * DEEP + "]" * DEEP
DEEP_TOML = "x = " + "[" * DEEP + "]" * DEEP


# ---- the readers --------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [DEEP_JSON, DEEP_JSON_OBJECT, "[" * DEEP])
def test_json_that_is_too_deep_is_a_value_error_like_json_that_is_not_json(text):
    with pytest.raises(ValueError, match="too deeply"):
        loads_json(text)


def test_json_that_is_fine_is_read_as_usual():
    assert loads_json('{"a": [1, 2, {"b": null}]}') == {"a": [1, 2, {"b": None}]}
    assert loads_json(b"[true]") == [True]
    with pytest.raises(ValueError, match="Expecting"):
        loads_json("{")


def test_yaml_that_is_too_deep_is_a_yaml_error():
    with pytest.raises(yaml.YAMLError, match="too deeply"):
        load_yaml(DEEP_YAML)
    assert load_yaml("a: [1, 2]\nb: {c: d}\n") == {"a": [1, 2], "b": {"c": "d"}}


def test_toml_that_is_too_deep_is_a_toml_error():
    with pytest.raises(tomllib.TOMLDecodeError, match="too deeply"):
        loads_toml(DEEP_TOML)
    assert loads_toml("[a]\nb = [1, 2]\n") == {"a": {"b": [1, 2]}}


# ---- every place that reads somebody else's text -------------------------------------------------
def test_the_arguments_a_model_wrote_that_are_too_deep_are_no_arguments():
    assert parse_args(DEEP_JSON) == {}
    assert parse_args(DEEP_JSON_OBJECT) == {}


def test_a_stored_value_that_is_too_deep_reads_back_as_the_default():
    assert loads(DEEP_JSON, default="fallback") == "fallback"


def test_a_webhook_body_that_is_too_deep_is_kept_as_text():
    assert parse_body(DEEP_JSON.encode(), "application/json") == DEEP_JSON


def test_a_json_page_that_is_too_deep_is_shown_as_it_came():
    page = Fetched(
        url="https://example.com/x",
        status=200,
        headers={"content-type": "application/json"},
        body=DEEP_JSON.encode(),
    )
    assert _readable(page) == DEEP_JSON


def test_a_skill_whose_front_matter_is_too_deep_is_refused_in_words():
    text = f"---\nname: x\ndescription: y\nextra: {DEEP_YAML}\n---\nbody\n"
    with pytest.raises(SkillFormatError, match="not valid YAML"):
        parse_skill(text)


def test_a_scenario_that_is_too_deep_is_refused_in_words():
    with pytest.raises(ScenarioError, match="not valid YAML"):
        parse_scenario(DEEP_YAML, source="deep.yaml")


def test_a_config_file_that_is_too_deep_is_refused_in_words(tmp_path):
    paths = Paths.resolve(tmp_path)
    paths.ensure()
    paths.config_file.write_text(DEEP_TOML, encoding="utf-8")
    with pytest.raises(ConfigError, match="too deeply"):
        load_config(paths)


# ---- aliases: a few hundred bytes that expand to more than the machine has --------------------------
def alias_bomb(key: str, levels: int = 14, width: int = 9) -> str:
    """Each list is `width` references to the one before: loading shares them and is instant, but
    printing, walking or validating the result never finishes."""
    lines = ["a0: &a0 [" + ",".join(["x"] * width) + "]"]
    for i in range(1, levels):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * width) + "]")
    lines.append(f"{key}: *a{levels - 1}")
    return "\n".join(lines)


def test_yaml_aliases_are_refused_so_a_small_document_cannot_become_a_huge_one():
    with within(5), pytest.raises(yaml.YAMLError, match="aliases"):
        load_yaml(alias_bomb("big"))
    with pytest.raises(yaml.YAMLError, match="aliases"):
        load_yaml("base: &b {a: 1}\nuse: *b\n")
    with pytest.raises(yaml.YAMLError, match="aliases"):
        load_yaml("base: &b {a: 1}\nuse:\n  <<: *b\n")


def test_yaml_anchors_alone_are_harmless_and_still_read():
    assert load_yaml("a: &x [1, 2]\nb: [3]\n") == {"a": [1, 2], "b": [3]}


@pytest.mark.parametrize("key", ["description", "metadata", "name", "allowed-tools", "license"])
def test_a_skill_with_an_alias_bomb_in_its_front_matter_is_refused_at_once(key):
    text = f"---\nname: x\ndescription: d\n{alias_bomb(key)}\n---\nbody\n"
    with within(5), pytest.raises(SkillFormatError, match="aliases"):
        parse_skill(text)


def test_a_scenario_with_an_alias_bomb_is_refused_at_once():
    with within(5), pytest.raises(ScenarioError, match="not valid YAML"):
        parse_scenario(alias_bomb("id"), source="bomb.yaml")


def test_a_front_matter_value_that_is_a_list_reads_as_its_items_and_a_table_as_nothing():
    text = "---\nname: x\ndescription: [first, second]\nmetadata:\n  tags: [a, b]\n  nested: {k: v}\n---\nbody\n"
    parsed = parse_skill(text)
    assert parsed.description == "first second"
    assert parsed.metadata == {"tags": "a b", "nested": ""}
    with pytest.raises(SkillFormatError, match="no `description`"):
        parse_skill("---\nname: x\ndescription: {a: b}\n---\nbody\n")
