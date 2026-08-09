from __future__ import annotations

from work_agent.agent.approval import TerminalApprovalProvider
from work_agent.agent.models import (
    ActionProposal,
    PolicyDecision,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
)


def _proposal() -> ActionProposal:
    return ActionProposal(
        action=PressKeyAction(type="press_key", key="Enter"),
        expected_outcome="A form may submit.",
        confidence=0.95,
        risk=RiskCategory.EXTERNAL_COMMUNICATION,
        reason_summary="Submit the form.",
    )


def _policy() -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionKind.REQUIRE_APPROVAL,
        reason="This can have an external effect.",
        inferred_risk=RiskCategory.EXTERNAL_COMMUNICATION,
    )


def test_terminal_approval_defaults_to_no() -> None:
    provider = TerminalApprovalProvider(input_reader=lambda _: "", output=lambda _: None)

    assert provider.approve(proposal=_proposal(), policy=_policy(), objective="Submit") is False


def test_terminal_approval_requires_explicit_yes() -> None:
    provider = TerminalApprovalProvider(input_reader=lambda _: "yes", output=lambda _: None)

    assert provider.approve(proposal=_proposal(), policy=_policy(), objective="Submit") is True
