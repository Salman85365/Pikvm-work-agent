from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from PIL import Image

from work_agent.pikvm import (
    PiKVMAuthenticationError,
    PiKVMClient,
    PiKVMConfigurationError,
    PiKVMConnectionError,
    PiKVMProtocolError,
    PiKVMResponseError,
    PiKVMSettings,
    PiKVMTimeoutError,
    ScreenSize,
)


class _TotpProvider:
    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def current_code(self) -> str:
        self.calls += 1
        return self.code


def _jpeg(width: int = 16, height: int = 9) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color="navy").save(output, format="JPEG")
    return output.getvalue()


def _settings(**overrides: object) -> PiKVMSettings:
    values: dict[str, object] = {
        "base_url": "https://pikvm.example",
        "username": "operator",
        "password": "secret",
        "totp_required": False,
        "verify_ssl": False,
        "retry_backoff": 0,
    }
    values.update(overrides)
    return PiKVMSettings(**values)  # type: ignore[arg-type]


def test_get_screenshot_uses_auth_and_returns_typed_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/streamer/snapshot"
        assert request.url.params["save"] == "0"
        assert request.headers["X-KVMD-User"] == "operator"
        assert request.headers["X-KVMD-Passwd"] == "secret"
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg())

    with PiKVMClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        screenshot = client.get_screenshot()

    assert screenshot.size == ScreenSize(16, 9)
    assert screenshot.media_type == "image/jpeg"


def test_totp_code_is_appended_to_password_for_request() -> None:
    provider = _TotpProvider("123456")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-KVMD-Passwd"] == "secret123456"
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg())

    with PiKVMClient(
        _settings(totp_required=True),
        totp_provider=provider,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get_screenshot()

    assert provider.calls == 1


@pytest.mark.parametrize(
    "code",
    ["", "12345", "1234567", "12 456", "abcdef", "\uff11\uff12\uff13\uff14\uff15\uff16"],
)
def test_totp_code_must_be_six_ascii_digits(code: str) -> None:
    with pytest.raises(PiKVMConfigurationError, match="exactly six digits"):
        PiKVMClient(
            _settings(totp_required=True),
            totp_provider=_TotpProvider(code),
        )


def test_totp_provider_is_required_when_2fa_is_enabled() -> None:
    with pytest.raises(PiKVMConfigurationError, match="no TOTP provider"):
        PiKVMClient(_settings(totp_required=True))


def test_get_screenshot_retries_transient_read_failure() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg())

    with PiKVMClient(_settings(max_retries=1), transport=httpx.MockTransport(handler)) as client:
        client.get_screenshot()

    assert attempts == 2


def test_invalid_screenshot_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            content=b"not really a jpeg",
        )
    )

    with (
        PiKVMClient(_settings(), transport=transport) as client,
        pytest.raises(PiKVMProtocolError, match="invalid JPEG"),
    ):
        client.get_screenshot()


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_error_is_specific(status: int) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status))

    with (
        PiKVMClient(_settings(), transport=transport) as client,
        pytest.raises(PiKVMAuthenticationError),
    ):
        client.get_screenshot()


def test_press_key_is_not_retried_after_timeout() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("uncertain outcome", request=request)

    with (
        PiKVMClient(
            _settings(max_retries=5),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PiKVMTimeoutError) as error,
    ):
        client.press_key("Enter")

    assert attempts == 1
    assert "HID outcome is uncertain" in str(error.value)
    assert "verify the screen state" in str(error.value)


def test_hid_methods_use_documented_endpoints_and_pixel_mapping() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    with PiKVMClient(
        _settings(),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    ) as client:
        client.press_key("Enter")
        client.hotkey("ControlLeft", "KeyL")
        client.type_text("hello")
        client.move_mouse(0, 1079, screen_size=ScreenSize(1920, 1080))
        client.click(1919, 0, screen_size=ScreenSize(1920, 1080))
        client.double_click(interval=0)
        client.scroll(-120)

    assert [request.url.path for request in requests] == [
        "/api/hid/events/send_key",
        "/api/hid/events/send_shortcut",
        "/api/hid/print",
        "/api/hid/events/send_mouse_move",
        "/api/hid/events/send_mouse_move",
        "/api/hid/events/send_mouse_button",
        "/api/hid/events/send_mouse_button",
        "/api/hid/events/send_mouse_button",
        "/api/hid/events/send_mouse_wheel",
    ]
    assert requests[0].url.params["key"] == "Enter"
    assert requests[1].url.params["keys"] == "ControlLeft,KeyL"
    assert requests[2].content == b"hello"
    assert requests[3].url.params["to_x"] == "-32768"
    assert requests[3].url.params["to_y"] == "32767"
    assert requests[4].url.params["to_x"] == "32767"
    assert requests[4].url.params["to_y"] == "-32768"


def test_coordinate_click_waits_briefly_after_absolute_mouse_move() -> None:
    delays: list[float] = []
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True, "result": {}}))

    with PiKVMClient(
        _settings(mouse_move_settle_seconds=0.15),
        transport=transport,
        sleeper=delays.append,
    ) as client:
        client.click(100, 200, screen_size=ScreenSize(1920, 1080))
        client.click()

    assert delays == [0.15]


def test_mouse_coordinates_are_bounds_checked_before_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("No HTTP request should be sent")

    with (
        PiKVMClient(_settings(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="outside"),
    ):
        client.move_mouse(1920, 10, screen_size=ScreenSize(1920, 1080))


def test_non_retryable_http_error_is_wrapped() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(500))

    with (
        PiKVMClient(_settings(), transport=transport) as client,
        pytest.raises(PiKVMResponseError) as error,
    ):
        client.press_key("Enter")

    assert error.value.status_code == 500
    assert error.value.outcome_uncertain is True
    assert "HID outcome is uncertain" in str(error.value)


def test_invalid_hid_response_reports_uncertain_outcome() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"not json"))

    with (
        PiKVMClient(_settings(), transport=transport) as client,
        pytest.raises(PiKVMProtocolError, match="HID outcome is uncertain"),
    ):
        client.press_key("Enter")


def test_hid_connection_failure_reports_uncertain_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection lost after request", request=request)

    with (
        PiKVMClient(_settings(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(PiKVMConnectionError, match="HID outcome is uncertain"),
    ):
        client.press_key("Enter")


@pytest.mark.parametrize("key", ["Enter Key", "Enter,Key", "   "])
def test_invalid_key_is_rejected_before_request(key: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("No HTTP request should be sent")

    with (
        PiKVMClient(_settings(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="commas or whitespace"),
    ):
        client.press_key(key)
