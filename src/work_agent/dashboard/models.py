from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from work_agent.slack.models import Availability


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ALL_KVMS = "__all__"


class JobKind(StrEnum):
    TRIAGE = "triage"
    AGENDA = "agenda"
    AVAILABILITY_GET = "availability_get"
    AVAILABILITY_SET = "availability_set"
    SCHEDULE_RUN_NOW = "schedule_run_now"
    SCHEDULE_RECONCILE = "schedule_reconcile"
    SCHEDULE_INSTALL = "schedule_install"
    SCHEDULE_UNINSTALL = "schedule_uninstall"
    MEETING_START = "meeting_start"
    MEETING_STOP = "meeting_stop"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduleAction(StrEnum):
    RUN_NOW = "run-now"
    RECONCILE = "reconcile"
    INSTALL = "install"
    UNINSTALL = "uninstall"


class KvmProfile(_Model):
    name: str
    endpoint: str | None
    totp_required: bool | None
    verify_ssl: bool | None
    interactive_totp: bool
    configured: bool
    problem: str | None


class ProfileCard(_Model):
    """A named PiKVM as the Profiles panel shows it. Never carries a password or seed."""

    name: str
    host: str
    url: str
    username: str
    source: str
    enabled: bool
    totp_required: bool
    totp_enrolled: bool | None
    verify_ssl: bool
    removable: bool


class ProfileCreateRequest(_Model):
    name: str
    url: str
    username: str
    password: str
    totp_required: bool = True
    verify_ssl: bool = False


class ProfileActionResult(_Model):
    ok: bool
    message: str
    notes: list[str] = []
    profile: ProfileCard | None = None
    screen_width: int | None = None
    screen_height: int | None = None


class MeetingRecorderCard(_Model):
    active: bool
    phase: str | None
    kvm: str | None
    session_id: str | None
    elapsed_seconds: float
    worker_alive: bool
    worker_stale: bool
    error_code: str | None
    next_step: str | None


class MeetingSessionCard(_Model):
    session_id: str
    kvm: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    stage: str
    has_report: bool
    interrupted: bool
    parts: int
    our_action_items: int | None
    possible_our_action_items: int | None
    decisions: int | None
    problem: str | None


class MeetingActionItemCard(_Model):
    task: str
    owner: str | None
    owner_category: str
    requested_by: str | None
    due_text: str | None
    reason: str | None
    timestamp_seconds: float | None


class MeetingTranscriptLine(_Model):
    start_seconds: float
    speaker: str
    text: str


class MeetingSessionDetailCard(_Model):
    session: MeetingSessionCard
    meeting_summary: str | None
    action_items: list[MeetingActionItemCard]
    decisions: list[str]
    blockers: list[str]
    open_questions: list[str]
    follow_ups: list[str]
    transcript: list[MeetingTranscriptLine]
    report_markdown: str | None


class MeetingsSnapshot(_Model):
    recorder: MeetingRecorderCard
    sessions: list[MeetingSessionCard]
    transcription_provider: str
    transcription_configured: bool
    identity_configured: dict[str, bool]
    data_directory: str


class MeetingStartRequest(_Model):
    kvm: str


class MeetingAbandonRequest(_Model):
    session_id: str


class DashboardConfig(_Model):
    # Feature names this running build serves. The page compares against what it needs, so a
    # server left running from before a new endpoint existed reports that plainly. Static
    # assets are read from disk per request; routes are registered at import.
    capabilities: list[str]
    kvms: list[KvmProfile]
    default_kvm: str | None
    log_path: str
    state_path: str
    timezone: str


class RunTelemetry(_Model):
    """Operation counts for one run; numbers only, never screen or model content."""

    sessions: int = 0
    steps: int = 0
    hid_actions: int = 0
    vision_calls: int = 0
    planner_calls: int = 0
    total_tokens: int = 0
    runtime_seconds: float = 0.0


class RunRecord(_Model):
    timestamp: datetime
    kvm: str
    desired: str | None
    observed: str
    changed: bool | None
    outcome: str
    stop_code: str | None
    error: str | None
    telemetry: RunTelemetry | None = None


class KvmOutcome(_Model):
    kvm: str
    total: int
    success: int
    failure: int
    # Runs skipped because another workflow held the PiKVM; excluded from the success rate.
    skipped: int = 0
    success_rate: float = Field(ge=0.0, le=1.0)
    last_outcome: str | None
    last_observed: str | None
    last_at: datetime | None
    last_stop_code: str | None = None
    consecutive_failures: int = 0


class FailureReason(_Model):
    reason: str
    count: int


class FailureCategory(_Model):
    """Stop reasons grouped into the roadmap's GUI-reliability buckets."""

    category: str
    label: str
    count: int


class HistorySummary(_Model):
    total: int
    success: int
    failure: int
    skipped: int = 0
    success_rate: float = Field(ge=0.0, le=1.0)
    changes_applied: int
    no_ops: int
    reads: int
    per_kvm: list[KvmOutcome]
    failure_reasons: list[FailureReason]
    failure_categories: list[FailureCategory]
    first_at: datetime | None
    last_at: datetime | None


class HistoryResponse(_Model):
    records: list[RunRecord]
    summary: HistorySummary
    log_present: bool
    unreadable_lines: int


class AgentState(_Model):
    label: str
    short_label: str
    path: str
    installed: bool
    loaded: bool


class KvmAlert(_Model):
    """A KVM whose latest runs all stopped because the PiKVM itself could not be used."""

    stop_code: str
    label: str
    count: int
    since: datetime | None
    # Sanitized controller reason from the latest failure (never screen or model content).
    reason: str | None


class KvmStatus(_Model):
    name: str
    endpoint: str | None
    # None when the endpoint was not probed (unconfigured profile or probing disabled).
    reachable: bool | None
    reachability_detail: str | None
    checked_at: datetime | None
    unreachable_since: datetime | None
    # Another local workflow (scheduled run, CLI, or this dashboard) holds the endpoint lock.
    workflow_running: bool
    consecutive_failures: int
    last_run_at: datetime | None
    last_run_outcome: str | None
    last_stop_code: str | None
    alert: KvmAlert | None


class LastRun(_Model):
    kvm: str
    at: datetime
    outcome: str
    stop_code: str | None


class ScheduleSnapshot(_Model):
    agents: list[AgentState]
    interpreter: str | None
    interpreter_can_run: bool
    timezone_name: str | None
    timezone_ok: bool
    problems: list[str]
    healthy: bool
    installed: bool
    desired_now: Availability
    next_transition_at: datetime
    next_transition_to: Availability
    applied: dict[str, str]
    applied_verified_at: dict[str, datetime]
    applied_updated_at: datetime | None
    last_transition_at: datetime | None = None
    last_run: LastRun | None = None
    kvms: list[KvmStatus] = []


class JobResultLine(_Model):
    kvm: str
    ok: bool
    text: str


class JobSnapshot(_Model):
    id: str
    kind: JobKind
    status: JobStatus
    target: str
    targets: list[str]
    started_at: datetime
    finished_at: datetime | None
    events: list[str]
    summary: str | None
    error: str | None
    results: list[JobResultLine]
    payload: dict[str, object] | None = None
    # True when the job can be asked to stop early (for example during a retry wait).
    cancellable: bool = False
    cancel_requested: bool = False


class AvailabilityRequest(_Model):
    kvm: str = Field(min_length=1, max_length=64)
    availability: Availability | None = None


class TriageRequest(_Model):
    kvm: str = Field(min_length=1, max_length=64)


class AgendaRequest(_Model):
    kvm: str = Field(min_length=1, max_length=64)


class ScheduleActionRequest(_Model):
    action: ScheduleAction
    availability: Availability | None = None
