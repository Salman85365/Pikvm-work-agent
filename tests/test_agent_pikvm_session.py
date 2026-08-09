from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import (
    PiKVMAuthenticationError,
    PiKVMClient,
    PiKVMSettings,
    PiKVMTimeoutError,
    Screenshot,
    ScreenSize,
)


class _Client:
    def __init__(self, *, screenshot_error: BaseException | None = None) -> None:
        self.screenshot_error = screenshot_error
        self.screenshot_calls = 0
        self.key_calls = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def get_screenshot(self) -> Screenshot:
        self.screenshot_calls += 1
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return Screenshot(
            content=b"jpeg",
            size=ScreenSize(64, 32),
            captured_at=datetime.now(UTC),
        )

    def press_key(self, key: str) -> None:
        self.key_calls += 1
        raise PiKVMTimeoutError("ambiguous HID timeout")


def _settings() -> PiKVMSettings:
    return PiKVMSettings(
        base_url="https://pikvm.test",
        username="user",
        password="password",
        totp_required=True,
    )


def test_read_only_authentication_refresh_prompts_and_retries_once() -> None:
    first = _Client(screenshot_error=PiKVMAuthenticationError("expired"))
    second = _Client()
    clients = iter([first, second])
    codes = iter(["111111", "222222"])
    supplied_codes: list[str | None] = []

    def factory(code: str | None) -> PiKVMClient:
        supplied_codes.append(code)
        return cast(PiKVMClient, next(clients))

    with PiKVMSession(
        _settings(),
        totp_provider=lambda: next(codes),
        client_factory=factory,
    ) as session:
        screenshot = session.get_screenshot()

    assert screenshot.content == b"jpeg"
    assert supplied_codes == ["111111", "222222"]
    assert first.screenshot_calls == 1
    assert second.screenshot_calls == 1
    assert first.closed is True
    assert second.closed is True


def test_ambiguous_hid_failure_is_never_retried_or_reauthenticated() -> None:
    client = _Client()
    supplied_codes: list[str | None] = []

    def factory(code: str | None) -> PiKVMClient:
        supplied_codes.append(code)
        return cast(PiKVMClient, client)

    with (
        PiKVMSession(
            _settings(),
            totp_provider=lambda: "111111",
            client_factory=factory,
        ) as session,
        pytest.raises(PiKVMTimeoutError),
    ):
        session.press_key("Escape")

    assert client.key_calls == 1
    assert supplied_codes == ["111111"]
