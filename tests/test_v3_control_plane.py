"""What the deployed control plane refuses, however it was configured or started."""

from __future__ import annotations

import os
import sys

import pytest

from personal_agent import deploy
from personal_agent.config import Config
from personal_agent.deploy import prepare_home, validate_control_config


def deployed(**changes):
    web = {"enabled": True, "public_url": "https://example.test"}
    return Config.model_validate({"web": web, **changes})


def test_a_control_plane_has_no_shell_even_if_it_is_given_a_configuration_with_one(
    make_app, monkeypatch
):
    monkeypatch.setenv("PA_CONTROL_ONLY", "1")
    assert Config().tools.shell
    assert not make_app(config=Config()).config.tools.shell


def test_a_startup_with_the_shell_enabled_is_refused():
    validate_control_config(deployed(tools={"shell": False}))
    with pytest.raises(ValueError, match="shell"):
        validate_control_config(deployed(tools={"shell": True}))


def test_a_startup_without_a_secure_public_address_is_refused():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_control_config(Config.model_validate({"tools": {"shell": False}}))
    with pytest.raises(ValueError, match="HTTPS"):
        validate_control_config(
            Config.model_validate(
                {
                    "web": {"enabled": True, "public_url": "http://example.test"},
                    "tools": {"shell": False},
                }
            )
        )


def test_administrator_commands_are_held_to_the_same_checks_as_the_service(tmp_path, monkeypatch):
    # `main` changes the umask and the environment of the whole process; give both back.
    previous = os.umask(0o077)
    monkeypatch.setenv("PA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PA_CONTROL_ONLY", "1")
    monkeypatch.delenv("PA_WEB_PUBLIC_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.setattr(deploy, "prepare_home", lambda home: home)
    monkeypatch.setattr(sys, "argv", ["deploy", "admin", "--help"])
    try:
        with pytest.raises(SystemExit) as stopped:
            deploy.main()
    finally:
        os.umask(previous)
    assert "PA_WEB_PUBLIC_URL" in str(stopped.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the folder is chowned and privileges drop")
def test_a_data_folder_is_used_only_by_the_account_that_owns_it(tmp_path, monkeypatch):
    home = tmp_path / "volume"
    assert prepare_home(home) == home
    monkeypatch.setattr(os, "geteuid", lambda: home.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="owned"):
        prepare_home(home)
