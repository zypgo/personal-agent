"""File export policy sees the same paths and metadata that publication actually uses."""

from __future__ import annotations

import json

import pytest
from agents.tool_context import ToolContext

from personal_agent.config import Config, PolicyConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.core.artifacts import ArtifactError
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.security.policy import Verdict
from personal_agent.tools.base import FULL
from personal_agent.tools.guard import ToolGuard, summarize
from test_artifacts import publish, source


def context(app):
    run_id = app.runs.start(
        session_key="main", trigger="message", profile=FULL, model="test", event_ids=[1]
    )
    return RunContext(app, run_id, "main", FULL, Origin("telegram", recipient="7"))


def wrapper(rc, args):
    return ToolContext(
        context=rc,
        tool_name="publish_artifact",
        tool_call_id="call1",
        tool_arguments=json.dumps(args),
    )


def no_snapshot(*_args, **_kwargs):
    pytest.fail("authorization must finish before a file is read or snapshotted")


@pytest.mark.parametrize("raw_kind", ["relative", "dot", "slashes", "absolute", "absolute_dot"])
@pytest.mark.parametrize("rule_kind", ["relative", "absolute"])
@pytest.mark.parametrize("policy_kind", ["deny", "always_ask"])
async def test_equivalent_paths_cannot_bypass_restrictive_rules(
    make_app, monkeypatch, raw_kind, rule_kind, policy_kind
):
    # The home is selected by the fixture; use that actual workspace in absolute rules.
    initial = make_app()
    workspace = initial.paths.workspace_dir
    pattern = "private/*" if rule_kind == "relative" else str(workspace / "private/*")
    cfg = Config(
        policy=PolicyConfig(
            **{policy_kind: [f"publish_artifact:{pattern}"], "allow": ["publish_artifact"]}
        )
    )
    app = make_app(config=cfg)
    source(app, "private/report.txt")
    raw = {
        "relative": "private/report.txt",
        "dot": "./private/report.txt",
        "slashes": "private//./report.txt",
        "absolute": str(workspace / "private/report.txt"),
        "absolute_dot": f"{workspace}/./private//report.txt",
    }[raw_kind]
    args = {"path": raw, "name": None, "description": "Requested file."}
    monkeypatch.setattr(app.artifacts, "_snapshot", no_snapshot)
    rc, guard = context(app), ToolGuard(app)
    tool = app.tools.get("publish_artifact")
    decision = await guard.decide(tool, args, rc, "call1")
    assert decision.verdict is (Verdict.DENY if policy_kind == "deny" else Verdict.ASK)
    assert decision.layer == ("deny_rule" if policy_kind == "deny" else "always_ask")
    output = await guard.wrap(tool).on_invoke_tool(wrapper(rc, args), json.dumps(args))
    assert output.startswith("Denied:")
    assert args["path"] == raw  # normalization is a view, not a mutation of model arguments
    assert app.audit.recent()[0].detail == "private/report.txt"
    assert app.db.scalar("SELECT COUNT(*) FROM artifacts") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


async def test_the_raw_spelling_remains_an_additional_restrictive_view(make_app, monkeypatch):
    app = make_app(
        config=Config(
            policy=PolicyConfig(deny=["publish_artifact:./private/*"], allow=["publish_artifact"])
        )
    )
    monkeypatch.setattr(app.artifacts, "_snapshot", no_snapshot)
    rc = context(app)
    args = {"path": "./private/report.txt"}
    decision = await ToolGuard(app).decide(app.tools.get("publish_artifact"), args, rc, "call1")
    assert decision.verdict is Verdict.DENY and decision.layer == "deny_rule"


async def test_canonical_relative_allow_rules_cover_absolute_requests(make_app):
    app = make_app(config=Config(policy=PolicyConfig(allow=["publish_artifact:public/*"])))
    path = source(app, "public/report.txt")
    rc = context(app)
    args = {"path": str(path), "name": None, "description": "Requested file."}
    tool = app.tools.get("publish_artifact")
    output = await ToolGuard(app).wrap(tool).on_invoke_tool(wrapper(rc, args), json.dumps(args))
    assert "Queued" in output
    assert app.db.row("SELECT source_path FROM artifacts")[0] == "public/report.txt"


@pytest.mark.parametrize("path", ["../private.txt", "/etc/passwd", "", "."])
async def test_invalid_path_is_refused_before_approval_or_file_access(make_app, monkeypatch, path):
    app = make_app(config=Config(policy=PolicyConfig(allow=["publish_artifact"])))
    monkeypatch.setattr(app.artifacts, "_snapshot", no_snapshot)
    rc = context(app)
    decision = await ToolGuard(app).decide(
        app.tools.get("publish_artifact"), {"path": path}, rc, "call1"
    )
    assert decision.verdict is Verdict.DENY and decision.layer == "floor"


@pytest.mark.parametrize("field", ["name", "description", "source_name"])
async def test_credentials_redacted_in_review_cannot_leave_as_artifact_metadata(
    make_app, monkeypatch, field
):
    secret = "fictitiousReviewCredentialXYZ"
    monkeypatch.setenv("PA_REVIEW_KEY", secret)
    app = make_app(config=Config(policy=PolicyConfig(allow=["publish_artifact"])))
    args = {"path": "report.txt", "name": None, "description": "Requested file."}
    if field == "source_name":
        args["path"] = f"{secret}.txt"
    else:
        args[field] = f"report-{secret}.txt" if field == "name" else f"The key is {secret}."
    tool = app.tools.get("publish_artifact")
    summary = summarize(tool, args)
    assert secret not in summary and "[REDACTED]" in summary
    monkeypatch.setattr(app.artifacts, "_snapshot", no_snapshot)
    rc = context(app)
    # Even an approval persisted by a previous version cannot bypass the publication floor.
    [approval] = app.approvals.create(rc.run_id, "main", [PendingCall("call1", tool.name, summary)])
    app.approvals.decide(approval.id, True, by="owner:test")
    output = await ToolGuard(app).wrap(tool).on_invoke_tool(wrapper(rc, args), json.dumps(args))
    assert output.startswith("Denied:") and "credentials" in output and secret not in output
    assert app.db.scalar("SELECT COUNT(*) FROM artifacts") == 0
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


@pytest.mark.parametrize(
    "known,metadata",
    [
        ("fictitiousReviewCredentialXYZ", "fictitious\u202eReviewCredentialXYZ"),
        ("fictitious\u202eReviewCredentialXYZ", "fictitiousReviewCredentialXYZ"),
        ("fictitious\r\nReviewCredentialXYZ", "fictitious\nReviewCredentialXYZ"),
        ("unrelatedReviewCredentialXYZ", "ghp_" + "A" * 40),
        ("unrelatedReviewCredentialXYZ", "Bearer " + "X" * 32),
    ],
)
def test_store_rejects_raw_normalized_and_credential_looking_captions(
    make_app, monkeypatch, known, metadata
):
    monkeypatch.setenv("PA_REVIEW_KEY", known)
    app = make_app()
    monkeypatch.setattr(app.artifacts, "_snapshot", no_snapshot)
    with pytest.raises(ArtifactError, match="credentials") as caught:
        publish(app, description=metadata)
    assert known not in str(caught.value) and metadata not in str(caught.value)
    assert app.db.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_safe_metadata_is_display_normalized_but_file_bytes_are_not_scanned(make_app, monkeypatch):
    secret = "fictitiousReviewCredentialXYZ"
    monkeypatch.setenv("PA_REVIEW_KEY", secret)
    app = make_app()
    source(app, content=secret.encode())
    artifact = publish(app, name="report.txt", description="The\u202e requested report.").artifact
    message = app.outbox.get(artifact.outbox_id)
    assert message.text == "The requested report."
    assert app.artifacts.read(artifact.id)[1] == secret.encode()
    summary = summarize(
        app.tools.get("publish_artifact"),
        {"path": "report.txt", "name": "report.txt", "description": "The\u202e requested report."},
    )
    assert message.text in summary and artifact.name in summary
