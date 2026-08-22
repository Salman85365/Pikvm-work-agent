from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx
import openai
import pytest
from openai import OpenAI
from openai.types.audio.transcription_diarized import (
    TranscriptionDiarized,
    TranscriptionDiarizedSegment,
    UsageDuration,
)

from work_agent.meeting.models import AudioPart
from work_agent.meeting.transcription import (
    OpenAITranscriptionProvider,
    TranscriptionInputError,
    TranscriptionNetworkError,
    TranscriptionTimeoutError,
    part_transcript_path,
)


def _response(
    *segments: tuple[str, float, float, str],
    duration: float = 10.0,
) -> TranscriptionDiarized:
    return TranscriptionDiarized(
        duration=duration,
        segments=[
            TranscriptionDiarizedSegment(
                id=f"provider-{index}",
                start=start,
                end=end,
                speaker=speaker,
                text=text,
                type="transcript.text.segment",
            )
            for index, (speaker, start, end, text) in enumerate(segments, start=1)
        ],
        task="transcribe",
        text=" ".join(item[3] for item in segments),
        usage=UsageDuration(seconds=duration, type="duration"),
    )


class _Transcriptions:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        stream = kwargs["file"]
        call = dict(kwargs)
        call["file"] = Path(stream.name).name
        self.calls.append(call)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeOpenAI:
    def __init__(self, responses: list[object]) -> None:
        self.audio = type("Audio", (), {})()
        self.audio.transcriptions = _Transcriptions(responses)


def _audio(tmp_path: Path, name: str = "part.ogg", content: bytes = b"opus") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_openai_uses_diarized_json_and_maps_only_anonymous_speakers(tmp_path: Path) -> None:
    fake = _FakeOpenAI(
        [
            _response(
                ("A", 0.25, 1.5, "Patrick asked Shafiq to validate it."),
                ("B", 1.75, 2.25, "Okay."),
                ("A", 2.5, 3.0, "Before cutover."),
                duration=4.0,
            )
        ]
    )
    clock = iter([10.0, 11.25])
    provider = OpenAITranscriptionProvider(
        client=cast(OpenAI, fake),
        clock=lambda: next(clock),
    )

    result = provider.transcribe([AudioPart(_audio(tmp_path), offset_seconds=5.0)])

    assert [speaker.label for speaker in result.transcript.speakers] == [
        "Speaker 1",
        "Speaker 2",
    ]
    assert [segment.speaker_id for segment in result.transcript.segments] == [
        "speaker-1",
        "speaker-2",
        "speaker-1",
    ]
    assert result.transcript.segments[0].start_seconds == 5.25
    assert result.transcript.segments[-1].end_seconds == 8.0
    assert result.transcript.duration_seconds == 9.0
    assert result.latency_seconds == 1.25
    assert result.usage.seconds == 4.0

    call = fake.audio.transcriptions.calls[0]
    assert call["file"] == "part.ogg"
    assert call["model"] == "gpt-4o-transcribe-diarize"
    assert call["response_format"] == "diarized_json"
    assert call["chunking_strategy"] == "auto"
    assert "prompt" not in call
    assert "known_speaker_names" not in call
    assert "known_speaker_references" not in call
    assert "timestamp_granularities" not in call


def test_the_same_provider_label_in_separate_parts_is_never_merged(tmp_path: Path) -> None:
    fake = _FakeOpenAI(
        [
            _response(("A", 0.0, 1.0, "First voice."), duration=2.0),
            _response(("A", 0.0, 1.0, "Unknown later voice."), duration=2.0),
        ]
    )
    provider = OpenAITranscriptionProvider(client=cast(OpenAI, fake))

    result = provider.transcribe(
        [
            AudioPart(_audio(tmp_path, "one.ogg"), offset_seconds=0.0),
            AudioPart(_audio(tmp_path, "two.ogg"), offset_seconds=10.0),
        ]
    )

    assert [speaker.label for speaker in result.transcript.speakers] == [
        "Speaker 1",
        "Speaker 2",
    ]
    assert [segment.speaker_id for segment in result.transcript.segments] == [
        "speaker-1",
        "speaker-2",
    ]
    assert result.transcript.segments[1].start_seconds == 10.0


def test_no_speech_is_a_valid_empty_timed_transcript(tmp_path: Path) -> None:
    fake = _FakeOpenAI([_response(duration=12.0)])
    provider = OpenAITranscriptionProvider(client=cast(OpenAI, fake))

    result = provider.transcribe([AudioPart(_audio(tmp_path))])

    assert result.transcript.duration_seconds == 12.0
    assert result.transcript.speakers == []
    assert result.transcript.segments == []
    assert result.usage.seconds == 12.0


def test_upload_size_is_checked_before_any_provider_call(tmp_path: Path) -> None:
    fake = _FakeOpenAI([])
    provider = OpenAITranscriptionProvider(
        client=cast(OpenAI, fake),
        max_upload_bytes=3,
    )

    with pytest.raises(TranscriptionInputError, match="upload limit"):
        provider.transcribe([AudioPart(_audio(tmp_path, content=b"four"))])

    assert fake.audio.transcriptions.calls == []


def test_transient_timeout_retries_are_bounded_and_sanitized(tmp_path: Path) -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    failures = [openai.APITimeoutError(request=request) for _ in range(3)]
    fake = _FakeOpenAI(failures)
    sleeps: list[float] = []
    provider = OpenAITranscriptionProvider(
        client=cast(OpenAI, fake),
        max_retries=2,
        sleeper=sleeps.append,
    )

    with pytest.raises(TranscriptionTimeoutError) as caught:
        provider.transcribe([AudioPart(_audio(tmp_path, content=b"SECRET-TRANSCRIPT"))])

    assert len(fake.audio.transcriptions.calls) == 3
    assert sleeps == [0.5, 1.0]
    assert "SECRET-TRANSCRIPT" not in str(caught.value)


def test_a_malformed_diarized_response_is_retried(tmp_path: Path) -> None:
    fake = _FakeOpenAI(
        [
            "not diarized",
            _response(("A", 0.0, 1.0, "Recovered."), duration=2.0),
        ]
    )
    sleeps: list[float] = []
    provider = OpenAITranscriptionProvider(
        client=cast(OpenAI, fake),
        sleeper=sleeps.append,
    )

    result = provider.transcribe([AudioPart(_audio(tmp_path))])

    assert result.retries == 1
    assert result.transcript.segments[0].text == "Recovered."
    assert sleeps == [0.5]


@pytest.mark.parametrize("suffix", [".txt", ".flac"])
def test_unsupported_audio_is_rejected_locally(tmp_path: Path, suffix: str) -> None:
    provider = OpenAITranscriptionProvider(client=cast(OpenAI, _FakeOpenAI([])))

    with pytest.raises(TranscriptionInputError, match="format"):
        provider.transcribe([AudioPart(_audio(tmp_path, name=f"part{suffix}"))])


def test_a_retry_after_a_later_part_fails_does_not_upload_the_finished_part_again(
    tmp_path: Path,
) -> None:
    first = _audio(tmp_path, "audio-0001.ogg", b"opus-one")
    second = _audio(tmp_path, "audio-0002.ogg", b"opus-two")
    parts = [AudioPart(first, offset_seconds=0.0), AudioPart(second, offset_seconds=10.0)]
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    failing = _FakeOpenAI(
        [
            _response(("A", 0.0, 1.0, "First part."), duration=5.0),
            openai.APIConnectionError(request=request),
        ]
    )
    with pytest.raises(TranscriptionNetworkError):
        OpenAITranscriptionProvider(
            client=cast(OpenAI, failing), max_retries=0, sleeper=lambda _: None
        ).transcribe(parts)

    sidecar = part_transcript_path(first)
    assert sidecar.is_file()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert not part_transcript_path(second).exists()

    resumed = _FakeOpenAI([_response(("A", 0.0, 1.0, "Second part."), duration=5.0)])
    result = OpenAITranscriptionProvider(client=cast(OpenAI, resumed)).transcribe(parts)

    assert [call["file"] for call in resumed.audio.transcriptions.calls] == ["audio-0002.ogg"]
    assert [segment.text for segment in result.transcript.segments] == [
        "First part.",
        "Second part.",
    ]
    assert result.transcript.segments[1].start_seconds == 10.0


def test_a_cached_part_transcript_is_ignored_when_the_audio_changed(tmp_path: Path) -> None:
    audio = _audio(tmp_path, "audio-0001.ogg", b"opus-one")
    parts = [AudioPart(audio, offset_seconds=0.0)]
    OpenAITranscriptionProvider(
        client=cast(OpenAI, _FakeOpenAI([_response(("A", 0.0, 1.0, "Old."), duration=5.0)]))
    ).transcribe(parts)
    audio.write_bytes(b"opus-rewritten")

    fresh = _FakeOpenAI([_response(("A", 0.0, 1.0, "New."), duration=5.0)])
    result = OpenAITranscriptionProvider(client=cast(OpenAI, fresh)).transcribe(parts)

    assert len(fresh.audio.transcriptions.calls) == 1
    assert result.transcript.segments[0].text == "New."


def test_part_transcript_caching_can_be_disabled(tmp_path: Path) -> None:
    audio = _audio(tmp_path, "audio-0001.ogg")
    fake = _FakeOpenAI([_response(("A", 0.0, 1.0, "Once."), duration=5.0)])

    OpenAITranscriptionProvider(client=cast(OpenAI, fake), cache_part_transcripts=False).transcribe(
        [AudioPart(audio, offset_seconds=0.0)]
    )

    assert not part_transcript_path(audio).exists()
