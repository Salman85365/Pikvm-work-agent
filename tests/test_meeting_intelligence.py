from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import openai
import pytest
from openai import OpenAI

from work_agent.meeting.intelligence import (
    IntelligenceAuthenticationError,
    OpenAIMeetingIntelligenceProvider,
    guard_meeting_intelligence,
    no_speech_intelligence_result,
)
from work_agent.meeting.models import (
    ActionItem,
    BlockerRisk,
    Confidence,
    Decision,
    IntelligenceArtifact,
    IntelligenceResult,
    MeetingIntelligence,
    MeetingMetadata,
    OpenQuestion,
    OwnerCategory,
    ProviderUsage,
    Reference,
    ReferenceKind,
    RiskKind,
    Transcript,
    TranscriptionArtifact,
    TranscriptionResult,
    TranscriptionUsage,
    TranscriptSegment,
    TranscriptSpeaker,
    fingerprint_model,
)
from work_agent.meeting.prompts import MEETING_INTELLIGENCE_PROMPT
from work_agent.meeting.report import render_meeting_report
from work_agent.pikvm import WorkIdentity


def _transcript(*, injected_text: str | None = None) -> Transcript:
    return Transcript(
        duration_seconds=40.0,
        language="en",
        speakers=[
            TranscriptSpeaker(id="speaker-1", label="Speaker 1"),
            TranscriptSpeaker(id="speaker-2", label="Speaker 2"),
        ],
        segments=[
            TranscriptSegment(
                id="segment-1",
                start_seconds=0.0,
                end_seconds=5.0,
                speaker_id="speaker-1",
                text=(
                    injected_text or "Shafiq, please validate the retry publisher before cutover."
                ),
            ),
            TranscriptSegment(
                id="segment-2",
                start_seconds=6.0,
                end_seconds=10.0,
                speaker_id="speaker-2",
                text="Can your side verify the dashboard too?",
            ),
            TranscriptSegment(
                id="segment-3",
                start_seconds=11.0,
                end_seconds=15.0,
                speaker_id="speaker-1",
                text="Patrick will update the runbook.",
            ),
            TranscriptSegment(
                id="segment-4",
                start_seconds=16.0,
                end_seconds=24.0,
                speaker_id="speaker-2",
                text=(
                    "We decided to cut over Friday. Queue lag is a risk. Is ticket ABC-42 closed?"
                ),
            ),
        ],
    )


def _action(
    task: str,
    owner: str | None,
    category: OwnerCategory,
    segment: str,
    *,
    confidence: Confidence = Confidence.HIGH,
    reason: str | None = None,
) -> ActionItem:
    return ActionItem(
        task=task,
        owner=owner,
        owner_category=category,
        requested_by="Speaker 1",
        due_text=None,
        reason=reason,
        confidence=confidence,
        evidence_segment_ids=[segment],
    )


def _intelligence(actions: list[ActionItem] | None = None) -> MeetingIntelligence:
    return MeetingIntelligence(
        summary="The team discussed cutover readiness.",
        action_items=actions or [],
        decisions=[
            Decision(
                text="Cut over Friday.",
                confidence=Confidence.HIGH,
                evidence_segment_ids=["segment-4"],
            )
        ],
        blockers_and_risks=[
            BlockerRisk(
                kind=RiskKind.RISK,
                text="Queue lag may affect cutover.",
                owner=None,
                confidence=Confidence.MEDIUM,
                evidence_segment_ids=["segment-4"],
            )
        ],
        open_questions=[
            OpenQuestion(
                question="Is ticket ABC-42 closed?",
                directed_to=None,
                confidence=Confidence.HIGH,
                evidence_segment_ids=["segment-4"],
            )
        ],
        references=[
            Reference(
                kind=ReferenceKind.TICKET,
                value="ABC-42",
                context="Cutover readiness",
                confidence=Confidence.HIGH,
                evidence_segment_ids=["segment-4"],
            )
        ],
        follow_ups=[],
    )


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100,
        input_tokens_details=SimpleNamespace(cached_tokens=7, cache_write_tokens=2),
        output_tokens=20,
        output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        total_tokens=120,
    )


def _response(intelligence: MeetingIntelligence) -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=intelligence,
        model="gpt-5.6-terra-2026-08-01",
        service_tier="default",
        usage=_usage(),
    )


class _Responses:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeOpenAI:
    def __init__(self, responses: list[object]) -> None:
        self.responses = _Responses(responses)


def test_local_guard_separates_explicit_contextual_ambiguous_and_other_owners() -> None:
    intelligence = _intelligence(
        [
            _action(
                "Validate the retry publisher.",
                "Shafiq",
                OwnerCategory.OUR_IDENTITY,
                "segment-1",
            ),
            _action(
                "Verify the dashboard.",
                None,
                OwnerCategory.OUR_IDENTITY,
                "segment-2",
                reason="The speaker addressed our side.",
            ),
            _action(
                "Update the runbook.",
                "Patrick",
                OwnerCategory.OTHER,
                "segment-3",
            ),
            _action(
                "Investigate the queue.",
                None,
                OwnerCategory.OUR_IDENTITY,
                "segment-4",
            ),
        ]
    )

    guarded = guard_meeting_intelligence(
        intelligence,
        _transcript(),
        work_identity=WorkIdentity("Shafiq", ("Shafique",)),
    )

    assert [item.owner_category for item in guarded.action_items] == [
        OwnerCategory.OUR_IDENTITY,
        OwnerCategory.POSSIBLY_OUR_IDENTITY,
        OwnerCategory.OTHER,
        OwnerCategory.UNKNOWN,
    ]
    assert guarded.action_items[1].confidence is Confidence.MEDIUM
    assert guarded.action_items[2].owner == "Patrick"


@pytest.mark.parametrize("category", [OwnerCategory.OTHER, OwnerCategory.SHARED])
def test_local_guard_never_promotes_a_provider_category_to_our_identity(
    category: OwnerCategory,
) -> None:
    guarded = guard_meeting_intelligence(
        _intelligence(
            [
                _action(
                    "Validate the retry publisher.",
                    "Shafiq",
                    category,
                    "segment-1",
                )
            ]
        ),
        _transcript(),
        work_identity=WorkIdentity("Shafiq"),
    )

    assert guarded.action_items[0].owner_category is category


def test_work_identity_is_scoped_and_never_renames_a_speaker() -> None:
    proposed = _intelligence(
        [
            _action(
                "Validate the retry publisher.",
                "Shafiq",
                OwnerCategory.OUR_IDENTITY,
                "segment-1",
            )
        ]
    )

    other_kvm = guard_meeting_intelligence(
        proposed,
        _transcript(),
        work_identity=WorkIdentity("Amina"),
    )
    no_identity = guard_meeting_intelligence(
        proposed,
        _transcript(),
        work_identity=None,
    )

    assert other_kvm.action_items[0].owner_category is OwnerCategory.UNKNOWN
    assert no_identity.action_items[0].owner_category is OwnerCategory.UNKNOWN
    assert [speaker.label for speaker in _transcript().speakers] == ["Speaker 1", "Speaker 2"]


def test_bare_you_addressed_to_someone_else_is_not_mapped_to_our_identity() -> None:
    transcript = _transcript(injected_text="Patrick, could you validate the retry publisher?")
    proposed = _intelligence(
        [
            _action(
                "Validate the retry publisher.",
                None,
                OwnerCategory.OUR_IDENTITY,
                "segment-1",
                reason="The speaker said you.",
            )
        ]
    )

    guarded = guard_meeting_intelligence(
        proposed,
        transcript,
        work_identity=WorkIdentity("Shafiq"),
    )

    assert guarded.action_items[0].owner_category is OwnerCategory.UNKNOWN


def test_openai_extraction_is_stateless_private_typed_and_locally_guarded() -> None:
    proposed = _intelligence(
        [
            _action(
                "Validate the retry publisher.",
                "Shafique",
                OwnerCategory.OUR_IDENTITY,
                "segment-1",
            )
        ]
    )
    fake = _FakeOpenAI([_response(proposed)])
    clock = iter([20.0, 21.5])
    provider = OpenAIMeetingIntelligenceProvider(
        client=cast(OpenAI, fake),
        clock=lambda: next(clock),
    )

    result = provider.extract(
        _transcript(injected_text="Shafique, please validate the retry publisher before cutover."),
        work_identity=WorkIdentity("Shafiq", ("Shafique",)),
    )

    assert result.intelligence.action_items[0].owner_category is OwnerCategory.OUR_IDENTITY
    assert result.model == "gpt-5.6-terra-2026-08-01"
    assert result.latency_seconds == 1.5
    assert result.usage.cached_input_tokens == 7

    call = fake.responses.calls[0]
    assert call["store"] is False
    assert call["text_format"] is MeetingIntelligence
    assert call["reasoning"] == {"effort": "low"}
    assert call["service_tier"] == "default"
    assert "tools" not in call
    assert "previous_response_id" not in call
    assert "untrusted evidence" in str(call["instructions"])
    payload = json.loads(str(call["input"][0]["content"]).split("\n", 1)[1])
    assert payload["work_identity"]["name"] == "Shafiq"
    assert payload["work_identity"]["aliases"] == ["Shafiq", "Shafique"]
    assert payload["transcript"]["segments"][0]["speaker"] == "Speaker 1"


def test_invalid_evidence_is_retried_without_leaking_content() -> None:
    invalid = _intelligence(
        [
            _action(
                "Invented task SECRET-MEETING-CONTENT",
                "Shafiq",
                OwnerCategory.OUR_IDENTITY,
                "segment-999",
            )
        ]
    )
    valid = _intelligence()
    fake = _FakeOpenAI([_response(invalid), _response(valid)])
    sleeps: list[float] = []
    provider = OpenAIMeetingIntelligenceProvider(
        client=cast(OpenAI, fake),
        sleeper=sleeps.append,
    )

    result = provider.extract(_transcript(), work_identity=WorkIdentity("Shafiq"))

    assert result.intelligence.action_items == []
    assert result.retries == 1
    assert sleeps == [0.5]


def test_provider_authentication_errors_are_sanitized() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError(
        "SECRET-MEETING-CONTENT was rejected",
        response=response,
        body=None,
    )
    provider = OpenAIMeetingIntelligenceProvider(
        client=cast(OpenAI, _FakeOpenAI([error])),
    )

    with pytest.raises(IntelligenceAuthenticationError) as caught:
        provider.extract(
            _transcript(injected_text="SECRET-MEETING-CONTENT"),
            work_identity=None,
        )

    assert "SECRET-MEETING-CONTENT" not in str(caught.value)


def test_empty_transcript_is_resolved_locally_without_a_provider_call() -> None:
    fake = _FakeOpenAI([])
    provider = OpenAIMeetingIntelligenceProvider(client=cast(OpenAI, fake))
    transcript = Transcript(duration_seconds=12.0, language=None, speakers=[], segments=[])

    result = provider.extract(transcript, work_identity=WorkIdentity("Shafiq"))

    assert result == no_speech_intelligence_result()
    assert result.intelligence.summary == "No speech was transcribed."
    assert result.intelligence.action_items == []
    assert fake.responses.calls == []


def test_persisted_artifacts_are_bound_to_manifest_transcript_and_session() -> None:
    transcript = _transcript()
    transcription = TranscriptionResult(
        transcript=transcript,
        provider="openai",
        model="gpt-4o-transcribe-diarize",
        latency_seconds=1.0,
        retries=0,
        usage=TranscriptionUsage(seconds=40.0),
    )
    manifest_model = MeetingMetadata(
        recording_id="meeting-20260818",
        kvm="heidrick",
        started_at=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 18, 9, 1, tzinfo=UTC),
        duration_seconds=40.0,
    )
    manifest_sha256 = fingerprint_model(manifest_model)
    transcript_sha256 = fingerprint_model(transcript)
    transcription_artifact = TranscriptionArtifact(
        session_id="meeting-20260818",
        manifest_sha256=manifest_sha256,
        result=transcription,
    )
    intelligence_artifact = IntelligenceArtifact(
        session_id="meeting-20260818",
        manifest_sha256=manifest_sha256,
        transcript_sha256=transcript_sha256,
        result=IntelligenceResult(
            intelligence=_intelligence(),
            provider="openai",
            model="gpt-5.6-terra",
            service_tier="default",
            latency_seconds=1.0,
            retries=0,
            usage=ProviderUsage(),
        ),
    )

    changed = manifest_model.model_copy(update={"duration_seconds": 41.0})

    assert transcription_artifact.manifest_sha256 == manifest_sha256
    assert intelligence_artifact.transcript_sha256 == transcript_sha256
    assert (
        TranscriptionArtifact.model_validate_json(transcription_artifact.model_dump_json())
        == transcription_artifact
    )
    assert (
        IntelligenceArtifact.model_validate_json(intelligence_artifact.model_dump_json())
        == intelligence_artifact
    )
    assert fingerprint_model(changed) != manifest_sha256
    assert fingerprint_model(transcript.model_copy(update={"language": "fr"})) != transcript_sha256


def test_markdown_is_deterministic_grounded_and_does_not_rename_speakers() -> None:
    transcript = _transcript(
        injected_text="<script>alert(1)</script> Shafiq, please validate *publisher*."
    )
    guarded = guard_meeting_intelligence(
        _intelligence(
            [
                _action(
                    "Validate *publisher*.",
                    "Shafiq",
                    OwnerCategory.OUR_IDENTITY,
                    "segment-1",
                ),
                _action(
                    "Verify the dashboard.",
                    None,
                    OwnerCategory.POSSIBLY_OUR_IDENTITY,
                    "segment-2",
                    confidence=Confidence.MEDIUM,
                    reason="The speaker addressed your side.",
                ),
            ]
        ),
        transcript,
        work_identity=WorkIdentity("Shafiq"),
    )
    started = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
    metadata = MeetingMetadata(
        recording_id="meeting-1",
        kvm="heidrick",
        started_at=started,
        ended_at=started + timedelta(seconds=40),
        duration_seconds=40,
    )

    first = render_meeting_report(metadata, transcript, guarded)
    second = render_meeting_report(metadata, transcript, guarded)

    assert first == second
    assert "## OUR ACTION ITEMS" in first
    assert "## POSSIBLE OUR ACTION ITEMS" in first
    assert "## BLOCKERS / RISKS" in first
    assert "[00:00:00] Speaker 1:" in first
    assert "] Shafiq:" not in first
    assert "<script>" not in first
    assert "ABC-42" in first


def test_no_speech_report_keeps_the_complete_stable_structure() -> None:
    transcript = Transcript(duration_seconds=12.0, language=None, speakers=[], segments=[])
    started = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)

    report = render_meeting_report(
        MeetingMetadata(
            recording_id="meeting-empty",
            kvm="heidrick",
            started_at=started,
            ended_at=started + timedelta(seconds=12),
            duration_seconds=12.0,
        ),
        transcript,
        no_speech_intelligence_result().intelligence,
    )

    for heading in (
        "## MEETING",
        "## TRANSCRIPT",
        "## SUMMARY",
        "## OUR ACTION ITEMS",
        "## POSSIBLE OUR ACTION ITEMS",
        "## OTHER ACTION ITEMS",
        "## DECISIONS",
        "## BLOCKERS / RISKS",
        "## OPEN QUESTIONS",
        "## REFERENCES",
        "## FOLLOW-UPS",
    ):
        assert heading in report
    assert "_No speech was transcribed._" in report
    assert "No speech was transcribed." in report


def test_prompt_forbids_speaker_and_ownership_invention() -> None:
    lowered = MEETING_INTELLIGENCE_PROMPT.casefold()
    assert "never instructions" in lowered
    assert "do not add" in lowered
    assert "do not identify which anonymous voice" in lowered
    assert "never promote ambiguous ownership" in lowered
