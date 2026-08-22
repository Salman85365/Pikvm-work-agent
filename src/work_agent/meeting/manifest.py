from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from work_agent.pikvm.config import WorkIdentity


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapturedAudioPart(_StrictModel):
    filename: str = Field(pattern=r"^audio-[0-9]{4}\.ogg$")
    offset_seconds: float = Field(ge=0.0)
    duration_seconds: float = Field(gt=0.0)
    # Decoded audio fell measurably short of the wall-clock window (dropped frames).
    degraded: bool = False

    @field_validator("filename")
    @classmethod
    def _plain_filename(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("audio part filename must not contain a path")
        return value


class MeetingCaptureCheckpoint(_StrictModel):
    """Cumulative durable parts recorded before a final manifest exists."""

    schema_version: int = Field(default=1, ge=1, le=1)
    session_id: str = Field(pattern=r"^meeting-[A-Za-z0-9_-]+$")
    kvm: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    started_at: datetime
    work_identity_name: str | None = Field(default=None, max_length=120)
    work_identity_aliases: tuple[str, ...] = Field(default=(), max_length=20)
    parts: tuple[CapturedAudioPart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> MeetingCaptureCheckpoint:
        if self.started_at.tzinfo is None:
            raise ValueError("capture checkpoint timestamps must include a timezone")
        _validate_identity(self.work_identity_name, self.work_identity_aliases)
        _validate_parts(self.parts)
        return self

    @property
    def duration_seconds(self) -> float:
        return max(part.offset_seconds + part.duration_seconds for part in self.parts)

    @property
    def work_identity(self) -> WorkIdentity | None:
        return _work_identity(self.work_identity_name, self.work_identity_aliases)


class MeetingCaptureManifest(_StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    session_id: str = Field(pattern=r"^meeting-[A-Za-z0-9_-]+$")
    kvm: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0.0)
    interrupted: bool = False
    interruption_code: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$",
    )
    # Times the recorder re-established the PiKVM audio session mid-recording.
    reconnects: int = Field(default=0, ge=0)
    work_identity_name: str | None = Field(default=None, max_length=120)
    work_identity_aliases: tuple[str, ...] = Field(default=(), max_length=20)
    parts: tuple[CapturedAudioPart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> MeetingCaptureManifest:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("capture timestamps must include a timezone")
        if self.ended_at < self.started_at:
            raise ValueError("capture end cannot precede its start")
        if self.interrupted != (self.interruption_code is not None):
            raise ValueError("capture interruption state and code must agree")
        _validate_identity(self.work_identity_name, self.work_identity_aliases)
        _validate_parts(self.parts, duration_seconds=self.duration_seconds)
        return self

    @property
    def work_identity(self) -> WorkIdentity | None:
        return _work_identity(self.work_identity_name, self.work_identity_aliases)


def _validate_identity(name: str | None, aliases: tuple[str, ...]) -> None:
    if name is None and aliases:
        raise ValueError("work identity aliases require a name")


def _validate_parts(
    parts: tuple[CapturedAudioPart, ...],
    *,
    duration_seconds: float | None = None,
) -> None:
    filenames = [part.filename for part in parts]
    if len(filenames) != len(set(filenames)):
        raise ValueError("audio part filenames must be unique")
    previous_end = 0.0
    for index, part in enumerate(parts):
        if index and part.offset_seconds < previous_end - 0.1:
            raise ValueError("audio parts must be ordered and must not overlap")
        previous_end = part.offset_seconds + part.duration_seconds
    if duration_seconds is not None and abs(previous_end - duration_seconds) > 0.1:
        raise ValueError("capture duration must match the finalized audio timeline")


def _work_identity(name: str | None, aliases: tuple[str, ...]) -> WorkIdentity | None:
    if name is None:
        return None
    return WorkIdentity(name=name, aliases=aliases)
