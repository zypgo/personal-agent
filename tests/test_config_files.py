"""The files the owner edits by hand (`config.toml`, `.env`): whatever is in them, the program says
what is wrong in words, and never fails with a traceback.

The usual way to break them is an editor that saves "ANSI" (GBK on a Chinese system) instead of
UTF-8, which used to end in a raw `UnicodeDecodeError` at start-up.
"""

from __future__ import annotations

import os

import pytest

from helpers import invoke
from personal_agent.config import ConfigError, load_config, load_env, read_text_file
from personal_agent.doctor import static_checks
from personal_agent.paths import Paths

GBK = "\u4f60\u597d\uff0c\u4e16\u754c".encode("gbk")  # "hello, world"


@pytest.fixture
def paths(home) -> Paths:
    p = Paths.resolve(home)
    p.ensure()
    return p


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("OPENAI_API_KEY", "PA_TEST_VALUE"):
        monkeypatch.delenv(name, raising=False)


# ---- reading text ----------------------------------------------------------------------------
def test_text_that_is_not_utf8_is_an_error_that_says_how_to_fix_it(paths):
    paths.config_file.write_bytes(b"[agent]\nname = '" + GBK + b"'\n")

    with pytest.raises(ConfigError) as caught:
        read_text_file(paths.config_file)

    message = str(caught.value)
    assert str(paths.config_file) in message and "not UTF-8" in message and "save" in message


def test_a_byte_order_mark_is_not_part_of_the_text(paths):
    paths.config_file.write_bytes(b"\xef\xbb\xbf[agent]\nname = 'Ada'\n")
    assert read_text_file(paths.config_file).startswith("[agent]")
    assert load_config(paths).agent.name == "Ada"


def test_a_file_that_cannot_be_read_at_all_is_a_config_error(paths):
    paths.config_file.mkdir()  # a directory where the file should be
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(paths)


# ---- config.toml -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [b"\xff\xfe\x00bad", b"[agent]\nname = '\xc3\x28'", b"\x00", b"= = =", b"[model]\nmain = 5\n"],
    ids=["bad-bytes", "bad-utf8-in-value", "nul", "syntax", "wrong-type"],
)
def test_a_broken_config_is_always_a_config_error(paths, payload):
    paths.config_file.write_bytes(payload)
    with pytest.raises(ConfigError):
        load_config(paths)


@pytest.mark.parametrize(
    ("section", "needs"),
    [
        ("[sandbox]\nbackend = 'none'\n", "sandbox.i_understand_no_sandbox"),
        ("[policy]\nmode = 'autonomous'\n", "policy.i_understand_autonomous_risk"),
    ],
    ids=["no-sandbox", "autonomous"],
)
def test_a_setting_that_removes_a_protection_must_say_so_where_it_is_set(paths, section, needs):
    paths.config_file.write_text(section)
    with pytest.raises(ConfigError, match=needs):
        load_config(paths)

    paths.config_file.write_text(section + needs.split(".")[1] + " = true\n")
    assert load_config(paths) is not None


# ---- .env ------------------------------------------------------------------------------------
def test_env_values_are_loaded_but_never_override_the_real_environment(paths, monkeypatch):
    paths.env_file.write_text(
        "# a comment\nexport PA_TEST_VALUE='from the file'\nOPENAI_API_KEY=sk-" + "f" * 24 + "\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "already-there")

    load_env(paths)

    assert os.environ["PA_TEST_VALUE"] == "from the file"
    assert os.environ["OPENAI_API_KEY"] == "already-there"


def test_a_missing_env_file_is_fine(paths):
    load_env(paths)
    assert "PA_TEST_VALUE" not in os.environ


def test_only_the_homes_env_file_is_read_never_one_in_the_working_directory(
    paths, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PA_TEST_VALUE=someone else's project\n")
    monkeypatch.chdir(elsewhere)

    load_env(paths)

    assert "PA_TEST_VALUE" not in os.environ


def test_an_env_file_with_a_byte_order_mark_still_names_its_first_variable(paths):
    paths.env_file.write_bytes(b"\xef\xbb\xbfPA_TEST_VALUE=first\n")
    load_env(paths)
    assert os.environ["PA_TEST_VALUE"] == "first"


def test_an_env_file_that_is_not_utf8_is_a_config_error(paths):
    paths.env_file.write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_env(paths)


# ---- what the commands say -------------------------------------------------------------------
def test_doctor_reports_an_env_file_that_is_not_utf8_instead_of_crashing(paths):
    paths.env_file.write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")
    paths.env_file.chmod(0o600)

    result = invoke("doctor")

    assert result.exit_code == 1
    assert "env file" in result.output and "not UTF-8" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_doctor_reports_a_config_that_is_not_utf8(paths):
    paths.config_file.write_bytes(b"[agent]\nname = '" + GBK + b"'\n")

    result = invoke("doctor")

    assert result.exit_code == 1 and "not UTF-8" in result.output


def test_static_checks_name_the_env_file_when_it_cannot_be_read(paths):
    paths.env_file.write_bytes(GBK)
    found = {c.name: c for c in static_checks(paths)}
    assert found["env file"].status == "fail" and "UTF-8" in found["env file"].fix


@pytest.mark.parametrize(
    ("which", "command", "code"),
    [
        ("env_file", ["ask", "hello"], 2),
        ("config_file", ["ask", "hello"], 2),
        ("env_file", ["timers"], 2),
        ("config_file", ["timers"], 2),
        ("env_file", ["service", "status"], 2),
        ("config_file", ["service", "status"], 0),  # still says how the service is doing
    ],
)
def test_commands_say_what_is_wrong_with_the_file_not_show_a_traceback(
    paths, which, command, code, monkeypatch
):
    from personal_agent.commands import service
    from personal_agent.launchd import ServiceManager
    from test_launchd import FakeLaunchctl

    monkeypatch.setattr(
        service,
        "manager_for",
        lambda p: ServiceManager(
            p,
            platform="darwin",
            launchctl=FakeLaunchctl(),
            agents=p.home / "launchagents",
        ),
    )
    getattr(paths, which).write_bytes(GBK)

    result = invoke(*command)

    assert result.exit_code == code, result.output
    assert "not UTF-8" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_eval_run_live_says_what_is_wrong_with_the_env_file(paths):
    paths.env_file.write_bytes(GBK)

    result = invoke("eval", "run", "--live")

    assert result.exit_code == 2 and "not UTF-8" in result.output
    assert isinstance(result.exception, SystemExit)
