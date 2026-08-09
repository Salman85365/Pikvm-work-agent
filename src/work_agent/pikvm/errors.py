from __future__ import annotations


class PiKVMError(Exception):
    """Base error for failures that are safe to show to a CLI user."""


class PiKVMConfigurationError(PiKVMError):
    """Local PiKVM configuration is missing or invalid."""


class PiKVMConnectionError(PiKVMError):
    """The PiKVM endpoint could not be reached."""


class PiKVMTimeoutError(PiKVMError):
    """The PiKVM endpoint did not respond before the configured timeout."""


class PiKVMAuthenticationError(PiKVMError):
    """PiKVM rejected the configured credentials."""


class PiKVMResponseError(PiKVMError):
    """PiKVM returned an unsuccessful HTTP response."""

    def __init__(self, status_code: int, method: str, path: str) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        super().__init__(f"PiKVM returned HTTP {status_code} for {method} {path}.")


class PiKVMProtocolError(PiKVMError):
    """PiKVM returned a response that does not match its documented API."""
