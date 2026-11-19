"""Real store + HTTP authorization across token generations; no provider account is used."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

from personal_agent.agent_adapters.bridge import (
    BridgeError,
    RequestError,
    WorkerBridge,
    WorkerClient,
    setup_credentials,
    update_credentials,
)
from personal_agent.agent_adapters.codex import CodexProvider
from personal_agent.agent_adapters.host import AgentHost
from test_agent_orchestration_http import approve, create_task
from web_fake import Web


async def test_rotated_credentials_recover_via_minimal_stop_receipt_without_reexecution(
    make_app, tmp_path, monkeypatch
):
    site = Web(make_app())
    site.pair()

    def through_actual_http(self, method, action, payload=None, *, binary=False):
        path = (
            f"/api/v4/agents/{self.agent_id}/worker/{action}?{urlencode({'space': self.space_id})}"
        )
        response = site.client.request(
            method,
            path,
            headers={
                "Authorization": "Bearer " + self.credentials.token,
                "Origin": self.origin,
                "X-PA": "1",
            },
            json=payload,
        )
        if response.status_code != 200:
            raise RequestError(
                "Worker authorization or generation check failed.", status=response.status_code
            )
        return response.content

    monkeypatch.setattr(WorkerClient, "_read_response", through_actual_http)
    try:
        registered = site.client.post(
            "/api/v4/agents", headers=site.write, json={"name": "Codex fixture", "kind": "codex"}
        ).json()
        agent_id = registered["agent"]["id"]
        task = create_task(site, [agent_id])
        approve(site, task)
        home = tmp_path / "worker"
        setup_credentials(
            home,
            url=site.origin,
            agent_id=agent_id,
            space_id="personal-owner",
            token_value=registered["worker_token"],
        )
        source = tmp_path / "project"
        source.mkdir()
        wire = tmp_path / "wire.jsonl"
        native = CodexProvider(
            (
                sys.executable,
                str(Path(__file__).parent / "fixtures" / "native_protocol_agent.py"),
                "codex_eof",
                str(wire),
            )
        )
        host = AgentHost(
            WorkerBridge(home), native, provider_name="codex", cwd=source, allow_direct_workdir=True
        )
        with pytest.raises(BridgeError, match="unknown"):
            await host.run_once()
        saved = host.journal_path.read_bytes()
        attempt_id = json.loads(saved)["attempt_id"]
        rotated = site.client.post(f"/api/v4/agents/{agent_id}/token", headers=site.write).json()
        updated = await asyncio.to_thread(
            update_credentials, home, token_value=rotated["worker_token"]
        )
        assert updated["updated"]
        assert host.journal_path.read_bytes() == saved
        assert registered["worker_token"] not in (home / "agent.json").read_text()
        restored_bridge = WorkerBridge(home)
        with pytest.raises(RequestError) as old_status:
            await asyncio.to_thread(restored_bridge.status, attempt_id)
        assert old_status.value.status == 409
        minimal = await asyncio.to_thread(restored_bridge.stop_receipt, attempt_id)
        assert not {"payload", "lease_token", "result"} & minimal.keys()
        restored = AgentHost(
            restored_bridge, native, provider_name="codex", cwd=source, allow_direct_workdir=True
        )
        with pytest.raises(BridgeError, match="owner"):
            await restored.reconcile_local()
        stopped = site.client.post(
            f"/api/v4/tasks/{task['id']}/stopped",
            headers=site.write,
            json={
                "step_id": "step0",
                "attempt_id": attempt_id,
                "note": "Fixture process is verified stopped.",
            },
        )
        assert stopped.status_code == 200, stopped.text
        assert (await restored.reconcile_local())["reconciled"]
        assert wire.read_text().count('"method": "turn/start"') == 1
    finally:
        site.client.close()
