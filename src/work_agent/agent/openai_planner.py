from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence

import openai
from openai import OpenAI
from openai.types.responses import EasyInputMessageParam, ResponseInputParam
from openai.types.shared_params import Reasoning
from pydantic import ValidationError

from work_agent.agent.config import AgentSettings
from work_agent.agent.errors import (
    PlannerAuthenticationError,
    PlannerNetworkError,
    PlannerPermissionError,
    PlannerRateLimitError,
    PlannerRequestError,
    PlannerServerError,
    PlannerStructuredOutputError,
    PlannerTimeoutError,
)
from work_agent.agent.models import (
    ActionProposal,
    AgentStepSummary,
    ClickElementAction,
    DoubleClickElementAction,
    ExecutionResult,
    MoveMouseAction,
    PlanningResult,
    ScrollAction,
    action_summary,
    zero_usage,
)
from work_agent.agent.policy import SAFE_HOTKEYS, SAFE_KEYS
from work_agent.agent.prompts import ACTION_PLANNER_PROMPT
from work_agent.vision.models import (
    ActionVerification,
    AnalysisUsage,
    ScreenAnalysis,
    UIElement,
)


class OpenAIActionPlanner:
    """Stateless text planner that returns one strict, untrusted proposal."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        client: OpenAI | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            api_key=settings.api_key,
            timeout=settings.planner_request_timeout_seconds,
            max_retries=0,
        )
        self._sleeper = sleeper
        self._clock = clock

    def plan(
        self,
        *,
        objective: str,
        screen: ScreenAnalysis,
        previous_action: ExecutionResult | None,
        previous_verification: ActionVerification | None,
        history: Sequence[AgentStepSummary],
        remaining_steps: int,
        feedback: str | None = None,
    ) -> PlanningResult:
        normalized_objective = objective.strip()
        if not normalized_objective:
            raise PlannerRequestError("The agent objective must not be empty.")
        if remaining_steps < 1:
            raise PlannerRequestError("The planner requires at least one remaining step.")

        started = self._clock()
        retries = 0
        invalid_proposals = 0
        correction: str | None = None
        while True:
            try:
                reasoning: Reasoning = {"effort": self._settings.planner_reasoning_effort.value}
                response = self._client.responses.parse(
                    model=self._settings.planner_model,
                    instructions=ACTION_PLANNER_PROMPT,
                    input=self._input(
                        objective=normalized_objective,
                        screen=screen,
                        previous_action=previous_action,
                        previous_verification=previous_verification,
                        history=history,
                        remaining_steps=remaining_steps,
                        feedback=feedback,
                        correction=correction,
                    ),
                    text_format=ActionProposal,
                    reasoning=reasoning,
                    service_tier=self._settings.planner_service_tier.value,
                    store=False,
                )
                proposal = response.output_parsed
                if not isinstance(proposal, ActionProposal):
                    raise PlannerStructuredOutputError(
                        "OpenAI returned no valid structured action proposal."
                    )
                problem = self._element_reference_problem(proposal, screen)
                if problem is not None:
                    # One invalid reference is a slip worth one correction, not a dead session.
                    if invalid_proposals >= self._settings.planner_max_retries:
                        raise PlannerStructuredOutputError(problem)
                    invalid_proposals += 1
                    retries += 1
                    correction = problem
                    continue
                return PlanningResult(
                    proposal=proposal,
                    requested_model=self._settings.planner_model,
                    model=response.model,
                    requested_service_tier=self._settings.planner_service_tier.value,
                    service_tier=response.service_tier,
                    reasoning_effort=self._settings.planner_reasoning_effort.value,
                    usage=self._extract_usage(response.usage),
                    latency_seconds=max(0.0, self._clock() - started),
                    retries=retries,
                )
            except PlannerStructuredOutputError:
                raise
            except (ValidationError, openai.APIResponseValidationError) as exc:
                if invalid_proposals >= self._settings.planner_max_retries:
                    raise PlannerStructuredOutputError(
                        "OpenAI returned an invalid structured action proposal."
                    ) from exc
                invalid_proposals += 1
                retries += 1
                correction = "The previous proposal was rejected by local validation: " + (
                    _validation_summary(exc)
                )
            except openai.AuthenticationError as exc:
                raise PlannerAuthenticationError(
                    "OpenAI planner authentication failed. Check the local API key."
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise PlannerPermissionError(
                    "The OpenAI project cannot access the configured planner model."
                ) from exc
            except openai.BadRequestError as exc:
                raise PlannerRequestError(
                    "OpenAI rejected the planner request configuration or structured-output schema."
                ) from exc
            except openai.RateLimitError as exc:
                if retries >= self._settings.planner_max_retries:
                    raise PlannerRateLimitError("OpenAI rate-limited the planner request.") from exc
                self._backoff(retries)
                retries += 1
            except openai.APITimeoutError as exc:
                if retries >= self._settings.planner_max_retries:
                    raise PlannerTimeoutError("The OpenAI planner request timed out.") from exc
                self._backoff(retries)
                retries += 1
            except openai.APIConnectionError as exc:
                if retries >= self._settings.planner_max_retries:
                    raise PlannerNetworkError("The OpenAI API could not be reached.") from exc
                self._backoff(retries)
                retries += 1
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    if retries >= self._settings.planner_max_retries:
                        raise PlannerServerError(
                            "OpenAI returned a server error during planning."
                        ) from exc
                    self._backoff(retries)
                    retries += 1
                    continue
                raise PlannerRequestError("OpenAI rejected the planner request.") from exc
            except (openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as exc:
                raise PlannerStructuredOutputError(
                    "OpenAI could not complete the structured action proposal."
                ) from exc
            except openai.OpenAIError as exc:
                raise PlannerRequestError("The OpenAI planner request failed.") from exc

    def _input(
        self,
        *,
        objective: str,
        screen: ScreenAnalysis,
        previous_action: ExecutionResult | None,
        previous_verification: ActionVerification | None,
        history: Sequence[AgentStepSummary],
        remaining_steps: int,
        feedback: str | None = None,
        correction: str | None = None,
    ) -> ResponseInputParam:
        elements: dict[str, UIElement] = {
            element.id: element for element in screen.relevant_elements
        }
        if screen.target is not None:
            elements[screen.target.id] = screen.target
        payload = {
            "objective": objective,
            "screen": {
                "application": screen.application,
                "screen_state": screen.screen_state.value,
                "summary": screen.summary,
                "confidence": screen.confidence,
                "safe_to_continue": screen.safe_to_continue,
                "warnings": [warning.value for warning in screen.warnings],
                "size": {"width": screen.screenshot_width, "height": screen.screenshot_height},
                "target_id": screen.target.id if screen.target is not None else None,
                "elements": [self._element_payload(element) for element in elements.values()],
            },
            "previous_action": (
                action_summary(previous_action.action) if previous_action is not None else None
            ),
            "previous_verification": (
                {
                    "status": previous_verification.status.value,
                    "confidence": previous_verification.confidence,
                    "expected_outcome_observed": (previous_verification.expected_outcome_observed),
                    "evidence": previous_verification.evidence,
                }
                if previous_verification is not None
                else None
            ),
            "recent_history": [item.model_dump(mode="json") for item in history[-5:]],
            "remaining_steps": remaining_steps,
            "controller_feedback": feedback,
            "correction": correction,
            "policy_preapproved": {
                "hotkeys": sorted("+".join(keys) for keys in SAFE_HOTKEYS),
                "keys": sorted(SAFE_KEYS),
                "note": (
                    "Other keys/hotkeys and any typed text outside a visible search field need "
                    "human approval, which unattended runs cannot give."
                ),
            },
        }
        message: EasyInputMessageParam = {
            "role": "user",
            "content": "Controller state:\n" + json.dumps(payload, ensure_ascii=True),
        }
        return [message]

    @staticmethod
    def _element_payload(element: UIElement) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": element.id,
            "label": element.label,
            "role": element.role.value,
            "confidence": element.confidence,
            "clickable": element.click_point is not None,
        }
        # visible_text is deliberately not forwarded: the planner needs geometry and labels,
        # not the private text an element happens to display.
        if element.bounding_box is not None:
            box = element.bounding_box
            payload["box"] = [box.x1, box.y1, box.x2, box.y2]
        elif element.click_point is not None:
            payload["at"] = [element.click_point.x, element.click_point.y]
        return payload

    @staticmethod
    def _element_reference_problem(
        proposal: ActionProposal,
        screen: ScreenAnalysis,
    ) -> str | None:
        action = proposal.action
        if not isinstance(
            action,
            (MoveMouseAction, ClickElementAction, DoubleClickElementAction, ScrollAction),
        ):
            return None
        target_id = action.element_id
        if target_id is None:
            return None
        elements = {element.id: element for element in screen.relevant_elements}
        if screen.target is not None:
            elements[screen.target.id] = screen.target
        clickable = sorted(
            element_id for element_id, item in elements.items() if item.click_point is not None
        )
        element = elements.get(target_id)
        if element is None:
            return (
                f"The planner referenced element {target_id!r}, which is not in the "
                f"current analysis. Clickable element ids: {clickable}."
            )
        if element.click_point is None:
            return (
                f"The planner referenced element {target_id!r}, which has no validated "
                f"click point. Clickable element ids: {clickable}."
            )
        return None

    def _backoff(self, retry_index: int) -> None:
        self._sleeper(0.5 * (2**retry_index))

    @staticmethod
    def _extract_usage(usage: openai.types.responses.ResponseUsage | None) -> AnalysisUsage:
        if usage is None:
            return zero_usage()
        return AnalysisUsage(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.input_tokens_details.cached_tokens,
            cache_write_tokens=usage.input_tokens_details.cache_write_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.output_tokens_details.reasoning_tokens,
            total_tokens=usage.total_tokens,
        )


def _validation_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'proposal'}: {error['msg']}"
            for error in exc.errors()[:5]
        )
    return "the response did not match the action schema"
