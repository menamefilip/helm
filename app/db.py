from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_PROJECTS = [
    ("Helm", "helm", "Sideprojekt", str(Path.home() / "ObsidianVault" / "helm" / "dev"), "sideproject"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _rename_legacy_bob_workspace_project(conn: sqlite3.Connection) -> None:
    legacy = conn.execute("SELECT id FROM projects WHERE slug = 'bob-workspace'").fetchone()
    if legacy is None:
        return
    helm = conn.execute("SELECT id FROM projects WHERE slug = 'helm'").fetchone()
    if helm is not None:
        conn.execute("UPDATE projects SET name = 'Helm Legacy', slug = 'bob-workspace-legacy' WHERE id = ?", (legacy["id"],))
        return
    conn.execute("UPDATE projects SET name = 'Helm', slug = 'helm' WHERE id = ?", (legacy["id"],))


def _delete_project_if_seed_empty(conn: sqlite3.Connection, slug: str) -> None:
    project = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
    if project is None:
        return
    project_id = project["id"]
    user_counts = [
        conn.execute(
            "SELECT COUNT(*) AS c FROM messages JOIN chats ON chats.id = messages.chat_id WHERE chats.project_id = ?",
            (project_id,),
        ).fetchone()["c"],
        conn.execute(
            "SELECT COUNT(*) AS c FROM runs JOIN chats ON chats.id = runs.chat_id WHERE chats.project_id = ?",
            (project_id,),
        ).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM project_goals WHERE project_id = ?", (project_id,)).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM project_tasks WHERE project_id = ?", (project_id,)).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM project_notes WHERE project_id = ?", (project_id,)).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM project_files WHERE project_id = ?", (project_id,)).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM project_stage_defaults WHERE project_id = ?", (project_id,)).fetchone()["c"],
        conn.execute("SELECT COUNT(*) AS c FROM everyday_items WHERE project_id = ?", (project_id,)).fetchone()["c"],
    ]
    if any(user_counts):
        return
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                root_path TEXT NOT NULL DEFAULT '',
                mode_default TEXT NOT NULL DEFAULT 'work',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'work',
                hermes_session_id TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                model TEXT,
                provider TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                error TEXT,
                artifact_path TEXT NOT NULL DEFAULT '',
                current_stage TEXT NOT NULL DEFAULT 'input',
                stage_status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            CREATE TABLE IF NOT EXISTS project_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                vision_id INTEGER REFERENCES goal_visions(id) ON DELETE SET NULL,
                parent_goal_id INTEGER REFERENCES project_goals(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                horizon TEXT NOT NULL DEFAULT 'outcome',
                status TEXT NOT NULL DEFAULT 'active',
                target_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS goal_visions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS project_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                priority INTEGER NOT NULL DEFAULT 2,
                due_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS project_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Note',
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS project_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'file',
                pinned INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, path)
            );

            CREATE TABLE IF NOT EXISTS project_stage_defaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                instructions TEXT NOT NULL DEFAULT '',
                artifact_template TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, stage)
            );

            CREATE TABLE IF NOT EXISTS everyday_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'task',
                bucket TEXT NOT NULL DEFAULT 'inbox',
                status TEXT NOT NULL DEFAULT 'open',
                priority INTEGER NOT NULL DEFAULT 2,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                goal_id INTEGER REFERENCES project_goals(id) ON DELETE SET NULL,
                due_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                focus TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                daily_plan_id INTEGER NOT NULL REFERENCES daily_plans(id) ON DELETE CASCADE,
                everyday_item_id INTEGER REFERENCES everyday_items(id) ON DELETE SET NULL,
                goal_id INTEGER REFERENCES project_goals(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL DEFAULT 'global',
                kind TEXT NOT NULL DEFAULT 'fact',
                content TEXT NOT NULL,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                chat_id INTEGER REFERENCES chats(id) ON DELETE SET NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                confidence INTEGER NOT NULL DEFAULT 3,
                status TEXT NOT NULL DEFAULT 'active',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_memory_items_scope ON memory_items(scope);
            CREATE INDEX IF NOT EXISTS idx_memory_items_kind ON memory_items(kind);
            CREATE INDEX IF NOT EXISTS idx_memory_items_project_id ON memory_items(project_id);
            CREATE INDEX IF NOT EXISTS idx_memory_items_chat_id ON memory_items(chat_id);
            CREATE INDEX IF NOT EXISTS idx_memory_items_status ON memory_items(status);

            CREATE TABLE IF NOT EXISTS team_workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                chat_id INTEGER REFERENCES chats(id) ON DELETE SET NULL,
                run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                current_role TEXT NOT NULL DEFAULT 'lead',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_team_workflows_project_id ON team_workflows(project_id);
            CREATE INDEX IF NOT EXISTS idx_team_workflows_chat_id ON team_workflows(chat_id);
            CREATE INDEX IF NOT EXISTS idx_team_workflows_run_id ON team_workflows(run_id);
            CREATE INDEX IF NOT EXISTS idx_team_workflows_status ON team_workflows(status);

            CREATE TABLE IF NOT EXISTS team_workflow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER NOT NULL REFERENCES team_workflows(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                notes TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                agent_goal TEXT NOT NULL DEFAULT '',
                agent_status TEXT NOT NULL DEFAULT 'queued',
                self_report TEXT NOT NULL DEFAULT '',
                verified_evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_team_workflow_steps_workflow_id ON team_workflow_steps(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_team_workflow_steps_status ON team_workflow_steps(status);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );


            CREATE TABLE IF NOT EXISTS telegram_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'telegram',
                external_chat_id TEXT NOT NULL DEFAULT '',
                external_thread_id TEXT NOT NULL DEFAULT '',
                external_message_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                sender_username TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                chat_id INTEGER REFERENCES chats(id) ON DELETE SET NULL,
                everyday_item_id INTEGER REFERENCES everyday_items(id) ON DELETE SET NULL,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_telegram_inbox_status ON telegram_inbox(status);
            CREATE INDEX IF NOT EXISTS idx_telegram_inbox_chat_id ON telegram_inbox(chat_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_inbox_external_message
            ON telegram_inbox(external_chat_id, external_message_id)
            WHERE external_chat_id != '' AND external_message_id != '';

            CREATE TABLE IF NOT EXISTS telegram_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'message',
                status TEXT NOT NULL DEFAULT 'draft',
                target_label TEXT NOT NULL DEFAULT '',
                external_chat_id TEXT NOT NULL DEFAULT '',
                external_thread_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                chat_id INTEGER REFERENCES chats(id) ON DELETE SET NULL,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_telegram_outbox_status ON telegram_outbox(status);
            CREATE INDEX IF NOT EXISTS idx_telegram_outbox_run_id ON telegram_outbox(run_id);
            CREATE INDEX IF NOT EXISTS idx_telegram_outbox_chat_id ON telegram_outbox(chat_id);

            CREATE TABLE IF NOT EXISTS project_run_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                summary TEXT NOT NULL DEFAULT '',
                changes TEXT NOT NULL DEFAULT '',
                tests TEXT NOT NULL DEFAULT '',
                next_actions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_project_run_summaries_run_id ON project_run_summaries(run_id);
            CREATE INDEX IF NOT EXISTS idx_project_run_summaries_project_id ON project_run_summaries(project_id);
            """
        )
        _ensure_column(conn, "runs", "artifact_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "current_stage", "TEXT NOT NULL DEFAULT 'input'")
        _ensure_column(conn, "runs", "stage_status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(conn, "project_goals", "vision_id", "INTEGER REFERENCES goal_visions(id) ON DELETE SET NULL")
        _ensure_column(conn, "project_goals", "parent_goal_id", "INTEGER REFERENCES project_goals(id) ON DELETE SET NULL")
        _ensure_column(conn, "project_goals", "horizon", "TEXT NOT NULL DEFAULT 'outcome'")
        _ensure_column(conn, "everyday_items", "goal_id", "INTEGER REFERENCES project_goals(id) ON DELETE SET NULL")
        _ensure_column(conn, "team_workflow_steps", "agent_goal", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "team_workflow_steps", "agent_status", "TEXT NOT NULL DEFAULT 'queued'")
        _ensure_column(conn, "team_workflow_steps", "self_report", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "team_workflow_steps", "verified_evidence", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "team_workflow_steps", "spawn_command", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "team_workflow_steps", "spawn_status", "TEXT NOT NULL DEFAULT 'none'")
        _ensure_column(conn, "team_workflow_steps", "spawn_pid", "INTEGER")
        _ensure_column(conn, "runs", "waiting_for_tokens", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "needs_attention", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "attention_reason", "TEXT NOT NULL DEFAULT ''")
        # M20: git HEAD at run creation, so a run's diff can be scoped to what the
        # run actually changed. NULL means "no baseline captured" (legacy runs) and
        # must never be reported as a file list -- see _auto_run_summary.
        _ensure_column(conn, "runs", "base_commit", "TEXT")
        _ensure_column(conn, "runs", "token_refresh_at", "TEXT")
        _ensure_column(conn, "runs", "token_refresh_seconds", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES ('safety_policy_enabled', '1')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES ('theme_id', 'helm-default')")
        for key, value in {
            'telegram_auto_sync_enabled': '1',
            'telegram_read_allowed': '1',
            'telegram_auto_send_allowed': '0',
            'telegram_sync_interval_seconds': '300',
            'telegram_last_sync_at': '',
            'telegram_last_sync_status': 'idle',
            'telegram_last_sync_error': '',
            'telegram_last_source': 'hermes_state_db',
        }.items():
            conn.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (key, value))
        _rename_legacy_bob_workspace_project(conn)
        for mock_slug in ("esp32-pentest", "school-tutor-ai", "osint-lab"):
            _delete_project_if_seed_empty(conn, mock_slug)
        count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
        if count == 0:
            for name, slug, description, root_path, mode in DEFAULT_PROJECTS:
                cur = conn.execute(
                    "INSERT INTO projects(name, slug, description, root_path, mode_default) VALUES (?, ?, ?, ?, ?)",
                    (name, slug, description, root_path, mode),
                )
                conn.execute(
                    "INSERT INTO chats(project_id, title, mode) VALUES (?, ?, ?)",
                    (cur.lastrowid, "Main", mode),
                )


# ── project_run_summaries helpers ─────────────────────────────────────────


def create_or_update_run_summary(
    conn: sqlite3.Connection,
    run_id: int,
    project_id: int,
    summary: str,
    changes: str = "",
    tests: str = "",
    next_actions: str = "",
) -> tuple[dict[str, Any], bool]:
    """Create or update a run summary. Returns (row_dict, was_update)."""
    existing = conn.execute(
        "SELECT id FROM project_run_summaries WHERE run_id = ?", (run_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE project_run_summaries
               SET summary = ?, changes = ?, tests = ?, next_actions = ?, updated_at = CURRENT_TIMESTAMP
               WHERE run_id = ?""",
            (summary, changes, tests, next_actions, run_id),
        )
        return row_to_dict(
            conn.execute("SELECT * FROM project_run_summaries WHERE run_id = ?", (run_id,)).fetchone()
        ), True
    conn.execute(
        """INSERT INTO project_run_summaries(run_id, project_id, summary, changes, tests, next_actions)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, project_id, summary, changes, tests, next_actions),
    )
    return row_to_dict(
        conn.execute("SELECT * FROM project_run_summaries WHERE run_id = ?", (run_id,)).fetchone()
    ), False


def get_run_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM project_run_summaries WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else row_to_dict(row)


def list_run_summaries_by_project(conn: sqlite3.Connection, project_id: int, limit: int = 20) -> list[dict[str, Any]]:
    return [
        row_to_dict(row)
        for row in conn.execute(
            """SELECT * FROM project_run_summaries
               WHERE project_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (project_id, limit),
        )
    ]
