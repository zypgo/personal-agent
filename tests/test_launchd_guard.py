"""The guard every test runs under: launchd can be looked at, never changed.

On this machine a CLI test that was meant to stop at "run `pa init` first" once installed and
loaded a real job (a mutation had removed the check it relied on). The guard in `conftest.py`
makes that impossible whatever the code under test does; this file checks the guard itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent import launchd
from personal_agent.launchd import ServiceManager

REAL_HOME_AGENTS = Path("~/Library/LaunchAgents").expanduser()


@pytest.mark.parametrize(
    "verb",
    [
        "bootstrap",
        "bootout",
        "kickstart",
        "enable",
        "disable",
        "load",
        "unload",
        "remove",
        "submit",
    ],
)
def test_changing_the_real_launchd_from_a_test_is_refused(verb):
    said = rf"real launchd: launchctl {verb} gui/1/local\.personal-agent\.guard"
    with pytest.raises(AssertionError, match=said):
        launchd.system_launchctl(verb, "gui/1/local.personal-agent.guard")


def test_a_call_without_a_verb_is_refused_too():
    with pytest.raises(AssertionError, match="real launchd"):
        launchd.system_launchctl()


def test_looking_is_allowed(monkeypatch):
    seen = []

    class Looked:
        returncode, stdout, stderr = 113, "", "Could not find service"

    monkeypatch.setattr(
        launchd.subprocess, "run", lambda command, **kw: seen.append(command) or Looked
    )
    done = launchd.system_launchctl("print", "gui/1/local.personal-agent.guard")
    assert done.code == 113 and seen == [
        ["/bin/launchctl", "print", "gui/1/local.personal-agent.guard"]
    ]


def test_a_manager_built_the_default_way_is_held_to_the_same_rule(paths, tmp_path):
    manager = ServiceManager(paths)
    with pytest.raises(AssertionError, match="real launchd"):
        manager.launchctl("bootstrap", "gui/1", "/tmp/x.plist")
    assert manager.agents != REAL_HOME_AGENTS
    assert tmp_path in manager.agents.parents


def test_the_default_job_folder_is_never_the_real_one_under_test():
    assert launchd.agents_dir() != REAL_HOME_AGENTS
