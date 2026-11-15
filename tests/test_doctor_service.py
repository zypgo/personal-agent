"""`pa doctor` and the background service: say something only when a service was asked for, and
then only what the owner can act on."""

from __future__ import annotations

import pytest

from personal_agent import doctor
from personal_agent.doctor import _service_checks
from personal_agent.launchd import ServiceManager
from test_launchd import WAITING, FakeLaunchctl


@pytest.fixture
def where(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "somebody"))
    return tmp_path


def installed(paths, where, *, loaded=True, program=None, fake=None):
    """A manager whose job file is on disk, running `program` (default: a program that exists)."""
    if program is None:
        program = where / "tools" / "bin" / "python"
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_text("")
        program.chmod(0o755)
    fake = fake or FakeLaunchctl(loaded=loaded)
    manager = ServiceManager(
        paths,
        launchctl=fake,
        agents=where / "agents",
        uid=501,
        python=str(program),
        platform="darwin",
        probe=lambda _p: None,
        linter=lambda _f: None,
        sleep=lambda _s: None,
    )
    manager.install(grace_s=30, start=False)
    return manager, fake


def only(checks):
    [check] = checks
    assert check.name == "background service"
    return check


def test_nothing_is_said_when_no_service_was_asked_for(paths, where):
    manager = ServiceManager(
        paths, launchctl=FakeLaunchctl(), agents=where / "agents", platform="darwin"
    )
    assert _service_checks(paths, manager) == []


def test_nothing_is_said_off_a_mac(paths, where, monkeypatch):
    manager, _ = installed(paths, where)
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert _service_checks(paths, manager) == []


def test_a_running_service_is_fine_and_its_process_is_named(paths, where):
    manager, _ = installed(paths, where)
    check = only(_service_checks(paths, manager))
    assert check.status == "ok" and "pid 689" in check.detail


def test_a_job_file_that_is_not_loaded_says_how_to_load_it(paths, where):
    manager, _ = installed(paths, where, loaded=False)
    check = only(_service_checks(paths, manager))
    assert check.status == "warn" and "not loaded" in check.detail
    assert "pa service install" in check.fix


def test_a_loaded_job_that_is_not_running_points_at_its_log(paths, where):
    fake = FakeLaunchctl(loaded=True)
    fake.printed = WAITING
    manager, _ = installed(paths, where, fake=fake)
    check = only(_service_checks(paths, manager))
    assert (
        check.status == "warn" and "not running" in check.detail and "last exit 1" in check.detail
    )
    assert "pa service logs --launchd" in check.fix


def test_a_program_that_has_moved_away_is_named(paths, where):
    manager, _ = installed(paths, where)
    program = manager.installed_python()
    assert program is not None
    import os

    os.remove(program)
    check = only(_service_checks(paths, manager))
    assert check.status == "warn" and program in check.detail and "no longer exists" in check.detail
    assert "pa service install" in check.fix


def test_a_program_in_a_protected_folder_is_named(paths, where, monkeypatch):
    monkeypatch.setenv("HOME", str(where))
    program = where / "Desktop" / "project" / ".venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    manager, _ = installed(paths, where, program=program)
    check = only(_service_checks(paths, manager))
    assert check.status == "warn" and "Desktop" in check.detail and "uv tool install" in check.fix


def test_a_job_file_written_by_something_else_is_reported_not_trusted(paths, where):
    manager, _ = installed(paths, where)
    manager.plist_file.write_text("not a plist")
    check = only(_service_checks(paths, manager))
    assert check.status == "warn" and "not a job file this version wrote" in check.detail


def test_a_job_file_that_runs_something_else_is_reported_too(paths, where):
    import plistlib

    manager, _ = installed(paths, where)
    manager.plist_file.write_bytes(plistlib.dumps({"ProgramArguments": ["/bin/echo", "hi"]}))
    assert "not a job file this version wrote" in only(_service_checks(paths, manager)).detail


def test_the_check_is_part_of_the_capability_checks(paths, where, monkeypatch):
    monkeypatch.setattr(
        doctor, "_service_checks", lambda p: [doctor.Check("background service", "ok")]
    )
    assert "background service" in [c.name for c in doctor.capability_checks(paths)]


def test_the_installed_interpreter_is_the_one_before_the_module_flag(paths, where):
    manager, _ = installed(paths, where)
    assert manager.installed_python() == manager.installed_program()[0]
    keep, _ = installed(paths, where)
    keep.install(grace_s=30, start=False, keep_awake=True)
    assert keep.installed_program()[0] == "/usr/bin/caffeinate"
    assert keep.installed_python() == keep.installed_program()[2]
    keep.plist_file.write_text("garbage")
    assert keep.installed_python() is None


def test_a_status_that_cannot_be_read_is_reported_not_raised(paths, where):
    manager, _ = installed(paths, where)
    manager.platform = "linux"  # `status()` now refuses, as it does when launchd is out of reach
    check = only(_service_checks(paths, manager))
    assert check.status == "warn" and "LaunchAgent" in check.detail
