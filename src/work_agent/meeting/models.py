from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class AudioPart:
    path: Path
    offset_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.offset_seconds < 0:
            raise ValueError("audio part offsets cannot be negative")


class TranscriptSpeaker(_StrictModel):
    id: str = Field(pattern=r"^speaker-[1-9][0-9]*$")
    label: str = Field(pattern=r"^Speaker [1-9][0-9]*$")


class TranscriptSegment(_StrictModel):
    id: str = Field(pattern=r"^segment-[1-9][0-9]*$")
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    speaker_id: str = Field(pattern=r"^speaker-[1-9][0-9]*$")
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("transcript segment text cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def _ordered_times(self) -> TranscriptSegment:
        if self.end_seconds < self.start_seconds:
            raise ValueError("transcript segment end cannot precede its start")
        return self


class Transcript(_StrictModel):
    duration_seconds: float = Field(ge=0.0)
    language: str | None = Field(default=None, min_length=1, max_length=40)
    speakers: list[TranscriptSpeaker]
    segments: list[TranscriptSegment]

    @model_validator(mode="after")
    def _consistent_references(self) -> Transcript:
        speaker_ids = [speaker.id for speaker in self.speakers]
        if len(speaker_ids) != len(set(speaker_ids)):
            raise ValueError("transcript speaker IDs must be unique")
        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("transcript segment IDs must be unique")
        known_speakers = set(speaker_ids)
        previous_start = -1.0
        for segment in self.segments:
            if segment.speaker_id not in known_speakers:
                raise ValueError("transcript segment references an unknown speaker")
            if segment.start_seconds < previous_start:
                raise ValueError("transcript segments must be ordered by start time")
            if segment.end_seconds > self.duration_seconds + 1.0:
                raise ValueError("transcript segment extends beyond the recording duration")
            previous_start = segment.start_seconds
        return self


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OwnerCategory(StrEnum):
    OUR_IDENTITY = "our_identity"
    POSSIBLY_OUR_IDENTITY = "possibly_our_identity"
    OTHER = "other"
    SHARED = "shared"
    UNKNOWN = "unknown"


class RiskKind(StrEnum):
    BLOCKER = "blocker"
    RISK = "risk"


class ReferenceKind(StrEnum):
    TICKET = "ticket"
    PULL_REQUEST = "pull_request"
    SYSTEM = "system"
    COMPONENT = "component"
    IDENTIFIER = "identifier"
    OTHER = "other"


class _EvidenceItem(_StrictModel):
    confidence: Confidence
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("evidence_segment_ids")
    @classmethod
    def _unique_evidence(cls, values: list[str]) -> list[str]:
        if any(re.fullmatch(r"segment-[1-9][0-9]*", value) is None for value in values):
            raise ValueError("evidence must use transcript segment IDs")
        if len(values) != len(set(values)):
            raise ValueError("evidence segment IDs must be unique")
        return values


class ActionItem(_EvidenceItem):
    task: str = Field(min_length=1, max_length=1_000)
    owner: str | None = Field(default=None, max_length=200)
    owner_category: OwnerCategory
    requested_by: str | None = Field(default=None, max_length=200)
    due_text: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _possible_requires_reason(self) -> ActionItem:
        if self.owner_category is OwnerCategory.POSSIBLY_OUR_IDENTITY and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("a possible work-identity action requires a reason")
        return self


class Decision(_EvidenceItem):
    text: str = Field(min_length=1, max_length=1_000)


class BlockerRisk(_EvidenceItem):
    kind: RiskKind
    text: str = Field(min_length=1, max_length=1_000)
    owner: str | None = Field(default=None, max_length=200)


class OpenQuestion(_EvidenceItem):
    question: str = Field(min_length=1, max_length=1_000)
    directed_to: str | None = Field(default=None, max_length=200)


class Reference(_EvidenceItem):
    kind: ReferenceKind
    value: str = Field(min_length=1, max_length=500)
    context: str | None = Field(default=None, max_length=500)


class FollowUp(_EvidenceItem):
    text: str = Field(min_length=1, max_length=1_000)
    owner: str | None = Field(default=None, max_length=200)
    owner_category: OwnerCategory
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _possible_requires_reason(self) -> FollowUp:
        if self.owner_category is OwnerCategory.POSSIBLY_OUR_IDENTITY and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("a possible work-identity follow-up requires a reason")
        return self


class MeetingIntelligence(_StrictModel):
    summary: str = Field(min_length=1, max_length=5_000)
    action_items: list[ActionItem]
    decisions: list[Decision]
    blockers_and_risks: list[BlockerRisk]
    open_questions: list[OpenQuestion]
    references: list[Reference]
    follow_ups: list[FollowUp]


class MeetingMetadata(_StrictModel):
    recording_id: str = Field(min_length=1, max_length=120)
    kvm: str = Field(min_length=1, max_length=120)
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0.0)
    interrupted: bool = False

    @model_validator(mode="after")
    def _ordered_times(self) -> MeetingMetadata:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("meeting times must be timezone-aware")
        if self.ended_at < self.started_at:
            raise ValueError("meeting end cannot precede its start")
        return self


class TranscriptionUsage(_StrictModel):
    seconds: float = Field(default=0.0, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def __add__(self, other: TranscriptionUsage) -> TranscriptionUsage:
        return TranscriptionUsage(
            seconds=self.seconds + other.seconds,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class ProviderUsage(_StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class TranscriptionResult(_StrictModel):
    transcript: Transcript
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    latency_seconds: float = Field(ge=0.0)
    retries: int = Field(ge=0)
    usage: TranscriptionUsage


class IntelligenceResult(_StrictModel):
    intelligence: MeetingIntelligence
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    service_tier: str | None = None
    latency_seconds: float = Field(ge=0.0)
    retries: int = Field(ge=0)
    usage: ProviderUsage


class TranscriptionArtifact(_StrictModel):
    """A persisted transcription bound to the exact capture manifest."""

    schema_version: int = Field(default=1, ge=1, le=1)
    session_id: str = Field(pattern=r"^meeting-[A-Za-z0-9_-]+$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: TranscriptionResult


class IntelligenceArtifact(_StrictModel):
    """Persisted intelligence bound to its capture manifest and transcript."""

    schema_version: int = Field(default=1, ge=1, le=1)
    session_id: str = Field(pattern=r"^meeting-[A-Za-z0-9_-]+$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: IntelligenceResult


def fingerprint_model(model: BaseModel) -> str:
    """Return a canonical content fingerprint for a validated model."""

    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
