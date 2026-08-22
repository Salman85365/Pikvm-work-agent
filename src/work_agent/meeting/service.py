from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from work_agent.diagnostics import log_exception
from work_agent.meeting.config import MeetingProvider, MeetingSettings
from work_agent.meeting.errors import (
    MeetingError,
    MeetingStateConflictError,
    MeetingStorageError,
)
from work_agent.meeting.intelligence import (
    IntelligenceAuthenticationError,
    IntelligenceConfigurationError,
    IntelligencePermissionError,
    IntelligenceRequestError,
    MeetingIntelligenceError,
    MeetingIntelligenceProvider,
    OpenAIMeetingIntelligenceProvider,
    guard_meeting_intelligence,
    no_speech_intelligence_result,
)
from work_agent.meeting.manifest import MeetingCaptureCheckpoint, MeetingCaptureManifest
from work_agent.meeting.models import (
    AudioPart,
    IntelligenceArtifact,
    MeetingMetadata,
    OwnerCategory,
    RiskKind,
    TranscriptionArtifact,
    fingerprint_model,
)
from work_agent.meeting.report import render_meeting_report
from work_agent.meeting.state import (
    CAPTURE_PHASES,
    MeetingRecorderState,
    MeetingStateStore,
    RecorderPhase,
)
from work_agent.meeting.storage import MeetingStorage
from work_agent.meeting.transcription import (
    MeetingTranscriptionError,
    OpenAITranscriptionProvider,
    TranscriptionAuthenticationError,
    TranscriptionConfigurationError,
    TranscriptionInputError,
    TranscriptionPermissionError,
    TranscriptionProvider,
    TranscriptionRequestError,
)
from work_agent.pikvm import configured_pikvm_profiles

_LOGGER = logging.getLogger(__name__)


class MeetingLifecycleError(MeetingError):
    """A sanitized start, stop, or status workflow failure."""


_ModelT = TypeVar("_ModelT", bound=BaseModel)

# Phases a dead recorder can be recovered from. Anything later already has its finalized audio
# in hand and must never be forced back to FAILED by a stale recovery decision.
_RECOVERABLE_PHASES = CAPTURE_PHASES | {RecorderPhase.DISCONNECTED, RecorderPhase.INTERRUPTED}


class _TranscriptionFactory(Protocol):
    def __call__(self) -> TranscriptionProvider: ...


class _IntelligenceFactory(Protocol):
    def __call__(self) -> MeetingIntelligenceProvider: ...


@dataclass(frozen=True, slots=True)
class MeetingStartResult:
    session_id: str
    kvm: str
    started_at: datetime
    directory: Path


@dataclass(frozen=True, slots=True)
class MeetingStopResult:
    session_id: str
    kvm: str
    duration_seconds: float
    report_path: Path
    our_action_items: int
    possible_our_action_items: int
    decisions: int
    blockers: int
    interrupted: bool


@dataclass(frozen=True, slots=True)
class MeetingAbandonResult:
    session_id: str
    kvm: str
    directory: Path


@dataclass(frozen=True, slots=True)
class MeetingStateResetResult:
    """A corrupt state file was moved aside so the recorder can be used again."""

    state_path: Path
    moved_to: Path


@dataclass(frozen=True, slots=True)
class MeetingStatusResult:
    state: MeetingRecorderState | None
    worker_alive: bool
    elapsed_seconds: float
    worker_stale: bool = False
    worker_pid_alive: bool = False

    @property
    def active(self) -> bool:
        return self.state is not None and not self.state.phase.terminal


class MeetingService:
    """Coordinate one detached capture and resumable local processing pipeline."""

    def __init__(
        self,
        settings: MeetingSettings,
        *,
        state_store: MeetingStateStore | None = None,
        storage: MeetingStorage | None = None,
        spawn_worker: Callable[[str], int] | None = None,
        transcription_factory: _TranscriptionFactory | None = None,
        intelligence_factory: _IntelligenceFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        pid_is_alive: Callable[[int], bool] | None = None,
        capture_lock_held: Callable[[Path], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._state = state_store or MeetingStateStore(settings.state_path)
        self._storage = storage or MeetingStorage(settings.data_directory)
        self._spawn_worker = spawn_worker or self._default_spawn_worker
        self._transcription_factory = transcription_factory or self._default_transcription
        self._intelligence_factory = intelligence_factory or self._default_intelligence
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._pid_is_alive = pid_is_alive or _pid_is_alive
        self._capture_lock_held = capture_lock_held or self._storage.capture_lock_held
        # Detached workers spawned by this process. Kept so a long-lived parent (the dashboard)
        # reaps them instead of leaving zombies behind.
        self._worker_processes: list[subprocess.Popen[bytes]] = []

    def start(self, kvm: str) -> MeetingStartResult:
        self._reap_worker_processes()
        target = kvm.strip().lower()
        profiles = configured_pikvm_profiles()
        if not profiles:
            raise MeetingLifecycleError(
                "Meeting capture requires at least one name in PIKVM_PROFILES."
            )
        if target not in profiles:
            raise MeetingLifecycleError(f"Unknown PiKVM profile {target!r}.")

        self._recover_abandoned_capture()
        started_at = self._now()
        session_id = f"meeting-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        session_directory = self._storage.session_directory(
            kvm=target,
            session_id=session_id,
            started_at=started_at,
        )
        state = MeetingRecorderState(
            session_id=session_id,
            kvm=target,
            phase=RecorderPhase.STARTING,
            started_at=started_at,
            updated_at=started_at,
            session_directory=session_directory,
        )
        self._state.reserve(state)
        try:
            artifacts = self._storage.create_session(
                kvm=target,
                session_id=session_id,
                started_at=started_at,
            )
        except MeetingError:
            self._update_state(
                session_id,
                phase=RecorderPhase.FAILED,
                error_code="artifact_setup_failed",
            )
            raise MeetingLifecycleError(
                "The protected local meeting directory could not be created."
            ) from None
        try:
            try:
                worker_pid = self._spawn_worker(session_id)
                if worker_pid <= 0:
                    raise ValueError("Meeting worker PID must be positive.")
            except (OSError, ValueError):
                self._update_state(
                    session_id,
                    phase=RecorderPhase.FAILED,
                    error_code="worker_start_failed",
                )
                raise MeetingLifecycleError(
                    "The Mac could not start the protected meeting recorder."
                ) from None

            deadline = self._monotonic() + self._settings.start_handshake_timeout_seconds
            while self._monotonic() < deadline:
                current = self._required_state(session_id)
                if current.phase in {
                    RecorderPhase.AUDIO_UNAVAILABLE,
                    RecorderPhase.DISCONNECTED,
                    RecorderPhase.INTERRUPTED,
                    RecorderPhase.FAILED,
                }:
                    raise MeetingLifecycleError(_capture_failure_message(current.error_code))
                worker_active = self._capture_worker_active(current)
                if current.phase is RecorderPhase.RECORDING and worker_active:
                    return MeetingStartResult(
                        session_id=session_id,
                        kvm=target,
                        started_at=current.recording_started_at or current.started_at,
                        directory=artifacts.directory,
                    )
                if current.phase is not RecorderPhase.STARTING and not worker_active:
                    recovered = self._recover_dead_capture(current)
                    raise MeetingLifecycleError(_capture_failure_message(recovered.error_code))
                self._sleeper(self._settings.poll_interval_seconds)

            current = self._required_state(session_id)
            if not self._capture_worker_active(current) and not self._worker_pid_alive(current):
                # Nothing is left to wait for: the recorder never got going, so say so now
                # rather than leaving a STARTING session for status to explain later.
                recovered = self._recover_dead_capture(current)
                raise MeetingLifecycleError(_capture_failure_message(recovered.error_code))
            self._request_stop(state)
            raise MeetingLifecycleError(
                "PiKVM WebRTC audio did not become ready before the start timeout."
            )
        except KeyboardInterrupt:
            with contextlib.suppress(MeetingError):
                self._request_stop(state)
            raise

    def stop(self) -> MeetingStopResult:
        self._reap_worker_processes()
        state = self._state.read()
        if state is None:
            raise MeetingLifecycleError("No meeting recording is available to stop.")
        state = self._required_state(state.session_id)
        if state.phase is RecorderPhase.COMPLETED:
            return self._completed_result(state)

        if state.phase in {
            RecorderPhase.STARTING,
            RecorderPhase.RECORDING,
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
        }:
            self._request_stop(state)
            state = self._wait_for_capture(state.session_id)

        if state.phase in {RecorderPhase.DISCONNECTED, RecorderPhase.INTERRUPTED}:
            manifest_path = self._storage.artifact_path(
                state.session_directory,
                "manifest.json",
            )
            if not manifest_path.exists():
                state = self._recover_dead_capture(state)

        if state.phase is RecorderPhase.AUDIO_UNAVAILABLE:
            raise MeetingLifecycleError(
                "The selected PiKVM provided no incoming HDMI audio to process."
            )
        if state.phase is RecorderPhase.FAILED:
            raise MeetingLifecycleError(_capture_failure_message(state.error_code))

        process_lock = self._storage.artifact_path(
            state.session_directory,
            "processing.lock",
        )
        with _exclusive_processing_lock(process_lock):
            latest = self._required_state(state.session_id)
            if latest.phase is RecorderPhase.COMPLETED:
                return self._completed_result(latest)
            return self._process(latest)

    def reset_corrupt_state(self) -> MeetingStateResetResult:
        """Move an unreadable state file aside so start, stop, and status work again.

        Nothing is deleted: the file is renamed next to itself for inspection, and no session
        artifacts are touched. A readable state file is never moved; abandon it by session ID.
        """

        moved_to = self._state.set_aside_corrupt()
        return MeetingStateResetResult(state_path=self._state.path, moved_to=moved_to)

    def abandon(self, session_id: str) -> MeetingAbandonResult:
        """Release one exact stale session without deleting any local artifacts."""

        expected = session_id.strip()
        state = self._state.read()
        if state is None or state.session_id != expected:
            raise MeetingStateConflictError("The active meeting recorder state changed.")
        state = self._required_state(expected)
        if state.phase.terminal:
            abandoned = state
        else:
            try:
                self._storage.require_expected_session_directory(
                    state.session_directory,
                    kvm=state.kvm,
                    session_id=state.session_id,
                    started_at=state.started_at,
                )
            except MeetingError:
                abandoned = self._state.abandon(
                    state.session_id,
                    state.revision,
                    self._now(),
                )
            else:
                process_lock = self._storage.artifact_path(
                    state.session_directory,
                    "processing.lock",
                )
                with self._storage.capture_lock(state.session_directory) as capture_lock_acquired:
                    if not capture_lock_acquired:
                        raise MeetingLifecycleError(
                            "The meeting recorder is still running; stop it before abandoning "
                            "this session."
                        )
                    with _exclusive_processing_lock(process_lock):
                        latest = self._required_state(state.session_id)
                        abandoned = self._state.abandon(
                            latest.session_id,
                            latest.revision,
                            self._now(),
                        )
        return MeetingAbandonResult(
            session_id=abandoned.session_id,
            kvm=abandoned.kvm,
            directory=abandoned.session_directory,
        )

    def status(self) -> MeetingStatusResult:
        self._reap_worker_processes()
        state = self._state.read()
        if state is None:
            return MeetingStatusResult(state=None, worker_alive=False, elapsed_seconds=0.0)
        state = self._required_state(state.session_id)
        worker_alive = state.phase in {
            RecorderPhase.STARTING,
            RecorderPhase.RECORDING,
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
        } and self._capture_worker_active(state)
        worker_pid_alive = self._worker_pid_alive(state)
        endpoint = state.ended_at or self._now()
        elapsed = max(
            0.0,
            (endpoint - (state.recording_started_at or state.started_at)).total_seconds(),
        )
        worker_stale = False
        if (
            state.phase
            in {
                RecorderPhase.STARTING,
                RecorderPhase.RECORDING,
                RecorderPhase.STOP_REQUESTED,
                RecorderPhase.FINALIZING,
            }
            and not worker_alive
        ):
            if state.phase is RecorderPhase.STARTING:
                worker_stale = elapsed >= self._settings.start_handshake_timeout_seconds
            else:
                worker_stale = True
        return MeetingStatusResult(
            state=state,
            worker_alive=worker_alive,
            elapsed_seconds=elapsed,
            worker_stale=worker_stale,
            worker_pid_alive=worker_pid_alive,
        )

    def _wait_for_capture(self, session_id: str) -> MeetingRecorderState:
        deadline = self._monotonic() + self._settings.stop_wait_timeout_seconds
        waiting = {
            RecorderPhase.STARTING,
            RecorderPhase.RECORDING,
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
        }
        while self._monotonic() < deadline:
            state = self._required_state(session_id)
            if state.phase not in waiting:
                return state
            if not self._capture_worker_active(state):
                try:
                    return self._recover_dead_capture(state)
                except MeetingStateConflictError:
                    pass
            self._sleeper(self._settings.poll_interval_seconds)
        raise MeetingLifecycleError(
            "The meeting recorder is still finalizing; run meeting status, then stop again."
        )

    def _request_stop(self, state: MeetingRecorderState) -> None:
        requested = self._state.request_stop(state.session_id, self._now())
        stop_file = self._storage.artifact_path(state.session_directory, "stop.request")
        if requested.phase in {
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
        }:
            self._storage.prepare_output(stop_file, exist_ok=True)

    def _recover_dead_capture(self, state: MeetingRecorderState) -> MeetingRecorderState:
        """Recover only after the exact claimed recorder is proven absent.

        The state is re-read under the capture lock: a recorder that finalized between the
        caller's read and the lock has already moved on, and its good recording must not be
        forced to FAILED by a stale view.
        """

        with self._storage.capture_lock(state.session_directory) as capture_lock_acquired:
            if not capture_lock_acquired:
                raise MeetingStateConflictError("The meeting recorder is still running.")
            latest = self._required_state(state.session_id)
            if latest.phase not in _RECOVERABLE_PHASES:
                return latest
            return self._recover_dead_capture_with_lock(latest)

    def _recover_abandoned_capture(self) -> None:
        """Before a new start, close out a capture whose recorder is proven dead.

        Only a session with no live worker - lock free and PID gone (or never claimed past the
        handshake window) - is touched, and it is recovered rather than discarded: audio that
        was checkpointed becomes an INTERRUPTED session that stop can still process, and the
        new start is refused with that as the reason.
        """

        state = self._state.read()
        if state is None or state.phase not in CAPTURE_PHASES:
            return
        if self._capture_worker_active(state) or self._worker_pid_alive(state):
            return
        if state.worker_pid is None:
            elapsed = (self._now() - state.started_at).total_seconds()
            if elapsed < self._settings.start_handshake_timeout_seconds:
                return
        with contextlib.suppress(MeetingStateConflictError):
            self._recover_dead_capture(state)

    def _worker_pid_alive(self, state: MeetingRecorderState) -> bool:
        return state.worker_pid is not None and self._pid_is_alive(state.worker_pid)

    def _recover_dead_capture_with_lock(
        self,
        latest: MeetingRecorderState,
    ) -> MeetingRecorderState:
        manifest_path = self._storage.artifact_path(
            latest.session_directory,
            "manifest.json",
        )
        checkpoint_path = self._storage.artifact_path(
            latest.session_directory,
            "capture.checkpoint.json",
        )
        try:
            if manifest_path.exists():
                manifest = self._read_model(manifest_path, MeetingCaptureManifest)
            elif checkpoint_path.exists():
                checkpoint = self._read_model(checkpoint_path, MeetingCaptureCheckpoint)
                expected_started_at = latest.recording_started_at or latest.started_at
                if (
                    checkpoint.session_id != latest.session_id
                    or checkpoint.kvm != latest.kvm
                    or checkpoint.started_at != expected_started_at
                ):
                    raise MeetingLifecycleError(
                        "The protected meeting checkpoint does not match recorder state."
                    )
                for part in checkpoint.parts:
                    self._storage.input_artifact_path(
                        latest.session_directory,
                        part.filename,
                    )
                ended_at = checkpoint.started_at + timedelta(seconds=checkpoint.duration_seconds)
                manifest = MeetingCaptureManifest(
                    session_id=checkpoint.session_id,
                    kvm=checkpoint.kvm,
                    started_at=checkpoint.started_at,
                    ended_at=ended_at,
                    duration_seconds=checkpoint.duration_seconds,
                    interrupted=True,
                    interruption_code="recorder_process_stopped",
                    work_identity_name=checkpoint.work_identity_name,
                    work_identity_aliases=checkpoint.work_identity_aliases,
                    parts=checkpoint.parts,
                )
            else:
                return self._update_state(
                    latest.session_id,
                    phase=RecorderPhase.FAILED,
                    worker_pid=None,
                    ended_at=self._now(),
                    error_code="no_audio_before_stop",
                )
            expected_started_at = latest.recording_started_at or latest.started_at
            if (
                manifest.session_id != latest.session_id
                or manifest.kvm != latest.kvm
                or manifest.started_at != expected_started_at
            ):
                raise MeetingLifecycleError(
                    "The protected meeting manifest does not match recorder state."
                )
            for part in manifest.parts:
                self._storage.input_artifact_path(
                    latest.session_directory,
                    part.filename,
                )
            recovered = manifest.model_copy(
                update={
                    "interrupted": True,
                    "interruption_code": "recorder_process_stopped",
                }
            )
            self._storage.write_text(
                manifest_path,
                recovered.model_dump_json(indent=2) + "\n",
            )
            return self._update_state(
                latest.session_id,
                phase=(
                    RecorderPhase.INTERRUPTED if latest.phase in CAPTURE_PHASES else latest.phase
                ),
                recording_started_at=(latest.recording_started_at or recovered.started_at),
                worker_pid=None,
                heartbeat_at=latest.heartbeat_at,
                ended_at=recovered.ended_at,
                error_code="recorder_process_stopped",
            )
        except (MeetingError, ValidationError, OSError, ValueError) as exc:
            log_exception(_LOGGER, "Meeting capture recovery failed", exc)
            return self._update_state(
                latest.session_id,
                phase=RecorderPhase.FAILED,
                worker_pid=None,
                ended_at=self._now(),
                error_code="capture_recovery_failed",
            )

    def _process(self, state: MeetingRecorderState) -> MeetingStopResult:
        manifest_path = self._storage.artifact_path(state.session_directory, "manifest.json")
        transcript_path = self._storage.artifact_path(state.session_directory, "transcript.json")
        intelligence_path = self._storage.artifact_path(
            state.session_directory,
            "intelligence.json",
        )
        report_path = self._storage.artifact_path(state.session_directory, "report.md")
        try:
            manifest = self._read_model(manifest_path, MeetingCaptureManifest)
            expected_started_at = state.recording_started_at or state.started_at
            if (
                manifest.session_id != state.session_id
                or manifest.kvm != state.kvm
                or manifest.started_at != expected_started_at
            ):
                raise MeetingLifecycleError(
                    "The protected meeting manifest does not match the recorder state."
                )
            manifest_sha256 = fingerprint_model(manifest)
            self._update_state(
                state.session_id,
                phase=RecorderPhase.TRANSCRIBING,
                error_code=None,
            )
            if transcript_path.exists():
                transcript_artifact = self._read_model(
                    transcript_path,
                    TranscriptionArtifact,
                )
                if (
                    transcript_artifact.session_id != state.session_id
                    or transcript_artifact.manifest_sha256 != manifest_sha256
                ):
                    raise MeetingLifecycleError(
                        "The protected meeting transcription provenance is invalid."
                    )
                transcription = transcript_artifact.result
            else:
                parts = tuple(
                    AudioPart(
                        path=self._storage.input_artifact_path(
                            state.session_directory,
                            part.filename,
                        ),
                        offset_seconds=part.offset_seconds,
                    )
                    for part in manifest.parts
                )
                transcription = self._transcription_factory().transcribe(parts)
                transcript_artifact = TranscriptionArtifact(
                    session_id=state.session_id,
                    manifest_sha256=manifest_sha256,
                    result=transcription,
                )
                self._storage.write_text(
                    transcript_path,
                    transcript_artifact.model_dump_json(indent=2) + "\n",
                )

            self._update_state(state.session_id, phase=RecorderPhase.ANALYZING)
            transcript_sha256 = fingerprint_model(transcription.transcript)
            fresh_intelligence = False
            if intelligence_path.exists():
                intelligence_artifact = self._read_model(
                    intelligence_path,
                    IntelligenceArtifact,
                )
                if (
                    intelligence_artifact.session_id != state.session_id
                    or intelligence_artifact.manifest_sha256 != manifest_sha256
                    or intelligence_artifact.transcript_sha256 != transcript_sha256
                ):
                    raise MeetingLifecycleError(
                        "The protected meeting intelligence provenance is invalid."
                    )
                extracted = intelligence_artifact.result
            else:
                fresh_intelligence = True
                extracted = (
                    self._intelligence_factory().extract(
                        transcription.transcript,
                        work_identity=manifest.work_identity,
                    )
                    if transcription.transcript.segments
                    else no_speech_intelligence_result()
                )
            guarded = guard_meeting_intelligence(
                extracted.intelligence,
                transcription.transcript,
                work_identity=manifest.work_identity,
            )
            extracted = extracted.model_copy(update={"intelligence": guarded})
            if fresh_intelligence:
                intelligence_artifact = IntelligenceArtifact(
                    session_id=state.session_id,
                    manifest_sha256=manifest_sha256,
                    transcript_sha256=transcript_sha256,
                    result=extracted,
                )
                self._storage.write_text(
                    intelligence_path,
                    intelligence_artifact.model_dump_json(indent=2) + "\n",
                )

            metadata = MeetingMetadata(
                recording_id=manifest.session_id,
                kvm=manifest.kvm,
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                duration_seconds=manifest.duration_seconds,
                interrupted=manifest.interrupted,
            )
            report = render_meeting_report(
                metadata,
                transcription.transcript,
                extracted.intelligence,
            )
            self._storage.write_text(report_path, report)
            our_actions = sum(
                1
                for item in extracted.intelligence.action_items
                if item.owner_category is OwnerCategory.OUR_IDENTITY
            )
            possible_actions = sum(
                1
                for item in extracted.intelligence.action_items
                if item.owner_category is OwnerCategory.POSSIBLY_OUR_IDENTITY
            )
            blockers = sum(
                1
                for item in extracted.intelligence.blockers_and_risks
                if item.kind is RiskKind.BLOCKER
            )
            self._update_state(
                state.session_id,
                phase=RecorderPhase.COMPLETED,
                ended_at=manifest.ended_at,
                error_code=None,
                report_path=report_path,
                our_action_items=our_actions,
                possible_our_action_items=possible_actions,
                decisions=len(extracted.intelligence.decisions),
                blockers=blockers,
            )
            return MeetingStopResult(
                session_id=manifest.session_id,
                kvm=manifest.kvm,
                duration_seconds=manifest.duration_seconds,
                report_path=report_path,
                our_action_items=our_actions,
                possible_our_action_items=possible_actions,
                decisions=len(extracted.intelligence.decisions),
                blockers=blockers,
                interrupted=manifest.interrupted,
            )
        except KeyboardInterrupt:
            self._mark_processing_failed(state.session_id, "processing_interrupted")
            raise
        except MeetingLifecycleError:
            self._mark_processing_failed(state.session_id, "artifact_validation_failed")
            raise
        except (MeetingTranscriptionError, MeetingIntelligenceError) as exc:
            # These providers raise sanitized messages by construction, so the message can be
            # shown; the code says whether another `meeting stop` can succeed unchanged.
            code, retryable = _provider_failure(exc)
            log_exception(_LOGGER, f"Meeting processing failed ({code})", exc)
            self._mark_processing_failed(state.session_id, code)
            remedy = (
                "Finalized audio was preserved; run `pikvm-agent meeting stop` again to retry."
                if retryable
                else (
                    "This will not succeed on retry until the configuration or provider access "
                    "is fixed; finalized audio was preserved."
                )
            )
            raise MeetingLifecycleError(f"{str(exc).rstrip('.')}. {remedy}") from None
        except (MeetingError, ValidationError, OSError, ValueError) as exc:
            log_exception(_LOGGER, "Meeting processing failed", exc)
            self._mark_processing_failed(state.session_id, "provider_processing_failed")
            raise MeetingLifecycleError(
                "Meeting processing failed; finalized audio was preserved for retry."
            ) from None
        except Exception as exc:
            log_exception(_LOGGER, "Meeting processing crashed", exc)
            self._mark_processing_failed(state.session_id, "unexpected_local_error")
            raise MeetingLifecycleError(
                "An unexpected local error stopped meeting processing; audio was preserved."
            ) from None

    def _completed_result(self, state: MeetingRecorderState) -> MeetingStopResult:
        if state.report_path is None:
            raise MeetingLifecycleError("The completed meeting state has no report path.")
        expected_report = self._storage.input_artifact_path(
            state.session_directory,
            "report.md",
        )
        if state.report_path.resolve() != expected_report:
            raise MeetingLifecycleError("The completed meeting report path is invalid.")
        manifest = self._read_model(
            self._storage.artifact_path(state.session_directory, "manifest.json"),
            MeetingCaptureManifest,
        )
        expected_started_at = state.recording_started_at or state.started_at
        if (
            manifest.session_id != state.session_id
            or manifest.kvm != state.kvm
            or manifest.started_at != expected_started_at
        ):
            raise MeetingLifecycleError(
                "The protected meeting manifest does not match the completed state."
            )
        return MeetingStopResult(
            session_id=state.session_id,
            kvm=state.kvm,
            duration_seconds=manifest.duration_seconds,
            report_path=expected_report,
            our_action_items=state.our_action_items or 0,
            possible_our_action_items=state.possible_our_action_items or 0,
            decisions=state.decisions or 0,
            blockers=state.blockers or 0,
            interrupted=manifest.interrupted,
        )

    def _mark_processing_failed(self, session_id: str, error_code: str) -> None:
        with contextlib.suppress(MeetingError):
            self._update_state(
                session_id,
                phase=RecorderPhase.PROCESSING_FAILED,
                error_code=error_code,
            )

    def _required_state(self, session_id: str) -> MeetingRecorderState:
        state = self._state.read()
        if state is None or state.session_id != session_id:
            raise MeetingStateConflictError("The active meeting recorder state changed.")
        expected_directory = self._storage.session_directory(
            kvm=state.kvm,
            session_id=state.session_id,
            started_at=state.started_at,
        )
        if state.session_directory != expected_directory:
            raise MeetingStateConflictError(
                "The active meeting recorder directory does not match its session."
            )
        return state

    def _capture_worker_active(self, state: MeetingRecorderState) -> bool:
        try:
            return self._capture_lock_held(state.session_directory)
        except MeetingStorageError:
            if not state.session_directory.exists():
                return False
            raise

    def _update_state(self, session_id: str, **changes: object) -> MeetingRecorderState:
        for _ in range(8):
            current = self._required_state(session_id)
            updated = replace(
                current,
                updated_at=max(self._now(), current.updated_at),
                **cast(dict[str, Any], changes),
            )
            try:
                return self._state.compare_and_set(
                    session_id,
                    current.revision,
                    updated,
                )
            except MeetingStateConflictError:
                latest = self._state.read()
                if latest is None or latest.session_id != session_id:
                    raise
        raise MeetingStateConflictError("Meeting recorder state remained busy.")

    def _read_model(self, path: Path, model: type[_ModelT]) -> _ModelT:
        try:
            content = self._storage.read_text(path)
        except MeetingError:
            raise MeetingLifecycleError("A protected meeting artifact could not be read.") from None
        try:
            return model.model_validate_json(content)
        except (ValidationError, ValueError, TypeError):
            raise MeetingLifecycleError(
                "A protected meeting artifact is invalid; it was not overwritten."
            ) from None

    def _default_spawn_worker(self, session_id: str) -> int:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "work_agent",
                "meeting",
                "_capture",
                "--session-id",
                session_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        self._worker_processes.append(process)
        return process.pid

    def _reap_worker_processes(self) -> None:
        self._worker_processes = [
            process for process in self._worker_processes if process.poll() is None
        ]

    def _default_transcription(self) -> TranscriptionProvider:
        if self._settings.transcription_provider is MeetingProvider.DEEPGRAM:
            from work_agent.meeting.deepgram import DeepgramTranscriptionProvider

            return DeepgramTranscriptionProvider(
                api_key=self._settings.deepgram_api_key,
                model=self._settings.deepgram_model,
                language=self._settings.deepgram_language,
                request_timeout_seconds=self._settings.transcription_timeout_seconds,
                max_retries=self._settings.transcription_max_retries,
            )
        if self._settings.transcription_provider is not MeetingProvider.OPENAI:
            raise MeetingLifecycleError("The configured transcription provider is unsupported.")
        return OpenAITranscriptionProvider(
            api_key=self._settings.openai_api_key,
            model=self._settings.transcription_model,
            request_timeout_seconds=self._settings.transcription_timeout_seconds,
            max_retries=self._settings.transcription_max_retries,
        )

    def _default_intelligence(self) -> MeetingIntelligenceProvider:
        if self._settings.intelligence_provider is not MeetingProvider.OPENAI:
            raise MeetingLifecycleError(
                "The configured meeting-intelligence provider is unsupported."
            )
        return OpenAIMeetingIntelligenceProvider(
            api_key=self._settings.openai_api_key,
            model=self._settings.meeting_model,
            service_tier=self._settings.meeting_service_tier.value,
            reasoning_effort=self._settings.meeting_reasoning_effort.value,
            request_timeout_seconds=self._settings.intelligence_timeout_seconds,
            max_retries=self._settings.intelligence_max_retries,
        )


@contextmanager
def _exclusive_processing_lock(path: Path) -> Iterator[None]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("meeting processing lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MeetingStateConflictError("This meeting is already being processed.") from None
        yield
    except MeetingError:
        raise
    except OSError:
        raise MeetingStorageError("The protected meeting processing lock is unavailable.") from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _capture_failure_message(error_code: str | None) -> str:
    if error_code == "audio_unavailable":
        return "The selected PiKVM provided no incoming HDMI audio."
    if error_code == "interactive_totp_unsupported":
        return "Detached meeting capture requires Keychain TOTP or a profile without 2FA."
    if error_code == "webrtc_connection_failed":
        return "The PiKVM WebRTC audio connection failed."
    if error_code == "pikvm_unreachable":
        return "The PiKVM could not be reached for meeting audio; check the network and URL."
    if error_code == "pikvm_auth_failed":
        return "The PiKVM rejected this profile's credentials for meeting audio."
    if error_code == "capture_local_failed":
        return "The Mac could not safely finalize local meeting audio; artifacts were preserved."
    if error_code == "recorder_process_stopped":
        return "The meeting recorder exited; finalized audio was preserved for meeting stop."
    if error_code == "no_audio_before_stop":
        return "The meeting recorder stopped before any incoming audio was finalized."
    if error_code == "capture_recovery_failed":
        return "The stopped meeting artifacts could not be safely recovered."
    if error_code == "abandoned_by_user":
        return "The meeting session was explicitly abandoned; its artifacts were preserved."
    return "The protected meeting recorder stopped before audio capture was ready."


_NON_RETRYABLE_PROVIDER_ERRORS: tuple[type[MeetingError], ...] = (
    TranscriptionAuthenticationError,
    TranscriptionPermissionError,
    TranscriptionRequestError,
    TranscriptionConfigurationError,
    TranscriptionInputError,
    IntelligenceAuthenticationError,
    IntelligencePermissionError,
    IntelligenceRequestError,
    IntelligenceConfigurationError,
)


def _provider_failure(exc: MeetingError) -> tuple[str, bool]:
    """Sanitized state error code and whether an unchanged retry could succeed."""

    stage = "transcription" if isinstance(exc, MeetingTranscriptionError) else "intelligence"
    if isinstance(exc, (TranscriptionAuthenticationError, IntelligenceAuthenticationError)):
        return f"{stage}_auth_failed", False
    if isinstance(exc, (TranscriptionPermissionError, IntelligencePermissionError)):
        return f"{stage}_permission_denied", False
    if isinstance(exc, (TranscriptionConfigurationError, IntelligenceConfigurationError)):
        return f"{stage}_configuration_invalid", False
    if isinstance(exc, TranscriptionInputError):
        return f"{stage}_input_invalid", False
    if isinstance(exc, (TranscriptionRequestError, IntelligenceRequestError)):
        return f"{stage}_request_rejected", False
    return f"{stage}_provider_unavailable", not isinstance(exc, _NON_RETRYABLE_PROVIDER_ERRORS)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
