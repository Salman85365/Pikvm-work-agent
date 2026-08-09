from __future__ import annotations

from typing import Protocol

from work_agent.vision.models import (
    AnalysisOptions,
    ObservationContext,
    ScreenAnalysis,
    ScreenObservation,
)


class ScreenAnalyzer(Protocol):
    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenAnalysis: ...

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenObservation: ...
