from __future__ import annotations

import asyncio
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from work_agent.meeting import service as service_module
from work_agent.meeting import worker as worker_module
from work_agent.meeting.capture import (
    CaptureResult,
    MeetingCaptureAuthError,
    MeetingCaptureConnectionError,
    MeetingCaptureLocalError,
    MeetingCaptureUnreachableError,
    RecordedAudioPart,
)
from work_agent.meeting.config import MeetingSettings
from work_agent.meeting.errors import MeetingError, MeetingStateConflictError
from work_agent.meeting.intelligence import IntelligenceAuthenticationError
from work_agent.meeting.manifest import (
    CapturedAudioPart,
    MeetingCaptureCheckpoint,
    MeetingCaptureManifest,
)
from work_agent.meeting.models import (
    IntelligenceResult,
    MeetingIntelligence,
    ProviderUsage,
    Transcript,
    TranscriptionResult,
    TranscriptionUsage,
    TranscriptSegment,
    TranscriptSpeaker,
)
from work_agent.meeting.service import MeetingLifecycleError, MeetingService
from work_agent.meeting.state import MeetingRecorderState, MeetingStateStore, RecorderPhase
from work_agent.meeting.storage import MeetingArtifacts, MeetingStorage
from work_agent.meeting.transcription import (
    TranscriptionAuthenticationError,
    TranscriptionNetworkError,
    TranscriptionPermissionError,
    TranscriptionRequestError,
)
from work_agent.pikvm import TotpProviderKind, WorkIdentity

_STARTED = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> MeetingSettings:
    return MeetingSettings(
        openai_api_key="unused-test-key",
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state" / "meeting.json",
        capture_signaling_timeout_seconds=0.2,
        capture_audio_start_timeout_seconds=0.2,
        start_handshake_timeout_seconds=1,
        stop_wait_timeout_seconds=1,
        poll_interval_seconds=0.01,
    )


def _reserve_session(
    settings: MeetingSettings,
    *,
    kvm: str = "heidrick",
    phase: RecorderPhase = RecorderPhase.STARTING,
) -> tuple[MeetingStateStore, MeetingStorage, MeetingArtifacts, MeetingRecorderState]:
    storage = MeetingStorage(settings.data_directory)
    artifacts = storage.create_session(
        kvm=kvm,
        session_id="meeting-20260818T100000Z-test",
        started_at=_STARTED,
    )
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-test",
        kvm=kvm,
        phase=phase,
        started_at=_STARTED,
        updated_at=_STARTED,
        session_directory=artifacts.directory,
        worker_pid=4321 if phase is RecorderPhase.RECORDING else None,
        ended_at=(
            _STARTED + timedelta(seconds=5)
            if phase
            in {
                RecorderPhase.READY_FOR_PROCESSING,
                RecorderPhase.DISCONNECTED,
                RecorderPhase.INTERRUPTED,
                RecorderPhase.PROCESSING_FAILED,
                RecorderPhase.COMPLETED,
            }
            else None
        ),
    )
    store = MeetingStateStore(settings.state_path)
    store.reserve(state)
    return store, storage, artifacts, state


def _write_manifest(
    storage: MeetingStorage,
    artifacts: MeetingArtifacts,
    state: MeetingRecorderState,
) -> MeetingCaptureManifest:
    audio = storage.artifact_path(artifacts.directory, "audio-0001.ogg")
    audio.write_bytes(b"protected-opus-audio")
    audio.chmod(0o600)
    manifest = MeetingCaptureManifest(
        session_id=state.session_id,
        kvm=state.kvm,
        started_at=state.started_at,
        ended_at=state.started_at + timedelta(seconds=5),
        duration_seconds=5,
        parts=(
            CapturedAudioPart(
                filename=audio.name,
                offset_seconds=0,
                duration_seconds=5,
            ),
        ),
    )
    storage.write_text(artifacts.manifest, manifest.model_dump_json(indent=2) + "\n")
    return manifest


def _write_checkpoint(
    storage: MeetingStorage,
    artifacts: MeetingArtifacts,
    state: MeetingRecorderState,
    *,
    identity: WorkIdentity | None = None,
) -> MeetingCaptureCheckpoint:
    audio = storage.artifact_path(artifacts.directory, "audio-0001.ogg")
    audio.write_bytes(b"protected-opus-audio")
    audio.chmod(0o600)
    checkpoint = MeetingCaptureCheckpoint(
        session_id=state.session_id,
        kvm=state.kvm,
        started_at=state.started_at,
        work_identity_name=identity.name if identity is not None else None,
        work_identity_aliases=identity.aliases if identity is not None else (),
        parts=(
            CapturedAudioPart(
                filename=audio.name,
                offset_seconds=0,
                duration_seconds=5,
            ),
        ),
    )
    storage.write_text(
        storage.artifact_path(artifacts.directory, "capture.checkpoint.json"),
        checkpoint.model_dump_json(indent=2) + "\n",
    )
    return checkpoint


def _transcription() -> TranscriptionResult:
    return TranscriptionResult(
        transcript=Transcript(
            duration_seconds=5,
            language="en",
            speakers=[TranscriptSpeaker(id="speaker-1", label="Speaker 1")],
            segments=[
                TranscriptSegment(
                    id="segment-1",
                    start_seconds=0,
                    end_seconds=1,
                    speaker_id="speaker-1",
                    text="Sensitive meeting words stay in protected artifacts.",
                )
            ],
        ),
        provider="test",
        model="test-transcriber",
        latency_seconds=0,
        retries=0,
        usage=TranscriptionUsage(seconds=5),
    )


def _intelligence() -> IntelligenceResult:
    return IntelligenceResult(
        intelligence=MeetingIntelligence(
            summary="A private summary.",
            action_items=[],
            decisions=[],
            blockers_and_risks=[],
            open_questions=[],
            references=[],
            follow_ups=[],
        ),
        provider="test",
        model="test-reasoner",
        service_tier="default",
        latency_seconds=0,
        retries=0,
        usage=ProviderUsage(),
    )


def test_start_waits_for_the_worker_recording_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = MeetingStateStore(settings.state_path)
    spawned: list[str] = []
    monkeypatch.setattr(
        service_module, "configured_pikvm_profiles", lambda: ("heidrick", "nbc_kvm")
    )

    def spawn(session_id: str) -> int:
        spawned.append(session_id)
        claimed = store.claim_worker(session_id, 9876, _STARTED)
        store.compare_and_set(
            session_id,
            claimed.revision,
            replace(
                claimed,
                phase=RecorderPhase.RECORDING,
                recording_started_at=_STARTED,
            ),
        )
        return 9876

    result = MeetingService(
        settings,
        state_store=store,
        spawn_worker=spawn,
        capture_lock_held=lambda _: True,
        now=lambda: _STARTED,
    ).start(" HEIDRICK ")

    assert spawned == [result.session_id]
    assert result.kvm == "heidrick"
    assert result.started_at == _STARTED
    assert result.directory.is_relative_to(settings.data_directory / "heidrick")
    assert store.read() is not None
    assert store.read().phase is RecorderPhase.RECORDING  # type: ignore[union-attr]


def test_already_recording_rejection_does_not_create_an_orphan_for_another_kvm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, original = _reserve_session(settings, phase=RecorderPhase.RECORDING)
    monkeypatch.setattr(
        service_module, "configured_pikvm_profiles", lambda: ("heidrick", "nbc_kvm")
    )
    service = MeetingService(
        settings,
        state_store=store,
        capture_lock_held=lambda _: True,
        pid_is_alive=lambda pid: pid == 4321,
        now=lambda: _STARTED,
    )

    with pytest.raises(MeetingStateConflictError, match="already recording from heidrick"):
        service.start("nbc_kvm")

    assert store.read() == original
    assert not (settings.data_directory / "nbc_kvm").exists()


def test_start_recovers_a_proven_dead_capture_instead_of_refusing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RECORDING session whose worker is gone and lock is free is not "already recording"."""

    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(settings, phase=RecorderPhase.RECORDING)
    _write_checkpoint(storage, artifacts, state)
    monkeypatch.setattr(
        service_module, "configured_pikvm_profiles", lambda: ("heidrick", "nbc_kvm")
    )
    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        capture_lock_held=lambda _: False,
        pid_is_alive=lambda pid: False,
        now=lambda: _STARTED + timedelta(minutes=5),
    )

    # Checkpointed audio is recovered, not discarded: the new start is refused with the honest
    # reason, and stop can still process what was captured.
    with pytest.raises(MeetingStateConflictError) as caught:
        service.start("nbc_kvm")

    assert "still interrupted" in str(caught.value)
    recovered = store.read()
    assert recovered is not None
    assert recovered.phase is RecorderPhase.INTERRUPTED
    assert recovered.worker_pid is None
    assert artifacts.manifest.is_file()


def test_start_replaces_a_dead_capture_that_never_produced_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, state = _reserve_session(settings, phase=RecorderPhase.RECORDING)
    monkeypatch.setattr(
        service_module, "configured_pikvm_profiles", lambda: ("heidrick", "nbc_kvm")
    )
    spawned: list[str] = []

    def spawn(session_id: str) -> int:
        spawned.append(session_id)
        claimed = store.claim_worker(session_id, 9876, _STARTED + timedelta(minutes=5))
        store.compare_and_set(
            session_id,
            claimed.revision,
            replace(
                claimed,
                phase=RecorderPhase.RECORDING,
                recording_started_at=_STARTED + timedelta(minutes=5),
            ),
        )
        return 9876

    result = MeetingService(
        settings,
        state_store=store,
        spawn_worker=spawn,
        capture_lock_held=lambda _: bool(spawned),
        pid_is_alive=lambda pid: pid == 9876,
        now=lambda: _STARTED + timedelta(minutes=5),
    ).start("nbc_kvm")

    assert result.kvm == "nbc_kvm"
    assert result.session_id != state.session_id
    assert spawned == [result.session_id]


def test_start_handshake_timeout_with_no_live_worker_fails_the_session_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = MeetingStateStore(settings.state_path)
    storage = MeetingStorage(settings.data_directory)
    monkeypatch.setattr(service_module, "configured_pikvm_profiles", lambda: ("heidrick",))
    clock = [0.0]

    def sleep(_: float) -> None:
        clock[0] += 0.5

    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        spawn_worker=lambda _: 4321,
        sleeper=sleep,
        monotonic=lambda: clock[0],
        now=lambda: _STARTED,
        capture_lock_held=lambda _: False,
        pid_is_alive=lambda pid: False,
    )

    with pytest.raises(MeetingLifecycleError, match="stopped before any incoming audio"):
        service.start("heidrick")

    state = store.read()
    assert state is not None
    assert state.phase is RecorderPhase.FAILED
    assert state.error_code == "no_audio_before_stop"
    assert not storage.artifact_path(state.session_directory, "stop.request").exists()


def test_ctrl_c_during_start_handshake_requests_detached_worker_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = MeetingStateStore(settings.state_path)
    storage = MeetingStorage(settings.data_directory)
    monkeypatch.setattr(service_module, "configured_pikvm_profiles", lambda: ("heidrick",))

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        spawn_worker=lambda _: 4321,
        sleeper=interrupt,
        monotonic=lambda: 0,
        now=lambda: _STARTED,
        capture_lock_held=lambda _: True,
        pid_is_alive=lambda pid: pid == 4321,
    )

    with pytest.raises(KeyboardInterrupt):
        service.start("heidrick")

    state = store.read()
    assert state is not None
    assert storage.artifact_path(state.session_directory, "stop.request").is_file()


def test_status_is_local_only_and_does_not_build_processing_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, _ = _reserve_session(settings, phase=RecorderPhase.RECORDING)
    monkeypatch.setattr(
        service_module,
        "configured_pikvm_profiles",
        lambda: pytest.fail("status must not inspect PiKVM profiles"),
    )

    def forbidden_provider() -> Any:
        pytest.fail("status must not instantiate a provider")

    status = MeetingService(
        settings,
        state_store=store,
        transcription_factory=forbidden_provider,
        intelligence_factory=forbidden_provider,
        capture_lock_held=lambda _: True,
        pid_is_alive=lambda pid: pid == 4321,
        now=lambda: _STARTED + timedelta(seconds=65),
    ).status()

    assert status.active is True
    assert status.worker_alive is True
    assert status.elapsed_seconds == 65


def test_provider_failure_is_sanitized_and_stop_resumes_without_retranscribing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(
        settings,
        phase=RecorderPhase.READY_FOR_PROCESSING,
    )
    _write_manifest(storage, artifacts, state)
    transcriptions: list[tuple[Any, ...]] = []

    class Transcriber:
        def transcribe(self, parts: tuple[Any, ...]) -> TranscriptionResult:
            transcriptions.append(parts)
            return _transcription()

    class FailingIntelligence:
        def extract(self, transcript: Transcript, *, work_identity: WorkIdentity | None) -> None:
            del transcript, work_identity
            raise MeetingError("provider rejected SECRET-TRANSCRIPT-CONTENT")

    first = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=Transcriber,
        intelligence_factory=FailingIntelligence,
        now=lambda: _STARTED + timedelta(seconds=6),
    )
    with pytest.raises(MeetingLifecycleError) as caught:
        first.stop()

    assert (
        str(caught.value) == "Meeting processing failed; finalized audio was preserved for retry."
    )
    assert "SECRET-TRANSCRIPT-CONTENT" not in str(caught.value)
    failed = store.read()
    assert failed is not None
    assert failed.phase is RecorderPhase.PROCESSING_FAILED
    assert failed.error_code == "provider_processing_failed"
    assert artifacts.transcript.is_file()
    assert len(transcriptions) == 1

    class Intelligence:
        def extract(
            self,
            transcript: Transcript,
            *,
            work_identity: WorkIdentity | None,
        ) -> IntelligenceResult:
            assert transcript == _transcription().transcript
            assert work_identity is None
            return _intelligence()

    resumed = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=lambda: pytest.fail("a saved transcript must be reused"),
        intelligence_factory=Intelligence,
        now=lambda: _STARTED + timedelta(seconds=7),
    ).stop()

    assert resumed.session_id == state.session_id
    assert resumed.report_path == artifacts.report
    assert resumed.report_path.is_file()
    assert len(transcriptions) == 1
    completed = store.read()
    assert completed is not None
    assert completed.phase is RecorderPhase.COMPLETED


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            TranscriptionAuthenticationError(
                "OpenAI authentication failed during meeting transcription."
            ),
            "transcription_auth_failed",
            False,
        ),
        (
            TranscriptionPermissionError(
                "The OpenAI project cannot access the configured transcription model."
            ),
            "transcription_permission_denied",
            False,
        ),
        (
            TranscriptionRequestError("OpenAI rejected the meeting transcription request."),
            "transcription_request_rejected",
            False,
        ),
        (
            TranscriptionNetworkError(
                "The OpenAI API could not be reached for meeting transcription."
            ),
            "transcription_provider_unavailable",
            True,
        ),
    ],
)
def test_provider_errors_keep_their_sanitized_message_and_say_whether_a_retry_can_work(
    tmp_path: Path,
    error: MeetingError,
    code: str,
    retryable: bool,
) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(
        settings,
        phase=RecorderPhase.READY_FOR_PROCESSING,
    )
    _write_manifest(storage, artifacts, state)

    class Transcriber:
        def transcribe(self, parts: tuple[Any, ...]) -> TranscriptionResult:
            raise error

    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=Transcriber,
        intelligence_factory=lambda: pytest.fail("transcription failed first"),
        now=lambda: _STARTED + timedelta(seconds=6),
    )

    with pytest.raises(MeetingLifecycleError) as caught:
        service.stop()

    message = str(caught.value)
    assert message.startswith(str(error).rstrip("."))
    if retryable:
        assert "run `pikvm-agent meeting stop` again to retry" in message
    else:
        assert "will not succeed on retry" in message
    failed = store.read()
    assert failed is not None
    assert failed.phase is RecorderPhase.PROCESSING_FAILED
    assert failed.error_code == code


def test_intelligence_auth_failure_is_reported_as_not_retryable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(
        settings,
        phase=RecorderPhase.READY_FOR_PROCESSING,
    )
    _write_manifest(storage, artifacts, state)

    class Transcriber:
        def transcribe(self, parts: tuple[Any, ...]) -> TranscriptionResult:
            return _transcription()

    class Intelligence:
        def extract(self, transcript: Transcript, *, work_identity: WorkIdentity | None) -> None:
            raise IntelligenceAuthenticationError(
                "OpenAI authentication failed during meeting analysis."
            )

    with pytest.raises(MeetingLifecycleError, match="will not succeed on retry"):
        MeetingService(
            settings,
            state_store=store,
            storage=storage,
            transcription_factory=Transcriber,
            intelligence_factory=Intelligence,
            now=lambda: _STARTED + timedelta(seconds=6),
        ).stop()

    failed = store.read()
    assert failed is not None
    assert failed.error_code == "intelligence_auth_failed"
    assert artifacts.transcript.is_file()


def test_stop_recovers_checkpoint_only_after_the_claimed_worker_is_dead(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(
        settings,
        phase=RecorderPhase.RECORDING,
    )
    identity = WorkIdentity("Shafiq", ("Shafique",))
    checkpoint = _write_checkpoint(storage, artifacts, state, identity=identity)
    transcribed: list[tuple[Any, ...]] = []

    class Transcriber:
        def transcribe(self, parts: tuple[Any, ...]) -> TranscriptionResult:
            transcribed.append(parts)
            return _transcription()

    class Intelligence:
        def extract(
            self,
            transcript: Transcript,
            *,
            work_identity: WorkIdentity | None,
        ) -> IntelligenceResult:
            assert transcript == _transcription().transcript
            assert work_identity == identity
            return _intelligence()

    result = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=Transcriber,
        intelligence_factory=Intelligence,
        pid_is_alive=lambda pid: False if pid == 4321 else pytest.fail("unexpected PID"),
        now=lambda: _STARTED + timedelta(seconds=10),
    ).stop()

    assert result.interrupted is True
    assert result.report_path == artifacts.report
    assert len(transcribed) == 1
    assert transcribed[0][0].path == artifacts.directory / "audio-0001.ogg"
    manifest = MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text())
    assert manifest.interrupted is True
    assert manifest.session_id == state.session_id
    assert manifest.kvm == state.kvm
    assert manifest.work_identity == identity
    assert manifest.parts == checkpoint.parts


def test_dead_capture_recovery_re_reads_state_under_the_lock_and_keeps_a_finished_recording(
    tmp_path: Path,
) -> None:
    """A recorder that finalized between the caller's read and the lock is left alone."""

    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(settings, phase=RecorderPhase.RECORDING)
    _write_manifest(storage, artifacts, state)
    stale = store.read()
    assert stale is not None
    finalized = store.compare_and_set(
        state.session_id,
        stale.revision,
        replace(
            stale,
            phase=RecorderPhase.READY_FOR_PROCESSING,
            worker_pid=None,
            ended_at=_STARTED + timedelta(seconds=5),
            updated_at=_STARTED + timedelta(seconds=5),
        ),
    )
    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        capture_lock_held=lambda _: False,
        pid_is_alive=lambda _: False,
        now=lambda: _STARTED + timedelta(seconds=10),
    )

    recovered = service._recover_dead_capture(stale)

    assert recovered == finalized
    assert store.read() == finalized
    manifest = MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text())
    assert manifest.interrupted is False


def test_stop_never_promotes_a_checkpoint_while_the_claimed_worker_is_alive(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(
        settings,
        phase=RecorderPhase.RECORDING,
    )
    _write_checkpoint(storage, artifacts, state)
    clock = [0.0]

    def sleep(_: float) -> None:
        clock[0] += 2

    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=lambda: pytest.fail("a live capture must not be transcribed"),
        intelligence_factory=lambda: pytest.fail("a live capture must not be analyzed"),
        capture_lock_held=lambda _: True,
        pid_is_alive=lambda pid: pid == 4321,
        monotonic=lambda: clock[0],
        sleeper=sleep,
        now=lambda: _STARTED + timedelta(seconds=10),
    )

    with pytest.raises(MeetingLifecycleError, match="still finalizing"):
        service.stop()

    assert not artifacts.manifest.exists()


def test_stop_winning_before_worker_claim_terminates_without_capture_or_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, storage, _, state = _reserve_session(settings, phase=RecorderPhase.STARTING)
    clock = [0.0]

    def sleep(_: float) -> None:
        clock[0] += 2

    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=lambda: pytest.fail("no audio means no transcription"),
        intelligence_factory=lambda: pytest.fail("no audio means no analysis"),
        monotonic=lambda: clock[0],
        sleeper=sleep,
        now=lambda: _STARTED + timedelta(seconds=1),
    )

    with pytest.raises(MeetingLifecycleError) as caught:
        service.stop()

    assert "still finalizing" not in str(caught.value)
    stopped = store.read()
    assert stopped is not None
    assert stopped.phase is RecorderPhase.FAILED
    assert stopped.error_code == "no_audio_before_stop"
    monkeypatch.setattr(
        worker_module.PiKVMSettings,
        "from_env",
        lambda _: pytest.fail("a worker that lost the stop race must not access PiKVM"),
    )
    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 1
    assert store.read() == stopped


def test_capture_worker_uses_only_the_state_kvm_and_writes_a_private_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, artifacts, state = _reserve_session(settings)
    selected_profiles: list[str] = []
    captured_directories: list[Path] = []
    pikvm = type(
        "PiKVM",
        (),
        {
            "totp_required": False,
            "totp_provider": TotpProviderKind.KEYCHAIN,
            "totp_interactive_fallback": False,
            "work_identity": WorkIdentity("Shafiq", ("Shafique",)),
        },
    )()

    class PiKVMFactory:
        @staticmethod
        def from_env(profile: str) -> Any:
            selected_profiles.append(profile)
            return pikvm

    class Capture:
        def __init__(self, selected: Any, **kwargs: object) -> None:
            assert selected is pikvm
            assert kwargs["totp_provider"] is None

        async def record(
            self,
            directory: Path,
            *,
            stop_requested: asyncio.Event,
            on_ready: Any,
            on_part: Any,
            on_heartbeat: Any,
            on_reconnect: Any,
        ) -> CaptureResult:
            assert stop_requested.is_set() is False
            captured_directories.append(directory)
            on_ready()
            on_heartbeat()
            audio = directory / "audio-0001.ogg"
            audio.write_bytes(b"opus")
            audio.chmod(0o600)
            part = RecordedAudioPart(audio, offset_seconds=0, duration_seconds=5)
            on_part(part)
            return CaptureResult(
                parts=(part,),
                duration_seconds=5,
            )

    monkeypatch.setattr(worker_module, "PiKVMSettings", PiKVMFactory)
    monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)
    monkeypatch.setattr(
        worker_module,
        "build_totp_provider",
        lambda _: pytest.fail("a no-2FA profile must not build TOTP"),
    )

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 0

    assert selected_profiles == ["heidrick"]
    assert captured_directories == [artifacts.directory]
    completed_capture = store.read()
    assert completed_capture is not None
    assert completed_capture.kvm == "heidrick"
    assert completed_capture.phase is RecorderPhase.READY_FOR_PROCESSING
    manifest = MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text())
    assert manifest.kvm == "heidrick"
    assert manifest.work_identity == WorkIdentity("Shafiq", ("Shafique",))
    assert [part.filename for part in manifest.parts] == ["audio-0001.ogg"]
    assert artifacts.manifest.stat().st_mode & 0o777 == 0o600
    checkpoint_path = artifacts.directory / "capture.checkpoint.json"
    checkpoint = MeetingCaptureCheckpoint.model_validate_json(checkpoint_path.read_text())
    assert checkpoint.session_id == state.session_id
    assert checkpoint.kvm == "heidrick"
    assert checkpoint.work_identity == WorkIdentity("Shafiq", ("Shafique",))
    assert checkpoint.parts == manifest.parts
    assert checkpoint_path.stat().st_mode & 0o777 == 0o600


def test_capture_worker_reports_local_capture_failure_without_mislabeling_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, artifacts, state = _reserve_session(settings)
    pikvm = type(
        "PiKVM",
        (),
        {
            "totp_required": False,
            "totp_provider": TotpProviderKind.KEYCHAIN,
            "totp_interactive_fallback": False,
            "work_identity": None,
        },
    )()

    class PiKVMFactory:
        @staticmethod
        def from_env(profile: str) -> Any:
            assert profile == state.kvm
            return pikvm

    class Capture:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def record(self, *_: object, **__: object) -> CaptureResult:
            raise MeetingCaptureLocalError("SECRET-LOCAL-PATH")

    monkeypatch.setattr(worker_module, "PiKVMSettings", PiKVMFactory)
    monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 1

    failed = store.read()
    assert failed is not None
    assert failed.phase is RecorderPhase.FAILED
    assert failed.worker_pid is None
    assert failed.error_code == "capture_local_failed"
    assert failed.error_code != "webrtc_connection_failed"
    assert "SECRET-LOCAL-PATH" not in settings.state_path.read_text()
    assert not artifacts.manifest.exists()


@pytest.mark.parametrize(
    ("error", "code", "phrase"),
    [
        (
            MeetingCaptureAuthError("rejected"),
            "pikvm_auth_failed",
            "rejected this profile's credentials",
        ),
        (
            MeetingCaptureUnreachableError("unreachable"),
            "pikvm_unreachable",
            "could not be reached",
        ),
        (
            MeetingCaptureConnectionError("negotiation failed"),
            "webrtc_connection_failed",
            "WebRTC audio connection failed",
        ),
    ],
)
def test_worker_distinguishes_auth_unreachable_and_negotiation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: MeetingCaptureConnectionError,
    code: str,
    phrase: str,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, state = _reserve_session(settings)
    pikvm = type(
        "PiKVM",
        (),
        {
            "totp_required": False,
            "totp_provider": TotpProviderKind.KEYCHAIN,
            "totp_interactive_fallback": False,
            "work_identity": None,
        },
    )()

    class Capture:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def record(self, *_: object, **__: object) -> CaptureResult:
            raise error

    monkeypatch.setattr(
        worker_module,
        "PiKVMSettings",
        type("PiKVMFactory", (), {"from_env": staticmethod(lambda _profile: pikvm)}),
    )
    monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 1

    failed = store.read()
    assert failed is not None
    assert failed.phase is RecorderPhase.FAILED
    assert failed.error_code == code
    assert phrase in service_module._capture_failure_message(code)


def test_stop_watcher_failure_aborts_the_capture_and_requests_stop() -> None:
    async def exercise() -> None:
        aborted: list[MeetingCaptureConnectionError] = []
        stop_requested = asyncio.Event()

        class Capture:
            def abort(self, error: MeetingCaptureConnectionError) -> None:
                aborted.append(error)

        async def crashing_watcher() -> None:
            raise RuntimeError("SECRET-WATCHER-DETAIL")

        task = asyncio.create_task(crashing_watcher())
        with pytest.raises(RuntimeError):
            await task

        worker_module._stop_watcher_finished(task, Capture(), stop_requested)  # type: ignore[arg-type]

        assert len(aborted) == 1
        assert isinstance(aborted[0], MeetingCaptureLocalError)
        assert "SECRET-WATCHER-DETAIL" not in str(aborted[0])
        assert stop_requested.is_set()

        cancelled = asyncio.create_task(asyncio.sleep(10))
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        worker_module._stop_watcher_finished(cancelled, Capture(), asyncio.Event())  # type: ignore[arg-type]
        assert len(aborted) == 1

    asyncio.run(exercise())


def test_worker_manifest_records_reconnects_and_degraded_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, artifacts, state = _reserve_session(settings)
    pikvm = type(
        "PiKVM",
        (),
        {
            "totp_required": False,
            "totp_provider": TotpProviderKind.KEYCHAIN,
            "totp_interactive_fallback": False,
            "work_identity": None,
        },
    )()

    class Capture:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def record(
            self,
            directory: Path,
            *,
            stop_requested: asyncio.Event,
            on_ready: Any,
            on_part: Any,
            on_heartbeat: Any,
            on_reconnect: Any,
        ) -> CaptureResult:
            on_ready()
            first = directory / "audio-0001.ogg"
            first.write_bytes(b"opus")
            on_part(RecordedAudioPart(first, offset_seconds=0, duration_seconds=5))
            on_reconnect(1)
            second = directory / "audio-0002.ogg"
            second.write_bytes(b"opus")
            degraded = RecordedAudioPart(
                second, offset_seconds=8, duration_seconds=5, degraded=True
            )
            on_part(degraded)
            return CaptureResult(
                parts=(
                    RecordedAudioPart(first, offset_seconds=0, duration_seconds=5),
                    degraded,
                ),
                duration_seconds=13,
                reconnects=1,
            )

    monkeypatch.setattr(
        worker_module,
        "PiKVMSettings",
        type("PiKVMFactory", (), {"from_env": staticmethod(lambda _profile: pikvm)}),
    )
    monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 0

    manifest = MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text())
    assert manifest.reconnects == 1
    assert manifest.interrupted is False
    assert [part.degraded for part in manifest.parts] == [False, True]
    finished = store.read()
    assert finished is not None
    assert finished.phase is RecorderPhase.READY_FOR_PROCESSING


def test_checkpointed_worker_failure_is_still_processable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, storage, artifacts, state = _reserve_session(settings)
    pikvm = type(
        "PiKVM",
        (),
        {
            "totp_required": False,
            "totp_provider": TotpProviderKind.KEYCHAIN,
            "totp_interactive_fallback": False,
            "work_identity": None,
        },
    )()

    class PiKVMFactory:
        @staticmethod
        def from_env(profile: str) -> Any:
            assert profile == state.kvm
            return pikvm

    class Capture:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def record(
            self,
            directory: Path,
            *,
            stop_requested: asyncio.Event,
            on_ready: Any,
            on_part: Any,
            on_heartbeat: Any,
            on_reconnect: Any,
        ) -> CaptureResult:
            del stop_requested, on_heartbeat
            on_ready()
            audio = directory / "audio-0001.ogg"
            audio.write_bytes(b"opus")
            on_part(RecordedAudioPart(audio, offset_seconds=0, duration_seconds=1))
            raise MeetingError("SECRET-MEETING-CONTENT")

    monkeypatch.setattr(worker_module, "PiKVMSettings", PiKVMFactory)
    monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 1
    interrupted = store.read()
    assert interrupted is not None
    assert interrupted.phase is RecorderPhase.INTERRUPTED
    assert not artifacts.manifest.exists()
    assert (artifacts.directory / "capture.checkpoint.json").is_file()

    class Transcriber:
        def transcribe(self, _: tuple[Any, ...]) -> TranscriptionResult:
            return _transcription()

    class Intelligence:
        def extract(
            self,
            transcript: Transcript,
            *,
            work_identity: WorkIdentity | None,
        ) -> IntelligenceResult:
            assert transcript == _transcription().transcript
            assert work_identity is None
            return _intelligence()

    result = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        transcription_factory=Transcriber,
        intelligence_factory=Intelligence,
        now=lambda: _STARTED + timedelta(hours=2),
    ).stop()

    assert result.interrupted is True
    assert result.report_path == artifacts.report
    assert (
        MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text()).interrupted
        is True
    )


def test_duplicate_worker_cannot_steal_an_existing_capture_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, state = _reserve_session(settings)
    claimed = store.claim_worker(
        state.session_id,
        9876,
        _STARTED + timedelta(seconds=1),
    )
    monkeypatch.setattr(
        worker_module.PiKVMSettings,
        "from_env",
        lambda _: pytest.fail("a duplicate worker must not access PiKVM configuration"),
    )

    assert asyncio.run(worker_module._capture_worker(state.session_id, settings)) == 1
    assert store.read() == claimed


def test_worker_signal_is_recorded_as_local_interruption_not_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        settings = _settings(tmp_path)
        store, _, artifacts, state = _reserve_session(settings)
        handlers: dict[signal.Signals, Any] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda selected, callback: handlers.__setitem__(selected, callback),
        )
        monkeypatch.setattr(
            loop, "remove_signal_handler", lambda selected: handlers.pop(selected, None)
        )
        pikvm = type(
            "PiKVM",
            (),
            {
                "totp_required": False,
                "totp_provider": TotpProviderKind.KEYCHAIN,
                "totp_interactive_fallback": False,
                "work_identity": None,
            },
        )()

        class PiKVMFactory:
            @staticmethod
            def from_env(profile: str) -> Any:
                assert profile == "heidrick"
                return pikvm

        class Capture:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            async def record(
                self,
                directory: Path,
                *,
                stop_requested: asyncio.Event,
                on_ready: Any,
                on_part: Any,
                on_heartbeat: Any,
                on_reconnect: Any,
            ) -> CaptureResult:
                del on_heartbeat
                on_ready()
                handlers[signal.SIGTERM]()
                assert stop_requested.is_set()
                audio = directory / "audio-0001.ogg"
                audio.write_bytes(b"opus")
                part = RecordedAudioPart(audio, offset_seconds=0, duration_seconds=1)
                on_part(part)
                return CaptureResult(
                    parts=(part,),
                    duration_seconds=1,
                )

        monkeypatch.setattr(worker_module, "PiKVMSettings", PiKVMFactory)
        monkeypatch.setattr(worker_module, "PiKVMWebRTCAudioCapture", Capture)

        assert await worker_module._capture_worker(state.session_id, settings) == 0
        final = store.read()
        assert final is not None
        assert final.phase is RecorderPhase.INTERRUPTED
        manifest = MeetingCaptureManifest.model_validate_json(artifacts.manifest.read_text())
        assert manifest.interruption_code == "local_interruption"

    asyncio.run(exercise())


def test_worker_top_level_crash_is_silent_and_marks_only_its_session_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    store, _, _, state = _reserve_session(settings)

    async def crash(_: str, __: MeetingSettings) -> int:
        raise RuntimeError("SECRET-MEETING-CONTENT")

    monkeypatch.setattr(worker_module, "_capture_worker", crash)
    monkeypatch.setattr(worker_module.os, "umask", lambda _: 0o077)

    assert worker_module.run_capture_worker(state.session_id, settings) == 1

    assert capsys.readouterr() == ("", "")
    failed = store.read()
    assert failed is not None
    assert failed.session_id == state.session_id
    assert failed.phase is RecorderPhase.FAILED
    assert failed.worker_pid is None
    assert failed.error_code == "unexpected_local_error"


def test_worker_top_level_failure_never_marks_a_different_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store, _, _, state = _reserve_session(settings)

    async def crash(_: str, __: MeetingSettings) -> int:
        raise RuntimeError("SECRET-MEETING-CONTENT")

    monkeypatch.setattr(worker_module, "_capture_worker", crash)
    monkeypatch.setattr(worker_module.os, "umask", lambda _: 0o077)

    assert worker_module.run_capture_worker("meeting-other-session", settings) == 1
    assert store.read() == state


@pytest.mark.parametrize(
    "parts",
    [
        (
            CapturedAudioPart(filename="audio-0001.ogg", offset_seconds=0, duration_seconds=2),
            CapturedAudioPart(filename="audio-0001.ogg", offset_seconds=2, duration_seconds=2),
        ),
        (
            CapturedAudioPart(filename="audio-0001.ogg", offset_seconds=0, duration_seconds=4),
            CapturedAudioPart(filename="audio-0002.ogg", offset_seconds=3, duration_seconds=2),
        ),
        (CapturedAudioPart(filename="audio-0001.ogg", offset_seconds=4, duration_seconds=2),),
    ],
)
def test_manifest_rejects_duplicate_overlapping_or_out_of_duration_parts(
    parts: tuple[CapturedAudioPart, ...],
) -> None:
    with pytest.raises(ValidationError):
        MeetingCaptureManifest(
            session_id="meeting-manifest-test",
            kvm="heidrick",
            started_at=_STARTED,
            ended_at=_STARTED + timedelta(seconds=5),
            duration_seconds=5,
            parts=parts,
        )


def test_capture_checkpoint_is_session_bound_cumulative_and_contains_no_meeting_content() -> None:
    checkpoint = MeetingCaptureCheckpoint(
        session_id="meeting-checkpoint-test",
        kvm="heidrick",
        started_at=_STARTED,
        work_identity_name="Shafiq",
        work_identity_aliases=("Shafiq", "Shafique"),
        parts=(
            CapturedAudioPart(
                filename="audio-0001.ogg",
                offset_seconds=0,
                duration_seconds=2,
            ),
            CapturedAudioPart(
                filename="audio-0002.ogg",
                offset_seconds=2,
                duration_seconds=3,
            ),
        ),
    )

    serialized = checkpoint.model_dump_json()
    assert checkpoint.duration_seconds == 5
    assert checkpoint.work_identity == WorkIdentity("Shafiq", ("Shafique",))
    assert "SECRET-MEETING-CONTENT" not in serialized
    assert "transcript" not in serialized
    assert "provider" not in serialized


def test_capture_checkpoint_rejects_duplicate_parts() -> None:
    with pytest.raises(ValidationError):
        MeetingCaptureCheckpoint(
            session_id="meeting-checkpoint-test",
            kvm="heidrick",
            started_at=_STARTED,
            parts=(
                CapturedAudioPart(
                    filename="audio-0001.ogg",
                    offset_seconds=0,
                    duration_seconds=2,
                ),
                CapturedAudioPart(
                    filename="audio-0001.ogg",
                    offset_seconds=2,
                    duration_seconds=2,
                ),
            ),
        )
