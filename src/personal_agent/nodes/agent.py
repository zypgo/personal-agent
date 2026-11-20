"""A polling node worker: durable admission, detached execution, bounded recovery.

The caller holds the process lock. No unacknowledged result is garbage collected.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from collections.abc import Callable
from typing import Any

from personal_agent.core.live import preview_text
from personal_agent.nodes.backend import Backend, BackendError
from personal_agent.nodes.client import AuthorizationError, Transport, TransportError
from personal_agent.nodes.registry import DEFAULT_CAPABILITIES
from personal_agent.nodes.store import NodeStore, Receipt
from personal_agent.nodes.transfers import MAX_LOG_BYTES, collect_outputs, materialize
from personal_agent.security.redact import redact


class Runner:
    def __init__(
        self,
        store: NodeStore,
        backend: Backend,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.time,
        known_secrets: tuple[str, ...] = (),
        min_free_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.store, self.backend, self.transport = store, backend, transport
        self.clock, self.known_secrets = clock, known_secrets
        self.min_free_bytes = min_free_bytes
        self._tick_lock = asyncio.Lock()
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    @staticmethod
    def _path(receipt: Receipt, action: str) -> str:
        return f"/api/v3/nodes/attempts/{receipt.attempt_id}/{action}"

    @staticmethod
    def _body(receipt: Receipt) -> dict[str, Any]:
        return {
            "space_id": receipt.payload["space_id"],
            "generation": receipt.generation,
            "plan_hash": receipt.plan_hash,
        }

    def _safe_log(self, text: str) -> str:
        # Redact before truncation so clipping cannot expose the beginning of a token.
        text = preview_text(text)
        end = len(text)
        for secret in self.known_secrets:
            for size in range(min(len(secret) - 1, len(text)), 0, -1):
                if text.endswith(secret[:size]):
                    end = min(end, len(text) - size)
                    break
        return (
            redact(text[:end], self.known_secrets).encode()[:MAX_LOG_BYTES].decode(errors="replace")
        )

    async def tick(self) -> int:
        """One bounded reconciliation pass. Network failures leave receipts recoverable."""
        async with self._tick_lock:
            processed = 0
            authorization_lost = False
            for receipt in self.store.pending():
                try:
                    await self._advance(receipt)
                    processed += 1
                except AuthorizationError:
                    authorization_lost = True
                    # Token revocation blocks uploads, but must not leave the workload running.
                    self.store.update(receipt.attempt_id, desired_cancel=True)
                    try:
                        await self.backend.stop(receipt)
                    except (BackendError, OSError, TimeoutError):
                        self.store.update(receipt.attempt_id, state="uncertain")
                except (BackendError, OSError, TimeoutError):
                    # A failed inspect is not proof of absence. Never create a replacement.
                    self.store.update(receipt.attempt_id, state="uncertain")
                except TransportError:
                    # Running work still gets its local deadline even without the control plane.
                    if self.clock() >= receipt.deadline_at:
                        self.store.update(receipt.attempt_id, desired_cancel=True)
                        try:
                            await self.backend.stop(receipt)
                        except (BackendError, OSError, TimeoutError):
                            self.store.update(receipt.attempt_id, state="uncertain")
            if authorization_lost:
                raise AuthorizationError(
                    "node authorization was revoked; execution cancellation requested"
                )
            # One task at a time, including uncertain tasks: a lost lease is never extra capacity.
            if self.store.pending() or self._stopping:
                return processed
            if shutil.disk_usage(self.store.home).free < self.min_free_bytes:
                return processed
            body = await self.transport.request(
                "POST", "/api/v3/nodes/claim", {"capabilities": list(DEFAULT_CAPABILITIES)}
            )
            payload = body.get("attempt", body.get("job"))
            if payload is None and "attempt_id" in body:
                payload = body
            if payload is None:
                return processed
            if not isinstance(payload, dict):
                raise TransportError("control plane returned an invalid execution envelope")
            receipt = self.store.accept(payload, now=self.clock())
            await self._advance(receipt)
            return processed + 1

    async def _advance(self, receipt: Receipt) -> None:
        if receipt.acknowledged:
            await self._cleanup(receipt)
            return
        if receipt.result is not None:
            await self._send_result(receipt)
            return

        # POST carries the scope; never put authorization material in a URL query.
        state = await self.transport.request(
            "POST", self._path(receipt, "status"), self._body(receipt)
        )
        cancel = (
            receipt.desired_cancel
            or state.get("desired_state") == "cancel"
            or state.get("authorized") is False
            or self.clock() >= receipt.deadline_at
            or shutil.disk_usage(self.store.home).free < self.min_free_bytes
        )
        if cancel and not receipt.desired_cancel:
            receipt = self.store.update(receipt.attempt_id, desired_cancel=True)

        if not receipt.admitted:
            if cancel:
                await self._finish(receipt, "cancelled", None, "", "", [])
                return
            await self.transport.request(
                "POST",
                self._path(receipt, "admit"),
                {**self._body(receipt), "resource_id": receipt.resource_id},
            )
            receipt = self.store.update(receipt.attempt_id, admitted=True)

        observed = await self.backend.inspect(receipt)
        if observed.state == "missing":
            if receipt.state not in {"received", "prepared", "creating"}:
                self.store.update(receipt.attempt_id, state="uncertain")
                await self.transport.request(
                    "POST",
                    self._path(receipt, "observe"),
                    {**self._body(receipt), "state": "uncertain"},
                )
                return
            if cancel:
                await self._finish(receipt, "cancelled", None, "", "", [])
                return
            try:
                workspace = materialize(self.store.attempt_dir(receipt), receipt.payload)
            except (ValueError, OSError):
                await self._finish(
                    receipt, "failed", None, "", "Input snapshot failed validation.", []
                )
                return
            receipt = self.store.update(receipt.attempt_id, state="creating")
            await self.backend.create(receipt, workspace)
            receipt = self.store.update(receipt.attempt_id, state="created")
            observed = await self.backend.inspect(receipt)

        if observed.state == "created":
            if cancel:
                # Remove the unstarted resource before confirming cancellation. An older
                # start request may still be in flight; removal either wins that race or
                # fails because it is now running, which leaves the receipt recoverable.
                await self.backend.cleanup(receipt)
                await self._finish(receipt, "cancelled", None, "", "", [])
                return
            if receipt.state not in {"received", "prepared", "creating", "created"}:
                # A lost start response is not permission to repeat it. Even an inspect
                # showing 'created' can race a first request still executing at the daemon.
                self.store.update(receipt.attempt_id, state="uncertain")
                await self.transport.request(
                    "POST",
                    self._path(receipt, "observe"),
                    {**self._body(receipt), "state": "uncertain"},
                )
                return
            # Store start intent before the first possibly side-effecting call.
            receipt = self.store.update(receipt.attempt_id, state="starting")
            await self.backend.start(receipt)
            observed = await self.backend.inspect(receipt)

        if observed.state == "running":
            receipt = self.store.update(receipt.attempt_id, state="running")
            if cancel:
                await self.backend.stop(receipt)
                observed = await self.backend.inspect(receipt)
            if observed.state == "running":
                stdout, stderr = await self.backend.logs(receipt)
                stdout, stderr = self._safe_log(stdout), self._safe_log(stderr)
                digest = hashlib.sha256((stdout + "\x00" + stderr).encode()).hexdigest()
                report: dict[str, Any] = {**self._body(receipt), "state": "running"}
                if digest != receipt.log_hash or receipt.sequence == 0:
                    sequence = receipt.sequence + 1
                    # Persist the sequence first. A lost reply cannot reuse the same sequence
                    # for a later, different snapshot. Final results remain independently durable.
                    self.store.update(receipt.attempt_id, sequence=sequence, log_hash=digest)
                    report.update(
                        sequence=sequence, stdout=stdout, stderr=stderr, logs_snapshot=True
                    )
                await self.transport.request("POST", self._path(receipt, "observe"), report)
                return

        if observed.state == "exited":
            stdout, stderr = await self.backend.logs(receipt)
            status = (
                "cancelled" if cancel else ("succeeded" if observed.exit_code == 0 else "failed")
            )
            artifacts: list[dict[str, Any]] = []
            if not cancel:
                try:
                    artifacts = collect_outputs(
                        self.store.attempt_dir(receipt) / "workspace",
                        receipt.payload.get("output_paths", []),
                    )
                except (ValueError, OSError):
                    status, stderr = "failed", stderr + "\nOutput snapshot failed validation."
            await self._finish(receipt, status, observed.exit_code, stdout, stderr, artifacts)
            return
        if observed.state not in {"created", "running"}:
            self.store.update(receipt.attempt_id, state="uncertain")
            await self.transport.request(
                "POST",
                self._path(receipt, "observe"),
                {**self._body(receipt), "state": "uncertain"},
            )

    async def _finish(
        self,
        receipt: Receipt,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        artifacts: list[dict[str, Any]],
    ) -> None:
        result = {
            **self._body(receipt),
            "status": status,
            "exit_code": exit_code,
            "stdout": self._safe_log(stdout),
            "stderr": self._safe_log(stderr),
            "artifacts": artifacts,
        }
        receipt = self.store.update(receipt.attempt_id, state=status, result=result)
        await self._send_result(receipt)

    async def _send_result(self, receipt: Receipt) -> None:
        assert receipt.result is not None
        response = await self.transport.request(
            "POST", self._path(receipt, "complete"), receipt.result
        )
        # An explicit application acknowledgement is required, not merely a successful TCP/HTTP.
        if response.get("accepted") is not True:
            raise TransportError("execution result has not been durably acknowledged")
        receipt = self.store.update(
            receipt.attempt_id, acknowledged=True, cleanup_state="cleanup_pending"
        )
        await self._cleanup(receipt)

    async def _cleanup(self, receipt: Receipt) -> None:
        await self.backend.cleanup(receipt)
        directory = self.store.attempt_dir(receipt)
        if directory.exists():
            if directory.is_symlink():
                raise BackendError("refusing to clean a symlinked execution directory")
            shutil.rmtree(directory)
        response = await self.transport.request(
            "POST",
            self._path(receipt, "cleanup"),
            {**self._body(receipt), "released": True},
        )
        if response.get("accepted") is not True:
            raise TransportError("execution cleanup has not been durably acknowledged")
        # Keep the small immutable receipt as a dedupe tombstone.
        self.store.finish_cleanup(receipt.attempt_id)

    async def run(self, *, poll_s: float = 2) -> None:
        if not 0.1 <= poll_s <= 60:
            raise ValueError("poll interval must be between 0.1 and 60 seconds")
        delay = poll_s
        while not self._stopping:
            try:
                await self.tick()
                delay = poll_s
            except AuthorizationError:
                raise
            except (TransportError, BackendError, OSError, TimeoutError):
                delay = min(30, max(poll_s, delay * 2))
            await asyncio.sleep(delay)
