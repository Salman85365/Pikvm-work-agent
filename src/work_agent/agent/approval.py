from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from work_agent.agent.models import ActionProposal, PolicyDecision, action_summary


class ApprovalProvider(Protocol):
    def approve(
        self,
        *,
        proposal: ActionProposal,
        policy: PolicyDecision,
        objective: str,
    ) -> bool: ...

    def confirm_step(
        self,
        *,
        proposal: ActionProposal,
        policy: PolicyDecision,
    ) -> bool: ...


class TerminalApprovalProvider:
    def __init__(
        self,
        *,
        input_reader: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
    ) -> None:
        self._input = input_reader
        self._output = output

    def approve(
        self,
        *,
        proposal: ActionProposal,
        policy: PolicyDecision,
        objective: str,
    ) -> bool:
        self._output(f"Proposed action: {action_summary(proposal.action)}")
        self._output(f"Reason: {policy.reason}")
        self._output(f"Objective: {objective}")
        try:
            answer = self._input("Approve? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    def confirm_step(
        self,
        *,
        proposal: ActionProposal,
        policy: PolicyDecision,
    ) -> bool:
        self._output(f"Next action: {action_summary(proposal.action)}")
        self._output(f"Policy: {policy.decision.value} — {policy.reason}")
        try:
            answer = self._input("Press Enter to execute, or type q to stop: ").strip().lower()
        except EOFError:
            return False
        return answer == ""
