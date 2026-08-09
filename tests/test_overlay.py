from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from work_agent.vision import (
    AnalysisUsage,
    BoundingBox,
    ImageDetail,
    NormalizedPoint,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
    save_analysis_overlay,
)


def _image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (101, 101), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _analysis() -> ScreenAnalysis:
    target = UIElement(
        id="target",
        label="Profile",
        role=UIElementRole.BUTTON,
        visible_text="",
        bounding_box=BoundingBox(x1=100, y1=100, x2=300, y2=300),
        click_point=NormalizedPoint(x=200, y=200),
        confidence=0.95,
    )
    return ScreenAnalysis(
        objective="Locate profile",
        application="Slack",
        screen_state=ScreenState.APPLICATION,
        summary="Slack is visible.",
        target_found=True,
        target=target,
        relevant_elements=[target],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.95,
        screenshot_width=101,
        screenshot_height=101,
        requested_model="model",
        model="model",
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
        attempted_models=["model"],
    )


def test_overlay_draws_target_locally(tmp_path: Path) -> None:
    output = tmp_path / "overlay.png"

    saved = save_analysis_overlay(_image_bytes(), _analysis(), output)

    assert saved == output
    with Image.open(output) as image:
        assert image.size == (101, 101)
        assert image.getpixel((10, 10)) == (255, 60, 60)
