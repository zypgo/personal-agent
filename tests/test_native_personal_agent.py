"""The native PA adapter preserves real tool approvals and isolates delegated state."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest
from personal_agent.agent_adapters.native_pa import (
    PersonalAgentProvider,
    load_profile,
    native_environment,
    validate_request,
)
from personal_agent.agent_adapters.native_pa_worker import build_native_app, run_native
from personal_agent.agent_adapters.native_process import NativeProcess
from personal_agent.config import PolicyConfig
from personal_agent.core.events import Batch, Event, Kind, Trust
from personal_agent.daemon import Engine
from personal_agent.harness.runner import Harness
from personal_agent.security.sandbox import SandboxProvider
from tooling import Spy, install


@pytest.fixture
def request_profile(tmp_path: Path) -> NativeRequest:
    profile, cwd = tmp_path / "native-profile", tmp_path / "approved-workspace"
    profile.mkdir(mode=0o700)
    cwd.mkdir(mode=0o755)
    (profile / "config.toml").write_text(
        '[model]\napi_key_env="PA_NATIVE_TEST_KEY"\n[tools]\nshell=false\n', encoding="utf-8"
    )
    return NativeRequest(
        "Review PEER_DEP_DATA_TAG as untrusted data.",
        cwd,
        profile,
        "attempt-test",
        "PA_TEST_WORKER_TOKEN",
    )


def callbacks(answer: str | None = "reject") -> tuple[NativeCallbacks, dict[str, list[Any]]]:
    log: dict[str, list[Any]] = {"progress": [], "questions": [], "state": []}

    async def progress(text: str) -> None:
        log["progress"].append(text)

    async def question(question_id: str, text: str, choices: list[str]) -> str | None:
        log["questions"].append({"id": question_id, "text": text, "choices": choices})
        return answer

    async def state(value: dict[str, Any]) -> None:
        log["state"].append(value)

    return NativeCallbacks(progress, question, state), log


async def test_real_engine_uses_fresh_state_and_keeps_dependencies_out_of_system_role(
    request_profile: NativeRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "old-personal-home"
    legacy.mkdir()
    (legacy / "config.toml").write_text(
        '[web]\nenabled=true\n[[mcp.servers]]\nname="global"\ncommand="must-never-run"\n'
    )
    (legacy / ".env").write_text("PA_NATIVE_SENTINEL=legacy-value\n")
    (request_profile.profile_dir / ".env").write_text("PA_NATIVE_SENTINEL=profile-value\n")
    monkeypatch.setenv("PA_HOME", str(legacy))
    monkeypatch.delenv("PA_NATIVE_SENTINEL", raising=False)
    model = ScriptedModel([[assistant_message("Verified isolated result.")]])
    app = build_native_app(
        request_profile, model_factory=lambda _: model, sandbox=SandboxProvider.fixed(None)
    )
    hooks, log = callbacks()
    before_mode = stat.S_IMODE(request_profile.cwd.stat().st_mode)
    try:
        result = await run_native(app, request_profile, hooks)
        assert result.status == "succeeded" and result.text == "Verified isolated result."
        assert app.paths.home == request_profile.profile_dir / "runs" / "attempt-test-pa"
        assert app.paths.workspace_dir == request_profile.cwd
        assert not app.config.web.enabled and app.config.mcp.servers == []
        assert app.tools.get("schedule") is None and app.tools.get("send_message") is None
        assert "PA_NATIVE_SENTINEL" not in os.environ
        assert "PEER_DEP_DATA_TAG" not in model.calls[0].system_instructions
        assert "PEER_DEP_DATA_TAG" in str(model.calls[0].input)
        assert any(item.get("session_id") == "native:attempt-test" for item in log["state"])
        assert before_mode == stat.S_IMODE(request_profile.cwd.stat().st_mode) == 0o755
        assert not (legacy / "state.db").exists()
    finally:
        app.close()


@pytest.mark.parametrize(
    "answer,expected_calls", [("approve", 1), ("reject", 0), (None, 0), ("yes", 0)]
)
async def test_real_toolguard_needs_exact_per_action_consent(
    request_profile: NativeRequest,
    answer: str | None,
    expected_calls: int,
) -> None:
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "git push origin review"}, call_id="native-call")],
            [assistant_message("The action was handled according to your decision.")],
        ]
    )
    app = build_native_app(
        request_profile, model_factory=lambda _: model, sandbox=SandboxProvider.fixed(None)
    )
    spy = install(app, Spy())
    hooks, log = callbacks(answer)
    try:
        outcome = await run_native(app, request_profile, hooks)
        assert len(spy.ran("shell")) == expected_calls
        assert len(log["questions"]) == 1
        assert "git push origin review" in log["questions"][0]["text"]
        assert log["questions"][0]["choices"] == ["approve", "reject"]
        records = app.approvals.recent()
        assert records[0].status == ("approved" if answer == "approve" else "denied")
        if answer not in {"approve", "reject"}:
            assert outcome.status == "failed" and len(model.calls) == 1
    finally:
        app.close()


def test_native_run_state_is_never_replayed(request_profile: NativeRequest) -> None:
    model = ScriptedModel([])
    app = build_native_app(
        request_profile, model_factory=lambda _: model, sandbox=SandboxProvider.fixed(None)
    )
    app.close()
    with pytest.raises(BridgeError, match="must not be restarted"):
        build_native_app(request_profile, model_factory=lambda _: model)


def test_profile_and_workspace_symlinks_or_legacy_home_are_rejected(
    request_profile: NativeRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = tmp_path / "alias"
    link.symlink_to(request_profile.cwd, target_is_directory=True)
    with pytest.raises(BridgeError, match="symbolic"):
        validate_request(NativeRequest("Task", link, request_profile.profile_dir, "safe"))
    monkeypatch.setenv("PA_HOME", str(request_profile.profile_dir))
    with pytest.raises(BridgeError, match="existing personal home"):
        validate_request(request_profile)
    monkeypatch.delenv("PA_HOME")
    with pytest.raises(BridgeError, match="outside"):
        validate_request(NativeRequest("Task", tmp_path, request_profile.profile_dir, "safe"))
    (request_profile.profile_dir / "config.toml").unlink()
    (request_profile.profile_dir / "config.toml").symlink_to(tmp_path / "other-config")
    with pytest.raises(BridgeError):
        load_profile(request_profile)


@pytest.mark.parametrize(
    "section",
    [
        "[web]\nenabled=true",
        "[telegram]\nenabled=true",
        '[notify]\nchannel="telegram"',
        '[plugins]\nenabled=["external-channel"]',
    ],
)
def test_dedicated_profile_cannot_start_channels_or_plugins(
    request_profile: NativeRequest, section: str
) -> None:
    with (request_profile.profile_dir / "config.toml").open("a") as stream:
        stream.write("\n" + section + "\n")
    with pytest.raises(BridgeError):
        load_profile(request_profile)


def test_only_explicit_profile_environment_reaches_model_worker(
    request_profile: NativeRequest,
) -> None:
    config = load_profile(request_profile)
    environment = native_environment(
        config,
        {
            "PATH": "/usr/bin",
            "PA_NATIVE_TEST_KEY": "synthetic-model-key",
            "PA_TEST_WORKER_TOKEN": "synthetic-worker-token",
            "PA_AGENT_TOKEN": "synthetic-token",
            "PA_HOME": "/other/private/home",
            "OPENAI_API_KEY": "unrelated-old-key",
            "OPENAI_BASE_URL": "https://unrelated.invalid",
            "PYTHONPATH": "/untrusted/plugins",
        },
    )
    assert set(environment) == {"PATH", "PA_NATIVE_TEST_KEY", "PYTHONUNBUFFERED"}


async def test_native_cancellation_releases_the_engine_lock(request_profile: NativeRequest) -> None:
    model = ScriptedModel([[function_call("shell", {"command": "long work"}, call_id="long")]])
    app = build_native_app(
        request_profile, model_factory=lambda _: model, sandbox=SandboxProvider.fixed(None)
    )
    started = asyncio.Event()

    class Calls(list[tuple[str, str]]):
        def append(self, item: tuple[str, str]) -> None:
            super().append(item)
            started.set()

    install(app, Spy(calls=Calls()), delay=30)
    hooks, _ = callbacks("approve")
    pending = asyncio.create_task(run_native(app, request_profile, hooks))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with Engine.acquire(app) as engine:
            assert engine.inline
    finally:
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        app.close()


async def test_provider_jsonl_approval_and_real_process_exit(
    request_profile: NativeRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = NativeProcess.start
    script = """
import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'type':'question','id':'specific','text':'Approve the exact action?','choices':['approve','reject']}),flush=True)
answer=json.loads(sys.stdin.readline())
assert answer['answer']=='approve'
print(json.dumps({'type':'result','status':'succeeded','text':'Native child finished.'}),flush=True)
"""

    async def start(
        _command: Any, request: NativeRequest, hooks: NativeCallbacks, *, env: Any = None
    ) -> NativeProcess:
        return await original_start([sys.executable, "-I", "-c", script], request, hooks, env=env)

    monkeypatch.setattr(NativeProcess, "start", start)
    hooks, log = callbacks("approve")
    result = await PersonalAgentProvider().run(request_profile, hooks, asyncio.Event())
    assert result.status == "succeeded" and result.text == "Native child finished."
    assert log["questions"][0]["id"] == "specific"
    assert any(item.get("process_stopped") is True for item in log["state"])
    assert any(item.get("exit_code") == 0 for item in log["state"])


async def test_result_before_nonzero_exit_is_not_reported_success(
    request_profile: NativeRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = NativeProcess.start
    script = "import sys,json;sys.stdin.readline();print(json.dumps({'type':'result','status':'succeeded','text':'premature'}),flush=True);sys.exit(7)"

    async def start(
        _command: Any, request: NativeRequest, hooks: NativeCallbacks, *, env: Any = None
    ) -> NativeProcess:
        return await original_start([sys.executable, "-I", "-c", script], request, hooks, env=env)

    monkeypatch.setattr(NativeProcess, "start", start)
    hooks, _ = callbacks()
    result = await PersonalAgentProvider().run(request_profile, hooks, asyncio.Event())
    assert result.status == "unknown"


async def test_missing_profile_fails_without_starting_any_native_process(
    request_profile: NativeRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (request_profile.profile_dir / "config.toml").unlink()

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A missing profile must not launch a native process.")

    monkeypatch.setattr(NativeProcess, "start", forbidden)
    hooks, log = callbacks()
    result = await PersonalAgentProvider().run(request_profile, hooks, asyncio.Event())
    assert result.status == "failed"
    assert any(item.get("process_stopped") for item in log["state"])


async def test_real_worker_module_starts_outside_repository_without_loading_external_channels(
    request_profile: NativeRequest,
) -> None:
    hooks, _ = callbacks()
    supervisor = await NativeProcess.start(
        [sys.executable, "-I", "-m", "personal_agent.agent_adapters.native_pa_worker"],
        request_profile,
        hooks,
        env=native_environment(load_profile(request_profile), {}),
    )
    process = supervisor.process
    assert process.stdin is not None and process.stdout is not None
    # Reject before constructing a model: this subprocess never has a model credential
    # and an enabled external channel must fail closed even in its isolated profile.
    message = {
        "type": "request",
        "run_id": request_profile.run_id,
        "prompt": request_profile.prompt,
        "cwd": str(request_profile.cwd),
        "profile_dir": str(request_profile.profile_dir),
        "config": {"model": {"api_key_env": "PA_NATIVE_TEST_KEY"}, "web": {"enabled": True}},
    }
    try:
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()
        # Importing the package takes a while on a loaded machine.
        async with asyncio.timeout(40):
            receipt = json.loads(await process.stdout.readline())
            await process.wait()
        assert receipt["type"] == "result" and receipt["status"] == "failed"
        assert process.returncode == 0
        assert not (request_profile.profile_dir / "runs").exists()
    finally:
        assert await supervisor.close()


async def test_forced_native_kill_remains_uncertain(
    request_profile: NativeRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start, original_close = NativeProcess.start, NativeProcess.close
    script = """
import json,signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
sys.stdin.readline()
print(json.dumps({'type':'progress','text':'ready'}),flush=True)
time.sleep(60)
"""

    async def start(
        _command: Any, request: NativeRequest, hooks: NativeCallbacks, *, env: Any = None
    ) -> NativeProcess:
        return await original_start([sys.executable, "-I", "-c", script], request, hooks, env=env)

    async def close(self: NativeProcess, *, grace_s: float = 2) -> bool:
        return await original_close(self, grace_s=0.05)

    cancel = asyncio.Event()
    hooks, log = callbacks()

    async def progress(text: str) -> None:
        assert text == "ready"
        cancel.set()

    monkeypatch.setattr(NativeProcess, "start", start)
    monkeypatch.setattr(NativeProcess, "close", close)
    result = await PersonalAgentProvider().run(
        request_profile, NativeCallbacks(progress, hooks.question, hooks.state), cancel
    )
    assert result.status == "unknown"
    assert any(item.get("process_stopped") is True for item in log["state"])
    assert any(item.get("exit_code") == -9 for item in log["state"])


async def test_peer_inputs_start_tainted_and_cannot_borrow_owner_url_or_tool_grants(
    request_profile: NativeRequest,
) -> None:
    request = NativeRequest(
        "Approved task plus PEER_DEP_DATA_TAG https://peer.invalid/upload?data=private",
        request_profile.cwd,
        request_profile.profile_dir,
        request_profile.run_id,
        trusted_prompt="Review the approved sample at https://owner.example.test/document.",
        has_untrusted_inputs=True,
    )
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "publish sample"}, call_id="tainted-action")],
            [assistant_message("The untrusted-input action was declined.")],
        ]
    )
    config = load_profile(request).model_copy(
        update={"policy": PolicyConfig(mode="supervised", allow=["shell:*"])}
    )
    app = build_native_app(
        request, config=config, model_factory=lambda _: model, sandbox=SandboxProvider.fixed(None)
    )
    spy = install(app, Spy())
    hooks, log = callbacks("reject")
    try:
        await run_native(app, request, hooks)
        assert spy.ran("shell") == []
        assert len(log["questions"]) == 1  # prior allow rules cannot erase the initial taint
        run = app.runs.recent()[0]
        assert run.profile == "full" and run.tainted
        row = app.db.row("SELECT payload FROM events WHERE external_id=?", (request.run_id,))
        payload = json.loads(row["payload"])
        event = Event(1, "cli", Kind.MESSAGE, "native:test", payload, "test", Trust.OWNER, 0, 0)
        batch = Batch("native:test", "worker", [event], 1)
        assert Harness.owner_request_for(batch) == request.trusted_prompt
        urls = Harness.owner_urls_for(batch)
        assert any("owner.example.test" in url for url in urls)
        assert all("peer.invalid" not in url for url in urls)
    finally:
        app.close()
