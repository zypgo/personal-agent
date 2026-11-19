"""Per-space SQLite orchestration tables. All transitions use BEGIN IMMEDIATE."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE orchestration_agents (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
            allowed_actor_ids TEXT NOT NULL, shared_environment INTEGER NOT NULL,
            capabilities TEXT NOT NULL, observed_capabilities TEXT NOT NULL DEFAULT '[]',
            availability TEXT NOT NULL, secret_env TEXT NOT NULL, wake_mode TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active', generation INTEGER NOT NULL DEFAULT 1,
            token_hash TEXT NOT NULL UNIQUE, owner_authorization TEXT NOT NULL,
            runtime_version TEXT, last_seen_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""",
        """CREATE TABLE orchestration_tasks (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, request_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, title TEXT NOT NULL, visibility TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, plan_hash TEXT NOT NULL, plan TEXT NOT NULL,
            authorization TEXT NOT NULL, state TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'run', stop_reason TEXT,
            approved_by TEXT, approver_authorization TEXT, approved_at REAL,
            deadline_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(actor_id, request_id)
        )""",
        """CREATE TABLE orchestration_steps (
            task_id TEXT NOT NULL REFERENCES orchestration_tasks(id), id TEXT NOT NULL,
            position INTEGER NOT NULL, agent_id TEXT NOT NULL REFERENCES orchestration_agents(id),
            agent_generation INTEGER NOT NULL, prompt TEXT NOT NULL, acceptance TEXT NOT NULL,
            depends_on TEXT NOT NULL, required INTEGER NOT NULL, state TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0, current_attempt TEXT,
            accepted_by TEXT, accepted_at REAL, rejection_reason TEXT,
            PRIMARY KEY(task_id, id)
        )""",
        """CREATE TABLE orchestration_attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES orchestration_tasks(id),
            step_id TEXT NOT NULL, agent_id TEXT NOT NULL REFERENCES orchestration_agents(id),
            agent_generation INTEGER NOT NULL, worker_id TEXT NOT NULL,
            generation INTEGER NOT NULL, lease_token TEXT NOT NULL UNIQUE, lease_expires_at REAL NOT NULL,
            state TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'run',
            result TEXT, result_hash TEXT, result_received_at REAL, result_eligible INTEGER NOT NULL DEFAULT 0,
            last_sequence INTEGER NOT NULL DEFAULT 0, claimed_at REAL NOT NULL,
            last_seen_at REAL NOT NULL, finished_at REAL, stopped_by TEXT, stopped_at REAL, stop_note TEXT,
            UNIQUE(agent_id, worker_id), UNIQUE(task_id, step_id, generation),
            FOREIGN KEY(task_id, step_id) REFERENCES orchestration_steps(task_id, id)
        )""",
        """CREATE TABLE orchestration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES orchestration_tasks(id),
            step_id TEXT, attempt_id TEXT, event_type TEXT NOT NULL, data TEXT NOT NULL,
            worker_sequence INTEGER, event_hash TEXT, created_at REAL NOT NULL,
            UNIQUE(attempt_id, worker_sequence)
        )""",
        """CREATE TABLE orchestration_questions (
            task_id TEXT NOT NULL REFERENCES orchestration_tasks(id), id TEXT NOT NULL,
            step_id TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES orchestration_attempts(id),
            text TEXT NOT NULL, choices TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
            answer TEXT, answered_by TEXT, created_at REAL NOT NULL, answered_at REAL,
            PRIMARY KEY(task_id, id)
        )""",
        """CREATE TABLE orchestration_wakes (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES orchestration_agents(id),
            agent_generation INTEGER NOT NULL, task_id TEXT NOT NULL REFERENCES orchestration_tasks(id),
            step_id TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', http_status INTEGER,
            reserved_at REAL NOT NULL, expires_at REAL NOT NULL, finished_at REAL, claimed_at REAL, attempt_id TEXT,
            stopped_by TEXT, stopped_at REAL, stop_note TEXT,
            UNIQUE(task_id, step_id, agent_generation),
            FOREIGN KEY(task_id, step_id) REFERENCES orchestration_steps(task_id, id)
        )""",
        "CREATE INDEX orchestration_tasks_actor ON orchestration_tasks(actor_id, created_at)",
        "CREATE INDEX orchestration_steps_agent ON orchestration_steps(agent_id, state)",
        "CREATE INDEX orchestration_attempts_lease ON orchestration_attempts(state, lease_expires_at)",
        "CREATE INDEX orchestration_events_task ON orchestration_events(task_id, id)",
        "CREATE INDEX orchestration_wakes_state ON orchestration_wakes(state, expires_at)",
    ]
    for statement in statements:
        conn.execute(statement)
