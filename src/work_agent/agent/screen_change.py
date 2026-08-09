from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError

from work_agent.agent.errors import ScreenChangeError
from work_agent.agent.models import (
    Action,
    ClickElementAction,
    DoubleClickElementAction,
    MoveMouseAction,
)
from work_agent.pikvm import Screenshot
from work_agent.vision import ScreenAnalysis

_COMPARE_SIZE = (64, 36)


@dataclass(frozen=True, slots=True)
class ScreenSignature:
    width: int
    height: int
    fingerprint: str
    grayscale: bytes


class GuardStatus(StrEnum):
    ALLOW = "allow"
    STALE = "stale"
    RESOLUTION_CHANGED = "resolution_changed"
    INVALID_TARGET = "invalid_target"


@dataclass(frozen=True, slots=True)
class GuardResult:
    status: GuardStatus
    reason: str
    difference: float


@dataclass(frozen=True, slots=True)
class SettleResult:
    screenshot: Screenshot
    changed: bool
    stable: bool
    timed_out: bool
    polls: int
    elapsed_seconds: float
    difference: float


def signature(content: bytes) -> ScreenSignature:
    try:
        with Image.open(BytesIO(content)) as source:
            width, height = source.size
            grayscale = source.convert("L").resize(_COMPARE_SIZE, Image.Resampling.BILINEAR)
            pixels = grayscale.tobytes()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ScreenChangeError("A screenshot could not be decoded for local comparison.") from exc
    return ScreenSignature(
        width=width,
        height=height,
        fingerprint=hashlib.sha256(pixels).hexdigest(),
        grayscale=pixels,
    )


def difference(first: ScreenSignature, second: ScreenSignature) -> float:
    if (first.width, first.height) != (second.width, second.height):
        return 1.0
    first_image = Image.frombytes("L", _COMPARE_SIZE, first.grayscale)
    second_image = Image.frombytes("L", _COMPARE_SIZE, second.grayscale)
    value = ImageStat.Stat(ImageChops.difference(first_image, second_image)).mean[0]
    return value / 255.0


class PreActionGuard:
    def __init__(self, *, material_change_threshold: float = 0.06) -> None:
        if not 0.0 < material_change_threshold <= 1.0:
            raise ValueError("material change threshold must be greater than zero and at most one")
        self._threshold = material_change_threshold

    def check(
        self,
        *,
        planned: Screenshot,
        current: Screenshot,
        action: Action,
        screen: ScreenAnalysis,
    ) -> GuardResult:
        planned_size = (planned.size.width, planned.size.height)
        current_size = (current.size.width, current.size.height)
        analysis_size = (screen.screenshot_width, screen.screenshot_height)
        if current_size != planned_size or analysis_size != planned_size:
            return GuardResult(
                status=GuardStatus.RESOLUTION_CHANGED,
                reason="Screen dimensions changed after planning.",
                difference=1.0,
            )
        if isinstance(action, (MoveMouseAction, ClickElementAction, DoubleClickElementAction)):
            element = next(
                (
                    element
                    for element in ([screen.target] if screen.target is not None else [])
                    + screen.relevant_elements
                    if element.id == action.element_id
                ),
                None,
            )
            if element is None or element.click_point is None:
                return GuardResult(
                    status=GuardStatus.INVALID_TARGET,
                    reason="The coordinate-dependent target is no longer valid.",
                    difference=1.0,
                )
        screen_difference = difference(signature(planned.content), signature(current.content))
        if screen_difference >= self._threshold:
            return GuardResult(
                status=GuardStatus.STALE,
                reason="The screen materially changed after planning.",
                difference=screen_difference,
            )
        return GuardResult(
            status=GuardStatus.ALLOW,
            reason="The fresh screen is sufficiently close to the planned screen.",
            difference=screen_difference,
        )


class ScreenSettleDetector:
    def __init__(
        self,
        *,
        poll_interval_seconds: float,
        timeout_seconds: float,
        stable_frames: int,
        stable_threshold: float,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._stable_frames = stable_frames
        self._stable_threshold = stable_threshold
        self._sleeper = sleeper
        self._clock = clock

    def wait_for_settle(
        self,
        capture: Callable[[], Screenshot],
        *,
        before: Screenshot,
    ) -> SettleResult:
        started = self._clock()
        baseline = signature(before.content)
        previous = baseline
        latest = before
        latest_difference = 0.0
        changed = False
        stable_count = 0
        polls = 0

        while self._clock() - started < self._timeout:
            self._sleeper(self._poll_interval)
            latest = capture()
            polls += 1
            current = signature(latest.content)
            latest_difference = difference(baseline, current)
            frame_difference = difference(previous, current)
            if latest_difference >= self._stable_threshold:
                changed = True
            if changed and frame_difference <= self._stable_threshold:
                stable_count += 1
                if stable_count >= self._stable_frames:
                    return SettleResult(
                        screenshot=latest,
                        changed=True,
                        stable=True,
                        timed_out=False,
                        polls=polls,
                        elapsed_seconds=max(0.0, self._clock() - started),
                        difference=latest_difference,
                    )
            else:
                stable_count = 0
            previous = current

        return SettleResult(
            screenshot=latest,
            changed=changed,
            stable=False,
            timed_out=True,
            polls=polls,
            elapsed_seconds=max(0.0, self._clock() - started),
            difference=latest_difference,
        )
