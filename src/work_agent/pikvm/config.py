from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from work_agent.pikvm.errors import PiKVMConfigurationError


def _parse_bool(name: str, raw_value: str) -> bool:
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise PiKVMConfigurationError(f"{name} must be one of true/false, yes/no, on/off, or 1/0.")


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise PiKVMConfigurationError(f"{name} must be a number.") from exc


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise PiKVMConfigurationError(f"{name} must be an integer.") from exc


@dataclass(frozen=True, slots=True)
class PiKVMSettings:
    """Connection settings loaded locally on the controlling Mac."""

    base_url: str
    username: str
    password: str = field(repr=False)
    totp_required: bool = True
    verify_ssl: bool = True
    connect_timeout: float = 5.0
    request_timeout: float = 15.0
    max_retries: int = 2
    retry_backoff: float = 0.25
    keymap: str | None = "en-us"

    def __post_init__(self) -> None:
        raw_url = self.base_url.strip()
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PiKVMConfigurationError("PIKVM_URL must be an absolute http:// or https:// URL.")
        if parsed.username is not None or parsed.password is not None:
            raise PiKVMConfigurationError(
                "Do not place credentials in PIKVM_URL; use PIKVM_USERNAME and PIKVM_PASSWORD."
            )
        if parsed.query or parsed.fragment:
            raise PiKVMConfigurationError("PIKVM_URL cannot contain a query string or fragment.")

        normalized_path = parsed.path.rstrip("/")
        normalized = urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))
        object.__setattr__(self, "base_url", normalized)

        if not self.username.strip():
            raise PiKVMConfigurationError("PIKVM_USERNAME is required.")
        if not self.password:
            raise PiKVMConfigurationError("PIKVM_PASSWORD is required.")
        if self.connect_timeout <= 0 or self.request_timeout <= 0:
            raise PiKVMConfigurationError("PiKVM timeouts must be greater than zero.")
        if self.max_retries < 0:
            raise PiKVMConfigurationError("PIKVM_MAX_RETRIES cannot be negative.")
        if self.retry_backoff < 0:
            raise PiKVMConfigurationError("PIKVM_RETRY_BACKOFF cannot be negative.")
        if self.keymap is not None and not self.keymap.strip():
            object.__setattr__(self, "keymap", None)

    @classmethod
    def from_env(cls) -> PiKVMSettings:
        """Load settings from environment variables and an optional local .env file."""

        load_dotenv()

        required = {
            "PIKVM_URL": os.getenv("PIKVM_URL"),
            "PIKVM_USERNAME": os.getenv("PIKVM_USERNAME"),
            "PIKVM_PASSWORD": os.getenv("PIKVM_PASSWORD"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise PiKVMConfigurationError(
                "Missing required setting(s): " + ", ".join(sorted(missing))
            )

        verify_ssl = _parse_bool("PIKVM_VERIFY_SSL", os.getenv("PIKVM_VERIFY_SSL", "true"))
        return cls(
            base_url=required["PIKVM_URL"] or "",
            username=required["PIKVM_USERNAME"] or "",
            password=required["PIKVM_PASSWORD"] or "",
            totp_required=_parse_bool(
                "PIKVM_TOTP_REQUIRED", os.getenv("PIKVM_TOTP_REQUIRED", "true")
            ),
            verify_ssl=verify_ssl,
            connect_timeout=_env_float("PIKVM_CONNECT_TIMEOUT", 5.0),
            request_timeout=_env_float("PIKVM_REQUEST_TIMEOUT", 15.0),
            max_retries=_env_int("PIKVM_MAX_RETRIES", 2),
            retry_backoff=_env_float("PIKVM_RETRY_BACKOFF", 0.25),
            keymap=os.getenv("PIKVM_KEYMAP", "en-us"),
        )
