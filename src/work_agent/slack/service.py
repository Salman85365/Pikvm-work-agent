from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.logging import AvailabilityLogger
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult


class AvailabilityOperator(Protocol):
    def execute(self, kvm: str, desired: Availability | None) -> AvailabilityResult: ...


class SlackAvailabilityService:
    def __init__(self, operator: AvailabilityOperator, logger: AvailabilityLogger) -> None:
        self._operator = operator
        self._logger = logger

    def run(
        self,
        kvms: tuple[str, ...],
        desired: Availability | None,
    ) -> AvailabilityBatchResult:
        results: list[AvailabilityResult] = []
        for kvm in kvms:
            try:
                result = self._operator.execute(kvm, desired)
            except SlackAvailabilityError as exc:
                result = AvailabilityResult(
                    kvm=kvm,
                    desired=desired,
                    observed=None,
                    changed=None,
                    success=False,
                    error=str(exc),
                )
            except Exception:
                result = AvailabilityResult(
                    kvm=kvm,
                    desired=desired,
                    observed=None,
                    changed=None,
                    success=False,
                    error="An unexpected local error stopped this KVM workflow.",
                )
            try:
                self._logger.record(result)
            except SlackAvailabilityError as exc:
                result = replace(result, log_error=str(exc))
            except Exception:
                result = replace(
                    result,
                    log_error="An unexpected local error prevented availability logging.",
                )
            results.append(result)
        return AvailabilityBatchResult(results=tuple(results))
