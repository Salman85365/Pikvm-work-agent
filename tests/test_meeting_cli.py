from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from work_agent.meeting import cli
from work_agent.meeting.service import MeetingStartResult, MeetingStatusResult, MeetingStopResult
from work_agent.meeting.state import MeetingRecorderState, RecorderPhase

_STARTED = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def test_start_output_is_exact_and_contains_no_recorded_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".local-data" / "meetings" / "heidrick" / "session"
    secret = "SECRET-MEETING-CONTENT"
    directory.mkdir(parents=True)
    (directory / "transcript.json").write_text(secret)

    output = cli.format_meeting_start(
        MeetingStartResult(
            session_id="meeting-20260818T100000Z-test",
            kvm="heidrick",
            started_at=_STARTED,
            directory=directory,
        )
    )

    assert output == "\n".join(
        [
            "Meeting recording started.",
            "",
            "KVM: heidrick",
            "Audio source: PiKVM WebRTC incoming HDMI audio only",
            "Session: meeting-20260818T100000Z-test",
            "Artifacts: .local-data/meetings/heidrick/session",
        ]
    )
    assert secret not in output


def test_stop_output_is_exact_and_only_reports_counts_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    report = tmp_path / ".local-data" / "meetings" / "heidrick" / "session" / "report.md"
    report.parent.mkdir(parents=True)
    secret = "SECRET-MEETING-CONTENT"
    report.write_text(secret)

    output = cli.format_meeting_stop(
        MeetingStopResult(
            session_id="meeting-20260818T100000Z-test",
            kvm="heidrick",
            duration_seconds=2520,
            report_path=report,
            our_action_items=3,
            possible_our_action_items=1,
            decisions=2,
            blockers=1,
            interrupted=True,
        )
    )

    assert output == "\n".join(
        [
            "Meeting processed successfully.",
            "",
            "KVM: heidrick",
            "Duration: 42m 0s",
            "Capture: interrupted; finalized audio was processed",
            "",
            "Our action items: 3",
            "Possible our action items: 1",
            "Decisions: 2",
            "Blockers: 1",
            "",
            "Report:",
            ".local-data/meetings/heidrick/session/report.md",
        ]
    )
    assert secret not in output


def test_status_output_is_exact_and_never_reads_report_content(tmp_path: Path) -> None:
    report = tmp_path / "session" / "report.md"
    report.parent.mkdir()
    secret = "SECRET-MEETING-CONTENT"
    report.write_text(secret)
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-test",
        kvm="heidrick",
        phase=RecorderPhase.PROCESSING_FAILED,
        started_at=_STARTED,
        updated_at=_STARTED + timedelta(seconds=65),
        ended_at=_STARTED + timedelta(seconds=65),
        session_directory=report.parent.resolve(),
        report_path=report.resolve(),
        error_code="provider_processing_failed",
    )

    output = cli.format_meeting_status(
        MeetingStatusResult(state=state, worker_alive=False, elapsed_seconds=65)
    )

    assert output == "\n".join(
        [
            "Meeting recorder status",
            "",
            "KVM: heidrick",
            "Status: processing failed",
            "Duration: 1m 5s",
            "Session: meeting-20260818T100000Z-test",
            f"Artifacts: {report.parent.resolve()}",
            "Next: run `pikvm-agent meeting stop` to finish or retry processing.",
            "Error code: provider_processing_failed",
            "Report:",
            str(report.resolve()),
        ]
    )
    assert secret not in output


def test_abandon_reset_corrupt_moves_the_state_file_aside_and_reports_it(
    tmp_path: Path,
) -> None:
    from work_agent.meeting.config import MeetingSettings
    from work_agent.meeting.errors import MeetingStateCorruptError
    from work_agent.meeting.service import MeetingService

    state_path = tmp_path / "state" / "meeting.json"
    state_path.parent.mkdir(mode=0o700)
    state_path.write_text('{"schema_version": 1, "phase": "recording"}')
    state_path.chmod(0o600)
    service = MeetingService(
        MeetingSettings(data_directory=tmp_path / "meetings", state_path=state_path)
    )
    with pytest.raises(MeetingStateCorruptError):
        service.status()

    output, code = cli.execute_meeting_command(
        argparse.Namespace(meeting_command="abandon", session_id=None, reset_corrupt=True),
        service=service,
    )

    assert code == 0
    assert output.startswith(
        "Corrupt meeting recorder state moved aside; no artifacts were deleted."
    )
    assert not state_path.exists()
    moved = list(state_path.parent.glob("meeting.json.corrupt-*"))
    assert len(moved) == 1
    assert moved[0].read_text() == '{"schema_version": 1, "phase": "recording"}'
    assert str(moved[0]) in output
    assert service.status().state is None


def test_abandon_reset_corrupt_refuses_a_readable_state_file(tmp_path: Path) -> None:
    from work_agent.meeting.config import MeetingSettings
    from work_agent.meeting.errors import MeetingStateConflictError
    from work_agent.meeting.service import MeetingService
    from work_agent.meeting.state import MeetingStateStore

    settings = MeetingSettings(
        data_directory=tmp_path / "meetings",
        state_path=tmp_path / "state" / "meeting.json",
    )
    store = MeetingStateStore(settings.state_path)
    store.reserve(
        MeetingRecorderState(
            session_id="meeting-20260818T100000Z-test",
            kvm="heidrick",
            phase=RecorderPhase.STARTING,
            started_at=_STARTED,
            updated_at=_STARTED,
            session_directory=(tmp_path / "meetings" / "session").resolve(),
        )
    )

    with pytest.raises(MeetingStateConflictError, match="abandon it by session ID"):
        cli.execute_meeting_command(
            argparse.Namespace(meeting_command="abandon", session_id=None, reset_corrupt=True),
            service=MeetingService(settings, state_store=store),
        )

    assert settings.state_path.exists()


def test_abandon_requires_a_session_id_or_reset_corrupt() -> None:
    from work_agent.meeting.service import MeetingLifecycleError

    with pytest.raises(MeetingLifecycleError, match="requires --session-id"):
        cli.execute_meeting_command(
            argparse.Namespace(meeting_command="abandon", session_id=None, reset_corrupt=False),
            service=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "phase",
    [
        RecorderPhase.STARTING,
        RecorderPhase.STOP_REQUESTED,
        RecorderPhase.FINALIZING,
    ],
)
def test_stale_capture_status_has_exact_recovery_and_session_abandon_hints(
    tmp_path: Path,
    phase: RecorderPhase,
) -> None:
    state = MeetingRecorderState(
        session_id="meeting-20260818T100000Z-test",
        kvm="heidrick",
        phase=phase,
        started_at=_STARTED,
        updated_at=_STARTED + timedelta(seconds=65),
        session_directory=(tmp_path / "session").resolve(),
        worker_pid=None if phase is RecorderPhase.STARTING else 4321,
    )

    output = cli.format_meeting_status(
        MeetingStatusResult(
            state=state,
            worker_alive=False,
            elapsed_seconds=65,
            worker_stale=True,
        )
    )

    assert output == "\n".join(
        [
            "Meeting recorder status",
            "",
            "KVM: heidrick",
            "Status: interrupted (recorder process is no longer running)",
            "Duration: 1m 5s",
            "Session: meeting-20260818T100000Z-test",
            f"Artifacts: {state.session_directory}",
            "Next: run `pikvm-agent meeting stop` to recover finalized audio.",
            "If recovery is not possible, preserve the artifacts and release only this session:",
            "`pikvm-agent meeting abandon --session-id meeting-20260818T100000Z-test`",
        ]
    )


def test_status_command_only_calls_status_on_the_injected_service() -> None:
    result = MeetingStatusResult(state=None, worker_alive=False, elapsed_seconds=0)

    class Service:
        def status(self) -> MeetingStatusResult:
            return result

        def start(self, _: str) -> None:
            pytest.fail("status must not start PiKVM capture")

        def stop(self) -> None:
            pytest.fail("status must not build or call providers")

    output, code = cli.execute_meeting_command(
        argparse.Namespace(meeting_command="status"),
        service=Service(),  # type: ignore[arg-type]
    )

    assert (output, code) == ("No meeting recording is active.", 0)


def test_private_capture_command_has_no_stdout_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "work_agent.meeting.worker.run_capture_worker",
        lambda session_id: calls.append(session_id) or 1,
    )

    output, code = cli.execute_meeting_command(
        argparse.Namespace(
            meeting_command="_capture",
            session_id="meeting-20260818T100000Z-test",
        )
    )

    assert calls == ["meeting-20260818T100000Z-test"]
    assert output == ""
    assert code == 1


def test_importing_the_meeting_cli_does_not_load_the_webrtc_stack() -> None:
    """Slack and calendar commands must never depend on the aiortc/av/websockets wheels."""

    script = (
        "import sys; import work_agent.meeting.cli; import work_agent.cli; "
        "loaded = [name for name in ('aiortc', 'av', 'websockets') if name in sys.modules]; "
        "print(','.join(loaded))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.strip() == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (59.6, "1m 0s"), (3601, "1h 0m 1s")],
)
def test_duration_format_is_stable(seconds: float, expected: str) -> None:
    assert cli._duration(seconds) == expected


def test_parser_requires_one_explicit_kvm_for_start() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cli.add_meeting_parser(commands)

    args = parser.parse_args(["meeting", "start", "--kvm", "heidrick"])

    assert vars(args) == {
        "command": "meeting",
        "meeting_command": "start",
        "kvm": "heidrick",
    }
