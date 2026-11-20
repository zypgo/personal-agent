"""Dispatch persisted cloud wakes without running another model or buying a sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.agent_adapters.cloud import CloudWakeConfig, wake_cloud_agent

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Daemon


class OrchestrationService:
    name = "agent-orchestration"

    def __init__(self, app: App) -> None:
        self.app = app

    async def tick(self) -> int:
        store = self.app.orchestration
        if store is None:
            return 0
        store.reconcile()
        dispatched = 0
        for candidate in store.wake_candidates(limit=10, kind="grok", wake_mode="grok_routine"):
            agent = candidate["agent"]
            # Manual, native schedules and owned hosts pull from the same durable queue.
            # Merely inspecting those modes never consumes a wake receipt.
            if agent["kind"] != "grok" or agent["wake_mode"] != "grok_routine":
                continue
            wake = store.reserve_wake(agent["id"], candidate["task_id"], timeout_seconds=60)
            if wake is None:
                continue
            refs = agent["secret_env"]
            config = CloudWakeConfig(
                provider="grok",
                mode="grok_routine",
                url_env=refs.get("webhook_url", ""),
                secret_env=refs.get("webhook_key", ""),
            )
            # Reservation is committed before the network call. Cancellation or a process
            # crash leaves it reconcilable as uncertain; neither causes an automatic resend.
            receipt = await wake_cloud_agent(
                config,
                wake_id=wake["id"],
                agent_id=agent["id"],
                task_id=candidate["task_id"],
                net=self.app.config.net,
            )
            outcome = (
                "sent"
                if receipt.state == "wake_accepted"
                else ("uncertain" if receipt.state == "wake_unknown" else "failed")
            )
            store.finish_wake(
                wake["id"],
                outcome=outcome,
                detail=receipt.code,
                http_status=receipt.http_status,
            )
            self.app.live.publish({"type": "agents_changed"})
            dispatched += 1
        return dispatched

    async def run(self, daemon: Daemon) -> None:
        # No unbounded HTTP fan-out, polling model calls, or extra queue service. The task
        # ledger remains authoritative across daemon restarts and child project spaces.
        with daemon._wake.listener() as event:
            while not daemon.stopping:
                event.clear()
                await self.tick()
                await daemon._nap(event, max(1.0, min(daemon.poll_s, 5.0)))
