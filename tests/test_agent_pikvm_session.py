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
    TotpProvider,
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


class _TotpProvider:
    def __init__(self, codes: list[str]) -> None:
        self.codes = iter(codes)

    def current_code(self) -> str:
        return next(self.codes)


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
    provider = _TotpProvider(["111111", "222222"])
    supplied_codes: list[str | None] = []

    def factory(selected_provider: TotpProvider | None) -> PiKVMClient:
        supplied_codes.append(
            selected_provider.current_code() if selected_provider is not None else None
        )
        return cast(PiKVMClient, next(clients))

    with PiKVMSession(
        _settings(),
        totp_provider=provider,
        client_factory=factory,
    ) as session:
        screenshot = session.get_screenshot()

    assert screenshot.content == b"jpeg"
    assert supplied_codes == ["111111", "222222"]
    assert first.screenshot_calls == 1
    assert second.screenshot_calls == 1
    assert session.reauthentication_count == 1
    assert first.closed is True
    assert second.closed is True


def test_ambiguous_hid_failure_is_never_retried_or_reauthenticated() -> None:
    client = _Client()
    supplied_codes: list[str | None] = []

    provider = _TotpProvider(["111111"])

    def factory(selected_provider: TotpProvider | None) -> PiKVMClient:
        supplied_codes.append(
            selected_provider.current_code() if selected_provider is not None else None
        )
        return cast(PiKVMClient, client)

    with (
        PiKVMSession(
            _settings(),
            totp_provider=provider,
            client_factory=factory,
        ) as session,
        pytest.raises(PiKVMTimeoutError),
    ):
        session.press_key("Escape")

    assert client.key_calls == 1
    assert supplied_codes == ["111111"]
    assert session.reauthentication_count == 0


def test_repeated_read_authentication_failure_suggests_clock_check() -> None:
    first = _Client(screenshot_error=PiKVMAuthenticationError("expired"))
    second = _Client(screenshot_error=PiKVMAuthenticationError("still rejected"))
    clients = iter([first, second])
    provider = _TotpProvider(["111111", "222222"])
    supplied_codes: list[str] = []

    def factory(selected_provider: TotpProvider | None) -> PiKVMClient:
        assert selected_provider is not None
        supplied_codes.append(selected_provider.current_code())
        return cast(PiKVMClient, next(clients))

    with (
        PiKVMSession(
            _settings(),
            totp_provider=provider,
            client_factory=factory,
        ) as session,
        pytest.raises(PiKVMAuthenticationError, match="system clock"),
    ):
        session.get_screenshot()

    assert supplied_codes == ["111111", "222222"]
    assert session.reauthentication_count == 1
