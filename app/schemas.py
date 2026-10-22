from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


STAGE_PATTERN = "^(input|plan|execute|review|final_output)$"


def _not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Title must not be blank")
    return stripped


class MessageCreate(BaseModel):
    role: str = Field(pattern="^(user|assistant|system|tool|report)$")
    content: str = Field(min_length=1)


class ChatCreate(BaseModel):
    title: str = Field(default="Nový chat", min_length=1, max_length=80)
    mode: str = Field(default="work", pattern="^(learning|work|sideproject)$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode_default: str = Field(default="work", pattern="^(learning|work|sideproject)$")
    description: str = ""


class ProjectOpen(BaseModel):
    path: str = Field(min_length=1)
    name: str = Field(default="", max_length=80)
    mode_default: str = Field(default="work", pattern="^(learning|work|sideproject)$")
    description: str = ""


class ProjectContextUpdate(BaseModel):
    content: str


class GoalVisionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(default="", max_length=10000)
    status: str = Field(default="active", pattern="^(active|paused|done)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class GoalVisionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    body: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, pattern="^(active|paused|done)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class SettingsUpdate(BaseModel):
    safety_policy_enabled: bool | None = None
    theme_id: str | None = Field(default=None, pattern="^(helm-default|helm-rpg)$")


class ProjectGoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    status: str = Field(default="active", pattern="^(active|paused|done)$")
    target_date: str | None = None
    vision_id: int | None = None
    parent_goal_id: int | None = None
    horizon: str = Field(default="outcome", pattern="^(outcome|quarter|month|week)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class ProjectGoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(active|paused|done)$")
    target_date: str | None = None
    vision_id: int | None = None
    parent_goal_id: int | None = None
    horizon: str | None = Field(default=None, pattern="^(outcome|quarter|month|week)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class DailyPlanUpdate(BaseModel):
    focus: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=10000)


class DailyPlanItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    everyday_item_id: int | None = None
    goal_id: int | None = None
    status: str = Field(default="open", pattern="^(open|done|archived)$")
    sort_order: int = 0

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class DailyPlanItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    everyday_item_id: int | None = None
    goal_id: int | None = None
    status: str | None = Field(default=None, pattern="^(open|done|archived)$")
    sort_order: int | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class ProjectTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    notes: str = ""
    status: str = Field(default="active", pattern="^(active|waiting|done)$")
    priority: int = Field(default=2, ge=1, le=3)
    due_date: str | None = None


class ProjectTaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = None
    status: str | None = Field(default=None, pattern="^(active|waiting|done)$")
    priority: int | None = Field(default=None, ge=1, le=3)
    due_date: str | None = None


class ProjectNoteCreate(BaseModel):
    title: str = Field(default="Note", min_length=1, max_length=120)
    content: str = ""


class ProjectFileCreate(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    label: str = ""
    kind: str = Field(default="file", pattern="^(file|dir|url|note)$")
    pinned: bool = True


class ProjectStageDefaultUpsert(BaseModel):
    instructions: str = ""
    artifact_template: str = ""


class EverydayItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(default="", max_length=10000)
    kind: str = Field(default="task", pattern="^(task|note|reminder|idea)$")
    bucket: str = Field(default="inbox", pattern="^(inbox|today|waiting|later)$")
    status: str = Field(default="open", pattern="^(open|done|archived)$")
    priority: int = Field(default=2, ge=1, le=3)
    project_id: int | None = None
    goal_id: int | None = None
    due_date: str | None = Field(default=None, max_length=10)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class EverydayItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    body: str | None = Field(default=None, max_length=10000)
    kind: str | None = Field(default=None, pattern="^(task|note|reminder|idea)$")
    bucket: str | None = Field(default=None, pattern="^(inbox|today|waiting|later)$")
    status: str | None = Field(default=None, pattern="^(open|done|archived)$")
    priority: int | None = Field(default=None, ge=1, le=3)
    project_id: int | None = None
    goal_id: int | None = None
    due_date: str | None = Field(default=None, max_length=10)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _not_blank(value)




class TelegramInboxCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    source: str = Field(default="telegram", max_length=40)
    external_chat_id: str = Field(default="", max_length=120)
    external_thread_id: str = Field(default="", max_length=120)
    external_message_id: str = Field(default="", max_length=120)
    sender_name: str = Field(default="", max_length=120)
    sender_username: str = Field(default="", max_length=120)
    chat_id: int | None = None
    metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class TelegramInboxUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(new|triaged|captured|routed|archived)$")
    chat_id: int | None = None


class TelegramCaptureRequest(BaseModel):
    bucket: str = Field(default="inbox", pattern="^(inbox|today|waiting|later)$")
    project_id: int | None = None
    goal_id: int | None = None


class TelegramOutboxCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    kind: str = Field(default="message", pattern="^(message|run_report)$")
    status: str = Field(default="draft", pattern="^(draft|ready|cancelled|archived)$")
    target_label: str = Field(default="", max_length=160)
    external_chat_id: str = Field(default="", max_length=120)
    external_thread_id: str = Field(default="", max_length=120)
    run_id: int | None = None
    chat_id: int | None = None
    metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class TelegramOutboxUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    status: str | None = Field(default=None, pattern="^(draft|ready|cancelled|archived)$")
    target_label: str | None = Field(default=None, max_length=160)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class TelegramRunReportCreate(BaseModel):
    target_label: str = Field(default="Telegram Home", max_length=160)
    external_chat_id: str = Field(default="", max_length=120)
    external_thread_id: str = Field(default="", max_length=120)
    include_artifact_path: bool = True


class TelegramSyncSettingsUpdate(BaseModel):
    auto_sync_enabled: bool | None = None
    sync_interval_seconds: int | None = Field(default=None, ge=30, le=86400)


class MemoryItemCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    scope: str = Field(default="global", pattern="^(global|project|chat|user)$")
    kind: str = Field(default="fact", pattern="^(fact|preference|decision|constraint|summary|task|blocker|lesson)$")
    project_id: int | None = None
    chat_id: int | None = None
    source: str = Field(default="manual", max_length=80)
    confidence: int = Field(default=3, ge=1, le=5)
    metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class MemoryItemUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    scope: str | None = Field(default=None, pattern="^(global|project|chat|user)$")
    kind: str | None = Field(default=None, pattern="^(fact|preference|decision|constraint|summary|task|blocker|lesson)$")
    project_id: int | None = None
    chat_id: int | None = None
    source: str | None = Field(default=None, max_length=80)
    confidence: int | None = Field(default=None, ge=1, le=5)
    status: str | None = Field(default=None, pattern="^(active|archived|superseded)$")
    metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class TeamWorkflowCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    chat_id: int | None = None
    run_id: int | None = None
    status: str = Field(default="active", pattern="^(active|paused|done|archived)$")
    current_role: str = Field(default="lead", pattern="^(lead|planner|executor|reviewer|verifier)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class TeamWorkflowStepCreate(BaseModel):
    role: str = Field(pattern="^(lead|planner|executor|reviewer|verifier)$")
    title: str = Field(min_length=1, max_length=180)
    status: str = Field(default="pending", pattern="^(pending|active|done|blocked)$")
    notes: str = Field(default="", max_length=10000)
    sort_order: int = 0
    agent_goal: str = Field(default="", max_length=10000)
    agent_status: str = Field(default="queued", pattern="^(queued|running|done|failed)$")
    self_report: str = Field(default="", max_length=10000)
    verified_evidence: str = Field(default="", max_length=10000)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class TeamWorkflowStepUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(lead|planner|executor|reviewer|verifier)$")
    title: str | None = Field(default=None, min_length=1, max_length=180)
    status: str | None = Field(default=None, pattern="^(pending|active|done|blocked)$")
    notes: str | None = Field(default=None, max_length=10000)
    sort_order: int | None = None
    agent_goal: str | None = Field(default=None, max_length=10000)
    agent_status: str | None = Field(default=None, pattern="^(queued|running|done|failed)$")
    self_report: str | None = Field(default=None, max_length=10000)
    verified_evidence: str | None = Field(default=None, max_length=10000)
    spawn_command: str | None = Field(default=None, max_length=2000)
    spawn_status: str | None = Field(default=None, pattern="^(none|spawning|spawned|completed|failed)$")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class TeamWorkflowStepSpawn(BaseModel):
    command: str = Field(default="", max_length=2000)
    timeout_seconds: int = Field(default=900, ge=1, le=1800)


class TeamWorkflowTemplateCreate(BaseModel):
    title: str = Field(default="TEAM workflow", min_length=1, max_length=180)
    project_id: int | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        return _not_blank(value)


class RunCreate(BaseModel):
    mode: str = Field(default="work", pattern="^(learning|work|sideproject)$")
    prompt: str = ""
    explicit_permission: bool = False
    context_tier: str | None = Field(default=None, pattern="^(XS|S|M|L)$")


class ChatRunCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    start_runtime: bool = True
    timeout_seconds: int = Field(default=900, ge=1, le=1800)
    confirmed_external_runtime: bool = False
    explicit_permission: bool = False
    context_tier: str | None = Field(default=None, pattern="^(XS|S|M|L)$")


class ContextClassifyRequest(BaseModel):
    prompt: str = ""
    context_tier: str | None = Field(default=None, pattern="^(XS|S|M|L)$")


class ArtifactUpdate(BaseModel):
    content: str


class RerunStageRequest(BaseModel):
    stage: str = Field(pattern=STAGE_PATTERN)
    reason: str = ""


class StageAdvanceRequest(BaseModel):
    note: str = ""


class RuntimeStartRequest(BaseModel):
    timeout_seconds: int = Field(default=900, ge=1, le=1800)
    confirmed_external_runtime: bool = False


class RunSummaryCreate(BaseModel):
    summary: str = Field(default="", max_length=5000)
    changes: str = Field(default="", max_length=2000)
    tests: str = Field(default="", max_length=2000)
    next_actions: str | None = Field(default=None, max_length=2000)


class RunSummaryResponse(BaseModel):
    id: int
    run_id: int
    project_id: int
    summary: str
    changes: str
    tests: str
    next_actions: str
    created_at: str


class RunTokenRefreshResponse(BaseModel):
    run_id: int
    waiting_for_tokens: bool
    token_refresh_at: str | None
    token_refresh_seconds: int
    seconds_remaining: int
    auto_resume_enabled: bool
