"""Authenticated, attachment-only artifact downloads. Mount behind the existing Web Guard."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from personal_agent.core.artifacts import ArtifactError, ArtifactExpired

if TYPE_CHECKING:
    from personal_agent.app import App


def artifact_routes(app: App) -> list[Route]:
    async def download(request: Request) -> Response:
        # Check independently: the outer Guard checks hosts/CSRF, not the session cookie.
        if app.web_auth.lookup(request.cookies.get("pa_session", "")) is None:
            return JSONResponse(
                {"error": "Not signed in."}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        try:
            artifact, data = await asyncio.to_thread(
                app.artifacts.read, request.path_params["artifact_id"]
            )
        except ArtifactError as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=410 if isinstance(exc, ArtifactExpired) else 404,
                headers={"Cache-Control": "no-store"},
            )
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", artifact.name)
        return Response(
            data,
            # Even HTML, SVG and PDF are downloads, never executable same-origin documents.
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{fallback}"; '
                    f"filename*=UTF-8''{quote(artifact.name, safe='')}"
                ),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    return [Route("/api/artifacts/{artifact_id}", download, methods=["GET"])]
