from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image

from work_agent.agent.models import PressKeyAction, zero_usage
from work_agent.agent.screen_change import (
    GuardStatus,
    PreActionGuard,
    ScreenSettleDetector,
    changed_fraction,
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


def _popover_screenshot(*, size: tuple[int, int] = (1920, 1080)) -> Screenshot:
    """A dark desktop with a small, low-contrast panel.

    Sized and toned to reproduce the real nbc_kvm measurement: Slack's profile menu covers
    roughly 6% of a 1920x1080 screen and differs from its surroundings by only ~26 grey
    levels, which is why the whole-screen mean read 0.0046.
    """
    image = Image.new("RGB", size, "#202020")
    panel = Image.new("RGB", (300, 400), "#3a3a3a")
    image.paste(panel, (40, 560))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Screenshot(
        content=buffer.getvalue(),
        size=ScreenSize(*size),
        captured_at=datetime.now(UTC),
        media_type="image/png",
    )


def test_a_small_popover_is_invisible_to_the_whole_screen_mean() -> None:
    """Regression for the real nbc_kvm trace: the profile menu opened, but the settle
    detector reported changed=no with difference=0.0046 and burned the full timeout."""
    before = signature(_screenshot("#202020", size=(1920, 1080)).content)
    after = signature(_popover_screenshot().content)

    mean_change = difference(before, after)
    localized = changed_fraction(before, after)

    # The whole-screen mean stays under the 0.015 stable threshold, as observed on hardware.
    assert mean_change < 0.015
    # The localized signal clears the 0.004 threshold, so the change is still detected.
    assert localized >= 0.004


def test_settle_detects_a_small_popover_and_returns_before_the_timeout() -> None:
    frames = [_popover_screenshot(), _popover_screenshot(), _popover_screenshot()]
    ticks = iter(range(0, 400))
    detector = ScreenSettleDetector(
        poll_interval_seconds=0.1,
        timeout_seconds=5.0,
        stable_frames=2,
        stable_threshold=0.015,
        localized_change_threshold=0.004,
        sleeper=lambda _: None,
        clock=lambda: next(ticks) * 0.1,
    )

    result = detector.wait_for_settle(
        lambda: frames.pop(0),
        before=_screenshot("#202020", size=(1920, 1080)),
    )

    assert result.changed is True
    assert result.stable is True
    assert result.timed_out is False
    assert result.changed_fraction >= 0.004
    assert result.difference < 0.015


def test_settle_still_reports_no_change_when_nothing_moved() -> None:
    ticks = iter(range(0, 400))
    detector = ScreenSettleDetector(
        poll_interval_seconds=0.1,
        timeout_seconds=1.0,
        stable_frames=2,
        stable_threshold=0.015,
        localized_change_threshold=0.004,
        sleeper=lambda _: None,
        clock=lambda: next(ticks) * 0.1,
    )

    result = detector.wait_for_settle(
        lambda: _screenshot("#202020", size=(1920, 1080)),
        before=_screenshot("#202020", size=(1920, 1080)),
    )

    assert result.changed is False
    assert result.changed_fraction == 0.0


def test_localized_signal_does_not_loosen_the_stale_plan_guard() -> None:
    """The stale guard must stay on the insensitive whole-screen mean, or a popover
    opening between planning and acting would cancel every valid plan."""
    guard = PreActionGuard(material_change_threshold=0.06)

    result = guard.check(
        planned=_screenshot("#202020", size=(1920, 1080)),
        current=_popover_screenshot(),
        action=PressKeyAction(type="press_key", key="Escape"),
        screen=_analysis(width=1920, height=1080),
    )

    assert result.status is GuardStatus.ALLOW
