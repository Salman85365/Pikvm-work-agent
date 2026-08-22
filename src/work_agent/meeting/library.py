"""Read-only view over recorded meeting sessions for the dashboard and the CLI.

Walks `<data dir>/<kvm>/<date>/<session>/`, reads each session's manifest and whichever of the
transcript / intelligence / report artifacts exist, and returns typed summaries. Meeting content
(summary text, action items, transcript lines) is returned to the caller that asked for one
specific session; the listing itself carries only metadata and counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from work_agent.meeting.config import MeetingSettings
from work_agent.meeting.manifest import MeetingCaptureManifest
from work_agent.meeting.models import (
    ActionItem,
    FollowUp,
    IntelligenceArtifact,
    MeetingIntelligence,
    OwnerCategory,
    Transcript,
    TranscriptionArtifact,
)

MANIFEST_NAME = "manifest.json"
TRANSCRIPT_NAME = "transcript.json"
INTELLIGENCE_NAME = "intelligence.json"
REPORT_NAME = "report.md"
_MAX_REPORT_BYTES = 2 * 1024 * 1024
_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class MeetingSessionSummary:
    session_id: str
    kvm: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    directory: Path
    has_transcript: bool
    has_intelligence: bool
    has_report: bool
    interrupted: bool
    parts: int
    our_action_items: int | None = None
    possible_our_action_items: int | None = None
    decisions: int | None = None
    problem: str | None = None

    @property
    def stage(self) -> str:
        if self.has_report:
            return "complete"
        if self.has_intelligence:
            return "report_missing"
        if self.has_transcript:
            return "analysis_pending"
        return "transcription_pending"


@dataclass(frozen=True, slots=True)
class ActionItemView:
    task: str
    owner: str | None
    owner_category: str
    requested_by: str | None
    due_text: str | None
    reason: str | None
    timestamp_seconds: float | None


@dataclass(frozen=True, slots=True)
class TranscriptLineView:
    start_seconds: float
    speaker: str
    text: str


@dataclass(frozen=True, slots=True)
class MeetingSessionDetail:
    summary: MeetingSessionSummary
    meeting_summary: str | None
    action_items: list[ActionItemView] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    transcript: list[TranscriptLineView] = field(default_factory=list)
    report_markdown: str | None = None


class MeetingLibrary:
    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or MeetingSettings.from_env().data_directory).expanduser()

    @property
    def root(self) -> Path:
        return self._root

    def list_sessions(self, *, limit: int = 50) -> list[MeetingSessionSummary]:
        sessions: list[MeetingSessionSummary] = []
        if not self._root.is_dir():
            return sessions
        for manifest_path in self._root.glob(f"*/*/*/{MANIFEST_NAME}"):
            summary = self._summarize(manifest_path.parent)
            if summary is not None:
                sessions.append(summary)
        sessions.sort(key=lambda item: item.started_at, reverse=True)
        return sessions[:limit]

    def find(self, session_id: str) -> MeetingSessionSummary | None:
        for session in self.list_sessions(limit=10_000):
            if session.session_id == session_id:
                return session
        return None

    def detail(self, session_id: str) -> MeetingSessionDetail | None:
        summary = self.find(session_id)
        if summary is None:
            return None
        directory = summary.directory
        intelligence = _load_intelligence(directory)
        transcript = _load_transcript(directory)
        report: str | None = None
        report_path = directory / REPORT_NAME
        try:
            if report_path.is_file() and report_path.stat().st_size <= _MAX_REPORT_BYTES:
                report = report_path.read_text(encoding="utf-8")
        except OSError:
            report = None

        lines: list[TranscriptLineView] = []
        if transcript is not None:
            labels = {speaker.id: speaker.label for speaker in transcript.speakers}
            lines = [
                TranscriptLineView(
                    start_seconds=segment.start_seconds,
                    speaker=labels.get(segment.speaker_id, segment.speaker_id),
                    text=segment.text,
                )
                for segment in transcript.segments
            ]
        detail = MeetingSessionDetail(
            summary=summary,
            meeting_summary=intelligence.summary if intelligence is not None else None,
            transcript=lines,
            report_markdown=report,
        )
        if intelligence is None:
            return detail
        return MeetingSessionDetail(
            summary=summary,
            meeting_summary=intelligence.summary,
            action_items=[
                ActionItemView(
                    task=item.task,
                    owner=item.owner,
                    owner_category=item.owner_category.value,
                    requested_by=item.requested_by,
                    due_text=item.due_text,
                    reason=item.reason,
                    timestamp_seconds=_first_timestamp(item, transcript),
                )
                for item in intelligence.action_items
            ],
            decisions=[item.text for item in intelligence.decisions],
            blockers=[item.text for item in intelligence.blockers_and_risks],
            open_questions=[item.question for item in intelligence.open_questions],
            follow_ups=[_follow_up_text(item) for item in intelligence.follow_ups],
            transcript=lines,
            report_markdown=report,
        )

    def _summarize(self, directory: Path) -> MeetingSessionSummary | None:
        manifest = _load_json_model(directory / MANIFEST_NAME, MeetingCaptureManifest)
        if manifest is None:
            # A session directory without a readable manifest is still worth listing so the
            # user knows it exists; it just cannot be processed.
            session_id = directory.name
            kvm = directory.parents[1].name
            try:
                started = datetime.fromtimestamp(directory.stat().st_mtime).astimezone()
            except OSError:
                return None
            return MeetingSessionSummary(
                session_id=session_id,
                kvm=kvm,
                started_at=started,
                ended_at=None,
                duration_seconds=0.0,
                directory=directory,
                has_transcript=(directory / TRANSCRIPT_NAME).is_file(),
                has_intelligence=(directory / INTELLIGENCE_NAME).is_file(),
                has_report=(directory / REPORT_NAME).is_file(),
                interrupted=False,
                parts=0,
                problem="The capture manifest is missing or unreadable.",
            )
        counts = _counts(_load_intelligence(directory))
        return MeetingSessionSummary(
            session_id=manifest.session_id,
            kvm=manifest.kvm,
            started_at=manifest.started_at,
            ended_at=manifest.ended_at,
            duration_seconds=manifest.duration_seconds,
            directory=directory,
            has_transcript=(directory / TRANSCRIPT_NAME).is_file(),
            has_intelligence=(directory / INTELLIGENCE_NAME).is_file(),
            has_report=(directory / REPORT_NAME).is_file(),
            interrupted=manifest.interrupted,
            parts=len(manifest.parts),
            our_action_items=counts[0],
            possible_our_action_items=counts[1],
            decisions=counts[2],
        )


def _counts(
    intelligence: MeetingIntelligence | None,
) -> tuple[int | None, int | None, int | None]:
    if intelligence is None:
        return None, None, None
    ours = sum(
        1 for item in intelligence.action_items if item.owner_category is OwnerCategory.OUR_IDENTITY
    )
    possible = sum(
        1
        for item in intelligence.action_items
        if item.owner_category is OwnerCategory.POSSIBLY_OUR_IDENTITY
    )
    return ours, possible, len(intelligence.decisions)


def _load_intelligence(directory: Path) -> MeetingIntelligence | None:
    artifact = _load_json_model(directory / INTELLIGENCE_NAME, IntelligenceArtifact)
    return artifact.result.intelligence if artifact is not None else None


def _load_transcript(directory: Path) -> Transcript | None:
    artifact = _load_json_model(directory / TRANSCRIPT_NAME, TranscriptionArtifact)
    return artifact.result.transcript if artifact is not None else None


def _first_timestamp(item: ActionItem, transcript: Transcript | None) -> float | None:
    if transcript is None:
        return None
    by_id = {segment.id: segment.start_seconds for segment in transcript.segments}
    for source in item.evidence_segment_ids:
        if source in by_id:
            return by_id[source]
    return None


def _follow_up_text(item: FollowUp) -> str:
    return item.text


def _load_json_model(path: Path, model: type[_ModelT]) -> _ModelT | None:
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        return None
