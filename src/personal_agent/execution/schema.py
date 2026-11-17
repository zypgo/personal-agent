"""Append-only V3 execution schema; each collaboration space owns its database."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE execution_tasks (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, title TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
            authorization TEXT NOT NULL DEFAULT '{}',
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            session_key TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(actor_id,request_id)
        )""",
        """CREATE TABLE execution_jobs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES execution_tasks(id),
            task_revision INTEGER NOT NULL, step_id TEXT NOT NULL,
            plan TEXT NOT NULL, plan_hash TEXT NOT NULL,
            state TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'run',
            approver_id TEXT NOT NULL, approved_by TEXT, approved_at REAL,
            node_id TEXT, current_attempt TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, deadline_at REAL NOT NULL,
            result TEXT, completion_event_id INTEGER,
            UNIQUE(task_id,task_revision,step_id)
        )""",
        """CREATE TABLE execution_attempts (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES execution_jobs(id),
            generation INTEGER NOT NULL DEFAULT 1, node_id TEXT NOT NULL,
            node_version INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL, resource_id TEXT,
            admitted_at REAL, started_at REAL, last_seen_at REAL NOT NULL,
            finished_at REAL, result_hash TEXT, result TEXT,
            log_sequence INTEGER NOT NULL DEFAULT -1, log_hash TEXT,
            cleanup_state TEXT NOT NULL DEFAULT 'retained',
            UNIQUE(job_id,generation)
        )""",
        """CREATE TABLE execution_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
            generation INTEGER NOT NULL, sequence INTEGER NOT NULL,
            state TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(attempt_id,generation,sequence)
        )""",
        """CREATE TABLE execution_inputs (
            job_id TEXT NOT NULL REFERENCES execution_jobs(id), path TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
            PRIMARY KEY(job_id,path)
        )""",
        """CREATE TABLE execution_questions (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES execution_tasks(id),
            task_revision INTEGER NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            asked_by TEXT NOT NULL, question TEXT NOT NULL, choices TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL,
            answered_by TEXT, answer TEXT, answer_authorization TEXT, answered_at REAL,
            UNIQUE(task_id,task_revision,request_id)
        )""",
        "CREATE INDEX execution_tasks_updated ON execution_tasks(updated_at,id)",
        "CREATE INDEX execution_jobs_ready ON execution_jobs(state,created_at)",
        "CREATE INDEX execution_attempts_node ON execution_attempts(node_id,state)",
        "CREATE INDEX execution_questions_open ON execution_questions(task_id,task_revision,state)",
    ]
    for statement in statements:
        conn.execute(statement)
