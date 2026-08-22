from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from work_agent.dashboard import operations
from work_agent.dashboard.app import TOKEN_HEADER, create_app
from work_agent.dashboard.models import KvmProfile
from work_agent.meeting.cli import execute_meeting_command
from work_agent.meeting.library import MeetingLibrary
from work_agent.meeting.models import MeetingIntelligence
from work_agent.meeting.state import RecorderPhase

TOKEN = "test-dashboard-token"
_BASE = "http://127.0.0.1"


def _write_session(
    root: Path,
    *,
    kvm: str = "nbc_kvm",
    session_id: str = "meeting-20260819T0900Z-abc",
    with_report: bool = True,
    day: int = 19,
) -> Path:
    started = datetime(2026, 8, day, 9, 0, tzinfo=UTC)
    directory = root / kvm / f"2026-08-{day}" / session_id
    directory.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "session_id": session_id,
        "kvm": kvm,
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(minutes=12)).isoformat(),
        "duration_seconds": 720.0,
        "interrupted": False,
        "interruption_code": None,
        "reconnects": 0,
        "work_identity_name": "Shafiq",
        "work_identity_aliases": ["Shafiq"],
        "parts": [
            {"filename": "audio-0001.ogg", "offset_seconds": 0.0, "duration_seconds": 300.0},
            {"filename": "audio-0002.ogg", "offset_seconds": 300.0, "duration_seconds": 420.0},
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    if not with_report:
        return directory
    transcript = {
        "schema_version": 1,
        "session_id": session_id,
        "manifest_sha256": "a" * 64,
        "result": {
            "transcript": {
                "duration_seconds": 720.0,
                "language": "en",
                "speakers": [
                    {"id": "speaker-1", "label": "Speaker 1"},
                    {"id": "speaker-2", "label": "Speaker 2"},
                ],
                "segments": [
                    {
                        "id": "segment-1",
                        "start_seconds": 1.0,
                        "end_seconds": 4.0,
                        "speaker_id": "speaker-1",
                        "text": "Shafiq, can you send the deck by Friday?",
                    },
                    {
                        "id": "segment-2",
                        "start_seconds": 4.5,
                        "end_seconds": 6.0,
                        "speaker_id": "speaker-2",
                        "text": "Yes, will do.",
                    },
                ],
            },
            "provider": "deepgram",
            "model": "nova-3",
            "latency_seconds": 3.0,
            "retries": 0,
            "usage": {"seconds": 720.0},
        },
    }
    (directory / "transcript.json").write_text(json.dumps(transcript))
    intelligence = MeetingIntelligence.model_validate(
        {
            "summary": "Planning sync about the deck.",
            "action_items": [
                {
                    "confidence": "high",
                    "evidence_segment_ids": ["segment-1", "segment-2"],
                    "task": "Send the deck",
                    "owner": "Shafiq",
                    "owner_category": "our_identity",
                    "requested_by": "Speaker 1",
                    "due_text": "Friday",
                    "reason": None,
                }
            ],
            "decisions": [
                {
                    "confidence": "high",
                    "evidence_segment_ids": ["segment-1"],
                    "text": "Ship Friday.",
                }
            ],
            "blockers_and_risks": [],
            "open_questions": [],
            "references": [],
            "follow_ups": [],
        }
    )
    artifact = {
        "schema_version": 1,
        "session_id": session_id,
        "manifest_sha256": "a" * 64,
        "transcript_sha256": "b" * 64,
        "result": {
            "intelligence": intelligence.model_dump(mode="json"),
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "service_tier": "default",
            "latency_seconds": 2.0,
            "retries": 0,
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_tokens": 0,
                "total_tokens": 15,
            },
        },
    }
    (directory / "intelligence.json").write_text(json.dumps(artifact))
    (directory / "report.md").write_text("# Meeting\n\n## OUR ACTION ITEMS\n- Send the deck\n")
    return directory


def test_library_lists_sessions_and_loads_detail(tmp_path: Path) -> None:
    _write_session(tmp_path)
    _write_session(
        tmp_path, kvm="heidrick", session_id="meeting-20260818T0900Z-old", with_report=False, day=18
    )
    library = MeetingLibrary(tmp_path)

    sessions = library.list_sessions()
    assert [item.session_id for item in sessions] == [
        "meeting-20260819T0900Z-abc",
        "meeting-20260818T0900Z-old",
    ]
    assert sessions[0].stage == "complete" and sessions[0].our_action_items == 1
    assert sessions[1].stage == "transcription_pending"

    detail = library.detail("meeting-20260819T0900Z-abc")
    assert detail is not None
    assert detail.meeting_summary == "Planning sync about the deck."
    assert detail.action_items[0].task == "Send the deck"
    assert detail.action_items[0].owner_category == "our_identity"
    assert detail.action_items[0].timestamp_seconds == 1.0
    assert detail.decisions == ["Ship Friday."]
    assert [line.speaker for line in detail.transcript] == ["Speaker 1", "Speaker 2"]
    assert detail.report_markdown is not None and "OUR ACTION ITEMS" in detail.report_markdown
    assert library.detail("meeting-nope") is None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(token=TOKEN)
    app.state.log_path = tmp_path / "slack-availability.jsonl"
    app.state.state_path = tmp_path / "state.json"
    profiles = [
        KvmProfile(
            name="nbc_kvm",
            endpoint="https://nbc.example",
            totp_required=True,
            verify_ssl=False,
            interactive_totp=False,
            configured=True,
            problem=None,
        )
    ]
    monkeypatch.setattr(operations, "profile_snapshots", lambda: profiles)
    monkeypatch.setattr(operations, "ensure_runnable", lambda targets: None)
    return TestClient(app, base_url=_BASE, headers={TOKEN_HEADER: TOKEN})


class _FakeMeetingService:
    def __init__(self) -> None:
        self.phase: RecorderPhase | None = None
        self.started: list[str] = []
        self.stopped = 0

    def status(self) -> SimpleNamespace:
        if self.phase is None:
            return SimpleNamespace(
                state=None,
                worker_alive=False,
                elapsed_seconds=0.0,
                worker_stale=False,
                active=False,
            )
        return SimpleNamespace(
            state=SimpleNamespace(
                phase=self.phase,
                kvm="nbc_kvm",
                session_id="meeting-live",
                error_code=None,
            ),
            worker_alive=True,
            elapsed_seconds=42.0,
            worker_stale=False,
            active=not self.phase.terminal,
        )

    def start(self, kvm: str) -> SimpleNamespace:
        self.started.append(kvm)
        self.phase = RecorderPhase.RECORDING
        return SimpleNamespace(
            session_id="meeting-live",
            kvm=kvm,
            started_at=datetime.now(UTC),
            directory=Path("/tmp/x"),
        )

    def stop(self) -> SimpleNamespace:
        self.stopped += 1
        self.phase = RecorderPhase.COMPLETED
        return SimpleNamespace(
            session_id="meeting-live",
            kvm="nbc_kvm",
            duration_seconds=61.0,
            report_path=Path("/tmp/x/report.md"),
            our_action_items=1,
            possible_our_action_items=0,
            decisions=2,
            blockers=0,
            interrupted=False,
        )


def _stream_done(client: TestClient, job_id: str) -> dict[str, object]:
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        event = "message"
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "done":
                return json.loads(line.split(":", 1)[1].strip())
    raise AssertionError("job stream ended without a final result")


def test_meetings_endpoints_drive_the_recorder_and_show_reports(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeMeetingService()
    root = tmp_path / "meetings"
    _write_session(root)
    monkeypatch.setattr(operations, "meeting_service", lambda: fake)
    monkeypatch.setattr(operations, "meeting_library", lambda: MeetingLibrary(root))
    monkeypatch.setattr(
        operations,
        "meeting_settings",
        lambda: SimpleNamespace(
            deepgram_api_key="dg",
            openai_api_key="oa",
            transcription_provider=SimpleNamespace(value="deepgram"),
            data_directory=root,
        ),
    )

    snapshot = client.get("/api/meetings")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["recorder"]["active"] is False
    assert body["transcription_provider"] == "deepgram"
    assert [s["session_id"] for s in body["sessions"]] == ["meeting-20260819T0900Z-abc"]
    assert body["sessions"][0]["our_action_items"] == 1

    started = client.post("/api/meetings/start", json={"kvm": "nbc_kvm"})
    assert started.status_code == 202
    done = _stream_done(client, started.json()["id"])
    assert done["status"] == "succeeded" and fake.started == ["nbc_kvm"]
    assert client.get("/api/meetings").json()["recorder"]["phase"] == "recording"

    stopped = client.post("/api/meetings/stop")
    assert stopped.status_code == 202
    done = _stream_done(client, stopped.json()["id"])
    assert done["status"] == "succeeded" and fake.stopped == 1
    assert "ours 1" in json.dumps(done)

    detail = client.get("/api/meetings/sessions/meeting-20260819T0900Z-abc")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["meeting_summary"] == "Planning sync about the deck."
    assert payload["action_items"][0]["task"] == "Send the deck"
    assert payload["transcript"][1]["speaker"] == "Speaker 2"
    assert client.get("/api/meetings/sessions/missing").status_code == 404
    assert client.post("/api/meetings/start", json={"kvm": "ghost"}).status_code == 404


def test_validate_command_records_for_the_requested_time_then_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeMeetingService()
    root = tmp_path / "meetings"
    _write_session(root, session_id="meeting-live")
    monkeypatch.setenv("MEETING_DATA_DIR", str(root))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state.json"))
    output: list[str] = []
    sleeps: list[float] = []
    args = argparse.Namespace(
        meeting_command="validate",
        kvm="nbc_kvm",
        seconds=40.0,
        _output=output.append,
        _sleeper=sleeps.append,
    )

    text, code = execute_meeting_command(args, service=fake)  # type: ignore[arg-type]

    assert code == 0
    assert sum(sleeps) == 40.0 and fake.started == ["nbc_kvm"] and fake.stopped == 1
    assert any("Recording for 40s" in line for line in output)
    assert "Our action items: 1" in text
    assert "OUR ACTION ITEMS:" in text and "Send the deck" in text
