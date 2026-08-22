from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from work_agent.meeting.errors import (
    MeetingStateConflictError,
    MeetingStateCorruptError,
    MeetingStorageError,
)

_SCHEMA_VERSION = 2
_KVM_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "session_id",
        "kvm",
        "phase",
        "started_at",
        "recording_started_at",
        "updated_at",
        "session_directory",
        "worker_pid",
        "heartbeat_at",
        "stop_requested_at",
        "ended_at",
        "error_code",
        "report_path",
        "our_action_items",
        "possible_our_action_items",
        "decisions",
        "blockers",
    }
)


class RecorderPhase(StrEnum):
    STARTING = "starting"
    RECORDING = "recording"
    STOP_REQUESTED = "stop_requested"
    FINALIZING = "finalizing"
    READY_FOR_PROCESSING = "ready_for_processing"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    PROCESSING_FAILED = "processing_failed"
    COMPLETED = "completed"
    AUDIO_UNAVAILABLE = "audio_unavailable"
    DISCONNECTED = "disconnected"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            RecorderPhase.COMPLETED,
            RecorderPhase.AUDIO_UNAVAILABLE,
            RecorderPhase.FAILED,
        }


CAPTURE_PHASES = frozenset(
    {
        RecorderPhase.STARTING,
        RecorderPhase.RECORDING,
        RecorderPhase.STOP_REQUESTED,
        RecorderPhase.FINALIZING,
    }
)
_ALLOWED_TRANSITIONS: dict[RecorderPhase, frozenset[RecorderPhase]] = {
    RecorderPhase.STARTING: frozenset(
        {
            RecorderPhase.RECORDING,
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.INTERRUPTED,
            RecorderPhase.DISCONNECTED,
            RecorderPhase.AUDIO_UNAVAILABLE,
            RecorderPhase.FAILED,
        }
    ),
    RecorderPhase.RECORDING: frozenset(
        {
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
            RecorderPhase.READY_FOR_PROCESSING,
            RecorderPhase.INTERRUPTED,
            RecorderPhase.DISCONNECTED,
            RecorderPhase.AUDIO_UNAVAILABLE,
            RecorderPhase.FAILED,
        }
    ),
    RecorderPhase.STOP_REQUESTED: frozenset(
        {
            RecorderPhase.FINALIZING,
            RecorderPhase.READY_FOR_PROCESSING,
            RecorderPhase.INTERRUPTED,
            RecorderPhase.DISCONNECTED,
            RecorderPhase.AUDIO_UNAVAILABLE,
            RecorderPhase.FAILED,
        }
    ),
    RecorderPhase.FINALIZING: frozenset(
        {
            RecorderPhase.READY_FOR_PROCESSING,
            RecorderPhase.INTERRUPTED,
            RecorderPhase.DISCONNECTED,
            RecorderPhase.AUDIO_UNAVAILABLE,
            RecorderPhase.FAILED,
        }
    ),
    RecorderPhase.READY_FOR_PROCESSING: frozenset(
        {RecorderPhase.TRANSCRIBING, RecorderPhase.PROCESSING_FAILED, RecorderPhase.FAILED}
    ),
    RecorderPhase.DISCONNECTED: frozenset(
        {RecorderPhase.TRANSCRIBING, RecorderPhase.PROCESSING_FAILED, RecorderPhase.FAILED}
    ),
    RecorderPhase.INTERRUPTED: frozenset(
        {RecorderPhase.TRANSCRIBING, RecorderPhase.PROCESSING_FAILED, RecorderPhase.FAILED}
    ),
    RecorderPhase.TRANSCRIBING: frozenset(
        {RecorderPhase.ANALYZING, RecorderPhase.PROCESSING_FAILED, RecorderPhase.FAILED}
    ),
    RecorderPhase.ANALYZING: frozenset(
        {
            RecorderPhase.TRANSCRIBING,
            RecorderPhase.COMPLETED,
            RecorderPhase.PROCESSING_FAILED,
            RecorderPhase.FAILED,
        }
    ),
    RecorderPhase.PROCESSING_FAILED: frozenset({RecorderPhase.TRANSCRIBING, RecorderPhase.FAILED}),
    RecorderPhase.COMPLETED: frozenset(),
    RecorderPhase.AUDIO_UNAVAILABLE: frozenset(),
    RecorderPhase.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class MeetingRecorderState:
    session_id: str
    kvm: str
    phase: RecorderPhase
    started_at: datetime
    updated_at: datetime
    session_directory: Path
    revision: int = 0
    recording_started_at: datetime | None = None
    worker_pid: int | None = None
    heartbeat_at: datetime | None = None
    stop_requested_at: datetime | None = None
    ended_at: datetime | None = None
    error_code: str | None = None
    report_path: Path | None = None
    our_action_items: int | None = None
    possible_our_action_items: int | None = None
    decisions: int | None = None
    blockers: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ValueError("Meeting state revision must be a non-negative integer.")
        if _SESSION_PATTERN.fullmatch(self.session_id) is None:
            raise ValueError("Meeting state contains an invalid session ID.")
        if _KVM_PATTERN.fullmatch(self.kvm) is None:
            raise ValueError("Meeting state contains an invalid KVM.")
        if not self.session_directory.is_absolute():
            raise ValueError("Meeting state requires an absolute session directory.")
        for value in (
            self.started_at,
            self.recording_started_at,
            self.updated_at,
            self.heartbeat_at,
            self.stop_requested_at,
            self.ended_at,
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError("Meeting state timestamps must include a timezone.")
            if value is not None and value < self.started_at:
                raise ValueError("Meeting state timestamps cannot precede the start time.")
        if self.worker_pid is not None and self.worker_pid <= 0:
            raise ValueError("Meeting worker PID must be positive.")
        if self.worker_pid is not None and self.phase not in CAPTURE_PHASES:
            raise ValueError("Only a capture phase may retain meeting worker ownership.")
        if self.error_code is not None and _ERROR_CODE_PATTERN.fullmatch(self.error_code) is None:
            raise ValueError("Meeting state error codes must be sanitized identifiers.")
        if self.report_path is not None:
            if not self.report_path.is_absolute():
                raise ValueError("Meeting state requires an absolute report path.")
            directory = self.session_directory.resolve()
            report = self.report_path.resolve()
            if directory not in report.parents:
                raise ValueError("Meeting report path must remain inside its session directory.")
        for count in (
            self.our_action_items,
            self.possible_our_action_items,
            self.decisions,
            self.blockers,
        ):
            if count is not None and count < 0:
                raise ValueError("Meeting result counts cannot be negative.")


class MeetingStateStore:
    """Coordinate exactly one Mac-local recorder through private atomic state."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> MeetingRecorderState | None:
        with self._lock(exclusive=False):
            return self._read_unlocked()

    def reserve(self, state: MeetingRecorderState) -> MeetingRecorderState:
        with self._lock(exclusive=True):
            current = self._read_unlocked()
            if current is not None and not current.phase.terminal:
                raise MeetingStateConflictError(_busy_message(current))
            if state.revision != 0:
                raise MeetingStateConflictError(
                    "A new meeting recorder must start at revision zero."
                )
            self._write_unlocked(state)
            return state

    def compare_and_set(
        self,
        expected_session_id: str,
        expected_revision: int,
        state: MeetingRecorderState,
    ) -> MeetingRecorderState:
        with self._lock(exclusive=True):
            current = self._read_unlocked()
            if (
                current is None
                or current.session_id != expected_session_id
                or current.revision != expected_revision
            ):
                raise MeetingStateConflictError("The active meeting recorder state changed.")
            self._validate_update(current, state)
            updated = replace(state, revision=current.revision + 1)
            self._write_unlocked(updated)
            return updated

    def claim_worker(
        self,
        expected_session_id: str,
        worker_pid: int,
        claimed_at: datetime,
    ) -> MeetingRecorderState:
        """Atomically grant one process ownership of an unclaimed starting session."""

        if worker_pid <= 0:
            raise ValueError("Meeting worker PID must be positive.")
        with self._lock(exclusive=True):
            current = self._required_unlocked(expected_session_id)
            if current.worker_pid == worker_pid:
                return current
            if current.phase is not RecorderPhase.STARTING or current.worker_pid is not None:
                raise MeetingStateConflictError("The meeting recorder worker is already claimed.")
            effective_claimed_at = max(claimed_at, current.updated_at)
            candidate = replace(
                current,
                worker_pid=worker_pid,
                heartbeat_at=effective_claimed_at,
                updated_at=effective_claimed_at,
            )
            self._validate_update(current, candidate, worker_claim=True)
            updated = replace(candidate, revision=current.revision + 1)
            self._write_unlocked(updated)
            return updated

    def request_stop(
        self,
        expected_session_id: str,
        requested_at: datetime,
    ) -> MeetingRecorderState:
        """Durably and idempotently record capture stop intent."""

        with self._lock(exclusive=True):
            current = self._required_unlocked(expected_session_id)
            if current.phase not in {
                RecorderPhase.STARTING,
                RecorderPhase.RECORDING,
                RecorderPhase.STOP_REQUESTED,
                RecorderPhase.FINALIZING,
            }:
                return current
            if current.stop_requested_at is not None:
                return current
            effective_requested_at = max(requested_at, current.updated_at)
            candidate = replace(
                current,
                phase=(
                    RecorderPhase.STOP_REQUESTED
                    if current.phase in {RecorderPhase.STARTING, RecorderPhase.RECORDING}
                    else current.phase
                ),
                stop_requested_at=effective_requested_at,
                updated_at=effective_requested_at,
            )
            self._validate_update(current, candidate)
            updated = replace(candidate, revision=current.revision + 1)
            self._write_unlocked(updated)
            return updated

    def mark_recording_started(
        self,
        expected_session_id: str,
        worker_pid: int,
        started_at: datetime,
    ) -> MeetingRecorderState:
        """Persist the first incoming-audio time without regressing a concurrent stop."""

        with self._lock(exclusive=True):
            current = self._required_unlocked(expected_session_id)
            if current.worker_pid != worker_pid or current.phase not in CAPTURE_PHASES:
                raise MeetingStateConflictError("The meeting recorder worker ownership changed.")
            if current.recording_started_at is not None:
                return current
            effective_started_at = max(started_at, current.started_at)
            candidate = replace(
                current,
                phase=(
                    RecorderPhase.RECORDING
                    if current.phase is RecorderPhase.STARTING
                    else current.phase
                ),
                recording_started_at=effective_started_at,
                heartbeat_at=max(effective_started_at, current.heartbeat_at or current.started_at),
                updated_at=max(effective_started_at, current.updated_at),
            )
            self._validate_update(current, candidate)
            updated = replace(candidate, revision=current.revision + 1)
            self._write_unlocked(updated)
            return updated

    def abandon(
        self,
        expected_session_id: str,
        expected_revision: int,
        abandoned_at: datetime,
    ) -> MeetingRecorderState:
        """Terminalize one exact stale session without deleting its artifacts."""

        with self._lock(exclusive=True):
            current = self._required_unlocked(expected_session_id)
            if current.revision != expected_revision:
                raise MeetingStateConflictError("The active meeting recorder state changed.")
            if current.phase.terminal:
                return current
            effective_abandoned_at = max(abandoned_at, current.updated_at)
            candidate = replace(
                current,
                phase=RecorderPhase.FAILED,
                worker_pid=None,
                ended_at=current.ended_at or effective_abandoned_at,
                updated_at=effective_abandoned_at,
                error_code="abandoned_by_user",
            )
            self._validate_update(current, candidate)
            updated = replace(candidate, revision=current.revision + 1)
            self._write_unlocked(updated)
            return updated

    def clear(self, expected_session_id: str, expected_revision: int) -> None:
        with self._lock(exclusive=True):
            current = self._required_unlocked(expected_session_id)
            if current.revision != expected_revision:
                raise MeetingStateConflictError("The active meeting recorder state changed.")
            if not current.phase.terminal:
                raise MeetingStateConflictError(
                    "A nonterminal meeting must be abandoned before its state is cleared."
                )
            try:
                self._path.unlink(missing_ok=True)
                _sync_directory(self._path.parent)
            except OSError:
                raise MeetingStorageError(
                    "The local meeting recorder state could not be cleared."
                ) from None

    def set_aside_corrupt(self) -> Path:
        """Rename an unreadable state file next to itself and return the new path.

        This is the escape hatch for a corrupt or old-schema file, which otherwise blocks every
        command. A file that still parses is never moved: it describes a real session that must
        be stopped or abandoned by ID instead.
        """

        with self._lock(exclusive=True):
            if not self._path.exists():
                raise MeetingStateConflictError("There is no meeting recorder state to reset.")
            try:
                self._read_unlocked()
            except MeetingStateCorruptError:
                pass
            else:
                raise MeetingStateConflictError(
                    "The meeting recorder state is readable; abandon it by session ID instead "
                    "of resetting it."
                )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
            try:
                if target.exists():
                    raise OSError("target exists")
                os.replace(self._path, target)
                _sync_directory(self._path.parent)
            except OSError:
                raise MeetingStorageError(
                    "The corrupt meeting recorder state could not be moved aside."
                ) from None
            return target

    def _required_unlocked(self, expected_session_id: str) -> MeetingRecorderState:
        current = self._read_unlocked()
        if current is None or current.session_id != expected_session_id:
            raise MeetingStateConflictError("The active meeting recorder state changed.")
        return current

    def _validate_update(
        self,
        current: MeetingRecorderState,
        state: MeetingRecorderState,
        *,
        worker_claim: bool = False,
    ) -> None:
        if (
            state.session_id != current.session_id
            or state.kvm != current.kvm
            or state.started_at != current.started_at
            or state.session_directory != current.session_directory
        ):
            raise MeetingStateConflictError(
                "A meeting recorder update cannot change its session, KVM, start, or directory."
            )
        if state.revision != current.revision:
            raise MeetingStateConflictError("The active meeting recorder state changed.")
        if state.updated_at < current.updated_at:
            raise MeetingStateConflictError(
                "Meeting recorder updates cannot move backward in time."
            )
        if state.recording_started_at != current.recording_started_at and (
            current.recording_started_at is not None or state.recording_started_at is None
        ):
            raise MeetingStateConflictError(
                "The meeting recording start time is immutable once recorded."
            )
        if state.stop_requested_at != current.stop_requested_at and (
            current.stop_requested_at is not None or state.stop_requested_at is None
        ):
            raise MeetingStateConflictError(
                "The meeting stop request time is immutable once recorded."
            )
        if state.worker_pid != current.worker_pid:
            if worker_claim:
                if current.worker_pid is not None or state.worker_pid is None:
                    raise MeetingStateConflictError(
                        "The meeting recorder worker is already claimed."
                    )
            elif state.worker_pid is not None or state.phase in CAPTURE_PHASES:
                raise MeetingStateConflictError(
                    "Meeting worker ownership must be claimed atomically."
                )
        if not _transition_allowed(current.phase, state.phase):
            raise MeetingStateConflictError(
                f"Meeting recorder cannot transition from {current.phase.value} "
                f"to {state.phase.value}."
            )

    @contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor: int | None = None
        try:
            self._prepare_parent()
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("meeting state lock is not a regular file")
            os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, operation)
            yield
        except MeetingStateCorruptError:
            raise
        except OSError:
            raise MeetingStorageError("The local meeting recorder state is unavailable.") from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(descriptor)

    def _prepare_parent(self) -> None:
        parent = self._path.parent
        existed = parent.exists()
        if parent.is_symlink():
            raise MeetingStorageError("The local meeting state directory must not be a symlink.")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            parent.chmod(0o700)
        mode = stat.S_IMODE(parent.stat().st_mode)
        if mode & 0o077:
            raise MeetingStorageError(
                "The local meeting state directory must already be private (mode 0700)."
            )

    def _read_unlocked(self) -> MeetingRecorderState | None:
        if not self._path.exists():
            return None
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self._path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Meeting state must be a regular file.")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                payload = json.load(stream)
            return _state_from_payload(payload)
        except (OSError, ValueError, TypeError, KeyError):
            raise MeetingStateCorruptError(
                "The local meeting recorder state is unreadable; it was not overwritten."
            ) from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def _write_unlocked(self, state: MeetingRecorderState) -> None:
        temporary: Path | None = None
        descriptor: int | None = None
        try:
            self._prepare_parent()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(
                    _state_payload(state),
                    stream,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
            _sync_directory(self._path.parent)
        except OSError:
            raise MeetingStorageError(
                "The local meeting recorder state could not be written."
            ) from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


def _state_payload(state: MeetingRecorderState) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "revision": state.revision,
        "session_id": state.session_id,
        "kvm": state.kvm,
        "phase": state.phase.value,
        "started_at": state.started_at.isoformat(),
        "recording_started_at": _optional_datetime(state.recording_started_at),
        "updated_at": state.updated_at.isoformat(),
        "session_directory": str(state.session_directory),
        "worker_pid": state.worker_pid,
        "heartbeat_at": _optional_datetime(state.heartbeat_at),
        "stop_requested_at": _optional_datetime(state.stop_requested_at),
        "ended_at": _optional_datetime(state.ended_at),
        "error_code": state.error_code,
        "report_path": str(state.report_path) if state.report_path is not None else None,
        "our_action_items": state.our_action_items,
        "possible_our_action_items": state.possible_our_action_items,
        "decisions": state.decisions,
        "blockers": state.blockers,
    }


def _state_from_payload(payload: object) -> MeetingRecorderState:
    if (
        not isinstance(payload, dict)
        or set(payload) != _STATE_KEYS
        or payload.get("schema_version") != _SCHEMA_VERSION
    ):
        raise ValueError("Unsupported meeting state schema.")
    return MeetingRecorderState(
        revision=_required_nonnegative_int(payload, "revision"),
        session_id=_required_string(payload, "session_id"),
        kvm=_required_string(payload, "kvm"),
        phase=RecorderPhase(_required_string(payload, "phase")),
        started_at=_required_datetime(payload, "started_at"),
        recording_started_at=_parse_optional_datetime(payload.get("recording_started_at")),
        updated_at=_required_datetime(payload, "updated_at"),
        session_directory=Path(_required_string(payload, "session_directory")),
        worker_pid=_optional_positive_int(payload.get("worker_pid")),
        heartbeat_at=_parse_optional_datetime(payload.get("heartbeat_at")),
        stop_requested_at=_parse_optional_datetime(payload.get("stop_requested_at")),
        ended_at=_parse_optional_datetime(payload.get("ended_at")),
        error_code=_optional_string(payload.get("error_code")),
        report_path=_optional_path(payload.get("report_path")),
        our_action_items=_optional_nonnegative_int(payload.get("our_action_items")),
        possible_our_action_items=_optional_nonnegative_int(
            payload.get("possible_our_action_items")
        ),
        decisions=_optional_nonnegative_int(payload.get("decisions")),
        blockers=_optional_nonnegative_int(payload.get("blockers")),
    )


def _required_string(payload: dict[object, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {name}.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid optional string.")
    return value


def _required_datetime(payload: dict[object, object], name: str) -> datetime:
    value = _parse_optional_datetime(payload[name])
    if value is None:
        raise ValueError(f"Invalid {name}.")
    return value


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Meeting state timestamps must include a timezone.")
    return parsed


def _optional_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Invalid worker PID.")
    return value


def _required_nonnegative_int(payload: dict[object, object], name: str) -> int:
    value = payload[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Invalid {name}.")
    return value


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Invalid result count.")
    return value


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid report path.")
    return Path(value)


def _busy_message(current: MeetingRecorderState) -> str:
    """Name the phase that is in the way and the command that clears it."""

    phase = current.phase.value.replace("_", " ")
    session = current.session_id
    if current.phase in CAPTURE_PHASES:
        return (
            f"A meeting is already {phase} from {current.kvm} (session {session}). Run "
            "`pikvm-agent meeting stop` to finish it; if its recorder is no longer running, "
            f"run `pikvm-agent meeting abandon --session-id {session}`."
        )
    return (
        f"The previous meeting from {current.kvm} is still {phase} (session {session}). Run "
        "`pikvm-agent meeting stop` to process it, or "
        f"`pikvm-agent meeting abandon --session-id {session}` to release it without processing."
    )


def _transition_allowed(current: RecorderPhase, requested: RecorderPhase) -> bool:
    return requested is current or requested in _ALLOWED_TRANSITIONS[current]


def _sync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
