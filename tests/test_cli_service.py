"""`pa service`: the agent kept running in the background, from the owner's side of the screen.

launchd is replaced by a recorder, the interpreter by a file that exists, so that what the owner is
told and what is written can be checked without installing anything on the machine running the
tests.
"""

from __future__ import annotations

import os
import plistlib
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import invoke
from personal_agent import launchd
from personal_agent.commands import service as service_commands
from personal_agent.commands.service import follow
from personal_agent.config import DEFAULT_CONFIG_TOML
from personal_agent.db import open_database
from personal_agent.launchd import ServiceManager
from personal_agent.paths import Paths
from personal_agent.presence import HEARTBEAT_KEY, LOCK_NAME, EngineLock
from test_launchd import RUNNING, WAITING, FakeLaunchctl

UID = 501


def flat(output: str) -> str:
    return " ".join(output.split())


@dataclass
class Setup:
    fake: FakeLaunchctl
    agents: object
    program: str
    paths: Paths
    agent_comes_up: bool = True
    """Whether the agent takes its lock once the command has waited for it a little."""
    pauses: list[float] = field(default_factory=list)
    lock: EngineLock | None = None

    @property
    def job(self):
        [file] = list(self.agents.glob("*.plist"))
        return plistlib.loads(file.read_bytes())


@pytest.fixture
def service(home, tmp_path, monkeypatch):
    """An initialised home, a launchd that only takes notes, and an interpreter that exists."""
    assert invoke("init").exit_code == 0
    monkeypatch.setenv("HOME", str(tmp_path / "somebody"))  # nothing here is on the "Desktop"
    monkeypatch.setattr(launchd, "_package_dir", lambda: "/opt/code/personal_agent")
    program = tmp_path / "venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    fake = FakeLaunchctl()
    agents = tmp_path / "LaunchAgents"

    def make(paths, *, python=None):
        return ServiceManager(
            paths,
            launchctl=fake,
            agents=agents,
            uid=UID,
            python=python or str(program),
            platform="darwin",
            probe=lambda _python: None,
            linter=lambda _file: None,
            sleep=lambda _s: None,
        )

    monkeypatch.setattr(service_commands, "manager_for", make)
    setup = Setup(fake, agents, str(program), Paths.resolve(home))
    lock = setup.lock = EngineLock(setup.paths.run_dir / LOCK_NAME)

    def pause(seconds: float) -> None:
        setup.pauses.append(seconds)
        if setup.agent_comes_up and not lock.held:
            lock.acquire()  # the daemon the job started, loading, then taking its lock

    monkeypatch.setattr(service_commands, "pause", pause)
    yield setup
    lock.release()


# ---- install ------------------------------------------------------------------------------------------
def test_install_writes_the_job_loads_it_and_says_what_happens_now(service):
    result = invoke("service", "install")
    out = flat(result.output)
    assert result.exit_code == 0, result.output
    assert "Installed the background service" in out
    assert "starts at every login" in out and "restarted if it stops by accident" in out
    assert "pa service status" in out and "pa service uninstall" in out
    assert str(service.agents) in out and service.program in out
    assert service.job["ProgramArguments"] == [service.program, "-m", "personal_agent", "serve"]
    assert service.job["EnvironmentVariables"]["PA_HOME"] == str(service.paths.home)
    assert service.fake.names().count("bootstrap") == 1
    assert stat.S_IMODE(next(service.agents.glob("*.plist")).stat().st_mode) == 0o600


def test_install_waits_for_the_agent_to_report_in_and_says_so(service):
    out = flat(invoke("service", "install").output)
    assert "The agent is up" in out and "starts at every login" in out
    assert service.pauses == [0.5]  # it came up at the first look after the first pause


def test_an_agent_that_never_reports_in_is_not_called_up(service):
    service.agent_comes_up = False
    result = invoke("service", "install")
    out = flat(result.output)
    assert result.exit_code == 0, result.output  # the job is installed; it just is not up yet
    assert "has not reported in after 30 seconds" in out and "pa service logs --launchd" in out
    assert "The agent is up" not in out
    assert len(service.pauses) == 60 and set(service.pauses) == {0.5}


def test_nothing_is_waited_for_when_the_job_is_not_started(service):
    service.agent_comes_up = False
    result = invoke("service", "install", "--no-start")
    assert result.exit_code == 0 and service.pauses == []
    assert "reported in" not in flat(result.output)


def test_the_daemon_is_given_as_long_as_the_config_says_to_finish_its_work(service):
    config = service.paths.config_file
    text = config.read_text()
    assert "shutdown_grace_s = 30" in text  # the line `pa init` writes
    config.write_text(text.replace("shutdown_grace_s = 30", "shutdown_grace_s = 100"))
    assert invoke("service", "install").exit_code == 0
    assert service.job["ExitTimeOut"] == 115


PROXY = "http://private-proxy.example:7890"


def test_a_dry_run_shows_the_job_and_changes_nothing(service, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    result = invoke("service", "install", "--dry-run", "--pass-env", "HTTPS_PROXY")
    assert result.exit_code == 0, result.output
    assert "<key>ProgramArguments</key>" in result.output and service.program in result.output
    assert "(hidden)" in result.output and "private-proxy" not in result.output
    assert service.fake.calls == [] and not service.agents.exists()


def test_a_copied_variable_goes_into_the_job_but_its_value_is_never_printed(service, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    result = invoke("service", "install", "--pass-env", "HTTPS_PROXY")
    assert result.exit_code == 0, result.output
    assert "HTTPS_PROXY" in result.output and "private-proxy" not in result.output
    assert service.job["EnvironmentVariables"]["HTTPS_PROXY"] == PROXY


def test_a_proxy_address_that_carries_a_password_is_refused_and_nothing_is_written(
    service, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:" + "p4ss" * 3 + "@private-proxy.example:7890")
    for flags in (["--dry-run"], []):
        result = invoke("service", "install", *flags, "--pass-env", "HTTPS_PROXY")
        out = flat(result.output)
        assert result.exit_code == 1, result.output
        assert "holds a key or a password" in out and ".env" in out
        assert "p4ss" not in out and "private-proxy" not in out
        assert service.fake.calls == [] and not service.agents.exists()


def test_a_secret_is_refused_and_nothing_is_written(service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "refused-0123456789abcdef")
    result = invoke("service", "install", "--pass-env", "OPENAI_API_KEY")
    assert result.exit_code == 1
    out = flat(result.output)
    assert "looks like a secret" in out and ".env" in out and "refused-0123" not in out
    assert service.fake.calls == [] and not service.agents.exists()


def test_a_variable_that_is_not_set_is_reported(service, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    result = invoke("service", "install", "--pass-env", "HTTPS_PROXY")
    assert result.exit_code == 1 and "HTTPS_PROXY is not set" in flat(result.output)
    assert not service.agents.exists()


def test_without_an_api_key_the_owner_is_told_the_service_cannot_answer_yet(service):
    out = flat(invoke("service", "install").output)
    assert "OPENAI_API_KEY is not set" in out and str(service.paths.env_file) in out


def test_with_a_key_there_is_nothing_to_warn_about(service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "present-0123456789abcdef")
    assert "OPENAI_API_KEY is not set" not in invoke("service", "install").output


def test_a_key_in_the_homes_env_file_counts_as_a_key(service, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")  # so that what load_env sets is undone
    monkeypatch.delenv("OPENAI_API_KEY")
    service.paths.env_file.write_text("OPENAI_API_KEY=" + "sk-" + "present-" + "0123456789abcdef\n")
    assert "OPENAI_API_KEY is not set" not in invoke("service", "install").output


def test_a_custom_endpoint_in_the_config_needs_no_openai_key(service):
    (service.paths.home / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.replace("[model]", '[model]\nbase_url = "http://127.0.0.1:9/v1"', 1)
    )
    assert "OPENAI_API_KEY is not set" not in invoke("service", "install").output


def test_the_base_url_variable_needs_no_openai_key_either(service, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    assert "OPENAI_API_KEY is not set" not in invoke("service", "install").output


def test_service_preflight_uses_the_selected_model_key(service, monkeypatch):
    service.paths.config_file.write_text('[model]\napi_key_env = "PA_MODEL_AUTH"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-credential")
    missing = invoke("service", "install").output
    assert "PA_MODEL_AUTH is not set" in flat(missing)
    monkeypatch.setenv("PA_MODEL_AUTH", "selected-provider-credential")
    present = invoke("service", "install").output
    assert "PA_MODEL_AUTH is not set" not in flat(present)
    assert "selected-provider-credential" not in present


@pytest.mark.parametrize(
    "section, field", [("model", "api_key_env"), ("media", "transcribe_api_key_env")]
)
def test_selected_provider_credentials_cannot_be_copied_to_the_service_job(
    service, monkeypatch, section, field
):
    service.paths.config_file.write_text(f'[{section}]\n{field} = "PA_VALUE"\n')
    monkeypatch.setenv("PA_VALUE", "opaque-service-value")
    for flags in (["--dry-run"], []):
        result = invoke("service", "install", *flags, "--pass-env", "PA_VALUE")
        assert result.exit_code == 1
        assert "configured provider credential" in flat(result.output)
        assert "opaque-service-value" not in result.output
        assert service.fake.calls == [] and not service.agents.exists()


def test_a_job_that_cannot_be_read_from_the_desktop_is_warned_about(service, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "Desktop" / "project" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    out = flat(invoke("service", "install", "--python", str(program)).output)
    assert "Desktop" in out and "uv tool install" in out and "Installed" in out


def test_no_start_writes_the_file_and_says_it_is_not_running_yet(service):
    result = invoke("service", "install", "--no-start")
    out = flat(result.output)
    assert result.exit_code == 0 and "not running yet" in out and "starts at every login" not in out
    assert list(service.agents.glob("*.plist")) and service.fake.calls == []


def test_keep_awake_is_passed_on(service):
    assert invoke("service", "install", "--keep-awake").exit_code == 0
    assert service.job["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-s"]


def test_an_agent_already_running_in_a_terminal_is_not_started_a_second_time(service):
    held = EngineLock(service.paths.run_dir / LOCK_NAME)
    assert held.acquire()
    try:
        result = invoke("service", "install")
    finally:
        held.release()
    out = flat(result.output)
    assert result.exit_code == 0 and "already running in a terminal" in out
    assert "not loaded" in out and "pa service install" in out
    assert list(service.agents.glob("*.plist")) and "bootstrap" not in service.fake.names()


def test_the_service_itself_holding_the_lock_is_no_reason_to_stop_a_reinstall(service):
    service.fake.loaded = True
    held = EngineLock(service.paths.run_dir / LOCK_NAME)
    assert held.acquire()
    try:
        result = invoke("service", "install")
    finally:
        held.release()
    assert result.exit_code == 0 and "already running in a terminal" not in flat(result.output)
    assert "bootout" in service.fake.names() and "bootstrap" in service.fake.names()


def test_launchd_refusing_the_job_is_reported_and_exits_with_an_error(service):
    service.fake.bootstrap_codes[:] = [5]
    result = invoke("service", "install")
    assert result.exit_code == 1 and "launchd refused the job" in flat(result.output)


def test_a_home_that_was_never_set_up_says_to_run_init(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_HOME", str(tmp_path / "never-created"))
    result = invoke("service", "install")
    assert result.exit_code == 2 and "pa init" in flat(result.output)
    assert not (tmp_path / "never-created").exists()


def test_a_broken_config_is_reported_and_nothing_is_installed(service):
    service.paths.config_file.write_text("[runtime\nbroken")
    result = invoke("service", "install")
    assert result.exit_code == 2 and not service.agents.exists()


def test_anywhere_but_a_mac_the_owner_is_pointed_at_systemd(home, monkeypatch):
    assert invoke("init").exit_code == 0
    monkeypatch.setattr(launchd.sys, "platform", "linux")
    monkeypatch.setattr(
        service_commands,
        "manager_for",
        lambda paths, python=None: ServiceManager(paths, platform="linux"),
    )
    result = invoke("service", "install")
    assert result.exit_code == 1 and "systemd" in flat(result.output)


# ---- uninstall and restart ------------------------------------------------------------------------------
def test_uninstall_stops_the_job_and_leaves_the_data(service):
    invoke("service", "install")
    memory = service.paths.memory_dir
    result = invoke("service", "uninstall")
    out = flat(result.output)
    assert result.exit_code == 0 and "Removed the background service" in out
    assert "untouched" in out and str(service.paths.home) in out
    assert not list(service.agents.glob("*.plist")) and memory.is_dir()
    assert "bootout" in service.fake.names()


def test_uninstalling_what_is_not_installed_says_so(service):
    result = invoke("service", "uninstall")
    assert result.exit_code == 0 and "was not installed" in flat(result.output)


def test_uninstall_cleans_up_whichever_half_is_left(service):
    invoke("service", "install")
    [job_file] = service.agents.glob("*.plist")
    job_file.unlink()  # still loaded, but its file is gone
    out = flat(invoke("service", "uninstall").output)
    assert "Removed the background service" in out and "was not installed" not in out
    assert not service.fake.loaded
    invoke("service", "install")
    service.fake.loaded = False  # its file is there, but nothing is loaded
    out = flat(invoke("service", "uninstall").output)
    assert "Removed the background service" in out and "was not installed" not in out
    assert not list(service.agents.glob("*.plist"))


def test_a_job_launchd_will_not_stop_is_reported(service):
    invoke("service", "install")
    service.fake.bootout_code = 5
    result = invoke("service", "uninstall")
    assert result.exit_code == 1 and "would not stop the job" in flat(result.output)
    assert list(service.agents.glob("*.plist"))


def test_uninstalling_a_service_that_is_still_finishing_its_work_says_so(service):
    invoke("service", "install")
    service.fake.linger = 10**6
    out = flat(invoke("service", "uninstall").output)
    assert "Removed the background service" in out and "still finishing its work" in out
    assert not list(service.agents.glob("*.plist"))


def test_uninstalling_a_service_that_stopped_does_not_cry_wolf(service):
    invoke("service", "install")
    service.fake.linger = 3
    out = flat(invoke("service", "uninstall").output)
    assert "Removed the background service" in out and "still finishing" not in out


def test_restart_asks_launchd_and_reports_the_new_process(service):
    invoke("service", "install")
    service.lock.release()  # the old process is gone; the new one has yet to take the lock
    service.pauses.clear()
    result = invoke("service", "restart")
    out = flat(result.output)
    assert result.exit_code == 0, result.output
    assert "Restarting" in out and "never sooner than 30 seconds" in out
    assert "The agent is running again (pid 700)" in out
    assert service.pauses.count(0.5) == 1  # it waited for the agent, not only for launchd
    assert any(call[:2] == ("kickstart", "-k") for call in service.fake.calls)


def test_a_restart_launchd_has_not_carried_out_yet_is_not_claimed(service):
    invoke("service", "install")
    service.fake.spawn_after = None
    result = invoke("service", "restart")
    out = flat(result.output)
    assert result.exit_code == 0 and "has not started it again yet" in out
    assert "running again" not in out


def test_a_restart_whose_agent_never_reports_in_says_where_to_look(service):
    invoke("service", "install")
    service.lock.release()
    service.agent_comes_up = False
    result = invoke("service", "restart")
    out = flat(result.output)
    assert result.exit_code == 0
    assert "started it again (pid 700)" in out and "pa service logs --launchd" in out


def test_restarting_a_service_that_is_not_loaded_says_how_to_load_it(service):
    result = invoke("service", "restart")
    assert result.exit_code == 1 and "pa service install" in flat(result.output)


# ---- status ---------------------------------------------------------------------------------------------
def test_status_has_a_command_of_its_own_and_says_what_a_bare_pa_service_says(service):
    invoke("service", "install")
    bare, named = invoke("service"), invoke("service", "status")
    assert bare.exit_code == 0 and named.exit_code == 0, named.output
    assert flat(named.output) == flat(bare.output) and "running (pid 689)" in flat(named.output)


def test_every_command_that_the_service_tells_the_owner_to_run_exists(service):
    """The install summary once named `pa service status`, which was not a command at all."""
    root = Path(launchd.__file__).parent
    mentioned: set[str] = set()
    for name in ("commands/service.py", "doctor.py", "launchd.py"):
        mentioned |= set(re.findall(r"pa service ([a-z]+)", (root / name).read_text()))
    assert {"install", "uninstall", "status", "logs"} <= mentioned
    for word in sorted(mentioned):
        found = invoke("service", word, "--help")
        assert found.exit_code == 0, f"`pa service {word}` is suggested but is not a command"


def test_status_of_a_service_that_was_never_installed(service):
    out = flat(invoke("service").output)
    assert "not installed" in out and "pa service install" in out
    assert "not running" in out  # nor is the agent


def test_status_of_a_running_service(service):
    invoke("service", "install")
    out = flat(invoke("service").output)
    assert "running (pid 689)" in out and "started 3 time(s)" in out and "last exit: 1" in out
    assert str(service.agents) in out and service.program in out


def test_a_job_that_has_a_process_is_running_whatever_launchd_calls_the_state(service):
    """Between being started and having started, launchd reports `xpcproxy`, with a pid."""
    invoke("service", "install")
    service.fake.printed = RUNNING.replace("state = running", "state = xpcproxy")
    out = flat(invoke("service").output)
    assert "running (pid 689)" in out and "loaded, not running" not in out


def test_status_of_a_service_that_stopped_points_at_the_logs(service):
    invoke("service", "install")
    service.fake.printed = WAITING
    out = flat(invoke("service").output)
    assert "loaded, not running" in out and "pa service logs --launchd" in out


def test_status_of_a_job_file_that_is_not_loaded(service):
    invoke("service", "install", "--no-start")
    out = flat(invoke("service").output)
    assert "installed but not loaded" in out and "pa service install" in out


def test_status_warns_when_the_program_has_moved_away(service):
    invoke("service", "install")
    os.remove(service.program)
    out = flat(invoke("service").output)
    assert "no longer exists" in out and "pa service install" in out


def test_status_warns_about_a_program_in_a_folder_macos_protects(service, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "Documents" / "pa" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    invoke("service", "install", "--python", str(program))
    assert "Documents folder" in flat(invoke("service").output)


def test_status_tells_a_service_started_agent_from_one_started_in_a_terminal(service):
    held = EngineLock(service.paths.run_dir / LOCK_NAME)
    assert held.acquire()
    try:
        assert f"run from a terminal (pid {os.getpid()})" in flat(invoke("service").output)
        db = open_database(service.paths.db_file)
        try:
            db.kv_set(HEARTBEAT_KEY, f"{os.getpid()} {__import__('time').time()}", now=0)
        finally:
            db.close()
        out = flat(invoke("service").output)
    finally:
        held.release()
    assert f"alive (pid {os.getpid()}" in out and "heartbeat" in out


# ---- logs -------------------------------------------------------------------------------------------------
def test_logs_before_anything_has_run_say_so(service):
    (service.paths.logs_dir / "pa.log").unlink(missing_ok=True)  # `pa init` leaves an empty one
    result = invoke("service", "logs")
    assert result.exit_code == 1 and "no log yet" in flat(result.output)


def test_logs_show_the_end_of_the_agents_own_log(service):
    log = service.paths.logs_dir / "pa.log"
    log.write_text("".join(f"line {n}\n" for n in range(100)))
    out = invoke("service", "logs", "-n", "3").output
    assert out.splitlines() == ["line 97", "line 98", "line 99"]


def test_logs_do_not_read_brackets_in_the_log_as_formatting(service):
    (service.paths.logs_dir / "pa.log").write_text("[bold red]alarm[/bold red] [x\n")
    assert "[bold red]alarm[/bold red] [x" in invoke("service", "logs").output


def test_logs_of_the_job_show_both_files_with_their_names(service):
    invoke("service", "install")
    manager = service_commands.manager_for(service.paths)
    manager.err_log.write_text("Traceback: boom\n")
    manager.out_log.write_text("started\n")
    out = invoke("service", "logs", "--launchd").output
    assert out.index("service.err.log") < out.index("Traceback: boom")
    assert out.index("Traceback: boom") < out.index("service.out.log") < out.index("started")


def test_an_empty_log_says_nothing_was_logged(service):
    (service.paths.logs_dir / "pa.log").write_text("")
    assert "nothing logged yet" in invoke("service", "logs").output


def test_following_starts_from_the_agents_own_log(service, monkeypatch):
    log = service.paths.logs_dir / "pa.log"
    log.write_text("one\n")
    seen = []
    monkeypatch.setattr(service_commands, "follow", lambda path, emit: seen.append(path))
    result = invoke("service", "logs", "-f")
    assert result.exit_code == 0 and seen == [log] and "one" in result.output


def test_following_the_jobs_own_output_starts_from_its_error_file(service, monkeypatch):
    invoke("service", "install")
    manager = service_commands.manager_for(service.paths)
    manager.err_log.write_text("Traceback: boom\n")  # where a crash before the log opens goes
    manager.out_log.write_text("started\n")
    seen = []
    monkeypatch.setattr(service_commands, "follow", lambda path, emit: seen.append(path))
    result = invoke("service", "logs", "--launchd", "-f")
    assert result.exit_code == 0 and seen == [manager.err_log]


def test_following_can_be_stopped_with_ctrl_c(service, monkeypatch):
    (service.paths.logs_dir / "pa.log").write_text("one\n")

    def interrupted(path, emit):
        raise KeyboardInterrupt

    monkeypatch.setattr(service_commands, "follow", interrupted)
    assert invoke("service", "logs", "-f").exit_code == 0


# ---- following a log -----------------------------------------------------------------------------------------
def follow_while(path, steps):
    """Follow `path`, doing one of `steps` each time there is nothing new to read."""
    seen: list[str] = []
    todo = list(steps)
    state = SimpleNamespace(done=False)

    def sleeper(_seconds):
        if todo:
            todo.pop(0)()
        else:
            state.done = True

    follow(path, seen.append, stop=lambda: state.done, sleep=sleeper)
    return seen


def append(path, text):
    def do():
        with open(path, "a") as handle:
            handle.write(text)

    return do


def test_following_shows_what_is_added_and_not_what_was_there(tmp_path):
    log = tmp_path / "log"
    log.write_text("old\n")
    assert follow_while(log, [append(log, "new 1\nnew 2\n")]) == ["new 1", "new 2"]


def test_a_line_is_shown_once_it_is_whole(tmp_path):
    log = tmp_path / "log"
    log.write_text("")
    assert follow_while(log, [append(log, "half"), append(log, " done\n")]) == ["half done"]


def test_a_rotated_log_is_followed_into_its_new_file(tmp_path):
    log = tmp_path / "log"
    log.write_text("old\n")

    def rotate():
        os.replace(log, tmp_path / "log.1")
        log.write_text("fresh 1\nfresh 2\n")

    assert follow_while(log, [append(log, "before\n"), rotate]) == [
        "before",
        "fresh 1",
        "fresh 2",
    ]


def test_a_log_that_is_cut_short_is_read_again_from_its_start(tmp_path):
    log = tmp_path / "log"
    log.write_text("a long first line, a long second line\n" * 3)

    def cut():
        log.write_text("short\n")

    assert follow_while(log, [cut]) == ["short"]


def test_a_log_that_appears_later_is_read_from_its_first_line(tmp_path):
    log = tmp_path / "log"
    assert follow_while(log, [lambda: log.write_text("a\nb\n")]) == ["a", "b"]


def test_a_log_that_is_not_there_is_waited_for_not_an_error(tmp_path):
    assert follow_while(tmp_path / "never", []) == []


def test_when_told_to_stop_at_once_nothing_is_read_or_waited_for(tmp_path):
    log = tmp_path / "log"
    log.write_text("x\n")
    waited: list[float] = []
    follow(log, lambda line: None, stop=lambda: True, sleep=waited.append)
    assert waited == []


def test_text_that_is_not_utf8_does_not_stop_the_following(tmp_path):
    log = tmp_path / "log"
    log.write_text("")

    def junk():
        with open(log, "ab") as handle:
            handle.write(b"ok \xff\xfe end\n")

    [line] = follow_while(log, [junk])
    assert line.startswith("ok ") and line.endswith(" end")


# ---- waiting for the agent ---------------------------------------------------------------------------
def test_an_agent_that_reports_in_during_the_very_last_pause_is_still_noticed(service, monkeypatch):
    calls: list[float] = []

    def pause(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 4:
            service.lock.acquire()

    monkeypatch.setattr(service_commands, "pause", pause)
    assert service_commands.wait_for_agent(service.paths, seconds=2.0, step_s=0.5) is True
    assert calls == [0.5, 0.5, 0.5, 0.5]


def test_an_agent_that_never_reports_in_is_given_exactly_the_time_asked_for(service, monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(service_commands, "pause", calls.append)
    assert service_commands.wait_for_agent(service.paths, seconds=2.0, step_s=0.5) is False
    assert calls == [0.5, 0.5, 0.5, 0.5]


def test_an_agent_that_is_already_up_is_not_waited_for(service, monkeypatch):
    service.lock.acquire()
    calls: list[float] = []
    monkeypatch.setattr(service_commands, "pause", calls.append)
    assert service_commands.wait_for_agent(service.paths) is True
    assert calls == []
