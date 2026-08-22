from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from work_agent.meeting.config import MeetingConfigurationError, MeetingProvider, MeetingSettings
from work_agent.meeting.errors import (
    MeetingStateConflictError,
    MeetingStateCorruptError,
    MeetingStorageError,
)
from work_agent.meeting.state import MeetingRecorderState, MeetingStateStore, RecorderPhase
from work_agent.meeting.storage import MeetingStorage


def _state(
    directory: Path,
    *,
    session_id: str = "session-a",
    kvm: str = "heidrick",
    phase: RecorderPhase = RecorderPhase.STARTING,
) -> MeetingRecorderState:
    now = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    return MeetingRecorderState(
        session_id=session_id,
        kvm=kvm,
        phase=phase,
        started_at=now,
        updated_at=now,
        session_directory=directory,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_meeting_settings_resolve_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.meeting.config.load_dotenv", lambda: False)
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "data" / ".." / "meetings"))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state" / "recorder.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")

    settings = MeetingSettings.from_env()

    assert settings.data_directory == (tmp_path / "meetings").resolve()
    assert settings.state_path == (tmp_path / "state" / "recorder.json").resolve()


def test_state_path_cannot_live_inside_artifact_directory(tmp_path: Path) -> None:
    with pytest.raises(MeetingConfigurationError, match="outside"):
        MeetingSettings(
            openai_api_key="private-key",
            data_directory=tmp_path / "meetings",
            state_path=tmp_path / "meetings" / "state.json",
        )


def test_default_meeting_storage_is_stable_across_working_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.meeting.config.load_dotenv", lambda: False)
    monkeypatch.delenv("MEETING_DATA_DIR", raising=False)
    monkeypatch.delenv("MEETING_STATE_PATH", raising=False)
    first = MeetingSettings.from_env()

    alternate = tmp_path / "alternate"
    alternate.mkdir()
    monkeypatch.chdir(alternate)
    second = MeetingSettings.from_env()

    assert first.data_directory == second.data_directory
    assert first.data_directory.is_absolute()


def test_storage_creates_private_per_kvm_session_and_atomic_artifacts(tmp_path: Path) -> None:
    storage = MeetingStorage(tmp_path / "meetings")
    started = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)

    artifacts = storage.create_session(
        kvm="Heidrick",
        session_id="meeting-abc123",
        started_at=started,
    )
    storage.write_json(artifacts.manifest, {"kvm": "heidrick", "segments": 1})
    storage.write_text(artifacts.report, "# Meeting\n")

    assert artifacts.directory.parts[-3:] == (
        "heidrick",
        started.astimezone(UTC).date().isoformat(),
        "meeting-abc123",
    )
    assert json.loads(artifacts.manifest.read_text(encoding="utf-8"))["kvm"] == "heidrick"
    assert _mode(storage.root) == 0o700
    assert _mode(artifacts.directory) == 0o700
    assert _mode(artifacts.manifest) == 0o600
    assert _mode(artifacts.report) == 0o600
    assert not list(artifacts.directory.glob("*.tmp"))


def test_storage_finalizes_only_same_session_and_preserves_nonempty_partial(
    tmp_path: Path,
) -> None:
    storage = MeetingStorage(tmp_path / "meetings")
    artifacts = storage.create_session(
        kvm="heidrick",
        session_id="session-a",
        started_at=datetime(2026, 8, 18, 12, 30, tzinfo=UTC),
    )
    partial = storage.artifact_path(artifacts.directory, "audio-0001.part.ogg")
    final = storage.artifact_path(artifacts.directory, "audio-0001.ogg")
    storage.prepare_output(partial)
    partial.write_bytes(b"remote audio")

    assert storage.remove_empty_partial(partial) is False
    assert storage.finalize(partial, final) == final
    assert final.read_bytes() == b"remote audio"
    assert _mode(final) == 0o600

    other = tmp_path / "meetings" / "other.ogg"
    with pytest.raises(MeetingStorageError, match="session"):
        storage.finalize(final, other)


def test_storage_removes_only_empty_partial(tmp_path: Path) -> None:
    storage = MeetingStorage(tmp_path / "meetings")
    artifacts = storage.create_session(
        kvm="heidrick",
        session_id="session-a",
        started_at=datetime(2026, 8, 18, 12, 30, tzinfo=UTC),
    )
    partial = storage.artifact_path(artifacts.directory, "audio.part.ogg")
    storage.prepare_output(partial)

    assert storage.remove_empty_partial(partial) is True
    assert not partial.exists()


@pytest.mark.parametrize(
    ("kvm", "session_id"),
    [
        ("../heidrick", "safe"),
        ("heidrick", "../session"),
        ("heidrick/work", "safe"),
    ],
)
def test_storage_rejects_path_components(
    tmp_path: Path,
    kvm: str,
    session_id: str,
) -> None:
    storage = MeetingStorage(tmp_path / "meetings")
    with pytest.raises(MeetingStorageError, match="identifier"):
        storage.create_session(
            kvm=kvm,
            session_id=session_id,
            started_at=datetime.now(UTC),
        )


def test_storage_rejects_a_naive_start_time(tmp_path: Path) -> None:
    with pytest.raises(MeetingStorageError, match="timezone"):
        MeetingStorage(tmp_path / "meetings").create_session(
            kvm="heidrick",
            session_id="session-a",
            started_at=datetime(2026, 8, 18, 12, 30),
        )


def test_storage_rejects_escape_and_symlinked_profile_directory(tmp_path: Path) -> None:
    root = tmp_path / "meetings"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "heidrick").symlink_to(outside, target_is_directory=True)
    storage = MeetingStorage(root)

    with pytest.raises(MeetingStorageError, match="escaped"):
        storage.create_session(
            kvm="heidrick",
            session_id="session-a",
            started_at=datetime.now(UTC),
        )
    with pytest.raises(MeetingStorageError, match="escaped"):
        storage.write_text(outside / "report.md", "must not be written")


def test_storage_rejects_cross_session_symlinked_input(tmp_path: Path) -> None:
    storage = MeetingStorage(tmp_path / "meetings")
    started = datetime.now(UTC)
    first = storage.create_session(kvm="heidrick", session_id="session-a", started_at=started)
    second = storage.create_session(kvm="nbc_kvm", session_id="session-b", started_at=started)
    protected = second.directory / "audio-0001.ogg"
    protected.write_bytes(b"other-kvm")
    protected.chmod(0o600)
    (first.directory / "audio-0001.ogg").symlink_to(protected)

    with pytest.raises(MeetingStorageError, match="symbolic link"):
        storage.input_artifact_path(first.directory, "audio-0001.ogg")


def test_state_store_reserves_one_global_session_and_uses_private_atomic_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "meeting.json"
    store = MeetingStateStore(path)
    first = _state(tmp_path / "first")

    store.reserve(first)

    assert store.read() == first
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    assert _mode(path.with_name("meeting.json.lock")) == 0o600
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(MeetingStateConflictError, match="already starting from heidrick") as caught:
        store.reserve(_state(tmp_path / "second", session_id="session-b", kvm="nbc_kvm"))
    assert "session-a" in str(caught.value)
    assert "meeting abandon --session-id session-a" in str(caught.value)


def test_state_store_refuses_but_does_not_chmod_a_shared_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    store = MeetingStateStore(shared / "meeting.json")

    with pytest.raises(MeetingStorageError, match="already be private"):
        store.read()

    assert _mode(shared) == 0o755


def test_terminal_state_can_be_replaced_by_a_new_session(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    store.reserve(_state(tmp_path / "first", phase=RecorderPhase.COMPLETED))

    second = _state(tmp_path / "second", session_id="session-b", kvm="nbc_kvm")
    store.reserve(second)

    assert store.read() == second


@pytest.mark.parametrize(
    "phase",
    [
        RecorderPhase.DISCONNECTED,
        RecorderPhase.INTERRUPTED,
        RecorderPhase.READY_FOR_PROCESSING,
        RecorderPhase.PROCESSING_FAILED,
    ],
)
def test_recoverable_state_cannot_be_replaced(tmp_path: Path, phase: RecorderPhase) -> None:
    """The refusal names the real phase and the command that clears it, not "recording"."""

    store = MeetingStateStore(tmp_path / "meeting.json")
    store.reserve(_state(tmp_path / "first", phase=phase))

    with pytest.raises(MeetingStateConflictError) as caught:
        store.reserve(_state(tmp_path / "second", session_id="session-b"))

    message = str(caught.value)
    assert "already recording" not in message
    assert phase.value.replace("_", " ") in message
    assert "meeting stop" in message
    assert "meeting abandon --session-id session-a" in message


def test_compare_and_set_preserves_session_and_kvm(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    original = _state(tmp_path / "session")
    store.reserve(original)
    claimed = store.claim_worker(
        original.session_id,
        4321,
        original.updated_at + timedelta(seconds=1),
    )
    updated = replace(
        claimed,
        phase=RecorderPhase.RECORDING,
        updated_at=original.updated_at + timedelta(seconds=2),
    )

    written = store.compare_and_set("session-a", claimed.revision, updated)
    assert written.revision == 2
    assert store.read() == written

    with pytest.raises(MeetingStateConflictError, match="state changed"):
        store.compare_and_set("wrong-session", written.revision, written)
    with pytest.raises(MeetingStateConflictError, match="state changed"):
        store.compare_and_set("session-a", claimed.revision, updated)
    with pytest.raises(MeetingStateConflictError, match="cannot change"):
        store.compare_and_set(
            "session-a",
            written.revision,
            replace(written, session_id="session-b", kvm="nbc_kvm"),
        )


def test_claim_stop_and_revision_updates_are_linearizable(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    original = _state(tmp_path / "session")
    store.reserve(original)
    claimed_at = original.started_at + timedelta(seconds=1)

    claimed = store.claim_worker(original.session_id, 4321, claimed_at)
    assert claimed.worker_pid == 4321
    assert claimed.revision == 1
    assert store.claim_worker(original.session_id, 4321, claimed_at) == claimed
    with pytest.raises(MeetingStateConflictError, match="already claimed"):
        store.claim_worker(original.session_id, 9876, claimed_at)

    requested = store.request_stop(
        original.session_id,
        original.started_at + timedelta(seconds=2),
    )
    assert requested.phase is RecorderPhase.STOP_REQUESTED
    assert requested.stop_requested_at == original.started_at + timedelta(seconds=2)
    assert requested.revision == 2
    assert (
        store.request_stop(
            original.session_id,
            original.started_at + timedelta(seconds=3),
        )
        == requested
    )
    with pytest.raises(MeetingStateConflictError, match="state changed"):
        store.compare_and_set(
            original.session_id,
            claimed.revision,
            replace(claimed, heartbeat_at=original.started_at + timedelta(seconds=3)),
        )


def test_stop_before_worker_claim_prevents_capture_and_can_be_abandoned(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    original = _state(tmp_path / "session")
    store.reserve(original)

    requested = store.request_stop(
        original.session_id,
        original.started_at + timedelta(seconds=1),
    )
    with pytest.raises(MeetingStateConflictError, match="already claimed"):
        store.claim_worker(
            original.session_id,
            4321,
            original.started_at + timedelta(seconds=2),
        )

    abandoned = store.abandon(
        requested.session_id,
        requested.revision,
        original.started_at + timedelta(seconds=3),
    )
    assert abandoned.phase is RecorderPhase.FAILED
    assert abandoned.error_code == "abandoned_by_user"
    assert abandoned.worker_pid is None


def test_illegal_phase_jump_is_rejected(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    original = _state(tmp_path / "session")
    store.reserve(original)

    with pytest.raises(MeetingStateConflictError, match="cannot transition"):
        store.compare_and_set(
            original.session_id,
            original.revision,
            replace(original, phase=RecorderPhase.COMPLETED),
        )


def test_crashed_analysis_can_restart_from_transcription(tmp_path: Path) -> None:
    store = MeetingStateStore(tmp_path / "meeting.json")
    analyzing = _state(tmp_path / "session", phase=RecorderPhase.ANALYZING)
    store.reserve(analyzing)

    resumed = store.compare_and_set(
        analyzing.session_id,
        analyzing.revision,
        replace(analyzing, phase=RecorderPhase.TRANSCRIBING),
    )
    assert resumed.phase is RecorderPhase.TRANSCRIBING
    assert resumed.revision == 1


def test_clear_is_compare_and_set_and_does_not_remove_another_session(tmp_path: Path) -> None:
    path = tmp_path / "meeting.json"
    store = MeetingStateStore(path)
    terminal = _state(tmp_path / "session", phase=RecorderPhase.FAILED)
    store.reserve(terminal)

    with pytest.raises(MeetingStateConflictError, match="state changed"):
        store.clear("another-session", terminal.revision)
    assert path.exists()

    store.clear("session-a", terminal.revision)
    assert store.read() is None


def test_corrupt_state_is_refused_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "meeting.json"
    path.write_text("{not-json", encoding="utf-8")
    original = path.read_bytes()
    store = MeetingStateStore(path)

    with pytest.raises(MeetingStateCorruptError, match="not overwritten"):
        store.read()
    with pytest.raises(MeetingStateCorruptError, match="not overwritten"):
        store.reserve(_state(tmp_path / "session"))
    assert path.read_bytes() == original


def test_state_contains_only_sanitized_coordination_metadata(tmp_path: Path) -> None:
    path = tmp_path / "meeting.json"
    state = _state(tmp_path / "session")
    store = MeetingStateStore(path)
    store.reserve(state)

    raw = path.read_text(encoding="utf-8")

    assert "confidential transcript words" not in raw
    assert "password" not in raw.casefold()
    assert "totp" not in raw.casefold()
    assert set(json.loads(raw)) == {
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


def test_state_rejects_naive_timestamps_and_invalid_worker_pid(tmp_path: Path) -> None:
    aware = _state(tmp_path / "session")
    with pytest.raises(ValueError, match="timezone"):
        replace(aware, updated_at=aware.updated_at.replace(tzinfo=None))
    with pytest.raises(ValueError, match="positive"):
        replace(aware, worker_pid=0)
    with pytest.raises(ValueError, match="cannot be negative"):
        replace(aware, blockers=-1)
    with pytest.raises(ValueError, match="sanitized identifiers"):
        replace(aware, error_code="raw provider error: confidential words")
    with pytest.raises(ValueError, match="inside its session"):
        replace(aware, report_path=tmp_path / "outside.md")


def test_meeting_settings_load_typed_provider_and_capture_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.meeting.config.load_dotenv", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", " private-key ")
    monkeypatch.setenv("OPENAI_MODEL", "fallback-meeting-model")
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "flex")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_REQUEST_TIMEOUT_SECONDS", "450")
    monkeypatch.setenv("OPENAI_TRANSCRIPTION_MAX_RETRIES", "1")
    monkeypatch.setenv("OPENAI_MEETING_REQUEST_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("OPENAI_MEETING_MAX_RETRIES", "0")
    monkeypatch.setenv("MEETING_SIGNALING_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("MEETING_AUDIO_START_TIMEOUT_SECONDS", "18")
    monkeypatch.setenv("MEETING_SEGMENT_SECONDS", "240")
    monkeypatch.setenv("MEETING_START_HANDSHAKE_TIMEOUT_SECONDS", "40")
    monkeypatch.setenv("MEETING_STOP_WAIT_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MEETING_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "meetings"))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state.json"))

    settings = MeetingSettings.from_env()

    assert settings.transcription_provider is MeetingProvider.OPENAI
    assert settings.intelligence_provider is MeetingProvider.OPENAI
    assert settings.transcription_model == "gpt-4o-transcribe-diarize"
    assert settings.transcription_timeout_seconds == 450
    assert settings.transcription_max_retries == 1
    assert settings.meeting_model == "fallback-meeting-model"
    assert settings.intelligence_timeout_seconds == 90
    assert settings.intelligence_max_retries == 0
    assert settings.meeting_service_tier.value == "flex"
    assert settings.meeting_reasoning_effort.value == "medium"
    assert settings.capture_signaling_timeout_seconds == 12
    assert settings.capture_audio_start_timeout_seconds == 18
    assert settings.capture_segment_seconds == 240
    assert settings.start_handshake_timeout_seconds == 40
    assert settings.stop_wait_timeout_seconds == 45
    assert settings.poll_interval_seconds == 0.5
    assert "private-key" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("OPENAI_STORE", "true", "must remain false"),
        ("MEETING_TRANSCRIPTION_PROVIDER", "local", "must be one of: openai"),
        ("OPENAI_TRANSCRIPTION_MAX_RETRIES", "3", "between 0 and 2"),
        ("OPENAI_MEETING_REQUEST_TIMEOUT_SECONDS", "0", "greater than zero"),
        ("MEETING_AUDIO_START_TIMEOUT_SECONDS", "-1", "greater than zero"),
        ("MEETING_START_HANDSHAKE_TIMEOUT_SECONDS", "29", "must be at least"),
    ],
)
def test_invalid_meeting_configuration_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setattr("work_agent.meeting.config.load_dotenv", lambda: False)
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "meetings"))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv(name, value)

    with pytest.raises(MeetingConfigurationError, match=message):
        MeetingSettings.from_env()


def test_openai_key_is_deferred_until_processing_and_repr_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("work_agent.meeting.config.load_dotenv", lambda: False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "meetings"))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state.json"))

    unconfigured = MeetingSettings.from_env()
    assert unconfigured.openai_api_key == ""

    configured = MeetingSettings(
        openai_api_key="private-key",
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state.json",
    )
    assert "private-key" not in repr(configured)
