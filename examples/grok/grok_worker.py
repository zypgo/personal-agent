#!/usr/bin/env python3
"""Persistent Grok Bot worker CLI; the existing Bot owns task execution and permissions.

Credentials require a verified environment injection mechanism. This is not a Grok Vault
API and does not export secrets. Use one --home for Routine wakes and scheduled polls.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

core = None


class AdapterError(RuntimeError):
    """Presentable error without credentials, remote response bodies or request headers."""


def load_core(path=None, expected_sha256=None):
    if path is None:
        if expected_sha256 is not None:
            raise AdapterError("--bridge-sha256 requires --bridge.")
        try:
            from personal_agent.agent_adapters import bridge
        except ImportError:
            raise AdapterError(
                "Install Personal Agent, or supply --bridge and --bridge-sha256."
            ) from None
        module = bridge
    else:
        if not expected_sha256 or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
            raise AdapterError("An external bridge requires its independently verified SHA-256.")
        path = Path(path).expanduser().absolute()
        if path.is_symlink() or not path.is_file():
            raise AdapterError("The bridge must be a regular file, not a symbolic link.")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
            raise AdapterError("Bridge SHA-256 mismatch; the file was not executed.")
        name = "pa_grok_verified_bridge"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec"), module.__dict__)  # noqa: S102 -- verified bytes only
    if getattr(module, "VERSION", None) != "pa-cloud-worker/1" or not hasattr(
        module, "WorkerDispatcher"
    ):
        raise AdapterError("Use the matching current bridge with shared dispatch support.")
    return module


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise AdapterError("Invalid worker identifier.")
    return value


def token_env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", value):
        raise AdapterError(
            "Use a conventional uppercase environment variable name, never a secret value."
        )
    return value


def profile(home):
    value = core._read_private(core._private_home(home) / "agent.json")
    reference = value.get("token_env")
    if value.get("version") != 1 or "token" in value:
        raise AdapterError(
            "Grok requires an environment reference, not a stored or literal worker token."
        )
    token_env_name(reference)
    core.control_origin(value.get("url", ""))
    identifier(value.get("agent_id"))
    identifier(value.get("space_id"))
    if value.get("transport", "system-proxy") not in {"system-proxy", "direct"}:
        raise AdapterError("Invalid configured worker transport.")
    return value


class OfflineClient:
    """Only the non-secret binding is needed for locked local status."""

    def __init__(self, value):
        self.origin = core.control_origin(value["url"])
        self.agent_id, self.space_id = value["agent_id"], value["space_id"]

    def request(self, *args, **kwargs):
        raise AdapterError("Local status cannot send requests.")

    def artifact(self, *args, **kwargs):
        raise AdapterError("Local status cannot download artifacts.")


class CheckedClient:
    """Validate worker acknowledgements before the shared bridge clears its outbox."""

    def __init__(self, client):
        self.client = client
        self.credentials = client.credentials
        self.origin, self.agent_id, self.space_id = client.origin, client.agent_id, client.space_id

    def request(self, method, action, payload=None):
        if action == "claim":
            self.request(
                "GET", "status"
            )  # Re-check the current provider declaration before admission.
        response = self.client.request(method, action, payload)
        core.validate_worker_response(
            action, payload, response, agent_id=self.agent_id, space_id=self.space_id
        )
        if action in {"hello", "status"} and response.get("agent", {}).get("kind") != "grok":
            raise core.RequestError("The configured worker is not registered as a Grok Bot.")
        return response

    def artifact(self, attempt_id, artifact_id):
        return self.client.artifact(attempt_id, artifact_id)


def read_text(args):
    if getattr(args, "text_file", None):
        path = Path(args.text_file)
        if path.is_symlink() or not path.is_file():
            raise AdapterError("Text input must be a regular file, not a symbolic link.")
        with path.open("rb") as stream:
            raw = stream.read(256_001)
        if len(raw) > 256_000:
            raise AdapterError("Text file exceeds 256 KB.")
        text = raw.decode("utf-8")
    else:
        text = getattr(args, "text", None) or ""
    if len(text) > 64_000:
        raise AdapterError("Result text exceeds 64000 characters; attach a file instead.")
    return text


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--home", type=Path, required=True, help="Same private persistent directory for all wakes"
    )
    cli.add_argument("--bridge", type=Path)
    cli.add_argument("--bridge-sha256")
    cli.add_argument(
        "--transport",
        choices=("system-proxy", "direct"),
        help="Saved during setup; defaults to system-proxy",
    )
    commands = cli.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "setup", help="Save only the worker identity and environment variable name"
    )
    setup.add_argument("--url", required=True)
    setup.add_argument("--agent", required=True, type=identifier)
    setup.add_argument("--space", required=True, type=identifier)
    setup.add_argument("--token-env", required=True)
    commands.add_parser(
        "probe", help="Authenticate and register capabilities via hello; never claim a task"
    )
    commands.add_parser(
        "local-status", help="Read sanitized local receipts without reading the token or network"
    )
    commands.add_parser(
        "reconcile", help="Release admission only with the owner's explicit stop proof"
    )
    claim = commands.add_parser(
        "claim", help="Hand off one approved task once; wakes and schedules share this ledger"
    )
    claim.add_argument("--task", type=identifier)
    status = commands.add_parser("status")
    status.add_argument("attempt", nargs="?", type=identifier)
    for name in ("heartbeat", "progress", "ask", "files", "result", "retry", "uncertain"):
        command = commands.add_parser(name)
        command.add_argument("attempt", type=identifier)
        if name in {"progress", "ask", "result", "uncertain"}:
            text = command.add_mutually_exclusive_group(required=name != "result")
            text.add_argument("--text")
            text.add_argument("--text-file", type=Path)
        if name == "ask":
            command.add_argument("--request-id", required=True, type=identifier)
            command.add_argument("--choice", action="append", default=[])
        if name == "files":
            command.add_argument("--out", required=True, type=Path)
        if name == "result":
            command.add_argument(
                "--status", choices=("succeeded", "failed", "cancelled"), default="succeeded"
            )
            command.add_argument("--file", action="append", type=Path, default=[])
    return cli


def execute(args):
    if args.command == "setup":
        token_env_name(args.token_env)
        result = core.setup_credentials(
            args.home,
            url=args.url,
            agent_id=args.agent,
            space_id=args.space,
            token_env=args.token_env,
        )
        path = core._private_home(args.home) / "agent.json"
        value = core._read_private(path)
        value["transport"] = args.transport or "system-proxy"
        core._write_private(path, value)
        return {**result, "transport": value["transport"], "connected": False}
    value = profile(args.home)
    secret = ""
    if args.command == "local-status":
        client = OfflineClient(value)
    else:
        credentials = core.load_credentials(args.home)
        secret = credentials.token
        transport = args.transport or value.get("transport", "system-proxy")
        client_type = (
            core.SystemProxyWorkerClient if transport == "system-proxy" else core.WorkerClient
        )
        client = CheckedClient(client_type(credentials))
    worker = core.WorkerBridge(args.home, client=client)
    dispatcher = core.WorkerDispatcher(
        worker, ledger_name="grok-dispatch.json", lock_name="grok-dispatch", execution="native_grok"
    )
    with worker._lock(dispatcher.lock_name):
        command = args.command
        if command == "local-status":
            result = dispatcher.local_status()
        elif command == "probe":
            response = worker.hello()
            result = {
                "connected": True,
                "agent_id": client.agent_id,
                "kind": "grok",
                "protocol_version": response["protocol_version"],
            }
        elif command == "claim":
            result = dispatcher.claim(args.task)
        elif command == "reconcile":
            result = dispatcher.reconcile()
        elif command == "status":
            response = worker.status(args.attempt)
            remote = (
                core.worker_attempt_view(response)
                if args.attempt
                else {
                    "agent_id": client.agent_id,
                    "attempts": [core.worker_attempt_view(item) for item in response["attempts"]],
                }
            )
            result = {"remote": remote, "local": dispatcher.local_status()}
        elif command == "files":
            result = worker.files(args.attempt, args.out)
        else:
            if command == "heartbeat":
                response = worker.heartbeat(args.attempt)
            elif command == "progress":
                response = worker.progress(args.attempt, read_text(args))
            elif command == "ask":
                response = worker.ask(
                    args.attempt, read_text(args), args.choice, request_id=args.request_id
                )
            elif command == "result":
                response = worker.report(
                    args.attempt, status=args.status, text=read_text(args), files=args.file
                )
            elif command == "retry":
                response = worker.retry(args.attempt)
            elif command == "uncertain":
                response = worker.uncertain(args.attempt, read_text(args))
            else:
                raise AdapterError("Unknown worker command.")
            result = {
                "recorded": True,
                **core.worker_attempt_view(response),
                "local": dispatcher.local_status(),
            }
        return core.public_result(result, secret)


def main(argv=None):
    global core
    try:
        args = parser().parse_args(argv)
        core = load_core(args.bridge, args.bridge_sha256)
        print(json.dumps(execute(args), ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        safe = isinstance(exc, AdapterError) or (
            core is not None and isinstance(exc, core.BridgeError)
        )
        message = (
            str(exc)
            if safe
            else "Local worker operation failed; preserve receipts and inspect local-status."
        )
        print(json.dumps({"error": message, "reexecute": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
