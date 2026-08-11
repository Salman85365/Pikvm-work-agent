from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from work_agent.slack.models import Availability


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ALL_KVMS = "__all__"


class JobKind(StrEnum):
    AVAILABILITY_GET = "availability_get"
    AVAILABILITY_SET = "availability_set"
    SCHEDULE_RUN_NOW = "schedule_run_now"
    SCHEDULE_RECONCILE = "schedule_reconcile"
    SCHEDULE_INSTALL = "schedule_install"
    SCHEDULE_UNINSTALL = "schedule_uninstall"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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


class DashboardConfig(_Model):
    kvms: list[KvmProfile]
    default_kvm: str | None
    log_path: str
    state_path: str
    timezone: str


class RunRecord(_Model):
    timestamp: datetime
    kvm: str
    desired: str | None
    observed: str
    changed: bool | None
    outcome: str
    error: str | None


class KvmOutcome(_Model):
    kvm: str
    total: int
    success: int
    failure: int
    success_rate: float = Field(ge=0.0, le=1.0)
    last_outcome: str | None
    last_observed: str | None
    last_at: datetime | None


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
    applied_updated_at: datetime | None


class JobResultLine(_Model):
    kvm: str
    ok: bool
    text: str


class JobSnapshot(_Model):
    id: str
    kind: JobKind
    status: JobStatus
    target: str
    started_at: datetime
    finished_at: datetime | None
    events: list[str]
    summary: str | None
    error: str | None
    results: list[JobResultLine]


class AvailabilityRequest(_Model):
    kvm: str = Field(min_length=1, max_length=64)
    availability: Availability | None = None


class ScheduleActionRequest(_Model):
    action: ScheduleAction
    availability: Availability | None = None
