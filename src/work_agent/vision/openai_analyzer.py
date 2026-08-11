from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import openai
from openai import OpenAI
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseInputImageParam,
    ResponseInputParam,
    ResponseInputTextParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

from work_agent.vision.config import VisionSettings
from work_agent.vision.errors import (
    VisionAuthenticationError,
    VisionError,
    VisionImageError,
    VisionNetworkError,
    VisionPermissionError,
    VisionRateLimitError,
    VisionRequestError,
    VisionServerError,
    VisionStructuredOutputError,
    VisionTimeoutError,
)
from work_agent.vision.images import decode_image
from work_agent.vision.models import (
    AnalysisOptions,
    AnalysisUsage,
    ImageDetail,
    ObservationContext,
    PerceptionTelemetry,
    ReasoningEffort,
    SafetyWarning,
    ScreenAnalysis,
    ScreenObservation,
    ScreenObservationPerception,
    ScreenPerception,
    ScreenState,
    ServiceTier,
)
from work_agent.vision.prompts import SCREEN_ANALYSIS_PROMPT, SCREEN_OBSERVATION_PROMPT

_PerceptionT = TypeVar("_PerceptionT", bound=BaseModel)


def _transient_perception_error(exc: Exception) -> VisionError:
    if isinstance(exc, openai.RateLimitError):
        return VisionRateLimitError("OpenAI rate-limited the perception request.")
    if isinstance(exc, openai.APITimeoutError):
        return VisionTimeoutError("The OpenAI perception request timed out.")
    return VisionNetworkError("The OpenAI API could not be reached.")


def _perception_request_error(exc: openai.APIStatusError) -> VisionError:
    if isinstance(exc, openai.AuthenticationError):
        return VisionAuthenticationError("OpenAI authentication failed. Check the local API key.")
    if isinstance(exc, openai.PermissionDeniedError):
        return VisionPermissionError("The OpenAI project cannot access the requested model.")
    return VisionRequestError("OpenAI rejected the perception request.")


@dataclass(frozen=True, slots=True)
class _ModelResult:
    perception: ScreenPerception
    model: str
    service_tier: str | None
    usage: AnalysisUsage
    latency_seconds: float
    retries: int


@dataclass(frozen=True, slots=True)
class _ObservationModelResult:
    perception: ScreenObservationPerception
    model: str
    service_tier: str | None
    usage: AnalysisUsage
    latency_seconds: float
    retries: int


class OpenAIScreenAnalyzer:
    """Stateless screen perception using the OpenAI Responses API."""

    def __init__(
        self,
        settings: VisionSettings,
        *,
        client: OpenAI | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            api_key=settings.api_key,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )
        self._sleeper = sleeper
        self._clock = clock

    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenAnalysis:
        normalized_objective = objective.strip()
        if not normalized_objective:
            raise VisionRequestError("The screen-analysis objective must not be empty.")

        decoded = decode_image(screenshot)
        if decoded.width != width or decoded.height != height:
            raise VisionImageError(
                "The supplied screenshot dimensions do not match the decoded image."
            )

        selected = options or AnalysisOptions()
        requested_model = selected.model or self._settings.model
        service_tier = selected.service_tier or self._settings.service_tier
        reasoning_effort = selected.reasoning_effort or self._settings.reasoning_effort
        image_detail = selected.image_detail or self._settings.image_detail
        data_url = self._data_url(decoded.content, decoded.media_type)

        primary = self._run_model(
            data_url=data_url,
            objective=normalized_objective,
            model=requested_model,
            service_tier=service_tier,
            reasoning_effort=reasoning_effort,
            image_detail=image_detail,
        )
        final = primary
        usage = primary.usage
        latency = primary.latency_seconds
        retries = primary.retries
        attempted_models = [requested_model]
        escalated = False

        if (
            self._settings.escalation_enabled
            and primary.perception.confidence < self._settings.confidence_threshold
            and requested_model != self._settings.fallback_model
        ):
            fallback_model = self._settings.fallback_model
            fallback = self._run_model(
                data_url=data_url,
                objective=normalized_objective,
                model=fallback_model,
                service_tier=service_tier,
                reasoning_effort=reasoning_effort,
                image_detail=image_detail,
            )
            final = fallback
            usage += fallback.usage
            latency += fallback.latency_seconds
            retries += fallback.retries
            attempted_models.append(fallback_model)
            escalated = True

        perception = self._apply_local_safety(final.perception)
        return ScreenAnalysis(
            objective=normalized_objective,
            **perception.model_dump(),
            screenshot_width=width,
            screenshot_height=height,
            requested_model=requested_model,
            model=final.model,
            requested_service_tier=service_tier,
            service_tier=final.service_tier,
            image_detail=image_detail,
            reasoning_effort=reasoning_effort,
            usage=usage,
            latency_seconds=latency,
            retries=retries,
            escalated=escalated,
            attempted_models=attempted_models,
        )

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenObservation:
        decoded = decode_image(screenshot)
        if decoded.width != width or decoded.height != height:
            raise VisionImageError(
                "The supplied screenshot dimensions do not match the decoded image."
            )

        selected = options or AnalysisOptions()
        requested_model = selected.model or self._settings.model
        service_tier = selected.service_tier or self._settings.service_tier
        reasoning_effort = selected.reasoning_effort or self._settings.reasoning_effort
        image_detail = selected.image_detail or self._settings.image_detail
        data_url = self._data_url(decoded.content, decoded.media_type)

        primary = self._run_observation_model(
            data_url=data_url,
            context=context,
            model=requested_model,
            service_tier=service_tier,
            reasoning_effort=reasoning_effort,
            image_detail=image_detail,
        )
        final = primary
        usage = primary.usage
        latency = primary.latency_seconds
        retries = primary.retries
        attempted_models = [requested_model]
        escalated = False

        if (
            self._settings.escalation_enabled
            and primary.perception.analysis.confidence < self._settings.confidence_threshold
            and requested_model != self._settings.fallback_model
        ):
            fallback_model = self._settings.fallback_model
            fallback = self._run_observation_model(
                data_url=data_url,
                context=context,
                model=fallback_model,
                service_tier=service_tier,
                reasoning_effort=reasoning_effort,
                image_detail=image_detail,
            )
            final = fallback
            usage += fallback.usage
            latency += fallback.latency_seconds
            retries += fallback.retries
            attempted_models.append(fallback_model)
            escalated = True

        verification = final.perception.previous_action_verification
        if context.previous_action is None and verification is not None:
            raise VisionStructuredOutputError(
                "OpenAI returned previous-action verification for the first observation."
            )
        if context.previous_action is not None and verification is None:
            raise VisionStructuredOutputError(
                "OpenAI omitted required previous-action verification."
            )

        perception = self._apply_local_safety(final.perception.analysis)
        analysis = ScreenAnalysis(
            objective=context.objective,
            **perception.model_dump(),
            screenshot_width=width,
            screenshot_height=height,
            requested_model=requested_model,
            model=final.model,
            requested_service_tier=service_tier,
            service_tier=final.service_tier,
            image_detail=image_detail,
            reasoning_effort=reasoning_effort,
            usage=usage,
            latency_seconds=latency,
            retries=retries,
            escalated=escalated,
            attempted_models=attempted_models,
        )
        return ScreenObservation(
            analysis=analysis,
            previous_action_verification=verification,
        )

    def _run_model(
        self,
        *,
        data_url: str,
        objective: str,
        model: str,
        service_tier: ServiceTier,
        reasoning_effort: ReasoningEffort,
        image_detail: ImageDetail,
    ) -> _ModelResult:
        started = self._clock()
        retries = 0
        while True:
            try:
                reasoning: Reasoning = {"effort": reasoning_effort.value}
                response = self._client.responses.parse(
                    model=model,
                    instructions=SCREEN_ANALYSIS_PROMPT,
                    input=self._build_input(data_url, objective, image_detail),
                    text_format=ScreenPerception,
                    reasoning=reasoning,
                    service_tier=service_tier.value,
                    store=False,
                )
                perception = response.output_parsed
                if not isinstance(perception, ScreenPerception):
                    raise VisionStructuredOutputError(
                        "OpenAI returned no valid structured screen analysis."
                    )
                return _ModelResult(
                    perception=perception,
                    model=response.model,
                    service_tier=response.service_tier,
                    usage=self._extract_usage(response.usage),
                    latency_seconds=max(0.0, self._clock() - started),
                    retries=retries,
                )
            except VisionStructuredOutputError:
                raise
            except (ValidationError, openai.APIResponseValidationError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI returned an invalid structured screen analysis."
                ) from exc
            except openai.AuthenticationError as exc:
                raise VisionAuthenticationError(
                    "OpenAI authentication failed. Check the local API key."
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise VisionPermissionError(
                    "The OpenAI project cannot access the requested screen-analysis model."
                ) from exc
            except openai.BadRequestError as exc:
                raise VisionRequestError(
                    "OpenAI rejected the screen-analysis request configuration."
                ) from exc
            except openai.RateLimitError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionRateLimitError(
                        "OpenAI rate-limited the screen-analysis request."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APITimeoutError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionTimeoutError(
                        "The OpenAI screen-analysis request timed out."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIConnectionError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionNetworkError("The OpenAI API could not be reached.") from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._settings.max_retries:
                        raise VisionServerError(
                            "OpenAI returned a server error during screen analysis."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise VisionRequestError("OpenAI rejected the screen-analysis request.") from exc
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI could not complete the structured screen analysis."
                ) from exc
            except openai.OpenAIError as exc:
                raise VisionRequestError("The OpenAI screen-analysis request failed.") from exc

    def _run_observation_model(
        self,
        *,
        data_url: str,
        context: ObservationContext,
        model: str,
        service_tier: ServiceTier,
        reasoning_effort: ReasoningEffort,
        image_detail: ImageDetail,
    ) -> _ObservationModelResult:
        started = self._clock()
        retries = 0
        while True:
            try:
                reasoning: Reasoning = {"effort": reasoning_effort.value}
                response = self._client.responses.parse(
                    model=model,
                    instructions=SCREEN_OBSERVATION_PROMPT,
                    input=self._build_observation_input(data_url, context, image_detail),
                    text_format=ScreenObservationPerception,
                    reasoning=reasoning,
                    service_tier=service_tier.value,
                    store=False,
                )
                perception = response.output_parsed
                if not isinstance(perception, ScreenObservationPerception):
                    raise VisionStructuredOutputError(
                        "OpenAI returned no valid structured screen observation."
                    )
                return _ObservationModelResult(
                    perception=perception,
                    model=response.model,
                    service_tier=response.service_tier,
                    usage=self._extract_usage(response.usage),
                    latency_seconds=max(0.0, self._clock() - started),
                    retries=retries,
                )
            except VisionStructuredOutputError:
                raise
            except (ValidationError, openai.APIResponseValidationError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI returned an invalid structured screen observation."
                ) from exc
            except openai.AuthenticationError as exc:
                raise VisionAuthenticationError(
                    "OpenAI authentication failed. Check the local API key."
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise VisionPermissionError(
                    "The OpenAI project cannot access the requested screen-analysis model."
                ) from exc
            except openai.BadRequestError as exc:
                raise VisionRequestError(
                    "OpenAI rejected the screen-observation request configuration."
                ) from exc
            except openai.RateLimitError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionRateLimitError(
                        "OpenAI rate-limited the screen-observation request."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APITimeoutError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionTimeoutError(
                        "The OpenAI screen-observation request timed out."
                    ) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIConnectionError as exc:
                if retries >= self._settings.max_retries:
                    raise VisionNetworkError("The OpenAI API could not be reached.") from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._settings.max_retries:
                        raise VisionServerError(
                            "OpenAI returned a server error during screen observation."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise VisionRequestError("OpenAI rejected the screen-observation request.") from exc
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI could not complete the structured screen observation."
                ) from exc
            except openai.OpenAIError as exc:
                raise VisionRequestError("The OpenAI screen-observation request failed.") from exc

    def perceive(
        self,
        screenshot: bytes,
        *,
        schema: type[_PerceptionT],
        instructions: str,
        context: str,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> tuple[_PerceptionT, PerceptionTelemetry]:
        """Read one screenshot into an arbitrary strict schema.

        Skills that need richer typed output than UIElement labels can carry use this rather
        than encoding structure into element text. It issues no HID and returns only what the
        supplied schema allows.
        """

        decoded = decode_image(screenshot)
        if decoded.width != width or decoded.height != height:
            raise VisionImageError(
                "The supplied screenshot dimensions do not match the decoded image."
            )

        selected = options or AnalysisOptions()
        requested_model = selected.model or self._settings.model
        service_tier = selected.service_tier or self._settings.service_tier
        reasoning_effort = selected.reasoning_effort or self._settings.reasoning_effort
        image_detail = selected.image_detail or self._settings.image_detail
        data_url = self._data_url(decoded.content, decoded.media_type)

        started = self._clock()
        retries = 0
        while True:
            try:
                reasoning: Reasoning = {"effort": reasoning_effort.value}
                response = self._client.responses.parse(
                    model=requested_model,
                    instructions=instructions,
                    input=self._build_perception_input(data_url, context, image_detail),
                    text_format=schema,
                    reasoning=reasoning,
                    service_tier=service_tier.value,
                    store=False,
                )
                perception = response.output_parsed
                if not isinstance(perception, schema):
                    raise VisionStructuredOutputError(
                        "OpenAI returned no valid structured perception."
                    )
                telemetry = PerceptionTelemetry(
                    requested_model=requested_model,
                    model=response.model,
                    requested_service_tier=service_tier,
                    service_tier=response.service_tier,
                    image_detail=image_detail,
                    reasoning_effort=reasoning_effort,
                    usage=self._extract_usage(response.usage),
                    latency_seconds=max(0.0, self._clock() - started),
                    retries=retries,
                )
                return perception, telemetry
            except VisionStructuredOutputError:
                raise
            except (ValidationError, openai.APIResponseValidationError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI returned an invalid structured perception."
                ) from exc
            except (
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.APIConnectionError,
            ) as exc:
                if retries >= self._settings.max_retries:
                    raise _transient_perception_error(exc) from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._settings.max_retries:
                        raise VisionServerError(
                            "OpenAI returned a server error during perception."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise _perception_request_error(exc) from exc
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                raise VisionStructuredOutputError(
                    "OpenAI could not complete the structured perception."
                ) from exc
            except openai.OpenAIError as exc:
                raise VisionRequestError("The OpenAI perception request failed.") from exc

    def _apply_local_safety(self, perception: ScreenPerception) -> ScreenPerception:
        warnings = list(perception.warnings)
        state_warning = {
            ScreenState.AUTHENTICATION: SafetyWarning.AUTHENTICATION_PROMPT,
            ScreenState.LOCK_SCREEN: SafetyWarning.LOCK_SCREEN,
            ScreenState.DISCONNECTED: SafetyWarning.REMOTE_DISCONNECT,
            ScreenState.UNKNOWN: SafetyWarning.UNKNOWN_STATE,
        }.get(perception.screen_state)
        if state_warning is not None and state_warning not in warnings:
            warnings.append(state_warning)
        if (
            perception.confidence < self._settings.confidence_threshold
            and SafetyWarning.LOW_CONFIDENCE not in warnings
        ):
            warnings.append(SafetyWarning.LOW_CONFIDENCE)

        stop_warnings = {
            SafetyWarning.AUTHENTICATION_PROMPT,
            SafetyWarning.LOCK_SCREEN,
            SafetyWarning.UNEXPECTED_DIALOG,
            SafetyWarning.DESTRUCTIVE_CONFIRMATION,
            SafetyWarning.REMOTE_DISCONNECT,
            SafetyWarning.LOW_CONFIDENCE,
            SafetyWarning.UNKNOWN_STATE,
        }
        must_stop = any(warning in stop_warnings for warning in warnings)
        safe_to_continue = perception.safe_to_continue and not must_stop
        stop_reason = perception.stop_reason
        if not safe_to_continue and not stop_reason:
            stop_reason = "Local safety policy requires review before continuing."
        if safe_to_continue:
            stop_reason = None
        return perception.model_copy(
            update={
                "warnings": warnings,
                "safe_to_continue": safe_to_continue,
                "stop_reason": stop_reason,
            }
        )

    def _backoff(self, retry_index: int) -> None:
        self._sleeper(0.5 * (2**retry_index))

    @staticmethod
    def _data_url(content: bytes, media_type: str) -> str:
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{media_type};base64,{encoded}"

    @staticmethod
    def _build_input(
        data_url: str,
        objective: str,
        image_detail: ImageDetail,
    ) -> ResponseInputParam:
        text: ResponseInputTextParam = {
            "type": "input_text",
            "text": f"Objective: {objective}",
        }
        image: ResponseInputImageParam = {
            "type": "input_image",
            "image_url": data_url,
            "detail": image_detail.value,
        }
        message: EasyInputMessageParam = {
            "role": "user",
            "content": [text, image],
        }
        return [message]

    @staticmethod
    def _build_observation_input(
        data_url: str,
        context: ObservationContext,
        image_detail: ImageDetail,
    ) -> ResponseInputParam:
        context_payload = {
            "objective": context.objective,
            "previous_action": context.previous_action,
            "expected_outcome": context.expected_outcome,
        }
        text: ResponseInputTextParam = {
            "type": "input_text",
            "text": "Observation context:\n" + json.dumps(context_payload, ensure_ascii=True),
        }
        image: ResponseInputImageParam = {
            "type": "input_image",
            "image_url": data_url,
            "detail": image_detail.value,
        }
        message: EasyInputMessageParam = {
            "role": "user",
            "content": [text, image],
        }
        return [message]

    @staticmethod
    def _build_perception_input(
        data_url: str,
        context: str,
        image_detail: ImageDetail,
    ) -> ResponseInputParam:
        text: ResponseInputTextParam = {"type": "input_text", "text": context}
        image: ResponseInputImageParam = {
            "type": "input_image",
            "image_url": data_url,
            "detail": image_detail.value,
        }
        message: EasyInputMessageParam = {"role": "user", "content": [text, image]}
        return [message]

    @staticmethod
    def _extract_usage(usage: openai.types.responses.ResponseUsage | None) -> AnalysisUsage:
        if usage is None:
            return AnalysisUsage(
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                total_tokens=0,
            )
        return AnalysisUsage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.input_tokens_details.cached_tokens,
            cache_write_tokens=usage.input_tokens_details.cache_write_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
            total_tokens=usage.total_tokens,
        )
