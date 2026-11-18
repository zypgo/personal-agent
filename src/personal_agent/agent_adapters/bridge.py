#!/usr/bin/env python3
"""Standalone cloud-agent worker bridge. Python 3.11+, standard library only.

This file can be downloaded and run without installing Personal Agent. It never starts a
model or executes task text. The existing Grok/Muse agent owns execution and approvals.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
import ipaddress
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

VERSION = "pa-cloud-worker/1"
MAX_FILE_BYTES = 2_097_152
MAX_RESULT_FILES_BYTES = 8_388_608
MAX_REQUEST_BYTES = 16_777_216
MAX_RESPONSE_BYTES = 4_194_304
MAX_STATE_BYTES = 25_165_824
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
DEFAULT_TOKEN_ENV = "PA_AGENT_TOKEN"  # noqa: S105 — environment variable name, not a credential
CAPABILITIES = ["claim", "heartbeat", "progress", "result", "question", "cancel_observation"]


class BridgeError(RuntimeError):
    """Safe to show: never contains a credential, server body, or private endpoint."""


class RequestError(BridgeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BridgeError("Invalid agent, space, worker, or attempt identifier.")
    return value


def control_origin(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 33 for c in value):
        raise BridgeError("Use an HTTPS control origin without credentials or a path.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BridgeError("Invalid control origin.") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or "\\" in parsed.netloc
        or "%" in parsed.netloc
        or port == 0
    ):
        raise BridgeError("Use a control origin without credentials, a path, query, or fragment.")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise BridgeError("HTTPS is required; HTTP is allowed only on loopback for local testing.")
    return value.rstrip("/")


def _token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 8192
        or not value.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        raise BridgeError(
            "Missing or invalid agent-specific token; use stdin or a secret environment variable."
        )
    return value


def _private_home(home: Path) -> Path:
    home = home.expanduser().absolute()
    if home.is_symlink():
        raise BridgeError("Worker state directory must not be a symbolic link.")
    home = home.resolve(strict=False)
    if any((parent / ".git").exists() for parent in (home, *home.parents)):
        raise BridgeError("Keep worker credentials and receipts outside a Git worktree.")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not home.is_dir():
        raise BridgeError("Worker state path must be a private directory.")
    home.chmod(0o700)
    return home


def _write_private(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise BridgeError("Worker state must not use symbolic links.")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise BridgeError("Worker receipt exceeds the local state limit.")
    fd, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise BridgeError("Worker files must be regular, private files with mode 0600.")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise BridgeError("Worker files must belong to the current operating-system user.")
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise BridgeError("Worker state exceeds its size limit.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BridgeError("Worker state is invalid; preserve it for reconciliation.") from None
    if not isinstance(value, dict):
        raise BridgeError("Worker state must be a JSON object.")
    return value


@dataclass(frozen=True)
class Credentials:
    url: str
    agent_id: str
    space_id: str
    token: str = field(repr=False)


def setup_credentials(
    home: Path,
    *,
    url: str,
    agent_id: str,
    space_id: str,
    token_env: str = DEFAULT_TOKEN_ENV,
    token_value: str | None = None,
) -> dict[str, Any]:
    """Store either an environment reference or a token received through a secure stdin flow."""
    value: dict[str, Any] = {
        "version": 1,
        "url": control_origin(url),
        "agent_id": identifier(agent_id),
        "space_id": identifier(space_id),
    }
    if token_value is not None:
        value["token"] = _token(token_value)
    else:
        if not ENV_NAME.fullmatch(token_env):
            raise BridgeError("Use an environment variable name, never the token itself.")
        value["token_env"] = token_env
    root = _private_home(home)
    try:
        _write_private(root / "agent.json", value, replace=False)
    except FileExistsError:
        raise BridgeError(
            "This directory is already configured; keep its receipts and use a new directory for another identity."
        ) from None
    return {
        "configured": True,
        "agent_id": agent_id,
        "space_id": space_id,
        "token_stored": token_value is not None,
    }


def load_credentials(home: Path, environ: Mapping[str, str] | None = None) -> Credentials:
    value = _read_private(home.expanduser().absolute() / "agent.json")
    if value.get("version") != 1:
        raise BridgeError("Unsupported worker configuration version.")
    env = os.environ if environ is None else environ
    reference = value.get("token_env")
    if reference is not None:
        if not isinstance(reference, str) or not ENV_NAME.fullmatch(reference):
            raise BridgeError("Invalid token environment reference.")
        token = _token(env.get(reference, ""))
    else:
        token = _token(value.get("token", ""))
    return Credentials(
        control_origin(value.get("url", "")),
        identifier(value.get("agent_id", "")),
        identifier(value.get("space_id", "")),
        token,
    )


def update_credentials(
    home: Path, *, token_env: str = DEFAULT_TOKEN_ENV, token_value: str | None = None
) -> dict[str, Any]:
    """Replace only the credential after verifying the same server/agent/space binding."""
    root = _private_home(home)
    original = _read_private(root / "agent.json")
    if original.get("version") != 1:
        raise BridgeError("Unsupported worker configuration version.")
    value = {key: item for key, item in original.items() if key not in {"token", "token_env"}}
    if token_value is not None:
        secret = _token(token_value)
        value["token"] = secret
    else:
        if not ENV_NAME.fullmatch(token_env):
            raise BridgeError("Use an environment variable name, never the token value.")
        secret = _token(os.environ.get(token_env, ""))
        value["token_env"] = token_env
    credentials = Credentials(
        control_origin(value.get("url", "")),
        identifier(value.get("agent_id", "")),
        identifier(value.get("space_id", "")),
        secret,
    )
    response = WorkerClient(credentials).request("GET", "status")
    if response.get("agent", {}).get("id") != credentials.agent_id:
        raise BridgeError("The replacement token did not verify the existing agent identity.")
    current = _read_private(root / "agent.json")
    if any(
        current.get(key) != original.get(key) for key in ("version", "url", "agent_id", "space_id")
    ):
        raise BridgeError(
            "Worker identity changed during credential verification; no replacement was saved."
        )
    _write_private(root / "agent.json", value)
    return {
        "updated": True,
        "agent_id": credentials.agent_id,
        "space_id": credentials.space_id,
        "token_stored": token_value is not None,
    }


class BoundedConnection(http.client.HTTPConnection):
    """Bound DNS resolution as well as TCP/TLS; a slow resolver cannot hold the CLI open."""

    def __init__(
        self, hostname: str, port: int, *, timeout: float, tls_context: ssl.SSLContext | None
    ) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self.budget, self.tls_context = timeout, tls_context

    def connect(self) -> None:
        deadline = time.monotonic() + self.budget
        resolved: queue.Queue[Any] = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                resolved.put(socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM))
            except OSError as exc:
                resolved.put(exc)

        threading.Thread(target=resolve, daemon=True).start()
        try:
            addresses = resolved.get(timeout=self.budget)
        except queue.Empty:
            raise TimeoutError("DNS deadline expired.") from None
        if isinstance(addresses, OSError):
            raise addresses
        for family, kind, protocol, _canonical, address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Connection deadline expired.")
            connected = socket.socket(family, kind, protocol)
            try:
                connected.settimeout(remaining)
                self.sock = connected
                connected.connect(address)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TLS deadline expired.")
                connected.settimeout(remaining)
                if self.tls_context is not None:
                    self.sock = self.tls_context.wrap_socket(connected, server_hostname=self.host)
                return
            except OSError:
                connected.close()
                self.sock = None
        raise OSError("Control service connection failed.")


def _worker_request_body(
    method: str,
    action: str,
    payload: dict[str, Any] | None,
    *,
    binary: bool = False,
) -> bytes | None:
    parts = action.split("/")
    artifact_route = (
        binary
        and method == "GET"
        and payload is None
        and len(parts) == 4
        and parts[0] == "runs"
        and IDENTIFIER.fullmatch(parts[1])
        and parts[2] == "artifacts"
        and IDENTIFIER.fullmatch(parts[3])
    )
    if (
        not artifact_route
        and action not in {"hello", "claim", "status"}
        and not (
            len(parts) == 3
            and parts[0] == "runs"
            and IDENTIFIER.fullmatch(parts[1])
            and parts[2]
            in {
                "heartbeat",
                "events",
                "result",
                "question",
                "status",
                "uncertain",
                "stop-receipt",
            }
        )
    ):
        raise BridgeError("Invalid worker endpoint.")
    if method not in {"GET", "POST"}:
        raise BridgeError("Invalid worker request method.")
    body = (
        None
        if payload is None
        else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise BridgeError("Request exceeds 16 MiB; reduce the result or attachments.")
    return body


class WorkerClient:
    """Same-origin, no-redirect HTTP client. Does not read environment proxy settings."""

    def __init__(self, credentials: Credentials, *, timeout_s: float = 20.0) -> None:
        self.credentials = credentials
        self.origin = control_origin(credentials.url)
        self.agent_id = identifier(credentials.agent_id)
        self.space_id = identifier(credentials.space_id)
        _token(credentials.token)
        if not 0 < timeout_s <= 60:
            raise BridgeError("Request timeout must be between 0 and 60 seconds.")
        self.timeout_s = timeout_s

    def request(
        self, method: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raw = self._read_response(method, action, payload)
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise RequestError(
                "Control service returned invalid JSON; reconcile before retrying."
            ) from None
        if not isinstance(result, dict):
            raise RequestError("Control service returned an invalid worker response.")
        return result

    def artifact(self, attempt_id: str, artifact_id: str) -> bytes:
        return self._read_response(
            "GET", f"runs/{identifier(attempt_id)}/artifacts/{identifier(artifact_id)}", binary=True
        )

    def _read_response(
        self,
        method: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        binary: bool = False,
    ) -> bytes:
        body = _worker_request_body(method, action, payload, binary=binary)
        parsed = urlsplit(self.origin)
        hostname = parsed.hostname
        if hostname is None:
            raise BridgeError("Invalid control origin.")
        connection = BoundedConnection(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            timeout=self.timeout_s,
            tls_context=ssl.create_default_context() if parsed.scheme == "https" else None,
        )
        expired = threading.Event()
        connected_socket: socket.socket | None = None
        response: http.client.HTTPResponse | None = None

        def expire() -> None:
            expired.set()
            live_socket = connected_socket or connection.sock
            if live_socket is not None:
                try:
                    live_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(self.timeout_s, expire)
        timer.daemon = True
        path = (
            f"/api/v4/agents/{self.agent_id}/worker/{action}?{urlencode({'space': self.space_id})}"
        )
        headers = {
            "Authorization": f"Bearer {self.credentials.token}",
            "Origin": self.origin,
            "X-PA": "1",
            "Content-Type": "application/json",
            "Accept": "application/octet-stream" if binary else "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": VERSION,
        }
        timer.start()
        try:
            connection.connect()
            connected_socket = connection.sock
            if expired.is_set():
                raise RequestError(
                    "Request deadline expired; reconcile the original attempt before retrying."
                )
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                labels = {
                    400: "Worker request was rejected as invalid.",
                    401: "Agent token is invalid or revoked.",
                    403: "Agent is not authorized for this scope.",
                    409: "Attempt is stale, expired, or conflicts with saved state; reconcile it.",
                    413: "Worker request exceeds the server limit.",
                }
                message = labels.get(
                    response.status,
                    f"Worker request failed (HTTP {response.status}); redirects are not followed.",
                )
                raise RequestError(message, status=response.status)
            encoding = response.getheader("Content-Encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise RequestError("Compressed worker responses are not accepted.")
            raw = bytearray()
            limit = MAX_FILE_BYTES if binary else MAX_RESPONSE_BYTES
            while True:
                if expired.is_set():
                    raise RequestError("Request deadline expired; its outcome may be unknown.")
                chunk = response.read1(min(65_536, limit + 1 - len(raw)))
                raw.extend(chunk)
                if len(raw) > limit:
                    raise RequestError("Worker response exceeds its size limit.")
                if not chunk:
                    break
            if expired.is_set():
                raise RequestError("Request deadline expired; its outcome may be unknown.")
        except (OSError, http.client.HTTPException):
            raise RequestError(
                "Control service is unreachable or the request was interrupted; preserve the original worker ID and attempt."
            ) from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
        return bytes(raw)


class _NoWorkerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        if fp is not None:
            fp.close()
        raise RequestError(
            "Worker redirects are not followed; preserve the original request.", status=code
        )


class SystemProxyWorkerClient(WorkerClient):
    """Use the platform's normal proxy and verified TLS; never retry through another route."""

    def __init__(self, credentials: Credentials, *, timeout_s: float = 120.0) -> None:
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise BridgeError("Proxy request timeout must be between 0 and 120 seconds.")
        super().__init__(credentials, timeout_s=min(timeout_s, 60.0))
        self.timeout_s = timeout_s
        # Retain the default ProxyHandler, HTTPS certificate validation and environment policy.
        self.opener = urllib.request.build_opener(_NoWorkerRedirect())

    def _read_response(
        self,
        method: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        binary: bool = False,
    ) -> bytes:
        body = _worker_request_body(method, action, payload, binary=binary)
        url = (
            self.origin
            + f"/api/v4/agents/{self.agent_id}/worker/{action}?"
            + urlencode({"space": self.space_id})
        )
        request = urllib.request.Request(  # noqa: S310 -- control_origin permits HTTPS and explicit loopback HTTP only
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.credentials.token}",
                "Origin": self.origin,
                "X-PA": "1",
                "Content-Type": "application/json",
                "Accept": "application/octet-stream" if binary else "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": VERSION,
            },
        )
        limit = MAX_FILE_BYTES if binary else MAX_RESPONSE_BYTES
        deadline = time.monotonic() + self.timeout_s
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                if response.status != 200:
                    raise RequestError(
                        "Unexpected worker status; preserve the original request.",
                        status=response.status,
                    )
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in {"", "identity"}:
                    raise RequestError("Compressed worker responses are not accepted.")
                raw = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise RequestError(
                            "Worker response deadline expired; its outcome may be unknown."
                        )
                    chunk = response.read1(min(65_536, limit + 1 - len(raw)))
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise RequestError("Worker response exceeds its size limit.")
                    if not chunk:
                        break
                return bytes(raw)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise RequestError(
                f"Worker HTTP {code}; check authorization and preserve the original request.",
                status=code,
            ) from None
        except RequestError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise RequestError(
                "Platform proxy or worker request failed; preserve the receipt. No alternate route was attempted."
            ) from None


def _attempt(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("attempt", response)
    if not isinstance(value, dict) or not value.get("attempt_id"):
        raise RequestError("Control service did not return a valid attempt.")
    identifier(value["attempt_id"])
    return value


def _attachment(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError("Attachments must be regular files, not links.")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise BridgeError("Each attachment is limited to 2 MiB.")
    return {
        "name": path.name,
        "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class WorkerBridge:
    """Durable claim receipts and a same-payload outbox; no task execution loop."""

    def __init__(self, home: Path, *, client: WorkerClient | None = None) -> None:
        self.home = _private_home(home)
        self.client = client or WorkerClient(load_credentials(self.home))
        self.binding = {
            "url": self.client.origin,
            "agent_id": self.client.agent_id,
            "space_id": self.client.space_id,
        }

    def begin(self) -> dict[str, Any]:
        worker_id = "worker_" + secrets.token_hex(16)
        _write_private(
            self.home / "workers" / f"{worker_id}.json",
            {**self.binding, "worker_id": worker_id},
            replace=False,
        )
        return {"worker_id": worker_id}

    def _read(self, attempt_id: str) -> dict[str, Any]:
        value = _read_private(self.home / "attempts" / f"{identifier(attempt_id)}.json")
        if value.get("binding") != self.binding:
            raise BridgeError("Attempt belongs to another control service or agent identity.")
        attempt = value.get("attempt")
        if (
            not isinstance(attempt, dict)
            or attempt.get("attempt_id") != attempt_id
            or type(attempt.get("generation")) is not int
            or attempt["generation"] < 1
            or not isinstance(attempt.get("lease_token"), str)
            or not attempt["lease_token"]
            or not isinstance(attempt.get("payload"), dict)
            or type(value.get("sequence")) is not int
            or value["sequence"] < 0
            or (value.get("pending") is not None and not isinstance(value["pending"], dict))
        ):
            raise BridgeError("Attempt receipt is invalid; preserve it for reconciliation.")
        return value

    def _save(self, record: dict[str, Any]) -> None:
        _write_private(
            self.home / "attempts" / f"{identifier(record['attempt']['attempt_id'])}.json", record
        )

    @contextlib.contextmanager
    def _lock(self, identity: str) -> Iterator[None]:
        directory = self.home / "locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise BridgeError("Worker lock directory must not be a symbolic link.")
        fd = os.open(
            directory / identifier(identity),
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError(
                    "Another bridge call is using this receipt; wait for it before retrying."
                ) from None
            yield
        finally:
            os.close(fd)

    def hello(self) -> dict[str, Any]:
        return self.client.request(
            "POST", "hello", {"runtime_version": VERSION, "capabilities": CAPABILITIES}
        )

    def claim(self, worker_id: str, *, task_id: str | None = None) -> dict[str, Any]:
        with self._lock("claim-" + identifier(worker_id)):
            return self._claim(worker_id, task_id=task_id)

    def _claim(self, worker_id: str, *, task_id: str | None = None) -> dict[str, Any]:
        worker = identifier(worker_id)
        path = self.home / "workers" / f"{worker}.json"
        request: dict[str, Any] = {"worker_id": worker}
        if task_id is not None:
            request["task_id"] = identifier(task_id)
        if path.exists():
            existing = _read_private(path)
            if any(existing.get(key) != value for key, value in self.binding.items()):
                raise BridgeError("Worker nonce belongs to another identity.")
            if existing.get("claim") not in (None, request):
                raise BridgeError("Reuse a worker nonce only for the original claim.")
        else:
            existing = {**self.binding, "worker_id": worker}
        existing["claim"] = request
        _write_private(path, existing)
        response = self.client.request("POST", "claim", request)
        if "attempt" not in response:
            raise RequestError(
                "Claim response is missing its attempt field; preserve the worker nonce."
            )
        if response.get("attempt") is None:
            return response
        attempt = _attempt(response)
        if (
            type(attempt.get("generation")) is not int
            or attempt["generation"] < 1
            or not isinstance(attempt.get("lease_token"), str)
            or not attempt["lease_token"]
            or not isinstance(attempt.get("payload"), dict)
        ):
            raise RequestError("Claim response lacks its generation, receipt identity, or payload.")
        with self._lock(attempt["attempt_id"]):
            receipt_path = self.home / "attempts" / f"{attempt['attempt_id']}.json"
            if receipt_path.exists():
                record = self._read(attempt["attempt_id"])
                if record.get("worker_id") != worker:
                    raise RequestError(
                        "A different worker nonce cannot acquire an existing local attempt."
                    )
                previous = record["attempt"]
                if any(
                    previous.get(k) != attempt.get(k)
                    for k in ("attempt_id", "task_id", "generation", "lease_token")
                ):
                    raise RequestError(
                        "Claim changed an existing attempt's identity; reconcile it."
                    )
                record["attempt"].update(attempt)
            else:
                record = {
                    "binding": self.binding,
                    "worker_id": worker,
                    "attempt": attempt,
                    "sequence": 0,
                    "pending": None,
                }
            self._save(record)
        existing["attempt_id"] = attempt["attempt_id"]
        _write_private(path, existing)
        return response

    def status(self, attempt_id: str | None = None) -> dict[str, Any]:
        if attempt_id is None:
            return self.client.request("GET", "status")
        self._read(attempt_id)
        return self.client.request("GET", f"runs/{identifier(attempt_id)}/status")

    def stop_receipt(self, attempt_id: str) -> dict[str, Any]:
        self._read(attempt_id)
        return self.client.request("GET", f"runs/{identifier(attempt_id)}/stop-receipt")

    def _post(self, attempt_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock(identifier(attempt_id)):
            return self._post_locked(attempt_id, action, payload)

    def _post_locked(self, attempt_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._read(attempt_id)
        attempt = record["attempt"]
        body = {
            "generation": attempt["generation"],
            "lease_token": attempt["lease_token"],
            **payload,
        }
        if action == "events":
            body.setdefault("sequence", int(record["sequence"]) + 1)
        if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
            raise BridgeError("Request exceeds 16 MiB; reduce the result or attachments.")
        pending = {"action": action, "payload": body}
        if record.get("pending") not in (None, pending):
            raise BridgeError(
                "An earlier report is unconfirmed. Check status, then retry the saved report unchanged."
            )
        record["pending"] = pending
        self._save(record)
        try:
            response = self.client.request("POST", f"runs/{identifier(attempt_id)}/{action}", body)
        except RequestError as exc:
            if exc.status in {400, 413}:
                record["pending"] = None
                self._save(record)
            raise
        record["pending"] = None
        if action == "events":
            record["sequence"] = body["sequence"]
        if action == "result":
            record["result_receipt"] = response
        self._save(record)
        return response

    def heartbeat(self, attempt_id: str) -> dict[str, Any]:
        return self._post(attempt_id, "heartbeat", {})

    def uncertain(self, attempt_id: str, reason: str) -> dict[str, Any]:
        return self._post(attempt_id, "uncertain", {"reason": reason[:1000]})

    def progress(self, attempt_id: str, text: str) -> dict[str, Any]:
        return self._post(attempt_id, "events", {"kind": "progress", "text": text})

    def report(
        self, attempt_id: str, *, status: str, text: str, files: Sequence[Path] = ()
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise BridgeError("Result status must be succeeded, failed, or cancelled.")
        if len(files) > 8:
            raise BridgeError("At most eight result files are allowed.")
        artifacts = [_attachment(path) for path in files]
        if sum(len(item["content_base64"]) for item in artifacts) > 4 * (
            (MAX_RESULT_FILES_BYTES + 2) // 3
        ):
            raise BridgeError("Result attachments exceed the 8 MiB total limit.")
        if len({item["name"] for item in artifacts}) != len(artifacts):
            raise BridgeError("Result attachments must have distinct filenames.")
        return self._post(
            attempt_id, "result", {"status": status, "text": text, "artifacts": artifacts}
        )

    def ask(
        self,
        attempt_id: str,
        text: str,
        choices: Sequence[str] = (),
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            attempt_id,
            "question",
            {
                "request_id": identifier(request_id or "question_" + secrets.token_hex(12)),
                "text": text,
                "choices": list(choices),
            },
        )

    def retry(self, attempt_id: str) -> dict[str, Any]:
        record = self._read(attempt_id)
        pending = record.get("pending")
        if not isinstance(pending, dict):
            raise BridgeError("There is no saved unconfirmed report to retry.")
        payload = {
            key: value
            for key, value in pending["payload"].items()
            if key not in {"generation", "lease_token"}
        }
        return self._post(attempt_id, pending["action"], payload)

    def files(self, attempt_id: str, destination: Path) -> dict[str, Any]:
        record = self._read(attempt_id)
        target = destination.expanduser().absolute()
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink():
            raise BridgeError("Input destination must not be a symbolic link.")
        saved = []
        total_bytes = 0
        for index, dependency in enumerate(record["attempt"]["payload"].get("dependencies", [])):
            dependency_dir = target / identifier(dependency.get("step_id", f"dependency_{index}"))
            dependency_dir.mkdir(mode=0o700, exist_ok=True)
            if dependency_dir.is_symlink():
                raise BridgeError("Input dependency directory must not be a symbolic link.")
            for item in dependency.get("artifacts", []):
                name = item.get("name", "")
                if (
                    not isinstance(name, str)
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or Path(name).name != name
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                ):
                    raise BridgeError("Input artifact has an unsafe filename.")
                if "content_base64" in item:
                    try:
                        raw = base64.b64decode(item["content_base64"], validate=True)
                    except (ValueError, TypeError):
                        raise BridgeError("Input artifact encoding is invalid.") from None
                else:
                    if item.get("size", 0) > MAX_FILE_BYTES:
                        raise BridgeError("Each input artifact is limited to 2 MiB by this bridge.")
                    raw = self.client.artifact(attempt_id, identifier(item.get("id", "")))
                if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != item.get(
                    "sha256"
                ):
                    raise BridgeError("Input artifact failed its hash or size check.")
                total_bytes += len(raw)
                if total_bytes > MAX_RESULT_FILES_BYTES:
                    raise BridgeError("Dependency files exceed the 8 MiB total input limit.")
                # Uploaded project-instruction/config names must remain inert data.
                local_name = (
                    ("artifact_" + item["sha256"][:16] + "_" + name)
                    .encode()[:240]
                    .decode("utf-8", errors="ignore")
                )
                local_path = dependency_dir / local_name
                fd = os.open(
                    local_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                saved.append(
                    {
                        "name": name,
                        "sha256": item["sha256"],
                        "path": str(local_path),
                        "step_id": dependency.get("step_id"),
                    }
                )
        return {"files": saved}


LIVE_WORKER_STATES = frozenset({"claimed", "running", "waiting_input"})
COMPLETE_WORKER_STATES = frozenset(
    {
        "result_reported",
        "accepted",
        "rejected",
        "cancelled",
        "failed",
        "expired",
        "revoked",
        "stopped",
    }
)


def validate_worker_response(
    action: str,
    payload: dict[str, Any] | None,
    result: dict[str, Any],
    *,
    agent_id: str,
    space_id: str,
) -> None:
    """Validate native-agent replies before an outbox can be acknowledged."""
    try:
        _validate_worker_response(action, payload, result, agent_id=agent_id, space_id=space_id)
    except RequestError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, BridgeError):
        raise RequestError("Invalid worker response; preserve the original request.") from None


def _validate_worker_response(
    action: str,
    payload: dict[str, Any] | None,
    result: dict[str, Any],
    *,
    agent_id: str,
    space_id: str,
) -> None:
    """An HTTP 200 alone must not clear the bridge's durable outbox."""
    body = payload or {}
    failure = "Worker response does not match its operation; preserve the original request."

    def attempt(value: Any, expected_id: str | None = None) -> None:
        if not isinstance(value, dict):
            raise RequestError(failure)
        try:
            identifier(value.get("attempt_id", ""))
            identifier(value.get("task_id", ""))
        except BridgeError:
            raise RequestError(failure) from None
        if (
            value.get("agent_id") != agent_id
            or (expected_id is not None and value.get("attempt_id") != expected_id)
            or type(value.get("generation")) is not int
            or value["generation"] < 1
            or value.get("state") not in LIVE_WORKER_STATES | COMPLETE_WORKER_STATES | {"uncertain"}
            or value.get("desired_state") not in {"run", "cancel"}
            or not isinstance(value.get("lease_token"), str)
            or not value["lease_token"]
        ):
            raise RequestError(failure)
        if "generation" in body and (
            value["generation"] != body["generation"] or value["lease_token"] != body["lease_token"]
        ):
            raise RequestError(failure)

    if action == "hello":
        if (
            type(result.get("protocol_version")) is not int
            or result.get("protocol_version") != 1
            or result.get("agent", {}).get("id") != agent_id
        ):
            raise RequestError(failure)
    elif action == "status":
        if result.get("agent", {}).get("id") != agent_id or not isinstance(
            result.get("attempts"), list
        ):
            raise RequestError(failure)
        for item in result["attempts"]:
            attempt(item)
    elif action == "claim":
        if "attempt" not in result:
            raise RequestError(failure)
        value = result["attempt"]
        if value is None:
            return
        attempt(value)
        if (
            value.get("worker_id") != body.get("worker_id")
            or (body.get("task_id") is not None and value["task_id"] != body["task_id"])
            or not isinstance(value.get("payload"), dict)
            or value["payload"].get("space_id") != space_id
        ):
            raise RequestError(failure)
    else:
        parts = action.split("/")
        if "artifacts" in parts:
            return  # artifact() validates decoded size, ID, and content SHA-256.
        if parts[-1] == "stop-receipt":
            if (
                result.get("attempt_id") != parts[1]
                or type(result.get("generation")) is not int
                or result["generation"] < 1
                or "stopped_by" not in result
                or "stopped_at" not in result
            ):
                raise RequestError(failure)
            return
        attempt(result, parts[1])
        if parts[-1] == "events" and (
            type(result.get("last_sequence")) is not int
            or result.get("last_sequence") != body.get("sequence")
        ):
            raise RequestError(failure)
        if parts[-1] == "question":
            questions = result.get("questions")
            if not isinstance(questions, list):
                raise RequestError(failure)
            matching = [
                question
                for question in questions
                if isinstance(question, dict) and question.get("id") == body.get("request_id")
            ]
            if len(matching) != 1:
                raise RequestError(failure)
            question = matching[0]
            # The controller redacts text and choices. Stable identity and a complete
            # recorded question acknowledge the write without comparing unredacted secrets.
            if (
                question.get("attempt_id") != result.get("attempt_id")
                or question.get("task_id") != result.get("task_id")
                or question.get("state") not in {"open", "answered", "closed"}
                or not isinstance(question.get("text"), str)
                or not question["text"].strip()
                or not isinstance(question.get("choices"), list)
                or len(question["choices"]) != len(body.get("choices", []))
                or any(
                    not isinstance(choice, str) or not choice.strip()
                    for choice in question["choices"]
                )
            ):
                raise RequestError(failure)
        if parts[-1] == "uncertain" and (
            result.get("state") != "uncertain" or result.get("desired_state") != "cancel"
        ):
            raise RequestError(failure)
        if parts[-1] == "result" and (
            result.get("state") not in COMPLETE_WORKER_STATES | {"uncertain"}
            or not isinstance(result.get("result"), dict)
            or result["result"].get("outcome") != body.get("status")
            or type(result.get("result_eligible")) is not bool
        ):
            raise RequestError(failure)


def worker_attempt_view(attempt: dict[str, Any]) -> dict[str, Any]:
    """Never print a raw worker response: it includes the private lease token."""
    keys = (
        "attempt_id",
        "task_id",
        "step_id",
        "agent_id",
        "generation",
        "state",
        "desired_state",
        "desired_cancel",
        "last_sequence",
        "lease_expires_at",
        "result_eligible",
    )
    result = {key: attempt[key] for key in keys if key in attempt}
    if isinstance(attempt.get("result"), dict):
        result["result_outcome"] = attempt["result"].get("outcome")
    if isinstance(attempt.get("questions"), list):
        result["questions"] = [
            {
                key: question[key]
                for key in ("id", "request_id", "state", "text", "choices", "answer")
                if key in question
            }
            for question in attempt["questions"]
            if isinstance(question, dict)
        ]
    return result


def has_owner_stop_proof(record: dict[str, Any], proof: Any) -> bool:
    """Both recovery paths require the same explicit proof for the saved attempt."""
    if not isinstance(proof, dict):
        return False
    stopped_at = proof.get("stopped_at")
    valid_time = (type(stopped_at) is int and stopped_at > 0) or (
        type(stopped_at) is float and math.isfinite(stopped_at) and stopped_at > 0
    )
    return (
        proof.get("attempt_id") == record["attempt"]["attempt_id"]
        and type(proof.get("generation")) is int
        and proof["generation"] == record["attempt"]["generation"]
        and proof.get("state") in {"stopped", "accepted"}
        and isinstance(proof.get("stopped_by"), str)
        and bool(proof["stopped_by"].strip())
        and valid_time
    )


class WorkerDispatcher:
    """Durable one-time handoff shared by native agents; use the same command lock."""

    def __init__(
        self,
        bridge: WorkerBridge,
        *,
        ledger_name: str = "worker-dispatch.json",
        lock_name: str = "worker-dispatch",
        execution: str = "native_agent",
    ) -> None:
        self.bridge = bridge
        if ledger_name == "agent.json":
            raise BridgeError("Dispatch ledger cannot replace the credential configuration.")
        self.path = bridge.home / identifier(ledger_name)
        self.lock_name = identifier(lock_name)
        self.execution = execution

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        state = _read_private(self.path)
        if state.get("version") != 1 or state.get("binding") != self.bridge.binding:
            raise BridgeError("Dispatch ledger belongs to another version or agent; preserve it.")
        identifier(state.get("worker_id", ""))
        if state.get("attempt_id") is not None:
            identifier(state["attempt_id"])
        return state

    def new(self, task_id: str | None) -> dict[str, Any]:
        state = {
            "version": 1,
            "binding": self.bridge.binding,
            **self.bridge.begin(),
            "task_id": task_id,
            "attempt_id": None,
            "delivered": False,
        }
        _write_private(self.path, state)
        return state

    def local_status(self) -> dict[str, Any]:
        state = self.read()
        if state is None:
            return {"initialized": False}
        out = {
            "initialized": True,
            "attempt_id": state.get("attempt_id"),
            "delivered": state.get("delivered", False),
            "released": state.get("released", False),
        }
        if state.get("attempt_id"):
            record = self.bridge._read(state["attempt_id"])
            pending = record.get("pending")
            out.update(
                {
                    "pending_action": pending.get("action") if pending else None,
                    "result_confirmed": bool(record.get("result_receipt")),
                    "sequence": record["sequence"],
                }
            )
        return out

    def reconcile(self) -> dict[str, Any]:
        """Owner attestation releases admission, never replays an unconfirmed report."""
        state = self.read()
        if state is None or state.get("attempt_id") is None:
            return {"released": False, "reason": "no_local_attempt"}
        record = self.bridge._read(state["attempt_id"])
        stopped = self.bridge.stop_receipt(state["attempt_id"])
        if not has_owner_stop_proof(record, stopped):
            raise BridgeError("No explicit owner stop attestation; keep the original receipt.")
        state["released"] = True
        state["stop_receipt"] = stopped
        _write_private(self.path, state)
        return {
            "released": True,
            "attempt_id": state["attempt_id"],
            "pending_preserved": record.get("pending") is not None,
        }

    def claim(self, task_id: str | None = None) -> dict[str, Any]:
        # The CLI also holds this gate for progress/result/retry operations.
        state = self.read()
        if state and state.get("released"):
            state = None
        if state and state.get("attempt_id"):
            attempt_id = state["attempt_id"]
            record = self.bridge._read(attempt_id)
            if record.get("pending"):
                return {
                    "claimed": False,
                    "busy": True,
                    "attempt_id": attempt_id,
                    "pending_action": record["pending"]["action"],
                    "next": "status_then_retry",
                }
            try:
                current = self.bridge.status(attempt_id)
            except RequestError as exc:
                if exc.status != 409:
                    raise
                current = self.bridge.stop_receipt(attempt_id)
            stopped = has_owner_stop_proof(record, current)
            confirmed = bool(record.get("result_receipt")) or stopped
            if not confirmed or current.get("state") not in COMPLETE_WORKER_STATES:
                return {"claimed": False, "busy": True, **worker_attempt_view(current)}
            # Retain historical attempt/worker receipts. Only the dispatch pointer advances.
            state = None
        if state and state.get("task_id") != task_id:
            raise BridgeError(
                "An unfinished claim uses another task filter; reuse the original filter."
            )
        state = state or self.new(task_id)
        response = self.bridge.claim(state["worker_id"], task_id=task_id)
        attempt = response.get("attempt")
        if attempt is None:
            return {"claimed": False, "idle": True}
        if (
            attempt.get("agent_id") != self.bridge.client.agent_id
            or attempt.get("worker_id") != state["worker_id"]
            or (task_id is not None and attempt.get("task_id") != task_id)
            or attempt.get("payload", {}).get("space_id") != self.bridge.client.space_id
        ):
            raise RequestError("Claimed task identity mismatch; preserve the original nonce.")
        state.update({"attempt_id": identifier(attempt["attempt_id"]), "delivered": True})
        # Persist before stdout: a repeated schedule never re-delivers an execution instruction.
        _write_private(self.path, state)
        if (
            attempt.get("state") not in LIVE_WORKER_STATES
            or attempt.get("desired_cancel")
            or attempt.get("desired_state") != "run"
        ):
            return {"claimed": False, "busy": True, **worker_attempt_view(attempt)}
        return {
            "claimed": True,
            **worker_attempt_view(attempt),
            "payload": attempt["payload"],
            "heartbeat_seconds": 60,
            "execution": self.execution,
            "handoff": "once",
        }


def public_result(value: Any, token: str = "") -> Any:
    """Do not echo credentials or attachment bodies to the agent transcript."""
    if isinstance(value, dict):
        return {
            key: public_result(item, token)
            for key, item in value.items()
            if key not in {"token", "lease_token", "content_base64", "authorization"}
        }
    if isinstance(value, list):
        return [public_result(item, token) for item in value]
    if isinstance(value, str) and token:
        return value.replace(token, "[REDACTED]")
    return value


def _text(args: argparse.Namespace) -> str:
    if getattr(args, "text_file", None):
        with Path(args.text_file).open("rb") as stream:
            raw = stream.read(256_001)
        if len(raw) > 256_000:
            raise BridgeError("Text exceeds 256 KB.")
        return raw.decode("utf-8")
    return str(getattr(args, "text", "") or "")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("PA_AGENT_HOME", "~/.personal-agent-worker")),
    )
    commands = cli.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "setup", help="Save a private worker connection; does not contact a model."
    )
    setup.add_argument("--url", required=True)
    setup.add_argument("--agent", required=True)
    setup.add_argument("--space", required=True)
    token = setup.add_mutually_exclusive_group()
    token.add_argument("--token-env", default="PA_AGENT_TOKEN")
    token.add_argument("--token-stdin", action="store_true")
    update = commands.add_parser(
        "update-credentials",
        help="Verify a replacement token for the same agent without resetting receipts.",
    )
    update_token = update.add_mutually_exclusive_group()
    update_token.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    update_token.add_argument("--token-stdin", action="store_true")
    for name in ("begin", "check", "hello"):
        commands.add_parser(name)
    claim = commands.add_parser("claim")
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--task-id")
    status = commands.add_parser("status")
    status.add_argument("attempt_id", nargs="?")
    for name in ("heartbeat", "retry"):
        command = commands.add_parser(name)
        command.add_argument("attempt_id")
    for name in ("progress", "report", "ask", "uncertain"):
        command = commands.add_parser(name)
        command.add_argument("attempt_id")
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--text")
        group.add_argument("--text-file", type=Path)
        if name == "report":
            command.add_argument(
                "--status", choices=["succeeded", "failed", "cancelled"], required=True
            )
            command.add_argument("--file", type=Path, action="append", default=[])
        elif name == "ask":
            command.add_argument("--choice", action="append", default=[])
            command.add_argument("--request-id")
    files = commands.add_parser("files")
    files.add_argument("attempt_id")
    files.add_argument("--out", type=Path, required=True)
    return cli


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    secret = ""
    try:
        if args.command == "update-credentials":
            value = sys.stdin.readline(8194).strip() if args.token_stdin else None
            result = update_credentials(args.home, token_env=args.token_env, token_value=value)
        elif args.command == "setup":
            value = sys.stdin.readline(8194).strip() if args.token_stdin else None
            result = setup_credentials(
                args.home,
                url=args.url,
                agent_id=args.agent,
                space_id=args.space,
                token_env=args.token_env,
                token_value=value,
            )
        else:
            bridge = WorkerBridge(args.home)
            secret = bridge.client.credentials.token
            if args.command == "begin":
                result = bridge.begin()
            elif args.command in {"hello", "check"}:
                result = bridge.hello()
            elif args.command == "claim":
                result = bridge.claim(args.worker_id, task_id=args.task_id)
            elif args.command == "status":
                result = bridge.status(args.attempt_id)
            elif args.command == "heartbeat":
                result = bridge.heartbeat(args.attempt_id)
            elif args.command == "progress":
                result = bridge.progress(args.attempt_id, _text(args))
            elif args.command == "uncertain":
                result = bridge.uncertain(args.attempt_id, _text(args))
            elif args.command == "report":
                result = bridge.report(
                    args.attempt_id, status=args.status, text=_text(args), files=args.file
                )
            elif args.command == "ask":
                result = bridge.ask(
                    args.attempt_id, _text(args), args.choice, request_id=args.request_id
                )
            elif args.command == "retry":
                result = bridge.retry(args.attempt_id)
            else:
                result = bridge.files(args.attempt_id, args.out)
        print(json.dumps(public_result(result, secret), ensure_ascii=False, allow_nan=False))
        return 0
    except (BridgeError, OSError, ValueError, UnicodeError):
        error = sys.exception()
        message = (
            str(error)
            if isinstance(error, BridgeError)
            else "Local worker state or input could not be read safely. Preserve receipts and check paths and permissions."
        )
        print(
            json.dumps({"error": public_result(message, secret)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
