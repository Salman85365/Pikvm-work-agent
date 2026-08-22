from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from typing import Literal, Protocol, TypeAlias, TypeVar, cast

import openai
from openai import OpenAI
from openai.types.responses import EasyInputMessageParam, ResponseInputParam
from openai.types.shared_params import Reasoning
from pydantic import ValidationError

from work_agent.meeting.errors import MeetingError
from work_agent.meeting.models import (
    ActionItem,
    BlockerRisk,
    Confidence,
    Decision,
    FollowUp,
    IntelligenceResult,
    MeetingIntelligence,
    OpenQuestion,
    OwnerCategory,
    ProviderUsage,
    Reference,
    Transcript,
)
from work_agent.meeting.prompts import MEETING_INTELLIGENCE_PROMPT
from work_agent.pikvm import WorkIdentity


class MeetingIntelligenceError(MeetingError):
    """A sanitized intelligence failure safe to show or record."""


class IntelligenceConfigurationError(MeetingIntelligenceError):
    pass


class IntelligenceAuthenticationError(MeetingIntelligenceError):
    pass


class IntelligencePermissionError(MeetingIntelligenceError):
    pass


class IntelligenceRequestError(MeetingIntelligenceError):
    pass


class IntelligenceNetworkError(MeetingIntelligenceError):
    pass


class IntelligenceRateLimitError(MeetingIntelligenceError):
    pass


class IntelligenceTimeoutError(MeetingIntelligenceError):
    pass


class IntelligenceServerError(MeetingIntelligenceError):
    pass


class IntelligenceStructuredOutputError(MeetingIntelligenceError):
    pass


class MeetingIntelligenceProvider(Protocol):
    def extract(
        self,
        transcript: Transcript,
        *,
        work_identity: WorkIdentity | None,
    ) -> IntelligenceResult: ...


_OwnedItemT = TypeVar("_OwnedItemT", ActionItem, FollowUp)
_EvidenceItem: TypeAlias = ActionItem | Decision | BlockerRisk | OpenQuestion | Reference | FollowUp
_CONTEXTUAL_OUR_REFERENCE = re.compile(
    r"(?<!\w)(?:your side|your team|your group)(?!\w)",
    re.IGNORECASE,
)


def no_speech_intelligence_result() -> IntelligenceResult:
    """Return the provider-neutral deterministic result for an empty transcript."""

    return IntelligenceResult(
        intelligence=MeetingIntelligence(
            summary="No speech was transcribed.",
            action_items=[],
            decisions=[],
            blockers_and_risks=[],
            open_questions=[],
            references=[],
            follow_ups=[],
        ),
        provider="local",
        model="deterministic-no-speech-v1",
        service_tier=None,
        latency_seconds=0.0,
        retries=0,
        usage=ProviderUsage(),
    )


def guard_meeting_intelligence(
    intelligence: MeetingIntelligence,
    transcript: Transcript,
    *,
    work_identity: WorkIdentity | None,
) -> MeetingIntelligence:
    """Validate grounding and conservatively enforce per-KVM ownership locally."""

    evidence = {segment.id: segment.text for segment in transcript.segments}
    for item in _all_evidence_items(intelligence):
        missing = set(item.evidence_segment_ids) - evidence.keys()
        if missing:
            raise IntelligenceStructuredOutputError(
                "Meeting intelligence referenced evidence absent from the transcript."
            )

    actions = [
        _guard_owned_item(item, evidence, work_identity=work_identity)
        for item in intelligence.action_items
    ]
    follow_ups = [
        _guard_owned_item(item, evidence, work_identity=work_identity)
        for item in intelligence.follow_ups
    ]
    return intelligence.model_copy(
        update={
            "action_items": actions,
            "follow_ups": follow_ups,
        }
    )


def _all_evidence_items(intelligence: MeetingIntelligence) -> Iterable[_EvidenceItem]:
    yield from intelligence.action_items
    yield from intelligence.decisions
    yield from intelligence.blockers_and_risks
    yield from intelligence.open_questions
    yield from intelligence.references
    yield from intelligence.follow_ups


def _guard_owned_item(
    item: _OwnedItemT,
    evidence: dict[str, str],
    *,
    work_identity: WorkIdentity | None,
) -> _OwnedItemT:
    evidence_text = "\n".join(evidence[item_id] for item_id in item.evidence_segment_ids)
    if work_identity is None:
        if item.owner_category in {
            OwnerCategory.OUR_IDENTITY,
            OwnerCategory.POSSIBLY_OUR_IDENTITY,
        }:
            return item.model_copy(update={"owner_category": OwnerCategory.UNKNOWN})
        return item

    explicit_identity = _identity_matches(work_identity, item.owner) and _mentions_identity(
        evidence_text,
        work_identity,
    )
    if item.owner_category not in {
        OwnerCategory.OUR_IDENTITY,
        OwnerCategory.POSSIBLY_OUR_IDENTITY,
    }:
        return item
    if explicit_identity:
        return item

    owner_claims_identity = _identity_matches(work_identity, item.owner)
    if owner_claims_identity:
        return item.model_copy(
            update={
                "owner": None,
                "owner_category": OwnerCategory.UNKNOWN,
            }
        )

    contextual = bool(item.reason and _CONTEXTUAL_OUR_REFERENCE.search(evidence_text))
    if contextual:
        return item.model_copy(
            update={
                "owner": None,
                "owner_category": OwnerCategory.POSSIBLY_OUR_IDENTITY,
                "confidence": (
                    Confidence.MEDIUM if item.confidence is Confidence.HIGH else item.confidence
                ),
            }
        )
    return item.model_copy(update={"owner_category": OwnerCategory.UNKNOWN})


def _mentions_identity(text: str, identity: WorkIdentity) -> bool:
    normalized_text = _normalized_identity(text)
    return any(
        re.search(rf"(?<!\w){re.escape(_normalized_identity(name))}(?!\w)", normalized_text)
        is not None
        for name in identity.aliases
    )


def _identity_matches(identity: WorkIdentity, value: str | None) -> bool:
    return value is not None and identity.matches(value)


def _normalized_identity(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class OpenAIMeetingIntelligenceProvider:
    """Stateless, strict meeting extraction through the OpenAI Responses API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-5.6-terra",
        service_tier: str = "default",
        reasoning_effort: str = "low",
        request_timeout_seconds: float = 120.0,
        max_retries: int = 2,
        client: OpenAI | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        normalized_model = model.strip()
        if not normalized_model or any(character.isspace() for character in normalized_model):
            raise IntelligenceConfigurationError(
                "The meeting-intelligence model must be a non-empty identifier without whitespace."
            )
        if service_tier not in {"default", "flex"}:
            raise IntelligenceConfigurationError(
                "The meeting-intelligence service tier must be default or flex."
            )
        if reasoning_effort not in {"none", "low", "medium"}:
            raise IntelligenceConfigurationError(
                "Meeting-intelligence reasoning effort must be none, low, or medium."
            )
        if request_timeout_seconds <= 0:
            raise IntelligenceConfigurationError(
                "The meeting-intelligence request timeout must be greater than zero."
            )
        if not 0 <= max_retries <= 2:
            raise IntelligenceConfigurationError(
                "Meeting-intelligence retries must be between zero and two."
            )
        if client is None and not (api_key and api_key.strip()):
            raise IntelligenceConfigurationError(
                "An API key is required for OpenAI meeting intelligence."
            )

        self._model = normalized_model
        self._service_tier = cast(Literal["default", "flex"], service_tier)
        self._reasoning_effort = cast(
            Literal["none", "low", "medium"],
            reasoning_effort,
        )
        self._max_retries = max_retries
        self._client = client or OpenAI(
            api_key=api_key.strip() if api_key is not None else None,
            timeout=request_timeout_seconds,
            max_retries=0,
        )
        self._sleeper = sleeper
        self._clock = clock

    def extract(
        self,
        transcript: Transcript,
        *,
        work_identity: WorkIdentity | None,
    ) -> IntelligenceResult:
        if not transcript.segments:
            return no_speech_intelligence_result()
        started = self._clock()
        retries = 0
        while True:
            try:
                reasoning: Reasoning = {"effort": self._reasoning_effort}
                response = self._client.responses.parse(
                    model=self._model,
                    instructions=MEETING_INTELLIGENCE_PROMPT,
                    input=self._input(transcript, work_identity),
                    text_format=MeetingIntelligence,
                    reasoning=reasoning,
                    service_tier=self._service_tier,
                    store=False,
                )
                parsed = response.output_parsed
                if not isinstance(parsed, MeetingIntelligence):
                    raise IntelligenceStructuredOutputError(
                        "OpenAI returned no valid structured meeting intelligence."
                    )
                intelligence = guard_meeting_intelligence(
                    parsed,
                    transcript,
                    work_identity=work_identity,
                )
                return IntelligenceResult(
                    intelligence=intelligence,
                    provider="openai",
                    model=response.model,
                    service_tier=response.service_tier,
                    latency_seconds=max(0.0, self._clock() - started),
                    retries=retries,
                    usage=self._usage(response.usage),
                )
            except IntelligenceStructuredOutputError:
                if retries >= self._max_retries:
                    raise
                self._backoff(retries)
                retries += 1
            except (ValidationError, openai.APIResponseValidationError) as exc:
                if retries >= self._max_retries:
                    raise IntelligenceStructuredOutputError(
                        "OpenAI returned invalid structured meeting intelligence."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.AuthenticationError as exc:
                raise IntelligenceAuthenticationError(
                    "OpenAI authentication failed during meeting intelligence."
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise IntelligencePermissionError(
                    "The OpenAI project cannot access the configured meeting-intelligence model."
                ) from exc
            except openai.BadRequestError as exc:
                raise IntelligenceRequestError(
                    "OpenAI rejected the meeting-intelligence request."
                ) from exc
            except openai.RateLimitError as exc:
                if retries >= self._max_retries:
                    raise IntelligenceRateLimitError(
                        "OpenAI rate-limited meeting intelligence."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APITimeoutError as exc:
                if retries >= self._max_retries:
                    raise IntelligenceTimeoutError(
                        "The OpenAI meeting-intelligence request timed out."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIConnectionError as exc:
                if retries >= self._max_retries:
                    raise IntelligenceNetworkError(
                        "The OpenAI API could not be reached for meeting intelligence."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._max_retries:
                        raise IntelligenceServerError(
                            "OpenAI returned a server error during meeting intelligence."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise IntelligenceRequestError(
                    "OpenAI rejected the meeting-intelligence request."
                ) from exc
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                raise IntelligenceStructuredOutputError(
                    "OpenAI could not complete the structured meeting intelligence."
                ) from exc
            except openai.OpenAIError as exc:
                raise IntelligenceRequestError(
                    "The OpenAI meeting-intelligence request failed."
                ) from exc

    def _input(
        self,
        transcript: Transcript,
        work_identity: WorkIdentity | None,
    ) -> ResponseInputParam:
        speaker_labels = {speaker.id: speaker.label for speaker in transcript.speakers}
        payload = {
            "work_identity": (
                {"name": work_identity.name, "aliases": work_identity.aliases}
                if work_identity is not None
                else None
            ),
            "transcript": {
                "duration_seconds": transcript.duration_seconds,
                "language": transcript.language,
                "segments": [
                    {
                        "id": segment.id,
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "speaker": speaker_labels[segment.speaker_id],
                        "text": segment.text,
                    }
                    for segment in transcript.segments
                ],
            },
        }
        message: EasyInputMessageParam = {
            "role": "user",
            "content": "Meeting evidence:\n" + json.dumps(payload, ensure_ascii=True),
        }
        return [message]

    def _backoff(self, retry_index: int) -> None:
        self._sleeper(0.5 * (2**retry_index))

    @staticmethod
    def _usage(usage: openai.types.responses.ResponseUsage | None) -> ProviderUsage:
        if usage is None:
            return ProviderUsage()
        return ProviderUsage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.input_tokens_details.cached_tokens,
            cache_write_tokens=usage.input_tokens_details.cache_write_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
            total_tokens=usage.total_tokens,
        )
