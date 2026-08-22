from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from work_agent.diagnostics import log_exception
from work_agent.meeting.capture import (
    CaptureResult,
    MeetingAudioUnavailableError,
    MeetingCaptureAuthError,
    MeetingCaptureConnectionError,
    MeetingCaptureLocalError,
    MeetingCaptureUnreachableError,
    PiKVMWebRTCAudioCapture,
    RecordedAudioPart,
)
from work_agent.meeting.config import MeetingSettings
from work_agent.meeting.errors import MeetingError, MeetingStateConflictError
from work_agent.meeting.manifest import (
    CapturedAudioPart,
    MeetingCaptureCheckpoint,
    MeetingCaptureManifest,
)
from work_agent.meeting.state import MeetingRecorderState, MeetingStateStore, RecorderPhase
from work_agent.meeting.storage import MeetingStorage
from work_agent.pikvm import PiKVMSettings, TotpProviderKind, build_totp_provider

# Lifecycle events and exception classes only. Never SDP, credentials, or anything recorded.
_LOGGER = logging.getLogger(__name__)


class _WorkerState:
    def __init__(self, store: MeetingStateStore, session_id: str, worker_pid: int) -> None:
        self._store = store
        self._session_id = session_id
        self._worker_pid = worker_pid

    def update(
        self,
        *,
        expected_phases: frozenset[RecorderPhase] | None = None,
        **changes: object,
    ) -> MeetingRecorderState:
        for _ in range(8):
            current = self._store.read()
            if current is None or current.session_id != self._session_id:
                raise MeetingStateConflictError("The active meeting recorder state changed.")
            if current.worker_pid != self._worker_pid:
                raise MeetingStateConflictError("The meeting recorder worker ownership changed.")
            if expected_phases is not None and current.phase not in expected_phases:
                return current
            updated = replace(
                current,
                updated_at=max(datetime.now(UTC), current.updated_at),
                **cast(dict[str, Any], changes),
            )
            try:
                return self._store.compare_and_set(
                    self._session_id,
                    current.revision,
                    updated,
                )
            except MeetingStateConflictError:
                continue
        raise MeetingStateConflictError("Meeting recorder state remained busy.")


async def _watch_stop_file(
    path: Path,
    stop_requested: asyncio.Event,
    store: MeetingStateStore,
    worker_state: _WorkerState,
    session_id: str,
) -> None:
    while not stop_requested.is_set():
        try:
            current = store.read()
        except MeetingError:
            stop_requested.set()
            return
        if current is None or current.session_id != session_id:
            stop_requested.set()
            return
        if path.exists() and current.phase in {
            RecorderPhase.STARTING,
            RecorderPhase.RECORDING,
        }:
            current = store.request_stop(current.session_id, datetime.now(UTC))
        if current.phase in {RecorderPhase.STOP_REQUESTED, RecorderPhase.FINALIZING}:
            worker_state.update(
                expected_phases=frozenset({RecorderPhase.STOP_REQUESTED}),
                phase=RecorderPhase.FINALIZING,
            )
            stop_requested.set()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_requested.wait(), timeout=0.25)


def _manifest(
    state: MeetingRecorderState,
    result: CaptureResult,
    *,
    ended_at: datetime,
    interrupted_by_signal: bool,
    settings: PiKVMSettings,
) -> MeetingCaptureManifest:
    identity = settings.work_identity
    return MeetingCaptureManifest(
        session_id=state.session_id,
        kvm=state.kvm,
        started_at=state.recording_started_at or state.started_at,
        ended_at=ended_at,
        duration_seconds=result.duration_seconds,
        interrupted=result.interrupted or interrupted_by_signal,
        interruption_code=(
            result.interruption_code
            if result.interruption_code is not None
            else ("local_interruption" if interrupted_by_signal else None)
        ),
        reconnects=result.reconnects,
        work_identity_name=identity.name if identity is not None else None,
        work_identity_aliases=identity.aliases if identity is not None else (),
        parts=tuple(
            CapturedAudioPart(
                filename=part.path.name,
                offset_seconds=part.offset_seconds,
                duration_seconds=part.duration_seconds,
                degraded=part.degraded,
            )
            for part in result.parts
        ),
    )


def _checkpoint_is_recoverable(
    storage: MeetingStorage,
    checkpoint_path: Path,
    state: MeetingRecorderState | None,
) -> bool:
    if state is None or state.recording_started_at is None:
        return False
    try:
        checkpoint = MeetingCaptureCheckpoint.model_validate_json(
            storage.read_text(checkpoint_path)
        )
        if (
            checkpoint.session_id != state.session_id
            or checkpoint.kvm != state.kvm
            or checkpoint.started_at != state.recording_started_at
        ):
            return False
        for part in checkpoint.parts:
            storage.input_artifact_path(state.session_directory, part.filename)
    except (MeetingError, ValueError, TypeError):
        return False
    return True


def _safe_state(store: MeetingStateStore) -> MeetingRecorderState | None:
    try:
        return store.read()
    except MeetingError:
        return None


def _mark_unclaimed_worker_failed(
    store: MeetingStateStore,
    session_id: str,
    error_code: str,
) -> None:
    for _ in range(8):
        current = _safe_state(store)
        if (
            current is None
            or current.session_id != session_id
            or current.phase is not RecorderPhase.STARTING
            or current.worker_pid is not None
        ):
            return
        ended_at = max(datetime.now(UTC), current.updated_at)
        failed = replace(
            current,
            phase=RecorderPhase.FAILED,
            ended_at=ended_at,
            updated_at=ended_at,
            error_code=error_code,
        )
        try:
            store.compare_and_set(session_id, current.revision, failed)
            return
        except MeetingStateConflictError:
            continue
        except MeetingError:
            return


async def _capture_worker(session_id: str, settings: MeetingSettings) -> int:
    store = MeetingStateStore(settings.state_path)
    state = store.read()
    if state is None or state.session_id != session_id:
        return 1
    worker_pid = os.getpid()
    storage = MeetingStorage(settings.data_directory)
    try:
        session_directory = storage.require_expected_session_directory(
            state.session_directory,
            kvm=state.kvm,
            session_id=state.session_id,
            started_at=state.started_at,
        )
        stop_file = storage.artifact_path(session_directory, "stop.request")
        checkpoint_file = storage.artifact_path(
            session_directory,
            "capture.checkpoint.json",
        )
    except MeetingError:
        _mark_unclaimed_worker_failed(store, session_id, "artifact_setup_failed")
        return 1

    capture_lock_stack: contextlib.ExitStack | None = None
    loop = asyncio.get_running_loop()
    lock_deadline = loop.time() + settings.start_handshake_timeout_seconds
    while capture_lock_stack is None:
        candidate_stack = contextlib.ExitStack()
        try:
            acquired = candidate_stack.enter_context(storage.capture_lock(session_directory))
        except MeetingError:
            candidate_stack.close()
            _mark_unclaimed_worker_failed(store, session_id, "artifact_setup_failed")
            return 1
        if acquired:
            capture_lock_stack = candidate_stack
            break
        candidate_stack.close()
        current = _safe_state(store)
        if (
            current is None
            or current.session_id != session_id
            or current.phase is not RecorderPhase.STARTING
            or current.worker_pid not in {None, worker_pid}
            or loop.time() >= lock_deadline
        ):
            return 1
        await asyncio.sleep(settings.poll_interval_seconds)

    try:
        state = store.claim_worker(session_id, worker_pid, datetime.now(UTC))
    except MeetingError:
        capture_lock_stack.close()
        return 1
    worker_state = _WorkerState(store, session_id, worker_pid)

    stop_requested = asyncio.Event()
    interrupted_by_signal = False
    captured_parts: list[CapturedAudioPart] = []

    def interrupt() -> None:
        nonlocal interrupted_by_signal
        interrupted_by_signal = True
        stop_requested.set()

    installed_signals: list[signal.Signals] = []
    try:
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(selected_signal, interrupt)
                installed_signals.append(selected_signal)

        pikvm = PiKVMSettings.from_env(state.kvm)
        if pikvm.totp_required and (
            pikvm.totp_provider is TotpProviderKind.INTERACTIVE or pikvm.totp_interactive_fallback
        ):
            worker_state.update(
                phase=RecorderPhase.FAILED,
                worker_pid=None,
                error_code="interactive_totp_unsupported",
            )
            return 1
        totp_provider = build_totp_provider(pikvm) if pikvm.totp_required else None
        capture = PiKVMWebRTCAudioCapture(
            pikvm,
            totp_provider=totp_provider,
            signaling_timeout_seconds=settings.capture_signaling_timeout_seconds,
            audio_start_timeout_seconds=settings.capture_audio_start_timeout_seconds,
            segment_seconds=settings.capture_segment_seconds,
        )
        stop_watcher = asyncio.create_task(
            _watch_stop_file(stop_file, stop_requested, store, worker_state, session_id)
        )
        stop_watcher.add_done_callback(
            lambda task: _stop_watcher_finished(task, capture, stop_requested)
        )

        def ready() -> None:
            current = store.read()
            if current is None or current.session_id != session_id:
                stop_requested.set()
                return
            store.mark_recording_started(
                session_id,
                worker_pid,
                datetime.now(UTC),
            )

        def heartbeat() -> None:
            worker_state.update(heartbeat_at=datetime.now(UTC))

        def checkpoint(part: RecordedAudioPart) -> None:
            captured_parts.append(
                CapturedAudioPart(
                    filename=part.path.name,
                    offset_seconds=part.offset_seconds,
                    duration_seconds=part.duration_seconds,
                    degraded=part.degraded,
                )
            )
            _LOGGER.info(
                "Meeting worker checkpointed part %d (%.1fs%s)",
                len(captured_parts),
                part.duration_seconds,
                ", degraded" if part.degraded else "",
            )
            current = store.read()
            if current is None or current.session_id != session_id:
                raise MeetingStateConflictError("The active meeting recorder state changed.")
            if current.recording_started_at is None:
                inferred_start = datetime.now(UTC) - timedelta(
                    seconds=part.offset_seconds + part.duration_seconds
                )
                current = store.mark_recording_started(
                    session_id,
                    worker_pid,
                    inferred_start,
                )
            recording_started_at = current.recording_started_at
            if recording_started_at is None:
                raise MeetingStateConflictError(
                    "The meeting recording start was not durably recorded."
                )
            identity = pikvm.work_identity
            durable = MeetingCaptureCheckpoint(
                session_id=current.session_id,
                kvm=current.kvm,
                started_at=recording_started_at,
                work_identity_name=identity.name if identity is not None else None,
                work_identity_aliases=identity.aliases if identity is not None else (),
                parts=tuple(captured_parts),
            )
            storage.write_text(
                checkpoint_file,
                durable.model_dump_json(indent=2) + "\n",
            )

        def reconnected(count: int) -> None:
            _LOGGER.warning("Meeting worker lost the PiKVM audio session; reconnect %d", count)

        _LOGGER.info("Meeting worker %d claimed session; starting capture", worker_pid)
        try:
            result = await capture.record(
                session_directory,
                stop_requested=stop_requested,
                on_ready=ready,
                on_part=checkpoint,
                on_heartbeat=heartbeat,
                on_reconnect=reconnected,
            )
        finally:
            stop_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_watcher

        ended_at = datetime.now(UTC)
        latest = store.read()
        if latest is None or latest.session_id != session_id:
            return 1
        manifest = _manifest(
            latest,
            result,
            ended_at=ended_at,
            interrupted_by_signal=interrupted_by_signal,
            settings=pikvm,
        )
        storage.write_text(
            storage.artifact_path(session_directory, "manifest.json"),
            manifest.model_dump_json(indent=2) + "\n",
        )
        final_phase = (
            RecorderPhase.INTERRUPTED
            if interrupted_by_signal
            else (
                RecorderPhase.DISCONNECTED
                if manifest.interrupted
                else RecorderPhase.READY_FOR_PROCESSING
            )
        )
        worker_state.update(
            phase=final_phase,
            worker_pid=None,
            heartbeat_at=ended_at,
            ended_at=ended_at,
            error_code=manifest.interruption_code,
        )
        _LOGGER.info(
            "Meeting worker finished: phase=%s parts=%d duration=%.1fs reconnects=%d code=%s",
            final_phase.value,
            len(manifest.parts),
            manifest.duration_seconds,
            manifest.reconnects,
            manifest.interruption_code,
        )
        return 0
    except MeetingAudioUnavailableError as exc:
        log_exception(_LOGGER, "Meeting worker found no incoming audio", exc)
        worker_state.update(
            phase=RecorderPhase.AUDIO_UNAVAILABLE,
            worker_pid=None,
            ended_at=datetime.now(UTC),
            error_code="audio_unavailable",
        )
        return 1
    except MeetingCaptureLocalError as exc:
        log_exception(_LOGGER, "Meeting worker local capture failure", exc)
        recoverable = _checkpoint_is_recoverable(storage, checkpoint_file, _safe_state(store))
        worker_state.update(
            phase=(RecorderPhase.INTERRUPTED if recoverable else RecorderPhase.FAILED),
            worker_pid=None,
            ended_at=datetime.now(UTC),
            error_code="capture_local_failed",
        )
        return 1
    except MeetingCaptureConnectionError as exc:
        log_exception(_LOGGER, "Meeting worker could not keep the PiKVM audio session", exc)
        worker_state.update(
            phase=RecorderPhase.FAILED,
            worker_pid=None,
            ended_at=datetime.now(UTC),
            error_code=_connection_error_code(exc),
        )
        return 1
    except (MeetingError, OSError, ValueError) as exc:
        log_exception(_LOGGER, "Meeting worker failed", exc)
        recoverable = _checkpoint_is_recoverable(storage, checkpoint_file, _safe_state(store))
        with contextlib.suppress(MeetingError):
            worker_state.update(
                phase=(RecorderPhase.INTERRUPTED if recoverable else RecorderPhase.FAILED),
                worker_pid=None,
                ended_at=datetime.now(UTC),
                error_code=("capture_interrupted" if captured_parts else "capture_failed"),
            )
        return 1
    except Exception as exc:
        log_exception(_LOGGER, "Meeting worker crashed", exc)
        recoverable = _checkpoint_is_recoverable(storage, checkpoint_file, _safe_state(store))
        with contextlib.suppress(MeetingError):
            worker_state.update(
                phase=(RecorderPhase.INTERRUPTED if recoverable else RecorderPhase.FAILED),
                worker_pid=None,
                ended_at=datetime.now(UTC),
                error_code=("capture_interrupted" if captured_parts else "unexpected_local_error"),
            )
        return 1
    finally:
        for selected_signal in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(selected_signal)
        capture_lock_stack.close()


def _connection_error_code(exc: MeetingCaptureConnectionError) -> str:
    if isinstance(exc, MeetingCaptureAuthError):
        return "pikvm_auth_failed"
    if isinstance(exc, MeetingCaptureUnreachableError):
        return "pikvm_unreachable"
    return "webrtc_connection_failed"


def _stop_watcher_finished(
    task: asyncio.Task[None],
    capture: PiKVMWebRTCAudioCapture,
    stop_requested: asyncio.Event,
) -> None:
    """A crashed stop watcher would otherwise leave a recording nobody can stop.

    Its failure is forwarded to the capture as a local fatal error, so the audio recorded so far
    is finalized and the session is reported as interrupted instead of running on unattended.
    """

    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        log_exception(_LOGGER, "Meeting worker stop watcher failed", exc)
        capture.abort(
            MeetingCaptureLocalError(
                "The meeting recorder could no longer watch for a stop request."
            )
        )
        stop_requested.set()


def run_capture_worker(session_id: str, settings: MeetingSettings | None = None) -> int:
    """Run the private detached recorder entry point without printing sensitive data."""

    os.umask(0o077)
    selected = settings
    try:
        selected = selected or MeetingSettings.from_env()
        return asyncio.run(_capture_worker(session_id, selected))
    except Exception as exc:
        log_exception(_LOGGER, "Meeting worker failed outside the capture loop", exc)
        if selected is not None:
            _mark_top_level_worker_failed(session_id, selected)
        return 1


def _mark_top_level_worker_failed(session_id: str, settings: MeetingSettings) -> None:
    """Best-effort sanitized state update for a crash outside the async worker."""

    try:
        store = MeetingStateStore(settings.state_path)
        current = store.read()
        if current is None or current.session_id != session_id:
            return
        worker_pid = os.getpid()
        if current.worker_pid is None and current.phase is RecorderPhase.STARTING:
            current = store.claim_worker(session_id, worker_pid, datetime.now(UTC))
        if current.worker_pid != worker_pid or current.phase not in {
            RecorderPhase.STARTING,
            RecorderPhase.RECORDING,
            RecorderPhase.STOP_REQUESTED,
            RecorderPhase.FINALIZING,
        }:
            return
        _WorkerState(store, session_id, worker_pid).update(
            phase=RecorderPhase.FAILED,
            worker_pid=None,
            ended_at=datetime.now(UTC),
            error_code="unexpected_local_error",
        )
    except Exception:
        return
