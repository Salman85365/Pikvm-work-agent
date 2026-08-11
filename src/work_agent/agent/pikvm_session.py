from __future__ import annotations

from collections.abc import Callable

from work_agent.pikvm import (
    MouseButton,
    PiKVMAuthenticationError,
    PiKVMClient,
    PiKVMSettings,
    Screenshot,
    ScreenSize,
)
from work_agent.pikvm.totp import TotpProvider


class PiKVMSession:
    """Refresh read-only authentication without ever replaying an HID operation."""

    def __init__(
        self,
        settings: PiKVMSettings,
        *,
        totp_provider: TotpProvider,
        client_factory: Callable[[TotpProvider | None], PiKVMClient] | None = None,
    ) -> None:
        self._settings = settings
        self._totp_provider = totp_provider
        self._client_factory = client_factory or (
            lambda provider: PiKVMClient(settings, totp_provider=provider)
        )
        self._reauthentication_count = 0
        self._client = self._new_client()

    def __enter__(self) -> PiKVMSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def reauthentication_count(self) -> int:
        return self._reauthentication_count

    def get_screenshot(self) -> Screenshot:
        try:
            return self._client.get_screenshot()
        except PiKVMAuthenticationError:
            self._refresh_client()
            try:
                return self._client.get_screenshot()
            except PiKVMAuthenticationError:
                raise PiKVMAuthenticationError(
                    "PiKVM rejected the credentials after a fresh TOTP was generated. "
                    "Check the username/password and, if they are correct, verify the Mac's "
                    "system clock."
                ) from None

    def press_key(self, key: str) -> None:
        self._client.press_key(key)

    def hotkey(self, *keys: str) -> None:
        self._client.hotkey(*keys)

    def type_text(
        self,
        text: str,
        *,
        keymap: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self._client.type_text(text, keymap=keymap, delay=delay)

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        self._client.move_mouse(x, y, screen_size=screen_size)

    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
    ) -> None:
        self._client.click(x, y, screen_size=screen_size, button=button)

    def double_click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        screen_size: ScreenSize | None = None,
        button: MouseButton = MouseButton.LEFT,
        interval: float = 0.1,
    ) -> None:
        self._client.double_click(
            x,
            y,
            screen_size=screen_size,
            button=button,
            interval=interval,
        )

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        self._client.scroll(delta_y, delta_x=delta_x)

    def _new_client(self) -> PiKVMClient:
        provider = self._totp_provider if self._settings.totp_required else None
        return self._client_factory(provider)

    def _refresh_client(self) -> None:
        self._client.close()
        self._reauthentication_count += 1
        self._client = self._new_client()
