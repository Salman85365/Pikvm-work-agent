from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image

from work_agent.agent.models import PressKeyAction, zero_usage
from work_agent.agent.screen_change import (
    GuardStatus,
    PreActionGuard,
    ScreenSettleDetector,
    difference,
    signature,
)
from work_agent.pikvm import Screenshot, ScreenSize
from work_agent.vision import (
    ImageDetail,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
)


def _screenshot(color: str, *, size: tuple[int, int] = (64, 32)) -> Screenshot:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return Screenshot(
        content=buffer.getvalue(),
        size=ScreenSize(*size),
        captured_at=datetime.now(UTC),
        media_type="image/png",
    )


def _analysis(*, width: int = 64, height: int = 32) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Navigate",
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
        screenshot_width=width,
        screenshot_height=height,
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


def test_local_fingerprint_difference_detects_material_change() -> None:
    white = signature(_screenshot("white").content)
    same = signature(_screenshot("white").content)
    black = signature(_screenshot("black").content)

    assert difference(white, same) == 0
    assert difference(white, black) > 0.9


def test_preaction_guard_allows_unchanged_screen() -> None:
    planned = _screenshot("white")
    current = _screenshot("white")

    result = PreActionGuard(material_change_threshold=0.05).check(
        planned=planned,
        current=current,
        action=PressKeyAction(type="press_key", key="Escape"),
        screen=_analysis(),
    )

    assert result.status is GuardStatus.ALLOW


def test_preaction_guard_cancels_changed_or_resized_screen() -> None:
    planned = _screenshot("white")
    guard = PreActionGuard(material_change_threshold=0.05)

    changed = guard.check(
        planned=planned,
        current=_screenshot("black"),
        action=PressKeyAction(type="press_key", key="Escape"),
        screen=_analysis(),
    )
    resized = guard.check(
        planned=planned,
        current=_screenshot("white", size=(80, 40)),
        action=PressKeyAction(type="press_key", key="Escape"),
        screen=_analysis(),
    )

    assert changed.status is GuardStatus.STALE
    assert resized.status is GuardStatus.RESOLUTION_CHANGED


class _Time:
    def __init__(self) -> None:
        self.value = 0.0

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_settle_detector_waits_for_change_and_stable_frame() -> None:
    timer = _Time()
    frames = iter([_screenshot("black"), _screenshot("black")])
    detector = ScreenSettleDetector(
        poll_interval_seconds=0.1,
        timeout_seconds=1,
        stable_frames=1,
        stable_threshold=0.01,
        sleeper=timer.sleep,
        clock=timer.clock,
    )

    result = detector.wait_for_settle(lambda: next(frames), before=_screenshot("white"))

    assert result.changed is True
    assert result.stable is True
    assert result.timed_out is False
    assert result.polls == 2


def test_settle_detector_times_out_when_screen_never_changes() -> None:
    timer = _Time()
    detector = ScreenSettleDetector(
        poll_interval_seconds=0.1,
        timeout_seconds=0.25,
        stable_frames=1,
        stable_threshold=0.01,
        sleeper=timer.sleep,
        clock=timer.clock,
    )

    result = detector.wait_for_settle(
        lambda: _screenshot("white"),
        before=_screenshot("white"),
    )

    assert result.changed is False
    assert result.stable is False
    assert result.timed_out is True
    assert result.polls == 3
