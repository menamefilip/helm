from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
import asyncio
from pathlib import Path
from collections.abc import Sequence
from datetime import date as dt_date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("helm")

from .db import connect, init_db, row_to_dict
from .db import create_or_update_run_summary, get_run_summary, list_run_summaries_by_project
from .sse import sse_broadcaster
from .schemas import (
    ArtifactUpdate,
    ChatCreate,
    ChatRunCreate,
    ContextClassifyRequest,
    DailyPlanItemCreate,
    DailyPlanItemUpdate,
    DailyPlanUpdate,
    EverydayItemCreate,
    EverydayItemUpdate,
    GoalVisionCreate,
    GoalVisionUpdate,
    MemoryItemCreate,
    MemoryItemUpdate,
    MessageCreate,
    ProjectCreate,
    ProjectOpen,
    ProjectContextUpdate,
    ProjectFileCreate,
    ProjectGoalCreate,
    ProjectGoalUpdate,
    ProjectNoteCreate,
    ProjectStageDefaultUpsert,
    ProjectTaskCreate,
    ProjectTaskUpdate,
    RerunStageRequest,
    RunCreate,
    RunSummaryCreate,
    RunSummaryResponse,
    RunTokenRefreshResponse,
    RuntimeStartRequest,
    SettingsUpdate,
    StageAdvanceRequest,
    TelegramCaptureRequest,
    TelegramInboxCreate,
    TelegramInboxUpdate,
    TelegramOutboxCreate,
    TelegramOutboxUpdate,
    TelegramRunReportCreate,
    TelegramSyncSettingsUpdate,
    TeamWorkflowCreate,
    TeamWorkflowStepCreate,
    TeamWorkflowStepSpawn,
    TeamWorkflowStepUpdate,
    TeamWorkflowTemplateCreate,
)
from .workspace import (
    TIER_SEQUENCE,
    WRITABLE_ARTIFACTS,
    append_event,
    classify_context_tier,
    bootstrap_workspace,
    create_project_workspace,
    create_run_artifacts,
    list_artifact_versions,
    list_artifacts,
    load_project_map_docs,
    load_retrieved_context_chunks,
    load_symbol_index,
    load_runtime_contracts,
    load_workflow_policies,
    resolve_artifact,
    resolve_artifact_version,
    save_artifact_version,
    slugify,
    workspace_tree,
)

HELM_DATA_HOME = Path(os.getenv("HELM_DATA_HOME", str(Path.home() / ".local" / "share" / "helm")))
DEFAULT_DB = Path(os.getenv("BOB_WORKSPACE_DB", str(HELM_DATA_HOME / "workspace.db")))
DEFAULT_WORKSPACE_ROOT = Path(os.getenv("HELM_WORKSPACE_ROOT", str(HELM_DATA_HOME / "workspace")))
# Use the repository's tracked UI by default. Deployments can override this
# when they need a separately managed UI root.
SHARED_UI_ROOT = Path(
    os.getenv("HELM_SHARED_UI_ROOT", str(Path(__file__).resolve().parents[1] / "shared-ui"))
)
# Set deployment labels through environment variables so instances can be
# distinguished without embedding machine-specific topology in the source.
HELM_ENV = os.getenv("HELM_ENV", "live")
HELM_ENV_LABEL = os.getenv("HELM_ENV_LABEL", "Helm")
SHARED_UI = SHARED_UI_ROOT / "index.html"
SHARED_UI_STYLES = SHARED_UI_ROOT / "styles.css"
SHARED_UI_SCRIPT = SHARED_UI_ROOT / "app.js"
SHARED_UI_ASSETS = SHARED_UI_ROOT / "assets"
STAGE_SEQUENCE = ["input", "plan", "execute", "review", "final_output"]
STAGE_LABELS = {
    "input": "Input",
    "plan": "Plan",
    "execute": "Execute",
    "review": "Review",
    "final_output": "Final output",
}
DEFAULT_RUNTIME_COMMAND: tuple[str, ...] = ()


def _runtime_command(runtime_command: Sequence[str] | str | None = None) -> tuple[str, ...]:
    if runtime_command is None:
        env_command = os.getenv("HELM_RUNTIME_COMMAND", "").strip()
        if env_command:
            return tuple(shlex.split(env_command))
        return DEFAULT_RUNTIME_COMMAND
    if isinstance(runtime_command, str):
        return tuple(shlex.split(runtime_command))
    return tuple(str(part) for part in runtime_command)


def create_app(
    db_path: str | Path = DEFAULT_DB,
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    runtime_command: Sequence[str] | str | None = None,
) -> FastAPI:
    db_path = Path(db_path)
    workspace_root = bootstrap_workspace(workspace_root)
    init_db(db_path)
    app = FastAPI(title="Helm API", version="0.2.0")
    app.state.db_path = db_path
    app.state.workspace_root = workspace_root
    app.state.runtime_command = _runtime_command(runtime_command)
    with connect(app.state.db_path) as conn:
        _normalize_project_roots(conn, app.state.workspace_root)
    if SHARED_UI_ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=SHARED_UI_ASSETS), name="assets")

    def get_conn():
        return connect(app.state.db_path)

    @app.get("/")
    def index():
        if SHARED_UI.exists():
            return FileResponse(SHARED_UI)
        return {"service": "helm-api", "ui": "not_found"}

    @app.get("/styles.css")
    def shared_ui_styles():
        if SHARED_UI_STYLES.exists():
            return FileResponse(SHARED_UI_STYLES, media_type="text/css")
        raise HTTPException(status_code=404, detail="Shared UI stylesheet not found")

    @app.get("/app.js")
    def shared_ui_script():
        if SHARED_UI_SCRIPT.exists():
            return FileResponse(SHARED_UI_SCRIPT, media_type="application/javascript")
        raise HTTPException(status_code=404, detail="Shared UI script not found")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "service": "helm-api",
            "env": HELM_ENV,
            "label": HELM_ENV_LABEL,
        }

    @app.get("/api/events/stream")
    async def events_stream():
        """Server-Sent Events endpoint for real-time push.

        Clients connect here and receive events:
        - run_completed / run_failed / run_needs_attention
        - new_message (new user message detected)
        - heartbeat (keeps connection alive)
        """
        client_id, queue = sse_broadcaster.register_sync()

        async def event_generator():
            try:
                # Initial hello so client knows connection is live
                hello = json.dumps({"client_id": client_id})
                yield f"event: connected\ndata: {hello}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                        event = payload.get("event", "message")
                        data = json.dumps(payload.get("data", {}))
                        yield f"event: {event}\ndata: {data}\n\n"
                    except asyncio.TimeoutError:
                        # Keep-alive comment to prevent proxy timeout
                        yield f": heartbeat\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                sse_broadcaster.unregister_sync(client_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/state")
    def state(mode: str | None = None):
        with get_conn() as conn:
            projects = []
            query = "SELECT * FROM projects"
            params: tuple[str, ...] = ()
            if mode:
                if mode not in {"learning", "work", "sideproject"}:
                    raise HTTPException(status_code=400, detail="Invalid mode")
                query += " WHERE mode_default = ?"
                params = (mode,)
            query += " ORDER BY id"
            for project_row in conn.execute(query, params):
                project = row_to_dict(project_row)
                chats = [
                    row_to_dict(row)
                    for row in conn.execute(
                        "SELECT id, title, mode, archived, created_at, updated_at FROM chats WHERE project_id = ? ORDER BY id",
                        (project["id"],),
                    )
                ]
                project["chats"] = chats
                projects.append(project)
        return {
            "modes": ["learning", "work", "sideproject"],
            "active_project_slug": projects[0]["slug"] if projects else None,
            "projects": projects,
        }

    @app.get("/api/settings")
    def get_settings():
        with get_conn() as conn:
            return _settings_payload(conn)

    @app.patch("/api/settings")
    def update_settings(body: SettingsUpdate):
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="No settings updates provided")
        with get_conn() as conn:
            if body.safety_policy_enabled is not None:
                conn.execute(
                    "INSERT INTO app_settings(key, value, updated_at) VALUES ('safety_policy_enabled', ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    ('1' if body.safety_policy_enabled else '0',),
                )
            if body.theme_id is not None:
                _set_app_setting(conn, "theme_id", body.theme_id)
            return _settings_payload(conn)

    @app.get("/api/chats/{chat_id}/context-meter")
    def chat_context_meter(chat_id: int):
        with get_conn() as conn:
            chat = _require_chat(conn, chat_id)
            project = _require_project(conn, chat["project_id"])
            return _context_meter_payload(conn, chat_id, project)

    @app.get("/api/chats/{chat_id}/context-packet")
    def chat_context_packet(
        chat_id: int,
        max_chars: int = 12000,
        include_recent_messages: bool = True,
        include_runs: bool = True,
        date: str | None = None,
    ):
        packet_date = _validate_iso_date(date) if date else dt_date.today().isoformat()
        with get_conn() as conn:
            chat = _require_chat(conn, chat_id)
            project = _require_project(conn, chat["project_id"])
            return _context_packet_payload(
                conn,
                chat,
                project,
                packet_date=packet_date,
                max_chars=max_chars,
                include_recent_messages=include_recent_messages,
                include_runs=include_runs,
            )

    @app.get("/api/goals/visions")
    def list_goal_visions():
        with get_conn() as conn:
            return {"visions": [row_to_dict(row) for row in conn.execute("SELECT * FROM goal_visions ORDER BY id")]}

    @app.post("/api/goals/visions", status_code=201)
    def create_goal_vision(body: GoalVisionCreate):
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO goal_visions(title, body, status) VALUES (?, ?, ?)",
                (body.title.strip(), body.body, body.status),
            )
            return row_to_dict(conn.execute("SELECT * FROM goal_visions WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/goals/visions/{vision_id}")
    def update_goal_vision(vision_id: int, body: GoalVisionUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        if not fields:
            raise HTTPException(status_code=400, detail="No vision updates provided")
        with get_conn() as conn:
            _require_goal_vision(conn, vision_id)
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE goal_visions SET {assignments} WHERE id = ?", (*fields.values(), vision_id))
            return row_to_dict(conn.execute("SELECT * FROM goal_visions WHERE id = ?", (vision_id,)).fetchone())

    @app.get("/api/daily-plan/{date}")
    def get_daily_plan(date: str):
        date = _validate_iso_date(date)
        with get_conn() as conn:
            plan = _get_or_create_daily_plan(conn, date)
            return _daily_plan_payload(conn, plan)

    @app.put("/api/daily-plan/{date}")
    def upsert_daily_plan(date: str, body: DailyPlanUpdate):
        date = _validate_iso_date(date)
        with get_conn() as conn:
            plan = _get_or_create_daily_plan(conn, date)
            conn.execute(
                "UPDATE daily_plans SET focus = ?, notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (body.focus, body.notes, plan["id"]),
            )
            return row_to_dict(conn.execute("SELECT * FROM daily_plans WHERE id = ?", (plan["id"],)).fetchone())

    @app.post("/api/daily-plan/{date}/items", status_code=201)
    def create_daily_plan_item(date: str, body: DailyPlanItemCreate):
        date = _validate_iso_date(date)
        with get_conn() as conn:
            plan = _get_or_create_daily_plan(conn, date)
            _validate_daily_plan_links(conn, body.goal_id, body.everyday_item_id)
            cur = conn.execute(
                """
                INSERT INTO daily_plan_items(daily_plan_id, everyday_item_id, goal_id, title, status, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (plan["id"], body.everyday_item_id, body.goal_id, body.title.strip(), body.status, body.sort_order),
            )
            return _daily_plan_item_to_dict(conn.execute(_DAILY_PLAN_ITEM_SELECT + " WHERE daily_plan_items.id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/daily-plan/items/{item_id}")
    def update_daily_plan_item(item_id: int, body: DailyPlanItemUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"title", "status", "sort_order"})
        if not fields:
            raise HTTPException(status_code=400, detail="No daily plan item updates provided")
        with get_conn() as conn:
            existing = conn.execute("SELECT id FROM daily_plan_items WHERE id = ?", (item_id,)).fetchone()
            if existing is None:
                raise HTTPException(status_code=404, detail="Daily plan item not found")
            _validate_daily_plan_links(conn, fields.get("goal_id"), fields.get("everyday_item_id"))
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE daily_plan_items SET {assignments} WHERE id = ?", (*fields.values(), item_id))
            return _daily_plan_item_to_dict(conn.execute(_DAILY_PLAN_ITEM_SELECT + " WHERE daily_plan_items.id = ?", (item_id,)).fetchone())

    @app.get("/api/everyday")
    def everyday_workspace():
        with get_conn() as conn:
            return _everyday_payload(conn)

    @app.post("/api/everyday/items", status_code=201)
    def create_everyday_item(body: EverydayItemCreate):
        with get_conn() as conn:
            if body.project_id is not None:
                _require_project(conn, body.project_id)
            if body.goal_id is not None:
                _require_project_goal(conn, body.goal_id)
            cur = conn.execute(
                """
                INSERT INTO everyday_items(title, body, kind, bucket, status, priority, project_id, goal_id, due_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (body.title, body.body, body.kind, body.bucket, body.status, body.priority, body.project_id, body.goal_id, body.due_date),
            )
            return _everyday_item_to_dict(conn.execute(_EVERYDAY_SELECT + " WHERE everyday_items.id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/everyday/items/{item_id}")
    def update_everyday_item(item_id: int, body: EverydayItemUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        if not fields:
            raise HTTPException(status_code=400, detail="No item updates provided")
        with get_conn() as conn:
            existing = conn.execute("SELECT id FROM everyday_items WHERE id = ?", (item_id,)).fetchone()
            if existing is None:
                raise HTTPException(status_code=404, detail="Everyday item not found")
            if "project_id" in fields and fields["project_id"] is not None:
                _require_project(conn, fields["project_id"])
            if "goal_id" in fields and fields["goal_id"] is not None:
                _require_project_goal(conn, fields["goal_id"])
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE everyday_items SET {assignments} WHERE id = ?", (*fields.values(), item_id))
            return _everyday_item_to_dict(conn.execute(_EVERYDAY_SELECT + " WHERE everyday_items.id = ?", (item_id,)).fetchone())


    @app.get("/api/memory")
    def list_memory(project_id: int | None = None, chat_id: int | None = None, kind: str | None = None, status: str = "active"):
        if status not in {"active", "archived", "superseded", "all"}:
            raise HTTPException(status_code=400, detail="Invalid memory status")
        with get_conn() as conn:
            clauses = []
            params: list = []
            if status != "all":
                clauses.append("status = ?")
                params.append(status)
            if project_id is not None:
                _require_project(conn, project_id)
                clauses.append("project_id = ?")
                params.append(project_id)
            if chat_id is not None:
                _require_chat(conn, chat_id)
                clauses.append("chat_id = ?")
                params.append(chat_id)
            if kind is not None:
                clauses.append("kind = ?")
                params.append(kind)
            query = "SELECT * FROM memory_items"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC LIMIT 50"
            return {"items": [_memory_item_to_dict(row) for row in conn.execute(query, tuple(params))]}

    @app.post("/api/memory", status_code=201)
    def create_memory_item(body: MemoryItemCreate):
        with get_conn() as conn:
            _validate_memory_links(conn, body.project_id, body.chat_id)
            cur = conn.execute(
                """
                INSERT INTO memory_items(scope, kind, content, project_id, chat_id, source, confidence, status, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    body.scope,
                    body.kind,
                    body.content.strip(),
                    body.project_id,
                    body.chat_id,
                    body.source,
                    body.confidence,
                    json.dumps(body.metadata or {}, ensure_ascii=False),
                ),
            )
            return _memory_item_to_dict(conn.execute("SELECT * FROM memory_items WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/memory/{memory_id}")
    def update_memory_item(memory_id: int, body: MemoryItemUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"content", "scope", "kind", "source", "confidence", "status"})
        if not fields:
            raise HTTPException(status_code=400, detail="No memory updates provided")
        metadata = fields.pop("metadata", None) if "metadata" in fields else None
        with get_conn() as conn:
            existing = _require_memory_item(conn, memory_id)
            merged_project_id = fields.get("project_id", existing.get("project_id"))
            merged_chat_id = fields.get("chat_id", existing.get("chat_id"))
            if merged_project_id is not None or merged_chat_id is not None:
                _validate_memory_links(conn, merged_project_id, merged_chat_id)
            assignments = []
            values = []
            for key, value in fields.items():
                assignments.append(f"{key} = ?")
                values.append(value.strip() if key == "content" and isinstance(value, str) else value)
            if metadata is not None:
                assignments.append("metadata_json = ?")
                values.append(json.dumps(metadata, ensure_ascii=False))
            assignments.append("updated_at = CURRENT_TIMESTAMP")
            conn.execute(f"UPDATE memory_items SET {', '.join(assignments)} WHERE id = ?", (*values, memory_id))
            return _memory_item_to_dict(conn.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone())



    @app.get("/api/telegram/sync")
    def telegram_sync_snapshot():
        with get_conn() as conn:
            return _telegram_sync_payload(conn)

    @app.patch("/api/telegram/sync/settings")
    def update_telegram_sync_settings(body: TelegramSyncSettingsUpdate):
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(status_code=400, detail="No Telegram sync settings provided")
        with get_conn() as conn:
            if body.auto_sync_enabled is not None:
                _set_app_setting(conn, "telegram_auto_sync_enabled", "1" if body.auto_sync_enabled else "0")
            if body.sync_interval_seconds is not None:
                _set_app_setting(conn, "telegram_sync_interval_seconds", str(body.sync_interval_seconds))
            _set_app_setting(conn, "telegram_read_allowed", "1")
            _set_app_setting(conn, "telegram_auto_send_allowed", "0")
            return _telegram_sync_payload(conn)

    @app.get("/api/telegram/inbox")
    def list_telegram_inbox(status: str | None = None):
        if status and status not in {"new", "triaged", "captured", "routed", "archived"}:
            raise HTTPException(status_code=400, detail="Invalid Telegram inbox status")
        with get_conn() as conn:
            query = "SELECT * FROM telegram_inbox"
            params: tuple[str, ...] = ()
            if status:
                query += " WHERE status = ?"
                params = (status,)
            query += " ORDER BY id DESC LIMIT 50"
            return {"items": [_telegram_inbox_to_dict(row) for row in conn.execute(query, params)]}

    @app.post("/api/telegram/inbox", status_code=201)
    def create_telegram_inbox_item(body: TelegramInboxCreate):
        with get_conn() as conn:
            if body.chat_id is not None:
                _require_chat(conn, body.chat_id)
            metadata = {**(body.metadata or {}), "local_only": True, "dry_run": body.source != "telegram"}
            try:
                cur = conn.execute(
                    """
                    INSERT INTO telegram_inbox(source, external_chat_id, external_thread_id, external_message_id, sender_name, sender_username, content, status, chat_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                    """,
                    (
                        body.source,
                        body.external_chat_id,
                        body.external_thread_id,
                        body.external_message_id,
                        body.sender_name,
                        body.sender_username,
                        body.content,
                        body.chat_id,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if body.external_chat_id and body.external_message_id:
                    raise HTTPException(status_code=409, detail="Telegram inbox message already exists") from exc
                raise
            return _telegram_inbox_to_dict(conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/telegram/inbox/{inbox_id}")
    def update_telegram_inbox_item(inbox_id: int, body: TelegramInboxUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"status"})
        if not fields:
            raise HTTPException(status_code=400, detail="No Telegram inbox updates provided")
        with get_conn() as conn:
            _require_telegram_inbox(conn, inbox_id)
            if "chat_id" in fields and fields["chat_id"] is not None:
                _require_chat(conn, fields["chat_id"])
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE telegram_inbox SET {assignments} WHERE id = ?", (*fields.values(), inbox_id))
            return _telegram_inbox_to_dict(conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,)).fetchone())

    @app.post("/api/telegram/inbox/{inbox_id}/capture", status_code=201)
    def capture_telegram_inbox_item(inbox_id: int, body: TelegramCaptureRequest):
        with get_conn() as conn:
            inbox = _require_telegram_inbox(conn, inbox_id)
            if inbox.get("everyday_item_id"):
                existing = conn.execute(_EVERYDAY_SELECT + " WHERE everyday_items.id = ?", (inbox["everyday_item_id"],)).fetchone()
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content={"inbox_item": _telegram_inbox_to_dict(conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,)).fetchone()), "everyday_item": _everyday_item_to_dict(existing)},
                    )
            if body.project_id is not None:
                _require_project(conn, body.project_id)
            if body.goal_id is not None:
                goal = _require_project_goal(conn, body.goal_id)
                if body.project_id is not None and goal["project_id"] != body.project_id:
                    raise HTTPException(status_code=400, detail="Goal must belong to selected project")
            title = inbox["content"].strip().splitlines()[0][:180]
            cur = conn.execute(
                """
                INSERT INTO everyday_items(title, body, kind, bucket, status, priority, project_id, goal_id)
                VALUES (?, ?, 'note', ?, 'open', 2, ?, ?)
                """,
                (title, inbox["content"], body.bucket, body.project_id, body.goal_id),
            )
            conn.execute(
                "UPDATE telegram_inbox SET status = 'captured', everyday_item_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (cur.lastrowid, inbox_id),
            )
            return {
                "inbox_item": _telegram_inbox_to_dict(conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,)).fetchone()),
                "everyday_item": _everyday_item_to_dict(conn.execute(_EVERYDAY_SELECT + " WHERE everyday_items.id = ?", (cur.lastrowid,)).fetchone()),
            }

    @app.get("/api/telegram/outbox")
    def list_telegram_outbox(status: str | None = None):
        if status and status not in {"draft", "ready", "cancelled", "archived"}:
            raise HTTPException(status_code=400, detail="Invalid Telegram outbox status")
        with get_conn() as conn:
            query = "SELECT * FROM telegram_outbox"
            params: tuple[str, ...] = ()
            if status:
                query += " WHERE status = ?"
                params = (status,)
            query += " ORDER BY id DESC LIMIT 50"
            return {"items": [_telegram_outbox_to_dict(row) for row in conn.execute(query, params)]}

    @app.post("/api/telegram/outbox", status_code=201)
    def create_telegram_outbox_draft(body: TelegramOutboxCreate):
        with get_conn() as conn:
            if body.chat_id is not None:
                _require_chat(conn, body.chat_id)
            if body.run_id is not None:
                _require_run_with_artifacts(conn, body.run_id)
            metadata = {**(body.metadata or {}), "local_only": True, "external_send_blocked": True, "requires_explicit_approval": True}
            cur = conn.execute(
                """
                INSERT INTO telegram_outbox(kind, status, target_label, external_chat_id, external_thread_id, content, run_id, chat_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.kind,
                    body.status,
                    body.target_label,
                    body.external_chat_id,
                    body.external_thread_id,
                    body.content,
                    body.run_id,
                    body.chat_id,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            return _telegram_outbox_to_dict(conn.execute("SELECT * FROM telegram_outbox WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/telegram/outbox/{outbox_id}")
    def update_telegram_outbox_draft(outbox_id: int, body: TelegramOutboxUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"content", "status", "target_label"})
        if not fields:
            raise HTTPException(status_code=400, detail="No Telegram outbox updates provided")
        with get_conn() as conn:
            _require_telegram_outbox(conn, outbox_id)
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE telegram_outbox SET {assignments} WHERE id = ?", (*fields.values(), outbox_id))
            return _telegram_outbox_to_dict(conn.execute("SELECT * FROM telegram_outbox WHERE id = ?", (outbox_id,)).fetchone())

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate):
        with get_conn() as conn:
            slug = _unique_project_slug(conn, body.name)
            root_path = str(create_project_workspace(app.state.workspace_root, slug, body.name, body.mode_default))
            cur = conn.execute(
                "INSERT INTO projects(name, slug, description, root_path, mode_default) VALUES (?, ?, ?, ?, ?)",
                (body.name, slug, body.description, root_path, body.mode_default),
            )
            project_id = cur.lastrowid
            conn.execute(
                "INSERT INTO chats(project_id, title, mode) VALUES (?, ?, ?)",
                (project_id, "Main", body.mode_default),
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row_to_dict(row)

    @app.post("/api/projects/{project_id}/chats", status_code=201)
    def create_chat(project_id: int, body: ChatCreate):
        with get_conn() as conn:
            project = conn.execute("SELECT id, mode_default FROM projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            chat_mode = project["mode_default"]
            session_id = str(uuid.uuid4())
            cur = conn.execute(
                "INSERT INTO chats(project_id, title, mode, hermes_session_id) VALUES (?, ?, ?, ?)",
                (project_id, body.title, chat_mode, session_id),
            )
            row = conn.execute("SELECT id, title, mode, hermes_session_id, archived, created_at, updated_at FROM chats WHERE id = ?", (cur.lastrowid,)).fetchone()
            return row_to_dict(row)

    @app.delete("/api/projects/{project_id}/chats/{chat_id}")
    def delete_chat(project_id: int, chat_id: int):
        with get_conn() as conn:
            _require_project(conn, project_id)
            chat = conn.execute("SELECT id FROM chats WHERE id = ? AND project_id = ?", (chat_id, project_id)).fetchone()
            if chat is None:
                raise HTTPException(status_code=404, detail="Chat not found")
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM runs WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            return {"deleted": True, "chat_id": chat_id}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: int):
        with get_conn() as conn:
            _require_project(conn, project_id)
            chats = conn.execute("SELECT id FROM chats WHERE project_id = ?", (project_id,)).fetchall()
            for chat in chats:
                conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat["id"],))
                conn.execute("DELETE FROM runs WHERE chat_id = ?", (chat["id"],))
            conn.execute("DELETE FROM chats WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_goals WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_tasks WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_notes WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_files WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM project_stage_defaults WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            # A project's root_path may be an imported user folder. Removing the
            # Helm record must never recursively delete user-owned files.
            return {"deleted": True, "project_id": project_id}

    @app.get("/api/browse")
    def browse_directory(path: str | None = None):
        """List subdirectories of a given path (restricted to home dir for security)."""
        home = Path.home().resolve()
        target = Path(path).expanduser().resolve() if path else home
        # Security: restrict to home directory
        try:
            target.relative_to(home)
        except ValueError:
            raise HTTPException(status_code=403, detail="Cesta je mimo domáceho adresára")
        if not target.exists():
            raise HTTPException(status_code=404, detail="Adresár neexistuje")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="Cesta nie je adresár")
        try:
            entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            directories = [
                {"name": e.name, "path": str(e), "is_dir": True}
                for e in entries
                if e.is_dir() and not e.name.startswith(".")
            ]
            return {"path": str(target), "parent": str(target.parent) if str(target.parent) != str(target) else None, "directories": directories}
        except PermissionError:
            raise HTTPException(status_code=403, detail="Prístup zamietnutý")

    @app.post("/api/projects/open", status_code=201)
    def open_existing_folder(body: ProjectOpen):
        """Register an existing directory inside the user's home as a project."""
        home = Path.home().resolve()
        folder_path = Path(body.path).expanduser().resolve()
        try:
            relative_path = folder_path.relative_to(home)
        except ValueError:
            raise HTTPException(status_code=403, detail="Cesta je mimo domáceho adresára")
        if not relative_path.parts:
            raise HTTPException(status_code=403, detail="Domovský adresár nemožno otvoriť ako projekt")
        if not folder_path.exists() or not folder_path.is_dir():
            raise HTTPException(status_code=404, detail="Adresár neexistuje")
        name = body.name or folder_path.name
        with get_conn() as conn:
            slug = _unique_project_slug(conn, name)
            root_path = str(folder_path)
            cur = conn.execute(
                "INSERT INTO projects(name, slug, description, root_path, mode_default) VALUES (?, ?, ?, ?, ?)",
                (name, slug, body.description or f"Existujúci adresár: {root_path}", root_path, body.mode_default),
            )
            project_id = cur.lastrowid
            conn.execute(
                "INSERT INTO chats(project_id, title, mode) VALUES (?, ?, ?)",
                (project_id, "Main", body.mode_default),
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row_to_dict(row)

    @app.get("/api/projects/{project_id}/workspace")
    def get_project_workspace(project_id: int):
        with get_conn() as conn:
            project = _require_project(conn, project_id)
            return _project_workspace_payload(conn, project)

    @app.put("/api/projects/{project_id}/context")
    def update_project_context(project_id: int, body: ProjectContextUpdate):
        with get_conn() as conn:
            project = _require_project(conn, project_id)
            path = _project_context_path(project)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.content, encoding="utf-8")
            return {"path": str(path), "content": body.content, "size": path.stat().st_size}

    @app.post("/api/projects/{project_id}/goals", status_code=201)
    def create_project_goal(project_id: int, body: ProjectGoalCreate):
        with get_conn() as conn:
            _require_project(conn, project_id)
            _validate_project_goal_links(conn, project_id, body.vision_id, body.parent_goal_id)
            cur = conn.execute(
                """
                INSERT INTO project_goals(project_id, vision_id, parent_goal_id, title, description, horizon, status, target_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, body.vision_id, body.parent_goal_id, body.title.strip(), body.description, body.horizon, body.status, body.target_date),
            )
            return _project_goal_to_dict(conn, cur.lastrowid)

    @app.patch("/api/projects/{project_id}/goals/{goal_id}")
    def update_project_goal(project_id: int, goal_id: int, body: ProjectGoalUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"title", "description", "status", "horizon"})
        if not fields:
            raise HTTPException(status_code=400, detail="No goal updates provided")
        with get_conn() as conn:
            _require_project(conn, project_id)
            existing = _require_project_goal(conn, goal_id)
            if existing["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="Project goal not found")
            _validate_project_goal_links(conn, project_id, fields.get("vision_id"), fields.get("parent_goal_id"), goal_id=goal_id)
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE project_goals SET {assignments} WHERE id = ? AND project_id = ?", (*fields.values(), goal_id, project_id))
            return _project_goal_to_dict(conn, goal_id)

    @app.post("/api/projects/{project_id}/tasks", status_code=201)
    def create_project_task(project_id: int, body: ProjectTaskCreate):
        with get_conn() as conn:
            _require_project(conn, project_id)
            cur = conn.execute(
                "INSERT INTO project_tasks(project_id, title, notes, status, priority, due_date) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, body.title, body.notes, body.status, body.priority, body.due_date),
            )
            return row_to_dict(conn.execute("SELECT * FROM project_tasks WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/projects/{project_id}/tasks/{task_id}")
    def update_project_task(project_id: int, task_id: int, body: ProjectTaskUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items() if value is not None}
        if not fields:
            raise HTTPException(status_code=400, detail="No task updates provided")
        with get_conn() as conn:
            _require_project(conn, project_id)
            existing = conn.execute("SELECT id FROM project_tasks WHERE id = ? AND project_id = ?", (task_id, project_id)).fetchone()
            if existing is None:
                raise HTTPException(status_code=404, detail="Project task not found")
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(f"UPDATE project_tasks SET {assignments} WHERE id = ? AND project_id = ?", (*fields.values(), task_id, project_id))
            return row_to_dict(conn.execute("SELECT * FROM project_tasks WHERE id = ?", (task_id,)).fetchone())


    @app.get("/api/projects/{project_id}/team-workflows")
    def list_team_workflows(project_id: int, status: str = "active"):
        if status not in {"active", "paused", "done", "archived", "all"}:
            raise HTTPException(status_code=400, detail="Invalid team workflow status")
        with get_conn() as conn:
            _require_project(conn, project_id)
            query = "SELECT * FROM team_workflows WHERE project_id = ?"
            params: tuple = (project_id,)
            if status != "all":
                query += " AND status = ?"
                params = (project_id, status)
            query += " ORDER BY id DESC LIMIT 20"
            return {"workflows": [_team_workflow_to_dict(conn, row) for row in conn.execute(query, params)]}

    @app.post("/api/projects/{project_id}/team-workflows", status_code=201)
    def create_team_workflow(project_id: int, body: TeamWorkflowCreate):
        with get_conn() as conn:
            _require_project(conn, project_id)
            if body.chat_id is not None:
                chat = _require_chat(conn, body.chat_id)
                if chat["project_id"] != project_id:
                    raise HTTPException(status_code=400, detail="Chat must belong to selected project")
            if body.run_id is not None:
                run = _require_run_with_artifacts(conn, body.run_id)
                run_chat = _require_chat(conn, run["chat_id"])
                if run_chat["project_id"] != project_id:
                    raise HTTPException(status_code=400, detail="Run must belong to selected project")
            cur = conn.execute(
                """
                INSERT INTO team_workflows(project_id, chat_id, run_id, title, status, current_role)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, body.chat_id, body.run_id, body.title.strip(), body.status, body.current_role),
            )
            if body.run_id is not None:
                run = _require_run_with_artifacts(conn, body.run_id)
                _record_run_event(conn, body.run_id, "team_workflow_created", {"workflow_id": cur.lastrowid, "title": body.title.strip()}, run.get("artifact_path") or None)
            return _team_workflow_to_dict(conn, conn.execute("SELECT * FROM team_workflows WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.post("/api/runs/{run_id}/team-workflow-template", status_code=201)
    def create_run_team_workflow_template(run_id: int, body: TeamWorkflowTemplateCreate):
        template_steps = [
            ("planner", "Plan scope and split work", "active", "Plan the TEAM run scope and define handoff-ready subtasks. Distinguish assumptions from verified context.", "Plan scope and split work", "running", 10),
            ("executor", "Implement or execute assigned work", "pending", "Executor/implementer self-report belongs here; it is not verified evidence until reviewer/verifier checks it.", "Implement or execute assigned work", "queued", 20),
            ("reviewer", "Review self-report and risks", "pending", "Self-report review: identify gaps, unsafe assumptions, and missing tests before verifier signs off.", "Review executor self-report and risks", "queued", 30),
            ("verifier", "Verify evidence before completion", "pending", "Parent verification requires verified evidence: tests, file reads, browser/API smoke, or concrete artifact handles.", "Verify evidence before completion", "queued", 40),
        ]
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            chat = _require_chat(conn, run["chat_id"])
            project_id = body.project_id if body.project_id is not None else chat["project_id"]
            if project_id != chat["project_id"]:
                raise HTTPException(status_code=400, detail="Run must belong to selected project")
            _require_project(conn, project_id)
            cur = conn.execute(
                """
                INSERT INTO team_workflows(project_id, chat_id, run_id, title, status, current_role)
                VALUES (?, ?, ?, ?, 'active', 'planner')
                """,
                (project_id, run["chat_id"], run_id, body.title.strip()),
            )
            workflow_id = cur.lastrowid
            for role, title, status, notes, agent_goal, agent_status, sort_order in template_steps:
                conn.execute(
                    """
                    INSERT INTO team_workflow_steps(workflow_id, role, title, status, notes, sort_order, agent_goal, agent_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (workflow_id, role, title, status, notes, sort_order, agent_goal, agent_status),
                )
            roles = [role for role, *_ in template_steps]
            _record_run_event(
                conn,
                run_id,
                "team_workflow_template_initialized",
                {"workflow_id": workflow_id, "roles": roles},
                run.get("artifact_path") or None,
            )
            return _team_workflow_to_dict(conn, conn.execute("SELECT * FROM team_workflows WHERE id = ?", (workflow_id,)).fetchone())

    @app.post("/api/team-workflows/{workflow_id}/steps", status_code=201)
    def create_team_workflow_step(workflow_id: int, body: TeamWorkflowStepCreate):
        with get_conn() as conn:
            workflow = _require_team_workflow(conn, workflow_id)
            if body.status == "active":
                conn.execute(
                    "UPDATE team_workflow_steps SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ? AND status = 'active'",
                    (workflow_id,),
                )
                conn.execute(
                    "UPDATE team_workflows SET current_role = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (body.role, workflow_id),
                )
            cur = conn.execute(
                """
                INSERT INTO team_workflow_steps(workflow_id, role, title, status, notes, sort_order, agent_goal, agent_status, self_report, verified_evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (workflow_id, body.role, body.title.strip(), body.status, body.notes, body.sort_order, body.agent_goal.strip(), body.agent_status, body.self_report.strip(), body.verified_evidence.strip()),
            )
            if workflow.get("run_id"):
                run = _require_run_with_artifacts(conn, workflow["run_id"])
                _record_run_event(conn, workflow["run_id"], "team_workflow_step_created", {"workflow_id": workflow_id, "step_id": cur.lastrowid, "role": body.role}, run.get("artifact_path") or None)
                if body.agent_goal.strip() or body.agent_status != "queued" or body.self_report.strip() or body.verified_evidence.strip():
                    _record_run_event(
                        conn,
                        workflow["run_id"],
                        "team_agent_task_updated",
                        {
                            "workflow_id": workflow_id,
                            "step_id": cur.lastrowid,
                            "role": body.role,
                            "agent_status": body.agent_status,
                            "self_report_present": bool(body.self_report.strip()),
                            "verified_evidence_present": bool(body.verified_evidence.strip()),
                        },
                        run.get("artifact_path") or None,
                    )
            return _team_workflow_step_to_dict(conn.execute("SELECT * FROM team_workflow_steps WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.patch("/api/team-workflows/{workflow_id}/steps/{step_id}")
    def update_team_workflow_step(workflow_id: int, step_id: int, body: TeamWorkflowStepUpdate):
        fields = {key: value for key, value in body.model_dump(exclude_unset=True).items()}
        _reject_null_fields(fields, {"role", "title", "status", "notes", "sort_order", "agent_goal", "agent_status", "self_report", "verified_evidence"})
        if not fields:
            raise HTTPException(status_code=400, detail="No team workflow step updates provided")
        with get_conn() as conn:
            workflow = _require_team_workflow(conn, workflow_id)
            existing_step = _require_team_workflow_step(conn, workflow_id, step_id)
            next_role = fields.get("role") or existing_step["role"]
            next_status = fields.get("status") or existing_step["status"]
            if next_status == "active":
                conn.execute(
                    "UPDATE team_workflow_steps SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ? AND id != ? AND status = 'active'",
                    (workflow_id, step_id),
                )
                conn.execute(
                    "UPDATE team_workflows SET current_role = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (next_role, workflow_id),
                )
            assignments = ", ".join(f"{key} = ?" for key in fields) + ", updated_at = CURRENT_TIMESTAMP"
            values = [value.strip() if key in {"title", "agent_goal", "self_report", "verified_evidence"} and isinstance(value, str) else value for key, value in fields.items()]
            conn.execute(f"UPDATE team_workflow_steps SET {assignments} WHERE id = ? AND workflow_id = ?", (*values, step_id, workflow_id))
            if next_status != "active" and existing_step["status"] == "active":
                active_step = conn.execute(
                    """
                    SELECT role FROM team_workflow_steps
                    WHERE workflow_id = ? AND status = 'active'
                    ORDER BY sort_order ASC, id ASC
                    LIMIT 1
                    """,
                    (workflow_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE team_workflows SET current_role = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (active_step["role"] if active_step else "lead", workflow_id),
                )
            if workflow.get("run_id"):
                run = _require_run_with_artifacts(conn, workflow["run_id"])
                event_fields = dict(fields)
                for bridge_key in ("agent_goal", "self_report", "verified_evidence"):
                    if bridge_key in event_fields:
                        event_fields[f"{bridge_key}_present"] = bool(str(event_fields.pop(bridge_key) or "").strip())
                _record_run_event(conn, workflow["run_id"], "team_workflow_step_updated", {"workflow_id": workflow_id, "step_id": step_id, **event_fields}, run.get("artifact_path") or None)
                if any(key in fields for key in {"agent_goal", "agent_status", "self_report", "verified_evidence"}):
                    updated_step = conn.execute("SELECT * FROM team_workflow_steps WHERE id = ?", (step_id,)).fetchone()
                    _record_run_event(
                        conn,
                        workflow["run_id"],
                        "team_agent_task_updated",
                        {
                            "workflow_id": workflow_id,
                            "step_id": step_id,
                            "role": fields.get("role") or existing_step["role"],
                            "agent_status": fields.get("agent_status") or updated_step["agent_status"],
                            "self_report_present": bool(str((fields.get("self_report") if "self_report" in fields else updated_step["self_report"]) or "").strip()),
                            "verified_evidence_present": bool(str((fields.get("verified_evidence") if "verified_evidence" in fields else updated_step["verified_evidence"]) or "").strip()),
                        },
                        run.get("artifact_path") or None,
                    )
            return _team_workflow_step_to_dict(conn.execute("SELECT * FROM team_workflow_steps WHERE id = ?", (step_id,)).fetchone())

    @app.post("/api/team-workflows/{workflow_id}/steps/{step_id}/spawn", status_code=202)
    def spawn_team_workflow_step(workflow_id: int, step_id: int, body: TeamWorkflowStepSpawn = TeamWorkflowStepSpawn()):
        with get_conn() as conn:
            workflow = _require_team_workflow(conn, workflow_id)
            step = _require_team_workflow_step(conn, workflow_id, step_id)
            if not workflow.get("run_id"):
                raise HTTPException(status_code=400, detail="Workflow must be linked to a run to spawn executor")
            run = _require_run_with_artifacts(conn, workflow["run_id"])
            role = step["role"]
            if role != "executor":
                raise HTTPException(status_code=400, detail="Only executor steps can be spawned")
            if step["spawn_status"] in {"spawning", "spawned"}:
                raise HTTPException(status_code=409, detail="Step is already spawning or spawned")
            command = body.command.strip() or os.getenv("HELM_RUNTIME_COMMAND", "").strip()
            if not command:
                raise HTTPException(status_code=503, detail="No executor runtime configured; set HELM_RUNTIME_COMMAND.")
            command_argv = shlex.split(command)
            if not command_argv:
                raise HTTPException(status_code=503, detail="No executor runtime configured; set HELM_RUNTIME_COMMAND.")
            artifact_path = Path(run["artifact_path"])
            input_path = artifact_path / "input.md"
            runtime_prompt = (
                f"You are the Executor in a Helm TEAM workflow. "
                f"Step goal: {step['agent_goal'] or step['title']}. "
                f"Read the run input at `{input_path}` for full context. "
                f"Complete the assigned work, then save your self-report to `{artifact_path}/executor-report.md`. "
                f"Append progress events to `{artifact_path}/runtime-events.jsonl`."
            )
            conn.execute(
                "UPDATE team_workflow_steps SET spawn_command = ?, spawn_status = 'spawning', agent_status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND workflow_id = ?",
                (command, step_id, workflow_id),
            )
            conn.commit()
        cwd = str(artifact_path) if artifact_path is not None else None
        try:
            proc = subprocess.Popen(
                [*command_argv, runtime_prompt],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
            )
            with get_conn() as conn:
                conn.execute(
                    "UPDATE team_workflow_steps SET spawn_pid = ?, spawn_status = 'spawned', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND workflow_id = ?",
                    (proc.pid, step_id, workflow_id),
                )
            return {
                "spawned": True,
                "step_id": step_id,
                "workflow_id": workflow_id,
                "role": role,
                "pid": proc.pid,
                "command": command,
            }
        except OSError as exc:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE team_workflow_steps SET spawn_status = 'failed', agent_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND workflow_id = ?",
                    (step_id, workflow_id),
                )
            raise HTTPException(status_code=500, detail=f"Failed to spawn executor: {exc}")

    @app.get("/api/team-workflows/{workflow_id}/steps/{step_id}/spawn")
    def get_team_workflow_spawn_status(workflow_id: int, step_id: int):
        with get_conn() as conn:
            step = _require_team_workflow_step(conn, workflow_id, step_id)
            return {
                "step_id": step_id,
                "workflow_id": workflow_id,
                "role": step["role"],
                "spawn_status": step["spawn_status"],
                "spawn_command": step["spawn_command"],
                "spawn_pid": step["spawn_pid"],
                "agent_status": step["agent_status"],
                "agent_goal": step["agent_goal"],
            }

    @app.post("/api/projects/{project_id}/notes", status_code=201)
    def create_project_note(project_id: int, body: ProjectNoteCreate):
        with get_conn() as conn:
            _require_project(conn, project_id)
            cur = conn.execute(
                "INSERT INTO project_notes(project_id, title, content) VALUES (?, ?, ?)",
                (project_id, body.title, body.content),
            )
            return row_to_dict(conn.execute("SELECT * FROM project_notes WHERE id = ?", (cur.lastrowid,)).fetchone())

    @app.post("/api/projects/{project_id}/files", status_code=201)
    def create_project_file(project_id: int, body: ProjectFileCreate):
        with get_conn() as conn:
            project = _require_project(conn, project_id)
            safe_path = _validate_project_file_path(project, body.path, body.kind)
            cur = conn.execute(
                "INSERT OR REPLACE INTO project_files(project_id, path, label, kind, pinned, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (project_id, safe_path, body.label, body.kind, 1 if body.pinned else 0),
            )
            row_id = cur.lastrowid
            return _project_file_to_dict(conn.execute("SELECT * FROM project_files WHERE id = ?", (row_id,)).fetchone())

    @app.put("/api/projects/{project_id}/stage-defaults/{stage}")
    def upsert_project_stage_default(project_id: int, stage: str, body: ProjectStageDefaultUpsert):
        stage = _validated_stage(stage)
        with get_conn() as conn:
            _require_project(conn, project_id)
            conn.execute(
                """
                INSERT INTO project_stage_defaults(project_id, stage, instructions, artifact_template)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, stage) DO UPDATE SET
                    instructions = excluded.instructions,
                    artifact_template = excluded.artifact_template,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (project_id, stage, body.instructions, body.artifact_template),
            )
            return row_to_dict(conn.execute("SELECT * FROM project_stage_defaults WHERE project_id = ? AND stage = ?", (project_id, stage)).fetchone())

    @app.get("/api/workspace/tree")
    def get_workspace_tree(path: str = "", depth: int = 2, project_id: int | None = None):
        root = Path(app.state.workspace_root).resolve()
        if project_id is not None:
            with get_conn() as conn:
                project = _require_project(conn, project_id)
                if project.get("root_path"):
                    root = Path(project["root_path"]).expanduser().resolve()
        target = root
        if path.strip():
            candidate = (root / path.strip()).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                raise HTTPException(status_code=400, detail="Path escapes workspace root")
            target = candidate
        if not target.exists() or not target.is_dir():
            raise HTTPException(status_code=404, detail="Directory not found")
        depth = max(1, min(int(depth or 1), 4))
        return {
            "root": str(root),
            "path": str(target),
            "relative_path": "" if target == root else str(target.relative_to(root)),
            "depth": depth,
            "entries": _workspace_entries(root, target, depth),
            "items": workspace_tree(app.state.workspace_root),
        }

    @app.get("/api/runs/{run_id}/live")
    def get_run_live_progress(run_id: int):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
            run = row_to_dict(row)
            artifact_path = str(run.get("artifact_path") or "")
            if artifact_path:
                _ingest_runtime_events(conn, run_id, Path(artifact_path) / "runtime-events.jsonl", artifact_path)
                _harvest_runtime_tool_events(conn, run_id, artifact_path)
            tool_rows = [
                row_to_dict(event)
                for event in conn.execute(
                    "SELECT * FROM run_events WHERE run_id = ? AND type = 'tool_activity' ORDER BY id",
                    (run_id,),
                )
            ]
            tools_recent = []
            for event in tool_rows[-LIVE_TOOLS_RECENT:]:
                try:
                    payload = json.loads(event.get("payload_json") or "{}")
                except json.JSONDecodeError:
                    payload = {}
                tools_recent.append(
                    {
                        "tool": payload.get("tool") or "tool",
                        "summary": payload.get("summary") or "",
                        "source": payload.get("source") or "",
                    }
                )
            return {
                "run_id": run_id,
                "status": run.get("status"),
                "current_stage": run.get("current_stage"),
                "stage_status": run.get("stage_status"),
                "waiting_for_tokens": bool(run.get("waiting_for_tokens")),
                "stdout_tail": _read_stdout_tail(artifact_path),
                "tool_count": len(tool_rows),
                "tools_recent": tools_recent,
            }

    @app.get("/api/runs/{run_id}/diff")
    def get_run_diff(run_id: int):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
        return _repo_diff_payload(run_id)

    def _create_run_for_chat(conn, chat_id: int, prompt: str, explicit_permission: bool = False, context_tier: str | None = None) -> dict:
        chat = _require_chat(conn, chat_id)
        project = _require_project(conn, chat["project_id"])
        run_mode = project["mode_default"]
        project_context = _read_project_context(project)
        cur = conn.execute(
            "INSERT INTO runs(chat_id, status, mode, prompt, current_stage, stage_status, base_commit) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, "queued", run_mode, prompt, "input", "active", _git_head_commit() or None),
        )
        run_id = cur.lastrowid
        project_map_docs = load_project_map_docs(app.state.workspace_root)
        retrieved_context_chunks = load_retrieved_context_chunks(app.state.workspace_root, prompt)
        symbol_index = load_symbol_index(app.state.workspace_root, prompt)
        workflow_policies = load_workflow_policies(app.state.workspace_root)
        runtime_contracts = load_runtime_contracts()
        # M15a -> M17 bridge: auto-generated summaries are fresher than any
        # hand-maintained "current milestone" prose, so feed them as context.
        recent_run_summaries = list_run_summaries_by_project(conn, project["id"], limit=3)
        artifact_dir = create_run_artifacts(
            app.state.workspace_root,
            run_id,
            prompt,
            run_mode,
            project_context,
            project_map_docs=project_map_docs,
            retrieved_context_chunks=retrieved_context_chunks,
            symbol_index=symbol_index,
            workflow_policies=workflow_policies,
            runtime_contracts=runtime_contracts,
            context_tier=context_tier,
            recent_run_summaries=recent_run_summaries,
        )
        conn.execute("UPDATE runs SET artifact_path = ? WHERE id = ?", (str(artifact_dir), run_id))
        conn.execute(
            "INSERT INTO run_events(run_id, type, payload_json) VALUES (?, ?, ?)",
            (run_id, "run_created", json.dumps({"mode": run_mode, "project_id": project["id"]}, ensure_ascii=False)),
        )
        safety_enabled = _settings_payload(conn)["safety_policy_enabled"]
        safety_message = (
            "Externé akcie a zápisy sú blokované bez potvrdenia."
            if safety_enabled
            else "Safety policy je vypnutá: runtime môže štartovať bez approval kroku."
        )
        conn.execute(
            "INSERT INTO run_events(run_id, type, payload_json) VALUES (?, ?, ?)",
            (run_id, "safety_note", json.dumps({"message": safety_message, "enabled": safety_enabled}, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO run_events(run_id, type, payload_json) VALUES (?, ?, ?)",
            (run_id, "artifacts_initialized", json.dumps({"artifact_path": str(artifact_dir)}, ensure_ascii=False)),
        )
        # Log the tier decision for every run so thresholds can be tuned from
        # real data later (see GET /api/context/tier-stats).
        tier_decision = classify_context_tier(prompt, override=context_tier)
        _record_run_event(
            conn,
            run_id,
            "context_tier_selected",
            {
                "tier": tier_decision["tier"],
                "budget_tokens": tier_decision["budget_tokens"],
                "override": tier_decision["override"],
                "reason": tier_decision["reason"],
                "signals": tier_decision["signals"],
                "rendered_chars": len((Path(artifact_dir) / "input.md").read_text(encoding="utf-8"))
                if (Path(artifact_dir) / "input.md").exists()
                else 0,
                "summaries_injected": len(recent_run_summaries),
            },
            str(artifact_dir),
        )
        _record_run_event(
            conn,
            run_id,
            "run_contract_initialized",
            {"artifact": "run-contract.md", "mode": run_mode},
            str(artifact_dir),
        )
        for map_doc in project_map_docs:
            _record_run_event(
                conn,
                run_id,
                "context_map_loaded",
                {
                    "name": map_doc["name"],
                    "path": map_doc["path"],
                    "source_path": map_doc["source_path"],
                    "source": map_doc["source"],
                    "size": len(str(map_doc["content"])),
                    "original_size": map_doc["original_size"],
                    "truncated": map_doc["truncated"],
                },
                str(artifact_dir),
            )
        if symbol_index:
            # M22: log the decision so the grep-avoidance benefit is measurable
            # later rather than assumed.
            _record_run_event(
                conn,
                run_id,
                "symbol_index_loaded",
                {
                    "count": len(symbol_index),
                    "symbols": [
                        {"name": hit["name"], "path": hit["path"], "line": hit["line"]}
                        for hit in symbol_index[:12]
                    ],
                },
                str(artifact_dir),
            )
        for chunk in retrieved_context_chunks:
            _record_run_event(
                conn,
                run_id,
                "retrieved_context_chunk",
                {
                    "path": chunk["path"],
                    "source_path": chunk["source_path"],
                    "start_line": chunk["start_line"],
                    "end_line": chunk["end_line"],
                    "score": chunk["score"],
                    "truncated": chunk["truncated"],
                    "size": len(str(chunk["content"])),
                },
                str(artifact_dir),
            )
        for policy in workflow_policies:
            _record_run_event(
                conn,
                run_id,
                "workflow_policy_loaded",
                {"name": policy["name"], "path": policy["path"], "size": len(policy["content"])},
                str(artifact_dir),
            )
        for contract in runtime_contracts:
            _record_run_event(
                conn,
                run_id,
                "runtime_contract_loaded",
                {"name": contract["name"], "path": contract["path"], "size": len(contract["content"])},
                str(artifact_dir),
            )
        _record_run_event(
            conn,
            run_id,
            "stage_started",
            {"stage": "input", "label": STAGE_LABELS["input"]},
            str(artifact_dir),
        )
        return row_to_dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    @app.get("/api/chats/{chat_id}/messages")
    def list_messages(chat_id: int):
        with get_conn() as conn:
            _require_chat(conn, chat_id)
            messages = [
                row_to_dict(row)
                for row in conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
            ]
        return {"messages": messages}

    @app.post("/api/chats/{chat_id}/messages", status_code=201)
    def create_message(chat_id: int, body: MessageCreate):
        with get_conn() as conn:
            _require_chat(conn, chat_id)
            cur = conn.execute(
                "INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)",
                (chat_id, body.role, body.content),
            )
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
            return row_to_dict(row)

    @app.post("/api/chats/{chat_id}/runs", status_code=201)
    def create_run(chat_id: int, body: RunCreate):
        with get_conn() as conn:
            return _create_run_for_chat(conn, chat_id, body.prompt, explicit_permission=body.explicit_permission, context_tier=body.context_tier)

    @app.post("/api/chats/{chat_id}/run-loop", status_code=201)
    def create_chat_run_loop(chat_id: int, body: ChatRunCreate):
        with get_conn() as conn:
            _require_chat(conn, chat_id)
            cur = conn.execute(
                "INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)",
                (chat_id, "user", body.content),
            )
            message = row_to_dict(conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone())
            run = _create_run_for_chat(conn, chat_id, body.content, explicit_permission=body.explicit_permission, context_tier=body.context_tier)
            response: dict[str, object] = {"message": message, "run": run, "runtime": None}
            if not body.start_runtime:
                return response
            runtime = _start_runtime_bridge_background(conn, app.state.db_path, run, app.state.runtime_command, body.timeout_seconds)
            updated = row_to_dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone())
            response.update({"run": updated, "runtime": runtime})
            return response

    @app.get("/api/chats/{chat_id}/runs/latest")
    def latest_run(chat_id: int):
        with get_conn() as conn:
            _require_chat(conn, chat_id)
            row = conn.execute("SELECT * FROM runs WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
            return {"run": row_to_dict(row) if row else None}

    @app.get("/api/runs/{run_id}/events")
    def list_run_events(run_id: int):
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
            run = row_to_dict(row)
            if run.get("artifact_path"):
                artifact_path = Path(run["artifact_path"])
                _ingest_runtime_events(conn, run_id, artifact_path / "runtime-events.jsonl", str(artifact_path))
                _harvest_runtime_tool_events(conn, run_id, str(artifact_path))
            events = [
                row_to_dict(event)
                for event in conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,))
            ]
        return {"events": events}



    @app.post("/api/runs/{run_id}/telegram-report", status_code=201)
    def create_run_telegram_report(run_id: int, body: TelegramRunReportCreate):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            content = _build_run_telegram_report(conn, run, body.include_artifact_path)
            msg_cur = conn.execute(
                "INSERT INTO messages(chat_id, role, content, metadata_json) VALUES (?, 'report', ?, ?)",
                (run["chat_id"], content, json.dumps({"source": "telegram_report_draft", "run_id": run_id}, ensure_ascii=False)),
            )
            cur = conn.execute(
                """
                INSERT INTO telegram_outbox(kind, status, target_label, external_chat_id, external_thread_id, content, run_id, chat_id, message_id, metadata_json)
                VALUES ('run_report', 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    body.target_label,
                    body.external_chat_id,
                    body.external_thread_id,
                    content,
                    run_id,
                    run["chat_id"],
                    msg_cur.lastrowid,
                    json.dumps({"local_only": True, "external_send_blocked": True, "requires_explicit_approval": True}, ensure_ascii=False),
                ),
            )
            _record_run_event(
                conn,
                run_id,
                "telegram_report_drafted",
                {"outbox_id": cur.lastrowid, "message_id": msg_cur.lastrowid, "target_label": body.target_label, "local_only": True},
                run.get("artifact_path") or None,
            )
            return {
                "outbox_item": _telegram_outbox_to_dict(conn.execute("SELECT * FROM telegram_outbox WHERE id = ?", (cur.lastrowid,)).fetchone()),
                "message": row_to_dict(conn.execute("SELECT * FROM messages WHERE id = ?", (msg_cur.lastrowid,)).fetchone()),
                "external_send": "blocked_until_explicit_approval",
            }

    @app.get("/api/runs/{run_id}/stages")
    def get_run_stages(run_id: int):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
        return {
            "run": run,
            "stages": _stage_progress(run),
            "needs_attention": bool(run.get("needs_attention")),
            "attention_reason": str(run.get("attention_reason") or ""),
        }

    @app.get("/api/runs/needs-attention")
    def runs_needing_attention(limit: int = 20):
        """Runs that finished but need user review: failing tests, errors,
        or external actions. Distinguishes 'waiting for review' from 'done'."""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT id, chat_id, status, current_stage, prompt, attention_reason, started_at
                   FROM runs
                   WHERE needs_attention = 1 AND status NOT IN ('approved', 'completed')
                   ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        runs = [row_to_dict(row) for row in rows]
        return {"count": len(runs), "runs": runs}

    @app.get("/api/runs/{run_id}/terminal")
    def get_run_terminal(run_id: int, lines: int = 200):
        """Tail the runtime stdout + stderr logs.

        Deliberately bounded: the panel shows the newest N lines only, because
        dumping a full log is what makes a console unusable. Secrets are scrubbed
        since this is rendered straight into the UI.
        """
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
            run = row_to_dict(row)

        limit = max(1, min(int(lines or 200), 1000))
        artifact_path = str(run.get("artifact_path") or "")
        log_path = Path(artifact_path) / "runtime-stdout.log" if artifact_path else None
        err_path = Path(artifact_path) / "runtime-stderr.log" if artifact_path else None

        if log_path is None or not log_path.exists() or not log_path.is_file():
            return {
                "output": "",
                "line_count": 0,
                "truncated": False,
                "total_lines": 0,
                "status": run.get("status"),
                "stderr_tail": "",
                "stderr_lines": 0,
            }
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                all_lines = handle.readlines()
        except OSError as exc:
            logger.warning("terminal tail failed for run %s: %s", run_id, exc)
            return {"output": "", "line_count": 0, "truncated": False, "total_lines": 0, "status": run.get("status"), "stderr_tail": "", "stderr_lines": 0}

        tail = all_lines[-limit:]
        rendered = "".join(line[:2000] for line in tail)

        # Read stderr tail if available
        stderr_tail = ""
        stderr_lines = 0
        if err_path and err_path.exists() and err_path.is_file():
            try:
                with err_path.open("r", encoding="utf-8", errors="replace") as handle:
                    err_all = handle.readlines()
                err_tail = err_all[-50:]  # stderr is usually shorter
                stderr_tail = "".join(line[:2000] for line in err_tail)
                stderr_lines = len(err_tail)
            except OSError:
                stderr_tail = ""

        return {
            "output": _scrub_secrets(rendered),
            "line_count": len(tail),
            "truncated": len(all_lines) > len(tail),
            "total_lines": len(all_lines),
            "status": run.get("status"),
            "stderr_tail": _scrub_secrets(stderr_tail),
            "stderr_lines": stderr_lines,
        }

    @app.post("/api/runs/{run_id}/advance-stage")
    def advance_stage(run_id: int, body: StageAdvanceRequest = StageAdvanceRequest()):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            current_stage = _validated_stage(run.get("current_stage") or "input")
            current_index = STAGE_SEQUENCE.index(current_stage)
            if current_index == len(STAGE_SEQUENCE) - 1:
                conn.execute(
                    "UPDATE runs SET status = ?, stage_status = ?, finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP) WHERE id = ?",
                    ("completed", "completed", run_id),
                )
                _record_run_event(conn, run_id, "run_finalized", {"stage": current_stage, "note": body.note}, run["artifact_path"])
            else:
                next_stage = STAGE_SEQUENCE[current_index + 1]
                next_status = "review" if next_stage == "review" else "running"
                conn.execute(
                    "UPDATE runs SET status = ?, current_stage = ?, stage_status = ? WHERE id = ?",
                    (next_status, next_stage, "active", run_id),
                )
                _record_run_event(
                    conn,
                    run_id,
                    "stage_advanced",
                    {"from": current_stage, "to": next_stage, "note": body.note},
                    run["artifact_path"],
                )
                _record_run_event(
                    conn,
                    run_id,
                    "stage_started",
                    {"stage": next_stage, "label": STAGE_LABELS[next_stage]},
                    run["artifact_path"],
                )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            updated = row_to_dict(row)
            return {"run": updated, "stages": _stage_progress(updated)}

    @app.post("/api/runs/{run_id}/summary", status_code=201)
    def create_run_summary(run_id: int, body: RunSummaryCreate):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            chat = conn.execute("SELECT project_id FROM chats WHERE id = ?", (run["chat_id"],)).fetchone()
            if chat is None:
                raise HTTPException(status_code=404, detail="Chat not found")
            created, was_update = create_or_update_run_summary(
                conn,
                run_id=run_id,
                project_id=chat["project_id"],
                summary=body.summary or "",
                changes=body.changes or "",
                tests=body.tests or "",
                next_actions=(body.next_actions or ""),
            )
            if was_update:
                from fastapi import Response
                return Response(status_code=200, content=RunSummaryResponse(**created).model_dump_json(), media_type="application/json")
            return RunSummaryResponse(**created)

    @app.get("/api/runs/{run_id}/summary")
    def read_run_summary(run_id: int):
        with get_conn() as conn:
            _require_run_with_artifacts(conn, run_id)
            summary = get_run_summary(conn, run_id)
            if summary is None:
                raise HTTPException(status_code=404, detail="No summary for this run")
            return RunSummaryResponse(**summary)

    @app.get("/api/projects/{project_id}/run-summaries")
    def list_run_summaries(project_id: int):
        with get_conn() as conn:
            _require_project(conn, project_id)
            summaries = list_run_summaries_by_project(conn, project_id)
            return [RunSummaryResponse(**s) for s in summaries]

    @app.post("/api/context/classify")
    def classify_context(body: ContextClassifyRequest):
        """Preview which context tier a prompt would get. Used by the UI badge.

        Deterministic and LLM-free, so it is safe to call on every keystroke.
        """
        decision = classify_context_tier(body.prompt, override=body.context_tier)
        return {
            "tier": decision["tier"],
            "budget_tokens": decision["budget_tokens"],
            "override": decision["override"],
            "reason": decision["reason"],
            "signals": decision["signals"],
            "tiers": list(TIER_SEQUENCE),
        }

    @app.get("/api/context/tier-stats")
    def context_tier_stats(limit: int = 100):
        """Observed tier distribution (dry-run data for tuning thresholds)."""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM run_events
                   WHERE type = 'context_tier_selected'
                   ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        counts: dict[str, int] = {}
        overrides = 0
        trimmed = 0
        total_chars = 0
        samples: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            tier = str(payload.get("tier") or "?")
            counts[tier] = counts.get(tier, 0) + 1
            overrides += 1 if payload.get("override") else 0
            trimmed += 1 if payload.get("trimmed") else 0
            total_chars += int(payload.get("rendered_chars") or 0)
            if len(samples) < 20:
                samples.append(payload)
        sampled = sum(counts.values())
        return {
            "sampled_runs": sampled,
            "counts": counts,
            "override_count": overrides,
            "trimmed_count": trimmed,
            "avg_rendered_tokens": (total_chars // 4 // sampled) if sampled else 0,
            "samples": samples,
        }

    @app.get("/api/runs/{run_id}/artifacts")
    def get_run_artifacts(run_id: int):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
        return {"artifacts": list_artifacts(run["artifact_path"])}

    @app.get("/api/runs/{run_id}/artifact-versions/{artifact_name:path}")
    def get_artifact_versions(run_id: int, artifact_name: str, version: str | None = None):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
        if artifact_name not in WRITABLE_ARTIFACTS:
            raise HTTPException(status_code=403, detail="Artifact is not versioned")
        if version:
            path = _safe_artifact_version_path(run["artifact_path"], artifact_name, version)
            if not path.exists() or not path.is_file():
                raise HTTPException(status_code=404, detail="Artifact version not found")
            return {"artifact": artifact_name, "version": version, "content": path.read_text(encoding="utf-8")}
        try:
            return {"artifact": artifact_name, "versions": list_artifact_versions(run["artifact_path"], artifact_name)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name:path}")
    def read_run_artifact(run_id: int, artifact_name: str):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
        path = _safe_artifact_path(run["artifact_path"], artifact_name)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return {"name": path.name, "content": path.read_text(encoding="utf-8")}

    @app.put("/api/runs/{run_id}/artifacts/{artifact_name:path}")
    def update_run_artifact(run_id: int, artifact_name: str, body: ArtifactUpdate):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            if artifact_name not in WRITABLE_ARTIFACTS:
                raise HTTPException(status_code=403, detail="Artifact is read-only")
            path = _safe_artifact_path(run["artifact_path"], artifact_name)
            if not path.exists() or not path.is_file():
                raise HTTPException(status_code=404, detail="Artifact not found")
            try:
                version = save_artifact_version(run["artifact_path"], artifact_name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            path.write_text(body.content, encoding="utf-8")
            _record_run_event(
                conn,
                run_id,
                "artifact_updated",
                {"name": path.name, "size": path.stat().st_size, "version_saved": version["id"]},
                run["artifact_path"],
            )
        return {"name": path.name, "size": path.stat().st_size, "version_saved": version}

    @app.post("/api/runs/{run_id}/approve")
    def approve_run(run_id: int, body: StageAdvanceRequest = StageAdvanceRequest()):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            updates = {"status": "approved"}
            if run.get("current_stage") == "review":
                updates["current_stage"] = "final_output"
                updates["stage_status"] = "active"
                conn.execute(
                    "UPDATE runs SET status = ?, current_stage = ?, stage_status = ? WHERE id = ?",
                    (updates["status"], updates["current_stage"], updates["stage_status"], run_id),
                )
                _record_run_event(
                    conn,
                    run_id,
                    "stage_advanced",
                    {"from": "review", "to": "final_output", "note": body.note or "approved"},
                    run["artifact_path"],
                )
                _record_run_event(
                    conn,
                    run_id,
                    "stage_started",
                    {"stage": "final_output", "label": STAGE_LABELS["final_output"]},
                    run["artifact_path"],
                )
            else:
                conn.execute("UPDATE runs SET status = ? WHERE id = ?", ("approved", run_id))
            _record_run_event(conn, run_id, "approved", {"note": body.note}, run["artifact_path"])
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row_to_dict(row)

    @app.post("/api/runs/{run_id}/rerun-stage")
    def rerun_stage(run_id: int, body: RerunStageRequest):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            conn.execute(
                "UPDATE runs SET status = ?, current_stage = ?, stage_status = ?, error = NULL, finished_at = NULL WHERE id = ?",
                ("queued", body.stage, "active", run_id),
            )
            _record_run_event(
                conn,
                run_id,
                "rerun_stage_requested",
                {"stage": body.stage, "reason": body.reason},
                run["artifact_path"],
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return row_to_dict(row)

    @app.post("/api/runs/{run_id}/runtime/start")
    def start_runtime(run_id: int, body: RuntimeStartRequest = RuntimeStartRequest()):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            result = _start_runtime_bridge_background(conn, app.state.db_path, run, app.state.runtime_command, body.timeout_seconds)
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            updated = row_to_dict(row)
            return JSONResponse(status_code=202, content={"run": updated, "runtime": result, "stages": _stage_progress(updated)})

    @app.get("/api/runs/{run_id}/token-refresh")
    def get_token_refresh_status(run_id: int):
        with get_conn() as conn:
            _require_run_with_artifacts(conn, run_id)
            row = conn.execute(
                "SELECT waiting_for_tokens, token_refresh_at, token_refresh_seconds FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Run not found")
            waiting = bool(row["waiting_for_tokens"])
            refresh_at = row["token_refresh_at"]
            refresh_seconds = row["token_refresh_seconds"] or 0
            seconds_remaining = 0
            if waiting and refresh_at:
                from datetime import datetime, timezone
                try:
                    refresh_dt = datetime.fromisoformat(refresh_at)
                    delta = (refresh_dt - datetime.now(timezone.utc)).total_seconds()
                    seconds_remaining = max(0, int(delta))
                except (TypeError, ValueError):
                    seconds_remaining = 0
            return RunTokenRefreshResponse(
                run_id=run_id,
                waiting_for_tokens=waiting,
                token_refresh_at=refresh_at,
                token_refresh_seconds=refresh_seconds,
                seconds_remaining=seconds_remaining,
                auto_resume_enabled=bool(os.getenv("HELM_TOKEN_AUTO_RESUME", "0") not in {"0", "false", "False", "", "off"}),
            )

    @app.post("/api/runs/{run_id}/token-refresh/retry")
    def retry_after_token_refresh(run_id: int, body: RuntimeStartRequest = RuntimeStartRequest()):
        with get_conn() as conn:
            run = _require_run_with_artifacts(conn, run_id)
            status = str(run.get("status") or "").lower()
            if status != "waiting_for_tokens":
                raise HTTPException(status_code=409, detail="Run is not waiting for token refresh")
            result = _start_runtime_bridge_background(conn, app.state.db_path, run, app.state.runtime_command, body.timeout_seconds)
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            updated = row_to_dict(row)
            return JSONResponse(status_code=202, content={"run": updated, "runtime": result, "stages": _stage_progress(updated)})

    @app.on_event("startup")
    def _start_token_refresh_monitor() -> None:
        if os.getenv("HELM_TOKEN_AUTO_RESUME", "0") in {"1", "true", "True", "on"}:
            monitor = threading.Thread(
                target=_token_refresh_monitor,
                args=(Path(app.state.db_path), app.state.runtime_command),
                kwargs={"interval_seconds": 30},
                daemon=True,
                name="helm-token-refresh-monitor",
            )
            monitor.start()

    return app


def _require_chat(conn, chat_id: int) -> dict:
    row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return row_to_dict(row)


_EVERYDAY_SELECT = """
SELECT everyday_items.*, projects.name AS project_name, projects.slug AS project_slug,
       project_goals.title AS goal_title, project_goals.horizon AS goal_horizon
FROM everyday_items
LEFT JOIN projects ON projects.id = everyday_items.project_id
LEFT JOIN project_goals ON project_goals.id = everyday_items.goal_id
"""

_GOAL_SELECT = """
SELECT project_goals.*, goal_visions.title AS vision_title, parent.title AS parent_goal_title
FROM project_goals
LEFT JOIN goal_visions ON goal_visions.id = project_goals.vision_id
LEFT JOIN project_goals AS parent ON parent.id = project_goals.parent_goal_id
"""

_DAILY_PLAN_ITEM_SELECT = """
SELECT daily_plan_items.*, project_goals.title AS goal_title, everyday_items.title AS everyday_title
FROM daily_plan_items
LEFT JOIN project_goals ON project_goals.id = daily_plan_items.goal_id
LEFT JOIN everyday_items ON everyday_items.id = daily_plan_items.everyday_item_id
"""


def _everyday_item_to_dict(row) -> dict:
    return row_to_dict(row)


def _validate_iso_date(value: str) -> str:
    from datetime import date

    try:
        date.fromisoformat(value or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD")
    return value


def _reject_null_fields(fields: dict, names: set[str]) -> None:
    null_fields = sorted(name for name in names if name in fields and fields[name] is None)
    if null_fields:
        raise HTTPException(status_code=400, detail=f"Fields cannot be null: {', '.join(null_fields)}")


def _require_goal_vision(conn, vision_id: int) -> dict:
    row = conn.execute("SELECT * FROM goal_visions WHERE id = ?", (vision_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Goal vision not found")
    return row_to_dict(row)


def _require_project_goal(conn, goal_id: int) -> dict:
    row = conn.execute("SELECT * FROM project_goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project goal not found")
    return row_to_dict(row)


def _validate_project_goal_links(conn, project_id: int, vision_id: int | None, parent_goal_id: int | None, goal_id: int | None = None) -> None:
    if vision_id is not None:
        _require_goal_vision(conn, vision_id)
    if parent_goal_id is not None:
        if goal_id is not None and parent_goal_id == goal_id:
            raise HTTPException(status_code=400, detail="Goal cannot be its own parent")
        parent = _require_project_goal(conn, parent_goal_id)
        if parent["project_id"] != project_id:
            raise HTTPException(status_code=400, detail="Parent goal must belong to the same project")
        if goal_id is not None:
            seen = {goal_id}
            current_parent = parent
            while current_parent.get("parent_goal_id") is not None:
                next_parent_id = current_parent["parent_goal_id"]
                if next_parent_id in seen:
                    raise HTTPException(status_code=400, detail="Goal parent cycle is not allowed")
                seen.add(next_parent_id)
                current_parent = _require_project_goal(conn, next_parent_id)
                if current_parent["project_id"] != project_id:
                    raise HTTPException(status_code=400, detail="Parent goal chain must belong to the same project")


def _project_goal_to_dict(conn, goal_id: int) -> dict:
    row = conn.execute(_GOAL_SELECT + " WHERE project_goals.id = ?", (goal_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project goal not found")
    return row_to_dict(row)


def _get_or_create_daily_plan(conn, date: str) -> dict:
    row = conn.execute("SELECT * FROM daily_plans WHERE date = ?", (date,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO daily_plans(date) VALUES (?)", (date,))
        row = conn.execute("SELECT * FROM daily_plans WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_dict(row)


def _validate_daily_plan_links(conn, goal_id: int | None, everyday_item_id: int | None) -> None:
    if goal_id is not None:
        _require_project_goal(conn, goal_id)
    if everyday_item_id is not None:
        row = conn.execute("SELECT id FROM everyday_items WHERE id = ?", (everyday_item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Everyday item not found")


def _daily_plan_item_to_dict(row) -> dict:
    return row_to_dict(row)


def _daily_plan_payload(conn, plan: dict) -> dict:
    items = [
        _daily_plan_item_to_dict(row)
        for row in conn.execute(
            _DAILY_PLAN_ITEM_SELECT + " WHERE daily_plan_items.daily_plan_id = ? ORDER BY daily_plan_items.sort_order, daily_plan_items.id",
            (plan["id"],),
        )
    ]
    return {"plan": plan, "items": items}


def _everyday_payload(conn) -> dict:
    rows = [
        _everyday_item_to_dict(row)
        for row in conn.execute(
            _EVERYDAY_SELECT + " WHERE everyday_items.status = 'open' ORDER BY everyday_items.priority, everyday_items.id"
        )
    ]
    buckets = {bucket: [] for bucket in ["today", "inbox", "waiting", "later"]}
    for row in rows:
        buckets.setdefault(row["bucket"], []).append(row)
    open_loops = [row for row in rows if row["bucket"] in {"today", "waiting"}]
    next_item = None
    for bucket in ["today", "inbox", "waiting", "later"]:
        if buckets.get(bucket):
            next_item = buckets[bucket][0]
            break
    next_action = (
        {"title": next_item["title"], "source": next_item["bucket"], "item_id": next_item["id"]}
        if next_item
        else {"title": "Zachyť prvú vec do Inboxu", "source": "empty"}
    )
    return {
        "today": buckets["today"],
        "inbox": buckets["inbox"],
        "waiting": buckets["waiting"],
        "later": buckets["later"],
        "open_loops": open_loops,
        "next_action": next_action,
    }




TELEGRAM_INBOX_STATUSES = {"new", "triaged", "captured", "routed", "archived"}
TELEGRAM_OUTBOX_STATUSES = {"draft", "ready", "cancelled", "archived"}


def _metadata_dict(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _telegram_inbox_to_dict(row) -> dict:
    item = row_to_dict(row)
    item["metadata"] = _metadata_dict(item.pop("metadata_json", "{}"))
    return item


def _telegram_outbox_to_dict(row) -> dict:
    item = row_to_dict(row)
    item["metadata"] = _metadata_dict(item.pop("metadata_json", "{}"))
    item["external_send"] = "blocked_until_explicit_approval"
    return item


def _require_telegram_inbox(conn, inbox_id: int) -> dict:
    row = conn.execute("SELECT * FROM telegram_inbox WHERE id = ?", (inbox_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Telegram inbox item not found")
    return row_to_dict(row)


def _require_telegram_outbox(conn, outbox_id: int) -> dict:
    row = conn.execute("SELECT * FROM telegram_outbox WHERE id = ?", (outbox_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Telegram outbox item not found")
    return row_to_dict(row)


def _telegram_sync_payload(conn) -> dict:
    inbox = [
        _telegram_inbox_to_dict(row)
        for row in conn.execute("SELECT * FROM telegram_inbox WHERE status != 'archived' ORDER BY id DESC LIMIT 8")
    ]
    outbox = [
        _telegram_outbox_to_dict(row)
        for row in conn.execute("SELECT * FROM telegram_outbox WHERE status != 'archived' ORDER BY id DESC LIMIT 8")
    ]
    interval_raw = _app_setting(conn, "telegram_sync_interval_seconds", "300")
    try:
        interval_seconds = int(interval_raw)
    except ValueError:
        interval_seconds = 300
    auto_sync = {
        "enabled": _bool_setting(conn, "telegram_auto_sync_enabled", True),
        "read_allowed": _bool_setting(conn, "telegram_read_allowed", True),
        "auto_send_allowed": False,
        "interval_seconds": interval_seconds,
        "last_sync_at": _app_setting(conn, "telegram_last_sync_at", ""),
        "last_sync_status": _app_setting(conn, "telegram_last_sync_status", "idle"),
        "last_sync_error": _app_setting(conn, "telegram_last_sync_error", ""),
        "source": _app_setting(conn, "telegram_last_source", "hermes_state_db"),
    }
    return {
        "mode": "practical_autonomy_read_only",
        "external_send": "blocked_until_explicit_approval",
        "auto_sync": auto_sync,
        "inbox": inbox,
        "outbox": outbox,
        "counts": {
            "inbox_new": conn.execute("SELECT COUNT(*) AS c FROM telegram_inbox WHERE status = 'new'").fetchone()["c"],
            "outbox_draft": conn.execute("SELECT COUNT(*) AS c FROM telegram_outbox WHERE status = 'draft'").fetchone()["c"],
        },
    }


def _memory_item_to_dict(row) -> dict:
    item = row_to_dict(row)
    item["metadata"] = _metadata_dict(item.pop("metadata_json", "{}"))
    return item


def _require_memory_item(conn, memory_id: int) -> dict:
    row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Memory item not found")
    return row_to_dict(row)


def _validate_memory_links(conn, project_id: int | None, chat_id: int | None) -> None:
    project = _require_project(conn, project_id) if project_id is not None else None
    chat = _require_chat(conn, chat_id) if chat_id is not None else None
    if project is not None and chat is not None and chat["project_id"] != project["id"]:
        raise HTTPException(status_code=400, detail="Chat must belong to selected project")


def _team_workflow_step_to_dict(row) -> dict:
    return row_to_dict(row)


def _team_workflow_to_dict(conn, row) -> dict:
    item = row_to_dict(row)
    item["steps"] = [
        _team_workflow_step_to_dict(step)
        for step in conn.execute("SELECT * FROM team_workflow_steps WHERE workflow_id = ? ORDER BY sort_order, id", (item["id"],))
    ]
    return item


def _require_team_workflow(conn, workflow_id: int) -> dict:
    row = conn.execute("SELECT * FROM team_workflows WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Team workflow not found")
    return row_to_dict(row)


def _require_team_workflow_step(conn, workflow_id: int, step_id: int) -> dict:
    row = conn.execute("SELECT * FROM team_workflow_steps WHERE id = ? AND workflow_id = ?", (step_id, workflow_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Team workflow step not found")
    return row_to_dict(row)


def _build_run_telegram_report(conn, run: dict, include_artifact_path: bool = True) -> str:
    events = [
        row_to_dict(row)
        for row in conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT 5", (run["id"],))
    ]
    prompt = (run.get("prompt") or "").strip()
    if len(prompt) > 140:
        prompt = prompt[:137] + "..."
    lines = [
        "🧭 Helm run report",
        f"Run: #{run['id']}",
        f"Status: {run.get('status') or '-'} · Stage: {run.get('current_stage') or '-'} / {run.get('stage_status') or '-'}",
    ]
    if prompt:
        lines.append(f"Prompt: {prompt}")
    if run.get("error"):
        lines.append(f"Error: {run['error']}")
    if include_artifact_path and run.get("artifact_path"):
        lines.append(f"Artifacts: {run['artifact_path']}")
    if events:
        lines.append("Latest events:")
        for event in reversed(events):
            lines.append(f"- {event['type']}")
    lines.extend(["", "Local draft only — external Telegram send requires explicit approval."])
    return "\n".join(lines)

def _normalize_project_roots(conn, workspace_root: Path) -> None:
    workspace_root = Path(workspace_root).expanduser().resolve()
    for row in conn.execute("SELECT * FROM projects ORDER BY id"):
        project = row_to_dict(row)
        root = str(project.get("root_path") or "")
        safe = False
        if root:
            try:
                resolved = Path(root).expanduser().resolve()
                safe = resolved == workspace_root or workspace_root in resolved.parents
            except OSError:
                safe = False
        if safe:
            continue
        new_root = create_project_workspace(workspace_root, project["slug"], project["name"], project["mode_default"])
        conn.execute("UPDATE projects SET root_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (str(new_root), project["id"]))


def _require_project(conn, project_id: int) -> dict:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row_to_dict(row)


def _project_context_path(project: dict) -> Path:
    root = Path(project["root_path"]).expanduser().resolve()
    path = (root / "CONTEXT.md").resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid project context path")
    return path


def _read_project_context(project: dict) -> str:
    path = _project_context_path(project)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _validate_project_file_path(project: dict, path: str, kind: str) -> str:
    if kind == "url":
        if path.startswith(("http://", "https://")):
            return path
        raise HTTPException(status_code=400, detail="Invalid project URL")
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise HTTPException(status_code=400, detail="Invalid project file path")
    root = Path(project["root_path"]).expanduser().resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid project file path")
    return candidate.relative_to(root).as_posix()


def _project_file_to_dict(row) -> dict:
    data = row_to_dict(row)
    data["pinned"] = bool(data.get("pinned"))
    return data


def _stage_defaults(conn, project_id: int) -> list[dict]:
    rows = {row["stage"]: row_to_dict(row) for row in conn.execute("SELECT * FROM project_stage_defaults WHERE project_id = ?", (project_id,))}
    return [
        rows.get(stage, {"project_id": project_id, "stage": stage, "instructions": "", "artifact_template": ""})
        for stage in STAGE_SEQUENCE
    ]


def _project_workspace_payload(conn, project: dict) -> dict:
    project_id = project["id"]
    context_path = _project_context_path(project)
    context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    run_history = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT runs.*, chats.title AS chat_title
            FROM runs
            JOIN chats ON chats.id = runs.chat_id
            WHERE chats.project_id = ?
            ORDER BY runs.id DESC
            LIMIT 20
            """,
            (project_id,),
        )
    ]
    return {
        "project": project,
        "context": {"path": str(context_path), "content": context},
        "goals": [row_to_dict(row) for row in conn.execute(_GOAL_SELECT + " WHERE project_goals.project_id = ? ORDER BY project_goals.id", (project_id,))],
        "tasks": [row_to_dict(row) for row in conn.execute("SELECT * FROM project_tasks WHERE project_id = ? ORDER BY id", (project_id,))],
        "files": [_project_file_to_dict(row) for row in conn.execute("SELECT * FROM project_files WHERE project_id = ? ORDER BY id", (project_id,))],
        "notes": [row_to_dict(row) for row in conn.execute("SELECT * FROM project_notes WHERE project_id = ? ORDER BY id", (project_id,))],
        "stage_defaults": _stage_defaults(conn, project_id),
        "run_summaries": list_run_summaries_by_project(conn, project_id, limit=10),
        "run_history": run_history,
    }


def _app_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else row["value"]


def _set_app_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )


def _bool_setting(conn, key: str, default: bool = False) -> bool:
    value = _app_setting(conn, key, "1" if default else "0")
    return value not in {"0", "false", "False", "off", ""}


def _settings_payload(conn) -> dict:
    theme_id = _app_setting(conn, "theme_id", "helm-default")
    if theme_id not in {"helm-default", "helm-rpg"}:
        theme_id = "helm-default"
    return {
        "safety_policy_enabled": _bool_setting(conn, "safety_policy_enabled", True),
        "theme_id": theme_id,
        "themes": [
            {"id": "helm-default", "name": "Helm Default"},
            {"id": "helm-rpg", "name": "Helm RPG"},
        ],
    }


def _context_meter_payload(conn, chat_id: int, project: dict) -> dict:
    context = _read_project_context(project)
    messages = conn.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
    message_chars = sum(len(row["role"] or "") + len(row["content"] or "") for row in messages)
    used_chars = len(context) + message_chars
    limit_chars = int(os.getenv("HELM_CONTEXT_LIMIT_CHARS", "60000"))
    percent = 0 if used_chars == 0 else max(1, min(100, round((used_chars / limit_chars) * 100)))
    return {
        "used_chars": used_chars,
        "limit_chars": limit_chars,
        "percent": percent,
        "message_count": len(messages),
        "project_context_chars": len(context),
    }


def _context_packet_payload(
    conn,
    chat: dict,
    project: dict,
    *,
    packet_date: str,
    max_chars: int,
    include_recent_messages: bool,
    include_runs: bool,
) -> dict:
    max_chars = max(200, min(int(max_chars or 12000), 20000))
    sections = [
        _section("identity", "Identity", _identity_context(chat, project)),
        _section("project", "Project", _project_context(project)),
        _section("memory", "Persistent memory", _memory_context(conn, project["id"], chat["id"])),
        _section("team", "TEAM workflow", _team_context(conn, project["id"], chat["id"])),
        _section("goals", "Active goals", _goals_context(conn, project["id"])),
        _section("daily", "Today", _daily_context(conn, project["id"], packet_date)),
        _section(
            "recent_messages",
            "Recent messages",
            _recent_messages_context(conn, chat["id"]) if include_recent_messages else "Skipped by request.",
        ),
        _section("runs", "Recent runs", _recent_runs_context(conn, chat["id"]) if include_runs else "Skipped by request."),
        _section("safety", "Safety", _safety_context(conn, chat["id"])),
        _section("files", "Pinned files", _pinned_files_context(conn, project["id"])),
    ]
    sections = _cap_context_sections(sections, max_chars)
    markdown = _render_context_packet(sections)
    if len(markdown) > max_chars:
        marker = "\n\n[truncated to max_chars]"
        markdown = markdown[: max_chars - len(marker)].rstrip() + marker
    return {
        "chat_id": chat["id"],
        "project_id": project["id"],
        "generated_at": dt_date.today().isoformat(),
        "char_count": len(markdown),
        "max_chars": max_chars,
        "sections": sections,
        "packet_markdown": markdown,
    }


def _section(key: str, title: str, content: str) -> dict:
    return {"key": key, "title": title, "content": content.strip() or "None."}


def _short(value: str | None, limit: int = 500) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)].rstrip() + " …[truncated]"


def _identity_context(chat: dict, project: dict) -> str:
    return "\n".join(
        [
            f"Chat: {chat['title']} (id={chat['id']}, mode={chat['mode']})",
            f"Project: {project['name']} (id={project['id']}, mode={project['mode_default']})",
        ]
    )


def _project_context(project: dict) -> str:
    context = _short(_read_project_context(project), 1200)
    lines = [
        f"Name: {project['name']}",
        f"Slug: {project['slug']}",
        f"Root: {project['root_path']}",
    ]
    if project.get("description"):
        lines.append(f"Description: {_short(project['description'], 240)}")
    lines.append("CONTEXT.md excerpt: " + (context or "empty"))
    return "\n".join(lines)


def _memory_context(conn, project_id: int, chat_id: int) -> str:
    rows = [
        _memory_item_to_dict(row)
        for row in conn.execute(
            """
            SELECT * FROM memory_items
            WHERE status = 'active'
            AND (
                project_id = ? OR chat_id = ? OR scope IN ('global','user')
            )
            ORDER BY confidence DESC, id DESC
            LIMIT 10
            """,
            (project_id, chat_id),
        )
    ]
    if not rows:
        return "No active memory."
    lines = []
    for item in rows:
        target = item.get("scope") or "global"
        lines.append(f"- [{item['kind']}/{target}/c{item['confidence']}] {_short(item['content'], 260)}")
    return "\n".join(lines)


def _team_context(conn, project_id: int, chat_id: int) -> str:
    workflows = [
        _team_workflow_to_dict(conn, row)
        for row in conn.execute(
            """
            SELECT * FROM team_workflows
            WHERE status = 'active' AND project_id = ? AND (chat_id = ? OR chat_id IS NULL)
            ORDER BY id DESC
            LIMIT 5
            """,
            (project_id, chat_id),
        )
    ]
    if not workflows:
        return "No active TEAM workflow."
    lines = []
    for workflow in workflows:
        lines.append(f"- Workflow #{workflow['id']}: {workflow['title']} ({workflow['current_role']})")
        for step in workflow.get("steps", [])[:8]:
            lines.append(f"  - [{step['status']}] {step['role']}: {_short(step['title'], 180)}")
    return "\n".join(lines)


def _goals_context(conn, project_id: int) -> str:
    rows = [
        row_to_dict(row)
        for row in conn.execute(
            _GOAL_SELECT + " WHERE project_goals.project_id = ? AND project_goals.status = 'active' ORDER BY project_goals.id LIMIT 12",
            (project_id,),
        )
    ]
    if not rows:
        return "No active goals."
    lines: list[str] = []
    seen_visions: set[str] = set()
    for goal in rows:
        if goal.get("vision_title") and goal["vision_title"] not in seen_visions:
            seen_visions.add(goal["vision_title"])
            lines.append(f"Vision: {goal['vision_title']}")
        suffixes = []
        if goal.get("parent_goal_title"):
            suffixes.append(f"parent: {goal['parent_goal_title']}")
        if goal.get("target_date"):
            suffixes.append(f"target: {goal['target_date']}")
        line = f"- [{goal['horizon']}] {goal['title']}"
        if suffixes:
            line += " (" + "; ".join(suffixes) + ")"
        if goal.get("description"):
            line += f" — {_short(goal['description'], 180)}"
        lines.append(line)
    return "\n".join(lines)


def _daily_context(conn, project_id: int, packet_date: str) -> str:
    plan = conn.execute("SELECT * FROM daily_plans WHERE date = ?", (packet_date,)).fetchone()
    lines = [f"Date: {packet_date}"]
    if plan is None:
        lines.append("Focus: none")
    else:
        plan_dict = row_to_dict(plan)
        lines.append("Focus: " + (plan_dict.get("focus") or "none"))
        if plan_dict.get("notes"):
            lines.append("Notes: " + _short(plan_dict["notes"], 240))
        items = [
            row_to_dict(row)
            for row in conn.execute(
                _DAILY_PLAN_ITEM_SELECT
                + " WHERE daily_plan_items.daily_plan_id = ? AND daily_plan_items.status = 'open' ORDER BY daily_plan_items.sort_order, daily_plan_items.id LIMIT 8",
                (plan_dict["id"],),
            )
        ]
        for item in items:
            details = []
            if item.get("goal_title"):
                details.append(f"goal: {item['goal_title']}")
            if item.get("everyday_title"):
                details.append(f"everyday: {item['everyday_title']}")
            lines.append("- " + item["title"] + (" (" + "; ".join(details) + ")" if details else ""))
    today_items = [
        _everyday_item_to_dict(row)
        for row in conn.execute(
            _EVERYDAY_SELECT
            + " WHERE everyday_items.status = 'open' AND everyday_items.bucket = 'today' AND (everyday_items.project_id = ? OR everyday_items.project_id IS NULL) ORDER BY everyday_items.priority, everyday_items.id LIMIT 8",
            (project_id,),
        )
    ]
    if today_items:
        lines.append("Today bucket:")
        for item in today_items:
            goal = f" (goal: {item['goal_title']})" if item.get("goal_title") else ""
            lines.append(f"- {item['title']}{goal}")
    return "\n".join(lines)


def _recent_messages_context(conn, chat_id: int, limit: int = 8) -> str:
    rows = list(
        conn.execute(
            "SELECT role, content, created_at FROM messages WHERE chat_id = ? AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
    )
    rows.reverse()
    if not rows:
        return "No recent user/assistant messages."
    return "\n".join(f"- {row['role']}: {_short(row['content'], 500)}" for row in rows)


def _recent_runs_context(conn, chat_id: int, limit: int = 5) -> str:
    runs = [row_to_dict(row) for row in conn.execute("SELECT * FROM runs WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit))]
    if not runs:
        return "No recent runs."
    lines = []
    for run in runs:
        event_rows = [
            row["type"]
            for row in conn.execute("SELECT type FROM run_events WHERE run_id = ? ORDER BY id DESC LIMIT 4", (run["id"],))
        ]
        line = f"- Run #{run['id']}: status={run['status']}, stage={run['current_stage']}/{run['stage_status']}"
        if run.get("error"):
            line += f", error={_short(run['error'], 160)}"
        if run.get("artifact_path"):
            line += f", artifact_path={run['artifact_path']}"
        summary = get_run_summary(conn, run["id"])
        if summary:
            line += f", summary={_short(summary['summary'], 200)}"
            if summary.get("next_actions"):
                line += f", next={_short(summary['next_actions'], 100)}"
        if event_rows:
            line += ", latest_events=" + ",".join(event_rows)
        lines.append(line)
    return "\n".join(lines)


def _safety_context(conn, chat_id: int) -> str:
    safety = _settings_payload(conn)["safety_policy_enabled"]
    return "\n".join([f"Safety policy: {'ON' if safety else 'OFF'}", "Policy note: Ask before external action."])


def _pinned_files_context(conn, project_id: int) -> str:
    rows = [_project_file_to_dict(row) for row in conn.execute("SELECT * FROM project_files WHERE project_id = ? AND pinned = 1 ORDER BY id LIMIT 12", (project_id,))]
    if not rows:
        return "No pinned files."
    return "\n".join(f"- {row['path']} ({row['kind']}; label={row['label'] or 'none'})" for row in rows)


def _render_context_packet(sections: list[dict]) -> str:
    parts = ["# Helm Context Packet"]
    for section in sections:
        parts.append(f"\n## {section['title']}\n{section['content']}")
    return "\n".join(parts).strip()


def _cap_context_sections(sections: list[dict], max_chars: int) -> list[dict]:
    capped = [dict(section) for section in sections]
    marker = " …[truncated]"
    for _ in range(40):
        markdown = _render_context_packet(capped)
        if len(markdown) <= max_chars:
            return capped
        longest = max(capped, key=lambda section: len(section["content"]))
        if len(longest["content"]) <= len(marker) + 8:
            break
        overflow = len(markdown) - max_chars
        next_limit = max(len(marker) + 8, len(longest["content"]) - overflow - 20)
        longest["content"] = _short(longest["content"], next_limit)
    return capped


def _require_run_with_artifacts(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = row_to_dict(row)
    if not run.get("artifact_path"):
        raise HTTPException(status_code=404, detail="Run artifacts not found")
    return run


def _token_refresh_monitor(db_path: Path, runtime_command: tuple[str, ...], interval_seconds: int = 30) -> None:
    """Background thread: auto-resume runs waiting for token refresh."""
    import time
    while True:
        time.sleep(interval_seconds)
        try:
            _process_token_refresh_waiters(db_path, runtime_command)
        except Exception:
            pass


def _process_token_refresh_waiters(db_path: Path, runtime_command: tuple[str, ...]) -> None:
    from datetime import datetime, timezone
    with connect(db_path) as conn:
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            """
            SELECT id, chat_id, artifact_path, token_refresh_at FROM runs
            WHERE waiting_for_tokens = 1 AND token_refresh_at IS NOT NULL AND token_refresh_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            run = row_to_dict(row)
            status = str(run.get("status") or "").lower()
            if status != "waiting_for_tokens":
                continue
            if run.get("artifact_path"):
                artifact_path = Path(run["artifact_path"])
                prompt = run.get("prompt") or ""
                input_path = artifact_path / "input.md"
                runtime_prompt = (
                    f"Token quota refreshed — resuming Helm runtime. "
                    f"Original prompt: {prompt[:500]}. "
                    f"Read `{input_path}` for full context and continue where you left off."
                )
                try:
                    subprocess.Popen(
                        [*runtime_command, runtime_prompt],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=str(artifact_path),
                    )
                    conn.execute(
                        "UPDATE runs SET waiting_for_tokens = 0, token_refresh_at = NULL, status = 'running', current_stage = 'execute', stage_status = 'active', error = NULL WHERE id = ?",
                        (run["id"],),
                    )
                except OSError:
                    pass


def _validated_stage(stage: str) -> str:
    if stage not in STAGE_SEQUENCE:
        raise HTTPException(status_code=400, detail="Invalid stage")
    return stage


def _start_runtime_bridge_background(
    conn,
    db_path: str | Path,
    run: dict,
    command: Sequence[str],
    timeout_seconds: int,
) -> dict[str, object]:
    if not command:
        raise HTTPException(status_code=503, detail="No agent runtime configured; set HELM_RUNTIME_COMMAND.")
    run_id = run["id"]
    status = str(run.get("status") or "").lower()
    if status == "running":
        raise HTTPException(status_code=409, detail="Runtime is already running for this run")
    if status in {"review", "completed", "approved"}:
        raise HTTPException(status_code=409, detail="Run runtime has already completed or the run is finalized")
    artifact_path = Path(run["artifact_path"])
    conn.execute(
        "UPDATE runs SET status = ?, current_stage = ?, stage_status = ?, error = NULL WHERE id = ?",
        ("running", "execute", "active", run_id),
    )
    _record_run_event(
        conn,
        run_id,
        "runtime_started",
        {"command": command[0], "command_argv": list(command), "timeout_seconds": timeout_seconds, "stage": "execute", "background": True},
        str(artifact_path),
    )
    _record_run_event(conn, run_id, "stage_started", {"stage": "execute", "label": STAGE_LABELS["execute"]}, str(artifact_path))
    conn.commit()

    worker = threading.Thread(
        target=_run_runtime_bridge_worker,
        args=(Path(db_path), run_id, list(command), timeout_seconds),
        daemon=True,
        name=f"helm-runtime-{run_id}",
    )
    worker.start()
    return {"started": True, "background": True, "run_id": run_id, "timeout_seconds": timeout_seconds}


def _run_runtime_bridge_worker(db_path: Path, run_id: int, command: Sequence[str], timeout_seconds: int) -> None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return
        run = row_to_dict(row)
        if str(run.get("status") or "").lower() != "running":
            return
        result = _execute_runtime_process(run, command, timeout_seconds)
        _finalize_runtime_bridge(conn, run, result)


def _runtime_launcher_prompt(input_path: Path) -> str:
    return (
        "You are Bob running inside Helm. Before answering, read the complete Helm runtime input "
        f"from `HELM_INPUT_PATH` (`{input_path}`) and follow that file as the source of truth. "
        "It contains the user prompt, project context, context navigation maps (including HELM_SYSTEM_MAP), "
        "hybrid retrieved source chunks, file/API/UI indexes, token/chunking rules, workflow governance, runtime contracts, and policies. "
        "Append safe progress events to HELM_RUNTIME_EVENTS_JSONL (thought_summary/tool_activity/verification/result) when useful; never raw chain-of-thought. "
        "Do not rely only on this launcher prompt."
    )


def _execute_runtime_process(run: dict, command: Sequence[str], timeout_seconds: int) -> dict[str, object]:
    run_id = run["id"]
    artifact_path = Path(run["artifact_path"])
    runtime_events_path = artifact_path / "runtime-events.jsonl"
    prompt = run.get("prompt") or ""
    input_path = artifact_path / "input.md"
    runtime_prompt = _runtime_launcher_prompt(input_path)
    env = os.environ.copy()
    env.update(
        {
            "HELM_RUN_ID": str(run_id),
            "HELM_RUN_MODE": run.get("mode") or "work",
            "HELM_RUN_PROMPT": prompt,
            "HELM_ARTIFACT_PATH": str(artifact_path),
            "HELM_INPUT_PATH": str(input_path),
            "HELM_RUN_CONTRACT_PATH": str(artifact_path / "run-contract.md"),
            "HELM_RUNTIME_EVENTS_JSONL": str(runtime_events_path),
        }
    )
    stdout_log = artifact_path / "runtime-stdout.log"
    chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            [*command, runtime_prompt],
            cwd=str(artifact_path),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
    except OSError as exc:
        return {"exit_code": None, "stdout": "", "stderr": str(exc), "timed_out": False}

    def _drain_stderr() -> None:
        # Must run concurrently: stderr is a pipe with a ~64KB kernel buffer.
        # Reading it only after the stdout loop deadlocks a child that is
        # verbose on stderr (AI runtimes routinely are).
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                stderr_chunks.append(line)
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    timeout_flag: list[bool] = [False]

    def _on_deadline() -> None:
        # The stdout iterator blocks until the next line arrives, so an
        # in-loop deadline check cannot catch a child that stalls after
        # printing. A watchdog kill closes the pipes and unblocks the loop.
        if proc.poll() is None:
            timeout_flag[0] = True
            try:
                proc.kill()
            except OSError:
                pass

    watchdog = threading.Timer(max(1.0, float(timeout_seconds or 1)), _on_deadline)
    watchdog.daemon = True
    watchdog.start()
    stderr_log = artifact_path / "runtime-stderr.log"
    try:
        # Tee stdout + stderr to disk line-by-line so /api/runs/{id}/live and
        # /api/runs/{id}/terminal can stream them while the run is still executing.
        with stdout_log.open("w", encoding="utf-8") as log_handle:
            if proc.stdout is not None:
                for line in proc.stdout:
                    chunks.append(line)
                    log_handle.write(line)
                    log_handle.flush()
        # Write the concurrently-drained stderr to its own log for the terminal.
        if stderr_chunks:
            with stderr_log.open("w", encoding="utf-8") as err_handle:
                err_handle.write("".join(stderr_chunks))
        proc.wait()
    except OSError as exc:
        stderr_chunks.append(str(exc))
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        stderr_thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    timed_out = timeout_flag[0]
    return {
        "exit_code": None if timed_out else proc.returncode,
        "stdout": "".join(chunks),
        "stderr": "".join(stderr_chunks),
        "timed_out": timed_out,
    }


TEST_RESULT_PATTERNS = (
    re.compile(r"\b(\d+)\s+passed(?:[^\n]*?\b(\d+)\s+failed)?[^\n]*", re.IGNORECASE),
    re.compile(r"\bTests?:\s*(\d+)\s+passed[^\n]*", re.IGNORECASE),
    re.compile(r"\bok\s+(\d+)\s+tests?\b[^\n]*", re.IGNORECASE),
)

_SUMMARY_NEXT_HINTS = re.compile(
    r"^\s*(?:next steps?|next|todo|remaining|ďalšie kroky|next actions?)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_test_results(text: str) -> str:
    """Pull a pytest/jest-style result line out of runtime stdout. Deterministic, no LLM."""
    if not text:
        return ""
    for pattern in TEST_RESULT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()[:300]
    return ""


def _summarise_runtime_stdout(text: str, limit: int = 600) -> str:
    """Prefer the agent's own closing lines; they read as a human summary."""
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    tail = lines[-12:]
    summary = "\n".join(tail).strip()
    if len(summary) > limit:
        summary = summary[: limit - 1].rstrip() + "…"
    return summary


def _git_head_commit() -> str:
    """Current HEAD of the Helm dev repo, or '' when unavailable.

    Recorded at run creation so a run's changes can later be diffed against the
    state the run actually started from.
    """
    repo = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:  # pragma: no cover - baseline capture is best-effort
        pass
    return ""


def _run_scoped_changed_files(base_commit: str | None) -> list[str] | None:
    """Files changed since `base_commit`, or None when it cannot be determined.

    Returning None (rather than an empty list) is deliberate: it distinguishes
    "this run changed nothing" from "we have no idea what this run changed".
    Callers must not present the latter as a file list -- that is how the M19
    backfill ended up attributing the developer's dirty working tree to 10 runs.
    """
    if not base_commit:
        return None
    repo = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", base_commit],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:  # pragma: no cover - diff is best-effort context
        return None


def _auto_run_summary(conn, run: dict, result: dict[str, object]) -> None:
    """Generate a run summary from artifacts already on disk.

    Deliberately LLM-free: everything here is harvested from data the run
    already produced (stdout, diff, tool activity). Never overwrites a summary
    that already exists, so a human-authored summary always wins and repeated
    finalisation stays idempotent.
    """
    run_id = run["id"]
    try:
        if get_run_summary(conn, run_id) is not None:
            return
        chat = conn.execute("SELECT project_id FROM chats WHERE id = ?", (run["chat_id"],)).fetchone()
        if chat is None:
            return

        # Scrub BEFORE summarising: summaries are persisted and re-injected into
        # later prompts, so an unredacted secret would propagate across runs.
        stdout = _scrub_secrets(str(result.get("stdout") or ""))

        tool_count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND type = 'tool_activity'",
            (run_id,),
        ).fetchone()[0]

        # M20 guard: require evidence the runtime actually produced something.
        # A run that never dispatched has no stdout and no tool activity; its
        # artifacts are still untouched templates ("_TODO: vystup stage._").
        # Summarising those persists a fabricated result which then gets
        # re-injected into later prompts as project context.
        if not stdout.strip() and not tool_count:
            return

        summary_text = _summarise_runtime_stdout(stdout) or _scrub_secrets(
            (run.get("prompt") or "").strip()[:600]
        )
        tests = _extract_test_results(stdout)

        changes = ""
        # Scope to what THIS run changed, using the baseline captured at run
        # creation. None means no baseline (legacy run) -> report no file list
        # rather than falling back to a repo-wide `git diff HEAD`, which would
        # attribute unrelated working-tree edits to this run.
        changed_files = _run_scoped_changed_files(run.get("base_commit"))
        if changed_files:
            names = changed_files[:8]
            changes = f"{len(changed_files)} file(s): " + ", ".join(names)
            if len(changed_files) > 8:
                changes += f", +{len(changed_files) - 8} more"

        tool_count = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND type = 'tool_activity'",
            (run_id,),
        ).fetchone()[0]
        if tool_count:
            changes = (changes + " | " if changes else "") + f"{tool_count} tool call(s)"

        next_actions = ""
        hint = _SUMMARY_NEXT_HINTS.search(stdout)
        if hint:
            next_actions = _scrub_secrets(hint.group(1).strip())[:300]

        create_or_update_run_summary(
            conn,
            run_id=run_id,
            project_id=chat["project_id"],
            summary=summary_text,
            changes=changes,
            tests=tests,
            next_actions=next_actions,
        )
        _record_run_event(
            conn,
            run_id,
            "run_summary_generated",
            {"auto": True, "has_tests": bool(tests), "has_changes": bool(changes)},
            str(run.get("artifact_path") or ""),
        )
    except Exception as exc:  # pragma: no cover - summary must never fail a run
        logger.warning("auto run summary failed for run %s: %s", run_id, exc)


_TEST_FAILURE_RE = re.compile(r"\b(\d+)\s+failed\b", re.IGNORECASE)
_ERROR_MARKERS = re.compile(
    r"(?i)\b(traceback \(most recent call last\)|fatal error|unhandled exception)\b"
)
# Signals that a run touched the outside world. External actions require
# explicit approval, so these never auto-complete.
_EXTERNAL_ACTION_RE = re.compile(
    r"(?i)\b(?:POST|PUT|DELETE|PATCH)\s+https?://"
    r"|\bgit\s+push\b"
    r"|\bcurl\b[^\n]*\bhttps?://"
    r"|\bsend_message\b|\bpublish\b|\bdeploy(?:ed|ing)?\s+to\s+prod"
)


def _assess_run_completion(result: dict[str, object]) -> dict[str, object]:
    """Decide whether a finished run is safe to auto-complete.

    Returns {"auto_complete": bool, "needs_attention": bool, "reason": str}.
    Conservative by design: anything ambiguous stays in review for a human.
    """
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    blob = f"{stdout}\n{stderr}"

    failures = _TEST_FAILURE_RE.search(blob)
    if failures and failures.group(1) != "0":
        return {
            "auto_complete": False,
            "needs_attention": True,
            "reason": f"{failures.group(1)} failing test(s) — needs your review",
        }
    if _ERROR_MARKERS.search(blob):
        return {
            "auto_complete": False,
            "needs_attention": True,
            "reason": "error/traceback in runtime output",
        }
    if _EXTERNAL_ACTION_RE.search(blob):
        return {
            "auto_complete": False,
            "needs_attention": True,
            "reason": "external action detected — approval required",
        }
    return {"auto_complete": True, "needs_attention": False, "reason": "clean run, no failures detected"}


def _finalize_runtime_bridge(conn, run: dict, result: dict[str, object]) -> None:
    run_id = run["id"]
    artifact_path = Path(run["artifact_path"])
    runtime_events_path = artifact_path / "runtime-events.jsonl"
    _write_runtime_output(artifact_path, run, result)
    _ingest_runtime_events(conn, run_id, runtime_events_path, str(artifact_path))
    if result["exit_code"] == 0 and not result["timed_out"]:
        conn.execute(
            "UPDATE runs SET status = ?, current_stage = ?, stage_status = ?, error = NULL, waiting_for_tokens = 0 WHERE id = ?",
            ("review", "review", "active", run_id),
        )
        assistant_message = _plain_assistant_message(result)
        if assistant_message:
            conn.execute(
                "INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)",
                (run["chat_id"], "assistant", assistant_message),
            )
        _record_run_event(
            conn,
            run_id,
            "runtime_completed",
            {
                "exit_code": 0,
                "stdout_size": len(str(result["stdout"])),
                "stderr_size": len(str(result["stderr"])),
                "assistant_message_saved": bool(assistant_message),
            },
            str(artifact_path),
        )
        _record_run_event(conn, run_id, "stage_started", {"stage": "review", "label": STAGE_LABELS["review"]}, str(artifact_path))
        _harvest_runtime_tool_events(conn, run_id, str(artifact_path))
        _auto_run_summary(conn, run, result)
        # M18: a clean run must finish on its own. Previously runs could park
        # in `review` waiting for a manual approve that never came.
        assessment = _assess_run_completion(result)
        if assessment["auto_complete"]:
            conn.execute(
                "UPDATE runs SET status = ?, current_stage = ?, stage_status = ?, needs_attention = 0, attention_reason = '' WHERE id = ?",
                ("completed", "final_output", "done", run_id),
            )
            _record_run_event(
                conn,
                run_id,
                "run_auto_completed",
                {"reason": assessment["reason"], "from_stage": "review"},
                str(artifact_path),
            )
            _record_run_event(
                conn,
                run_id,
                "stage_started",
                {"stage": "final_output", "label": STAGE_LABELS["final_output"]},
                str(artifact_path),
            )
            sse_broadcaster.broadcast_sync("run_completed", {
                "run_id": run_id, "status": "completed", "reason": assessment["reason"],
            })
        else:
            conn.execute(
                "UPDATE runs SET needs_attention = 1, attention_reason = ? WHERE id = ?",
                (str(assessment["reason"]), run_id),
            )
            _record_run_event(
                conn,
                run_id,
                "run_needs_attention",
                {"reason": assessment["reason"]},
                str(artifact_path),
            )
            sse_broadcaster.broadcast_sync("run_needs_attention", {
                "run_id": run_id, "reason": assessment["reason"],
            })
    elif _is_token_exhaustion_error(result):
        refresh_seconds = _get_token_refresh_seconds(60)
        refresh_at = _seconds_from_now_iso(refresh_seconds)
        conn.execute(
            "UPDATE runs SET status = ?, waiting_for_tokens = 1, token_refresh_seconds = ?, token_refresh_at = ?, error = NULL WHERE id = ?",
            ("waiting_for_tokens", refresh_seconds, refresh_at, run_id),
        )
        _record_run_event(
            conn,
            run_id,
            "runtime_waiting_for_tokens",
            {
                "exit_code": result.get("exit_code"),
                "timed_out": result.get("timed_out"),
                "refresh_seconds": refresh_seconds,
                "refresh_at": refresh_at,
                "pattern_matched": _matched_token_pattern(result),
            },
            str(artifact_path),
        )
        sse_broadcaster.broadcast_sync("run_waiting_for_tokens", {
            "run_id": run_id, "refresh_seconds": refresh_seconds, "refresh_at": refresh_at,
        })
    else:
        exit_code = result["exit_code"]
        error = "Runtime bridge timed out" if result["timed_out"] else f"Runtime bridge failed with exit code {exit_code}"
        conn.execute(
            "UPDATE runs SET status = ?, current_stage = ?, stage_status = ?, error = ?, waiting_for_tokens = 0 WHERE id = ?",
            ("failed", "execute", "failed", error, run_id),
        )
        _record_run_event(
            conn,
            run_id,
            "runtime_failed",
            {"exit_code": exit_code, "timed_out": result["timed_out"], "stderr_size": len(str(result["stderr"]))},
            str(artifact_path),
        )
        sse_broadcaster.broadcast_sync("run_failed", {
            "run_id": run_id, "error": error, "exit_code": exit_code, "timed_out": result["timed_out"],
        })


def _plain_assistant_message(result: dict[str, object]) -> str:
    stdout = str(result.get("stdout") or "").strip()
    if not stdout:
        return ""
    return stdout


RUNTIME_EVENT_TYPES = {"thought_summary", "reasoning_summary", "plan_note", "tool_call", "tool_activity", "verification", "result"}


def _ingest_runtime_events(conn, run_id: int, events_path: Path, artifact_path: str) -> None:
    if not events_path.exists() or not events_path.is_file():
        return
    offset_path = Path(artifact_path) / ".runtime-events.offset"
    try:
        offset = int(offset_path.read_text(encoding="utf-8").strip() or "0") if offset_path.exists() else 0
    except (OSError, ValueError):
        offset = 0
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            lines = []
            for _, line in zip(range(200), handle):
                lines.append(line[:10_000])
            new_offset = handle.tell()
    except OSError:
        return
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type not in RUNTIME_EVENT_TYPES:
            continue
        _record_run_event(conn, run_id, event_type, payload, artifact_path)
    try:
        offset_path.write_text(str(new_offset), encoding="utf-8")
    except OSError:
        pass


TOOL_SUMMARY_ARG_KEYS = ("path", "command", "pattern", "file_path", "url", "query", "goal")
# Values under these keys routinely carry secrets (API keys in shell commands,
# tokens in URLs, PII in prompts). Summaries are persisted and served over the
# events/live APIs, so redact rather than truncate.
TOOL_SUMMARY_REDACT_KEYS = frozenset({"command", "url", "query", "goal"})
TOOL_HARVEST_MAX_ROWS = 400
LIVE_TOOLS_RECENT = 12
LIVE_STDOUT_TAIL_BYTES = 8000
WORKSPACE_TREE_MAX_ENTRIES = 400
WORKSPACE_TREE_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv", "snapshots"}
DIFF_MAX_PATCH_BYTES = 40_000


def _workspace_entries(root: Path, target: Path, depth: int) -> list[dict[str, object]]:
    """List bounded directory entries under target, relative to root."""
    entries: list[dict[str, object]] = []
    visited: set[Path] = set()

    def walk(current: Path, level: int) -> None:
        if level > depth or len(entries) >= WORKSPACE_TREE_MAX_ENTRIES:
            return
        try:
            real = current.resolve()
        except OSError:
            return
        if real in visited:
            return  # symlink cycle guard
        visited.add(real)
        try:
            children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(entries) >= WORKSPACE_TREE_MAX_ENTRIES:
                return
            if child.name.startswith(".") or child.name in WORKSPACE_TREE_SKIP_DIRS:
                continue
            try:
                relative = str(child.relative_to(root))
            except ValueError:
                continue
            is_dir = child.is_dir()
            record: dict[str, object] = {
                "name": child.name,
                "path": relative,
                "type": "dir" if is_dir else "file",
                "depth": level,
            }
            if not is_dir:
                try:
                    record["size"] = child.stat().st_size
                except OSError:
                    record["size"] = 0
            entries.append(record)
            if is_dir:
                walk(child, level + 1)

    walk(target, 1)
    return entries


def _read_stdout_tail(artifact_path: str) -> str:
    """Return the tail of live runtime stdout, or the finished output.md fallback."""
    if not artifact_path:
        return ""
    base = Path(artifact_path)
    for name in ("runtime-stdout.log", "output.md"):
        candidate = base / name
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            size = candidate.stat().st_size
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                if size > LIVE_STDOUT_TAIL_BYTES:
                    handle.seek(size - LIVE_STDOUT_TAIL_BYTES)
                    handle.readline()
                return handle.read()
        except OSError:
            continue
    return ""


def _repo_diff_payload(run_id: int) -> dict[str, object]:
    """Return the CURRENT git working-tree diff for the Helm dev repo.

    Scope note: this is repo-wide (`git diff HEAD`), not per-run. Runs do not
    record a base commit, so `run_id` is contextual only and the payload
    reports `scope: "repo_working_tree"` so the UI cannot imply run isolation.
    Never raises: missing git or a non-repo path returns available=False.
    """
    repo = Path(__file__).resolve().parents[1]
    payload: dict[str, object] = {
        "run_id": run_id,
        "repo": str(repo),
        "scope": "repo_working_tree",
        "available": False,
        "files": [],
        "patch": "",
        "truncated": False,
    }
    try:
        inside = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return payload

        stat = subprocess.run(
            ["git", "-C", str(repo), "diff", "--numstat", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        files: list[dict[str, object]] = []
        for line in (stat.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            files.append(
                {
                    "path": path,
                    "added": int(added) if added.isdigit() else 0,
                    "removed": int(removed) if removed.isdigit() else 0,
                }
            )

        patch = subprocess.run(
            ["git", "-C", str(repo), "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        body = patch.stdout or ""
        truncated = len(body) > DIFF_MAX_PATCH_BYTES
        payload.update(
            {
                "available": True,
                "files": files,
                "patch": body[:DIFF_MAX_PATCH_BYTES],
                "truncated": truncated,
            }
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return payload
    return payload



def _runtime_state_db_path() -> Path | None:
    """Resolve the runtime agent's own session DB.

    Helm harvests real tool activity from here instead of depending on the
    runtime agent voluntarily appending events to runtime-events.jsonl.
    """
    explicit = os.getenv("HELM_RUNTIME_STATE_DB", "").strip()
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.exists() else None
    # Runtime-state discovery is opt-in so the app never assumes a local Hermes profile layout.
    return None


def _scrub_secrets(value: str) -> str:
    """Mask secret-looking substrings while keeping the text useful to read."""
    scrubbed = re.sub(
        r"\b(sk|pk|gsk|ghp|xoxb|xoxp|hf)[-_][A-Za-z0-9_\-]{6,}",
        "<redacted>",
        value,
    )
    scrubbed = re.sub(
        r"(?i)\b(authorization|bearer|api[-_]?key|token|secret|password|passwd)\b"
        r"(\s*[:=]\s*|\s+)(\"|')?[^\s\"']{4,}",
        r"\1\2<redacted>",
        scrubbed,
    )
    scrubbed = re.sub(
        r"(?i)([?&](?:token|key|api_key|access_token|secret)=)[^&\s]+",
        r"\1<redacted>",
        scrubbed,
    )
    return scrubbed


def _tool_call_summary(name: str, arguments: object) -> str:
    """Build a short, secret-scrubbed one-line summary for a tool call."""
    args: dict = {}
    if isinstance(arguments, dict):
        args = arguments
    elif isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            return _scrub_secrets(arguments[:120])
    for key in TOOL_SUMMARY_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()[:120]
            if key in TOOL_SUMMARY_REDACT_KEYS:
                text = _scrub_secrets(text)
            return f"{key}={text}"
    return name


_TOOL_HARVEST_LOCK = threading.Lock()


def _harvest_runtime_tool_events(conn, run_id: int, artifact_path: str) -> None:
    """Import tool calls recorded by the runtime agent for this run's artifact dir.

    Sessions are matched by the artifact path appearing in their system prompt
    (the runtime runs with cwd set to the artifact dir). Import is idempotent:
    already-recorded (run_id, external message id) pairs are skipped. A process
    lock serialises the read-then-insert so concurrent polling of
    /api/runs/{id}/events and /api/runs/{id}/live cannot double-insert.
    """
    if not artifact_path:
        return
    state_db = _runtime_state_db_path()
    if state_db is None:
        return

    with _TOOL_HARVEST_LOCK:
        already: set[int] = set()
        for row in conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ? AND type = 'tool_activity'",
            (run_id,),
        ):
            try:
                payload = json.loads(row[0] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            external_id = payload.get("external_message_id")
            if isinstance(external_id, int):
                already.add(external_id)

        try:
            source = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            source.row_factory = sqlite3.Row
        except sqlite3.Error:
            return

        try:
            session_ids = [
                row["id"]
                for row in source.execute(
                    "SELECT id, system_prompt FROM sessions WHERE system_prompt LIKE ?",
                    (f"%{artifact_path}%",),
                )
            ]
            if not session_ids:
                return
            placeholders = ",".join("?" for _ in session_ids)
            # Two distinct shapes carry tool information and BOTH matter:
            #   * assistant rows with a tool_calls JSON array (planned calls)
            #   * role='tool' rows where tool_calls is NULL and the executed
            #     tool name sits in tool_name (this is the common case)
            # Filtering on tool_calls alone dropped ~96% of real tool activity.
            rows = source.execute(
                f"SELECT id, tool_calls, tool_name, role FROM messages WHERE session_id IN ({placeholders}) "
                "AND ((tool_calls IS NOT NULL AND tool_calls != '') "
                "     OR (tool_name IS NOT NULL AND tool_name != '')) "
                "ORDER BY id LIMIT ?",
                (*session_ids, TOOL_HARVEST_MAX_ROWS),
            ).fetchall()
        except sqlite3.Error:
            return
        finally:
            source.close()

        for row in rows:
            external_id = row["id"]
            if external_id in already:
                continue

            keys = row.keys()
            raw_tool_calls = row["tool_calls"] if "tool_calls" in keys else None
            executed_name = str((row["tool_name"] if "tool_name" in keys else "") or "").strip()

            # role='tool' row: the tool already ran, name is in tool_name.
            if executed_name and not raw_tool_calls:
                _record_run_event(
                    conn,
                    run_id,
                    "tool_activity",
                    {
                        "tool": executed_name,
                        "summary": _tool_call_summary(executed_name, None),
                        "source": "runtime_state_db",
                        "external_message_id": external_id,
                    },
                    artifact_path,
                )
                already.add(external_id)
                continue

            try:
                calls = json.loads(raw_tool_calls or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                raw_function = call.get("function")
                function: dict = raw_function if isinstance(raw_function, dict) else {}
                name = str(function.get("name") or call.get("name") or "").strip()
                if not name:
                    continue
                _record_run_event(
                    conn,
                    run_id,
                    "tool_activity",
                    {
                        "tool": name,
                        "summary": _tool_call_summary(name, function.get("arguments")),
                        "source": "runtime_state_db",
                        "external_message_id": external_id,
                    },
                    artifact_path,
                )
            already.add(external_id)
        conn.commit()


def _write_runtime_output(artifact_path: Path, run: dict, result: dict[str, object]) -> None:
    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    sections = [
        "# Runtime output",
        "",
        f"Run: #{run['id']}",
        f"Mode: {run.get('mode') or 'work'}",
        "",
        "## Prompt",
        "",
        run.get("prompt") or "",
        "",
        "## stdout",
        "",
        stdout or "_No stdout._",
    ]
    if stderr:
        sections.extend(["", "## stderr", "", stderr])
    sections.extend(["", f"Exit code: {result.get('exit_code')}", f"Timed out: {result.get('timed_out')}", ""])
    (artifact_path / "output.md").write_text("\n".join(sections), encoding="utf-8")


def _stage_progress(run: dict) -> list[dict[str, str]]:
    current_stage = run.get("current_stage") or "input"
    if current_stage not in STAGE_SEQUENCE:
        current_stage = "input"
    current_index = STAGE_SEQUENCE.index(current_stage)
    run_completed = str(run.get("status") or "").lower() == "completed" or run.get("stage_status") == "completed"
    stages = []
    for index, stage in enumerate(STAGE_SEQUENCE):
        if run_completed or index < current_index:
            status = "completed"
        elif index == current_index:
            status = run.get("stage_status") or "active"
        else:
            status = "pending"
        stages.append({"name": stage, "label": STAGE_LABELS[stage], "status": status})
    return stages


TOKEN_EXHAUSTION_PATTERNS = (
    "rate_limit",
    "rate limit",
    "429",
    "quota exceeded",
    "quota_exceeded",
    "too many requests",
    "token limit",
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
    "insufficient_quota",
    "insufficient quota",
)


def _seconds_from_now_iso(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _matched_token_pattern(result: dict[str, object]) -> str:
    stderr = str(result.get("stderr") or "").lower()
    stdout = str(result.get("stdout") or "").lower()
    combined = stderr + "\n" + stdout
    for pattern in TOKEN_EXHAUSTION_PATTERNS:
        if pattern in combined:
            return pattern
    return "unknown"


def _is_token_exhaustion_error(result: dict[str, object]) -> bool:
    """Detect whether a runtime result looks like a token/quota exhaustion failure."""
    if result.get("exit_code") == 0:
        return False
    stderr = str(result.get("stderr") or "").lower()
    stdout = str(result.get("stdout") or "").lower()
    combined = stderr + "\n" + stdout
    return any(pattern in combined for pattern in TOKEN_EXHAUSTION_PATTERNS)


def _get_token_refresh_seconds(default: int = 60) -> int:
    """Seconds to wait before attempting a token-refresh retry."""
    raw = os.getenv("HELM_TOKEN_REFRESH_SECONDS", str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(10, min(value, 3600))


def _safe_artifact_path(artifact_path: str, artifact_name: str) -> Path:
    try:
        return resolve_artifact(artifact_path, artifact_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _safe_artifact_version_path(artifact_path: str, artifact_name: str, version_name: str) -> Path:
    try:
        return resolve_artifact_version(artifact_path, artifact_name, version_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _record_run_event(conn, run_id: int, event_type: str, payload: dict, artifact_path: str | None = None) -> None:
    conn.execute(
        "INSERT INTO run_events(run_id, type, payload_json) VALUES (?, ?, ?)",
        (run_id, event_type, json.dumps(payload, ensure_ascii=False)),
    )
    if artifact_path:
        append_event(artifact_path, event_type, payload)


def _unique_project_slug(conn, name: str) -> str:
    base = slugify(name) or "project"
    slug = base
    suffix = 2
    while conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone() is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


app = create_app()
