from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from work_agent.dashboard.models import JobKind, JobResultLine, JobSnapshot, JobStatus
from work_agent.diagnostics import log_exception

_LOGGER = logging.getLogger(__name__)


_MAX_RETAINED_JOBS = 50
_MAX_EVENTS = 2000

Emit = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class JobOutcome:
    ok: bool
    summary: str
    results: tuple[JobResultLine, ...] = ()
    # Skill-specific structured detail, rendered by the matching panel.
    payload: dict[str, object] | None = None
    # The work stopped early because cancellation was requested; results so far still count.
    cancelled: bool = False


Work = Callable[[Emit], JobOutcome]


class JobConflictError(Exception):
    """A job is already running against one of the requested targets."""


@dataclass
class _Job:
    id: str
    kind: JobKind
    target: str
    targets: tuple[str, ...]
    started_at: datetime
    status: JobStatus = JobStatus.RUNNING
    finished_at: datetime | None = None
    summary: str | None = None
    error: str | None = None
    events: list[str] = field(default_factory=list)
    results: list[JobResultLine] = field(default_factory=list)
    payload: dict[str, object] | None = None
    # Present only when the work can honor a cancel request (for example a retry wait).
    cancel: threading.Event | None = None
    cancel_requested: bool = False


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
        cancel: threading.Event | None = None,
    ) -> JobSnapshot:
        reserved = tuple(dict.fromkeys(keys))
        job = _Job(
            id=uuid.uuid4().hex,
            kind=kind,
            target=target,
            targets=reserved,
            started_at=datetime.now(UTC),
            cancel=cancel,
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
            self._prune_unlocked()

        thread = threading.Thread(
            target=self._run,
            args=(job, reserved, work),
            name=f"work-agent-job-{job.id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            # No worker thread means no `_run` finally-block, so the reservation would leak
            # and every later request for these KVMs would be refused with a phantom conflict.
            log_exception(_LOGGER, f"Dashboard job {job.id[:8]} could not start a thread", exc)
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = "The dashboard could not start a worker thread for this job."
                job.finished_at = datetime.now(UTC)
                self._release_unlocked(job, reserved)
        return self.snapshot(job.id)

    def cancel(self, job_id: str) -> JobSnapshot:
        """Ask a running job to stop at its next cancellation point.

        Only jobs started with a cancel event can honor this; the request is recorded either
        way so the page can show it. Busy keys are released by the worker when it returns,
        never here: releasing them while a workflow still drives HID would let a second job
        start against the same KVM.
        """

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status is JobStatus.RUNNING and job.cancel is not None:
                job.cancel_requested = True
                job.cancel.set()
            return self._snapshot_unlocked(job)

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
            log_exception(_LOGGER, f"Dashboard job {job.id[:8]} ({job.kind.value}) crashed", exc)
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = str(exc) or "The workflow stopped with an unexpected local error."
                job.finished_at = datetime.now(UTC)
        else:
            with self._lock:
                result_states = {result.ok for result in outcome.results}
                if outcome.cancelled:
                    job.status = JobStatus.CANCELLED
                elif result_states == {False, True}:
                    job.status = JobStatus.PARTIAL
                elif False in result_states:
                    job.status = JobStatus.FAILED
                else:
                    job.status = JobStatus.SUCCEEDED if outcome.ok else JobStatus.FAILED
                job.summary = outcome.summary
                job.results = list(outcome.results)
                job.payload = outcome.payload
                job.finished_at = datetime.now(UTC)
        finally:
            with self._lock:
                self._release_unlocked(job, reserved)

    def _release_unlocked(self, job: _Job, reserved: tuple[str, ...]) -> None:
        for key in reserved:
            if self._busy.get(key) == job.id:
                del self._busy[key]
        self._prune_unlocked()

    def _prune_unlocked(self) -> None:
        """Discard the oldest completed jobs while retaining every active job."""

        while len(self._jobs) > _MAX_RETAINED_JOBS:
            completed_id = next(
                (
                    job_id
                    for job_id, retained in self._jobs.items()
                    if retained.status is not JobStatus.RUNNING
                ),
                None,
            )
            if completed_id is None:
                return
            del self._jobs[completed_id]

    def snapshot(self, job_id: str) -> JobSnapshot:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._snapshot_unlocked(job)

    def snapshots(self, *, limit: int) -> list[JobSnapshot]:
        """Return the newest retained jobs first, capped by the retention bound."""

        bounded_limit = min(max(0, limit), _MAX_RETAINED_JOBS)
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)
            return [self._snapshot_unlocked(job) for job in jobs[:bounded_limit]]

    @staticmethod
    def _snapshot_unlocked(job: _Job) -> JobSnapshot:
        return JobSnapshot(
            id=job.id,
            kind=job.kind,
            status=job.status,
            target=job.target,
            targets=list(job.targets),
            started_at=job.started_at,
            finished_at=job.finished_at,
            events=list(job.events),
            summary=job.summary,
            error=job.error,
            results=list(job.results),
            payload=job.payload,
            cancellable=job.cancel is not None,
            cancel_requested=job.cancel_requested,
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
