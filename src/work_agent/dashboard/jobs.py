from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from work_agent.dashboard.models import JobKind, JobResultLine, JobSnapshot, JobStatus

_MAX_RETAINED_JOBS = 50
_MAX_EVENTS = 2000

Emit = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class JobOutcome:
    ok: bool
    summary: str
    results: tuple[JobResultLine, ...] = ()


Work = Callable[[Emit], JobOutcome]


class JobConflictError(Exception):
    """A job is already running against one of the requested targets."""


@dataclass
class _Job:
    id: str
    kind: JobKind
    target: str
    started_at: datetime
    status: JobStatus = JobStatus.RUNNING
    finished_at: datetime | None = None
    summary: str | None = None
    error: str | None = None
    events: list[str] = field(default_factory=list)
    results: list[JobResultLine] = field(default_factory=list)


class JobManager:
    """Run one blocking workflow per target in a background thread and buffer its trace."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._busy: dict[str, str] = {}

    def start(
        self,
        *,
        kind: JobKind,
        target: str,
        keys: Iterable[str],
        work: Work,
    ) -> JobSnapshot:
        reserved = tuple(dict.fromkeys(keys))
        job = _Job(
            id=uuid.uuid4().hex,
            kind=kind,
            target=target,
            started_at=datetime.now(UTC),
        )
        with self._lock:
            conflict = next((key for key in reserved if key in self._busy), None)
            if conflict is not None:
                raise JobConflictError(
                    f"A workflow is already running for {conflict}. Wait for it to finish."
                )
            for key in reserved:
                self._busy[key] = job.id
            self._jobs[job.id] = job
            while len(self._jobs) > _MAX_RETAINED_JOBS:
                _, evicted = self._jobs.popitem(last=False)
                if evicted.status is JobStatus.RUNNING:
                    self._jobs[evicted.id] = evicted
                    break

        thread = threading.Thread(
            target=self._run,
            args=(job, reserved, work),
            name=f"work-agent-job-{job.id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.snapshot(job.id)

    def _run(self, job: _Job, reserved: tuple[str, ...], work: Work) -> None:
        def emit(message: str) -> None:
            text = message.rstrip("\n")
            if not text:
                return
            with self._lock:
                if len(job.events) < _MAX_EVENTS:
                    job.events.append(text)

        try:
            outcome = work(emit)
        # A workflow failure must mark the job, never kill the dashboard thread.
        except Exception as exc:
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = str(exc) or "The workflow stopped with an unexpected local error."
                job.finished_at = datetime.now(UTC)
        else:
            with self._lock:
                job.status = JobStatus.SUCCEEDED if outcome.ok else JobStatus.FAILED
                job.summary = outcome.summary
                job.results = list(outcome.results)
                job.finished_at = datetime.now(UTC)
        finally:
            with self._lock:
                for key in reserved:
                    if self._busy.get(key) == job.id:
                        del self._busy[key]

    def snapshot(self, job_id: str) -> JobSnapshot:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return JobSnapshot(
                id=job.id,
                kind=job.kind,
                status=job.status,
                target=job.target,
                started_at=job.started_at,
                finished_at=job.finished_at,
                events=list(job.events),
                summary=job.summary,
                error=job.error,
                results=list(job.results),
            )

    def events_since(self, job_id: str, index: int) -> tuple[list[str], JobStatus]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job.events[max(0, index) :], job.status

    def busy_targets(self) -> dict[str, str]:
        with self._lock:
            return dict(self._busy)
