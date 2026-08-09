from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from work_agent.agent.errors import DebugArtifactError
from work_agent.agent.models import ActionProposal, PolicyDecision
from work_agent.pikvm import Screenshot
from work_agent.vision import (
    ActionVerification,
    ScreenAnalysis,
    VisionError,
    save_analysis_overlay,
)


class DebugArtifacts:
    def __init__(self, directory: Path | None) -> None:
        self._directory = directory.expanduser() if directory is not None else None
        if self._directory is not None:
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DebugArtifactError("Could not create the requested debug directory.") from exc

    @property
    def enabled(self) -> bool:
        return self._directory is not None

    def save_before(
        self,
        step: int,
        screenshot: Screenshot,
        analysis: ScreenAnalysis,
    ) -> None:
        if self._directory is None:
            return
        self._write_bytes(step, "before.jpg", screenshot.content)
        self._write_model(step, "analysis.json", analysis)
        try:
            save_analysis_overlay(
                screenshot.content,
                analysis,
                self._path(step, "overlay.jpg"),
            )
        except (OSError, VisionError) as exc:
            raise DebugArtifactError("Could not save a debug overlay.") from exc

    def save_proposal(
        self,
        step: int,
        proposal: ActionProposal,
        policy: PolicyDecision,
    ) -> None:
        if self._directory is None:
            return
        self._write_model(step, "proposal.json", proposal)
        self._write_model(step, "policy.json", policy)

    def save_after(self, step: int, screenshot: Screenshot) -> None:
        if self._directory is not None:
            self._write_bytes(step, "after.jpg", screenshot.content)

    def save_verification(self, step: int, verification: ActionVerification) -> None:
        if self._directory is not None:
            self._write_model(step, "verification.json", verification)

    def _write_model(self, step: int, suffix: str, model: BaseModel) -> None:
        try:
            self._path(step, suffix).write_text(model.model_dump_json(indent=2), encoding="utf-8")
        except OSError as exc:
            raise DebugArtifactError("Could not write a requested debug artifact.") from exc

    def _write_bytes(self, step: int, suffix: str, content: bytes) -> None:
        try:
            self._path(step, suffix).write_bytes(content)
        except OSError as exc:
            raise DebugArtifactError("Could not write a requested debug screenshot.") from exc

    def _path(self, step: int, suffix: str) -> Path:
        if self._directory is None:
            raise AssertionError("Debug directory is disabled.")
        return self._directory / f"step-{step:03d}-{suffix}"
