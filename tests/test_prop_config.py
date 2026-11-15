"""`config.toml` is typed by a person (or a model that was asked to help). Whatever is in it, the
program either accepts it or says in words what is wrong, never with a traceback, and a secret
pasted into the wrong setting is never repeated back on the terminal.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from personal_agent.config import (
    Config,
    ConfigError,
    McpServerConfig,
    _describe,
    load_config,
)
from personal_agent.doctor import static_checks
from personal_agent.paths import Paths

# ---- values a person can write in TOML ----------------------------------------------------------
SECRET = (
    "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0" * 2
)  # the shape of a key, built so no scanner sees one
SCALARS = st.one_of(
    st.booleans(),
    st.integers(-(2**63), 2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=60),
    st.sampled_from(
        ["", " ", "0", "-1", "all", "true", "none", "auto", "https://x.y", "http://169.254.169.254"]
    ),
)
VALUES = st.recursive(
    SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.text(max_size=12), inner, max_size=4),
    ),
    max_leaves=12,
)


def keys_of(model: Any) -> list[str]:
    return list(model.model_fields)


SECTIONS: dict[str, list[str]] = {
    name: keys_of(field.annotation)
    for name, field in Config.model_fields.items()
    if hasattr(field.annotation, "model_fields")
}


@st.composite
def configs(draw, values=VALUES) -> dict[str, Any]:
    """A configuration with a few settings, each a value of any shape: mostly the right names in
    the wrong place or the wrong type, which is how people get configuration wrong."""
    data: dict[str, Any] = {}
    for _ in range(draw(st.integers(1, 4))):
        section = draw(st.sampled_from(sorted(SECTIONS)))
        key = draw(st.one_of(st.sampled_from(SECTIONS[section]), st.text(max_size=12)))
        data.setdefault(section, {})[key] = draw(values)
    if draw(st.booleans()):
        names = st.one_of(st.text(max_size=12), st.just(SECRET))
        data["secrets"] = draw(st.dictionaries(names, values, max_size=3))
    return data


# ---- accepted or refused -------------------------------------------------------------------------
@settings(max_examples=600)
@given(configs())
def test_a_configuration_is_either_accepted_or_refused_with_a_validation_error(data):
    try:
        config = Config.model_validate(data)
    except ValidationError as refusal:
        assert _describe(refusal).strip()
    else:
        assert config.model_dump()  # and what was accepted can be looked at


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_every_section_survives_every_kind_of_value_for_every_one_of_its_settings(section):
    """Not random: each setting of each section gets each kind of value, once."""
    kinds: list[Any] = [None, True, 0, -1, 2**70, 1.5, "", "x" * 300, [], [1], {}, {"a": 1}, [[]]]
    for key in SECTIONS[section]:
        for value in kinds:
            try:
                Config.model_validate({section: {key: value}})
            except ValidationError as refusal:
                assert _describe(refusal).strip()


# ---- a secret in the wrong place ----------------------------------------------------------------
LEAKY = st.one_of(
    st.just(SECRET),
    st.just(f"{SECRET}@host"),
    st.just(f"Bearer {SECRET}"),
    st.just([SECRET]),
    st.just({SECRET: SECRET}),
    st.just({"name": SECRET}),
)


@settings(max_examples=600)
@given(configs(LEAKY))
def test_a_refusal_never_repeats_a_secret_that_was_pasted_into_the_wrong_setting(data):
    try:
        Config.model_validate(data)
    except ValidationError as refusal:
        assert SECRET not in _describe(refusal), _describe(refusal)


def test_every_setting_that_names_an_environment_variable_refuses_a_pasted_key_without_repeating_it():
    for section, key in [
        ("telegram", "token_env"),
        ("webhooks", "routes"),
    ]:
        value: Any = {"x": {"secret_env": SECRET}} if key == "routes" else SECRET
        with pytest.raises(ValidationError) as refusal:
            Config.model_validate({section: {key: value}})
        assert SECRET not in _describe(refusal.value)
    with pytest.raises(ValidationError) as refusal:
        Config.model_validate({"secrets": {"x": {"env": SECRET}}})
    assert SECRET not in _describe(refusal.value)
    with pytest.raises(ValidationError) as refusal:
        McpServerConfig.model_validate({"name": "x", "command": "y", "env": {"A": SECRET}})
    # An MCP server's environment *values* may legitimately be references only; a literal secret
    # there is refused or kept as the owner's own value, but never echoed.
    assert SECRET not in _describe(refusal.value)


# ---- the file ------------------------------------------------------------------------------------
def quote(text: str) -> str:
    escaped = []
    for ch in text:
        if ch in '"\\':
            escaped.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            escaped.append(f"\\u{ord(ch):04x}")
        else:
            escaped.append(ch)
    return '"' + "".join(escaped) + '"'


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return "{" + ", ".join(f"{quote(k)} = {toml_value(v)}" for k, v in value.items()) + "}"


def toml_file(data: dict[str, Any]) -> str:
    return "".join(f"{quote(name)} = {toml_value(value)}\n" for name, value in data.items())


@pytest.fixture(scope="module")
def paths(tmp_path_factory) -> Paths:
    p = Paths.resolve(tmp_path_factory.mktemp("home"))
    p.ensure()
    return p


@settings(max_examples=300)
@given(data=configs())
def test_a_file_is_loaded_or_refused_in_words_and_the_doctor_reads_it_either_way(data, paths):
    paths.config_file.write_text(toml_file(data), encoding="utf-8")
    try:
        load_config(paths)
    except ConfigError as refusal:
        assert str(refusal)
    checks = static_checks(paths)  # never raises: a broken file is a failed check
    assert checks and all(check.name and check.detail for check in checks)


JUNK_FILES = st.one_of(
    st.binary(max_size=200),
    st.text(max_size=200).map(str.encode),
    st.sampled_from(
        [
            b"[agent",
            b"[agent]\nname = ",
            b"[agent]\nname = 'x'\n[agent]\n",
            b"a = 1\na = 2\n",
            b"[[mcp.servers]]\n",
            b"\xff\xfe[\x00a\x00]\x00",
            b"x = 0x" + b"f" * 5000,
            b"x = " + b"[" * 3000 + b"]" * 3000,
            b"[" + b"a." * 2000 + b"b]",
        ]
    ),
)


@settings(max_examples=400)
@given(blob=JUNK_FILES)
def test_a_file_of_any_bytes_is_loaded_or_refused_in_words(blob, paths):
    paths.config_file.write_bytes(blob)
    try:
        load_config(paths)
    except ConfigError as refusal:
        assert str(refusal)
    assert static_checks(paths)
