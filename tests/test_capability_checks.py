"""`pa doctor`'s view of what this machine lets the agent do."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers import invoke
from personal_agent.doctor import capability_checks
from personal_agent.paths import Paths


def checks_for(home: Path, toml: str = ""):
    paths = Paths.resolve(home)
    paths.ensure()
    if toml:
        paths.config_file.write_text(toml)
    return {c.name: c for c in capability_checks(paths)}


needs_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="needs macOS sandbox-exec",
)


@needs_seatbelt
def test_a_working_sandbox_is_described(tmp_path):
    check = checks_for(tmp_path / "h")["shell sandbox"]
    assert check.status == "ok" and "seatbelt" in check.detail and "no network" in check.detail


def test_a_missing_sandbox_is_a_warning_that_says_the_shell_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    check = checks_for(tmp_path / "h")["shell sandbox"]
    assert (
        check.status == "warn"
        and "shell tool is off" in check.detail
        and "docker pull" in check.fix
    )


def test_running_without_isolation_is_flagged_even_when_chosen(tmp_path):
    toml = '[sandbox]\nbackend = "none"\ni_understand_no_sandbox = true\n'
    check = checks_for(tmp_path / "h", toml)["shell sandbox"]
    assert check.status == "warn" and "NONE" in check.detail


def test_a_switched_off_shell_needs_no_sandbox(tmp_path):
    check = checks_for(tmp_path / "h", "[tools]\nshell = false\n")["shell"]
    assert check.status == "ok" and "switched off" in check.detail


def test_secrets_are_checked_without_revealing_them(tmp_path, monkeypatch):
    toml = (
        '[tools]\nshell = false\n[secrets.github]\nenv = "PA_TEST_GH"\nhosts = ["api.github.com"]\n'
    )
    monkeypatch.delenv("PA_TEST_GH", raising=False)
    missing = checks_for(tmp_path / "h1", toml)["secret github"]
    assert missing.status == "warn" and "PA_TEST_GH is not set" in missing.detail

    monkeypatch.setenv("PA_TEST_GH", "ghp_" + "z" * 30)
    present = checks_for(tmp_path / "h2", toml)["secret github"]
    assert present.status == "ok" and "api.github.com" in present.detail
    assert "z" * 30 not in present.detail


def test_a_broken_config_is_left_to_the_static_checks(tmp_path):
    assert checks_for(tmp_path / "h", "this is not toml [[[") == {}


def test_doctor_prints_the_capability_section(home):
    assert invoke("init").exit_code == 0
    result = invoke("doctor")
    assert "shell" in result.output


def test_the_web_tools_are_described(tmp_path):
    check = checks_for(tmp_path / "h", "[tools]\nshell = false\n")["web"]
    assert check.status == "ok"
    assert "web_fetch, http_request" in check.detail and "hosted search: auto" in check.detail


@pytest.mark.parametrize(
    ("toml", "expected"),
    [
        ("web_fetch = false", "tools: http_request;"),
        ("web_fetch = false\nhttp_request = false", "tools: none;"),
        ('web_search = "off"', "hosted search: off"),
        ('web_search = "cached"', "hosted search: cached"),
    ],
)
def test_switched_off_web_tools_are_reported(tmp_path, toml, expected):
    check = checks_for(tmp_path / "h", f"[tools]\nshell = false\n{toml}\n")["web"]
    assert expected in check.detail


def test_hosted_search_is_reported_unavailable_off_the_responses_api(tmp_path):
    toml = '[tools]\nshell = false\n[model]\napi = "chat_completions"\n'
    assert "unavailable" in checks_for(tmp_path / "h", toml)["web"].detail


def test_a_small_private_allowance_is_fine_and_a_huge_one_is_flagged(tmp_path):
    small = '[tools]\nshell = false\n[net]\nallow_private = ["192.168.1.0/24", "nas.local"]\n'
    check = checks_for(tmp_path / "a", small)["private network access"]
    assert check.status == "ok" and "192.168.1.0/24" in check.detail

    huge = '[tools]\nshell = false\n[net]\nallow_private = ["10.0.0.0/8", "192.168.1.5"]\n'
    check = checks_for(tmp_path / "b", huge)["private network access"]
    assert (
        check.status == "warn"
        and "10.0.0.0/8" in check.detail
        and "192.168.1.5" not in check.detail
    )


def test_no_private_allowance_means_no_line_about_it(tmp_path):
    assert "private network access" not in checks_for(tmp_path / "h", "[tools]\nshell = false\n")


def fake_dns(monkeypatch, *addresses):
    monkeypatch.setattr(
        "personal_agent.doctor._resolve_probe", lambda name, timeout=3.0: list(addresses)
    )


def test_a_proxy_in_fake_ip_mode_is_detected_with_the_fix(tmp_path, monkeypatch):
    fake_dns(monkeypatch, "198.18.1.59")
    check = checks_for(tmp_path / "h", "[tools]\nshell = false\n")["proxy fake-IP DNS"]
    assert (
        check.status == "warn"
        and "198.18.1.59" in check.detail
        and "refuse every site" in check.detail
    )
    assert '"198.18.0.0/15"' in check.fix and "allow_private" in check.fix


def test_fake_ip_mode_is_fine_once_the_owner_allowed_it(tmp_path, monkeypatch):
    fake_dns(monkeypatch, "198.18.1.59")
    toml = '[tools]\nshell = false\n[net]\nallow_private = ["198.18.0.0/15"]\n'
    check = checks_for(tmp_path / "h", toml)["proxy fake-IP DNS"]
    assert check.status == "ok" and "allows it" in check.detail


def test_ordinary_dns_and_a_failed_lookup_say_nothing(tmp_path, monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34", "2606:2800:21f:cb07:6820:80da:af6b:8b2c")
    assert "proxy fake-IP DNS" not in checks_for(tmp_path / "a", "[tools]\nshell = false\n")
    fake_dns(monkeypatch)  # offline: no answer
    assert "proxy fake-IP DNS" not in checks_for(tmp_path / "b", "[tools]\nshell = false\n")


def test_nothing_is_probed_when_there_are_no_web_tools(tmp_path, monkeypatch):
    def forbidden(name, timeout=3.0):
        raise AssertionError("the network must not be touched")

    monkeypatch.setattr("personal_agent.doctor._resolve_probe", forbidden)
    toml = "[tools]\nshell = false\nweb_fetch = false\nhttp_request = false\n"
    assert "proxy fake-IP DNS" not in checks_for(tmp_path / "h", toml)
