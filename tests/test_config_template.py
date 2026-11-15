"""The config.toml that `pa init` writes is documentation: its examples must be valid."""

from __future__ import annotations

import re
import tomllib

from personal_agent.config import DEFAULT_CONFIG_TOML, Config

EXAMPLE = re.compile(r"^# ?((?:\[[A-Za-z_.]+\])|(?:[a-z_]+ = .*))$")


def uncommented(toml: str) -> str:
    """Turn every commented-out table header or assignment into a live one."""
    return "\n".join(
        m.group(1) if (m := EXAMPLE.match(line)) else line for line in toml.splitlines()
    )


def test_the_default_file_is_valid_as_it_is():
    assert Config.model_validate(tomllib.loads(DEFAULT_CONFIG_TOML)) == Config()


def test_every_commented_example_is_valid_when_switched_on():
    live = uncommented(DEFAULT_CONFIG_TOML)
    assert live != DEFAULT_CONFIG_TOML  # the examples were really switched on
    config = Config.model_validate(tomllib.loads(live))
    assert config.policy.allow_when_tainted and config.net.allow_private and config.secrets
