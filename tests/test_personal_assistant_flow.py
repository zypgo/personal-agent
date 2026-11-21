"""A real conversation can prepare a user-reviewed operation and observe its result."""

from __future__ import annotations

import base64
import hashlib
import json

from agents.testing import ScriptedModel, assistant_message, function_call
from starlette.testclient import TestClient

from personal_agent.channels.ingress import Ingress
from personal_agent.core.worker import Worker
from test_agent_orchestration_http import claim, report
from web_fake import Web


async def test_conversation_to_card_to_knowledge_and_back(make_app):
    draft = {"path": "weekend-plan.md", "text": "周六去公园，带上雨伞。", "revision": 0}
    model = ScriptedModel(
        [
            [function_call("workspace_capabilities", {}, call_id="catalog")],
            [
                function_call(
                    "inspect_workspace",
                    {"topic": "knowledge", "resource_id": "", "query": "", "limit": 20},
                    call_id="read",
                )
            ],
            [
                function_call(
                    "prepare_workspace_action",
                    {
                        "action": "write_knowledge",
                        "action_key": "weekend-plan",
                        "target_id": "",
                        "draft_json": json.dumps(draft, ensure_ascii=False),
                    },
                    call_id="propose",
                )
            ],
            [assistant_message("周末计划已整理好，请在卡片里确认保存。")],
            [
                function_call(
                    "inspect_workspace",
                    {
                        "topic": "knowledge",
                        "resource_id": "weekend-plan.md",
                        "query": "",
                        "limit": 20,
                    },
                    call_id="verify",
                )
            ],
            [assistant_message("已保存为周日去公园，记得带伞。")],
        ]
    )
    app = make_app(model)
    web = Web(app)
    web.pair()
    try:
        sent = web.send("把周六去公园、带雨伞这两件事整理成周末计划。")
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "queued"
        result = await Worker(app).run_once()
        assert result.outcome.status == "completed"
        assert app.memory.read_text("/memories/state/knowledge/weekend-plan.md") is None

        cards = web.client.get("/api/assistant/cards").json()["cards"]
        [card] = [item for item in cards if item["kind"] == "workspace_action"]
        assert card["data"]["draft"] == draft
        assert card["status"] == "proposed"
        base = f"/api/assistant/cards/{card['id']}"

        # The person changes the proposed text before the fixed business operation runs.
        approved = {**draft, "text": "周日去公园，带上雨伞。"}
        begun = web.client.post(base + "/begin", headers=web.write, json={"payload": approved})
        assert begun.status_code == 200, begun.text
        receipt = begun.json()
        assert receipt["action"] == "write_knowledge"
        assert app.memory.read_text("/memories/state/knowledge/weekend-plan.md") is None
        saved = web.client.post("/api/v3/knowledge", headers=web.write, json=receipt["payload"])
        assert saved.status_code == 200, saved.text
        finished = web.client.post(
            base + "/complete",
            headers=web.write,
            json={"execution_id": receipt["execution_id"], "outcome": "succeeded"},
        )
        assert finished.status_code == 200, finished.text
        assert not web.client.get("/api/assistant/cards").json()["cards"]
        assert (
            web.client.post(
                base + "/begin", headers=web.write, json={"payload": approved}
            ).status_code
            == 409
        )

        await Ingress(app).owner_message(channel="web", text="周末计划现在是什么？")
        followup = await Worker(app).run_once()
        assert followup.outcome.status == "completed"
        seen = json.dumps(model.calls[-1].input, ensure_ascii=False)
        assert "周日去公园，带上雨伞。" in seen
        assert app.memory.read_text("/memories/state/knowledge/weekend-plan.md") == approved["text"]
    finally:
        web.client.close()


async def test_agent_work_returns_plan_question_file_and_acceptance_cards(make_app):
    app = make_app()
    registered = app.orchestration.create_agent("owner", name="资料助理", kind="personal_agent")
    agent_id = registered["agent"]["id"]
    model = ScriptedModel(
        [
            [function_call("list_agents", {}, call_id="connections")],
            [
                function_call(
                    "delegate_agents",
                    {
                        "task_key": "weekend-checklist",
                        "title": "周末出行清单",
                        "steps": [
                            {
                                "id": "outline",
                                "agent_id": agent_id,
                                "prompt": "按提供的信息整理周末出行清单。",
                                "depends_on": [],
                                "acceptance": "提供清楚的清单和可下载的文件。",
                            }
                        ],
                        "timeout_s": 3600,
                        "visibility": "private",
                    },
                    call_id="plan",
                )
            ],
            [assistant_message("分工已拟好，请查看计划卡片。")],
        ]
    )
    app.model_factory = lambda name: model
    web = Web(app)
    web.pair()
    path = f"/api/v4/agents/{agent_id}/worker"
    try:
        with TestClient(
            web.client.app,
            base_url=web.origin,
            headers={"Authorization": "Bearer " + registered["worker_token"]},
        ) as worker:
            await Ingress(app).owner_message(channel="web", text="请资料助理整理一份周末出行清单。")
            assert (await Worker(app).run_once()).outcome.status == "completed"
            [plan] = [
                item
                for item in web.client.get("/api/assistant/cards").json()["cards"]
                if item["kind"] == "agent_plan"
            ]
            task = plan["data"]["task"]
            assert claim(worker, path) is None
            assert "/?space=personal-owner#assistant-inbox" in json.dumps(model.calls[-1].input)
            approved = web.client.post(
                f"/api/v4/tasks/{task['id']}/approve",
                headers=web.write,
                json={"revision": task["revision"], "plan_hash": task["plan_hash"]},
            )
            assert approved.status_code == 200, approved.text
            attempt = claim(worker, path)
            assert attempt is not None
            question = report(
                worker,
                path,
                attempt,
                "question",
                request_id="format",
                text="希望用哪种格式？",
                choices=["Markdown", "纯文本"],
            )
            assert question.status_code == 200, question.text
            [card] = [
                item
                for item in web.client.get("/api/assistant/cards").json()["cards"]
                if item["kind"] == "agent_question"
            ]
            question_id = card["data"]["question"]["id"]
            answered = web.client.post(
                f"/api/v4/tasks/{task['id']}/questions/{question_id}/answer",
                headers=web.write,
                json={"answer": "Markdown"},
            )
            assert answered.status_code == 200, answered.text
            content = "# 出行清单\n\n- 带伞\n- 备份照片\n".encode()
            reported = report(
                worker,
                path,
                attempt,
                text="已整理出行清单，请核对。",
                artifacts=[
                    {
                        "name": "checklist.md",
                        "mime": "text/markdown",
                        "content_base64": base64.b64encode(content).decode(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            )
            assert reported.status_code == 200, reported.text
            [result] = [
                item
                for item in web.client.get("/api/assistant/cards").json()["cards"]
                if item["kind"] == "agent_result"
            ]
            assert any(action["id"] == "accept" for action in result["actions"])
            assert "worker_token" not in json.dumps(result)
            assert "lease_token" not in json.dumps(result)
            file_id = result["data"]["attempt"]["result"]["artifacts"][0]["id"]
            assert web.client.get(f"/api/v4/artifacts/{file_id}").content == content
            accepted = web.client.post(
                f"/api/v4/tasks/{task['id']}/accept",
                headers=web.write,
                json={
                    "step_id": "outline",
                    "attempt_id": attempt["attempt_id"],
                    "revision": task["revision"],
                    "plan_hash": task["plan_hash"],
                },
            )
            assert accepted.status_code == 200, accepted.text
            cards = web.client.get("/api/assistant/cards").json()["cards"]
            assert not any(
                card["kind"] in {"agent_plan", "agent_question", "agent_result"} for card in cards
            )
            assert any(
                card["kind"] == "task_status" and card["status"] == "succeeded" for card in cards
            )
    finally:
        web.client.close()
