"""Run this project's Personal Agent in an explicit, isolated host profile."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest, NativeResult
from personal_agent.agent_adapters.native_process import NativeProcess
from personal_agent.config import Config

MAX_PROTOCOL_BYTES = 1_000_000
MAX_NATIVE_TEXT = 200_000
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def checked_directory(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise BridgeError("Native paths must not contain symbolic links.")
    resolved = absolute.resolve(strict=True)
    if not resolved.is_dir():
        raise BridgeError("Native profile and working directory must exist.")
    return resolved


def validate_request(request: NativeRequest) -> tuple[Path, Path]:
    if not RUN_ID.fullmatch(request.run_id):
        raise BridgeError("Invalid native run identity.")
    if (
        not request.prompt.strip()
        or len(request.prompt) > MAX_NATIVE_TEXT
        or "\x00" in request.prompt
    ):
        raise BridgeError("The native request is empty or exceeds its context limit.")
    profile, cwd = checked_directory(request.profile_dir), checked_directory(request.cwd)
    legacy = Path(os.environ.get("PA_HOME", "~/.personal-agent")).expanduser().resolve()
    if profile == legacy or profile == Path("~/.personal-agent").expanduser().resolve():
        raise BridgeError("Use a dedicated Agent Host profile, never the existing personal home.")
    if profile.is_relative_to(cwd):
        raise BridgeError("The native profile must be outside the agent's readable workspace.")
    return profile, cwd


def native_config(config: Config) -> Config:
    """A request worker has no external conversation channel or background scheduler."""
    if (
        config.web.enabled
        or config.webhooks.enabled
        or config.webhooks.routes
        or config.telegram.enabled
        or config.dingtalk.enabled
        or config.plugins.enabled
        or config.notify.channel not in {"auto", "cli"}
    ):
        raise BridgeError(
            "Native Personal Agent profiles cannot enable listeners, external channels or plugins."
        )
    return config.model_copy(
        update={
            "notify": config.notify.model_copy(update={"channel": "cli"}),
            "dreaming": config.dreaming.model_copy(update={"enabled": False}),
        }
    )


def load_profile(request: NativeRequest) -> Config:
    profile, _ = validate_request(request)
    path = profile / "config.toml"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000:
                raise BridgeError("The native profile configuration is invalid or too large.")
            raw = tomllib.loads(stream.read(64_001).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError(
            "Configure config.toml in this dedicated Personal Agent host profile."
        ) from exc
    if not isinstance(raw.get("model"), dict) or not raw["model"].get("api_key_env"):
        raise BridgeError("Set model.api_key_env explicitly in the dedicated profile.")
    try:
        return native_config(Config.model_validate(raw))
    except ValueError as exc:
        raise BridgeError("The dedicated Personal Agent configuration is invalid.") from exc


def native_environment(config: Config, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Only explicitly configured credentials cross the native-process boundary."""
    source = os.environ if environ is None else environ
    names = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        config.model.api_key_env,
    }
    names.update(secret.env for secret in config.secrets.values())
    if config.media.transcribe_api_key_env:
        names.add(config.media.transcribe_api_key_env)
    if config.mcp.enabled:
        for server in config.mcp.servers:
            if server.enabled:
                names.update(server.referenced_env())
    result = {name: source[name] for name in names if name in source}
    for name in {"PA_HOME", "PA_AGENT_TOKEN", "OPENAI_BASE_URL", "PYTHONPATH", "PYTHONHOME"}:
        result.pop(name, None)
    result["PYTHONUNBUFFERED"] = "1"
    return result


class PersonalAgentProvider:
    async def run(
        self,
        request: NativeRequest,
        callbacks: NativeCallbacks,
        cancel: asyncio.Event,
    ) -> NativeResult:
        if cancel.is_set():
            await callbacks.state({"process_stopped": True, "native_terminal": "cancelled"})
            return NativeResult("cancelled", "Cancelled before Personal Agent started.")
        try:
            config = load_profile(request)
        except (BridgeError, OSError):
            await callbacks.state({"process_stopped": True, "native_terminal": "failed"})
            return NativeResult(
                "failed",
                "Configure a dedicated Personal Agent host profile with an explicit model credential reference and no external channels.",
            )
        supervisor = await NativeProcess.start(
            [sys.executable, "-I", "-m", "personal_agent.agent_adapters.native_pa_worker"],
            request,
            callbacks,
            env=native_environment(config),
        )
        process = supervisor.process
        assert process.stdin is not None and process.stdout is not None
        initial = {
            "type": "request",
            "run_id": request.run_id,
            "prompt": request.prompt,
            "cwd": str(request.cwd.resolve()),
            "profile_dir": str(request.profile_dir.resolve()),
            "trusted_prompt": request.trusted_prompt,
            "has_untrusted_inputs": request.has_untrusted_inputs,
            "config": config.model_dump(mode="json"),
        }

        async def send(value: dict[str, Any]) -> None:
            assert process.stdin is not None
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if len(encoded) > MAX_PROTOCOL_BYTES:
                raise BridgeError("The native protocol message is too large.")
            process.stdin.write(encoded)
            await process.stdin.drain()

        async def exchange() -> NativeResult:
            await send(initial)
            assert process.stdout is not None
            while line := await process.stdout.readline():
                if len(line) > MAX_PROTOCOL_BYTES:
                    raise BridgeError("The native protocol response is too large.")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise BridgeError("Invalid Personal Agent worker response.")
                kind = value.get("type")
                if kind == "state":
                    updates = value.get("data")
                    if not isinstance(updates, dict):
                        raise BridgeError("Invalid native state receipt.")
                    await callbacks.state(updates)
                elif kind == "progress":
                    text = value.get("text")
                    if isinstance(text, str):
                        await callbacks.progress(text[:1000])
                elif kind == "question":
                    question_id, text, choices = (
                        value.get("id"),
                        value.get("text"),
                        value.get("choices"),
                    )
                    if (
                        not isinstance(question_id, str)
                        or len(question_id) > 128
                        or not isinstance(text, str)
                        or not 1 <= len(text) <= 4000
                        or choices != ["approve", "reject"]
                    ):
                        raise BridgeError("Invalid or incomplete native approval request.")
                    answer = await callbacks.question(question_id, text, choices)
                    # No fuzzy matching, default consent or approval inferred from plan approval.
                    await send(
                        {
                            "type": "answer",
                            "id": question_id,
                            "answer": answer if answer in {"approve", "reject"} else None,
                        }
                    )
                elif kind == "result":
                    status, text = value.get("status"), value.get("text")
                    if (
                        status not in {"succeeded", "failed", "cancelled", "unknown"}
                        or not isinstance(text, str)
                        or len(text) > MAX_NATIVE_TEXT
                    ):
                        raise BridgeError("Invalid native final receipt.")
                    return NativeResult(status, text)
                else:
                    raise BridgeError("Unknown native protocol response.")
            return NativeResult(
                "unknown", "Personal Agent exited without a final execution receipt."
            )

        work = asyncio.create_task(exchange())
        cancelled = asyncio.create_task(cancel.wait())
        result = NativeResult("unknown", "Personal Agent execution could not be confirmed.")
        exited = False
        stopped = False
        try:
            done, _ = await asyncio.wait({work, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                result = NativeResult("cancelled", "Personal Agent cancellation was requested.")
            else:
                result = await work
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                    exited = True
                except TimeoutError:
                    result = NativeResult(
                        "unknown", "Personal Agent reported a result but did not finish stopping."
                    )
        except (BridgeError, OSError, ValueError):
            result = NativeResult(
                "unknown", "Personal Agent stopped without a valid final receipt."
            )
        finally:
            for pending in (work, cancelled):
                if not pending.done():
                    pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError, ValueError, BridgeError):
                    await pending
            stopped = await supervisor.close(grace_s=5)
        if (
            not stopped
            or (result.status == "succeeded" and (not exited or process.returncode != 0))
            or (result.status == "cancelled" and process.returncode != 0)
        ):
            result = NativeResult(
                "unknown", "Personal Agent process termination remains unconfirmed."
            )
        await callbacks.state({"native_terminal": result.status})
        return result
