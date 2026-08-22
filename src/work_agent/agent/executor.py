from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from work_agent.agent.errors import ExecutionError
from work_agent.agent.models import (
    Action,
    ActionProposal,
    ClickElementAction,
    DoubleClickElementAction,
    ExecutionResult,
    ExecutionTransportStatus,
    FinishAction,
    HotkeyAction,
    MoveMouseAction,
    PressKeyAction,
    RequestUserAction,
    ScrollAction,
    ScrollDirection,
    TypeTextAction,
    WaitAction,
)
from work_agent.pikvm import (
    MouseButton,
    PiKVMAuthenticationError,
    PiKVMConnectionError,
    PiKVMError,
    PiKVMProtocolError,
    PiKVMResponseError,
    PiKVMTimeoutError,
    ScreenSize,
)
from work_agent.vision import ScreenAnalysis, UIElement, normalized_to_pixel

_HOVER_SETTLE_SECONDS = 0.1


class HIDClient(Protocol):
    def press_key(self, key: str) -> None: ...

    def hotkey(self, *keys: str) -> None: ...

    def type_text(
        self,
        text: str,
        *,
        keymap: str | None = None,
        delay: float = 0.0,
    ) -> None: ...

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None: ...

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
    ) -> None: ...

    def double_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
        interval: float = 0.1,
    ) -> None: ...

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None: ...


class ActionExecutor:
    """Map one validated proposal to existing PiKVM methods without policy or OpenAI."""

    def __init__(
        self,
        client: HIDClient,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._sleeper = sleeper
        self._now = now or (lambda: datetime.now(UTC))

    def execute(self, proposal: ActionProposal, screen: ScreenAnalysis) -> ExecutionResult:
        action = proposal.action
        if isinstance(action, (FinishAction, RequestUserAction)):
            raise ExecutionError("Finish and request-user actions cannot be sent to HID.")

        started = self._now()
        try:
            if isinstance(action, WaitAction):
                self._sleeper(action.seconds)
            elif isinstance(action, PressKeyAction):
                self._client.press_key(action.key)
            elif isinstance(action, HotkeyAction):
                self._client.hotkey(*action.keys)
            elif isinstance(action, TypeTextAction):
                self._client.type_text(action.text)
            elif isinstance(action, MoveMouseAction):
                point, size = self._resolve(action.element_id, screen)
                self._client.move_mouse(point[0], point[1], screen_size=size)
            elif isinstance(action, ClickElementAction):
                point, size = self._resolve(action.element_id, screen)
                self._client.click(
                    point[0],
                    point[1],
                    screen_size=size,
                    button=MouseButton.LEFT,
                )
            elif isinstance(action, DoubleClickElementAction):
                point, size = self._resolve(action.element_id, screen)
                self._client.double_click(
                    point[0],
                    point[1],
                    screen_size=size,
                    button=MouseButton.LEFT,
                )
            elif isinstance(action, ScrollAction):
                if action.element_id is not None:
                    point, size = self._resolve(action.element_id, screen)
                    self._client.move_mouse(point[0], point[1], screen_size=size)
                    self._sleeper(_HOVER_SETTLE_SECONDS)
                multiplier = 1 if action.direction is ScrollDirection.UP else -1
                self._client.scroll(multiplier * action.amount * 120)
            else:
                raise ExecutionError("The action is outside the executor vocabulary.")
        except (PiKVMTimeoutError, PiKVMConnectionError, PiKVMProtocolError) as exc:
            return self._result(
                action,
                started,
                ExecutionTransportStatus.UNCERTAIN,
                type(exc).__name__,
                "The HID transport outcome is uncertain; the action was not retried.",
            )
        except PiKVMResponseError as exc:
            status = (
                ExecutionTransportStatus.UNCERTAIN
                if exc.outcome_uncertain
                else ExecutionTransportStatus.FAILED
            )
            return self._result(
                action,
                started,
                status,
                type(exc).__name__,
                "PiKVM rejected the HID request; the action was not retried.",
            )
        except PiKVMAuthenticationError as exc:
            return self._result(
                action,
                started,
                ExecutionTransportStatus.FAILED,
                type(exc).__name__,
                "PiKVM authentication expired before the HID action could be accepted.",
            )
        except (PiKVMError, ValueError, ExecutionError) as exc:
            return self._result(
                action,
                started,
                ExecutionTransportStatus.FAILED,
                type(exc).__name__,
                "The validated action could not be sent through PiKVM.",
            )

        return self._result(
            action,
            started,
            ExecutionTransportStatus.SENT,
            None,
            None,
        )

    def _resolve(
        self, element_id: str, screen: ScreenAnalysis
    ) -> tuple[tuple[int, int], ScreenSize]:
        element = self._element(screen, element_id)
        if element is None or element.click_point is None:
            raise ExecutionError(
                "The current screen does not contain the validated target element."
            )
        pixel = normalized_to_pixel(
            element.click_point,
            width=screen.screenshot_width,
            height=screen.screenshot_height,
        )
        return (
            (pixel.x, pixel.y),
            ScreenSize(width=screen.screenshot_width, height=screen.screenshot_height),
        )

    @staticmethod
    def _element(screen: ScreenAnalysis, element_id: str) -> UIElement | None:
        if screen.target is not None and screen.target.id == element_id:
            return screen.target
        return next(
            (element for element in screen.relevant_elements if element.id == element_id),
            None,
        )

    def _result(
        self,
        action: Action,
        started: datetime,
        status: ExecutionTransportStatus,
        error_code: str | None,
        error: str | None,
    ) -> ExecutionResult:
        return ExecutionResult(
            action=action,
            started_at=started,
            finished_at=self._now(),
            transport_status=status,
            hid_action=not isinstance(action, WaitAction),
            error_code=error_code,
            sanitized_error=error,
        )
