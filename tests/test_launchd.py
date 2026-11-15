"""Keeping the agent running in the background: the LaunchAgent that starts `pa serve` at login.

The job must be a file launchd accepts, hold no secret, start the right program with the right
home, and be installed, replaced and removed without ever touching anything but its own file. All
of launchd is replaced by a recorder here; the one real thing checked is the property-list parser
(`plutil -lint`), because a job file that the machine rejects is the failure that matters.
"""

from __future__ import annotations

import math
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from personal_agent import launchd
from personal_agent.launchd import (
    LABEL,
    POLL_S,
    SAFE_PATH,
    STOP_WAIT_S,
    THROTTLE_S,
    Done,
    ServiceError,
    ServiceManager,
    build_plist,
    label_for,
    masked,
    parse_print,
    pass_env,
    protected_folder,
    render,
    tail,
)
from personal_agent.paths import Paths

UID = 501
REAL_PLUTIL = Path("/usr/bin/plutil").exists()
REAL_AGENTS_DIR = launchd.agents_dir  # taken before the guard in conftest replaces it

# What `launchctl print` really prints for a running job (shortened, from a Mac).
RUNNING = """\
gui/501/local.personal-agent = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/local.personal-agent.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /usr/bin/python3
\tdomain = gui/501 [100020]
\texit timeout = 45
\truns = 3
\tpid = 689
\timmediate reason = non-ipc demand
\tlast exit code = 1

\tendpoints = {
\t\t"local.x" = {
\t\t\tstate = active
\t\t}
\t}
}
"""
WAITING = RUNNING.replace("state = running", "state = not running").replace("\tpid = 689\n", "")
STARTED_AGAIN = RUNNING.replace("runs = 3", "runs = 4").replace("pid = 689", "pid = 700")


class FakeLaunchctl:
    """Remembers every call and plays launchd's part for the few commands the code uses."""

    def __init__(
        self,
        *,
        loaded: bool = False,
        bootstrap=(0,),
        bootout: int = 0,
        kickstart: int = 0,
        linger: int = 0,
        spawn_after: int | None = 0,
    ):
        """`linger`: how many times `print` still finds a job after a successful `bootout` (a
        daemon that is finishing its work). `spawn_after`: how many times `print` still shows the
        old run after a `kickstart` before the new one shows (None: it never does)."""
        self.calls: list[tuple[str, ...]] = []
        self.loaded = loaded
        self.bootstrap_codes = list(bootstrap)
        self.bootout_code = bootout
        self.kickstart_code = kickstart
        self.linger = linger
        self.spawn_after = spawn_after
        self.stopping = 0
        self.kicked = False
        self.since_kick = 0
        self.printed = RUNNING

    def __call__(self, *args: str) -> Done:
        self.calls.append(args)
        command = args[0]
        if command == "print":
            if self.stopping > 0:
                self.stopping -= 1
                if self.stopping == 0:
                    self.loaded = False
                return Done(0, self.printed)
            if not self.loaded:
                return Done(113, "", "Could not find service")
            text = self.printed
            if self.kicked and self.spawn_after is not None:
                if self.since_kick >= self.spawn_after:
                    text = text.replace("runs = 3", "runs = 4").replace("pid = 689", "pid = 700")
                self.since_kick += 1
            return Done(0, text)
        if command == "bootstrap":
            code = (
                self.bootstrap_codes.pop(0)
                if len(self.bootstrap_codes) > 1
                else self.bootstrap_codes[0]
            )
            if code == 0:
                self.loaded = True
                return Done(0)
            return Done(code, "", "Bootstrap failed: 5: Input/output error")
        if command == "bootout":
            if self.bootout_code == 0:
                if self.linger > 0:
                    self.stopping = self.linger
                else:
                    self.loaded = False
            return Done(self.bootout_code, "", "" if self.bootout_code == 0 else "Boot-out failed")
        if command == "kickstart":
            if self.kickstart_code == 0:
                self.kicked = True
                self.since_kick = 0
            return Done(self.kickstart_code, "", "" if self.kickstart_code == 0 else "no such job")
        return Done(0)  # enable

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def paths(tmp_path):
    value = Paths(home=(tmp_path / "home").resolve())
    value.ensure()
    return value


@pytest.fixture
def python(tmp_path):
    """A program that exists and can be run, standing in for the interpreter of the install."""
    program = tmp_path / "venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    return str(program)


@pytest.fixture
def agents(tmp_path):
    return tmp_path / "LaunchAgents"


def manager(paths, python, agents, fake=None, **kw) -> tuple[ServiceManager, FakeLaunchctl]:
    fake = fake or FakeLaunchctl()
    kw.setdefault("probe", lambda _python: None)
    kw.setdefault("linter", lambda _file: None)
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("platform", "darwin")
    return ServiceManager(paths, launchctl=fake, agents=agents, uid=UID, python=python, **kw), fake


# ---- naming ---------------------------------------------------------------------------------------
def test_the_default_home_has_the_plain_name_and_another_home_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    default = Paths.resolve(None)
    assert label_for(default) == LABEL
    other = Paths(home=tmp_path / "elsewhere")
    third = Paths(home=tmp_path / "third")
    assert label_for(other).startswith(LABEL + ".") and len(label_for(other)) == len(LABEL) + 9
    assert label_for(other) == label_for(Paths(home=tmp_path / "elsewhere"))
    assert label_for(other) != label_for(third) != LABEL


# ---- what may be copied into the job ---------------------------------------------------------------
ENV_FILE = Path("/home/x/.env")


def test_a_proxy_setting_can_be_copied_into_the_job():
    env = {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost", "OTHER": "x"}
    assert pass_env(["HTTPS_PROXY", " NO_PROXY "], env, ENV_FILE) == {
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost",
    }
    assert pass_env([], env, ENV_FILE) == {}


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "DB_PASSWORD",
        "SESSION_ID",
        "AUTH_HEADER",
        "MY_COOKIE",
        "AWS_CREDENTIALS",
    ],
)
def test_a_variable_that_looks_like_a_secret_is_refused_and_pointed_at_the_env_file(name):
    with pytest.raises(ServiceError) as caught:  # a value that no detector would flag
        pass_env([name], {name: "plain-looking-value"}, ENV_FILE)
    assert "looks like a secret" in str(caught.value) and str(ENV_FILE) in str(caught.value)
    assert "plain-looking" not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "http://" + "alice" + ":" + "p4ss" * 3 + "@proxy.local:8080",
        "socks5://:" + "hunter2" * 2 + "@10.0.0.2:1080",
        "sk-" + "proj-" + "Zx9Y8w7V" * 4,
        "ghp_" + "Zx9Y8w7V" * 4,
    ],
)
def test_a_harmless_name_does_not_make_a_password_acceptable(value):
    """A proxy address can carry a password, and a job's definition is a file that gets copied."""
    with pytest.raises(ServiceError) as caught:
        pass_env(["HTTPS_PROXY"], {"HTTPS_PROXY": value}, ENV_FILE)
    assert str(ENV_FILE) in str(caught.value) and "HTTPS_PROXY" in str(caught.value)
    assert value not in str(caught.value) and "p4ss" not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:7890", "http://proxy.local:8080/path@x", "localhost,127.0.0.1", "a@b"],
)
def test_an_ordinary_value_is_not_mistaken_for_a_secret(value):
    assert pass_env(["HTTPS_PROXY"], {"HTTPS_PROXY": value}, ENV_FILE) == {"HTTPS_PROXY": value}


@pytest.mark.parametrize("name", ["lower", "WITH SPACE", "A=B", "1ABC", "", "A-B", "$HOME"])
def test_only_real_variable_names_are_accepted(name):
    with pytest.raises(ServiceError, match="not the name of an environment variable"):
        pass_env([name], {name: "x"}, ENV_FILE)


def test_a_variable_that_is_not_set_cannot_be_copied():
    with pytest.raises(ServiceError, match="HTTPS_PROXY is not set"):
        pass_env(["HTTPS_PROXY"], {}, ENV_FILE)
    with pytest.raises(ServiceError, match="HTTPS_PROXY is not set"):
        pass_env(["HTTPS_PROXY"], {"HTTPS_PROXY": ""}, ENV_FILE)


# ---- the folders macOS keeps from background jobs ---------------------------------------------------
@pytest.mark.parametrize(
    ("path", "folder"),
    [
        ("Desktop/project/.venv/bin/python", "Desktop"),
        ("Desktop", "Desktop"),
        ("Documents/a b/c", "Documents"),
        ("Downloads/x", "Downloads"),
        ("Library/Mobile Documents/com~apple~CloudDocs/x", "Library/Mobile Documents"),
        ("Library/CloudStorage/Dropbox/x", "Library/CloudStorage"),
    ],
)
def test_a_path_in_a_protected_folder_is_recognised(tmp_path, path, folder):
    assert protected_folder(tmp_path / path, home=tmp_path) == folder


@pytest.mark.parametrize(
    "path",
    ["Developer/x/python", "Desktop2/x", "MyDocuments/x", ".local/share/uv/python", "Library/x"],
)
def test_other_paths_are_fine(tmp_path, path):
    assert protected_folder(tmp_path / path, home=tmp_path) is None


def test_a_path_outside_the_home_is_fine(tmp_path):
    assert protected_folder("/usr/local/bin/python", home=tmp_path) is None
    assert protected_folder("/opt/homebrew/bin/python3", home=tmp_path) is None


def test_a_link_in_a_protected_folder_counts_wherever_it_points(tmp_path):
    """A virtualenv on the Desktop is on the Desktop, even though its `python` is a link to an
    interpreter somewhere else: the packages it loads are on the Desktop."""
    target = tmp_path / ".local" / "python"
    target.parent.mkdir(parents=True)
    target.write_text("")
    link = tmp_path / "Desktop" / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert protected_folder(link, home=tmp_path) == "Desktop"


def test_a_dotdot_path_is_judged_by_where_it_ends_up(tmp_path):
    assert protected_folder(tmp_path / "Desktop" / ".." / "Developer", home=tmp_path) is None
    assert (
        protected_folder(tmp_path / "Developer" / ".." / "Desktop" / "x", home=tmp_path)
        == "Desktop"
    )


# ---- the job itself ---------------------------------------------------------------------------------
def job(paths, **kw):
    kw.setdefault("python", "/opt/venv/bin/python")
    kw.setdefault("label", "local.personal-agent")
    kw.setdefault("grace_s", 30)
    return build_plist(paths, **kw)


def test_the_job_starts_the_daemon_of_this_home(paths):
    plist = job(paths)
    assert plist["Label"] == "local.personal-agent"
    assert plist["ProgramArguments"] == ["/opt/venv/bin/python", "-m", "personal_agent", "serve"]
    assert plist["WorkingDirectory"] == str(paths.home)
    assert plist["EnvironmentVariables"]["PA_HOME"] == str(paths.home)  # type: ignore[index]
    assert plist["StandardOutPath"] == str(paths.logs_dir / "service.out.log")
    assert plist["StandardErrorPath"] == str(paths.logs_dir / "service.err.log")


def test_it_starts_at_login_and_comes_back_after_a_crash_but_not_after_a_normal_stop(paths):
    plist = job(paths)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == launchd.THROTTLE_S


def test_launchd_waits_as_long_as_the_daemon_does_for_running_work(paths):
    assert job(paths, grace_s=30)["ExitTimeOut"] == 45
    assert job(paths, grace_s=0)["ExitTimeOut"] == 15
    assert job(paths, grace_s=120.9)["ExitTimeOut"] == 135


def test_the_path_the_job_gets_has_the_interpreters_folder_first_and_no_repeats(paths):
    value = job(paths, python="/opt/homebrew/bin/python3")["EnvironmentVariables"]["PATH"]  # type: ignore[index]
    parts = value.split(":")
    assert parts[0] == "/opt/homebrew/bin"
    assert parts.count("/opt/homebrew/bin") == 1
    assert all(p in parts for p in SAFE_PATH)
    own = job(paths, python="/opt/my venv/bin/python")["EnvironmentVariables"]["PATH"]  # type: ignore[index]
    assert own.split(":")[0] == "/opt/my venv/bin"


def test_text_is_utf8_whatever_the_login_locale(paths):
    environment = job(paths)["EnvironmentVariables"]
    assert environment["PYTHONUTF8"] == "1" and environment["PYTHONUNBUFFERED"] == "1"  # type: ignore[index]


def test_variables_the_owner_chose_are_added_and_nothing_else_is_taken_from_the_shell(
    paths, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "must-not-appear-0123456789")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    plist = job(paths, env={"HTTPS_PROXY": "http://127.0.0.1:7890"})
    environment = plist["EnvironmentVariables"]
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:7890"  # type: ignore[index]
    assert "must-not-appear" not in render(plist).decode()
    assert set(environment) == {"PA_HOME", "PATH", "PYTHONUNBUFFERED", "PYTHONUTF8", "HTTPS_PROXY"}  # type: ignore[arg-type]


def test_keeping_the_mac_awake_wraps_the_program_in_caffeinate_for_mains_power(paths):
    plist = job(paths, keep_awake=True)
    assert plist["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-s",
        "/opt/venv/bin/python",
        "-m",
        "personal_agent",
        "serve",
    ]


def test_the_rendered_job_reads_back_as_the_same_job(paths):
    plist = job(paths, env={"HTTPS_PROXY": "http://127.0.0.1:7890"}, keep_awake=True)
    assert plistlib.loads(render(plist)) == plist


@pytest.mark.skipif(not REAL_PLUTIL, reason="needs macOS plutil")
def test_the_machines_own_property_list_parser_accepts_the_job(paths, tmp_path):
    file = tmp_path / "job.plist"
    file.write_bytes(render(job(paths, env={"A": "b & <c>"}, keep_awake=True)))
    assert launchd.lint(file) is None


@pytest.mark.skipif(not REAL_PLUTIL, reason="needs macOS plutil")
def test_the_parser_check_rejects_a_broken_file(tmp_path):
    file = tmp_path / "bad.plist"
    file.write_text("<plist><dict><key>Label</key></plist>")
    assert launchd.lint(file)


def test_a_missing_plutil_is_not_a_reason_to_refuse(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd.Path, "exists", lambda self: False)
    assert launchd.lint(tmp_path / "x.plist") is None


def test_the_job_as_shown_on_a_screen_hides_the_copied_values(paths):
    plist = job(paths, env={"HTTPS_PROXY": "http://user:pw@host:1"})
    shown = masked(plist, ["HTTPS_PROXY"])
    assert shown["EnvironmentVariables"]["HTTPS_PROXY"] == "(hidden)"  # type: ignore[index]
    assert "user:pw" not in render(shown).decode()
    assert plist["EnvironmentVariables"]["HTTPS_PROXY"] == "http://user:pw@host:1"  # type: ignore[index]


# ---- reading launchd's answer ------------------------------------------------------------------------
def test_the_state_of_a_running_job_is_read_from_what_launchctl_prints():
    assert parse_print(RUNNING) == {
        "state": "running",
        "pid": "689",
        "runs": "3",
        "last_exit": "1",
    }


def test_a_job_that_is_waiting_has_no_pid():
    facts = parse_print(WAITING)
    assert facts["state"] == "not running" and "pid" not in facts


def test_a_layout_we_do_not_know_gives_no_facts_instead_of_an_error():
    assert parse_print("something else entirely") == {}
    assert parse_print("") == {}


def test_only_the_jobs_own_state_counts_not_the_ones_nested_further_down():
    text = "x = {\n\tstate = running\n\tendpoints = {\n\t\tstate = active\n\t}\n}\n"
    assert parse_print(text)["state"] == "running"


# ---- installing --------------------------------------------------------------------------------------
def test_installing_writes_the_job_privately_and_loads_it(paths, python, agents):
    service, fake = manager(paths, python, agents)
    done = service.install(grace_s=30)

    assert done.written and done.loaded and done.plist_file == agents / f"{service.label}.plist"
    assert stat.S_IMODE(done.plist_file.stat().st_mode) == 0o600
    written = plistlib.loads(done.plist_file.read_bytes())
    assert written["ProgramArguments"][:1] == [python] and written["Label"] == service.label
    assert fake.calls == [
        ("enable", f"gui/{UID}/{service.label}"),
        ("print", f"gui/{UID}/{service.label}"),
        ("bootstrap", f"gui/{UID}", str(done.plist_file)),
    ]
    assert not list(agents.glob(".*tmp"))  # nothing is left lying about


def test_the_log_files_exist_and_are_private_before_the_job_can_write_to_them(
    paths, python, agents
):
    service, _ = manager(paths, python, agents)
    service.install(grace_s=30)
    for log in (service.out_log, service.err_log):
        assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o600


def test_existing_logs_keep_what_is_in_them(paths, python, agents):
    service, _ = manager(paths, python, agents)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    service.err_log.write_text("an earlier crash\n")
    service.err_log.chmod(0o644)
    service.install(grace_s=30)
    assert service.err_log.read_text() == "an earlier crash\n"
    assert stat.S_IMODE(service.err_log.stat().st_mode) == 0o600


def test_installing_again_replaces_a_loaded_job_by_stopping_it_first(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True))
    service.install(grace_s=30)
    assert fake.names() == ["enable", "print", "bootout", "print", "bootstrap"]
    assert fake.loaded


def test_launchd_that_is_slow_to_let_go_of_the_old_job_is_given_time(paths, python, agents):
    waits: list[float] = []
    fake = FakeLaunchctl(loaded=True, bootstrap=(5, 5, 0))
    service, _ = manager(paths, python, agents, fake, sleep=waits.append)
    service.install(grace_s=30)
    assert fake.names().count("bootstrap") == 3 and waits == [1.0, 1.0] and fake.loaded


def test_a_job_launchd_keeps_refusing_is_reported_with_launchds_words(paths, python, agents):
    waits: list[float] = []
    service, fake = manager(
        paths, python, agents, FakeLaunchctl(bootstrap=(5,)), sleep=waits.append
    )
    with pytest.raises(ServiceError, match="launchd refused the job: Bootstrap failed: 5"):
        service.install(grace_s=30)
    assert fake.names().count("bootstrap") == 6 and len(waits) == 5


def test_no_start_only_writes_the_file(paths, python, agents):
    service, fake = manager(paths, python, agents)
    done = service.install(grace_s=30, start=False)
    assert done.written and not done.loaded and done.plist_file.exists()
    assert fake.calls == []


def test_a_job_file_the_parser_rejects_is_never_put_in_place(paths, python, agents):
    service, fake = manager(paths, python, agents, linter=lambda _f: "line 3: junk")
    with pytest.raises(ServiceError, match="was not accepted: line 3: junk"):
        service.install(grace_s=30)
    assert not service.plist_file.exists() and not list(agents.glob(".*tmp")) and fake.calls == []


def test_a_bad_new_file_does_not_replace_a_good_old_one(paths, python, agents):
    service, _ = manager(paths, python, agents)
    service.install(grace_s=30, start=False)
    before = service.plist_file.read_bytes()
    broken, _ = manager(paths, python, agents, linter=lambda _f: "bad")
    with pytest.raises(ServiceError):
        broken.install(grace_s=99, start=False)
    assert service.plist_file.read_bytes() == before


def test_an_interpreter_that_cannot_run_the_agent_is_not_installed(paths, python, agents):
    service, fake = manager(
        paths,
        python,
        agents,
        probe=lambda p: "ModuleNotFoundError: No module named 'personal_agent'",
    )
    with pytest.raises(ServiceError, match=r"cannot run the agent .*No module named"):
        service.install(grace_s=30)
    assert not service.plist_file.exists() and fake.calls == []


def test_a_program_that_does_not_exist_is_not_installed(paths, agents, tmp_path):
    service, fake = manager(paths, str(tmp_path / "nope" / "python"), agents)
    with pytest.raises(ServiceError, match="is not a program that can be run"):
        service.install(grace_s=30)
    assert fake.calls == []


def test_a_file_that_is_not_executable_is_not_installed(paths, agents, tmp_path):
    plain = tmp_path / "python"
    plain.write_text("")
    plain.chmod(0o644)
    service, _ = manager(paths, str(plain), agents)
    with pytest.raises(ServiceError, match="not a program that can be run"):
        service.install(grace_s=30)


def test_planning_changes_nothing_and_warns_about_a_protected_folder(
    paths, tmp_path, agents, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "Desktop" / "project" / ".venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    service, fake = manager(paths, str(program), agents)
    plan = service.plan(grace_s=30)
    assert not plan.written and not plan.loaded and fake.calls == []
    assert not agents.exists()
    assert len(plan.warnings) == 1 and "Desktop" in plan.warnings[0]
    assert "uv tool install" in plan.warnings[0]


def test_a_program_outside_the_protected_folders_gets_no_warning(
    paths, tmp_path, agents, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "somebody"))
    monkeypatch.setattr(launchd, "_package_dir", lambda: "/opt/code/personal_agent")
    program = tmp_path / "tools" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    service, _ = manager(paths, str(program), agents)
    assert service.plan(grace_s=30).warnings == []


def test_the_code_itself_being_in_a_protected_folder_is_noticed_too(
    paths, python, tmp_path, agents, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launchd, "_package_dir", lambda: str(tmp_path / "Documents" / "pa" / "src"))
    service, _ = manager(paths, python, agents)
    [warning] = service.plan(grace_s=30).warnings
    assert "Documents" in warning and "agent's code" in warning


def test_the_shown_job_hides_what_was_copied_but_the_written_one_has_it(paths, python, agents):
    service, _ = manager(paths, python, agents)
    plan = service.plan(grace_s=30, env={"HTTPS_PROXY": "http://user:pw@h:1"})
    assert "user:pw" not in plan.shown_text and "user:pw" in plan.text
    done = service.install(grace_s=30, env={"HTTPS_PROXY": "http://user:pw@h:1"})
    assert "user:pw" in done.plist_file.read_text()


@pytest.mark.parametrize("action", ["plan", "install", "uninstall", "restart", "status"])
def test_anywhere_but_a_mac_the_service_says_what_to_do_instead(paths, python, agents, action):
    service, fake = manager(paths, python, agents, platform="linux")
    with pytest.raises(ServiceError, match="systemd"):
        if action in ("plan", "install"):
            getattr(service, action)(grace_s=30)
        else:
            getattr(service, action)()
    assert fake.calls == [] and not agents.exists()


# ---- removing, restarting, looking ---------------------------------------------------------------------
def test_uninstalling_stops_the_job_and_removes_its_file_but_nothing_else(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True))
    service.install(grace_s=30, start=False)
    (paths.home / "memory" / "keep.txt").write_text("mine")
    removed = service.uninstall()
    assert removed.was_loaded and removed.removed_file and not service.plist_file.exists()
    assert ("bootout", service.target) in fake.calls
    assert (paths.home / "memory" / "keep.txt").read_text() == "mine"


def test_uninstalling_something_that_is_not_installed_says_so_and_does_nothing(
    paths, python, agents
):
    service, fake = manager(paths, python, agents)
    removed = service.uninstall()
    assert (removed.was_loaded, removed.removed_file) == (False, False)
    assert fake.names() == ["print"]


def test_a_job_file_left_behind_is_removed_even_when_the_job_is_not_loaded(paths, python, agents):
    service, _ = manager(paths, python, agents)
    service.install(grace_s=30, start=False)
    removed = service.uninstall()
    assert not removed.was_loaded and removed.removed_file and not service.plist_file.exists()


def test_a_job_that_is_loaded_but_has_no_file_is_still_stopped(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True))
    removed = service.uninstall()
    assert removed.was_loaded and not removed.removed_file
    assert ("bootout", service.target) in fake.calls


def test_a_job_that_vanishes_while_being_stopped_is_not_an_error(paths, python, agents):
    service, _ = manager(paths, python, agents, FakeLaunchctl(loaded=True, bootout=3))
    service.install(grace_s=30, start=False)
    assert service.uninstall().removed_file


def test_a_job_launchd_will_not_stop_keeps_its_file_and_is_reported(paths, python, agents):
    service, _ = manager(paths, python, agents, FakeLaunchctl(loaded=True, bootout=5))
    service.install(grace_s=30, start=False)
    with pytest.raises(ServiceError, match="would not stop the job: Boot-out failed"):
        service.uninstall()
    assert service.plist_file.exists()


def test_restarting_asks_launchd_to_kill_and_start_again(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True))
    again = service.restart()
    assert ("kickstart", "-k", service.target) in fake.calls
    assert again.started and again.status.pid == 700 and again.status.runs == 4


def test_restarting_a_job_that_is_not_loaded_says_how_to_load_it(paths, python, agents):
    service, fake = manager(paths, python, agents)
    with pytest.raises(ServiceError, match="pa service install"):
        service.restart()
    assert "kickstart" not in fake.names()


def test_a_restart_launchd_refuses_is_reported(paths, python, agents):
    service, _ = manager(paths, python, agents, FakeLaunchctl(loaded=True, kickstart=113))
    with pytest.raises(ServiceError, match="could not restart the job: no such job"):
        service.restart()


# ---- waiting for launchd: it answers before it is done -------------------------------------------------
def test_uninstalling_waits_until_launchd_has_really_let_go_of_the_job(paths, python, agents):
    sleeps: list[float] = []
    fake = FakeLaunchctl(loaded=True, linger=3)
    service, _ = manager(paths, python, agents, fake, sleep=sleeps.append)
    service.install(grace_s=30, start=False)
    removed = service.uninstall()
    assert removed.was_loaded and removed.stopped
    assert sleeps == [POLL_S] * 3  # it looked, waited, looked again until the job was gone
    assert not service.is_loaded()


def test_a_job_that_will_not_go_is_reported_not_hidden(paths, python, agents):
    sleeps: list[float] = []
    service, _ = manager(
        paths, python, agents, FakeLaunchctl(loaded=True, linger=10**6), sleep=sleeps.append
    )
    service.install(grace_s=1, start=False)  # ExitTimeOut is 16 seconds
    assert service.installed_exit_timeout() == 16
    removed = service.uninstall()
    assert not removed.stopped and removed.removed_file and not service.plist_file.exists()
    assert len(sleeps) == math.ceil((16 + 5) / POLL_S)  # the job's own time to stop, plus a margin


def test_a_job_whose_file_is_gone_is_waited_for_as_long_as_a_default_job(paths, python, agents):
    sleeps: list[float] = []
    service, _ = manager(
        paths, python, agents, FakeLaunchctl(loaded=True, linger=10**6), sleep=sleeps.append
    )
    removed = service.uninstall()
    assert not removed.stopped and len(sleeps) == math.ceil((STOP_WAIT_S + 5) / POLL_S)


def test_reinstalling_over_a_job_that_is_still_stopping_waits_before_loading_again(
    paths, python, agents
):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True, linger=4))
    done = service.install(grace_s=30)
    names = fake.names()
    assert done.loaded and fake.loaded
    assert names.index("bootout") < names.index("bootstrap")
    assert names.count("bootstrap") == 1  # it waited instead of being refused and retrying
    between = names[names.index("bootout") + 1 : names.index("bootstrap")]
    assert between.count("print") >= 4 and set(between) == {"print"}


def test_a_new_copy_is_not_loaded_while_the_old_one_is_still_running(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True, linger=10**6))
    with pytest.raises(ServiceError, match="has not finished stopping"):
        service.install(grace_s=1)
    assert "bootstrap" not in fake.names()


def test_the_exit_timeout_is_read_back_from_the_job_file(paths, python, agents):
    service, _ = manager(paths, python, agents)
    assert service.installed_exit_timeout() is None  # no file yet
    service.install(grace_s=30, start=False)
    assert service.installed_exit_timeout() == 45.0


@pytest.mark.parametrize("value", [0, -5, True, "45", [1], {}])
def test_an_exit_timeout_that_makes_no_sense_is_ignored(paths, python, agents, value):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_bytes(plistlib.dumps({"ExitTimeOut": value}))
    assert service.installed_exit_timeout() is None


def test_a_job_file_without_an_exit_timeout_or_that_is_not_a_property_list_has_none(
    paths, python, agents
):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_bytes(plistlib.dumps({}))
    assert service.installed_exit_timeout() is None
    service.plist_file.write_bytes(plistlib.dumps([45]))
    assert service.installed_exit_timeout() is None
    service.plist_file.write_text("garbage")
    assert service.installed_exit_timeout() is None


def test_a_restart_that_launchd_delays_is_waited_for(paths, python, agents):
    sleeps: list[float] = []
    service, _ = manager(
        paths, python, agents, FakeLaunchctl(loaded=True, spawn_after=3), sleep=sleeps.append
    )
    again = service.restart()
    assert again.started and again.status.pid == 700 and again.status.runs == 4
    assert sleeps == [1.0] * 4  # three looks at the old run, then the new one


def test_a_restart_that_has_not_happened_in_time_is_not_claimed(paths, python, agents):
    sleeps: list[float] = []
    service, _ = manager(
        paths, python, agents, FakeLaunchctl(loaded=True, spawn_after=None), sleep=sleeps.append
    )
    again = service.restart(wait_s=5)
    assert not again.started and (again.status.pid, again.status.runs) == (689, 3)
    assert len(sleeps) == 5


def test_the_default_wait_for_a_restart_covers_the_throttle_and_the_old_copys_exit(
    paths, python, agents
):
    sleeps: list[float] = []
    service, _ = manager(
        paths, python, agents, FakeLaunchctl(loaded=True, spawn_after=None), sleep=sleeps.append
    )
    service.install(grace_s=30, start=False)  # ExitTimeOut: 45 seconds
    service.restart()
    assert len(sleeps) == THROTTLE_S + 45 + 5
    sleeps.clear()
    service.plist_file.unlink()
    service.restart()
    assert len(sleeps) == math.ceil(THROTTLE_S + STOP_WAIT_S + 5)


def test_a_restart_is_told_by_a_new_pid_when_launchd_does_not_count_the_runs(paths, python, agents):
    sleeps: list[float] = []
    fake = FakeLaunchctl(loaded=True, spawn_after=2)
    fake.printed = RUNNING.replace("\truns = 3\n", "")
    service, _ = manager(paths, python, agents, fake, sleep=sleeps.append)
    again = service.restart()
    assert again.started and again.status.pid == 700 and again.status.runs is None
    assert len(sleeps) == 3


def test_a_job_between_two_runs_is_not_taken_for_a_restart(paths, python, agents):
    fake = FakeLaunchctl(loaded=True, spawn_after=0)
    fake.printed = WAITING  # a new `runs` count, but no process
    service, _ = manager(paths, python, agents, fake)
    again = service.restart(wait_s=3)
    assert not again.started and again.status.pid is None


def test_status_of_a_job_that_is_not_there_at_all(paths, python, agents):
    service, _ = manager(paths, python, agents)
    status = service.status()
    assert (status.installed, status.loaded, status.pid, status.state) == (False, False, None, None)


def test_status_of_a_job_with_a_file_that_launchd_does_not_know(paths, python, agents):
    service, _ = manager(paths, python, agents)
    service.install(grace_s=30, start=False)
    status = service.status()
    assert status.installed and not status.loaded


def test_status_of_a_running_job(paths, python, agents):
    service, fake = manager(paths, python, agents, FakeLaunchctl(loaded=True))
    service.install(grace_s=30, start=False)
    status = service.status()
    assert (status.installed, status.loaded, status.state) == (True, True, "running")
    assert (status.pid, status.runs, status.last_exit) == (689, 3, "1")
    assert fake.calls[-1] == ("print", f"gui/{UID}/{service.label}")


def test_status_of_a_job_that_is_loaded_but_not_running(paths, python, agents):
    fake = FakeLaunchctl(loaded=True)
    fake.printed = WAITING
    service, _ = manager(paths, python, agents, fake)
    status = service.status()
    assert status.loaded and status.pid is None and status.state == "not running"


def test_the_installed_program_is_read_back_from_the_file(paths, python, agents):
    service, _ = manager(paths, python, agents)
    assert service.installed_program() is None
    service.install(grace_s=30, start=False)
    assert service.installed_program() == [python, "-m", "personal_agent", "serve"]


def test_a_job_file_that_cannot_be_read_gives_no_program(paths, python, agents):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_text("not a plist")
    assert service.installed_program() is None
    service.plist_file.write_bytes(plistlib.dumps({"ProgramArguments": "not a list"}))
    assert service.installed_program() is None
    service.plist_file.write_bytes(plistlib.dumps(["a", "list"]))
    assert service.installed_program() is None


# ---- the real machine, where it is safe to touch ------------------------------------------------------
def test_the_interpreter_running_the_tests_can_import_the_agent():
    assert launchd.can_import(sys.executable) is None


def test_an_interpreter_that_cannot_import_it_says_why(tmp_path):
    broken = tmp_path / "python"
    broken.write_text('#!/bin/sh\necho "ModuleNotFoundError: nope" >&2\nexit 1\n')
    broken.chmod(0o755)
    assert launchd.can_import(str(broken)) == "ModuleNotFoundError: nope"


def test_a_program_that_cannot_even_start_is_reported_without_an_exception(tmp_path):
    assert "could not be run" in (launchd.can_import(str(tmp_path / "missing")) or "")


def test_a_process_that_dies_silently_still_gets_a_reason(tmp_path):
    silent = tmp_path / "python"
    silent.write_text("#!/bin/sh\nexit 3\n")
    silent.chmod(0o755)
    assert launchd.can_import(str(silent)) == "it exited with status 3"


def test_the_system_launchctl_reports_a_missing_job_without_raising():
    if not Path("/bin/launchctl").exists():
        pytest.skip("needs macOS")
    done = launchd.system_launchctl(
        "print", f"gui/{os.getuid()}/local.personal-agent.does-not-exist"
    )
    assert done.code != 0


# ---- the tail of a log ---------------------------------------------------------------------------------------
def test_the_tail_of_a_small_file_is_its_last_lines(tmp_path):
    log = tmp_path / "log"
    log.write_text("".join(f"line {n}\n" for n in range(10)))
    assert tail(log, 3) == ["line 7", "line 8", "line 9"]
    assert tail(log, 100) == [f"line {n}" for n in range(10)]
    assert tail(log, 0) == []


def test_the_tail_of_a_big_file_is_read_from_its_end(tmp_path):
    log = tmp_path / "log"
    log.write_text("".join(f"line {n:06d}\n" for n in range(200_000)))
    assert tail(log, 2) == ["line 199998", "line 199999"]


def test_the_tail_of_a_file_with_no_final_newline_keeps_the_last_line(tmp_path):
    log = tmp_path / "log"
    log.write_text("a\nb\nc")
    assert tail(log, 2) == ["b", "c"]


def test_the_tail_of_an_empty_or_missing_file_is_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("")
    assert tail(empty, 5) == [] and tail(tmp_path / "missing", 5) == []


def test_the_tail_survives_bytes_that_are_not_text(tmp_path):
    log = tmp_path / "log"
    log.write_bytes(b"ok\n\xff\xfe broken\nlast\n")
    assert tail(log, 3)[-1] == "last" and len(tail(log, 3)) == 3


# ---- details that decide whether the job works on a real machine -----------------------------------------
def test_job_files_go_in_the_users_launchagents_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert REAL_AGENTS_DIR() == tmp_path / "Library" / "LaunchAgents"


def test_secret_looking_names_are_recognised_whatever_their_case():
    for name in ("openai_api_key", "My_Token", "db_PASSWORD", "Session_Id"):
        assert launchd._SECRET_NAME.search(name), name
    for name in ("HTTPS_PROXY", "NO_PROXY", "LANG"):
        assert not launchd._SECRET_NAME.search(name), name


def test_a_path_written_with_a_tilde_is_judged_by_the_folder_it_means(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert protected_folder("~/Desktop/project/.venv/bin/python") == "Desktop"
    assert protected_folder("~/Developer/project/.venv/bin/python") is None


def test_the_job_can_find_the_tools_homebrew_installs(paths):
    plist = build_plist(paths, python="/x/bin/python", label="l", grace_s=30)
    entries = plist["EnvironmentVariables"]["PATH"].split(":")  # type: ignore[index]
    assert entries[0] == "/x/bin"
    assert entries[1:] == [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]


def test_hiding_a_value_never_invents_a_variable():
    plist = {"EnvironmentVariables": {"PATH": "/usr/bin", "HTTPS_PROXY": "http://u:p@h:1"}}
    shown = masked(plist, ["HTTPS_PROXY", "NOT_THERE"])
    assert shown["EnvironmentVariables"] == {"PATH": "/usr/bin", "HTTPS_PROXY": "(hidden)"}
    assert masked({"Label": "x"}, ["A"]) == {"Label": "x", "EnvironmentVariables": {}}
    assert (
        plist["EnvironmentVariables"]["HTTPS_PROXY"] == "http://u:p@h:1"
    )  # the original is intact


def completed(code: int, out: str = "", err: str = "") -> object:
    return lambda command, **_kw: subprocess.CompletedProcess(command, code, out, err)


def test_launchctl_is_run_by_its_full_path_and_its_answer_passed_on(monkeypatch):
    seen: dict = {}

    def fake(command, **kw):
        seen.update(command=command, **kw)
        return subprocess.CompletedProcess(command, 5, "out", "err")

    monkeypatch.setattr(launchd.subprocess, "run", fake)
    assert launchd.system_launchctl("print", "gui/1/x") == Done(5, "out", "err")
    assert seen["command"] == ["/bin/launchctl", "print", "gui/1/x"]
    assert 0 < seen["timeout"] <= 60 and seen["check"] is False


@pytest.mark.parametrize(
    "error", [FileNotFoundError("/bin/launchctl"), subprocess.TimeoutExpired("launchctl", 30)]
)
def test_a_launchctl_that_cannot_run_is_a_failed_call_not_an_exception(monkeypatch, error):
    def broken(*_a, **_kw):
        raise error

    monkeypatch.setattr(launchd.subprocess, "run", broken)
    assert launchd.system_launchctl("print", "x") == Done(127, "", type(error).__name__)


@pytest.mark.skipif(not REAL_PLUTIL, reason="needs macOS plutil")
def test_a_rejection_is_passed_on_in_the_parsers_words_or_in_ours(monkeypatch, tmp_path):
    file = tmp_path / "x.plist"
    monkeypatch.setattr(launchd.subprocess, "run", completed(1, "x.plist: bad\n", "and worse\n"))
    assert launchd.lint(file) == "x.plist: bad\nand worse"
    monkeypatch.setattr(launchd.subprocess, "run", completed(1))
    assert launchd.lint(file) == "the job file is not a valid property list"
    monkeypatch.setattr(launchd.subprocess, "run", completed(0, "OK"))
    assert launchd.lint(file) is None


@pytest.mark.skipif(not REAL_PLUTIL, reason="needs macOS plutil")
def test_a_parser_that_cannot_be_run_is_not_a_reason_to_refuse(monkeypatch, tmp_path):
    def broken(*_a, **_kw):
        raise OSError("no")

    monkeypatch.setattr(launchd.subprocess, "run", broken)
    assert launchd.lint(tmp_path / "x.plist") is None


def test_the_probe_asks_the_interpreter_to_import_the_agent_and_gives_it_nothing_secret(
    monkeypatch,
):
    seen: dict = {}

    def fake(command, **kw):
        seen.update(command=command, **kw)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "the-probe-must-not-see-this")
    monkeypatch.setattr(launchd.subprocess, "run", fake)
    assert launchd.can_import("/x/bin/python") is None
    assert seen["command"] == ["/x/bin/python", "-c", "import personal_agent"]
    assert set(seen["env"]) == {"PATH", "HOME"}


def test_the_reason_an_import_failed_is_the_last_thing_the_interpreter_said(monkeypatch):
    said = "Traceback (most recent call last):\n  File x\n\nModuleNotFoundError: no 'x'\n\n"
    monkeypatch.setattr(launchd.subprocess, "run", completed(1, "", said))
    assert launchd.can_import("/x/python") == "ModuleNotFoundError: no 'x'"


def test_a_field_is_only_taken_from_where_the_line_starts_with_it():
    assert parse_print("\tlast spawn state = zombie\n\tstate = running\n")["state"] == "running"
    assert parse_print("\tppid = 1\n\tpid = 689\n")["pid"] == "689"
    assert parse_print("\ttotal runs = 99\n\truns = 3\n")["runs"] == "3"
    assert parse_print("\tprevious last exit code = 9\n\tlast exit code = 0\n")["last_exit"] == "0"


def test_a_line_that_goes_on_after_the_number_is_not_taken_for_it():
    assert "pid" not in parse_print("\tpid = 12 (zombie)\n")
    assert "runs" not in parse_print("\truns = 3 or so\n")


def test_the_first_mention_of_a_field_is_the_jobs_own():
    text = (
        "\tpid = 1\n\tpid = 2\n\truns = 5\n\truns = 6\n\tlast exit code = 0\n\tlast exit code = 1\n"
    )
    assert parse_print(text) == {"pid": "1", "runs": "5", "last_exit": "0"}


def test_a_folder_is_not_a_program(paths, agents, tmp_path):
    folder = tmp_path / "bin"
    folder.mkdir()
    service, _ = manager(paths, str(folder), agents)
    with pytest.raises(ServiceError, match="is not a program that can be run"):
        service.plan(grace_s=30)


def test_a_program_given_by_a_relative_path_is_written_down_in_full(
    paths, agents, tmp_path, monkeypatch
):
    program = tmp_path / "rel" / "python"
    program.parent.mkdir()
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    service, _ = manager(paths, "rel/python", agents)
    planned = service.plan(grace_s=30)
    assert planned.program[0] == os.path.abspath("rel/python") and os.path.isabs(planned.program[0])


def test_one_warning_is_enough_when_both_the_program_and_the_code_are_in_a_protected_folder(
    paths, agents, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / "Desktop" / "venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.setattr(launchd, "_package_dir", lambda: str(tmp_path / "Documents" / "src"))
    service, _ = manager(paths, str(program), agents)
    [warning] = service.plan(grace_s=30).warnings
    assert warning.startswith("The program is in your Desktop folder")


def test_the_interpreter_that_is_tried_out_is_the_one_that_gets_installed(paths, python, agents):
    probed: list[str] = []
    service, _ = manager(paths, python, agents, probe=lambda p: probed.append(p))
    service.install(grace_s=30, start=False)
    assert probed == [os.path.abspath(python)]


def test_the_job_file_is_checked_as_a_temporary_file_before_it_is_put_in_place(
    paths, python, agents
):
    seen: list[tuple[str, bool]] = []

    def linter(file: Path) -> None:
        seen.append((file.name, service.plist_file.exists()))

    service, _ = manager(paths, python, agents, linter=linter)
    service.install(grace_s=30, start=False)
    assert seen == [(f".{service.label}.plist.tmp", False)]


def test_a_new_run_is_told_by_the_run_count_when_there_is_one_and_by_the_pid_when_not():
    from personal_agent.launchd import LaunchStatus, _is_a_new_run

    def status(pid, runs):
        return LaunchStatus(installed=True, loaded=True, pid=pid, runs=runs)

    assert _is_a_new_run(status(1, 3), status(2, 4))
    assert not _is_a_new_run(status(1, 3), status(1, 3))
    assert not _is_a_new_run(status(1, 3), status(2, 3))  # the count wins over a different pid
    assert not _is_a_new_run(status(1, 4), status(2, 3))
    assert _is_a_new_run(status(1, None), status(2, 7))  # no count before: the pid decides
    assert not _is_a_new_run(status(1, None), status(1, 7))
    assert _is_a_new_run(status(1, 3), status(2, None))  # no count now: the pid decides
    assert not _is_a_new_run(status(1, 3), status(1, None))
    assert _is_a_new_run(status(None, None), status(5, None))


def test_an_exit_timeout_may_be_a_fraction_of_a_second(paths, python, agents):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_bytes(plistlib.dumps({"ExitTimeOut": 20.5}))
    assert service.installed_exit_timeout() == 20.5
    service.plist_file.write_bytes(plistlib.dumps({"ExitTimeOut": 20}))
    assert service.installed_exit_timeout() == 20.0 and isinstance(
        service.installed_exit_timeout(), float
    )


def test_a_program_list_that_is_not_all_text_is_not_a_program(paths, python, agents):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_bytes(plistlib.dumps({"ProgramArguments": ["python", 1, "serve"]}))
    assert service.installed_program() is None


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        (["/x/python", "-m", "personal_agent", "serve"], "/x/python"),
        (
            ["/usr/bin/caffeinate", "-s", "/x/python", "-m", "personal_agent", "serve"],
            "/x/python",
        ),
        (["-m", "personal_agent", "serve"], None),
        (["/x/python", "-m", "other_module", "serve"], None),
        (["/x/python", "-c", "personal_agent"], None),
        (["/x/python", "-m"], None),
        (["/x/python"], None),
        ([], None),
    ],
)
def test_the_interpreter_is_the_program_in_front_of_dash_m_personal_agent(
    paths, python, agents, program, expected
):
    service, _ = manager(paths, python, agents)
    agents.mkdir(parents=True)
    service.plist_file.write_bytes(plistlib.dumps({"ProgramArguments": program}))
    assert service.installed_python() == expected


def test_a_log_is_private_from_the_moment_it_exists_even_where_chmod_is_refused(
    tmp_path, monkeypatch
):
    def refuse(self, *_a, **_kw):
        raise PermissionError("no")

    monkeypatch.setattr(Path, "chmod", refuse)
    log = tmp_path / "service.err.log"
    before = os.umask(0)  # whatever the user's umask is, the file must not rely on it
    try:
        launchd._touch_private(log)
    finally:
        os.umask(before)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
