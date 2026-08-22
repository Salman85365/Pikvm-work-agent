from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv

from work_agent.meeting.errors import MeetingConfigurationError
from work_agent.vision.models import ReasoningEffort, ServiceTier

DEFAULT_MEETING_DATA_DIRECTORY = (
    Path.home() / "Library" / "Application Support" / "pikvm-work-agent" / "meetings"
)
DEFAULT_MEETING_STATE_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "pikvm-work-agent"
    / "meeting-recorder-state.json"
)
_EnumT = TypeVar("_EnumT", bound=StrEnum)


class MeetingProvider(StrEnum):
    OPENAI = "openai"
    DEEPGRAM = "deepgram"


@dataclass(frozen=True, slots=True)
class MeetingSettings:
    """Mac-local paths for protected meeting artifacts and recorder coordination."""

    openai_api_key: str = field(default="", repr=False)
    deepgram_api_key: str = field(default="", repr=False)
    deepgram_model: str = "nova-3"
    deepgram_language: str = "en"
    data_directory: Path = DEFAULT_MEETING_DATA_DIRECTORY
    state_path: Path = DEFAULT_MEETING_STATE_PATH
    transcription_provider: MeetingProvider = MeetingProvider.OPENAI
    intelligence_provider: MeetingProvider = MeetingProvider.OPENAI
    transcription_model: str = "gpt-4o-transcribe-diarize"
    transcription_timeout_seconds: float = 600.0
    transcription_max_retries: int = 2
    meeting_model: str = "gpt-5.6-terra"
    intelligence_timeout_seconds: float = 120.0
    intelligence_max_retries: int = 2
    meeting_service_tier: ServiceTier = ServiceTier.DEFAULT
    meeting_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    store: bool = False
    capture_signaling_timeout_seconds: float = 15.0
    capture_audio_start_timeout_seconds: float = 15.0
    capture_segment_seconds: float = 300.0
    # The worker must connect, negotiate, and see its first audio frame inside this window, so
    # it has to exceed signaling + audio-start with margin for process start-up.
    start_handshake_timeout_seconds: float = 45.0
    stop_wait_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        api_key = self.openai_api_key.strip()
        object.__setattr__(self, "openai_api_key", api_key)
        object.__setattr__(self, "deepgram_api_key", self.deepgram_api_key.strip())
        object.__setattr__(self, "deepgram_model", _model("DEEPGRAM_MODEL", self.deepgram_model))
        object.__setattr__(self, "deepgram_language", self.deepgram_language.strip())
        if self.intelligence_provider is MeetingProvider.DEEPGRAM:
            raise MeetingConfigurationError(
                "MEETING_INTELLIGENCE_PROVIDER must be openai; Deepgram only transcribes."
            )
        data_directory = self.data_directory.expanduser().resolve()
        state_path = self.state_path.expanduser().resolve()
        if state_path == data_directory or data_directory in state_path.parents:
            raise MeetingConfigurationError(
                "MEETING_STATE_PATH must be outside the meeting artifact directory."
            )
        object.__setattr__(self, "data_directory", data_directory)
        object.__setattr__(self, "state_path", state_path)
        object.__setattr__(
            self,
            "transcription_model",
            _model("OPENAI_TRANSCRIPTION_MODEL", self.transcription_model),
        )
        object.__setattr__(
            self,
            "meeting_model",
            _model("OPENAI_MEETING_MODEL", self.meeting_model),
        )
        if self.store:
            raise MeetingConfigurationError(
                "OPENAI_STORE must remain false for meeting processing."
            )
        for name, value in (
            ("OPENAI_TRANSCRIPTION_REQUEST_TIMEOUT_SECONDS", self.transcription_timeout_seconds),
            ("OPENAI_MEETING_REQUEST_TIMEOUT_SECONDS", self.intelligence_timeout_seconds),
            ("MEETING_SIGNALING_TIMEOUT_SECONDS", self.capture_signaling_timeout_seconds),
            ("MEETING_AUDIO_START_TIMEOUT_SECONDS", self.capture_audio_start_timeout_seconds),
            ("MEETING_SEGMENT_SECONDS", self.capture_segment_seconds),
            ("MEETING_START_HANDSHAKE_TIMEOUT_SECONDS", self.start_handshake_timeout_seconds),
            ("MEETING_STOP_WAIT_TIMEOUT_SECONDS", self.stop_wait_timeout_seconds),
            ("MEETING_POLL_INTERVAL_SECONDS", self.poll_interval_seconds),
        ):
            if value <= 0:
                raise MeetingConfigurationError(f"{name} must be greater than zero.")
        for name, value in (
            ("OPENAI_TRANSCRIPTION_MAX_RETRIES", self.transcription_max_retries),
            ("OPENAI_MEETING_MAX_RETRIES", self.intelligence_max_retries),
        ):
            if not 0 <= value <= 2:
                raise MeetingConfigurationError(f"{name} must be between 0 and 2.")
        negotiation = (
            self.capture_signaling_timeout_seconds + self.capture_audio_start_timeout_seconds
        )
        if self.start_handshake_timeout_seconds < negotiation:
            raise MeetingConfigurationError(
                "MEETING_START_HANDSHAKE_TIMEOUT_SECONDS must be at least "
                "MEETING_SIGNALING_TIMEOUT_SECONDS + MEETING_AUDIO_START_TIMEOUT_SECONDS "
                f"({negotiation:g}); leave margin above that so a slow but healthy handshake "
                "is not cut off."
            )

    @classmethod
    def from_env(cls) -> MeetingSettings:
        load_dotenv()
        data_directory = Path(os.getenv("MEETING_DATA_DIR", str(DEFAULT_MEETING_DATA_DIRECTORY)))
        state_path = Path(os.getenv("MEETING_STATE_PATH", str(DEFAULT_MEETING_STATE_PATH)))
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            deepgram_language=os.getenv("DEEPGRAM_LANGUAGE", "en"),
            data_directory=data_directory,
            state_path=state_path,
            transcription_provider=_enum(
                "MEETING_TRANSCRIPTION_PROVIDER",
                os.getenv("MEETING_TRANSCRIPTION_PROVIDER", "openai"),
                MeetingProvider,
            ),
            intelligence_provider=_enum(
                "MEETING_INTELLIGENCE_PROVIDER",
                os.getenv("MEETING_INTELLIGENCE_PROVIDER", "openai"),
                MeetingProvider,
            ),
            transcription_model=os.getenv(
                "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe-diarize"
            ),
            transcription_timeout_seconds=_float(
                "OPENAI_TRANSCRIPTION_REQUEST_TIMEOUT_SECONDS",
                os.getenv("OPENAI_TRANSCRIPTION_REQUEST_TIMEOUT_SECONDS", "600"),
            ),
            transcription_max_retries=_int(
                "OPENAI_TRANSCRIPTION_MAX_RETRIES",
                os.getenv("OPENAI_TRANSCRIPTION_MAX_RETRIES", "2"),
            ),
            meeting_model=os.getenv(
                "OPENAI_MEETING_MODEL",
                os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            ),
            intelligence_timeout_seconds=_float(
                "OPENAI_MEETING_REQUEST_TIMEOUT_SECONDS",
                os.getenv("OPENAI_MEETING_REQUEST_TIMEOUT_SECONDS", "120"),
            ),
            intelligence_max_retries=_int(
                "OPENAI_MEETING_MAX_RETRIES",
                os.getenv("OPENAI_MEETING_MAX_RETRIES", "2"),
            ),
            meeting_service_tier=_enum(
                "OPENAI_MEETING_SERVICE_TIER",
                os.getenv(
                    "OPENAI_MEETING_SERVICE_TIER",
                    os.getenv("OPENAI_SERVICE_TIER", "default"),
                ),
                ServiceTier,
            ),
            meeting_reasoning_effort=_enum(
                "OPENAI_MEETING_REASONING_EFFORT",
                os.getenv(
                    "OPENAI_MEETING_REASONING_EFFORT",
                    os.getenv("OPENAI_REASONING_EFFORT", "low"),
                ),
                ReasoningEffort,
            ),
            store=_bool("OPENAI_STORE", os.getenv("OPENAI_STORE", "false")),
            capture_signaling_timeout_seconds=_float(
                "MEETING_SIGNALING_TIMEOUT_SECONDS",
                os.getenv("MEETING_SIGNALING_TIMEOUT_SECONDS", "15"),
            ),
            capture_audio_start_timeout_seconds=_float(
                "MEETING_AUDIO_START_TIMEOUT_SECONDS",
                os.getenv("MEETING_AUDIO_START_TIMEOUT_SECONDS", "15"),
            ),
            capture_segment_seconds=_float(
                "MEETING_SEGMENT_SECONDS",
                os.getenv("MEETING_SEGMENT_SECONDS", "300"),
            ),
            start_handshake_timeout_seconds=_float(
                "MEETING_START_HANDSHAKE_TIMEOUT_SECONDS",
                os.getenv("MEETING_START_HANDSHAKE_TIMEOUT_SECONDS", "45"),
            ),
            stop_wait_timeout_seconds=_float(
                "MEETING_STOP_WAIT_TIMEOUT_SECONDS",
                os.getenv("MEETING_STOP_WAIT_TIMEOUT_SECONDS", "30"),
            ),
            poll_interval_seconds=_float(
                "MEETING_POLL_INTERVAL_SECONDS",
                os.getenv("MEETING_POLL_INTERVAL_SECONDS", "0.25"),
            ),
        )


def _bool(name: str, raw_value: str) -> bool:
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise MeetingConfigurationError(f"{name} must be one of true/false, yes/no, on/off, or 1/0.")


def _float(name: str, raw_value: str) -> float:
    try:
        return float(raw_value)
    except ValueError as exc:
        raise MeetingConfigurationError(f"{name} must be a number.") from exc


def _int(name: str, raw_value: str) -> int:
    try:
        return int(raw_value)
    except ValueError as exc:
        raise MeetingConfigurationError(f"{name} must be an integer.") from exc


def _enum(name: str, raw_value: str, enum_type: type[_EnumT]) -> _EnumT:
    try:
        return enum_type(raw_value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise MeetingConfigurationError(f"{name} must be one of: {allowed}.") from exc


def _model(name: str, raw_value: str) -> str:
    value = raw_value.strip()
    if not value or any(character.isspace() for character in value):
        raise MeetingConfigurationError(
            f"{name} must be a non-empty model identifier without whitespace."
        )
    return value
