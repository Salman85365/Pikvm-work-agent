from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import openai
from openai import OpenAI
from openai.types.audio.transcription_diarized import TranscriptionDiarized
from pydantic import ValidationError

from work_agent.meeting.errors import MeetingError
from work_agent.meeting.models import (
    AudioPart,
    Transcript,
    TranscriptionResult,
    TranscriptionUsage,
    TranscriptSegment,
    TranscriptSpeaker,
)

_MAX_OPENAI_UPLOAD_BYTES = 25 * 1024 * 1024
# A finished part's raw provider response is kept beside it (owner-only, inside the protected
# session directory) so a retry after a later part fails does not upload it again.
_PART_TRANSCRIPT_SUFFIX = ".transcript.json"
_SUPPORTED_OPENAI_SUFFIXES = {
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}


class MeetingTranscriptionError(MeetingError):
    """A sanitized transcription failure safe to show or record."""


class TranscriptionConfigurationError(MeetingTranscriptionError):
    pass


class TranscriptionInputError(MeetingTranscriptionError):
    pass


class TranscriptionAuthenticationError(MeetingTranscriptionError):
    pass


class TranscriptionPermissionError(MeetingTranscriptionError):
    pass


class TranscriptionRequestError(MeetingTranscriptionError):
    pass


class TranscriptionNetworkError(MeetingTranscriptionError):
    pass


class TranscriptionRateLimitError(MeetingTranscriptionError):
    pass


class TranscriptionTimeoutError(MeetingTranscriptionError):
    pass


class TranscriptionServerError(MeetingTranscriptionError):
    pass


class TranscriptionStructuredOutputError(MeetingTranscriptionError):
    pass


class TranscriptionProvider(Protocol):
    def transcribe(self, parts: Sequence[AudioPart]) -> TranscriptionResult: ...


class OpenAITranscriptionProvider:
    """Completed-file transcription with anonymous, locally normalized speakers."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o-transcribe-diarize",
        request_timeout_seconds: float = 600.0,
        max_retries: int = 2,
        max_upload_bytes: int = _MAX_OPENAI_UPLOAD_BYTES,
        client: OpenAI | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        cache_part_transcripts: bool = True,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model or any(character.isspace() for character in normalized_model):
            raise TranscriptionConfigurationError(
                "The transcription model must be a non-empty identifier without whitespace."
            )
        if request_timeout_seconds <= 0:
            raise TranscriptionConfigurationError(
                "The transcription request timeout must be greater than zero."
            )
        if not 0 <= max_retries <= 2:
            raise TranscriptionConfigurationError(
                "Transcription retries must be between zero and two."
            )
        if max_upload_bytes <= 0:
            raise TranscriptionConfigurationError(
                "The transcription upload limit must be greater than zero."
            )
        if client is None and not (api_key and api_key.strip()):
            raise TranscriptionConfigurationError(
                "An API key is required for OpenAI meeting transcription."
            )

        self._model = normalized_model
        self._max_retries = max_retries
        self._max_upload_bytes = max_upload_bytes
        self._client = client or OpenAI(
            api_key=api_key.strip() if api_key is not None else None,
            timeout=request_timeout_seconds,
            max_retries=0,
        )
        self._sleeper = sleeper
        self._clock = clock
        self._cache_part_transcripts = cache_part_transcripts

    def transcribe(self, parts: Sequence[AudioPart]) -> TranscriptionResult:
        selected_parts = tuple(parts)
        if not selected_parts:
            raise TranscriptionInputError("At least one finalized audio part is required.")
        for part in selected_parts:
            self._validate_part(part.path)

        started = self._clock()
        raw_segments: list[tuple[float, float, int, str, str]] = []
        speaker_numbers: dict[tuple[int, str], int] = {}
        total_retries = 0
        usage = TranscriptionUsage()
        duration = 0.0

        indexed_parts = sorted(
            enumerate(selected_parts),
            key=lambda item: (item[1].offset_seconds, item[0]),
        )
        for part_index, part in indexed_parts:
            response, retries = self._cached_or_transcribed(part.path)
            total_retries += retries
            usage += self._usage(response)
            duration = max(duration, part.offset_seconds + response.duration)
            for segment in response.segments:
                text = segment.text.strip()
                if not text:
                    continue
                raw_speaker = segment.speaker.strip() or "anonymous"
                speaker_key = (part_index, raw_speaker)
                speaker_number = speaker_numbers.setdefault(
                    speaker_key,
                    len(speaker_numbers) + 1,
                )
                start = part.offset_seconds + max(0.0, segment.start)
                end = part.offset_seconds + max(segment.start, segment.end)
                duration = max(duration, end)
                raw_segments.append((start, end, speaker_number, text, segment.id))

        raw_segments.sort(key=lambda item: (item[0], item[1], item[4]))
        speakers = [
            TranscriptSpeaker(id=f"speaker-{number}", label=f"Speaker {number}")
            for number in range(1, len(speaker_numbers) + 1)
        ]
        segments = [
            TranscriptSegment(
                id=f"segment-{index}",
                start_seconds=start,
                end_seconds=end,
                speaker_id=f"speaker-{speaker_number}",
                text=text,
            )
            for index, (start, end, speaker_number, text, _) in enumerate(
                raw_segments,
                start=1,
            )
        ]
        return TranscriptionResult(
            transcript=Transcript(
                duration_seconds=duration,
                language=None,
                speakers=speakers,
                segments=segments,
            ),
            provider="openai",
            model=self._model,
            latency_seconds=max(0.0, self._clock() - started),
            retries=total_retries,
            usage=usage,
        )

    def _validate_part(self, path: Path) -> None:
        try:
            is_file = path.is_file()
            size = path.stat().st_size if is_file else 0
        except OSError:
            raise TranscriptionInputError("A finalized audio part could not be read.") from None
        if not is_file:
            raise TranscriptionInputError("A finalized audio part could not be read.")
        if path.suffix.casefold() not in _SUPPORTED_OPENAI_SUFFIXES:
            raise TranscriptionInputError(
                "The finalized audio format is not supported by the transcription provider."
            )
        if size <= 0:
            raise TranscriptionInputError("A finalized audio part is empty.")
        if size > self._max_upload_bytes:
            raise TranscriptionInputError(
                "A finalized audio part exceeds the transcription provider's upload limit."
            )

    def _cached_or_transcribed(self, path: Path) -> tuple[TranscriptionDiarized, int]:
        if not self._cache_part_transcripts:
            return self._transcribe_part(path)
        digest = _sha256(path)
        cached = _load_part_transcript(path, digest)
        if cached is not None:
            return cached, 0
        response, retries = self._transcribe_part(path)
        _store_part_transcript(path, digest, response)
        return response, retries

    def _transcribe_part(self, path: Path) -> tuple[TranscriptionDiarized, int]:
        retries = 0
        while True:
            try:
                with path.open("rb") as stream:
                    response = self._client.audio.transcriptions.create(
                        model=self._model,
                        file=stream,
                        response_format="diarized_json",
                        chunking_strategy="auto",
                    )
                if not isinstance(response, TranscriptionDiarized):
                    raise TranscriptionStructuredOutputError(
                        "The transcription provider returned no diarized transcript."
                    )
                return response, retries
            except TranscriptionStructuredOutputError:
                if retries >= self._max_retries:
                    raise
                self._backoff(retries)
                retries += 1
            except openai.AuthenticationError as exc:
                raise TranscriptionAuthenticationError(
                    "OpenAI authentication failed during meeting transcription."
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise TranscriptionPermissionError(
                    "The OpenAI project cannot access the configured transcription model."
                ) from exc
            except openai.BadRequestError as exc:
                raise TranscriptionRequestError(
                    "OpenAI rejected the meeting transcription request."
                ) from exc
            except openai.RateLimitError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionRateLimitError(
                        "OpenAI rate-limited meeting transcription."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APITimeoutError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionTimeoutError(
                        "The OpenAI meeting transcription request timed out."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIConnectionError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionNetworkError(
                        "The OpenAI API could not be reached for meeting transcription."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIResponseValidationError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionStructuredOutputError(
                        "OpenAI returned an invalid diarized transcript."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._max_retries:
                        raise TranscriptionServerError(
                            "OpenAI returned a server error during meeting transcription."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise TranscriptionRequestError(
                    "OpenAI rejected the meeting transcription request."
                ) from exc
            except OSError as exc:
                raise TranscriptionInputError("A finalized audio part could not be read.") from exc
            except openai.OpenAIError as exc:
                raise TranscriptionRequestError(
                    "The OpenAI meeting transcription request failed."
                ) from exc

    def _backoff(self, retry_index: int) -> None:
        self._sleeper(0.5 * (2**retry_index))

    @staticmethod
    def _usage(response: TranscriptionDiarized) -> TranscriptionUsage:
        usage = response.usage
        if usage is None:
            return TranscriptionUsage()
        if usage.type == "duration":
            return TranscriptionUsage(seconds=usage.seconds)
        return TranscriptionUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        )


def part_transcript_path(audio_path: Path) -> Path:
    return audio_path.with_name(audio_path.name + _PART_TRANSCRIPT_SUFFIX)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise TranscriptionInputError("A finalized audio part could not be read.") from None
    return digest.hexdigest()


def _load_part_transcript(audio_path: Path, digest: str) -> TranscriptionDiarized | None:
    """Return the cached response for exactly this audio, or None when absent or unusable."""

    sidecar = part_transcript_path(audio_path)
    try:
        if sidecar.is_symlink() or not sidecar.is_file():
            return None
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("audio_sha256") != digest:
            return None
        return TranscriptionDiarized.model_validate(payload.get("response"))
    except (OSError, ValueError, TypeError, ValidationError):
        return None


def _store_part_transcript(audio_path: Path, digest: str, response: TranscriptionDiarized) -> None:
    """Best effort: a cache write failure must never fail a transcription that succeeded."""

    sidecar = part_transcript_path(audio_path)
    if sidecar.is_symlink():
        return
    payload = {"audio_sha256": digest, "response": response.model_dump(mode="json")}
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{sidecar.name}.", suffix=".tmp", dir=sidecar.parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sidecar)
        temporary = None
    except OSError:
        return
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
