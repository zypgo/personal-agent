from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import psutil
import pytest
from typer.testing import CliRunner

from personal_agent.agent_adapters import native_process
from personal_agent.agent_adapters.bridge import BridgeError, WorkerBridge
from personal_agent.agent_adapters.codex import (
    APPROVAL_ANSWERS,
    COMMAND_APPROVAL,
    FILE_APPROVAL,
    CodexProvider,
    approval_text,
)
from personal_agent.agent_adapters.cursor import CursorProvider
from personal_agent.agent_adapters.host import AgentHost, exclusive_lock
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest
from personal_agent.agent_adapters.native_process import (
    NativeProcess,
    process_identity,
    process_matches,
    process_resolved,
)
from personal_agent.commands.agents import app as agents_cli
from test_v4_agent_bridge import configured, worker_server

FIXTURE = Path(__file__).parent / "fixtures" / "native_protocol_agent.py"


def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir(exist_ok=True)
    return path


def provider(tmp_path, mode):
    command = (sys.executable, str(FIXTURE), mode, str(tmp_path / "wire.jsonl"))
    return CursorProvider(command) if mode.startswith("cursor") else CodexProvider(command)


def callbacks(*, decision=None, cancel=None):
    state, questions, progress = {}, [], []

    async def update(value):
        state.update(value)
        if cancel is not None and "turn_id" in value:
            cancel.set()

    async def ask(identity, text, choices):
        questions.append((identity, text, choices))
        return decision(choices) if callable(decision) else decision

    async def event(text):
        progress.append(text)

    return NativeCallbacks(event, ask, update), state, questions


@pytest.mark.parametrize("mode", ["codex_ok", "cursor_ok"])
async def test_native_stdio_completion_persists_identity_and_stops_group(tmp_path, mode):
    cb, state, _ = callbacks()
    result = await provider(tmp_path, mode).run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    assert result.status == "succeeded"
    assert result.text == "native result"
    assert state["session_id"]
    assert state["turn_id"]
    assert state["process"]["create_time"]
    assert state["process"]["boot_time"]
    assert state["process_stopped"]
    assert not process_matches(state["process"])


@pytest.mark.parametrize(
    "mode,answer",
    [
        ("codex_permission", "允许"),
        ("cursor_permission", "allow-once"),
        ("codex_permission", None),
        ("cursor_permission", None),
    ],
)
async def test_native_permission_requires_explicit_answer(tmp_path, mode, answer):
    cb, _, questions = callbacks(decision=answer)
    result = await provider(tmp_path, mode).run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    assert result.status == ("succeeded" if answer else "cancelled")
    assert len(questions) == 1
    assert "allow-always" not in questions[0][2]
    assert "acceptForSession" not in questions[0][2]


async def test_codex_asks_in_plain_words_and_offers_three_answers(tmp_path):
    cb, _, questions = callbacks(decision="允许")
    await provider(tmp_path, "codex_permission").run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    assert questions == [
        (
            questions[0][0],
            "Codex 想运行这条命令：\necho safe-fixture",
            ["允许", "拒绝", "拒绝并停止"],
        )
    ]
    assert APPROVAL_ANSWERS == {"允许": "accept", "拒绝": "decline", "拒绝并停止": "cancel"}


@pytest.mark.parametrize(
    "answer,decision",
    [
        ("允许", "accept"),
        ("拒绝", "decline"),
        ("拒绝并停止", "cancel"),
        (None, "cancel"),
        # Only the words that were offered count; anything else stops the attempt.
        ("accept", "cancel"),
        ("acceptForSession", "cancel"),
        ("", "cancel"),
    ],
)
async def test_codex_is_told_exactly_what_the_person_chose(tmp_path, answer, decision):
    cb, _, _ = callbacks(decision=answer)
    await provider(tmp_path, "codex_permission").run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    sent = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert [item["result"] for item in sent if item.get("id") == "approve_a"] == [
        {"decision": decision}
    ]


async def test_an_approval_answered_after_the_attempt_was_cancelled_is_not_granted(tmp_path):
    cancel = asyncio.Event()
    cb, _, _ = callbacks()

    async def answer_while_cancelling(identity, text, choices):
        cancel.set()
        return "允许"

    cb = NativeCallbacks(cb.progress, answer_while_cancelling, cb.state)
    result = await provider(tmp_path, "codex_permission").run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, cancel
    )
    assert result.status == "cancelled"
    sent = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert [item["result"] for item in sent if item.get("id") == "approve_a"] == [
        {"decision": "cancel"}
    ]


def host_command(tmp_path, *options):
    return CliRunner().invoke(
        agents_cli,
        ["--home", str(tmp_path / "home"), "host", "--cwd", str(tmp_path), *options],
    )


@pytest.mark.parametrize("provider_name,program", [("codex", "codex"), ("cursor", "agent")])
def test_the_host_says_at_once_when_the_program_cannot_be_found(
    tmp_path, monkeypatch, provider_name, program
):
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    result = host_command(tmp_path, "--provider", provider_name)
    assert result.exit_code == 1
    assert f"Cannot find '{program}' on this computer." in result.output
    assert "--executable" in result.output
    # It stopped before touching the connection, so no task could have been claimed.
    assert not (tmp_path / "home").exists()


def test_a_program_given_by_path_is_the_one_that_is_looked_for(tmp_path, monkeypatch):
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    missing = tmp_path / "elsewhere" / "codex"
    result = host_command(tmp_path, "--provider", "codex", "--executable", str(missing))
    assert result.exit_code == 1
    assert f"Cannot find '{missing}' on this computer." in result.output
    present = tmp_path / "codex"
    present.write_text("#!/bin/sh\n")
    present.chmod(0o755)
    result = host_command(tmp_path, "--provider", "codex", "--executable", str(present))
    assert "Cannot find" not in result.output


def test_a_provider_that_starts_no_other_program_is_not_asked_for_one(tmp_path, monkeypatch):
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    result = host_command(tmp_path, "--provider", "personal_agent")
    assert "Cannot find" not in result.output


IDS = {"threadId": "t_1", "turnId": "u_1", "itemId": "i_1", "startedAtMs": 1}


def test_a_command_approval_names_the_command_the_place_and_the_reason():
    text = approval_text(
        COMMAND_APPROVAL,
        {
            **IDS,
            "approvalId": None,
            "availableDecisions": ["accept", "cancel"],
            "command": "git status --short",
            "commandActions": [{"type": "unknown", "command": "git status --short"}],
            "cwd": "/work/attempt_1",
            "environmentId": "local",
            "kind": "command",
            "proposedExecpolicyAmendment": ["git", "status"],
            "reason": "check the tree",
        },
    )
    assert text == (
        "Codex 想运行这条命令：\ngit status --short\n\n在这个目录里：/work/attempt_1\n原因：check the tree"
    )


def test_a_command_is_shown_whole_however_long():
    command = "echo " + "x" * 3500 + "\necho second line"
    assert approval_text(COMMAND_APPROVAL, {**IDS, "command": command}).startswith(
        "Codex 想运行这条命令：\n" + command
    )


def test_a_request_to_use_the_network_says_where_to():
    text = approval_text(
        COMMAND_APPROVAL,
        {
            **IDS,
            "command": "curl example.test",
            "networkApprovalContext": {"host": "example.test", "protocol": "https"},
        },
    )
    assert text.endswith('需要联网：{"host": "example.test", "protocol": "https"}')


def test_what_codex_adds_that_this_code_does_not_know_is_still_shown():
    extra = {"additionalPermissions": {"fileSystem": {"write": ["/etc"]}}}
    text = approval_text(COMMAND_APPROVAL, {**IDS, "command": "ls", **extra, "nothing": None})
    assert (
        text
        == 'Codex 想运行这条命令：\nls\n\n其他信息：{"additionalPermissions": {"fileSystem": {"write": ["/etc"]}}}'
    )
    assert "nothing" not in text
    changes = approval_text(FILE_APPROVAL, {**IDS, "diff": "+a"})
    assert changes == 'Codex 想修改文件。\n\n其他信息：{"diff": "+a"}'


@pytest.mark.parametrize(
    "params",
    [
        {**IDS, "kind": "writeStdin", "command": "ls"},
        {**IDS, "kind": "humanCommand", "command": "ls"},
        {**IDS},
        {**IDS, "command": "   "},
        {**IDS, "command": ["ls"]},
    ],
)
def test_a_command_request_in_a_shape_it_does_not_know_is_shown_as_sent(params):
    text = approval_text(COMMAND_APPROVAL, params)
    head, body = text.split("\n", 1)
    assert head == "Codex 请求批准（item/commandExecution/requestApproval）："
    assert json.loads(body) == params


def test_a_file_change_approval_names_the_range_and_the_reason():
    assert approval_text(FILE_APPROVAL, IDS) == "Codex 想修改文件。"
    assert approval_text(FILE_APPROVAL, {**IDS, "grantRoot": "/work", "reason": "to write"}) == (
        "Codex 想修改文件。\n想要写入的范围：/work\n原因：to write"
    )


def test_any_other_request_is_shown_as_sent():
    assert approval_text("item/other/request", {"a": 1}) == (
        'Codex 请求批准（item/other/request）：\n{"a": 1}'
    )


@pytest.mark.parametrize("mode", ["codex_cancel", "cursor_cancel"])
async def test_native_cancel_requires_terminal_notification_not_send_ack(tmp_path, mode):
    cancel = asyncio.Event()
    cb, state, _ = callbacks(cancel=cancel)
    result = await provider(tmp_path, mode).run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, cancel
    )
    assert result.status == "cancelled"
    assert state["native_terminal"] in {"cancelled", "interrupted"}
    assert state["process_stopped"]


async def test_cursor_documented_extensions_use_sdk_and_human_answers(tmp_path):
    cb, _, questions = callbacks(decision=lambda choices: choices[0])
    result = await provider(tmp_path, "cursor_extensions").run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    assert result.status == "succeeded"
    assert len(questions) == 2


async def test_codex_wire_requests_match_cli_generated_official_schemas(tmp_path):
    cb, _, _ = callbacks(decision="允许")
    await provider(tmp_path, "codex_permission").run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    schemas = {
        "initialize": "InitializeParams",
        "thread/start": "ThreadStartParams",
        "turn/start": "TurnStartParams",
    }
    for line in (tmp_path / "wire.jsonl").read_text().splitlines():
        item = json.loads(line)
        assert "jsonrpc" not in item
        if item.get("method") in schemas:
            schema = json.loads(
                (FIXTURE.parent / "codex_schema" / (schemas[item["method"]] + ".json")).read_text()
            )
            jsonschema.validate(item["params"], schema)
        if item.get("id") == "approve_a":
            schema = json.loads(
                (
                    FIXTURE.parent / "codex_schema" / "CommandExecutionRequestApprovalResponse.json"
                ).read_text()
            )
            jsonschema.validate(item["result"], schema)


@pytest.mark.parametrize("mode", ["codex_eof", "cursor_eof"])
async def test_eof_after_native_start_stays_unknown(tmp_path, mode):
    cb, state, _ = callbacks()
    result = await provider(tmp_path, mode).run(
        NativeRequest("fixture", tmp_path, tmp_path, "run_a"), cb, asyncio.Event()
    )
    assert result.status == "unknown"
    assert state["process_stopped"]


async def test_host_actual_http_executes_once_and_recovers_only_report(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        host = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_ok"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
        )
        server["drop_result"] = True
        with pytest.raises(BridgeError):
            await host.run_once()
        calls_before = (tmp_path / "wire.jsonl").read_text().count('"method": "turn/start"')
        restored = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_ok"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
        )
        assert (await restored.run_once())["state"] == "result_reported"
        assert (
            (tmp_path / "wire.jsonl").read_text().count('"method": "turn/start"')
            == calls_before
            == 1
        )
        assert len(server["claims"]) == 1


async def test_unknown_host_execution_cannot_be_restarted_or_given_new_nonce(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        host = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_eof"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
        )
        with pytest.raises(BridgeError, match="unknown"):
            await host.run_once()
        with pytest.raises(BridgeError, match="unresolved"):
            await host.run_once()
        assert len(server["claims"]) == 1
        assert (tmp_path / "wire.jsonl").read_text().count('"method": "turn/start"') == 1


def test_profile_lock_and_pid_reuse_are_not_ownership(tmp_path):
    with exclusive_lock(tmp_path / "lock"), pytest.raises(BridgeError, match="owns"):  # noqa: SIM117 — the second acquisition must fail while the first lock is held
        with exclusive_lock(tmp_path / "lock"):
            pass
    identity = process_identity(os.getpid())
    assert process_matches(identity)
    assert not process_matches({**identity, "create_time": identity["create_time"] - 1})


async def test_process_group_cleanup_stops_descendants_and_removes_worker_secret(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRIVATE_WORKER_TEST_TOKEN", "not-for-native-agent")
    script = tmp_path / "child.py"
    script.write_text(
        "import subprocess,sys,time,os\nassert 'PRIVATE_WORKER_TEST_TOKEN' not in os.environ\np=subprocess.Popen([sys.executable,'-c','import time,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'], start_new_session=True)\nprint(p.pid,flush=True)\ntime.sleep(60)\n"
    )
    cb, state, _ = callbacks()
    native = await NativeProcess.start(
        (sys.executable, str(script)),
        NativeRequest("fixture", tmp_path, tmp_path, "run_a", "PRIVATE_WORKER_TEST_TOKEN"),
        cb,
    )
    child_pid = int(await native.process.stdout.readline())
    identity = process_identity(child_pid)
    assert await native.close(grace_s=0.1)
    assert not process_matches(identity)
    assert state["process_stopped"]
    assert state["descendants"]
    assert state["forced_kill"]


def test_macos_identity_branch_uses_creation_and_boot_times(tmp_path, monkeypatch):
    monkeypatch.setattr(native_process.sys, "platform", "darwin")
    identity = process_identity(os.getpid())
    assert "start_ticks" not in identity
    assert identity["create_time"]
    assert process_matches(identity)


def macos_getpgid(monkeypatch):
    """macOS answers getpgid() for a process that has exited but is not yet collected with
    "no such process"; Linux still names its group. Make this machine answer like a Mac."""
    real = os.getpgid

    def getpgid(pid):
        with contextlib.suppress(psutil.NoSuchProcess):
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                raise ProcessLookupError(3, "No such process")
        return real(pid)

    monkeypatch.setattr(native_process.os, "getpgid", getpgid)


def wait_until_zombie(pid):
    deadline = time.monotonic() + 15
    while psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
        assert time.monotonic() < deadline, "the child never finished"
        time.sleep(0.01)


def test_a_process_that_finished_but_was_not_collected_is_still_known_and_has_stopped(monkeypatch):
    macos_getpgid(monkeypatch)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        wait_until_zombie(child.pid)
        identity = process_identity(child.pid)
        assert identity is not None, "a process that is only waiting to be collected is not unknown"
        assert identity["status"] == psutil.STATUS_ZOMBIE
        assert identity["pgid"] is None
        assert process_resolved(identity)
        assert not process_matches(identity)
    finally:
        child.wait()

    # A running process whose group cannot be read is a different thing: it gets no identity.
    def vanished(pid):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(native_process.os, "getpgid", vanished)
    assert process_identity(os.getpid()) is None


async def test_a_descendant_that_finished_but_was_not_collected_does_not_make_the_stop_unconfirmed(
    tmp_path, monkeypatch
):
    macos_getpgid(monkeypatch)
    script = tmp_path / "child.py"
    script.write_text(
        "import subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(0.6)'])\n"
        "print(p.pid,flush=True)\ntime.sleep(60)\n"
    )
    cb, state, _ = callbacks()
    native = await NativeProcess.start(
        (sys.executable, str(script)),
        NativeRequest("fixture", tmp_path, tmp_path, "run_zombie", None),
        cb,
    )
    child_pid = int(await native.process.stdout.readline())
    # The child is seen while it runs. It then finishes, and its parent never collects it.
    await asyncio.to_thread(wait_until_zombie, child_pid)
    assert any(record["pid"] == child_pid for record in native.descendants.values())
    assert await native.close(grace_s=0.5)
    assert state["process_stopped"]
    assert state["descendant_coverage"] == "observed"


async def test_reconcile_requires_owner_server_confirmation_and_preserves_original_attempt(
    tmp_path,
):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        host = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_eof"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
        )
        with pytest.raises(BridgeError, match="unknown"):
            await host.run_once()
        with pytest.raises(BridgeError, match="owner"):
            await host.reconcile_local()
        server["stopped"] = True
        result = await host.reconcile_local()
        assert result == {"reconciled": True, "attempt_id": "attempt_a", "server_confirmed": True}
        assert len(server["claims"]) == 1
        assert (tmp_path / "wire.jsonl").read_text().count('"method": "turn/start"') == 1


async def test_mismatched_registered_provider_is_rejected_before_claim(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        server["kind"] = "grok"
        host = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_ok"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
        )
        with pytest.raises(BridgeError, match="kind"):
            await host.run_once()
        assert not server["claims"]
        assert not (tmp_path / "wire.jsonl").exists()


async def test_private_peer_task_cannot_use_owner_native_profile(tmp_path):
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        server["requester_id"] = "peer"
        host = AgentHost(
            WorkerBridge(home),
            provider(tmp_path, "codex_ok"),
            provider_name="codex",
            cwd=project(tmp_path),
            allow_direct_workdir=True,
            allow_shared_native_profile=True,
        )
        result = await host.run_once()
        assert result["state"] == "result_reported"
        assert server["result"]["status"] == "failed"
        assert not (tmp_path / "wire.jsonl").exists()


async def test_git_host_returns_patch_and_keeps_original_worktree_clean(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout

    git("init", "--quiet")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Test fixture")
    (source / "base.txt").write_text("original\n")
    git("add", "base.txt")
    git("commit", "--quiet", "-m", "fixture")
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        host = AgentHost(
            WorkerBridge(home), provider(tmp_path, "codex_edit"), provider_name="codex", cwd=source
        )
        assert (await host.run_once())["state"] == "result_reported"
        assert (source / "base.txt").read_text() == "original\n"
        assert not (source / "new.txt").exists()
        assert git("status", "--porcelain") == ""
        names = {item["name"] for item in server["result"]["artifacts"]}
        assert {"changes.patch", "changes.json", "new-files.tar"} <= names
        journal = json.loads(host.journal_path.read_text())
        assert journal["worktree"]["base_commit"] == git("rev-parse", "HEAD").strip()


async def test_dirty_git_source_is_rejected_without_claiming(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "--quiet", str(source)], check=True, timeout=5
    )
    (source / "dirty.txt").write_text("uncommitted")
    with worker_server() as (url, server):
        home = configured(tmp_path, url)
        host = AgentHost(
            WorkerBridge(home), provider(tmp_path, "codex_ok"), provider_name="codex", cwd=source
        )
        with pytest.raises(BridgeError):
            await host.run_once()
        assert not server["claims"]
