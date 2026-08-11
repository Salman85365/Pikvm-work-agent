from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import math
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import parse_qs, urlsplit, urlunsplit

import keyring
import keyring.errors
import pyotp

from work_agent.pikvm.config import PiKVMSettings, TotpProviderKind
from work_agent.pikvm.errors import (
    PiKVMConfigurationError,
    PiKVMKeychainError,
    PiKVMKeychainMissingError,
    PiKVMTotpError,
    PiKVMTotpSecretError,
)

_TOTP_DIGITS = 6
_TOTP_INTERVAL_SECONDS = 30
_TOTP_EXPIRY_GUARD_SECONDS = 4.0
_TOTP_TRANSITION_BUFFER_SECONDS = 0.05
_BASE32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


class TotpProvider(Protocol):
    def current_code(self) -> str:
        """Return one current six-digit TOTP without retaining or displaying it."""


class SecretStore(Protocol):
    def get_secret(self, service: str, account: str) -> str | None: ...

    def set_secret(self, service: str, account: str, secret: str) -> None: ...

    def delete_secret(self, service: str, account: str) -> bool: ...


class KeyringSecretStore:
    """Narrow adapter around keyring's native platform backend."""

    def get_secret(self, service: str, account: str) -> str | None:
        try:
            return keyring.get_password(service, account)
        except keyring.errors.KeyringError:
            raise PiKVMKeychainError(
                f"macOS Keychain access was denied or unavailable for {account}."
            ) from None

    def set_secret(self, service: str, account: str, secret: str) -> None:
        try:
            keyring.set_password(service, account, secret)
        except keyring.errors.KeyringError:
            raise PiKVMKeychainError(
                f"macOS Keychain could not store the TOTP credential for {account}."
            ) from None

    def delete_secret(self, service: str, account: str) -> bool:
        if self.get_secret(service, account) is None:
            return False
        try:
            keyring.delete_password(service, account)
        except keyring.errors.KeyringError:
            raise PiKVMKeychainError(
                f"macOS Keychain could not remove the TOTP credential for {account}."
            ) from None
        return True


class InteractiveTotpProvider:
    def __init__(self, prompt: Callable[[str], str] | None = None) -> None:
        self._prompt = prompt

    def current_code(self) -> str:
        try:
            raw_code = (
                self._prompt("PiKVM 2FA code: ")
                if self._prompt is not None
                else getpass.getpass("PiKVM 2FA code: ")
            )
        except EOFError:
            raise PiKVMTotpError(
                "Could not read a 2FA code. Run this command in an interactive terminal."
            ) from None
        return validate_totp_code(raw_code)


class KeychainTotpProvider:
    def __init__(
        self,
        *,
        service: str,
        account: str,
        store: SecretStore | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._account = account
        self._store = store or KeyringSecretStore()
        self._clock = clock
        self._sleeper = sleeper

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self._service!r}, account={self._account!r})"

    def current_code(self) -> str:
        secret = self._store.get_secret(self._service, self._account)
        if secret is None:
            raise PiKVMKeychainMissingError(
                f"No TOTP credential is configured in macOS Keychain for {self._account}."
            )
        return generate_totp_code(secret, clock=self._clock, sleeper=self._sleeper)


class FallbackTotpProvider:
    def __init__(self, primary: TotpProvider, fallback: TotpProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    def current_code(self) -> str:
        try:
            return self._primary.current_code()
        except PiKVMTotpError:
            return self._fallback.current_code()


def build_totp_provider(
    settings: PiKVMSettings,
    *,
    store: SecretStore | None = None,
    interactive_prompt: Callable[[str], str] | None = None,
) -> TotpProvider:
    interactive = InteractiveTotpProvider(interactive_prompt)
    if settings.totp_provider is TotpProviderKind.INTERACTIVE:
        return interactive

    account = normalize_pikvm_host(settings.base_url)
    keychain = KeychainTotpProvider(
        service=settings.totp_keychain_service,
        account=account,
        store=store,
    )
    if settings.totp_interactive_fallback:
        return FallbackTotpProvider(keychain, interactive)
    return keychain


def normalize_pikvm_host(endpoint: str) -> str:
    raw_endpoint = endpoint.strip()
    if not raw_endpoint:
        raise PiKVMConfigurationError("A PiKVM URL or host is required.")

    has_scheme = "://" in raw_endpoint
    parsed = urlsplit(raw_endpoint if has_scheme else f"//{raw_endpoint}")
    scheme = parsed.scheme.lower()
    if has_scheme and scheme not in {"http", "https"}:
        raise PiKVMConfigurationError("PiKVM URLs must use http:// or https://.")
    if parsed.username is not None or parsed.password is not None:
        raise PiKVMConfigurationError("Do not include credentials in a PiKVM URL or host.")
    hostname = parsed.hostname
    if hostname is None:
        raise PiKVMConfigurationError("A PiKVM URL or host must include a hostname.")
    hostname = hostname.lower()

    try:
        port = parsed.port
    except ValueError:
        raise PiKVMConfigurationError("The PiKVM URL or host contains an invalid port.") from None
    if port is not None and not 1 <= port <= 65535:
        raise PiKVMConfigurationError("The PiKVM port must be between 1 and 65535.")
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None

    host_identifier = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host_identifier += f":{port}"
    return host_identifier


def endpoint_url(endpoint: str, *, default_scheme: str = "https") -> str:
    raw_endpoint = endpoint.strip()
    has_scheme = "://" in raw_endpoint
    parsed = urlsplit(raw_endpoint if has_scheme else f"//{raw_endpoint}")
    scheme = parsed.scheme.lower() if has_scheme else default_scheme
    if scheme not in {"http", "https"}:
        raise PiKVMConfigurationError("PiKVM URLs must use http:// or https://.")
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, normalize_pikvm_host(raw_endpoint), path, "", ""))


def normalize_totp_seed(raw_secret: str) -> str:
    candidate = raw_secret.strip()
    if not candidate:
        raise PiKVMTotpSecretError("The TOTP setup secret is empty.")

    if candidate.lower().startswith("otpauth://"):
        try:
            otp = pyotp.parse_uri(candidate)
        except (TypeError, ValueError):
            raise PiKVMTotpSecretError(
                "The TOTP provisioning URI is malformed or unsupported."
            ) from None
        if not isinstance(otp, pyotp.TOTP):
            raise PiKVMTotpSecretError("HOTP provisioning URIs are not supported.")
        if (
            otp.digits != _TOTP_DIGITS
            or otp.interval != _TOTP_INTERVAL_SECONDS
            or otp.digest().name.lower() != hashlib.sha1().name
        ):
            raise PiKVMTotpSecretError(
                "Only six-digit, 30-second, SHA-1 TOTP credentials are supported."
            )
        candidate = otp.secret

    normalized = "".join(candidate.split()).rstrip("=").upper()
    if not normalized.isascii() or not normalized or not set(normalized) <= _BASE32_ALPHABET:
        raise PiKVMTotpSecretError("The TOTP setup secret is not valid Base32.")
    padded = normalized + "=" * ((8 - len(normalized) % 8) % 8)
    try:
        decoded = base64.b32decode(padded, casefold=True)
    except (binascii.Error, ValueError):
        raise PiKVMTotpSecretError("The TOTP setup secret is not valid Base32.") from None
    if not decoded:
        raise PiKVMTotpSecretError("The TOTP setup secret is not valid Base32.")
    return normalized


def normalize_totp_uri(raw_uri: str) -> str:
    candidate = raw_uri.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "otpauth":
        raise PiKVMTotpSecretError("The QR code does not contain a TOTP provisioning URI.")
    if parsed.netloc.lower() == "hotp":
        raise PiKVMTotpSecretError("HOTP provisioning URIs are not supported.")
    if parsed.netloc.lower() != "totp":
        raise PiKVMTotpSecretError("The QR code does not contain a TOTP provisioning URI.")
    secret_values = parse_qs(parsed.query, keep_blank_values=True).get("secret", [])
    if len(secret_values) != 1 or not secret_values[0].strip():
        raise PiKVMTotpSecretError(
            "The TOTP provisioning URI must contain exactly one non-empty secret."
        )
    return normalize_totp_seed(candidate)


def generate_totp_code(
    raw_secret: str,
    *,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    secret = normalize_totp_seed(raw_secret)
    otp = pyotp.TOTP(secret, digits=_TOTP_DIGITS, interval=_TOTP_INTERVAL_SECONDS)
    now = clock()
    if not math.isfinite(now) or now < 0:
        raise PiKVMTotpError("The local system clock could not be used for TOTP generation.")
    remaining = otp.interval - (now % otp.interval)
    if remaining < _TOTP_EXPIRY_GUARD_SECONDS:
        sleeper(remaining + _TOTP_TRANSITION_BUFFER_SECONDS)
        now = clock()
    try:
        code = otp.at(int(now))
    except (TypeError, ValueError):
        raise PiKVMTotpError("A current TOTP code could not be generated locally.") from None
    return validate_totp_code(code)


def validate_totp_code(raw_code: str) -> str:
    code = raw_code.strip()
    if len(code) != _TOTP_DIGITS or not code.isascii() or not code.isdigit():
        raise PiKVMConfigurationError("PiKVM 2FA code must be exactly six digits.")
    return code
