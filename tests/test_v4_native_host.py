from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from personal_agent.agent_adapters import native_process
from personal_agent.agent_adapters.bridge import BridgeError, WorkerBridge
from personal_agent.agent_adapters.codex import CodexProvider
from personal_agent.agent_adapters.cursor import CursorProvider
from personal_agent.agent_adapters.host import AgentHost, exclusive_lock
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest
from personal_agent.agent_adapters.native_process import (
    NativeProcess,
    process_identity,
    process_matches,
)
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
        ("codex_permission", "accept"),
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
    cb, _, _ = callbacks(decision="accept")
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
