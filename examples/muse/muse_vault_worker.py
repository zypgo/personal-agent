#!/usr/bin/env python3
"""Muse-native worker CLI; the native agent executes tasks, this file never does.

Reuse the installed Personal Agent bridge, or load an explicitly SHA-256-verified export.
Only Muse's official Vault helper injects credentials; normal proxy/TLS stay enabled.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HELPERS = Path("/opt/muse/skills/custom-connector-setup/bin")
core = None


class AdapterError(RuntimeError):
    """Presentable adapter failures without credentials or remote bodies."""


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
        if not hasattr(bridge, "WorkerDispatcher"):
            raise AdapterError(
                "Use the matching updated Personal Agent bridge with shared dispatch support."
            )
        return bridge
    if not expected_sha256 or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise AdapterError(
            "An external --bridge requires its independently verified --bridge-sha256."
        )
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise AdapterError("The bridge must be a regular file, not a symbolic link.")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
        raise AdapterError("Bridge SHA-256 mismatch; do not execute the downloaded file.")
    name = "pa_muse_verified_bridge"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Execute the verified bytes, not a second read that could change after verification.
    exec(compile(raw, str(path), "exec"), module.__dict__)  # noqa: S102
    if getattr(module, "VERSION", None) != "pa-cloud-worker/1":
        raise AdapterError("Unsupported worker bridge protocol.")
    if not hasattr(module, "WorkerDispatcher"):
        raise AdapterError(
            "Re-export the matching bridge; the existing private ledger remains compatible."
        )
    return module


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise AdapterError("Invalid agent, space, worker, or attempt identifier.")
    return value


def load_vault_helpers():
    path = HELPERS / "dynamic_credentials.py"
    if not path.is_file() or path.resolve().parent != HELPERS.resolve():
        raise AdapterError("Muse's official custom connector helper is unavailable.")
    name = "pa_muse_official_dynamic_credentials"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Official helper imports may refer to sibling modules in the same installation.
    sys.path.insert(0, str(HELPERS))
    try:
        spec.loader.exec_module(module)
        return module.add_surrogate_to_request, module.read_json_response
    except Exception:
        raise AdapterError("Muse's official custom connector helper could not be loaded.") from None
    finally:
        sys.path.remove(str(HELPERS))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise core.RequestError("Redirect refused; preserve the original request.")


class BoundedResponse:
    """Keep the official JSON reader while bounding its response.read calls."""

    def __init__(self, response):
        self.response, self.remaining = response, core.MAX_RESPONSE_BYTES

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self, amount=-1):
        size = (
            self.remaining + 1 if amount is None or amount < 0 else min(amount, self.remaining + 1)
        )
        data = self.response.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise core.RequestError("Worker response exceeds 4 MiB; preserve the receipt.")
        return data


class VaultClient:
    def __init__(self, url, credential, agent_id, space_id, *, offline=False):
        origin = core.control_origin(url)
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.endswith(".")
            or "*" in parsed.hostname
        ):
            raise AdapterError("Muse Vault requires one exact HTTPS origin without wildcard hosts.")
        if not re.fullmatch(r"custom\.[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", credential):
            raise AdapterError("Use the approved custom.<provider> Vault credential name.")
        self.origin = urllib.parse.urlunsplit(("https", parsed.netloc.lower(), "", "", ""))
        self.host, self.credential = parsed.hostname, credential
        self.agent_id, self.space_id = identifier(agent_id), identifier(space_id)
        self.offline = offline
        if offline:
            return  # Local diagnostics need only the identity binding, never the Vault helper.
        self.authorize, self.decode = load_vault_helpers()
        # Default ProxyHandler and certificate validation are intentionally retained.
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, action, payload=None):
        if self.offline:
            raise AdapterError("Offline diagnostics cannot send worker requests.")
        artifact_route = bool(
            re.fullmatch(r"runs/[A-Za-z0-9_.:-]+/artifacts/[A-Za-z0-9_.:-]+", action)
        )
        if (
            artifact_route
            or action in {"status"}
            or re.fullmatch(r"runs/[A-Za-z0-9_.:-]+/(?:status|stop-receipt)", action)
        ):
            valid = method == "GET"
        else:
            valid = method == "POST" and bool(
                re.fullmatch(
                    r"(?:hello|claim|runs/[A-Za-z0-9_.:-]+/(?:heartbeat|events|result|question|uncertain))",
                    action,
                )
            )
        if not valid or (method == "GET" and payload is not None):
            raise core.BridgeError("Invalid worker operation.")
        query = {"space": self.space_id}
        if artifact_route:
            query["encoding"] = "base64"
        url = (
            f"{self.origin}/api/v4/agents/{self.agent_id}/worker/{action}?"
            + urllib.parse.urlencode(query)
        )
        encoded = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if encoded is not None and len(encoded) > core.MAX_REQUEST_BYTES:
            raise core.BridgeError("Worker request exceeds 16 MiB.")
        request = urllib.request.Request(  # noqa: S310 -- exact HTTPS origin validated above
            url,
            data=encoded,
            method=method,
            headers={
                "Origin": self.origin,
                "X-PA": "1",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pa-muse-vault-worker/1",
            },
        )
        try:
            self.authorize(request, self.credential, allowed_hosts=[self.host])
            with self.opener.open(request, timeout=120) as response:
                if response.status != 200:
                    raise core.RequestError(
                        "Unexpected HTTP status; preserve the pending request.",
                        status=response.status,
                    )
                result = self.decode(BoundedResponse(response))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise core.RequestError(
                f"Worker HTTP {status}; preserve the receipt and check authorization/status.",
                status=status,
            ) from None
        except core.BridgeError:
            raise
        except Exception:
            # Helper/network errors may contain requests or secret surrogates. Never echo them.
            raise core.RequestError(
                "Vault or network response unconfirmed; preserve the receipt and retry unchanged."
            ) from None
        if not isinstance(result, dict):
            raise core.RequestError("Worker response is not a JSON object; preserve the receipt.")
        try:
            self.validate_response(action, payload, result)
        except core.RequestError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, AdapterError):
            raise core.RequestError(
                "Invalid worker response; preserve the original request."
            ) from None
        return result

    def validate_response(self, action, payload, result):
        core.validate_worker_response(
            action, payload, result, agent_id=self.agent_id, space_id=self.space_id
        )

    def artifact(self, attempt_id, artifact_id):
        result = self.request(
            "GET", f"runs/{identifier(attempt_id)}/artifacts/{identifier(artifact_id)}"
        )
        try:
            raw = base64.b64decode(result["content_base64"], validate=True)
        except (KeyError, ValueError, TypeError):
            raise core.RequestError("Artifact encoding is invalid.") from None
        if (
            result.get("id") != artifact_id
            or len(raw) > core.MAX_FILE_BYTES
            or result.get("size") != len(raw)
            or result.get("sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise core.RequestError("Artifact identity, size, or hash is invalid.")
        return raw


def view(attempt):
    return core.worker_attempt_view(attempt)


def has_owner_stop_proof(record, proof):
    return core.has_owner_stop_proof(record, proof)


class Dispatcher:
    """Compatibility facade for Muse's existing ledger and CLI."""

    def __init__(self, bridge):
        self._shared = core.WorkerDispatcher(
            bridge,
            ledger_name="muse-dispatch.json",
            lock_name="muse-dispatch",
            execution="native_muse",
        )

    def __getattr__(self, name):
        return getattr(self._shared, name)


def read_text(args):
    if getattr(args, "text_file", None):
        path = Path(args.text_file)
        if path.is_symlink() or not path.is_file():
            raise core.BridgeError("Text input must be a regular file, not a symbolic link.")
        with path.open("rb") as stream:
            raw = stream.read(256_001)
        if len(raw) > 256_000:
            raise core.BridgeError("Result text file exceeds 256 KiB.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise core.BridgeError("Text input must use UTF-8.") from None
    else:
        text = getattr(args, "text", None) or ""
    if len(text) > 64_000:
        raise core.BridgeError("Result text exceeds 64000 characters; attach a file instead.")
    return text


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", required=True, help="Exact approved control HTTPS origin")
    result.add_argument(
        "--credential", required=True, help="Approved Muse Vault custom.<provider> entry"
    )
    result.add_argument("--bridge", type=Path, help="Optional exported original bridge file")
    result.add_argument("--bridge-sha256", help="Independently verified SHA-256 of --bridge")
    result.add_argument("--agent", required=True, type=identifier)
    result.add_argument("--space", required=True, type=identifier)
    result.add_argument(
        "--home", type=Path, default=Path.home() / "workspace" / "personal-agent-muse-dispatch"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "local-status", help="Inspect sanitized local receipts without Vault access or network"
    )
    commands.add_parser(
        "reconcile", help="Confirm owner stop before releasing an unresolved local handoff"
    )
    commands.add_parser(
        "probe", help="Authorize official Vault transport and announce capabilities"
    )
    claim = commands.add_parser(
        "claim", help="Deliver one approved task once; idle/busy exits immediately"
    )
    claim.add_argument("--task", type=identifier)
    status = commands.add_parser("status", help="Read sanitized remote and local status")
    status.add_argument("attempt", nargs="?", type=identifier)
    for name in ("progress", "heartbeat", "result", "retry", "ask", "files", "uncertain"):
        command = commands.add_parser(name)
        command.add_argument("attempt", type=identifier)
        if name in {"progress", "result", "ask", "uncertain"}:
            text = command.add_mutually_exclusive_group(required=name != "result")
            text.add_argument("--text")
            text.add_argument("--text-file")
        if name == "result":
            command.add_argument(
                "--status", choices=("succeeded", "failed", "cancelled"), default="succeeded"
            )
            command.add_argument("--file", type=Path, action="append", default=[])
        if name == "ask":
            command.add_argument("--request-id", type=identifier, required=True)
            command.add_argument("--choice", action="append", default=[])
        if name == "files":
            command.add_argument("--out", type=Path, required=True)
    return result


def execute(args):
    bridge = core.WorkerBridge(
        args.home,
        client=VaultClient(
            args.url,
            args.credential,
            args.agent,
            args.space,
            offline=args.command == "local-status",
        ),
    )
    dispatcher = Dispatcher(bridge)
    with bridge._lock("muse-dispatch"):
        command = args.command
        if command == "local-status":
            return dispatcher.local_status()
        if command == "probe":
            response = bridge.hello()
            return {
                "connected": response.get("protocol_version") == 1,
                "agent_id": response.get("agent", {}).get("id"),
                "protocol_version": response.get("protocol_version"),
            }
        if command == "reconcile":
            return dispatcher.reconcile()
        if command == "claim":
            return dispatcher.claim(args.task)
        if command == "status":
            response = bridge.status(args.attempt)
            remote = (
                view(response)
                if args.attempt
                else {
                    "agent_id": response.get("agent", {}).get("id"),
                    "attempts": [view(item) for item in response.get("attempts", [])],
                }
            )
            return {"remote": remote, "local": dispatcher.local_status()}
        if command == "heartbeat":
            response = bridge.heartbeat(args.attempt)
        elif command == "progress":
            response = bridge.progress(args.attempt, read_text(args))
        elif command == "result":
            response = bridge.report(
                args.attempt, status=args.status, text=read_text(args), files=args.file
            )
        elif command == "retry":
            response = bridge.retry(args.attempt)
        elif command == "ask":
            response = bridge.ask(
                args.attempt, read_text(args), args.choice, request_id=args.request_id
            )
        elif command == "uncertain":
            response = bridge.uncertain(args.attempt, read_text(args))
        elif command == "files":
            return bridge.files(args.attempt, args.out)
        else:
            raise core.BridgeError("Unknown command.")
        return {"recorded": True, **view(response), "local": dispatcher.local_status()}


def main():
    global core
    try:
        args = parser().parse_args()
        core = load_core(args.bridge, args.bridge_sha256)
        output = execute(args)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        safe = isinstance(exc, AdapterError) or (
            core is not None and isinstance(exc, core.BridgeError)
        )
        message = (
            str(exc) if safe else "Local operation failed; preserve the ledger and inspect status."
        )
        print(json.dumps({"error": message, "reexecute": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
