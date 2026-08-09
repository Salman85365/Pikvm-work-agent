from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from work_agent.agent.debug import DebugArtifacts
from work_agent.agent.models import zero_usage
from work_agent.pikvm import Screenshot, ScreenSize
from work_agent.vision import (
    ImageDetail,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
)


def _screenshot() -> Screenshot:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), "white").save(buffer, format="JPEG")
    return Screenshot(
        content=buffer.getvalue(),
        size=ScreenSize(64, 32),
        captured_at=datetime.now(UTC),
    )


def _analysis() -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Inspect",
        application="Desktop",
        screen_state=ScreenState.DESKTOP,
        summary="Desktop is visible.",
        target_found=False,
        target=None,
        relevant_elements=[],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.95,
        screenshot_width=64,
        screenshot_height=32,
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


def test_debug_artifacts_save_nothing_by_default(tmp_path: Path) -> None:
    debug = DebugArtifacts(None)

    debug.save_before(1, _screenshot(), _analysis())
    debug.save_after(1, _screenshot())

    assert debug.enabled is False
    assert list(tmp_path.iterdir()) == []


def test_explicit_debug_directory_saves_before_analysis_overlay_and_after(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"
    debug = DebugArtifacts(directory)

    debug.save_before(1, _screenshot(), _analysis())
    debug.save_after(1, _screenshot())

    assert (directory / "step-001-before.jpg").is_file()
    assert (directory / "step-001-analysis.json").is_file()
    assert (directory / "step-001-overlay.jpg").is_file()
    assert (directory / "step-001-after.jpg").is_file()
