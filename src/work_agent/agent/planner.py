from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from work_agent.agent.models import AgentStepSummary, ExecutionResult, PlanningResult
from work_agent.vision.models import ActionVerification, ScreenAnalysis


class ActionPlanner(Protocol):
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
    ) -> PlanningResult: ...
