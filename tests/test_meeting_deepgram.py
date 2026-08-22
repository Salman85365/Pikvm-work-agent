from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from work_agent.meeting.config import MeetingProvider, MeetingSettings
from work_agent.meeting.deepgram import DeepgramTranscriptionProvider, cache_path
from work_agent.meeting.models import AudioPart
from work_agent.meeting.transcription import (
    TranscriptionAuthenticationError,
    TranscriptionConfigurationError,
    TranscriptionServerError,
)


def _part(tmp_path: Path, name: str, offset: float) -> AudioPart:
    path = tmp_path / name
    path.write_bytes(b"OggS" + bytes(64))
    return AudioPart(path=path, offset_seconds=offset)


def _response(utterances: list[dict[str, object]], duration: float) -> dict[str, object]:
    return {
        "metadata": {"duration": duration, "models": ["nova-3"]},
        "results": {
            "channels": [{"alternatives": [{"transcript": "", "words": []}]}],
            "utterances": utterances,
        },
    }


def _provider(handler: object, **overrides: object) -> DeepgramTranscriptionProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.Client(transport=transport, headers={"Authorization": "Token test"})
    return DeepgramTranscriptionProvider(
        api_key="test", client=client, sleeper=lambda _: None, **overrides
    )  # type: ignore[arg-type]


def test_utterances_become_anonymous_speaker_segments_across_parts(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params["language"] != "en":
            raise AssertionError("language must be forwarded")
        if b"audio-0002" in request.content or len(requests) == 2:
            body = _response(
                [{"transcript": "Second part first line.", "start": 0.5, "end": 2.0, "speaker": 1}],
                duration=30.0,
            )
        else:
            body = _response(
                [
                    {"transcript": "Hello team.", "start": 0.0, "end": 1.5, "speaker": 0},
                    {"transcript": "Hi there.", "start": 1.6, "end": 3.0, "speaker": 1},
                    {"transcript": "   ", "start": 3.0, "end": 3.1, "speaker": 1},
                ],
                duration=300.0,
            )
        return httpx.Response(200, json=body)

    provider = _provider(handler)
    result = provider.transcribe(
        [_part(tmp_path, "audio-0002.ogg", 300.0), _part(tmp_path, "audio-0001.ogg", 0.0)]
    )

    assert result.provider == "deepgram" and result.model == "nova-3"
    transcript = result.transcript
    assert [speaker.label for speaker in transcript.speakers] == [
        "Speaker 1",
        "Speaker 2",
        "Speaker 3",
    ]
    assert [(round(s.start_seconds, 1), s.text) for s in transcript.segments] == [
        (0.0, "Hello team."),
        (1.6, "Hi there."),
        (300.5, "Second part first line."),
    ]
    assert transcript.duration_seconds == 330.0
    assert result.usage.seconds == 330.0
    assert all(r.headers["Authorization"] == "Token test" for r in requests)
    assert all(r.headers["Content-Type"] == "audio/ogg" for r in requests)
    assert requests[0].url.params["diarize"] == "true"
    assert requests[0].url.params["utterances"] == "true"

    # Cached sidecars mean a second run uploads nothing.
    assert cache_path(tmp_path / "audio-0001.ogg").is_file()
    before = len(requests)
    provider.transcribe([_part(tmp_path, "audio-0001.ogg", 0.0)])
    assert len(requests) == before


def test_server_errors_are_retried_then_sanitized(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"err_msg": "busy"})

    provider = _provider(handler, max_retries=1, cache_part_transcripts=False)
    with pytest.raises(TranscriptionServerError):
        provider.transcribe([_part(tmp_path, "audio-0001.ogg", 0.0)])
    assert calls == 2


def test_bad_key_is_an_authentication_error_without_the_body(tmp_path: Path) -> None:
    provider = _provider(
        lambda _: httpx.Response(401, json={"err_msg": "secret-ish body"}),
        cache_part_transcripts=False,
    )
    with pytest.raises(TranscriptionAuthenticationError) as caught:
        provider.transcribe([_part(tmp_path, "audio-0001.ogg", 0.0)])
    assert "secret-ish" not in str(caught.value)


def test_missing_key_is_a_configuration_error() -> None:
    with pytest.raises(TranscriptionConfigurationError, match="DEEPGRAM_API_KEY"):
        DeepgramTranscriptionProvider(api_key="")


def test_settings_select_deepgram_and_refuse_it_for_intelligence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEETING_TRANSCRIPTION_PROVIDER", "deepgram")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("DEEPGRAM_MODEL", "nova-3")
    monkeypatch.setenv("MEETING_DATA_DIR", str(tmp_path / "meetings"))
    monkeypatch.setenv("MEETING_STATE_PATH", str(tmp_path / "state.json"))

    settings = MeetingSettings.from_env()
    assert settings.transcription_provider is MeetingProvider.DEEPGRAM
    assert settings.deepgram_api_key == "dg-key"
    assert "dg-key" not in repr(settings)

    monkeypatch.setenv("MEETING_INTELLIGENCE_PROVIDER", "deepgram")
    with pytest.raises(Exception, match="Deepgram only transcribes"):
        MeetingSettings.from_env()


def test_word_level_fallback_groups_by_speaker(tmp_path: Path) -> None:
    body = {
        "metadata": {"duration": 4.0},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "one two three",
                            "words": [
                                {
                                    "word": "one",
                                    "punctuated_word": "One",
                                    "start": 0.0,
                                    "end": 0.5,
                                    "speaker": 0,
                                },
                                {
                                    "word": "two",
                                    "punctuated_word": "two.",
                                    "start": 0.6,
                                    "end": 1.0,
                                    "speaker": 0,
                                },
                                {
                                    "word": "three",
                                    "punctuated_word": "Three.",
                                    "start": 2.0,
                                    "end": 2.5,
                                    "speaker": 1,
                                },
                            ],
                        }
                    ]
                }
            ]
        },
    }
    provider = _provider(lambda _: httpx.Response(200, json=body), cache_part_transcripts=False)
    result = provider.transcribe([_part(tmp_path, "audio-0001.ogg", 0.0)])
    assert [(s.speaker_id, s.text) for s in result.transcript.segments] == [
        ("speaker-1", "One two."),
        ("speaker-2", "Three."),
    ]
    assert json.dumps(result.model_dump(mode="json"))  # serialisable
