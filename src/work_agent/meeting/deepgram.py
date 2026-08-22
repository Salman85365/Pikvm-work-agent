"""Deepgram pre-recorded transcription with diarization, as a `TranscriptionProvider`.

Each finalized Ogg/Opus part is posted once to Deepgram's `/v1/listen` endpoint with
`diarize=true&utterances=true`; the utterances become anonymous `Speaker N` segments exactly as
the OpenAI provider produces them, so the intelligence stage cannot tell the providers apart. The
raw response is cached beside the part (owner-only, inside the protected session directory) so a
retry after a later failure never re-uploads or re-bills a part. Nothing here logs or returns
transcript text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from work_agent.meeting.models import (
    AudioPart,
    Transcript,
    TranscriptionResult,
    TranscriptionUsage,
    TranscriptSegment,
    TranscriptSpeaker,
)
from work_agent.meeting.transcription import (
    TranscriptionAuthenticationError,
    TranscriptionConfigurationError,
    TranscriptionInputError,
    TranscriptionNetworkError,
    TranscriptionPermissionError,
    TranscriptionRateLimitError,
    TranscriptionRequestError,
    TranscriptionServerError,
    TranscriptionStructuredOutputError,
    TranscriptionTimeoutError,
)

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEFAULT_DEEPGRAM_MODEL = "nova-3"
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # Deepgram's documented pre-recorded limit
_PART_CACHE_SUFFIX = ".deepgram.json"
_CONTENT_TYPES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


class DeepgramTranscriptionProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = DEFAULT_DEEPGRAM_MODEL,
        language: str | None = "en",
        request_timeout_seconds: float = 600.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        cache_part_transcripts: bool = True,
        listen_url: str = DEEPGRAM_LISTEN_URL,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model or any(character.isspace() for character in normalized_model):
            raise TranscriptionConfigurationError(
                "DEEPGRAM_MODEL must be a non-empty identifier without whitespace."
            )
        if request_timeout_seconds <= 0:
            raise TranscriptionConfigurationError(
                "The transcription request timeout must be greater than zero."
            )
        if not 0 <= max_retries <= 5:
            raise TranscriptionConfigurationError(
                "Transcription retries must be between zero and five."
            )
        key = (api_key or "").strip()
        if client is None and not key:
            raise TranscriptionConfigurationError(
                "DEEPGRAM_API_KEY is required for Deepgram meeting transcription."
            )
        self._model = normalized_model
        self._language = (language or "").strip() or None
        self._max_retries = max_retries
        self._sleeper = sleeper
        self._clock = clock
        self._cache = cache_part_transcripts
        self._listen_url = listen_url
        self._client = client or httpx.Client(
            headers={"Authorization": f"Token {key}", "User-Agent": "pikvm-work-agent/0.1.1"},
            timeout=httpx.Timeout(request_timeout_seconds, connect=15.0),
        )

    # ----- provider protocol ---------------------------------------------------------------

    def transcribe(self, parts: Sequence[AudioPart]) -> TranscriptionResult:
        selected = tuple(parts)
        if not selected:
            raise TranscriptionInputError("At least one finalized audio part is required.")
        for part in selected:
            _validate_part(part.path)

        started = self._clock()
        raw_segments: list[tuple[float, float, int, str, int]] = []
        speaker_numbers: dict[tuple[int, int], int] = {}
        total_retries = 0
        usage = TranscriptionUsage()
        duration = 0.0
        language: str | None = None

        indexed = sorted(enumerate(selected), key=lambda item: (item[1].offset_seconds, item[0]))
        for part_index, part in indexed:
            response, retries = self._cached_or_transcribed(part.path)
            total_retries += retries
            metadata = response.get("metadata") if isinstance(response, dict) else None
            part_duration = _as_float(metadata.get("duration")) if isinstance(metadata, dict) else 0
            usage += TranscriptionUsage(seconds=part_duration)
            duration = max(duration, part.offset_seconds + part_duration)
            for order, utterance in enumerate(_utterances(response)):
                text = str(utterance.get("transcript") or "").strip()
                if not text:
                    continue
                raw_speaker = utterance.get("speaker")
                speaker_index = int(raw_speaker) if isinstance(raw_speaker, int | float) else 0
                number = speaker_numbers.setdefault(
                    (part_index, speaker_index), len(speaker_numbers) + 1
                )
                start = part.offset_seconds + max(0.0, _as_float(utterance.get("start")))
                end = part.offset_seconds + max(
                    _as_float(utterance.get("start")), _as_float(utterance.get("end"))
                )
                duration = max(duration, end)
                raw_segments.append((start, end, number, text, order))
            detected = _detected_language(response)
            if detected and language is None:
                language = detected

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
                speaker_id=f"speaker-{number}",
                text=text,
            )
            for index, (start, end, number, text, _) in enumerate(raw_segments, start=1)
        ]
        return TranscriptionResult(
            transcript=Transcript(
                duration_seconds=duration,
                language=language,
                speakers=speakers,
                segments=segments,
            ),
            provider="deepgram",
            model=self._model,
            latency_seconds=max(0.0, self._clock() - started),
            retries=total_retries,
            usage=usage,
        )

    # ----- transport -----------------------------------------------------------------------

    def _cached_or_transcribed(self, path: Path) -> tuple[dict[str, Any], int]:
        if not self._cache:
            return self._transcribe_part(path)
        digest = _sha256(path)
        cached = _load_cache(path, digest)
        if cached is not None:
            return cached, 0
        response, retries = self._transcribe_part(path)
        _store_cache(path, digest, response)
        return response, retries

    def _transcribe_part(self, path: Path) -> tuple[dict[str, Any], int]:
        params: dict[str, str] = {
            "model": self._model,
            "diarize": "true",
            "utterances": "true",
            "smart_format": "true",
            "punctuate": "true",
        }
        if self._language:
            params["language"] = self._language
        try:
            audio = path.read_bytes()
        except OSError as exc:
            raise TranscriptionInputError("A finalized audio part could not be read.") from exc
        content_type = _CONTENT_TYPES.get(path.suffix.casefold(), "application/octet-stream")

        retries = 0
        while True:
            try:
                response = self._client.post(
                    self._listen_url,
                    params=params,
                    content=audio,
                    headers={"Content-Type": content_type},
                )
            except httpx.TimeoutException as exc:
                if retries >= self._max_retries:
                    raise TranscriptionTimeoutError(
                        "The Deepgram transcription request timed out."
                    ) from exc
                self._backoff(retries)
                retries += 1
                continue
            except httpx.HTTPError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionNetworkError(
                        "Deepgram could not be reached for meeting transcription."
                    ) from exc
                self._backoff(retries)
                retries += 1
                continue

            status = response.status_code
            if status == 401:
                raise TranscriptionAuthenticationError(
                    "Deepgram rejected the API key. Check DEEPGRAM_API_KEY."
                )
            if status in {402, 403}:
                raise TranscriptionPermissionError(
                    "The Deepgram project is not allowed to use the configured model or has no "
                    "remaining balance."
                )
            if status == 429:
                if retries >= self._max_retries:
                    raise TranscriptionRateLimitError(
                        "Deepgram rate-limited meeting transcription."
                    )
                self._backoff(retries)
                retries += 1
                continue
            if status >= 500:
                if retries >= self._max_retries:
                    raise TranscriptionServerError(
                        "Deepgram returned a server error during meeting transcription."
                    )
                self._backoff(retries)
                retries += 1
                continue
            if status >= 400:
                raise TranscriptionRequestError(
                    f"Deepgram rejected the transcription request (HTTP {status})."
                )
            try:
                payload = response.json()
            except ValueError as exc:
                if retries >= self._max_retries:
                    raise TranscriptionStructuredOutputError(
                        "Deepgram returned a response that was not valid JSON."
                    ) from exc
                self._backoff(retries)
                retries += 1
                continue
            if not isinstance(payload, dict) or "results" not in payload:
                raise TranscriptionStructuredOutputError(
                    "Deepgram returned a transcript without results."
                )
            return payload, retries

    def _backoff(self, retry_index: int) -> None:
        self._sleeper(0.5 * (2**retry_index))


# ----- helpers ------------------------------------------------------------------------------


def _utterances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    utterances = results.get("utterances")
    if isinstance(utterances, list) and utterances:
        return [item for item in utterances if isinstance(item, dict)]
    # Fallback when utterances were not returned: one segment per channel alternative,
    # using word-level speaker labels to split.
    channels = results.get("channels")
    if not isinstance(channels, list) or not channels:
        return []
    alternatives = channels[0].get("alternatives") if isinstance(channels[0], dict) else None
    if not isinstance(alternatives, list) or not alternatives:
        return []
    words = alternatives[0].get("words") if isinstance(alternatives[0], dict) else None
    if not isinstance(words, list):
        transcript = str(alternatives[0].get("transcript") or "")
        return [{"transcript": transcript, "start": 0.0, "end": 0.0, "speaker": 0}]
    grouped: list[dict[str, Any]] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        speaker = word.get("speaker", 0)
        text = str(word.get("punctuated_word") or word.get("word") or "")
        if grouped and grouped[-1]["speaker"] == speaker:
            grouped[-1]["transcript"] += " " + text
            grouped[-1]["end"] = word.get("end", grouped[-1]["end"])
        else:
            grouped.append(
                {
                    "transcript": text,
                    "start": word.get("start", 0.0),
                    "end": word.get("end", 0.0),
                    "speaker": speaker,
                }
            )
    return grouped


def _detected_language(payload: dict[str, Any]) -> str | None:
    results = payload.get("results")
    if not isinstance(results, dict):
        return None
    channels = results.get("channels")
    if isinstance(channels, list) and channels and isinstance(channels[0], dict):
        language = channels[0].get("detected_language")
        if isinstance(language, str) and language.strip():
            return language.strip()[:40]
    return None


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _validate_part(path: Path) -> None:
    try:
        is_file = path.is_file()
        size = path.stat().st_size if is_file else 0
    except OSError:
        raise TranscriptionInputError("A finalized audio part could not be read.") from None
    if not is_file:
        raise TranscriptionInputError("A finalized audio part could not be read.")
    if path.suffix.casefold() not in _CONTENT_TYPES:
        raise TranscriptionInputError(
            "The finalized audio format is not supported by the transcription provider."
        )
    if size <= 0:
        raise TranscriptionInputError("A finalized audio part is empty.")
    if size > _MAX_UPLOAD_BYTES:
        raise TranscriptionInputError(
            "A finalized audio part exceeds the transcription provider's upload limit."
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise TranscriptionInputError("A finalized audio part could not be read.") from None
    return digest.hexdigest()


def cache_path(audio_path: Path) -> Path:
    return audio_path.with_name(audio_path.name + _PART_CACHE_SUFFIX)


def _load_cache(audio_path: Path, digest: str) -> dict[str, Any] | None:
    sidecar = cache_path(audio_path)
    try:
        if sidecar.is_symlink() or not sidecar.is_file():
            return None
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("audio_sha256") != digest:
        return None
    response = payload.get("response")
    return response if isinstance(response, dict) else None


def _store_cache(audio_path: Path, digest: str, response: dict[str, Any]) -> None:
    sidecar = cache_path(audio_path)
    if sidecar.is_symlink():
        return
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
            json.dump(
                {"audio_sha256": digest, "response": response},
                stream,
                ensure_ascii=True,
                separators=(",", ":"),
            )
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
