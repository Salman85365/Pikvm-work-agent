from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import work_agent.meeting.worker as worker_module
from work_agent.meeting.capture import MeetingCaptureLocalError
from work_agent.meeting.config import MeetingSettings
from work_agent.meeting.errors import MeetingStateConflictError
from work_agent.meeting.service import (
    MeetingLifecycleError,
    MeetingService,
    _exclusive_processing_lock,
)
from work_agent.meeting.state import MeetingRecorderState, MeetingStateStore, RecorderPhase
from work_agent.meeting.storage import MeetingStorage

_STARTED = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _session(
    tmp_path: Path,
    *,
    phase: RecorderPhase,
) -> tuple[MeetingSettings, MeetingStateStore, Path]:
    settings = MeetingSettings(
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state" / "meeting.json",
    )
    storage = MeetingStorage(settings.data_directory)
    artifacts = storage.create_session(
        kvm="heidrick",
        session_id="meeting-20260818T100000Z-concurrency",
        started_at=_STARTED,
    )
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-concurrency",
        kvm="heidrick",
        phase=phase,
        started_at=_STARTED,
        updated_at=_STARTED,
        session_directory=artifacts.directory,
        ended_at=(
            _STARTED + timedelta(seconds=5) if phase is RecorderPhase.PROCESSING_FAILED else None
        ),
    )
    store = MeetingStateStore(settings.state_path)
    store.reserve(state)
    return settings, store, artifacts.directory


def test_abandon_refuses_a_live_claimed_worker(tmp_path: Path) -> None:
    settings, store, directory = _session(tmp_path, phase=RecorderPhase.STARTING)
    claimed = store.claim_worker(
        "meeting-20260818T100000Z-concurrency",
        4321,
        _STARTED + timedelta(seconds=1),
    )
    storage = MeetingStorage(settings.data_directory)
    service = MeetingService(settings, state_store=store, storage=storage)

    with storage.capture_lock(directory) as acquired:
        assert acquired is True
        assert (directory / "capture.lock").stat().st_mode & 0o777 == 0o600
        with pytest.raises(MeetingLifecycleError, match="still running"):
            service.abandon(claimed.session_id)

    assert store.read() == claimed


def test_reused_pid_cannot_wedge_a_stale_capture(tmp_path: Path) -> None:
    settings, store, _ = _session(tmp_path, phase=RecorderPhase.STARTING)
    claimed = store.claim_worker(
        "meeting-20260818T100000Z-concurrency",
        4321,
        _STARTED + timedelta(seconds=1),
    )
    recording = store.compare_and_set(
        claimed.session_id,
        claimed.revision,
        replace(
            claimed,
            phase=RecorderPhase.RECORDING,
            recording_started_at=_STARTED + timedelta(seconds=1),
        ),
    )
    service = MeetingService(
        settings,
        state_store=store,
        pid_is_alive=lambda pid: pid == recording.worker_pid,
        now=lambda: _STARTED + timedelta(seconds=10),
    )

    status = service.status()

    assert status.worker_alive is False
    assert status.worker_pid_alive is True
    assert status.worker_stale is True
    with pytest.raises(MeetingLifecycleError, match="before any incoming audio"):
        service.stop()
    failed = store.read()
    assert failed is not None
    assert failed.phase is RecorderPhase.FAILED
    assert failed.error_code == "no_audio_before_stop"


def test_status_does_not_create_a_lock_or_missing_session_directory(tmp_path: Path) -> None:
    settings = MeetingSettings(
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state" / "meeting.json",
        capture_signaling_timeout_seconds=0.2,
        capture_audio_start_timeout_seconds=0.2,
        start_handshake_timeout_seconds=1,
    )
    storage = MeetingStorage(settings.data_directory)
    directory = storage.session_directory(
        kvm="heidrick",
        session_id="meeting-20260818T100000Z-missing",
        started_at=_STARTED,
    )
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-missing",
        kvm="heidrick",
        phase=RecorderPhase.STARTING,
        started_at=_STARTED,
        updated_at=_STARTED,
        session_directory=directory,
    )
    store = MeetingStateStore(settings.state_path)
    store.reserve(state)

    status = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        now=lambda: _STARTED + timedelta(seconds=2),
    ).status()

    assert status.worker_alive is False
    assert status.worker_stale is True
    assert not directory.exists()


def test_worker_retries_when_a_preclaim_lock_probe_temporarily_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        started_at = datetime.now(UTC) - timedelta(seconds=1)
        settings = MeetingSettings(
            data_directory=tmp_path / "meetings",
            state_path=tmp_path / "state" / "meeting.json",
        )
        storage = MeetingStorage(settings.data_directory)
        artifacts = storage.create_session(
            kvm="heidrick",
            session_id="meeting-preclaim-probe",
            started_at=started_at,
        )
        store = MeetingStateStore(settings.state_path)
        store.reserve(
            MeetingRecorderState(
                session_id="meeting-preclaim-probe",
                kvm="heidrick",
                phase=RecorderPhase.STARTING,
                started_at=started_at,
                updated_at=started_at,
                session_directory=artifacts.directory,
            )
        )
        selected_profiles: list[str] = []

        class PiKVMFactory:
            @staticmethod
            def from_env(profile: str) -> object:
                selected_profiles.append(profile)
                raise MeetingCaptureLocalError("transient test failure")

        monkeypatch.setattr(worker_module, "PiKVMSettings", PiKVMFactory)
        with storage.capture_lock(artifacts.directory) as acquired:
            assert acquired is True
            worker = asyncio.create_task(
                worker_module._capture_worker(
                    "meeting-preclaim-probe",
                    settings,
                )
            )
            await asyncio.sleep(settings.poll_interval_seconds * 2)
            assert not worker.done()

        assert await worker == 1
        assert selected_profiles == ["heidrick"]
        failed = store.read()
        assert failed is not None
        assert failed.phase is RecorderPhase.FAILED
        assert failed.error_code == "capture_local_failed"

    asyncio.run(exercise())


def test_abandon_releases_reservation_when_parent_died_before_directory_creation(
    tmp_path: Path,
) -> None:
    settings = MeetingSettings(
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state" / "meeting.json",
    )
    storage = MeetingStorage(settings.data_directory)
    directory = storage.session_directory(
        kvm="heidrick",
        session_id="meeting-20260818T100000Z-crash-window",
        started_at=_STARTED,
    )
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-crash-window",
        kvm="heidrick",
        phase=RecorderPhase.STARTING,
        started_at=_STARTED,
        updated_at=_STARTED,
        session_directory=directory,
    )
    store = MeetingStateStore(settings.state_path)
    store.reserve(state)
    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        pid_is_alive=lambda _: False,
        now=lambda: _STARTED + timedelta(seconds=10),
    )

    result = service.abandon(state.session_id)

    abandoned = store.read()
    assert abandoned is not None
    assert abandoned.phase is RecorderPhase.FAILED
    assert abandoned.error_code == "abandoned_by_user"
    assert result.directory == directory
    assert not directory.exists()


def test_abandon_is_exact_and_cannot_race_active_processing(tmp_path: Path) -> None:
    settings, store, directory = _session(tmp_path, phase=RecorderPhase.PROCESSING_FAILED)
    storage = MeetingStorage(settings.data_directory)
    audio = directory / "audio-0001.ogg"
    audio.write_bytes(b"preserved")
    audio.chmod(0o600)
    service = MeetingService(
        settings,
        state_store=store,
        storage=storage,
        pid_is_alive=lambda _: False,
        now=lambda: _STARTED + timedelta(seconds=10),
    )

    with pytest.raises(MeetingStateConflictError, match="state changed"):
        service.abandon("meeting-another-session")

    process_lock = storage.artifact_path(directory, "processing.lock")
    with (
        _exclusive_processing_lock(process_lock),
        pytest.raises(MeetingStateConflictError, match="already being processed"),
    ):
        service.abandon("meeting-20260818T100000Z-concurrency")

    result = service.abandon("meeting-20260818T100000Z-concurrency")
    abandoned = store.read()
    assert abandoned is not None
    assert abandoned.phase is RecorderPhase.FAILED
    assert abandoned.error_code == "abandoned_by_user"
    assert result.directory == directory
    assert audio.read_bytes() == b"preserved"
