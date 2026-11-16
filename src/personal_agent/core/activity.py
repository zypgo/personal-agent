"""A bounded, owner-facing view of durable work, including retries between runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from personal_agent.db import loads
from personal_agent.security.sanitize import strip_invisible

if TYPE_CHECKING:
    from personal_agent.app import App


def activity_snapshot(app: App, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    counts = {
        row["status"]: row["n"]
        for row in app.db.query(
            "SELECT status, COUNT(*) n FROM events WHERE status IN ('pending','leased','dead') GROUP BY status"
        )
    }
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for run in app.runs.recent(limit * 3):
        ids = run.event_ids[:200]
        if ids and all(event_id in seen for event_id in ids):
            continue
        seen.update(ids)
        events = (
            app.db.query(
                f"SELECT * FROM events WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id", ids
            )
            if ids
            else []
        )
        pending = [event for event in events if event["status"] == "pending"]
        state = run.status
        retry_at = None
        if state in {"failed", "cancelled", "paused_budget"} and pending:
            state = "paused_budget" if run.status == "paused_budget" else "retrying"
            retry_at = min(event["available_at"] for event in pending)
        elif state == "failed" and events and all(event["status"] == "dead" for event in events):
            state = "stopped"
        payload = loads(events[0]["payload"], {}) if events else {}
        title = (
            payload.get("text")
            or payload.get("name")
            or payload.get("prompt")
            or run.trigger.replace("_", " ")
        )
        items.append(
            {
                "id": run.id,
                "state": state,
                "title": _title(title),
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "next_retry_at": retry_at,
                "attempts": max((event["attempts"] for event in events), default=1),
            }
        )
        if len(items) >= limit:
            break
    # New messages are visible before their first run, including while another run waits.
    queued = app.db.query(
        "SELECT id,payload,created_at,available_at,attempts FROM events WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    for row in reversed(queued):
        if row["id"] in seen:
            continue
        payload = loads(row["payload"], {})
        items.insert(
            0,
            {
                "id": f"event:{row['id']}",
                "state": "queued",
                "title": _title(payload.get("text") or payload.get("name") or "Queued task"),
                "started_at": row["created_at"],
                "finished_at": None,
                "next_retry_at": row["available_at"]
                if row["available_at"] > app.clock.now()
                else None,
                "attempts": row["attempts"],
            },
        )
    return {
        "queue": {name: counts.get(name, 0) for name in ("pending", "leased", "dead")},
        "items": items[:limit],
    }


def _title(value: Any) -> str:
    return " ".join(strip_invisible(str(value)).split())[:140]
