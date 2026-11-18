"""The shipped worker Bridge and controller speak through an actual HTTP listener."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from personal_agent.agent_adapters.bridge import WorkerBridge, setup_credentials
from personal_agent.channels.http import HttpListener
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import build_web_app
from personal_agent.channels.web.security import allowed_hosts, allowed_origins


async def test_real_bridge_claim_question_restart_and_result(make_app, tmp_path):
    app = make_app()
    listener = HttpListener(name="agent-bridge-contract", host="127.0.0.1", port=0)
    port = listener.bind()
    origin = f"http://127.0.0.1:{port}"
    asgi = build_web_app(
        app,
        Ingress(app, wake=lambda: None),
        hosts=allowed_hosts("127.0.0.1", None, ()),
        origins=allowed_origins("127.0.0.1", port, None, ()),
        stop=asyncio.Event(),
        secure=False,
        wake=lambda: None,
    )
    server = asyncio.create_task(listener.serve(asgi))
    try:
        await listener.wait_started(5)
        async with httpx.AsyncClient(
            base_url=origin, headers={"Origin": origin, "X-PA": "1"}
        ) as browser:
            paired = await browser.post(
                "/api/pair", json={"code": app.web_auth.invite("test-phone").code}
            )
            assert paired.status_code == 200
            created = await browser.post("/api/v4/agents", json={"name": "Muse", "kind": "muse"})
            assert created.status_code == 201, created.text
            agent = created.json()
            agent_id = agent["agent"]["id"]
            home = tmp_path / "muse-bridge"
            setup_credentials(
                home,
                url=origin,
                agent_id=agent_id,
                space_id="personal-owner",
                token_value=agent["worker_token"],
            )
            bridge = WorkerBridge(home)
            assert (await asyncio.to_thread(bridge.hello))["agent"]["id"] == agent_id
            response = await browser.post(
                "/api/v4/tasks",
                json={
                    "client_request_id": "socket-integration",
                    "title": "Bridge contract",
                    "steps": [{"id": "research", "agent_id": agent_id, "prompt": "Summarize."}],
                },
            )
            assert response.status_code == 201, response.text
            task = response.json()["task"]
            approved = await browser.post(
                f"/api/v4/tasks/{task['id']}/approve",
                json={"revision": task["revision"], "plan_hash": task["plan_hash"]},
            )
            assert approved.status_code == 200
            nonce = bridge.begin()["worker_id"]
            run = (await asyncio.to_thread(bridge.claim, nonce))["attempt"]
            attempt_id = run["attempt_id"]
            await asyncio.to_thread(bridge.progress, attempt_id, "Reading sources.")
            question = await asyncio.to_thread(
                bridge.ask, attempt_id, "Which scope?", ("Small", "Full"), request_id="scope"
            )
            assert question["questions"][0]["id"] == "scope"
            answer = await browser.post(
                f"/api/v4/tasks/{task['id']}/questions/scope/answer",
                json={"answer": "Small"},
            )
            assert answer.status_code == 200, answer.text
            assert (await asyncio.to_thread(bridge.status, attempt_id))["questions"][0][
                "answer"
            ] == "Small"
            # A fresh bridge process must reuse durable identity and sequence state.
            recovered = WorkerBridge(home)
            resumed = (await asyncio.to_thread(recovered.claim, nonce))["attempt"]
            assert resumed["attempt_id"] == attempt_id
            await asyncio.to_thread(recovered.progress, attempt_id, "Ready for review.")
            output = tmp_path / "research.md"
            output.write_text("A sourced answer.\n")
            receipt = await asyncio.to_thread(
                recovered.report,
                attempt_id,
                status="succeeded",
                text="Research complete.",
                files=[output],
            )
            replayed = await asyncio.to_thread(
                recovered.report,
                attempt_id,
                status="succeeded",
                text="Research complete.",
                files=[output],
            )
            assert replayed == receipt
            accepted = await browser.post(
                f"/api/v4/tasks/{task['id']}/accept",
                json={
                    "step_id": "research",
                    "attempt_id": attempt_id,
                    "revision": task["revision"],
                    "plan_hash": task["plan_hash"],
                },
            )
            assert accepted.status_code == 200, accepted.text
            result = accepted.json()["task"]
            assert result["state"] == "succeeded"
            artifact = result["steps"][0]["attempt"]["result"]["artifacts"][0]
            downloaded = await browser.get(f"/api/v4/artifacts/{artifact['id']}")
            assert downloaded.content == output.read_bytes()
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(server, 5)
        listener.close()
