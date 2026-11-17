"""Publish a generated workspace file through the existing, durable delivery boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from agents import RunContextWrapper, function_tool

from personal_agent.core.artifacts import (
    ArtifactError,
    artifact_source_parts,
    validate_artifact_metadata,
)
from personal_agent.harness.runctx import RunContext
from personal_agent.tools.base import FULL, SCHEDULED, Category, ToolDef, ToolRegistry, ToolResult


@function_tool(
    name_override="publish_artifact",
    description_override=(
        "Deliver one finished workspace file to the owner. Takes an immutable private snapshot "
        "and queues one file notification; delivery is confirmed by the system later. Web offers "
        "an authenticated download; Telegram sends a document. path must stay within the "
        "workspace and use no symlinks. name optionally changes the download filename. "
        "description is a caption of at most 500 characters. Publishing the same path in the "
        "same task reuses its first snapshot; use a new path for a corrected revision."
    ),
)
async def publish_artifact_tool(
    ctx: RunContextWrapper[RunContext],
    path: str,
    name: str | None = None,
    description: str = "",
) -> str:
    rc = ctx.context
    try:
        publication = await asyncio.to_thread(
            rc.app.artifacts.publish,
            path,
            session_key=rc.session_key,
            work_id=rc.work_id,
            run_id=rc.run_id,
            channel=rc.origin.channel,
            recipient=rc.origin.recipient,
            name=name,
            description=description,
            web_enabled=rc.app.config.web.enabled,
            interactive=rc.origin.interactive,
        )
    except ArtifactError as exc:
        return ToolResult(f"Error: {exc}", error=True)
    artifact = publication.artifact
    if artifact.outbox_id is not None and artifact.outbox_id not in rc.queued_messages:
        rc.queued_messages.append(artifact.outbox_id)
    if publication.reused:
        if artifact.expires_at <= rc.app.clock.now():
            return (
                f"Artifact {artifact.id} was already published but has expired. "
                "Use a new source path for a fresh revision; no duplicate notification was queued."
            )
        return (
            f"Artifact {artifact.id} ({artifact.name}) was already published for this task. "
            "The original snapshot and notification are reused; no duplicate was queued."
        )
    return (
        f"Artifact {artifact.id}: {artifact.name}, {artifact.size} bytes, SHA-256 {artifact.sha256}. "
        f"Queued as message {artifact.outbox_id}; do not claim delivery until it is confirmed."
    )


def _canonical_path(rc: RunContext, args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str):
        raise ArtifactError("Give a path inside the workspace.")
    return "/".join(artifact_source_parts(rc.app.paths.workspace_dir, path))


def _path_views(rc: RunContext, args: dict[str, Any]) -> tuple[str, str]:
    canonical = _canonical_path(rc, args)
    return canonical, str(rc.app.paths.workspace_dir / canonical)


def _publication_floor(rc: RunContext, args: dict[str, Any]) -> str | None:
    """Check syntax and outgoing metadata without opening or inspecting any source file."""
    try:
        canonical = _canonical_path(rc, args)
        name = args.get("name")
        description = args.get("description", "")
        if (name is not None and not isinstance(name, str)) or not isinstance(description, str):
            return "Artifact name and description must be text."
        validate_artifact_metadata(
            name if name is not None else canonical.rsplit("/", 1)[-1], description
        )
    except ArtifactError as exc:
        return str(exc)
    return None


def register_artifacts(registry: ToolRegistry) -> None:
    registry.register(
        ToolDef(
            name="publish_artifact",
            category=Category.NET_WRITE,
            tool=publish_artifact_tool,
            profiles=frozenset({FULL, SCHEDULED}),
            detail=lambda args: str(args.get("path") or ""),
            detail_for=_canonical_path,
            views=lambda args: (str(args.get("path") or ""),),
            views_for=_path_views,
            floor=_publication_floor,
            review=lambda args: (
                f"Publish workspace file {args.get('path', '')} to the owner. "
                f"Filename: {args.get('name') or '(source filename)'}. "
                f"Caption: {args.get('description') or '(file summary)'}"
            ),
            available=lambda app: (
                None if app.config.artifacts.enabled else "artifact publication is disabled"
            ),
        )
    )
