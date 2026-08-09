from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from work_agent.pikvm.config import PiKVMSettings
from work_agent.pikvm.errors import (
    PiKVMAuthenticationError,
    PiKVMConfigurationError,
    PiKVMConnectionError,
    PiKVMProtocolError,
    PiKVMResponseError,
    PiKVMTimeoutError,
)
from work_agent.pikvm.models import MouseButton, Screenshot, ScreenSize

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_HID_MIN = -32768
_HID_MAX = 32767
_UNCERTAIN_HID_OUTCOME = (
    " The HID outcome is uncertain; verify the screen state before deciding whether to retry."
)


class PiKVMClient:
    """Typed synchronous client for the documented PiKVM HTTP API."""

    def __init__(
        self,
        settings: PiKVMSettings,
        *,
        totp_code: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleeper = sleeper
        timeout = httpx.Timeout(
            settings.request_timeout,
            connect=settings.connect_timeout,
        )
        password = settings.password
        if totp_code is not None:
            normalized_totp = totp_code.strip()
            if (
                len(normalized_totp) != 6
                or not normalized_totp.isascii()
                or not normalized_totp.isdigit()
            ):
                raise PiKVMConfigurationError("PiKVM 2FA code must be exactly six digits.")
            password += normalized_totp

        self._http = httpx.Client(
            base_url=f"{settings.base_url}/",
            headers={
                "X-KVMD-User": settings.username,
                # PiKVM's documented TOTP flow appends the current code to the password.
                "X-KVMD-Passwd": password,
                "User-Agent": "pikvm-work-agent/0.1.1",
            },
            timeout=timeout,
            verify=settings.verify_ssl,
            transport=transport,
            follow_redirects=False,
        )

    def __enter__(self) -> PiKVMClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get_screenshot(self) -> Screenshot:
        """Capture a transient JPEG frame; PiKVM does not retain it server-side."""

        response = self._request(
            "GET",
            "api/streamer/snapshot",
            params={"save": "0"},
            retryable=True,
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "image/jpeg":
            raise PiKVMProtocolError(
                "PiKVM snapshot response was not a JPEG image "
                f"(content type: {content_type or 'missing'})."
            )
        if not response.content:
            raise PiKVMProtocolError("PiKVM returned an empty screenshot.")

        try:
            with Image.open(BytesIO(response.content)) as image:
                size = ScreenSize(width=image.width, height=image.height)
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise PiKVMProtocolError("PiKVM returned an invalid JPEG screenshot.") from exc

        return Screenshot(
            content=response.content,
            size=size,
            captured_at=datetime.now(UTC),
            media_type=content_type,
        )

    def press_key(self, key: str) -> None:
        """Press and release one PiKVM web key, such as Enter or Escape."""

        self._post_hid_action("api/hid/events/send_key", params={"key": _validate_key(key)})

    def hotkey(self, *keys: str) -> None:
        """Press keys in order and release them in reverse order."""

        if not keys:
            raise ValueError("A hotkey requires at least one key.")
        validated = [_validate_key(key) for key in keys]
        self._post_hid_action(
            "api/hid/events/send_shortcut",
            params={"keys": ",".join(validated)},
        )

    def type_text(
        self,
        text: str,
        *,
        keymap: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """Type text using PiKVM's configured keyboard mapping."""

        if not text:
            raise ValueError("Text cannot be empty.")
        if not 0 <= delay <= 5:
            raise ValueError("Typing delay must be between 0 and 5 seconds.")

        selected_keymap = keymap if keymap is not None else self._settings.keymap
        params: dict[str, Any] = {"limit": 0, "delay": delay}
        if selected_keymap:
            params["keymap"] = selected_keymap
        self._post_hid_action(
            "api/hid/print",
            params=params,
            content=text.encode("utf-8"),
        )

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        """Move the absolute mouse to screenshot pixel coordinates."""

        hid_x = _pixel_to_hid(x, screen_size.width, axis="x")
        hid_y = _pixel_to_hid(y, screen_size.height, axis="y")
        self._post_hid_action(
            "api/hid/events/send_mouse_move",
            params={"to_x": hid_x, "to_y": hid_y},
        )

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
    ) -> None:
        """Optionally move to a screenshot pixel and click once."""

        self._move_if_requested(x, y, screen_size)
        self._post_hid_action(
            "api/hid/events/send_mouse_button",
            params={"button": button.value},
        )

    def double_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
        interval: float = 0.1,
    ) -> None:
        """Optionally move to a screenshot pixel and click twice."""

        if not 0 <= interval <= 1:
            raise ValueError("Double-click interval must be between 0 and 1 second.")
        self._move_if_requested(x, y, screen_size)
        for index in range(2):
            self._post_hid_action(
                "api/hid/events/send_mouse_button",
                params={"button": button.value},
            )
            if index == 0 and interval:
                self._sleeper(interval)

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        """Send a vertical and optional horizontal mouse-wheel delta."""

        if delta_x == 0 and delta_y == 0:
            raise ValueError("At least one scroll delta must be non-zero.")
        self._post_hid_action(
            "api/hid/events/send_mouse_wheel",
            params={"delta_x": delta_x, "delta_y": delta_y},
        )

    def _move_if_requested(
        self,
        x: int | None,
        y: int | None,
        screen_size: ScreenSize | None,
    ) -> None:
        coordinate_values = (x, y, screen_size)
        if all(value is None for value in coordinate_values):
            return
        if x is None or y is None or screen_size is None:
            raise ValueError("x, y, and screen_size must be provided together.")
        self.move_mouse(x, y, screen_size=screen_size)

    def _post_hid_action(
        self,
        path: str,
        *,
        params: dict[str, Any],
        content: bytes | None = None,
    ) -> None:
        # Mutating HID calls are never retried: a timeout may occur after PiKVM acted.
        response = self._request(
            "POST",
            path,
            params=params,
            content=content,
            retryable=False,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PiKVMProtocolError(
                "PiKVM HID response was not valid JSON." + _UNCERTAIN_HID_OUTCOME
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise PiKVMProtocolError(
                "PiKVM HID response did not report success." + _UNCERTAIN_HID_OUTCOME
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        retryable: bool,
    ) -> httpx.Response:
        attempts = self._settings.max_retries + 1 if retryable else 1

        for attempt in range(attempts):
            try:
                response = self._http.request(method, path, params=params, content=content)
            except httpx.TimeoutException as exc:
                if attempt + 1 < attempts:
                    self._wait_before_retry(attempt)
                    continue
                message = f"PiKVM timed out during {method} /{path}."
                if not retryable:
                    message += _UNCERTAIN_HID_OUTCOME
                raise PiKVMTimeoutError(message) from exc
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    self._wait_before_retry(attempt)
                    continue
                message = f"Could not reach PiKVM during {method} /{path}."
                if not retryable:
                    message += _UNCERTAIN_HID_OUTCOME
                raise PiKVMConnectionError(message) from exc

            if response.status_code in {401, 403}:
                raise PiKVMAuthenticationError(
                    "PiKVM rejected the configured username/password or requires "
                    "a current 2FA code."
                )
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                self._wait_before_retry(attempt)
                continue
            if response.is_error:
                raise PiKVMResponseError(
                    response.status_code,
                    method,
                    f"/{path}",
                    outcome_uncertain=not retryable,
                )
            return response

        raise AssertionError("PiKVM request retry loop exited unexpectedly.")

    def _wait_before_retry(self, attempt: int) -> None:
        delay = self._settings.retry_backoff * (2**attempt)
        if delay:
            self._sleeper(delay)


def _validate_key(key: str) -> str:
    normalized = key.strip()
    if not normalized or "," in normalized or any(character.isspace() for character in normalized):
        raise ValueError(
            "PiKVM key names must be non-empty and cannot contain commas or whitespace."
        )
    return normalized


def _pixel_to_hid(pixel: int, dimension: int, *, axis: str) -> int:
    if not 0 <= pixel < dimension:
        raise ValueError(f"Mouse {axis} coordinate {pixel} is outside 0..{dimension - 1}.")
    ratio = pixel / (dimension - 1)
    return round(_HID_MIN + ratio * (_HID_MAX - _HID_MIN))
