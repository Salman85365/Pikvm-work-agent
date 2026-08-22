from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import OpenAI

from work_agent.agent.config import AgentSettings
from work_agent.agent.errors import (
    PlannerAuthenticationError,
    PlannerRequestError,
    PlannerStructuredOutputError,
    PlannerTimeoutError,
)
from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    FinishAction,
    RequestUserAction,
    RiskCategory,
    zero_usage,
)
from work_agent.agent.openai_planner import OpenAIActionPlanner
from work_agent.agent.prompts import ACTION_PLANNER_PROMPT
from work_agent.vision import (
    BoundingBox,
    ImageDetail,
    NormalizedPoint,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
)


class _Responses:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _OpenAI:
    def __init__(self, outcomes: list[object]) -> None:
        self.responses = _Responses(outcomes)


def _element() -> UIElement:
    return UIElement(
        id="profile",
        label="Profile menu",
        role=UIElementRole.BUTTON,
        visible_text="private visible text must not be copied",
        bounding_box=BoundingBox(x1=10, y1=10, x2=20, y2=20),
        click_point=NormalizedPoint(x=15, y=15),
        confidence=0.96,
    )


def _screen() -> ScreenAnalysis:
    element = _element()
    return ScreenAnalysis(
        objective="Open profile",
        application="Slack",
        screen_state=ScreenState.APPLICATION,
        summary="Slack main window is visible.",
        target_found=True,
        target=element,
        relevant_elements=[element],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.95,
        screenshot_width=1920,
        screenshot_height=1080,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.AUTO,
        reasoning_effort=ReasoningEffort.LOW,
        usage=zero_usage(),
        latency_seconds=0,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


def _proposal(element_id: str = "profile") -> ActionProposal:
    return ActionProposal(
        action=ClickElementAction(
            type="click_element",
            element_id=element_id,
            button="left",
        ),
        expected_outcome="Profile menu becomes visible.",
        confidence=0.95,
        risk=RiskCategory.NAVIGATION,
        reason_summary="Open the visible profile menu.",
    )


def _response(proposal: ActionProposal | None) -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=proposal,
        model="gpt-5.6-terra-2026-07-01",
        service_tier="default",
        usage=SimpleNamespace(
            input_tokens=80,
            input_tokens_details=SimpleNamespace(cached_tokens=5, cache_write_tokens=0),
            output_tokens=20,
            output_tokens_details=SimpleNamespace(reasoning_tokens=4),
            total_tokens=100,
        ),
    )


def _settings(**overrides: object) -> AgentSettings:
    values: dict[str, object] = {"api_key": "local-planner-secret"}
    values.update(overrides)
    return AgentSettings(**values)  # type: ignore[arg-type]


def test_planner_request_is_stateless_private_and_strict() -> None:
    fake = _OpenAI([_response(_proposal())])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    result = planner.plan(
        objective="Open profile",
        screen=_screen(),
        previous_action=None,
        previous_verification=None,
        history=[],
        remaining_steps=5,
    )

    assert len(fake.responses.calls) == 1
    request = fake.responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["store"] is False
    assert request["service_tier"] == "default"
    assert request["reasoning"] == {"effort": "low"}
    assert request["text_format"] is ActionProposal
    assert request["instructions"] == ACTION_PLANNER_PROMPT
    assert "previous_response_id" not in request
    serialized_input = str(request["input"])
    assert "private visible text must not be copied" not in serialized_input
    assert "data:image" not in serialized_input
    assert result.usage.total_tokens == 100
    assert result.model == "gpt-5.6-terra-2026-07-01"


def test_planner_corrects_an_invented_element_id_once_then_gives_up() -> None:
    fake = _OpenAI([_response(_proposal("invented")), _response(_proposal("still_invented"))])
    planner = OpenAIActionPlanner(_settings(planner_max_retries=1), client=cast(OpenAI, fake))

    with pytest.raises(PlannerStructuredOutputError, match="not in the current analysis"):
        planner.plan(
            objective="Open profile",
            screen=_screen(),
            previous_action=None,
            previous_verification=None,
            history=[],
            remaining_steps=5,
        )

    assert len(fake.responses.calls) == 2
    second_input = fake.responses.calls[1]["input"][0]["content"]
    assert '"correction": "The planner referenced element \'invented\'' in second_input


def test_planner_accepts_the_corrected_proposal() -> None:
    fake = _OpenAI([_response(_proposal("invented")), _response(_proposal())])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    result = planner.plan(
        objective="Open profile",
        screen=_screen(),
        previous_action=None,
        previous_verification=None,
        history=[],
        remaining_steps=5,
        feedback="The previous action could not be verified.",
    )

    assert isinstance(result.proposal.action, ClickElementAction)
    assert result.retries == 1
    first_input = fake.responses.calls[0]["input"][0]["content"]
    assert '"controller_feedback": "The previous action could not be verified."' in first_input


def test_planner_accepts_finish_action_as_one_proposal() -> None:
    proposal = ActionProposal(
        action=FinishAction(type="finish", summary="Objective is visible."),
        expected_outcome="No action is required.",
        confidence=0.99,
        risk=RiskCategory.READ_ONLY,
        reason_summary="The objective is already complete.",
    )
    fake = _OpenAI([_response(proposal)])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    result = planner.plan(
        objective="Open profile",
        screen=_screen(),
        previous_action=None,
        previous_verification=None,
        history=[],
        remaining_steps=5,
    )

    assert isinstance(result.proposal.action, FinishAction)


def test_planner_accepts_request_user_action() -> None:
    proposal = ActionProposal(
        action=RequestUserAction(
            type="request_user",
            reason="authentication",
            message="Please handle the visible authentication screen.",
        ),
        expected_outcome="The controller pauses for the user.",
        confidence=0.99,
        risk=RiskCategory.AUTHENTICATION,
        reason_summary="Human authentication is required.",
    )
    fake = _OpenAI([_response(proposal)])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    result = planner.plan(
        objective="Open profile",
        screen=_screen(),
        previous_action=None,
        previous_verification=None,
        history=[],
        remaining_steps=5,
    )

    assert isinstance(result.proposal.action, RequestUserAction)


def test_planner_timeout_retries_are_bounded() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    fake = _OpenAI([openai.APITimeoutError(request=request) for _ in range(3)])
    sleeps: list[float] = []
    planner = OpenAIActionPlanner(
        _settings(planner_max_retries=2),
        client=cast(OpenAI, fake),
        sleeper=sleeps.append,
    )

    with pytest.raises(PlannerTimeoutError):
        planner.plan(
            objective="Open profile",
            screen=_screen(),
            previous_action=None,
            previous_verification=None,
            history=[],
            remaining_steps=5,
        )

    assert len(fake.responses.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_planner_authentication_error_is_not_retried_or_leaked() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError(
        "local-planner-secret was rejected",
        response=response,
        body=None,
    )
    fake = _OpenAI([error])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(PlannerAuthenticationError) as caught:
        planner.plan(
            objective="Open profile",
            screen=_screen(),
            previous_action=None,
            previous_verification=None,
            history=[],
            remaining_steps=5,
        )

    assert len(fake.responses.calls) == 1
    assert "local-planner-secret" not in str(caught.value)


def test_planner_bad_request_is_not_retried_or_leaked() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(400, request=request)
    error = openai.BadRequestError(
        "local-planner-secret appeared in the rejected request",
        response=response,
        body=None,
    )
    fake = _OpenAI([error])
    planner = OpenAIActionPlanner(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(PlannerRequestError) as caught:
        planner.plan(
            objective="Open profile",
            screen=_screen(),
            previous_action=None,
            previous_verification=None,
            history=[],
            remaining_steps=5,
        )

    assert len(fake.responses.calls) == 1
    assert "structured-output schema" in str(caught.value)
    assert "local-planner-secret" not in str(caught.value)
