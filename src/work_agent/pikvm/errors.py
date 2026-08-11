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


class PiKVMTotpError(PiKVMError):
    """A local TOTP provider could not produce a safe current code."""


class PiKVMKeychainError(PiKVMTotpError):
    """macOS Keychain could not service a TOTP credential request."""


class PiKVMKeychainMissingError(PiKVMKeychainError):
    """No TOTP seed exists for the exact requested PiKVM account."""


class PiKVMTotpSecretError(PiKVMTotpError):
    """A supplied or stored TOTP seed is malformed or unsupported."""


class PiKVMQrError(PiKVMTotpSecretError):
    """A local TOTP QR image could not be decoded or validated safely."""


class PiKVMResponseError(PiKVMError):
    """PiKVM returned an unsuccessful HTTP response."""

    def __init__(
        self,
        status_code: int,
        method: str,
        path: str,
        *,
        outcome_uncertain: bool = False,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        self.outcome_uncertain = outcome_uncertain
        message = f"PiKVM returned HTTP {status_code} for {method} {path}."
        if outcome_uncertain:
            message += (
                " The HID outcome is uncertain; verify the screen state before deciding whether "
                "to retry."
            )
        super().__init__(message)


class PiKVMProtocolError(PiKVMError):
    """PiKVM returned a response that does not match its documented API."""
