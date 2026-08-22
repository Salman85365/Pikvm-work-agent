from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_agent.agent import controller
from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.lock import ControllerLock
from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    ClickElementAction,
    ExecutionTransportStatus,
    RequestUserAction,
    RiskCategory,
    StopCode,
)
from work_agent.slack.agent_operator import AgentAvailabilityOperator
from work_agent.slack.logging import JsonlAvailabilityLogger
from work_agent.slack.models import Availability, AvailabilityResult
from work_agent.slack.objectives import availability_objective
from work_agent.slack.policy import SlackAvailabilityPolicyEngine
from work_agent.slack.service import SlackAvailabilityService
from work_agent.slack.state import AvailabilityTracker, infer_slack_availability
from work_agent.vision import (
    ActionVerification,
    AnalysisUsage,
    ImageDetail,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
    VerificationStatus,
)


@pytest.fixture(autouse=True)
def _temporary_controller_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from work_agent.slack import agent_operator

    monkeypatch.setattr(
        agent_operator,
        "_controller_lock_for_profile",
        lambda kvm: ControllerLock(tmp_path / f"{kvm}.lock"),
    )
    monkeypatch.setattr(agent_operator, "_press_escape", _ESCAPES.append)


_ESCAPES: list[str] = []


def _analysis(toggle: str | None) -> ScreenAnalysis:
    elements = []
    if toggle is not None:
        elements.append(
            UIElement(
                id="manual-availability-toggle",
                label=toggle,
                role=UIElementRole.MENU_ITEM,
                visible_text=toggle,
                bounding_box=None,
                click_point=None,
                confidence=0.98,
            )
        )
    return ScreenAnalysis(
        objective="Inspect Slack manual availability",
        application="Slack",
        screen_state=ScreenState.APPLICATION,
        summary="Slack profile menu is visible.",
        target_found=False,
        target=None,
        relevant_elements=elements,
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.98,
        screenshot_width=1920,
        screenshot_height=1080,
        requested_model="vision-model",
        model="vision-model",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.AUTO,
        reasoning_effort=ReasoningEffort.LOW,
        usage=AnalysisUsage(
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            reasoning_tokens=0,
            total_tokens=2,
        ),
        latency_seconds=0.1,
        retries=0,
        escalated=False,
        attempted_models=["vision-model"],
    )


@pytest.mark.parametrize(
    ("toggle", "expected"),
    [
        ("Set yourself as away", Availability.ACTIVE),
        ("Set yourself as active", Availability.AWAY),
        ("Clear status", None),
        (None, None),
    ],
)
def test_availability_is_inferred_only_from_visible_manual_toggle(
    toggle: str | None,
    expected: Availability | None,
) -> None:
    assert infer_slack_availability(_analysis(toggle)) is expected


@pytest.mark.parametrize(
    ("desired", "observations", "changed"),
    [
        (Availability.ACTIVE, ["Set yourself as away"], False),
        (Availability.AWAY, ["Set yourself as active"], False),
        (
            Availability.AWAY,
            ["Set yourself as away", "Set yourself as active"],
            True,
        ),
        (
            Availability.ACTIVE,
            ["Set yourself as active", "Set yourself as away"],
            True,
        ),
    ],
)
def test_agent_operator_reports_verified_transitions_and_no_ops(
    desired: Availability,
    observations: list[str],
    changed: bool,
) -> None:
    received: dict[str, object] = {}

    def executor(args: object, **kwargs: object) -> object:
        received["args"] = args
        received.update(kwargs)
        observe = kwargs["observation_sink"]
        validate = kwargs["completion_validator"]
        for toggle in observations:
            observe(_analysis(toggle))
        assert validate(_analysis(observations[-1])) is None
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    result = AgentAvailabilityOperator(executor=executor).execute("work-kvm", desired)

    assert result == AvailabilityResult(
        kvm="work-kvm",
        desired=desired,
        observed=desired,
        changed=changed,
        success=True,
        telemetry=result.telemetry,
    )
    assert result.telemetry is not None and result.telemetry.sessions == 1
    args = received["args"]
    assert args.profile == "work-kvm"
    assert args.dry_run is False
    assert args.approval_mode.value == "safe"
    assert isinstance(received["approval_provider"], NonInteractiveApprovalProvider)
    assert isinstance(received["policy_engine"], SlackAvailabilityPolicyEngine)
    assert received["vision_detail"] is ImageDetail.HIGH
    assert received["verification_override"] is not None
    assert received["analyzer_transform"] is not None


def test_get_operation_never_requests_a_change() -> None:
    objectives: list[str] = []

    def executor(args: object, **kwargs: object) -> object:
        objectives.append(args.objective)
        kwargs["observation_sink"](_analysis("Set yourself as away"))
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    result = AgentAvailabilityOperator(executor=executor).execute("work-kvm", None)

    assert result.success is True
    assert result.observed is Availability.ACTIVE
    assert result.changed is False
    assert "Do not change it" in objectives[0]


def test_completion_requires_visible_requested_state() -> None:
    tracker = AvailabilityTracker(Availability.AWAY)

    assert "lacked visible" in tracker.validate_completion(_analysis(None))
    assert "disagreed" in tracker.validate_completion(_analysis("Set yourself as away"))
    assert tracker.validate_completion(_analysis("Set yourself as active")) is None


def test_uncertain_verification_is_overridden_only_by_visible_expected_state() -> None:
    uncertain = ActionVerification(
        status=VerificationStatus.UNCERTAIN,
        confidence=0.95,
        evidence="The generic expected outcome is not directly visible.",
        expected_outcome_observed=False,
    )
    tracker = AvailabilityTracker(Availability.ACTIVE)

    overridden = tracker.override_uncertain_verification(
        _analysis("Set yourself as away"),
        uncertain,
    )

    assert overridden is not None
    assert overridden.status is VerificationStatus.SUCCESS
    assert overridden.expected_outcome_observed is True
    assert (
        tracker.override_uncertain_verification(
            _analysis("Set yourself as active"),
            uncertain,
        )
        is None
    )
    assert tracker.override_uncertain_verification(_analysis(None), uncertain) is None


def _availability_click_proposal() -> ActionProposal:
    return ActionProposal(
        action=ClickElementAction(
            type="click_element",
            element_id="manual_availability_toggle",
            button="left",
        ),
        expected_outcome="The manual availability control visibly changes.",
        confidence=0.99,
        risk=RiskCategory.LOCAL_EDIT,
        reason_summary="Change manual availability.",
    )


def _profile_click_proposal() -> ActionProposal:
    return ActionProposal(
        action=ClickElementAction(
            type="click_element",
            element_id="slack_profile_avatar",
            button="left",
        ),
        expected_outcome="The profile menu opens.",
        confidence=0.98,
        risk=RiskCategory.READ_ONLY,
        reason_summary="Open profile menu.",
    )


def _executed_step(proposal: ActionProposal) -> SimpleNamespace:
    return SimpleNamespace(
        proposal=proposal,
        execution_result=SimpleNamespace(
            hid_action=True,
            transport_status=ExecutionTransportStatus.SENT,
        ),
    )


def test_failed_mutation_gets_one_read_only_final_state_verification() -> None:
    calls: list[object] = []
    trace: list[str] = []

    def executor(args: object, **kwargs: object) -> object:
        calls.append(args)
        if len(calls) == 1:
            kwargs["observation_sink"](_analysis("Set yourself as active"))
            return SimpleNamespace(
                status=AgentFinalStatus.FAILED,
                stop_code=StopCode.VERIFICATION_FAILED,
                summary="Previous action was not verified; it will not be repeated.",
                telemetry=SimpleNamespace(hid_actions=1),
                history=[SimpleNamespace(proposal=_availability_click_proposal())],
            )
        kwargs["observation_sink"](_analysis("Set yourself as away"))
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    result = AgentAvailabilityOperator(
        executor=executor,
        trace_output=trace.append,
    ).execute("work-kvm", Availability.ACTIVE)

    assert result == AvailabilityResult(
        kvm="work-kvm",
        desired=Availability.ACTIVE,
        observed=Availability.ACTIVE,
        changed=True,
        success=True,
        telemetry=result.telemetry,
    )
    assert result.telemetry is not None and result.telemetry.sessions == 2
    assert len(calls) == 2
    assert "Do not change it" in calls[1].objective
    assert trace == [
        "work-kvm  | Starting one read-only final-state verification pass.",
        "work-kvm  | Closed the profile menu with Escape.",
    ]


def test_read_only_verification_never_retries_a_failed_availability_change() -> None:
    calls: list[object] = []

    def executor(args: object, **kwargs: object) -> object:
        calls.append(args)
        kwargs["observation_sink"](_analysis("Set yourself as active"))
        if len(calls) == 1:
            return SimpleNamespace(
                status=AgentFinalStatus.FAILED,
                stop_code=StopCode.VERIFICATION_FAILED,
                summary="Previous action was not verified; it will not be repeated.",
                telemetry=SimpleNamespace(hid_actions=1),
                history=[SimpleNamespace(proposal=_availability_click_proposal())],
            )
        return SimpleNamespace(status=AgentFinalStatus.SUCCESS, stop_code=StopCode.COMPLETED)

    result = AgentAvailabilityOperator(executor=executor).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert result.observed is Availability.AWAY
    assert result.changed is False
    assert "found away, not requested active" in (result.error or "")
    assert len(calls) == 2
    assert all("Set Slack manual availability" not in call.objective for call in calls[1:])


def test_profile_navigation_failure_gets_one_fresh_observe_first_recovery() -> None:
    calls: list[object] = []
    trace: list[str] = []

    def executor(args: object, **kwargs: object) -> object:
        calls.append(args)
        if len(calls) == 2:
            kwargs["observation_sink"](_analysis("Set yourself as away"))
            return SimpleNamespace(
                status=AgentFinalStatus.SUCCESS,
                stop_code=StopCode.COMPLETED,
            )
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.VERIFICATION_FAILED,
            summary="Previous action was not verified; it will not be repeated.",
            telemetry=SimpleNamespace(hid_actions=1),
            history=[_executed_step(_profile_click_proposal())],
        )

    result = AgentAvailabilityOperator(executor=executor, trace_output=trace.append).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is True
    assert result.observed is Availability.ACTIVE
    assert len(calls) == 2
    assert trace == [
        "work-kvm  | Starting one fresh observe-first recovery after uncertain Slack navigation.",
        "work-kvm  | Closed the profile menu with Escape.",
    ]
    assert _ESCAPES[-1] == "work-kvm"


def test_profile_navigation_recovery_is_bounded_to_one_extra_session() -> None:
    calls: list[object] = []

    def executor(args: object, **_kwargs: object) -> object:
        calls.append(args)
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.VERIFICATION_FAILED,
            summary="Previous action was not verified; it will not be repeated.",
            telemetry=SimpleNamespace(hid_actions=1),
            history=[_executed_step(_profile_click_proposal())],
        )

    result = AgentAvailabilityOperator(executor=executor).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert len(calls) == 2


def test_navigation_recovery_followed_by_toggle_uncertainty_uses_read_only_pass() -> None:
    calls: list[object] = []
    trace: list[str] = []

    def executor(args: object, **kwargs: object) -> object:
        calls.append(args)
        if len(calls) == 1:
            return SimpleNamespace(
                status=AgentFinalStatus.FAILED,
                stop_code=StopCode.VERIFICATION_FAILED,
                telemetry=SimpleNamespace(hid_actions=1),
                history=[_executed_step(_profile_click_proposal())],
            )
        if len(calls) == 2:
            kwargs["observation_sink"](_analysis("Set yourself as active"))
            return SimpleNamespace(
                status=AgentFinalStatus.FAILED,
                stop_code=StopCode.VERIFICATION_FAILED,
                telemetry=SimpleNamespace(hid_actions=1),
                history=[SimpleNamespace(proposal=_availability_click_proposal())],
            )
        kwargs["observation_sink"](_analysis("Set yourself as away"))
        return SimpleNamespace(
            status=AgentFinalStatus.SUCCESS,
            stop_code=StopCode.COMPLETED,
        )

    result = AgentAvailabilityOperator(
        executor=executor,
        trace_output=trace.append,
    ).execute("work-kvm", Availability.ACTIVE)

    assert result.success is True
    assert result.observed is Availability.ACTIVE
    assert len(calls) == 3
    assert "Set Slack manual availability" in calls[1].objective
    assert "Do not change it" in calls[2].objective
    assert trace == [
        "work-kvm  | Starting one fresh observe-first recovery after uncertain Slack navigation.",
        "work-kvm  | Starting one read-only final-state verification pass.",
        "work-kvm  | Closed the profile menu with Escape.",
    ]


def test_unexecuted_profile_proposal_does_not_start_navigation_recovery() -> None:
    calls: list[object] = []

    def executor(args: object, **_kwargs: object) -> object:
        calls.append(args)
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.VERIFICATION_FAILED,
            telemetry=SimpleNamespace(hid_actions=1),
            history=[
                SimpleNamespace(
                    proposal=_profile_click_proposal(),
                    execution_result=None,
                )
            ],
        )

    result = AgentAvailabilityOperator(executor=executor).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert len(calls) == 1


def test_unrelated_verification_failure_does_not_start_navigation_recovery() -> None:
    calls: list[object] = []
    unrelated = ActionProposal(
        action=ClickElementAction(
            type="click_element",
            element_id="unrelated_control",
            button="left",
        ),
        expected_outcome="An unrelated control changes.",
        confidence=0.98,
        risk=RiskCategory.READ_ONLY,
        reason_summary="Use an unrelated control.",
    )

    def executor(args: object, **_kwargs: object) -> object:
        calls.append(args)
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.VERIFICATION_FAILED,
            summary="Previous action was not verified; it will not be repeated.",
            telemetry=SimpleNamespace(hid_actions=1),
            history=[_executed_step(unrelated)],
        )

    result = AgentAvailabilityOperator(executor=executor).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert len(calls) == 1


def test_profile_status_control_does_not_qualify_as_profile_menu_navigation() -> None:
    calls: list[object] = []
    status_control = ActionProposal(
        action=ClickElementAction(
            type="click_element",
            element_id="profile_status_button",
            button="left",
        ),
        expected_outcome="The status editor opens.",
        confidence=0.98,
        risk=RiskCategory.READ_ONLY,
        reason_summary="Open a status control.",
    )

    def executor(args: object, **_kwargs: object) -> object:
        calls.append(args)
        return SimpleNamespace(
            status=AgentFinalStatus.FAILED,
            stop_code=StopCode.VERIFICATION_FAILED,
            telemetry=SimpleNamespace(hid_actions=1),
            history=[_executed_step(status_control)],
        )

    result = AgentAvailabilityOperator(executor=executor).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert len(calls) == 1


def test_objective_preserves_slack_workflow_boundary() -> None:
    objective = availability_objective(Availability.ACTIVE)

    assert "Do not read or send messages" in objective
    assert "Do not edit status text" in objective
    assert "Do not simulate activity" in objective
    assert "Never infer availability" in objective
    assert "macOS Dock or Windows taskbar" in objective
    assert "visible profile/avatar control" in objective
    assert "manual Active/Away toggle" in objective
    assert "immediate" in objective
    assert "visibly verifiable expected outcome" in objective
    assert "reopen the profile menu" in objective
    assert "full clickable account avatar/button" in objective
    assert "never on the smaller" in objective


def test_paused_operator_reports_safe_request_user_classification() -> None:
    proposal = ActionProposal(
        action=RequestUserAction(
            type="request_user",
            reason="private model reason",
            message="private visible screen text",
        ),
        expected_outcome="Pause for user assistance.",
        confidence=0.99,
        risk=RiskCategory.UNKNOWN,
        reason_summary="Request assistance.",
    )
    session = SimpleNamespace(
        status=AgentFinalStatus.PAUSED,
        stop_code=StopCode.USER_ASSISTANCE_REQUESTED,
        summary="private visible screen text",
        history=[SimpleNamespace(proposal=proposal)],
        telemetry=SimpleNamespace(planner_calls=1),
    )

    result = AgentAvailabilityOperator(executor=lambda *_args, **_kwargs: session).execute(
        "work-kvm",
        Availability.ACTIVE,
    )

    assert result.success is False
    assert result.error == (
        "The planner requested user assistance instead of guessing from the visible screen."
    )
    assert "private" not in result.error


def test_policy_approval_pause_is_classified_without_private_summary() -> None:
    session = SimpleNamespace(
        status=AgentFinalStatus.PAUSED,
        stop_code=StopCode.APPROVAL_DENIED,
        summary="User rejected or did not provide approval.",
        history=[],
        telemetry=SimpleNamespace(planner_calls=1),
    )
    trace: list[str] = []

    def executor(_args: object, **kwargs: object) -> object:
        kwargs["output"]("State: waiting_approval")
        return session

    result = AgentAvailabilityOperator(executor=executor, trace_output=trace.append).execute(
        "work-kvm",
        None,
    )

    assert result.error == (
        "Local policy required interactive approval, which unattended Slack workflows deny."
    )
    assert trace == ["work-kvm  | State: waiting_approval"]


class _MemoryLogger:
    def __init__(self) -> None:
        self.results: list[AvailabilityResult] = []

    def record(self, result: AvailabilityResult) -> None:
        self.results.append(result)


def test_all_kvms_run_sequentially_and_one_failure_does_not_stop_later_kvms() -> None:
    calls: list[str] = []

    class Operator:
        def execute(self, kvm: str, desired: Availability | None) -> AvailabilityResult:
            calls.append(kvm)
            if kvm == "broken-kvm":
                raise RuntimeError("sensitive internal details")
            return AvailabilityResult(
                kvm=kvm,
                desired=desired,
                observed=desired,
                changed=False,
                success=True,
            )

    logger = _MemoryLogger()
    result = SlackAvailabilityService(Operator(), logger).run(
        ("first-kvm", "broken-kvm", "last-kvm"),
        Availability.AWAY,
    )

    assert calls == ["first-kvm", "broken-kvm", "last-kvm"]
    assert [item.success for item in result.results] == [True, False, True]
    assert "sensitive internal details" not in (result.results[1].error or "")
    assert logger.results == list(result.results)
    assert result.success is False


def test_every_stop_code_maps_to_a_distinct_sanitized_reason() -> None:
    """No controller stop cause may be flattened into a generic string.

    Before stop codes existed, agent_operator matched on the prose summary and mapped only 3 of
    the controller's 8 failure summaries, so 13 of 26 real failures were logged as "stopped with
    status failed" with the cause discarded.
    """
    from work_agent.slack.agent_operator import _STOP_REASONS

    assert set(_STOP_REASONS) == set(StopCode), "every StopCode needs an explicit reason"

    failure_codes = set(StopCode) - {StopCode.COMPLETED, StopCode.DRY_RUN}
    reasons = [_STOP_REASONS[code] for code in failure_codes]
    assert len(set(reasons)) == len(reasons), "failure reasons must not collide"
    assert not any("status failed" in reason for reason in reasons)

    for reason in _STOP_REASONS.values():
        assert reason == reason.strip() and reason.endswith(".")


def test_controller_reaches_every_stop_code_it_declares() -> None:
    """StopCode members must be reachable, or the vocabulary is lying about what can happen."""
    source = Path(controller.__file__).read_text(encoding="utf-8")
    unreachable = [code.name for code in StopCode if f"StopCode.{code.name}" not in source]

    assert unreachable == [], f"declared but never emitted: {unreachable}"


def test_jsonl_log_contains_only_sanitized_operation_metadata(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "availability.jsonl"
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    logger = JsonlAvailabilityLogger(path)
    logger.record(
        AvailabilityResult(
            kvm="work-kvm",
            desired=Availability.AWAY,
            observed=Availability.AWAY,
            changed=True,
            success=True,
        )
    )

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert set(entry) == {
        "timestamp",
        "kvm",
        "desired_availability",
        "observed_availability",
        "changed",
        "outcome",
        "stop_code",
        "error",
        "telemetry",
    }
    assert "screenshot" not in entry
    assert "objective" not in entry
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
